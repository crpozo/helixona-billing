"""What eCW has on file — Billing → Payments, Lookup, the grid read by its
column names.

Two ways in. `list_payments`: no Check # filter, every payment since a
date — one lookup instead of one per cheque, since a thousand cheques
against eCW is a thousand round trips while the Payments list since
07/01/2025 is a few pages. `find_payments`: the operator's own step for one
cheque — Check # = the number, Lookup — used when a run is about one or a
few cheques (the test of 1). The grid's columns (docs/ecw_posting.md:
PAYMENT ID · AMOUNT · POSTED · UNPOSTED, plus the cheque number and dates)
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

COLUMNS = {
    'payment_id': re.compile(r'^(payment\s*)?id$|pmt\s*id|payment\s*id', re.I),
    'check_no': re.compile(r'check|chk|reference', re.I),
    'amount': re.compile(r'^amount|^amt|total', re.I),
    'posted': re.compile(r'^posted', re.I),
    'unposted': re.compile(r'^un-?posted|balance|remaining', re.I),
    'received': re.compile(r'rcvd|received|pmt\s*date|payment\s*date', re.I),
    'payer': re.compile(r'payer|insurance|from|name', re.I),
}

GRID_JS = r"""() => {
    const tables = Array.from(document.querySelectorAll('table')).filter(vis);
    let best = null;
    for (const tb of tables) {
        const hdrs = Array.from(tb.querySelectorAll('th, thead td')).map(txt);
        if (!hdrs.some(h => /amount|amt/i.test(h)) || !hdrs.some(h => /check|chk|posted|payment/i.test(h))) continue;
        const rows = Array.from(tb.querySelectorAll('tbody tr, tr')).filter(tr => tr.querySelector('td'))
            .map(tr => Array.from(tr.querySelectorAll('td')).map(txt)).filter(c => c.length >= 3);
        if (!best || rows.length > best.rows.length) best = { hdrs, rows };
    }
    return best;
}"""


def _read_grid(page):
    for frm in page.frames:
        try:
            data = frm.evaluate(_js(GRID_JS))
        except Exception:
            data = None
        if data and data.get('rows'):
            return data['hdrs'], data['rows']
    return [], []


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
    """Grid rows -> payment dicts, by header name. Rows without a cheque
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
            'payer': cell('payer'),
        })
    return out


def list_payments(page, since):
    """All payments received since `since`, or None when the screen could
    not be read (a screenshot says what was on it)."""
    if not open_payments(page):
        return None
    logger.info(f"🔎 Payments since {since}, every cheque")
    _set_field(page, RCVD_RX, since, what='Rcvd Pmt Dts from')
    _set_field(page, CHECK_RX, '', what='Check # (blank)')
    _click_text(page, LOOKUP, timeout=5, what='lookup', after_target=True)
    time.sleep(4)

    seen, payments = set(), []
    for _ in range(200):
        hdrs, rows = _read_grid(page)
        if not rows:
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
    """Every grid row (any frame, any layout) with a cell that is the cheque number."""
    for frm in page.frames:
        try:
            rows = frm.evaluate(_js(ROWS_WITH_JS), str(check_no))
        except Exception:
            continue
        if rows:
            return rows
    return []


def find_payments(page, check_no, since, navigate=True):
    """The payments eCW holds under one cheque number — the operator's own
    step: Billing → Payments, Rcvd Pmt Dts from `since`, Check # = the
    number, Lookup. [] when the grid comes back empty (not in eCW), None
    when the screen could not be worked (a screenshot says what was on it).
    `navigate` False: the Payments screen is already up from the last call."""
    if navigate and not open_payments(page):
        return None
    check_no = str(check_no).strip()
    want = check_no.lstrip('0')
    logger.info(f"🔎 Payments lookup: Rcvd Pmt Dts from {since}, Check # {check_no}")
    _set_field(page, RCVD_RX, since, what='Rcvd Pmt Dts from')
    if not _set_field(page, CHECK_RX, check_no, what='Check #'):
        _shot(page, f'{check_no}_no_check_field')
        return None
    _click_text(page, LOOKUP, timeout=5, what='lookup', after_target=True)
    time.sleep(3)
    hdrs, rows = _read_grid(page)
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
    else:
        logger.info(f"  no payment on file under check {check_no}")
    return got
