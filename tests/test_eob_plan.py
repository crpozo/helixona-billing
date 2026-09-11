"""A cheque's posting plan, and the duplicate-report rule.

A plan is what the posting step will type into eCW. It must refuse — with a
reason — whenever the money does not add up or a line cannot be placed,
because a wrong posting puts dollars on the wrong line of the ledger.
"""
import os
import unittest

from src.eob.plan import plan_cheque

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def L(cpt, billed, paid, units='1', allowed='0.00', deductible='0.00', copay='0.00', drug=''):
    return {'cpt': cpt, 'billed': billed, 'paid': paid, 'units': units, 'allowed': allowed,
            'deductible': deductible, 'copay': copay, 'drug': drug}


ECW = {
    '2627': [L('96365', '325.00', ''), L('96375', '575.00', '', units='5'),
             L('J3490', '40.00', '', drug='1 ML B COMPLEX 100')],
    '11': [L('99213', '197.00', '')],
}


def EOB(claims, approve):
    return {'approve_to_pay': approve, 'check_amount': approve, 'interest': '0.00', 'claims': claims}


class AChequeIsReadyOnlyWhenEveryClaimIs(unittest.TestCase):
    def test_a_clean_cheque(self):
        eob = EOB([{'patient_account_number': '2627', 'bsc_claim_number': '260703051601',
                    'claim_paid': '150.00',
                    'lines': [L('96365', '325.00', '100.00'), L('J3490', '40.00', '50.00')]}], '150.00')
        p = plan_cheque(eob, ECW.get, claim_charges={'2627': '940.00'})
        self.assertEqual(p['status'], 'ready', p)
        claim = p['claims'][0]
        self.assertEqual(claim['claim_id'], '2627')
        self.assertEqual(len(claim['rows']), 2)
        self.assertEqual(claim['unposted_ecw'], [1])          # 96375 not on this EOB

    def test_one_bad_claim_holds_the_whole_cheque(self):
        eob = EOB([{'patient_account_number': '11', 'claim_paid': '50.00', 'lines': [L('99213', '197.00', '50.00')]},
                   {'patient_account_number': '404', 'claim_paid': '10.00', 'lines': [L('96365', '1.00', '10.00')]}],
                  '60.00')
        p = plan_cheque(eob, ECW.get)
        self.assertEqual(p['status'], 'needs_review')
        self.assertEqual(p['claims'][0]['status'], 'ready')
        self.assertIn('no HCFA lines on file for claim 404', p['claims'][1]['reasons'])


class TheMoneyIsReconciled(unittest.TestCase):
    def test_claims_must_add_up_to_approve_to_pay(self):
        eob = EOB([{'patient_account_number': '11', 'claim_paid': '50.00',
                    'lines': [L('99213', '197.00', '50.00')]}], '75.00')
        p = plan_cheque(eob, ECW.get)
        self.assertEqual(p['status'], 'needs_review')
        self.assertIn('claims paid add up to 50.00, the EOB approves 75.00', p['reasons'])

    def test_a_missed_hcfa_line_is_caught_by_the_charges_on_record(self):
        eob = EOB([{'patient_account_number': '11', 'claim_paid': '50.00',
                    'lines': [L('99213', '197.00', '50.00')]}], '50.00')
        p = plan_cheque(eob, ECW.get, claim_charges={'11': '394.00'})
        self.assertEqual(p['status'], 'needs_review')
        self.assertIn('a line was not read', p['claims'][0]['reasons'][0])

    def test_totals_are_reported(self):
        eob = EOB([{'patient_account_number': '11', 'claim_paid': '50.00',
                    'lines': [L('99213', '197.00', '50.00')]}], '50.00')
        t = plan_cheque(eob, ECW.get)['totals']
        self.assertEqual((t['claims_paid'], t['approve_to_pay']), ('50.00', '50.00'))

    def test_an_eob_with_no_claims_is_not_ready(self):
        self.assertEqual(plan_cheque(EOB([], '0.00'), ECW.get)['status'], 'needs_review')

    def test_a_claim_without_an_account_number_is_not_guessed(self):
        eob = EOB([{'patient_account_number': '', 'claim_paid': '5.00', 'lines': []}], '5.00')
        p = plan_cheque(eob, ECW.get)
        self.assertIn('no patient account number', p['claims'][0]['reasons'][0])


class IdenticalReportsAreStoredOnce(unittest.TestCase):
    """The operator's step 2: 'check for duplicate if file with same
    information we remove it'. The same EOB downloaded twice is byte-for-byte
    identical, so identity is the file's hash."""

    def _capture(self):
        with open(os.path.join(REPO, 'src', 'eob', 'capture.py'), encoding='utf-8') as fh:
            return fh.read()

    def test_every_report_is_hashed(self):
        c = self._capture()
        self.assertIn('sha = _sha256(pdf_path)', c)
        self.assertIn("'eob_pdf_sha256': sha", c)

    def test_a_known_hash_reuses_the_stored_copy(self):
        c = self._capture()
        i = c.index('dup = known_pdfs.get(sha)')
        block = c[i:i + 600]
        self.assertIn('s3_path = dup[1]', block)
        self.assertIn('not storing a second copy', block)

    def test_hashes_already_on_file_are_loaded_before_the_run(self):
        self.assertIn("ProjectionExpression='check_eft, eob_pdf_s3_path, eob_pdf_sha256'", self._capture())


if __name__ == '__main__':
    unittest.main()
