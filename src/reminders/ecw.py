"""
eCW, read for a patient's e-mail address and phone (the Reminders bot).

The claim popup is opened by its number, its text read for an address; when
none is there, the patient's information window is opened from the popup and
read the same way. Read-only: every window is closed with Cancel/Close.

`contact_in` is pure and is the part the tests pin. The button names
(`PATIENT_INFO`) are the part to tune against the real screen — the first
run saves `reminders_claim_<n>.png` and logs what it found.
"""
import re
import time

from src.denials.ecw import _popup_text, open_claims
from src.eob.post import _click_text, _shot
from src.utils.logger import get_logger

logger = get_logger(__name__)

EMAIL_RX = re.compile(r'[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}')
PHONE_RX = re.compile(r'(?<!\d)\(?(\d{3})\)?[\s.\-]?(\d{3})[\s.\-]?(\d{4})(?!\d)')
# Addresses that are never the patient's: the clinic's own, the EHR's, the payer's.
NOT_PATIENT_RX = re.compile(r'@(?:[a-z0-9\-]+\.)*(helixona|eclinicalworks|ecwcloud|blueshieldca|mindfultech)\.', re.I)
EMAIL_LABEL_RX = re.compile(r'e-?mail', re.I)
CELL_LABEL_RX = re.compile(r'cell|mobile', re.I)
PATIENT_INFO = ['Patient Info', 'Pt. Info', 'Pt Info', 'Info', 'Demographics', 'Patient Hub', 'Hub']
CLOSE_INFO = ['Cancel', 'Close']


def contact_in(text):
    """{'email', 'phone'} found in a screen's text. The address on a line
    that says E-mail wins over one elsewhere; the clinic's own addresses are
    never the patient's. The phone on a Cell/Mobile line wins."""
    lines = str(text or '').splitlines()
    emails, labelled = [], []
    phones, cells = [], []
    for i, line in enumerate(lines):
        near = line + ' ' + (lines[i - 1] if i else '')
        for m in EMAIL_RX.finditer(line):
            a = m.group(0).rstrip('.').lower()
            if NOT_PATIENT_RX.search(a):
                continue
            (labelled if EMAIL_LABEL_RX.search(near) else emails).append(a)
        for m in PHONE_RX.finditer(line):
            p = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
            (cells if CELL_LABEL_RX.search(near) else phones).append(p)
    return {'email': (labelled or emails or [''])[0], 'phone': (cells or phones or [''])[0]}


BUTTONS_JS = r"""() => {
    // What can be clicked on the claim popup that might lead to the patient:
    // the names only, for the log, so the right one can be added to PATIENT_INFO.
    const vis = el => el.offsetWidth > 0 && el.offsetHeight > 0;
    const out = new Set();
    for (const el of document.querySelectorAll('button, input[type="button"], a')) {
        const t = (el.value || el.textContent || '').replace(/\s+/g, ' ').trim();
        if (t && t.length <= 30 && vis(el) && /info|hub|demog|patient|pt\b|contact|acct|account/i.test(t)) out.add(t);
    }
    return Array.from(out).slice(0, 25);
}"""


def _candidate_buttons(page):
    names = []
    for frm in page.frames:
        try:
            names += frm.evaluate(BUTTONS_JS) or []
        except Exception:
            continue
    return sorted(set(names))


FIELDS_JS = r"""() => {
    // The patient's information is a form: the address and the numbers sit
    // in <input> boxes, and a box's value is not text on the page. Each
    // visible, filled box with whatever names it: its id, name, model,
    // placeholder, title, and the label or cell just before it.
    const vis = el => el.offsetWidth > 0 && el.offsetHeight > 0;
    const out = [];
    for (const el of document.querySelectorAll('input, textarea')) {
        const type = (el.type || '').toLowerCase();
        if (['hidden', 'password', 'checkbox', 'radio', 'button', 'submit', 'file'].includes(type)) continue;
        const value = (el.value || '').trim();
        if (!value || value.length > 120 || !vis(el)) continue;
        let near = '';
        if (el.id) { const lab = document.querySelector('label[for="' + el.id.replace(/"/g, '') + '"]'); if (lab) near = lab.textContent || ''; }
        if (!near) {
            let p = el.previousElementSibling;
            if (!p && el.parentElement) p = el.parentElement.previousElementSibling;
            if (p) near = (p.textContent || '').slice(0, 60);
        }
        const name = [el.id, el.name, el.getAttribute('ng-model'), el.placeholder, el.title, near]
            .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
        out.push({ name, value });
    }
    return out.slice(0, 400);
}"""


def _fields(page):
    out = []
    for frm in page.frames:
        try:
            out += frm.evaluate(FIELDS_JS) or []
        except Exception:
            continue
    return out


def contact_in_fields(fields):
    """{'email', 'phone'} from a form's filled boxes ({'name', 'value'}):
    the box named e-mail wins over any other holding an address; the box
    named cell/mobile wins over home and work. Never the clinic's own."""
    emails, labelled, phones, cells = [], [], [], []
    for f in fields or ():
        name, value = str(f.get('name') or ''), str(f.get('value') or '')
        m = EMAIL_RX.fullmatch(value.strip()) or EMAIL_RX.search(value)
        if m and not NOT_PATIENT_RX.search(m.group(0)):
            (labelled if EMAIL_LABEL_RX.search(name) else emails).append(m.group(0).rstrip('.').lower())
            continue
        if re.search(r'phone|cell|mobile|tel\b|home|work', name, re.I) and not re.search(r'fax|emergency|pharm|employer|guarantor|insur', name, re.I):
            p = PHONE_RX.search(value)
            if p:
                (cells if CELL_LABEL_RX.search(name) else phones).append(f"({p.group(1)}) {p.group(2)}-{p.group(3)}")
    return {'email': (labelled or emails or [''])[0], 'phone': (cells or phones or [''])[0]}


