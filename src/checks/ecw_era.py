"""The 835s eCW holds and nobody has posted — Billing → ERA, Posting Status
UnPosted, Filter.

An ERA is the payer's electronic remittance advice: the payer says "this
payment covers these claims". eCW having the 835 means the payer sent the
money; it does not mean Helixona has the check in hand, and an UnPosted ERA
means nobody in the office has applied it either (2026-09-21, the operator:
"ECW has the 835 files. But Helixona has not confirmed whether they have the
checks or not").

The grid, as eCW draws it (screenshot, 2026-09-21):

    STATUS · FILE · CHECK · PAYER · POSTED BY · POSTED DATE · METHOD ·
    ACTION · DATED · TRACE# · AMOUNT

**TRACE# is the check number** (the operator, 2026-09-21: "in ERA this is
the column where the Check Number is"). In an 835 the trace number (TRN02)
is the check or EFT the payer issued — 31407766, 31385053 — the same shape
as the Check/EFT numbers the Blue Shield portal lists. FILE and CHECK are
eCW's own sequence for the imported file (both read 664 on the same row),
so they identify the ERA inside eCW but say nothing about the paper. Both
are kept: the trace is what the sources meet on, the file number is what a
person types into eCW to find the row again.

Every page is read, not the first: a check that matches a trace number on
page 37 is as much "in eCW, unconfirmed at Helixona" as one on page 1.

Read only. Nothing here posts, imports or marks anything.
"""
import re
import time

from src.eob.post import _js, _click_text, _shot, _page_has, _menu_items, _hover_text, _where, route_in
from src.checks.ecw_payments import _read_grid, _grid_signature, describe_screen
from src.utils.logger import get_logger

logger = get_logger(__name__)

ERA_MARKERS = ('ERA Process', 'Filter ERA', 'Posting Status', 'Import ERA')
# The menu item, by its text or by the route in its handler.
ERA_ITEM_RX = r'^era\b|electronic\s*remittance|/era/|era[A-Z_]\w*\.jsp'

# Billing → ERA. The menu is tried first; a route that works is logged, so a
# screen the bot could not reach today is one it can go to directly tomorrow.
ERA_HASHES = [
    # The route the Billing → ERA menu item took on 2026-09-24 (logged by
    # open_era: openURL('#/mobiledoc/jsp/webemr/webpm/era/ERAListView.jsp')).
    '/mobiledoc/jsp/webemr/webpm/era/ERAListView.jsp',
    '/mobiledoc/jsp/webemr/webpm/era/eraListView.jsp',
    '/mobiledoc/jsp/webemr/webpm/eraProcess.jsp',
    '/mobiledoc/jsp/webemr/webpm/era.jsp',
]
STATUS_RX = r'posting\s*status|^status$'
UNPOSTED_LABELS = ('UnPosted', 'Unposted', 'Un-Posted', 'UNPOSTED')
FILTER_LABELS = ['Filter', 'Search', 'Lookup']
# The filter is left as the team has it — Posting Status UnPosted and
# nothing else touched (the operator, 2026-09-24: "no puedes modificar ese
# filtro, déjalo como unposted y revisa las que hagan match").
GRID_WAIT_S = 15
NEXT_LABELS = ['Next', '>', 'Next Page']

COLUMNS = {
    # The trace number first: it is the one that means a check number.
    'trace': re.compile(r'trace', re.I),
    'era_file': re.compile(r'^file', re.I),
    'era_check': re.compile(r'^check$|^check\s*(no|#)', re.I),
    'payer': re.compile(r'^payer|payment\s*from|insurance', re.I),
    'posted_by': re.compile(r'posted\s*by', re.I),
    'posted_date': re.compile(r'posted\s*date', re.I),
    'method': re.compile(r'^method|^type', re.I),
    'dated': re.compile(r'^dated|^date$|check\s*date', re.I),
    'amount': re.compile(r'^amount|^amt', re.I),
    'status': re.compile(r'^status', re.I),
}


