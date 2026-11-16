"""The Reminders bot: Blue Shield checks mailed to the patient — which ones,
when a reminder is due, what it says, and that nothing reaches a patient
unless the task says send (the operator, 2026-11-12)."""
import os
import unittest
from datetime import date
from unittest import mock

from src.reminders import ecw, run

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY = run.load_policy()
TODAY = date(2026, 11, 12)


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def eob(check='0031401901', payee='KARI THORNE', status='Check Issued', member='THORNE, KARI', **kw):
    return {'check_eft': check, 'check_amount': '$257.09', 'check_status': status, 'check_date': '10/20/2026',
            'payee_name': payee, 'claims': [{'member_name': member, 'claim_id': '2187', 'dos': '09/01/2026'}], **kw}


class WhichChecks(unittest.TestCase):
    def test_a_check_made_out_to_the_patient_is_kept(self):
        rows = run.patient_checks([eob()])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r['check_number'], r['amount'], r['patient'], r['claim_ids'], r['dos']),
                         ('31401901', '257.09', 'THORNE, KARI', ['2187'], '09/01/2026'))
        self.assertFalse(r['in_hand'])

    def test_helixona_and_other_payees_are_left_out(self):
        self.assertEqual(run.patient_checks([eob(payee='HELIXONA INC'), eob(check='7', payee='DR SOMEONE ELSE MD'),
                                             eob(check='8', payee='')]), [])

    def test_the_eob_saying_paid_to_member_is_enough(self):
        self.assertEqual(len(run.patient_checks([eob(payee='J SMITH', paid_to_member=True)])), 1)

    def test_a_check_helixona_holds_is_received(self):
        for held in ({'has_copy': True}, {'in_tracker': True}, {'in_ecw': True}):
            r = run.patient_checks([eob()], [{'check_number': '31401901', **held}])[0]
            self.assertTrue(r['in_hand'], held)
            self.assertEqual(run.plan(r, {}, POLICY, TODAY), ('received', ''))

    def test_checks_issued_before_the_window_are_left_out(self):
        self.assertEqual(run.patient_checks([eob()], since=date(2026, 11, 1)), [])

    def test_cashed_and_voided_are_read_from_the_status(self):
        self.assertTrue(run.patient_checks([eob(status='Check Cashed')])[0]['cashed'])
        r = run.patient_checks([eob(status='Check Voided')])[0]
        self.assertEqual(run.plan(r, {}, POLICY, TODAY), ('voided', ''))


class WhenAReminderIsDue(unittest.TestCase):
    ROW = {'in_hand': False, 'voided': False}

    def test_a_new_check_gets_the_first_reminder(self):
        self.assertEqual(run.plan(self.ROW, {}, POLICY, TODAY), ('to remind', 'first'))

    def test_then_it_waits_the_interval(self):
        prev = {'reminders_sent': 1, 'last_reminder_at': '2026-11-10', 'deadline': '2026-11-24'}
        self.assertEqual(run.plan(self.ROW, prev, POLICY, TODAY), ('reminded', ''))
        prev['last_reminder_at'] = '2026-11-07'
        self.assertEqual(run.plan(self.ROW, prev, POLICY, TODAY), ('to remind', 'reminder'))

    def test_the_last_one_is_the_final_notice_and_then_no_more(self):
        prev = {'reminders_sent': POLICY['max_reminders'] - 1, 'last_reminder_at': '2026-11-01', 'deadline': '2026-11-24'}
        self.assertEqual(run.plan(self.ROW, prev, POLICY, TODAY), ('to remind', 'final'))
        prev['reminders_sent'] = POLICY['max_reminders']
        self.assertEqual(run.plan(self.ROW, prev, POLICY, TODAY), ('reminded', ''))

    def test_past_the_deadline_it_is_a_persons_decision(self):
        prev = {'reminders_sent': 1, 'last_reminder_at': '2026-10-20', 'deadline': '2026-11-11'}
        self.assertEqual(run.plan(self.ROW, prev, POLICY, TODAY), ('deadline passed', ''))


