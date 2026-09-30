"""Denials — the SOP's cheat sheet, read and matched (2026-09-30)."""
import json
import os
import unittest

from src.denials.cheatsheet import (DATA, Rule, action_kind, codes_in, load_rules, match, norm_code,
                                    payer_key, procedure_codes)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TheSheetIsInTheRepoWithoutTheLogins(unittest.TestCase):
    def test_rules_and_appeals_only(self):
        data = json.load(open(DATA, encoding='utf-8'))
        self.assertEqual(set(data), {'source', 'rules', 'appeals'})
        self.assertGreaterEqual(len(data['rules']), 16)
        blob = json.dumps(data).lower()
        for secret in ('password', 'dharu', 'sasikumar', 'user name'):
            self.assertNotIn(secret, blob)
        first = data['rules'][0]
        self.assertEqual((first['codes'], first['drug'], first['payer'], first['reason']), ('J0612', 'Calcium Gluconate', 'Medicare', 'CO-16'))
        self.assertIn('write off', first['action'].lower())
        # The importer reads only those two sheets.
        src = open(os.path.join(REPO, 'scripts', 'import_denial_sop.py'), encoding='utf-8').read()
        self.assertIn("SHEETS = ('Denial Cheatsheet', 'Appeals')", src)
        self.assertNotIn('Insurance Portal', src.split('"""', 2)[2])


class CodesAreReadTheWayPayersWriteThem(unittest.TestCase):
    def test_reason_codes(self):
        self.assertEqual(codes_in('CO-16'), ['CO-16', '16'])
        self.assertEqual(codes_in('CO-16 / 96'), ['CO-16', '16', '96'])
        self.assertEqual(codes_in('PI:96, PR:96'), ['PI-96', '96', 'PR-96'])
        self.assertEqual(codes_in('N846 / N816'), ['N846', 'N816'])
        self.assertEqual(codes_in('B16 - New Patient qualifications not met'), ['B16'])
        self.assertEqual(codes_in('Non-Covered'), ['non-covered'])
        self.assertEqual(codes_in('Duplicate claim/service'), ['duplicate'])
        # As eCW / an ERA prints them.
        self.assertEqual(codes_in('CO16 Claim/service lacks information; N382'), ['CO-16', '16', 'N382'])
        self.assertEqual(norm_code('co 16'), 'CO-16')
        self.assertEqual(norm_code('n846'), 'N846')

    def test_procedure_codes_and_payers(self):
        self.assertEqual(procedure_codes('J3490 / J7999'), ['J3490', 'J7999'])
        self.assertEqual(procedure_codes('J0612-JZ, 96365, 99215'), ['96365', '99215', 'J0612'])
        self.assertEqual(payer_key('Medicare Southern CA (Noridian)'), 'medicare')
        self.assertEqual(payer_key('ANTHEM BLUE CROSS OF CALIFORNIA'), 'anthem')
        self.assertEqual(payer_key('Any Payer'), 'any')
        self.assertEqual(payer_key(''), 'any')

    def test_the_kind_of_action(self):
        self.assertEqual(action_kind('Reimbursement is only $0.04. So please write off the balance'), 'write off')
        self.assertEqual(action_kind('Send an appeal with the following content'), 'appeal')
        self.assertEqual(action_kind('Please send an inquiry through Availity'), 'inquiry')
        self.assertEqual(action_kind('Send the medical necessity with the following verbiage in the cover letter'), 'medical records')
        self.assertEqual(action_kind('change the code to CPT 99215'), 'recode')
        self.assertEqual(action_kind('Ask the lead'), 'review')


class ADeniedClaimFindsItsRule(unittest.TestCase):
    def setUp(self):
        self.rules, self.appeals = load_rules()

    def rows(self, hits):
        return [r.sop_row for r in hits]

    def test_the_sops_own_example(self):
        hits = match({'cpt': 'J0612', 'payer': 'Medicare', 'reasons': 'CO-16'}, self.rules)
        self.assertEqual(self.rows(hits), ['1'])
        self.assertEqual(hits[0].kind, 'write off')

    def test_the_drug_line_decides_between_j3490_rules(self):
        # CO-16 on J3490 for Medicare fits rules 2, 3, 4, 5 alike — the drug
        # name is not on the claim row, so all four come back for a person.
        hits = match({'cpt': 'J3490', 'payer': 'Medicare', 'reasons': ['CO-16']}, self.rules)
        self.assertEqual(self.rows(hits), ['2', '3', '4', '5'])
        # '96' too narrows to the two rules that name it.
        hits = match({'cpt': 'J3490', 'payer': 'Medicare', 'reasons': 'CO-16 / 96'}, self.rules)
        self.assertEqual(self.rows(hits), ['4', '5'])

    def test_anthem_rules(self):
        self.assertEqual(self.rows(match({'cpt': 'J0640', 'payer': 'Anthem Blue Cross', 'reasons': 'N846'}, self.rules)), ['8'])
        self.assertEqual(self.rows(match({'cpt': 'J3490 96365', 'payer': 'Anthem Blue Cross CA', 'reasons': 'CO-252'}, self.rules)), ['15'])
        self.assertEqual(self.rows(match({'cpt': '99417 99215', 'payer': 'Anthem', 'reasons': 'CO-97 N19'}, self.rules)), ['16'])
        self.assertEqual(self.rows(match({'cpt': 'J3490', 'payer': 'Anthem', 'reasons': 'PR-96'}, self.rules)), ['11'])
        self.assertEqual(self.rows(match({'cpt': 'J3490', 'payer': 'Anthem', 'reasons': 'Duplicate'}, self.rules)), ['10', '12', '13'])

    def test_any_payer_and_no_rule(self):
        self.assertEqual(self.rows(match({'cpt': '99205', 'payer': 'Cigna', 'reasons': 'B16'}, self.rules)), ['6'])
        # Medicare CO-16 on a code no rule names: nothing — a person decides.
        self.assertEqual(match({'cpt': '96365', 'payer': 'Medicare', 'reasons': 'CO-16'}, self.rules), [])
        # Blue Shield is not Anthem: no Anthem rule applies.
        self.assertEqual(match({'cpt': 'J3490', 'payer': 'Blue Shield of California', 'reasons': 'CO-96'}, self.rules), [])

    def test_a_rule_round_trips(self):
        r = Rule({'sop_row': '9', 'codes': 'J3490 / J7999', 'drug': 'X', 'payer': 'Medicare', 'reason': 'CO-16 / 96', 'action': 'write off'})
        self.assertEqual(r.as_dict()['codes'], 'J3490 / J7999')
        self.assertEqual(r.reasons, ['CO-16', '16', '96'])


if __name__ == '__main__':
    unittest.main()
