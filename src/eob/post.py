"""Remittance — posting a captured EOB into eCW.

The operator's procedure (2026-09-14), step for step, for every cheque the
capture stored with at least one claim pinned to ours:

    1. Billing → Payments. Rcvd Pmt Dts from 07/01/2025, Check # = the
       Check/EFT number from Blue Shield → Lookup. A payment already there
       under that cheque means it was posted before: nothing to do.
    2. Otherwise "Single insurance payment", and the claim is found by its
       claim number — the PATIENT ACCOUNT NUMBER printed on the EOB.
    3. The payment popup: amount = what the EOB approves to pay; check date
       and EOB date = the Check/EFT date from Blue Shield's Check/EFT
       details; deposit date = the check cashed date from the same page;
       Type = Check; Check No = the cheque number. Then "Payment Advisory".
    4. The advisory: Go (F3), then for every CPT line copy Allowed, CoPay,
       Deductible and Paid from the EOB's line for that code. When the same
       code is on the claim more than once, the billed amount tells the lines
       apart, and where the EOB and eCW disagree on the billed amount the
       drug table (fee_table.py) translates between them. A line that cannot
       be paired with certainty stops the claim: it is left for a person,
       with the reason on the dashboard.
    5. Post.

The gate: a task body without `"post": true` is a DRY RUN. Everything up to
and including the advisory is filled in and photographed, then the popup is
cancelled and nothing is saved. Only `post: true` clicks Post. Posting writes
money into the practice's ledger, and this module was written from the
operator's description, not from eCW's live DOM — the first real run is
expected to teach us something, and it should teach us on a dry run.

Every element is found by its LABEL (the text beside it, its placeholder,
its aria-label, its ng-model), never by position, and every miss leaves a
screenshot in /tmp and a log line that names what was looked for.
"""
import os
import re
import time
from datetime import datetime

from src.aws.clients import scan_all
from src.eob.fee_table import match_lines
from src.eob.parse import money
from src.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_SINCE = '07/01/2025'
EOB_TABLE = 'helixona-eobs'
CLAIMS_TABLE = 'helixona-claims'
DIAG_DIR = '/tmp'

# The Payments screen, as eCW routes it. Clicking Billing → Payments in the
# menu is tried first; these are the fallbacks, in order.
PAYMENT_HASHES = [
    '/mobiledoc/jsp/webemr/webpm/paymentLookup.jsp',
    '/mobiledoc/jsp/webemr/webpm/paymentsLookup.jsp',
    '/mobiledoc/jsp/webemr/webpm/payments.jsp',
]
PAYMENTS_MARKERS = ('Rcvd Pmt', 'Received Payment', 'Payment Lookup', 'Single Insurance')

# Advisory grid columns, by header text.
GRID_COLUMNS = {
    'cpt': re.compile(r'^(cpt|code|proc)', re.I),
    'billed': re.compile(r'(billed|charge|amount)$|^(billed|charges?)', re.I),
    'allowed': re.compile(r'allow', re.I),
    'copay': re.compile(r'co-?pay|co-?ins', re.I),
    'deductible': re.compile(r'deduct|^ded', re.I),
    'paid': re.compile(r'^paid|payment|pmt', re.I),
}


def _shot(page, name):
    path = os.path.join(DIAG_DIR, f'eob_post_{name}.png')
    try:
        page.screenshot(path=path, full_page=True)
        logger.warning(f"     📸 screenshot: {path}")
    except Exception:
        pass
    return path


def _now():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')


