"""
Reminders — Blue Shield checks that were mailed to the patient.

The operator (2026-11-12): "Blue Shield: read if a new check arrives for the
patient; send a reminder to the patient or they will be charged credit card."

    1. Blue Shield — the checks, as the Remittance bot reads them (Claims →
       Check claim status → each Check/EFT: payee, amount, status). With
       `blue_shield` the run walks the portal itself; otherwise it takes the
       checks already on file (helixona-eobs).
    2. The ones made out to the patient (src/checks/reconcile.payee_kind)
       that Helixona does not hold — no scan, not in the tracker, no payment
       in eCW (helixona-checks).
    3. The patient's e-mail: the address kept from an earlier run, else the
       host's contact file, else eCW (the claim, then the patient's info).
    4. The reminder, from data/patient_check_reminder.json: the first when
       the check is first seen, then every `reminder_every_days`, at most
       `max_reminders`; the last one is the final notice. The deadline is
       `deadline_days` after the first reminder.
    5. Past the deadline the check is listed as `deadline passed` for a
       person to decide. THE BOT NEVER CHARGES A CARD.

Nothing is e-mailed unless the task says `send: true`; without it the run
writes each reminder as a draft the tab shows. `test_to` sends every due
reminder to that address instead of the patient and counts none of them.
"""
import json
import os
import re
import time
from datetime import date, datetime, timedelta

from src.aws.clients import scan_all
from src.checks.reconcile import norm_check, payee_kind, _cashed, _name_tokens
from src.eob.parse import money, norm_text
from src.notify import email as mail
from src.utils.logger import get_logger

logger = get_logger(__name__)

REMINDERS_TABLE = 'helixona-patient-checks'
EOB_TABLE = 'helixona-eobs'
CHECKS_TABLE = 'helixona-checks'
DEFAULT_SINCE = '07/01/2025'
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
POLICY_FILE = os.path.join(_ROOT, 'data', 'patient_check_reminder.json')
# Host-side, git-ignored (it is PHI): {"claims": {"1234": {"email": "..."}},
# "patients": {"DOE, JANE": {"email": "...", "phone": "..."}}}
CONTACTS_FILE = os.environ.get('PATIENT_CONTACTS', '/opt/helixona-agent/data/patient_contacts.json')
DEFAULTS = {'clinic': 'Helixona', 'phone': '', 'from': '', 'deadline_days': 14, 'reminder_every_days': 5, 'max_reminders': 3}
OPEN_STAGES = ('to remind', 'reminded', 'deadline passed')


def _now():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')


def ensure_table(aws_client):
    ddb = aws_client.dynamodb
    if REMINDERS_TABLE in [t.name for t in ddb.tables.all()]:
        return ddb.Table(REMINDERS_TABLE)
    logger.info(f"🆕 creating table {REMINDERS_TABLE}")
    t = ddb.create_table(TableName=REMINDERS_TABLE,
                         KeySchema=[{'AttributeName': 'check_number', 'KeyType': 'HASH'}],
                         AttributeDefinitions=[{'AttributeName': 'check_number', 'AttributeType': 'S'}],
                         BillingMode='PAY_PER_REQUEST')
    t.wait_until_exists()
    return t


def _put(table, key, item):
    table.update_item(Key={'check_number': key},
                      UpdateExpression='SET ' + ', '.join(f'#{k} = :{k}' for k in item),
                      ExpressionAttributeNames={f'#{k}': k for k in item},
                      ExpressionAttributeValues={f':{k}': v for k, v in item.items()})


