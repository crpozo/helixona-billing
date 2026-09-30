"""Denials — which claims eCW shows as denied, why, and what the SOP says to
do about each.

    1. eCW › Billing › Claims, Service Dt from `since`, Claim Status = each
       denied status the screen offers: the denied claims.
    2. Each claim opened once for its reason codes (CO-16, N846, B16,
       Duplicate…); a claim whose codes were read before keeps them unless
       `redo` is set.
    3. The SOP's cheat sheet matched on payer, procedure code and reason:
       the action — write off, inquiry, appeal, medical records, recode — or
       "review" when no rule fits.

Everything lands in helixona-denials, one item per claim, with `_run` as the
live trace and `_summary` the counts. Read-only in eCW.
"""
import time
from datetime import datetime

from src.aws.clients import scan_all
from src.denials.cheatsheet import load_rules, match, procedure_codes
from src.denials.ecw import list_denied, read_reasons
from src.utils.logger import get_logger

logger = get_logger(__name__)

DENIALS_TABLE = 'helixona-denials'
DEFAULT_SINCE = '07/01/2025'


def _now():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')


def ensure_table(aws_client):
    ddb = aws_client.dynamodb
    if DENIALS_TABLE in [t.name for t in ddb.tables.all()]:
        return ddb.Table(DENIALS_TABLE)
    logger.info(f"🆕 creating table {DENIALS_TABLE}")
    t = ddb.create_table(TableName=DENIALS_TABLE,
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


def summarize(rows):
    kinds = ('write off', 'inquiry', 'appeal', 'medical records', 'recode', 'review')
    out = {'claims': len(rows), 'with_codes': sum(bool(r.get('denial_codes')) for r in rows),
           'matched': sum(bool(r.get('sop_rows')) for r in rows)}
    for k in kinds:
        out[k.replace(' ', '_')] = sum(r.get('action_kind') == k for r in rows)
    return out


def decide(claim, codes, rules):
    """The SOP's answer for one claim: (matched rules, action_kind, action)."""
    hits = match({'cpt': claim.get('cpt', ''), 'payer': claim.get('payer', ''), 'reasons': codes or []}, rules)
    if not hits:
        return [], 'review', ''
    kinds = {h.kind for h in hits}
    kind = hits[0].kind if len(kinds) == 1 else 'review'
    action = hits[0].action if len(hits) == 1 else ' || '.join(f"[SOP {h.sop_row} · {h.drug or h.codes}] {h.action}" for h in hits)
    return [h.as_dict() for h in hits], kind, action


def run_claim_denials(aws_client, body, login, get_page, open_claim, close_claim):
    since = str(body.get('since') or DEFAULT_SINCE)
    statuses = body.get('statuses') or None
    limit = int(body.get('limit_claims') or 0)
    redo = bool(body.get('redo'))
    do_reasons = body.get('reasons', True)
    logger.info(f"Denials — since={since} statuses={statuses or 'default'} limit={limit or 'none'} reasons={do_reasons} redo={redo}")
    table = ensure_table(aws_client)
    run = {'started_at': _now(), 'steps': {}, 'order': []}

    def progress(step, text):
        run['steps'][step] = text
        if step not in run['order']:
            run['order'].append(step)
        run['updated_at'] = _now()
        _put(table, '_run', run)
        logger.info(f"▶ {step}: {text}")

    rules, _ = load_rules()
    progress('sop', f"{len(rules)} rule(s) in the cheat sheet")
    known = {str(it.get('claim_id')): it for it in scan_all(table) if not str(it.get('claim_id', '')).startswith('_')}

    page = get_page()
    if not login(page, aws_client.get_secret('ecw_credentials'), aws_client):
        progress('ecw', 'login failed')
        return {'ok': False, 'reason': 'eCW login failed'}
    progress('ecw', 'listing the denied claims…')
    claims, used, options = list_denied(page, since, statuses)
    progress('ecw', f"{len(claims)} denied claim(s) under {used or 'no status'}")
    if not claims:
        _put(table, '_summary', {'since': since, 'run_at': _now(), 'statuses': used, 'status_options': options[:60], **summarize(list(known.values()))})
        progress('done', 'nothing to review' if used else 'the denied statuses were not found — see the log')
        return {'ok': True, 'claims': 0}
    if limit:
        claims = claims[:limit]

    written, read, kept = 0, 0, 0
    for i, c in enumerate(claims, 1):
        cid = str(c['claim_id'])
        prev = known.get(cid, {})
        codes = list(prev.get('denial_codes') or [])
        snippet = str(prev.get('denial_text') or '')
        cpt = procedure_codes(c.get('cpt', '')) or list(prev.get('cpt_codes') or [])
        if do_reasons and (redo or not codes or not cpt):
            got = read_reasons(page, cid, open_claim, close_claim, shot=(i == 1))
            if got is None:
                pass            # could not open: what an earlier run read stands
            else:
                codes, snippet, read = got['codes'], got['snippet'] or snippet, read + 1
                cpt = got['cpt'] or cpt
        elif codes:
            kept += 1
        # The grid has no CPT column: the codes billed come off the claim.
        c = {**c, 'cpt': c.get('cpt') or ' '.join(cpt)}
        hits, kind, action = decide(c, codes, rules)
        _put(table, cid, {
            'patient': c.get('patient', ''), 'payer': c.get('payer', ''), 'dos': c.get('dos', ''), 'cpt': c.get('cpt', ''),
            'cpt_codes': cpt, 'charges': c.get('charges', ''), 'paid': c.get('paid', ''),
            'adjustment': c.get('adjustment', ''), 'balance': c.get('balance', ''), 'ecw_status': c.get('status', '') or c.get('ecw_status_filter', ''),
            'ecw_status_filter': c.get('ecw_status_filter', ''), 'denial_codes': codes, 'denial_text': snippet,
            'sop_rows': [h['sop_row'] for h in hits], 'sop_matches': hits, 'action_kind': kind, 'action': action,
            'reviewed_at': _now(), 'since': since})
        written += 1
        if i % 10 == 0 or i == len(claims):
            progress('reasons', f"{i}/{len(claims)} · codes read {read} · kept {kept}")
        time.sleep(0.3)

    rows = [it for it in scan_all(table) if not str(it.get('claim_id', '')).startswith('_')]
    sm = summarize(rows)
    _put(table, '_summary', {'since': since, 'run_at': _now(), 'statuses': used, 'status_options': options[:60], 'run_claims': written, **sm})
    progress('done', f"{written} claim(s) reviewed · {sm['matched']} with an SOP action · {sm['review']} for a person")
    logger.info(f"═══ Denials: {written} reviewed · {sm} ═══")
    return {'ok': True, 'claims': written, 'summary': sm}
