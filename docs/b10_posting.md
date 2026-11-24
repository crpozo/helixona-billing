# B10 — from the payer's EOB to the payment in eCW

The B10 flows (2026-11): *B10a Payer EOB Download and EOB-Ready Check*,
*B10b Manual Posting in eCW*, *B10 Remittance to Bank to eCW Reconciliation
v2*.

**Its own bot** (the operator, 2026-11-19: Remittance is the SharePoint
checks flow; B10 is a new bot): tab **💵 Posting · EOB to eCW (B10)**, unit
`helixona-agent-posting`, display :106, noVNC 6087, queue
`SQS_QUEUE_URL_POSTING`, Chrome profile `browser-profile-posting`, tasks
`b10_daily` and `eob_post` — fenced to this bot, and this bot runs nothing
else. It reuses Remittance's readers (no second reconciliation code);
Remittance itself is unchanged and still only reads.

## Host, once

    sudo bash -c 'ROLE=posting bash /opt/helixona-agent/setup_denials_host.sh 106 6087'

creates display :106, VNC 5907, noVNC 6087, the queue
`helixona-agent-tasks-posting` and `SQS_QUEUE_URL_POSTING` in `.env`. Open
TCP 6087 on the security group. On the first daily run the bot's own Chrome
asks for the **SharePoint sign-in once** on its live screen (the scans and
Karina's tracker are read through it) — sign in and tick *Stay signed in*.

## The daily run — ▶ Run → Daily run

`b10_daily`: Remittance's reading with the EOB reports kept
(`blue_shield: true, download_eob: true`). Reads only.

1. **Blue Shield** — Claims → Check claim status. Every new check is opened
   and its **EOB report saved** (S3, under the check number). A check
   already on file that is deposited and has no EOB file gets it now
   (`eob_for`). Checks not cashed when last read are asked again.
2. **The scans** in SharePoint and **Karina's Insurance Check Tracker**.
3. **eCW** — Billing → Payments, each check by number.
4. The list **📥 Payments to enter in eCW** is recomputed.

## The rule — when a payment is ready (src/eob/ready.py)

An EOB only says the payer issued a check. The payment is entered when the
money is in the bank: **the check number is in the tracker**.

| stage | meaning |
|---|---|
| `ready` | in the tracker (deposited) and the EOB is saved |
| `EOB missing` | deposited, no EOB file — the next daily run downloads it |
| `waiting for deposit` | the EOB is here, the check is not in the tracker yet |
| `$0 EOB` | no check exists (deductible, denial) — listed, held (see open points) |
| `held` | the posting plan is not complete: a line not matched, several claims on one check |
| `needs a person` | eCW has the payment with a balance unposted, or the bot could not finish one |
| `other payer` | Cigna, UMR…: the EOB is in the scan; posted by hand for now |
| `posted` | eCW has the payment, nothing unposted |

## Entering a payment — `eob_post` (src/eob/post.py)

**A dry run unless the task says `"post": true`.** One check at a time.

1. Billing → Payments · Check # lookup. A payment already there → nothing.
2. Single Ins Payment → Claim No = the EOB's **patient account number** →
   Get Insurance → Blue Shield of California → OK.
3. The Payments popup (src/eob/ecw_payment.payment_header):

| field | value | from |
|---|---|---|
| Facility | the one office | selected |
| Type | Check | |
| Check No. | the check number | Blue Shield's Check/EFT # |
| Amount $ | the EOB's approve-to-pay | EOB |
| Received Date | Date Check Received | tracker |
| Check Date · EOB Date | the check's date | Blue Shield |
| Deposit Date | Deposit Date, else the cashed date | tracker, else Blue Shield |

   **── a dry run stops here: screenshot, Cancel, nothing saved ──**
4. **Payment Advisory** — this click saves the payment.
5. **Scan → Add → the EOB file → OK**: the EOB attached to the payment.
6. Claim ID → Go (F3) → the Payment Posting grid. For each CPT line of the
   plan (src/eob/plan.py — by code, and by billed amount through Vignesh's
   table when a code repeats): **Allowed · Deduct · CoPay · Paid**. Adjust
   is eCW's own arithmetic. A line that would overflow its billed fee stops
   the check before eCW is opened.
7. Commit (`commit_buttons`, Auto Post (F2) → Yes).
8. **The quality check**: the payment is read back by Check #. `posted`
   only when **Unposted is 0**; otherwise `unbalanced` → needs a person.

A payment created whose lines could not be finished is reported as
`incomplete` with its Payment ID — never hidden.

`data/ecw_posting_policy.json` holds every choice above (amount, dates,
facility, attaching, the commit button, multi-claim checks, $0 EOBs).

## The bank — 🏦 Bank reconciliation (same tab)

docs/bank_reconcile.md: the statement uploaded by a person, each credit
matched to its checks and to what eCW has posted.

## Run cards

| card | writes? |
|---|---|
| 📥 Daily run | no |
| 🧪 Dry run: 1 payment in eCW | no — fills the popup and cancels |
| 💵 Post 1 payment in eCW | **yes** — asks first; one check |

## What the calls with Vignesh settled (2026-11)

From the recordings of B10a (EOB downloads), B10b (single claim), B10c
(several claims on one check) and B10d (adjusted payment), and the weekly
B10 meeting. Each line is in the code or in `data/ecw_posting_policy.json`.

- **The claim number** is the EOB's *patient account number* — four digits
  today, five later; never the long number printed under it. On a UHC PRA
  it is the *Patient Control Number*.
- **One insurance on the patient** is selected by default after Get
  Insurance: OK. No radio to pick is not a failure.
- **Facility**: Select → the drop-down → the one office.
- **Check date = EOB date**, always: the EOB is issued with the check.
- **Received and Deposit dates** come from Karina's tracker. A check that is
  not in the tracker is not cashed yet — the bot waits; it never assumes
  today.
- **Paid** is typed per CPT line, never the statement total. **Adjust** is
  eCW's arithmetic. **Coinsurance** has its own column (UHC: PR2 =
  coinsurance, PR3 = copay; Blue Shield's EOB has none).
- **The commit**: "Post payment", then OK (B10b). The bot presses the first
  of `commit_buttons` it finds and logs every button the screen offered.
- **The quality check**: Unposted (the balance on Payment Advisory) is 0.
- **The claim's status after posting** — always one of three:
  any denied line → **ERA Payer Denied**; no denial but a deductible,
  coinsurance or copay → **Patient**; nothing owed → **Paid in Full**.
  The bot works it out (`claim_status_for`) and sets it before the commit;
  when the control is not found the result says "set the status by hand".
- **A denied line**: Allowed 0, Paid 0, and the EOB's reason code (PI 16 in
  the demo; "hundreds of codes, assign the right one") in the line's Code
  box. The Blue Shield EOB reader does not read reason codes yet, so a
  claim with a denied line is **held**, with the status it will need.
