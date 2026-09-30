"""eCW, read for denials: Billing → Claims with a denied status, and each
claim's reason codes.

The Claims lookup screen (claimLookup.jsp) has a Service Dt range, a Claim
Status dropdown and a Lookup button; the grid's rows are Angular-bound
(tr[ng-repeat*="lstClaimReport"]), so each row's fields come off its scope
(claimNo, patName, payerName, serviceDate, cpt, claimStatus, amount,
payment, NetAdj, balance) — the same way the claims bot reads them.

The denial reason is not in the grid. It is on the claim itself, in what the
payer's ERA/EOB posted: CARC group codes (CO-16), remark codes (N846), the
payer's letter codes (B16) and words like Duplicate or Non-Covered. The
claim is opened (its popup), the popup's text is read for those codes, and
the popup is closed with Cancel. Nothing is changed in eCW.
"""
import re
import time

from src.denials.cheatsheet import codes_in
from src.eob.post import _js, _click_text, _shot, _page_text
from src.utils.logger import get_logger

logger = get_logger(__name__)

CLAIMS_HASH = '/mobiledoc/jsp/webemr/webpm/claimLookup.jsp'
# eCW's own words for a denied or rejected claim (the SOP's Claim Status
# Codes sheet). The dropdown is read first; only the ones it has are used.
DENIED_STATUSES = ['ERA Payer Denied', 'EOB Payer Denied', 'Waiting for Denial', 'Requires further review',
                   'Insurance Rejected', 'Clearinghouse Rejected', '277 Rejected', '997 Rejected', 'Denied', 'Rejected']
# Tabs inside the claim popup that may hold the payer's codes, tried in
# order when the front page shows none. Tabs only — never a button that acts.
REASON_TABS = ['Payments', 'Claim Notes', 'Notes', 'History', 'Insurance']
ROW_SEL = 'tr[ng-repeat*="lstClaimReport"]'


def open_claims(page):
    logger.info("🧭 Billing → Claims")
    page.evaluate("(h) => { window.location.hash = h; }", CLAIMS_HASH)
    time.sleep(4)
    try:
        page.wait_for_load_state('networkidle', timeout=10000)
    except Exception:
        pass
    ok = bool(page.query_selector('input[id^="claimLookupIpt"]')) or 'claimLookup' in (page.url or '')
    logger.info(f"  {'✅ Claims screen is up' if ok else '⚠️ Claims screen not confirmed'} · {page.url[:120]}")
    return ok


def set_dates(page, since, until=None):
    """Service Dt from `since` to today: the first two date-looking inputs."""
    until = until or time.strftime('%m/%d/%Y')
    done = 0
    try:
        for inp in page.locator('input[type="text"]:visible').all()[:20]:
            try:
                val = inp.input_value()
            except Exception:
                continue
            if val and '/' in val and len(val) == 10:
                inp.click(click_count=3)
                inp.fill(since if done == 0 else until)
                inp.dispatch_event('change')
                inp.dispatch_event('blur')
                done += 1
                if done == 2:
                    break
        page.keyboard.press('Escape')
        time.sleep(0.4)
    except Exception as e:
        logger.warning(f"  ⚠️ dates: {e}")
    logger.info(f"  {'✅' if done == 2 else '⚠️'} Service Dt {since} → {until} ({done} of 2 fields set)")
    return done == 2


STATUS_JS = r"""([labels]) => {
    // The Claim Status dropdown: the <select> whose options include the
    // bot's own statuses. Returns its options, and selects `labels[0]` when
    // one is given (the first option whose text contains it).
    const sels = Array.from(document.querySelectorAll('select')).filter(vis);
    let pick = null;
    for (const s of sels) {
        const opts = Array.from(s.options).map(o => o.text.trim());
        const model = (s.getAttribute('ng-model') || '').toLowerCase();
        if (opts.some(t => /symplisend|denied|ready to submit/i.test(t)) || model.includes('claimstatus')) { pick = s; break; }
    }
    if (!pick) return {found: false, options: []};
    const opts = Array.from(pick.options).map(o => o.text.trim());
    if (!labels.length) return {found: true, options: opts, chose: opts[pick.selectedIndex] || ''};
    const want = labels[0].toLowerCase();
    let i = opts.findIndex(t => t.toLowerCase() === want);
    if (i < 0) i = opts.findIndex(t => t.toLowerCase().includes(want));
    if (i < 0) return {found: true, options: opts, chose: ''};
    pick.selectedIndex = i;
    pick.dispatchEvent(new Event('change', {bubbles: true}));
    const ng = window.angular && window.angular.element(pick);
    if (ng && ng.scope && ng.scope()) { try { ng.scope().$apply(); } catch (e) {} }
    return {found: true, options: opts, chose: opts[i]};
}"""


