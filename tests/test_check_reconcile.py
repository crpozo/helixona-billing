"""Remittance — the check reconciliation: copies · Blue Shield · eCW.

The operator's procedure (2026-09-16): read every check image in the
SharePoint folder, see whether Blue Shield cashed it, flag the cashed
checks we hold no copy of, and see whether eCW has a payment under that
number. Posted / unposted / not in eCW, per check.
"""
import os
import unittest
from unittest import mock

from src.checks.ecw_payments import rows_to_payments
from src.checks.read_check import parse_check_text, parse_model_json
from src.checks.reconcile import norm_check, reconcile, summarize

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


class CheckNumbersAreComparable(unittest.TestCase):
    def test_leading_zeros_and_labels_fall_away(self):
        self.assertEqual(norm_check('0031401901'), '31401901')
        self.assertEqual(norm_check('31401901 Check/EFT information'), '31401901')
        self.assertEqual(norm_check(''), '')
        self.assertEqual(norm_check('0000'), '0')


class TheVerdictPerCheck(unittest.TestCase):
    COPIES = [{'check_number': '0030912971', 'amount': '275.09', 'file': 'a.pdf'},
              {'check_number': '4061950', 'amount': '265.75', 'file': 'b.jpg'},
              {'check_number': '99999999', 'amount': '10.00', 'file': 'c.png'}]
    CHEQUES = [{'check_eft': '30912971', 'check_amount': '275.09', 'check_status': 'Check Cashed', 'cashed_date': '09/20/2026', 'check_date': '09/14/2026'},
               {'check_eft': '4061950', 'check_amount': '265.75', 'check_status': 'Check Cashed', 'cashed_date': '09/15/2026', 'check_date': '09/11/2026'},
               {'check_eft': '31401901', 'check_amount': '38.81', 'check_status': 'Check Number Assigned', 'cashed_date': '', 'check_date': '09/11/2026'},
               {'check_eft': '31406730', 'check_amount': '739.91', 'check_status': 'Check Cashed', 'cashed_date': '09/16/2026', 'check_date': '09/11/2026'}]
    PAYMENTS = [{'check_no': '30912971', 'amount': '275.09', 'posted': '275.09', 'unposted': '0.00', 'payment_id': '5043'},
                {'check_no': '4061950', 'amount': '265.75', 'posted': '0.00', 'unposted': '265.75', 'payment_id': '5044'}]

    def _rows(self, **kw):
        rows, summary = reconcile(self.COPIES, self.CHEQUES, self.PAYMENTS, **kw)
        return {r['check_number']: r for r in rows}, summary

    def test_posted_unposted_missing_and_not_cashed(self):
        by, summary = self._rows()
        self.assertEqual(by['30912971']['verdict'], 'posted')
        self.assertEqual(by['4061950']['verdict'], 'unposted')
        self.assertEqual(by['31406730']['verdict'], 'not in eCW')
        self.assertEqual(by['31401901']['verdict'], 'not cashed')
        # A scanned check Blue Shield does not list is still a check we
        # received: eCW decides. Not there → not in eCW, marked other payer.
        self.assertEqual(by['99999999']['verdict'], 'not in eCW')
        self.assertIn('other payer', by['99999999']['flags'])
        self.assertNotIn('other payer', by['30912971']['flags'])
        self.assertEqual((summary['posted'], summary['unposted'], summary['not_in_ecw'], summary['not_cashed'], summary['other_payer']),
                         (1, 1, 2, 1, 1))

    def test_a_cashed_check_without_an_image_is_flagged(self):
        by, summary = self._rows()
        self.assertIn('no copy of the check', by['31406730']['flags'])
        self.assertNotIn('no copy of the check', by['30912971']['flags'])
        # Not cashed yet: nothing to hold a copy of, no flag.
        self.assertNotIn('no copy of the check', by['31401901']['flags'])
        self.assertEqual(summary['no_copy'], 1)

    def test_the_copy_pins_the_check_despite_leading_zeros(self):
        by, _ = self._rows()
        self.assertTrue(by['30912971']['has_copy'])
        self.assertEqual(by['30912971']['copy_amount'], '275.09')

    def test_amounts_that_disagree_are_flagged(self):
        copies = [dict(self.COPIES[0], amount='257.09')]
        rows, summary = reconcile(copies, self.CHEQUES[:1], self.PAYMENTS[:1])
        self.assertTrue(any(f.startswith('amounts differ') for f in rows[0]['flags']))
        self.assertEqual(summary['amount_mismatch'], 1)

    def test_when_ecw_was_not_consulted_nothing_is_called_missing(self):
        by, summary = self._rows(ecw_checked=False)
        self.assertEqual(by['31406730']['verdict'], 'eCW not checked')
        self.assertEqual(by['99999999']['verdict'], 'eCW not checked')   # not "copy only", not "not found"
        self.assertEqual(by['31401901']['verdict'], 'eCW not checked')
        self.assertEqual(summary['ecw_unchecked'], 3)

    def test_the_tiles_count_over_whatever_rows_they_are_given(self):
        # The dashboard counts over the whole table, not over the last run,
        # so a test of 1 does not make the tiles say "1 check".
        rows, _ = reconcile(self.COPIES, self.CHEQUES, self.PAYMENTS)
        table_rows = rows + [{'check_number': '7', 'verdict': 'posted', 'flags': ['amounts differ: copy 1.00, bs 2.00']}]
        sm = summarize(table_rows)
        self.assertEqual((sm['checks'], sm['posted'], sm['amount_mismatch'], sm['no_copy'], sm['other_payer']), (6, 2, 1, 1, 1))
        self.assertEqual(summarize([]), {'checks': 0, 'posted': 0, 'unposted': 0, 'not_in_ecw': 0, 'not_cashed': 0,
                                         'ecw_unchecked': 0, 'other_payer': 0, 'no_copy': 0, 'amount_mismatch': 0})


class TheImageReaderIsShapeChecked(unittest.TestCase):
    def test_the_models_json_is_taken_only_when_it_is_a_check_number(self):
        got = parse_model_json('Here you go: {"check_number": "0031401901", "amount": "$38.81", "check_date": "09/11/2026", '
                               '"payer": "Blue Shield of California", "payee": "Helixona Inc", "confidence": "high"}')
        self.assertEqual(got['check_number'], '0031401901')
        self.assertEqual(got['amount'], '38.81')
        self.assertEqual(parse_model_json('{"check_number": "12", "amount": "x"}')['check_number'], '')
        self.assertEqual(parse_model_json('no json here'), {})

    def test_a_stub_with_a_text_layer_needs_no_model(self):
        got = parse_check_text('BLUE SHIELD OF CALIFORNIA   Check No. 31401901   Date 09/11/2026   Amount $38.81   Pay to HELIXONA INC')
        self.assertEqual(got['check_number'], '31401901')
        self.assertEqual(got['amount'], '38.81')
        self.assertEqual(got['check_date'], '09/11/2026')
        self.assertEqual(got['payer'], 'Blue Shield of California')

    def test_a_bare_number_is_not_a_check_number(self):
        self.assertEqual(parse_check_text('Patient account 31401901 statement')['check_number'], '')


