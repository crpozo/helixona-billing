"""Three lists of checks, one answer per check number.

* copies   — check images from SharePoint (or the S3 inbox), as read by
             src/checks/read_check.py: {'check_number', 'amount', 'file', ...}
* checks  — what Blue Shield says (the helixona-eobs table Remittance
             captured): {'check_eft', 'check_amount', 'check_status',
             'check_date', 'cashed_date', ...}
* payments — what eCW has on file (Billing → Payments since a date):
             {'check_no', 'amount', 'posted', 'unposted', 'payment_id', ...}
* eras     — the 835s eCW holds and nobody posted (Billing → ERA, Posting
             Status UnPosted): {'check_no' (the trace number), 'amount',
             'era_file', 'payer', 'dated', ...}
* tracker  — the billing team's own sheet of checks received (Insurance
             Check Tracker): {'check_no', 'amount', 'received',
             'deposit_date', 'payer', 'posted', 'sheet', 'row'}

The verdict, per check — eCW is the source of truth (the operator,
2026-09-18: "if it is not in eCW, we do not have the check"), whatever the
payer:

    posted           in eCW, nothing left unposted
    unposted         in eCW, but a balance is still unposted — the payment
                     was created and the lines never finished
    not cashed       a Blue Shield check the bank has not cashed yet —
                     nothing to enter
    not in eCW       eCW has no payment under the number: a cashed Blue
                     Shield check, or a scanned check from any payer
    eCW not checked  eCW was not read this run, so nothing can be said

An ERA is the payer saying "this payment is yours". It proves the money was
sent; it does not prove the check reached Helixona, and unposted means
nobody applied it either. So an ERA never settles a check on its own: it
adds the flag `835 in eCW, not posted` and, when nothing else knows the
number, the verdict `835 only`.

The tracker is the one source that says Helixona received the check. A
check in it with no scan found in any SharePoint folder is flagged
`in tracker, no scan found` — the team has it, the image is missing or
misfiled — and not `no copy of the check`, which is kept for the cashed
checks nobody logged. A check the tracker lists is not `835 only` either:
Helixona confirmed it.

Flags alongside: `no copy of the check` (Blue Shield cashed it, no scan),
`other payer` (Blue Shield's results do not list it — Cigna, Aetna…, or
outside the search window; informational), `amounts differ: …`. Pure: no
browser, no AWS.
"""
from decimal import Decimal, InvalidOperation

from src.eob.parse import money, norm_text


def norm_check(s):
    """A check number as a key: digits only, leading zeros dropped — a scan
    reads 0031401901 where the portal prints 31401901."""
    digits = ''.join(ch for ch in str(s or '') if ch.isdigit())
    return digits.lstrip('0') or ('0' if digits else '')


