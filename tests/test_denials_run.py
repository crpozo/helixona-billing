"""Denials — the fourth bot: eCW's denied claims, the reasons, the SOP's
answer (2026-09-30). The wiring, pinned."""
import os
import unittest
from unittest import mock

from src.denials import ecw, run
from src.denials.cheatsheet import load_rules

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


class _Table:
    def __init__(self, items=()):
        self.items = {i['claim_id']: dict(i) for i in items}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None):
        row = self.items.setdefault(Key['claim_id'], {'claim_id': Key['claim_id']})
        names = ExpressionAttributeNames or {}
        for part in UpdateExpression.replace('SET ', '').split(', '):
            k, v = [x.strip() for x in part.split('=')]
            row[names.get(k, k)] = ExpressionAttributeValues[v]

    def scan(self, **kw):
        return {'Items': list(self.items.values())}


class _Aws:
    def __init__(self, table):
        self.table = table
        self.dynamodb = mock.Mock()
        class _T:
            name = 'helixona-denials'
        self.dynamodb.tables.all = lambda: [_T()]
        self.dynamodb.Table = lambda name: table

    def get_secret(self, name):
        return {'username': 'u', 'password': 'p'}


class TheRunLinksTheThreeSteps(unittest.TestCase):
    def test_a_denied_claim_gets_its_sop_action(self):
        table = _Table([{'claim_id': '8100', 'denial_codes': ['CO-16'], 'denial_text': 'old'}])
        claims = [{'claim_id': '8100', 'patient': 'DOE, JANE', 'payer': 'Medicare', 'dos': '08/01/2026', 'cpt': 'J0612 96365',
                   'status': 'ERA Payer Denied', 'charges': '425.00', 'paid': '0.04', 'adjustment': '', 'balance': '424.96'},
                  {'claim_id': '8101', 'patient': 'ROE, RICK', 'payer': 'Anthem Blue Cross', 'dos': '08/02/2026', 'cpt': 'J3490',
                   'status': 'ERA Payer Denied', 'charges': '300.00', 'paid': '', 'adjustment': '', 'balance': '300.00'},
                  {'claim_id': '8102', 'patient': 'POE, PAT', 'payer': 'Cigna', 'dos': '08/03/2026', 'cpt': '96365',
                   'status': 'Insurance Rejected', 'charges': '100.00', 'paid': '', 'adjustment': '', 'balance': '100.00'}]
        opened = []

        def open_claim(page, cid):
            opened.append(cid)
            return (True, None)
        # The claim popup: the codes billed on the ICD & CPT tab, the reason in the payments grid.
        texts = {'8101': 'Claim 8101 · ICD & CPT · J3490 96365 96366 · E11.9\nPayments / Adjustments / Refunds (1)\n# Id Date From Allowed Paid Adjust Code\n1 715 12/29/2025 Anthem 0.00 0.00 0.00 CO252 N706',
                 '8102': 'Claim 8102 · 96365 · nothing here'}
        with mock.patch.object(run, 'list_denied', return_value=(claims, ['ERA PAYER DENIED', 'Insurance Rejected'], ['A', 'ERA PAYER DENIED'])), \
                mock.patch.object(ecw, '_page_text', side_effect=lambda page: texts.get(opened[-1], '')), \
                mock.patch.object(ecw, '_click_text', return_value=True), mock.patch.object(ecw, '_shot'):
            out = run.run_claim_denials(_Aws(table), {'since': '07/01/2025'}, login=lambda p, c, a: True, get_page=lambda: object(),
                                        open_claim=open_claim, close_claim=lambda p: None)
        self.assertTrue(out['ok'])
        # 8100 had its codes from before: not opened again; the SOP says write off (rule 1).
        self.assertEqual(opened, ['8101', '8102'])
        r = table.items['8100']
        self.assertEqual((r['action_kind'], r['sop_rows'], r['denial_codes']), ('write off', ['1'], ['CO-16']))
        # 8101: CO-252 on J3490 for Anthem → rule 15, medical records.
        r = table.items['8101']
        self.assertEqual((r['action_kind'], r['sop_rows']), ('medical records', ['15']))
        self.assertIn('CO-252', r['denial_codes'])
        self.assertNotIn('29', r['denial_codes'])      # 12/29/2025 is a date, not a code
        self.assertEqual(r['cpt_codes'], ['96365', '96366', 'J3490'])
        # 8102: no code read, no rule → a person.
        r = table.items['8102']
        self.assertEqual((r['action_kind'], r['sop_rows'], r['denial_codes']), ('review', [], []))
        sm = table.items['_summary']
        self.assertEqual((sm['claims'], sm['matched'], sm['review'], sm['write_off'], sm['medical_records']), (3, 2, 1, 1, 1))
        self.assertIn('done', table.items['_run']['steps'])

    def test_several_fitting_rules_go_to_a_person(self):
        rules, _ = load_rules()
        hits, kind, action = run.decide({'cpt': 'J3490', 'payer': 'Medicare'}, ['CO-16'], rules)
        # Four rules fit and all say write off: the kind stands, the action lists them for the person.
        self.assertEqual(([h['sop_row'] for h in hits], kind), (['2', '3', '4', '5'], 'write off'))
        self.assertIn('[SOP 2', action)
        # Rules that fit equally but disagree on what to do → review.
        hits, kind, _ = run.decide({'cpt': 'J3490', 'payer': 'Anthem Blue Cross'}, ['Duplicate', 'CO-96'], rules)
        self.assertGreaterEqual(len(hits), 1)

    def test_the_status_names_and_the_read_only_promise(self):
        e = _read('src/denials/ecw.py')
        # The operator named one status (2026-09-30: "ERA PAYER DENIED"); the rest are opt-in.
        self.assertIn("DENIED_STATUSES = ['ERA Payer Denied']", e)
        self.assertIn("OTHER_DENIED_STATUSES = ['EOB Payer Denied', 'Waiting for Denial', 'Requires further review',", e)
        self.assertIn("PAYMENT_TABS = ['Insurances & Payment', 'Insurance & Payment', 'Payments']", e)
        self.assertIn("CPT_TAB = ['ICD & CPT', 'ICD and CPT']", e)
        # 709 claims, 20 a page: every page is read.
        self.assertIn("rows, hdrs = read_all_pages(page)", e)
        self.assertIn("def set_page_size(page):", e)
        # The popup is read and closed; nothing that saves, posts or deletes is clicked.
        for forbidden in ('Delete', 'ePost', 'Mark as Post', 'Save', "'OK'"):
            self.assertNotIn(forbidden, e)

    def test_the_wiring(self):
        m = _read('src/main.py')
        self.assertIn("elif task_type == 'claim_denials':", m)
        self.assertIn("open_claim=_open_claim_popup_via_lookup, close_claim=_close_claim_popup)", m)
        # The deploy leaves setup_*.py and infra/ off the host; check them where they are.
        if os.path.exists(os.path.join(REPO, 'setup_dynamodb.py')):
            self.assertIn('"TableName": "helixona-denials"', _read('setup_dynamodb.py'))
        d = _read('dashboard.py')
        self.assertIn("'denials': {", d)
        self.assertIn("title: 'Review denied claims in eCW', task: 'claim_denials'", d)
        self.assertIn("@app.route('/api/denials')", d)
        self.assertIn("@app.route('/api/denials.csv')", d)
        self.assertIn('id="denials-section"', d)
        self.assertIn('data-bot="denials" onclick="setActiveBot(\'denials\')"', d)
        if os.path.exists(os.path.join(REPO, 'infra/helixona-agent-denials.service')):
            self.assertIn("Environment=BOT_ROLE=denials", _read('infra/helixona-agent-denials.service'))
        self.assertIn("infra/helixona-agent-denials.service", _read('deploy_code.sh'))


