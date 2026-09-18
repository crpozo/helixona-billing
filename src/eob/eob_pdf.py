"""The Blue Shield of California provider EOB, read by position.

The report is a print stream (Letter, 612x792pt): twelve columns under a
three-line header, amounts right-aligned in theirs, text left-aligned. Plain
text extraction loses the columns — a blank CONTRACTUAL cell simply vanishes
and every amount after it shifts one column left — so every word is placed by
its x coordinate against the header instead:

1. glyphs that are not content are dropped: barcodes (1.44pt), the vertical
   form id, near-white hidden text, the right-margin OMR strip;
2. words are cut with x_tolerance=1 (glyphs inside a word abut; words are
   1.68pt apart, and the default tolerance of 3 glues them together) and
   merged into cells when closer than 3.5pt — column gutters are wider;
3. the header gives each column's span. An amount belongs to the amount
   column whose right edge is within 8pt of its own; a text cell to the
   column its left edge falls in;
4. a small state machine walks the lines: RECEIPT DATE, service lines,
   TOTALS:, NOTES:, Adjusted Payment, the notes legend, STATEMENT TOTALS:,
   and the recap (APPROVE-TO-PAY / INTEREST PAYMENTS / OFFSETS TAKEN /
   CHECK AMOUNT).

What the report says that posting has to respect:

* The claim TOTALS row carries no allowed amount; interest exists only at the
  statement level, never on a claim or a line.
* "PAYMENT WAS ISSUED TO <member>": Blue Shield paid the member and the CHECK
  AMOUNT is 0.00 — there is no insurance payment to post.
* "Adjusted Payment": the claim was paid before, so eCW may already hold a
  posting for it.

The core takes plain word dicts, so it is tested without a PDF.
"""
import re
from decimal import Decimal, InvalidOperation

AMOUNT_RE = re.compile(r'^\(?\$?-?[\d,]*\d\.\d{2}\)?-?(CR)?$')
DATE_RE = re.compile(r'^\d{2}/\d{2}/\d{2}(\d{2})?$')

AMOUNT_COLS = ('billed', 'allowed', 'contractual', 'deductible', 'copay', 'paid')
TEXT_COLS = ('patient', 'account', 'dos', 'procedure', 'units', 'notes')
HEADER_KEYS = [  # a word in the header group -> column
    ('NAME', 'patient'), ('ACCOUNT', 'account'), ('DATES', 'dos'),
    ('PROCEDURE', 'procedure'), ('UNITS', 'units'), ('BILLED', 'billed'),
    ('ALLOWED', 'allowed'), ('CONTRACTUAL', 'contractual'), ('NOTES', 'notes'),
    ('DEDUCTIBLE', 'deductible'), ('CO-PAY', 'copay'), ('PAID', 'paid'),
]
RECAP_KEYS = {
    'APPROVE-TO-PAY:': 'approve_to_pay',
    'INTEREST PAYMENTS:': 'interest_payments',
    'OFFSETS TAKEN:': 'offsets_taken',
    'CHECK AMOUNT:': 'check_amount',
}
HEADER_LABELS = {'ISSUE DATE:': 'issue_date', 'EOB NUMBER:': 'eob_number',
                 'PROVIDER NUMBER:': 'provider_number', 'PROVIDER NPI:': 'provider_npi'}

WORD_GAP = 3.5      # words closer than this are one cell
AMOUNT_TOL = 8      # an amount's right edge vs its column's
LINE_TOL = 2.0      # words on one printed line
LABEL_X = 30        # row labels (TOTALS:, NOTES:, ...) start at the left margin
FOOTER_TOP = 740    # the "5 of 47" batch counter
LEGEND_SIZE = 6.5   # the notes legend is set larger than the table


