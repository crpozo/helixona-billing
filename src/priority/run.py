"""
Claim Prioritization — which open claims to work first.

The operator's flow (2026-10-26):
    1. Billing → Encounters, Progress Notes Done/Locked, last 120 days: any
       encounter without a claim → Claims IPE All creates it (optional:
       `create_claims`; it writes to eCW).
    2. Billing → Claims: DOS from 07/01/2025 · All Claims · Balance > 0.
    3. Each claim opened once: ERA payers (Medicare, Anthem) dated by the
       latest "Claim Status" row of the Billing Logs; the rest by the latest
       follow-up note. Days = today − that date.
    4. Priority by aging: 91+ → P1 · 61–90 → P2 · 31–60 → P3 · 0–30 → P4.
       Days over the SLA for the claim's status → overdue (data/claim_sla.json).
    5. Report sorted P1 → P4, overdue first (helixona-priority; the dashboard
       tab and its CSV).
"""
import json
import os
import time
from datetime import date, datetime, timedelta

from src.aws.clients import scan_all
from src.priority.ecw import list_open_claims, read_reference
from src.utils.logger import get_logger

logger = get_logger(__name__)

PRIORITY_TABLE = 'helixona-priority'
DEFAULT_SINCE = '07/01/2025'
ENCOUNTER_DAYS = 120
SLA_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'claim_sla.json')


def _now():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')


def ensure_table(aws_client):
    ddb = aws_client.dynamodb
    if PRIORITY_TABLE in [t.name for t in ddb.tables.all()]:
        return ddb.Table(PRIORITY_TABLE)
    logger.info(f"🆕 creating table {PRIORITY_TABLE}")
    t = ddb.create_table(TableName=PRIORITY_TABLE,
                         KeySchema=[{'AttributeName': 'claim_id', 'KeyType': 'HASH'}],
                         AttributeDefinitions=[{'AttributeName': 'claim_id', 'AttributeType': 'S'}],
                         BillingMode='PAY_PER_REQUEST')
    t.wait_until_exists()
    return t


def _put(table, key, item):
    table.update_item(Key={'claim_id': key},
                      UpdateExpression='SET ' + ', '.join(f'#{k} = :{k}' for k in item),
                      ExpressionAttributeNames={f'#{k}': k for k in item},
                      ExpressionAttributeValues={f':{k}': v for k, v in item.items()})