class TheEcwPaymentsGridIsReadByName(unittest.TestCase):
    def test_columns_by_header_whatever_their_order(self):
        hdrs = ['Payment ID', 'Rcvd Date', 'Payer', 'Check No.', 'Amount', 'Posted', 'Unposted']
        rows = [['5043', '09/15/2026', 'Blue Shield of California', '30979207', '262.69', '262.69', '0.00'],
                ['', '', 'Total', '', '262.69', '', ''],
                ['5044', '09/15/2026', 'Blue Shield of California', '4061950', '265.75', '0.00', '265.75']]
        got = rows_to_payments(hdrs, rows)
        self.assertEqual([p['check_no'] for p in got], ['30979207', '4061950'])
        self.assertEqual(got[1]['unposted'], '265.75')
        self.assertEqual(got[0]['payment_id'], '5043')

    def test_an_unread_screen_is_described_in_the_log(self):
        # 2026-09-17: the full run read no payment rows and the log said only
        # that. Now it says what the screen is made of, and saves the HTML.
        e = _read('src/checks/ecw_payments.py')
        self.assertIn('def describe_screen(page, why):', e)
        self.assertIn("describe_screen(page, 'no payment rows read')", e)
        self.assertIn("describe_screen(page, 'grid not recognised')", e)
        self.assertIn("'/tmp', 'eob_post_payments_dom.html'", e)
        # Header table + body table, the other shape eCW lists come in.
        self.assertIn('headerTables', e)
        self.assertIn("a grid was read but its headers are not the ones expected", e)

    def test_the_grid_as_ecw_shows_it(self):
        # The Payments screen, 2026-09-18 (screenshot): POSTED BY sits before POSTED.
        hdrs = ['', '', '', '', '', 'BATCH ID', 'PAYMENT ID', 'POSTED BY', 'ERA EXCEPTION', 'DATE', 'PAYMENT FROM',
                'PAYMENT TYPE', 'CHECK NO', 'CHECK DATE', 'DEPOSIT DATE', 'AMOUNT', 'POSTED', 'UNPOSTED']
        rows = [['', '', '', '', '', '', '934', 'Fernand...', '0', '01/07/2026', 'Cigna', 'Check', '766832993',
                 '12/31/2025', '01/06/2026', '475.19', '475.19', '0.00']]
        got = rows_to_payments(hdrs, rows)
        self.assertEqual(len(got), 1)
        p = got[0]
        self.assertEqual((p['payment_id'], p['check_no'], p['amount'], p['posted'], p['unposted']),
                         ('934', '766832993', '475.19', '475.19', '0.00'))
        self.assertEqual((p['check_date'], p['deposit_date'], p['payer']), ('12/31/2025', '01/06/2026', 'Cigna'))

    def test_an_unread_grid_is_not_called_empty(self):
        e = _read('src/checks/ecw_payments.py')
        self.assertIn('def _read_grid(page):', e)
        self.assertIn('return best[\'hdrs\'], best[\'rows\'], False', e)
        self.assertIn("the Payments grid was not read for check {check_no} — eCW stays unchecked", e)
        self.assertIn('def _set_dates(page, since):', e)
        p = _read('src/eob/post.py')
        self.assertNotIn("page.keyboard.press('Enter')\n                time.sleep(0.3)", p)
        self.assertIn('def _read_value(frm):', p)
        e2 = _read('src/checks/ecw_payments.py')
        self.assertIn('c.every(x => x.length <= 120)', e2)
        self.assertIn("(!best || !best.right)) best = { hdrs, rows: [], score: 500, right: true }", e2)

    def test_a_grid_filtered_by_check_number_need_not_show_the_column(self):
        hdrs = ['Payment ID', 'Rcvd Date', 'Amount', 'Posted', 'Unposted']
        rows = [['5043', '09/15/2026', '262.69', '262.69', '0.00'], ['', 'Total', '262.69', '', '']]
        got = rows_to_payments(hdrs, rows, assume_check='0030979207')
        self.assertEqual([(p['payment_id'], p['check_no']) for p in got], [('5043', '0030979207')])
        # Without the searched number nothing is assumed.
        self.assertEqual(rows_to_payments(hdrs, rows), [])


class _Table:
    """Enough of a DynamoDB table for the run: scan, get/put, SET updates."""

    def __init__(self, name, items=()):
        self.name = name
        self.items = {it['check_number' if 'check_number' in it else 'check_eft']: dict(it) for it in items}
        self.key = 'check_number' if name == 'helixona-checks' else 'check_eft'

    def scan(self, **kw):
        return {'Items': [dict(v) for v in self.items.values()]}

    def get_item(self, Key):
        it = self.items.get(Key[self.key])
        return {'Item': dict(it)} if it else {}

    def put_item(self, Item):
        self.items[Item[self.key]] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None):
        it = self.items.setdefault(Key[self.key], dict(Key))
        names = ExpressionAttributeNames or {}
        for clause in UpdateExpression[len('SET '):].split(','):
            name, val = [x.strip() for x in clause.split('=')]
            it[names.get(name, name)] = ExpressionAttributeValues[val]

    def delete_item(self, Key):
        self.items.pop(Key[self.key], None)

    def wait_until_exists(self):
        pass


class _Ddb:
    def __init__(self, tables):
        self._t = {t.name: t for t in tables}
        self.tables = self

    def all(self):
        return list(self._t.values())

    def Table(self, name):
        return self._t[name]


class _Aws:
    def __init__(self, tables):
        self.dynamodb = _Ddb(tables)

    def get_secret(self, name):
        if name == 'ecw_credentials':
            return {'username': 'u', 'password': 'p'}
        raise RuntimeError('no such secret')