def go_to_claims(page):
    """After the login eCW is on the schedule; the claim lookup lives on
    Billing → Claims. (2026-11-16: the lookup box was not on the page, the
    claim was never opened, and nothing said so.)"""
    return open_claims(page)


HUB = ['Hub']
HUB_TITLE_RX = re.compile(r'patient\s*hub', re.I)
CLOSE_HUB_JS = r"""() => {
    // The Patient Hub's own close control: an x / Close button inside the
    // window whose title bar says "Patient Hub" — never the claim's.
    const vis = el => el.offsetWidth > 0 && el.offsetHeight > 0;
    const looks = el => {
        const t = (el.textContent || '').trim();
        const hint = [el.getAttribute('aria-label'), el.title, el.className && String(el.className), el.id].filter(Boolean).join(' ');
        return t === '×' || t === 'x' || t === 'X' || /close/i.test(t) && t.length < 8 || /close|dismiss/i.test(hint);
    };
    for (const el of document.querySelectorAll('button, a, span, i, div[role="button"]')) {
        if (!vis(el) || !looks(el)) continue;
        let up = el.parentElement;
        for (let n = 0; up && n < 7; n++, up = up.parentElement) {
            if (/^\s*patient\s*hub/i.test((up.textContent || '').slice(0, 40))) { el.click(); return true; }
        }
    }
    return false;
}"""


def card_on_file(text):
    """What the Patient Hub's Billing box says after "Card on File": False
    when it offers "Add Card" (none on file), True when it names a card,
    None when the line is not there to read."""
    m = re.search(r'card\s*on\s*file', str(text or ''), re.I)
    if not m:
        return None
    after = re.sub(r'^[\s:]+', '', str(text)[m.end():m.end() + 120])
    first = (after.splitlines() or [''])[0].strip(' :')
    if not first or re.match(r'billing alert|guarantor|account inquiry|billing logs', first, re.I):
        return None
    return not re.search(r'add\s*card|no\s*card|none', first, re.I)


def _wait_hub(page, timeout=10.0):
    """The Patient Hub is up when its title is on the page."""
    deadline = time.time() + timeout
    text = ''
    while time.time() < deadline:
        text = _popup_text(page)
        if HUB_TITLE_RX.search(text):
            time.sleep(1.0)   # the header fills a moment after the frame
            return _popup_text(page)
        time.sleep(0.5)
    return ''


def _close_hub(page):
    for frm in page.frames:
        try:
            if frm.evaluate(CLOSE_HUB_JS):
                time.sleep(0.8)
                return True
        except Exception:
            continue
    try:
        page.keyboard.press('Escape')
        time.sleep(0.8)
    except Exception:
        pass
    return not HUB_TITLE_RX.search(_popup_text(page))


def read_contact(page, claim_id, open_claim, close_claim, shot=False):
    """The patient's contact off claim `claim_id`: {'email', 'phone',
    'source', 'card_on_file'}, or None when the claim could not be opened.

    The operator (2026-11-17): "you go inside Hub and check the email" — the
    claim's Hub button opens the Patient Hub, whose header carries the
    patient's mobile number and e-mail, and whose Billing box says whether a
    card is on file. The Info form is the fallback when the Hub gives none."""
    cid = str(claim_id)
    ok = False
    try:
        got = open_claim(page, cid)
        ok = bool(got[0] if isinstance(got, tuple) else got)
    except Exception as e:
        logger.warning(f"  ⚠️ claim {cid}: could not be opened: {str(e)[:120]}")
    if not ok:
        logger.warning(f"  ⚠️ claim {cid}: the claim did not open — no contact read")
        if shot:
            _shot(page, f'reminders_not_opened_{cid}')
        return None
    out = {'email': '', 'phone': '', 'source': '', 'card_on_file': None}
    try:
        # 1. The Patient Hub.
        if _click_text(page, HUB, timeout=3.0, what='Patient Hub'):
            text = _wait_hub(page)
            if text:
                hub = contact_in(text)
                out['card_on_file'] = card_on_file(text)
                if hub['email'] or hub['phone']:
                    out.update(email=hub['email'], phone=hub['phone'], source='eCW Patient Hub')
            else:
                logger.warning(f"  ⚠️ claim {cid}: the Patient Hub did not come up")
            if shot:
                _shot(page, f'reminders_hub_{cid}')
            if not _close_hub(page):
                logger.warning(f"  ⚠️ claim {cid}: the Patient Hub may still be open")
        else:
            logger.info(f"  (claim {cid}: no Hub button — buttons seen: {_candidate_buttons(page) or 'none'})")
        # 2. The Info form, only when the Hub gave no address.
        if not out['email'] and _click_text(page, ['Info'], timeout=2.0, what='patient info'):
            time.sleep(2.0)
            boxed = contact_in_fields(_fields(page))
            if boxed['email'] or (boxed['phone'] and not out['phone']):
                out.update(email=boxed['email'], phone=out['phone'] or boxed['phone'],
                           source='eCW patient info' if boxed['email'] else out['source'] or 'eCW patient info')
            _click_text(page, CLOSE_INFO, timeout=1.5, what='close patient info')
            time.sleep(0.5)
        card = {True: 'yes', False: 'NO', None: 'not read'}[out['card_on_file']]
        logger.info(f"  📇 claim {cid}: e-mail {'found' if out['email'] else 'not found'} · "
                    f"phone {'found' if out['phone'] else 'not found'} · card on file {card}"
                    f"{' · ' + out['source'] if out['source'] else ''}")
    finally:
        try:
            close_claim(page)
        except Exception:
            pass
    return out
