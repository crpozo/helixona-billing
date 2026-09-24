"""Remittance — a scan holding many checks with no deposit slip.

2026-09-23, the operator, of Unposted Checks/08-18-2026/08192026143346.pdf
(25 EOBs scanned together, each with its check on its first page; check
31291665 is the 22nd): "this file has many checks and the check number
31291665 is there as well. So you have to check all the document. And with
the help of the tracker you can check which checks are missing."
"""
import os
import unittest
from unittest import mock

from src.checks import read_check as rc

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def _check(num, amount, claim=''):
    return {'kind': 'check', 'confidence': 'high',
            'check': {'check_number': num, 'amount': amount, 'check_date': '08/07/2026', 'payer': 'Blue Shield of California',
                      'payee': '', 'claim_number': claim, 'confidence': 'high'}}


OTHER = {'kind': 'other', 'check': {}}


class EveryPageIsLookedAt(unittest.TestCase):
    def _read(self, pages, n, texts=None, path='08192026143346.pdf'):
        calls = []

        def read_page(aws, p, page_no, model_id=None):
            calls.append(page_no)
            return pages.get(page_no, OTHER), ''
        with mock.patch.object(rc, 'pdf_page_count', return_value=n), \
                mock.patch.object(rc, 'pdf_page_texts', return_value=texts or []), \
                mock.patch.object(rc, 'image_payload', return_value=('', 'image/png')), \
                mock.patch.object(rc, 'read_page', side_effect=read_page):
            return rc.read_check(None, path), calls

    def test_a_stack_of_eobs_with_their_checks_is_every_check(self):
        # Four pages an EOB, the check at the foot of page 1 of each; the
        # 22nd check is on page 85. The old reader stopped at the first.
        pages = {0: _check('31291664', '269.01', '264411443300'), 4: _check('31291658', '103.18'),
                 84: _check('31291665', '71.72', '264411415100'), 96: _check('31291663', '208.39')}
        got, calls = self._read(pages, 100)
        self.assertEqual(got['kind'], 'deposit')
        self.assertFalse(got['slip'])
        self.assertEqual([c['check_number'] for c in got['checks']], ['31291664', '31291658', '31291665', '31291663'])
        by = {c['check_number']: c for c in got['checks']}
        self.assertEqual((by['31291665']['page'], by['31291665']['amount'], by['31291665']['claim_number']), (85, '71.72', '264411415100'))
        self.assertEqual(got['deposit_total'], '652.30')
        self.assertEqual(got['deposit_date'], '')
        self.assertEqual(len(calls), 100, 'every page was looked at')

    def test_one_check_on_the_second_page_of_two_is_one_check(self):
        got, calls = self._read({1: _check('30900703', '200.93')}, 2)
        self.assertEqual((got['kind'], got['check_number'], got['amount'], got['page']), ('check', '30900703', '200.93', 2))
        self.assertEqual(calls, [0, 1])

    def test_the_same_check_on_two_pages_is_one_check(self):
        # The check and its stub, or a page scanned twice.
        got, _ = self._read({0: _check('30900703', '200.93'), 1: _check('0030900703', '200.93')}, 2)
        self.assertEqual((got['kind'], got['check_number']), ('check', '30900703'))

    def test_a_text_layer_spares_the_model(self):
        texts = ['', 'Check Number: 31291665 Amount: $71.72 Claim Number: 264411415100', '']
        got, calls = self._read({0: _check('31291664', '269.01')}, 3, texts=texts)
        self.assertEqual(got['kind'], 'deposit')
        by = {c['check_number']: c for c in got['checks']}
        self.assertEqual((by['31291665']['read_by'], by['31291665']['claim_number']), ('pdf_text', '264411415100'))
        self.assertEqual(calls, [0, 2], 'page 2 was read from its text')

    def test_a_slip_on_page_one_is_still_a_deposit(self):
        slip = {'kind': 'deposit_slip', 'check': {}, 'confidence': 'medium',
                'deposit': {'date': '04/22/2026', 'total': '271.65', 'checks': [{'check_number': '30880301', 'amount': '71.36'}]}}
        got, _ = self._read({0: slip, 1: _check('30880301', '71.36'), 2: _check('30880303', '200.29')}, 3)
        self.assertEqual((got['kind'], got['slip'], got['deposit_date']), ('deposit', True, '04/22/2026'))

    def test_nothing_on_any_page_is_unreadable_not_a_check(self):
        got, calls = self._read({}, 3)
        self.assertEqual(got['kind'], 'check')
        self.assertEqual(got['check_number'], '')
        self.assertIn('pages 1, 2, 3 of 3', got['problem'])

    def test_an_api_refusal_stops_the_file_at_once(self):
        with mock.patch.object(rc, 'pdf_page_count', return_value=100), mock.patch.object(rc, 'pdf_page_texts', return_value=[]), \
                mock.patch.object(rc, 'image_payload', return_value=('', 'image/png')), \
                mock.patch.object(rc, 'read_page', side_effect=RuntimeError('credit balance is too low')) as reader:
            got = rc.read_check(None, 'x.pdf')
        self.assertEqual(got['error_kind'], 'api')
        self.assertEqual(reader.call_count, 1)

    def test_the_prompt_knows_a_check_at_the_foot_of_an_eob(self):
        self.assertIn('or printed at the bottom of an Explanation of Benefits page', rc.PROMPT)
        self.assertIn('"claim_number"', rc.PROMPT)
        self.assertGreaterEqual(rc.MAX_PAGES, 100)


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
        return {'Items': list(self.items.values())}


