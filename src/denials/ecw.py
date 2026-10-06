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

As the operator showed it (2026-09-30): Claim Status "ERA PAYER DENIED" lists
709 claims, 20 a page over 36 pages, and the grid has no CPT column (COLL ·
CLAIM # · SERVICE DATE · PVDR · PATIENT · PAYER · STATUS · CHARGES · PMTS/ADJS
· ADJUSTMENT · WITHHELD · BALANCE). Inside the claim, the "ICD & CPT" tab
holds the codes billed and the "Insurances & Payment" tab the Payments /
Adjustments / Refunds grid whose Code column is the payer's reason; "View
CPT Pmts" opens the same per line. So every page of the grid is read, and
every claim is opened for both tabs.
"""
import re
import time

from src.denials.cheatsheet import codes_in, procedure_codes
from src.eob.post import _js, _click_text, _shot, _page_text
from src.utils.logger import get_logger

logger = get_logger(__name__)

CLAIMS_HASH = '/mobiledoc/jsp/webemr/webpm/claimLookup.jsp'
# The status the operator named (2026-09-30): "ERA PAYER DENIED, y se entra
# al claim". That one alone is read by default. eCW's other denied or
# rejected statuses (the SOP's Claim Status Codes sheet) are read only when
# the task names them: statuses: [...] or statuses: "all".
DENIED_STATUSES = ['ERA Payer Denied']
OTHER_DENIED_STATUSES = ['EOB Payer Denied', 'Waiting for Denial', 'Requires further review',
                         'Insurance Rejected', 'Clearinghouse Rejected', '277 Rejected', '997 Rejected', 'Denied', 'Rejected']
# Inside the claim popup: the tab with the codes billed, the tab with the
# payments grid (its Code column is the payer's reason), and the view that
# lists the same per CPT line. Tabs and views only — never a button that acts.
CPT_TAB = ['ICD & CPT', 'ICD and CPT']
PAYMENT_TABS = ['Insurances & Payment', 'Insurance & Payment', 'Payments']
CPT_PMTS_VIEW = ['View CPT Pmts']
CLOSE_VIEW = ['Close']
# When nothing is posted on the claim (an ERA PAYER DENIED claim whose 835
# was never posted: empty payments grid, empty CPT Payment view — claim 2187,
# 2026-10-06), the only words about the denial are the people's: the Billing
# Notes panel and the Claims Logs / Error tabs of the Summary box. Read-only.
NOTE_TABS = ['Claims Logs', '*Error', 'Error']
MAX_PAGES = 60
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
    // Headers: the rows' own table first; else the header table (floatThead
    // clone) with as many columns as a row has cells. Every <th> on the page
    // mixed in a session table ("Device · Login Time · Log Out Time") and
    // shifted every column by three.
    const clean = th => (th.innerText || '').replace(/\s+/g, ' ').trim();
    const width = rows.length ? rows[0].querySelectorAll('td').length : 0;
    let hdrs = [];
    const own = rows.length ? rows[0].closest('table') : null;
    if (own) hdrs = Array.from(own.querySelectorAll('th')).map(clean);
    if (!hdrs.some(Boolean) || (width && hdrs.length !== width)) {
        const tables = Array.from(document.querySelectorAll('table')).map(t => Array.from(t.querySelectorAll('th')).map(clean)).filter(h => h.length);
        const same = tables.find(h => h.length === width) || tables.find(h => h.some(x => /claim\s*#/i.test(x)));
        if (same) hdrs = same;
    }
    return {rows: out, hdrs, width};
}"""

COLS = {'claim_id': re.compile(r'claim\s*#|claim\s*no|invoice', re.I), 'patient': re.compile(r'patient|pat\s*name', re.I),
        'payer': re.compile(r'payer|insurance', re.I), 'dos': re.compile(r'service\s*d|dos', re.I), 'cpt': re.compile(r'cpt|proc', re.I),
        'status': re.compile(r'status', re.I), 'charges': re.compile(r'charge|amount|billed', re.I), 'paid': re.compile(r'paid|payment', re.I),
        'balance': re.compile(r'balance', re.I)}


KEYS = ('claim_id', 'patient', 'payer', 'dos', 'cpt', 'status', 'charges', 'paid', 'adjustment', 'balance')
CLAIM_RX = re.compile(r'^\d{1,8}$')
DATE_RX = re.compile(r'^\d{1,2}/\d{1,2}/\d{2,4}$')
MONEY_RX = re.compile(r'^-?\$?\s*[\d,]*\.\d{2}$')
NAME_RX = re.compile(r'^[A-Za-z][A-Za-z\'\-. ]+,\s*[A-Za-z]')


