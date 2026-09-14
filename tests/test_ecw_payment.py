"""The values that go into eCW, taken from the two operator recordings.

Cheque 30979207 pays $263.67, of which $0.98 is interest; the payment entered
in eCW is the EOB's $262.69 approve-to-pay. See docs/ecw_posting.md.
"""
import unittest

from src.eob.ecw_payment import grid_rows, line_overflows, payment_header, posting_total

EOB = {'approve_to_pay': '262.69', 'check_amount': '263.67', 'interest': '0.98',
       'payment_issued_to': ''}
CHECK = {'check_eft': '30979207', 'check_date': '05/05/2026', 'cashed_date': '05/14/2026'}


class ThePaymentHeader(unittest.TestCase):
    def test_the_fields_the_operator_fills(self):
        fields, problems = payment_header(EOB, CHECK)
        self.assertEqual(problems, [])
        self.assertEqual(fields['Type'], 'Check')
        self.assertEqual(fields['Check No.'], '30979207')
        self.assertEqual(fields['Check Date'], '05/05/2026')
        self.assertEqual(fields['Deposit Date'], '05/14/2026')

    def test_the_amount_is_what_the_eob_approves_not_what_the_cheque_pays(self):
        fields, _ = payment_header(EOB, CHECK)
        self.assertEqual(fields['Amount $'], '262.69')
        self.assertNotEqual(fields['Amount $'], EOB['check_amount'])

    def test_the_eob_date_is_the_cheque_date(self):
        fields, _ = payment_header(EOB, CHECK)
        self.assertEqual(fields['EOB Date'], fields['Check Date'])

    def test_the_fields_the_operator_leaves_alone(self):
        fields, _ = payment_header(EOB, CHECK)
        for f in ('Facility', 'Batch No.', 'Received Date', 'Notes'):
            self.assertIsNone(fields[f], f)

    def test_a_cheque_paid_to_the_member_has_nothing_to_enter(self):
        _, problems = payment_header({**EOB, 'payment_issued_to': 'the member'}, CHECK)
        self.assertTrue(any('paid the member' in p for p in problems), problems)

    def test_a_cheque_missing_its_dates_is_refused(self):
        _, problems = payment_header(EOB, {'check_eft': '30979207'})
        self.assertEqual(len(problems), 2, problems)

    def test_an_eob_that_approves_nothing_is_refused(self):
        _, problems = payment_header({**EOB, 'approve_to_pay': '0.00'}, CHECK)
        self.assertTrue(any('nothing to post' in p for p in problems), problems)


class TheGrid(unittest.TestCase):
    ROWS = [{'ecw_index': 0, 'cpt': '96365', 'ecw_billed': '325.00', 'ecw_drug': '',
             'allowed': '96.87', 'deductible': '0.00', 'copay': '38.75', 'paid': '58.12'},
            {'ecw_index': 9, 'cpt': 'J3490', 'ecw_billed': '900.00',
             'ecw_drug': '50 ML ASCORBIC ACID (500MG/ML)',
             'allowed': '5.32', 'deductible': '0.00', 'copay': '2.13', 'paid': '3.19'}]

    def test_only_the_four_cells_the_operator_types(self):
        rows, problems = grid_rows({'rows': self.ROWS})
        self.assertEqual(problems, [])
        self.assertEqual(rows[0]['values'],
                         {'Allowed': '96.87', 'Deduct': '0.00', 'CoPay': '38.75', 'Paid': '58.12'})
        self.assertEqual(rows[1]['drug'], '50 ML ASCORBIC ACID (500MG/ML)')
        self.assertEqual(rows[1]['ecw_index'], 9)

    def test_the_operators_j0640_slip_is_caught_before_ecw_complains(self):
        # On camera J0640 was given J1955's paid amount — 13.96 instead of 2.35
        # — and eCW warned "The sum of Deduct, Coins, CoPay, Paid, Adjustment
        # and WithHeld 111.61 is more than Billed Fee 100.00".
        bad = {'ecw_index': 7, 'cpt': 'J0640', 'ecw_billed': '100.00', 'allowed': '3.91',
               'deductible': '0.00', 'copay': '1.56', 'paid': '13.96'}
        self.assertTrue(line_overflows(bad, '100.00'))
        rows, problems = grid_rows({'rows': [bad]})
        self.assertEqual(rows, [])
        self.assertIn('eCW would refuse the line', problems[0])

    def test_the_corrected_line_passes(self):
        good = {'ecw_index': 7, 'cpt': 'J0640', 'ecw_billed': '100.00', 'allowed': '3.91',
                'deductible': '0.00', 'copay': '1.56', 'paid': '2.35'}
        self.assertFalse(line_overflows(good, '100.00'))
        rows, problems = grid_rows({'rows': [good]})
        self.assertEqual(problems, [])
        self.assertEqual(rows[0]['values']['Paid'], '2.35')

    def test_a_line_whose_amounts_were_not_read_is_not_typed(self):
        rows, problems = grid_rows({'rows': [{'cpt': '96365', 'ecw_billed': '325.00', 'allowed': '',
                                              'deductible': '0.00', 'copay': '0.00', 'paid': '58.12'}]})
        self.assertEqual(rows, [])
        self.assertIn('not all read', problems[0])

    def test_what_the_paid_column_will_add_up_to(self):
        rows, _ = grid_rows({'rows': self.ROWS})
        self.assertEqual(posting_total(rows), '61.31')


if __name__ == '__main__':
    unittest.main()
