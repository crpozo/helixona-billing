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


def go_to_claims(page):
    """After the login eCW is on the schedule; the claim lookup lives on
    Billing → Claims. (2026-11-16: the lookup box was not on the page, the
    claim was never opened, and nothing said so.)"""
    return open_claims(page)


def read_contact(page, claim_id, open_claim, close_claim, shot=False):
    """The patient's contact off claim `claim_id`: {'email', 'phone',
    'source'}, or None when the claim could not be opened."""
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
    out = {'email': '', 'phone': '', 'source': ''}
    try:
        found = contact_in(_popup_text(page))
        if found['email']:
            out.update(found, source='eCW claim')
        else:
            if shot:
                logger.info(f"  🔘 claim {cid}: buttons that may lead to the patient: {_candidate_buttons(page) or 'none seen'}")
            opened = _click_text(page, PATIENT_INFO, timeout=2.5, what='patient info')
            if not opened:
                logger.info(f"  (claim {cid}: no patient-info button among {PATIENT_INFO} — the claim popup alone was read)")
            if opened:
                time.sleep(2.0)
                info = contact_in(_popup_text(page))
                if info['email'] or info['phone']:
                    out.update(email=info['email'], phone=info['phone'] or found['phone'], source='eCW patient info')
                if shot:
                    _shot(page, f'reminders_info_{cid}')
                _click_text(page, CLOSE_INFO, timeout=1.5, what='close patient info')
                time.sleep(0.5)
            if not out['source'] and found['phone']:
                out.update(phone=found['phone'], source='eCW claim')
        if shot:
            _shot(page, f'reminders_claim_{cid}')
        logger.info(f"  📇 claim {cid}: e-mail {'found' if out['email'] else 'not found'} · "
                    f"phone {'found' if out['phone'] else 'not found'}{' · ' + out['source'] if out['source'] else ''}")
    finally:
        try:
            close_claim(page)
        except Exception:
            pass
    return out
