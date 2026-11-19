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

## Not built yet, and what each needs

- **UHC** — portal credentials, a sample PRA, and who answers the
  verification call every 30 days.
- **Other payers' EOBs** (Cigna, UMR) read from the scan — sample PDFs.
- **$0 EOBs** — how the denial is recorded in Payment Advisory, which date
  goes in the four date boxes; the portal search must list them
  (`include_zero: true` is there to try).
- **Moving the file** Unposted → Posted checks, and **tracker column G** —
  permission to write in SharePoint and in Karina's sheet.
- **A check for several claims** — "Add insurance payment".
- **Posting every ready check in one run** — after the supervised pilot.

## To confirm before the first real post

1. The commit button: Auto Post (F2) (the September recordings) or "Post
   payment, then OK" (the November flow)?
2. Amount: approve-to-pay (without Blue Shield's interest) or the EOB total?
3. What to do when Unposted is not 0; can a payment be deleted, by whom?
4. Who approves the bot's postings, and the eCW user it posts with.
5. A patient with more than one insurance: which is selected?
