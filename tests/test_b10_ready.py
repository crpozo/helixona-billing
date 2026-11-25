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
    # Each check pays its own claim unless a test says otherwise.
    return {'check_eft': check, 'check_amount': amount, 'check_status': 'Check Cashed', 'check_date': '10/20/2026',
            'cashed_date': '11/02/2026', 'payee_name': 'HELIXONA INC', 'eob_pdf_s3_path': pdf,
            'claims': [{'claim_id': str(int(check) % 10000), 'member_name': 'DOE, JANE'}], **kw}


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


class WhatTheCallsTaught(unittest.TestCase):
    """The B10b, B10c and B10d calls with Vignesh (2026-11)."""

    def test_the_claim_status_is_always_one_of_three(self):
        from src.eob.ecw_payment import claim_status_for
        paid = {'ecw_billed': '100.00', 'allowed': '80.00', 'deductible': '0.00', 'copay': '0.00', 'paid': '80.00'}
        owes = {**paid, 'copay': '20.00', 'paid': '60.00'}
        denied = {'ecw_billed': '125.00', 'allowed': '0.00', 'deductible': '0.00', 'copay': '0.00', 'paid': '0.00'}
        self.assertEqual(claim_status_for([paid]), 'Paid in Full')
        self.assertEqual(claim_status_for([paid, owes]), 'Patient')
        self.assertEqual(claim_status_for([paid, {**paid, 'coinsurance': '5.00'}]), 'Patient')
        self.assertEqual(claim_status_for([paid, owes, denied]), 'ERA Payer Denied', 'even one denied line')
        # A deductible that takes the whole allowed amount is owed, not denied.
        self.assertEqual(claim_status_for([{**paid, 'deductible': '80.00', 'paid': '0.00'}]), 'Patient')

    def test_a_denied_line_is_held_until_its_reason_code_can_be_entered(self):
        from src.eob import post
        plan = {'totals': {'approve_to_pay': '80.00'}, 'claims': [{'claim_id': '2187', 'rows': [
            {'ecw_index': 0, 'cpt': '96365', 'ecw_billed': '100.00', 'allowed': '80.00', 'deductible': '0.00', 'copay': '0.00', 'paid': '80.00'},
            {'ecw_index': 1, 'cpt': 'J3490', 'ecw_billed': '125.00', 'allowed': '0.00', 'deductible': '0.00', 'copay': '0.00', 'paid': '0.00'}]}]}
        page = mock.Mock()
        res = post.post_check(page, {'check_eft': '31401901', 'check_date': '10/20/2026', 'cashed_date': '11/02/2026'}, plan,
                              '07/01/2025', post=True, policy=POLICY)
        self.assertEqual((res['status'], res['claim_status']), ('held', 'ERA Payer Denied'))
        self.assertIn('J3490', res['reason'])
        page.assert_not_called()

    def test_coinsurance_has_its_own_column_and_counts_against_the_allowed_amount(self):
        from src.eob.ecw_payment import grid_rows, line_overflows
        row = {'ecw_index': 0, 'cpt': 'J3490', 'ecw_billed': '360.00', 'allowed': '72.00', 'deductible': '0.00', 'copay': '0.00',
               'coinsurance': '28.80', 'paid': '43.20'}
        rows, problems = grid_rows({'rows': [row]})
        self.assertEqual((problems, rows[0]['values'].get('CoIns')), ([], '28.80'))
        self.assertTrue(line_overflows({**row, 'paid': '50.00'}, '360.00'))
        # Blue Shield's EOB has no coinsurance: the column is left alone.
        plain, _ = grid_rows({'rows': [{**row, 'coinsurance': '', 'paid': '72.00'}]})
        self.assertNotIn('CoIns', plain[0]['values'])

    def test_a_second_payment_for_a_claim_is_an_adjusted_payment_and_is_held(self):
        first = eob('1111111', check_date='09/01/2026', claims=[{'claim_id': '9322'}])
        second = eob('2222222', amount='30.77', check_date='10/15/2026', claims=[{'claim_id': '9322'}])
        tracked = [{**TRACKED, 'check_number': '1111111'}, {**TRACKED, 'check_number': '2222222'}]
        by = {r['check']: r for r in ready.ready_queue([second, first], tracked, POLICY)}
        self.assertEqual(by['1111111']['stage'], 'ready')
        self.assertEqual(by['2222222']['stage'], 'held')
        self.assertIn('REV line', by['2222222']['reason'])
        self.assertIn('check 1111111', by['2222222']['reason'])

    def test_a_zero_eob_is_non_payment_data_under_the_eob_number_with_one_date(self):
        f, problems = payment_header({'approve_to_pay': '0.00', 'eob_number': '260703051601'},
                                     {'check_eft': '260703051601', 'check_date': '11/10/2026'}, {}, {**POLICY, 'post_zero_eobs': True})
        self.assertEqual(problems, [])
        self.assertEqual((f['Type'], f['Check No.'], f['Amount $'], f['Received Date'], f['Check Date'], f['EOB Date'], f['Deposit Date']),
                         ('Non Payment Data', '260703051601', '0.00', '11/10/2026', '11/10/2026', '11/10/2026', '11/10/2026'))
        # Off by default: a $0 EOB is refused before eCW is opened.
        _, problems = payment_header({'approve_to_pay': '0.00'}, {'check_eft': '1', 'check_date': '11/10/2026', 'cashed_date': ''}, {}, POLICY)
        self.assertTrue(problems)

    def test_the_commit_follows_the_november_demo_first_and_the_status_is_set_before_it(self):
        self.assertEqual(POLICY['commit_buttons'][0], 'Post Payment')
        self.assertIn('Auto Post', POLICY['commit_buttons'])
        p = _read('src/eob/post.py')
        body = p[p.index('def post_check('):]
        self.assertLess(body.index('set_claim_status(page, statuses[cid], check)'), body.index("what='commit the lines'"))
        self.assertIn("commit = ['Save and Next', 'Save & Next'] + commit", body)
        self.assertIn('visible_buttons(page)', body)

    def test_one_insurance_selected_by_default_is_not_a_failure(self):
        p = _read('src/eob/post.py')
        fn = p[p.index('def open_single_insurance_payment('):p.index('def fill_payment_popup(')]
        self.assertIn('taking the insurance eCW selected by default', fn)
        self.assertNotIn("_no_payer_radio')\n        return False", fn)


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
        self.assertEqual((problems, f['Deposit Date'], f['Received Date']), ([], '11/02/2026', '11/02/2026'))

    def test_no_received_date_takes_the_deposit_date(self):
        # Vignesh, 2026-11-25: "Use the deposit date instead".
        f, _ = payment_header(self.EOB, self.CHECK, {'received': '', 'deposit': '10/30/2026'}, POLICY)
        self.assertEqual((f['Received Date'], f['Deposit Date']), ('10/30/2026', '10/30/2026'))

    def test_the_bot_posts_with_its_own_ecw_user_when_its_secret_exists(self):
        from src.eob import post
        aws = mock.Mock()
        aws.secrets.get_secret_value.return_value = {'SecretString': '{"username": "billingsupport", "password": "x"}'}
        self.assertEqual(post.posting_credentials(aws)['username'], 'billingsupport')
        aws.get_secret.assert_not_called()
        aws.secrets.get_secret_value.side_effect = Exception('ResourceNotFoundException')
        aws.get_secret.return_value = {'username': 'shared', 'password': 'y'}
        self.assertEqual(post.posting_credentials(aws)['username'], 'shared')
        self.assertIn('creds = posting_credentials(aws_client)', _read('src/eob/post.py'))

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

    def test_the_daily_run_is_the_posting_bots_own_task_and_reads_only(self):
        d = _read('dashboard.py')
        block = d[d.index('ACTIONS.posting = ['):]
        card = block[:block.index("{icon: '🧪'")]
        self.assertIn("task: 'b10_daily'", card)
        self.assertNotIn('post:', card)
        m = _read('src/main.py')
        daily = m[m.index("elif task_type == 'b10_daily':"):m.index("elif task_type == 'eob_post':")]
        self.assertIn("{'blue_shield': True, 'download_eob': True, 'copies': True, 'ecw': True, 'era': False, **body}", daily)
        self.assertIn('run_check_reconcile(aws_client, daily,', daily)
        self.assertNotIn('run_eob_post', daily)


