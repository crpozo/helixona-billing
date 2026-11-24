"""B10 — which payments are ready to be entered in eCW.

The billing team's rule, as Vignesh and Karina described it (the B10 flows,
2026-11): an EOB only says the payer ISSUED a check. The payment is entered
in eCW when the money is in the bank — and the sign of that is the check's
number in Karina's Insurance Check Tracker (her column E means deposited).

    $0 EOB (deductible, denial)   no check exists: post right away
    non-zero EOB                  wait for the check number in the tracker
    in the tracker, no EOB file   download the EOB again from the portal
    in the tracker, EOB saved     READY — enter it in eCW
    other payers (Cigna, UMR…)    no portal: the scan in Unposted Checks is
                                  the EOB; the tracker confirms the deposit

One row per check, with the stage it is at and why. Pure: the rows of
helixona-eobs (what the portal walk captured) and of helixona-checks (the
tracker, the scans, eCW's payments), nothing else.
"""
import json
import os
import re
from decimal import Decimal, InvalidOperation

from src.checks.reconcile import norm_check, payee_kind
from src.eob.parse import norm_text

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
POLICY_FILE = os.path.join(_ROOT, 'data', 'ecw_posting_policy.json')
DEFAULT_POLICY = {'amount': 'approve_to_pay', 'received_date': 'tracker', 'deposit_date': 'tracker', 'facility': 'select',
                  'attach_eob': True, 'commit_buttons': ['Post Payment', 'Post Payment (F4)', 'Auto Post', 'Auto Post (F2)'],
                  'set_claim_status': True, 'multi_claim_checks': False, 'post_zero_eobs': False, 'post_denied_lines': False}
# Worked first to last on the tab.
STAGES = ('ready', 'EOB missing', 'needs a person', 'held', '$0 EOB', 'waiting for deposit', 'other payer', 'posted', 'voided')


