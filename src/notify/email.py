"""
E-mail from the bots, through Amazon SES, to the people named in a roster.

The roster is a host-side file (git-ignored; data/note_lock_roster.example.json
shows the shape): the sender, who gets the weekly summary, and each
clinician's address and manager. People are matched by name as eCW shows it
("DOE, JANE", "Jane Doe", "Doe, J"): the surname must match and the first
initial when both sides have one.

Nothing here raises for a missing roster or a refused send: the caller logs
and records that the notice is still owed, so the dashboard can say so.
"""
import json
import os
import re

from src.utils.logger import get_logger

logger = get_logger(__name__)

ROSTER_FILE = os.environ.get('NOTE_LOCK_ROSTER', '/opt/helixona-agent/data/note_lock_roster.json')
NOTIFY_FROM = os.environ.get('NOTIFY_FROM', '')


def load_roster(path=None):
    path = path or ROSTER_FILE
    try:
        with open(path, encoding='utf-8') as fh:
            r = json.load(fh)
    except FileNotFoundError:
        logger.warning(f"  ⚠️ no roster at {path} — notices cannot be addressed (see data/note_lock_roster.example.json)")
        return {}
    except ValueError as e:
        logger.error(f"  ❌ roster {path} is not valid JSON: {e}")
        return {}
    r.setdefault('people', {})
    r.setdefault('weekly_to', [])
    r.setdefault('from', NOTIFY_FROM)
    return r


def _tokens(name):
    s = re.sub(r'[^a-z ,]', ' ', str(name or '').lower())
    if ',' in s:
        last, first = s.split(',', 1)
    else:
        parts = s.split()
        last, first = (parts[-1], ' '.join(parts[:-1])) if parts else ('', '')
    return last.strip().split()[-1] if last.strip() else '', (first.strip().split() or [''])[0]


def find_person(roster, name):
    """The roster entry for a clinician's name as eCW shows it, or None."""
    last, first = _tokens(name)
    if not last:
        return None
    best = None
    for key, entry in (roster.get('people') or {}).items():
        kl, kf = _tokens(key)
        if kl != last:
            continue
        if first and kf and first[0] != kf[0]:
            continue
        if best is None or (first and kf and first == kf):
            best = {'name': key, **(entry or {})}
    return best


def manager_of(roster, person):
    """The manager's address: the person's own `manager`, else the roster's
    `default_manager`."""
    if person and person.get('manager'):
        return person['manager']
    return (roster or {}).get('default_manager', '')


def send(aws_client, roster, to, subject, text, html=None, cc=None, dry_run=False):
    """One e-mail through SES. Returns the message id, or '' when nothing
    was sent (dry run, nobody to send to, no sender, SES refused)."""
    to = [a for a in (to or []) if a]
    cc = [a for a in (cc or []) if a and a not in to]
    sender = (roster or {}).get('from') or NOTIFY_FROM
    if not to:
        logger.warning(f"  ✉️ not sent (no recipient): {subject}")
        return ''
    if not sender:
        logger.warning(f"  ✉️ not sent (no sender — set `from` in the roster or NOTIFY_FROM): {subject}")
        return ''
    if dry_run:
        logger.info(f"  ✉️ DRY RUN → {', '.join(to)}{' cc ' + ', '.join(cc) if cc else ''}: {subject}\n{text[:600]}")
        return 'dry-run'
    try:
        ses = aws_client.session.client('ses')
        body = {'Text': {'Data': text, 'Charset': 'UTF-8'}}
        if html:
            body['Html'] = {'Data': html, 'Charset': 'UTF-8'}
        resp = ses.send_email(Source=sender, Destination={'ToAddresses': to, 'CcAddresses': cc},
                              Message={'Subject': {'Data': subject, 'Charset': 'UTF-8'}, 'Body': body})
        mid = resp.get('MessageId', '')
        logger.info(f"  ✉️ sent → {', '.join(to)}{' cc ' + ', '.join(cc) if cc else ''}: {subject} ({mid})")
        return mid
    except Exception as e:
        logger.error(f"  ❌ SES refused \"{subject}\": {str(e)[:200]} — the sender must be verified in SES and the role needs ses:SendEmail")
        return ''
