"""The posting plan for one check: every claim it paid, every line's money.

Input is the EOB as parsed per claim, and a way to fetch eCW's own lines for a
claim (the HCFA PDF in S3, read by src/eob/hcfa_lines.py). Output is exactly
what the posting step will type into eCW — and, where anything is uncertain,
the reason it will not.

Nothing here touches eCW or the portal, so every rule is testable and a plan
can be shown on the dashboard before a single payment is created.

Parsed EOB shape (produced by src/eob/parse.py):

    {'approve_to_pay': '262.69', 'check_amount': '263.67', 'interest': '0.98',
     'claims': [{'patient_account_number': '2627',
                 'bsc_claim_number': '260703051601',
                 'claim_paid': '262.69',
                 'lines': [{'cpt', 'units', 'billed', 'allowed',
                            'deductible', 'copay', 'paid'}, ...]}, ...]}
"""
from decimal import Decimal, InvalidOperation

from src.eob.hcfa_lines import total_billed
from src.eob.matching import plan_claim


def _d(v):
    try:
        return Decimal(str(v).replace('$', '').replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return None


def _sum(values):
    return sum((_d(v) or Decimal('0') for v in values), Decimal('0'))


def plan_check(parsed_eob, load_ecw_lines, claim_charges=None):
    """Plan every claim on a check.

    `load_ecw_lines(claim_id)` returns eCW's service lines for the claim (from
    its HCFA) or None when there is no HCFA to read. `claim_charges` maps
    claim_id -> the charges on record, used to prove no HCFA line was missed.

    Returns {'status', 'reasons', 'claims': [per-claim plan], 'totals': {...}}.
    A check is 'ready' only when every claim on it is.
    """
    claim_charges = claim_charges or {}
    plans, reasons = [], []
    for c in parsed_eob.get('claims') or []:
        claim_id = str(c.get('patient_account_number') or '').strip()
        base = {'claim_id': claim_id, 'bsc_claim_number': c.get('bsc_claim_number', ''),
                'claim_paid': c.get('claim_paid', '')}
        if not claim_id:
            plans.append({**base, 'status': 'needs_review', 'rows': [], 'unposted_ecw': [],
                          'reasons': ['EOB claim has no patient account number']})
            continue
        if not c.get('lines'):
            plans.append({**base, 'status': 'needs_review', 'rows': [], 'unposted_ecw': [],
                          'reasons': [f'the EOB lines for claim {claim_id} were not read']})
            continue
        ecw = load_ecw_lines(claim_id)
        if not ecw:
            plans.append({**base, 'status': 'needs_review', 'rows': [], 'unposted_ecw': [],
                          'reasons': [f'no HCFA lines on file for claim {claim_id}']})
            continue
        p = plan_claim(c.get('lines') or [], ecw, eob_claim_paid=c.get('claim_paid') or None)
        extra = []
        on_record = claim_charges.get(claim_id)
        if on_record not in (None, ''):
            got = total_billed(ecw)
            if _d(got) != _d(on_record):
                extra.append(f'HCFA lines add up to {got}, the claim is on record at '
                             f'{_d(on_record):.2f} — a line was not read')
        if c.get('adjusted_payment'):
            extra.append(f"Blue Shield adjusted an earlier payment on this claim (adjusted payment "
                         f"{c['adjusted_payment']}) — find the first posting in eCW before posting this one")
        status = 'needs_review' if (p['status'] != 'ready' or extra) else 'ready'
        plans.append({**base, **p, 'status': status, 'reasons': p['reasons'] + extra,
                      'ecw_lines': ecw})

    claims = parsed_eob.get('claims') or []
    paid = _sum(c.get('claim_paid') for c in claims)
    atp = _d(parsed_eob.get('approve_to_pay'))
    if atp is not None and paid != atp:
        reasons.append(f'claims paid add up to {paid:.2f}, the EOB approves {atp:.2f}')
    if not plans:
        reasons.append('the EOB lists no claims')
    if parsed_eob.get('payment_issued_to'):
        reasons.append(f"Blue Shield paid the member, not the clinic (check amount "
                       f"{parsed_eob.get('check_amount') or '0.00'}) — there is no insurance payment to post")
    offsets = _d(parsed_eob.get('offsets'))
    if offsets:
        reasons.append(f'Blue Shield took an offset of {offsets:.2f} out of this check — '
                       f'the posting has to account for it')
    if parsed_eob.get('adjusted_claim') and not any(c.get('adjusted_payment') for c in claims):
        reasons.append('the EOB says it adjusts a previously processed claim')
    # The report disagreeing with itself means something was misread.
    reasons.extend(parsed_eob.get('problems') or [])

    ready = not reasons and all(p['status'] == 'ready' for p in plans)
    return {
        'status': 'ready' if ready else 'needs_review',
        'reasons': reasons,
        'claims': plans,
        'totals': {
            'claims_paid': f'{paid:.2f}',
            'approve_to_pay': parsed_eob.get('approve_to_pay', ''),
            'interest': parsed_eob.get('interest', ''),
            'check_amount': parsed_eob.get('check_amount', ''),
        },
    }


def _stored(item):
    """What capture stored for a check, in plan_check's shape."""
    return {
        'eob_number': item.get('eob_number', ''),
        'approve_to_pay': item.get('approve_to_pay', ''),
        'interest': item.get('eob_interest', ''),
        'offsets': item.get('eob_offsets', ''),
        'check_amount': item.get('eob_check_amount', ''),
        'payment_issued_to': 'the member' if item.get('paid_to_member') else '',
        'adjusted_claim': bool(item.get('eob_adjusted_claim')),
        'problems': list(item.get('eob_problems') or []),
        'claims': list(item.get('eob_claims') or []),
    }


def plan_from_item(eob_item, get_claim, read_hcfa, read_eob=None):
    """The plan for a stored check (a helixona-eobs item).

    `get_claim(claim_id)` returns our claim record or None; `read_hcfa(s3_path)`
    returns the HCFA's service lines; `read_eob(s3_path)` re-reads the EOB
    report, so a check captured before a parser fix is planned from its
    report rather than from what was stored at the time.
    """
    parsed, notes = None, []
    pdf = eob_item.get('eob_pdf_s3_path') or ''
    if read_eob and pdf:
        try:
            parsed = read_eob(pdf)
        except Exception as e:
            notes.append(f'the EOB report could not be re-read ({e}); planned from what was stored')
        if parsed is not None and not parsed.get('parsed_ok'):
            notes.append('the EOB report could not be read by position; planned from what was stored')
            parsed = None
    if parsed is None:
        parsed = _stored(eob_item)

    records = {}
    for c in parsed.get('claims') or []:
        cid = str(c.get('patient_account_number') or '').strip()
        if cid and cid not in records:
            records[cid] = get_claim(cid) or {}

    def load(cid):
        path = records.get(cid, {}).get('hcfa_s3_path') or ''
        if not path:
            return None
        try:
            return read_hcfa(path)
        except Exception:
            return None

    charges = {cid: r['charges'] for cid, r in records.items() if r.get('charges') not in (None, '')}
    plan = plan_check(parsed, load, claim_charges=charges)
    plan.update({'check_eft': eob_item.get('check_eft', ''), 'eob_number': parsed.get('eob_number', ''),
                 'notes': notes})
    return plan
