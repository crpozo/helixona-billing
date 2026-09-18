"""What eCW has on file — Billing → Payments, Lookup, the grid read by its
column names.

Two ways in. `list_payments`: no Check # filter, every payment since a
date — one lookup instead of one per check, since a thousand checks
against eCW is a thousand round trips while the Payments list since
07/01/2025 is a few pages. `find_payments`: the operator's own step for one
check — Check # = the number, Lookup — used when a run is about one or a
few checks (the test of 1). The grid's columns (docs/ecw_posting.md:
PAYMENT ID · AMOUNT · POSTED · UNPOSTED, plus the check number and dates)
are matched by name, and a run that finds none of them says so with a
screenshot.
"""
import re
import time

from src.eob.post import _js, _set_field, _click_text, _shot, open_payments
from src.utils.logger import get_logger

logger = get_logger(__name__)

RCVD_RX = r'rcvd\s*pmt|received.*(from|date)|from\s*date|start'
CHECK_RX = r'check\s*#|check\s*no|check\s*num|chk'
LOOKUP = ['Lookup', 'Look Up', 'Search']

# The Payments grid as it is (screenshot, 2026-09-18): BATCH ID · PAYMENT ID ·
# POSTED BY · ERA EXCEPTION · DATE · PAYMENT FROM · PAYMENT TYPE · CHECK NO ·
# CHECK DATE · DEPOSIT DATE · AMOUNT · POSTED · UNPOSTED. Matched by name,
# most specific first, each header used once.
COLUMNS = {
    'payment_id': re.compile(r'^payment\s*id|^pmt\s*id|^id$', re.I),
    'check_no': re.compile(r'^check\s*(no|#|num)|^chk\s*(no|#)|^check$|reference', re.I),
    'check_date': re.compile(r'^check\s*date|^chk\s*date', re.I),
    'deposit_date': re.compile(r'deposit', re.I),
    'unposted': re.compile(r'^un-?posted|balance|remaining', re.I),
    'posted': re.compile(r'^posted(?!\s*by)', re.I),
    'amount': re.compile(r'^amount|^amt|total', re.I),
    'received': re.compile(r'rcvd|received|pmt\s*date|payment\s*date|^date$', re.I),
    'payer': re.compile(r'payment\s*from|payer|insurance|^from', re.I),
}
TO_RX = r'^to$|toDate|to\s*date'

GRID_JS = r"""() => {
    // eCW's list views come in two shapes: one table with <th> and rows, or a
    // header table above a body table (the rows scroll on their own). Pair
    // every body table with its own headers, else with the header table of
    // the same column count, else the nearest header table above it.
    const tables = Array.from(document.querySelectorAll('table')).filter(vis);
    const headersOf = tb => Array.from(tb.querySelectorAll('th, thead td')).map(txt).filter(h => h !== undefined);
    const rowsOf = tb => Array.from(tb.querySelectorAll('tbody tr, tr')).filter(tr => tr.querySelector('td') && !tr.querySelector('th'))
        .map(tr => Array.from(tr.querySelectorAll('td')).map(txt)).filter(c => c.length >= 3 && c.some(x => x));
    const headerTables = tables.map((tb, i) => ({ i, hdrs: headersOf(tb) })).filter(h => h.hdrs.filter(x => x).length >= 3);
    const looksRight = hdrs => hdrs.some(h => /amount|amt/i.test(h)) && hdrs.some(h => /check|chk|posted|payment|pmt/i.test(h));
    let best = null;
    tables.forEach((tb, i) => {
        const rows = rowsOf(tb);
        let hdrs = headersOf(tb).filter(x => x !== '');
        if (!rows.length) {
            // No data rows: still an answer when the headers are the grid's
            // (the filter simply matched nothing).
            if (hdrs.length >= 3 && looksRight(hdrs) && !best) best = { hdrs, rows: [], score: 500, right: true };
            return;
        }
        if (hdrs.length < 3) {
            const width = rows[0].length;
            const same = headerTables.filter(h => h.i < i && h.hdrs.length === width);
            const above = headerTables.filter(h => h.i < i);
            const pick = (same.length ? same : above).slice(-1)[0];
            hdrs = pick ? pick.hdrs : [];
        }
        const score = (looksRight(hdrs) ? 1000 : 0) + rows.length;
        if (!best || score > best.score) best = { hdrs, rows, score, right: looksRight(hdrs) };
    });
    if (!best) {
        // Angular grids without <table>: rows by role, cells by role.
        const rows = Array.from(document.querySelectorAll('[role="row"]')).filter(vis)
            .map(r => Array.from(r.querySelectorAll('[role="cell"], [role="gridcell"]')).map(txt)).filter(c => c.length >= 3);
        const hdrs = Array.from(document.querySelectorAll('[role="columnheader"]')).map(txt);
        if (rows.length) best = { hdrs, rows, score: rows.length, right: looksRight(hdrs) };
    }
    return best;
}"""

