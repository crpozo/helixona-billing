"""The service lines of a HCFA-1500 (box 24) — what eCW actually billed.

The claim record keeps only a list of codes (mixed with ICD-10 codes, at
that). The HCFA PDF eCW generated for the claim has every line: dates, POS,
CPT/HCPCS, charges, units — and, in the shaded supplemental row above each
drug line, the NDC and the drug itself ("N467457014630 , 1 ML B COMPLEX 100").
That drug text is what tells four J3490 lines on one claim apart.

Parsed from word coordinates as fractions of the page, the same way box 1a
is read in main.py; claims with more than six lines continue on later pages.
The parser takes plain word dicts so it is testable without a PDF.
"""
import re
from decimal import Decimal

CPT_RE = re.compile(r'^(\d{5}|[A-Z]\d{4})$')
# "N4" is the CMS-1500 qualifier for an NDC; the 11-digit NDC follows it.
NDC_RE = re.compile(r'^N4(\d{11})$')

# Box 24 column bands, as fractions of page width (CMS-1500 02-12).
X_CPT = (0.30, 0.345)
X_CHARGES = (0.62, 0.705)     # dollars and cents cells; see _charges
X_UNITS = (0.705, 0.74)
X_POS = (0.23, 0.27)
X_DOS_FROM = (0.0, 0.13)
X_DOS_TO = (0.13, 0.23)
Y_BOX24 = (0.62, 0.87)
ROW_TOL = 0.006               # one service line's tokens drift a little in y
SUPP_ABOVE = (0.008, 0.022)   # the shaded NDC/drug row sits just above its line


def _in(v, band):
    return band[0] <= v < band[1]


def _charges(tokens):
    """Dollars and cents come as separate cells ("325" "00") or merged by the
    text layer ("57500", "2750"). Concatenate the digits; the last two are
    cents."""
    digits = ''.join(t['text'] for t in sorted(tokens, key=lambda t: t['x'])
                     if t['text'].isdigit())
    if len(digits) < 3:
        return ''
    return f'{Decimal(digits[:-2] + "." + digits[-2:]):.2f}'


def _date(tokens):
    parts = [t['text'] for t in sorted(tokens, key=lambda t: t['x']) if t['text'].isdigit()]
    if len(parts) < 3:
        return ''
    mm, dd, yy = parts[0].zfill(2), parts[1].zfill(2), parts[2]
    return f'{mm}/{dd}/{"20" + yy if len(yy) == 2 else yy}'


def parse_hcfa_lines(pages_words):
    """Service lines from box 24.

    `pages_words`: one list per page of {'text', 'x', 'y'} with x, y as
    fractions of the page's width and height (top-left origin).
    Returns dicts: page, dos_from, dos_to, pos, cpt, units, billed, ndc, drug.
    """
    lines = []
    for page_no, words in enumerate(pages_words, 1):
        box = [w for w in words if _in(w['y'], Y_BOX24)]
        for cpt_tok in sorted((w for w in box if CPT_RE.match(w['text']) and _in(w['x'], X_CPT)),
                              key=lambda w: w['y']):
            cy = cpt_tok['y']
            row = [w for w in box if abs(w['y'] - cy) <= ROW_TOL]
            supp = [w for w in words if SUPP_ABOVE[0] <= cy - w['y'] <= SUPP_ABOVE[1]]
            supp = sorted(supp, key=lambda w: w['x'])
            ndc, drug = '', ''
            m = NDC_RE.match(supp[0]['text']) if supp else None
            if m:
                ndc = m.group(1)
                rest = [w['text'] for w in supp[1:] if w['text'] != ',']
                drug = ' '.join(rest)
            units = [w['text'] for w in row if _in(w['x'], X_UNITS) and w['text'].isdigit()]
            pos = [w['text'] for w in row if _in(w['x'], X_POS) and w['text'].isdigit()]
            lines.append({
                'page': page_no,
                'dos_from': _date([w for w in row if _in(w['x'], X_DOS_FROM)]),
                'dos_to': _date([w for w in row if _in(w['x'], X_DOS_TO)]),
                'pos': pos[0] if pos else '',
                'cpt': cpt_tok['text'],
                'units': units[0] if units else '',
                'billed': _charges([w for w in row if _in(w['x'], X_CHARGES)]),
                'ndc': ndc,
                'drug': drug,
            })
    return lines


def hcfa_lines_from_pdf(path):
    import pdfplumber
    pages = []
    with pdfplumber.open(path) as pdf:
        for pg in pdf.pages:
            W, H = float(pg.width), float(pg.height)
            pages.append([{'text': w['text'], 'x': w['x0'] / W, 'y': w['top'] / H}
                          for w in pg.extract_words()])
    return parse_hcfa_lines(pages)


def total_billed(lines):
    return f"{sum((Decimal(l['billed']) for l in lines if l['billed']), Decimal('0')):.2f}"