def open_era(page, wait_for_person=90):
    """Billing → ERA. True once the screen is up."""
    logger.info("🧭 Billing → ERA")
    if _page_has(page, ERA_MARKERS):
        logger.info(f"  ✅ the ERA screen is already up · {_where(page)}")
        return True
    routes = list(ERA_HASHES)
    if _click_text(page, ['Billing'], timeout=6, what='menu'):
        time.sleep(1.5)
        _hover_text(page, 'Billing')
        if not _click_text(page, ['ERA'], timeout=4, what='menu'):
            # The item is in the document before it is on screen. The click
            # goes to the element with the handler; whatever route it names
            # is tried directly too.
            hit = _menu_items(page, ERA_ITEM_RX, click=True, exact=r'^era$')
            if hit:
                logger.info(f"  ✅ clicked menu item {hit['text']!r} <{hit.get('tag', '?')}> "
                            f"({'showing' if hit['visible'] else 'hidden'}) {hit['attrs'][:120]}")
                if route_in(hit['attrs']) and route_in(hit['attrs']) not in routes:
                    routes.insert(0, route_in(hit['attrs']))
        time.sleep(4)
    if _page_has(page, ERA_MARKERS):
        logger.info(f"  ✅ the ERA screen is up · {_where(page)}")
        return True
    # What the menu holds, for the log: the route the bot should take is
    # in one of these lines when the click above did not take it there.
    items = _menu_items(page, ERA_ITEM_RX) or []
    for it in items[:12]:
        logger.info(f"  menu item {it['text']!r} <{it.get('tag', '?')}> {'showing' if it['visible'] else 'hidden'} · {it['attrs'][:140]}")
        r = route_in(it['attrs'])
        if r and r not in routes:
            routes.append(r)
    for h in routes:
        try:
            page.evaluate("(h) => { window.location.hash = h; }", h)
        except Exception:
            continue
        time.sleep(4)
        if _page_has(page, ERA_MARKERS):
            logger.info(f"  ✅ the ERA screen via #{h}")
            return True
    if wait_for_person:
        logger.warning(f"  ⚠️ the bot did not find Billing → ERA — open it on the live screen (noVNC); "
                       f"waiting up to {wait_for_person}s")
        deadline = time.time() + wait_for_person
        while time.time() < deadline:
            if _page_has(page, ERA_MARKERS):
                logger.info(f"  ✅ the ERA screen is up (opened on the live screen) · {_where(page)} "
                            f"— that route belongs in ERA_HASHES")
                return True
            time.sleep(3)
    _shot(page, 'no_era_screen')
    logger.error("  ❌ the ERA screen was not reached")
    return False


SELECT_JS = r"""(([rx, want]) => {
    const re = new RegExp(rx, 'i');
    const labelOf = (el) => {
        const id = el.getAttribute('id');
        if (id) {
            const lab = document.querySelector(`label[for="${CSS.escape(id)}"]`);
            if (lab && lab.textContent) return lab.textContent;
        }
        const cell = el.closest('td');
        const prev = cell && cell.previousElementSibling;
        if (prev && prev.textContent) return prev.textContent;
        const near = el.closest('div, tr');
        return near ? (near.textContent || '').slice(0, 80) : '';
    };
    for (const sel of Array.from(document.querySelectorAll('select'))) {
        if (!re.test(labelOf(sel)) && !re.test(sel.getAttribute('ng-model') || '')) continue;
        const opts = Array.from(sel.options).map(o => (o.textContent || '').trim());
        const i = opts.findIndex(t => want.some(w => t.toLowerCase() === String(w).toLowerCase()));
        if (i < 0) return {ok: false, options: opts.slice(0, 20)};
        sel.selectedIndex = i;
        sel.dispatchEvent(new Event('change', {bubbles: true}));
        const ng = window.angular && window.angular.element(sel);
        if (ng && ng.scope && ng.scope()) { try { ng.scope().$apply(); } catch (e) {} }
        return {ok: true, chose: opts[i]};
    }
    return {ok: false, options: null};
})"""


def set_posting_status(page, labels=UNPOSTED_LABELS):
    """Posting Status → UnPosted. True when the dropdown took it."""
    for frm in [page] + list(page.frames):
        try:
            got = frm.evaluate(_js(SELECT_JS), [STATUS_RX, list(labels)])
        except Exception:
            continue
        if got and got.get('ok'):
            logger.info(f"  ✅ Posting Status = {got.get('chose')}")
            return True
        if got and got.get('options'):
            logger.warning(f"  ⚠️ the Posting Status list has no UnPosted: {got['options']}")
            return False
    logger.warning("  ⚠️ no Posting Status dropdown on screen")
    return False


def rows_to_era(hdrs, rows):
    """The grid's rows as ERA records. The check number is the trace number;
    a row without one is left out, since it is nothing the three systems can
    be compared on."""
    cols = {}
    for key, rx in COLUMNS.items():
        for i, h in enumerate(hdrs):
            if i in cols.values():
                continue
            if rx.search(h or ''):
                cols[key] = i
                break
    out = []
    for cells in rows:
        cell = lambda k: (cells[cols[k]].strip() if k in cols and cols[k] < len(cells) else '')
        trace = re.sub(r'\D', '', cell('trace'))
        amount = cell('amount').replace('$', '').replace(',', '').strip()
        if not re.fullmatch(r'\d{4,15}', trace):
            continue
        if not re.fullmatch(r'-?\d+(\.\d{1,2})?', amount or ''):
            amount = ''
        out.append({
            'check_no': trace,
            'era_file': cell('era_file'),
            'era_check': cell('era_check'),
            'payer': cell('payer'),
            'method': cell('method'),
            'dated': cell('dated'),
            'amount': amount,
            'status': cell('status'),
            'posted_by': cell('posted_by'),
        })
    return out