def load_sla(path=None):
    try:
        with open(path or SLA_FILE, encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {'default': 30}
    return {str(k).lower(): int(v) for k, v in raw.items() if not str(k).startswith('_')}


def sla_for(status, sla):
    return sla.get(str(status or '').strip().lower(), sla.get('default', 30))


def priority_for(days):
    """91+ → P1 · 61–90 → P2 · 31–60 → P3 · 0–30 → P4. None → P4 (undated)."""
    if days is None:
        return 'P4'
    if days >= 91:
        return 'P1'
    if days >= 61:
        return 'P2'
    if days >= 31:
        return 'P3'
    return 'P4'


def rank(row):
    """Sort key: overdue first, then P1 → P4, then the oldest."""
    return (0 if row.get('overdue') else 1, row.get('priority', 'P4'), -(row.get('days') or 0))


def summarize(rows):
    out = {'claims': len(rows), 'overdue': sum(1 for r in rows if r.get('overdue')),
           'dated': sum(1 for r in rows if r.get('ref_date')),
           'balance': round(sum(_num(r.get('balance')) for r in rows), 2)}
    for p in ('P1', 'P2', 'P3', 'P4'):
        out[p] = sum(1 for r in rows if r.get('priority') == p)
        out[p + '_balance'] = round(sum(_num(r.get('balance')) for r in rows if r.get('priority') == p), 2)
    return out


def _num(v):
    try:
        return float(str(v or '0').replace(',', '').replace('$', ''))
    except ValueError:
        return 0.0


def run_claim_priority(aws_client, body, login, get_page, open_claim, close_claim, create_claims=None):
    """The task. body: since, limit_claims, create_claims (run Claims IPE All
    on the encounters without a claim — writes to eCW), redo, today."""
    since = str(body.get('since') or DEFAULT_SINCE)
    limit = int(body.get('limit_claims') or 0)
    redo = bool(body.get('redo'))
    do_ipe = bool(body.get('create_claims'))
    today = datetime.strptime(body['today'], '%Y-%m-%d').date() if body.get('today') else date.today()
    logger.info(f"Priority — since={since} limit={limit or 'none'} create_claims={do_ipe} redo={redo} as of {today}")
    table = ensure_table(aws_client)
    sla = load_sla()
    run = {'started_at': _now(), 'steps': {}, 'order': []}

    def progress(step, text):
        run['steps'][step] = text
        if step not in run['order']:
            run['order'].append(step)
        run['updated_at'] = _now()
        _put(table, '_run', run)
        logger.info(f"▶ {step}: {text}")

    known = {str(it.get('claim_id')): it for it in scan_all(table) if not str(it.get('claim_id', '')).startswith('_')}
    page = get_page()
    if not login(page, aws_client.get_secret('ecw_credentials'), aws_client):
        progress('ecw', 'login failed')
        return {'ok': False, 'reason': 'eCW login failed'}

    if do_ipe and create_claims:
        enc_since = (today - timedelta(days=ENCOUNTER_DAYS)).strftime('%m/%d/%Y')
        progress('encounters', f'Progress Notes Done/Locked since {enc_since} → Claims IPE All')
        try:
            create_claims(page, enc_since)
        except Exception as e:
            logger.warning(f"  ⚠️ Claims IPE All step: {str(e)[:160]}")
    else:
        progress('encounters', 'skipped (create_claims off) — no claim is created in eCW')

    progress('ecw', 'listing the open claims…')
    claims, hdrs = list_open_claims(page, since)
    progress('ecw', f"{len(claims)} claim(s) with a balance since {since}")
    if limit:
        claims = claims[:limit]

    written, read, kept = 0, 0, 0
    for i, c in enumerate(claims, 1):
        cid = str(c['claim_id'])
        prev = known.get(cid, {})
        ref = prev.get('ref_date') or ''
        src = prev.get('ref_source') or ''
        snippet = prev.get('snippet') or ''
        era = bool(prev.get('era_payer')) if prev else None
        if redo or not ref:
            got = read_reference(page, c, open_claim, close_claim, shot=(i == 1))
            if got is not None:
                read += 1
                era = got['era_payer']
                ref = got['ref_date'].isoformat() if got['ref_date'] else ''
                src, snippet = got['ref_source'], got['snippet']
        else:
            kept += 1
        # No date on any log: age from the DOS, and say so.
        if not ref:
            dos = c.get('dos') or ''
            try:
                ref = datetime.strptime(dos[:10], '%m/%d/%Y').date().isoformat()
                src = src or 'DOS (no log date)'
            except ValueError:
                ref = ''
        days = (today - date.fromisoformat(ref)).days if ref else None
        status = c.get('status') or prev.get('status') or ''
        sla_days = sla_for(status, sla)
        row = {
            'patient': c.get('patient', ''), 'payer': c.get('payer', ''), 'dos': c.get('dos', ''), 'status': status,
            'charges': c.get('charges', ''), 'paid': c.get('paid', ''), 'balance': c.get('balance', ''),
            'era_payer': bool(era) if era is not None else False,
            'ref_date': ref, 'ref_source': src, 'snippet': snippet,
            'days': days if days is not None else -1, 'priority': priority_for(days),
            'sla_days': sla_days, 'overdue': bool(days is not None and days > sla_days),
            'reviewed_at': _now(), 'since': since, 'as_of': today.isoformat(),
        }
        _put(table, cid, row)
        written += 1
        if i % 10 == 0 or i == len(claims):
            progress('claims', f"{i}/{len(claims)} · logs read {read} · kept {kept}")
        time.sleep(0.2)

    rows = [it for it in scan_all(table) if not str(it.get('claim_id', '')).startswith('_')]
    seen = {str(c['claim_id']) for c in claims}
    summary = summarize([r for r in rows if r.get('claim_id') in seen] if limit else rows)
    _put(table, '_summary', {'since': since, 'run_at': _now(), 'as_of': today.isoformat(), 'run_claims': len(claims),
                             'create_claims': do_ipe, **summary})
    progress('done', f"{written} claim(s) prioritized · P1 {summary['P1']} · P2 {summary['P2']} · P3 {summary['P3']} · P4 {summary['P4']} · overdue {summary['overdue']}")
    return {'ok': True, 'claims': written, **summary}
