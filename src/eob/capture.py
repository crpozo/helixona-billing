"""Remittance — capturing Explanations of Benefits from the Blue Shield portal.

This is the operator's own procedure (screen recording of 2026-09-07), step
for step, up to the point where money would be posted into eCW:

    1. Claims → Check claim status → Claim status = Finalized, Payment
       information = "Claim amount paid" from $0.01 → Search.
    2. Every Check/EFT number in the results → its Check/EFT details page:
       the transaction summary (check number, amount, date, status, cashed
       date, payee) and the claims that cheque paid.
    3. "Download EOB report" → the EOB PDF, which carries the PATIENT ACCOUNT
       NUMBER — the eCW claim number, our `claim_id` — and the CPT-level
       amounts a payment posting needs.

Read-only against the payer. Posting into eCW is a separate task with its
own gate, because it writes money into the practice's ledger.

Idempotent by cheque: a Check/EFT already stored with its PDF is skipped, so a
run can be repeated after a crash or a portal outage without re-downloading
anything. Every miss leaves a screenshot in /tmp and a log line naming it —
the selectors here were written from a recording, not from the live DOM, and
the first run is expected to teach us something.
"""
import csv
import hashlib
import json
import os
import re
import time
from datetime import datetime

from src.aws.clients import scan_all
from src.blueshield.session import login_to_provider_portal, CLAIM_STATUS_URL
from src.eob.eob_pdf import read_eob_pdf
from src.eob.parse import (
    dos_start, index_claims, match_claim, money, norm_text,
    parse_check_summary, parse_eob_pdf_text, rows_by_header,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_SINCE = '07/01/2025'
EOB_TABLE = 'helixona-eobs'
ADJUDICATIONS_TABLE = 'helixona-adjudications'
CLAIMS_TABLE = 'helixona-claims'
S3_PREFIX = 'eobs'
DIAG_DIR = '/tmp'

# Page text that means the results list is empty rather than unparsed.
NO_RESULTS_MARKERS = ("couldn't find any claims", "couldn’t find any claims",
                      "we couldn't find", "we couldn’t find")

# One DOM read for any Angular-Material or plain table on the page: header
# texts, then each row's cell texts and links. Parsed BY HEADER NAME in
# parse.rows_by_header, never by position.
TABLE_JS = r"""(() => {
    const txt = el => (el.innerText || el.textContent || '').trim();
    const hdrs = Array.from(document.querySelectorAll(
        'th, [role="columnheader"], mat-header-cell')).map(txt).filter(Boolean);
    const rowEls = Array.from(document.querySelectorAll(
        'tbody tr, tr[mat-row], mat-row, [role="row"]'));
    const rows = [];
    for (const r of rowEls) {
        const cells = Array.from(r.querySelectorAll(
            'td, mat-cell, [role="cell"], [role="gridcell"]')).map(txt);
        if (cells.length < 3) continue;
        const links = Array.from(r.querySelectorAll('a[href]')).map(a => ({
            text: txt(a), href: a.href }));
        rows.push({ cells, links });
    }
    return { hdrs, rows };
})"""


def _shot(page, name):
    path = os.path.join(DIAG_DIR, f'eob_{name}.png')
    try:
        page.screenshot(path=path, full_page=True)
        logger.warning(f"     📸 screenshot: {path}")
    except Exception:
        pass
    return path


def _today():
    return datetime.now().strftime('%m/%d/%Y')


# ------------------------------------------------------------- the search
def _mat_select(page, label_contains, option_exact):
    """Open the mat-select whose text contains `label_contains` and pick the
    option whose text is exactly `option_exact`. Logs the options it saw."""
    idx = page.evaluate(r"""((label) => {
        const sels = document.querySelectorAll('mat-select, [role="combobox"]');
        for (let i = 0; i < sels.length; i++) {
            if ((sels[i].textContent || '').includes(label)) return i;
        }
        return -1;
    })""", label_contains)
    if idx < 0:
        logger.warning(f"  ⚠️ no mat-select containing {label_contains!r}")
        return False
    sels = page.query_selector_all('mat-select, [role="combobox"]')
    sels[idx].scroll_into_view_if_needed()
    sels[idx].click()
    time.sleep(1.5)
    seen = page.evaluate(r"""(() => Array.from(document.querySelectorAll(
        'mat-option, [role="option"]')).map(o => (o.textContent || '').trim()))""")
    logger.info(f"  {label_contains}: options {seen}")
    ok = page.evaluate(r"""((wanted) => {
        for (const o of document.querySelectorAll('mat-option, [role="option"]')) {
            if ((o.textContent || '').trim() === wanted) { o.click(); return true; }
        }
        return false;
    })""", option_exact)
    time.sleep(0.5)
    if not ok:
        page.keyboard.press('Escape')
        logger.warning(f"  ⚠️ option {option_exact!r} not offered under {label_contains!r}")
    else:
        logger.info(f"  ✅ {label_contains} = {option_exact}")
    return ok


def _type_into(page, handle, value):
    handle.click()
    time.sleep(0.2)
    page.keyboard.press('Control+a')
    page.keyboard.press('Backspace')
    page.keyboard.type(value, delay=40)
    page.keyboard.press('Tab')
    time.sleep(0.4)


def _apply_filters(page, since):
    """Finalized · Claim amount paid ≥ $0.01 · status/payment date from `since`."""
    logger.info("🔎 Applying EOB search filters")
    _mat_select(page, 'Claim status', 'Finalized')
    time.sleep(0.8)
    if _mat_select(page, 'Payment information', 'Claim amount paid'):
        time.sleep(1.0)
        # The amount range appears once the option is chosen: two inputs,
        # minimum first. Anything that looks like a money field will do; the
        # log says which one was used.
        amount_inputs = page.evaluate(r"""(() => Array.from(document.querySelectorAll('input'))
            .map((el, i) => ({ i, ph: el.placeholder || '', aria: el.getAttribute('aria-label') || '',
                               fc: el.getAttribute('formcontrolname') || '', type: el.type || '' }))
            .filter(d => /amount|\$|0\.00|min|max|paid/i.test(d.ph + ' ' + d.aria + ' ' + d.fc)))""")
        logger.info(f"  amount-looking inputs: {amount_inputs}")
        if amount_inputs:
            handles = page.query_selector_all('input')
            _type_into(page, handles[amount_inputs[0]['i']], '0.01')
            logger.info("  ✅ Claim amount paid ≥ 0.01")
        else:
            logger.warning("  ⚠️ no amount input found after choosing 'Claim amount paid'")
            _shot(page, 'no_amount_input')

    # Date pairs on the form: dates of service first, status/payment second.
    date_inputs = page.evaluate(r"""(() => Array.from(document.querySelectorAll('input'))
        .map((el, i) => ({ i, ph: el.placeholder || '', aria: el.getAttribute('aria-label') || '',
                           fc: el.getAttribute('formcontrolname') || '' }))
        .filter(d => /start|end|date|from|to\b/i.test(d.ph + ' ' + d.aria + ' ' + d.fc)))""")
    logger.info(f"  date-looking inputs: {date_inputs}")
    handles = page.query_selector_all('input')
    if len(date_inputs) >= 4:
        si, ei = handles[date_inputs[2]['i']], handles[date_inputs[3]['i']]
        which = 'status/payment date (2nd pair)'
    elif len(date_inputs) >= 2:
        si, ei = handles[date_inputs[0]['i']], handles[date_inputs[1]['i']]
        which = 'first date pair'
    else:
        si = ei = None
        which = 'none'
    if si is not None:
        _type_into(page, si, since)
        _type_into(page, ei, _today())
        logger.info(f"  ✅ {which}: {since} → {_today()}")
    else:
        logger.warning("  ⚠️ no date inputs found; the portal's default range applies")

    try:
        page.click('button:has-text("Search")', timeout=5000)
    except Exception:
        page.click('button[type="submit"]', timeout=5000)
    logger.info("  ✅ Search")
    time.sleep(6)


def _read_table(page):
    data = page.evaluate(TABLE_JS)
    return data.get('hdrs', []), data.get('rows', [])


def _rows_with_links(hdrs, raw_rows):
    rows = rows_by_header(hdrs, [r['cells'] for r in raw_rows])
    # rows_by_header drops non-claim rows, so re-pair links by claim number.
    by_claim = {}
    for r in raw_rows:
        cells = ' '.join(r['cells'])
        m = re.search(r'\b(\d{12})\b', cells)
        if m:
            by_claim[m.group(1)] = r['links']
    for row in rows:
        links = by_claim.get(row.get('bsc_claim_number'), [])
        for l in links:
            if row.get('check_eft') and l['text'] == row['check_eft']:
                row['check_href'] = l['href']
            elif 'eob' in l['text'].lower():
                row['eob_href'] = l['href']
    return rows


def _try_export(page):
    """The portal's Export gives every result (up to 5,000) in one file; the
    on-screen list stops at 1,000. Returns parsed rows or None."""
    try:
        with page.expect_download(timeout=20000) as dl:
            page.click('text=Export', timeout=5000)
        path = os.path.join(DIAG_DIR, 'eob_export' + os.path.splitext(dl.value.suggested_filename or '.csv')[1])
        dl.value.save_as(path)
        logger.info(f"  ✅ Export downloaded: {path}")
    except Exception as e:
        logger.info(f"  (no export: {str(e)[:80]})")
        return None
    try:
        if path.lower().endswith('.csv'):
            with open(path, newline='', encoding='utf-8-sig') as fh:
                reader = csv.reader(fh)
                table = [r for r in reader if any(c.strip() for c in r)]
        else:
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            table = [[str(c) if c is not None else '' for c in row]
                     for row in wb.active.iter_rows(values_only=True)]
        if len(table) < 2:
            return None
        rows = rows_by_header(table[0], table[1:])
        logger.info(f"  export columns: {table[0]}")
        return rows
    except Exception as e:
        logger.warning(f"  ⚠️ export file unreadable: {e}")
        return None


def _collect_results(page):
    rows = _try_export(page)
    if rows:
        return rows, 'export'
    clicks = 0
    while clicks < 80:
        btn = page.query_selector('button:has-text("Show more claims")')
        if not (btn and btn.is_visible()):
            break
        btn.click()
        clicks += 1
        time.sleep(3)
    if clicks:
        logger.info(f"  loaded {clicks} more pages")
    hdrs, raw = _read_table(page)
    rows = _rows_with_links(hdrs, raw)
    if not rows:
        body = page.inner_text('body')
        if any(m in body.lower() for m in NO_RESULTS_MARKERS):
            logger.info("  portal reports no claims for these filters")
            return [], 'empty'
        logger.warning(f"  ⚠️ no rows parsed. headers seen: {hdrs[:20]}; "
                       f"raw rows: {len(raw)}; first raw: {raw[0]['cells'][:12] if raw else None}")
        _shot(page, 'results_unparsed')
    return rows, 'dom'


# --------------------------------------------------------- per-cheque work
def _download_eob_pdf(page, check):
    path = os.path.join(DIAG_DIR, f'eob_{check}.pdf')
    try:
        with page.expect_download(timeout=30000) as dl:
            page.click('text=Download EOB report', timeout=8000)
        dl.value.save_as(path)
        return path
    except Exception as e:
        logger.warning(f"  ⚠️ EOB report download did not start: {str(e)[:100]}")
    # The report may have opened as a tab instead. Fetch it from inside that
    # page — the only download path Cloudflare lets through (see the HCFA
    # capture, which learned this the hard way).
    for pg in page.context.pages:
        if pg is page or '.pdf' not in (pg.url or '').lower():
            continue
        try:
            b64 = pg.evaluate(r"""(() => fetch(location.href, { credentials: 'include' })
                .then(r => r.arrayBuffer()).then(buf => {
                    const bytes = new Uint8Array(buf); let s = '';
                    for (let i = 0; i < bytes.length; i += 8192)
                        s += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
                    return btoa(s);
                }))""")
            import base64
            data = base64.b64decode(b64)
            if data[:5] == b'%PDF-':
                with open(path, 'wb') as fh:
                    fh.write(data)
                return path
        except Exception as e:
            logger.warning(f"  ⚠️ in-tab fetch failed: {str(e)[:80]}")
        finally:
            try:
                pg.close()
            except Exception:
                pass
    _shot(page, f'{check}_no_pdf')
    return ''


def _fits_in_item(claims, limit=300_000):
    """The parsed claims, lines and all, unless they would push the DynamoDB
    item past its 400 KB cap; then the claims without their lines (the PDF
    in S3 can always be re-read)."""
    if len(json.dumps(claims, default=str)) <= limit:
        return claims
    logger.warning(f"  ⚠️ {len(claims)} claims' lines are too large for the item — storing claims only")
    return [{k: v for k, v in c.items() if k != 'lines'} for c in claims]


def _pdf_text(path):
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return '\n'.join((p.extract_text() or '') for p in pdf.pages)
    except Exception as e:
        logger.warning(f"  ⚠️ could not read PDF text: {e}")
        return ''


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _capture_check(page, aws_client, check, href, result_rows, claim_idx, known_pdfs):
    logger.info(f"═══ Check/EFT {check} — {len(result_rows)} result row(s) ═══")
    if href:
        page.goto(href, wait_until='domcontentloaded', timeout=60000)
    else:
        page.click(f'a:has-text("{check}")', timeout=10000)
    try:
        page.wait_for_load_state('networkidle', timeout=15000)
    except Exception:
        pass
    time.sleep(2)

    body = page.inner_text('body')
    summary = parse_check_summary(body)
    if not summary.get('check_eft'):
        summary['check_eft'] = check
    if not summary.get('check_amount') and not summary.get('check_date'):
        logger.warning("  ⚠️ transaction summary not recognised on this page")
        _shot(page, f'{check}_summary')

    hdrs, raw = _read_table(page)
    detail_rows = rows_by_header(hdrs, [r['cells'] for r in raw])
    logger.info(f"  summary: {summary} · claims table: {len(detail_rows)} row(s)")

    pdf_path = _download_eob_pdf(page, check)
    s3_path, parsed, sha = '', {}, ''
    if pdf_path:
        # The operator's step 2: the same report downloaded twice is one
        # report. Identical bytes are stored once and shared.
        sha = _sha256(pdf_path)
        dup = known_pdfs.get(sha)
        if dup and dup[0] != check and dup[1]:
            s3_path = dup[1]
            logger.info(f"  ♻️ EOB report is byte-identical to check {dup[0]}'s — "
                        f"reusing it, not storing a second copy")
        else:
            s3_path = aws_client.upload_to_s3(pdf_path, f'{S3_PREFIX}/{check}.pdf') or ''
        known_pdfs[sha] = (check, s3_path)
        try:
            parsed = read_eob_pdf(pdf_path)
        except Exception as e:
            logger.warning(f"  ⚠️ EOB report could not be read by position: {e}")
            parsed = {}
        if not parsed.get('parsed_ok'):
            # The text reading still yields the account numbers that pin
            # each payer claim to ours; the lines are left unknown.
            problems = (parsed.get('problems') or []) + [
                'the report was not read by position — its lines are unknown']
            parsed = {**parse_eob_pdf_text(_pdf_text(pdf_path)), 'problems': problems}
        logger.info(f"  PDF: {os.path.getsize(pdf_path)} bytes · EOB {parsed.get('eob_number') or '?'} "
                    f"· {len(parsed.get('claims', []))} claim(s) · parsed_ok={parsed.get('parsed_ok')} "
                    f"· {len(parsed.get('problems') or [])} problem(s)")
        for prob in (parsed.get('problems') or [])[:10]:
            logger.warning(f"    ⚠️ {prob}")

    # Pin each claim the cheque paid to one of ours. The PDF's account number
    # is definitive when its claim number matches; otherwise subscriber+DOS.
    acct_by_bsc = {c['bsc_claim_number']: c['patient_account_number']
                   for c in parsed.get('claims', []) if c.get('bsc_claim_number')}
    claims_out, matched = [], 0
    source_rows = detail_rows or result_rows
    for r in source_rows:
        bsc = r.get('bsc_claim_number', '')
        cid = acct_by_bsc.get(bsc) or match_claim(r, claim_idx)
        if cid:
            matched += 1
        claims_out.append({
            'bsc_claim_number': bsc,
            'subscriber_id': r.get('subscriber_id', ''),
            'member_name': r.get('member_name', ''),
            'dos': dos_start(r.get('dos')),
            'date_finalized': r.get('date_finalized', ''),
            'amount_billed': money(r.get('amount_billed')),
            'amount_paid': money(r.get('amount_paid')),
            'patient_resp': money(r.get('patient_resp')),
            'claim_id': cid,
            'match_source': 'pdf_account_number' if acct_by_bsc.get(bsc) else ('subscriber_dos' if cid else ''),
        })

    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
    item = {
        'check_eft': check,
        'check_amount': summary.get('check_amount', ''),
        'check_date': summary.get('check_date', ''),
        'check_status': summary.get('check_status', ''),
        'cashed_date': summary.get('cashed_date', ''),
        'payee_name': summary.get('payee_name', ''),
        'num_claims': summary.get('num_claims', '') or str(len(claims_out)),
        'eob_number': parsed.get('eob_number', ''),
        'eob_issue_date': parsed.get('issue_date', ''),
        'approve_to_pay': parsed.get('approve_to_pay', ''),
        'eob_interest': parsed.get('interest', ''),
        'eob_offsets': parsed.get('offsets', ''),
        'eob_check_amount': parsed.get('check_amount', ''),
        'paid_to_member': bool(parsed.get('payment_issued_to')),
        'eob_adjusted_claim': bool(parsed.get('adjusted_claim')),
        'eob_problems': (parsed.get('problems') or [])[:50],
        'eob_pdf_s3_path': s3_path,
        'eob_pdf_sha256': sha,
        'eob_pdf_parsed_ok': bool(parsed.get('parsed_ok')),
        'eob_claims': _fits_in_item(parsed.get('claims', [])),
        'claims': claims_out,
        'claims_matched': matched,
        'details_url': page.url,
        'captured_at': now,
        'posted_in_ecw': False,
    }
    aws_client.dynamodb.Table(EOB_TABLE).put_item(Item=item)

    adj = aws_client.dynamodb.Table(ADJUDICATIONS_TABLE)
    for c in claims_out:
        if not c['claim_id']:
            continue
        adj.put_item(Item={
            'claim_id': c['claim_id'],
            'check_eft': check,
            'check_date': item['check_date'],
            'cashed_date': item['cashed_date'],
            'bsc_claim_number': c['bsc_claim_number'],
            'dos': c['dos'],
            'amount_billed': c['amount_billed'],
            'amount_paid': c['amount_paid'],
            'patient_resp': c['patient_resp'],
            'date_finalized': c['date_finalized'],
            'outcome': 'Paid' if money(c['amount_paid']) not in ('', '0.00') else 'Denied',
            'eob_pdf_s3_path': s3_path,
            'match_source': c['match_source'],
            'captured_at': now,
        })
        aws_client.update_claim_status(c['claim_id'], {
            'eob_status': 'paid' if money(c['amount_paid']) not in ('', '0.00') else 'denied',
            'eob_paid_amount': c['amount_paid'],
            'eob_check_eft': check,
            'eob_check_date': item['check_date'],
            'eob_captured_at': now,
        })

    logger.info(f"  💰 Check {check} · ${item['check_amount'] or '?'} · {item['check_date'] or '?'} "
                f"· {item['check_status'] or '?'} · cashed {item['cashed_date'] or '?'} "
                f"· {len(claims_out)} claim(s), matched {matched} · PDF {'ok' if s3_path else 'MISSING'}")
    return item


# ------------------------------------------------------------------- run
def run_eob_capture(page, aws_client, body):
    since = str(body.get('since') or DEFAULT_SINCE)
    limit = int(body.get('limit_checks') or 0)
    only = str(body.get('check_eft') or '').strip()
    force = bool(body.get('force'))
    logger.info(f"Remittance: since={since} limit={limit or 'none'} only={only or 'all'} force={force}")

    if not login_to_provider_portal(page, aws_client):
        logger.error("❌ Blue Shield login failed — nothing captured")
        return {'ok': False, 'reason': 'login'}
    page.goto(CLAIM_STATUS_URL, wait_until='domcontentloaded', timeout=60000)
    try:
        page.wait_for_load_state('networkidle', timeout=20000)
    except Exception:
        pass
    time.sleep(2)

    _apply_filters(page, since)
    rows, how = _collect_results(page)
    logger.info(f"📋 {len(rows)} finalized/paid row(s) via {how}")
    if not rows:
        return {'ok': True, 'checks': 0, 'rows': 0}

    checks = {}
    for r in rows:
        ck = norm_text(r.get('check_eft'))
        if not re.fullmatch(r'\d{5,}', ck):
            continue
        checks.setdefault(ck, {'href': '', 'rows': []})
        checks[ck]['rows'].append(r)
        if r.get('check_href') and not checks[ck]['href']:
            checks[ck]['href'] = r['check_href']
    logger.info(f"🧾 {len(checks)} distinct Check/EFT number(s)")

    claim_idx = index_claims(scan_all(
        aws_client.dynamodb.Table(CLAIMS_TABLE),
        ProjectionExpression='claim_id, subscriber_id, service_date, dos, charges'))
    done, known_pdfs = {}, {}
    for it in scan_all(aws_client.dynamodb.Table(EOB_TABLE),
                       ProjectionExpression='check_eft, eob_pdf_s3_path, eob_pdf_sha256'):
        done[str(it.get('check_eft'))] = bool(it.get('eob_pdf_s3_path'))
        if it.get('eob_pdf_sha256'):
            known_pdfs[str(it['eob_pdf_sha256'])] = (str(it.get('check_eft')),
                                                     str(it.get('eob_pdf_s3_path') or ''))

    captured, skipped, failed = 0, 0, 0
    for ck, info in checks.items():
        if only and ck != only:
            continue
        if limit and captured + failed >= limit:
            break
        if done.get(ck) and not force:
            skipped += 1
            continue
        try:
            _capture_check(page, aws_client, ck, info['href'], info['rows'], claim_idx, known_pdfs)
            captured += 1
        except Exception as e:
            failed += 1
            logger.error(f"  ❌ Check {ck} failed: {e}")
            _shot(page, f'{ck}_error')
    logger.info(f"═══ Remittance complete: {captured} captured · {skipped} already on file · {failed} failed ═══")
    return {'ok': True, 'captured': captured, 'skipped': skipped, 'failed': failed,
            'checks': len(checks), 'rows': len(rows)}
