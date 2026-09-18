"""Pure parsing for the EOB bot — no browser, no AWS, so every rule here is
testable in isolation.

Three things come out of the Blue Shield portal and one out of a PDF:

* the claim-status results table (one row per finalized, paid claim),
* the Check/EFT transaction summary (label/value pairs),
* the Check/EFT claims table (the claims one check paid),
* the EOB report PDF (per-claim CPT lines, and the PATIENT ACCOUNT NUMBER,
  which is the eCW claim number — our `claim_id`).

Tables are read BY HEADER NAME. The old In-process scraper split each row on
tabs and trusted column positions; the Finalized view has an extra "EOB"
column and moves Check/EFT to the end, so positional parsing silently reads
the wrong field. Names survive column reshuffles; positions do not.
"""
import re
from decimal import Decimal, InvalidOperation

# Header text (lower-cased, whitespace-collapsed) -> canonical key. Both the
# results table and the Check/EFT claims table are covered.
_HEADER_KEYS = [
    ('claim status', 'status'),
    ('claim number', 'bsc_claim_number'),
    ('claim #', 'bsc_claim_number'),
    ('claim type', 'claim_type'),
    ('dates of service', 'dos'),
    ('eob', 'eob'),
    ('member name', 'member_name'),
    ('member id', 'subscriber_id'),
    ('subscriber id', 'subscriber_id'),
    ('provider name', 'provider'),
    ('claim amount billed', 'amount_billed'),
    ('claim amount paid', 'amount_paid'),
    ('patient responsibility', 'patient_resp'),
    ('check/eft number', 'check_eft'),
    ('check/eft date', 'check_date'),
    ('check/eft amount', 'check_amount'),
    ('check status', 'check_status'),
    ('check/eft', 'check_eft'),
    ('patient account number', 'patient_account_number'),
    ('payment date', 'payment_date'),
    ('allowed amount', 'allowed'),
    ('deductible amount', 'deductible'),
    ('co-insurance amount', 'coinsurance'),
    ('date finalized', 'date_finalized'),
    ('row #', 'row'),
]

MONEY_RE = re.compile(r'-?\$?\s*([\d,]+\.\d{2})')
DATE_RE = re.compile(r'\b(\d{2}/\d{2}/\d{4})\b')


def norm_text(s) -> str:
    return re.sub(r'\s+', ' ', str(s or '')).strip()


def header_key(header: str):
    """Canonical key for a column header, or None if it is not one we use."""
    h = norm_text(header).lower()
    for needle, key in _HEADER_KEYS:
        if h.startswith(needle):
            return key
    return None


CLAIM_NO_RE = re.compile(r'(\d{6,})\s*(?:\((.*?)\))?')
CHECK_NO_RE = re.compile(r'\d{5,11}')


def rows_by_header(headers, rows):
    """Turn header texts + cell-text rows into dicts keyed by canonical name.

    Cells beyond the header count are ignored; short rows fill what they can.
    Rows with no recognisable claim number are dropped — they are spacer or
    note rows ("(adjusted)"), not claims.

    A column the header row does not name — a select-all checkbox, a chevron
    — puts one cell more in each row than there are headers, and every value
    lands one column to the left of its name. Each row is therefore slid
    until its claim number sits under the "Claim number" header (2026-09-15:
    fifty rows parsed, not one check number among them).
    """
    keys = [header_key(h) for h in headers]
    claim_at = next((i for i, k in enumerate(keys) if k == 'bsc_claim_number'), None)
    out = []
    for cells in rows:
        cells = [norm_text(c) for c in cells]
        shift = 0
        if claim_at is not None:
            for cand in (0, 1, -1, 2, -2):
                j = claim_at + cand
                if 0 <= j < len(cells) and CLAIM_NO_RE.match(cells[j]):
                    shift = cand
                    break
        aligned = cells[shift:] if shift >= 0 else [''] * (-shift) + cells
        d = {}
        for k, v in zip(keys, aligned):
            if k and k not in d:
                d[k] = v
        # "260703051601 (adjusted)" -> keep the number, remember the note.
        m = CLAIM_NO_RE.match(d.get('bsc_claim_number', ''))
        if not m:
            continue
        d['bsc_claim_number'] = m.group(1)
        if m.group(2):
            d['claim_note'] = m.group(2)
        if shift:
            d['column_shift'] = shift
        # The check cell carries a label with the number — "30912971
        # Check/EFT information" on screen (2026-09-15). Keep the number.
        if d.get('check_eft'):
            cm = CHECK_NO_RE.search(d['check_eft'])
            if cm and cm.group(0) != d['check_eft']:
                d['check_eft_text'] = d['check_eft']
                d['check_eft'] = cm.group(0)
        out.append(d)
    return out