class FoundOnTheFirstRealRun(unittest.TestCase):
    """2026-11-16, the first test on the host."""

    def test_the_summary_holds_no_float(self):
        # "Float types are not supported. Use Decimal types instead."
        sm = run.summarize([{'stage': 'to remind', 'amount': '323.58'}, {'stage': 'received', 'amount': '10.10'}])
        self.assertFalse([k for k, v in sm.items() if isinstance(v, float)])
        self.assertEqual((sm['outstanding_amount'], sm['amount'], sm['to remind']), ('323.58', '333.68', 1))

    def test_a_check_with_no_claim_finds_the_patients_claim_by_name(self):
        index = run.index_patients([{'claim_id': '2190', 'patient_name': 'Almanza, Yazmin M', 'service_date': '10/23/2026'},
                                    {'claim_id': '2150', 'patient_name': 'Almanza, Yazmin M', 'service_date': '09/04/2026'},
                                    {'claim_id': '2200', 'patient_name': 'Almanza, Rosa', 'service_date': '10/23/2026'}],
                                   [{'claim_id': '_run'}, {'claim_id': '2301', 'patient': 'THORNE, KARI', 'dos': '10/23/2026'}])
        row = {'patient': 'YAZMIN ALMANZA', 'dos': '10/23/2026'}
        self.assertEqual(run.match_by_patient(row, index), (['2190'], 'patient + DOS'))
        # Another date of service: any claim of hers leads to her record.
        self.assertEqual(run.match_by_patient({'patient': 'YAZMIN ALMANZA', 'dos': '10/16/2026'}, index), (['2190'], 'patient (another visit)'))
        self.assertEqual(run.match_by_patient({'patient': 'SOMEONE ELSE', 'dos': '10/23/2026'}, index), ([], ''))
        self.assertEqual(run.match_by_patient({'patient': 'ALMANZA', 'dos': '10/23/2026'}, index), ([], ''), 'a surname alone is not a patient')

    def test_a_check_just_issued_is_still_in_the_mail(self):
        row = {'in_hand': False, 'voided': False, 'cashed': False, 'check_date': '11/12/2026'}
        self.assertEqual(run.plan(row, {}, POLICY, TODAY), ('in the mail', ''))
        self.assertEqual(run.plan({**row, 'check_date': '10/30/2026'}, {}, POLICY, TODAY), ('to remind', 'first'))
        self.assertEqual(run.plan({**row, 'cashed': True}, {}, POLICY, TODAY), ('to remind', 'first'), 'cashed: it arrived')


class TheMessage(unittest.TestCase):
    def test_it_names_the_check_the_amount_and_the_deadline(self):
        row = run.patient_checks([eob()])[0]
        subject, text = run.compose(POLICY, row, date(2026, 11, 26))
        self.assertIn('Blue Shield', subject)
        for piece in ('Hello Kari,', '31401901', '$257.09', '10/20/2026', 'November 26, 2026', 'card we have on file'):
            self.assertIn(piece, text)
        self.assertNotIn('{', text)

    def test_a_cashed_check_asks_for_the_payment_not_the_check(self):
        row = run.patient_checks([eob(status='Check Cashed')])[0]
        _, text = run.compose(POLICY, row, date(2026, 11, 26))
        self.assertIn('has been cashed', text)
        self.assertNotIn('Bring or mail the check', text)

    def test_the_final_notice_says_so(self):
        row = run.patient_checks([eob()])[0]
        self.assertTrue(run.compose(POLICY, row, date(2026, 11, 26), 'final')[0].startswith('Final notice'))

    def test_first_names(self):
        self.assertEqual([run.first_name(n) for n in ('THORNE, KARI A', 'Kari Thorne', '')], ['Kari', 'Kari', 'there'])


class TheContact(unittest.TestCase):
    def test_the_labelled_address_wins_and_the_clinics_own_is_never_the_patients(self):
        got = ecw.contact_in("billing@helixona.com\nother@example.org\nE-mail: kari.t@example.com   Cell: 415-555-0100\nHome: (415) 555-0199")
        self.assertEqual(got, {'email': 'kari.t@example.com', 'phone': '(415) 555-0100'})

    def test_nothing_found_is_empty(self):
        self.assertEqual(ecw.contact_in('Claim 2187 · THORNE, KARI · 09/01/2026 · 257.09'), {'email': '', 'phone': ''})

    def test_the_contact_file_by_claim_or_by_name(self):
        row = {'claim_ids': ['2187'], 'patient': 'THORNE, KARI'}
        self.assertEqual(run.contact_from_file({'claims': {'2187': {'email': 'a@x.com'}}, 'patients': {}}, row)['email'], 'a@x.com')
        self.assertEqual(run.contact_from_file({'claims': {}, 'patients': {'Kari Thorne': {'email': 'b@x.com'}}}, row)['email'], 'b@x.com')
        self.assertIsNone(run.contact_from_file({'claims': {}, 'patients': {}}, row))


class FakeTable:
    def __init__(self, items=()):
        self.items = {i[self.key]: dict(i) for i in items}

    key = 'check_number'

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        it = self.items.setdefault(Key[self.key], {self.key: Key[self.key]})
        import re
        pairs = dict(part.strip().split(' = ') for part in UpdateExpression[4:].split(', '))
        for ph, name in ExpressionAttributeNames.items():
            # DynamoDB's own rule for a placeholder.
            if not re.fullmatch(r'#[A-Za-z0-9_]+', ph) or not re.fullmatch(r':[A-Za-z0-9_]+', pairs[ph]):
                raise ValueError(f'ExpressionAttributeNames contains invalid key: "{ph}"')
            v = ExpressionAttributeValues[pairs[ph]]
            if isinstance(v, float):
                raise TypeError('Float types are not supported. Use Decimal types instead.')
            it[name] = v


