"""
Unlocked notes: who still has to lock what, and the e-mails about it.

The rule (2026-10-02, from Tom's finding that unsigned notes were unlocked
notes):
  1. A claim whose IV Note is not locked is held — src/rules/submission_gate.
  2. When a note is still unlocked 3 business days after the date of service,
     the person who should lock it and their manager get an e-mail.
  3. Every week, one summary of every unlocked note goes to the billing team:
     eCW chart number, DOS, appointment type, who is responsible.

This runs as the `unlocked_notes` task on the Intake bot, with no browser:
it reads what the extractor already wrote on each claim (iv_note_locked,
note_owner) and sends through src/notify/email. Each notice is recorded on
the claim and in the helixona-notices table, so a note is told once (or
again with `remind`) and the dashboard can show what went out.
"""
import os
from datetime import date, datetime, timezone

from src.aws.clients import scan_all
from src.ecw.note_lock import business_days_since
from src.notify import email as mail
from src.rules.submission_gate import is_office_visit
from src.utils.logger import get_logger

logger = get_logger(__name__)

NOTICES_TABLE = os.environ.get('NOTICES_TABLE', 'helixona-notices')
NOTICE_AFTER_BUSINESS_DAYS = 3
CLINIC = 'Helixona'


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def ensure_table(ddb):
    if NOTICES_TABLE in [t.name for t in ddb.tables.all()]:
        return ddb.Table(NOTICES_TABLE)
    logger.info(f"🆕 creating table {NOTICES_TABLE}")
    t = ddb.create_table(TableName=NOTICES_TABLE,
                         KeySchema=[{'AttributeName': 'k', 'KeyType': 'HASH'}],
                         AttributeDefinitions=[{'AttributeName': 'k', 'AttributeType': 'S'}],
                         BillingMode='PAY_PER_REQUEST')
    t.wait_until_exists()
    return t


def appt_type(claim):
    """What the clinic calls the visit: eCW's visit type when on file, else
    Office Visit / IV Therapy from the CPT."""
    vt = str(claim.get('visit_type') or claim.get('appt_type') or '').strip()
    if vt:
        return vt
    if claim.get('office_visit') or is_office_visit(claim.get('cpt')):
        return 'Office Visit'
    return 'IV Therapy'


def unlocked_notes(claims, today=None):
    """The outstanding unlocked notes: claims still to send whose IV Note was
    read and found unlocked. Sorted oldest DOS first."""
    today = today or date.today()
    out = []
    for c in claims:
        if str(c.get('claim_id', '')).startswith('_'):
            continue
        if c.get('iv_note_locked') is not False:
            continue
        if c.get('symplisend_submitted') or c.get('ecw_visible') is False:
            continue
        dos = c.get('service_date') or c.get('dos') or ''
        days = business_days_since(dos, today)
        out.append({
            'claim_id': str(c.get('claim_id', '')),
            'chart_no': str(c.get('account_no') or c.get('chart_no') or ''),
            'patient': str(c.get('patient_name', '')),
            'dos': str(dos),
            'appt_type': appt_type(c),
            'responsible': str(c.get('note_owner') or c.get('provider') or ''),
            'business_days': days,
            'overdue': days is not None and days >= NOTICE_AFTER_BUSINESS_DAYS,
            'notice_sent_at': str(c.get('unlock_notice_sent_at') or ''),
            'lock_checked_at': str(c.get('iv_note_lock_checked_at') or ''),
        })
    out.sort(key=lambda r: (r['business_days'] is None, -(r['business_days'] or 0), r['claim_id']))
    return out


def _row(n):
    return f"{n['chart_no'] or '—':>10}  {n['dos']:<10}  {n['appt_type']:<14}  {n['patient']:<28.28}  claim {n['claim_id']:<6}  {n['business_days'] if n['business_days'] is not None else '?':>3} business days"


def _html_table(rows):
    head = ''.join(f'<th style="text-align:left;padding:6px 10px;border-bottom:1px solid #ccc">{h}</th>'
                   for h in ('eCW chart no.', 'DOS', 'Appt type', 'Patient', 'eCW claim', 'Responsible', 'Business days'))
    body = ''.join(
        '<tr>' + ''.join(f'<td style="padding:6px 10px;border-bottom:1px solid #eee">{v}</td>' for v in (
            n['chart_no'] or '—', n['dos'], n['appt_type'], n['patient'], n['claim_id'], n['responsible'] or '—',
            n['business_days'] if n['business_days'] is not None else '?')) + '</tr>' for n in rows)
    return f'<table style="border-collapse:collapse;font:13px system-ui,sans-serif"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def notice_text(person_name, rows):
    lines = [f"Hello {person_name.split(',')[-1].strip() if ',' in person_name else person_name},", "",
             f"The following progress note{'s are' if len(rows) > 1 else ' is'} still not locked in eCW "
             f"{NOTICE_AFTER_BUSINESS_DAYS} or more business days after the visit. The claim cannot be billed until the note is locked "
             f"(the signature is added when you lock it).", "",
             "  Chart no.   DOS         Appt type       Patient                       Claim        Days"]
    lines += ['  ' + _row(n) for n in rows]
    lines += ["", "Please lock the note in eCW today. Reply to this e-mail if the note belongs to someone else.", "",
              f"— {CLINIC} billing bot (automatic notice; your manager is copied)"]
    return '\n'.join(lines)