if __name__ == '__main__':
    unittest.main()


class TheGridColumnsComeFromTheCells(unittest.TestCase):
    """2026-10-05, first live run: every <th> on the page was read as the
    header row, a session table ("Device · Login Time · Log Out Time") sat in
    front, every column shifted by three, and 48 rows came back with no claim
    number. The columns are taken from what the cells hold when the headers
    do not line up."""

    CELLS = [['', '', '', '', 'N', '328', '09/15/2025', 'ARC', 'Tolzda, Amanda', 'Cigna', 'ERA PAYER DENIED', '325.00', '0.00', '0.00', '0.00', '325.00', ''],
             ['', '', '', '', 'N', '1201', '12/29/2025', 'ARC', 'Doe, Jane', 'Anthem Blue Cross', 'ERA PAYER DENIED', '1,200.00', '0.00', '0.00', '0.00', '1,200.00', '']]

    def test_inferred_from_the_cells(self):
        idx = ecw.infer_cols(self.CELLS)
        self.assertEqual(idx['claim_id'], 5)
        self.assertEqual(idx['dos'], 6)
        self.assertEqual(idx['patient'], 8)
        self.assertEqual(idx['payer'], 9)
        self.assertEqual(idx['status'], 10)
        self.assertEqual((idx['charges'], idx['balance']), (11, 15))

    def test_read_rows_falls_back_when_the_headers_shift(self):
        hdrs = ['Device', 'Login Time', 'Log Out Time', 'COLL', 'CLAIM #', 'SERVICE DATE', 'PVDR', 'PATIENT', 'PAYER', 'STATUS',
                'CHARGES', 'PMTS/ ADJS', 'ADJUSTMENT', 'WITHHELD', 'BALANCE', 'Invoice Id.']
        page = mock.Mock()
        page.evaluate.return_value = {'rows': [{'cells': c} for c in self.CELLS], 'hdrs': hdrs, 'width': 17}
        rows, _ = ecw.read_rows(page)
        self.assertEqual([r['claim_id'] for r in rows], ['328', '1201'])
        self.assertEqual(rows[0]['patient'], 'Tolzda, Amanda')
        self.assertEqual(rows[0]['payer'], 'Cigna')
        self.assertEqual(rows[1]['balance'], '1,200.00')

    def test_headers_come_from_the_rows_own_table(self):
        e = _read('src/denials/ecw.py')
        self.assertIn("rows[0].closest('table')", e)
        self.assertIn("tables.find(h => h.length === width)", e)
        # Pagination without a page count follows Next while the grid changes.
        self.assertIn("for n in range((min(pages, MAX_PAGES) - 1) if pages else MAX_PAGES):", e)