class TheTestOfOne(unittest.TestCase):
    """One check from Blue Shield, compared with the copies and eCW, its
    row alone written — while the tiles keep counting the whole table."""
    CHEQUE = {'check_eft': '30925163', 'check_amount': '227.20', 'check_status': 'Check Cashed',
              'check_date': '04/17/2026', 'cashed_date': '07/27/2026'}

    def _run(self, body, find=lambda page, ck, since, navigate=True, shot=True, **kw: [], capture=None, copies=None):
        checks = _Table('helixona-checks', [
            {'check_number': '11111111', 'verdict': 'posted', 'flags': [], 'has_copy': True, 'reconciled_at': 'before'},
            {'check_number': '22222222', 'verdict': 'not in eCW', 'flags': ['no copy of the check'], 'reconciled_at': 'before'}])
        eobs = _Table('helixona-eobs', [self.CHEQUE, {'check_eft': '11111111', 'check_amount': '1.00',
                                                      'check_status': 'Check Cashed', 'check_date': '01/01/2026',
                                                      'cashed_date': '01/02/2026'}])
        aws = _Aws([checks, eobs])

        def fake_capture(page, aws_client, cap_body):
            self.capture_body = cap_body
            return capture if capture is not None else {'ok': True, 'captured_checks': ['30925163']}

        def fake_copies(aws_client, table, body, prefer=(), **kw):
            self.prefer = set(prefer)
            table.update_item(Key={'check_number': '30925163'}, UpdateExpression='SET has_copy = :a, copy_amount = :b, copy_file = :c',
                              ExpressionAttributeValues={':a': True, ':b': '227.20', ':c': 'Check 30925163.pdf'})
            return (1, 0, 0)

        from src.checks import run as run_mod
        with mock.patch('src.eob.capture.run_eob_capture', side_effect=fake_capture), \
                mock.patch.object(run_mod, 'read_new_copies', side_effect=copies or fake_copies), \
                mock.patch.object(run_mod, 'find_payments', side_effect=find), \
                mock.patch.object(run_mod, 'list_payments', side_effect=AssertionError('a targeted run never lists every payment')):
            result = run_mod.run_check_reconcile(aws, body, login=lambda page, creds, aws_client: True,
                                                 get_page=lambda: object())
        return result, checks

    def test_one_check_end_to_end(self):
        result, checks = self._run({'blue_shield': True, 'limit_checks': 1})
        self.assertEqual(result['targets'], ['30925163'])
        self.assertEqual(self.capture_body['limit_checks'], 1)
        self.assertFalse(self.capture_body['force'], 'an unnamed test takes the next check not yet on file')
        self.assertFalse(self.capture_body['download_eob'])
        self.assertEqual(self.prefer, {'30925163'})
        row = checks.items['30925163']
        self.assertEqual((row['verdict'], row['has_copy'], row['bs_status'], row['in_ecw']),
                         ('not in eCW', True, 'Check Cashed', False))
        # The other rows were not this run's, and were left as they were.
        self.assertEqual(checks.items['11111111']['reconciled_at'], 'before')
        self.assertEqual(result['run_rows'], 1)
        # ...yet the tiles count the whole table.
        sm = checks.items['_summary']
        self.assertEqual((sm['checks'], sm['posted'], sm['not_in_ecw'], sm['no_copy']), (3, 1, 2, 1))
        run = checks.items['_run']
        self.assertEqual((run['mode'], run['targets']), ('test of 1', ['30925163']))
        for step in ('blue_shield', 'copies', 'ecw', 'verdict', 'done'):
            self.assertIn(step, run['steps'])
        self.assertIn('30925163 not found', run['steps']['ecw'])

    def test_the_payment_on_file_makes_it_posted(self):
        result, checks = self._run(
            {'blue_shield': True, 'limit_checks': 1},
            find=lambda page, ck, since, navigate=True, shot=True, **kw: [{'check_no': ck, 'amount': '227.20', 'posted': '227.20',
                                                                     'unposted': '0.00', 'payment_id': '5050'}])
        row = checks.items['30925163']
        self.assertEqual((row['verdict'], row['ecw_payment_id'], row['flags']), ('posted', '5050', []))
        self.assertIn('30925163 on file', checks.items['_run']['steps']['ecw'])

    def test_ecw_is_asked_check_by_check_and_settled_ones_are_kept(self):
        # 2026-09-18, the operator: put the number in Check #, dates from
        # 07/01/25 to today, look the check up — never the whole list.
        r = _read('src/checks/run.py')
        self.assertIn("got = find_payments(page, ck, since, navigate=first, shot=bool(targets), spellings=spellings.get(ck))", r)
        self.assertIn("known = {norm_check(it.get('check_number')): it for it in scan_all(table)", r)
        self.assertIn("settled = {ck: it for ck, it in known.items() if it.get('in_ecw') and it.get('verdict') == 'posted'}", r)
        self.assertIn("if not targets:   # a targeted run asks again; a full run trusts the settled answer", r)
        e = _read('src/checks/ecw_payments.py')
        self.assertIn('def find_payments(page, check_no, since, navigate=True, shot=True, spellings=None):', e)
        self.assertIn('deadline = time.time() + (8 if before[0] else 3)', e)
        # eCW checked per check number: the ones not looked up stay 'eCW not checked'.
        rows, _ = reconcile([], [{'check_eft': '1', 'check_status': 'Check Cashed', 'cashed_date': '01/01/2026'},
                                 {'check_eft': '2', 'check_status': 'Check Cashed', 'cashed_date': '01/01/2026'}],
                            [], ecw_checked={'1'})
        by = {x['check_number']: x['verdict'] for x in rows}
        self.assertEqual((by['1'], by['2']), ('not in eCW', 'eCW not checked'))
        d = _read('dashboard.py')
        self.assertIn('eCW only — nothing is sent to Blue Shield.', d)

    def test_a_failed_lookup_does_not_erase_what_ecw_said_before(self):
        # 2026-09-18: a full run whose eCW lookups all failed rewrote every
        # posted check as 'eCW not checked' — the team page went from 4
        # posted to 1. An earlier answer stands until eCW says otherwise.
        from src.checks import run as run_mod
        checks = _Table('helixona-checks', [
            {'check_number': '766832993', 'verdict': 'posted', 'in_ecw': True, 'ecw_amount': '227.20',
             'ecw_posted': '227.20', 'ecw_unposted': '', 'ecw_payment_id': '934', 'flags': [], 'has_copy': True},
            {'check_number': '766832992', 'verdict': 'unposted', 'in_ecw': True, 'ecw_amount': '50.00',
             'ecw_posted': '0.00', 'ecw_unposted': '50.00', 'ecw_payment_id': '935', 'flags': [], 'has_copy': True},
            {'check_number': '30610041', 'verdict': 'not in eCW', 'in_ecw': False, 'flags': [], 'has_copy': True},
            {'check_number': '10248460', 'verdict': 'eCW not checked', 'in_ecw': False, 'flags': [], 'has_copy': True}])
        aws = _Aws([checks, _Table('helixona-eobs', [])])
        asked = []

        def failing(page, ck, since, navigate=True, shot=True, **kw):
            asked.append(ck)
            return None

        with mock.patch.object(run_mod, 'read_new_copies', return_value=(0, 4, 0)), \
                mock.patch.object(run_mod, 'find_payments', side_effect=failing):
            run_mod.run_check_reconcile(aws, {'blue_shield': False, 'copies': True, 'ecw': True},
                                        login=lambda page, creds, aws_client: True, get_page=lambda: object())
        # The posted one is settled and never asked; the rest were asked and the screen failed.
        self.assertNotIn('766832993', asked)
        self.assertEqual(sorted(asked), ['10248460', '30610041', '766832992'])
        by = {k: (v['verdict'], v.get('ecw_payment_id')) for k, v in checks.items.items() if not k.startswith('_')}
        self.assertEqual(by['766832993'], ('posted', '934'))
        self.assertEqual(by['766832992'], ('unposted', '935'))
        self.assertEqual(by['30610041'][0], 'not in eCW')
        self.assertEqual(by['10248460'][0], 'eCW not checked')
        # 766832993 was settled (never asked); the other two earlier answers were kept when the screen failed.
        self.assertIn('2 kept from earlier runs', checks.items['_run']['steps']['ecw'])
        self.assertEqual(checks.items['_summary']['posted'], 1)

    def test_the_number_is_kept_as_printed_and_ecw_is_asked_that_way(self):
        # 2026-09-18, Cigna check 0231282015: the bot stored 231282015 and
        # eCW, which matches Check # exactly, found nothing. The full number
        # is kept and tried first; the bare one is the key the sources meet on.
        from src.checks.ecw_payments import spellings_of
        self.assertEqual(spellings_of('0231282015', '231282015'), ['0231282015', '00231282015', '231282015'])
        self.assertEqual(spellings_of('00231282015'), ['00231282015', '0231282015', '231282015'])
        self.assertEqual(spellings_of('30925163', '30925163'), ['30925163'])
        from src.checks.run import _zeros_in_name
        self.assertEqual(_zeros_in_name('Posted Checks/2026/01-2026/Check 0231282015.pdf', '231282015'), '0231282015')
        self.assertEqual(_zeros_in_name('Posted Checks/2026/01-2026/IMG_4021.jpg', '231282015'), '')
        asked = []

        def fake_copies(aws_client, table, body, prefer=(), **kw):
            table.update_item(Key={'check_number': '231282015'},
                              UpdateExpression='SET has_copy = :a, copy_amount = :b, copy_file = :c, copy_check_raw = :d',
                              ExpressionAttributeValues={':a': True, ':b': '4226.70', ':c': 'Posted Checks/2026/01-2026/scan.pdf', ':d': '0231282015'})
            return (1, 0, 0)

        def find(page, ck, since, navigate=True, shot=True, spellings=None):
            asked.append((ck, spellings))
            return [{'check_no': '0231282015', 'amount': '4226.70', 'posted': '4226.70', 'unposted': '0.00', 'payment_id': '1111'}]

        _, checks = self._run({'blue_shield': False, 'check_eft': '231282015'}, find=find, copies=fake_copies)
        self.assertEqual(asked, [('231282015', ['0231282015', '00231282015', '231282015'])])
        row = checks.items['231282015']
        self.assertEqual((row['verdict'], row['ecw_payment_id'], row['copy_check_raw']), ('posted', '1111', '0231282015'))
        d = _read('dashboard.py')
        self.assertIn("r['check_full'] = raw if raw.endswith(str(r['check_number'])) else str(r['check_number'])", d)
        self.assertIn("${esc(r.check_full || r.check_number)}", d)
        self.assertIn("${esc(r.check_full || r.check_number)}", _read('dashboard_checks.html'))

    def test_a_named_check_is_reopened_for_its_status_today(self):
        self._run({'blue_shield': True, 'check_eft': '30925163'})
        self.assertTrue(self.capture_body['force'])
        self.assertEqual(self.capture_body['check_eft'], '30925163')

    def test_a_named_check_needs_no_portal_walk(self):
        result, checks = self._run({'blue_shield': False, 'check_eft': '30925163'})
        self.assertFalse(hasattr(self, 'capture_body'))
        self.assertEqual(checks.items['30925163']['verdict'], 'not in eCW')
        self.assertEqual(checks.items['_run']['mode'], 'test of 1')

    def test_nothing_captured_means_nothing_compared(self):
        result, checks = self._run({'blue_shield': True, 'limit_checks': 1}, capture={'ok': False, 'reason': 'login'})
        self.assertEqual(result, {'ok': False, 'reason': 'no check captured'})
        self.assertNotIn('30925163', checks.items)
        self.assertEqual(checks.items['_run']['steps']['done'], 'stopped')