def rows_with_links(headers, raw_rows):
    """rows_by_header over DOM rows ({'cells', 'links'}), with each row's
    check and EOB links attached.

    The check number is a link on the results page. When the header
    mapping did not yield one (a column named unexpectedly, a layout not
    seen before) the link is the fallback: the row's one link whose text is
    a 5-to-11-digit number that is not the 12-digit claim number.
    """
    rows = rows_by_header(headers, [r['cells'] for r in raw_rows])
    # rows_by_header drops non-claim rows, so re-pair links by claim number.
    by_claim = {}
    for r in raw_rows:
        m = re.search(r'\b(\d{12})\b', ' '.join(str(c) for c in r.get('cells', [])))
        if m:
            by_claim[m.group(1)] = r.get('links', [])
    def number_in(text):
        t = norm_text(text)
        if re.search(r'\b\d{12}\b', t):
            return ''                       # the claim number, not a check
        m = CHECK_NO_RE.search(t)
        return m.group(0) if m else ''

    for row in rows:
        links = by_claim.get(row.get('bsc_claim_number'), [])
        if not CHECK_NO_RE.fullmatch(row.get('check_eft', '')):
            numbered = [l for l in links if number_in(l.get('text'))]
            if len(numbered) == 1:
                row['check_eft'] = number_in(numbered[0]['text'])
                row['check_from'] = 'link'
        for l in links:
            t = norm_text(l.get('text'))
            if row.get('check_eft') and number_in(t) == row['check_eft'] and l.get('href'):
                row['check_href'] = l['href']
            elif 'eob' in t.lower() and l.get('href'):
                row['eob_href'] = l['href']
    return rows


def money(s):
    """'$1,113.00' -> '1113.00'; '' -> ''. Kept as a string: DynamoDB and the
    audit trail want exact cents, not floats."""
    m = MONEY_RE.search(str(s or ''))
    if not m:
        return ''
    val = m.group(1).replace(',', '')
    try:
        d = Decimal(val)
    except InvalidOperation:
        return ''
    if str(s).strip().startswith('-'):
        d = -d
    return f'{d:.2f}'


def dos_start(dos_text):
    """'01/23/2026–01/23/2026' -> '01/23/2026' (the first date in the cell)."""
    m = DATE_RE.search(str(dos_text or ''))
    return m.group(1) if m else ''


def parse_check_summary(text):
    """The Check/EFT transaction summary as label/value pairs, from page text.

    The portal lays it out as two rows of four LABELS, each followed by a row
    of four VALUES — so "the value after its label" is usually some other
    field's value. Each field is therefore found by its own shape (a check
    number, a money amount, a date, a name) inside the text that follows its
    label, which holds whether the labels are grouped or interleaved.
    """
    t = norm_text(text)
    low = t.lower()

    def window(label, span=400):
        i = low.find(label.lower())
        return t[i + len(label):i + len(label) + span] if i >= 0 else ''

    def first(pattern, seg):
        m = re.search(pattern, seg)
        return m.group(1) if m else ''

    out = {
        'check_eft': first(r'(\d{5,})', window('Check/EFT #')),
        'check_amount': money(first(r'(-?\$?[\d,]+\.\d{2})', window('Check/EFT amount'))),
        'check_date': first(r'(\d{2}/\d{2}/\d{4})', window('Check/EFT date')),
        'cashed_date': first(r'(\d{2}/\d{2}/\d{4})', window('Check cashed date')),
    }

    # Status: the words after the label, once any check number / amount /
    # date that precede it in a grouped layout are skipped.
    seg = window('Check/EFT status', 200)
    seg = re.sub(r'\d{5,}|-?\$?[\d,]+\.\d{2}|\d{2}/\d{2}/\d{4}', ' ', seg)
    seg = re.split(r'Check cashed date|Payee name|Payee address|Number of claims|Claims information',
                   seg, flags=re.I)[0]
    out['check_status'] = norm_text(seg)[:40]

    # Payee: the capitalised name after the label, once a leading date (the
    # cashed date in a grouped layout) is skipped; it ends where the street
    # address starts with a number.
    seg = window('Payee name', 300)
    seg = re.split(r'Claims information', seg, flags=re.I)[0]
    seg = re.sub(r'Payee address|Number of claims|\d{2}/\d{2}/\d{4}', ' ', seg, flags=re.I)
    m = re.search(r'([A-Za-z][A-Za-z .,\'-]{2,60}?)(?=\s+\d|\s*$)', seg)
    out['payee_name'] = norm_text(m.group(1)) if m else ''

    # Number of claims: the last small integer before the claims table, with
    # dates, money and street numbers removed first.
    seg = window('Number of claims', 300)
    seg = re.split(r'Claims information', seg, flags=re.I)[0]
    seg = re.sub(r'\d{2}/\d{2}/\d{4}|-?\$?[\d,]+\.\d{2}|\d{5,}(?:-\d+)?', ' ', seg)
    nums = re.findall(r'(?<![\w-])(\d{1,4})(?![\w-])', seg)
    out['num_claims'] = nums[-1] if nums else ''
    return {k: v.strip() for k, v in out.items()}


