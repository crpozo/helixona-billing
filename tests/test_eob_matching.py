"""Posting an EOB onto eCW's service lines: which line gets which money.

Built from claim 2627 (13 lines, four J3490s told apart only by the drug in
the HCFA's shaded row) and from the operator's rule for repeated codes and
Vignesh's table. The fixtures are synthetic — the same geometry and amounts
the real forms have, no patient data.
"""
import unittest

from src.eob.drug_table import DRUG_TABLE, ecw_amounts_for
from src.eob.hcfa_lines import parse_hcfa_lines, total_billed
from src.eob.matching import drug_key, plan_claim


def W(text, x, y):
    return {'text': text, 'x': x, 'y': y}


# One HCFA page in the real geometry: a plain CPT line, a drug line with its
# shaded NDC row above it, charges split into dollars/cents, and one merged.
PAGE = [
    W('R5382', 0.059, 0.604),                                    # box 21, not a line
    W('01', 0.041, 0.678), W('23', 0.072, 0.679), W('26', 0.109, 0.679),
    W('01', 0.141, 0.679), W('23', 0.180, 0.680), W('26', 0.213, 0.679),
    W('11', 0.242, 0.679), W('96365', 0.317, 0.678), W('ABCD', 0.552, 0.680),
    W('325', 0.645, 0.679), W('00', 0.685, 0.680), W('1', 0.716, 0.679),
    W('1407241524', 0.823, 0.680),
    W('01', 0.039, 0.740), W('23', 0.071, 0.740), W('26', 0.108, 0.740),
    W('01', 0.142, 0.740), W('23', 0.178, 0.740), W('26', 0.213, 0.740),
    W('11', 0.243, 0.740), W('96375', 0.316, 0.740), W('57500', 0.645, 0.740),
    W('5', 0.714, 0.740),
    W('N467457014630', 0.036, 0.815), W(',', 0.194, 0.815), W('1', 0.217, 0.815),
    W('ML', 0.239, 0.815), W('B', 0.273, 0.815), W('COMPLEX', 0.296, 0.815), W('100', 0.386, 0.815),
    W('01', 0.040, 0.831), W('23', 0.072, 0.831), W('26', 0.107, 0.831),
    W('01', 0.144, 0.831), W('23', 0.179, 0.831), W('26', 0.214, 0.830),
    W('11', 0.241, 0.830), W('J3490', 0.318, 0.830), W('40', 0.655, 0.830),
    W('00', 0.684, 0.830), W('1', 0.717, 0.830),
]
PAGE2 = [
    W('N472682630501', 0.036, 0.757), W(',', 0.194, 0.757), W('0.2', 0.217, 0.757),
    W('ML', 0.262, 0.757), W('METHYLCOBALAMIN', 0.296, 0.757), W('(B-12)', 0.476, 0.757),
    W('01', 0.038, 0.772), W('23', 0.070, 0.771), W('26', 0.108, 0.772),
    W('01', 0.140, 0.771), W('23', 0.178, 0.771), W('26', 0.213, 0.772),
    W('11', 0.242, 0.771), W('J3490', 0.316, 0.772), W('125', 0.646, 0.771),
    W('00', 0.686, 0.772), W('1', 0.716, 0.771),
    W('N467157010151', 0.036, 0.664), W(',', 0.194, 0.664), W('50', 0.217, 0.664),
    W('ML', 0.250, 0.664), W('ASCORBIC', 0.284, 0.664), W('ACID', 0.386, 0.664),
    W('(500MG/ML)', 0.442, 0.664),
    W('01', 0.041, 0.678), W('23', 0.072, 0.679), W('26', 0.109, 0.679),
    W('01', 0.141, 0.679), W('23', 0.180, 0.680), W('26', 0.213, 0.679),
    W('11', 0.242, 0.679), W('J3490', 0.317, 0.678), W('900', 0.645, 0.679),
    W('00', 0.685, 0.680), W('1', 0.716, 0.679),
]