class _Resp:
    def __init__(self, data=None, body=b'', status=200):
        self._d, self._b, self.status = data, body, status

    @property
    def ok(self):
        return self.status < 400

    def json(self):
        return self._d

    def body(self):
        return self._b


class _Page:
    """A browser already signed in to SharePoint: the sharing link lands
    on the library view, the REST API answers with the browser's cookies."""
    FOLDER = '/sites/BillingDepartment/Shared Documents/Insurance Checks'

    def __init__(self):
        self.url = ''
        self.got = []

    def goto(self, url, **kw):
        self.url = ('https://helixona.sharepoint.com/sites/BillingDepartment/Shared%20Documents/Forms/AllItems.aspx'
                    '?id=%2Fsites%2FBillingDepartment%2FShared%20Documents%2FInsurance%20Checks&viewid=x')

    def locator(self, sel):
        class L:
            first = None
            def count(self_inner):
                return 0
        return L()

    def get_by_text(self, *a, **k):
        return self.locator('')

    class request:
        @staticmethod
        def get(url, headers=None):
            _Page.got.append(url)
            from urllib.parse import unquote
            u = unquote(url)
            if "01''2026" in u:
                return _Resp(status=503)   # SharePoint's answer on that folder, 2026-09-17
            # One expanded call per folder: Files and Folders together.
            if '$expand=Files,Folders' in u and 'Insurance Checks/2026' in u:
                return _Resp({'Files': [{'Name': 'Check 30925163.pdf', 'ServerRelativeUrl': _Page.FOLDER + '/2026/Check 30925163.pdf',
                                         'Length': '120', 'UniqueId': 'u2', 'ETag': '"2"', 'TimeLastModified': '2026-09-15T10:00:00Z'}],
                              'Folders': []})
            if '$expand=Files,Folders' in u:
                return _Resp({'Files': [{'Name': 'notes.txt', 'ServerRelativeUrl': _Page.FOLDER + '/notes.txt', 'Length': '3', 'UniqueId': 'u0', 'ETag': '"0"'},
                                        {'Name': 'Check 4022519.jpg', 'ServerRelativeUrl': _Page.FOLDER + '/Check 4022519.jpg',
                                         'Length': '99', 'UniqueId': 'u1', 'ETag': '"1"', 'TimeLastModified': '2026-09-16T10:00:00Z'}],
                              'Folders': [{'Name': 'Forms', 'ServerRelativeUrl': _Page.FOLDER + '/Forms'},
                                          {'Name': 'Insurance Check Tracker', 'ServerRelativeUrl': _Page.FOLDER + '/Insurance Check Tracker'},
                                          {'Name': '2026', 'ServerRelativeUrl': _Page.FOLDER + '/2026'},
                                          {'Name': "01'2026", 'ServerRelativeUrl': _Page.FOLDER + "/01'2026"}]})
            if u.endswith('Check 4022519.jpg'):
                return _Resp(body=b'JPEGBYTES')
            return _Resp(status=404)

    got = []


