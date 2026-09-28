"""Remittance — who has Blue Shield's money.

The operator (2026-09-28): "what patients cashed the checks from Blue Shield
that we have to charge them for it. And what patients did Blue Shield send
checks and they are missing so we need to ask Blue Shield to reissue …
Total checks sent · Checks cashed by Helixona · Checks cashed by patient ·
Checks missing/never cashed."
"""
import datetime
import os
import unittest

from src.checks.reconcile import reconcile, summarize

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = datetime.date(2026, 9, 28)


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def _bs(no, status, date, payee='HELIXONA INC', cashed='', member=False, patient='DOE, JANE'):
    return {'check_eft': no, 'check_amount': '100.00', 'check_status': status, 'check_date': date, 'cashed_date': cashed,
            'payee_name': payee, 'paid_to_member': member, 'claims': [{'member_name': patient}]}


class WhoHasBlueShieldsMoney(unittest.TestCase):
    def test_the_buckets(self):
        checks = [
            _bs('1', 'Check Cashed', '08/01/2026', cashed='08/10/2026'),                       # scanned: Helixona cashed it
            _bs('2', 'Check Cashed', '08/01/2026', payee='DOE, JANE', cashed='08/10/2026'),    # patient cashed it
            _bs('3', 'Check Cashed', '08/01/2026', cashed='08/10/2026'),                       # cashed, payee Helixona, no trace
            _bs('4', 'Check Number Assigned', '06/01/2026'),                                   # 119 days, nobody has it
            _bs('5', 'Check Number Assigned', '09/20/2026'),                                   # 8 days: in the mail
            _bs('6', 'Check Number Assigned', '09/01/2026'),                                   # in the tracker, not cleared
            _bs('7', 'Check Number Assigned', '06/01/2026', member=True),                      # sent to the patient, not cashed
            _bs('8', 'Void - Check Payment Stopped', '06/01/2026'),
            _bs('9', 'Check Cashed', '08/01/2026', payee='DOE, JANE', cashed='08/10/2026'),    # patient payee, but posted in eCW
        ]
        rows, sm = reconcile(copies=[{'check_number': '1', 'amount': '100.00'}], checks=checks,
                             payments=[{'check_no': '9', 'amount': '100.00', 'posted': '100.00', 'unposted': '0.00'}],
                             tracker=[{'check_no': '6', 'amount': '100.00'}], today=TODAY)
        by = {r['check_number']: r['bs_bucket'] for r in rows}
        self.assertEqual(by, {'1': 'cashed by Helixona', '2': 'cashed by patient', '3': 'cashed, not on file',
                              '4': 'missing, ask Blue Shield to reissue', '5': 'in transit', '6': 'in hand, not cleared',
                              '7': 'sent to patient, not cashed', '8': 'voided', '9': 'cashed by Helixona'})
        r2 = next(r for r in rows if r['check_number'] == '2')
        self.assertIn('cashed by patient, bill the patient', r2['flags'])
        self.assertEqual((r2['bs_payee'], r2['bs_patients']), ('DOE, JANE', 'DOE, JANE'))
        self.assertIn('missing, ask Blue Shield to reissue', next(r for r in rows if r['check_number'] == '4')['flags'])
        self.assertEqual((sm['bs_sent'], sm['bs_cashed_helixona'], sm['bs_cashed_patient'], sm['bs_cashed_unknown'],
                          sm['bs_missing'], sm['bs_in_transit'], sm['bs_in_hand_uncleared'], sm['bs_to_patient_uncashed'], sm['bs_voided']),
                         (9, 2, 1, 1, 1, 1, 1, 1, 1))

    def test_a_check_no_payer_lists_has_no_bucket(self):
        rows, _ = reconcile(copies=[{'check_number': '77', 'amount': '1.00'}], checks=[], payments=[], today=TODAY)
        self.assertEqual(rows[0]['bs_bucket'], '')

    def test_the_run_and_the_pages_carry_it(self):
        r = _read('src/checks/run.py')
        self.assertIn("bs_payee = :bp, bs_paid_to_member = :bpm, bs_patients = :bpa, bs_bucket = :bb", r)
        # The Blue Shield rows are read WITH the payee and the claims, or every bucket reads "not on file".
        self.assertIn("'payee_name, paid_to_member, claims')", r)
        d = _read('dashboard.py')
        self.assertIn("'bs_payee', 'bs_paid_to_member', 'bs_patients', 'bs_bucket'", d)
        self.assertIn("tile('cashed by patient', sm.bs_cashed_patient, 'bs:cashed by patient'", d)
        h = _read('dashboard_checks.html')
        self.assertIn("['bs:cashed by patient', 'Cashed by patient'", h)
        self.assertIn("['bs:missing, ask Blue Shield to reissue', 'Missing, ask Blue Shield to reissue'", h)
        self.assertIn("if (f.startsWith('bs:')) return f === 'bs:all' ? !!r.in_blue_shield : r.bs_bucket === f.slice(3);", h)


if __name__ == '__main__':
    unittest.main()
