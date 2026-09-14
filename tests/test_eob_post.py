"""Remittance → eCW: pairing EOB lines with eCW lines, and the gate on posting.

The J3490 problem: an IV claim bills the same unclassified-drug code once per
drug, and the EOB comes back with the same code repeated. Only the billed
amount tells the lines apart — and when Blue Shield restates a line at the
Medicare amount, the operator's drug table (2026-09-14) translates it.
"""
import os
import unittest

from src.eob.fee_table import DRUG_TABLE, drug_for, equivalent_amounts, match_lines
from src.eob.post import claim_amount, lines_for

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def _pairs(ecw, eob):
    pairs, unresolved = match_lines(ecw, eob)
    return {p['eob']['billed']: (p['ecw']['idx'], p['how']) for p in pairs}, unresolved


class TheDrugTableIsTheOperators(unittest.TestCase):
    def test_every_row_from_the_table(self):
        rows = {(c, d): (m, b) for c, d, m, b in DRUG_TABLE}
        self.assertEqual(rows[('J3490', 'Ascorbic Acid - 25000 mG')], ('300.00', '900.00'))
        self.assertEqual(rows[('J3490', 'Ascorbic Acid - 100000 mG')], ('1200.00', '3600.00'))
        self.assertEqual(rows[('J7999', 'Pyridoxine')], ('20.00', '40.00'))
        self.assertEqual(rows[('J3490', 'Methylcobalamin')], ('40.00', '125.00'))
        self.assertEqual(len(DRUG_TABLE), 14)

    def test_an_amount_translates_to_its_twin(self):
        self.assertEqual({str(a) for a in equivalent_amounts('J3490', '300.00')}, {'300.00', '900.00'})
        # $900 sits in two rows: Ascorbic 25000 (Blue Shield) and Ascorbic
        # 75000 (Medicare). Both twins come back; match_lines still needs a
        # unique eCW row before it pairs anything.
        self.assertEqual({str(a) for a in equivalent_amounts('J3490', '$900')}, {'300.00', '900.00', '2700.00'})
        self.assertEqual(equivalent_amounts('J3490', '123.45'), set())
        self.assertEqual(equivalent_amounts('99213', '300.00'), set())

    def test_the_drug_is_named_and_ambiguity_is_kept(self):
        self.assertEqual(drug_for('J3490', '300.00'), ['Ascorbic Acid - 25000 mG'])
        # $40 under J3490 is B Complex or Methylcobalamin (Medicare column).
        self.assertEqual(set(drug_for('J3490', '40.00')), {'B Complex 100', 'Methylcobalamin'})


