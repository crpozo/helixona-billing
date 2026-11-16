"""The check flow, reviewed end to end (2026-11-16). Each class is one thing
that was wrong or wasteful and what it does now."""
import os
import unittest
from datetime import datetime
from unittest import mock

from src.checks import read_check as rc
from src.checks import run as run_mod
from src.checks.reconcile import near_number, reconcile, summarize
from src.eob import capture

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


class _Table:
    def __init__(self, items):
        self.items = {i['check_number']: dict(i) for i in items}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None):
        row = self.items.setdefault(Key['check_number'], {'check_number': Key['check_number']})
        names = ExpressionAttributeNames or {}
        for part in UpdateExpression.replace('SET ', '').split(', '):
            k, v = [x.strip() for x in part.split('=')]
            row[names.get(k, k)] = ExpressionAttributeValues[v]

    def delete_item(self, Key):
        self.items.pop(Key['check_number'], None)

    def scan(self, **kw):
        return {'Items': [dict(i) for i in self.items.values()]}


class _Aws:
    def __init__(self, table):
        self.dynamodb = mock.Mock()
        self.dynamodb.Table = lambda name: table


class AnUncashedCheckIsAskedAgain(unittest.TestCase):
    """A check read while it was only issued said 'not cashed' for ever: the
    walk skipped every check already on file."""
    NOW = datetime(2026, 11, 16, 12, 0, 0)

    def test_cashed_voided_and_stopped_are_final(self):
        for it in ({'check_status': 'Check Cashed'}, {'cashed_date': '10/01/2026'}, {'check_status': 'Voided'},
                   {'check_status': 'Stop Payment'}):
            self.assertFalse(capture.needs_refresh({**it, 'captured_at': '2026-09-01 10:00:00 UTC'}, self.NOW), it)

    def test_an_issued_check_is_asked_again_but_not_twice_a_day(self):
        issued = {'check_status': 'Check Issued', 'captured_at': '2026-10-01 10:00:00 UTC'}
        self.assertTrue(capture.needs_refresh(issued, self.NOW))
        self.assertFalse(capture.needs_refresh({**issued, 'status_checked_at': '2026-11-16 03:00:00 UTC'}, self.NOW))
        self.assertTrue(capture.needs_refresh({**issued, 'status_checked_at': '2026-11-15 03:00:00 UTC'}, self.NOW))
        self.assertTrue(capture.needs_refresh({'check_status': ''}, self.NOW), 'no date on file: ask')

    def test_the_refresh_writes_the_status_alone(self):
        table = mock.Mock()
        aws = mock.Mock()
        aws.dynamodb.Table.return_value = table
        page = mock.Mock()
        page.inner_text.return_value = 'x'
        summary = {'check_eft': '0031401901', 'check_amount': '257.09', 'check_status': 'Check Cashed',
                   'cashed_date': '11/02/2026', 'payee_name': 'HELIXONA INC'}
        with mock.patch.object(capture, '_open_check_link'), mock.patch.object(capture, '_wait_for'), \
                mock.patch.object(capture, 'parse_check_summary', return_value=summary), mock.patch.object(capture.time, 'sleep'):
            self.assertEqual(capture._refresh_status(page, aws, '31401901'), 'Check Cashed')
        kw = table.update_item.call_args.kwargs
        self.assertEqual(kw['Key'], {'check_eft': '31401901'})
        self.assertEqual(set(kw['ExpressionAttributeNames'].values()),
                         {'check_status', 'cashed_date', 'status_checked_at', 'payee_name'})
        table.put_item.assert_not_called()

    def test_another_checks_page_or_an_unread_one_changes_nothing(self):
        aws, page = mock.Mock(), mock.Mock()
        page.inner_text.return_value = 'x'
        with mock.patch.object(capture, '_open_check_link'), mock.patch.object(capture, '_wait_for'), mock.patch.object(capture.time, 'sleep'):
            with mock.patch.object(capture, 'parse_check_summary', return_value={'check_eft': '999999', 'check_status': 'Check Cashed'}):
                with self.assertRaises(RuntimeError):
                    capture._refresh_status(page, aws, '31401901')
            with mock.patch.object(capture, 'parse_check_summary', return_value={'check_eft': '31401901', 'check_status': '', 'cashed_date': ''}):
                self.assertEqual(capture._refresh_status(page, aws, '31401901'), '')
        aws.dynamodb.Table.return_value.update_item.assert_not_called()

    def test_a_full_walk_refreshes_and_a_capped_one_does_not(self):
        src = _read('src/eob/capture.py')
        self.assertIn("refresh = bool(body.get('refresh', not limit and not only))", src)
        self.assertIn('if done.get(ck) and not force and ck in stale:', src)