def status_options(page):
    got = page.evaluate(_js(STATUS_JS), [[]])
    return got.get('options', []) if got and got.get('found') else []


def set_status(page, label):
    got = page.evaluate(_js(STATUS_JS), [[label]])
    if not got or not got.get('found'):
        logger.warning("  ⚠️ no Claim Status dropdown on screen")
        return False
    if not got.get('chose'):
        logger.warning(f"  ⚠️ Claim Status has no {label!r}")
        return False
    logger.info(f"  ✅ Claim Status = {got['chose']}")
    return True


def lookup(page, wait_s=20):
    """Click Lookup (not Patient Lookup) and wait for the grid to change."""
    before = _grid_sig(page)
    clicked = False
    try:
        for btn in page.locator('button:has-text("Lookup"), input[type="button"][value*="Lookup"]').all():
            txt = btn.inner_text() if btn.evaluate('el => el.tagName') == 'BUTTON' else (btn.get_attribute('value') or '')
            if 'Patient' in txt or not btn.is_visible():
                continue
            btn.click()
            clicked = True
            break
    except Exception as e:
        logger.warning(f"  ⚠️ Lookup: {e}")
    if not clicked:
        clicked = _click_text(page, ['Lookup', 'Look up'], timeout=4, what='lookup')
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(0.5)
        if _grid_sig(page) != before:
            time.sleep(1.5)
            break
    return clicked


def _grid_sig(page):
    try:
        return page.evaluate("(sel) => Array.from(document.querySelectorAll(sel)).slice(0, 5).map(r => r.innerText).join('|') + '#' + document.querySelectorAll(sel).length", ROW_SEL)
    except Exception:
        return ''


ROWS_JS = r"""(sel) => {
    const rows = Array.from(document.querySelectorAll(sel));
    const out = [];
    for (const tr of rows) {
        let obj = null;
        try {
            const sc = window.angular && window.angular.element(tr).scope();
            if (sc) obj = sc.claim || sc.item || sc.row || sc.x || sc.c || null;
            if (!obj && sc) { for (const k of Object.keys(sc)) { const v = sc[k]; if (v && typeof v === 'object' && !Array.isArray(v) && ('claimNo' in v || 'patName' in v || 'claimStatus' in v)) { obj = v; break; } } }
        } catch (e) {}
        const cells = Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').replace(/\s+/g, ' ').trim());
        if (obj) {
            out.push({claim_id: String(obj.claimNo || obj.claimNumber || obj.claim_no || ''), patient: String(obj.patName || obj.patientName || ''),
                      payer: String(obj.payerName || obj.payer || obj.insuranceName || ''), dos: String(obj.serviceDate || obj.srvDate || obj.dos || ''),
                      cpt: String(obj.cpt || obj.cptCode || obj.procCode || ''), status: String(obj.claimStatus || obj.status || ''),
                      charges: String(obj.amount || obj.charges || obj.totalCharge || ''), paid: String(obj.payment || obj.payments || ''),
                      adjustment: String(obj.NetAdj || obj.adjustment || ''), balance: String(obj.balance || ''), cells});
        } else {
            out.push({cells});
        }
    }
    const hdrs = Array.from(document.querySelectorAll('th')).map(th => (th.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
    return {rows: out, hdrs};
}"""

COLS = {'claim_id': re.compile(r'claim\s*#|claim\s*no|invoice', re.I), 'patient': re.compile(r'patient|pat\s*name', re.I),
        'payer': re.compile(r'payer|insurance', re.I), 'dos': re.compile(r'service\s*d|dos', re.I), 'cpt': re.compile(r'cpt|proc', re.I),
        'status': re.compile(r'status', re.I), 'charges': re.compile(r'charge|amount|billed', re.I), 'paid': re.compile(r'paid|payment', re.I),
        'balance': re.compile(r'balance', re.I)}


