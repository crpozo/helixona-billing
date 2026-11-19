"""B10 — the bank against the remittances against eCW.

RCM Automation Punch List v1.0 (2026-11-05), B10: "Match EFT/trace to ERA,
check scan/log to deposits, deposits to remittances, and remittances to
claim/service-line postings; allow one-to-many batches and reversals."

Remittance already knows, per check or EFT (helixona-checks): the scan, the
deposit slip it was scanned under, the tracker's deposit date, the payer's
status, the 835 and the eCW payment with what is posted. This module adds
the one source it lacked — the bank's own transactions, from the statement
export a person uploads — and answers, for every credit the bank shows:
which checks or EFT it is, and whether eCW has them posted.

    matched · posted        every item behind the credit is posted in eCW
    matched · not posted    identified, but eCW has not posted all of it —
                            the variance is the dollars not posted
    unmatched               money in the bank nothing accounts for
    ambiguous               more than one equally good explanation: no guess
    reversed                the bank took it back (returned item)
    other income            a card settlement or other credit — not a check
                            or an EFT; out of this reconciliation's scope

and, the other way: a deposit the team recorded that the bank never showed
(`not in bank`).

The rules, strongest first; a bank line is used once and so is a check:

    1. deposit slip   a scanned deposit's total = a bank credit, days apart
    2. tracker date   the checks the tracker says were deposited on one day
                      add up to a bank credit (or split between two)
    3. number         the check or trace number is printed on the bank line
    4. one check      one check's amount = one credit, the dates agree, and
                      neither has another candidate
    5. several checks a unique set of unexplained checks adds up to the credit

Pure: no browser, no AWS, no model. Read-only everywhere — nothing here
posts, corrects or approves; a person does (Karina, per the punch list).
"""
import csv
import datetime
import hashlib
import io
import itertools
import re
from decimal import Decimal, InvalidOperation

from src.checks.reconcile import norm_check

SLIP_DAYS = 5        # a deposit slip's date and the bank's posting date
TRACKER_DAYS = 4     # the tracker's deposit date and the bank's
SINGLE_DAYS = 6      # one check on its own
SUBSET_BEFORE, SUBSET_AFTER = 12, 2   # a check is dated before the credit it is in
SUBSET_MAX = 18      # candidates a set is searched among (2^18 sums at most)
REVERSAL_DAYS = 45
NOT_IN_BANK_AFTER = 4   # days a recorded deposit may take to show

COLS = {
    'date': re.compile(r'^(posting|posted|post|transaction|trans\.?|effective|value)?\s*date$|^date\s*posted$', re.I),
    'desc': re.compile(r'descr|memo|detail|narrative|payee|^name$|transaction$', re.I),
    'amount': re.compile(r'^amount$|^amt$|transaction\s*amount|^net\s*amount$', re.I),
    'credit': re.compile(r'credit|deposit|money\s*in|^in$', re.I),
    'debit': re.compile(r'debit|withdraw|money\s*out|^out$', re.I),
    'ref': re.compile(r'check\s*(no|num|#)|serial|reference|ref\.?\s*(no|num|#)?$|bank\s*ref|trace', re.I),
    'type': re.compile(r'^type$|tran(saction)?\s*type|^code$', re.I),
}
EFT_RX = re.compile(r'\bACH\b|\bEFT\b|HCCLAIMPMT|ELECTRONIC|DIRECT\s*DEP|\bTRN\b|\bPPD\b|\bCCD\b|\bCTX\b|PREAUTH', re.I)
DEPOSIT_RX = re.compile(r'DEPOSIT|REMOTE|MOBILE|\bRDC\b|\bATM\b|BRANCH|COUNTER', re.I)
CARD_RX = re.compile(r'MERCH|BANKCARD|BKCRD|CARD\s*SETTL|SETTLEMENT|SQUARE|STRIPE|CLOVER|AMEX|AMERICAN EXPRESS|DISCOVER|PAYPAL|'
                     r'REPEATMD|SHOPIFY|TOAST|HEARTLAND|ELAVON|WORLDPAY|FISERV|GLOBAL PAY|INTEREST', re.I)
