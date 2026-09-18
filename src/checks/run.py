"""Remittance — which checks are posted, which are not, and which we hold.

The operator's procedure (2026-09-16):

    1. SharePoint, Shared Documents / Insurance Checks: every file is the
       image of a check. Read its number and amount.
    2. Blue Shield: the check's Check/EFT status — Check Cashed or not.
       With blue_shield:true this run walks the portal itself (src/eob/
       capture.py); otherwise it takes what earlier captures stored in
       helixona-eobs.
    3. Every check we hold no copy of is flagged.
    4. eCW, Billing → Payments, the check number: on file means posted
       (or entered and left unposted).

Friday, Vignesh's team finishes entering the payments; the run after that
is the one that says what is left.

Two shapes of run. A full run reconciles every check known to the three
sources. A targeted run — limit_checks / check_eft, the "test of 1" — takes
one (or a few) check(s) from Blue Shield and compares just those: the
copies named after them are read first, eCW is asked for each by Check #,
and only their rows are written. Either way the dashboard's tiles are
recomputed over the whole table, so they agree with the rows under them.

Every file read is stored under its check number in helixona-checks, so
the next run only reads new files (by name + etag). The `_run` item is the
live trace of the current run — step by step, for the dashboard — and
`_summary` the counts. Nothing here writes to SharePoint, Blue Shield or
eCW.
"""
import os
import re
import tempfile
from datetime import datetime

from src.aws.clients import scan_all
from src.checks.ecw_payments import find_payments, list_payments
from src.checks.read_check import read_check
from src.checks.reconcile import norm_check, reconcile, summarize
from src.checks import sharepoint as sp
from src.checks import sharepoint_browser as spb
from src.utils.logger import get_logger

logger = get_logger(__name__)

CHECKS_TABLE = 'helixona-checks'
EOB_TABLE = 'helixona-eobs'
DEFAULT_SINCE = '07/01/2025'
SNAPSHOT_KEY = '_ecw_payments'
SUMMARY_KEY = '_summary'
RUN_KEY = '_run'


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
def _sources(aws_client, body, get_page=None):
    """(files, download, source). SharePoint through Graph when the secret
    carries an app registration; else SharePoint through the bot's browser
    (a Helixona account signed in once on the live screen); the S3 inbox
    with source:'s3' or when there is no browser. `download(item, dest) -> path`."""
    bucket = os.environ.get('S3_BUCKET_NAME', '')
    source = body.get('source', 'auto')
    if source in ('auto', 'sharepoint'):
        # Quietly: the app-registration secret is optional (the browser
        # session is the usual way in), so its absence is not an error.
        try:
            raw = aws_client.secrets.get_secret_value(SecretId='prod/helixona/sharepoint_credentials').get('SecretString')
            import json
            creds = json.loads(raw) if raw else {}
        except Exception:
            creds = {}
        if creds.get('client_id'):
            token = sp.graph_token(creds)
            return sp.list_check_files(token, creds), (lambda it, d: sp.download(it, d)), 'sharepoint'
        if get_page is not None:
            page = get_page()
            site, folder = spb.open_folder(page, link=body.get('share_link') or creds.get('share_link'), creds=creds)
            skip = tuple(body.get('skip_folders') or spb.SKIP_FOLDERS)
            return (spb.list_folder(page, site, folder, skip=skip),
                    (lambda it, d: spb.download(page, it, d)), 'sharepoint')
        if source == 'sharepoint':
            raise RuntimeError('SharePoint needs the browser or an app registration — see src/checks/sharepoint_browser.py')
        logger.info("  (no browser for SharePoint — reading the S3 inbox instead)")
    from src.config import settings
    bucket = bucket or settings.s3_bucket_name
    return (sp.list_s3_inbox(aws_client, bucket),
            (lambda it, d: sp.download_s3(aws_client, bucket, it, d)), 's3_inbox')


