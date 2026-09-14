"""The EOB report, read by where each word sits on the page.

Fixtures are synthetic: words placed at the column geometry measured on a real
Blue Shield EOB (right-aligned amounts, left-aligned text), with invented
people and numbers.
"""
import unittest

from src.eob.eob_pdf import keep_char, money, parse_eob_pages
from src.eob.plan import plan_cheque

COLS = {'patient': (18.0, 67.68), 'account': (84.0, 141.36), 'dos': (151.44, 178.08),
        'procedure': (213.12, 251.52), 'units': (265.2, 291.84), 'billed': (316.8, 342.72),
        'allowed': (353.04, 382.56), 'contractual': (389.04, 434.16), 'notes': (439.92, 460.56),
        'deductible': (476.16, 514.32), 'copay': (528.72, 554.64), 'paid': (567.84, 593.76)}
HEADER_WORDS = {'patient': 'NAME', 'account': 'ACCOUNT', 'dos': 'DATES', 'procedure': 'PROCEDURE',
                'units': 'UNITS', 'billed': 'BILLED', 'allowed': 'ALLOWED', 'contractual': 'CONTRACTUAL',
                'notes': 'NOTES', 'deductible': 'DEDUCTIBLE', 'copay': 'CO-PAY', 'paid': 'PAID'}
GLYPH = 3.0


def W(text, top, x0=None, x1=None, size=6.0):
    if x0 is None:
        x0 = x1 - len(text) * GLYPH
    if x1 is None:
        x1 = x0 + len(text) * GLYPH
    return {'text': text, 'x0': x0, 'x1': x1, 'top': top, 'size': size}


def at(col, text, top):
    """Amounts sit on their column's right edge, text on its left."""
    if col in ('billed', 'allowed', 'contractual', 'deductible', 'copay', 'paid'):
        return W(text, top, x1=COLS[col][1])
    if col == 'units':
        return W(text, top, x1=COLS[col][1])
    return W(text, top, x0=COLS[col][0])


def row(top, **cells):
    return [at(k, v, top) for k, v in cells.items() if v is not None]


def page_header():
    ws = [W('ISSUE DATE:', 40, x0=400), W('05 05 26', 40, x0=445),
          W('EOB NUMBER:', 48, x0=400), W('26125B00000000000001', 48, x0=445),
          W('PROVIDER NPI:', 56, x0=400), W('1000000001', 56, x0=445)]
    ws += [W(HEADER_WORDS[k], 200, x0=a, x1=b) for k, (a, b) in COLS.items()]
    ws += [W('CODE', 207, x0=213.12, x1=230.0), W('AMOUNT', 214, x0=567.84, x1=593.76)]
    return ws


def line(top, dos, cpt, units, billed, allowed, ded, copay, paid, patient=None, account=None, notes='1'):
    return row(top, patient=patient, account=account, dos=dos, procedure=cpt, units=units, billed=billed,
               allowed=allowed, notes=notes, deductible=ded, copay=copay, paid=paid)


