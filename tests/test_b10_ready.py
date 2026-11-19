"""B10 — which payments are ready to enter in eCW, and what the popup takes
(the B10 flows of 2026-11: the EOB-ready check and the posting field map).
All data fabricated."""
import os
import unittest
from unittest import mock

from src.eob import ready
from src.eob.ecw_payment import payment_header

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY = ready.load_policy()


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def eob(check='0031401901', amount='257.09', pdf='s3://b/eob.pdf', **kw):
    return {'check_eft': check, 'check_amount': amount, 'check_status': 'Check Cashed', 'check_date': '10/20/2026',
            'cashed_date': '11/02/2026', 'payee_name': 'HELIXONA INC', 'eob_pdf_s3_path': pdf,
            'claims': [{'claim_id': '2187', 'member_name': 'DOE, JANE'}], **kw}


TRACKED = {'check_number': '31401901', 'in_tracker': True, 'tracker_received': '10/28/2026', 'tracker_deposit': '10/30/2026'}


class TheRule(unittest.TestCase):
    def stage(self, e, h=None):
        return ready.stage_of(e, h, POLICY)[0]

    def test_an_eob_alone_is_not_enough_the_check_must_be_in_the_tracker(self):
        self.assertEqual(self.stage(eob()), 'waiting for deposit')
        self.assertEqual(self.stage(eob(), TRACKED), 'ready')

    def test_in_the_tracker_without_the_eob_file_is_downloaded_again(self):
        self.assertEqual(self.stage(eob(pdf=''), TRACKED), 'EOB missing')
        rows = ready.ready_queue([eob(pdf='')], [TRACKED], POLICY)
        self.assertEqual(ready.eob_wanted(rows), ['0031401901'])

    def test_a_zero_eob_needs_no_deposit_and_is_held_until_its_steps_are_defined(self):
        self.assertEqual(self.stage(eob(amount='0.00')), '$0 EOB')
        self.assertEqual(ready.stage_of(eob(amount='0.00'), None, {**POLICY, 'post_zero_eobs': True})[0], 'ready')

    def test_what_ecw_already_has_is_not_entered_again(self):
        self.assertEqual(self.stage(eob(), {**TRACKED, 'in_ecw': True, 'verdict': 'posted'}), 'posted')
        self.assertEqual(self.stage(eob(), {**TRACKED, 'in_ecw': True, 'verdict': 'unposted', 'ecw_unposted': '12.00'}), 'needs a person')

    def test_a_payment_the_bot_left_unfinished_or_unbalanced_needs_a_person(self):
        for status in ('incomplete', 'unbalanced'):
            self.assertEqual(self.stage(eob(post_result={'status': status, 'reason': 'x'}), TRACKED), 'needs a person')

    def test_a_check_for_several_claims_is_held_for_now(self):
        e = eob(claims=[{'claim_id': '1'}, {'claim_id': '2'}])
        self.assertEqual(self.stage(e, TRACKED), 'held')

    def test_a_dry_run_or_a_failed_attempt_leaves_it_ready(self):
        for status in ('dry_run', 'failed'):
            self.assertEqual(self.stage(eob(post_result={'status': status, 'reason': 'x'}), TRACKED), 'ready')

    def test_other_payers_come_from_the_scan_the_tracker_confirms(self):
        rows = ready.ready_queue([], [{'check_number': '555123', 'has_copy': True, 'in_tracker': True, 'tracker_payer': 'Cigna',
                                       'copy_amount': '80.00', 'tracker_deposit': '11/01/2026'},
                                      {'check_number': '555124', 'has_copy': True, 'in_tracker': False}], POLICY)
        self.assertEqual([(r['check'], r['payer'], r['stage']) for r in rows], [('555123', 'Cigna', 'other payer')])

    def test_the_queue_is_in_working_order_and_counted(self):
        rows = ready.ready_queue([eob('1111111'), eob('2222222'), eob('3333333', amount='0.00')],
                                 [{**TRACKED, 'check_number': '2222222'}], POLICY)
        self.assertEqual([r['stage'] for r in rows], ['ready', '$0 EOB', 'waiting for deposit'])
        sm = ready.summarize(rows)
        self.assertEqual((sm['ready'], sm['zero'], sm['waiting_for_deposit'], sm['ready_amount']), (1, 1, 1, '257.09'))


