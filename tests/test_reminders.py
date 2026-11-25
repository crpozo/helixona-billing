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


def eob(check='0031401901', payee='JANE DOE', status='Check Issued', member='DOE, JANE', **kw):
    return {'check_eft': check, 'check_amount': '$257.09', 'check_status': status, 'check_date': '10/20/2026',
            'payee_name': payee, 'claims': [{'member_name': member, 'claim_id': '2187', 'dos': '09/01/2026'}], **kw}


class WhichChecks(unittest.TestCase):
    def test_a_check_made_out_to_the_patient_is_kept(self):
        rows = run.patient_checks([eob()])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r['check_number'], r['amount'], r['patient'], r['claim_ids'], r['dos']),
                         ('31401901', '257.09', 'DOE, JANE', ['2187'], '09/01/2026'))
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
                                   [{'claim_id': '_run'}, {'claim_id': '2301', 'patient': 'DOE, JANE', 'dos': '10/23/2026'}])
        row = {'patient': 'YAZMIN ALMANZA', 'dos': '10/23/2026'}
        self.assertEqual(run.match_by_patient(row, index), (['2190'], 'patient + DOS'))
        # Another date of service: any claim of hers leads to her record.
        self.assertEqual(run.match_by_patient({'patient': 'YAZMIN ALMANZA', 'dos': '10/16/2026'}, index), (['2190'], 'patient (another visit)'))
        self.assertEqual(run.match_by_patient({'patient': 'SOMEONE ELSE', 'dos': '10/23/2026'}, index), ([], ''))
        self.assertEqual(run.match_by_patient({'patient': 'ALMANZA', 'dos': '10/23/2026'}, index), ([], ''), 'a surname alone is not a patient')

    def test_the_claims_screen_is_opened_before_any_claim_and_a_miss_is_said(self):
        # The lookup box is on Billing → Claims, not on the schedule the login lands on.
        src = _read('src/reminders/run.py')
        self.assertLess(src.index('go_to_claims(page)'), src.index('read_contact(page, r['))
        page = mock.Mock()
        with mock.patch.object(ecw, 'logger') as log:
            self.assertIsNone(ecw.read_contact(page, '2190', lambda p, c: (False, None), lambda p: None))
        self.assertIn('the claim did not open', log.warning.call_args.args[0])

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
        for piece in ('Hello Jane,', '31401901', '$257.09', '10/20/2026', 'November 26, 2026', 'card we have on file'):
            self.assertIn(piece, text)
        self.assertNotIn('{', text)

    def test_a_cashed_check_asks_for_the_payment_not_the_check(self):
        row = run.patient_checks([eob(status='Check Cashed')])[0]
        _, text = run.compose(POLICY, row, date(2026, 11, 26))
        self.assertIn('has been cashed', text)
        self.assertNotIn('Bring or mail the check', text)

    def test_no_card_on_file_means_no_promise_to_charge_one(self):
        row = run.patient_checks([eob()])[0]
        _, text = run.compose(POLICY, {**row, 'card_on_file': False}, date(2026, 11, 26))
        self.assertNotIn('card we have on file', text)
        self.assertIn('billed to you as a patient balance', text)
        self.assertNotIn('{', text)
        # Not read is not "no card": the card sentence stays.
        self.assertIn('card we have on file', run.compose(POLICY, row, date(2026, 11, 26))[1])
        self.assertIn('card we have on file', run.compose(POLICY, {**row, 'card_on_file': True}, date(2026, 11, 26))[1])

    def test_the_final_notice_says_so(self):
        row = run.patient_checks([eob()])[0]
        self.assertTrue(run.compose(POLICY, row, date(2026, 11, 26), 'final')[0].startswith('Final notice'))

    def test_first_names(self):
        self.assertEqual([run.first_name(n) for n in ('DOE, JANE A', 'Jane Doe', '')], ['Jane', 'Jane', 'there'])