def money(s):
    """'$1,113.00' '-5.00' '5.00-' '(5.00)' '5.00CR' -> Decimal."""
    t = str(s).replace('$', '').replace(',', '').strip()
    neg = False
    if t.endswith('CR'):
        t, neg = t[:-2], True
    if t.startswith('(') and t.endswith(')'):
        t, neg = t[1:-1], True
    if t.endswith('-'):
        t, neg = t[:-1], True
    if t.startswith('-'):
        t, neg = t[1:], True
    d = Decimal(t)
    return -d if neg else d


def keep_char(obj):
    """pdfplumber filter: content glyphs only."""
    if obj.get('object_type') != 'char':
        return True
    if obj['size'] < 5:                 # barcodes 1.44, form id 2.88, hidden 1.0
        return False
    if not obj.get('upright', True):    # the vertical form id
        return False
    if obj['x0'] > 595:                 # right-margin OMR/barcode strip
        return False
    col = obj.get('non_stroking_color')
    if isinstance(col, (tuple, list)) and col and all(isinstance(c, (int, float)) for c in col):
        if len(col) in (1, 3) and min(col) > 0.9:   # near-white
            return False
    return True


def lines_from_words(words):
    """Printed lines, each with its words merged into cells."""
    lines = []
    for w in sorted(words, key=lambda w: (round(w['top'], 1), w['x0'])):
        if lines and abs(w['top'] - lines[-1]['top']) < LINE_TOL:
            lines[-1]['words'].append(w)
        else:
            lines.append({'top': w['top'], 'words': [w]})
    for ln in lines:
        ln['words'].sort(key=lambda w: w['x0'])
        cells = []
        for w in ln['words']:
            if cells and w['x0'] - cells[-1]['x1'] <= WORD_GAP:
                cells[-1]['text'] += ' ' + w['text']
                cells[-1]['x1'] = w['x1']
            else:
                cells.append({'text': w['text'], 'x0': w['x0'], 'x1': w['x1'],
                              'size': w.get('size', 0)})
        ln['cells'] = cells
        ln['size'] = max(c['size'] for c in cells)
        ln['text'] = ' '.join(c['text'] for c in cells)
    return lines


def find_columns(lines):
    """({column: {x0, x1, right_limit}}, first data line, missing columns).
    Columns are None when the page has no table header."""
    for i, ln in enumerate(lines):
        t = ln['text']
        if not ('PROCEDURE' in t and 'BILLED' in t and 'DEDUCTIBLE' in t):
            continue
        hdr_lines = [l for l in lines[i:i + 3] if l['top'] - ln['top'] < 20]
        groups = []
        for w in sorted((w for l in hdr_lines for w in l['words']), key=lambda w: w['x0']):
            if groups and w['x0'] <= groups[-1]['x1'] + WORD_GAP:
                groups[-1]['x1'] = max(groups[-1]['x1'], w['x1'])
                groups[-1]['text'] += ' ' + w['text']
            else:
                groups.append({'x0': w['x0'], 'x1': w['x1'], 'text': w['text']})
        cols = {}
        for g in groups:
            for kw, key in HEADER_KEYS:
                if kw in g['text']:
                    if key == 'patient' and 'ACCOUNT' in g['text']:
                        continue
                    cols.setdefault(key, {'x0': g['x0'], 'x1': g['x1']})
                    break
        missing = [k for _, k in HEADER_KEYS if k not in cols]
        if missing:
            return None, None, missing
        order = sorted(cols, key=lambda k: cols[k]['x0'])
        for a, b in zip(order, order[1:]):
            cols[a]['right_limit'] = cols[b]['x0'] - 2
        cols[order[-1]]['right_limit'] = 612
        cols['units']['right_limit'] = (cols['units']['x1'] + cols['billed']['x0']) / 2
        return cols, i + len(hdr_lines), []
    return None, None, []