class ThePopupTakesTheTrackersDates(unittest.TestCase):
    EOB = {'approve_to_pay': '257.09'}
    CHECK = {'check_eft': '31401901', 'check_date': '10/20/2026', 'cashed_date': '11/02/2026'}
    T = {'received': '10/28/2026', 'deposit': '10/30/2026'}

    def test_received_and_deposit_from_the_tracker_and_the_facility_selected(self):
        f, problems = payment_header(self.EOB, self.CHECK, self.T, POLICY)
        self.assertEqual(problems, [])
        self.assertEqual((f['Received Date'], f['Deposit Date'], f['Check Date'], f['EOB Date'], f['Facility'], f['Type'], f['Amount $']),
                         ('10/28/2026', '10/30/2026', '10/20/2026', '10/20/2026', 'select', 'Check', '257.09'))

    def test_no_tracker_date_falls_back_to_the_cashed_date(self):
        f, problems = payment_header(self.EOB, self.CHECK, {'received': '', 'deposit': ''}, POLICY)
        self.assertEqual((problems, f['Deposit Date'], f['Received Date']), ([], '11/02/2026', None))

    def test_no_deposit_date_anywhere_is_a_problem(self):
        _, problems = payment_header(self.EOB, {**self.CHECK, 'cashed_date': ''}, {}, POLICY)
        self.assertTrue(any('no deposit date' in p for p in problems))

    def test_without_a_policy_the_popup_is_the_recordings(self):
        f, _ = payment_header(self.EOB, self.CHECK)
        self.assertEqual((f['Facility'], f['Received Date'], f['Deposit Date']), (None, None, '11/02/2026'))


class TheRunObeysTheRule(unittest.TestCase):
    """run_eob_post enters only what the queue calls ready."""

    def _run(self, eobs, checks, body):
        from src.eob import post
        tables = {post.EOB_TABLE: mock.Mock(), 'helixona-checks': mock.Mock()}
        aws = mock.Mock()
        aws.dynamodb.Table.side_effect = lambda n: tables.setdefault(n, mock.Mock())
        planned = []

        def fake_scan(t, **kw):
            return [dict(e) for e in (eobs if t is tables[post.EOB_TABLE] else checks)]

        def fake_plan(it, **kw):
            planned.append(str(it['check_eft']))
            return {'status': 'needs_review', 'reasons': ['stop here'], 'claims': [], 'totals': {}}
        with mock.patch.object(post, 'scan_all', side_effect=fake_scan), mock.patch.object(post, 'plan_from_item', side_effect=fake_plan), \
                mock.patch.object(post, 'make_readers', return_value={}):
            out = post.run_eob_post(mock.Mock(), aws, body, login=mock.Mock(side_effect=AssertionError('eCW must not be opened')))
        return planned, out

    def test_only_deposited_checks_with_an_eob_are_considered(self):
        eobs = [eob('1111111'), eob('2222222'), eob('3333333', pdf='')]
        checks = [{**TRACKED, 'check_number': '2222222'}, {**TRACKED, 'check_number': '3333333'}]
        planned, out = self._run(eobs, checks, {})
        self.assertEqual(planned, ['2222222'])
        self.assertTrue(out['ok'])

    def test_naming_a_check_does_not_lift_the_rule(self):
        planned, _ = self._run([eob('1111111')], [], {'check_eft': '1111111', 'post': True})
        self.assertEqual(planned, [])

    def test_the_gate_and_the_quality_check_are_in_the_code(self):
        p = _read('src/eob/post.py')
        self.assertIn("post = bool(body.get('post'))", p)
        body = p[p.index('def post_check('):]
        self.assertLess(body.index('if not post:'), body.index("['Payment Advisory']"))
        self.assertIn("'status': 'unbalanced'", body)
        self.assertIn('attach_eob(page, eob_file(), check)', body)

    def test_the_policy_file_is_valid_and_keeps_the_safe_defaults(self):
        self.assertEqual((POLICY['multi_claim_checks'], POLICY['post_zero_eobs'], POLICY['attach_eob']), (False, False, True))
        self.assertIn('Auto Post', POLICY['commit_buttons'])


class TheDailyRunSavesTheEobs(unittest.TestCase):
    def test_the_reconcile_walk_passes_the_eob_options_to_the_portal(self):
        src = _read('src/checks/run.py')
        self.assertIn("'download_eob': bool(body.get('download_eob'))", src)
        self.assertIn('eob_wanted(ready_queue(', src)
        cap = _read('src/eob/capture.py')
        self.assertIn("if download_eob and not it.get('eob_pdf_s3_path') and _norm(it.get('check_eft')) in eob_for:", cap)
        self.assertIn("include_zero=bool(body.get('include_zero'))", cap)

    def test_the_daily_card_downloads_and_reads_only(self):
        d = _read('dashboard.py')
        card = d[d.index("title: 'B10 daily run'"):][:900]
        self.assertIn("task: 'check_reconcile'", card)
        self.assertIn('download_eob: true', card)
        self.assertNotIn('post:', card)


if __name__ == '__main__':
    unittest.main()