REVERSAL_RX = re.compile(r'RETURN|REVERS|CHARGE\s*BACK|CHARGEBACK|\bNSF\b|DISHONOR|ADJUSTMENT\s*DEBIT', re.I)


def _dec(v):
    t = str(v if v is not None else '').strip()
    if not t:
        return None
    neg = (t.startswith('(') and t.endswith(')')) or t.endswith('-') or t.upper().endswith('DR') or t.lstrip('$ ').startswith('-')
    t = re.sub(r'(?i)(cr|dr)$', '', t).replace('$', '').replace(',', '').strip('()+- ')
    try:
        d = Decimal(t)
    except (InvalidOperation, ValueError):
        return None
    return -abs(d) if neg else d


def _date(v):
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    t = str(v or '').strip()
    m = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', t)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})', t)
        if not m:
            return None
        mo, d, y = (int(x) for x in m.groups())
        y += 2000 if y < 100 else 0
    try:
        return datetime.date(y, mo, d)
    except ValueError:
        return None


def _cell(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return '' if v is None else str(v).strip()


def header_map(row):
    """{field: column} for a header row; {} when it is not one. A statement
    has a date and either one signed amount or a credit/debit pair."""
    cells = [_cell(c) for c in row]
    cols = {}
    for key, rx in COLS.items():
        for i, h in enumerate(cells):
            if h and i not in cols.values() and rx.search(h):
                cols[key] = i
                break
    if 'date' in cols and ('amount' in cols or 'credit' in cols):
        return cols
    return {}


def kind_of(desc, amount, type_text=''):
    """What a bank line is, from its sign and its words."""
    text = f'{desc} {type_text}'
    if amount < 0:
        return 'reversal' if REVERSAL_RX.search(text) else 'debit'
    if CARD_RX.search(text):
        return 'card'
    if EFT_RX.search(text):
        return 'eft'
    if DEPOSIT_RX.search(text):
        return 'deposit'
    return 'credit'


def rows_to_txns(rows, source=''):
    """Bank transactions from a statement's rows (lists of cells), the
    header anywhere in the first 25. Credits positive, debits negative.
    Two identical lines on one day are two transactions (`n` tells them apart)."""
    for h_i, row in enumerate(rows[:25]):
        cols = header_map(row)
        if cols:
            break
    else:
        return []
    out, seen = [], {}
    for row in rows[h_i + 1:]:
        get = lambda k: (row[cols[k]] if k in cols and cols[k] < len(row) else None)
        when = _date(get('date'))
        if not when:
            continue
        if 'amount' in cols and _dec(get('amount')) is not None:
            amount = _dec(get('amount'))
            # A separate type column may carry the sign ("DEBIT" 125.00).
            if amount > 0 and re.search(r'debit|withdraw|\bdr\b', _cell(get('type')), re.I) and not re.search(r'credit', _cell(get('type')), re.I):
                amount = -amount
        else:
            cr, db = _dec(get('credit')), _dec(get('debit'))
            if cr is None and db is None:
                continue
            amount = abs(cr) if cr else -abs(db or Decimal(0))
        if amount == 0:
            continue
        desc = re.sub(r'\s+', ' ', _cell(get('desc')))
        ref = re.sub(r'\D', '', _cell(get('ref')))
        base = f"{when.isoformat()}|{amount:.2f}|{desc.upper()}|{ref}"
        n = seen[base] = seen.get(base, 0) + 1
        out.append({
            'id': hashlib.sha1(f'{base}|{n}'.encode()).hexdigest()[:16],
            'date': when.isoformat(), 'amount': f'{amount:.2f}', 'desc': desc[:200], 'ref': ref,
            'kind': kind_of(desc, amount, _cell(get('type'))), 'n': n, 'source': source,
        })
    return out


def parse_statement(data, filename=''):
    """Transactions from an uploaded statement: CSV or Excel bytes."""
    name = str(filename or '').lower()
    if name.endswith(('.xlsx', '.xlsm')) or data[:2] == b'PK':
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            out = []
            for ws in wb.worksheets:
                out.extend(rows_to_txns([list(r) for r in ws.iter_rows(values_only=True)], filename))
            return out
        finally:
            wb.close()
    text = data.decode('utf-8-sig', errors='replace')
    try:
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=',;\t|')
    except csv.Error:
        dialect = csv.excel
    return rows_to_txns([list(r) for r in csv.reader(io.StringIO(text), dialect)], filename)


