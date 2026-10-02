# Unlocked notes — the lock rule, the notices, the weekly summary

**Why (2026-10-02).** Tom traced the notes that reached Blue Shield without
a signature: every one was a progress note nobody had locked in eCW. A
locked note carries its electronic signature ("Electronically signed by …");
an unlocked one does not. So the signature is the lock, and the Intake bot
now treats it as one.

## 1. No note leaves unless it is locked

When the extractor captures a claim's IV Note it reads the note's text and
records on the claim: `iv_note_locked` (true/false), `iv_note_signed_by`,
`note_owner` (who should lock it: the signer, else the note's own provider /
administered-by line, else the claim's rendering provider) and
`iv_note_lock_checked_at`.

The submission gate (`src/rules/submission_gate.py`) holds a claim whose
note is not locked ("IV Note not locked in eCW"). Notes captured before
this rule existed have no lock status yet; they are held too ("IV Note lock
not verified") and read again on the next **Get documentation from eCW**
run, which then records the status. The dashboard shows 🔒 / 🔓 beside each
IV Note, and the blocker in the claim's row.

## 2. Three business days: a notice to the person and their manager

The task `unlocked_notes` (no browser) lists every unlocked note on a claim
still to send, counts business days (Mon–Fri) since the date of service,
and for each note at 3 or more sends one e-mail to the responsible person
with their manager in copy: chart number, DOS, appointment type, patient,
claim, days. Each note is told once (`unlock_notice_sent_at` on the claim;
`remind: true` tells again). When nobody in the roster matches the name,
the manager alone is written to, so nothing is silent.

Runs every weekday at 9:00 Pacific from the host timer
`helixona-unlocked-notes.timer`, and from the Intake run card **Notify
about unlocked notes**.

## 3. Weekly summary

With `weekly: true` the same task also sends the billing team one table of
every outstanding unlocked note — eCW chart number, DOS, appointment type,
patient, claim, responsible person, business days — plus a count per
person. Mondays at 8:00 Pacific (`helixona-unlocked-notes-weekly.timer`),
and from the run card **Weekly unlocked-notes summary**. **Unlocked notes —
dry run** composes everything into the log and sends nothing.

## Setup on the host (once)

1. **The roster**: copy `data/note_lock_roster.example.json` to
   `/opt/helixona-agent/data/note_lock_roster.json` (git-ignored) and fill
   in `from`, `weekly_to`, `default_manager` and each clinician's `email`
   and `manager`. Names as eCW shows them; the surname must match, and the
   first initial when both sides have one.
2. **Amazon SES** in us-west-2: verify the `from` address (and, while the
   account is in the SES sandbox, every recipient — or request production
   access). Give the instance role `helixona-agent-role` the permission
   `ses:SendEmail` / `ses:SendRawEmail`. Until then every notice is logged
   as *owed* and nothing is sent.
3. Deploy; the timers are installed and enabled by `deploy_code.sh`. Run the
   dry run from the dashboard and read the log before the first real send.

## Records

- On each claim: `iv_note_locked`, `iv_note_lock_status`,
  `iv_note_signed_by`, `note_owner`, `iv_note_lock_checked_at`,
  `unlock_notice_sent_at`, `unlock_notice_to`, `unlock_notice_count`.
- Table `helixona-notices` (key `k`): `notice:<claim>:<ts>` for every
  e-mail, `weekly:<date>` for each summary, `_last` for the last run
  (counts, and the claims whose notice is still owed).

## Known gaps

- The eCW chart number (patient account number) is not yet on the claim
  record, so the column shows "—" until the extractor captures it; the eCW
  claim number is always there.
- The appointment type is eCW's visit type when on file; otherwise Office
  Visit / IV Therapy from the CPT.