def header_map(hdrs):
    idx = {}
    for key, rx in COLS.items():
        for i, h in enumerate(hdrs):
            if i not in idx.values() and rx.search(h):
                idx[key] = i
                break
    return idx


def infer_cols(cell_rows):
    """Which cell is which, from what the cells hold — the claim number is
    the integer column, the DOS the date column, the patient the "Last,
    First" column, the payer the text after it, the status the text after
    the payer, the money columns charges · paid · adjustment · withheld ·
    balance in the grid's order. Used when the headers do not line up."""
    rows = [r for r in cell_rows if r]
    if not rows:
        return {}
    width = max(len(r) for r in rows)
    def share(i, rx):
        vals = [r[i] for r in rows if i < len(r) and r[i]]
        return (sum(1 for v in vals if rx.match(v)) / len(vals)) if vals else 0
    idx = {}
    ints = [i for i in range(width) if share(i, CLAIM_RX) >= 0.6]
    if ints:
        idx['claim_id'] = ints[0]
    dates = [i for i in range(width) if share(i, DATE_RX) >= 0.6]
    if dates:
        idx['dos'] = dates[0]
    names = [i for i in range(width) if share(i, NAME_RX) >= 0.5 and i not in idx.values()]
    if names:
        idx['patient'] = names[0]
        texts = [i for i in range(names[0] + 1, width) if i not in idx.values() and share(i, MONEY_RX) < 0.3
                 and sum(1 for r in rows if i < len(r) and r[i]) >= len(rows) * 0.5]
        if texts:
            idx['payer'] = texts[0]
        if len(texts) > 1:
            idx['status'] = texts[1]
    money = [i for i in range(width) if share(i, MONEY_RX) >= 0.6 and i not in idx.values()]
    for key, i in zip(('charges', 'paid', 'adjustment', 'withheld', 'balance'), money):
        if key == 'withheld':
            continue
        idx[key] = i
    if len(money) >= 2:
        idx['balance'] = money[-1]
    return idx


def read_rows(page):
    """The grid's claims: {'claim_id', 'patient', 'payer', 'dos', 'cpt',
    'status', 'charges', 'paid', 'adjustment', 'balance'}. From each row's
    Angular scope; else from the cells by header name; else by what the
    cells hold (infer_cols) when the headers do not line up with the cells."""
    got = page.evaluate(_js(ROWS_JS), ROW_SEL) or {}
    hdrs = got.get('hdrs', [])
    raw = got.get('rows', [])
    scoped = [{k: r.get(k, '') for k in KEYS} for r in raw if r.get('claim_id')]
    if scoped and len(scoped) >= len(raw) * 0.8:
        return scoped, hdrs
    cell_rows = [r.get('cells', []) for r in raw]
    idx = header_map(hdrs)
    def apply(idx):
        out = []
        for cells in cell_rows:
            row = {k: (cells[i] if i < len(cells) else '') for k, i in idx.items()}
            out.append({k: row.get(k, '') for k in KEYS})
        return out
    rows = apply(idx)
    good = sum(1 for r in rows if CLAIM_RX.match(r['claim_id']))
    if cell_rows and good < len(cell_rows) * 0.6:
        inferred = infer_cols(cell_rows)
        if inferred.get('claim_id') is not None:
            logger.info(f"  (headers {hdrs[:16]} do not line up with {got.get('width')} cells — columns taken from the cells: {inferred})")
            idx = inferred
            rows = apply(idx)
    rows = [r for r in rows if r['claim_id'] or r['patient']]
    if rows:
        logger.info(f"  first row: {rows[0]}")
    return rows, hdrs


