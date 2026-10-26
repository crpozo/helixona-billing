"""The Priority bot: open claims dated by their logs, aged into P1–P4,
flagged against the status SLA, sorted overdue first (the operator's flow of
2026-10-26)."""
import os
import unittest
from datetime import date
from unittest import mock

from src.priority import ecw, run

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


class Aging(unittest.TestCase):
    def test_priority_bands(self):
        self.assertEqual([run.priority_for(d) for d in (0, 30, 31, 60, 61, 90, 91, 400, None)],
                         ['P4', 'P4', 'P3', 'P3', 'P2', 'P2', 'P1', 'P1', 'P4'])

    def test_sla_by_status_with_a_default(self):
        sla = run.load_sla()
        self.assertEqual(run.sla_for('ERA PAYER DENIED', sla), 30)
        self.assertEqual(run.sla_for('era payer denied', sla), 30)
        self.assertEqual(run.sla_for('Clearinghouse Rejected', sla), 7)
        self.assertEqual(run.sla_for('Something New', sla), sla['default'])

    def test_rank_puts_overdue_first_then_p1_to_p4_then_oldest(self):
        rows = [{'claim_id': 'a', 'overdue': False, 'priority': 'P1', 'days': 95},
                {'claim_id': 'b', 'overdue': True, 'priority': 'P3', 'days': 40},
                {'claim_id': 'c', 'overdue': True, 'priority': 'P1', 'days': 120},
                {'claim_id': 'd', 'overdue': False, 'priority': 'P4', 'days': 3}]
        self.assertEqual([r['claim_id'] for r in sorted(rows, key=run.rank)], ['c', 'b', 'a', 'd'])


class TheLogsDateTheClaim(unittest.TestCase):
    def test_era_payers_and_the_rest(self):
        self.assertTrue(ecw.is_era_payer('CA Medicare Part B South J1 - PGBA'))
        self.assertTrue(ecw.is_era_payer('Anthem Blue Cross'))
        self.assertFalse(ecw.is_era_payer('Blue Shield of California'))
        self.assertFalse(ecw.is_era_payer('United Healthcare'))

    def test_billing_logs_take_the_latest_claim_status_row(self):
        text = "Billing Logs\n07/25/2025 Claim created\n08/01/2025 Claim Status: Claim sent via Symplisend\n09/14/2025 Claim Status: ERA PAYER DENIED\n09/20/2025 Note added"
        self.assertEqual(ecw.latest_status_date(text), date(2025, 9, 14))
        self.assertEqual(ecw.latest_date(text, not_after=date(2026, 10, 26)), date(2025, 9, 20))

    def test_follow_up_logs_take_the_latest_note_not_a_future_date(self):
        text = "Follow Up Logs\n08/05/2025 Called BSC, pending\n10/01/2025 Reprocessing requested\n12/31/2099 (scheduled)"
        self.assertEqual(ecw.latest_date(text, not_after=date(2026, 10, 26)), date(2025, 10, 1))


