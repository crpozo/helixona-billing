"""The dashboard's door: a PIN before any page or API (the operator,
2026-11-25). The host's own calls pass; five wrong tries lock an address."""
import os
import unittest
from unittest import mock

os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')
os.environ.setdefault('SQS_QUEUE_URL', 'https://sqs.test/q')
import dashboard

OUTSIDE = {'REMOTE_ADDR': '203.0.113.7'}


class TheDoor(unittest.TestCase):
    def setUp(self):
        dashboard._pin_tries.clear()
        p = mock.patch.object(dashboard, 'admin_key', return_value='test-admin-key')
        p.start()
        self.addCleanup(p.stop)
        self.c = dashboard.app.test_client()

    def test_a_page_goes_to_the_pin_and_an_api_says_401(self):
        r = self.c.get('/', environ_base=OUTSIDE)
        self.assertEqual((r.status_code, r.headers['Location']), (302, '/pin'))
        r = self.c.get('/checks', environ_base=OUTSIDE)
        self.assertEqual((r.status_code, r.headers['Location']), (302, '/pin?next=%2Fchecks'))
        for path in ('/api/claims', '/api/reminders', '/api/bank', '/api/post-queue', '/api/reminders/review.xlsx'):
            r = self.c.get(path, environ_base=OUTSIDE)
            self.assertEqual(r.status_code, 401, path)
            self.assertTrue(r.get_json()['pin_required'])
        self.assertEqual(self.c.post('/api/send-task', json={'task_type': 'eob_post', 'post': True}, environ_base=OUTSIDE).status_code, 401)

    def test_the_right_pin_opens_it_and_the_wrong_one_does_not(self):
        r = self.c.post('/pin', data={'pin': '0000'}, environ_base=OUTSIDE)
        self.assertEqual(r.status_code, 401)
        self.assertIn(b'Wrong PIN', r.data)
        r = self.c.post('/pin', data={'pin': dashboard.DASHBOARD_PIN, 'next': '/checks'}, environ_base=OUTSIDE)
        self.assertEqual((r.status_code, r.headers['Location']), (302, '/checks'))
        self.assertIn('HttpOnly', r.headers['Set-Cookie'])
        with mock.patch.object(dashboard, 'render_template_string', return_value='page'):
            self.assertEqual(self.c.get('/', environ_base=OUTSIDE).status_code, 200)

    def test_five_wrong_tries_lock_the_address_even_for_the_right_pin(self):
        for _ in range(dashboard.PIN_MAX_TRIES):
            r = self.c.post('/pin', data={'pin': '1'}, environ_base=OUTSIDE)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.c.post('/pin', data={'pin': dashboard.DASHBOARD_PIN}, environ_base=OUTSIDE).status_code, 429)
        # Another address is not locked out by it.
        other = dashboard.app.test_client()
        self.assertEqual(other.post('/pin', data={'pin': dashboard.DASHBOARD_PIN}, environ_base={'REMOTE_ADDR': '198.51.100.9'}).status_code, 302)

    def test_the_hosts_own_timers_still_reach_the_api(self):
        with mock.patch.object(dashboard, 'sqs') as sqs:
            r = self.c.post('/api/send-task', json={'task_type': 'unlocked_notes', 'bot': 'submissions'})
        self.assertNotEqual(r.status_code, 401)

    def test_a_cookie_from_another_pin_or_an_old_one_is_refused(self):
        self.c.post('/pin', data={'pin': dashboard.DASHBOARD_PIN}, environ_base=OUTSIDE)
        with mock.patch.object(dashboard, 'DASHBOARD_PIN', '9999'):
            self.assertEqual(self.c.get('/api/claims', environ_base=OUTSIDE).status_code, 401)
        with mock.patch('time.time', return_value=__import__('time').time() + dashboard.PIN_HOURS * 3600 + 5):
            self.assertEqual(self.c.get('/api/claims', environ_base=OUTSIDE).status_code, 401)

    def test_next_never_leaves_the_dashboard(self):
        for bad in ('https://evil.example', '//evil.example', 'javascript:alert(1)'):
            r = self.c.post('/pin', data={'pin': dashboard.DASHBOARD_PIN, 'next': bad}, environ_base=OUTSIDE)
            self.assertEqual(r.headers['Location'], '/', bad)
            dashboard._pin_tries.clear()

    def test_logout_and_the_open_page_goes_back_to_the_door(self):
        self.c.post('/pin', data={'pin': dashboard.DASHBOARD_PIN}, environ_base=OUTSIDE)
        self.c.get('/logout', environ_base=OUTSIDE)
        self.assertEqual(self.c.get('/api/claims', environ_base=OUTSIDE).status_code, 401)
        self.assertIn("if (res.status === 401) { location.href = '/pin'; return; }", dashboard.DASHBOARD_HTML)


if __name__ == '__main__':
    unittest.main()
