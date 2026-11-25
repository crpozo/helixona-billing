"""OhMD — the reminder goes into the patient's own conversation.

The operator (2026-11-25): "we have to send a message through here, not the
SMS. We need an integration." OhMD (app.ohmd.io) is where the clinic already
talks to its patients; a message sent there lands in the thread the team
reads, under Billing and Insurance, and the patient's answer comes back to
the same place.

This drives the OhMD web app in the bot's browser, the way the bots drive
eCW and the payer portals:

    sign in (once — the Chrome profile keeps the session; a code or a
        prompt is finished by a person on the live screen)
    search the patient by name → open the conversation
    PROVE it is the patient: the Chat Details panel shows DOB, Mobile and
        ID/MRN — the MRN is eCW's account number. One of them must match
        what eCW's Patient Hub said, besides the name. No proof, no message.
    type the reminder
    ── a test stops here: screenshot, the box is cleared, nothing is sent ──
    Send, and see the message in the thread

OhMD also sells an HTTP API (API key, webhooks). It would replace the
clicking; it needs a key from OhMD and its endpoint reference, neither of
which is here. `details_in` and `identity_ok` are pure and tested; the
selectors are written from one screenshot and the first test is expected
to teach us something.
"""
import re
import time

from src.eob.post import _shot
from src.utils.logger import get_logger

logger = get_logger(__name__)

OHMD_URL = 'https://app.ohmd.io'
SEARCH_PLACEHOLDER = re.compile(r'name,\s*dob|search', re.I)
DATE_RX = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b')
PHONE_RX = re.compile(r'\(?(\d{3})\)?[\s.\-]?(\d{3})[\s.\-]?(\d{4})')


def _digits(s):
    return re.sub(r'\D', '', str(s or ''))


def _norm_date(s):
    m = DATE_RX.search(str(s or ''))
    return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}" if m else ''


def _name_tokens(name):
    return {t for t in re.split(r'[^a-z]+', str(name or '').lower()) if len(t) > 1}


def search_name(patient):
    """'DORIAN, MARK A' → 'Mark Dorian' — the way OhMD shows a patient."""
    s = str(patient or '').strip()
    if ',' in s:
        last, first = s.split(',', 1)
        first = (first.strip().split() or [''])[0]
        s = f"{first} {last.strip()}"
    return ' '.join(w.capitalize() for w in s.split())


def details_in(text):
    """The Chat Details panel: {'dob', 'mobile', 'mrn'} — each the value on
    or right after its label's line."""
    lines = [l.strip() for l in str(text or '').splitlines() if l.strip()]
    out = {'dob': '', 'mobile': '', 'mrn': ''}

    def after(label_rx, value_rx):
        for i, line in enumerate(lines):
            if re.fullmatch(label_rx, line, re.I) or re.match(label_rx + r'\b', line, re.I):
                for cand in (re.sub(label_rx, '', line, count=1, flags=re.I), *(lines[i + 1:i + 2])):
                    m = re.search(value_rx, cand)
                    if m:
                        return m.group(0)
        return ''
    out['dob'] = _norm_date(after(r'DOB', DATE_RX.pattern))
    out['mobile'] = _digits(after(r'Mobile', PHONE_RX.pattern))[-10:]
    out['mrn'] = _digits(after(r'ID\s*/\s*MRN|MRN', r'\d{2,12}'))
    return out


def identity_ok(row, details):
    """(ok, how). The name alone never sends a message: besides it, OhMD's
    MRN must be eCW's account number, or the date of birth must agree, or
    the mobile number must."""
    acct, mrn = _digits(row.get('acct_no')), _digits(details.get('mrn'))
    if acct and mrn:
        # Both systems give the number: they must agree, whatever else does.
        return (True, 'MRN = eCW account number') if acct.lstrip('0') == mrn.lstrip('0') else \
               (False, f"OhMD's MRN {mrn} is not eCW's account number {acct}")
    dob, theirs = _norm_date(row.get('dob')), _norm_date(details.get('dob'))
    if dob and theirs:
        return (True, 'date of birth agrees') if dob == theirs else (False, 'the date of birth does not agree')
    phone, mobile = _digits(row.get('phone'))[-10:], _digits(details.get('mobile'))[-10:]
    if len(phone) == 10 and len(mobile) == 10:
        return (True, 'mobile number agrees') if phone == mobile else (False, 'the mobile number does not agree')
    return False, 'nothing besides the name to prove who this is (no account number, date of birth or mobile on both sides)'


# ------------------------------------------------------------- the clicking
def _body(page):
    try:
        return page.inner_text('body')
    except Exception:
        return ''


def _signed_in(page):
    text = _body(page)
    return bool(re.search(r'All Chats|Chat Details|Colleagues', text)) and 'ohmd' in (page.url or '')


