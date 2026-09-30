# Denials: eCW's denied claims, the reason, the SOP's answer

The fourth bot (2026-09-30). The operator: "revisar en eCW los claims que
están denied y revisa la razón, y las posibles soluciones. En el SOP
encuentras los códigos y las razones."

## What a run does (`claim_denials`, src/denials/run.py)

1. **eCW › Billing › Claims** — Service Dt from `since` (07/01/2025), Claim
   Status set to each denied status the dropdown offers (`DENIED_STATUSES`:
   ERA Payer Denied, EOB Payer Denied, Waiting for Denial, Requires further
   review, Insurance Rejected, …; the options are logged, and `statuses:
   [...]` in the task names others). Lookup, and the grid's rows are read off
   their Angular scope: claim, patient, payer, DOS, CPT/HCPCS, charges, paid,
   balance, status.
   The screen shows 20 a page (709 ERA PAYER DENIED claims on 2026-09-30,
   36 pages): No. of Result is set to its largest option and every page is
   read. The grid has no CPT column.
2. **The reason** — each claim is opened once (its popup): the **ICD & CPT**
   tab for the codes billed, then **Insurances & Payment**, whose Payments /
   Adjustments / Refunds grid carries the payer's reason in its Code column
   (CARC group codes CO-16, PR-96; remark codes N846; letter codes B16;
   words like Duplicate). When that grid shows no code, **View CPT Pmts**
   is opened and read. The popup is closed with Cancel. A claim whose
   codes were read before keeps them (`redo: true` reads again). The first
   claim of a run is photographed (`/tmp/eob_post_denial_claim_<id>.png`).
3. **The SOP** — `data/denial_cheatsheet.json`, the workbook's "Denial
   Cheatsheet" and "Appeals" sheets (`scripts/import_denial_sop.py
   <SOP.xlsx>` refreshes it; the workbook itself holds logins and is never
   committed). A rule is matched on payer (Medicare, Anthem Blue Cross, Any
   Payer), procedure code (J3490 / J7999) and reason (CO-16 / 96, N846,
   Duplicate…). The best-fitting rules give the action and its kind: write
   off · inquiry · appeal · medical records · recode. Several equally good
   rules are all listed (the four Medicare CO-16 rows differ only by drug,
   which the grid does not show); when they agree on the kind of action it
   stands, when they disagree — or no rule fits — the claim is "review": a
   person decides.

Everything lands in **helixona-denials**, one item per claim (`denial_codes`,
`denial_text`, `sop_rows`, `sop_matches`, `action_kind`, `action`), with
`_run` the live trace and `_summary` the counts. Read-only in eCW.

## The Denials tab

Tiles by action kind (write off, inquiry, appeal, medical records, recode,
needs a person, no code read), the table with the claim, patient, payer,
codes billed, charges and balance, eCW status, the denial codes and the SOP
action, and a CSV (`/api/denials.csv`). Run cards: **Review denied claims in
eCW** (everything), **Test on 5 claims** (with the popup screenshot), **List
only** (no claim opened).

## The bot on the host

Like the other three: its own unit, display, noVNC port, queue and Chrome
profile.

    role      unit                     display  noVNC  queue env var           profile
    denials   helixona-agent-denials   :103     6084   SQS_QUEUE_URL_DENIALS   browser-profile-denials

One-time setup on the host, once (the deploy installs and enables the unit,
which idles until the queue is configured). After the first deploy that
carries the bot:

    sudo bash /opt/helixona-agent/setup_denials_host.sh

The script creates display :103 (`xvfb@103`), `x11vnc` on 5904 and noVNC on
6084 (units `x11vnc-103`, `novnc-6084`, same `.vncpass` as the other bots),
creates the SQS queue `helixona-agent-tasks-denials` and writes its URL to
`/opt/helixona-agent/.env` as `SQS_QUEUE_URL_DENIALS`, then restarts the bot
and the dashboard. It is idempotent. Two steps stay in the AWS console: open
inbound TCP 6084 on the instance's security group (the browser reaches noVNC
directly, as it does 6083), and create the queue by hand if the host has no
permission to. Without the display and the port, the Live screen for the
Denials tab stays blank — the dashboard already points it at 6084. Then the
first eCW login of the new profile on the live screen, if the captcha flow
asks for it.
