"""The audit log as staff and payers actually consume it.

Covers the read path: what the API returns, how the filters behave, what lands
in the CSV that gets sent to a payer, and that the page renders.
"""
import os
import unittest

os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')
os.environ.setdefault('AWS_REGION', 'us-west-2')
os.environ.setdefault('SQS_QUEUE_URL', 'https://sqs.test/q')

import dashboard  # noqa: E402  (import needs the env above)


ROWS = [
    {   # genuine first-time submission, all three documents
        'submission_id': 'a1', 'claim_id': '6967', 'blueshield_claim_number': '',
        'patient_name': 'Revers, Felicia', 'dos': '07/23/2026',
        'subscriber_id': 'OCO903699558',
        'submitted_at_utc': '2026-07-31T16:24:47Z',
        'submitted_date_pt': '2026-07-31', 'submitted_time_pt': '09:24:47',
        'method': 'SympliSend upload (Blue Shield provider portal)',
        'submission_form_type': 'Provider First Submission Claim',
        'claim_submission_type': 'First Time Submission',
        'documents': [{'document': 'hcfa'}, {'document': 'prog_notes'},
                      {'document': 'encounter'}],
        'document_count': 3, 'outcome': 'submitted', 'fln': '',
        'error': '', 'notes': '', 'record_origin': 'agent',
    },
    {   # replacement claim transmitted as a new one
        'submission_id': 'a2', 'claim_id': '4774',
        'blueshield_claim_number': '262850914200',
        'patient_name': 'Drannikov, Eduard', 'dos': '04/23/2026',
        'subscriber_id': 'XEA914555915',
        'submitted_at_utc': '2026-06-28T19:36:03Z',
        'submitted_date_pt': '2026-06-28', 'submitted_time_pt': '12:36:03',
        'method': 'SympliSend upload (Blue Shield provider portal)',
        'submission_form_type': 'Provider First Submission Claim',
        'claim_submission_type': 'Resubmission',
        'documents': [{'document': 'hcfa'}, {'document': 'prog_notes'}],
        'document_count': 2, 'outcome': 'submitted', 'fln': 'FLN-2026-0628-4471',
        'error': '', 'notes': '', 'record_origin': 'reconstructed-from-claim-record',
    },
    {   # blocked before anything was uploaded
        'submission_id': 'a3', 'claim_id': '5555', 'blueshield_claim_number': '',
        'patient_name': 'Test, Blocked', 'dos': '05/01/2026',
        'subscriber_id': 'XED111111111',
        'submitted_at_utc': '2026-08-01T10:00:00Z',
        'submitted_date_pt': '2026-08-01', 'submitted_time_pt': '03:00:00',
        'method': 'SympliSend upload (Blue Shield provider portal)',
        'submission_form_type': '', 'claim_submission_type': 'Resubmission',
        'documents': [], 'document_count': 0, 'outcome': 'blocked', 'fln': '',
        'error': 'only 1/3 documents available',
        'notes': 'nothing was uploaded to the payer', 'record_origin': 'agent',
    },
    {   # pre-audit stub row with no timestamp — must be skipped, not rendered blank
        'submission_id': 'legacy', 'claim_id': '9999',
        'fln_number': 'FLN-X', 'status': 'Submitted',
    },
]


class FakeTable:
    """Returns rows across two pages, so pagination is exercised."""

    def scan(self, **kw):
        if 'ExclusiveStartKey' in kw:
            return {'Items': [dict(r) for r in ROWS[2:]]}
        return {'Items': [dict(r) for r in ROWS[:2]],
                'LastEvaluatedKey': {'submission_id': 'a2'}}


class FakeDDB:
    def Table(self, name):
        assert name == 'helixona-submissions', name
        return FakeTable()


class AuditApiCase(unittest.TestCase):
    def setUp(self):
        dashboard.dynamodb = FakeDDB()
        dashboard.app.config['TESTING'] = True
        self.client = dashboard.app.test_client()

    def get(self, path):
        return self.client.get(path).get_json()