# ------------------------------------------------------------ DOM helpers
# Text of anything, and whether it is really on screen. Shared by every
# evaluate below through _js(), which puts these declarations INSIDE the one
# function Playwright calls — a declaration concatenated in front of an arrow
# parses as a call (see _with_popup_finder in src.main for the scar).
_JS_COMMON = r"""
    const txt = el => ((el.innerText || el.textContent || el.value || '') + '').replace(/\s+/g, ' ').trim();
    const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
    // Everything that can label a field: its own attributes, a <label for>,
    // the cell to its left, the text right before it.
    const labelOf = el => {
        const bits = [el.id, el.name, el.placeholder, el.title, el.getAttribute('aria-label'),
                      el.getAttribute('ng-model'), el.getAttribute('data-ng-model')];
        if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l) bits.push(txt(l)); }
        const lab = el.closest('label'); if (lab) bits.push(txt(lab));
        const td = el.closest('td, th');
        if (td) {
            let prev = td.previousElementSibling; let hops = 0;
            while (prev && hops < 2 && !txt(prev)) { prev = prev.previousElementSibling; hops++; }
            if (prev) bits.push(txt(prev).slice(0, 80));
        }
        const box = td || el.parentElement;
        if (box) {
            const own = Array.from(box.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' ');
            if (own.trim()) bits.push(own.replace(/\s+/g, ' ').trim().slice(0, 80));
        }
        return bits.filter(Boolean).join(' | ');
    };
"""


def _js(arrow_src):
    """One expression: the helpers, then `arrow_src` applied to the argument."""
    return '((...args) => {%s\nreturn (%s).apply(null, args);})' % (_JS_COMMON, arrow_src)


def _click_text(page, labels, timeout=8.0, what='', after_target=False):
    """Click the first visible button/link/menu item whose text is one of
    `labels` (case-insensitive). Polls every frame until `timeout`.

    `after_target` prefers the candidate that FOLLOWS the field _set_field
    last filled (data-helixona="target"): a screen with a Lookup button per
    panel must get the one beside the box just typed into, not the first."""
    wanted = [l.lower() for l in labels]
    deadline = time.time() + timeout
    while True:
        for frm in page.frames:
            try:
                hit = frm.evaluate(_js(r"""
                    ([wanted, afterTarget]) => {
                        const els = document.querySelectorAll(
                            'button, input[type="button"], input[type="submit"], a, li, span, div[role], td, label, option');
                        const cands = Array.from(els).filter(el => wanted.includes(txt(el).toLowerCase()) && vis(el));
                        if (!cands.length) return null;
                        const target = afterTarget ? document.querySelector('[data-helixona="target"]') : null;
                        let pick = cands[0];
                        if (target) {
                            const after = cands.find(el => target.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING);
                            if (after) pick = after;
                        }
                        pick.click();
                        return txt(pick).toLowerCase();
                    }"""), [wanted, after_target])
            except Exception:
                hit = None
            if hit:
                logger.info(f"  ✅ clicked {hit!r}{' (' + what + ')' if what else ''}")
                return True
        if time.time() >= deadline:
            logger.warning(f"  ⚠️ nothing to click among {labels}{' (' + what + ')' if what else ''}")
            return False
        time.sleep(0.5)


def _mark_field(page, label_rx, kinds='input, textarea, select', prefer='first'):
    """Find the visible field whose label matches `label_rx`, tag it with
    data-helixona="target" and return (frame, description). None if absent.

    `prefer='last'` takes the LAST visible match: eCW appends its popups to
    the end of the document, so a popup's "Check No" sits after the lookup
    screen's "Check #", and both are on screen at once."""
    for frm in page.frames:
        try:
            desc = frm.evaluate(_js(r"""
                ([rx, kinds, prefer]) => {
                    const re = new RegExp(rx, 'i');
                    document.querySelectorAll('[data-helixona]').forEach(e => e.removeAttribute('data-helixona'));
                    const skip = kinds.includes('checkbox') ? ['hidden', 'button', 'submit', 'image']
                                                            : ['hidden', 'button', 'submit', 'checkbox', 'radio', 'image'];
                    let found = null;
                    for (const el of document.querySelectorAll(kinds)) {
                        const type = (el.type || '').toLowerCase();
                        if (skip.includes(type)) continue;
                        if (!vis(el) || el.disabled) continue;
                        const lab = labelOf(el);
                        if (!re.test(lab)) continue;
                        found = [el, lab];
                        if (prefer !== 'last') break;
                    }
                    if (!found) return null;
                    found[0].setAttribute('data-helixona', 'target');
                    return found[0].tagName + ' :: ' + found[1].slice(0, 120);
                }"""), [label_rx, kinds, prefer])
        except Exception:
            desc = None
        if desc:
            return frm, desc
    return None, ''