class AMisreadDigitIsOneCheckNotTwo(unittest.TestCase):
    def test_what_counts_as_one_misread_apart(self):
        self.assertTrue(near_number('31401901', '31401907'))      # one digit
        self.assertTrue(near_number('31401901', '31410901'))      # two neighbours swapped
        self.assertTrue(near_number('31401901', '3140190'))       # one dropped
        self.assertTrue(near_number('0031401901', '31401981'))    # zeros do not count
        self.assertFalse(near_number('31401901', '31401901'))
        self.assertFalse(near_number('31401901', '31401877'))

    def _rows(self, copy_no, copy_amt):
        rows, _ = reconcile([{'check_number': copy_no, 'amount': copy_amt, 'file': 'a.pdf'}],
                            [{'check_eft': '31401901', 'check_amount': '257.09', 'check_status': 'Check Cashed', 'check_date': '10/20/2026'}],
                            [], ecw_checked=True)
        return {r['check_number']: r for r in rows}

    def test_same_amount_one_digit_apart_flags_both(self):
        by = self._rows('31401907', '257.09')
        self.assertIn('possible misread of 31401901', by['31401907']['flags'])
        self.assertIn('possible misread: scan filed as 31401907', by['31401901']['flags'])
        self.assertEqual(summarize(by.values())['possible_misread'], 2)

    def test_a_different_amount_or_a_distant_number_is_left_alone(self):
        for no, amt in (('31401907', '10.00'), ('88812345', '257.09')):
            by = self._rows(no, amt)
            self.assertFalse(any(f.startswith('possible misread') for r in by.values() for f in r['flags']), (no, amt))

    def test_two_candidates_is_no_guess(self):
        rows, _ = reconcile([{'check_number': '31401907', 'amount': '257.09', 'file': 'a.pdf'}],
                            [{'check_eft': '31401901', 'check_amount': '257.09', 'check_status': 'Check Cashed'},
                             {'check_eft': '31401908', 'check_amount': '257.09', 'check_status': 'Check Cashed'}], [], ecw_checked=True)
        self.assertFalse(any(f.startswith('possible misread') for r in rows for f in r['flags']))

    def test_the_dashboard_shows_it(self):
        d = _read('dashboard.py')
        self.assertIn("['misread', 'Possible misread']", d)
        self.assertIn("String(f).startsWith('possible misread')", d)


class TheChecksOwnAmount(unittest.TestCase):
    def test_an_eob_page_gives_the_check_amount_not_the_billed_one(self):
        got = rc.parse_check_text('Amount Billed $500.00  Total Charges 500.00  Check No. 31401901  '
                                  'Service 09/01/2026  Check Date 09/11/2026  Check Amount $38.81')
        self.assertEqual((got['check_number'], got['amount'], got['check_date']), ('31401901', '38.81', '09/11/2026'))

    def test_net_payment_and_the_plain_stub_still_read(self):
        self.assertEqual(rc.parse_check_text('Check # 31401901 Total Charges 90.00 Net Payment: $12.50')['amount'], '12.50')
        self.assertEqual(rc.parse_check_text('Check No. 31401901   Date 09/11/2026   Amount $38.81')['amount'], '38.81')