def _assign(cell, cols):
    if AMOUNT_RE.match(cell['text']):
        best = min(AMOUNT_COLS, key=lambda k: abs(cell['x1'] - cols[k]['x1']))
        if abs(cell['x1'] - cols[best]['x1']) <= AMOUNT_TOL:
            return best
    for k in TEXT_COLS:
        if cols[k]['x0'] - 2 <= cell['x0'] < cols[k]['right_limit']:
            return k
    return max(cols, key=lambda k: min(cell['x1'], cols[k]['x1']) - max(cell['x0'], cols[k]['x0']))


def _row(cells, cols):
    out = {}
    for c in cells:
        k = _assign(c, cols)
        out[k] = (out[k] + ' ' + c['text']) if k in out else c['text']
    return out


def _amounts(row, where, problems):
    out = {}
    for k in AMOUNT_COLS:
        if k not in row:
            continue
        try:
            out[k] = f'{money(row[k]):.2f}'
        except (InvalidOperation, ValueError):
            problems.append(f'{where}: {k} reads "{row[k]}", not an amount')
            out[k] = ''
    return out


def _header_fields(lines):
    h = {}
    for ln in lines:
        t = ln['text']
        for lab, key in HEADER_LABELS.items():
            i = t.find(lab)
            if i >= 0 and key not in h:
                h[key] = t[i + len(lab):].strip()
    if 'eob_number' in h:
        h['eob_number'] = (h['eob_number'].split() or [''])[0]
    if 'issue_date' in h:
        digits = re.sub(r'\D', '', h['issue_date'])[:8]
        if len(digits) == 6:
            h['issue_date'] = f'{digits[:2]}/{digits[2:4]}/20{digits[4:]}'
        elif len(digits) == 8:
            h['issue_date'] = f'{digits[:2]}/{digits[2:4]}/{digits[4:]}'
    for key in ('provider_number', 'provider_npi'):
        if key in h:
            h[key] = (h[key].split() or [''])[0]
    return h