class ReadingTheLog(AuditApiCase):
    def test_returns_every_page_of_results(self):
        # A single unpaginated scan would silently drop rows past DynamoDB's
        # 1MB page — an audit log that quietly omits evidence is worse than none.
        self.assertEqual(self.get('/api/audit-log')['count'], 3)

    def test_skips_rows_that_predate_the_log(self):
        ids = [r['claim_id'] for r in self.get('/api/audit-log')['rows']]
        self.assertNotIn('9999', ids)

    def test_newest_first(self):
        ids = [r['claim_id'] for r in self.get('/api/audit-log')['rows']]
        self.assertEqual(ids, ['5555', '6967', '4774'])

    def test_document_names_are_human_readable(self):
        rows = {r['claim_id']: r for r in self.get('/api/audit-log')['rows']}
        self.assertEqual(rows['6967']['documents_label'],
                         'HCFA-1500, IV Note, Progress Note')
        self.assertEqual(rows['5555']['documents_label'], '')


class LinkageDetection(AuditApiCase):
    """The defect behind the denials, surfaced rather than inferred."""

    def rows(self):
        return {r['claim_id']: r for r in self.get('/api/audit-log')['rows']}

    def test_replacement_sent_as_new_claim_is_flagged(self):
        self.assertTrue(self.rows()['4774']['linkage_risk'])

    def test_genuine_first_submission_is_not_flagged(self):
        self.assertFalse(self.rows()['6967']['linkage_risk'])

    def test_flag_reads_as_plain_language(self):
        self.assertTrue(self.rows()['4774']['linkage_label'].startswith('No'))
        self.assertEqual(self.rows()['6967']['linkage_label'], 'Yes')


class Searching(AuditApiCase):
    """One box over everything a payer might quote back at you."""

    def test_finds_by_patient_surname(self):
        self.assertEqual(self.get('/api/audit-log?q=revers')['count'], 1)

    def test_is_case_insensitive(self):
        self.assertEqual(self.get('/api/audit-log?q=REVERS')['count'], 1)

    def test_finds_by_ecw_claim_number(self):
        self.assertEqual(self.get('/api/audit-log?q=6967')['count'], 1)

    def test_finds_by_blue_shield_claim_number(self):
        self.assertEqual(self.get('/api/audit-log?q=262850914200')['count'], 1)

    def test_finds_by_date_of_service(self):
        # How payers cite a claim — and the field the first version could not search.
        self.assertEqual(self.get('/api/audit-log?q=07/23/2026')['count'], 1)

    def test_finds_by_subscriber_id(self):
        self.assertEqual(self.get('/api/audit-log?q=OCO903699558')['count'], 1)

    def test_finds_by_fln(self):
        self.assertEqual(self.get('/api/audit-log?q=FLN-2026-0628-4471')['count'], 1)

    def test_no_match_returns_empty_not_everything(self):
        self.assertEqual(self.get('/api/audit-log?q=nobodyhere')['count'], 0)


class Filtering(AuditApiCase):
    def test_by_outcome(self):
        self.assertEqual(self.get('/api/audit-log?outcome=blocked')['count'], 1)
        self.assertEqual(self.get('/api/audit-log?outcome=submitted')['count'], 2)

    def test_by_date_range(self):
        self.assertEqual(self.get('/api/audit-log?from=2026-07-01')['count'], 2)
        self.assertEqual(self.get('/api/audit-log?to=2026-06-30')['count'], 1)
        self.assertEqual(
            self.get('/api/audit-log?from=2026-07-01&to=2026-07-31')['count'], 1)

    def test_unlinked_flag(self):
        self.assertEqual(self.get('/api/audit-log?flag=unlinked')['count'], 1)

    def test_missing_acknowledgement_flag(self):
        self.assertEqual(self.get('/api/audit-log?flag=no-fln')['count'], 2)

    def test_filters_combine(self):
        self.assertEqual(
            self.get('/api/audit-log?q=drannikov&flag=unlinked')['count'], 1)
        self.assertEqual(
            self.get('/api/audit-log?q=revers&flag=unlinked')['count'], 0)