def statement(paid_to_member=False, adjusted=False, split_page=False, tamper=None):
    """Two claims: 1001 with three lines, 1002 with one. Interest 0.50."""
    a = [W('RECEIPT DATE:', 232, x0=18), W('01/29/26', 232, x0=70)]
    a += line(240, '01/23/26', '99213', '1', '197.00', '100.00', '0.00', '40.00', '60.00',
              patient='JANE DOE', account='1001')
    a += line(250, '01/23/26', 'J3490', '1', '300.00', '5.32', '0.00', '2.13', tamper or '3.19',
              patient='900000001', account='260700000001')
    b = line(260, '01/23/26', 'J3490', '1', '40.00', '16.00', '0.00', '6.40', '9.60', patient='W00000000001')
    b += [W('TOTALS:', 272, x0=18)] + row(272, billed='537.00', contractual='0.00', deductible='0.00',
                                          copay='48.53', paid='72.79')
    b += [W('NOTES:', 282, x0=18), W('1', 282, x0=60)]
    b += [W('RECEIPT DATE:', 296, x0=18), W('02/02/26', 296, x0=70)]
    b += line(304, '01/24/26', '96365', '1', '325.00', '96.87', '10.00', '34.75', '52.12',
              patient='JOHN ROE', account='1002')
    b += row(314, patient='900000002', account='260700000002') + row(322, patient='W00000000002')
    b += [W('TOTALS:', 332, x0=18)] + row(332, billed='325.00', contractual='0.00', deductible='10.00',
                                          copay='34.75', paid='52.12')
    b += [W('NOTES:', 342, x0=18), W('1', 342, x0=60)]
    if adjusted:
        b += [W('Adjusted Payment', 350, x0=400), W('52.12', 350, x1=593.76)]
    b += [W('NOTES:', 370, x0=18),
          W('1', 382, x0=18, size=8), W('THE ALLOWED AMOUNT IS BASED ON THE FEE SCHEDULE.', 382, x0=40, size=8)]
    if paid_to_member:
        b += [W('PAYMENT WAS ISSUED TO JANE DOE.', 400, x0=18, size=8)]
    b += [W('STATEMENT TOTALS:', 420, x0=18)] + row(420, billed='862.00', allowed='218.19', contractual='0.00',
                                                    deductible='10.00', copay='83.28', paid='125.41')
    check = '$0.00' if paid_to_member else '$125.41'
    for top, lab, val in ((440, 'APPROVE-TO-PAY:', '$124.91'), (450, 'INTEREST PAYMENTS:', '0.50'),
                          (460, 'OFFSETS TAKEN:', '0.00'), (470, 'CHECK AMOUNT:', check)):
        b += [W(lab, top, x0=300), W(val, top, x1=593.76)]
    b += [W('5 of 47', 760, x0=280)]
    if split_page:
        return [page_header() + a, page_header() + b]
    return [page_header() + a + b]


class TheColumnsComeFromWhereWordsSit(unittest.TestCase):
    def setUp(self):
        self.eob = parse_eob_pages(statement())

    def test_the_report_is_identified(self):
        e = self.eob
        self.assertTrue(e['parsed_ok'])
        self.assertEqual((e['eob_number'], e['issue_date'], e['provider_npi']),
                         ('26125B00000000000001', '05/05/2026', '1000000001'))

    def test_each_claim_carries_our_claim_number_and_the_payers(self):
        got = [(c['patient_account_number'], c['bsc_claim_number'], c['member_id'], c['group_number'],
                c['receipt_date']) for c in self.eob['claims']]
        self.assertEqual(got, [('1001', '260700000001', '900000001', 'W00000000001', '01/29/26'),
                               ('1002', '260700000002', '900000002', 'W00000000002', '02/02/26')])

    def test_amounts_land_in_their_own_columns(self):
        l = self.eob['claims'][0]['lines'][1]
        self.assertEqual((l['cpt'], l['units'], l['billed'], l['allowed'], l['deductible'], l['copay'], l['paid']),
                         ('J3490', '1', '300.00', '5.32', '0.00', '2.13', '3.19'))

    def test_a_blank_contractual_cell_stays_blank_and_shifts_nothing(self):
        for l in self.eob['claims'][0]['lines']:
            self.assertEqual(l['contractual'], '')
        self.assertEqual(self.eob['claims'][0]['lines'][0]['paid'], '60.00')

    def test_claim_paid_is_the_totals_row(self):
        self.assertEqual([c['claim_paid'] for c in self.eob['claims']], ['72.79', '52.12'])
        self.assertNotIn('allowed', self.eob['claims'][0]['claim_totals'])

    def test_the_recap_and_interest(self):
        e = self.eob
        self.assertEqual((e['approve_to_pay'], e['interest'], e['offsets'], e['check_amount']),
                         ('124.91', '0.50', '0.00', '125.41'))
        self.assertEqual(e['statement_totals']['paid'], '125.41')
        self.assertEqual(e['note_codes'], {'1': 'THE ALLOWED AMOUNT IS BASED ON THE FEE SCHEDULE.'})

    def test_a_consistent_report_has_no_problems(self):
        self.assertEqual(self.eob['problems'], [])
        self.assertEqual(self.eob['payment_issued_to'], '')

    def test_the_footer_is_not_a_line(self):
        self.assertEqual(sum(len(c['lines']) for c in self.eob['claims']), 4)