- **A $0 EOB**: Type *Non Payment Data*, the EOB number as the check
  number, one date in all four boxes, the EOB attached and the denial
  recorded in Payment Advisory. Coded (`post_zero_eobs`), off until the
  portal walk lists them.
- **Several claims on one check** (B10c): *Add Ins Payment (F3)*, the payer
  chosen in the insurance lookup (any entry — only the payer ID matters),
  each claim typed and committed with *Save and Next*, the EOB uploaded
  once, OK at the end. Coded (`multi_claim_checks`), off until tried.
- **An adjusted payment** (B10d): a corrected claim paid again under a new
  check number. Its EOB lists every line as paid again, so after posting
  Unposted is negative; a reversal line (REV, "Insurance payment reversal")
  is added on the claim for that amount and posted. The bot **recognises**
  it — a second check for a claim an earlier check paid — and **holds** it.
- **Anthem and Medicare** pay by ACH with an ERA in eCW (Billing → ERA →
  E-post): not part of this bot. **B05** is not needed, per Vignesh.

## Not built yet, and what each needs

- **UHC** — portal credentials, a sample PRA, and who answers the
  verification call every 30 days. Its rules are known (above).
- **Other payers' EOBs** (Cigna, UMR) read from the scan — sample PDFs.
  "Each insurance issues the EOBs in their own way."
- **Reason codes on denied lines** — read off the EOB, then typed in the
  Code box. Until then such claims are held.
- **The reversal for adjusted payments** (B10d) — the field that takes the
  negative amount was not clear in the recording.
- **Moving the file** Unposted → Posted checks, and **tracker column G** —
  permission to write in SharePoint and in Karina's sheet.
- **Posting every ready check in one run** — after the supervised pilot.

## To confirm before the first real post

1. Amount when Blue Shield adds interest: approve-to-pay or the check total?
2. What to do when Unposted is not 0; can a payment be deleted, by whom?
3. Who approves the bot's postings, and the eCW user it posts with.
4. Is the claim status set before "Post payment" or by going back into the
   claim afterwards (the B10c demo went back in)?
5. Received date when the tracker has none: today (B10c) or wait?
