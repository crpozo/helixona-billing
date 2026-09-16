"""Every payment eCW has on file since a date — Billing → Payments, no
Check # filter, Lookup, the grid read by its column names.

One lookup instead of one per cheque: a thousand cheques against eCW is a
thousand round trips, while the Payments list since 07/01/2025 is a few
pages. The grid's columns (docs/ecw_posting.md: PAYMENT ID · AMOUNT ·
POSTED · UNPOSTED, plus the cheque number and dates) are matched by name,
and a run that finds none of them says so with a screenshot.
"""
import re
import time

from src.eob.post import _js, _set_field, _click_text, _shot, open_payments
from src.utils.logger import get_logger

logger = get_logger(__name__)

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


def rows_to_payments(hdrs, rows):
    """Grid rows -> payment dicts, by header name. Rows without a cheque
    number or an amount are not payments (totals, spacers)."""
    cols = _columns(hdrs)
    out = []
    for cells in rows:
        def cell(key):
            i = cols.get(key)
            return cells[i] if i is not None and i < len(cells) else ''
        check = re.sub(r'\D', '', cell('check_no'))
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
    _set_field(page, r'rcvd\s*pmt|received.*(from|date)|from\s*date|start', since, what='Rcvd Pmt Dts from')
    _set_field(page, r'check\s*#|check\s*no|check\s*num|chk', '', what='Check # (blank)')
    _click_text(page, ['Lookup', 'Look Up', 'Search'], timeout=5, what='lookup', after_target=True)
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