# ------------------------------------------------------------ what we hold
def _item_amount(r):
    for k in ('bs_amount', 'copy_amount', 'tracker_amount', 'ecw_amount', 'era_amount'):
        d = _dec(r.get(k))
        if d is not None and d > 0:
            return d
    return None


def items_from_checks(rows):
    """One item per check or EFT Helixona should find in its bank: the rows
    of helixona-checks, less the ones the payer made out to somebody else."""
    out = []
    for r in rows:
        key = str(r.get('check_number') or '')
        if not key or key.startswith(('_', 'deposit:', 'unreadable:')):
            continue
        amount = _item_amount(r)
        if amount is None:
            continue
        bucket = str(r.get('bs_bucket') or '')
        if bucket in ('cashed by patient', 'sent to patient, not cashed', 'voided'):
            continue
        in_hand = bool(r.get('has_copy') or r.get('in_tracker') or r.get('in_ecw') or r.get('in_era'))
        unposted = _dec(r.get('ecw_unposted'))
        out.append({
            'number': norm_check(key), 'amount': amount,
            'payer': str(r.get('tracker_payer') or r.get('copy_payer') or r.get('era_payer') or ('Blue Shield of California' if r.get('in_blue_shield') else '')),
            'slip_file': str(r.get('deposit_file') or '') if r.get('deposit_slip', True) is not False else '',
            'tracker_deposit': _date(r.get('tracker_deposit')),
            'cashed': _date(r.get('cashed_date')),
            'dated': _date(r.get('era_dated')) or _date(r.get('bs_date')) or _date(r.get('copy_date')),
            'received': _date(r.get('tracker_received')),
            'has_scan': bool(r.get('has_copy')), 'in_hand': in_hand,
            'verdict': str(r.get('verdict') or ''),
            'posted': str(r.get('verdict') or '') == 'posted',
            'unposted': unposted if unposted is not None else (Decimal(0) if str(r.get('verdict') or '') == 'posted' else amount),
            'file': str(r.get('copy_file') or ''),
        })
    return out


def slips_from_checks(rows):
    """The scanned bank deposits: {'file', 'date', 'total', 'checks'}."""
    out = []
    for r in rows:
        key = str(r.get('check_number') or '')
        if not key.startswith('deposit:') or r.get('deposit_slip', True) is False:
            continue
        total = _dec(r.get('deposit_total'))
        if total is None or total <= 0:
            continue
        out.append({'file': str(r.get('copy_file') or key[8:]), 'date': _date(r.get('deposit_date')), 'total': total,
                    'checks': [norm_check(c) for c in (r.get('deposit_checks') or [])]})
    return out


# ------------------------------------------------------------- the matching
def _days(a, b):
    return abs((a - b).days) if a and b else None


def _unique_subset(cands, target, cap=SUBSET_MAX):
    """The one set of candidates whose amounts add up to `target`: the set,
    'many' when more than one does, None when none does (or too many to try)."""
    cands = [c for c in cands if c['amount'] <= target]
    if not cands or len(cands) > cap or sum(c['amount'] for c in cands) < target:
        return None
    found = None
    for size in range(1, len(cands) + 1):
        for combo in itertools.combinations(cands, size):
            if sum(c['amount'] for c in combo) == target:
                if found is not None:
                    return 'many'
                found = list(combo)
    return found