class TheRun(unittest.TestCase):
    def _run(self, body, contacts=None, prev=()):
        table = FakeTable(prev)
        sent = []

        def fake_send(aws, roster, to, subject, text, **kw):
            sent.append((to, subject))
            return 'mid-1'

        def fake_scan(t, **kw):
            return list(t.items.values()) if isinstance(t, FakeTable) else t
        aws = mock.Mock()
        aws.dynamodb.Table.side_effect = lambda name: [eob()] if name == run.EOB_TABLE else []
        with mock.patch.object(run, 'ensure_table', return_value=table), \
                mock.patch.object(run, 'scan_all', side_effect=fake_scan), \
                mock.patch.object(run, 'load_contacts', return_value=contacts or {'claims': {}, 'patients': {}}), \
                mock.patch.object(run.mail, 'send', side_effect=fake_send), \
                mock.patch.object(run.mail, 'load_roster', return_value={'from': 'billing@helixona.com'}):
            out = run.run_patient_check_reminders(aws, {'today': '2026-11-12', **body})
        return out, table.items, sent

    CONTACTS = {'claims': {'2187': {'email': 'kari.t@example.com'}}, 'patients': {}}

    def test_without_send_nothing_is_emailed_and_the_draft_is_kept(self):
        out, items, sent = self._run({}, self.CONTACTS)
        self.assertEqual(sent, [])
        it = items['31401901']
        self.assertEqual((it['stage'], it['reminders_sent'], it['next_action']), ('to remind', 0, 'first'))
        self.assertIn('$257.09', it['draft_text'])
        self.assertEqual(out['sent'], 0)

    def test_send_emails_the_patient_once_and_sets_the_deadline(self):
        out, items, sent = self._run({'send': True}, self.CONTACTS)
        self.assertEqual([s[0] for s in sent], [['kari.t@example.com']])
        it = items['31401901']
        self.assertEqual((it['stage'], it['reminders_sent'], it['deadline'], it['last_reminder_at']),
                         ('reminded', 1, '2026-11-26', '2026-11-12'))
        # The same day again: nothing more goes out.
        out2, _, sent2 = self._run({'send': True}, self.CONTACTS, prev=[it])
        self.assertEqual(sent2, [])

    def test_a_test_copy_goes_to_the_tester_and_counts_nothing(self):
        out, items, sent = self._run({'test_to': 'me@helixona.com'}, self.CONTACTS)
        self.assertEqual(sent[0][0], ['me@helixona.com'])
        self.assertTrue(sent[0][1].startswith('[TEST] '))
        self.assertEqual(items['31401901']['reminders_sent'], 0)
        self.assertNotIn('deadline', items['31401901'])

    def test_no_email_on_file_is_owed_not_sent(self):
        out, items, sent = self._run({'send': True, 'ecw': False})
        self.assertEqual(sent, [])
        self.assertEqual(out['owed'], 1)
        self.assertEqual(out['no_email'], 1)


class Wiring(unittest.TestCase):
    def test_the_bot_never_charges_a_card(self):
        for rel in ('src/reminders/run.py', 'src/reminders/ecw.py'):
            src = _read(rel).lower()
            for word in ('stripe', 'authorize.net', 'square', 'charge_card', 'payment_intent'):
                self.assertNotIn(word, src, rel)

    def test_only_the_reminders_bot_runs_the_task(self):
        src = _read('src/main.py')
        self.assertIn("REMINDER_TASKS = {'patient_check_reminders'}", src)
        self.assertIn("if _settings.bot_role != 'reminders' and task_type in REMINDER_TASKS:", src)
        self.assertIn("elif task_type == 'patient_check_reminders':", src)

    def test_the_send_card_asks_first_and_the_others_do_not_send(self):
        import re
        os.environ.setdefault('SQS_QUEUE_URL', 'https://sqs.test/q')
        import dashboard
        block = re.search(r'ACTIONS\.reminders = \[(.*?)\];', dashboard.DASHBOARD_HTML, re.S).group(1)
        cards = block.split("{icon:")[1:]
        self.assertEqual(len(cards), 3)
        sending = [c for c in cards if 'send: true' in c]
        self.assertEqual(len(sending), 1)
        self.assertIn('confirm:', sending[0])
        self.assertIn('if (a.confirm && !confirm(a.confirm)) return;', dashboard.DASHBOARD_HTML)

    def test_the_unit_and_the_contact_file(self):
        unit = os.path.join(REPO, 'infra', 'helixona-agent-reminders.service')
        if not os.path.exists(unit):
            self.skipTest('infra/ is not deployed to the host')
        u = _read('infra/helixona-agent-reminders.service')
        self.assertIn('Environment=DISPLAY=:105', u)
        self.assertIn('Environment=BOT_ROLE=reminders', u)
        self.assertIn('data/patient_contacts.json', _read('.gitignore'))


if __name__ == '__main__':
    unittest.main()