DOM_JS = r"""() => {
    // What the Payments screen is made of, for the log: every visible table
    // (headers, size, first row) and any role-based grid, plus a few lines
    // of text around 'Check'. Enough to write the reader without seeing it.
    const out = { url: location.href, tables: [], grids: 0, text: '' };
    for (const tb of Array.from(document.querySelectorAll('table')).filter(vis)) {
        const hdrs = Array.from(tb.querySelectorAll('th, thead td')).map(txt);
        const trs = Array.from(tb.querySelectorAll('tr')).filter(tr => tr.querySelector('td'));
        const first = trs[0] ? Array.from(trs[0].querySelectorAll('td')).map(txt) : [];
        if (!hdrs.length && trs.length < 2) continue;
        out.tables.push({ id: tb.id || '', cls: (tb.className || '').toString().slice(0, 60), hdrs: hdrs.slice(0, 16), rows: trs.length, first: first.slice(0, 16) });
    }
    out.grids = document.querySelectorAll('[role="grid"], [role="row"]').length;
    const body = (document.body && document.body.innerText) || '';
    const i = body.search(/check\s*#|check no|payment id|rcvd/i);
    out.text = i >= 0 ? body.slice(Math.max(0, i - 200), i + 600).replace(/\s+/g, ' ') : body.slice(0, 400).replace(/\s+/g, ' ');
    return out;
}"""


def describe_screen(page, why):
    """Log what the Payments screen is made of (every frame) and save its
    HTML to DIAG_DIR — the next run's log then says how to read the grid."""
    logger.warning(f"  🧩 {why}: describing the Payments screen")
    for frm in page.frames:
        try:
            got = frm.evaluate(_js(DOM_JS))
        except Exception:
            continue
        if not got or (not got['tables'] and not got['grids']):
            continue
        logger.warning(f"  frame {got['url'][:100]} · {len(got['tables'])} table(s) · {got['grids']} role rows")
        for t in got['tables'][:8]:
            logger.warning(f"    table id={t['id']!r} cls={t['cls']!r} rows={t['rows']} hdrs={t['hdrs']} first={t['first']}")
        if got['text']:
            logger.warning(f"    text near Check: {got['text'][:500]!r}")
    try:
        import os
        path = os.path.join('/tmp', 'eob_post_payments_dom.html')
        with open(path, 'w', encoding='utf-8') as fh:
            for frm in page.frames:
                try:
                    fh.write(f"\n<!-- frame {frm.url} -->\n" + frm.content())
                except Exception:
                    continue
        logger.warning(f"  📄 page HTML: {path}")
    except Exception as e:
        logger.warning(f"  (page HTML not saved: {e})")