def reconcile_bank(txns, check_rows, today=None):
    """(rows, missing, summary). `rows`: one per bank transaction with its
    status, the items behind it, the rule that matched and the variance.
    `missing`: deposits the team recorded that the bank does not show."""
    today = today or datetime.date.today()
    items = items_from_checks(check_rows)
    slips = slips_from_checks(check_rows)
    by_number = {}
    for it in items:
        by_number.setdefault(it['number'], it)

    bank = []
    for t in txns:
        amount = _dec(t.get('amount'))
        when = _date(t.get('date'))
        if amount is None or when is None:
            continue
        bank.append({**t, '_amount': amount, '_date': when, 'items': [], 'rule': '', 'status': '', 'reason': '', 'note': ''})
    bank.sort(key=lambda b: (b['_date'], b['id']))
    credits = [b for b in bank if b['_amount'] > 0 and b['kind'] != 'card']
    used = set()        # item numbers already behind a bank line

    def free(b):
        return not b['items'] and not b['status']

    def attach(b, its, rule, note=''):
        b['items'] = [it['number'] for it in its]
        b['_its'] = its
        b['rule'] = rule
        b['note'] = note
        used.update(it['number'] for it in its)

    def nearest(cands, when):
        """The candidate closest in date; None when two are equally close."""
        cands = sorted(cands, key=lambda b: (_days(b['_date'], when) if when else 0, b['_date']))
        if not cands:
            return None
        if len(cands) > 1 and when and _days(cands[0]['_date'], when) == _days(cands[1]['_date'], when):
            # Same amount, same distance: identical deposits — take the earlier, they are interchangeable.
            return cands[0]
        return cands[0]

    # 1. Deposit slips.
    slip_missing = []
    for s in sorted(slips, key=lambda s: (s['date'] or datetime.date.min, s['file'])):
        members = [by_number[n] for n in s['checks'] if n in by_number and n not in used]
        cands = [b for b in credits if free(b) and b['_amount'] == s['total']
                 and (s['date'] is None or _days(b['_date'], s['date']) <= SLIP_DAYS)]
        b = nearest(cands, s['date'])
        if b is not None:
            attach(b, members, 'deposit slip', f"slip {s['file'].rsplit('/', 1)[-1]}"
                   + (f", {_days(b['_date'], s['date'])} day(s) apart" if s['date'] else ''))
            b['slip_file'] = s['file']
        else:
            slip_missing.append((s, members))

    # 2. The tracker's deposit dates.
    groups = {}
    for it in items:
        if it['number'] not in used and it['tracker_deposit']:
            groups.setdefault(it['tracker_deposit'], []).append(it)
    tracker_missing = []
    for day in sorted(groups):
        group = [it for it in groups[day] if it['number'] not in used]
        if not group:
            continue
        total = sum(it['amount'] for it in group)
        near = [b for b in credits if free(b) and _days(b['_date'], day) <= TRACKER_DAYS]
        b = nearest([b for b in near if b['_amount'] == total], day)
        if b is not None:
            attach(b, group, 'tracker deposit date', f"{len(group)} check(s) the tracker dates {day.strftime('%m/%d/%Y')}")
            continue
        # The day's checks went in as more than one deposit.
        left = list(group)
        for b in sorted(near, key=lambda b: (_days(b['_date'], day), b['_date'])):
            got = _unique_subset([it for it in left], b['_amount'])
            if isinstance(got, list):
                attach(b, got, 'tracker deposit date', f"{len(got)} of the {len(group)} check(s) the tracker dates {day.strftime('%m/%d/%Y')}")
                left = [it for it in left if it['number'] not in used]
        if left:
            tracker_missing.append((day, left))

    # 3. The number printed on the bank line (a check's serial, an EFT's trace).
    for b in credits:
        if not free(b):
            continue
        digits = set(re.findall(r'\d{5,}', f"{b.get('desc', '')} {b.get('ref', '')}"))
        hits = [it for n, it in by_number.items() if n not in used and len(n) >= 5
                and any(d.lstrip('0') == n or (len(n) >= 7 and n in d) for d in digits)]
        hits = [it for it in hits if it['amount'] == b['_amount']]
        if len(hits) == 1:
            attach(b, hits, 'number on the bank line', f"{hits[0]['number']} is printed on the line")

    # 4. One check, one credit, no other candidate on either side.
    def item_date(it):
        return it['tracker_deposit'] or it['cashed'] or it['received'] or it['dated']

    def close(b, it):
        d = item_date(it)
        return d is not None and _days(b['_date'], d) <= SINGLE_DAYS

    for b in credits:
        if not free(b):
            continue
        cands = [it for it in items if it['number'] not in used and it['amount'] == b['_amount'] and close(b, it)]
        if len(cands) == 1:
            it = cands[0]
            rivals = [x for x in credits if not x['items'] and x is not b and x['_amount'] == it['amount'] and close(x, it)]
            if not rivals:
                attach(b, [it], 'amount and date', f"one {'EFT' if b['kind'] == 'eft' else 'check'} for this amount within {SINGLE_DAYS} days")
            else:
                b['status'], b['reason'] = 'ambiguous', (f"{len(rivals) + 1} bank lines of ${b['_amount']:.2f} could each be "
                                                         f"{it['number']} — look at the deposit")
        elif len(cands) > 1:
            same_pool = [x for x in credits if free(x) and x['_amount'] == b['_amount'] and any(close(x, it) for it in cands)]
            if len(same_pool) == len(cands):
                # As many identical lines as identical checks: pair them in date order.
                for x, it in zip(sorted(same_pool, key=lambda x: x['_date']), sorted(cands, key=lambda it: (item_date(it), it['number']))):
                    if free(x):
                        attach(x, [it], 'amount and date', 'identical amounts paired in date order')
            else:
                b['status'] = 'ambiguous'
                b['reason'] = f"{len(cands)} items of ${b['_amount']:.2f} fit: {', '.join(it['number'] for it in cands[:5])}"

    # 5. Several checks in one deposit, nobody wrote down which.
    for b in credits:
        if not free(b) or b['kind'] == 'eft':
            continue
        cands = [it for it in items if it['number'] not in used and it['in_hand'] and item_date(it) is not None
                 and -SUBSET_AFTER <= (b['_date'] - item_date(it)).days <= SUBSET_BEFORE]
        got = _unique_subset(cands, b['_amount'])
        if isinstance(got, list) and len(got) > 1:
            attach(b, got, 'sum of checks', f"the only set of {len(got)} unexplained checks adding up to it")
        elif got == 'many':
            b['status'], b['reason'] = 'ambiguous', 'more than one set of checks adds up to this deposit'

    # Reversals: the bank took a credit back.
    for b in bank:
        if b['kind'] != 'reversal':
            continue
        back = [c for c in bank if c['_amount'] == -b['_amount'] and c['status'] != 'reversed'
                and 0 <= (b['_date'] - c['_date']).days <= REVERSAL_DAYS]
        named = [c for c in back if c.get('ref') and c['ref'] == b.get('ref')] or back
        if named:
            c = max(named, key=lambda c: c['_date'])
            c['status'] = 'reversed'
            c['reason'] = f"the bank took it back on {b['_date'].strftime('%m/%d/%Y')}"
            c['reversed_by'] = b['id']
            b['status'], b['reason'], b['reverses'] = 'reversal', f"takes back the credit of {c['_date'].strftime('%m/%d/%Y')}", c['id']
            for n in c.get('items') or []:
                used.discard(n)

    # The statuses.
    rows = []
    for b in bank:
        its = b.pop('_its', [])
        variance = Decimal(0)
        if b['status'] in ('reversed', 'reversal', 'ambiguous'):
            pass
        elif b['_amount'] < 0:
            b['status'] = 'debit'
        elif b['kind'] == 'card':
            b['status'], b['reason'] = 'other income', 'a card settlement or other credit — not a check or an EFT'
        elif b['rule']:
            variance = sum((it['unposted'] for it in its if not it['posted']), Decimal(0))
            got = sum((it['amount'] for it in its), Decimal(0))
            if its and got != b['_amount']:
                # A slip whose checks were not all read: identified, the difference is unexplained.
                variance += abs(b['_amount'] - got)
                b['reason'] = f"the items found add up to ${got:.2f} of ${b['_amount']:.2f}"
            if not its:
                b['status'], b['reason'] = 'matched · not posted', "the deposit slip matches; none of its checks is on file"
                variance = b['_amount']
            elif variance > 0:
                b['status'] = 'matched · not posted'
                not_posted = [it['number'] for it in its if not it['posted']]
                if not_posted:
                    b['reason'] = (b['reason'] + ' · ' if b['reason'] else '') + f"not posted in eCW: {', '.join(not_posted[:8])}" \
                                  + (' …' if len(not_posted) > 8 else '')
            else:
                b['status'] = 'matched · posted'
        else:
            b['status'] = 'unmatched'
            b['reason'] = ('an electronic payment no 835 or eCW payment accounts for' if b['kind'] == 'eft'
                           else 'a deposit no scan, tracker line or eCW payment accounts for')
            variance = b['_amount']
        rows.append({k: v for k, v in b.items() if not k.startswith('_')} | {
            'variance': f'{variance:.2f}', 'item_count': len(b.get('items') or []),
            'payers': sorted({it['payer'] for it in its if it['payer']})[:3],
            'files': sorted({it['file'] for it in its if it['file']})[:5]})

    # What the team recorded and the bank never showed.
    first = min((b['_date'] for b in bank), default=None) if bank else None
    last = max((b['_date'] for b in bank), default=None) if bank else None
    missing = []

    def covered(day):
        return bool(first and last and day and first <= day <= last - datetime.timedelta(days=NOT_IN_BANK_AFTER))

    for s, members in slip_missing:
        if covered(s['date']):
            missing.append({'what': 'deposit slip', 'date': s['date'].isoformat(), 'amount': f"{s['total']:.2f}",
                            'items': s['checks'], 'file': s['file'],
                            'reason': 'a scanned deposit slip with no bank credit of that amount near its date'})
    for day, left in tracker_missing:
        left = [it for it in left if it['number'] not in used]
        if left and covered(day):
            missing.append({'what': 'tracker deposit', 'date': day.isoformat(), 'amount': f"{sum(it['amount'] for it in left):.2f}",
                            'items': [it['number'] for it in left], 'file': '',
                            'reason': 'the tracker says deposited that day; the bank shows no matching credit'})
    missing.sort(key=lambda m: m['date'], reverse=True)
    rows.sort(key=lambda r: (r['date'], r['id']), reverse=True)
    return rows, missing, summarize(rows, missing, first, last)


