# Priority — which open claims to work first

The operator's "Claim Prioritization Bot – Flow for Review" (2026-10-26), as
the fifth bot: tab **🎯 Priority · Claim prioritization**, unit
`helixona-agent-priority`, display :104, noVNC 6085, queue
`SQS_QUEUE_URL_PRIORITY`, Chrome profile `browser-profile-priority`.

## The flow

1. **Billing → Encounters** — Progress Notes Done/Locked, last 120 days.
   Encounters without a claim → **Claims IPE All** creates them. This is the
   one step that writes to eCW; it runs only with `create_claims: true`
   (the run card *Prioritize all open claims*). *Test on 1 claim* and
   *Prioritize without creating claims* skip it.
2. **Billing → Claims** — Service Dt from 07/01/2025 · Claim Status **All
   Claims** · Balance **> 0**; every page is read.
3. **Each claim opened once.** An ERA payer (Medicare, Anthem — `ERA_PAYER_RX`
   in `src/priority/ecw.py`) is dated by the latest "Claim Status" row of
   its **Billing Logs**; every other payer (BSC, UHC, Cigna…) by the latest
   note in its **Follow Up Logs**. No date in either → the latest date on
   the claim, else the DOS, and the row says so. A claim dated on an earlier
   run keeps its date unless `redo` is set.
4. **Days = today − reference date.** Priority by aging: 91+ → **P1** ·
   61–90 → **P2** · 31–60 → **P3** · 0–30 → **P4**. Days over the SLA for
   the claim's status → **overdue** (`data/claim_sla.json`, per status,
   `default` 30; edit the numbers there).
5. **Report** — `helixona-priority`, one item per claim; the tab sorts
   overdue first, then P1 → P4, then the oldest; tiles per band with the
   balance in each; CSV at `/api/priority.csv`.

Read-only in eCW apart from step 1: the claim popup is opened, its log view
read and closed, and the claim closed with Cancel.

## Host, once

    sudo bash -c 'ROLE=priority bash /opt/helixona-agent/setup_denials_host.sh 104 6085'

creates display :104, VNC 5905, noVNC 6085, the queue
`helixona-agent-tasks-priority` and `SQS_QUEUE_URL_PRIORITY` in `.env`, and
restarts the bot and the dashboard. Open TCP 6085 on the instance's security
group for the live screen.

## First run

🎯 Priority → ▶ Run → **Test on 1 claim**. The log shows the Balance filter,
the claim count, and for the first claim which log view opened and the date
it took (`priority_claim_<n>.png` is the popup). The log tab names
(`BILLING_LOG_TABS`, `FOLLOWUP_LOG_TABS`) and the date rule are the parts to
tune against the real screen.