def _named_after(path, numbers):
    """A file whose name carries one of the check numbers (a scan is often
    saved as 'Check 30912971.pdf')."""
    digits = re.sub(r'\D', '', os.path.basename(str(path)))
    return any(n and n in digits for n in numbers)


MAX_READ_ATTEMPTS = 2   # a file that gave no check twice is a letter or an EOB page, not a check


def read_new_copies(aws_client, table, body, prefer=(), get_page=None):
    """Read every check image not yet on file. Returns (read, skipped, failed).

    `prefer`: check numbers this run is about — files named after them are
    read first, so a capped test run reaches them before anything else.
    A file that gave no check is tried again once (the model may have been
    down), then left alone unless it changes; a targeted run never re-tries."""
    files, download, source = _sources(aws_client, body, get_page)
    limit = int(body.get('limit_files') or 0)
    want = {norm_check(p) for p in prefer if norm_check(p)}
    targeted = bool(want)
    if want:
        files = sorted(files, key=lambda f: 0 if _named_after(f['path'], want) else 1)
    # Files already looked at, by name + etag: the ones that read (skip while
    # unchanged) and the ones that did not (skip after MAX_READ_ATTEMPTS, or
    # always on a targeted run).
    known, tried = {}, {}
    for it in scan_all(table, ProjectionExpression='check_number, copy_file, copy_etag, has_copy, copy_attempts'):
        path = str(it.get('copy_file') or '')
        if not path:
            continue
        if it.get('has_copy'):
            known[path] = str(it.get('copy_etag') or '')
        else:
            tried[path] = (str(it.get('copy_etag') or ''), int(it.get('copy_attempts') or 1))
    read = skipped = failed = 0
    api_errors = 0
    logger.info(f"📁 {len(files)} file(s) in {source}" + (f", {len(known)} already read" if known else '')
                + (f", {len(tried)} gave no check before" if tried else ''))
    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            if limit and read + failed >= limit:
                break
            etag = f.get('etag') or ''
            if f['path'] in known and known[f['path']] == etag:
                skipped += 1
                continue
            prev = tried.get(f['path'])
            if prev and prev[0] == etag and (targeted or prev[1] >= MAX_READ_ATTEMPTS):
                skipped += 1
                continue
            try:
                path = download(f, tmp)
                got = read_check(aws_client, path)
            except Exception as e:
                got = {'check_number': '', 'amount': '', 'problem': f'download or read failed: {str(e)[:160]}'}
            if got.get('error_kind') == 'api':
                # Not the file's fault: nothing is written for it, so it is
                # read on the next run as if never seen. Three in a row and
                # the reading stops — the account needs a person, not retries.
                api_errors += 1
                if api_errors >= 3:
                    raise RuntimeError(f"the Anthropic API refused three files in a row — stopping the image reading. "
                                       f"{got.get('problem', '')}")
                continue
            api_errors = 0
            key = norm_check(got.get('check_number')) or f"unreadable:{f['path']}"
            item = {
                'check_number': key,
                'has_copy': bool(norm_check(got.get('check_number'))),
                'copy_amount': got.get('amount', ''),
                'copy_date': got.get('check_date', ''),
                'copy_payer': got.get('payer', ''),
                'copy_file': f['path'],
                # Where the department filed it — 'Posted Checks/2026/07-2026'
                # — the team's own word on the check, shown on the dashboard.
                'copy_folder': f['path'].rsplit('/', 1)[0] if '/' in f['path'] else '',
                'copy_status': ('posted' if f['path'].lower().startswith('posted')
                                else 'unposted' if f['path'].lower().startswith('unposted') else ''),
                'copy_etag': f.get('etag') or '',
                'copy_url': f.get('web_url', ''),
                'copy_source': source,
                'copy_read_by': got.get('read_by', ''),
                'copy_confidence': got.get('confidence', ''),
                'copy_problem': got.get('problem', ''),
                'copy_attempts': (prev[1] if prev and prev[0] == etag else 0) + 1,
                'copy_read_at': _now(),
            }
            table.update_item(
                Key={'check_number': key},
                UpdateExpression='SET ' + ', '.join(f'#{k} = :{k}' for k in item if k != 'check_number'),
                ExpressionAttributeNames={f'#{k}': k for k in item if k != 'check_number'},
                ExpressionAttributeValues={f':{k}': v for k, v in item.items() if k != 'check_number'})
            if item['has_copy']:
                read += 1
                logger.info(f"  🖼 {f['path']}: check {key} ${item['copy_amount'] or '?'} ({item['copy_read_by']})")
            else:
                failed += 1
                logger.warning(f"  ⚠️ {f['path']}: {item['copy_problem']}")
    logger.info(f"📁 copies: {read} read · {skipped} already on file · {failed} unreadable ({source})")
    return read, skipped, failed