PAGE_JS = r"""() => {
    // 'Page [1] of 36' (the page number may sit in an <input>), '1 - 50 of 709',
    // and the Next control — a button, a link, an icon with a title, or '»' / '>'.
    const body = (document.body && document.body.innerText) || '';
    let page = 0, pages = 0;
    let m = body.match(/Page\s*(\d+)?\s*of\s*(\d+)/i);
    if (m) { pages = +m[2]; page = m[1] ? +m[1] : 0; }
    if (!page) {
        const inp = Array.from(document.querySelectorAll('input')).find(i => vis(i) && /^\d{1,4}$/.test((i.value || '').trim())
            && /of\s*\d+/i.test(((i.parentElement && i.parentElement.innerText) || '')));
        if (inp) page = +inp.value;
    }
    if (!pages) {
        const r = body.match(/(\d+)\s*-\s*(\d+)\s*of\s*(\d+)/i);
        if (r) { const per = +r[2] - +r[1] + 1; pages = per > 0 ? Math.ceil(+r[3] / per) : 0; page = per > 0 ? Math.ceil(+r[2] / per) : 0; }
    }
    const isNext = el => {
        const t = ((el.innerText || el.value || '') + ' ' + (el.title || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.className || '') + ' ' + (el.id || '')).toLowerCase();
        return /\bnext\b|»|^\s*>\s*$|chevron-right|angle-right|fa-forward|pagination-next|nextpage/.test(t) && !/last|prev|previous|«/.test(t.replace(/next/g, ''));
    };
    const cands = Array.from(document.querySelectorAll('button, a, input[type="button"], span[ng-click], i[ng-click], li[ng-click] > a, [class*="next"]')).filter(vis).filter(isNext);
    const next = cands[0] || null;
    const disabled = !!next && (next.disabled || /disabled/.test(next.className || '') || /disabled/.test((next.parentElement && next.parentElement.className) || ''));
    return {page, pages, next: !!next && !disabled, pager: body.match(/[^\n]*\bof\s+\d+[^\n]*/i) ? body.match(/[^\n]*\bof\s+\d+[^\n]*/i)[0].trim().slice(0, 120) : ''};
}"""


def set_page_size(page):
    """No. of Result → its largest option, so 709 claims are 8 pages, not 36."""
    try:
        got = page.evaluate(_js(r"""() => {
            for (const s of Array.from(document.querySelectorAll('select')).filter(vis)) {
                const opts = Array.from(s.options).map(o => o.text.trim());
                if (opts.length >= 2 && opts.every(t => /^\d+$/.test(t))) {
                    const i = opts.map(Number).indexOf(Math.max(...opts.map(Number)));
                    s.selectedIndex = i; s.dispatchEvent(new Event('change', {bubbles: true}));
                    const ng = window.angular && window.angular.element(s);
                    if (ng && ng.scope && ng.scope()) { try { ng.scope().$apply(); } catch (e) {} }
                    return opts[i];
                }
            }
            return '';
        }"""))
        if got:
            logger.info(f"  ✅ No. of Result = {got}")
            time.sleep(0.5)
        return got
    except Exception:
        return ''


NEXT_JS = r"""() => {
    const isNext = el => {
        const t = ((el.innerText || el.value || '') + ' ' + (el.title || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.className || '') + ' ' + (el.id || '')).toLowerCase();
        return /\bnext\b|»|^\s*>\s*$|chevron-right|angle-right|fa-forward|pagination-next|nextpage/.test(t) && !/last|prev|previous|«/.test(t.replace(/next/g, ''));
    };
    const el = Array.from(document.querySelectorAll('button, a, input[type="button"], span[ng-click], i[ng-click], li[ng-click] > a, [class*="next"]')).filter(vis).find(isNext);
    if (!el) return false;
    el.click();
    return true;
}"""


def _wait_grid(page, before, timeout_s=20):
    """eCW empties the grid and hides the pager while the next page loads.
    Wait until rows are back, differ from `before`, and hold still for two
    polls — reading in the gap gave an empty page and no Next button."""
    deadline = time.time() + timeout_s
    last, still = None, 0
    while time.time() < deadline:
        time.sleep(0.5)
        sig = _grid_sig(page)
        if sig and not sig.endswith('#0') and sig != before:
            still = still + 1 if sig == last else 0
            if still >= 1:
                return True
        last = sig
    return False


def next_page(page):
    """Click Next and wait for the next page's grid. False on the last page
    or when nothing moved after two tries."""
    for attempt in range(2):
        info = {}
        for _ in range(6):            # the pager hides while a page loads
            info = page.evaluate(_js(PAGE_JS)) or {}
            if info.get('next') or (info.get('pages') and info.get('page')):
                break
            time.sleep(0.5)
        if not info.get('next') or (info.get('pages') and info.get('page', 0) >= info['pages']):
            return False
        before = _grid_sig(page)
        if not _click_text(page, ['Next'], timeout=2, what='next page') and not page.evaluate(_js(NEXT_JS)):
            return False
        if _wait_grid(page, before):
            return True
        logger.warning(f"  ⚠️ the grid did not change after Next (try {attempt + 1}/2)")
    return False