def load_policy(path=None):
    try:
        with open(path or POLICY_FILE, encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    return {**DEFAULT_POLICY, **{k: v for k, v in raw.items() if not str(k).startswith('_')}}


def _d(v):
    try:
        return Decimal(str(v).replace('$', '').replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return None


def tracker_of(check_row):
    """What the tracker says of a check: {'deposited', 'received', 'deposit'}."""
    h = check_row or {}
    return {'deposited': bool(h.get('in_tracker')), 'received': str(h.get('tracker_received') or ''),
            'deposit': str(h.get('tracker_deposit') or '')}


def earlier_payments(eobs):
    """{check: (claim, the earlier check)} for every check that pays a claim
    an earlier check already paid — an adjusted payment (the B10d call): a
    corrected claim paid again under a new check number. Its EOB lists every
    line as paid again, so posting it needs the reversal (REV) steps."""
    by_claim = {}
    for e in eobs or ():
        key = norm_check(e.get('check_eft'))
        for c in (e.get('claims') or []):
            if isinstance(c, dict) and c.get('claim_id') and key:
                by_claim.setdefault(str(c['claim_id']), []).append((_sort_date(e.get('check_date')), key))
    out = {}
    for claim, checks in by_claim.items():
        checks = sorted(set(checks))
        for (_, later) in checks[1:]:
            out.setdefault(later, (claim, checks[0][1]))
    return out


def _sort_date(text):
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', str(text or ''))
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else '9999'


def stage_of(eob, check_row, policy=None, adjusted=None):
    """(stage, reason) for one Blue Shield check. `adjusted`: (claim, the
    earlier check) when this check pays a claim an earlier one already paid."""
    policy = policy or DEFAULT_POLICY
    h = check_row or {}
    res = eob.get('post_result') or {}
    res_status = str(res.get('status') or '') if isinstance(res, dict) else ''
    amount = _d(eob.get('check_amount'))
    status = norm_text(eob.get('check_status'))
    if h.get('verdict') == 'posted':
        return 'posted', 'eCW has the payment, nothing left unposted'
    if h.get('in_ecw'):
        return 'needs a person', f"eCW has the payment with ${h.get('ecw_unposted') or '?'} still unposted — finish it"
    if res_status in ('incomplete', 'unbalanced'):
        return 'needs a person', str(res.get('reason') or 'the bot created the payment and could not finish it')[:300]
    if res_status in ('posted', 'existing') and eob.get('posted_in_ecw'):
        return 'posted', str(res.get('reason') or 'entered by the bot')[:200]
    if re.search(r'void|stop', status, re.I):
        return 'voided', 'Blue Shield voided or stopped the check'
    if amount is not None and amount == 0:
        if not policy.get('post_zero_eobs'):
            return '$0 EOB', 'no check exists — to be posted as Non Payment Data; held until the denial steps are defined'
        return ('ready', '$0 EOB: posted right away, no deposit to wait for') if eob.get('eob_pdf_s3_path') \
            else ('EOB missing', 'the $0 EOB was not downloaded')
    t = tracker_of(h)
    if not t['deposited']:
        who = ' (made out to the patient, who brings it in)' if payee_kind(
            {'bs_payee': eob.get('payee_name', ''), 'bs_paid_to_member': bool(eob.get('paid_to_member')),
             'bs_patients': ' · '.join(norm_text(c.get('member_name')) for c in (eob.get('claims') or []) if isinstance(c, dict))}
        ) == 'patient' else ''
        return 'waiting for deposit', f"the check number is not in the tracker yet{who}"
    if not eob.get('eob_pdf_s3_path'):
        return 'EOB missing', 'deposited per the tracker, but the EOB file is not saved — download it again'
    claims = [c for c in (eob.get('claims') or []) if isinstance(c, dict)]
    if adjusted:
        return 'held', (f"a second payment for claim {adjusted[0]} (first: check {adjusted[1]}) — an adjusted payment needs the "
                        f"reversal steps (REV); by hand for now")
    if len(claims) > 1 and not policy.get('multi_claim_checks'):
        return 'held', f"the check pays {len(claims)} claims — 'Add insurance payment' is not covered yet"
    if res_status == 'held':
        return 'held', str(res.get('reason') or 'the posting plan is not complete')[:300]
    if res_status == 'failed':
        return 'ready', 'last attempt did not get through the eCW screens: ' + str(res.get('reason') or '')[:200]
    if res_status == 'dry_run':
        return 'ready', 'dry run done: ' + str(res.get('reason') or '')[:200]
    return 'ready', 'deposited per the tracker and the EOB is saved'


def ready_queue(eobs, check_rows, policy=None):
    """One row per check, in working order."""
    policy = policy or DEFAULT_POLICY
    by = {}
    for r in check_rows or ():
        key = str(r.get('check_number') or '')
        if key and not key.startswith(('_', 'deposit:', 'unreadable:')):
            by[norm_check(key)] = r
    rows, seen = [], set()
    eobs = list(eobs or ())
    again = earlier_payments(eobs)
    for e in eobs:
        key = norm_check(e.get('check_eft'))
        if not key:
            continue
        seen.add(key)
        h = by.get(key, {})
        stage, reason = stage_of(e, h, policy, again.get(key))
        claims = [c for c in (e.get('claims') or []) if isinstance(c, dict)]
        res = e.get('post_result') if isinstance(e.get('post_result'), dict) else {}
        t = tracker_of(h)
        rows.append({
            'check': key, 'check_as_printed': str(e.get('check_eft') or ''), 'payer': 'Blue Shield of California',
            'amount': str(e.get('check_amount') or ''), 'approve_to_pay': str(e.get('approve_to_pay') or ''),
            'check_date': str(e.get('check_date') or ''), 'bs_status': norm_text(e.get('check_status')),
            'stage': stage, 'reason': reason,
            'in_tracker': t['deposited'], 'tracker_received': t['received'], 'tracker_deposit': t['deposit'],
            'has_eob': bool(e.get('eob_pdf_s3_path')), 'has_scan': bool(h.get('has_copy')), 'scan_file': str(h.get('copy_file') or ''),
            'claim_ids': sorted({str(c.get('claim_id')) for c in claims if c.get('claim_id')}),
            'patients': sorted({norm_text(c.get('member_name')) for c in claims if c.get('member_name')})[:3],
            'claims': len(claims),
            'last_attempt': str(res.get('status') or ''), 'last_attempt_at': str(e.get('post_attempted_at') or ''),
            'last_mode': str(e.get('post_mode') or ''), 'payment_id': str(res.get('payment_id') or ''),
            'claim_status': str(res.get('claim_status') or ''),
        })
    # Other payers: a scan the tracker confirms, that the Blue Shield portal does not list.
    for key, h in by.items():
        if key in seen or h.get('in_blue_shield') or not (h.get('has_copy') and h.get('in_tracker')):
            continue
        if h.get('verdict') == 'posted':
            stage, reason = 'posted', 'eCW has the payment, nothing left unposted'
        elif h.get('in_ecw'):
            stage, reason = 'needs a person', f"eCW has the payment with ${h.get('ecw_unposted') or '?'} still unposted — finish it"
        else:
            stage, reason = 'other payer', 'the EOB is in the scan; reading another payer\'s EOB is not automated yet — a person posts it'
        rows.append({
            'check': key, 'check_as_printed': str(h.get('copy_check_raw') or key),
            'payer': str(h.get('tracker_payer') or h.get('copy_payer') or 'other payer'),
            'amount': str(h.get('copy_amount') or h.get('tracker_amount') or ''), 'approve_to_pay': '',
            'check_date': str(h.get('copy_date') or ''), 'bs_status': '', 'stage': stage, 'reason': reason,
            'in_tracker': True, 'tracker_received': str(h.get('tracker_received') or ''),
            'tracker_deposit': str(h.get('tracker_deposit') or ''), 'has_eob': True, 'has_scan': True,
            'scan_file': str(h.get('copy_file') or ''), 'claim_ids': [str(h['tracker_claim'])] if h.get('tracker_claim') else [],
            'patients': [], 'claims': 1 if h.get('tracker_claim') else 0, 'last_attempt': '', 'last_attempt_at': '',
            'last_mode': '', 'payment_id': str(h.get('ecw_payment_id') or ''), 'claim_status': '',
        })
    order = {s: i for i, s in enumerate(STAGES)}
    rows.sort(key=lambda r: (order.get(r['stage'], 99), r['tracker_deposit'] or r['check_date'] or '', r['check']))
    return rows


def eob_wanted(rows):
    """The checks whose EOB the next portal walk must download again."""
    return [r['check_as_printed'] or r['check'] for r in rows if r['stage'] == 'EOB missing']


def summarize(rows):
    def money(sel):
        return f"{sum((_d(r.get('amount')) or Decimal(0) for r in rows if sel(r)), Decimal(0)):.2f}"
    out = {'checks': len(rows)}
    for st in STAGES:
        key = st.replace('$0 EOB', 'zero').replace(' ', '_')
        out[key] = sum(1 for r in rows if r['stage'] == st)
        out[key + '_amount'] = money(lambda r, st=st: r['stage'] == st)
    return out