def parse_eob_pages(pages):
    """The EOB from its words: one list per page of {text, x0, x1, top, size}.

    Returns the shape src/eob/plan.py posts from:
        {eob_number, issue_date, approve_to_pay, interest, offsets, check_amount,
         statement_totals, payment_issued_to, adjusted_claim, note_codes,
         claims: [{patient_account_number, bsc_claim_number, claim_paid,
                   adjusted_payment, claim_totals, lines: [...]}],
         problems: [every inconsistency found], parsed_ok}
    """
    header, raw_claims, legend = {}, [], []
    note_codes, messages, problems = {}, [], []
    statement, recap = {}, {}
    claim, receipt_date, state = None, None, 'none'   # none|lines|trailer|legend|recap
    saw_table = False

    for pno, words in enumerate(pages, 1):
        lines = lines_from_words(words)
        if not lines:
            continue
        for k, v in _header_fields(lines).items():
            header.setdefault(k, v)
        cols, start, missing = find_columns(lines)
        if cols is None:
            if missing:
                problems.append(f'page {pno}: the table header is missing {", ".join(missing)}')
            continue
        saw_table = True
        prev_legend_top = None
        for idx in range(start, len(lines)):
            ln = lines[idx]
            first = ln['cells'][0]
            label = first['text'] if first['x0'] < LABEL_X else ''
            if ln['top'] > FOOTER_TOP:
                continue
            if label.startswith('STATEMENT TOTALS'):
                statement = _amounts(_row(ln['cells'][1:], cols), 'STATEMENT TOTALS', problems)
                state = 'recap'
                continue
            if state == 'recap':
                for lab, key in RECAP_KEYS.items():
                    if ln['text'].startswith(lab):
                        try:
                            recap[key] = f"{money(ln['cells'][-1]['text']):.2f}"
                        except (InvalidOperation, ValueError):
                            problems.append(f'{lab} reads "{ln["cells"][-1]["text"]}", not an amount')
                continue
            if label.startswith('RECEIPT DATE'):
                receipt_date = ln['cells'][1]['text'] if len(ln['cells']) > 1 else ''
                continue
            if state == 'legend':
                if label and re.match(r'^[A-Z0-9]{1,5}$', label) and first['size'] > LEGEND_SIZE:
                    legend.append({'code': label, 'lines': [' '.join(c['text'] for c in ln['cells'][1:])]})
                elif not legend or (prev_legend_top is not None and ln['top'] - prev_legend_top > 10):
                    legend.append({'code': None, 'lines': [ln['text']]})
                else:
                    legend[-1]['lines'].append(ln['text'])
                prev_legend_top = ln['top']
                continue
            if label == 'TOTALS:' and claim is not None:
                claim['claim_totals'] = _amounts(_row(ln['cells'][1:], cols),
                                                 f"claim {len(raw_claims)} TOTALS", problems)
                state = 'trailer'
                continue
            if label == 'NOTES:':
                nxt = lines[idx + 1] if idx + 1 < len(lines) else None
                if state == 'trailer' and 'claim_notes' not in claim:
                    claim['claim_notes'] = ' '.join(c['text'] for c in ln['cells'][1:])
                    continue
                if nxt is not None and nxt['size'] > LEGEND_SIZE:
                    state, prev_legend_top = 'legend', None
                    continue
            if claim is not None and any(c['text'].startswith('Adjusted Payment') for c in ln['cells']):
                amts = [tok for c in ln['cells'] for tok in c['text'].split() if AMOUNT_RE.match(tok)]
                claim['adjusted_payment'] = f'{money(amts[-1]):.2f}' if amts else ''
                continue

            m = _row(ln['cells'], cols)
            dos = m.get('dos', '').split()
            if not (dos and DATE_RE.match(dos[0]) and m.get('procedure')):
                # A one-line claim stacks its member id / group and its claim
                # number on the rows under it, with no service data beside them.
                if claim is not None and state == 'lines' and m and set(m) <= {'patient', 'account'}:
                    claim['_names'] += [m['patient']] if 'patient' in m else []
                    claim['_accts'] += [m['account']] if 'account' in m else []
                continue
            if claim is None or ((m.get('patient') or m.get('account')) and state == 'trailer'):
                claim = {'receipt_date': receipt_date, '_names': [], '_accts': [], 'lines': [], 'pages': []}
                raw_claims.append(claim)
                state = 'lines'
            if pno not in claim['pages']:
                claim['pages'].append(pno)
            if m.get('patient'):
                claim['_names'].append(m['patient'])
            if m.get('account'):
                claim['_accts'].append(m['account'])
            proc = m['procedure'].split()
            amts = _amounts(m, f"claim {len(raw_claims)} line {len(claim['lines']) + 1} ({proc[0]})", problems)
            claim['lines'].append({
                'dos': dos[0], 'dos_to': dos[-1],
                'cpt': proc[0], 'modifiers': proc[1:],
                'units': m.get('units', '').strip(),
                'billed': amts.get('billed', '0.00'),
                'allowed': amts.get('allowed', '0.00'),
                'contractual': amts.get('contractual', ''),
                'notes_code': m.get('notes', ''),
                'deductible': amts.get('deductible', '0.00'),
                'copay': amts.get('copay', '0.00'),
                'paid': amts.get('paid', '0.00'),
                'page': pno,
            })

    claims = []
    for c in raw_claims:
        names, accts = c.pop('_names'), c.pop('_accts')
        k = next((i for i, t in enumerate(names) if re.search(r'\d', t)), len(names))
        totals = c.get('claim_totals') or {}
        adjusted = c.get('adjusted_payment') or ''
        claims.append({
            'patient_account_number': accts[0] if accts else '',
            'bsc_claim_number': accts[1] if len(accts) > 1 else '',
            'patient_name': ' '.join(names[:k]),
            'member_id': names[k] if k < len(names) else '',
            'group_number': names[k + 1] if k + 1 < len(names) else '',
            'receipt_date': c.get('receipt_date') or '',
            # What this check pays on the claim. An adjustment pays its own
            # amount, which need not equal the lines' paid total.
            'claim_paid': adjusted or totals.get('paid', ''),
            'adjusted_payment': adjusted,
            'claim_totals': totals,
            'claim_notes': c.get('claim_notes', ''),
            'pages': c['pages'],
            'lines': c['lines'],
        })
    for p in legend:
        text = re.sub(r'\s+', ' ', ' '.join(p['lines'])).strip()
        if p['code']:
            note_codes[p['code']] = text
        else:
            messages.append(text)
    blob = ' '.join(messages)
    m = re.search(r'PAYMENT WAS ISSUED TO (.+?)\.', blob)

    eob = {
        'eob_number': header.get('eob_number', ''),
        'issue_date': header.get('issue_date', ''),
        'provider_number': header.get('provider_number', ''),
        'provider_npi': header.get('provider_npi', ''),
        'approve_to_pay': recap.get('approve_to_pay', ''),
        'interest': recap.get('interest_payments', ''),
        'offsets': recap.get('offsets_taken', ''),
        'check_amount': recap.get('check_amount', ''),
        'statement_totals': statement,
        'payment_issued_to': m.group(1).strip() if m else '',
        'adjusted_claim': 'ADJUSTED PAYMENT FOR A PREVIOUSLY PROCESSED CLAIM' in blob,
        'note_codes': note_codes,
        'messages': messages,
        'claims': claims,
    }
    eob['problems'] = problems + _inconsistencies(eob)
    eob['parsed_ok'] = bool(saw_table and eob['eob_number'] and claims)
    return eob


