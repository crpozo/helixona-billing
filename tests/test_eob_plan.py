"""A cheque's posting plan, and the duplicate-report rule.

A plan is what the posting step will type into eCW. It must refuse — with a
reason — whenever the money does not add up or a line cannot be placed,
because a wrong posting puts dollars on the wrong line of the ledger.
"""
import os
import unittest

from src.eob.plan import plan_cheque, plan_from_item

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


class AStoredChequeIsPlannedFromItsReport(unittest.TestCase):
    CLAIMS = {'11': {'claim_id': '11', 'hcfa_s3_path': 's3://b/hcfa/11.pdf', 'charges': '197.00'}}

    def _item(self, **kw):
        return {'check_eft': '555', 'approve_to_pay': '50.00', 'eob_interest': '0.00', 'eob_offsets': '0.00',
                'eob_check_amount': '50.00', 'eob_problems': [],
                'eob_claims': [{'patient_account_number': '11', 'claim_paid': '50.00',
                                'lines': [L('99213', '197.00', '50.00')]}], **kw}

    def test_what_was_stored_is_used_when_there_is_no_report(self):
        p = plan_from_item(self._item(), self.CLAIMS.get, lambda path: ECW['11'] if path.endswith('11.pdf') else None)
        self.assertEqual(p['status'], 'ready', p)
        self.assertEqual(p['check_eft'], '555')

    def test_the_report_is_reread_when_it_can_be(self):
        fresh = {'parsed_ok': True, 'eob_number': 'X1', 'approve_to_pay': '40.00', 'interest': '0.00',
                 'offsets': '0.00', 'check_amount': '40.00', 'problems': [],
                 'claims': [{'patient_account_number': '11', 'claim_paid': '40.00',
                             'lines': [L('99213', '197.00', '40.00')]}]}
        p = plan_from_item(self._item(eob_pdf_s3_path='s3://b/eobs/555.pdf'), self.CLAIMS.get,
                           lambda path: ECW['11'], read_eob=lambda path: fresh)
        self.assertEqual((p['eob_number'], p['claims'][0]['rows'][0]['paid']), ('X1', '40.00'))

    def test_an_unreadable_report_falls_back_with_a_note(self):
        p = plan_from_item(self._item(eob_pdf_s3_path='s3://b/eobs/555.pdf'), self.CLAIMS.get,
                           lambda path: ECW['11'], read_eob=lambda path: {'parsed_ok': False})
        self.assertEqual(p['status'], 'ready')
        self.assertIn('could not be read by position', p['notes'][0])

    def test_a_member_paid_cheque_stays_held_from_storage(self):
        p = plan_from_item(self._item(paid_to_member=True), self.CLAIMS.get, lambda path: ECW['11'])
        self.assertTrue(any('paid the member' in r for r in p['reasons']), p['reasons'])

    def test_a_missing_hcfa_is_a_reason_not_a_crash(self):
        def missing(path):
            raise OSError('NoSuchKey')
        p = plan_from_item(self._item(), self.CLAIMS.get, missing)
        self.assertIn('no HCFA lines on file for claim 11', p['claims'][0]['reasons'])

    def test_a_claim_whose_eob_lines_were_not_read_is_not_ready(self):
        item = self._item(eob_claims=[{'patient_account_number': '11', 'claim_paid': '50.00', 'lines': []}])
        p = plan_from_item(item, self.CLAIMS.get, lambda path: ECW['11'])
        self.assertEqual(p['status'], 'needs_review')
        self.assertIn('the EOB lines for claim 11 were not read', p['claims'][0]['reasons'])


class ThePlanIsNoLongerOnTheDashboard(unittest.TestCase):
    """2026-09-17: the bot compares cheques and does not enter payments, so
    the posting plan (what to type into eCW per cheque) left the dashboard.
    The planner itself stays a library, pinned above."""
    def test_the_dashboard_neither_plans_nor_posts(self):
        with open(os.path.join(REPO, 'dashboard.py'), encoding='utf-8') as fh:
            d = fh.read()
        for gone in ('def api_eob_plan', 'plan_from_item', 'Posting plan', 'window._eobPlans', 'posted_in_ecw'):
            self.assertNotIn(gone, d, gone)


if __name__ == '__main__':
    unittest.main()
