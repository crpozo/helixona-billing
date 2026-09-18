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
        self.assertIn("page.keyboard.press('Enter')", p)
        self.assertIn('def _read_value(frm):', p)

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

    def test_one_cheque_end_to_end(self):
        result, checks = self._run({'blue_shield': True, 'limit_checks': 1})
        self.assertEqual(result['targets'], ['30925163'])
        self.assertEqual(self.capture_body['limit_checks'], 1)
        self.assertFalse(self.capture_body['force'], 'an unnamed test takes the next cheque not yet on file')
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

    def test_a_named_cheque_is_reopened_for_its_status_today(self):
        self._run({'blue_shield': True, 'check_eft': '30925163'})
        self.assertTrue(self.capture_body['force'])
        self.assertEqual(self.capture_body['check_eft'], '30925163')

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
            if '/Files?' in u and 'Insurance Checks/2026' in u:
                return _Resp({'value': [{'Name': 'Check 30925163.pdf', 'ServerRelativeUrl': _Page.FOLDER + '/2026/Check 30925163.pdf',
                                          'Length': '120', 'UniqueId': 'u2', 'ETag': '"2"', 'TimeLastModified': '2026-09-15T10:00:00Z'}]})
            if '/Files?' in u:
                return _Resp({'value': [{'Name': 'notes.txt', 'ServerRelativeUrl': _Page.FOLDER + '/notes.txt', 'Length': '3', 'UniqueId': 'u0', 'ETag': '"0"'},
                                        {'Name': 'Check 4022519.jpg', 'ServerRelativeUrl': _Page.FOLDER + '/Check 4022519.jpg',
                                         'Length': '99', 'UniqueId': 'u1', 'ETag': '"1"', 'TimeLastModified': '2026-09-16T10:00:00Z'}]})
            if '/Folders?' in u and 'Insurance Checks/2026' in u:
                return _Resp({'value': []})
            if '/Folders?' in u:
                return _Resp({'value': [{'Name': 'Forms', 'ServerRelativeUrl': _Page.FOLDER + '/Forms'},
                                        {'Name': 'Insurance Check Tracker', 'ServerRelativeUrl': _Page.FOLDER + '/Insurance Check Tracker'},
                                        {'Name': '2026', 'ServerRelativeUrl': _Page.FOLDER + '/2026'},
                                        {'Name': "01'2026", 'ServerRelativeUrl': _Page.FOLDER + "/01'2026"}]})
            if u.endswith('Check 4022519.jpg'):
                return _Resp(body=b'JPEGBYTES')
            return _Resp(status=404)

    got = []


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
        self.assertFalse(any('Forms' in u and '/Files?' in u for u in _Page.got))

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

    def test_an_unreadable_file_is_tried_again_and_is_not_a_cheque(self):
        r = _read('src/checks/run.py')
        self.assertIn("if it.get('copy_file') and it.get('has_copy'):", r)
        self.assertIn("startswith(('_', 'unreadable:'))", r)
        self.assertIn("startswith(('_', 'unreadable:'))", _read('dashboard.py'))
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

    def test_a_cheque_captured_without_a_pdf_is_on_file(self):
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

    def test_the_capture_hands_back_the_cheques_it_opened(self):
        c = _read('src/eob/capture.py')
        self.assertIn("'captured_checks': captured_checks, 'failed_checks': failed_checks", c)
        # One cheque asked for: looked at, then stop — no paging through the rest.
        self.assertIn("            if only:\n                # The one cheque asked for has been looked at", c)
        self.assertIn("'cheque data only'", c)
        self.assertNotIn("· PDF {'ok' if s3_path else 'MISSING'}", c)

    def test_the_team_has_its_own_page(self):
        d = _read('dashboard.py')
        self.assertIn("@app.route('/checks')", d)
        self.assertIn("'dashboard_checks.html'", d)
        self.assertIn('href="/checks" target="_blank"', d)
        h = _read('dashboard_checks.html')
        for want in ("fetch('/api/checks')", 'cheques need attention', 'What to do', 'Cashed, not in eCW',
                     'Entered, unposted', 'No copy of cheque', 'How to read this', 'prefers-color-scheme: dark',
                     'data-theme="dark"', 'href="/api/checks.csv"'):
            self.assertIn(want, h, want)
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
        self.assertIn("'force': bool(body.get('force', bool(only))), 'download_eob': False", _read('src/checks/run.py'))

    def test_the_table_is_declared_and_self_creating(self):
        self.assertIn("CHECKS_TABLE = 'helixona-checks'", _read('src/checks/run.py'))
        self.assertIn('def ensure_table(aws_client):', _read('src/checks/run.py'))
        if os.path.exists(os.path.join(REPO, 'setup_dynamodb.py')):
            self.assertIn('"TableName": "helixona-checks"', _read('setup_dynamodb.py'))


if __name__ == '__main__':
    unittest.main()