def _read_grid(page):
    """(headers, rows, recognised) of the payments grid. Recognised = the
    headers name an amount and a check/posted column; then an empty row
    list is a real answer (the filter matched nothing). Otherwise the biggest
    table on screen is returned with recognised=False and its headers logged."""
    best = None
    for frm in page.frames:
        try:
            data = frm.evaluate(_js(GRID_JS))
        except Exception:
            data = None
        if not data:
            continue
        if data.get('right'):
            return data['hdrs'], data['rows'], True
        if data.get('rows') and (not best or len(data['rows']) > len(best['rows'])):
            best = data
    if best:
        logger.warning(f"  ⚠️ a grid was read but its headers are not the ones expected: {best['hdrs'][:12]} · first row {best['rows'][0][:10]}")
        return best['hdrs'], best['rows'], False
    return [], [], False


def _set_dates(page, since):
    """Rcvd Pmt Dts from `since` to today — both, and both confirmed."""
    ok_from = _set_field(page, RCVD_RX, since, what='Rcvd Pmt Dts from')
    ok_to = _set_field(page, TO_RX, time.strftime('%m/%d/%Y'), what='Rcvd Pmt Dts to')
    if not ok_from:
        logger.warning("  ⚠️ the from-date did not take — the lookup may cover today only")
    return ok_from and ok_to


def _columns(hdrs):
    cols = {}
    for key, rx in COLUMNS.items():
        for i, h in enumerate(hdrs):
            if i in cols.values():
                continue
            if rx.search(h or ''):
                cols[key] = i
                break
    return cols


def rows_to_payments(hdrs, rows, assume_check=''):
    """Grid rows -> payment dicts, by header name. Rows without a check
    number or an amount are not payments (totals, spacers).

    `assume_check`: the grid was filtered by that Check # and may not show
    the column at all — then every row with an amount is a payment under
    that number."""
    cols = _columns(hdrs)
    out = []
    for cells in rows:
        def cell(key):
            i = cols.get(key)
            return cells[i] if i is not None and i < len(cells) else ''
        check = re.sub(r'\D', '', cell('check_no'))
        if not check and 'check_no' not in cols and assume_check and re.search(r'\d', cell('amount')):
            # ...but not the totals row, which carries no payment id.
            if 'payment_id' not in cols or re.sub(r'\D', '', cell('payment_id')):
                check = re.sub(r'\D', '', str(assume_check))
        if not check:
            continue
        out.append({
            'payment_id': re.sub(r'\D', '', cell('payment_id')),
            'check_no': check,
            'amount': cell('amount'),
            'posted': cell('posted'),
            'unposted': cell('unposted'),
            'received': cell('received'),
            'check_date': cell('check_date'),
            'deposit_date': cell('deposit_date'),
            'payer': cell('payer'),
        })
    return out


def list_payments(page, since):
    """All payments received since `since`, or None when the screen could
    not be read (a screenshot says what was on it)."""
    if not open_payments(page):
        return None
    logger.info(f"🔎 Payments since {since}, every check")
    _set_dates(page, since)
    _set_field(page, CHECK_RX, '', what='Check # (blank)')
    _click_text(page, LOOKUP, timeout=5, what='lookup', after_target=True)
    time.sleep(4)

    seen, payments = set(), []
    for _ in range(200):
        hdrs, rows, recognised = _read_grid(page)
        if not rows:
            if recognised:
                logger.info(f"  grid recognised ({hdrs[:8]}) but no rows for this filter")
            break
        batch = rows_to_payments(hdrs, rows)
        new = [p for p in batch if (p['payment_id'], p['check_no'], p['amount']) not in seen]
        for p in new:
            seen.add((p['payment_id'], p['check_no'], p['amount']))
        payments.extend(new)
        logger.info(f"  📄 payments grid: {len(rows)} row(s), {len(new)} new · columns {hdrs[:10]}")
        # Another page, if the grid pages.
        if not _click_text(page, ['Next', 'More', 'Show more', '>'], timeout=2, what='next page'):
            break
        time.sleep(3)
    if not payments:
        _shot(page, 'payments_grid_unread')
        describe_screen(page, 'no payment rows read')
        logger.warning("  ⚠️ no payment rows read from the Payments screen")
        return None
    logger.info(f"  💾 {len(payments)} payment(s) on file since {since}")
    return payments