class RepeatedCodesArePairedByBilledAmount(unittest.TestCase):
    ECW = [
        {'idx': 0, 'cpt': '96365', 'billed': '250.00'},
        {'idx': 1, 'cpt': 'J3490', 'billed': '900.00'},   # Ascorbic 25000 at the Blue Shield amount
        {'idx': 2, 'cpt': 'J3490', 'billed': '125.00'},   # Methylcobalamin
        {'idx': 3, 'cpt': 'J3490', 'billed': '40.00'},    # B Complex
    ]

    def test_a_lone_code_pairs_even_when_billed_differs(self):
        pairs, un = _pairs(self.ECW[:1], [{'cpt': '96365', 'billed': '260.00', 'paid': '1.00'}])
        self.assertEqual(pairs['260.00'][0], 0)
        self.assertIn('billed differs', pairs['260.00'][1])
        self.assertEqual(un, [])

    def test_exact_billed_amounts_pair(self):
        eob = [{'cpt': 'J3490', 'billed': '125.00'}, {'cpt': 'J3490', 'billed': '40.00'},
               {'cpt': 'J3490', 'billed': '900.00'}, {'cpt': '96365', 'billed': '250.00'}]
        pairs, un = _pairs(self.ECW, eob)
        self.assertEqual({k: v[0] for k, v in pairs.items()}, {'125.00': 2, '40.00': 3, '900.00': 1, '250.00': 0})
        self.assertEqual(un, [])

    def test_the_table_translates_a_medicare_amount(self):
        # The EOB restates Ascorbic 25000 at $300 while eCW billed $900.
        eob = [{'cpt': 'J3490', 'billed': '300.00'}, {'cpt': 'J3490', 'billed': '125.00'},
               {'cpt': 'J3490', 'billed': '40.00'}]
        pairs, un = _pairs(self.ECW[1:], eob)
        self.assertEqual(pairs['300.00'], (1, 'drug table: Ascorbic Acid - 25000 mG'))
        self.assertEqual(un, [])

    def test_an_unknown_amount_is_left_for_a_person(self):
        eob = [{'cpt': 'J3490', 'billed': '777.00'}, {'cpt': 'J3490', 'billed': '125.00'},
               {'cpt': 'J3490', 'billed': '40.00'}]
        pairs, un = _pairs(self.ECW[1:], eob)
        # 777 has no twin; two eCW rows remain? No — 125 and 40 pair exactly,
        # one row (900) and one line (777) are left: elimination pairs them.
        self.assertEqual(pairs['777.00'], (1, 'by elimination'))
        self.assertEqual(un, [])

    def test_two_unknowns_never_guess(self):
        ecw = [{'idx': 1, 'cpt': 'J3490', 'billed': '900.00'}, {'idx': 2, 'cpt': 'J3490', 'billed': '125.00'}]
        eob = [{'cpt': 'J3490', 'billed': '777.00'}, {'cpt': 'J3490', 'billed': '888.00'}]
        pairs, un = _pairs(ecw, eob)
        self.assertEqual(pairs, {})
        self.assertEqual(len([u for u in un if 'eob' in u]), 2)

    def test_a_shared_table_amount_needs_a_unique_ecw_row(self):
        # EOB $900 could be Ascorbic 25000 (eCW $900, already taken) or
        # Ascorbic 75000 restated (eCW $2700): with 300 and 2700 both on the
        # claim, nothing is guessed.
        ecw = [{'idx': 1, 'cpt': 'J3490', 'billed': '300.00'}, {'idx': 2, 'cpt': 'J3490', 'billed': '2700.00'},
               {'idx': 3, 'cpt': 'J3490', 'billed': '40.00'}]
        eob = [{'cpt': 'J3490', 'billed': '900.00'}, {'cpt': 'J3490', 'billed': '40.00'},
               {'cpt': 'J3490', 'billed': '1.00'}]
        pairs, un = _pairs(ecw, eob)
        self.assertEqual(list(pairs), ['40.00'])
        self.assertEqual(len([u for u in un if 'eob' in u]), 2)

    def test_the_table_does_not_pick_between_two_equal_ecw_rows(self):
        ecw = [{'idx': 1, 'cpt': 'J3490', 'billed': '40.00'}, {'idx': 2, 'cpt': 'J3490', 'billed': '40.00'}]
        eob = [{'cpt': 'J3490', 'billed': '40.00'}, {'cpt': 'J3490', 'billed': '40.00'}]
        pairs, un = _pairs(ecw, eob)
        self.assertEqual(pairs, {})
        self.assertEqual(len(un), 4)

    def test_a_code_missing_on_one_side_is_reported(self):
        pairs, un = _pairs(self.ECW[:1], [{'cpt': '99999', 'billed': '1.00'}])
        self.assertEqual(pairs, {})
        self.assertEqual({u['reason'].split(':')[0] for u in un}, {'96365', '99999'})


