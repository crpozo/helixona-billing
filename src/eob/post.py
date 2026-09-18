"""Remittance — entering a planned check into eCW.

The clicking, and only the clicking. Every number typed here was decided by
src/eob/plan.py (which eCW line gets which EOB money) and src/eob/ecw_payment.py
(the popup's values, the grid's cells); a check whose plan is not `ready` is
never opened. docs/ecw_posting.md is the procedure, read off the operator's
two recordings, and the order below is that order.

    Billing → Payments · Rcvd Pmt Dts from 07/01/2025 · Check # · Lookup
        rows → a payment for this check exists: nothing to do
        none → Single Ins Payment (F4) → Claim No: = the EOB's PATIENT ACCOUNT
               NUMBER (our claim id) → Get Insurance → the radio beside
               "Blue Shield of California" → OK
    the Payments popup: Type Check · Check No. · Amount $ = APPROVE-TO-PAY ·
        Check Date · EOB Date (the check date again) · Deposit Date = cashed
        date, ticked and corrected
    ── a DRY RUN stops here: screenshot, Cancel, nothing saved ──
    Payment Advisory  ← THIS CLICK SAVES THE PAYMENT and mints its Payment ID
    Claim ID → Go (F3) → the Alert about the fee schedule is dismissed
    the Payment Posting grid: Allowed · Deduct · CoPay · Paid typed on each
        planned line, after the line's Code and Billed are seen to be the
        plan's; Adjust is eCW's own arithmetic
    Auto Post (F2) → Yes on the balance / "Assign claim to" dialog

The gate: without `"post": true` in the task body nothing is saved — the
popup is filled and photographed, then cancelled. With it, the two writes
happen. A payment whose advisory then cannot be completed (a grid row that is
not what the plan expects) is left as eCW shows it — created, lines unposted —
and the result says so, with the Payment ID when it was read, so a person can
finish or delete it. That is the one state this module can leave behind that
needs a hand; it is reported, never hidden.

Every element is found by its LABEL (the text beside it, its placeholder,
its aria-label, its ng-model), never by position, and every miss leaves a
screenshot in /tmp and a log line that names what was looked for. The DOM
itself has not been seen — the recordings show the screens — so the first
real run is expected to teach us something, on a dry run.
"""
import os
import re
import tempfile
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

import boto3

from src.aws.clients import scan_all
from src.eob.ecw_payment import grid_rows, payment_header, posting_total
from src.eob.eob_pdf import read_eob_pdf
from src.eob.hcfa_lines import hcfa_lines_from_pdf
from src.eob.plan import plan_from_item
from src.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_SINCE = '07/01/2025'
EOB_TABLE = 'helixona-eobs'
CLAIMS_TABLE = 'helixona-claims'
DIAG_DIR = '/tmp'
PAYER_RADIO = 'Blue Shield of California'

# The Payments screen, as eCW routes it. Clicking Billing → Payments in the
# menu is tried first; these are the fallbacks, in order.
PAYMENT_HASHES = [
    # The route the Billing → Payments menu item took on 2026-09-17 (logged
    # by open_payments); the rest were guesses that never answered.
    '/mobiledoc/jsp/webemr/webpm/payments/paymentsListView.jsp',
    '/mobiledoc/jsp/webemr/webpm/paymentLookup.jsp',
    '/mobiledoc/jsp/webemr/webpm/paymentsLookup.jsp',
    '/mobiledoc/jsp/webemr/webpm/payments.jsp',
]
PAYMENTS_MARKERS = ('Rcvd Pmt', 'Single Ins Payment', 'Payment Lookup', 'Single Insurance')

