"""
eCW, read for claim prioritization (the operator's "Claim Prioritization
Bot – Flow for Review", 2026-10-26).

    Billing → Claims: DOS from 07/01/2025 · All Claims · Balance > 0, every
    page. Each claim opened once: an ERA payer (Medicare, Anthem) is dated
    by the latest "Claim Status" row of its Billing Logs; any other payer
    (BSC, UHC, Cigna…) by the latest follow-up note. Read-only; the popup is
    closed with Cancel.

The screen pieces (Claim Status dropdown, pager, grid) are the ones the
Denials bot already reads; this module adds the Balance filter and the two
log views.
"""
import re
import time
from datetime import date, datetime

from src.denials.ecw import (open_claims, set_dates, set_page_size, set_status, lookup, read_all_pages,
                             _popup_text, _clean)
from src.eob.post import _js, _click_text, _shot
from src.utils.logger import get_logger

logger = get_logger(__name__)

ALL_CLAIMS = 'All Claims'
# Payers whose answer comes back as an ERA, so the claim's history is in its
# Billing Logs; everyone else is worked by hand and the history is the
# follow-up notes.
ERA_PAYER_RX = re.compile(r'medicare|anthem', re.I)
BILLING_LOG_TABS = ['Billing Logs', 'Billing Log', 'Claims Logs', 'Claim Logs', 'Logs']
FOLLOWUP_LOG_TABS = ['Follow Up Logs', 'Follow-Up Logs', 'Follow Up Log', 'Follow Up', 'F/U']
CLOSE_VIEW = ['Close']
DATE_RX = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b')

BALANCE_JS = r"""() => {
    // The Balance filter: the operator (>) select and the amount input that
    // sit right after the word "Balance". Set to > 0.
    const lab = Array.from(document.querySelectorAll('label, span, td, div'))
        .find(el => el.children.length === 0 && /^\s*balance\s*$/i.test(el.textContent || '') && vis(el));
    if (!lab) return {found: false};
    let box = lab.closest('tr, .form-group, .row, div');
    let sel = null, inp = null;
    for (let hop = 0; hop < 4 && box; hop++) {
        sel = Array.from(box.querySelectorAll('select')).find(s => vis(s) && Array.from(s.options).some(o => /^\s*>\s*$/.test(o.text)));
        inp = Array.from(box.querySelectorAll('input')).find(i => vis(i) && /^(text|number)?$/.test(i.type || '') && /^-?\d*\.?\d*$/.test((i.value || '').trim()));
        if (sel && inp) break;
        box = box.parentElement;
    }
    if (!sel || !inp) return {found: false};
    const i = Array.from(sel.options).findIndex(o => /^\s*>\s*$/.test(o.text));
    if (i >= 0) { sel.selectedIndex = i; sel.dispatchEvent(new Event('change', {bubbles: true})); }
    inp.value = '0.0'; inp.dispatchEvent(new Event('input', {bubbles: true})); inp.dispatchEvent(new Event('change', {bubbles: true}));
    const ng = window.angular && window.angular.element(inp);
    if (ng && ng.scope && ng.scope()) { try { ng.scope().$apply(); } catch (e) {} }
    return {found: true, op: sel.options[sel.selectedIndex].text.trim(), value: inp.value};
}"""


def set_balance_positive(page):
    try:
        got = page.evaluate(_js(BALANCE_JS)) or {}
    except Exception as e:
        got = {'found': False, 'error': str(e)[:100]}
    if got.get('found'):
        logger.info(f"  ✅ Balance {got.get('op')} {got.get('value')}")
    else:
        logger.warning("  ⚠️ Balance filter not found — the screen's default (> 0.0) is relied on")
    return bool(got.get('found'))


def list_open_claims(page, since):
    """Every claim with a balance under All Claims since `since`, every page."""
    if not open_claims(page):
        return [], []
    set_dates(page, since)
    set_page_size(page)
    if not set_status(page, ALL_CLAIMS):
        logger.warning("  ⚠️ 'All Claims' not set — the dropdown's current value stands")
    set_balance_positive(page)
    lookup(page)
    rows, hdrs = read_all_pages(page)
    rows = [r for r in rows if r.get('claim_id')]
    logger.info(f"  📋 All Claims · Balance > 0: {len(rows)} claim(s)")
    return rows, hdrs


def is_era_payer(payer):
    return bool(ERA_PAYER_RX.search(str(payer or '')))


def dates_in(text):
    out = []
    for m in DATE_RX.finditer(str(text or '')):
        try:
            out.append(date(int(m.group(3)), int(m.group(1)), int(m.group(2))))
        except ValueError:
            continue
    return out


def latest_status_date(text):
    """Billing Logs: the date on the latest line that mentions Claim Status."""
    best = None
    for line in str(text or '').splitlines():
        if re.search(r'claim\s*status', line, re.I):
            for d in dates_in(line):
                if best is None or d > best:
                    best = d
    return best


def latest_date(text, not_after=None):
    not_after = not_after or date.today()
    ds = [d for d in dates_in(text) if d <= not_after and d.year >= 2020]
    return max(ds) if ds else None


def _section_after(text, markers, length=4000):
    t = str(text or '')
    for mk in markers:
        i = t.find(mk)
        if i >= 0:
            return t[i:i + length]
    return t


def read_reference(page, claim, open_claim, close_claim, shot=False):
    """Open the claim and date it: {'ref_date', 'ref_source', 'snippet',
    'era_payer'}; ref_date is None when no log carried a date."""
    cid = str(claim['claim_id'])
    era = is_era_payer(claim.get('payer'))
    out = {'era_payer': era, 'ref_date': None, 'ref_source': '', 'snippet': ''}
    ok = False
    try:
        got = open_claim(page, cid)
        ok = bool(got[0] if isinstance(got, tuple) else got)
    except Exception as e:
        logger.warning(f"  ⚠️ claim {cid}: could not be opened: {str(e)[:120]}")
    if not ok:
        return None
    try:
        tabs = BILLING_LOG_TABS if era else FOLLOWUP_LOG_TABS
        opened = _click_text(page, tabs, timeout=2.5, what='log view')
        time.sleep(1.5 if opened else 0.3)
        text = _popup_text(page)
        section = _section_after(text, tabs) if opened else text
        if era:
            d = latest_status_date(section) or latest_date(section)
            src = 'Billing Logs · latest Claim Status row' if d else ''
        else:
            d = latest_date(section)
            src = 'Follow Up Logs · latest note' if d else ''
        if d is None:
            # No log on the claim: the claim's own dates, so it is still aged.
            d = latest_date(text)
            src = 'claim (no log read)' if d else ''
        out['ref_date'] = d
        out['ref_source'] = src
        lines = [l.strip() for l in section.splitlines() if l.strip() and (d is None or d.strftime('%m/%d/%Y') in l)]
        out['snippet'] = ' | '.join(lines[:3])[:300]
        if shot:
            _shot(page, f'priority_claim_{cid}')
        logger.info(f"  🗓 claim {cid} ({'ERA' if era else 'manual'} payer {claim.get('payer', '')[:28]}): "
                    f"{d.isoformat() if d else 'no date'} — {src or 'nothing read'}"
                    + (f" · {out['snippet'][:90]}" if out['snippet'] else ''))
        if opened and not _click_text(page, CLOSE_VIEW, timeout=1.0, what='close view'):
            pass
    finally:
        try:
            close_claim(page)
        except Exception:
            pass
    return out
