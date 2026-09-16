"""Remittance — the cheque reconciliation: copies · Blue Shield · eCW.

The operator's procedure (2026-09-16): read every cheque image in the
SharePoint folder, see whether Blue Shield cashed it, flag the cashed
cheques we hold no copy of, and see whether eCW has a payment under that
number. Posted / unposted / not in eCW, per cheque.
"""
import os
import unittest

from src.checks.ecw_payments import rows_to_payments
from src.checks.read_check import parse_check_text, parse_model_json
from src.checks.reconcile import norm_check, reconcile

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


class TheTaskIsWiredReadOnly(unittest.TestCase):
    def test_the_bot_runs_it_and_opens_ecw_only_on_demand(self):
        src = _read('src/main.py')
        self.assertIn("EOB_TASKS = {'eob_capture', 'eob_post', 'check_reconcile'}", src)
        i = src.index("elif task_type == 'check_reconcile':")
        block = src[i:i + 1500]
        self.assertIn('run_check_reconcile(aws_client, body, login=_perform_ecw_login, get_page=get_page)', block)
        self.assertIn("holder['manager'] = BrowserManager().start(proxy_config=None)", block)

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
