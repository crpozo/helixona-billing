"""Remittance — the cheque reconciliation: copies · Blue Shield · eCW.

The operator's procedure (2026-09-16): read every cheque image in the
SharePoint folder, see whether Blue Shield cashed it, flag the cashed
cheques we hold no copy of, and see whether eCW has a payment under that
number. Posted / unposted / not in eCW, per cheque.
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


class ChequeNumbersAreComparable(unittest.TestCase):
    def test_leading_zeros_and_labels_fall_away(self):
        self.assertEqual(norm_check('0031401901'), '31401901')
        self.assertEqual(norm_check('31401901 Check/EFT information'), '31401901')
        self.assertEqual(norm_check(''), '')
        self.assertEqual(norm_check('0000'), '0')


class TheVerdictPerCheque(unittest.TestCase):
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
        self.assertEqual(by['99999999']['verdict'], 'copy only')
        self.assertEqual((summary['posted'], summary['unposted'], summary['not_in_ecw'], summary['not_cashed'], summary['copy_only']),
                         (1, 1, 1, 1, 1))

    def test_a_cashed_cheque_without_an_image_is_flagged(self):
        by, summary = self._rows()
        self.assertIn('no copy of the cheque', by['31406730']['flags'])
        self.assertNotIn('no copy of the cheque', by['30912971']['flags'])
        # Not cashed yet: nothing to hold a copy of, no flag.
        self.assertNotIn('no copy of the cheque', by['31401901']['flags'])
        self.assertEqual(summary['no_copy'], 1)

    def test_the_copy_pins_the_cheque_despite_leading_zeros(self):
        by, _ = self._rows()
        self.assertTrue(by['30912971']['has_copy'])
        self.assertEqual(by['30912971']['copy_amount'], '275.09')

    def test_amounts_that_disagree_are_flagged(self):
        copies = [dict(self.COPIES[0], amount='257.09')]
        rows, summary = reconcile(copies, self.CHEQUES[:1], self.PAYMENTS[:1])
        self.assertTrue(any(f.startswith('amounts differ') for f in rows[0]['flags']))
        self.assertEqual(summary['amount_mismatch'], 1)

    def test_when_ecw_was_not_consulted_nothing_is_called_missing(self):
        by, _ = self._rows(ecw_checked=False)
        self.assertEqual(by['31406730']['verdict'], 'eCW not checked')

    def test_the_tiles_count_over_whatever_rows_they_are_given(self):
        # The dashboard counts over the whole table, not over the last run,
        # so a test of 1 does not make the tiles say "1 cheque".
        rows, _ = reconcile(self.COPIES, self.CHEQUES, self.PAYMENTS)
        table_rows = rows + [{'check_number': '7', 'verdict': 'posted', 'flags': ['amounts differ: copy 1.00, bs 2.00']}]
        sm = summarize(table_rows)
        self.assertEqual((sm['checks'], sm['posted'], sm['amount_mismatch'], sm['no_copy']), (6, 2, 1, 1))
        self.assertEqual(summarize([]), {'checks': 0, 'posted': 0, 'unposted': 0, 'not_in_ecw': 0, 'not_cashed': 0,
                                         'copy_only': 0, 'no_copy': 0, 'amount_mismatch': 0})


class TheImageReaderIsShapeChecked(unittest.TestCase):
    def test_the_models_json_is_taken_only_when_it_is_a_cheque_number(self):
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

    def test_a_bare_number_is_not_a_cheque_number(self):
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
    """One cheque from Blue Shield, compared with the copies and eCW, its
    row alone written — while the tiles keep counting the whole table."""
    CHEQUE = {'check_eft': '30925163', 'check_amount': '227.20', 'check_status': 'Check Cashed',
              'check_date': '04/17/2026', 'cashed_date': '07/27/2026'}

    def _run(self, body, find=lambda page, ck, since, navigate=True: [], capture=None, copies=None):
        checks = _Table('helixona-checks', [
            {'check_number': '11111111', 'verdict': 'posted', 'flags': [], 'has_copy': True, 'reconciled_at': 'before'},
            {'check_number': '22222222', 'verdict': 'not in eCW', 'flags': ['no copy of the cheque'], 'reconciled_at': 'before'}])
        eobs = _Table('helixona-eobs', [self.CHEQUE, {'check_eft': '11111111', 'check_amount': '1.00',
                                                      'check_status': 'Check Cashed', 'check_date': '01/01/2026',
                                                      'cashed_date': '01/02/2026'}])
        aws = _Aws([checks, eobs])

        def fake_capture(page, aws_client, cap_body):
            self.capture_body = cap_body
            return capture if capture is not None else {'ok': True, 'captured_checks': ['30925163']}

        def fake_copies(aws_client, table, body, prefer=()):
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

    def test_one_cheque_end_to_end(self):
        result, checks = self._run({'blue_shield': True, 'limit_checks': 1})
        self.assertEqual(result['targets'], ['30925163'])
        self.assertEqual(self.capture_body['limit_checks'], 1)
        self.assertTrue(self.capture_body['force'], 'a test re-opens the cheque for its status today')
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
            find=lambda page, ck, since, navigate=True: [{'check_no': ck, 'amount': '227.20', 'posted': '227.20',
                                                          'unposted': '0.00', 'payment_id': '5050'}])
        row = checks.items['30925163']
        self.assertEqual((row['verdict'], row['ecw_payment_id'], row['flags']), ('posted', '5050', []))
        self.assertIn('30925163 on file', checks.items['_run']['steps']['ecw'])

    def test_a_named_cheque_needs_no_portal_walk(self):
        result, checks = self._run({'blue_shield': False, 'check_eft': '30925163'})
        self.assertFalse(hasattr(self, 'capture_body'))
        self.assertEqual(checks.items['30925163']['verdict'], 'not in eCW')
        self.assertEqual(checks.items['_run']['mode'], 'test of 1')

    def test_nothing_captured_means_nothing_compared(self):
        result, checks = self._run({'blue_shield': True, 'limit_checks': 1}, capture={'ok': False, 'reason': 'login'})
        self.assertEqual(result, {'ok': False, 'reason': 'no cheque captured'})
        self.assertNotIn('30925163', checks.items)
        self.assertEqual(checks.items['_run']['steps']['done'], 'stopped')


class TheTaskIsWiredReadOnly(unittest.TestCase):
    def test_the_bot_runs_it_and_opens_one_browser_on_demand(self):
        src = _read('src/main.py')
        self.assertIn("EOB_TASKS = {'eob_capture', 'eob_post', 'check_reconcile'}", src)
        i = src.index("elif task_type == 'check_reconcile':")
        block = src[i:i + 1500]
        self.assertIn('run_check_reconcile(aws_client, body, login=_perform_ecw_login, get_page=get_page)', block)
        # One Chrome on the profile dir: the Blue Shield and eCW steps share it.
        self.assertIn("if not holder.get('manager'):\n                holder['manager'] = BrowserManager().start(proxy_config=None)", block)

    def test_the_capture_hands_back_the_cheques_it_opened(self):
        c = _read('src/eob/capture.py')
        self.assertIn("'captured_checks': captured_checks, 'failed_checks': failed_checks", c)
        # One cheque asked for: looked at, then stop — no paging through the rest.
        self.assertIn("            if only:\n                # The one cheque asked for has been looked at", c)
        self.assertIn("'cheque data only'", c)
        self.assertNotIn("· PDF {'ok' if s3_path else 'MISSING'}", c)

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

    def test_the_dashboard_has_the_cheques_table_and_csv(self):
        d = _read('dashboard.py')
        self.assertIn('<option value="check_reconcile" data-bot="eob">', d)
        self.assertIn('id="checks-section"', d)
        self.assertIn("@app.route('/api/checks')", d)
        self.assertIn("@app.route('/api/checks.csv')", d)
        self.assertIn("if (bot === 'eob' && typeof loadChecks === 'function') loadChecks();", d)

    def test_the_dashboard_opens_on_what_does_not_match(self):
        d = _read('dashboard.py')
        self.assertIn("window._checksFilter = window._checksFilter || 'mismatch';", d)
        self.assertIn("const checkMismatch = r => r.verdict !== 'posted' || (r.flags || []).length > 0;", d)
        self.assertIn('id="checks-tiles"', d)

    def test_the_blue_shield_side_no_longer_needs_the_eob_report(self):
        c = _read('src/eob/capture.py')
        self.assertIn("download_eob = bool(body.get('download_eob', False))", c)
        self.assertIn("pdf_path = _download_eob_pdf(page, check) if download_eob else ''", c)
        d = _read('dashboard.py')
        self.assertIn('download_eob: false', d)
        self.assertIn('Collect cheques from Blue Shield', d)

    def test_the_table_is_declared_and_self_creating(self):
        self.assertIn("CHECKS_TABLE = 'helixona-checks'", _read('src/checks/run.py'))
        self.assertIn('def ensure_table(aws_client):', _read('src/checks/run.py'))
        if os.path.exists(os.path.join(REPO, 'setup_dynamodb.py')):
            self.assertIn('"TableName": "helixona-checks"', _read('setup_dynamodb.py'))


if __name__ == '__main__':
    unittest.main()