def subscriber_key(s):
    """A subscriber ID as a matching key: upper-case, without the member
    suffix the portal appends ("909151452-01" is subscriber 909151452)."""
    return re.sub(r'-\d{1,3}$', '', norm_text(s).upper())


def index_claims(claims):
    """Our claims by (subscriber_id, DOS start) for matching portal rows.

    A day with two claims for one member is real (an office visit and an IV),
    so a key can hold several claims; `match_claim` breaks the tie on billed
    charges.
    """
    idx = {}
    for c in claims:
        sid = subscriber_key(c.get('subscriber_id'))
        dos = dos_start(c.get('service_date') or c.get('dos'))
        if sid and dos:
            idx.setdefault((sid, dos), []).append(c)
    return idx


def match_claim(row, idx):
    """Our claim_id for a portal row, or '' when nothing is certain.

    Never guesses between candidates: an EOB pinned to the wrong claim would
    post a payment against the wrong service.
    """
    sid = subscriber_key(row.get('subscriber_id'))
    dos = dos_start(row.get('dos'))
    cands = idx.get((sid, dos), [])
    if len(cands) == 1:
        return str(cands[0].get('claim_id', ''))
    if len(cands) > 1:
        billed = money(row.get('amount_billed'))
        same = [c for c in cands if money(c.get('charges')) == billed and billed]
        if len(same) == 1:
            return str(same[0].get('claim_id', ''))
    return ''


# ---------------------------------------------------------------- EOB PDF
_CPT_LINE = re.compile(
    r'(?P<dos>\d{2}/\d{2}/\d{2,4})\s+'
    r'(?P<cpt>[A-Z]?\d{4,5})\s+'
    r'(?P<units>\d+)\s+'
    r'(?P<amounts>(?:-?[\d,]+\.\d{2}\s+){3,8}-?[\d,]+\.\d{2})'
)


def parse_eob_pdf_text(text):
    """Best-effort structure from an EOB report's text.

    What is reliable: the EOB number, the issue date, the statement totals,
    and every PATIENT ACCOUNT NUMBER / CLAIM NUMBER pair. What is best-effort:
    the CPT lines — their column set is inferred from the amounts present.
    The raw text is stored alongside, so a better parser can re-read it.
    """
    t = text or ''
    flat = norm_text(t)
    out = {
        'eob_number': '',
        'issue_date': '',
        'approve_to_pay': '',
        'check_amount': '',
        'claims': [],
        'parsed_ok': False,
    }
    m = re.search(r'EOB NUMBER:?\s*([A-Z0-9]{8,})', flat, re.I)
    if m:
        out['eob_number'] = m.group(1)
    m = re.search(r'ISSUE DATE:?\s*(\d{2}[ /.]\d{2}[ /.]\d{2,4})', flat, re.I)
    if m:
        out['issue_date'] = m.group(1)
    m = re.search(r'APPROVE-?TO-?PAY:?\s*\$?([\d,]+\.\d{2})', flat, re.I)
    if m:
        out['approve_to_pay'] = money(m.group(1))
    m = re.search(r'CHECK AMOUNT:?\s*\$?([\d,]+\.\d{2})', flat, re.I)
    if m:
        out['check_amount'] = money(m.group(1))

    # Each claim block names the patient, then the account number (our
    # claim_id) and the payer's claim number. Account numbers are short
    # integers; claim numbers are 12 digits.
    for bm in re.finditer(r'(\d{1,7})\s+(\d{12})\b', flat):
        acct, claim_no = bm.group(1), bm.group(2)
        out['claims'].append({
            'patient_account_number': acct,
            'bsc_claim_number': claim_no,
            'lines': [],
        })

    lines = []
    for lm in _CPT_LINE.finditer(flat):
        amts = [money(a) for a in lm.group('amounts').split()]
        lines.append({
            'dos': lm.group('dos'),
            'cpt': lm.group('cpt'),
            'units': lm.group('units'),
            'billed': amts[0] if amts else '',
            'allowed': amts[1] if len(amts) > 1 else '',
            'paid': amts[-1] if amts else '',
            'amounts': amts,
        })
    if out['claims'] and lines:
        # Without positions inside the PDF the lines cannot be assigned to
        # claims with certainty; keep them at the statement level.
        out['lines'] = lines
    out['parsed_ok'] = bool(out['eob_number'] and out['claims'])
    return out
