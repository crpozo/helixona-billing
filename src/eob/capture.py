"""Remittance — capturing Explanations of Benefits from the Blue Shield portal.

This is the operator's own procedure (screen recording of 2026-09-07), step
for step, up to the point where money would be posted into eCW:

    1. Claims → Check claim status → Claim status = Finalized, Payment
       information = "Claim amount paid" from $0.01 → Search.
    2. Every Check/EFT number in the results → its Check/EFT details page:
       the transaction summary (check number, amount, date, status, cashed
       date, payee) and the claims that check paid.
    3. "Download EOB report" → the EOB PDF, which carries the PATIENT ACCOUNT
       NUMBER — the eCW claim number, our `claim_id` — and the CPT-level
       amounts a payment posting needs.

Read-only against the payer. Posting into eCW is a separate task with its
own gate, because it writes money into the practice's ledger.

Idempotent by check: a Check/EFT already stored with its PDF is skipped, so a
run can be repeated after a crash or a portal outage without re-downloading
anything. Every miss leaves a screenshot in /tmp and a log line naming it —
the selectors here were written from a recording, not from the live DOM, and
the first run is expected to teach us something.
"""
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
    parse_check_summary, parse_eob_pdf_text, rows_by_header, rows_with_links,
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

# One DOM read of the results table: header texts, then each row's cell
# texts and links. Parsed BY HEADER NAME in parse.rows_by_header, never by
# position. Headers are kept as they are, EMPTY ONES INCLUDED, so they line
# up with the cells; the table with the most rows is the results table, the
# rest of the page (filters, pagers) is left alone.
TABLE_JS = r"""(() => {
    const txt = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    const read = c => {
        const hdrs = Array.from(c.querySelectorAll('th, [role="columnheader"], mat-header-cell')).map(txt);
        const rows = [];
        for (const r of c.querySelectorAll('tbody tr, tr[mat-row], mat-row, [role="row"]')) {
            const cells = Array.from(r.querySelectorAll('td, mat-cell, [role="cell"], [role="gridcell"]')).map(txt);
            if (cells.length < 3) continue;
            const links = Array.from(r.querySelectorAll('a, button')).map(a => ({ text: txt(a), href: a.href || '' }));
            rows.push({ cells, links });
        }
        return { hdrs, rows };
    };
    let best = null;
    for (const c of document.querySelectorAll('table, mat-table, [role="table"], [role="grid"]')) {
        const got = read(c);
        if (!got.rows.length) continue;
        if (!best || got.rows.length > best.rows.length
            || (got.rows.length === best.rows.length && got.hdrs.length < best.hdrs.length)) best = got;
    }
    return best || read(document);
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


def _check_no(row):
    """The row's Check/EFT number, or '' when the cell is not one."""
    ck = norm_text(row.get('check_eft'))
    return ck if re.fullmatch(r'\d{5,}', ck) else ''


def _visible_results(page):
    """The rows on screen right now, with the check links they carry."""
    hdrs, raw = _read_table(page)
    return rows_with_links(hdrs, raw), hdrs, raw


# The results list pages two ways: a "Show more claims" button that appends
# rows, and a pager at the bottom (Angular's mat-paginator or a numbered
# pager) whose next control replaces them. The operator (2026-09-17): "there
# are more checks — use the pagination at the bottom".
NEXT_PAGE_SELECTORS = (
    'button:has-text("Show more claims")',
    'button[aria-label*="Next page" i]', 'button.mat-paginator-navigation-next', 'button.mat-mdc-paginator-navigation-next',
    'a[aria-label*="Next" i]', 'button[aria-label*="Next" i]', 'li.pagination-next a', 'a.page-link:has-text("Next")',
    'a:has-text("Next")', 'button:has-text("Next")', 'a:has-text("›")', 'button:has-text("›")', 'a:has-text("»")',
)


def _show_more(page):
    """Load the next page of results: the Show-more button, else the pager's
    next control at the bottom of the list. False when there is no more."""
    try:
        page.mouse.wheel(0, 20000)
        time.sleep(0.6)
    except Exception:
        pass
    for sel in NEXT_PAGE_SELECTORS:
        try:
            btn = page.query_selector(sel)
            if not (btn or btn is None) or not btn or not btn.is_visible() or btn.is_disabled():
                continue
            if (btn.get_attribute('aria-disabled') or '').lower() == 'true' or 'disabled' in (btn.get_attribute('class') or ''):
                continue
        except Exception:
            continue
        btn.scroll_into_view_if_needed()
        btn.click()
        if 'Show more' not in sel:
            logger.info(f"  ⏭ next page via {sel}")
        time.sleep(3)
        return True
    # A numbered pager with no next arrow: the page after the current one.
    try:
        clicked = page.evaluate(_js_next_number())
    except Exception:
        clicked = False
    if clicked:
        logger.info(f"  ⏭ next page via page number {clicked}")
        time.sleep(3)
        return True
    return False


