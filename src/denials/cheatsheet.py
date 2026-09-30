"""The billing team's denial playbook — the SOP's "Denial Cheatsheet".

The operator (2026-09-30): "revisar en eCW los claims que están denied y
revisa la razón, y las posibles soluciones. En el SOP encuentras los códigos
y las razones." One row of the sheet:

    S No · Codes · Drug Name · Payer · Reason · Action
    1    · J0612 · Calcium Gluconate · Medicare · CO-16 · Reimbursement is
           only $0.04. So please write off the balance

A rule names the procedure codes it covers (J3490 / J7999), the payer
(Medicare, Anthem Blue Cross, Any Payer) and the denial reason as the payer
words it — CARC/RARC codes (CO-16, CO-16 / 96, PI:96, PR:96, N846 / N816,
CO-252) or a word (Duplicate, Non-Covered, B16 - New Patient qualifications
not met). The action is the team's own instruction, often a whole letter.

The sheet lives in data/denial_cheatsheet.json (scripts/import_denial_sop.py
copies it out of the SOP workbook; the workbook holds logins and is never
committed). Matching is by payer, code and reason; a claim that matches no
rule is still listed — for a person.
"""
import json
import os
import re

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    'data', 'denial_cheatsheet.json')

# Payer, as the sheet names it → what the claim's payer text must contain.
PAYER_WORDS = {
    'medicare': ('medicare', 'noridian', 'cms'),
    'anthem': ('anthem', 'blue cross'),
    'blue shield': ('blue shield',),
    'aetna': ('aetna',),
    'cigna': ('cigna',),
    'uhc': ('united', 'uhc', 'optum'),
}
ANY_PAYER = ('any payer', 'all payers', 'any', 'all')

# CO-16 · CO16 · CO 16 · PI:96 · PR-96 · OA-23 · CR-45
GROUP_CODE_RX = re.compile(r'\b(CO|PR|PI|OA|CR)\s*[-:\s]?\s*(\d{1,3})\b', re.I)
# N846 · M127 · MA130 — remark codes; B16 and the like — the payer's own reason codes.
REMARK_RX = re.compile(r'\b(N\d{2,3}|MA\d{2,3}|M\d{1,3}|B\d{1,2})\b', re.I)
BARE_NUM_RX = re.compile(r'(?<![A-Za-z\d-])(\d{1,3})(?![\d])')
WORD_REASONS = ('duplicate', 'non-covered', 'non covered', 'not covered', 'medical necessity', 'experimental',
                'investigational', 'timely', 'bundled', 'inclusive', 'not payable', 'no authorization',
                'authorization', 'documentation', 'coding')

KINDS = (
    ('write off', re.compile(r'write\s*off', re.I)),
    ('appeal', re.compile(r'\bappeal', re.I)),
    ('medical records', re.compile(r'medical necessity|medical records|medical documentation|cover letter', re.I)),
    ('inquiry', re.compile(r'inquiry|reprocess|clarification|availity', re.I)),
    ('recode', re.compile(r'change the code|bill (?:using|with)|rebill|corrected claim', re.I)),
)


def norm_code(text):
    """'co-16' / 'CO 16' / 'CO:16' → 'CO-16'; 'n846' → 'N846'; '96' → '96'."""
    t = str(text or '').strip().upper()
    m = GROUP_CODE_RX.fullmatch(t) or GROUP_CODE_RX.match(t)
    if m and m.end() == len(t):
        return f"{m.group(1)}-{int(m.group(2))}"
    if re.fullmatch(r'[A-Z]{1,2}\d{1,3}', t):
        return t
    if re.fullmatch(r'\d{1,3}', t):
        return str(int(t))
    return t


def norm_reason(text):
    """A code the way norm_code writes it; a reason word in lower case."""
    t = str(text or '').strip()
    if GROUP_CODE_RX.fullmatch(t) or re.fullmatch(r'[A-Za-z]{1,2}\d{1,3}|\d{1,3}', t):
        return norm_code(t)
    return t.lower()


