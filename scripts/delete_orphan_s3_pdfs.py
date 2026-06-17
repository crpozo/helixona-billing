"""
Find S3 objects under encounter_files/ and prog_notes/ that NO LONGER have a
DynamoDB claim pointing at them, and delete them. Run after the
audit_wrong_patient_pdfs.py cleanup so the wrong-patient PDFs are physically
removed (not just dereferenced).

Run on EC2:
  cd /opt/helixona-agent
  venv/bin/python scripts/delete_orphan_s3_pdfs.py            # dry-run
  venv/bin/python scripts/delete_orphan_s3_pdfs.py --apply    # actually delete
"""
import os
import sys
import boto3

DDB_TABLE = 'helixona-claims'
PREFIXES = ['encounter_files/', 'prog_notes/']

# Bucket name comes from the same env the agent uses
BUCKET = os.environ.get('S3_BUCKET_NAME')
if not BUCKET:
    # Fall back to reading from settings
    try:
        sys.path.insert(0, '/opt/helixona-agent')
        from src.config import settings
        BUCKET = settings.s3_bucket_name
    except Exception:
        pass
if not BUCKET:
    print('❌ S3_BUCKET_NAME not set and could not load from settings. Aborting.')
    sys.exit(1)

dynamodb = boto3.resource('dynamodb', region_name='us-west-2')
s3 = boto3.client('s3', region_name='us-west-2')
table = dynamodb.Table(DDB_TABLE)


def collect_referenced_uris():
    """Return set of every s3://... URI currently referenced by any claim."""
    referenced = set()
    resp = table.scan()
    items = resp.get('Items', [])
    while 'LastEvaluatedKey' in resp:
        resp = table.scan(ExclusiveStartKey=resp['LastEvaluatedKey'])
        items.extend(resp.get('Items', []))
    for c in items:
        for f in ('hcfa_s3_path', 'prog_notes_s3_path', 'encounter_file_s3_path'):
            v = c.get(f)
            if v:
                referenced.add(v)
        # encounter_files_s3_paths is a list
        for v in (c.get('encounter_files_s3_paths') or []):
            if v:
                referenced.add(v)
    return referenced, len(items)


def list_s3_objects(prefix):
    """Yield (key, size) for every object under the given prefix."""
    token = None
    while True:
        kwargs = {'Bucket': BUCKET, 'Prefix': prefix}
        if token:
            kwargs['ContinuationToken'] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get('Contents', []):
            yield obj['Key'], obj['Size']
        if not resp.get('IsTruncated'):
            return
        token = resp.get('NextContinuationToken')


def main():
    print(f'⏳ Scanning DynamoDB for referenced S3 URIs...')
    referenced, claim_count = collect_referenced_uris()
    print(f'   Found {len(referenced)} URIs referenced across {claim_count} claims.\n')

    orphans = []
    total_objs = 0
    total_bytes = 0
    for prefix in PREFIXES:
        print(f'📂 Listing s3://{BUCKET}/{prefix}')
        for key, size in list_s3_objects(prefix):
            total_objs += 1
            total_bytes += size
            uri = f's3://{BUCKET}/{key}'
            if uri not in referenced:
                orphans.append((key, size, uri))

    print(f'\n{"=" * 70}')
    print(f'  Total objects under {PREFIXES}: {total_objs} ({total_bytes // 1024} KB)')
    print(f'  Orphans (no DDB reference): {len(orphans)} '
          f'({sum(s for _, s, _ in orphans) // 1024} KB)')
    print(f'{"=" * 70}\n')

    if not orphans:
        print('✅ Nothing to delete.')
        return

    if '--apply' not in sys.argv:
        print('Dry-run only. Re-run with --apply to actually delete. Sample of orphans:')
        for key, size, _ in orphans[:30]:
            print(f'  - {key} ({size // 1024} KB)')
        if len(orphans) > 30:
            print(f'  ... and {len(orphans) - 30} more')
        return

    print(f'🗑️  Deleting {len(orphans)} orphan objects in batches of 1000...')
    # delete_objects accepts up to 1000 keys per call
    batch = []
    deleted = 0
    for key, _, _ in orphans:
        batch.append({'Key': key})
        if len(batch) >= 1000:
            s3.delete_objects(Bucket=BUCKET, Delete={'Objects': batch, 'Quiet': True})
            deleted += len(batch)
            batch = []
    if batch:
        s3.delete_objects(Bucket=BUCKET, Delete={'Objects': batch, 'Quiet': True})
        deleted += len(batch)
    print(f'\n✅ Deleted {deleted} orphan objects from s3://{BUCKET}/')


if __name__ == '__main__':
    main()
