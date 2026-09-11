"""The posting plan for one cheque: every claim it paid, every line's money.

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


def plan_cheque(parsed_eob, load_ecw_lines, claim_charges=None):
    """Plan every claim on a cheque.

    `load_ecw_lines(claim_id)` returns eCW's service lines for the claim (from
    its HCFA) or None when there is no HCFA to read. `claim_charges` maps
    claim_id -> the charges on record, used to prove no HCFA line was missed.

    Returns {'status', 'reasons', 'claims': [per-claim plan], 'totals': {...}}.
    A cheque is 'ready' only when every claim on it is.
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
        status = 'needs_review' if (p['status'] != 'ready' or extra) else 'ready'
        plans.append({**base, **p, 'status': status, 'reasons': p['reasons'] + extra,
                      'ecw_lines': ecw})

    paid = _sum(c.get('claim_paid') for c in parsed_eob.get('claims') or [])
    atp = _d(parsed_eob.get('approve_to_pay'))
    if atp is not None and paid != atp:
        reasons.append(f'claims paid add up to {paid:.2f}, the EOB approves {atp:.2f}')
    if not plans:
        reasons.append('the EOB lists no claims')

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
