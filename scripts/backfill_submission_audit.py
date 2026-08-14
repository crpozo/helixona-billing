"""
Seed the submission audit log from claim records written before the log existed.

Every claim carrying `symplisend_submitted` holds a usable, if thin, account of
one submission: when it went out, and which document paths were on file at the
time. This script turns each of those into an audit row so the log covers the
full history rather than starting at today.

Reconstructed rows are labelled `record_origin='reconstructed-from-claim-record'`
and are honest about their limits:

  * The document list is INFERRED from the S3 paths on the claim as they stand
    today. If a document was re-captured after submission, the path may point
    at a newer file than the one actually sent. Sizes/hashes are therefore not
    recorded — only the paths.
  * `submission_form_type` is recorded as the value the agent hardcoded for
    every submission in this period, not as an observed selection.
  * No FLN was captured for any submission in this period.

Rows are keyed by a deterministic uuid5, so re-running rewrites the same row
instead of piling up duplicates.

Usage:
    python3 scripts/backfill_submission_audit.py            # dry run
    python3 scripts/backfill_submission_audit.py --apply    # write rows
"""
import os
import sys
import uuid
from datetime import datetime, timedelta

import boto3
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

load_dotenv()

# Namespace for deterministic reconstructed-row ids — fixed so re-runs are idempotent.
RECON_NS = uuid.UUID('6f9619ff-8b86-d011-b42d-00c04fc964ff')

# Every submission in the pre-audit period went out through this channel, and
# the agent selected this form type unconditionally (src/main.py, Step 3).
METHOD = 'SympliSend upload (Blue Shield provider portal)'
FORM_TYPE = 'Provider First Submission Claim'

DOC_FIELDS = [
    ('hcfa', 'hcfa_s3_path'),
    ('prog_notes', 'prog_notes_s3_path'),
    ('encounter', 'encounter_file_s3_path'),
]

_PT_OFFSET = timedelta(hours=-7)


def parse_submitted_at(raw):
    """Claim records store '2026-07-06 16:23:09 UTC'."""
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).replace(' UTC', ''), '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def main():
    apply = '--apply' in sys.argv

    session = boto3.Session(
        aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
        region_name=os.environ.get('AWS_REGION', 'us-west-2'),
    )
    dynamodb = session.resource('dynamodb')
    claims_table = dynamodb.Table('helixona-claims')
    subs_table = dynamodb.Table('helixona-submissions')

    items, kwargs = [], {}
    while True:
        resp = claims_table.scan(**kwargs)
        items.extend(resp.get('Items', []))
        if 'LastEvaluatedKey' not in resp:
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

    submitted = [c for c in items if c.get('symplisend_submitted')]
    print(f"Claims scanned:          {len(items)}")
    print(f"Marked as submitted:     {len(submitted)}")

    written = skipped = 0
    for claim in submitted:
        claim_id = str(claim.get('claim_id', ''))
        ts = parse_submitted_at(claim.get('symplisend_submitted_at'))
        if not ts:
            print(f"  skip {claim_id}: unparseable submitted_at "
                  f"{claim.get('symplisend_submitted_at')!r}")
            skipped += 1
            continue

        local = ts + _PT_OFFSET
        documents = [
            {'document': label, 's3_path': str(claim.get(field)),
             'note': 'path as recorded on the claim; not hashed at send time'}
            for label, field in DOC_FIELDS if claim.get(field)
        ]

        fln = str(claim.get('symplisend_fln', '') or '')
        row = {
            'submission_id': str(uuid.uuid5(RECON_NS, f'{claim_id}|{ts.isoformat()}')),
            'claim_id': claim_id,
            'blueshield_claim_number': str(claim.get('original_ref_no', '') or ''),
            'patient_name': str(claim.get('patient_name', '') or ''),
            'dos': str(claim.get('dos') or claim.get('service_date') or ''),
            'subscriber_id': str(claim.get('subscriber_id', '') or ''),

            'submitted_at_utc': ts.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'submitted_date_pt': local.strftime('%Y-%m-%d'),
            'submitted_time_pt': local.strftime('%H:%M:%S'),

            'method': METHOD,
            'submission_form_type': FORM_TYPE,
            'claim_submission_type': str(claim.get('submission_type', '') or ''),

            'documents': documents,
            'document_count': len(documents),

            'outcome': 'submitted',
            # 'PENDING' was the placeholder written when no FLN came back.
            'fln': '' if fln in ('', 'PENDING') else fln,
            'error': '',
            'notes': 'no FLN acknowledgement was captured for this submission',
            'record_origin': 'reconstructed-from-claim-record',
        }

        if apply:
            subs_table.put_item(Item=row)
        written += 1

    print(f"Rows {'written' if apply else 'that WOULD be written'}: {written}")
    print(f"Skipped:                 {skipped}")
    if not apply:
        print("\nDry run — nothing was written. Re-run with --apply to commit.")


if __name__ == '__main__':
    main()