class TheContact(unittest.TestCase):
    def test_the_labelled_address_wins_and_the_clinics_own_is_never_the_patients(self):
        got = ecw.contact_in("billing@helixona.com\nother@example.org\nE-mail: jane.d@example.com   Cell: 415-555-0100\nHome: (415) 555-0199")
        self.assertEqual(got, {'email': 'jane.d@example.com', 'phone': '(415) 555-0100'})

    def test_the_address_inside_a_form_box_is_read_by_the_boxs_name(self):
        # 2026-11-17, claim 7874: "e-mail not found · phone found" — the
        # address was in an <input>, and a box's value is not page text.
        fields = [{'name': 'ptFname First Name', 'value': 'JANE'},
                  {'name': 'respEmail Guarantor', 'value': 'someone@helixona.com'},
                  {'name': 'homePhone Home', 'value': '415-555-0199'},
                  {'name': 'cellPhone Cell', 'value': '(415) 555-0100'},
                  {'name': 'faxNo Fax', 'value': '415-555-0000'},
                  {'name': 'notes', 'value': 'alt contact old@example.org'},
                  {'name': 'patientEmail E-mail', 'value': 'Jane.D@Example.com'}]
        self.assertEqual(ecw.contact_in_fields(fields), {'email': 'jane.d@example.com', 'phone': '(415) 555-0100'})
        self.assertEqual(ecw.contact_in_fields([{'name': 'zip', 'value': '94110'}]), {'email': '', 'phone': ''})

    HUB = ("Patient Hub\nDOE, Jane\nJan 1, 1990 (36 yo F)\nAcc No. 99999\nDoe, Jane 36Y, F\nINFO\n1 Main St, TOWN,CA-90000\n|\n555-010-0199\n|\n"
           "jane.doe@example.com\n|\n01/01/1990\nAccount No: 99999 | Messenger Enabled: Yes\nBilling\nPatient Balance\n:\n$10.00\n"
           "Card on File\n:\n{card}\nBilling Alert\nGuarantor Balance")

    def test_the_patient_hub_gives_the_address_the_mobile_and_the_card(self):
        # The operator, 2026-11-17: "you go inside Hub and check the email".
        text = self.HUB.format(card='Add Card')
        self.assertEqual(ecw.contact_in(text), {'email': 'jane.doe@example.com', 'phone': '(555) 010-0199'})
        self.assertIs(ecw.card_on_file(text), False)
        self.assertIs(ecw.card_on_file(self.HUB.format(card='Visa ending 4242')), True)
        self.assertIsNone(ecw.card_on_file('Billing\nPatient Balance : $10.00'))
        self.assertIsNone(ecw.card_on_file('Card on File\n:\nBilling Alert'))

    def test_the_hub_is_tried_before_the_info_form(self):
        src = _read('src/reminders/ecw.py')
        body = src[src.index('def read_contact('):]
        self.assertLess(body.index("_click_text(page, HUB,"), body.index("_click_text(page, ['Info'],"))

    def test_nothing_found_is_empty(self):
        self.assertEqual(ecw.contact_in('Claim 2187 · DOE, JANE · 09/01/2026 · 257.09'), {'email': '', 'phone': ''})

    def test_the_contact_file_by_claim_or_by_name(self):
        row = {'claim_ids': ['2187'], 'patient': 'DOE, JANE'}
        self.assertEqual(run.contact_from_file({'claims': {'2187': {'email': 'a@x.com'}}, 'patients': {}}, row)['email'], 'a@x.com')
        self.assertEqual(run.contact_from_file({'claims': {}, 'patients': {'Jane Doe': {'email': 'b@x.com'}}}, row)['email'], 'b@x.com')
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
    def _run(self, body, contacts=None, prev=(), kw=None):
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
            out = run.run_patient_check_reminders(aws, {'today': '2026-11-12', 'channel': 'email', **body}, **(kw or {}))
        return out, table.items, sent

    CONTACTS = {'claims': {'2187': {'email': 'jane.d@example.com'}}, 'patients': {}}

    def test_without_send_nothing_is_emailed_and_the_draft_is_kept(self):
        out, items, sent = self._run({}, self.CONTACTS)
        self.assertEqual(sent, [])
        it = items['31401901']
        self.assertEqual((it['stage'], it['reminders_sent'], it['next_action']), ('to remind', 0, 'first'))
        self.assertIn('$257.09', it['draft_text'])
        self.assertEqual(out['sent'], 0)

    def test_send_emails_the_patient_once_and_sets_the_deadline(self):
        out, items, sent = self._run({'send': True}, self.CONTACTS)
        self.assertEqual([s[0] for s in sent], [['jane.d@example.com']])
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