def read_all_pages(page):
    rows, hdrs = read_rows(page)
    info = page.evaluate(_js(PAGE_JS)) or {}
    pages = info.get('pages') or 0
    logger.info(f"  📄 page {info.get('page') or 1} of {pages or '?'} · next {'yes' if info.get('next') else 'no'}"
                + (f" · pager: \"{info['pager']}\"" if info.get('pager') else ''))
    # Without a page count, follow Next while the grid keeps changing.
    for n in range((min(pages, MAX_PAGES) - 1) if pages else MAX_PAGES):
        if not next_page(page):
            break
        more, _h = read_rows(page)
        for _ in range(6):                     # a page read in the loading gap is empty: look again
            if more:
                break
            time.sleep(1.0)
            more, _h = read_rows(page)
        if not more:
            logger.warning(f"  ⚠️ page {n + 2}: no rows read — stopping here")
            break
        rows.extend(more)
        if (n + 2) % 5 == 0 or (pages and n + 2 == pages):
            logger.info(f"  📄 page {n + 2}{f' of {pages}' if pages else ''} · {len(rows)} row(s) so far")
    if pages and len(rows) < 50 * (pages - 1):
        logger.warning(f"  ⚠️ {len(rows)} row(s) over {pages} page(s) is fewer than expected — some pages were not read")
    if len(rows) and (pages > 1 or info.get('next')):
        logger.info(f"  📄 {len(rows)} row(s) read over the pages")
    return rows, hdrs


def list_denied(page, since, statuses=None):
    """Every claim under each denied status the dropdown offers, every page
    of the grid. Returns (claims, statuses_used, options)."""
    if not open_claims(page):
        return [], [], []
    set_dates(page, since)
    set_page_size(page)
    options = status_options(page)
    logger.info(f"  Claim Status options ({len(options)}): {options[:40]}")
    if statuses == 'all':
        wanted = DENIED_STATUSES + OTHER_DENIED_STATUSES
    else:
        wanted = statuses or DENIED_STATUSES
    used, out, seen = [], [], set()
    for label in wanted:
        if options and not any(label.lower() == o.lower() or label.lower() in o.lower() for o in options):
            continue
        if not set_status(page, label):
            continue
        lookup(page)
        rows, hdrs = read_all_pages(page)
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
    """One claim, opened once: the codes billed (ICD & CPT tab) and the
    payer's reason codes (Insurances & Payment → the payments grid's Code
    column, then View CPT Pmts). Closed with Cancel. Returns None when the
    claim could not be opened, else {'codes', 'cpt', 'snippet', 'where'}."""
    ok = False
    try:
        got = open_claim(page, claim_id)
        ok = bool(got[0] if isinstance(got, tuple) else got)
    except Exception as e:
        logger.warning(f"  ⚠️ claim {claim_id}: could not be opened: {str(e)[:120]}")
    if not ok:
        return None
    out = {'codes': [], 'cpt': [], 'snippet': '', 'where': ''}
    try:
        front = _page_text(page)
        if _click_text(page, CPT_TAB, timeout=2, what='tab'):
            tab = _wait_text(page, lambda t: bool(_cpts_on(_cpt_region(t))), 4.0)
            out['cpt'] = _cpts_on(_cpt_region(tab))
        if not out['cpt']:
            out['cpt'] = _cpts_on(_cpt_region(front))
        text = front
        if _click_text(page, PAYMENT_TABS, timeout=2, what='tab'):
            text = _wait_text(page, lambda t: 'Payments / Adjustments' in t or 'Adjustments' in t, 4.0)
            out['where'] = 'payments'
        out['codes'] = codes_in(_clean(_after(text, 'Payments / Adjustments')))
        if not out['codes'] and _click_text(page, CPT_PMTS_VIEW, timeout=2, what='view'):
            time.sleep(1.5)
            view = _page_text(page)
            out['codes'] = codes_in(_clean(_after(view, 'CPT')))
            out['where'] = 'View CPT Pmts'
            text = view
            if not _click_text(page, CLOSE_VIEW, timeout=1.5, what='close view'):
                page.keyboard.press('Escape')
            time.sleep(0.5)
        if not out['codes']:
            out['codes'] = codes_in(_clean(front))
            out['where'] = 'claim' if out['codes'] else ''
        # Nothing posted: keep what people wrote about it (Billing Notes, the
        # Summary box's Claims Logs / Error tabs) so the row says something.
        if not out['codes']:
            notes = _section(text, 'Billing Notes', 600)
            for tab in NOTE_TABS:
                if _click_text(page, [tab], timeout=1.5, what='summary tab'):
                    time.sleep(1.0)
                    notes = (notes + ' | ' + _section(_page_text(page), tab.strip('*'), 600)).strip(' |')
                    break
            if notes:
                out['notes'] = notes[:1200]
                out['where'] = 'nothing posted — notes'
                out['codes'] = codes_in(_clean(notes))
        if shot:
            _shot(page, f'denial_claim_{claim_id}')
        out['snippet'] = _reason_snippet(text, out['codes'])
        logger.info(f"  🧾 claim {claim_id}: codes {', '.join(out['codes'][:8]) or 'none'} · billed {', '.join(out['cpt'][:6]) or '?'}"
                    + (f" ({out['where']})" if out['where'] else '')
                    + (f" · notes: {out['notes'][:120]}" if out.get('notes') else ''))
    finally:
        try:
            close_claim(page)
        except Exception:
            pass
    return out


