"""The lock rule end to end: a note's signature is its lock, an unlocked
note holds the claim, three business days bring a notice to the person and
their manager, and the weekly summary lists every one."""
import os
import unittest
from datetime import date
from unittest import mock

from src.ecw import note_lock as nl
from src.notes import unlocked as un
from src.notify import email as mail

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


SIGNED = "HELIXONA\nPatient: DOE, JANE  DOS: 09/15/2026\nAdministered by: Smith, John RN\nVitals...\nElectronically signed by Jane Provider MD on 09/16/2026 10:02"
UNSIGNED = "HELIXONA\nPatient: DOE, JANE  DOS: 09/15/2026\nAdministered by: Smith, John RN\nVitals..."


class TheSignatureIsTheLock(unittest.TestCase):
    def test_signed_is_locked_unsigned_is_not(self):
        self.assertEqual(nl.lock_status(SIGNED), 'locked')
        self.assertEqual(nl.lock_status(UNSIGNED), 'unlocked')
        self.assertEqual(nl.lock_status(''), 'unknown')
        self.assertEqual(nl.lock_status('Note locked by Dr. Who 09/16/2026'), 'locked')

    def test_who_signed_and_who_must_lock(self):
        self.assertEqual(nl.signer(SIGNED), 'Jane Provider MD')
        self.assertEqual(nl.responsible(SIGNED, 'X'), 'Jane Provider MD')
        self.assertEqual(nl.responsible(UNSIGNED, 'Rendering, Doc'), 'Smith, John RN')
        self.assertEqual(nl.responsible('nothing here', 'Rendering, Doc'), 'Rendering, Doc')

    def test_business_days(self):
        self.assertEqual(nl.business_days_since('09/25/2026', date(2026, 10, 2)), 5)   # Fri → next Fri
        self.assertEqual(nl.business_days_since('10/01/2026', date(2026, 10, 2)), 1)
        self.assertEqual(nl.business_days_since('09/26/2026', date(2026, 9, 28)), 1)   # Sat → Mon
        self.assertIsNone(nl.business_days_since('garbage', date(2026, 10, 2)))


class TheRoster(unittest.TestCase):
    roster = {'from': 'bot@x.com', 'default_manager': 'mgr@x.com', 'weekly_to': ['team@x.com'],
              'people': {'Smith, John': {'email': 'john@x.com', 'manager': 'lead@x.com'}, 'Doe, Jane': {'email': 'jane@x.com'}}}

    def test_names_as_ecw_shows_them(self):
        self.assertEqual(mail.find_person(self.roster, 'SMITH, JOHN RN')['email'], 'john@x.com')
        self.assertEqual(mail.find_person(self.roster, 'John Smith')['email'], 'john@x.com')
        self.assertEqual(mail.find_person(self.roster, 'Doe, J')['email'], 'jane@x.com')
        self.assertIsNone(mail.find_person(self.roster, 'Smith, Mary'))
        self.assertIsNone(mail.find_person(self.roster, ''))

    def test_manager_falls_back_to_the_default(self):
        self.assertEqual(mail.manager_of(self.roster, mail.find_person(self.roster, 'Smith, John')), 'lead@x.com')
        self.assertEqual(mail.manager_of(self.roster, mail.find_person(self.roster, 'Doe, Jane')), 'mgr@x.com')
        self.assertEqual(mail.manager_of(self.roster, None), 'mgr@x.com')

    def test_nothing_is_sent_without_a_recipient_or_sender(self):
        self.assertEqual(mail.send(mock.Mock(), self.roster, [], 's', 't'), '')
        self.assertEqual(mail.send(mock.Mock(), {}, ['a@x.com'], 's', 't'), '')
        with mock.patch.object(mail, 'NOTIFY_FROM', ''):
            self.assertEqual(mail.send(mock.Mock(), self.roster, ['a@x.com'], 's', 't', dry_run=True), 'dry-run')


