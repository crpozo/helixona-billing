"""
Is the note locked? (2026-10-02)

Tom traced the notes that went out without a signature: every one of them
was a progress note nobody had locked in eCW. A locked note carries its
electronic signature ("Electronically signed by <name> on <date>"); an
unlocked one shows the same text with no signature line. So the signature
is the lock, and a note with no signature must not leave the building.

This module reads that off the note's text (the viewer iframe the bot
already extracts, or the PDF it rendered) and names who should lock it.
"""
import re
from datetime import date, datetime, timedelta

SIGNED_RX = re.compile(
    r'(?:electronically|digitally)\s+signed\s+(?:by\s*:?\s*)?([A-Za-z][A-Za-z.,\'\- ]{1,60}?)'
    r'(?=\s+(?:on|at|\d{1,2}/\d{1,2}/\d{2,4})\b|\s*$|\s*\n)', re.I | re.M)
SIGNED_ANY_RX = re.compile(r'(?:electronically|digitally)\s+signed|signed\s+by\s*:|e-?signed|\bsignature\s*:\s*\S', re.I)
LOCKED_WORD_RX = re.compile(r'\b(?:note\s+)?locked\b(?!\s+out)|\block(?:ed)?\s+by\b', re.I)
UNLOCKED_WORD_RX = re.compile(r'\b(?:not\s+locked|unlocked|unsigned|pending\s+signature|draft)\b', re.I)
# Who wrote or administered: the note's own header lines.
WHO_RX = re.compile(r'(?:administered\s+by|provider|rendering\s+provider|nurse|clinician|author|seen\s+by)\s*:\s*([A-Za-z][A-Za-z.,\'\- ]{2,60})', re.I)


def lock_status(text):
    """'locked' when the note carries its signature or says it is locked,
    'unlocked' when there is text but no signature, 'unknown' for no text."""
    t = str(text or '')
    if not t.strip():
        return 'unknown'
    if UNLOCKED_WORD_RX.search(t) and not SIGNED_ANY_RX.search(t):
        return 'unlocked'
    if SIGNED_ANY_RX.search(t) or LOCKED_WORD_RX.search(t):
        return 'locked'
    return 'unlocked'


def signer(text):
    """The name after "Electronically signed by", or ''."""
    m = SIGNED_RX.search(str(text or ''))
    return re.sub(r'\s+', ' ', m.group(1)).strip(' .,') if m else ''


def responsible(text, fallback=''):
    """Who should lock the note: the signer when there is one, else the
    note's own provider / administered-by line, else the claim's rendering
    provider."""
    who = signer(text)
    if who:
        return who
    m = WHO_RX.search(str(text or ''))
    if m:
        return re.sub(r'\s+', ' ', m.group(1)).strip(' .,')
    return str(fallback or '').strip()


def parse_dos(value):
    """MM/DD/YYYY or YYYY-MM-DD → date, else None."""
    s = str(value or '').strip()[:10]
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m/%d/%y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def business_days_since(start, today=None):
    """Whole business days (Mon–Fri) after `start`, up to `today`. The day
    of service itself does not count; a Friday visit is 1 on Monday."""
    start = parse_dos(start) if not isinstance(start, date) else start
    if start is None:
        return None
    today = today or date.today()
    if today <= start:
        return 0
    n, d = 0, start
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n