class TheRun(unittest.TestCase):
    def test_one_claim_end_to_end(self):
        table = mock.Mock()
        aws = mock.Mock(); aws.dynamodb.tables.all.return_value = [mock.Mock(name='t')]
        aws.dynamodb.tables.all.return_value[0].name = 'helixona-priority'
        aws.dynamodb.Table.return_value = table
        aws.get_secret.return_value = {'username': 'u', 'password': 'p'}
        claims = [{'claim_id': '875', 'patient': 'Castaneda, Juan', 'payer': 'Blue Shield of California', 'dos': '09/18/2025',
                   'status': 'ERA PAYER DENIED', 'charges': '2,282.50', 'paid': '93.39', 'balance': '2,189.11'},
                  {'claim_id': '1179', 'patient': 'Brutman, Meryl', 'payer': 'CA Medicare Part B South J1 - PGBA', 'dos': '10/15/2025',
                   'status': 'In Process', 'charges': '991.00', 'paid': '901.96', 'balance': '89.04'}]
        refs = {'875': {'era_payer': False, 'ref_date': date(2026, 8, 1), 'ref_source': 'Follow Up Logs · latest note', 'snippet': 'called BSC'},
                '1179': {'era_payer': True, 'ref_date': date(2026, 10, 20), 'ref_source': 'Billing Logs · latest Claim Status row', 'snippet': ''}}
        written = {}
        def put(table_, key, item):
            written[key] = item
        ipe = mock.Mock()
        with mock.patch.object(run, 'scan_all', return_value=[]), mock.patch.object(run, '_put', side_effect=put), \
                mock.patch.object(run, 'list_open_claims', return_value=(claims, [])), \
                mock.patch.object(run, 'read_reference', side_effect=lambda page, c, o, cl, shot=False: refs[c['claim_id']]), \
                mock.patch.object(run.time, 'sleep'):
            out = run.run_claim_priority(aws, {'today': '2026-10-26', 'create_claims': False}, login=lambda p, c, a: True,
                                         get_page=lambda: object(), open_claim=None, close_claim=None, create_claims=ipe)
        ipe.assert_not_called()                      # nothing is created in eCW unless asked
        a, b = written['875'], written['1179']
        self.assertEqual((a['days'], a['priority'], a['overdue'], a['sla_days']), (86, 'P2', True, 30))
        self.assertEqual((b['days'], b['priority'], b['overdue'], b['sla_days']), (6, 'P4', False, 45))
        self.assertTrue(out['ok'])
        self.assertIn('skipped (create_claims off)', written['_run']['steps']['encounters'])

    def test_create_claims_runs_the_ipe_step_over_the_last_120_days(self):
        table = mock.Mock()
        aws = mock.Mock(); aws.dynamodb.tables.all.return_value = []
        aws.dynamodb.create_table.return_value = table
        ipe = mock.Mock()
        with mock.patch.object(run, 'scan_all', return_value=[]), mock.patch.object(run, '_put'), \
                mock.patch.object(run, 'list_open_claims', return_value=([], [])):
            run.run_claim_priority(aws, {'today': '2026-10-26', 'create_claims': True}, login=lambda p, c, a: True,
                                   get_page=lambda: object(), open_claim=None, close_claim=None, create_claims=ipe)
        ipe.assert_called_once()
        self.assertEqual(ipe.call_args.args[1], '06/28/2026')


class Wiring(unittest.TestCase):
    def test_bot_dispatch_dashboard_and_host(self):
        m = _read('src/main.py')
        self.assertIn("elif task_type == 'claim_priority':", m)
        self.assertIn("_run_fix_coding_ivs_stage1(page, {}, aws_client, ecw_url, since=enc_since)", m)
        self.assertIn("def _run_fix_coding_ivs_stage1(page, body, aws_client, ecw_url, since=None):", m)
        d = _read('dashboard.py')
        for s in ("'priority': {", "'service': 'helixona-agent-priority'", "'novnc_port': 6085", 'id="priority-section"',
                  "data-bot=\"priority\" onclick=\"setActiveBot('priority')\"", "title: 'Test on 1 claim', task: 'claim_priority'",
                  "@app.route('/api/priority')", "@app.route('/api/priority.csv')", "'priority': 'Priority'" if False else "'name': 'Priority'"):
            self.assertIn(s, d)
        self.assertIn("'priority':      'SQS_QUEUE_URL_PRIORITY',", _read('src/aws/clients.py'))
        self.assertIn('sqs_queue_url_priority: str = ""', _read('src/config.py'))
        self.assertIn("'priority':      '/opt/helixona-agent/browser-profile-priority',", _read('src/ecw/browser.py'))
        self.assertIn('helixona-agent-priority', _read('deploy_code.sh'))
        if os.path.exists(os.path.join(REPO, 'infra', 'helixona-agent-priority.service')):
            u = _read('infra/helixona-agent-priority.service')
            self.assertIn('DISPLAY=:104', u)
            self.assertIn('BOT_ROLE=priority', u)
        self.assertEqual(ecw.ALL_CLAIMS, 'All Claims')


if __name__ == '__main__':
    unittest.main()