def _section(text, marker, length=600):
    """The text right after `marker`, trimmed and on one line."""
    t = str(text or '')
    i = t.find(marker)
    if i < 0:
        return ''
    body = t[i + len(marker):i + len(marker) + length]
    body = re.sub(r'\s+', ' ', body).strip()
    return body if len(body) > 3 else ''


def _after(text, marker):
    i = str(text or '').find(marker)
    return str(text or '')[i:] if i >= 0 else str(text or '')


MODIFIER_HDR_RX = re.compile(r'\bM1\s+M2\s+M3\s+M4\b', re.I)
MONEY_TXT_RX = re.compile(r'-?\$?\b\d{1,3}(?:,\d{3})+(?:\.\d{2})?\b|-?\$?\b\d+\.\d{2}\b')
DATE_TXT_RX = re.compile(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b')


def _clean(text):
    """The popup's text with what reads as a code but is not: the CPT grid's
    modifier headers (M1 M2 M3 M4 — a run read them as remark codes on every
    claim), money (1,282.50 gave "282"), dates (12/29/2025 gave "29")."""
    t = MODIFIER_HDR_RX.sub(' ', str(text or ''))
    t = DATE_TXT_RX.sub(' ', t)
    t = MONEY_TXT_RX.sub(' ', t)
    return t


def _cpt_region(text):
    """The ICD & CPT tab's part of the popup text, from its first CPT marker."""
    t = str(text or '')
    for marker in ('CPT Code', 'CPT/HCPCS', 'ICD & CPT', 'CPT'):
        i = t.find(marker)
        if i >= 0:
            return t[i:]
    return t


def _wait_text(page, ready, timeout_s=4.0):
    """The popup's text once `ready(text)` holds, or whatever it says at the
    end of `timeout_s` — tabs fill a moment after the click."""
    deadline = time.time() + timeout_s
    text = _page_text(page)
    while not ready(text) and time.time() < deadline:
        time.sleep(0.5)
        text = _page_text(page)
    return text


def _no_dates(text):
    """12/29/2025 would read as the code 29 (a number after a slash)."""
    return re.sub(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b', ' ', str(text or ''))


def _cpts_on(text):
    """Procedure codes on a claim tab: HCPCS/CPT, not ICD (E11.9 has a dot),
    not the claim, account or subscriber numbers (longer), not money or
    dates, and not a 5-digit ZIP (a number followed by nothing but a line end
    after a state code)."""
    t = _clean(str(text or ''))
    t = re.sub(r'\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b', ' ', t)      # "CA 94110" — an address, not a code
    return procedure_codes(re.sub(r'\b\d{6,}\b', ' ', t))


def _reason_snippet(text, codes):
    """A few lines of the popup around the first code, for the dashboard."""
    lines = [l.strip() for l in str(text or '').splitlines() if l.strip()]
    for i, l in enumerate(lines):
        if any(c.lower() in l.lower() for c in codes if len(c) > 2):
            return ' | '.join(lines[max(0, i - 1):i + 3])[:400]
    return ''