class ItIsItsOwnBot(unittest.TestCase):
    """The operator, 2026-11-19: Remittance is the SharePoint checks flow;
    B10 is a new bot."""

    def test_its_tab_unit_screen_and_queue(self):
        os.environ.setdefault('SQS_QUEUE_URL', 'https://sqs.test/q')
        import dashboard
        r = dashboard.BOT_ROUTING['posting']
        self.assertEqual((r['name'], r['service'], r['novnc_port']), ('Posting', 'helixona-agent-posting', 6087))
        self.assertIn("""data-bot="posting" onclick="setActiveBot('posting')">💵 Posting""", dashboard.DASHBOARD_HTML)
        unit = os.path.join(REPO, 'infra', 'helixona-agent-posting.service')
        if os.path.exists(unit):
            u = _read('infra/helixona-agent-posting.service')
            self.assertIn('Environment=DISPLAY=:106', u)
            self.assertIn('Environment=BOT_ROLE=posting', u)

    def test_its_panels_are_on_its_tab_and_remittance_is_as_it_was(self):
        d = _read('dashboard.py')
        self.assertIn("if (postSec) postSec.hidden = (bot !== 'posting');", d)
        self.assertIn("if (bankSec) bankSec.hidden = (bot !== 'posting');", d)
        eob_cards = d[d.index('            eob: ['):d.index('        ACTIONS.denials')] if 'ACTIONS.denials' in d else d[d.index('            eob: ['):][:6000]
        self.assertNotIn("task: 'eob_post'", eob_cards[:eob_cards.index("title: 'Test one check'")])
        self.assertNotIn('download_eob', eob_cards[:eob_cards.index("title: 'Test one check'")])


if __name__ == '__main__':
    unittest.main()