class TheNotices(unittest.TestCase):
    claims = [
        {'claim_id': '100', 'patient_name': 'Doe, Jane', 'service_date': '09/24/2026', 'cpt': '96365', 'iv_note_locked': False,
         'note_owner': 'Smith, John RN', 'prog_notes_s3_path': 's3://x'},
        {'claim_id': '101', 'patient_name': 'Roe, Rick', 'service_date': '10/01/2026', 'cpt': '99214', 'iv_note_locked': False,
         'note_owner': 'Smith, John RN'},
        {'claim_id': '102', 'patient_name': 'Poe, Pat', 'service_date': '09/01/2026', 'cpt': '96365', 'iv_note_locked': False,
         'note_owner': 'Nobody, New', 'unlock_notice_sent_at': '2026-09-20T10:00:00+00:00'},
        {'claim_id': '103', 'patient_name': 'Locked, Lou', 'service_date': '09/01/2026', 'iv_note_locked': True},
        {'claim_id': '104', 'patient_name': 'Sent, Sam', 'service_date': '09/01/2026', 'iv_note_locked': False, 'symplisend_submitted': 1},
        {'claim_id': '105', 'patient_name': 'Gone, Gil', 'service_date': '09/01/2026', 'iv_note_locked': False, 'ecw_visible': False},
        {'claim_id': '_summary'},
    ]

    def test_which_notes_are_outstanding_and_overdue(self):
        rows = un.unlocked_notes(self.claims, date(2026, 10, 2))
        self.assertEqual([r['claim_id'] for r in rows], ['102', '100', '101'])
        by = {r['claim_id']: r for r in rows}
        self.assertTrue(by['100']['overdue'] and by['102']['overdue'])
        self.assertFalse(by['101']['overdue'])                 # 1 business day
        self.assertEqual(by['101']['appt_type'], 'Office Visit')
        self.assertEqual(by['100']['appt_type'], 'IV Therapy')

    def test_the_run_writes_to_person_and_manager_once_and_the_weekly_summary(self):
        aws = mock.Mock()
        table = mock.Mock()
        aws.dynamodb.Table.return_value = table
        sent = []

        def fake_send(aws_client, roster, to, subject, text, html=None, cc=None, dry_run=False):
            sent.append({'to': to, 'cc': cc or [], 'subject': subject, 'text': text})
            return 'mid-%d' % len(sent)
        roster = TheRoster.roster
        with mock.patch.object(un, 'scan_all', return_value=self.claims), mock.patch.object(un, 'ensure_table', return_value=table), \
                mock.patch.object(mail, 'load_roster', return_value=roster), mock.patch.object(mail, 'send', side_effect=fake_send):
            out = un.run_unlocked_notes(aws, {'weekly': True, 'today': '2026-10-02'})
        # 100 is due and untold → one notice to Smith cc lead. 102 was told already. 101 is not due.
        notices = [s for s in sent if 'to lock in eCW' in s['subject']]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['to'], ['john@x.com'])
        self.assertEqual(notices[0]['cc'], ['lead@x.com'])
        self.assertIn('claim 100', notices[0]['text'])
        self.assertNotIn('claim 101', notices[0]['text'])
        weekly = [s for s in sent if 'Weekly unlocked' in s['subject']]
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0]['to'], ['team@x.com'])
        for cid in ('100', '101', '102'):
            self.assertIn(f'claim {cid}', weekly[0]['text'])
        self.assertEqual(out['unlocked'], 3)
        self.assertEqual(out['notices_sent'], 1)
        aws.update_claim_status.assert_called_once()
        self.assertEqual(aws.update_claim_status.call_args.args[0], '100')

    def test_unknown_owner_goes_to_the_manager_alone(self):
        aws = mock.Mock(); table = mock.Mock(); aws.dynamodb.Table.return_value = table
        sent = []
        claims = [dict(self.claims[2], unlock_notice_sent_at='')]
        with mock.patch.object(un, 'scan_all', return_value=claims), mock.patch.object(un, 'ensure_table', return_value=table), \
                mock.patch.object(mail, 'load_roster', return_value=TheRoster.roster), \
                mock.patch.object(mail, 'send', side_effect=lambda *a, **k: sent.append((a[2], k.get('cc'))) or 'mid'):
            un.run_unlocked_notes(aws, {'today': '2026-10-02'})
        self.assertEqual(sent, [(['mgr@x.com'], [])])


class Wiring(unittest.TestCase):
    def test_extractor_gate_dashboard_and_deploy(self):
        m = _read('src/main.py')
        self.assertIn("elif task_type == 'unlocked_notes':", m)
        self.assertIn("'iv_note_locked': _lock == 'locked',", m)
        self.assertIn("or (has_prog_notes and 'iv_note_locked' not in item))", m)
        d = _read('dashboard.py')
        self.assertIn("c.iv_note_locked === false) ? 'IV Note not locked' : ''", d)
        self.assertIn("title: 'Notify about unlocked notes', task: 'unlocked_notes'", d)
        self.assertIn("title: 'Weekly unlocked-notes summary', task: 'unlocked_notes'", d)
        dep = _read('deploy_code.sh')
        self.assertIn('helixona-unlocked-notes.timer helixona-unlocked-notes-weekly.timer', dep)
        self.assertIn('OnCalendar=Mon..Fri *-*-* 16:00:00 UTC', _read('infra/helixona-unlocked-notes.timer'))
        self.assertIn('OnCalendar=Mon *-*-* 15:00:00 UTC', _read('infra/helixona-unlocked-notes-weekly.timer'))
        self.assertIn('"task_type": "unlocked_notes", "weekly": true', _read('infra/helixona-unlocked-notes-weekly.service'))
        self.assertIn('data/note_lock_roster.json', _read('.gitignore'))


if __name__ == '__main__':
    unittest.main()