def _js_next_number():
    return r"""(() => {
        const cur = document.querySelector('[aria-current="page"], li.active a, .pagination .active, .mat-paginator-range-label');
        const items = Array.from(document.querySelectorAll('.pagination a, .pagination button, nav[aria-label*="agination" i] a, nav[aria-label*="agination" i] button'))
            .filter(el => /^\d+$/.test((el.textContent || '').trim()));
        if (!items.length) return false;
        const curN = cur ? parseInt((cur.textContent || '').trim(), 10) : NaN;
        const target = items.find(el => parseInt(el.textContent.trim(), 10) === (isNaN(curN) ? 2 : curN + 1));
        if (!target) return false;
        target.click();
        return parseInt(target.textContent.trim(), 10);
    })()"""


def _back_to_results(page):
    """Leave a Check/EFT details page for the results list, when on one."""
    try:
        back = page.query_selector('a:has-text("Back to search results"), button:has-text("Back to search results")')
        on_details = (back and back.is_visible()) or 'Check/EFT details' in page.inner_text('body')
    except Exception:
        return
    if not on_details:
        return
    if back and back.is_visible():
        back.click()
        logger.info("  ↩ Back to search results")
    else:
        page.go_back(wait_until='domcontentloaded', timeout=30000)
        logger.info("  ↩ browser back to the results")
    try:
        page.wait_for_load_state('networkidle', timeout=15000)
    except Exception:
        pass
    time.sleep(2)


# --------------------------------------------------------- per-check work
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


def _open_check_link(page, check):
    """Click the check's link in the search results.

    Rows are read off the screen page by page, and the portal's check link
    is not always a plain href — so the link is clicked. If a details page is
    still up from the previous check, back to the results first; then "Show
    more claims" until this check's link is on the page (the portal may
    reset to the first page after a back).
    """
    link = f'a:has-text("{check}"), button:has-text("{check}")'

    def visible():
        try:
            el = page.query_selector(link)
            return bool(el and el.is_visible())
        except Exception:
            return False

    if not visible():
        _back_to_results(page)
    for _ in range(80):
        if visible():
            break
        if not _show_more(page):
            break
    if not visible():
        _shot(page, f'{check}_link_missing')
        raise RuntimeError(f'the link for check {check} is not on the results page')
    page.click(link, timeout=10000)


def _capture_check(page, aws_client, check, href, result_rows, claim_idx, known_pdfs, download_eob=False):
    logger.info(f"═══ Check/EFT {check} — {len(result_rows)} result row(s) ═══")
    # Always the click, never the href: every check's link points at the same
    # /claims/checkeftDetails (the portal's markup, 2026-09-15) and the app
    # decides which check to show from the click itself.
    _open_check_link(page, check)
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

    # The EOB report is optional (2026-09-16): the reconciliation compares
    # checks — number, amount, status, cashed date — not the report's lines.
    pdf_path = _download_eob_pdf(page, check) if download_eob else ''
    s3_path, parsed, sha = '', {}, ''
    if not download_eob:
        logger.info("  (EOB report not downloaded — check data only)")
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

    # Pin each claim the check paid to one of ours. The PDF's account number
    # is definitive when its claim number matches; otherwise subscriber+DOS.
    acct_by_bsc = {c['bsc_claim_number']: c['patient_account_number']
                   for c in parsed.get('claims', []) if c.get('bsc_claim_number')}
    claims_out, matched = [], 0
    source_rows = detail_rows or result_rows
    for r in source_rows:
        bsc = r.get('bsc_claim_number', '')
        # The portal's export names our claim outright ("Patient account
        # number", 2026-09-15); the PDF's account number and subscriber+DOS
        # are the fallbacks for rows read off the screen.
        exported = norm_text(r.get('patient_account_number'))
        exported = exported if re.fullmatch(r'\d{1,7}', exported) else ''
        cid = acct_by_bsc.get(bsc) or exported or match_claim(r, claim_idx)
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
            'match_source': ('pdf_account_number' if acct_by_bsc.get(bsc)
                             else 'export_account_number' if exported
                             else 'subscriber_dos' if cid else ''),
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
                f"· {len(claims_out)} claim(s), matched {matched} · "
                f"{'PDF ok' if s3_path else ('PDF MISSING' if download_eob else 'check data only')}")
    return item