ROWS_WITH_JS = r"""(ck) => {
    const out = [];
    const want = String(ck).replace(/^0+/, '');
    for (const tr of document.querySelectorAll('tbody tr, tr')) {
        const cells = Array.from(tr.querySelectorAll('td')).map(txt);
        if (cells.length >= 3 && cells.some(c => c.replace(/\D/g, '').replace(/^0+/, '') === want)) out.push(cells.slice(0, 12));
    }
    return out;
}"""


def _rows_with(page, check_no):
    """Every grid row (any frame, any layout) with a cell that is the check number."""
    for frm in page.frames:
        try:
            rows = frm.evaluate(_js(ROWS_WITH_JS), str(check_no))
        except Exception:
            continue
        if rows:
            return rows
    return []


def _grid_signature(page):
    try:
        hdrs, rows, _ = _read_grid(page)
        return (len(rows), tuple(rows[0][:14]) if rows else ())
    except Exception:
        return (0, ())


def find_payments(page, check_no, since, navigate=True, shot=True):
    """The payments eCW holds under one check number — the operator's own
    step (2026-09-18: "put the check number in Check #, dates from 07/01/25
    to today, look the check up"): Billing → Payments, Rcvd Pmt Dts from
    `since` to today, Check # = the number, Lookup. [] when the grid comes
    back empty (not in eCW), None when the screen could not be worked (a
    screenshot says what was on it).

    `navigate` True opens the screen and sets the dates; False means the
    screen is up from the last call — only Check # changes, and the lookup
    waits for the grid to answer instead of a fixed three seconds, so a run
    over hundreds of checks costs a second or two each."""
    check_no = str(check_no).strip()
    want = check_no.lstrip('0')
    if navigate:
        if not open_payments(page):
            return None
        _set_dates(page, since)
    before = _grid_signature(page)
    logger.info(f"🔎 Payments lookup: Check # {check_no} (Rcvd Pmt Dts {since} → today)")
    if not _set_field(page, CHECK_RX, check_no, what='Check #'):
        _shot(page, f'{check_no}_no_check_field')
        return None
    _click_text(page, LOOKUP, timeout=5, what='lookup', after_target=True)
    # The answer is in when the grid differs from what was there. Two empty
    # answers in a row look alike, so an unchanged empty grid is accepted
    # after a shorter wait.
    deadline = time.time() + (8 if before[0] else 3)
    while time.time() < deadline:
        time.sleep(0.25)
        if _grid_signature(page) != before:
            break
    time.sleep(0.4)
    # What eCW answered, on record: the grid's headers and row count, and
    # (on a targeted run) a screenshot — "not in eCW" must be checkable.
    hdrs, rows, recognised = _read_grid(page)
    logger.info(f"  payments grid: {len(rows)} row(s) · columns {hdrs[:10] or 'none'}{'' if recognised else ' · not recognised'}")
    if shot:
        _shot(page, f'{check_no}_payments_lookup')
    if not rows and not recognised:
        describe_screen(page, 'grid not recognised')
    got = [p for p in rows_to_payments(hdrs, rows, assume_check=check_no) if p['check_no'].lstrip('0') == want]
    if not got:
        # A grid laid out differently from the one the headers describe:
        # any row carrying the number is a payment under it.
        for cells in _rows_with(page, check_no):
            got.append({'payment_id': next((c for c in cells if c.isdigit() and c.lstrip('0') != want), ''),
                        'check_no': check_no,
                        'amount': next((c for c in cells if re.search(r'\d\.\d\d', c)), ''),
                        'posted': '', 'unposted': '', 'received': '', 'payer': ''})
    if got:
        logger.info(f"  💾 in eCW: {len(got)} payment(s) under check {check_no} · {got[0]}")
    elif recognised:
        logger.info(f"  no payment on file under check {check_no}")
    else:
        # Neither the grid nor a row with the number: the screen was not
        # read, which is not the same as "not in eCW".
        logger.warning(f"  ⚠️ the Payments grid was not read for check {check_no} — eCW stays unchecked")
        return None
    return got