def _inconsistencies(eob):
    """Every place the report disagrees with itself. Any of these means a
    column was misread or a field is not understood — never post over one."""
    D, out = Decimal, []
    for c in eob['claims']:
        who = f"claim {c['patient_account_number'] or '?'}"
        t = c['claim_totals']
        if not t:
            out.append(f'{who}: no TOTALS row was read')
        for f in ('billed', 'deductible', 'copay', 'paid'):
            if t.get(f):
                s = sum((D(l[f]) for l in c['lines'] if l[f]), D('0'))
                if s != D(t[f]):
                    out.append(f"{who}: its lines' {f} add up to {s:.2f}, its TOTALS row says {t[f]}")
        for n, l in enumerate(c['lines'], 1):
            vals = [l[f] for f in ('allowed', 'deductible', 'copay', 'paid')]
            if all(vals) and D(vals[0]) - D(vals[1]) - D(vals[2]) != D(vals[3]):
                out.append(f"{who} line {n} ({l['cpt']}): allowed {vals[0]} − deductible {vals[1]} − "
                           f"co-pay {vals[2]} is not paid {vals[3]}")
    need = ('approve_to_pay', 'interest', 'offsets', 'check_amount')
    if not all(eob[k] for k in need):
        out.append('the recap (approve-to-pay / interest / offsets / check amount) was not read in full')
        return out
    atp, intr, off, chk = (D(eob[k]) for k in need)
    st_paid = eob['statement_totals'].get('paid')
    if st_paid and D(st_paid) != atp + intr:
        out.append(f'statement paid {st_paid} is not approve-to-pay {atp:.2f} + interest {intr:.2f}')
    if eob['payment_issued_to']:
        if chk != 0:
            out.append(f'the member was paid, yet the check amount is {chk:.2f}')
    elif chk != atp + intr - off:
        out.append(f'check amount {chk:.2f} is not approve-to-pay {atp:.2f} + interest {intr:.2f} '
                   f'− offsets {off:.2f}')
    return out


def read_eob_pdf(path):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        pages = [p.filter(keep_char).extract_words(x_tolerance=1, y_tolerance=2, keep_blank_chars=False,
                                                   use_text_flow=False, extra_attrs=['size'])
                 for p in pdf.pages]
    return parse_eob_pages(pages)