class ThroughOhMD(unittest.TestCase):
    """The operator, 2026-11-25: the message goes through OhMD, the clinic's
    own conversation with the patient — not SMS, not e-mail."""
    DETAILS = "Chat Details\nJane Doe\nDOB\n01/02/1980\nMobile\n(555) 010-0199\nPref. Contact\nText\nID/MRN\n10263\nPref. Language\nN/A"

    def test_the_chat_details_panel_is_read_by_its_labels(self):
        from src.reminders import ohmd
        self.assertEqual(ohmd.details_in(self.DETAILS), {'dob': '01/02/1980', 'mobile': '5550100199', 'mrn': '10263'})
        self.assertEqual(ohmd.search_name('DOE, JANE A'), 'Jane Doe')

    def test_the_name_alone_never_sends_a_message(self):
        from src.reminders import ohmd
        d = ohmd.details_in(self.DETAILS)
        self.assertEqual(ohmd.identity_ok({'acct_no': '10263'}, d), (True, 'MRN = eCW account number'))
        self.assertFalse(ohmd.identity_ok({'acct_no': '99999', 'dob': '01/02/1980'}, d)[0], 'another account number is another patient, whatever the birthday')
        self.assertTrue(ohmd.identity_ok({'dob': '1/2/1980'}, d)[0])
        self.assertFalse(ohmd.identity_ok({'dob': '03/04/1975'}, d)[0])
        self.assertTrue(ohmd.identity_ok({'phone': '(555) 010-0199'}, d)[0])
        self.assertFalse(ohmd.identity_ok({}, d)[0])
        self.assertFalse(ohmd.identity_ok({'acct_no': '10263'}, {'dob': '', 'mobile': '', 'mrn': ''})[0])

    def test_the_hub_gives_the_account_number_and_the_date_of_birth(self):
        got = ecw.identity_in("Patient Hub\nDOE, Jane\nJan 2, 1980 (46 yo F)\nAcc No. 10263\n1 Main St\n555-010-0199\n01/02/1980\nAccount No: 10263")
        self.assertEqual(got, {'acct_no': '10263', 'dob': '01/02/1980'})

    def test_the_wording_for_a_conversation_is_short_and_has_no_subject_line(self):
        row = {**run.patient_checks([eob()])[0], 'card_on_file': False}
        _, text = run.compose(POLICY, row, date(2026, 11, 26), 'first', 'ohmd')
        self.assertTrue(text.startswith('Hello Jane, this is Helixona Billing.'))
        self.assertIn('billed to you as a patient balance', text)
        self.assertNotIn('{', text)
        self.assertLess(len(text), 900)
        self.assertTrue(run.compose(POLICY, row, date(2026, 11, 26), 'final', 'ohmd')[1].startswith('FINAL NOTICE.'))

    def _run(self, body, status='sent', prev=()):
        calls = []
        fake = mock.Mock()
        fake.login.return_value = True

        def send_message(page, row, text, send=False):
            calls.append((row['check_number'], send, text[:20]))
            return {'status': status if send else 'typed', 'reason': 'identity: MRN = eCW account number'}
        fake.send_message.side_effect = send_message
        known = [{'check_number': '31401901', 'acct_no': '10263', 'phone': '(555) 010-0199', **p} for p in (prev or [{}])]
        out, items, sent = TheRun()._run({'channel': 'ohmd', 'ecw': False, **body}, prev=known,
                                         kw={'ohmd': fake, 'get_page': lambda: mock.Mock()})
        return out, items, calls, sent

    def test_a_test_types_the_message_and_sends_nothing(self):
        out, items, calls, mailed = self._run({'ohmd_test': True})
        self.assertEqual(calls, [('31401901', False, 'Hello Jane, this is ')])
        it = items['31401901']
        self.assertEqual((it['ohmd_status'], it['reminders_sent'], it['channel']), ('typed', 0, 'ohmd'))
        self.assertEqual((out['sent'], mailed), (0, []))

    def test_send_goes_through_ohmd_and_counts_once(self):
        out, items, calls, mailed = self._run({'send': True})
        self.assertEqual([(c[0], c[1]) for c in calls], [('31401901', True)])
        it = items['31401901']
        self.assertEqual((it['ohmd_status'], it['reminders_sent'], it['stage'], it['deadline']), ('sent', 1, 'reminded', '2026-11-26'))
        self.assertEqual(mailed, [], 'no e-mail on the OhMD channel')

    def test_a_patient_not_proven_or_not_found_is_owed_not_counted(self):
        for status in ('unproven', 'not found', 'failed'):
            out, items, calls, _ = self._run({'send': True}, status=status)
            self.assertEqual((out['owed'], items['31401901']['reminders_sent'], items['31401901']['ohmd_status']), (1, 0, status))

    def test_without_send_or_a_test_ohmd_is_not_even_opened(self):
        out, items, calls, _ = self._run({})
        self.assertEqual(calls, [])
        self.assertIn('Hello Jane', items['31401901']['draft_text'])

    def test_the_sender_never_presses_send_in_a_test_and_never_uses_the_notes_box(self):
        src = _read('src/reminders/ohmd.py')
        fn = src[src.index('def send_message('):]
        self.assertLess(fn.index('if not send:'), fn.index("name=re.compile(r'^send', re.I)"))
        self.assertLess(fn.index('identity_ok(row, details)'), fn.index('page.keyboard.insert_text(text)'))
        self.assertIn("shared notes|colleagues about", src)


