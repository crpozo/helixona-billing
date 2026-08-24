"""The headline counter's own endpoint.

/api/claims returns every field of every claim — ~3MB and 2-3 seconds once the
table passed a thousand rows — and the dashboard polled it every 4 seconds to
move one number. The counter visibly lagged the bot updating it. This endpoint
projects only what the headline needs.
"""
import os
import unittest

os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')
os.environ.setdefault('AWS_REGION', 'us-west-2')
os.environ.setdefault('SQS_QUEUE_URL', 'https://sqs.test/q')

import dashboard  # noqa: E402

SUBMITTED = dashboard.PIPELINE_STAGES['submitted']['states'][0]

PAGE_1 = ([{'state': SUBMITTED, 'submission_type': 'First Time Submission'}] * 3 +
          [{'state': 1, 'submission_type': 'First Time Submission'}] * 2)
PAGE_2 = ([{'state': SUBMITTED, 'submission_type': 'Resubmission'}] * 4 +
          [{'state': 2, 'submission_type': 'Resubmission'}])


class PagedTable:
    def __init__(self):
        self.projections = []

    def scan(self, **kw):
        self.projections.append(kw.get('ProjectionExpression'))
        if 'ExclusiveStartKey' in kw:
            return {'Items': [dict(r) for r in PAGE_2]}
        return {'Items': [dict(r) for r in PAGE_1], 'LastEvaluatedKey': {'claim_id': 'x'}}


class CountsCase(unittest.TestCase):
    def setUp(self):
        self.table = PagedTable()
        outer = self

        class DDB:
            def Table(self, name):
                assert name == 'helixona-claims', name
                return outer.table

        dashboard.dynamodb = DDB()
        dashboard.app.config['TESTING'] = True
        self.data = dashboard.app.test_client().get('/api/claim-counts').get_json()


class ItCountsPerBot(CountsCase):
    def test_submissions_counts_only_first_time_claims(self):
        self.assertEqual(self.data['submissions'], {'submitted': 3, 'total': 5})

    def test_resubmissions_counts_only_resubmissions(self):
        self.assertEqual(self.data['resubmissions'], {'submitted': 4, 'total': 5})

    def test_the_two_tabs_add_up_to_the_whole_table(self):
        self.assertEqual(
            self.data['submissions']['total'] + self.data['resubmissions']['total'],
            self.data['all']['total'])

    def test_it_follows_pagination(self):
        # Page two holds every resubmission; a bare scan would report zero.
        self.assertEqual(self.data['all']['total'], len(PAGE_1) + len(PAGE_2))


class ItStaysCheap(CountsCase):
    def test_it_projects_instead_of_reading_whole_claims(self):
        # The point of the endpoint. Without a projection it is just
        # /api/claims again under a different name.
        for proj in self.table.projections:
            self.assertIsNotNone(proj)
            self.assertIn('submission_type', proj)
            self.assertIn('symplisend_submitted', proj)

    def test_it_aliases_the_reserved_state_keyword(self):
        # 'state' is reserved in DynamoDB; projecting it unaliased errors.
        for proj in self.table.projections:
            self.assertIn('#st', proj)


class TheDashboardUsesIt(unittest.TestCase):
    def test_the_headline_polls_counts_not_the_full_table(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn('/api/claim-counts', html)
        self.assertIn('setInterval(loadCounts, 4000)', html)

    def test_the_full_table_refreshes_less_often(self):
        html = dashboard.DASHBOARD_HTML
        self.assertIn('setInterval(loadData, 15000)', html)
        self.assertNotIn('setInterval(loadData, 4000)', html)


class ItFailsSoftly(unittest.TestCase):
    def test_an_error_does_not_500_the_dashboard(self):
        class Broken:
            def Table(self, name):
                raise RuntimeError('table gone')

        dashboard.dynamodb = Broken()
        dashboard.app.config['TESTING'] = True
        data = dashboard.app.test_client().get('/api/claim-counts').get_json()
        self.assertIn('table gone', data['error'])


if __name__ == '__main__':
    unittest.main()