def load_policy(path=None):
    try:
        with open(path or POLICY_FILE, encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as e:
        logger.error(f"  ❌ reminder policy not read ({e}) — nothing can be composed")
        raw = {}
    p = {**DEFAULTS, **{k: v for k, v in raw.items() if not str(k).startswith('_')}}
    for k in ('deadline_days', 'reminder_every_days', 'max_reminders'):
        p[k] = int(p[k])
    return p


def load_contacts(path=None):
    try:
        with open(path or CONTACTS_FILE, encoding='utf-8') as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {'claims': {}, 'patients': {}}
    except ValueError as e:
        logger.error(f"  ❌ contact file is not valid JSON: {e}")
        return {'claims': {}, 'patients': {}}
    return {'claims': raw.get('claims') or {}, 'patients': raw.get('patients') or {}}


def contact_from_file(contacts, row):
    for cid in row.get('claim_ids') or []:
        c = (contacts.get('claims') or {}).get(str(cid))
        if c and c.get('email'):
            return {'email': c['email'], 'phone': c.get('phone', ''), 'source': 'contact file'}
    want = _name_tokens(row.get('patient'))
    for name, c in (contacts.get('patients') or {}).items():
        if want and _name_tokens(name) == want and (c or {}).get('email'):
            return {'email': c['email'], 'phone': c.get('phone', ''), 'source': 'contact file'}
    return None


def _date(text):
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', str(text or ''))
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', str(text or ''))
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def first_name(patient):
    """'THORNE, KARI A' → 'Kari'; 'Kari Thorne' → 'Kari'."""
    s = norm_text(patient)
    part = s.split(',', 1)[1] if ',' in s else s
    words = [w for w in re.split(r'[^A-Za-z\'\-]+', part) if w]
    return words[0].capitalize() if words else 'there'


def patient_checks(eobs, held=(), since=None):
    """Blue Shield's checks made out to the patient, one row per check.

    `eobs`: helixona-eobs items. `held`: helixona-checks items — what says
    whether Helixona has the check (a scan, the tracker, a payment in eCW).
    `since`: leave out checks issued before this date."""
    held_by = {norm_check(h.get('check_number')): h for h in held or ()}
    out = []
    for q in eobs:
        key = norm_check(q.get('check_eft'))
        if not key:
            continue
        claims = [c for c in (q.get('claims') or []) if isinstance(c, dict)]
        names = sorted({norm_text(c.get('member_name')) for c in claims if c.get('member_name')})
        probe = {'bs_payee': q.get('payee_name', ''), 'bs_paid_to_member': bool(q.get('paid_to_member')),
                 'bs_patients': ' · '.join(names)}
        if payee_kind(probe) != 'patient':
            continue
        issued = _date(q.get('check_date'))
        if since and issued and issued < since:
            continue
        # The patient is the one the check is made out to; a check that
        # names nobody we know keeps the payee as printed.
        pt = _name_tokens(q.get('payee_name'))
        patient = next((n for n in names if len(_name_tokens(n) & pt) >= 2), names[0] if names else norm_text(q.get('payee_name')))
        mine = [c for c in claims if norm_text(c.get('member_name')) == patient] or claims
        h = held_by.get(key, {})
        how = [w for w, k in (('scan', 'has_copy'), ('tracker', 'in_tracker'), ('eCW payment', 'in_ecw')) if h.get(k)]
        status = norm_text(q.get('check_status'))
        out.append({
            'check_number': key,
            'amount': money(q.get('check_amount')),
            'check_date': norm_text(q.get('check_date')),
            'bs_status': status,
            'cashed': bool(_cashed(status) or q.get('cashed_date')),
            'cashed_date': norm_text(q.get('cashed_date')),
            'voided': bool(re.search(r'void|stop', status, re.I)),
            'payee': norm_text(q.get('payee_name')),
            'patient': patient,
            'claim_ids': sorted({str(c.get('claim_id')) for c in mine if c.get('claim_id')}),
            'dos': ', '.join(sorted({str(c.get('dos')) for c in mine if c.get('dos')})[:3]),
            'in_hand': bool(how),
            'in_hand_how': ' · '.join(how),
            'reconciled': bool(h),
        })
    out.sort(key=lambda r: (_date(r['check_date']) or date.min, r['check_number']), reverse=True)
    return out


def plan(row, prev, policy, today):
    """(stage, action) for one check. action: '' (nothing to send now),
    'first', 'reminder' or 'final'."""
    if row.get('in_hand'):
        return 'received', ''
    if row.get('voided'):
        return 'voided', ''
    n = int(prev.get('reminders_sent') or 0)
    if n == 0:
        return 'to remind', 'first'
    deadline = _date(prev.get('deadline'))
    if deadline and today > deadline:
        return 'deadline passed', ''
    if n >= policy['max_reminders']:
        return 'reminded', ''
    last = _date(prev.get('last_reminder_at')) or today
    if (today - last).days >= policy['reminder_every_days']:
        return 'to remind', 'final' if n + 1 >= policy['max_reminders'] else 'reminder'
    return 'reminded', ''


def compose(policy, row, deadline, action='first'):
    """(subject, text) of the reminder for one check."""
    cashed = bool(row.get('cashed'))
    body = policy.get('cashed_body' if cashed else 'body') or policy.get('body') or []
    subject = policy.get('cashed_subject' if cashed else 'subject') or policy.get('subject') or ''
    if action == 'final' and policy.get('final_subject'):
        subject = policy['final_subject']
    vals = {
        'first_name': first_name(row.get('patient')),
        'clinic': policy.get('clinic', 'Helixona'),
        'check_number': row.get('check_number', ''),
        'check_date': row.get('check_date', '') or 'not shown',
        'amount': row.get('amount', ''),
        'dos': row.get('dos', ''),
        'dos_phrase': f" on {row['dos']}" if row.get('dos') else '',
        'deadline': deadline.strftime('%B %-d, %Y'),
        'phone': policy.get('phone', ''),
        'phone_phrase': f" or call {policy['phone']}" if policy.get('phone') else '',
    }

    class _Keep(dict):
        def __missing__(self, k):
            return '{' + k + '}'
    fill = lambda s: str(s).format_map(_Keep(vals))
    return fill(subject), '\n'.join(fill(line) for line in body)


def summarize(rows):
    def total(sel):
        return round(sum(float(r.get('amount') or 0) for r in rows if sel(r)), 2)
    out = {'checks': len(rows), 'amount': total(lambda r: True)}
    for st in ('to remind', 'reminded', 'deadline passed', 'received', 'voided'):
        out[st] = sum(1 for r in rows if r.get('stage') == st)
        out[st + '_amount'] = total(lambda r, st=st: r.get('stage') == st)
    out['outstanding'] = sum(1 for r in rows if r.get('stage') in OPEN_STAGES)
    out['outstanding_amount'] = total(lambda r: r.get('stage') in OPEN_STAGES)
    out['no_email'] = sum(1 for r in rows if r.get('stage') in OPEN_STAGES and not r.get('email'))
    out['cashed_by_patient'] = sum(1 for r in rows if r.get('stage') in OPEN_STAGES and r.get('cashed'))
    return out


def run_patient_check_reminders(aws_client, body, login=None, get_page=None, open_claim=None, close_claim=None,
                                walk_portal=None):
    """The task. body: since · blue_shield (walk the portal first) ·
    limit_checks · check_eft · ecw (look the e-mail up in eCW, default true) ·
    send (e-mail the patients; default false = drafts only) · test_to (send
    the due reminders to this address instead, counting none) · limit (cap
    the reminders of this run) · today."""
    since_txt = str(body.get('since') or DEFAULT_SINCE)
    since = _date(since_txt)
    do_send = bool(body.get('send'))
    test_to = str(body.get('test_to') or '').strip()
    use_ecw = bool(body.get('ecw', True))
    limit = int(body.get('limit') or 0)
    only = norm_check(body.get('check_eft'))
    today = datetime.strptime(body['today'], '%Y-%m-%d').date() if body.get('today') else date.today()
    mode = 'SEND' if do_send and not test_to else f'test → {test_to}' if test_to else 'drafts only'
    logger.info(f"Reminders — checks mailed to patients: since={since_txt} blue_shield={bool(body.get('blue_shield'))} "
                f"mode={mode} limit={limit or 'none'} as of {today}")
    table = ensure_table(aws_client)
    policy = load_policy()
    contacts = load_contacts()
    run = {'started_at': _now(), 'steps': {}, 'order': [], 'mode': mode}

    def progress(step, text):
        run['steps'][step] = text
        if step not in run['order']:
            run['order'].append(step)
        run['updated_at'] = _now()
        _put(table, '_run', run)
        logger.info(f"▶ {step}: {text}")

    # 1. Blue Shield.
    if body.get('blue_shield') and walk_portal and get_page:
        progress('blue_shield', 'walking the portal…')
        try:
            caught = walk_portal(get_page(), aws_client, body, since_txt, int(body.get('limit_checks') or 0), only)
            progress('blue_shield', f"{len(caught)} check(s) read from the portal")
        except Exception as e:
            logger.error(f"❌ Blue Shield step failed: {e}")
            progress('blue_shield', f"portal not read ({str(e)[:100]}) — using the checks on file")
    eobs = scan_all(aws_client.dynamodb.Table(EOB_TABLE),
                    ProjectionExpression='check_eft, check_amount, check_status, check_date, cashed_date, '
                                         'payee_name, paid_to_member, claims')
    try:
        held = scan_all(aws_client.dynamodb.Table(CHECKS_TABLE),
                        ProjectionExpression='check_number, has_copy, in_tracker, in_ecw')
    except Exception as e:
        logger.warning(f"  ⚠️ {CHECKS_TABLE} not read ({str(e)[:100]}) — no check can be shown as received")
        held = []
    rows = patient_checks(eobs, held, since)
    if only:
        rows = [r for r in rows if r['check_number'] == only]
    if not body.get('blue_shield'):
        progress('blue_shield', f"{len(eobs)} check(s) on file from earlier captures")
    progress('patient_checks', f"{len(rows)} check(s) made out to the patient · {sum(1 for r in rows if r['in_hand'])} already at Helixona")

    known = {str(it.get('check_number')): it for it in scan_all(table) if not str(it.get('check_number', '')).startswith('_')}

    # 2. Who is due, and their address.
    due = []
    for r in rows:
        prev = known.get(r['check_number'], {})
        r['stage'], r['action'] = plan(r, prev, policy, today)
        r['email'], r['phone'], r['email_source'] = prev.get('email', ''), prev.get('phone', ''), prev.get('email_source', '')
        if r['action'] and not r['email']:
            got = contact_from_file(contacts, r)
            if got:
                r['email'], r['phone'], r['email_source'] = got['email'], got.get('phone', ''), got['source']
        if r['action']:
            due.append(r)
    if limit:
        due = due[:limit]
    need_ecw = [r for r in due if not r['email'] and r['claim_ids']]
    if need_ecw and use_ecw and login and get_page and open_claim:
        from src.reminders.ecw import read_contact
        progress('contacts', f"looking up {len(need_ecw)} patient e-mail(s) in eCW…")
        page = get_page()
        if login(page, aws_client.get_secret('ecw_credentials'), aws_client):
            for i, r in enumerate(need_ecw, 1):
                got = read_contact(page, r['claim_ids'][0], open_claim, close_claim, shot=(i == 1))
                if got and (got['email'] or got['phone']):
                    r['email'], r['phone'], r['email_source'] = got['email'], got['phone'], got['source']
                time.sleep(0.2)
        else:
            logger.error("❌ eCW login did not go through — no e-mail could be looked up")
    progress('contacts', f"{sum(1 for r in due if r['email'])} of {len(due)} due reminder(s) have an e-mail"
             + (f" · {sum(1 for r in due if not r['claim_ids'])} not matched to an eCW claim" if any(not r['claim_ids'] for r in due) else ''))

    # 3. Compose; send only when told to.
    sender = {'from': policy.get('from') or mail.load_roster().get('from', '')}
    sent, drafted, owed = 0, 0, 0
    due_keys = {r['check_number'] for r in due}
    for r in rows:
        prev = known.get(r['check_number'], {})
        n = int(prev.get('reminders_sent') or 0)
        item = {k: r[k] for k in ('amount', 'check_date', 'bs_status', 'cashed', 'cashed_date', 'voided', 'payee', 'patient',
                                  'claim_ids', 'dos', 'in_hand', 'in_hand_how', 'reconciled', 'stage', 'email', 'phone', 'email_source')}
        item.update(reminders_sent=n, updated_at=_now(), first_seen=prev.get('first_seen') or _now(), next_action=r['action'])
        if r['check_number'] in due_keys:
            deadline = _date(prev.get('deadline')) or today + timedelta(days=policy['deadline_days'])
            subject, text = compose(policy, r, deadline, r['action'])
            item.update(draft_subject=subject, draft_text=text, draft_deadline=deadline.isoformat(), draft_at=_now())
            drafted += 1
            to = [test_to] if test_to else [r['email']] if r['email'] else []
            if (do_send or test_to) and to:
                mid = mail.send(aws_client, sender, to, ('[TEST] ' if test_to else '') + subject, text)
                if mid and not test_to:
                    sent += 1
                    hist = list(prev.get('history') or [])[-19:] + [{'at': _now(), 'kind': r['action'], 'to': r['email'], 'message_id': mid}]
                    item.update(reminders_sent=n + 1, last_reminder_at=today.isoformat(), history=hist,
                                first_reminder_at=prev.get('first_reminder_at') or today.isoformat(),
                                deadline=deadline.isoformat(), stage='reminded', next_action='')
                elif mid:
                    sent += 1
                else:
                    owed += 1
            elif do_send:
                owed += 1
        _put(table, r['check_number'], item)

    final = [it for it in scan_all(table) if not str(it.get('check_number', '')).startswith('_')]
    summary = summarize(final)
    _put(table, '_summary', {'run_at': _now(), 'as_of': today.isoformat(), 'since': since_txt, 'mode': mode,
                             'due': len(due), 'sent': sent, 'owed': owed, **summary})
    progress('done', f"{len(due)} reminder(s) due · {drafted} drafted · {sent} sent{' (test)' if test_to else ''}"
             + (f" · {owed} not sent (no e-mail or SES refused)" if owed else '')
             + f" · {summary['deadline passed']} past the deadline — for a person to decide")
    return {'ok': True, 'due': len(due), 'sent': sent, 'owed': owed, **summary}