def _d(v):
    try:
        return Decimal(str(v).replace('$', '').replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return None


def _cashed(status):
    return 'cashed' in norm_text(status).lower()


def reconcile(copies, checks, payments, ecw_checked=True, eras=(), tracker=()):
    """Rows keyed by check number, plus a summary.

    `ecw_checked`: True/False for every check, or the set of check numbers
    that were looked up in eCW this run — the rest say 'eCW not checked'
    instead of 'not in eCW'.
    """
    by = {}
    if isinstance(ecw_checked, (set, frozenset, list, tuple)):
        checked_keys = {norm_check(k) for k in ecw_checked}
        checked = lambda key: key in checked_keys
    else:
        checked = lambda key: bool(ecw_checked)

    def row(key):
        return by.setdefault(key, {
            'check_number': key, 'has_copy': False, 'copy_amount': '', 'copy_file': '', 'copy_url': '',
            'copy_date': '', 'in_blue_shield': False, 'bs_amount': '', 'bs_status': '', 'bs_date': '',
            'cashed_date': '', 'in_ecw': False, 'ecw_amount': '', 'ecw_posted': '', 'ecw_unposted': '',
            'ecw_payment_id': '', 'in_era': False, 'era_amount': '', 'era_file': '',
            'era_dated': '', 'era_payer': '', 'in_tracker': False, 'tracker_received': '',
            'tracker_deposit': '', 'tracker_amount': '', 'tracker_payer': '', 'tracker_posted': '',
            'tracker_sheet': '', 'tracker_claim': '', 'flags': [], 'verdict': '',
        })

    for c in copies:
        key = norm_check(c.get('check_number'))
        if not key:
            continue
        r = row(key)
        r.update(has_copy=True, copy_amount=money(c.get('amount')), copy_file=c.get('file', ''),
                 copy_url=c.get('url', ''), copy_date=c.get('check_date', ''))
    for q in checks:
        key = norm_check(q.get('check_eft'))
        if not key:
            continue
        r = row(key)
        r.update(in_blue_shield=True, bs_amount=money(q.get('check_amount')), bs_status=q.get('check_status', ''),
                 bs_date=q.get('check_date', ''), cashed_date=q.get('cashed_date', ''))
    for p in payments:
        key = norm_check(p.get('check_no'))
        if not key:
            continue
        r = row(key)
        r.update(in_ecw=True, ecw_amount=money(p.get('amount')), ecw_posted=money(p.get('posted')),
                 ecw_unposted=money(p.get('unposted')), ecw_payment_id=str(p.get('payment_id', '')))

    for e in eras or ():
        key = norm_check(e.get('check_no'))
        if not key:
            continue
        r = row(key)
        r.update(in_era=True, era_amount=money(e.get('amount')), era_file=str(e.get('era_file', '')),
                 era_dated=e.get('dated', ''), era_payer=e.get('payer', ''))

    for t in tracker or ():
        key = norm_check(t.get('check_no'))
        if not key:
            continue
        r = row(key)
        # The same check logged twice keeps the first row: the sheet's own order.
        if not r['in_tracker']:
            r.update(in_tracker=True, tracker_received=t.get('received', ''),
                     tracker_deposit=t.get('deposit_date', ''), tracker_amount=money(t.get('amount')),
                     tracker_payer=t.get('payer', ''), tracker_posted=t.get('posted', ''),
                     tracker_claim=t.get('claim_no', ''),
                     tracker_sheet=f"{t.get('sheet', '')}:{t.get('row', '')}".strip(':'))

    for r in by.values():
        cashed = _cashed(r['bs_status']) or bool(r['cashed_date'])
        if r['in_ecw']:
            unposted = _d(r['ecw_unposted'])
            r['verdict'] = 'unposted' if (unposted is not None and unposted > 0) else 'posted'
        elif not checked(r['check_number']):
            r['verdict'] = 'eCW not checked'
        elif r['in_blue_shield'] and not cashed:
            r['verdict'] = 'not cashed'
        elif r['in_era'] and not r['in_blue_shield'] and not r['has_copy'] and not r['in_tracker']:
            # Only the 835 knows this payment: no scan, no portal row, no
            # payment in eCW. The payer sent money nobody has accounted for.
            r['verdict'] = '835 only'
        else:
            r['verdict'] = 'not in eCW'

        if r['in_era'] and r['verdict'] != 'posted':
            r['flags'].append('835 in eCW, not posted')
        if not r['in_blue_shield']:
            r['flags'].append('other payer')
        if r['in_tracker'] and not r['has_copy']:
            r['flags'].append('in tracker, no scan found')
        elif r['in_blue_shield'] and cashed and not r['has_copy']:
            r['flags'].append('no copy of the check')
        amounts = {k: _d(r[k]) for k in ('copy_amount', 'bs_amount', 'ecw_amount', 'era_amount', 'tracker_amount') if _d(r[k]) is not None}
        if len(set(amounts.values())) > 1:
            r['flags'].append('amounts differ: ' + ', '.join(f"{k.split('_')[0]} {v:.2f}" for k, v in amounts.items()))

    rows = sorted(by.values(), key=lambda r: (r['bs_date'] or r['copy_date'] or r['era_dated'] or r['tracker_received'] or '', r['check_number']), reverse=True)
    return rows, summarize(rows)


def summarize(rows):
    """The counts behind the dashboard tiles, from any reconciled rows —
    this run's, or the whole helixona-checks table so the tiles always agree
    with the table below them after a run that looked at one check."""
    def flags(r):
        return [str(f) for f in (r.get('flags') or [])]
    return {
        'checks': len(rows),
        'posted': sum(r.get('verdict') == 'posted' for r in rows),
        'unposted': sum(r.get('verdict') == 'unposted' for r in rows),
        'not_in_ecw': sum(r.get('verdict') == 'not in eCW' for r in rows),
        'not_cashed': sum(r.get('verdict') == 'not cashed' for r in rows),
        'ecw_unchecked': sum(r.get('verdict') == 'eCW not checked' for r in rows),
        'era_only': sum(r.get('verdict') == '835 only' for r in rows),
        'era_unposted': sum('835 in eCW, not posted' in flags(r) for r in rows),
        'in_tracker': sum(bool(r.get('in_tracker')) for r in rows),
        'tracker_no_scan': sum('in tracker, no scan found' in flags(r) for r in rows),
        'other_payer': sum('other payer' in flags(r) for r in rows),
        'no_copy': sum('no copy of the check' in flags(r) for r in rows),
        'amount_mismatch': sum(any(f.startswith('amounts differ') for f in flags(r)) for r in rows),
    }
