"""The audit log's promises.

The log exists because a payer said documents never arrived and there was no
dated, itemised answer. These tests hold it to that job: every attempt leaves a
row, rows are never overwritten, and an audit failure never breaks a submission
that already reached the payer.
"""
import os
import tempfile
import unittest

from src.audit.submission_log import (
    METHOD_SYMPLISEND,
    OUTCOME_BLOCKED,
    OUTCOME_FAILED,
    OUTCOME_SUBMITTED,
    describe_documents,
    record_submission,
)


class FakeTable:
    def __init__(self, explode=False):
        self.items = []
        self.explode = explode

    def put_item(self, Item):
        if self.explode:
            raise RuntimeError('DynamoDB is having a day')
        self.items.append(Item)


class FakeAWS:
    def __init__(self, explode=False):
        self.table = FakeTable(explode)
        self.asked_for = []

    @property
    def dynamodb(self):
        return self

    def Table(self, name):
        self.asked_for.append(name)
        return self.table


CLAIM = {
    'claim_id': '6967',
    'patient_name': 'Revers, Felicia',
    'dos': '07/23/2026',
    'subscriber_id': 'OCO903699558',
    'original_ref_no': '265283650300',
    'submission_type': 'Resubmission',
}


class RowCapturesWhatAPayerAsksFor(unittest.TestCase):
    """Date, time, method, claim, DOS, patient, documents."""

    def setUp(self):
        self.aws = FakeAWS()
        record_submission(
            self.aws, CLAIM,
            outcome=OUTCOME_SUBMITTED,
            documents=[{'document': 'hcfa'}, {'document': 'prog_notes'}],
            submission_form_type='Provider First Submission Claim',
        )
        self.row = self.aws.table.items[0]

    def test_writes_to_the_submissions_table(self):
        self.assertEqual(self.aws.asked_for, ['helixona-submissions'])

    def test_carries_the_identifying_fields(self):
        self.assertEqual(self.row['claim_id'], '6967')
        self.assertEqual(self.row['patient_name'], 'Revers, Felicia')
        self.assertEqual(self.row['dos'], '07/23/2026')
        self.assertEqual(self.row['subscriber_id'], 'OCO903699558')

    def test_carries_the_prior_claim_number(self):
        self.assertEqual(self.row['blueshield_claim_number'], '265283650300')

    def test_records_date_and_time_in_utc_and_local(self):
        self.assertRegex(self.row['submitted_at_utc'],
                         r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        self.assertRegex(self.row['submitted_date_pt'], r'^\d{4}-\d{2}-\d{2}$')
        self.assertRegex(self.row['submitted_time_pt'], r'^\d{2}:\d{2}:\d{2}$')

    def test_records_the_method(self):
        self.assertEqual(self.row['method'], METHOD_SYMPLISEND)

    def test_records_documents_and_their_count(self):
        self.assertEqual(self.row['document_count'], 2)
        self.assertEqual([d['document'] for d in self.row['documents']],
                         ['hcfa', 'prog_notes'])

    def test_records_the_form_type_actually_selected(self):
        # As observed in the payer's UI, not the claim's intent — otherwise the
        # log cannot reveal a mismatch between the two.
        self.assertEqual(self.row['submission_form_type'],
                         'Provider First Submission Claim')
        self.assertEqual(self.row['claim_submission_type'], 'Resubmission')


class EveryOutcomeIsRecorded(unittest.TestCase):
    """Failures are the ones you most need evidence of."""

    def test_submitted_failed_and_blocked_all_write_a_row(self):
        for outcome in (OUTCOME_SUBMITTED, OUTCOME_FAILED, OUTCOME_BLOCKED):
            aws = FakeAWS()
            record_submission(aws, CLAIM, outcome=outcome, documents=[])
            self.assertEqual(len(aws.table.items), 1, outcome)
            self.assertEqual(aws.table.items[0]['outcome'], outcome)

    def test_a_failure_keeps_its_reason(self):
        aws = FakeAWS()
        record_submission(aws, CLAIM, outcome=OUTCOME_FAILED, documents=[],
                          error='Submit button never fired — packet not sent')
        self.assertIn('never fired', aws.table.items[0]['error'])

    def test_an_attempt_with_no_documents_still_records(self):
        aws = FakeAWS()
        record_submission(aws, CLAIM, outcome=OUTCOME_BLOCKED, documents=[])
        self.assertEqual(aws.table.items[0]['document_count'], 0)


class RowsAreAppendOnly(unittest.TestCase):
    def test_two_submissions_for_one_claim_keep_both(self):
        aws = FakeAWS()
        record_submission(aws, CLAIM, outcome=OUTCOME_FAILED, documents=[])
        record_submission(aws, CLAIM, outcome=OUTCOME_SUBMITTED, documents=[])
        self.assertEqual(len(aws.table.items), 2)
        self.assertEqual([r['outcome'] for r in aws.table.items],
                         ['failed', 'submitted'])

    def test_each_attempt_gets_its_own_id(self):
        aws = FakeAWS()
        for _ in range(3):
            record_submission(aws, CLAIM, outcome=OUTCOME_SUBMITTED, documents=[])
        ids = {r['submission_id'] for r in aws.table.items}
        self.assertEqual(len(ids), 3)


class AuditFailureNeverBreaksASubmission(unittest.TestCase):
    """A packet that reached the payer must not be retried because logging broke."""

    def test_a_dynamodb_error_is_swallowed(self):
        aws = FakeAWS(explode=True)
        try:
            result = record_submission(aws, CLAIM, outcome=OUTCOME_SUBMITTED,
                                       documents=[])
        except Exception as e:
            self.fail(f'record_submission raised {e!r}')
        self.assertEqual(result, '')

    def test_success_returns_the_submission_id(self):
        aws = FakeAWS()
        sid = record_submission(aws, CLAIM, outcome=OUTCOME_SUBMITTED, documents=[])
        self.assertTrue(sid)
        self.assertEqual(sid, aws.table.items[0]['submission_id'])


class DocumentDescriptions(unittest.TestCase):
    def test_hashes_and_sizes_a_real_file(self):
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as fh:
            fh.write(b'%PDF-1.4 pretend claim form')
            path = fh.name
        try:
            [doc] = describe_documents([('hcfa', 's3://b/k.pdf', path)])
            self.assertEqual(doc['document'], 'hcfa')
            self.assertEqual(doc['bytes'], os.path.getsize(path))
            self.assertEqual(len(doc['sha256']), 64)
            self.assertEqual(doc['s3_path'], 's3://b/k.pdf')
        finally:
            os.unlink(path)

    def test_identical_content_hashes_identically(self):
        paths = []
        for _ in range(2):
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as fh:
                fh.write(b'same bytes')
                paths.append(fh.name)
        try:
            hashes = {describe_documents([('d', '', p)])[0]['sha256'] for p in paths}
            self.assertEqual(len(hashes), 1)
        finally:
            for p in paths:
                os.unlink(p)

    def test_a_missing_file_still_produces_a_row(self):
        # Dropping a document silently would be worse than recording it as
        # unhashed — the packet listing must stay complete.
        [doc] = describe_documents([('hcfa', 's3://b/k.pdf', '/nope/missing.pdf')])
        self.assertEqual(doc['document'], 'hcfa')
        self.assertIn('note', doc)
        self.assertNotIn('sha256', doc)

    def test_empty_input_gives_empty_output(self):
        self.assertEqual(describe_documents([]), [])


if __name__ == '__main__':
    unittest.main()
