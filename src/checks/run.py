"""Remittance — which cheques are posted, which are not, and which we hold.

The operator's procedure (2026-09-16):

    1. SharePoint, Shared Documents / Insurance Checks: every file is the
       image of a cheque. Read its number and amount.
    2. Blue Shield: the cheque's Check/EFT status — Check Cashed or not.
       (What Remittance's capture already stored in helixona-eobs; this run
       does not open the portal.)
    3. Every cheque we hold no copy of is flagged.
    4. eCW, Billing → Payments, the cheque number: on file means posted
       (or entered and left unposted).

Friday, Vignesh's team finishes entering the payments; the run after that
is the one that says what is left.

Every file read is stored under its cheque number in helixona-checks, so
the next run only reads new files (by name + etag), and the report is
rebuilt from the table each time. Nothing here writes to SharePoint, Blue
Shield or eCW.
"""
import os
import tempfile
from datetime import datetime

from src.aws.clients import scan_all
from src.checks.ecw_payments import list_payments
from src.checks.read_check import read_check
from src.checks.reconcile import norm_check, reconcile
from src.checks import sharepoint as sp
from src.utils.logger import get_logger

logger = get_logger(__name__)

CHECKS_TABLE = 'helixona-checks'
EOB_TABLE = 'helixona-eobs'
DEFAULT_SINCE = '07/01/2025'
SNAPSHOT_KEY = '_ecw_payments'
SUMMARY_KEY = '_summary'


def _now():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')


def ensure_table(aws_client):
    """helixona-checks, created on first use (setup_dynamodb.py is not
    deployed to the host)."""
    ddb = aws_client.dynamodb
    names = [t.name for t in ddb.tables.all()]
    if CHECKS_TABLE in names:
        return ddb.Table(CHECKS_TABLE)
    logger.info(f"🆕 creating table {CHECKS_TABLE}")
    t = ddb.create_table(TableName=CHECKS_TABLE,
                         KeySchema=[{'AttributeName': 'check_number', 'KeyType': 'HASH'}],
                         AttributeDefinitions=[{'AttributeName': 'check_number', 'AttributeType': 'S'}],
                         BillingMode='PAY_PER_REQUEST')
    t.wait_until_exists()
    return t


# ------------------------------------------------------------- the copies
def _sources(aws_client, body):
    """(files, download) for SharePoint when its secret exists, else the S3
    inbox. `download(item, dest_dir) -> path`."""
    bucket = os.environ.get('S3_BUCKET_NAME', '')
    if body.get('source', 'auto') in ('auto', 'sharepoint'):
        try:
            creds = aws_client.get_secret('sharepoint_credentials')
        except Exception:
            creds = None
        if creds and creds.get('client_id'):
            token = sp.graph_token(creds)
            return sp.list_check_files(token, creds), (lambda it, d: sp.download(it, d)), 'sharepoint'
        if body.get('source') == 'sharepoint':
            raise RuntimeError('no sharepoint_credentials secret — see src/checks/sharepoint.py')
        logger.info("  (no sharepoint_credentials secret — reading the S3 inbox instead)")
    from src.config import settings
    bucket = bucket or settings.s3_bucket_name
    return (sp.list_s3_inbox(aws_client, bucket),
            (lambda it, d: sp.download_s3(aws_client, bucket, it, d)), 's3_inbox')


def read_new_copies(aws_client, table, body):
    """Read every cheque image not yet on file. Returns (read, skipped, failed)."""
    files, download, source = _sources(aws_client, body)
    limit = int(body.get('limit_files') or 0)
    known = {}
    for it in scan_all(table, ProjectionExpression='check_number, copy_file, copy_etag'):
        if it.get('copy_file'):
            known[str(it['copy_file'])] = str(it.get('copy_etag') or '')
    read = skipped = failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            if limit and read + failed >= limit:
                break
            if known.get(f['path']) == (f.get('etag') or '') and f['path'] in known:
                skipped += 1
                continue
            try:
                path = download(f, tmp)
                got = read_check(aws_client, path)
            except Exception as e:
                got = {'check_number': '', 'amount': '', 'problem': f'download or read failed: {str(e)[:160]}'}
            key = norm_check(got.get('check_number')) or f"unreadable:{f['path']}"
            item = {
                'check_number': key,
                'has_copy': bool(norm_check(got.get('check_number'))),
                'copy_amount': got.get('amount', ''),
                'copy_date': got.get('check_date', ''),
                'copy_payer': got.get('payer', ''),
                'copy_file': f['path'],
                'copy_etag': f.get('etag') or '',
                'copy_url': f.get('web_url', ''),
                'copy_source': source,
                'copy_read_by': got.get('read_by', ''),
                'copy_confidence': got.get('confidence', ''),
                'copy_problem': got.get('problem', ''),
                'copy_read_at': _now(),
            }
            table.update_item(
                Key={'check_number': key},
                UpdateExpression='SET ' + ', '.join(f'#{k} = :{k}' for k in item if k != 'check_number'),
                ExpressionAttributeNames={f'#{k}': k for k in item if k != 'check_number'},
                ExpressionAttributeValues={f':{k}': v for k, v in item.items() if k != 'check_number'})
            if item['has_copy']:
                read += 1
                logger.info(f"  🖼 {f['path']}: cheque {key} ${item['copy_amount'] or '?'} ({item['copy_read_by']})")
            else:
                failed += 1
                logger.warning(f"  ⚠️ {f['path']}: {item['copy_problem']}")
    logger.info(f"📁 copies: {read} read · {skipped} already on file · {failed} unreadable ({source})")
    return read, skipped, failed