# ------------------------------------------------------------------- run
def run_eob_capture(page, aws_client, body):
    since = str(body.get('since') or DEFAULT_SINCE)
    limit = int(body.get('limit_checks') or 0)
    only = str(body.get('check_eft') or '').strip()
    force = bool(body.get('force'))
    download_eob = bool(body.get('download_eob', False))
    logger.info(f"Remittance: since={since} limit={limit or 'none'} only={only or 'all'} force={force} "
                f"download_eob={download_eob}")

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

    # What is already on file, so a run can be repeated and only does new work.
    claim_idx = index_claims(scan_all(
        aws_client.dynamodb.Table(CLAIMS_TABLE),
        ProjectionExpression='claim_id, subscriber_id, service_date, dos, charges'))
    done, known_pdfs = {}, {}
    for it in scan_all(aws_client.dynamodb.Table(EOB_TABLE),
                       ProjectionExpression='check_eft, eob_pdf_s3_path, eob_pdf_sha256, captured_at'):
        # On file = captured, with or without the report PDF. (captured_at
        # was missing from the projection, so once the PDFs were skipped
        # every check looked new and a test of 1 re-opened the same one.)
        done[str(it.get('check_eft'))] = bool(it.get('eob_pdf_s3_path') or it.get('captured_at'))
        if it.get('eob_pdf_sha256'):
            known_pdfs[str(it['eob_pdf_sha256'])] = (str(it.get('check_eft')),
                                                     str(it.get('eob_pdf_s3_path') or ''))
    logger.info(f"💾 {len(done)} check(s) already on file")

    # Page by page through the results on screen. Every check is saved the
    # moment it is captured, so a crash or a portal hiccup loses nothing and
    # the next run picks up where this one stopped. No export: the portal's
    # file did not line up with the screen.
    seen, pages = set(), 0
    captured, skipped, failed, rows_seen = 0, 0, 0, 0
    captured_checks, failed_checks = [], []
    stop = False
    while not stop:
        _back_to_results(page)
        rows, hdrs, raw = _visible_results(page)
        if not rows and pages == 0:
            body = page.inner_text('body')
            if any(m in body.lower() for m in NO_RESULTS_MARKERS):
                logger.info("  portal reports no claims for these filters")
            else:
                logger.warning(f"  ⚠️ no rows parsed. headers seen: {hdrs[:20]}; "
                               f"raw rows: {len(raw)}; first raw: {raw[0]['cells'][:12] if raw else None}")
                _shot(page, 'results_unparsed')
            break
        rows_seen = max(rows_seen, len(rows))
        if pages == 0:
            # The layout, once per run, so a change in the portal is visible
            # in the log rather than guessed at.
            logger.info(f"  columns: {hdrs}")
            if raw:
                logger.info(f"  first row: {raw[0]['cells'][:14]}")
                logger.info(f"  first row links: {[l['text'] for l in raw[0]['links']][:8]}")
            if rows and not any(_check_no(r) for r in rows):
                logger.warning("  ⚠️ rows were read but none carries a check number — the results "
                               "table is not laid out as expected; not paging further")
                _shot(page, 'results_no_checks')
                break

        checks = {}
        for r in rows:
            ck = _check_no(r)
            if not ck:
                continue
            checks.setdefault(ck, {'href': '', 'rows': []})
            checks[ck]['rows'].append(r)
            if r.get('check_href') and not checks[ck]['href']:
                checks[ck]['href'] = r['check_href']
        new = [ck for ck in checks if ck not in seen]
        logger.info(f"📄 results page {pages + 1}: {len(rows)} row(s) on screen, {len(new)} check(s) not yet looked at")

        for ck in new:
            seen.add(ck)
            info = checks[ck]
            if only and ck != only:
                continue
            if done.get(ck) and not force:
                skipped += 1
                if only:
                    logger.info(f"  check {only} is already on file (force:true re-opens it)")
                    stop = True
                    break
                continue
            if limit and captured + failed >= limit:
                stop = True
                break
            try:
                _capture_check(page, aws_client, ck, info['href'], info['rows'], claim_idx, known_pdfs,
                               download_eob=download_eob)
                captured += 1
                captured_checks.append(ck)
                done[ck] = True
            except Exception as e:
                failed += 1
                failed_checks.append(ck)
                logger.error(f"  ❌ Check {ck} failed: {e}")
                _shot(page, f'{ck}_error')
            logger.info(f"  progress: {captured} captured · {skipped} on file · {failed} failed")
            if only:
                # The one check asked for has been looked at; no need to
                # page through the rest of the results.
                stop = True
                break
        if stop:
            break

        # Next page of results. After a back the portal may be at the first
        # page again, so keep loading until something unseen appears.
        _back_to_results(page)
        found_new = False
        for _ in range(200):
            if not _show_more(page):
                break
            pages += 1
            rows, _h, _r = _visible_results(page)
            if any(_check_no(r) and _check_no(r) not in seen for r in rows):
                found_new = True
                break
        if not found_new:
            break

    logger.info(f"═══ Remittance complete: {captured} captured · {skipped} already on file · {failed} failed "
                f"· {len(seen)} check(s) seen over {pages + 1} page(s) ═══")
    return {'ok': True, 'captured': captured, 'skipped': skipped, 'failed': failed,
            'checks': len(seen), 'rows': rows_seen,
            'captured_checks': captured_checks, 'failed_checks': failed_checks}