def summary_text(rows, today):
    lines = [f"Unlocked progress notes as of {today.isoformat()}: {len(rows)}", "",
             f"Each one holds a claim. {sum(1 for r in rows if r['overdue'])} are {NOTICE_AFTER_BUSINESS_DAYS}+ business days past the visit; "
             f"{sum(1 for r in rows if r['notice_sent_at'])} have had a notice.", "",
             "  Chart no.   DOS         Appt type       Patient                       Claim        Days   Responsible"]
    lines += ['  ' + _row(n) + f"   {n['responsible'] or '—'}" for n in rows]
    by = {}
    for n in rows:
        by[n['responsible'] or '—'] = by.get(n['responsible'] or '—', 0) + 1
    lines += ["", "By person: " + ' · '.join(f"{k} {v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])), "",
              f"— {CLINIC} billing bot (weekly summary)"]
    return '\n'.join(lines)


def run_unlocked_notes(aws_client, body):
    """The task. body: weekly (send the summary), remind (notify again the
    notes already told), dry_run (compose, send nothing), today (YYYY-MM-DD)."""
    weekly = bool(body.get('weekly'))
    remind = bool(body.get('remind'))
    dry = bool(body.get('dry_run'))
    today = datetime.strptime(body['today'], '%Y-%m-%d').date() if body.get('today') else date.today()
    logger.info(f"Unlocked notes — weekly={weekly} remind={remind} dry_run={dry} as of {today}")

    claims_table = aws_client.dynamodb.Table('helixona-claims')
    notices = ensure_table(aws_client.dynamodb)
    roster = mail.load_roster()
    rows = unlocked_notes(scan_all(claims_table), today)
    logger.info(f"  🔓 {len(rows)} unlocked note(s), {sum(1 for r in rows if r['overdue'])} overdue")

    sent, owed = [], []
    due = [r for r in rows if r['overdue'] and (remind or not r['notice_sent_at'])]
    by_person = {}
    for r in due:
        by_person.setdefault(r['responsible'] or '', []).append(r)
    for who, items in by_person.items():
        person = mail.find_person(roster, who) if who else None
        to = [person['email']] if person and person.get('email') else []
        cc = [mail.manager_of(roster, person)]
        if not to:
            # Nobody to address: the manager alone, so it is not silent.
            to, cc = [mail.manager_of(roster, person)], []
        subject = f"[{CLINIC}] {len(items)} progress note{'s' if len(items) > 1 else ''} to lock in eCW — {who or 'owner unknown'}"
        mid = mail.send(aws_client, roster, to, subject, notice_text(who or 'team', items), cc=cc, dry_run=dry)
        for r in items:
            rec = {'claim_id': r['claim_id'], 'to': to, 'cc': cc, 'who': who, 'at': _now(), 'message_id': mid, 'subject': subject}
            if mid:
                sent.append(rec)
                if not dry:
                    aws_client.update_claim_status(r['claim_id'], {
                        'unlock_notice_sent_at': rec['at'], 'unlock_notice_to': ', '.join(to + cc),
                        'unlock_notice_count': int(r.get('notice_count') or 0) + 1})
                    notices.put_item(Item={'k': f"notice:{r['claim_id']}:{rec['at']}", **rec})
            else:
                owed.append(rec)

    summary_id = ''
    if weekly:
        to = roster.get('weekly_to') or []
        subject = f"[{CLINIC}] Weekly unlocked progress notes — {len(rows)} outstanding ({today.isoformat()})"
        summary_id = mail.send(aws_client, roster, to, subject, summary_text(rows, today), html=_html_table(rows), dry_run=dry)
        if summary_id and not dry:
            notices.put_item(Item={'k': f"weekly:{today.isoformat()}", 'at': _now(), 'to': to, 'count': len(rows),
                                   'overdue': sum(1 for r in rows if r['overdue']), 'message_id': summary_id})

    result = {'as_of': today.isoformat(), 'unlocked': len(rows), 'overdue': sum(1 for r in rows if r['overdue']),
              'notices_sent': len(sent), 'notices_owed': len(owed), 'weekly_sent': bool(summary_id), 'dry_run': dry,
              'roster_people': len(roster.get('people') or {}), 'at': _now()}
    if not dry:
        notices.put_item(Item={'k': '_last', **result, 'owed': [o['claim_id'] for o in owed][:200]})
    logger.info(f"  ✅ notices sent {len(sent)} · owed (no address / SES refused) {len(owed)} · weekly {'sent' if summary_id else 'not sent'}")
    return result