# ------------------------------------------------------------------- run
def run_check_reconcile(aws_client, body, login, get_page):
    """`get_page()` opens the eCW browser on demand; `login` is the shared
    eCW login from src.main."""
    since = str(body.get('since') or DEFAULT_SINCE)
    do_copies = body.get('copies', True)
    do_ecw = body.get('ecw', True)
    logger.info(f"Remittance — cheque reconciliation: since={since} copies={do_copies} ecw={do_ecw}")
    table = ensure_table(aws_client)

    if do_copies:
        try:
            read_new_copies(aws_client, table, body)
        except Exception as e:
            logger.error(f"❌ cheque images could not be read: {e}")

    cheques = scan_all(aws_client.dynamodb.Table(EOB_TABLE),
                       ProjectionExpression='check_eft, check_amount, check_status, check_date, cashed_date')
    logger.info(f"🧾 Blue Shield: {len(cheques)} cheque(s) captured")

    payments, ecw_checked = [], False
    if do_ecw:
        page = get_page()
        creds = aws_client.get_secret('ecw_credentials')
        if login(page, creds, aws_client):
            got = list_payments(page, since)
            if got is not None:
                payments, ecw_checked = got, True
                table.put_item(Item={'check_number': SNAPSHOT_KEY, 'since': since, 'payments': payments[:2000],
                                     'count': len(payments), 'read_at': _now()})
        else:
            logger.error("❌ eCW login failed — payments not read this run")
    if not ecw_checked:
        snap = table.get_item(Key={'check_number': SNAPSHOT_KEY}).get('Item') or {}
        if snap.get('payments'):
            payments, ecw_checked = list(snap['payments']), True
            logger.info(f"  (using the eCW payments read on {snap.get('read_at')})")

    copies = [it for it in scan_all(table) if it.get('has_copy')]
    rows, summary = reconcile(
        [{'check_number': c['check_number'], 'amount': c.get('copy_amount', ''), 'file': c.get('copy_file', ''),
          'url': c.get('copy_url', ''), 'check_date': c.get('copy_date', '')} for c in copies],
        cheques, payments, ecw_checked=ecw_checked)

    for r in rows:
        table.update_item(
            Key={'check_number': r['check_number']},
            UpdateExpression='SET verdict = :v, flags = :f, in_blue_shield = :b, bs_amount = :ba, bs_status = :bs, '
                             'bs_date = :bd, cashed_date = :cd, in_ecw = :e, ecw_amount = :ea, ecw_posted = :ep, '
                             'ecw_unposted = :eu, ecw_payment_id = :ei, has_copy = :hc, reconciled_at = :t',
            ExpressionAttributeValues={
                ':v': r['verdict'], ':f': r['flags'], ':b': r['in_blue_shield'], ':ba': r['bs_amount'],
                ':bs': r['bs_status'], ':bd': r['bs_date'], ':cd': r['cashed_date'], ':e': r['in_ecw'],
                ':ea': r['ecw_amount'], ':ep': r['ecw_posted'], ':eu': r['ecw_unposted'],
                ':ei': r['ecw_payment_id'], ':hc': r['has_copy'], ':t': _now()})
    table.put_item(Item={'check_number': SUMMARY_KEY, **summary, 'ecw_checked': ecw_checked,
                         'since': since, 'reconciled_at': _now()})
    logger.info(f"═══ Reconciliation: {summary} ═══")
    return {'ok': True, **summary, 'ecw_checked': ecw_checked}