class Box24IsReadLineByLine(unittest.TestCase):
    def setUp(self):
        self.lines = parse_hcfa_lines([PAGE, PAGE2])

    def test_every_service_line_and_only_those(self):
        self.assertEqual([l['cpt'] for l in self.lines], ['96365', '96375', 'J3490', 'J3490', 'J3490'])

    def test_split_and_merged_charges_both_read(self):
        self.assertEqual(self.lines[0]['billed'], '325.00')   # "325" "00"
        self.assertEqual(self.lines[1]['billed'], '575.00')   # "57500"

    def test_units_pos_and_dates(self):
        l = self.lines[1]
        self.assertEqual((l['units'], l['pos'], l['dos_from'], l['dos_to']), ('5', '11', '01/23/2026', '01/23/2026'))

    def test_the_drug_comes_from_the_shaded_row_above(self):
        self.assertEqual(self.lines[2]['drug'], '1 ML B COMPLEX 100')
        self.assertEqual(self.lines[2]['ndc'], '67457014630')
        # Lines come in the form's top-to-bottom order: on page 2 the
        # ascorbic-acid J3490 (y 0.678) sits above the methylcobalamin one.
        self.assertEqual(self.lines[3]['drug'], '50 ML ASCORBIC ACID (500MG/ML)')
        self.assertEqual(self.lines[4]['drug'], '0.2 ML METHYLCOBALAMIN (B-12)')

    def test_a_drug_row_is_only_taken_from_just_above_its_line(self):
        # The methylcobalamin row (y 0.757) is BELOW the ascorbic-acid J3490
        # at 0.678, so it must never be attached to it.
        self.assertNotIn('METHYLCOBALAMIN', self.lines[3]['drug'])

    def test_a_plain_cpt_line_has_no_drug(self):
        self.assertEqual(self.lines[0]['drug'], '')

    def test_later_pages_continue_the_claim(self):
        self.assertEqual([l['page'] for l in self.lines], [1, 1, 1, 2, 2])

    def test_the_total_is_the_sum_of_lines(self):
        self.assertEqual(total_billed(self.lines), '1965.00')


class DrugsAreComparable(unittest.TestCase):
    def test_ascorbic_acid_dose_is_computed_from_volume(self):
        self.assertEqual(drug_key('50 ML ASCORBIC ACID (500MG/ML)'), 'ascorbic acid 25000')
        self.assertEqual(drug_key('Ascorbic Acid - 25000 mG'), 'ascorbic acid 25000')

    def test_names_normalise(self):
        self.assertEqual(drug_key('0.2 ML METHYLCOBALAMIN (B-12) 5MG/ML'), 'methylcobalamin')
        self.assertEqual(drug_key('B Complex 100'), 'b complex')
        self.assertIsNone(drug_key(''))

    def test_the_table_is_vigneshs(self):
        self.assertEqual(len(DRUG_TABLE), 14)
        self.assertIn(('300.00', 'Ascorbic Acid - 25000 mG'), ecw_amounts_for('J3490', '900.00'))
        # Two drugs share J7999 $40.00 on the EOB.
        self.assertEqual({d for _, d in ecw_amounts_for('J7999', '40.00')}, {'Dexpanthenol', 'Pyridoxine'})


def E(cpt, billed, paid='0.00', units='1', allowed='0.00', deductible='0.00', copay='0.00'):
    return {'cpt': cpt, 'billed': billed, 'paid': paid, 'units': units,
            'allowed': allowed, 'deductible': deductible, 'copay': copay}


def C(cpt, billed, units='1', drug=''):
    return {'cpt': cpt, 'billed': billed, 'units': units, 'drug': drug}


class UniqueCodesMatchByCode(unittest.TestCase):
    def test_different_billed_amounts_still_match(self):
        p = plan_claim([E('96375', '250.00', paid='92.11', units='5', allowed='153.65', copay='61.54')],
                       [C('96375', '575.00', units='5')])
        self.assertEqual(p['status'], 'ready')
        r = p['rows'][0]
        self.assertEqual((r['allowed'], r['copay'], r['paid']), ('153.65', '61.54', '92.11'))
        self.assertIn('billed differs', r['how'])

    def test_a_code_missing_from_ecw_blocks_the_claim(self):
        p = plan_claim([E('99999', '10.00')], [C('96365', '325.00')])
        self.assertEqual(p['status'], 'needs_review')
        self.assertIn('not on the eCW claim', p['reasons'][0])

    def test_ecw_lines_the_eob_does_not_pay_are_reported(self):
        p = plan_claim([E('96365', '325.00')], [C('96365', '325.00'), C('96366', '150.00')])
        self.assertEqual(p['status'], 'ready')
        self.assertEqual(p['unposted_ecw'], [1])