def read_rows(page):
    """The grid's claims: {'claim_id', 'patient', 'payer', 'dos', 'cpt',
    'status', 'charges', 'paid', 'adjustment', 'balance'}. From each row's
    Angular scope; from the cells by header name when the scope gives none."""
    got = page.evaluate(_js(ROWS_JS), ROW_SEL) or {}
    hdrs = got.get('hdrs', [])
    idx = {}
    for key, rx in COLS.items():
        for i, h in enumerate(hdrs):
            if i not in idx.values() and rx.search(h):
                idx[key] = i
                break
    out = []
    for r in got.get('rows', []):
        if r.get('claim_id') or r.get('patient'):
            out.append({k: r.get(k, '') for k in ('claim_id', 'patient', 'payer', 'dos', 'cpt', 'status', 'charges', 'paid', 'adjustment', 'balance')})
            continue
        cells = r.get('cells', [])
        row = {k: (cells[i] if i < len(cells) else '') for k, i in idx.items()}
        row.setdefault('adjustment', '')
        if row.get('claim_id') or row.get('patient'):
            out.append({k: row.get(k, '') for k in ('claim_id', 'patient', 'payer', 'dos', 'cpt', 'status', 'charges', 'paid', 'adjustment', 'balance')})
    return out, hdrs


def list_denied(page, since, statuses=None):
    """Every claim under each denied status the dropdown offers. Returns
    (claims, statuses_used, options)."""
    if not open_claims(page):
        return [], [], []
    set_dates(page, since)
    options = status_options(page)
    logger.info(f"  Claim Status options ({len(options)}): {options[:40]}")
    wanted = statuses or DENIED_STATUSES
    used, out, seen = [], [], set()
    for label in wanted:
        if options and not any(label.lower() == o.lower() or label.lower() in o.lower() for o in options):
            continue
        if not set_status(page, label):
            continue
        lookup(page)
        rows, hdrs = read_rows(page)
        if not used:
            logger.info(f"  grid columns: {hdrs[:16]}")
        fresh = [r for r in rows if r.get('claim_id') and r['claim_id'] not in seen]
        for r in fresh:
            r['ecw_status_filter'] = label
            seen.add(r['claim_id'])
        out.extend(fresh)
        used.append(label)
        logger.info(f"  📋 {label}: {len(rows)} row(s), {len(fresh)} new")
    if not used:
        logger.warning(f"  ⚠️ none of {wanted} is a Claim Status here — the options are logged above; pass statuses:[...] with the right names")
    return out, used, options


def read_reasons(page, claim_id, open_claim, close_claim, shot=False):
    """The denial codes on one claim: open its popup, read its text, try the
    tabs that may hold the payer's codes, close it. (codes, text_snippet)."""
    ok = False
    try:
        got = open_claim(page, claim_id)
        ok = bool(got[0] if isinstance(got, tuple) else got)
    except Exception as e:
        logger.warning(f"  ⚠️ claim {claim_id}: could not be opened: {str(e)[:120]}")
    if not ok:
        return None, ''
    codes, snippet, where = [], '', 'claim'
    try:
        text = _page_text(page)
        codes = codes_in(text)
        if not codes:
            for tab in REASON_TABS:
                if _click_text(page, [tab], timeout=1.5, what='tab'):
                    time.sleep(1.2)
                    text = _page_text(page)
                    codes = codes_in(text)
                    if codes:
                        where = tab
                        break
        if shot:
            _shot(page, f'denial_claim_{claim_id}')
        snippet = _reason_snippet(text, codes)
        if codes:
            logger.info(f"  🧾 claim {claim_id}: {', '.join(codes[:8])} ({where})")
        else:
            logger.info(f"  🧾 claim {claim_id}: no reason code on the claim")
    finally:
        try:
            close_claim(page)
        except Exception:
            pass
    return codes, snippet


def _reason_snippet(text, codes):
    """A few lines of the popup around the first code, for the dashboard."""
    lines = [l.strip() for l in str(text or '').splitlines() if l.strip()]
    for i, l in enumerate(lines):
        if any(c.lower() in l.lower() for c in codes if len(c) > 2):
            return ' | '.join(lines[max(0, i - 1):i + 3])[:400]
    return ''
