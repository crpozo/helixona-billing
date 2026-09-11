"""Vignesh's table: what a drug is billed at in eCW versus on a Blue Shield EOB.

eCW bills these unlisted-drug codes at the Medicare amount; Blue Shield's EOB
shows the claim billed at the Blue Shield amount. So when a J3490 or J7999
appears more than once on a claim, the EOB line's billed amount does not equal
any eCW line's billed amount, and this table is the only way to tell which
eCW line an EOB line belongs to. Source: Vignesh's e-mail to the practice,
2026-09-09 ("This is the table you need to post the payment for J3490 when
billed amount in EOB and ECW are different").

Amounts are line totals as shown in the table, kept as exact-cents strings.
"""

# (code, drug, eCW billed amount, Blue Shield billed amount)
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


def ecw_amounts_for(code: str, eob_billed: str):
    """Every eCW billed amount the table allows for an EOB line of `code`
    billed at `eob_billed`, with the drug each would mean.

    Several drugs can share an EOB amount (J7999 $40.00 is Dexpanthenol at
    $40.00 in eCW, but Pyridoxine at $20.00 bills $40.00 to Blue Shield too),
    so this returns all of them; the matcher decides using the other lines.
    """
    return [(ecw, drug) for c, drug, ecw, bsc in DRUG_TABLE
            if c == code and bsc == eob_billed]