# ------------------------------------------------------------ blue shield
def collect_blue_shield(page, aws_client, body, since, limit_checks, only):
    """Walk the portal for this run's checks (src/eob/capture.py, check
    data only). Returns the check numbers captured. A named check is
    re-opened even when it is on file — the point is its status today; an
    unnamed test of 1 takes the next check not yet on file, so each test
    tries a different one."""
    from src.eob.capture import run_eob_capture
    got = run_eob_capture(page, aws_client, {
        'since': since, 'limit_checks': limit_checks, 'check_eft': only,
        'force': bool(body.get('force', bool(only))), 'download_eob': False}) or {}
    if not got.get('ok'):
        logger.error(f"❌ Blue Shield: {got.get('reason') or 'the portal could not be walked'}")
    return [norm_check(c) for c in got.get('captured_checks', []) if norm_check(c)]


# ------------------------------------------------------------------- run
def run_check_reconcile(aws_client, body, login, get_page):
    """`get_page()` opens the (one, shared) browser on demand; `login` is the
    shared eCW login from src.main.

    body: since · copies · ecw · limit_files · source · blue_shield ·
    limit_checks · check_eft · force. blue_shield:true with limit_checks:1
    is the test of 1: one check from the portal, compared with the copies
    and with eCW, its row on the dashboard.
    """
    since = str(body.get('since') or DEFAULT_SINCE)
    do_copies = body.get('copies', True)
    do_ecw = body.get('ecw', True)
    blue_shield = bool(body.get('blue_shield'))
    limit_checks = int(body.get('limit_checks') or 0)
    only = norm_check(body.get('check_eft'))
    logger.info(f"Remittance — check reconciliation: since={since} blue_shield={blue_shield} "
                f"limit_checks={limit_checks or 'none'} check_eft={only or 'any'} copies={do_copies} ecw={do_ecw}")
    table = ensure_table(aws_client)

    run = {'mode': 'full', 'since': since, 'started_at': _now(), 'targets': [], 'steps': {}, 'order': []}

    def progress(step, text):
        """The live trace the dashboard shows while the run is going."""
        if step not in run['order']:
            run['order'].append(step)
        run['steps'][step] = text
        run['updated_at'] = _now()
        try:
            table.put_item(Item={'check_number': RUN_KEY, **run})
        except Exception as e:
            logger.warning(f"  (run trace not written: {e})")

    # 1. Blue Shield — this run's checks.
    targets = set()
    if blue_shield:
        progress('blue_shield', 'walking the portal…')
        try:
            caught = collect_blue_shield(get_page(), aws_client, body, since, limit_checks, only)
        except Exception as e:
            logger.error(f"❌ Blue Shield step failed: {e}")
            caught = []
        if limit_checks or only:
            targets = set(caught) or ({only} if only else set())
            if not caught and not only:
                progress('blue_shield', 'no check captured — nothing to compare')
                progress('done', 'stopped')
                return {'ok': False, 'reason': 'no check captured'}
        progress('blue_shield', f"{len(caught)} check(s) collected"
                 + (f": {', '.join(caught)}" if caught and len(caught) <= 6 else ''))
    elif only:
        targets = {only}
    run['mode'] = 'test of 1' if len(targets) == 1 else ('targeted' if targets else 'full')
    run['targets'] = sorted(targets)
    if targets:
        logger.info(f"🎯 {run['mode']}: {', '.join(run['targets'])}")

    # 2. The copies. A targeted run whose checks already have a copy on
    # file has nothing to read: the folder is not walked again.
    on_file = {}
    if targets:
        for it in scan_all(table, ProjectionExpression='check_number, has_copy, copy_file'):
            if it.get('has_copy') and norm_check(it.get('check_number')) in targets:
                on_file[norm_check(it.get('check_number'))] = str(it.get('copy_file') or '')
    if do_copies and targets and set(on_file) >= targets:
        for ck, path in on_file.items():
            logger.info(f"📁 copy of {ck} already on file: {path}")
        progress('copies', 'already on file: ' + ' · '.join(f"{ck} → {path.rsplit('/', 1)[0]}" for ck, path in on_file.items()))
    elif do_copies:
        progress('copies', 'reading the check images…')
        try:
            read, skipped, failed = read_new_copies(aws_client, table, body, prefer=targets, get_page=get_page)
            progress('copies', f"{read} read · {skipped} on file · {failed} unreadable")
        except Exception as e:
            logger.error(f"❌ check images could not be read: {e}")
            progress('copies', f"not read: {str(e)[:120]}")
    else:
        progress('copies', 'skipped (copies:false)')

    checks = scan_all(aws_client.dynamodb.Table(EOB_TABLE),
                       ProjectionExpression='check_eft, check_amount, check_status, check_date, cashed_date')
    if targets:
        checks = [q for q in checks if norm_check(q.get('check_eft')) in targets]
    logger.info(f"🧾 Blue Shield: {len(checks)} check(s)" + ('' if blue_shield else ' on file from earlier captures'))
    if not blue_shield:
        progress('blue_shield', f"{len(checks)} check(s) on file from earlier captures")

    # 3. eCW — one lookup per check, the operator's own step (2026-09-18):
    # Check # = the number, Rcvd Pmt Dts from `since` to today, Lookup. The
    # full Payments list is thousands of card payments and pages; a lookup
    # is one row. Checks already posted are settled and not asked again;
    # their stored answer is reused. Any other answer eCW gave on an earlier
    # run (unposted, not in eCW) is kept when this run's lookup fails or
    # never happens — a failed lookup is not a new answer.
    copies = [it for it in scan_all(table) if it.get('has_copy')
              and (not targets or norm_check(it.get('check_number')) in targets)]
    known = {norm_check(it.get('check_number')): it for it in scan_all(table)
             if it.get('verdict') in ('posted', 'unposted', 'not in eCW')}
    settled = {ck: it for ck, it in known.items() if it.get('in_ecw') and it.get('verdict') == 'posted'}
    to_check = targets or ({norm_check(q.get('check_eft')) for q in checks} | {norm_check(c.get('check_number')) for c in copies})
    to_check = {k for k in to_check if k}
    payments, checked = [], set()

    def keep_earlier(ck):
        it = known[ck]
        if it.get('in_ecw'):
            payments.append({'check_no': ck, 'amount': it.get('ecw_amount', ''), 'posted': it.get('ecw_posted', ''),
                             'unposted': it.get('ecw_unposted', ''), 'payment_id': it.get('ecw_payment_id', '')})
        checked.add(ck)

    for ck in sorted(to_check & set(settled)):
        if not targets:   # a targeted run asks again; a full run trusts the settled answer
            keep_earlier(ck)
    ask = sorted(to_check - checked)
    if do_ecw and ask:
        progress('ecw', f"looking up {len(ask)} check(s)" + (f" ({len(checked)} already posted, kept)" if checked else '') + '…')
        page = get_page()
        creds = aws_client.get_secret('ecw_credentials')
        if login(page, creds, aws_client):
            failures, first = 0, True
            for i, ck in enumerate(ask, 1):
                got = find_payments(page, ck, since, navigate=first, shot=bool(targets))
                first = False
                if got is None:
                    failures += 1
                    if failures >= 3:
                        logger.error("❌ eCW: the Payments screen could not be read three times in a row — stopping the lookups")
                        break
                    first = True   # re-open the screen and try the next one
                    continue
                failures = 0
                payments.extend(got)
                checked.add(ck)
                if i % 25 == 0 or i == len(ask):
                    progress('ecw', f"{i}/{len(ask)} looked up · {sum(1 for p in payments if norm_check(p.get('check_no')) in checked)} on file")
            if targets:
                progress('ecw', ' · '.join(
                    f"{ck} {'on file' if any(norm_check(p['check_no']) == ck for p in payments) else ('not found' if ck in checked else 'not read')}"
                    for ck in sorted(targets)))
            else:
                progress('ecw', f"{len(checked)} check(s) looked up · {len({norm_check(p['check_no']) for p in payments})} on file"
                         + (f" · {len(ask) - (len(checked) - len(set(settled) & to_check))} not read" if len(checked) < len(to_check) else ''))
        else:
            logger.error("❌ eCW login failed — payments not read this run")
            progress('ecw', 'login failed')
    elif do_ecw:
        progress('ecw', f"nothing to look up ({len(checked)} already posted, kept)")
    else:
        progress('ecw', 'skipped (ecw:false)')
    # Whatever eCW said on an earlier run stands until eCW says otherwise.
    kept = sorted((to_check - checked) & set(known))
    for ck in kept:
        keep_earlier(ck)
    if kept:
        logger.info(f"🗂️ eCW: {len(kept)} check(s) not looked up this run keep their earlier eCW answer")
        progress('ecw', (run['steps'].get('ecw') or '') + f" · {len(kept)} kept from earlier runs")
    ecw_checked = checked

    # 4. The verdicts.
    rows, _ = reconcile(
        [{'check_number': c['check_number'], 'amount': c.get('copy_amount', ''), 'file': c.get('copy_file', ''),
          'url': c.get('copy_url', ''), 'check_date': c.get('copy_date', '')} for c in copies],
        checks, payments, ecw_checked=ecw_checked)
    ecw_checked = bool(ecw_checked)   # for the summary: was eCW consulted at all
    if targets:
        rows = [r for r in rows if r['check_number'] in targets]

    now = _now()
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
                ':ei': r['ecw_payment_id'], ':hc': r['has_copy'], ':t': now})
        if targets or len(rows) <= 25:
            logger.info(f"  ✅ {r['check_number']}: copy {'yes' if r['has_copy'] else 'NO'}"
                        f"{' $' + r['copy_amount'] if r['copy_amount'] else ''} · Blue Shield "
                        f"{r['bs_status'] or ('—' if not r['in_blue_shield'] else '?')}"
                        f"{' $' + r['bs_amount'] if r['bs_amount'] else ''} · eCW "
                        f"{'on file' if r['in_ecw'] else 'not found'} → {r['verdict']}"
                        + (f" · {' · '.join(r['flags'])}" if r['flags'] else ''))

    # The counts over the whole table, so the tiles agree with the rows.
    everything = [it for it in scan_all(table) if not str(it.get('check_number', '')).startswith(('_', 'unreadable:'))]
    summary = summarize(everything)
    table.put_item(Item={'check_number': SUMMARY_KEY, **summary, 'ecw_checked': ecw_checked,
                         'since': since, 'reconciled_at': now, 'run_rows': len(rows)})
    progress('verdict', ' · '.join(f"{r['check_number']}: {r['verdict']}" + (f" ({', '.join(r['flags'])})" if r['flags'] else '')
                                   for r in rows[:6]) if targets else f"{len(rows)} check(s) reconciled")
    progress('done', now)
    logger.info(f"═══ Reconciliation ({run['mode']}): {len(rows)} row(s) this run · table {summary} ═══")
    return {'ok': True, **summary, 'ecw_checked': ecw_checked, 'run_rows': len(rows), 'targets': run['targets']}
