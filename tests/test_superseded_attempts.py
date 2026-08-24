"""Failed attempts that a later submission resolved.

The interstitial outage produced 30 failures, every one of which went out
cleanly minutes later. Showing both rows reads as though those claims have a
problem. The rows are kept — an append-only log that quietly drops evidence
stops being an audit log — but hidden from the default view and counted out
loud, so nothing disappears silently.
"""
import os
import unittest

os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')
os.environ.setdefault('AWS_REGION', 'us-west-2')
os.environ.setdefault('SQS_QUEUE_URL', 'https://sqs.test/q')

import dashboard  # noqa: E402


def row(claim_id, at, outcome, **kw):
    r = {
        'submission_id': f'{claim_id}-{at}', 'claim_id': claim_id,
        'blueshield_claim_number': '', 'patient_name': 'Doe, Jane',
        'dos': '07/23/2026', 'subscriber_id': 'XED111111111',
        'submitted_at_utc': at, 'submitted_date_pt': at[:10],
        'submitted_time_pt': at[11:19],
        'method': 'SympliSend upload (Blue Shield provider portal)',
        'submission_form_type': '', 'claim_submission_type': 'First Time Submission',
        'documents': [], 'document_count': 0, 'outcome': outcome,
        'fln': '', 'error': '', 'notes': '', 'record_origin': 'agent',
    }
    r.update(kw)
    return r


ROWS = [
    # failed, then went out fine 25 minutes later — the interstitial case
    row('7300', '2026-08-24T15:24:02Z', 'failed'),
    row('7300', '2026-08-24T15:49:13Z', 'submitted'),
    # failed and never retried — a live problem
    row('8000', '2026-08-24T15:30:00Z', 'failed'),
    # blocked, later submitted
    row('8100', '2026-08-24T14:00:00Z', 'blocked'),
    row('8100', '2026-08-24T16:00:00Z', 'submitted'),
    # submitted, and then a LATER failure — not superseded, still a problem
    row('8200', '2026-08-24T10:00:00Z', 'submitted'),
    row('8200', '2026-08-24T17:00:00Z', 'failed'),
]


class FakeTable:
    def scan(self, **kw):
        return {'Items': [dict(r) for r in ROWS]}


class FakeDDB:
    def Table(self, name):
        return FakeTable()


class Case(unittest.TestCase):
    def setUp(self):
        dashboard.dynamodb = FakeDDB()
        dashboard.app.config['TESTING'] = True
        self.client = dashboard.app.test_client()

    def get(self, path='/api/audit-log'):
        return self.client.get(path).get_json()

    def by_id(self, path='/api/audit-log'):
        return {r['submission_id']: r for r in self.get(path)['rows']}


class MarkingIsCorrect(Case):
    def test_a_failure_a_later_success_resolved_is_superseded(self):
        rows = self.by_id('/api/audit-log?superseded=1')
        self.assertTrue(rows['7300-2026-08-24T15:24:02Z']['superseded'])

    def test_a_blocked_attempt_counts_too(self):
        rows = self.by_id('/api/audit-log?superseded=1')
        self.assertTrue(rows['8100-2026-08-24T14:00:00Z']['superseded'])

    def test_an_unretried_failure_is_not_superseded(self):
        rows = self.by_id('/api/audit-log?superseded=1')
        self.assertFalse(rows['8000-2026-08-24T15:30:00Z']['superseded'])

    def test_a_failure_AFTER_the_success_is_not_superseded(self):
        # Chronology matters: this one is a new problem, not an old resolved
        # one, and hiding it would be the dangerous kind of tidy.
        rows = self.by_id('/api/audit-log?superseded=1')
        self.assertFalse(rows['8200-2026-08-24T17:00:00Z']['superseded'])

    def test_successes_are_never_superseded(self):
        for r in self.get('/api/audit-log?superseded=1')['rows']:
            if r['outcome'] == 'submitted':
                self.assertFalse(r['superseded'], r['submission_id'])


class HiddenByDefault(Case):
    def test_the_default_view_leaves_them_out(self):
        ids = set(self.by_id())
        self.assertNotIn('7300-2026-08-24T15:24:02Z', ids)
        self.assertNotIn('8100-2026-08-24T14:00:00Z', ids)

    def test_the_default_view_keeps_real_problems(self):
        ids = set(self.by_id())
        self.assertIn('8000-2026-08-24T15:30:00Z', ids)
        self.assertIn('8200-2026-08-24T17:00:00Z', ids)

    def test_successes_are_untouched(self):
        self.assertEqual(
            sum(1 for r in self.get()['rows'] if r['outcome'] == 'submitted'), 3)

    def test_they_come_back_when_asked_for(self):
        self.assertEqual(self.get('/api/audit-log?superseded=1')['count'], len(ROWS))
        self.assertEqual(self.get()['count'], len(ROWS) - 2)


class TheCsvFollowsTheView(Case):
    def test_export_omits_them_by_default(self):
        body = self.client.get('/api/audit-log.csv').get_data(as_text=True)
        self.assertEqual(len(body.strip().split('\n')), len(ROWS) - 2 + 1)

    def test_export_includes_them_on_request(self):
        body = self.client.get('/api/audit-log.csv?superseded=1').get_data(as_text=True)
        self.assertEqual(len(body.strip().split('\n')), len(ROWS) + 1)


class NothingIsHiddenSilently(unittest.TestCase):
    def test_the_page_says_how_many_it_left_out(self):
        html = dashboard.DASHBOARD_HTML if False else dashboard.AUDIT_HTML
        self.assertIn('retried attempt', html)
        self.assertIn('toggleSuperseded', html)

    def test_the_page_loads_every_row_so_the_toggle_is_instant(self):
        self.assertIn("fetch('/api/audit-log?superseded=1')", dashboard.AUDIT_HTML)

    def test_clear_resets_the_toggle(self):
        self.assertIn('showSuperseded = false', dashboard.AUDIT_HTML)


if __name__ == '__main__':
    unittest.main()