def _set_field(page, label_rx, value, what='', prefer='first'):
    """Type `value` into the field labelled `label_rx` (a select gets the option
    whose text contains `value`). Real keystrokes, so masked date inputs and
    Angular bindings both see it. Returns True when a field took the value."""
    frm, desc = _mark_field(page, label_rx, prefer=prefer)
    if frm is None:
        logger.warning(f"  ⚠️ no field labelled /{label_rx}/ ({what or value})")
        return False
    handle = frm.query_selector('[data-helixona="target"]')
    if handle is None:
        return False
    try:
        if desc.startswith('SELECT'):
            try:
                handle.select_option(label=value)
            except Exception:
                ok = frm.evaluate(r"""(([sel, wanted]) => {
                    const el = document.querySelector(sel);
                    for (const o of el.options) {
                        if ((o.textContent || '').trim().toLowerCase().includes(wanted.toLowerCase())) {
                            el.value = o.value; el.dispatchEvent(new Event('change', { bubbles: true })); return true; }
                    }
                    return false; })""", ['[data-helixona="target"]', value])
                if not ok:
                    logger.warning(f"  ⚠️ option {value!r} not in select {desc}")
                    return False
        else:
            handle.click()
            time.sleep(0.15)
            page.keyboard.press('Control+a')
            page.keyboard.press('Backspace')
            page.keyboard.type(str(value), delay=30)
            page.keyboard.press('Tab')
        time.sleep(0.4)
        logger.info(f"  ✅ {what or label_rx} = {value!r}  ← {desc}")
        return True
    except Exception as e:
        logger.warning(f"  ⚠️ could not set {what or label_rx}: {str(e)[:100]}")
        return False


def _page_has(page, markers):
    for frm in page.frames:
        try:
            body = frm.evaluate("() => (document.body && document.body.innerText) || ''")
        except Exception:
            continue
        if any(m.lower() in body.lower() for m in markers):
            return True
    return False


def _dismiss_ok(page):
    """eCW's OK-only popups (data loading error, information)."""
    for frm in page.frames:
        try:
            if frm.evaluate(_js(r"""() => {
                const body = txt(document.body).toLowerCase();
                if (!/data loading error|information|eclinicalworks/.test(body)) return false;
                for (const b of document.querySelectorAll('button, input[type="button"]')) {
                    if (['ok', 'close'].includes(txt(b).toLowerCase()) && vis(b)) { b.click(); return true; }
                }
                return false; }""")):
                time.sleep(0.5)
                return True
        except Exception:
            continue
    return False


# ------------------------------------------------------------ the screens
def open_payments(page):
    """Billing → Payments. Menu clicks first, hash routes second."""
    logger.info("🧭 Billing → Payments")
    if _click_text(page, ['Billing'], timeout=6, what='menu'):
        time.sleep(1.5)
        _click_text(page, ['Payments', 'Payment'], timeout=6, what='menu')
        time.sleep(4)
    if _page_has(page, PAYMENTS_MARKERS):
        logger.info("  ✅ Payments screen is up")
        return True
    for h in PAYMENT_HASHES:
        try:
            page.evaluate("(h) => { window.location.hash = h; }", h)
        except Exception:
            continue
        time.sleep(4)
        if _page_has(page, PAYMENTS_MARKERS):
            logger.info(f"  ✅ Payments screen via #{h}")
            return True
    _shot(page, 'no_payments_screen')
    logger.error("  ❌ Payments screen not reached")
    return False