def summarize(rows, missing=(), first=None, last=None):
    def money(sel, key='amount'):
        return f"{sum((_dec(r.get(key)) or Decimal(0) for r in rows if sel(r)), Decimal(0)):.2f}"
    out = {'transactions': len(rows), 'from': first.isoformat() if first else '', 'to': last.isoformat() if last else ''}
    for st, key in (('matched · posted', 'posted'), ('matched · not posted', 'not_posted'), ('unmatched', 'unmatched'),
                    ('ambiguous', 'ambiguous'), ('reversed', 'reversed'), ('other income', 'other')):
        out[key] = sum(1 for r in rows if r['status'] == st)
        out[key + '_amount'] = money(lambda r, st=st: r['status'] == st)
    credits = lambda r: r['status'] not in ('debit', 'reversal')
    out['credits'] = sum(1 for r in rows if credits(r))
    out['credits_amount'] = money(credits)
    out['variance'] = money(lambda r: r['status'] in ('matched · not posted', 'unmatched'), 'variance')
    out['not_in_bank'] = len(missing)
    out['not_in_bank_amount'] = f"{sum((_dec(m['amount']) or Decimal(0) for m in missing), Decimal(0)):.2f}"
    # Every credit lands in exactly one bucket: the total is the proof.
    out['accounted'] = out['posted'] + out['not_posted'] + out['unmatched'] + out['ambiguous'] + out['reversed'] + out['other'] == out['credits']
    return out