class TheSameCheckOnTwoPages(unittest.TestCase):
    def test_the_second_page_fills_what_the_first_left_blank(self):
        got = rc.merge_pages([{'check_number': '0031401901', 'amount': '', 'check_date': '', 'page': 1},
                              {'check_number': '31401901', 'amount': '38.81', 'check_date': '09/11/2026', 'page': 2},
                              {'check_number': '31401950', 'amount': '5.00', 'page': 3}])
        self.assertEqual([(c['check_number'], c['amount'], c['page']) for c in got],
                         [('0031401901', '38.81', 1), ('31401950', '5.00', 3)])


class TheDepositSlipIsWrittenByHand(unittest.TestCase):
    def _deposit(self, slip_lines, total, pages):
        slip = {'kind': 'deposit_slip', 'deposit': {'date': '04/22/2026', 'total': total, 'checks': slip_lines}}
        it = iter(pages)

        def page_check(aws, path, page_no, texts, model_id):
            ck = next(it)
            return {'kind': 'check', 'check': ck, 'read_by': 'vision'}, '', None
        with mock.patch.object(rc, '_page_check', side_effect=page_check):
            return rc._read_deposit(None, 'dep.pdf', slip, len(pages) + 1, 'm', {'kind': 'check'})

    def test_a_slip_line_one_digit_off_a_printed_check_is_that_check(self):
        got = self._deposit([{'check_number': '31401907', 'amount': '38.81'}, {'check_number': '555123', 'amount': '10.00'}], '48.81',
                            [{'check_number': '31401901', 'amount': '38.81'}])
        self.assertEqual(sorted(c['check_number'] for c in got['checks']), ['31401901', '555123'])
        self.assertEqual(next(c for c in got['checks'] if c['check_number'] == '31401901')['slip_wrote'], '31401907')
        self.assertEqual(got['problem'], '')

    def test_checks_that_do_not_add_up_to_the_slip_say_so(self):
        got = self._deposit([{'check_number': '31401901', 'amount': '38.81'}], '99.99', [{'check_number': '31401901', 'amount': '38.81'}])
        self.assertIn('add up to $38.81, the slip says $99.99', got['problem'])
        self.assertEqual(got['deposit_sum'], '38.81')


class ASecondLook(unittest.TestCase):
    CHECK = lambda self, n, conf, amt='5.00': {'kind': 'check', 'check': {'check_number': n, 'amount': amt, 'confidence': conf}}

    def test_only_an_unsettled_check_page_gets_one(self):
        self.assertTrue(rc.wants_second_look(self.CHECK('', 'high')))
        self.assertTrue(rc.wants_second_look(self.CHECK('31401901', 'low')))
        self.assertTrue(rc.wants_second_look(self.CHECK('31401901', 'high', amt='')))
        self.assertFalse(rc.wants_second_look(self.CHECK('31401901', 'high')))
        self.assertFalse(rc.wants_second_look({'kind': 'other', 'check': {}}))

    def test_the_second_reading_wins_only_when_it_settles_it(self):
        first = self.CHECK('31401907', 'low')
        self.assertEqual(rc.pick_reading(first, self.CHECK('31401901', 'high'))['check']['check_number'], '31401901')
        self.assertIs(rc.pick_reading(first, self.CHECK('31401901', 'low')), first)
        self.assertIs(rc.pick_reading(first, {}), first)
        self.assertIs(rc.pick_reading(first, {'kind': 'other', 'check': {}}), first)
        none = self.CHECK('', 'low')
        self.assertEqual(rc.pick_reading(none, {'kind': 'other', 'check': {}})['kind'], 'other')

    def test_a_failed_second_look_keeps_the_first(self):
        first = self.CHECK('31401907', 'low')
        calls = []

        def read_one(aws, path, page_no, model_id):
            calls.append(model_id)
            if len(calls) == 1:
                return dict(first), 'raw', None
            return {}, '', {'problem': 'the Anthropic API did not answer', 'error_kind': 'api'}
        with mock.patch.object(rc, '_read_one', side_effect=read_one):
            page, raw, err = rc._page_check(None, 'x.png', 0, [], 'claude-sonnet-5')
        self.assertIsNone(err)
        self.assertEqual(page['check']['check_number'], '31401907')
        self.assertEqual(calls, ['claude-sonnet-5', rc.VISION_RETRY_MODEL_ID])