# The Payment Posting grid's columns (docs/ecw_posting.md):
#   Service Dt | POS | Units | Code | Billed | Allowed | Deduct | CoIns | CoPay | Paid | Adjust | Withheld | Code
GRID_COLUMNS = {
    'code': re.compile(r'^(code|cpt|proc)', re.I),
    'billed': re.compile(r'^billed|^charge', re.I),
    'Allowed': re.compile(r'^allow', re.I),
    'Deduct': re.compile(r'^deduct', re.I),
    'CoPay': re.compile(r'^co-?pay', re.I),
    'Paid': re.compile(r'^paid', re.I),
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


def _d(v):
    try:
        return Decimal(str(v).replace('$', '').replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return None


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
    `labels` (case-insensitive; a trailing "(F4)" style key hint on the
    element is ignored). Polls every frame until `timeout`.

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
                        const norm = s => s.toLowerCase().replace(/\s*\(f\d+\)\s*$/, '').trim();
                        const els = document.querySelectorAll(
                            'button, input[type="button"], input[type="submit"], a, li, span, div[role], td, label, option');
                        const cands = Array.from(els).filter(el => {
                            const t = txt(el); if (!t || t.length > 60) return false;
                            return (wanted.includes(t.toLowerCase()) || wanted.includes(norm(t))) && vis(el);
                        });
                        if (!cands.length) return null;
                        const target = afterTarget ? document.querySelector('[data-helixona="target"]') : null;
                        let pick = cands[0];
                        if (target) {
                            const after = cands.find(el => target.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING);
                            if (after) pick = after;
                        }
                        pick.click();
                        return txt(pick);
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
    the end of the document, so a popup's "Check No." sits after the lookup
    screen's "Check #", and both are on screen at once."""
    for frm in page.frames:
        try:
            desc = frm.evaluate(_js(r"""
                ([rx, kinds, prefer]) => {
                    const re = new RegExp(rx, 'i');
                    document.querySelectorAll('[data-helixona]').forEach(e => e.removeAttribute('data-helixona'));
                    const skip = kinds.includes('checkbox') || kinds.includes('radio')
                        ? ['hidden', 'button', 'submit', 'image']
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
            # Tab commits the value (Enter would fire the screen's default
            # action — on Payments that opened the Assistant's "no permission"
            # tooltip); the read-back below catches a date that did not stick.
            page.keyboard.press('Tab')
            time.sleep(0.4)
            got = _read_value(frm)
            if str(value) and got is not None and got.strip() != str(value).strip():
                # The keystrokes did not stick: set the value and fire the
                # events Angular listens to.
                frm.evaluate(r"""(([sel, v]) => { const el = document.querySelector(sel); if (!el) return;
                    el.focus(); el.value = v;
                    for (const t of ['input', 'change', 'blur']) el.dispatchEvent(new Event(t, { bubbles: true })); })""",
                             ['[data-helixona="target"]', str(value)])
                time.sleep(0.4)
                got = _read_value(frm)
            if str(value) and got is not None and got.strip() != str(value).strip():
                logger.warning(f"  ⚠️ {what or label_rx}: typed {value!r} but the field shows {got!r}")
                return False
        time.sleep(0.2)
        logger.info(f"  ✅ {what or label_rx} = {value!r}  ← {desc}")
        return True
    except Exception as e:
        logger.warning(f"  ⚠️ could not set {what or label_rx}: {str(e)[:100]}")
        return False


def _read_value(frm):
    try:
        return frm.evaluate("""() => { const el = document.querySelector('[data-helixona="target"]'); return el ? (el.value ?? '') : null; }""")
    except Exception:
        return None


def _tick(page, label_rx, what=''):
    """Tick the checkbox labelled `label_rx` if it is not already ticked."""
    frm, desc = _mark_field(page, label_rx, kinds='input[type="checkbox"]', prefer='last')
    if frm is None:
        return False
    try:
        cb = frm.query_selector('[data-helixona="target"]')
        if cb is not None and not cb.is_checked():
            cb.check()
        logger.info(f"  ✅ {what or label_rx} ✓  ← {desc}")
        return True
    except Exception as e:
        logger.warning(f"  ⚠️ could not tick {what or label_rx}: {str(e)[:80]}")
        return False


def _choose_radio(page, beside_text):
    """Select the radio button in the same row / label as `beside_text`."""
    for frm in page.frames:
        try:
            ok = frm.evaluate(_js(r"""(wanted) => {
                const w = wanted.toLowerCase();
                for (const r of document.querySelectorAll('input[type="radio"]')) {
                    if (!vis(r)) continue;
                    const row = r.closest('tr, label, li, div');
                    if (row && txt(row).toLowerCase().includes(w)) { r.click(); return true; }
                }
                return false; }"""), beside_text)
        except Exception:
            ok = False
        if ok:
            logger.info(f"  ✅ radio beside {beside_text!r}")
            return True
    logger.warning(f"  ⚠️ no radio beside {beside_text!r}")
    return False


def _page_text(page):
    out = []
    for frm in page.frames:
        try:
            out.append(frm.evaluate("() => (document.body && document.body.innerText) || ''"))
        except Exception:
            continue
    return '\n'.join(out)


def _page_has(page, markers):
    body = _page_text(page).lower()
    return any(m.lower() in body for m in markers)


def _dismiss_dialogs(page, answer='Yes', rounds=3):
    """eCW's small dialogs: the fee-schedule Alert (OK), the balance /
    "Assign claim to" question (Yes), the data-loading error (OK). Answers
    `answer` to a Yes/No, OK to the rest. Returns the texts seen."""
    seen = []
    for _ in range(rounds):
        got = None
        for frm in page.frames:
            try:
                got = frm.evaluate(_js(r"""(answer) => {
                    const btns = Array.from(document.querySelectorAll('button, input[type="button"]')).filter(vis);
                    const by = t => btns.find(b => txt(b).toLowerCase() === t.toLowerCase());
                    const yes = by(answer), ok = by('OK') || by('Ok');
                    const pick = yes || ok;
                    if (!pick) return null;
                    const box = pick.closest('[role="dialog"], .modal, .ui-dialog, .popup, div');
                    const text = box ? txt(box).slice(0, 200) : '';
                    // Only dialogs: something short with a question or alert in it.
                    if (!/alert|warning|continue|sure|error|schedule|balance|assign/i.test(text)) return null;
                    pick.click();
                    return text; }"""), answer)
            except Exception:
                got = None
            if got:
                break
        if not got:
            break
        logger.info(f"  💬 dialog answered {answer if answer.lower() in got.lower() else 'OK'}: {got[:120]!r}")
        seen.append(got)
        time.sleep(1.0)
    return seen


# ------------------------------------------------------------ the screens
MENU_JS = r"""([rx, click]) => {
    // Menu items that lead to Payments — showing or not: eCW's bands open on
    // hover, so the item is in the document before it is on screen.
    const re = new RegExp(rx, 'i');
    const out = [];
    for (const el of document.querySelectorAll('a, li, span, div, button, td, label')) {
        const t = txt(el); if (!t || t.length > 40) continue;
        const attrs = [el.getAttribute('href'), el.getAttribute('ng-click'), el.getAttribute('onclick'), el.id, el.title]
            .filter(Boolean).join(' ');
        if (re.test(t) || re.test(attrs)) out.push({ el, text: t, attrs: attrs.slice(0, 160), visible: vis(el) });
    }
    if (!click) return out.slice(0, 40).map(({ text, attrs, visible }) => ({ text, attrs, visible }));
    const exact = out.filter(c => /^payments?$/i.test(c.text));
    const pick = (exact.find(c => c.visible) || exact[0] || out.find(c => c.visible) || out[0]);
    if (!pick) return null;
    pick.el.click();
    return { text: pick.text, attrs: pick.attrs, visible: pick.visible };
}"""


def _menu_items(page, rx, click=False):
    for frm in page.frames:
        try:
            got = frm.evaluate(_js(MENU_JS), [rx, click])
        except Exception:
            got = None
        if got:
            return got
    return None


def _hover_text(page, text):
    try:
        page.get_by_text(text, exact=True).first.hover(timeout=2000)
        return True
    except Exception:
        return False


def _where(page):
    """The address the shell is at, and the frames' — eCW loads each screen
    as a .jsp, so this is the route a person's click took."""
    try:
        here = page.evaluate("() => location.href")
    except Exception:
        here = '?'
    frames = [f.url for f in page.frames if f.url and f.url != here and 'about:blank' not in f.url][:4]
    return f"{here}" + (f" · frames {frames}" if frames else '')


def open_payments(page, wait_for_person=90):
    """Billing → Payments.

    In order: already there; the menu — a click on Billing, a hover for a
    band that opens on hover, then Payments whether it is showing or not; the
    hash routes; and last a person: the run waits up to `wait_for_person`
    seconds for someone to open Billing → Payments in the noVNC window. The
    route it lands on is logged either way, so a screen the bot could not
    reach today is one it can go to directly tomorrow (PAYMENT_HASHES)."""
    logger.info("🧭 Billing → Payments")
    if _page_has(page, PAYMENTS_MARKERS):
        logger.info(f"  ✅ Payments screen is already up · {_where(page)}")
        return True
    if _click_text(page, ['Billing'], timeout=6, what='menu'):
        time.sleep(1.5)
        _hover_text(page, 'Billing')
        if not _click_text(page, ['Payments', 'Payment', 'Payment Lookup', 'Insurance Payments'], timeout=4, what='menu'):
            hit = _menu_items(page, r'^payments?$|payment\s*lookup|insurance\s*payments?', click=True)
            if hit:
                logger.info(f"  ✅ clicked menu item {hit['text']!r} ({'showing' if hit['visible'] else 'hidden'}) {hit['attrs'][:80]}")
        time.sleep(4)
    if _page_has(page, PAYMENTS_MARKERS):
        logger.info(f"  ✅ Payments screen is up · {_where(page)}")
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
    # What the menus offer, so the log says where Payments lives.
    items = _menu_items(page, r'payment') or []
    if items:
        logger.info("  menu items mentioning payment: " + '; '.join(
            f"{it['text']!r}{'' if it['visible'] else ' (hidden)'} {it['attrs'][:70]}".strip() for it in items[:12]))
    else:
        logger.info(f"  no menu item mentions payment · {_where(page)}")
    if wait_for_person:
        logger.warning(f"  ⚠️ the bot did not find Billing → Payments — open it on the live screen (noVNC); "
                       f"waiting up to {wait_for_person}s")
        deadline = time.time() + wait_for_person
        while time.time() < deadline:
            if _page_has(page, PAYMENTS_MARKERS):
                logger.info(f"  ✅ Payments screen is up (opened on the live screen) · {_where(page)} "
                            f"— that route belongs in PAYMENT_HASHES")
                return True
            time.sleep(3)
    _shot(page, 'no_payments_screen')
    logger.error("  ❌ Payments screen not reached")
    return False


def payment_exists(page, check_eft, since):
    """Lookup by Check # on the Payments screen. True when a payment already
    carries this check number, False when the grid comes back empty, None
    when the screen could not be worked."""
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
    logger.info("  no payment on file for this check")
    return False


def open_single_insurance_payment(page, claim_id):
    """Single Ins Payment (F4) → Claim No: → Get Insurance → Blue Shield → OK."""
    logger.info(f"➕ Single Ins Payment for claim {claim_id}")
    if not _click_text(page, ['Single Ins Payment', 'Single Insurance Payment', 'Single Ins. Payment'],
                       timeout=6, what='new payment'):
        _shot(page, f'{claim_id}_no_single_ins')
        return False
    time.sleep(2.5)
    if not _set_field(page, r'claim\s*(no|#|num|id)|_InvId', claim_id, what='Claim No', prefer='last'):
        _shot(page, f'{claim_id}_no_claim_field')
        return False
    if not _click_text(page, ['Get Insurance', 'Get Ins'], timeout=5, what='Get Insurance', after_target=True):
        page.keyboard.press('Enter')
    time.sleep(2.5)
    if not _choose_radio(page, PAYER_RADIO):
        _shot(page, f'{claim_id}_no_payer_radio')
        return False
    if not _click_text(page, ['OK', 'Ok'], timeout=5, what='OK', after_target=True):
        _shot(page, f'{claim_id}_no_ok')
        return False
    time.sleep(3)
    if not _page_has(page, ('Payment Advisory', 'Payment ID')):
        logger.warning("  ⚠️ the Payments popup did not open")
        _shot(page, f'{claim_id}_no_popup')
        return False
    logger.info("  ✅ Payments popup is up")
    return True


def fill_payment_popup(page, fields):
    """The Payments popup from ecw_payment.payment_header: None means leave
    alone. Every field prefers the last match — the popup is the newest DOM."""
    logger.info(f"🧾 Payments popup: {{k: v for k, v in fields.items() if v is not None}}")
    ok = True
    if fields.get('Type'):
        ok &= _set_field(page, r'^type|pmt\s*type|payment\s*type', fields['Type'], what='Type', prefer='last')
    if fields.get('Check No.'):
        ok &= _set_field(page, r'check\s*no|check\s*#|chk\s*no', fields['Check No.'], what='Check No.', prefer='last')
    if fields.get('Amount $'):
        ok &= _set_field(page, r'^amount|amount\s*\$|amt', fields['Amount $'], what='Amount $', prefer='last')
    if fields.get('Check Date'):
        ok &= _set_field(page, r'check\s*date|chk\s*date', fields['Check Date'], what='Check Date', prefer='last')
    if fields.get('EOB Date'):
        ok &= _set_field(page, r'eob\s*date', fields['EOB Date'], what='EOB Date', prefer='last')
    if fields.get('Deposit Date'):
        # Ticking fills today's date; the cashed date then overwrites it.
        _tick(page, r'deposit', what='Deposit')
        ok &= _set_field(page, r'deposit\s*date|deposit', fields['Deposit Date'], what='Deposit Date', prefer='last')
    return bool(ok)


def read_payment_id(page):
    m = re.search(r'Payment\s*ID\s*:?\s*(\d+)', _page_text(page), re.I)
    return m.group(1) if m else ''


def open_advisory_for_claim(page, claim_id):
    """In the advisory window: Claim ID → Go (F3); the fee-schedule Alert
    is dismissed. True when the Payment Posting grid is up."""
    if not _set_field(page, r'claim\s*id|claim\s*no|claim\s*#', claim_id, what='Claim ID', prefer='last'):
        _shot(page, f'{claim_id}_no_claim_id_field')
        return False
    if not _click_text(page, ['Go', 'Go (F3)'], timeout=6, what='Go F3', after_target=True):
        page.keyboard.press('F3')
    time.sleep(3)
    _dismiss_dialogs(page, answer='OK')
    frm, cols, rows = read_posting_grid(page)
    return bool(rows)


def read_posting_grid(page):
    """The Payment Posting grid: (frame, header keys by column, rows). Each
    row: {'idx', 'code', 'billed', 'cells', 'inputs': {col index}}."""
    for frm in page.frames:
        try:
            data = frm.evaluate(_js(r"""() => {
                const tables = Array.from(document.querySelectorAll('table')).filter(vis);
                for (const tb of tables) {
                    const hdrs = Array.from(tb.querySelectorAll('th, thead td')).map(txt);
                    if (!hdrs.some(h => /^(code|cpt)/i.test(h)) || !hdrs.some(h => /^allow/i.test(h))) continue;
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
            code = r['cells'][cols['code']] if 'code' in cols and cols['code'] < len(r['cells']) else ''
            code = re.sub(r'[^A-Z0-9]', '', code.upper())[:5]
            if not re.fullmatch(r'[A-Z]?\d{4,5}', code or ''):
                continue
            billed = r['cells'][cols['billed']] if 'billed' in cols and cols['billed'] < len(r['cells']) else ''
            rows.append({'idx': r['idx'], 'code': code, 'billed': billed, 'cells': r['cells'],
                         'inputs': {int(k) for k in r['inputs']}})
        logger.info(f"  📊 posting grid: {hdrs} → {cols}; {len(rows)} service line(s)")
        return frm, cols, rows
    _shot(page, 'no_posting_grid')
    logger.warning("  ⚠️ no grid with Code and Allowed columns found")
    return None, {}, []


def locate_rows(grid, planned):
    """Pin each planned line to a grid row, or say why not.

    The plan's ecw_index is the line's position on the claim, and the grid
    keeps the claim's order — so the row at that index should carry the
    plan's code and billed amount. When it does not, the row is found by
    code + billed if that pair is unique in the grid. Otherwise nothing is
    typed on this claim: (None, reason).
    """
    out, used = [], set()
    for p in planned:
        want_code, want_billed = str(p.get('cpt') or '').upper(), _d(p.get('ecw_billed'))
        i = p.get('ecw_index')
        row = grid[i] if isinstance(i, int) and 0 <= i < len(grid) else None
        if row is None or row['code'] != want_code or _d(row['billed']) != want_billed or row['idx'] in used:
            same = [r for r in grid if r['code'] == want_code and _d(r['billed']) == want_billed
                    and r['idx'] not in used]
            if len(same) != 1:
                return None, (f"{want_code} billed {p.get('ecw_billed')}: the grid row at position {i} is "
                              f"{(row or {}).get('code')} billed {(row or {}).get('billed')}, and "
                              f"{len(same)} other row(s) match — not typed")
            row = same[0]
        used.add(row['idx'])
        out.append((row, p))
    return out, ''


def type_grid(page, frm, cols, located):
    """Type Allowed / Deduct / CoPay / Paid into each located row. Returns
    (cells written, problems)."""
    written, problems = 0, []
    for row, p in located:
        for key in ('Allowed', 'Deduct', 'CoPay', 'Paid'):
            col, val = cols.get(key), p['values'].get(key, '')
            if col is None:
                problems.append(f"{p['cpt']}: the grid has no {key} column")
                continue
            if col not in row['inputs']:
                problems.append(f"{p['cpt']}: the {key} cell is not editable")
                continue
            try:
                handle = frm.query_selector(f'tr[data-helixona-row="{row["idx"]}"] td:nth-child({col + 1}) input')
                if handle is None:
                    problems.append(f"{p['cpt']}: no input in the {key} cell")
                    continue
                handle.click()
                page.keyboard.press('Control+a')
                page.keyboard.type(str(val), delay=20)
                page.keyboard.press('Tab')
                written += 1
            except Exception as e:
                problems.append(f"{p['cpt']} {key}: {str(e)[:80]}")
        logger.info(f"    {p['cpt']} billed {p['ecw_billed']}{' ' + p['drug'] if p.get('drug') else ''} ← "
                    + ' '.join(f"{k} {v}" for k, v in p['values'].items()))
    return written, problems


def cancel_popups(page):
    for _ in range(3):
        if not _click_text(page, ['Cancel', 'Close'], timeout=2):
            page.keyboard.press('Escape')
        time.sleep(1)


# ------------------------------------------------------------- one check
def post_check(page, item, plan, since, post):
    """Enter one planned check. Returns the result dict stored on the item."""
    check = str(item['check_eft'])
    claims = plan.get('claims') or []
    logger.info(f"═══ Check {check} · approve-to-pay ${plan['totals'].get('approve_to_pay')} · "
                f"{len(claims)} claim(s) · {'POST' if post else 'DRY RUN'} ═══")
    fields, problems = payment_header({'approve_to_pay': plan['totals'].get('approve_to_pay')}, item)
    if problems:
        return {'status': 'held', 'reason': '; '.join(problems)}
    typed = {}
    for c in claims:
        rows, probs = grid_rows(c)
        if probs or not rows:
            return {'status': 'held', 'reason': f"claim {c.get('claim_id')}: " + ('; '.join(probs) or 'nothing to type')}
        typed[str(c['claim_id'])] = rows

    if not open_payments(page):
        return {'status': 'failed', 'reason': 'Payments screen not reached'}
    exists = payment_exists(page, check, since)
    if exists:
        return {'status': 'existing', 'reason': f'a payment with check {check} is already in eCW'}
    if exists is None:
        return {'status': 'failed', 'reason': 'Check # field not found on the Payments screen'}

    first = str(claims[0]['claim_id'])
    if not open_single_insurance_payment(page, first):
        cancel_popups(page)
        return {'status': 'failed', 'reason': 'Single Ins Payment / claim lookup did not open the popup'}
    if not fill_payment_popup(page, fields):
        _shot(page, f'{check}_popup')
        cancel_popups(page)
        return {'status': 'failed', 'reason': 'a field of the Payments popup was not found (see screenshot)'}
    _shot(page, f'{check}_popup_filled')

    if not post:
        # Payment Advisory is the save. A dry run does not click it.
        cancel_popups(page)
        n = sum(len(r) for r in typed.values())
        return {'status': 'dry_run', 'reason': f'popup filled for ${fields["Amount $"]}, {n} line(s) planned '
                                               f'across {len(claims)} claim(s); not saved'}

    # ── first write ──
    if not _click_text(page, ['Payment Advisory'], timeout=8, what='Payment Advisory — saves the payment'):
        _shot(page, f'{check}_no_advisory')
        cancel_popups(page)
        return {'status': 'failed', 'reason': 'Payment Advisory button not found; nothing saved'}
    time.sleep(3)
    _dismiss_dialogs(page, answer='OK')
    payment_id = read_payment_id(page)
    logger.info(f"  💾 payment saved · Payment ID {payment_id or '?'}")

    posted, left = [], []
    for c in claims:
        cid = str(c['claim_id'])
        if not open_advisory_for_claim(page, cid):
            left.append(f'claim {cid}: the Payment Posting grid did not open')
            continue
        frm, cols, grid = read_posting_grid(page)
        located, why = locate_rows(grid, typed[cid])
        if located is None:
            _shot(page, f'{check}_{cid}_grid_mismatch')
            left.append(f'claim {cid}: {why}')
            continue
        written, probs = type_grid(page, frm, cols, located)
        if probs:
            _shot(page, f'{check}_{cid}_grid_problems')
            left.append(f'claim {cid}: ' + '; '.join(probs))
            continue
        _shot(page, f'{check}_{cid}_grid_typed')
        # ── second write ──
        if not _click_text(page, ['Auto Post', 'Auto Post (F2)'], timeout=6, what='Auto Post F2'):
            page.keyboard.press('F2')
        time.sleep(2)
        _dismiss_dialogs(page, answer='Yes')
        posted.append(f'{cid} (${posting_total(typed[cid])}, {written} cells)')
        logger.info(f"  ✅ claim {cid}: {written} cells, paid ${posting_total(typed[cid])}, Auto Post")

    if left:
        _shot(page, f'{check}_incomplete')
        return {'status': 'incomplete', 'payment_id': payment_id,
                'reason': f"payment {payment_id or '?'} was created; posted {', '.join(posted) or 'nothing'}; "
                          f"NOT posted: {'; '.join(left)} — finish or delete it in eCW"}
    return {'status': 'posted', 'payment_id': payment_id,
            'reason': f"payment {payment_id or '?'}: {', '.join(posted)}"}


# ------------------------------------------------------------------- run
def make_readers(aws_client):
    """The S3-backed readers plan_from_item wants, the dashboard's way."""
    s3 = boto3.client('s3', region_name='us-west-2')
    claims = aws_client.dynamodb.Table(CLAIMS_TABLE)

    def fetch(s3_path, reader):
        m = re.match(r's3://([^/]+)/(.+)', s3_path or '')
        if not m:
            return None
        with tempfile.NamedTemporaryFile(suffix='.pdf') as fh:
            s3.download_fileobj(m.group(1), m.group(2), fh)
            fh.flush()
            return reader(fh.name)

    return {
        'get_claim': lambda cid: claims.get_item(Key={'claim_id': cid}).get('Item'),
        'read_hcfa': lambda p: fetch(p, hcfa_lines_from_pdf),
        'read_eob': lambda p: fetch(p, read_eob_pdf),
    }


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
    readers = make_readers(aws_client)
    todo = []
    for it in scan_all(eobs):
        ck = str(it.get('check_eft', ''))
        if only and ck != only:
            continue
        if it.get('posted_in_ecw') and not only:
            continue
        if not it.get('eob_pdf_s3_path'):
            continue
        todo.append(it)
    todo.sort(key=lambda it: str(it.get('check_date', '')))
    logger.info(f"📋 {len(todo)} check(s) to consider")

    # Plan first, so a run with nothing ready never opens eCW.
    ready = []
    counts = {'posted': 0, 'dry_run': 0, 'existing': 0, 'held': 0, 'incomplete': 0, 'failed': 0}
    for it in todo:
        ck = str(it['check_eft'])
        try:
            plan = plan_from_item(it, **readers)
        except Exception as e:
            plan = {'status': 'needs_review', 'reasons': [f'planning failed: {e}'], 'claims': [], 'totals': {}}
        held = plan['reasons'] + [r for c in plan.get('claims', []) for r in (c.get('reasons') or [])]
        eobs.update_item(
            Key={'check_eft': ck},
            UpdateExpression='SET plan_status = :s, plan_reasons = :r, planned_at = :t',
            ExpressionAttributeValues={':s': plan['status'], ':r': len(held), ':t': _now()})
        if plan['status'] != 'ready':
            counts['held'] += 1
            logger.info(f"  ⏸ check {ck} held: {held[:3]}")
            _record(eobs, ck, {'status': 'held', 'reason': '; '.join(held)[:600]}, post)
            continue
        ready.append((it, plan))
    logger.info(f"🧮 {len(ready)} check(s) ready to enter, {counts['held']} held for review")
    if not ready:
        return dict({'ok': True, 'checks': len(todo)}, **counts)

    creds = aws_client.get_secret('ecw_credentials')
    if not login(page, creds, aws_client):
        logger.error("❌ eCW login failed — nothing entered")
        return {'ok': False, 'reason': 'login'}

    done = 0
    for it, plan in ready:
        if limit and done >= limit:
            break
        done += 1
        ck = str(it['check_eft'])
        try:
            res = post_check(page, it, plan, since, post)
        except Exception as e:
            res = {'status': 'failed', 'reason': str(e)[:300]}
            logger.error(f"  ❌ check {ck}: {e}")
            _shot(page, f'{ck}_error')
            cancel_popups(page)
        counts[res['status']] = counts.get(res['status'], 0) + 1
        logger.info(f"  → check {ck}: {res['status']} — {res.get('reason', '')}")
        _record(eobs, ck, res, post)
        if res['status'] in ('posted', 'existing'):
            for c in plan.get('claims', []):
                aws_client.update_claim_status(str(c['claim_id']), {
                    'payment_posted': True, 'payment_posted_at': _now(),
                    'payment_check_eft': ck, 'payment_post_status': res['status'],
                    'payment_ecw_id': res.get('payment_id', ''),
                })
    logger.info(f"═══ Remittance → eCW complete: {counts} ═══")
    return dict({'ok': True, 'checks': done}, **counts)


def _record(eobs, check, res, post):
    eobs.update_item(
        Key={'check_eft': check},
        UpdateExpression='SET posted_in_ecw = :p, post_result = :r, post_attempted_at = :t, post_mode = :m',
        ExpressionAttributeValues={':p': res['status'] in ('posted', 'existing'),
                                   ':r': dict(res, at=_now()), ':t': _now(),
                                   ':m': 'post' if post else 'dry_run'})
