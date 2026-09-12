"""Vignesh's table: what one drug line is billed at on each side.

The clinic bills these unlisted-drug codes in eCW at Blue Shield's amount, and
Blue Shield's EOB comes back billed at the Medicare amount. So when a J3490 or
J7999 appears more than once on a claim, an EOB line's billed amount matches no
eCW line's, and this table is the only thing that says which eCW line an EOB
line belongs to.

The operator's lookup, seen in the 2026-09-09 recording: take the eCW line's
billed amount, find it in "Blue Shield Amount", read "Medicare Billed Amount"
across from it, and post the EOB line billed that — eCW's $900.00 ascorbic acid
is the EOB's $300.00 line. Claims are also matched the other way round, so both
columns are searched.

The code is part of the key: Methylcobalamin is listed twice, once as J3490 and
once as J7999, with the same pair of amounts.

Source: Vignesh's e-mail to the practice, 2026-09-09 ("This is the table you
need to post the payment for J3490 when billed amount in EOB and ECW are
different"). Amounts are line totals, kept as exact-cents strings.
"""
from decimal import Decimal, InvalidOperation

# (code, drug, Medicare amount — what the EOB bills, Blue Shield amount — what eCW bills)
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


def _d(v):
    try:
        return Decimal(str(v).replace('$', '').replace(',', '').strip())
    except (InvalidOperation, ValueError):
        return None


def counterpart_amounts(code, amount):
    """What the other side bills for `code` at `amount`, and the drug it means.

    Several drugs share an amount — a J7999 billed $40.00 on the EOB is
    Dexpanthenol at $40.00 in eCW, but Pyridoxine bills $40.00 there too — so
    every candidate is returned and the matcher decides from the rest of the
    claim.
    """
    want, out = _d(amount), []
    if want is None:
        return out
    for c, drug, medicare, blue_shield in DRUG_TABLE:
        if c != code:
            continue
        for here, there in ((blue_shield, medicare), (medicare, blue_shield)):
            if _d(here) == want and (there, drug) not in out:
                out.append((there, drug))
    return out