class ADepositIsManyChecks(unittest.TestCase):
    """2026-09-18: 04222026_001.pdf is a bank deposit — the slip on page 1
    (29 checks by hand, $11,099.18) and a check per page after it. There is
    no check of $11k; each check is its own case inside the deposit."""
    SLIP = {'kind': 'deposit_slip', 'check': {}, 'confidence': 'medium',
            'deposit': {'date': '04/22/2026', 'total': '11099.18',
                        'checks': [{'check_number': '30880301', 'amount': '71.36'}, {'check_number': '30880303', 'amount': '224.29'},
                                   {'check_number': '7255595', 'amount': '139.73'}]}}
    PAGES = {1: {'kind': 'check', 'check': {'check_number': '30880301', 'amount': '71.36', 'check_date': '04/10/2026', 'payer': 'Blue Shield of California', 'payee': 'Cassandra Gray'}},
             2: {'kind': 'other', 'check': {}},
             3: {'kind': 'check', 'check': {'check_number': '30880303', 'amount': '224.29', 'check_date': '04/10/2026', 'payer': 'Blue Shield of California', 'payee': ''}}}

    def _read(self, path='x.pdf'):
        from src.checks import read_check as rc
        with mock.patch.object(rc, 'pdf_page_count', return_value=4), mock.patch.object(rc, 'pdf_text', return_value=''), \
                mock.patch.object(rc, 'image_payload', return_value=('', 'image/png')), \
                mock.patch.object(rc, 'read_page', side_effect=lambda aws, p, page_no, model_id=None: (self.SLIP if page_no == 0 else self.PAGES[page_no], '')):
            return rc.read_check(None, path)

    def test_the_slip_on_page_one_makes_the_file_a_deposit(self):
        got = self._read()
        self.assertEqual(got['kind'], 'deposit')
        self.assertEqual((got['deposit_date'], got['deposit_total'], got['pages']), ('04/22/2026', '11099.18', 4))
        by = {c['check_number']: c for c in got['checks']}
        # The checks as printed win; the slip fills in the one with no page of its own.
        self.assertEqual((by['30880301']['page'], by['30880301']['from_slip'], by['30880301']['payer']), (2, False, 'Blue Shield of California'))
        self.assertEqual((by['30880303']['page'], by['30880303']['amount']), (4, '224.29'))
        self.assertEqual((by['7255595']['from_slip'], by['7255595']['amount']), (True, '139.73'))
        self.assertEqual(len(got['checks']), 3)

    def test_a_page_reply_in_the_old_flat_shape_is_still_a_check(self):
        from src.checks.read_check import parse_page_json, parse_model_json
        flat = parse_page_json('{"check_number": "0231282015", "amount": "4,226.70", "confidence": "high"}')
        self.assertEqual((flat['kind'], flat['check']['check_number'], flat['check']['amount']), ('check', '0231282015', '4226.70'))
        self.assertEqual(parse_model_json('{"kind":"check","check":{"check_number":"30900703","amount":"200.93"}}')['check_number'], '30900703')
        # A slip with nothing legible is not a deposit; a "check" with no number but a list is a slip.
        self.assertEqual(parse_page_json('{"kind":"deposit_slip","deposit":{"checks":[]}}')['kind'], 'other')
        self.assertEqual(parse_page_json('{"kind":"check","check":{},"deposit":{"total":"10.00","checks":[{"check_number":"12345","amount":"10.00"}]}}')['kind'], 'deposit_slip')
        rc = _read('src/checks/read_check.py')
        self.assertIn('(b) a bank DEPOSIT SLIP / deposit ticket', rc)
        self.assertIn("if page.get('kind') == 'deposit_slip' and page_no == 0:", rc)

    def test_the_run_stores_the_deposit_and_each_check_and_drops_the_false_one(self):
        from src.checks import run as run_mod
        table = _Table('helixona-checks', [
            # The one-check reading of the deposit scan, before deposits were known.
            {'check_number': '1100411', 'has_copy': True, 'copy_file': 'Posted Checks/2026/04-22-2026/04222026_001.pdf',
             'copy_etag': '"7"', 'copy_amount': '11099.18', 'verdict': 'eCW not checked', 'flags': []},
            {'check_number': '30925163', 'has_copy': True, 'copy_file': 'Posted Checks/2026/Check 30925163.pdf', 'copy_etag': '"1"', 'copy_pages': 1}])
        files = [{'path': 'Posted Checks/2026/04-22-2026/04222026_001.pdf', 'etag': '"7"', 'web_url': 'https://sp/dep.pdf'},
                 {'path': 'Posted Checks/2026/Check 30925163.pdf', 'etag': '"1"', 'web_url': 'https://sp/one.pdf'}]
        deposit = {'kind': 'deposit', 'deposit_date': '04/22/2026', 'deposit_total': '11099.18', 'pages': 4, 'read_by': 'vision',
                   'confidence': 'medium', 'problem': '',
                   'checks': [{'check_number': '30880301', 'amount': '71.36', 'check_date': '04/10/2026', 'payer': 'Blue Shield of California', 'payee': '', 'page': 2, 'from_slip': False},
                              {'check_number': '7255595', 'amount': '139.73', 'check_date': '', 'payer': '', 'payee': '', 'page': 1, 'from_slip': True}]}
        with mock.patch.object(run_mod, '_sources', return_value=(files, lambda it, d: '/tmp/' + it['path'].split('/')[-1], 'sharepoint')), \
                mock.patch('src.checks.read_check.pdf_page_count', return_value=4), \
                mock.patch.object(run_mod, 'read_check', return_value=deposit) as reader:
            read, skipped, failed = run_mod.read_new_copies(_Aws([table]), table, {})
        # The one-page PDF was only counted (no model); the four-page one was read again, page by page.
        self.assertEqual(reader.call_count, 1)
        self.assertEqual((read, skipped, failed), (2, 1, 0))
        self.assertNotIn('1100411', table.items, 'the false check of $11k is gone')
        dep = table.items['deposit:Posted Checks/2026/04-22-2026/04222026_001.pdf']
        self.assertEqual((dep['kind'], dep['deposit_total'], dep['deposit_count'], dep['deposit_checks'], dep['has_copy']),
                         ('deposit', '11099.18', 2, ['30880301', '7255595'], False))
        one = table.items['30880301']
        self.assertEqual((one['has_copy'], one['copy_amount'], one['copy_page'], one['deposit_file'], one['deposit_total'], one['copy_from_slip']),
                         (True, '71.36', 2, 'Posted Checks/2026/04-22-2026/04222026_001.pdf', '11099.18', False))
        self.assertEqual((table.items['7255595']['copy_from_slip'], table.items['7255595']['copy_date']), (True, '04/22/2026'))
        self.assertEqual(table.items['30925163']['copy_pages'], 1)
        # A second run reads nothing again.
        with mock.patch.object(run_mod, '_sources', return_value=(files, lambda it, d: '/tmp/x', 'sharepoint')), \
                mock.patch.object(run_mod, 'read_check', side_effect=AssertionError('read twice')):
            self.assertEqual(run_mod.read_new_copies(_Aws([table]), table, {}), (0, 2, 0))

    def test_the_pages_show_the_deposit_as_one_accordion_with_its_checks(self):
        d = _read('dashboard.py')
        self.assertIn("startswith(('_', 'unreadable:', 'deposit:'))", d)
        self.assertIn("'deposits': dep_rows,", d)
        self.assertIn("🏦 deposit of {len(nums)} checks", d)
        h = _read('dashboard_checks.html')
        for pin in ('const deposits = new Map((sm.deposits || []).map(d => [d.file, d]));', 'data-dep="${esc(d.file)}"',
                    'Each check below is its own case.', 'tr.dep { cursor: pointer; }', 'rowHtml(r, true)', '(slip only)',
                    # 2026-09-20: one band across the table, with a chevron a person can see and hit.
                    'class="dep-band"', 'class="dep-toggle"', '<td colspan="9">', 'tr.dep.open .dep-toggle { transform: rotate(90deg); }'):
            self.assertIn(pin, h)
        self.assertIn("startswith(('_', 'unreadable:', 'deposit:'))", _read('src/checks/run.py'))