def payment_exists(page, check_eft, since):
    """Lookup by Check # on the Payments screen; True when a payment already
    carries this cheque number."""
    logger.info(f"🔎 Payments lookup: Rcvd Pmt Dts from {since}, Check # {check_eft}")
    _set_field(page, r'rcvd\s*pmt|received.*(from|date)|from\s*date|start', since, what='Rcvd Pmt Dts from')
    if not _set_field(page, r'check\s*#|check\s*no|check\s*num|chk', check_eft, what='Check #'):
        _shot(page, f'{check_eft}_no_check_field')
        return None
    _click_text(page, ['Lookup', 'Look Up', 'Search'], timeout=5, what='lookup', after_target=True)
    time.sleep(3)
    for frm in page.frames:
        try:
            rows = frm.evaluate(_js(r"""(ck) => {
                const out = [];
                for (const tr of document.querySelectorAll('tbody tr, tr')) {
                    const cells = Array.from(tr.querySelectorAll('td')).map(txt);
                    if (cells.length >= 3 && cells.some(c => c === ck)) out.push(cells.slice(0, 10));
                }
                return out; }"""), str(check_eft))
        except Exception:
            continue
        if rows:
            logger.info(f"  💾 already in eCW: {len(rows)} payment row(s) carry check {check_eft}: {rows[0]}")
            return True
    logger.info("  no payment on file for this cheque")
    return False


def open_single_insurance_payment(page, claim_id):
    """'Single insurance payment', then the claim by its eCW claim number."""
    logger.info(f"➕ Single insurance payment for claim {claim_id}")
    if not _click_text(page, ['Single Insurance Payment', 'Single insurance payment', 'Single Ins. Payment',
                              'Single Insurance Pmt'], timeout=6, what='new payment'):
        # It can sit under a "New" menu.
        if _click_text(page, ['New', 'New Payment', '+'], timeout=4, what='new menu'):
            time.sleep(1)
            if not _click_text(page, ['Single Insurance Payment', 'Single insurance payment'], timeout=5):
                _shot(page, f'{claim_id}_no_single_ins')
                return False
        else:
            _shot(page, f'{claim_id}_no_single_ins')
            return False
    time.sleep(2.5)
    if not _set_field(page, r'claim\s*(no|#|num|id)|invoice|account\s*(no|#)|_InvId', claim_id, what='Claim No',
                      prefer='last'):
        _shot(page, f'{claim_id}_no_claim_field')
        return False
    if not _click_text(page, ['Lookup', 'Look Up', 'Search', 'Go', 'OK'], timeout=4, what='find claim',
                       after_target=True):
        page.keyboard.press('Enter')
    time.sleep(3)
    _dismiss_ok(page)
    if not _page_has(page, (str(claim_id),)):
        logger.warning(f"  ⚠️ claim {claim_id} did not appear after the lookup")
        _shot(page, f'{claim_id}_claim_not_shown')
        return False
    logger.info(f"  ✅ claim {claim_id} is up")
    return True


def fill_payment_header(page, amount, check_date, cashed_date, check_eft):
    """Amount · Check date · EOB date · Deposit date · Type=Check · Check No."""
    logger.info(f"🧾 Payment header: ${amount} · check {check_date} · deposit {cashed_date} · #{check_eft}")
    # The popup is the newest DOM on screen: every field here prefers the
    # last match, so the lookup screen's Check # underneath is not refilled.
    ok = _set_field(page, r'^(amount|amt|payment\s*amount|check\s*amount|total)|amount', amount, what='Amount',
                    prefer='last')
    _set_field(page, r'check\s*date|chk\s*date|pmt\s*date|payment\s*date|received', check_date, what='Check date',
               prefer='last')
    _set_field(page, r'eob\s*date|remit.*date', check_date, what='EOB date', prefer='last')
    if cashed_date:
        _set_field(page, r'deposit', cashed_date, what='Deposit date', prefer='last')
        # A "Deposited" tick beside the date, when eCW shows one.
        frm, _ = _mark_field(page, r'deposit', kinds='input[type="checkbox"]', prefer='last')
        if frm is not None:
            try:
                cb = frm.query_selector('[data-helixona="target"]')
                if cb is not None and not cb.is_checked():
                    cb.check()
                    logger.info("  ✅ Deposited ✓")
            except Exception:
                pass
    _set_field(page, r'^type|pmt\s*type|payment\s*type|method', 'Check', what='Type', prefer='last')
    _set_field(page, r'check\s*(no|#|num)|chk\s*(no|#)|reference', check_eft, what='Check No', prefer='last')
    return ok


