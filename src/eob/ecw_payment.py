"""What gets typed into eCW for a check — the values, not the clicking.

Keeping this apart from the browser work means the numbers that land in the
ledger are decided by tested code, and a plan can be shown to a person before
anything is entered. docs/ecw_posting.md records where each rule comes from.

Two things here are easy to get wrong and cost money:

* the payment's amount is the EOB's APPROVE-TO-PAY, not the check's amount.
  Blue Shield's check includes interest ($262.69 approved, $0.98 interest,
  $263.67 paid), and interest is not a payment on any claim.
* eCW warns when Deduct + CoIns + CoPay + Paid + Adjust + Withheld exceeds the
  billed fee. Adjust is Billed − Allowed, so that happens exactly when
  deductible + co-pay + paid exceeds the allowed amount — the arithmetic of a
  value typed onto the wrong line. `line_overflows` predicts it so the bot
  never has to answer that dialog.
"""
from decimal import Decimal, InvalidOperation

# eCW's own column names, so the browser step and the tests speak one language.
TYPED_COLUMNS = ('Allowed', 'Deduct', 'CoPay', 'Paid')


def _d(v):
    try:
        return Decimal(str(v).replace('$', '').replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return None


def payment_header(eob, check, tracker=None, policy=None):
    """The `Payments` popup for one check: {field label: value or None}.

    `eob` is the parsed report (src/eob/eob_pdf.py), `check` the portal's
    Check/EFT summary (check_eft, check_date, cashed_date). Fields the operator
    leaves alone are None, so the browser step knows not to touch them.

    `tracker` is what Karina's Insurance Check Tracker says of the check
    ({'received', 'deposit'}) and `policy` data/ecw_posting_policy.json: with
    them the Received and Deposit dates come from the tracker (the B10 flow,
    2026-11) and the Facility is selected. Without them the popup is the one
    of the September recordings.

    Returns (fields, problems). Any problem means do not open the popup.
    """
    tracker, policy = tracker or {}, policy or {}
    problems = []
    amount = _d(eob.get('approve_to_pay'))
    zero = amount is not None and amount == 0 and bool(policy.get('post_zero_eobs'))
    if zero:
        # A $0 EOB (deductible, denial): no check exists. Vignesh, the B10b
        # call: Type = Non Payment Data, the EOB number in the check number
        # box, and the received, check, EOB and deposit dates all the same.
        return zero_header(eob, check, policy)
    if amount is None:
        problems.append('the EOB has no approve-to-pay amount')
    elif amount <= 0:
        problems.append(f'the EOB approves {amount:.2f} — there is nothing to post')
    if eob.get('payment_issued_to'):
        problems.append('Blue Shield paid the member, not the clinic — there is no payment to enter')
    check_no = str(check.get('check_eft') or '').strip()
    if not check_no:
        problems.append('the check has no Check/EFT number')
    check_date = str(check.get('check_date') or '').strip()
    if not check_date:
        problems.append('the check has no Check/EFT date')
    deposit = str(check.get('cashed_date') or '').strip()
    if policy.get('deposit_date') == 'tracker' and str(tracker.get('deposit') or '').strip():
        deposit = str(tracker['deposit']).strip()
    if not deposit:
        problems.append('no deposit date: the tracker has none and Blue Shield shows no cashed date')
    received = str(tracker.get('received') or '').strip() if policy.get('received_date') == 'tracker' else ''

    fields = {
        'Facility': 'select' if policy.get('facility') == 'select' else None,
        'Type': 'Check',
        'Check No.': check_no,
        'Batch No.': None,                    # left at 0
        'Amount $': f'{amount:.2f}' if amount is not None else '',
        'Received Date': received or None,    # the tracker's, else left at today
        'Check Date': check_date,
        # The operator puts the check's date in both boxes, not the EOB's
        # receipt date. docs/ecw_posting.md: confirm this is policy.
        'EOB Date': check_date,
        'Deposit Date': deposit,
        'Notes': None,                        # left empty
    }
    return fields, problems


def zero_header(eob, check, policy=None):
    """The popup for a $0 EOB: nothing was paid, so there is no check — the
    EOB's own number stands in for it and one date fills all four boxes."""
    policy = policy or {}
    problems = []
    number = str(eob.get('eob_number') or check.get('eob_number') or '').strip()
    if not number:
        problems.append('the $0 EOB has no EOB number to use as the check number')
    when = str(check.get('check_date') or check.get('eob_issue_date') or eob.get('issue_date') or '').strip()
    if not when:
        problems.append('the $0 EOB has no date')
    return {
        'Facility': 'select' if policy.get('facility') == 'select' else None,
        'Type': 'Non Payment Data',
        'Check No.': number,
        'Batch No.': None,
        'Amount $': '0.00',
        'Received Date': when or None,
        'Check Date': when,
        'EOB Date': when,
        'Deposit Date': when,
        'Notes': None,
    }, problems


def is_denied(row):
    """A line the payer denied: billed, and nothing allowed and nothing paid.
    eCW has no denial column — the line takes Allowed 0, Paid 0 and the
    EOB's reason code (PI 16 in the demo) in its Code box."""
    allowed, paid = _d(row.get('allowed')), _d(row.get('paid'))
    billed = _d(row.get('ecw_billed') or row.get('eob_billed') or row.get('billed'))
    return allowed is not None and paid is not None and allowed == 0 and paid == 0 and (billed is None or billed > 0)


def claim_status_for(rows):
    """The status the claim is set to once its payment is posted — Vignesh,
    the B10c call: "it's always these three statuses".

        even one denied line                          → ERA Payer Denied
        no denial, any deductible / coinsurance / copay → Patient
        no denial and nothing owed by the patient       → Paid in Full
    """
    rows = list(rows or [])
    if any(is_denied(r) for r in rows):
        return 'ERA Payer Denied'
    owed = sum(((_d(r.get(k)) or Decimal('0')) for r in rows for k in ('deductible', 'coinsurance', 'copay')), Decimal('0'))
    return 'Patient' if owed > 0 else 'Paid in Full'


def line_overflows(row, ecw_billed):
    """True when eCW would warn that this line's amounts exceed its billed fee.

    The warning fires on Deduct + CoIns + CoPay + Paid + Adjust + Withheld >
    Billed, and Adjust is Billed − Allowed, so it reduces to
    deductible + co-pay + paid > allowed.
    """
    parts = [_d(row.get('deductible')), _d(row.get('copay')), _d(row.get('paid')), _d(row.get('allowed'))]
    if any(p is None for p in parts):
        return False
    ded, copay, paid, allowed = parts
    coins = _d(row.get('coinsurance')) or Decimal('0')
    return ded + coins + copay + paid > allowed


def grid_rows(claim_plan):
    """What to type into the Payment Posting grid, one entry per planned line.

    Each entry names the eCW line to find (its index on the claim, its code,
    billed amount and drug) and the four cells to fill. CoIns and Withheld are
    not typed; Adjust is eCW's own arithmetic.

    Returns (rows, problems).
    """
    rows, problems = [], []
    for r in claim_plan.get('rows') or []:
        values = {'Allowed': r.get('allowed', ''), 'Deduct': r.get('deductible', ''),
                  'CoPay': r.get('copay', ''), 'Paid': r.get('paid', '')}
        # Coinsurance has its own column in eCW; Blue Shield's EOB has none,
        # UHC's PRA does (PR2 = coinsurance, PR3 = copay). Typed only when
        # the EOB gave one.
        if _d(r.get('coinsurance')) not in (None, Decimal('0')):
            values['CoIns'] = r.get('coinsurance')
        if any(_d(v) is None for v in values.values()):
            problems.append(f"{r.get('cpt')}: the EOB amounts for this line were not all read")
            continue
        if line_overflows(r, r.get('ecw_billed')):
            problems.append(
                f"{r.get('cpt')} billed {r.get('ecw_billed')}: deductible + co-pay + paid comes to more "
                f"than the allowed {r.get('allowed')} — eCW would refuse the line")
            continue
        rows.append({
            'ecw_index': r.get('ecw_index'),
            'cpt': r.get('cpt'),
            'ecw_billed': r.get('ecw_billed', ''),
            'drug': r.get('ecw_drug', ''),
            'values': values,
            'denied': is_denied(r),
            'reason_code': str(r.get('reason_code') or ''),
        })
    return rows, problems


def posting_total(rows):
    """What the grid's Paid column will add up to once these rows are typed."""
    return f"{sum((_d(r['values']['Paid']) or Decimal('0') for r in rows), Decimal('0')):.2f}"
