"""Which eCW line an EOB line is, when the same code appears more than once.

An IV claim bills J3490 (unclassified drug) several times on one claim — one
line per drug. The EOB comes back with the same J3490 lines, and the only
thing that tells them apart is the billed amount. Usually the amounts agree
and the pairing is exact. When they do not — Blue Shield restates the line at
the Medicare amount while eCW carries the Blue Shield amount — this table
translates one into the other: a J3490 billed at $300.00 on the EOB is the
Ascorbic Acid 25,000 mg line that eCW billed at $900.00.

The table is the operator's (2026-09-14). A row is (code, drug, Medicare
billed amount, Blue Shield amount). Add rows here as new drugs appear.
"""
from decimal import Decimal, InvalidOperation

# (code, drug, medicare_billed, blue_shield_amount)
DRUG_TABLE = [
    ('J3490', 'Ascorbic Acid - 10000 mG',  '120.00',  '360.00'),
    ('J3490', 'Ascorbic Acid - 100000 mG', '1200.00', '3600.00'),
    ('J3490', 'Ascorbic Acid - 15000 mG',  '180.00',  '540.00'),
    ('J3490', 'Ascorbic Acid - 20000 mG',  '240.00',  '720.00'),
    ('J3490', 'Ascorbic Acid - 25000 mG',  '300.00',  '900.00'),
    ('J3490', 'Ascorbic Acid - 3000 mG',   '36.00',   '108.00'),
    ('J3490', 'Ascorbic Acid - 5000 mG',   '60.00',   '180.00'),
    ('J3490', 'Ascorbic Acid - 50000 mG',  '600.00',  '1800.00'),
    ('J3490', 'Ascorbic Acid - 75000 mG',  '900.00',  '2700.00'),
    ('J3490', 'B Complex 100',             '40.00',   '40.00'),
    ('J7999', 'Dexpanthenol',              '40.00',   '40.00'),
    ('J7999', 'Methylcobalamin',           '40.00',   '125.00'),
    ('J3490', 'Methylcobalamin',           '40.00',   '125.00'),
    ('J7999', 'Pyridoxine',                '20.00',   '40.00'),
]


def _amt(s):
    """'$1,200.00' / '1200' / Decimal -> Decimal('1200.00'); unparseable -> None."""
    t = str(s if s is not None else '').replace('$', '').replace(',', '').strip()
    if not t:
        return None
    try:
        return Decimal(t).quantize(Decimal('0.01'))
    except InvalidOperation:
        return None


def equivalent_amounts(code, billed):
    """Every billed amount the table says is the same drug as `billed` under
    `code`, including `billed` itself. Empty when the table knows nothing."""
    b = _amt(billed)
    if b is None:
        return set()
    code = str(code or '').upper().strip()
    out = set()
    for c, _drug, medicare, bsc in DRUG_TABLE:
        if c != code:
            continue
        m, s = _amt(medicare), _amt(bsc)
        if b in (m, s):
            out.update({m, s})
    return out


def drug_for(code, billed):
    """The drug names the table lists for this code at this billed amount
    (either column). A list — $40.00 under J3490 is B Complex or
    Methylcobalamin, and the table alone cannot say which."""
    b = _amt(billed)
    code = str(code or '').upper().strip()
    return [drug for c, drug, medicare, bsc in DRUG_TABLE
            if c == code and b is not None and b in (_amt(medicare), _amt(bsc))]


def match_lines(ecw_rows, eob_lines):
    """Pair each EOB line with the eCW line it pays.

    `ecw_rows`: dicts with 'cpt' and 'billed' (and anything else — 'idx', the
    grid row — carried through untouched). `eob_lines`: dicts with 'cpt' and
    'billed' plus the amounts to post.

    Within one code, in order:
      1. a lone line on each side pairs (a billed mismatch is noted, not fatal);
      2. exact billed amounts pair, when the amount is unique on both sides;
      3. the drug table translates the EOB amount to its eCW equivalent, when
         that names exactly one remaining eCW row;
      4. a single leftover on each side pairs by elimination.
    Anything else is left unresolved with a reason — a payment posted against
    the wrong drug line is worse than one left for a person.

    Returns (pairs, unresolved): pairs are {'ecw': row, 'eob': line, 'how':
    ...}; unresolved are {'eob': line, 'reason': ...} and {'ecw': row,
    'reason': ...}.
    """
    def code(x):
        return str(x.get('cpt') or '').upper().strip()

    pairs, unresolved = [], []
    codes = sorted({code(r) for r in ecw_rows} | {code(l) for l in eob_lines})
    for c in codes:
        ecw = [r for r in ecw_rows if code(r) == c]
        eob = [l for l in eob_lines if code(l) == c]
        if not ecw:
            unresolved += [{'eob': l, 'reason': f'{c}: not on the eCW claim'} for l in eob]
            continue
        if not eob:
            unresolved += [{'ecw': r, 'reason': f'{c}: not on the EOB'} for r in ecw]
            continue

        if len(ecw) == 1 and len(eob) == 1:
            same = _amt(ecw[0].get('billed')) == _amt(eob[0].get('billed'))
            pairs.append({'ecw': ecw[0], 'eob': eob[0],
                          'how': 'only line' if same else 'only line (billed differs)'})
            continue

        # 2. exact billed amount, unique on both sides
        for l in list(eob):
            b = _amt(l.get('billed'))
            same_ecw = [r for r in ecw if _amt(r.get('billed')) == b]
            same_eob = [x for x in eob if _amt(x.get('billed')) == b]
            if b is not None and len(same_ecw) == 1 and len(same_eob) == 1:
                pairs.append({'ecw': same_ecw[0], 'eob': l, 'how': 'billed amount'})
                ecw.remove(same_ecw[0])
                eob.remove(l)

        # 3. the drug table
        for l in list(eob):
            eq = equivalent_amounts(c, l.get('billed'))
            if not eq:
                continue
            cands = [r for r in ecw if _amt(r.get('billed')) in eq]
            if len(cands) == 1:
                drug = drug_for(c, l.get('billed'))
                pairs.append({'ecw': cands[0], 'eob': l,
                              'how': 'drug table: ' + (drug[0] if len(drug) == 1 else '/'.join(drug))})
                ecw.remove(cands[0])
                eob.remove(l)

        # 4. one left on each side
        if len(ecw) == 1 and len(eob) == 1:
            pairs.append({'ecw': ecw[0], 'eob': eob[0], 'how': 'by elimination'})
            continue
        unresolved += [{'eob': l, 'reason': f'{c} billed {l.get("billed")}: no eCW line pairs with it'}
                       for l in eob]
        unresolved += [{'ecw': r, 'reason': f'{c} billed {r.get("billed")}: no EOB line pays it'}
                       for r in ecw]
    return pairs, unresolved