class _Aws:
    def __init__(self, table):
        self.dynamodb = mock.Mock()
        self.dynamodb.Table = lambda name: table


class TheRunStoresTheBundle(unittest.TestCase):
    FILE = 'Unposted Checks/08-18-2026/08192026143346.pdf'
    BUNDLE = {'kind': 'deposit', 'slip': False, 'deposit_date': '', 'deposit_total': '340.73', 'pages': 100, 'read_by': 'vision',
              'confidence': '', 'problem': '',
              'checks': [{'check_number': '31291664', 'amount': '269.01', 'check_date': '08/07/2026', 'payer': 'Blue Shield of California',
                          'payee': '', 'claim_number': '264411443300', 'page': 1, 'from_slip': False},
                         {'check_number': '31291665', 'amount': '71.72', 'check_date': '08/07/2026', 'payer': 'Blue Shield of California',
                          'payee': '', 'claim_number': '264411415100', 'page': 85, 'from_slip': False}]}

    def test_a_multi_page_pdf_read_as_one_check_before_is_read_again(self):
        from src.checks import run as run_mod
        table = _Table([
            # The first reader stopped at the first check; the pages were counted.
            {'check_number': '31291664', 'has_copy': True, 'copy_file': self.FILE, 'copy_etag': '"3"', 'copy_pages': 100, 'kind': 'check'},
            # A one-page scan, read the same way: nothing more to see.
            {'check_number': '30925163', 'has_copy': True, 'copy_file': 'Posted Checks/2026/Check 30925163.pdf', 'copy_etag': '"1"', 'copy_pages': 1},
            # Read since every page is looked at: left alone.
            {'check_number': '30900703', 'has_copy': True, 'copy_file': 'Posted Checks/2026/two.pdf', 'copy_etag': '"2"', 'copy_pages': 2,
             'copy_read_mode': 'all_pages'}])
        files = [{'path': self.FILE, 'etag': '"3"', 'web_url': 'https://sp/batch.pdf'},
                 {'path': 'Posted Checks/2026/Check 30925163.pdf', 'etag': '"1"', 'web_url': 'https://sp/one.pdf'},
                 {'path': 'Posted Checks/2026/two.pdf', 'etag': '"2"', 'web_url': 'https://sp/two.pdf'}]
        with mock.patch.object(run_mod, '_sources', return_value=(files, lambda it, d: '/tmp/' + it['path'].split('/')[-1], 'sharepoint')), \
                mock.patch.object(run_mod, 'read_check', return_value=self.BUNDLE) as reader:
            read, skipped, failed = run_mod.read_new_copies(_Aws(table), table, {})
        self.assertEqual(reader.call_count, 1, 'only the hundred-page file was read again')
        self.assertEqual((read, skipped, failed), (2, 2, 0))
        dep = table.items[f'deposit:{self.FILE}']
        self.assertEqual((dep['kind'], dep['deposit_slip'], dep['deposit_total'], dep['deposit_count'], dep['deposit_checks']),
                         ('deposit', False, '340.73', 2, ['31291664', '31291665']))
        row = table.items['31291665']
        self.assertEqual((row['has_copy'], row['copy_amount'], row['copy_page'], row['copy_claim'], row['deposit_file'], row['deposit_slip'],
                          row['copy_read_mode']),
                         (True, '71.72', 85, '264411415100', self.FILE, False, 'all_pages'))
        # The next run reads nothing again.
        with mock.patch.object(run_mod, '_sources', return_value=(files, lambda it, d: '/tmp/x', 'sharepoint')), \
                mock.patch.object(run_mod, 'read_check', side_effect=AssertionError('read twice')):
            self.assertEqual(run_mod.read_new_copies(_Aws(table), table, {}), (0, 3, 0))

    def test_both_pages_show_a_batch_apart_from_a_deposit(self):
        d = _read('dashboard.py')
        self.assertIn("'slip': d.get('deposit_slip', True) is not False,", d)
        self.assertIn("r.deposit_slip === false ? ` · 📎 batch of", d)
        self.assertIn("'deposit_file', 'deposit_slip', 'deposit_total', 'copy_page', 'copy_claim'", d)
        h = _read('dashboard_checks.html')
        self.assertIn("d.slip === false ? '📎 Batch scan · several checks in one file' : '🏦 Bank deposit'", h)
        self.assertIn('<strong>📎 Batch scan</strong>', h)