def login(page, creds=None, wait_for_person=180):
    """Leave `page` inside OhMD. The profile usually still holds the session;
    else the e-mail and password are typed, and whatever OhMD asks next (a
    code, a prompt) is left to a person on the live screen."""
    logger.info("💬 OhMD: opening the inbox")
    page.goto(OHMD_URL, wait_until='domcontentloaded', timeout=60000)
    deadline = time.time() + 20
    while time.time() < deadline and not _signed_in(page):
        if page.query_selector('input[type="password"], input[type="email"], input[name*="user" i]'):
            break
        time.sleep(1)
    if _signed_in(page):
        logger.info("  ✅ OhMD session is open")
        return True
    creds = creds or {}
    if creds.get('username') and creds.get('password'):
        try:
            user = page.query_selector('input[type="email"], input[name*="user" i], input[name*="email" i]')
            if user:
                user.fill(creds['username'])
            pw = page.query_selector('input[type="password"]')
            if not pw:
                page.keyboard.press('Enter')       # an e-mail-first form
                page.wait_for_selector('input[type="password"]', timeout=15000)
                pw = page.query_selector('input[type="password"]')
            pw.fill(creds['password'])
            page.keyboard.press('Enter')
        except Exception as e:
            logger.warning(f"  ⚠️ OhMD sign-in form: {str(e)[:120]}")
    else:
        logger.warning("  ⚠️ no ohmd_credentials secret — sign in on the live screen")
    logger.info(f"  ⏳ waiting up to {wait_for_person}s for the OhMD inbox (finish any code or prompt on the live screen)")
    deadline = time.time() + wait_for_person
    while time.time() < deadline:
        if _signed_in(page):
            logger.info("  ✅ signed in to OhMD")
            return True
        time.sleep(2)
    _shot(page, 'ohmd_not_signed_in')
    logger.error("❌ OhMD: not signed in")
    return False


def open_conversation(page, row):
    """Search the patient and open the conversation. (ok, reason)."""
    name = search_name(row.get('patient'))
    try:
        box = page.get_by_placeholder(SEARCH_PLACEHOLDER).first
        box.click(timeout=8000)
        box.fill('')
        box.fill(name)
    except Exception as e:
        _shot(page, 'ohmd_no_search')
        return False, f'the search box was not found ({str(e)[:80]})'
    time.sleep(3)
    want = _name_tokens(name)
    try:
        hits = page.get_by_text(re.compile(re.escape(name), re.I))
        n = hits.count()
        # The first hit that is not the search box itself and not the open header.
        for i in range(min(n, 8)):
            el = hits.nth(i)
            try:
                if el.evaluate("e => e.tagName === 'INPUT' || e.tagName === 'TEXTAREA'"):
                    continue
                el.click(timeout=4000)
                time.sleep(2.5)
                head = _body(page)
                if want and want <= _name_tokens(head[:6000]) and re.search(r'Chat Details', head):
                    return True, ''
            except Exception:
                continue
    except Exception as e:
        return False, f'the search results could not be read ({str(e)[:80]})'
    _shot(page, f"ohmd_not_found_{_digits(row.get('check_number'))}")
    return False, f'no OhMD conversation found for {name}'


def _message_box(page):
    for sel in ('[contenteditable="true"]', 'textarea'):
        try:
            loc = page.locator(sel)
            for i in range(loc.count() - 1, -1, -1):
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                hint = ' '.join(filter(None, [el.get_attribute('placeholder') or '', el.get_attribute('aria-label') or '']))
                if re.search(r'shared notes|colleagues about', hint, re.I):
                    continue        # the internal notes box is never the patient's
                return el
        except Exception:
            continue
    return None


def send_message(page, row, text, send=False):
    """One reminder into one patient's conversation. Returns {'status',
    'reason'}: sent · typed (a test: typed, photographed, cleared) ·
    not found · unproven (identity) · failed."""
    ck = _digits(row.get('check_number'))
    ok, why = open_conversation(page, row)
    if not ok:
        return {'status': 'not found', 'reason': why}
    details = details_in(_body(page))
    proven, how = identity_ok(row, details)
    if not proven:
        _shot(page, f'ohmd_unproven_{ck}')
        return {'status': 'unproven', 'reason': f'not sent — {how}'}
    box = _message_box(page)
    if box is None:
        _shot(page, f'ohmd_no_box_{ck}')
        return {'status': 'failed', 'reason': 'the message box was not found'}
    try:
        box.click(timeout=5000)
        page.keyboard.insert_text(text)      # no key events: a line break does not send
        time.sleep(1.0)
    except Exception as e:
        return {'status': 'failed', 'reason': f'the message could not be typed ({str(e)[:80]})'}
    _shot(page, f'ohmd_typed_{ck}')
    if not send:
        try:
            box.click()
            page.keyboard.press('Control+a')
            page.keyboard.press('Delete')
        except Exception:
            pass
        logger.info(f"  💬 OhMD test: the message was typed for {search_name(row.get('patient'))} ({how}) and cleared — not sent")
        return {'status': 'typed', 'reason': f'identity: {how}; typed and cleared, not sent'}
    try:
        btn = page.get_by_role('button', name=re.compile(r'^send', re.I)).last
        btn.click(timeout=5000)
    except Exception:
        try:
            page.locator('button[aria-label*="send" i], button[title*="send" i], [data-testid*="send" i]').last.click(timeout=4000)
        except Exception as e:
            _shot(page, f'ohmd_no_send_{ck}')
            return {'status': 'failed', 'reason': f'typed, but the Send button was not found ({str(e)[:60]}) — the text is still in the box'}
    time.sleep(3)
    first_line = next((l for l in text.splitlines() if l.strip()), '')[:40]
    seen = first_line and first_line in _body(page)
    _shot(page, f'ohmd_sent_{ck}')
    logger.info(f"  💬 OhMD: sent to {search_name(row.get('patient'))} ({how}){'' if seen else ' — not seen in the thread yet'}")
    return {'status': 'sent', 'reason': f"identity: {how}" + ('' if seen else '; the message was not seen in the thread — check it')}
