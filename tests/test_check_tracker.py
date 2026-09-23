"""Remittance — the Insurance Check Tracker, the team's own sheet of checks
received (2026-09-23): "the checks are not always in the exact folder, so
the idea is to search all of them and match"."""
import datetime
import os
import tempfile
import unittest

from src.checks.reconcile import reconcile, summarize
from src.checks.tracker import _guid_from, parse_tracker, rows_from_sheet, since_filter

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


HEADER = ['Date Check Received', 'Check Date', 'Deposit Date', 'Insurance Company',
          'Check Number', 'Amount Paid', 'Posted to Account (Yes/No)']


class TheSheetIsReadByItsHeaders(unittest.TestCase):
    def test_the_team_columns(self):
        rows = [['Insurance Check Tracker'], [], HEADER,
                [datetime.datetime(2026, 6, 3), datetime.datetime(2026, 5, 28), datetime.datetime(2026, 6, 4),
                 'Blue Shield', 4121991.0, 1234.5, 'Yes'],
                ['06/10/2026', '', '', 'Cigna', '0031407766', '$2,000.00', 'No'],
                ['', '', '', '', 'TOTAL', 3234.5, '']]
        got = rows_from_sheet('2026', rows)
        # The title row and the total are not checks; Excel's 4121991.0 is 4121991.
        self.assertEqual([g['check_no'] for g in got], ['4121991', '0031407766'])
        first = got[0]
        self.assertEqual((first['received'], first['check_date'], first['deposit_date']),
                         ('06/03/2026', '05/28/2026', '06/04/2026'))
        self.assertEqual((first['payer'], first['amount'], first['posted']), ('Blue Shield', '1234.50', 'Yes'))
        self.assertEqual((first['sheet'], first['row']), ('2026', 4))
        self.assertEqual(got[1]['amount'], '2000.00')

    def test_check_date_is_not_the_check_number(self):
        got = rows_from_sheet('s', [['Check Date', 'Check Number', 'Amount Paid'], ['01/02/2026', '12345', '10']])
        self.assertEqual(got[0]['check_no'], '12345')
        self.assertEqual(got[0]['check_date'], '01/02/2026')

    def test_a_sheet_without_the_header_gives_nothing(self):
        self.assertEqual(rows_from_sheet('notes', [['a', 'b'], ['1', '2']]), [])

    def test_every_sheet_of_the_workbook(self):
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active.title = '2025'
        wb.active.append(HEADER)
        wb.active.append(['07/15/2025', '', '', 'Aetna', 555001, 10, 'Yes'])
        ws = wb.create_sheet('2026')
        ws.append(HEADER)
        ws.append(['06/03/2026', '', '', 'Blue Shield', 4121991, 20, 'No'])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 't.xlsx')
            wb.save(path)
            got = parse_tracker(path)
        self.assertEqual([(g['sheet'], g['check_no']) for g in got], [('2025', '555001'), ('2026', '4121991')])

    def test_the_file_id_comes_from_the_excel_online_url(self):
        url = ('https://helixona.sharepoint.com/sites/BillingDepartment/_layouts/15/Doc.aspx?'
               'sourcedoc=%7B9F7ED140-2C4C-4B31-9A2F-5A5A6B1C2D3E%7D&file=Tracker.xlsx&action=default')
        self.assertEqual(_guid_from(url), '9F7ED140-2C4C-4B31-9A2F-5A5A6B1C2D3E')
        self.assertEqual(_guid_from('https://helixona.sharepoint.com/:x:/s/BillingDepartment/IQCR?e=x'), '')

    def test_only_the_reconciliation_window(self):
        rows = [{'check_no': '1', 'received': '06/30/2025'}, {'check_no': '2', 'received': '07/01/2025'},
                {'check_no': '3', 'received': ''}, {'check_no': '4', 'received': '', 'check_date': '1/5/26'}]
        # An undated row is kept: one lookup too many beats a check dropped.
        self.assertEqual([r['check_no'] for r in since_filter(rows, '07/01/2025')], ['2', '3', '4'])


class TheTrackerMeetsTheScansOnTheNumber(unittest.TestCase):
    def test_logged_checks_are_not_missing_copies(self):
        rows, sm = reconcile(
            copies=[{'check_number': '4121991', 'amount': '1234.50', 'folder': 'Unposted Checks/05-20-2026'}],
            checks=[{'check_eft': '4121991', 'check_amount': '1234.50', 'check_status': 'Check Cashed', 'cashed_date': '06/05/2026'},
                    {'check_eft': '4065349', 'check_amount': '50.00', 'check_status': 'Check Cashed', 'cashed_date': '06/05/2026'},
                    {'check_eft': '99999', 'check_amount': '5.00', 'check_status': 'Check Cashed', 'cashed_date': '06/05/2026'}],
            payments=[], tracker=[{'check_no': '4121991', 'amount': '1234.50', 'received': '06/03/2026', 'sheet': '2026', 'row': 4},
                                  {'check_no': '4065349', 'amount': '50.00', 'received': '06/03/2026'}])
        by = {r['check_number']: r for r in rows}
        # Filed in another date folder than its tracker dates: still matched, nothing missing.
        self.assertTrue(by['4121991']['in_tracker'])
        self.assertEqual(by['4121991']['tracker_sheet'], '2026:4')
        self.assertFalse(any(f.startswith(('no copy', 'in tracker')) for f in by['4121991']['flags']))
        # Logged as received, no scan anywhere: find it, not "we do not have it".
        self.assertIn('in tracker, no scan found', by['4065349']['flags'])
        self.assertNotIn('no copy of the check', by['4065349']['flags'])
        # Cashed, not scanned, not logged: the real missing copy.
        self.assertIn('no copy of the check', by['99999']['flags'])
        self.assertEqual((sm['in_tracker'], sm['tracker_no_scan'], sm['no_copy']), (2, 1, 1))

    def test_a_logged_check_is_not_835_only(self):
        rows, _ = reconcile(copies=[], checks=[], payments=[], eras=[{'check_no': '31407766', 'amount': '10'}],
                            tracker=[{'check_no': '31407766', 'amount': '10'}])
        self.assertEqual(rows[0]['verdict'], 'not in eCW')

    def test_the_tracker_amount_is_compared(self):
        rows, _ = reconcile(copies=[{'check_number': '12345', 'amount': '10.00'}], checks=[], payments=[],
                            tracker=[{'check_no': '12345', 'amount': '11.00'}])
        self.assertTrue(any(f.startswith('amounts differ') for f in rows[0]['flags']))

    def test_both_pages_and_the_run_show_it(self):
        d = _read('dashboard.py')
        self.assertIn("['tracker', 'Tracker']", d)
        self.assertIn("tile('in tracker, no scan', sm.tracker_no_scan, 'tracker no scan'", d)
        self.assertIn("'in_tracker', 'tracker_received', 'tracker_deposit'", d)
        h = _read('dashboard_checks.html')
        self.assertIn("case 'tracker no scan': return hasFlag(r, 'in tracker, no scan');", h)
        self.assertIn("hasFlag(r, 'in tracker, no scan') || hasFlag(r, 'amounts differ')", h)
        r = _read('src/checks/run.py')
        self.assertIn('tracker_rows = since_filter(tracker_rows, since)', r)
        self.assertIn('if not tracker_read:', r)
        # Read only: the tracker is downloaded, never written.
        t = _read('src/checks/tracker.py')
        self.assertNotIn('.post(', t)
        self.assertNotIn('.put(', t)


if __name__ == '__main__':
    unittest.main()
