"""Three lists of checks, one answer per check number.

* copies   — check images from SharePoint (or the S3 inbox), as read by
             src/checks/read_check.py: {'check_number', 'amount', 'file', ...}
* checks  — what Blue Shield says (the helixona-eobs table Remittance
             captured): {'check_eft', 'check_amount', 'check_status',
             'check_date', 'cashed_date', ...}
* payments — what eCW has on file (Billing → Payments since a date):
             {'check_no', 'amount', 'posted', 'unposted', 'payment_id', ...}

The verdict, per check:

    posted        in eCW, nothing left unposted
    unposted      in eCW, but a balance is still unposted — the payment was
                  created and the lines never finished (see docs/ecw_posting.md)
    not in eCW    Blue Shield shows it cashed, eCW has no payment for it
    not cashed    Blue Shield has not cashed it yet — nothing to enter
    copy only     we hold an image of a check Blue Shield's results do not
                  list (a different payer, or outside the search window)

and, alongside, whether we hold a copy of the check and whether the
amounts agree between the three sources. Pure: no browser, no AWS.
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


def reconcile(copies, checks, payments, ecw_checked=True):
    """Rows keyed by check number, plus a summary.

    `ecw_checked` False means eCW was not consulted this run: rows then say
    'eCW not checked' instead of 'not in eCW'.
    """
    by = {}

    def row(key):
        return by.setdefault(key, {
            'check_number': key, 'has_copy': False, 'copy_amount': '', 'copy_file': '', 'copy_url': '',
            'copy_date': '', 'in_blue_shield': False, 'bs_amount': '', 'bs_status': '', 'bs_date': '',
            'cashed_date': '', 'in_ecw': False, 'ecw_amount': '', 'ecw_posted': '', 'ecw_unposted': '',
            'ecw_payment_id': '', 'flags': [], 'verdict': '',
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

    for r in by.values():
        cashed = _cashed(r['bs_status']) or bool(r['cashed_date'])
        if r['in_ecw']:
            unposted = _d(r['ecw_unposted'])
            r['verdict'] = 'unposted' if (unposted is not None and unposted > 0) else 'posted'
        elif not r['in_blue_shield']:
            r['verdict'] = 'copy only'
        elif not cashed:
            r['verdict'] = 'not cashed'
        else:
            r['verdict'] = 'not in eCW' if ecw_checked else 'eCW not checked'

        if r['in_blue_shield'] and cashed and not r['has_copy']:
            r['flags'].append('no copy of the check')
        amounts = {k: _d(r[k]) for k in ('copy_amount', 'bs_amount', 'ecw_amount') if _d(r[k]) is not None}
        if len(set(amounts.values())) > 1:
            r['flags'].append('amounts differ: ' + ', '.join(f"{k.split('_')[0]} {v:.2f}" for k, v in amounts.items()))

    rows = sorted(by.values(), key=lambda r: (r['bs_date'] or r['copy_date'] or '', r['check_number']), reverse=True)
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
        'copy_only': sum(r.get('verdict') == 'copy only' for r in rows),
        'no_copy': sum('no copy of the check' in flags(r) for r in rows),
        'amount_mismatch': sum(any(f.startswith('amounts differ') for f in flags(r)) for r in rows),
    }