class TheTrackerHasAClaimColumnNow(unittest.TestCase):
    def test_the_claim_number_and_the_patient_checks(self):
        from src.checks.tracker import rows_from_sheet
        rows = [['Date Check Received', 'Check Date', 'Deposit Date', 'Insurance Company', 'Check Number', 'Amount Paid', 'Claim Number'],
                ['08/19/2026', '08/07/2026', '08/21/2026', 'Blue Shield of California', 31291665.0, 71.72, 264411415100.0],
                ['12/16/2025', '12/16/2026', '12/16/2025', 'Jennifer Waters (Patient Check)', 738, 408.05, None]]
        got = rows_from_sheet('08_2026', rows)
        self.assertEqual([(g['check_no'], g['claim_no']) for g in got], [('31291665', '264411415100'), ('738', '')])
        self.assertEqual(got[0]['posted'], '')

    def test_the_claim_reaches_the_row(self):
        from src.checks.reconcile import reconcile
        rows, _ = reconcile(copies=[], checks=[], payments=[], tracker=[{'check_no': '31291665', 'amount': '71.72', 'claim_no': '264411415100'}])
        self.assertEqual(rows[0]['tracker_claim'], '264411415100')
        self.assertIn('tracker_claim = :itc', _read('src/checks/run.py'))


if __name__ == '__main__':
    unittest.main()


class TheDownloadIsTheFile(unittest.TestCase):
    """2026-09-24: "No /Root object! - Is this really a PDF?" — the plain URL
    of 12292025_EXPLANATION OF BENEFITS This is NOT aBill_004.pdf answered
    with a page, not the PDF, and the reader blamed the file."""

    def _page(self, answers):
        class Resp:
            def __init__(self, body, ctype='application/octet-stream', status=200):
                self._b, self.status, self.ok = body, status, status < 400
                self.headers = {'content-type': ctype}

            def body(self):
                return self._b
        page = mock.Mock()
        page.request.get = mock.Mock(side_effect=[Resp(*a) for a in answers])
        return page

    def test_a_page_instead_of_the_pdf_is_fetched_again_by_the_api(self):
        import tempfile
        from src.checks import sharepoint_browser as spb
        item = {'name': 'x.pdf', 'path': 'Posted Checks/2025/x.pdf', 'download_url': 'https://sp/x.pdf', 'api_url': 'https://sp/_api/$value'}
        page = self._page([(b'<!DOCTYPE html><html>viewer', 'text/html'), (b'%PDF-1.4 real', 'application/pdf')])
        with tempfile.TemporaryDirectory() as d:
            path = spb.download(page, item, d)
            self.assertEqual(open(path, 'rb').read(), b'%PDF-1.4 real')
        self.assertEqual(page.request.get.call_args_list[1].args[0], 'https://sp/_api/$value')

    def test_neither_answer_being_the_file_names_what_came_back(self):
        import tempfile
        from src.checks import sharepoint_browser as spb
        item = {'name': 'x.pdf', 'path': 'p/x.pdf', 'download_url': 'https://sp/x.pdf', 'api_url': 'https://sp/api'}
        page = self._page([(b'<html>', 'text/html'), (b'', 'text/plain', 404)])
        with tempfile.TemporaryDirectory() as d, self.assertRaises(RuntimeError) as cm:
            spb.download(page, item, d)
        self.assertIn('HTTP 404', str(cm.exception))
        self.assertIn('p/x.pdf', str(cm.exception))

    def test_every_listed_file_carries_the_api_url(self):
        from src.checks.sharepoint_browser import looks_like
        s = _read('src/checks/sharepoint_browser.py')
        self.assertIn("'api_url': (f\"{site}/_api/web/GetFileByServerRelativePath(decodedurl=@f)/$value\"", s)
        self.assertTrue(looks_like('a.pdf', b'%PDF-1.7'))
        self.assertFalse(looks_like('a.pdf', b'<!DOCTYPE'))
        self.assertTrue(looks_like('a.heic', b'anything'))
