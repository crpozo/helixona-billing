"""The 2-step code hand-off from a person to a waiting bot.

On 2026-09-10 Remittance's first login stopped at Blue Shield's e-mail code: the
practice Gmail refused its app password, and the operator, holding the code,
had no way to give it to the bot. Now there is one — with two rules that keep
it from doing harm: a code counts only if typed after the bot asked for one,
and it is consumed on read.
"""
import os
import unittest

from src.utils import mfa_relay
from src.utils.mfa_relay import post_manual_code, wait_for_manual_code

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


class FakeTable:
    def __init__(self):
        self.item = None
        self.updates = 0

    def put_item(self, Item):
        self.item = dict(Item)

    def get_item(self, Key):
        return {'Item': dict(self.item)} if self.item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        self.updates += 1
        self.item['used'] = ExpressionAttributeValues[':t']


class FakeDynamo:
    def __init__(self):
        self.t = FakeTable()

    def Table(self, name):
        assert name == 'helixona-tasks'
        return self.t


class OnlyDigitsAreAccepted(unittest.TestCase):
    def test_a_six_digit_code_is_stored(self):
        db = FakeDynamo()
        item = post_manual_code(db, ' 526014 ')
        self.assertEqual(item['code'], '526014')
        self.assertFalse(item['used'])
        self.assertEqual(db.t.item['task_id'], 'mfa_code')

    def test_a_stray_paste_is_refused(self):
        for bad in ('', 'abc', '12', '123456789', 'DROP TABLE'):
            with self.assertRaises(ValueError, msg=bad):
                post_manual_code(FakeDynamo(), bad)


class AStaleCodeIsNeverTyped(unittest.TestCase):
    def test_a_code_from_before_the_request_is_ignored(self):
        db = FakeDynamo()
        db.t.item = {'task_id': 'mfa_code', 'code': '111111',
                     'submitted_at': '2026-09-10T20:00:00Z', 'used': False}
        got = wait_for_manual_code(db, requested_at='2026-09-10T21:00:00Z', timeout_s=1, poll_s=1)
        self.assertEqual(got, '')

    def test_a_fresh_code_is_returned_and_consumed(self):
        db = FakeDynamo()
        db.t.item = {'task_id': 'mfa_code', 'code': '526014',
                     'submitted_at': '2026-09-10T21:13:00Z', 'used': False}
        got = wait_for_manual_code(db, requested_at='2026-09-10T21:12:35Z', timeout_s=2, poll_s=1)
        self.assertEqual(got, '526014')
        self.assertTrue(db.t.item['used'])
        self.assertEqual(db.t.updates, 1)

    def test_a_used_code_is_not_reused(self):
        db = FakeDynamo()
        db.t.item = {'task_id': 'mfa_code', 'code': '526014',
                     'submitted_at': '2026-09-10T21:13:00Z', 'used': True}
        self.assertEqual(wait_for_manual_code(db, '2026-09-10T21:12:35Z', timeout_s=1, poll_s=1), '')

    def test_a_table_error_reads_as_no_code_not_a_crash(self):
        class Broken:
            def Table(self, name):
                raise RuntimeError('dynamo down')
        self.assertEqual(wait_for_manual_code(Broken(), '2026-09-10T21:12:35Z', timeout_s=1, poll_s=1), '')

    def test_the_relay_lives_in_the_tasks_table(self):
        self.assertEqual(mfa_relay.TABLE, 'helixona-tasks')


class TheLoginFallsBackToThePerson(unittest.TestCase):
    def test_the_request_time_is_taken_before_send_code(self):
        s = _read('src/blueshield/session.py')
        self.assertLess(s.index('code_requested_at = now_iso()'), s.index("page.click('text=Send code'"))

    def test_gmail_is_tried_first_then_the_dashboard(self):
        s = _read('src/blueshield/session.py')
        i = s.index('mfa_code = fetch_mfa_code(')
        j = s.index('wait_for_manual_code(aws_client.dynamodb, code_requested_at)')
        self.assertLess(i, j)
        self.assertIn('if not mfa_code:', s[i:j])

    def test_the_log_tells_the_operator_where_to_type(self):
        self.assertIn('MFA code box', _read('src/blueshield/session.py'))


class TheDashboardHasTheBox(unittest.TestCase):
    def test_the_input_and_endpoint_exist(self):
        d = _read('dashboard.py')
        self.assertIn('id="mfa-code"', d)
        self.assertIn("@app.route('/api/mfa-code', methods=['POST'])", d)
        self.assertIn('post_manual_code(dynamodb', d)

    def test_enter_sends_it(self):
        self.assertIn("event.key==='Enter'", _read('dashboard.py'))

    def test_the_box_is_not_tied_to_one_bot(self):
        # Intake and Follow-up hit the same 2-step on a fresh profile.
        d = _read('dashboard.py')
        i = d.index('id="mfa-box"')
        self.assertNotIn('data-bot', d[i - 200:i + 300])


if __name__ == '__main__':
    unittest.main()