class TheAmountsComeFromTheEob(unittest.TestCase):
    ITEM = {'check_eft': '30979207', 'approve_to_pay': '262.69', 'check_amount': '263.67',
            'eob_lines': [{'cpt': '96375', 'billed': '250.00'}],
            'claims': [{'claim_id': '2627', 'amount_paid': ''}]}

    def test_a_single_claim_takes_the_statements_approve_to_pay(self):
        self.assertEqual(claim_amount(self.ITEM, self.ITEM['claims'][0]), '262.69')

    def test_a_claim_with_its_own_paid_amount_uses_it(self):
        item = dict(self.ITEM, claims=[{'claim_id': '1', 'amount_paid': '$10.00'}, {'claim_id': '2', 'amount_paid': '$5.00'}])
        self.assertEqual(claim_amount(item, item['claims'][1]), '5.00')

    def test_a_multi_claim_cheque_never_posts_the_whole_cheque_on_one_claim(self):
        item = dict(self.ITEM, claims=[{'claim_id': '1', 'amount_paid': ''}, {'claim_id': '2', 'amount_paid': ''}])
        self.assertEqual(claim_amount(item, item['claims'][0]), '')

    def test_old_captures_fall_back_to_statement_lines_for_a_lone_claim(self):
        self.assertEqual(lines_for(self.ITEM, self.ITEM['claims'][0]), self.ITEM['eob_lines'])
        item = dict(self.ITEM, claims=[{'claim_id': '1'}, {'claim_id': '2'}])
        self.assertEqual(lines_for(item, item['claims'][0]), [])
        self.assertEqual(lines_for(item, {'claim_id': '1', 'lines': [{'cpt': 'X'}]}), [{'cpt': 'X'}])


class PostingIsGated(unittest.TestCase):
    def test_a_body_without_post_true_is_a_dry_run(self):
        p = _read('src/eob/post.py')
        self.assertIn("post = bool(body.get('post'))", p)
        self.assertIn("if not post:\n        cancel_popups(page)", p)
        self.assertIn("'status': 'dry_run'", p)

    def test_an_unresolved_line_stops_the_claim_before_anything_is_written(self):
        p = _read('src/eob/post.py')
        i = p.index("bad = [u for u in unresolved if 'eob' in u]")
        j = p.index("written = fill_grid(")
        self.assertLess(i, j)
        self.assertIn("'status': 'unresolved'", p[i:j])

    def test_the_operators_values_go_where_they_said(self):
        p = _read('src/eob/post.py')
        for step in ("'Single Insurance Payment'", "'Payment Advisory'", "'Go F3'", "'Check'",
                     "what='Deposit date'", "what='EOB date'", "what='Check No'", "'Post'"):
            self.assertIn(step, p, step)
        self.assertIn("DEFAULT_SINCE = '07/01/2025'", p)

    def test_the_capture_still_never_touches_ecw(self):
        c = _read('src/eob/capture.py')
        self.assertNotIn('Payment Advisory', c)
        self.assertNotIn('from src.eob.post', c)

    def test_the_bot_runs_it_without_a_proxy_and_with_the_shared_ecw_login(self):
        src = _read('src/main.py')
        i = src.index("elif task_type == 'eob_post':")
        block = src[i:i + 1200]
        self.assertIn('BrowserManager().start(proxy_config=None)', block)
        self.assertIn('run_eob_post(page, aws_client, body, login=_perform_ecw_login)', block)

    def test_the_dashboard_offers_it_to_remittance_only_and_defaults_to_dry_run(self):
        import re
        d = _read('dashboard.py')
        m = re.search(r'<option value="eob_post" data-bot="([^"]+)"', d)
        self.assertEqual(m.group(1).split(), ['eob'])
        i = d.index('eob_post: JSON.stringify({')
        self.assertIn('post: false', d[i:i + 200])


class DuplicateEobPdfsAreRemoved(unittest.TestCase):
    def test_a_byte_identical_pdf_is_deleted_and_the_stored_copy_reused(self):
        c = _read('src/eob/capture.py')
        self.assertIn('_sha256(pdf_path)', c)
        self.assertIn("twin.get('check_eft') != check", c)
        self.assertIn('os.remove(pdf_path)', c)
        self.assertIn("'eob_pdf_duplicate_of': duplicate_of", c)

    def test_each_claim_keeps_its_own_lines(self):
        self.assertIn("'lines': lines_by_bsc.get(bsc, [])", _read('src/eob/capture.py'))


class TheLiveScreenIsOnTheDashboard(unittest.TestCase):
    def test_the_panel_follows_the_active_bot(self):
        d = _read('dashboard.py')
        self.assertIn('id="live-screen-frame"', d)
        self.assertIn('view_only=true', d)
        self.assertIn('const port = BOT_NOVNC[bot] || 6080;', d)
        self.assertIn("if (typeof syncLiveScreen === 'function') syncLiveScreen();", d)


if __name__ == '__main__':
    unittest.main()
