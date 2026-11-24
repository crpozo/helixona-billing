# Reminders — Blue Shield checks mailed to the patient

The operator (2026-11-12): *"Blue Shield: read if a new check arrives for
the patient; send a reminder to the patient or they will be charged credit
card."* The sixth bot: tab **💌 Reminders · Checks sent to patients**, unit
`helixona-agent-reminders`, display :105, noVNC 6086, queue
`SQS_QUEUE_URL_REMINDERS`, Chrome profile `browser-profile-reminders`, task
`patient_check_reminders` (src/reminders).

**The bot never charges a card.** It finds the checks, writes to the
patient, and lists the ones past the deadline for a person to decide.
Read-only in the Blue Shield portal and in eCW.

## The flow

1. **Blue Shield** — Claims → Check claim status → each Check/EFT: payee,
   amount, status, cashed date (the Remittance bot's reader). With
   `blue_shield: true` the run walks the portal itself; otherwise it uses
   the checks already on file (`helixona-eobs`).
2. **Made out to the patient** — the EOB says paid to the member, or the
   payee is the patient on the check's claim. Checks made out to Helixona or
   to a clinician are left out.
3. **Not at Helixona** — no scan, not in the Insurance Check Tracker, no
   payment in eCW (`helixona-checks`, written by Remittance's check
   reconciliation). A check Helixona holds is `received` and gets nothing.
   Run the Remittance reconciliation first, so a check the patient already
   handed in is not chased.
4. **The patient's e-mail** — kept from an earlier run, else the host's
   contact file, else eCW: the claim popup, then the patient's information
   window. No address → the row says `none on file` (call the patient).
5. **The reminder** — first notice when the check is first seen, then one
   every `reminder_every_days`, at most `max_reminders`; the last is the
   final notice. The deadline is `deadline_days` after the first notice and
   does not move. A check Blue Shield shows as cashed gets the "pay the
   amount" wording instead of "bring the check".
6. **Past the deadline** → `deadline passed`, no more e-mails. A person
   decides whether the card on file is charged.

## First run on the host (2026-11-16)

What the first test taught:

- The summary held floats and DynamoDB refused it ("Float types are not
  supported") — amounts are strings to the cent now.
- The newest checks carry no eCW claim (the portal walk pins a check to a
  claim by subscriber + DOS, and many do not pin). They are now matched by
  the patient's name: the claim on the check's DOS, else the patient's most
  recent claim — any claim of the patient opens the same patient record.
- A check issued days ago is still in the envelope: `in the mail` until
  `mail_days` (7) after the check date, unless Blue Shield shows it cashed.
- Without a sender the run says so once: set `from` in
  `data/patient_check_reminder.json`.

## Where the e-mail is (2026-11-17)

The operator: "you go inside Hub and check the email". The claim popup's
**Hub** button opens the Patient Hub; its header carries the patient's
mobile number and e-mail, and its Billing box says **Card on File** — a
card, or an "Add Card" button when there is none. The bot reads all three
and closes the Hub; the Info form is only the fallback.

A patient with **no card on file** gets `no_card_sentence` ("billed to you
as a patient balance") in place of `card_sentence`: the message never
promises a charge nobody can make. Not read keeps the card sentence. The
tab shows *no card on file* on the row and as a tile.

## Stages

| stage | meaning |
|---|---|
| `in the mail` | issued less than `mail_days` ago — not chased yet |
| `to remind` | a reminder is due now; its draft is on the row |
| `reminded` | told, waiting for the patient |
| `deadline passed` | the deadline went by — a person decides on the card |
| `received` | the check is at Helixona |
| `voided` | Blue Shield voided or stopped the check |

## Nothing is e-mailed by default

| run card | what it does |
|---|---|
| 🧪 Test on 1 check | one check, e-mail looked up, draft written. Type your own address to get that one message as `[TEST]`; it counts nothing |
| 🔎 Find new checks and draft | walks the portal, drafts every reminder due. No e-mail |
| 💌 Send the reminders | e-mails the patients (asks for confirmation first) |

Task body: `since` · `blue_shield` · `limit_checks` · `check_eft` · `ecw`
(default true) · `send` (default false) · `test_to` · `limit` · `today`.

## Wording and numbers

`data/patient_check_reminder.json`: `mail_days` (7), `deadline_days` (14),
`reminder_every_days` (5), `max_reminders` (3), `clinic`, `phone`, `from`,
and the text (`subject`, `body`, `final_subject`, `cashed_subject`,
`cashed_body`). Placeholders: `{first_name}` `{clinic}` `{check_number}`
`{check_date}` `{amount}` `{dos_phrase}` `{deadline}` `{phone_phrase}`.

Before the first real send, the clinic should confirm: the card sentence
matches the financial policy the patient signed; the patient agreed to be
contacted by e-mail; the sender address is the one patients should reply to.

## Host, once

    sudo bash -c 'ROLE=reminders bash /opt/helixona-agent/setup_denials_host.sh 105 6086'

creates display :105, VNC 5906, noVNC 6086, the queue
`helixona-agent-tasks-reminders` and `SQS_QUEUE_URL_REMINDERS` in `.env`,
and restarts the bot and the dashboard. Open TCP 6086 on the security group
for the live screen.

E-mail goes through Amazon SES, like the unlocked-notes notices: the sender
(`from` in the policy file, else the roster's `from`, else `NOTIFY_FROM`)
must be verified in SES, the role needs `ses:SendEmail`, and the SES account
must be out of the sandbox to write to patients.

Optional contact file (host only, git-ignored — it is PHI),
`/opt/helixona-agent/data/patient_contacts.json`:

```json
{"claims": {"2187": {"email": "patient@example.com", "phone": "(415) 555-0100"}},
 "patients": {"DOE, JANE": {"email": "patient@example.com"}}}
```

## First run

💌 Reminders → ▶ Run → **Test on 1 check**. The log shows how many checks
are made out to patients, whether the e-mail was found in eCW
(`eob_post_reminders_claim_<n>.png` is the claim popup,
`eob_post_reminders_info_<n>.png` the patient's information window), and the
draft appears on the row (**View draft**). The button that opens the
patient's information (`PATIENT_INFO` in src/reminders/ecw.py) is the part
to tune against the real screen.