class CsvExport(AuditApiCase):
    """The artefact handed to a payer."""

    def csv(self, path='/api/audit-log.csv'):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200)
        return r, r.get_data(as_text=True)

    def test_downloads_as_a_file(self):
        r, _ = self.csv()
        self.assertIn('attachment', r.headers.get('Content-Disposition', ''))
        self.assertIn('csv', r.mimetype)

    def test_one_line_per_submission_plus_a_header(self):
        _, body = self.csv()
        self.assertEqual(len(body.strip().split('\n')), 4)

    def test_carries_the_evidence_a_payer_needs(self):
        _, body = self.csv()
        for expected in ('Revers, Felicia', '07/23/2026',
                         'HCFA-1500, IV Note, Progress Note',
                         'SympliSend upload (Blue Shield provider portal)'):
            self.assertIn(expected, body)

    def test_spells_out_the_linkage_problem(self):
        r, body = self.csv()
        self.assertIn('Attached to prior claim?', body.split('\n')[0])
        self.assertIn('No — sent as a new claim', body)

    def test_honours_the_current_search(self):
        _, body = self.csv('/api/audit-log.csv?q=revers')
        self.assertEqual(len(body.strip().split('\n')), 2)


class ThePage(AuditApiCase):
    def test_renders(self):
        r = self.client.get('/audit')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Submission Audit Log', r.get_data(as_text=True))

    def test_leaves_no_unsubstituted_placeholder(self):
        self.assertNotIn('__COLUMNS__', self.client.get('/audit').get_data(as_text=True))

    def test_matches_the_dashboard_theme(self):
        html = self.client.get('/audit').get_data(as_text=True)
        self.assertIn('--accent:#CDB486', html)
        self.assertIn('Playfair Display', html)

    def test_offers_a_single_search_box(self):
        html = self.client.get('/audit').get_data(as_text=True)
        self.assertEqual(html.count('id="q"'), 1)

    def test_surfaces_the_linkage_count(self):
        self.assertIn('Not attached to prior claim',
                      self.client.get('/audit').get_data(as_text=True))


class ClaimsApiReturnsEverything(unittest.TestCase):
    """A truncated claims list understates the backlog in the reassuring direction.

    The dashboard was reporting 690 of 1198 claims and calling the queue "100%
    complete" because a bare scan stops at DynamoDB's 1MB page.
    """

    PAGE_1 = [{'claim_id': str(i), 'symplisend_submitted': 1} for i in range(600)]
    PAGE_2 = [{'claim_id': str(i), 'symplisend_submitted': 0} for i in range(600, 1198)]

    def setUp(self):
        outer = self

        class PagedClaims:
            def scan(self, **kw):
                if 'ExclusiveStartKey' in kw:
                    return {'Items': [dict(c) for c in outer.PAGE_2]}
                return {'Items': [dict(c) for c in outer.PAGE_1],
                        'LastEvaluatedKey': {'claim_id': '599'}}

        class EmptyTasks:
            def scan(self, **kw):
                return {'Items': []}

        class DDB:
            def Table(self, name):
                return PagedClaims() if name == 'helixona-claims' else EmptyTasks()

        dashboard.dynamodb = DDB()
        dashboard.app.config['TESTING'] = True
        self.client = dashboard.app.test_client()

    def test_every_page_is_returned(self):
        data = self.client.get('/api/claims').get_json()
        self.assertEqual(len(data['claims']), 1198)

    def test_the_second_page_is_not_the_one_dropped(self):
        data = self.client.get('/api/claims').get_json()
        ids = {c['claim_id'] for c in data['claims']}
        self.assertIn('1197', ids)


class ApiFailsSoftly(unittest.TestCase):
    def test_a_dynamodb_error_returns_empty_rows_not_a_500(self):
        class Broken:
            def Table(self, name):
                raise RuntimeError('table gone')

        dashboard.dynamodb = Broken()
        dashboard.app.config['TESTING'] = True
        data = dashboard.app.test_client().get('/api/audit-log').get_json()
        self.assertEqual(data['rows'], [])
        self.assertIn('table gone', data['error'])


if __name__ == '__main__':
    unittest.main()