class RepeatedCodesAreToldApart(unittest.TestCase):
    ECW_2627 = [C('J3490', '40.00', drug='1 ML B COMPLEX 100'),
                C('J3490', '125.00', drug='0.2 ML METHYLCOBALAMIN (B-12) 5MG/ML'),
                C('J3490', '100.00', drug='5 ML GLUTATHIONE 200 MG/ML'),
                C('J3490', '900.00', drug='50 ML ASCORBIC ACID (500MG/ML)')]

    def test_by_billed_amount_when_the_amounts_agree(self):
        eob = [E('J3490', '900.00', paid='9.00'), E('J3490', '40.00', paid='4.00'),
               E('J3490', '125.00', paid='1.25'), E('J3490', '100.00', paid='1.00')]
        p = plan_claim(eob, self.ECW_2627)
        self.assertEqual(p['status'], 'ready')
        self.assertEqual({(r['ecw_billed'], r['paid']) for r in p['rows']},
                         {('40.00', '4.00'), ('125.00', '1.25'), ('100.00', '1.00'), ('900.00', '9.00')})

    def test_by_vigneshs_table_when_ecw_bills_the_medicare_amount(self):
        ecw = [C('J3490', '300.00', drug='50 ML ASCORBIC ACID (500MG/ML)'),
               C('J3490', '40.00', drug='1 ML B COMPLEX 100')]
        eob = [E('J3490', '900.00', paid='90.00'), E('J3490', '40.00', paid='4.00')]
        p = plan_claim(eob, ecw)
        self.assertEqual(p['status'], 'ready')
        by = {r['ecw_billed']: r for r in p['rows']}
        self.assertEqual(by['300.00']['paid'], '90.00')
        self.assertEqual(by['300.00']['how'], 'drug table')

    def test_the_drug_must_agree_with_the_table(self):
        # EOB J3490 $125 is Methylcobalamin per the table (eCW $40.00). An eCW
        # $40.00 line that is B Complex must NOT receive it.
        ecw = [C('J3490', '40.00', drug='1 ML B COMPLEX 100'),
               C('J3490', '40.00', drug='0.2 ML METHYLCOBALAMIN (B-12) 5MG/ML')]
        eob = [E('J3490', '125.00', paid='12.50'), E('J3490', '40.00', paid='4.00')]
        p = plan_claim(eob, ecw)
        self.assertEqual(p['status'], 'ready')
        by = {r['ecw_drug']: r['paid'] for r in p['rows']}
        self.assertEqual(by['0.2 ML METHYLCOBALAMIN (B-12) 5MG/ML'], '12.50')
        self.assertEqual(by['1 ML B COMPLEX 100'], '4.00')

    def test_two_forty_dollar_j7999s_paying_differently_are_not_guessed(self):
        # Dexpanthenol ($40 in eCW) and Pyridoxine ($20 in eCW) both bill
        # $40.00 to Blue Shield. With different payments, nothing on the EOB
        # says which line was which drug.
        ecw = [C('J7999', '20.00', drug='1 ML PYRIDOXINE 100MG/ML'),
               C('J7999', '40.00', drug='DEXPANTHENOL 250MG/ML')]
        eob = [E('J7999', '40.00', paid='2.00'), E('J7999', '40.00', paid='3.00')]
        p = plan_claim(eob, ecw)
        self.assertEqual(p['status'], 'needs_review')
        self.assertIn('pay different amounts', p['reasons'][0])
        self.assertEqual(p['rows'], [])

    def test_two_forty_dollar_j7999s_paying_the_same_can_go_either_way(self):
        ecw = [C('J7999', '20.00', drug='1 ML PYRIDOXINE 100MG/ML'),
               C('J7999', '40.00', drug='DEXPANTHENOL 250MG/ML')]
        eob = [E('J7999', '40.00', paid='2.00'), E('J7999', '40.00', paid='2.00')]
        p = plan_claim(eob, ecw)
        self.assertEqual(p['status'], 'ready')
        self.assertEqual(len(p['rows']), 2)

    def test_identical_ecw_lines_are_interchangeable(self):
        ecw = [C('96366', '150.00'), C('96366', '150.00')]
        eob = [E('96366', '150.00', paid='10.00'), E('96366', '150.00', paid='10.00')]
        self.assertEqual(plan_claim(eob, ecw)['status'], 'ready')

    def test_an_unresolvable_repeat_is_never_guessed(self):
        ecw = [C('J3490', '55.00', drug='5 ML GLUTATHIONE 200 MG/ML'),
               C('J3490', '65.00', drug='1 ML SOMETHING ELSE')]
        eob = [E('J3490', '777.00', paid='1.00')]
        p = plan_claim(eob, ecw)
        self.assertEqual(p['status'], 'needs_review')
        self.assertEqual(p['rows'], [])
        self.assertIn('no billed or drug-table match', p['reasons'][0])

    def test_more_eob_lines_than_ecw_lines_blocks(self):
        p = plan_claim([E('96366', '1.00'), E('96366', '2.00')], [C('96366', '1.00')])
        self.assertEqual(p['status'], 'needs_review')


class TheMoneyMustAddUp(unittest.TestCase):
    def test_lines_reconcile_to_the_claim_paid(self):
        p = plan_claim([E('96365', '325.00', paid='60.00'), E('96366', '150.00', paid='40.00')],
                       [C('96365', '325.00'), C('96366', '150.00')], eob_claim_paid='100.00')
        self.assertEqual(p['status'], 'ready')

    def test_a_mismatch_blocks_posting(self):
        p = plan_claim([E('96365', '325.00', paid='60.00')], [C('96365', '325.00')],
                       eob_claim_paid='100.00')
        self.assertEqual(p['status'], 'needs_review')
        self.assertIn('sum to 60.00', p['reasons'][0])


if __name__ == '__main__':
    unittest.main()