def open_advisory(page):
    if not _click_text(page, ['Payment Advisory', 'Payment advisory', 'Pmt Advisory', 'Advisory'],
                       timeout=8, what='advisory'):
        _shot(page, 'no_advisory_button')
        return False
    time.sleep(3)
    _dismiss_ok(page)
    if not _click_text(page, ['Go', 'Go F3', 'Go (F3)', 'F3'], timeout=6, what='Go F3'):
        page.keyboard.press('F3')
        logger.info("  pressed F3")
    time.sleep(3)
    _dismiss_ok(page)
    return True


def read_advisory_grid(page):
    """The advisory's CPT grid: (frame, header keys by column, rows). Each row:
    {'idx', 'cpt', 'billed', 'cells', 'inputs': {col: n}} — inputs are the
    editable cells, by column index."""
    for frm in page.frames:
        try:
            data = frm.evaluate(_js(r"""() => {
                const tables = Array.from(document.querySelectorAll('table')).filter(vis);
                for (const tb of tables) {
                    const hdrs = Array.from(tb.querySelectorAll('th, thead td')).map(txt);
                    if (!hdrs.some(h => /cpt|code|proc/i.test(h)) || !hdrs.some(h => /allow/i.test(h))) continue;
                    const rows = [];
                    const trs = Array.from(tb.querySelectorAll('tbody tr, tr')).filter(tr => tr.querySelector('td'));
                    trs.forEach((tr, i) => {
                        const tds = Array.from(tr.querySelectorAll('td'));
                        const cells = tds.map(td => { const inp = td.querySelector('input, select'); return inp ? (inp.value || '') : txt(td); });
                        const inputs = {};
                        tds.forEach((td, c) => { const inp = td.querySelector('input:not([type=hidden]):not([type=checkbox])'); if (inp && !inp.disabled) inputs[c] = true; });
                        tr.setAttribute('data-helixona-row', String(i));
                        rows.push({ idx: i, cells, inputs });
                    });
                    if (rows.length) return { hdrs, rows };
                }
                return null; }"""))
        except Exception:
            data = None
        if not data:
            continue
        hdrs = data['hdrs']
        cols = {}
        for key, rx in GRID_COLUMNS.items():
            for c, h in enumerate(hdrs):
                if rx.search(h or '') and c not in cols.values():
                    cols[key] = c
                    break
        rows = []
        for r in data['rows']:
            cpt = r['cells'][cols['cpt']] if 'cpt' in cols and cols['cpt'] < len(r['cells']) else ''
            cpt = re.sub(r'[^A-Z0-9]', '', cpt.upper())[:5]
            if not re.fullmatch(r'[A-Z]?\d{4,5}', cpt or ''):
                continue
            billed = money(r['cells'][cols['billed']]) if 'billed' in cols and cols['billed'] < len(r['cells']) else ''
            rows.append({'idx': r['idx'], 'cpt': cpt, 'billed': billed, 'cells': r['cells'],
                         'inputs': {int(k) for k in r['inputs']}})
        logger.info(f"  📊 advisory grid: columns {hdrs} → {cols}; {len(rows)} CPT row(s)")
        return frm, cols, rows
    _shot(page, 'no_advisory_grid')
    logger.warning("  ⚠️ no CPT grid with an Allowed column found")
    return None, {}, []


