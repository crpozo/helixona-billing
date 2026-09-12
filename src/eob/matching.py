"""Which eCW service line each EOB line is posted to.

The operator's rule, made exact:

* A code that appears once on each side is matched by code alone — even when
  the billed amounts differ, which they do (an adjusted claim bills 96375 at
  $250.00 on the EOB against $575.00 in eCW).
* A code that repeats is told apart by billed amount (also per unit, since
  one side can bill 2 units at $40.00 where the other bills 1 at $20.00);
  failing that, by Vignesh's table of Medicare vs Blue Shield amounts. The
  table is read BOTH ways: claim 2627's EOB bills an ascorbic acid at $300.00,
  the Medicare amount, while eCW bills the same line at $900.00, the Blue
  Shield amount. The HCFA's drug text must agree with the table's drug, so a
  $40.00 B Complex line is never taken for a Methylcobalamin.
* Among what is left, units break ties; lines identical in code, billed, units
  and drug are interchangeable. Units only ever identify a line: the money is
  posted exactly as the EOB prints it, never divided — the operator puts an
  EOB line of 2 units onto a 1-unit eCW row whole.
* Anything still undecided is NOT guessed. An EOB line posted to the wrong
  service puts money on the wrong line of the ledger, so the claim is marked
  needs_review with the reason, and nothing is posted for it.
"""
import re
from decimal import Decimal, InvalidOperation
from itertools import permutations

from src.eob.drug_table import counterpart_amounts

POSTED_FIELDS = ('allowed', 'deductible', 'copay', 'paid')

KNOWN_DRUGS = ('ascorbic acid', 'b complex', 'methylcobalamin', 'dexpanthenol',
               'pyridoxine', 'glutathione', 'levocarnitine', 'leucovorin',
               'magnesium sulfate', 'calcium gluconate')