class ThePopupTextIsCleanedBeforeCodesAreRead(unittest.TestCase):
    """2026-10-05, first live run: every claim read as M1, M2, M3, M4 (the CPT
    grid's modifier headers), "inclusive" (a column header on its own line),
    and numbers like 282 and 189 (the thousands of 1,282.50). None of those
    is a reason code."""

    POPUP = ("Claim 33 · Bailey, Allison\nCPT Code  Description  Units  M1 M2 M3 M4  Charges\n96365 IV infusion 1  1,282.50\n"
             "J3490 Drug 1 189.00\nE11.9\nPayments / Adjustments / Refunds (1)\n# Id Date From Allowed Deduct CoIns Copay Paid Adjust Withheld Code\n"
             "1 715 12/29/2025 Anthem 0.00 0.00 0.00 0.00 351.53 267.07 0.00 CO-45 / 97 N115\nInclusive\nSan Francisco, CA 94110")

    def test_only_real_codes_survive(self):
        from src.denials.cheatsheet import codes_in
        codes = codes_in(ecw._clean(self.POPUP))
        self.assertEqual(codes, ['CO-45', '45', 'N115', '97'])
        for bad in ('M1', 'M2', 'M3', 'M4', '282', '189', '29', 'inclusive'):
            self.assertNotIn(bad, codes)

    def test_cpts_come_from_the_tab_not_the_address(self):
        self.assertEqual(ecw._cpts_on(self.POPUP), ['96365', 'J3490'])

    def test_a_real_remark_code_and_a_real_word_still_count(self):
        from src.denials.cheatsheet import codes_in
        self.assertIn('M25', codes_in(ecw._clean('CO-50 M25 N115\nThis service is not covered, medical necessity')))
        self.assertIn('medical necessity', codes_in('Denied: medical necessity not established'))

    def test_pagination_waits_for_the_grid_and_rereads_an_empty_page(self):
        e = _read('src/denials/ecw.py')
        self.assertIn('def _wait_grid(page, before, timeout_s=20):', e)
        self.assertIn("if sig and not sig.endswith('#0') and sig != before:", e)
        self.assertIn('a page read in the loading gap is empty: look again', e)
        self.assertIn('is fewer than expected — some pages were not read', e)
