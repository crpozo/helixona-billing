# B10 — the bank against the remittances against eCW

RCM Automation Punch List v1.0 (2026-11-05), ticket B10: *"Match EFT/trace
to ERA, check scan/log to deposits, deposits to remittances, and remittances
to claim/service-line postings; allow one-to-many batches and reversals."*

Built on Remittance, not beside it ("no duplicate reconciliation codebase"):
the 🧾 Remittance tab already knows, per check or EFT, the scan, the deposit
slip, the tracker's deposit date, the payer's status, the 835 and the eCW
payment. B10 adds the one source it lacked — **the bank's own transactions**
— in the panel **🏦 Bank reconciliation** on the 💵 Posting tab (B10's own bot).

No bot, no browser, no model and no host setup: the dashboard computes it
from the uploaded statement and the Checks table each time the panel loads.
Read-only: nothing is written to the bank or to eCW.

## How to use it

1. Export the account's transactions from the bank as **CSV or Excel**.
2. 💵 Posting → 🏦 Bank reconciliation → choose the file → **⬆ Upload
   statement**. Overlapping statements are fine: a line already on file is
   not added twice.
3. Run the check reconciliation first (▶ Run → Reconcile all checks) so the
   checks' eCW status is today's; the panel says "checks as of …".
4. Work the **need a person** tile. **Mark reviewed** records who looked at
   a line, when, and what they found. It does not change the line's status:
   the reconciliation never closes anything on its own.

The statement needs a header row with a **Date** column and either an
**Amount** column (credits positive, debits negative or in parentheses) or a
**Credit / Debit** pair. Description, check/reference number and type
columns are used when present. The uploaded file is kept in S3
(`bank/statements/`) as the evidence of what the bank said.

## What each bank credit becomes

| status | meaning |
|---|---|
| `matched · posted` | identified, and every item behind it is posted in eCW |
| `matched · not posted` | identified, eCW has not posted all of it — the variance is the dollars not posted |
| `unmatched` | money in the bank nothing accounts for |
| `ambiguous` | more than one equally good explanation — no guess is made |
| `reversed` | the bank took it back (returned item); its checks are free again |
| `other income` | a card settlement or other non-insurance credit — out of scope here |

And the other way — **not in bank**: a scanned deposit slip or a tracker
deposit date with no matching credit, once the statement covers four days
past it.

## The rules, strongest first

A bank line is used once, and so is a check.

1. **Deposit slip** — a scanned deposit's total equals a credit within 5 days.
2. **Tracker deposit date** — the checks the tracker dates one day add up to
   a credit within 4 days; when they went in as two deposits, the one set
   that fits each.
3. **Number on the bank line** — the check's serial or the EFT's trace
   number is printed on the line and the amount agrees.
4. **Amount and date** — one check, one credit, 6 days, no other candidate
   on either side; identical amounts are paired in date order only when
   there are as many lines as checks.
5. **Sum of checks** — the only set of unexplained checks (dated up to 12
   days before the credit) that adds up to it. Two sets → `ambiguous`.

Checks the payer made out to the patient are never Helixona's deposit (the
💌 Reminders bot chases those).

## Against the punch list's acceptance test

"Every test deposit accounted for once; one-to-many, split checks, reversed
payments and date differences handled without false matches" — each is a
case in `tests/test_bank_reconcile.py`, on fabricated data, and the summary
carries `accounted`: the buckets add up to the credits, or the panel says so.

## What this first release does not do

- It does not log in to the bank: a person uploads the export. Tell the
  developer the bank and the export's columns and the reader is tuned to it.
- Posted means eCW's payment for the check has nothing left unposted (the
  Checks table's verdict). Service-line detail of each posting is not read.
- Only the unposted 835s are known (Billing → ERA, UnPosted); an EFT whose
  835 is posted is matched through its eCW payment.
- Card and patient payments are labelled `other income` and left for B14.
- Windows and rules are constants at the top of `src/checks/bank.py`,
  pending the SOP's review.
