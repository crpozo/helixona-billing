"""The Claude credits meter: every API response is priced and tallied, and
only the super admin's cookie opens the dashboard's view of it."""
import os
import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from src import usage

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


class Pricing(unittest.TestCase):
    def test_list_prices_by_longest_prefix(self):
        self.assertEqual(usage.price_for('claude-sonnet-5'), (2.0, 10.0, 2.5, 0.2))
        self.assertEqual(usage.price_for('claude-opus-5-5'), (4.0, 20.0, 5.0, 0.2))
        self.assertEqual(usage.price_for('claude-opus-5'), (5.0, 25.0, 6.25, 0.5))
        self.assertEqual(usage.price_for('claude-fable-5-1'), (10.0, 50.0, 12.5, 0.25))
        # An unknown model is priced as the vision reader's own, never zero.
        self.assertEqual(usage.price_for('claude-new-thing'), usage.price_for('claude-sonnet-5'))

    def test_cost_of_one_response(self):
        self.assertEqual(usage.cost_usd('claude-sonnet-5', {'input_tokens': 1500, 'output_tokens': 300}), Decimal('0.006'))
        u = SimpleNamespace(input_tokens=1000, output_tokens=0, cache_creation_input_tokens=1000, cache_read_input_tokens=1000)
        self.assertEqual(usage.cost_usd('claude-sonnet-5', u), Decimal('0.0047'))


class Tally(unittest.TestCase):
    def test_record_adds_to_today_and_all_time(self):
        table = mock.Mock()
        aws = SimpleNamespace(dynamodb=mock.Mock())
        with mock.patch.object(usage, '_TABLE', {'t': table}):
            resp = SimpleNamespace(model='claude-sonnet-5', usage=SimpleNamespace(input_tokens=2000, output_tokens=100,
                                                                                  cache_creation_input_tokens=0, cache_read_input_tokens=0))
            cost = usage.record(aws, resp, 'checks')
        self.assertEqual(cost, Decimal('0.005'))
        keys = [c.kwargs['Key']['k'] for c in table.update_item.call_args_list]
        self.assertTrue(keys[0].startswith('day:'))
        self.assertEqual(keys[1], '_total')
        expr = table.update_item.call_args_list[0].kwargs['UpdateExpression']
        self.assertIn('ADD calls :one', expr)
        self.assertEqual(table.update_item.call_args_list[0].kwargs['ExpressionAttributeNames'], {'#m': 'cost_claude_sonnet_5'})

    def test_record_never_raises(self):
        table = mock.Mock()
        table.update_item.side_effect = RuntimeError('dynamo down')
        with mock.patch.object(usage, '_TABLE', {'t': table}):
            out = usage.record(SimpleNamespace(dynamodb=None), SimpleNamespace(model='x', usage=SimpleNamespace(input_tokens=1, output_tokens=1)))
        self.assertIsNone(out)

    def test_read_computes_remaining_and_days_left(self):
        items = [{'k': '_total', 'calls': 10, 'cost_usd': Decimal('30'), 'input_tokens': 5, 'output_tokens': 1, 'cost_claude_sonnet_5': Decimal('30')},
                 {'k': 'day:2026-09-30', 'calls': 5, 'cost_usd': Decimal('14'), 'input_tokens': 3, 'output_tokens': 1},
                 {'k': 'day:2026-10-01', 'calls': 5, 'cost_usd': Decimal('14'), 'input_tokens': 2, 'output_tokens': 0},
                 {'k': '_credits', 'balance_usd': Decimal('100'), 'spent_before': Decimal('2'), 'as_of': '2026-09-29T00:00:00+00:00'}]
        with mock.patch('src.aws.clients.scan_all', return_value=items):
            out = usage.read(mock.Mock(), today=date(2026, 10, 1))
        self.assertTrue(out['ok'])
        self.assertEqual(out['today_cost_usd'], 14.0)
        self.assertEqual(out['month_cost_usd'], 14.0)
        self.assertEqual(out['total']['models'], {'claude_sonnet_5': 30.0})
        self.assertEqual(out['credits']['spent_since_usd'], 28.0)
        self.assertEqual(out['credits']['remaining_usd'], 72.0)
        self.assertEqual(out['credits']['per_day_usd'], 2.0)       # $28 over 14 days
        self.assertEqual(out['credits']['days_left'], 36)

    def test_the_vision_reader_tallies_every_call(self):
        rc = _read('src/checks/read_check.py')
        self.assertEqual(rc.count("record_usage(aws_client, response, 'checks')"), rc.count('client.beta.messages.create('))


class AdminGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
        os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')
        os.environ.setdefault('SQS_QUEUE_URL', 'https://sqs.test/q')
        import dashboard
        cls.d = dashboard

    def test_only_the_super_admin_cookie_opens_the_meter(self):
        d = self.d
        with mock.patch.dict(os.environ, {'DASHBOARD_ADMIN_KEY': 'test-key-123'}), \
                mock.patch.object(d.claude_usage, 'read', return_value={'ok': True, 'total': {}, 'days': [], 'credits': None, 'month_cost_usd': 0, 'today_cost_usd': 0}):
            c = d.app.test_client()
            self.assertEqual(c.get('/api/usage').status_code, 403)
            self.assertEqual(c.get('/admin').status_code, 403)
            self.assertEqual(c.get('/admin?key=wrong').status_code, 403)
            r = c.get('/admin?key=test-key-123')
            self.assertEqual(r.status_code, 302)
            self.assertIn('hx_admin=', r.headers.get('Set-Cookie', ''))
            r = c.get('/api/usage')
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()['admin'], 'carlos@mindfultech.ec')
            self.assertEqual(c.post('/api/usage/credits', json={'balance_usd': 'abc'}).status_code, 400)
            c.get('/admin/logout')
            self.assertEqual(c.get('/api/usage').status_code, 403)

    def test_a_forged_cookie_is_not_the_admin(self):
        d = self.d
        with mock.patch.dict(os.environ, {'DASHBOARD_ADMIN_KEY': 'test-key-123'}):
            c = d.app.test_client()
            c.set_cookie('hx_admin', 'carlos@mindfultech.ec')
            self.assertEqual(c.get('/api/usage').status_code, 403)

    def test_the_page_carries_the_pill_and_panel(self):
        h = self.d.DASHBOARD_HTML
        self.assertIn('id="usage-pill"', h)
        self.assertIn('hidden onclick="toggleUsage();return false"', h)
        self.assertIn('id="usage-panel"', h)
        self.assertIn("if (res.status !== 200) return;", h)
        self.assertEqual(self.d.ADMIN_EMAIL, 'carlos@mindfultech.ec')


if __name__ == '__main__':
    unittest.main()
