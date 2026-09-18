# Posting a Blue Shield payment into eCW

What the operator does, read off the two screen recordings frame by frame
("Bot EOBS part 1", 2026-09-07, 45 min; "Screen Recording 2026-09-09", 30 min,
a training call with Vignesh). Everything here is observed unless marked.

eCW runs in the browser (`…ecwcloud.com`), so the bot drives it the same way it
drives Blue Shield.

## The two writes

Nothing else in the procedure touches the server.

1. **Clicking `Payment Advisory`** at the bottom-left of the `Payments` popup
   **saves the payment** and creates its Payment ID. Before that click the popup
   header still reads `Payment ID: 0`; right after it, the Payments list holds a
   new row (`PAYMENT ID 5043 … AMOUNT 262.69, POSTED 0.00, UNPOSTED 262.69`).
   **A dry run stops here** — it may fill every field, and must not click this.
2. **`Auto Post (F2)`**, then `Yes` on the dialog, commits the typed lines. Until
   then the edits live in the browser: the window title carries a trailing `*`
   while there are unsaved changes, and the claim's `Payment(s)` panel still
   shows `Paid 0.00`.

`Post Payment (F4)` is never used, despite the name. `Add Claims (F2)` and
`Post CPT (F4)` in the advisory are never used either.

## Navigation

Billing → Payments. Filters: `Rcvd Pmt Dts` from **07/01/2025**, `Check #` =
the Blue Shield Check/EFT number, `Lookup`.

* Rows returned → a payment for this check already exists.
* **No rows** (an empty grid, no message) → `Single Ins Payment (F4)`, then in
  the `Single Insurance Payment` dialog type the **EOB's PATIENT ACCOUNT NUMBER**
  into `Claim No:` (2627 — our claim id, *not* the 12-digit Blue Shield claim
  number), `Get Insurance`, select the radio beside `Blue Shield of California`,
  `OK`.

## The `Payments` popup

| Field | Value | Source |
|---|---|---|
| `Facility` | left empty | |
| `Type` | `Check` | dropdown: Cash (default), Check, Credit Card, ACH, Non Payment Data |
| `Check No.` | `30979207` | portal `Check/EFT #` |
| `Batch No.` | left at `0` | |
| `Amount $` | `262.69` | the EOB's **APPROVE-TO-PAY** — *not* the portal's Check/EFT amount ($263.67). The difference is the EOB's interest ($0.98) |
| `Received Date` | left at today | |
| `Check Date` | `05/05/2026` | portal `Check/EFT date` |
| `EOB Date` | `05/05/2026` | **the check date again** — the operator does not use the EOB's own RECEIPT DATE (01/29/26). Confirm this is policy |
| `Deposit Date` | `05/14/2026` | portal `Check cashed date`. It auto-fills with today when ticked and must be corrected |
| `Notes` | left empty | |

Dates are `MM/DD/YYYY`; the amount is typed bare (`262.69`).

Then `Payment Advisory` — **the save** — which opens the advisory window with
the new Payment ID. Its `Claims Posted` grid stays empty at `of 0 claims`; the
operator types the claim id into `Claim ID` and clicks **`Go (F3)`**, which
opens `Payment Posting (Blue Shield of California)` directly.

An `Alert` — *"No Master/Default fee schedule is selected."* — appears when that
window opens. It is dismissed and has no effect (confirm).

## The `Payment Posting` grid

Columns: `Service Dt | POS | Units | Code | Billed | Allowed | Deduct | CoIns |
CoPay | Paid | Adjust | Withheld | Code`.