class AMovedScanIsNotReadAgain(unittest.TestCase):
    OLD, NEW = 'Unposted Checks/11-01-2026/scan.pdf', 'Posted Checks/2026/11-2026/scan.pdf'

    def _run(self, table, files, reader=None):
        with mock.patch.object(run_mod, '_sources', return_value=(files, lambda it, d: '/tmp/x.pdf', 'sharepoint')), \
                mock.patch.object(run_mod, 'read_check', side_effect=reader or AssertionError('the model was asked')) as r:
            return run_mod.read_new_copies(_Aws(table), table, {}), r

    def test_a_check_follows_its_file_to_the_new_folder(self):
        table = _Table([{'check_number': '31401901', 'has_copy': True, 'copy_file': self.OLD, 'copy_etag': '"{G},1"', 'copy_pages': 1,
                         'copy_read_mode': 'all_pages', 'copy_file_id': 'G', 'copy_size': 5000, 'copy_amount': '38.81',
                         'copy_status': 'unposted', 'verdict': 'posted'}])
        (read, skipped, failed), _ = self._run(table, [{'path': self.NEW, 'etag': '"{G},2"', 'id': 'G', 'size': 5000, 'web_url': 'https://sp/new'}])
        self.assertEqual((read, skipped, failed), (0, 1, 0))
        row = table.items['31401901']
        self.assertEqual((row['copy_file'], row['copy_folder'], row['copy_status'], row['copy_url'], row['copy_amount'], row['verdict']),
                         (self.NEW, 'Posted Checks/2026/11-2026', 'posted', 'https://sp/new', '38.81', 'posted'))
        # And the run after that has nothing to do.
        (read, skipped, failed), _ = self._run(table, [{'path': self.NEW, 'etag': '"{G},2"', 'id': 'G', 'size': 5000}])
        self.assertEqual((read, skipped, failed), (0, 1, 0))

    def test_a_deposit_moves_with_its_checks_and_leaves_no_ghost(self):
        table = _Table([{'check_number': f'deposit:{self.OLD}', 'kind': 'deposit', 'has_copy': False, 'copy_file': self.OLD, 'copy_etag': 'e',
                         'copy_file_id': 'G', 'copy_size': 9000, 'deposit_total': '48.81', 'deposit_checks': ['1111111', '2222222']},
                        {'check_number': '1111111', 'has_copy': True, 'copy_file': self.OLD, 'copy_etag': 'e', 'deposit_file': self.OLD,
                         'copy_file_id': 'G', 'copy_size': 9000},
                        {'check_number': '2222222', 'has_copy': True, 'copy_file': self.OLD, 'copy_etag': 'e', 'deposit_file': self.OLD,
                         'copy_file_id': 'G', 'copy_size': 9000}])
        self._run(table, [{'path': self.NEW, 'etag': 'e2', 'id': 'G', 'size': 9000}])
        self.assertNotIn(f'deposit:{self.OLD}', table.items)
        self.assertEqual(table.items[f'deposit:{self.NEW}']['deposit_total'], '48.81')
        self.assertEqual({table.items[k]['deposit_file'] for k in ('1111111', '2222222')}, {self.NEW})

    def test_a_different_size_or_a_file_still_in_place_is_read(self):
        row = {'check_number': '31401901', 'has_copy': True, 'copy_file': self.OLD, 'copy_etag': 'e', 'copy_file_id': 'G', 'copy_size': 5000,
               'copy_pages': 1, 'copy_read_mode': 'all_pages'}
        got = {'kind': 'check', 'check_number': '31401901', 'amount': '38.81', 'pages': 1, 'read_by': 'vision'}
        _, r = self._run(_Table([row]), [{'path': self.NEW, 'etag': 'e2', 'id': 'G', 'size': 7777}], reader=lambda *a, **k: got)
        self.assertEqual(r.call_count, 1, 'another size is another image')
        _, r = self._run(_Table([row]), [{'path': self.OLD, 'etag': 'e', 'id': 'G', 'size': 5000},
                                         {'path': self.NEW, 'etag': 'e2', 'id': 'G', 'size': 5000}], reader=lambda *a, **k: got)
        self.assertEqual(r.call_count, 1, 'the old file is still there: the new one is a copy, read it')

    def test_rows_read_before_the_id_was_kept_learn_it(self):
        table = _Table([{'check_number': '31401901', 'has_copy': True, 'copy_file': self.OLD, 'copy_etag': 'e', 'copy_pages': 1,
                         'copy_read_mode': 'all_pages'}])
        self._run(table, [{'path': self.OLD, 'etag': 'e', 'id': 'G', 'size': 5000}])
        self.assertEqual((table.items['31401901']['copy_file_id'], table.items['31401901']['copy_size']), ('G', 5000))