class TheReviewList(unittest.TestCase):
    """What would be sent and to whom, for the people who approve it."""
    ROWS = [
        {'check_number': '111', 'patient': 'DOE, JANE', 'amount': '250.00', 'check_date': '10/20/2026', 'dos': '09/01/2026',
         'stage': 'to remind', 'next_action': 'first', 'email': 'jane.d@example.com', 'claim_ids': ['2187'], 'card_on_file': False},
        {'check_number': '222', 'patient': 'ROE, MARY', 'amount': '80.00', 'check_date': '10/21/2026', 'stage': 'to remind',
         'next_action': 'first', 'email': '', 'claim_ids': ['2190'], 'cashed': True},
        {'check_number': '333', 'patient': 'POE, ANN', 'amount': '10.00', 'check_date': '11/10/2026', 'stage': 'in the mail', 'next_action': ''},
        {'check_number': '444', 'patient': 'LOE, SAM', 'amount': '99.00', 'stage': 'received', 'next_action': ''},
    ]

    def test_who_gets_a_message_who_gets_a_call_and_who_waits(self):
        from src.reminders import review
        to_send, to_call, waiting, sm = review.review_lists(self.ROWS, {**POLICY, 'channel': 'email'}, TODAY)
        self.assertEqual([(r[0], r[1], r[10], r[11]) for r in to_send], [('DOE, JANE', 'jane.d@example.com', 'NO', 'First notice')])
        self.assertIn('billed to you as a patient balance', to_send[0][14])
        self.assertEqual(to_send[0][12], '11/26/2026')
        self.assertEqual([(r[0], r[8]) for r in to_call], [('ROE, MARY', 'yes')])
        self.assertIn('not looked up in eCW yet', to_call[0][9])
        self.assertEqual([r[0] for r in waiting], ['POE, ANN'])
        self.assertEqual((sm['to_send'], sm['to_call'], sm['waiting'], sm['to_send_amount'], sm['no_card']), (1, 1, 1, 250.0, 1))

    def test_the_workbook_opens_and_carries_the_message_as_it_will_go_out(self):
        import io
        import openpyxl
        from src.reminders import review
        wb = openpyxl.load_workbook(io.BytesIO(review.review_workbook(self.ROWS, {**POLICY, 'channel': 'email'}, TODAY)))
        self.assertEqual(wb.sheetnames, ['Summary', 'To send', 'To call', 'Not due now', 'Message wording'])
        row = [c.value for c in wb['To send'][2]]
        self.assertEqual(row[:2], ['DOE, JANE', 'jane.d@example.com'])
        self.assertIn('Hello Jane,', row[14])
        self.assertEqual(wb['Message wording'].max_row, 5)
        self.assertIn('Nothing has been sent', ' '.join(str(c.value) for c in wb['Summary']['A']))

    def test_on_the_ohmd_channel_it_lists_who_can_be_proven_there(self):
        from src.reminders import review
        rows = [{**self.ROWS[0], 'email': '', 'acct_no': '10263'}, self.ROWS[1]]
        to_send, to_call, _, sm = review.review_lists(rows, POLICY, TODAY)
        self.assertEqual([(r[0], r[1]) for r in to_send], [('DOE, JANE', 'OhMD conversation (account no. 10263)')])
        self.assertTrue(to_send[0][14].startswith('Hello Jane, this is Helixona Billing.'))
        self.assertEqual([r[0] for r in to_call], ['ROE, MARY'])
        self.assertEqual(sm['channel'], 'ohmd')

    def test_the_download_is_on_the_tab_and_sends_nothing(self):
        self.assertIn('href="/api/reminders/review.xlsx"', _read('dashboard.py'))
        src = _read('src/reminders/review.py')
        for word in ('mail.send', 'send_email', 'update_item', 'put_item'):
            self.assertNotIn(word, src)


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