* **From the claim, never touched:** Service Dt, POS, Units, Code, Billed.
* **Typed:** `Allowed`, `CoPay`, `Paid` (and `Deduct` when the EOB has one; on
  both recordings the EOB's deductible is 0.00 throughout). `CoIns` and
  `Withheld` stay 0.00.
* **Auto-calculated:** `Adjust` = Billed − Allowed. Never typed.

Rows follow the claim's own CPT order, not the EOB's; 13 rows with only three
visible, scrolled with the grid's own scrollbar. Clicking a cell selects the row
and **the status bar at the bottom-left names the drug** — `J3490 : OON -
Ascorbic Acid (25,000mg)`. That is a second way to identify a line, independent
of the HCFA.

Live feedback while typing: the `Total Payment Posted for Claim` footer, the
`Net Balance`, and the `Write Off $X (F9)` button, which relabels itself to the
current balance.

`Assign Claim To` (BP/BS/BT/PT) is never set, and the resulting warning is
dismissed. `Financial Adjustments` is never used — the remaining balance is left
on the account.

## Amounts are copied whole

The EOB bills J3475 as **2 units at $40.00** (allowed 0.82, co-pay 0.33, paid
0.49); eCW has one **1-unit** row at $20.00. The operator types 0.82 / 0.33 /
0.49 unchanged. No per-unit arithmetic, no rounding — cents are transcribed
exactly. His own note says "Dont divide".

## Which eCW line gets which EOB line

By code; and when a code repeats, by billed amount. The billed amounts differ
between the two systems, so the tie-break runs through **Vignesh's table**
(e-mail of 2026-09-09, "Blue Shield Payment Posting - J3490 / J7999"): take the
eCW line's billed amount, find it under **Blue Shield Amount**, read **Medicare
Billed Amount** across from it, and post the EOB line billed that. eCW's $900.00
ascorbic acid is the EOB's $300.00 line. The code is part of the key —
Methylcobalamin is listed under both J3490 and J7999 with the same amounts.

Lines the operator could not match were left at 0.00.

## Warnings that mean a real mistake

`Payment Posting Warnings` — *"The sum of Deduct, Coins, CoPay, Paid,
Adjustment and WithHeld 111.61 is more than Billed Fee 100.00"* for CPT J0640,
answered `Yes`.

That warning is arithmetic, and it is worth predicting rather than meeting.
Since Adjust = Billed − Allowed, the sum exceeds the billed fee exactly when
**deductible + co-pay + paid exceeds the allowed amount** — which is what
happens when a value is copied onto the wrong line. On camera it was: J0640 got
J1955's paid amount (13.96 instead of 2.35), and it was corrected in the second
session by retyping the cell.

The other dialog, on `Auto Post`, is routine: *"The balance is 184.74 / Do you
want to continue without changing 'Assign claim to' anyway?"* → `Yes`.

## Duplicate payments

The claim's `Payment(s)` panel lists earlier payments (this claim already had
payment `2065` from 03/02/2026). The claim is flagged `(adjusted)` in the portal
and the EOB says *"THIS IS AN ADJUSTED PAYMENT FOR A PREVIOUSLY PROCESSED
CLAIM."* The operator's note requires checking for a duplicate payment on every
claim, and opening the claim history when the claim is an adjustment.

## Why the billed amounts disagree

The EOB bills this claim $1,113.00; eCW bills $2,697.50, and almost every line
differs (96365: 135.00 vs 325.00). Billing Logs answer it: **the charges were
amended after the claim was submitted**, on 01-27-2026 by another user. The
original charges in the log — J3490 $40.00, J3490 $300.00, 96365 $135.00 — are
exactly what the EOB shows.

## Left unresolved on camera

* The first session ended **unbalanced**: posted paid $235.41 against the EOB's
  $262.69, with unmatched rows at 0.00. The second session brought it to
  $261.08 — still $1.61 short of $262.69, which is exactly the difference
  between the EOB's unposted $4.80 line and the $3.19 line that appears to have
  been posted twice. Claim 2627 needs a line-by-line reconciliation by a person.
* The EOB's fourth J3490 ($20.00 billed, $4.80 paid) matches no eCW row and was
  never posted.
* Vignesh's table has no Glutathione row and no J3490 at $20.00 Medicare.
* A non-zero deductible, a denied line, and a check covering more than one
  claim never occur in either recording.
* What `Auto Post (F2)` does that `Post Payment (F4)` does not.
