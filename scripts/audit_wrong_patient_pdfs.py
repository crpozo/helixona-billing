"""
One-off retro audit: scan every claim with a stored encounter_file_s3_path
and/or prog_notes_s3_path, download the PDF, and verify the patient's surname
is actually in the document text. Anything that fails is treated as a
"wrong patient PDF" (introduced before the fail-safe verification was added)
and its references are cleared from DynamoDB so the next run re-captures.

Run on EC2: cd /opt/helixona-agent && venv/bin/python scripts/audit_wrong_patient_pdfs.py
"""
import os
import re
import sys
import time
import boto3
import pdfplumber

DDB_TABLE = 'helixona-claims'
TMP_DIR = '/tmp/pdf_audit'
os.makedirs(TMP_DIR, exist_ok=True)

dynamodb = boto3.resource('dynamodb', region_name='us-west-2')
s3 = boto3.client('s3', region_name='us-west-2')
table = dynamodb.Table(DDB_TABLE)


def parse_s3_uri(uri):
    m = re.match(r's3://([^/]+)/(.+)', uri or '')
    return (m.group(1), m.group(2)) if m else (None, None)


def extract_pdf_text(local_path):
    try:
        with pdfplumber.open(local_path) as doc:
            return '\n'.join((p.extract_text() or '') for p in doc.pages).upper()
    except Exception as e:
        return f'__EXTRACT_FAIL__: {e}'


def audit_field(claim_id, patient_name, s3_uri, field_label):
    """Returns ('OK'|'BAD'|'SKIP', reason) for one stored PDF."""
    if not s3_uri:
        return ('SKIP', 'no path stored')
    surname = (patient_name or '').split(',')[0].strip().upper()
    if not surname:
        return ('BAD', 'claim has no patient_name on record')
    bucket, key = parse_s3_uri(s3_uri)
    if not bucket:
        return ('BAD', f'unparseable s3 uri: {s3_uri}')
    local = os.path.join(TMP_DIR, f'{claim_id}_{field_label}.pdf')
    try:
        s3.download_file(bucket, key, local)
    except Exception as e:
        return ('BAD', f's3 download failed: {e}')
    text = extract_pdf_text(local)
    try:
        os.remove(local)
    except Exception:
        pass
    if text.startswith('__EXTRACT_FAIL__'):
        return ('BAD', text)
    if not text.strip():
        return ('BAD', 'PDF text empty (image-only / scanned)')
    if surname in text:
        return ('OK', f'verified surname={surname}')
    return ('BAD', f'surname={surname} NOT in PDF (first 80 chars: {text[:80].strip()!r})')


def scan_all_claims():
    items = []
    resp = table.scan()
    items.extend(resp.get('Items', []))
    while 'LastEvaluatedKey' in resp:
        resp = table.scan(ExclusiveStartKey=resp['LastEvaluatedKey'])
        items.extend(resp.get('Items', []))
    return items


def main():
    print(f'⏳ Scanning {DDB_TABLE} for claims with stored PDFs...')
    claims = scan_all_claims()
    print(f'   Found {len(claims)} total claims in DDB.\n')

    enc_bad, iv_bad = [], []
    enc_total, iv_total = 0, 0

    for c in claims:
        cid = c.get('claim_id')
        pname = c.get('patient_name', '')

        enc_uri = c.get('encounter_file_s3_path')
        if enc_uri:
            enc_total += 1
            status, reason = audit_field(cid, pname, enc_uri, 'enc')
            tag = '✅' if status == 'OK' else '⛔'
            print(f'  {tag} ENC  {cid} ({pname}): {status} — {reason}')
            if status == 'BAD':
                enc_bad.append((cid, pname, enc_uri, reason))

        iv_uri = c.get('prog_notes_s3_path')
        if iv_uri:
            iv_total += 1
            status, reason = audit_field(cid, pname, iv_uri, 'iv')
            tag = '✅' if status == 'OK' else '⛔'
            print(f'  {tag} IV   {cid} ({pname}): {status} — {reason}')
            if status == 'BAD':
                iv_bad.append((cid, pname, iv_uri, reason))

    print('\n' + '=' * 70)
    print(f'  Encounter files audited: {enc_total} — bad: {len(enc_bad)}')
    print(f'  IV notes audited:        {iv_total} — bad: {len(iv_bad)}')
    print('=' * 70 + '\n')

    if not enc_bad and not iv_bad:
        print('✅ Nothing to clean.')
        return

    if '--apply' not in sys.argv:
        print('Dry-run only. Re-run with --apply to actually clear DDB fields.')
        print('\nBad encounter files:')
        for cid, pname, uri, reason in enc_bad:
            print(f'  - {cid} ({pname}): {reason}')
        print('\nBad IV notes:')
        for cid, pname, uri, reason in iv_bad:
            print(f'  - {cid} ({pname}): {reason}')
        return

    print(f'🧹 Clearing DDB fields for {len(enc_bad)} bad encounters and {len(iv_bad)} bad IV notes...\n')

    now = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
    for cid, pname, uri, reason in enc_bad:
        table.update_item(
            Key={'claim_id': cid},
            UpdateExpression='SET encounter_file_s3_path = :empty, encounter_files_s3_paths = :emptylist, '
                             'encounter_files_count = :zero, encounter_files_visit_types = :emptylist, '
                             'encounter_revision_needed = :true, encounter_capture_failed = :why, '
                             'encounter_capture_failed_at = :now '
                             'REMOVE encounter_file_captured_at, encounter_date, encounter_pick_strategy',
            ExpressionAttributeValues={
                ':empty': '',
                ':emptylist': [],
                ':zero': 0,
                ':true': True,
                ':why': f'retro_audit: {reason[:200]}',
                ':now': now,
            },
        )
        print(f'  cleared encounter for {cid} ({pname})')

    for cid, pname, uri, reason in iv_bad:
        table.update_item(
            Key={'claim_id': cid},
            UpdateExpression='SET prog_notes_s3_path = :empty, prog_notes_capture_failed = :why, '
                             'prog_notes_capture_failed_at = :now '
                             'REMOVE prog_notes_captured_at, iv_note_rx_start_date, prog_notes_size',
            ExpressionAttributeValues={
                ':empty': '',
                ':why': f'retro_audit: {reason[:200]}',
                ':now': now,
            },
        )
        print(f'  cleared IV note for {cid} ({pname})')

    print('\n✅ Cleanup complete. Affected claims will be re-captured on the next BS Missing Docs run.')


if __name__ == '__main__':
    main()