def _d(v):
    try:
        return Decimal(str(v).replace('$', '').replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return None


def drug_key(text):
    """A comparable name for a drug, from HCFA text or a table row.

    Ascorbic acid is dosed, and the table distinguishes doses, so its key
    carries milligrams: the HCFA says "50 ML ASCORBIC ACID (500MG/ML)" =
    25000 mg; the table says "Ascorbic Acid - 25000 mG".
    """
    t = re.sub(r'\s+', ' ', str(text or '').lower())
    if not t:
        return None
    name = next((n for n in KNOWN_DRUGS if n in t), None)
    if name is None:
        return None
    if name == 'ascorbic acid':
        m = re.search(r'(\d[\d,]*)\s*mg\b(?!/)', t)
        if m:
            return f'ascorbic acid {int(m.group(1).replace(",", ""))}'
        m = re.search(r'(\d+(?:\.\d+)?)\s*ml\b.*?(\d+(?:\.\d+)?)\s*mg/ml', t)
        if m:
            return f'ascorbic acid {int(round(float(m.group(1)) * float(m.group(2))))}'
    return name


def _table_candidates(code, eob_billed):
    """(eCW billed, drug key) pairs Vignesh's table allows for this EOB line."""
    return [(_d(amount), drug_key(drug))
            for amount, drug in counterpart_amounts(code, eob_billed)]


def _per_unit(billed, units):
    b, u = _d(billed), _d(units)
    if b is None or not u:
        return None
    return (b / u).quantize(Decimal('0.01'))


def _candidates(e, ecw_lines, idxs):
    eb = _d(e.get('billed'))
    table = _table_candidates(e.get('cpt'), eb) if eb is not None else []
    cands = {}
    for i in idxs:
        c = ecw_lines[i]
        cb = _d(c.get('billed'))
        if eb is not None and cb == eb:
            cands[i] = 'billed'
            continue
        epu, cpu = _per_unit(e.get('billed'), e.get('units')), _per_unit(c.get('billed'), c.get('units'))
        if epu is not None and epu == cpu and str(e.get('units') or '') != str(c.get('units') or ''):
            cands[i] = 'billed per unit'
            continue
        ck = drug_key(c.get('drug'))
        for tb, tk in table:
            if cb == tb and (ck is None or tk is None or ck == tk):
                cands[i] = 'drug table'
                break
    return cands


def _same(a, b):
    return (a.get('cpt') == b.get('cpt') and _d(a.get('billed')) == _d(b.get('billed'))
            and str(a.get('units') or '') == str(b.get('units') or '')
            and drug_key(a.get('drug')) == drug_key(b.get('drug')))


def _assign_code(code, e_idx, c_idx, eob_lines, ecw_lines):
    """Assignments for one code, or (None, reason)."""
    if len(e_idx) == 1 and len(c_idx) == 1:
        e, c = eob_lines[e_idx[0]], ecw_lines[c_idx[0]]
        note = '' if _d(e.get('billed')) == _d(c.get('billed')) else \
            f"billed differs (EOB {e.get('billed')} vs eCW {c.get('billed')})"
        return [(c_idx[0], e_idx[0], 'code' + (f'; {note}' if note else ''))], None
    if len(e_idx) > len(c_idx):
        return None, f'{code}: EOB has {len(e_idx)} lines, eCW only {len(c_idx)}'

    cands = {ei: _candidates(eob_lines[ei], ecw_lines, c_idx) for ei in e_idx}
    # Every injective choice of an eCW line for each EOB line among candidates.
    solutions = []
    for perm in permutations(c_idx, len(e_idx)):
        if all(ci in cands[ei] for ei, ci in zip(e_idx, perm)):
            solutions.append(perm)
    if not solutions:
        # Identical eCW lines leave nothing to decide; take them in order.
        if all(_same(ecw_lines[c_idx[0]], ecw_lines[ci]) for ci in c_idx):
            return [(ci, ei, 'identical eCW lines')
                    for ei, ci in zip(e_idx, c_idx)], None
        detail = ', '.join(f"EOB {eob_lines[ei].get('billed')}" for ei in e_idx)
        opts = ', '.join(f"eCW {ecw_lines[ci].get('billed')}"
                         f"{' ' + ecw_lines[ci]['drug'] if ecw_lines[ci].get('drug') else ''}"
                         for ci in c_idx)
        return None, f'{code}: no billed or drug-table match ({detail} vs {opts})'
    if len(solutions) > 1:
        def units_ok(perm):
            return all(str(eob_lines[ei].get('units') or '') == str(ecw_lines[ci].get('units') or '')
                       for ei, ci in zip(e_idx, perm))
        by_units = [p for p in solutions if units_ok(p)]
        if by_units:
            solutions = by_units
    if len(solutions) > 1:
        # Several assignments are harmless when they put the same money on
        # every eCW line — identical eCW lines, or identical EOB lines. They
        # are not when two indistinguishable EOB lines carry different
        # amounts: nothing on the EOB says which drug each one was.
        def posting(perm):
            return {ci: tuple(str(eob_lines[ei].get(f, '')) for f in POSTED_FIELDS)
                    for ei, ci in zip(e_idx, perm)}
        def canonical(perm):
            # Identical eCW lines are interchangeable: compare by what they are.
            key = {}
            for ci, vals in posting(perm).items():
                c = ecw_lines[ci]
                ident = (c.get('cpt'), str(_d(c.get('billed'))), str(c.get('units') or ''),
                         drug_key(c.get('drug')))
                key.setdefault(ident, []).append(vals)
            return {k: sorted(v) for k, v in key.items()}
        first = canonical(solutions[0])
        if any(canonical(p) != first for p in solutions[1:]):
            return None, (f'{code}: {len(solutions)} different ways to assign {len(e_idx)} EOB lines '
                          f'that pay different amounts')
    chosen = solutions[0]
    return [(ci, ei, cands[ei][ci]) for ei, ci in zip(e_idx, chosen)], None


def plan_claim(eob_lines, ecw_lines, eob_claim_paid=None):
    """The posting plan for one claim.

    Returns {'status': 'ready' | 'needs_review', 'reasons': [...],
             'rows': [{ecw_index, eob_index, cpt, how, ecw billed, eob billed,
                       allowed, deductible, copay, paid}],
             'unposted_ecw': [ecw indexes the EOB does not pay]}
    """
    reasons, rows = [], []
    e_by, c_by = {}, {}
    for i, l in enumerate(eob_lines):
        e_by.setdefault(l.get('cpt'), []).append(i)
    for i, l in enumerate(ecw_lines):
        c_by.setdefault(l.get('cpt'), []).append(i)

    taken = set()
    for code, e_idx in e_by.items():
        c_idx = c_by.get(code, [])
        if not c_idx:
            reasons.append(f'{code}: on the EOB but not on the eCW claim')
            continue
        got, why = _assign_code(code, e_idx, c_idx, eob_lines, ecw_lines)
        if why:
            reasons.append(why)
            continue
        for ci, ei, how in got:
            taken.add(ci)
            e, c = eob_lines[ei], ecw_lines[ci]
            row = {'ecw_index': ci, 'eob_index': ei, 'cpt': code, 'how': how,
                   'ecw_billed': c.get('billed', ''), 'eob_billed': e.get('billed', ''),
                   'ecw_drug': c.get('drug', ''), 'units': c.get('units', '')}
            for f in POSTED_FIELDS:
                row[f] = e.get(f, '')
            rows.append(row)

    if eob_claim_paid is not None and not reasons:
        total = sum((_d(r['paid']) or Decimal('0') for r in rows), Decimal('0'))
        if _d(eob_claim_paid) is not None and total != _d(eob_claim_paid):
            reasons.append(f'line paid amounts sum to {total:.2f}, EOB claim paid is {_d(eob_claim_paid):.2f}')

    rows.sort(key=lambda r: r['ecw_index'])
    return {
        'status': 'needs_review' if reasons else 'ready',
        'reasons': reasons,
        'rows': rows,
        'unposted_ecw': [i for i in range(len(ecw_lines)) if i not in taken],
    }