class TheFolderIsReadThroughTheBrowser(unittest.TestCase):
    """2026-09-17: the folder is shared inside Helixona only and no Entra
    admin is at hand, so the bot uses a person's session signed in once on
    the live screen."""
    def test_the_sharing_link_resolves_to_the_folder(self):
        from src.checks import sharepoint_browser as spb
        page = _Page()
        site, folder = spb.open_folder(page, wait_for_person=1)
        self.assertEqual(site, 'https://helixona.sharepoint.com/sites/BillingDepartment')
        self.assertEqual(folder, '/sites/BillingDepartment/Shared Documents/Insurance Checks')

    def test_images_in_the_folder_and_its_subfolders_but_not_forms(self):
        from src.checks import sharepoint_browser as spb
        with mock.patch.object(spb.time, 'sleep'):
            page = _Page()
            files = spb.list_folder(page, 'https://helixona.sharepoint.com/sites/BillingDepartment', _Page.FOLDER)
        # The folder named 01'2026 answered 503 three times and was skipped; everything else is here.
        self.assertEqual([f['path'] for f in files], ['Check 4022519.jpg', '2026/Check 30925163.pdf'])
        self.assertEqual(sum('01%27%272026' in u for u in _Page.got), 3)   # the quote doubled, then encoded
        self.assertIn('GetFolderByServerRelativePath(decodedurl=@f)', _Page.got[0])
        # The tracker spreadsheet folder is left out on purpose, never even listed.
        self.assertFalse(any('Insurance%20Check%20Tracker' in u for u in _Page.got))
        self.assertEqual(files[0]['etag'], '"1"')
        self.assertTrue(files[0]['download_url'].startswith('https://helixona.sharepoint.com/sites/'))
        self.assertFalse(any('Forms' in u and '$expand' in u for u in _Page.got))
        # One round trip per folder, not two.
        self.assertEqual(sum('$expand=Files,Folders' in u for u in _Page.got if 'Insurance%20Checks%2F2026' in u or u.endswith("Insurance%20Checks'&$select=Files/Name,Files/ServerRelativeUrl,Files/TimeLastModified,Files/Length,Files/UniqueId,Files/ETag,Folders/Name,Folders/ServerRelativeUrl&$expand=Files,Folders&$top=5000")), 2)

    def test_posted_2025_is_left_out_and_the_folder_is_the_teams_word(self):
        from src.checks.sharepoint_browser import SKIP_FOLDERS, _skipped
        self.assertIn('Posted Checks/2025', SKIP_FOLDERS)
        self.assertTrue(_skipped('Posted Checks/2025', SKIP_FOLDERS))
        self.assertTrue(_skipped('posted checks/2025/03-2025', SKIP_FOLDERS))
        self.assertFalse(_skipped('Posted Checks/2026', SKIP_FOLDERS))
        self.assertFalse(_skipped('Unposted Checks/07-13-2026', SKIP_FOLDERS))
        r = _read('src/checks/run.py')
        self.assertIn("'copy_status': ('posted' if f['path'].lower().startswith('posted')", r)

    def test_the_bytes_come_through_the_session(self):
        import tempfile
        from src.checks import sharepoint_browser as spb
        item = {'name': 'Check 4022519.jpg', 'path': 'Check 4022519.jpg',
                'download_url': 'https://helixona.sharepoint.com' + _Page.FOLDER + '/Check%204022519.jpg'}
        with tempfile.TemporaryDirectory() as d:
            path = spb.download(_Page(), item, d)
            with open(path, 'rb') as fh:
                self.assertEqual(fh.read(), b'JPEGBYTES')

    def test_the_run_prefers_it_over_the_s3_inbox_when_a_browser_is_there(self):
        r = _read('src/checks/run.py')
        self.assertIn("if get_page is not None:\n            page = get_page()\n            site, folder = spb.open_folder(", r)
        self.assertIn("read_new_copies(aws_client, table, body, prefer=targets, get_page=get_page)", r)
        self.assertIn("DEFAULT_SHARE_LINK = ('https://helixona.sharepoint.com/:f:/s/BillingDepartment/'", _read('src/checks/sharepoint_browser.py'))

    def test_an_api_refusal_is_not_held_against_the_file(self):
        # 2026-09-18: "Your credit balance is too low" — 700 files would have
        # been marked unreadable, twice, for the account's sake.
        rc = _read('src/checks/read_check.py')
        self.assertIn("return {}, '', {'problem': problem, 'error_kind': 'api'}", rc)
        self.assertIn("return {}, '', {'problem': problem, 'error_kind': 'file'}", rc)
        r = _read('src/checks/run.py')
        self.assertIn("if got.get('error_kind') == 'api':", r)
        self.assertIn('the Anthropic API refused three files in a row', r)
        self.assertIn('🗑 Clear', _read('dashboard.py'))

    def test_an_unreadable_file_is_tried_again_once_and_is_not_a_check(self):
        r = _read('src/checks/run.py')
        self.assertIn('MAX_READ_ATTEMPTS = 2', r)
        self.assertIn("if prev and prev[0] == etag and (targeted or prev[1] >= MAX_READ_ATTEMPTS):", r)
        self.assertIn("prev_attempts = prev[1] if prev and prev[0] == etag else 0", r)
        self.assertIn("'copy_attempts': prev_attempts + 1,", r)
        # A test of 1 whose check already has its copy does not walk the folder.
        self.assertIn("if do_copies and targets and set(on_file) >= targets:", r)
        # A multi-page PDF is read beyond its cover page.
        rc = _read('src/checks/read_check.py')
        self.assertIn('def pdf_page_count(path):', rc)
        self.assertIn('pages += [p for p in (1, n - 1) if p not in pages]', rc)
        self.assertIn("startswith(('_', 'unreadable:', 'deposit:'))", r)
        self.assertIn("startswith(('_', 'unreadable:', 'deposit:'))", _read('dashboard.py'))
        self.assertIn("'copy_folder': f['path'].rsplit('/', 1)[0] if '/' in f['path'] else ''", r)

    def test_the_images_are_read_by_the_claude_api_not_bedrock(self):
        rc = _read('src/checks/read_check.py')
        self.assertIn("VISION_MODEL_ID = os.environ.get('VISION_MODEL_ID', 'claude-sonnet-5')", rc)
        self.assertIn("anthropic.Anthropic(api_key=key)", rc)
        self.assertIn("'helixona-prod-anthropic-api-key'", rc)
        self.assertIn("client.get_secret_value(SecretId=name)", rc)
        self.assertIn("KEY_SECRET_REGIONS", rc)
        # The key is accepted bare or as JSON — the provisioned secret is a plain string.
        from src.checks.read_check import _key_from_secret
        class _Secrets:
            def __init__(self, values):
                self.values = values
            def get_secret_value(self, SecretId):
                if SecretId not in self.values:
                    raise RuntimeError('ResourceNotFoundException')
                return {'SecretString': self.values[SecretId]}
        class _Session:
            @staticmethod
            def client(kind, region_name=''):
                # The provisioned secret is in us-east-1, not the bot's region.
                return _Secrets({'east': 'sk-ant-east'}) if region_name == 'us-east-1' else _Secrets({})
        class _Aws:
            secrets = _Secrets({'a': 'sk-ant-plain', 'b': '{"ANTHROPIC_API_KEY": "sk-ant-json"}', 'c': '{"api_key": "sk-ant-j2"}'})
            session = _Session()
        self.assertEqual(_key_from_secret(_Aws(), 'east'), 'sk-ant-east')
        self.assertEqual(_key_from_secret(_Aws(), 'missing'), '')
        self.assertEqual(_key_from_secret(_Aws(), 'a'), 'sk-ant-plain')
        self.assertEqual(_key_from_secret(_Aws(), 'b'), 'sk-ant-json')
        self.assertEqual(_key_from_secret(_Aws(), 'c'), 'sk-ant-j2')
        self.assertIn("fallbacks='default'", rc)
        self.assertIn("if response.stop_reason == 'refusal':", rc)
        for gone in ('bedrock-runtime', 'invoke_model', 'bedrock-2023-05-31'):
            self.assertNotIn(gone, rc, gone)
        self.assertIn('anthropic>=1.6', _read('requirements.txt'))

    def test_nothing_is_written_to_sharepoint(self):
        s = _read('src/checks/sharepoint_browser.py')
        for forbidden in ('request.post', 'request.put', 'request.patch', 'request.delete', 'X-RequestDigest'):
            self.assertNotIn(forbidden, s)