class AChangedFileIsReadOnce(unittest.TestCase):
    """A file replaced by a scan that reads as another number left two rows
    naming it; whichever the scan returned last decided, and the file could
    be read again on every run."""
    PATH = 'Unposted Checks/11-01-2026/scan.pdf'

    def test_the_earlier_reading_goes_and_the_next_run_reads_nothing(self):
        table = _Table([{'check_number': '31401907', 'has_copy': True, 'copy_file': self.PATH, 'copy_etag': 'v1', 'copy_pages': 1,
                         'copy_read_mode': 'all_pages', 'copy_read_at': '2026-11-01 10:00:00 UTC'}])
        files = [{'path': self.PATH, 'etag': 'v2'}]
        got = {'kind': 'check', 'check_number': '31401901', 'amount': '38.81', 'pages': 1, 'read_by': 'vision'}
        with mock.patch.object(run_mod, '_sources', return_value=(files, lambda it, d: '/tmp/x.pdf', 'sharepoint')), \
                mock.patch.object(run_mod, 'read_check', return_value=got) as r:
            run_mod.read_new_copies(_Aws(table), table, {})
            self.assertEqual(sorted(table.items), ['31401901'])
            run_mod.read_new_copies(_Aws(table), table, {})
        self.assertEqual(r.call_count, 1)

    def test_the_latest_reading_says_which_version_is_on_file(self):
        table = _Table([{'check_number': '31401901', 'has_copy': True, 'copy_file': self.PATH, 'copy_etag': 'v2', 'copy_pages': 1,
                         'copy_read_mode': 'all_pages', 'copy_read_at': '2026-11-10 10:00:00 UTC'},
                        {'check_number': '31401907', 'has_copy': True, 'copy_file': self.PATH, 'copy_etag': 'v1', 'copy_pages': 1,
                         'copy_read_mode': 'all_pages', 'copy_read_at': '2026-11-01 10:00:00 UTC'}])
        with mock.patch.object(run_mod, '_sources', return_value=([{'path': self.PATH, 'etag': 'v2'}], lambda it, d: '/tmp/x.pdf', 'sharepoint')), \
                mock.patch.object(run_mod, 'read_check', side_effect=AssertionError('read again')):
            self.assertEqual(run_mod.read_new_copies(_Aws(table), table, {}), (0, 1, 0))


if __name__ == '__main__':
    unittest.main()
