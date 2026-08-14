"""
Append-only audit log of every documentation submission.

Why this exists
───────────────
When a payer says "we never received the medical records", the only useful
answer is a dated, itemised record of what left this system and when. Until
now the agent kept that state on the *claim* record (`symplisend_submitted`,
`symplisend_submitted_at`, ...), which has two fatal properties for an audit
trail:

  1. It is overwritten. A second submission for the same claim erases any
     trace of the first one.
  2. It records only that *something* was sent — never which documents, and
     never the failed attempts.

Every row written here is immutable: one row per submission ATTEMPT, keyed by
a fresh uuid, written whether the attempt succeeded, failed or was blocked.
Nothing in this module ever updates or deletes an existing row.

Row shape (helixona-submissions)
────────────────────────────────
  submission_id            uuid4 — table HASH key
  claim_id                 ECW claim # / patient account # — GSI 'claim-index'
  blueshield_claim_number  prior BS claim # this packet belongs to (if any)
  patient_name             as billed
  dos                      date of service
  submitted_at_utc         ISO-8601 UTC, e.g. 2026-08-14T16:24:47Z
  submitted_date_pt        date in America/Los_Angeles (the practice's clock)
  submitted_time_pt        time in America/Los_Angeles
  method                   channel the packet went out through
  submission_form_type     the option ACTUALLY selected in the payer's form
  documents                [{document, filename, s3_path, bytes, sha256}, ...]
  document_count           len(documents)
  outcome                  submitted | failed | blocked
  fln                      payer acknowledgement number, '' when not captured
  ...plus subscriber_id, error, notes, record_origin.

`submission_form_type` deliberately records what was selected in the payer UI
rather than what the claim record intended. An audit log that echoes intent
cannot reveal a mismatch between the two — which is exactly the failure this
log is meant to catch.
"""
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

from src.utils.logger import get_logger

logger = get_logger(__name__)

TABLE_NAME = 'helixona-submissions'

# Channel constants — the "method of submission" recorded on every row.
METHOD_SYMPLISEND = 'SympliSend upload (Blue Shield provider portal)'

OUTCOME_SUBMITTED = 'submitted'
OUTCOME_FAILED = 'failed'
OUTCOME_BLOCKED = 'blocked'

# The practice runs on California time; auditors read dates in local terms.
# Fixed -07:00 keeps this dependency-free — the UTC field is authoritative.
_PT_OFFSET = timedelta(hours=-7)


def _sha256(path: str) -> str:
    """Content hash of a file, so a document can be proven identical later."""
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def describe_documents(docs) -> list:
    """Build the `documents` list for a row.

    `docs` is an iterable of (label, s3_path, local_path). The local file is
    the artefact actually handed to the payer's uploader, so its size and hash
    are what get recorded. A missing or unreadable local file still produces a
    row — an audit log that silently drops documents is worse than useless.
    """
    described = []
    for label, s3_path, local_path in docs:
        entry = {'document': label, 's3_path': s3_path or ''}
        try:
            if local_path and os.path.exists(local_path):
                entry['filename'] = os.path.basename(local_path)
                entry['bytes'] = os.path.getsize(local_path)
                entry['sha256'] = _sha256(local_path)
            else:
                entry['filename'] = os.path.basename(local_path) if local_path else ''
                entry['note'] = 'local file not present at audit time'
        except Exception as e:  # hashing must never break a submission
            entry['note'] = f'could not hash: {e}'
        described.append(entry)
    return described


def record_submission(
    aws_client,
    claim: dict,
    *,
    outcome: str,
    documents: list,
    method: str = METHOD_SYMPLISEND,
    submission_form_type: str = '',
    fln: str = '',
    error: str = '',
    notes: str = '',
    record_origin: str = 'agent',
) -> str:
    """Append one immutable row describing a submission attempt.

    Returns the submission_id, or '' if the row could not be written.

    This never raises. A submission that reached the payer must not be retried
    or marked failed because the audit write had a bad day — the exception is
    logged loudly instead, and the caller carries on.
    """
    now = datetime.now(timezone.utc)
    local = now + _PT_OFFSET
    submission_id = str(uuid.uuid4())

    row = {
        'submission_id': submission_id,
        'claim_id': str(claim.get('claim_id', '')),
        'blueshield_claim_number': str(claim.get('original_ref_no', '') or ''),
        'patient_name': str(claim.get('patient_name', '') or ''),
        'dos': str(claim.get('dos') or claim.get('service_date') or ''),
        'subscriber_id': str(claim.get('subscriber_id', '') or ''),

        'submitted_at_utc': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'submitted_date_pt': local.strftime('%Y-%m-%d'),
        'submitted_time_pt': local.strftime('%H:%M:%S'),

        'method': method,
        'submission_form_type': submission_form_type or '',
        'claim_submission_type': str(claim.get('submission_type', '') or ''),

        'documents': documents or [],
        'document_count': len(documents or []),

        'outcome': outcome,
        'fln': fln or '',
        'error': error or '',
        'notes': notes or '',
        'record_origin': record_origin,
    }

    try:
        table = aws_client.dynamodb.Table(TABLE_NAME)
        table.put_item(Item=row)
        doc_names = ', '.join(d.get('document', '?') for d in (documents or [])) or 'none'
        logger.info(
            f"AUDIT {outcome}: claim {row['claim_id']} | {row['patient_name']} | "
            f"DOS {row['dos']} | {row['submitted_at_utc']} | docs: {doc_names} | "
            f"id {submission_id}"
        )
        return submission_id
    except Exception as e:
        # Loud, but non-fatal — see docstring.
        logger.error(
            f"❌ AUDIT WRITE FAILED for claim {row['claim_id']} "
            f"({outcome}, {row['submitted_at_utc']}): {e}"
        )
        return ''