def fill_grid(page, frm, cols, pairs):
    """Copy allowed / copay / deductible / paid from each EOB line into its
    eCW row. Returns the number of cells written."""
    written = 0
    for p in pairs:
        row, line = p['ecw'], p['eob']
        for key in ('allowed', 'copay', 'deductible', 'paid'):
            col = cols.get(key)
            val = line.get(key, '')
            if col is None or val == '' or col not in row['inputs']:
                if val != '' and col is not None:
                    logger.warning(f"    row {row['idx']} {row['cpt']}: {key} cell is not editable")
                continue
            try:
                handle = frm.query_selector(
                    f'tr[data-helixona-row="{row["idx"]}"] td:nth-child({col + 1}) input')
                if handle is None:
                    continue
                handle.click()
                page.keyboard.press('Control+a')
                page.keyboard.type(val, delay=20)
                page.keyboard.press('Tab')
                written += 1
            except Exception as e:
                logger.warning(f"    row {row['idx']} {key}: {str(e)[:80]}")
        logger.info(f"    {row['cpt']} billed {row['billed']} ← EOB billed {line.get('billed')} "
                    f"allowed {line.get('allowed')} copay {line.get('copay')} ded {line.get('deductible')} "
                    f"paid {line.get('paid')}  [{p['how']}]")
    return written


def cancel_popups(page):
    for _ in range(3):
        if not _click_text(page, ['Cancel', 'Close'], timeout=2):
            page.keyboard.press('Escape')
        time.sleep(1)


# ------------------------------------------------------------- one claim
def claim_amount(item, claim):
    """What this claim's payment is: its own paid amount when the cheque paid
    several claims, else the statement's approve-to-pay (then the cheque)."""
    paid = money(claim.get('amount_paid'))
    if paid and paid != '0.00':
        return paid
    if len([c for c in item.get('claims', []) if c.get('claim_id')]) == 1:
        return money(item.get('approve_to_pay')) or money(item.get('check_amount'))
    return ''


def lines_for(item, claim):
    """The EOB's CPT lines for this claim: its own when the PDF was parsed
    per claim, else the statement's lines when the cheque paid one claim."""
    lines = claim.get('lines') or []
    if lines:
        return lines
    if len(item.get('claims', [])) == 1:
        return item.get('eob_lines') or []
    return []


def post_claim(page, item, claim, since, post):
    check = str(item['check_eft'])
    cid = str(claim['claim_id'])
    amount = claim_amount(item, claim)
    lines = lines_for(item, claim)
    logger.info(f"═══ Cheque {check} → claim {cid} · ${amount or '?'} · {len(lines)} EOB line(s) · "
                f"{'POST' if post else 'DRY RUN'} ═══")
    if not amount:
        return {'status': 'skipped', 'reason': 'no paid amount for this claim'}
    if not lines:
        return {'status': 'skipped', 'reason': 'no CPT lines parsed from the EOB for this claim'}

    if not open_payments(page):
        return {'status': 'failed', 'reason': 'Payments screen not reached'}
    exists = payment_exists(page, check, since)
    if exists:
        return {'status': 'existing', 'reason': f'a payment with check {check} is already in eCW'}
    if exists is None:
        return {'status': 'failed', 'reason': 'Check # field not found on the Payments screen'}

    if not open_single_insurance_payment(page, cid):
        cancel_popups(page)
        return {'status': 'failed', 'reason': 'Single insurance payment / claim lookup failed'}
    if not fill_payment_header(page, amount, item.get('check_date', ''), item.get('cashed_date', ''), check):
        _shot(page, f'{check}_{cid}_header')
        cancel_popups(page)
        return {'status': 'failed', 'reason': 'Amount field not found in the payment popup'}
    if not open_advisory(page):
        cancel_popups(page)
        return {'status': 'failed', 'reason': 'Payment Advisory did not open'}

    frm, cols, rows = read_advisory_grid(page)
    if not rows:
        cancel_popups(page)
        return {'status': 'failed', 'reason': 'advisory grid not read'}
    pairs, unresolved = match_lines(rows, lines)
    for u in unresolved:
        logger.warning(f"    ❓ {u['reason']}")
    bad = [u for u in unresolved if 'eob' in u]
    if bad:
        _shot(page, f'{check}_{cid}_unresolved')
        cancel_popups(page)
        return {'status': 'unresolved', 'reason': '; '.join(u['reason'] for u in bad)[:400],
                'pairs': len(pairs)}

    written = fill_grid(page, frm, cols, pairs)
    logger.info(f"  ✍️ {written} advisory cell(s) filled for {len(pairs)} line(s)")
    _shot(page, f'{check}_{cid}_filled')

    if not post:
        cancel_popups(page)
        return {'status': 'dry_run', 'reason': f'{len(pairs)} line(s) matched, {written} cells filled; not posted',
                'pairs': len(pairs)}
    if not _click_text(page, ['Post', 'Post Payment', 'Save & Post', 'Save'], timeout=8, what='post'):
        _shot(page, f'{check}_{cid}_no_post')
        cancel_popups(page)
        return {'status': 'failed', 'reason': 'Post button not found'}
    time.sleep(3)
    _dismiss_ok(page)
    return {'status': 'posted', 'reason': f'{len(pairs)} line(s)', 'pairs': len(pairs)}