CONTROLS_JS = r"""() => {
    const near = el => {
        const id = el.getAttribute('id');
        const lab = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
        if (lab) return txt(lab);
        const p = el.closest('label, td, li, div');
        return p ? txt(p).slice(0, 60) : '';
    };
    const boxes = Array.from(document.querySelectorAll('input[type="checkbox"], input[type="radio"]'))
        .filter(vis).map(el => `${el.type}[${el.checked ? 'x' : ' '}] ${near(el)} (${el.getAttribute('ng-model') || el.id || el.name || ''})`);
    const dates = Array.from(document.querySelectorAll('input'))
        .filter(el => vis(el) && /date|dt/i.test((el.getAttribute('ng-model') || '') + (el.id || '') + (el.name || '') + (el.placeholder || '')))
        .map(el => `${near(el)} = ${el.value} (${el.getAttribute('ng-model') || el.id || el.name || ''})`);
    return {boxes: boxes.slice(0, 30), dates: dates.slice(0, 12)};
}"""


def _log_filter_controls(page):
    """What the filter panel holds — for the log, when a control the bot
    expected is not there: the checkboxes and date fields as labelled."""
    for frm in [page] + list(page.frames):
        try:
            got = frm.evaluate(_js(CONTROLS_JS))
        except Exception:
            continue
        if got and (got.get('boxes') or got.get('dates')):
            logger.info(f"  filter controls · checkboxes: {got.get('boxes')}")
            logger.info(f"  filter controls · dates: {got.get('dates')}")
            return


def _next_page(page):
    """Click Next and wait for the grid to answer. False when there is no
    next page, or the grid did not change."""
    before = _grid_signature(page)
    if not _click_text(page, NEXT_LABELS, timeout=3, what='next page'):
        return False
    deadline = time.time() + 12
    while time.time() < deadline:
        time.sleep(0.3)
        if _grid_signature(page) != before:
            return True
    return False


def list_unposted(page, navigate=True, max_pages=60, shot=False):
    """Every unposted ERA eCW holds, page by page. None when the screen could
    not be worked — which is not the same as "there are none"."""
    if navigate and not open_era(page):
        return None
    set_posting_status(page)
    _click_text(page, FILTER_LABELS, timeout=6, what='filter', after_target=True)
    # The grid fills after the filter answers; an empty grid read too soon
    # would be taken for "no unposted 835s" (2026-09-24: 0 rows in 2s).
    deadline = time.time() + GRID_WAIT_S
    hdrs, rows, recognised = [], [], False
    while time.time() < deadline:
        time.sleep(1)
        hdrs, rows, recognised = _read_grid(page)
        if rows:
            break
    seen, out = set(), []
    for page_no in range(1, max_pages + 1):
        if page_no > 1:
            hdrs, rows, recognised = _read_grid(page)
        if not recognised and not rows:
            if page_no == 1:
                _shot(page, 'era_grid_unread')
                logger.error("  ❌ the ERA grid was not read — nothing is concluded from this run")
                return None
            break
        batch = rows_to_era(hdrs, rows)
        fresh = [r for r in batch if (r['check_no'], r['era_file']) not in seen]
        for r in fresh:
            seen.add((r['check_no'], r['era_file']))
        out.extend(fresh)
        if page_no == 1:
            logger.info(f"  ERA grid: {len(hdrs)} columns {hdrs[:16]} · {len(rows)} row(s) on the first page")
            if shot or not rows:
                _shot(page, 'era_unposted' if rows else 'era_empty')
            if not rows:
                _log_filter_controls(page)
                # Every table on screen, headers, row counts and first row:
                # the next log says how the grid is built when it read empty.
                describe_screen(page, 'the ERA grid read empty')
        if not fresh or not _next_page(page):
            break
        if page_no % 10 == 0:
            logger.info(f"  … {len(out)} unposted ERA(s) over {page_no} page(s)")
    logger.info(f"💸 eCW ERA: {len(out)} unposted remittance(s) over {page_no} page(s) — the payer sent the "
                f"money, nobody posted it")
    if page_no >= max_pages:
        logger.warning(f"  ⚠️ stopped at the {max_pages}-page cap — raise max_pages, there may be more")
    return out