def codes_in(text):
    """Every reason code in a piece of text — CARC group codes (CO-16), the
    bare CARC number too (16, so 'CO-16 / 96' also yields 96), remark codes
    (N846), the payer's letter codes (B16) — and the reason words."""
    t = str(text or '')
    out = []
    for m in GROUP_CODE_RX.finditer(t):
        out += [f"{m.group(1).upper()}-{int(m.group(2))}", str(int(m.group(2)))]
    for m in REMARK_RX.finditer(t):
        out.append(m.group(1).upper())
    # A bare number after a slash or comma continues the list: 'CO-16 / 96'.
    for m in re.finditer(r'[/,]\s*(\d{1,3})\b', t):
        out.append(str(int(m.group(1))))
    low = t.lower()
    out += [w for w in WORD_REASONS if w in low]
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def procedure_codes(text):
    """J3490, J0612-JZ → J0612, 99205, 96365, G2212 — the codes on a claim or a rule."""
    return sorted({m.group(1).upper() for m in re.finditer(r'\b([A-Z]\d{4}|\d{5})(?:-[A-Z0-9]{2})?\b', str(text or '').upper())})


def payer_key(text):
    low = str(text or '').lower()
    if not low.strip() or any(low.strip() == a for a in ANY_PAYER):
        return 'any'
    for key, words in PAYER_WORDS.items():
        if any(w in low for w in words):
            return key
    return low.strip()


def action_kind(action):
    for kind, rx in KINDS:
        if rx.search(str(action or '')):
            return kind
    return 'review'


class Rule:
    def __init__(self, raw):
        self.sop_row = str(raw.get('sop_row', ''))
        self.codes = procedure_codes(raw.get('codes', ''))
        self.drug = str(raw.get('drug', '')).strip()
        self.payer = str(raw.get('payer', '')).strip()
        self.payer_key = payer_key(self.payer)
        self.reason = str(raw.get('reason', '')).strip()
        self.reasons = codes_in(self.reason)
        self.action = str(raw.get('action', '')).strip()
        self.kind = action_kind(self.action)

    def as_dict(self):
        return {'sop_row': self.sop_row, 'codes': ' / '.join(self.codes), 'drug': self.drug, 'payer': self.payer,
                'reason': self.reason, 'action': self.action, 'kind': self.kind}

    def score(self, claim_codes, claim_payer_key, claim_reasons):
        """How well this rule fits: payer (2 exact, 1 any-payer, none → no
        match), procedure code (2; none named → 1), reason (3 per reason code
        in common; a rule with reasons that shares none → no match)."""
        s = 0
        if self.payer_key == 'any':
            s += 1
        elif self.payer_key == claim_payer_key:
            s += 2
        else:
            return 0
        if self.codes:
            if not (set(self.codes) & set(claim_codes)):
                return 0
            s += 2
        else:
            s += 1
        if self.reasons:
            shared = set(self.reasons) & set(claim_reasons)
            if not shared:
                return 0
            s += 3 * len(shared)
        return s


def load_rules(path=DATA):
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)
    return [Rule(r) for r in data.get('rules', [])], data.get('appeals', [])


def match(claim, rules):
    """The rules that fit a denied claim, best first. `claim`: {'cpt': text
    with the procedure codes, 'payer': text, 'reasons': list or text}."""
    codes = procedure_codes(claim.get('cpt', ''))
    pk = payer_key(claim.get('payer', ''))
    reasons = claim.get('reasons') or []
    if isinstance(reasons, str):
        reasons = codes_in(reasons)
    reasons = [norm_reason(r) for r in reasons]
    scored = [(r.score(codes, pk, reasons), r) for r in rules]
    hits = sorted([(s, r) for s, r in scored if s > 0], key=lambda x: (-x[0], x[1].sop_row))
    # Only the best fit and its ties, so a claim does not get every J3490 rule.
    if not hits:
        return []
    top = hits[0][0]
    return [r for s, r in hits if s == top]