class TheTaskIsWiredReadOnly(unittest.TestCase):
    def test_the_bot_runs_it_and_opens_one_browser_on_demand(self):
        src = _read('src/main.py')
        self.assertIn("EOB_TASKS = {'check_reconcile'}", src)
        i = src.index("elif task_type == 'check_reconcile':")
        block = src[i:i + 1500]
        self.assertIn('run_check_reconcile(aws_client, body, login=_perform_ecw_login, get_page=get_page)', block)
        # One Chrome on the profile dir: the Blue Shield and eCW steps share it.
        self.assertIn("if not holder.get('manager'):\n                holder['manager'] = BrowserManager().start(proxy_config=None)", block)

    def test_the_payments_screen_can_be_opened_by_a_person_when_the_menu_hides(self):
        # 2026-09-17: "nothing to click among ['Payments', 'Payment'] (menu)" —
        # the run must not die there: hidden menu items are tried, the menu
        # is logged, and someone can open Billing → Payments on the live screen.
        p = _read('src/eob/post.py')
        self.assertIn('def open_payments(page, wait_for_person=90):', p)
        self.assertIn("_menu_items(page, r'^payments?$|payment\\s*lookup|insurance\\s*payments?', click=True)", p)
        self.assertIn('open it on the live screen (noVNC)', p)
        self.assertIn('that route belongs in PAYMENT_HASHES', p)

    def test_a_check_captured_without_a_pdf_is_on_file(self):
        c = _read('src/eob/capture.py')
        self.assertIn("ProjectionExpression='check_eft, eob_pdf_s3_path, eob_pdf_sha256, captured_at'", c)
        # ...and the optional SharePoint app secret is looked for without an error line.
        r = _read('src/checks/run.py')
        self.assertNotIn("aws_client.get_secret('sharepoint_credentials')", r)
        self.assertIn("get_secret_value(SecretId='prod/helixona/sharepoint_credentials')", r)

    def test_the_results_are_paged_from_the_bottom_too(self):
        c = _read('src/eob/capture.py')
        self.assertIn('NEXT_PAGE_SELECTORS = (', c)
        for sel in ('button:has-text("Show more claims")', 'button[aria-label*="Next page" i]', 'button.mat-paginator-navigation-next',
                    'li.pagination-next a', 'a:has-text("Next")'):
            self.assertIn(sel, c)
        self.assertIn('page.mouse.wheel(0, 20000)', c)
        self.assertIn('def _js_next_number():', c)

    def test_the_walk_waits_for_the_page_not_for_the_clock(self):
        c = _read('src/eob/capture.py')
        self.assertIn('def _wait_for(page, ready, timeout=15.0, poll=0.2):', c)
        self.assertNotIn("page.wait_for_load_state('networkidle', timeout=15000)", c)
        self.assertNotIn("page.wait_for_load_state('networkidle', timeout=20000)", c)
        self.assertNotIn('time.sleep(6)', c)
        self.assertNotIn('time.sleep(3)', c)
        self.assertNotIn("page.wait_for_load_state('networkidle', timeout=20000)", _read('src/blueshield/session.py'))

    def test_the_capture_hands_back_the_checks_it_opened(self):
        c = _read('src/eob/capture.py')
        self.assertIn("'captured_checks': captured_checks, 'failed_checks': failed_checks", c)
        # One check asked for: looked at, then stop — no paging through the rest.
        self.assertIn("            if only:\n                # The one check asked for has been looked at", c)
        self.assertIn("'check data only'", c)
        self.assertNotIn("· PDF {'ok' if s3_path else 'MISSING'}", c)

    def test_the_folders_are_shown_as_the_bot_walked_them(self):
        import importlib, os as _os
        _os.environ.setdefault('SQS_QUEUE_URL', 'x'); _os.environ.setdefault('AWS_ACCESS_KEY_ID', 'x'); _os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'x')
        dash = importlib.import_module('dashboard')
        items = [
            {'check_number': '766832992', 'has_copy': True, 'copy_file': "Posted Checks/2026/01'2026/01-06-2026/Cigna._001.pdf", 'copy_amount': '491.18', 'copy_read_by': 'vision'},
            {'check_number': '766832993', 'has_copy': True, 'copy_file': "Posted Checks/2026/01'2026/01-06-2026/Cigna..pdf", 'copy_amount': '475.19'},
            {'check_number': '4018055', 'has_copy': True, 'copy_file': 'Unposted Checks/07-13-2026/Check No. 0004018055.pdf', 'copy_amount': '304.74'},
            {'check_number': "unreadable:Posted Checks/2026/01'2026/01-09-2026/Helpful resources.pdf", 'has_copy': False,
             'copy_file': "Posted Checks/2026/01'2026/01-09-2026/Helpful resources.pdf", 'copy_problem': 'no check number could be read off the image'},
            {'check_number': '30925163', 'has_copy': False},   # a Blue Shield check with no scan: not a file
        ]
        t = dash._folder_tree(items)
        self.assertEqual((t['total_files'], t['total_checks'], t['total_unreadable']), (4, 3, 1))
        posted = t['tree']['Posted Checks']
        self.assertEqual((posted['total_files'], posted['total_checks'], posted['total_unreadable']), (3, 2, 1))
        jan = posted['children']['2026']['children']["01'2026"]
        self.assertEqual(sorted(c['check_number'] for c in jan['children']['01-06-2026']['checks']), ['766832992', '766832993'])
        self.assertEqual(jan['children']['01-09-2026']['unreadable'][0]['file'], 'Helpful resources.pdf')
        self.assertEqual(t['tree']['Unposted Checks']['children']['07-13-2026']['checks'][0]['check_number'], '4018055')
        d = _read('dashboard.py')
        self.assertIn("@app.route('/api/checks/folders')", d)
        self.assertIn('id="folders-section"', d)
        self.assertIn('function renderFolders()', d)
        self.assertIn("fldSec.hidden = (bot !== 'eob')", d)

    def test_the_team_has_its_own_page(self):
        d = _read('dashboard.py')
        self.assertIn("@app.route('/checks')", d)
        self.assertIn("'dashboard_checks.html'", d)
        self.assertIn('href="/checks" target="_blank"', d)
        h = _read('dashboard_checks.html')
        self.assertIn('<th>Check #</th><th>SharePoint</th><th>Folder · file</th>', h)
        self.assertIn("fetch('/api/checks', {cache: 'no-store'})", h)
        self.assertIn('setInterval(load, 15000);', h)
        self.assertIn('Run in progress', h)
        self.assertIn("resp.headers['Cache-Control'] = 'no-store'", d)
        # The HTML too: a cached copy made a deployed change look missing.
        self.assertIn("resp.headers['Cache-Control'] = 'no-store, must-revalidate'", d)
        self.assertIn('<th>Check #</th><th>Copy in SharePoint</th><th class="num">Amount</th><th>Blue Shield</th><th>eCW</th><th>Verdict</th><th>What to do</th>', d)
        for want in ("fetch('/api/checks', {cache: 'no-store'})", "const issues = r => flags(r).filter(f => String(f) !== 'other payer');",
                     'checks need attention', 'What to do', 'Cashed, not in eCW',
                     'Entered, unposted', 'No copy of check', 'How to read this', 'prefers-color-scheme: dark',
                     "'other payer'", 'eCW is the source of truth', "r.verdict === 'eCW not checked' ? `<span class=\"pill info\">not checked</span>`",
                     'data-theme="dark"', 'href="/api/checks.csv"'):
            self.assertIn(want, h, want)
        self.assertIn('src="/static/helixona-logo.png"', h)
        self.assertTrue(os.path.exists(os.path.join(REPO, 'static', 'helixona-logo.png')))
        self.assertIn('href="/static/helixona-logo.png"', d)   # the favicon, on the main dashboard too
        # Status is never color alone: every pill carries a mark and a word.
        self.assertIn('<span class="pill ${c.cls}">● ${esc(c.label)}</span>', h)

    def test_the_dashboard_offers_the_test_of_one(self):
        d = _read('dashboard.py')
        self.assertIn('<option value="check_test_one" data-bot="eob">', d)
        self.assertIn("const TASK_DISPATCH = { check_test_one: { task_type: 'check_reconcile' } };", d)
        i = d.index('check_test_one: JSON.stringify({')
        tpl = d[i:i + 400]
        for want in ('blue_shield: true', 'limit_checks: 1', 'limit_files: 10', 'ecw: true'):
            self.assertIn(want, tpl)
        self.assertIn("['last run', '🧪 Last run']", d)
        self.assertIn('id="checks-run"', d)
        self.assertIn('from src.checks.reconcile import summarize', d)

    def test_nothing_is_written_to_the_three_systems(self):
        for rel in ('src/checks/run.py', 'src/checks/sharepoint.py', 'src/checks/ecw_payments.py'):
            s = _read(rel)
            for forbidden in ('Payment Advisory', 'Auto Post', 'Single Ins Payment', 'requests.put', 'requests.patch',
                              'requests.delete', 'Download EOB report'):
                self.assertNotIn(forbidden, s, f'{rel}: {forbidden}')

    def test_the_sharepoint_secret_is_documented_and_the_s3_inbox_is_the_fallback(self):
        s = _read('src/checks/sharepoint.py')
        for key in ('tenant_id', 'client_id', 'client_secret', 'site_hostname', 'site_path', 'folder'):
            self.assertIn(key, s)
        self.assertIn("S3_INBOX_PREFIX = 'checks/inbox/'", s)
        self.assertIn("reading the S3 inbox instead", _read('src/checks/run.py'))

    def test_the_dashboard_has_the_checks_table_and_csv(self):
        d = _read('dashboard.py')
        self.assertIn('<option value="check_reconcile" data-bot="eob">', d)
        self.assertIn('id="checks-section"', d)
        self.assertIn("@app.route('/api/checks')", d)
        self.assertIn("@app.route('/api/checks.csv')", d)
        self.assertIn("if (bot === 'eob' && typeof loadChecks === 'function') loadChecks();", d)

    def test_the_dashboard_opens_on_what_does_not_match(self):
        d = _read('dashboard.py')
        self.assertIn("window._checksFilter = window._checksFilter || 'mismatch';", d)
        # The same definition as the team view: what a person must act on.
        self.assertIn("const checkMismatch = r => ['not in eCW', 'unposted'].includes(r.verdict)", d)
        self.assertIn("['mismatch', '⚠ Needs attention']", d)
        self.assertIn('id="checks-tiles"', d)

    def test_the_blue_shield_side_no_longer_needs_the_eob_report(self):
        c = _read('src/eob/capture.py')
        self.assertIn("download_eob = bool(body.get('download_eob', False))", c)
        self.assertIn("pdf_path = _download_eob_pdf(page, check) if download_eob else ''", c)
        self.assertIn("'force': bool(body.get('force', bool(only))), 'download_eob': False", _read('src/checks/run.py'))

    def test_the_table_is_declared_and_self_creating(self):
        self.assertIn("CHECKS_TABLE = 'helixona-checks'", _read('src/checks/run.py'))
        self.assertIn('def ensure_table(aws_client):', _read('src/checks/run.py'))
        if os.path.exists(os.path.join(REPO, 'setup_dynamodb.py')):
            self.assertIn('"TableName": "helixona-checks"', _read('setup_dynamodb.py'))


if __name__ == '__main__':
    unittest.main()