class ClaimsCrossPages(unittest.TestCase):
    def test_a_claim_continues_on_the_next_page(self):
        e = parse_eob_pages(statement(split_page=True))
        self.assertEqual([len(c['lines']) for c in e['claims']], [3, 1])
        self.assertEqual(e['claims'][0]['pages'], [1, 2])
        self.assertEqual(e['problems'], [])

    def test_a_page_without_the_table_is_skipped(self):
        cover = [W('Dear Provider,', 100, x0=40)]
        e = parse_eob_pages([cover] + statement())
        self.assertTrue(e['parsed_ok'])
        self.assertEqual(len(e['claims']), 2)

    def test_no_table_at_all_is_not_parsed(self):
        e = parse_eob_pages([[W('EXPLANATION OF BENEFITS', 100, x0=40)]])
        self.assertFalse(e['parsed_ok'])


class TheReportMustAgreeWithItself(unittest.TestCase):
    def test_a_misread_amount_is_caught(self):
        e = parse_eob_pages(statement(tamper='3.20'))
        self.assertTrue(any('line 2 (J3490)' in p for p in e['problems']), e['problems'])
        self.assertTrue(any("paid add up to 72.80" in p for p in e['problems']), e['problems'])

    def test_money_in_every_notation(self):
        self.assertEqual(str(money('$1,113.00')), '1113.00')
        for s in ('262.69-', '-262.69', '(262.69)', '262.69CR'):
            self.assertEqual(str(money(s)), '-262.69')

    def test_glyphs_that_are_not_content_are_dropped(self):
        base = {'object_type': 'char', 'size': 6.0, 'upright': True, 'x0': 100, 'non_stroking_color': (0,)}
        self.assertTrue(keep_char(base))
        self.assertFalse(keep_char({**base, 'size': 1.44}))          # barcode
        self.assertFalse(keep_char({**base, 'upright': False}))      # vertical form id
        self.assertFalse(keep_char({**base, 'x0': 600}))             # OMR strip
        self.assertFalse(keep_char({**base, 'non_stroking_color': (1, 1, 1)}))  # hidden text


def C(cpt, billed, units='1', drug=''):
    return {'cpt': cpt, 'billed': billed, 'units': units, 'drug': drug}


ECW = {'1001': [C('99213', '197.00'), C('J3490', '900.00', drug='50 ML ASCORBIC ACID (500MG/ML)'),
                C('J3490', '40.00', drug='1 ML B COMPLEX 100')],
       '1002': [C('96365', '325.00')]}


class FromTheReportToAPlan(unittest.TestCase):
    def test_a_clean_cheque_is_ready(self):
        p = plan_cheque(parse_eob_pages(statement()), ECW.get)
        self.assertEqual(p['status'], 'ready', (p['reasons'], [c['reasons'] for c in p['claims']]))
        rows = {(r['cpt'], r['ecw_billed']): r['paid'] for r in p['claims'][0]['rows']}
        self.assertEqual(rows, {('99213', '197.00'): '60.00', ('J3490', '900.00'): '3.19',
                                ('J3490', '40.00'): '9.60'})

    def test_a_cheque_paid_to_the_member_is_not_posted(self):
        e = parse_eob_pages(statement(paid_to_member=True))
        self.assertEqual((e['payment_issued_to'], e['check_amount'], e['problems']), ('JANE DOE', '0.00', []))
        p = plan_cheque(e, ECW.get)
        self.assertEqual(p['status'], 'needs_review')
        self.assertTrue(any('paid the member' in r for r in p['reasons']), p['reasons'])

    def test_an_adjusted_claim_is_held(self):
        e = parse_eob_pages(statement(adjusted=True))
        self.assertEqual(e['claims'][1]['adjusted_payment'], '52.12')
        p = plan_cheque(e, ECW.get)
        self.assertEqual((p['claims'][0]['status'], p['claims'][1]['status']), ('ready', 'needs_review'))
        self.assertIn('adjusted an earlier payment', p['claims'][1]['reasons'][-1])

    def test_a_report_that_disagrees_with_itself_holds_the_cheque(self):
        p = plan_cheque(parse_eob_pages(statement(tamper='3.20')), ECW.get)
        self.assertEqual(p['status'], 'needs_review')
        self.assertTrue(any('J3490' in r for r in p['reasons']), p['reasons'])


if __name__ == '__main__':
    unittest.main()