# ------------------------------------------------------------------- run
def run_eob_post(page, aws_client, body, login):
    """`login(page, creds, aws_client) -> bool` is eCW's Turnstile-aware login
    from src.main, handed in so this module never imports the agent."""
    post = bool(body.get('post'))
    only = str(body.get('check_eft') or '').strip()
    limit = int(body.get('limit_checks') or 0)
    since = str(body.get('since') or DEFAULT_SINCE)
    logger.info(f"Remittance → eCW: mode={'POST' if post else 'DRY RUN'} only={only or 'all'} "
                f"limit={limit or 'none'} since={since}")

    eobs = aws_client.dynamodb.Table(EOB_TABLE)
    todo = []
    for it in scan_all(eobs):
        ck = str(it.get('check_eft', ''))
        if only and ck != only:
            continue
        if it.get('posted_in_ecw') and not only:
            continue
        claims = [c for c in it.get('claims', []) if c.get('claim_id')]
        if not claims:
            continue
        todo.append(it)
    todo.sort(key=lambda it: str(it.get('check_date', '')))
    logger.info(f"📋 {len(todo)} cheque(s) with pinned claims to post")
    if not todo:
        return {'ok': True, 'checks': 0}

    creds = aws_client.get_secret('ecw_credentials')
    if not login(page, creds, aws_client):
        logger.error("❌ eCW login failed — nothing posted")
        return {'ok': False, 'reason': 'login'}

    counts = {'posted': 0, 'dry_run': 0, 'existing': 0, 'unresolved': 0, 'failed': 0, 'skipped': 0}
    done = 0
    for it in todo:
        if limit and done >= limit:
            break
        done += 1
        ck = str(it['check_eft'])
        results = {}
        for c in it.get('claims', []):
            if not c.get('claim_id'):
                continue
            cid = str(c['claim_id'])
            try:
                res = post_claim(page, it, c, since, post)
            except Exception as e:
                res = {'status': 'failed', 'reason': str(e)[:300]}
                logger.error(f"  ❌ cheque {ck} claim {cid}: {e}")
                _shot(page, f'{ck}_{cid}_error')
                cancel_popups(page)
            results[cid] = dict(res, at=_now())
            counts[res['status']] = counts.get(res['status'], 0) + 1
            logger.info(f"  → claim {cid}: {res['status']} — {res.get('reason', '')}")
            if res['status'] in ('posted', 'existing'):
                aws_client.update_claim_status(cid, {
                    'payment_posted': True,
                    'payment_posted_at': _now(),
                    'payment_check_eft': ck,
                    'payment_post_status': res['status'],
                })
        all_in = bool(results) and all(r['status'] in ('posted', 'existing') for r in results.values())
        eobs.update_item(
            Key={'check_eft': ck},
            UpdateExpression='SET posted_in_ecw = :p, post_results = :r, post_attempted_at = :t, post_mode = :m',
            ExpressionAttributeValues={':p': all_in, ':r': results, ':t': _now(),
                                       ':m': 'post' if post else 'dry_run'})
    logger.info(f"═══ Remittance → eCW complete: {counts} ═══")
    return dict({'ok': True, 'checks': done}, **counts)
