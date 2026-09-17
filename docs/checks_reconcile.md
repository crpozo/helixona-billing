# Reconciling cheques: copies · Blue Shield · eCW

The operator's procedure (2026-09-16), as the Remittance bot runs it
(`check_reconcile`, src/checks/run.py). Read-only against all three systems.

1. **SharePoint** — `Shared Documents / Insurance Checks` on the
   BillingDepartment site holds an image of every cheque the clinic received.
   Each new file is read for its cheque number and amount (the PDF's text
   layer when it has one, else Claude on Bedrock looking at the image).
2. **Blue Shield** — the cheque's `Check/EFT status`: `Check Cashed` or not.
   With `blue_shield:true` the run walks the portal itself (Claims → Check
   claim status → each Check/EFT, cheque data only); otherwise it takes the
   cheques an earlier `eob_capture` stored (helixona-eobs).
3. **No copy** — every cashed cheque we hold no image of is flagged.
4. **eCW** — Billing → Payments since 07/01/2025, every cheque. A payment
   under the number means it was entered: `posted` when nothing is left
   unposted, `unposted` when a balance remains (the payment was created and
   the lines never finished; see docs/ecw_posting.md).

Friday: Vignesh's team finishes entering the payments. The run after that is
the one whose "not in eCW" list is real.

## The verdicts

| verdict | meaning |
|---|---|
| `posted` | in eCW, unposted balance 0.00 |
| `unposted` | in eCW, a balance still unposted |
| `not in eCW` | Blue Shield shows it cashed, eCW has no payment for it |
| `not cashed` | Blue Shield has not cashed it — nothing to enter yet |
| `copy only` | we hold an image of a cheque Blue Shield's results do not list |

Flags alongside: `no copy of the cheque`; `amounts differ: copy 275.09, bs 257.09`.

Everything lands in the **✅ Cheques** table on the Remittance tab (filters
per verdict, CSV export) and in the `helixona-checks` table, one item per
cheque number.

## SharePoint access

Microsoft Graph, app-only. A user login would need Microsoft's MFA on
every run; an app registration needs none.

1. Entra admin center → App registrations → New registration
   ("Helixona Remittance bot"). Note the **Application (client) ID** and
   **Directory (tenant) ID**.
2. Certificates & secrets → New client secret. Note the **value** (shown once).
3. API permissions → Add → Microsoft Graph → **Application** permissions →
   `Sites.Read.All` → Grant admin consent.
4. Secrets Manager, secret `prod/helixona/sharepoint_credentials`:

```json
{"tenant_id": "...", "client_id": "...", "client_secret": "...",
 "site_hostname": "helixona.sharepoint.com", "site_path": "/sites/BillingDepartment",
 "folder": "Insurance Checks"}
```

Until that secret exists the run reads `s3://<claims bucket>/checks/inbox/`
instead — drop the cheque images there by hand to try the flow.

## The image reader

`BEDROCK_VISION_MODEL_ID` (environment) names the Claude model on Bedrock
that reads the images; the default is the model the agent already uses.
If the first run logs "could not be read by the model", the account does
not have that model enabled in us-west-2 — enable one with vision in the
Bedrock console and set the variable in the bot's unit file.

## Task body

```json
{"since": "07/01/2025", "blue_shield": false, "limit_checks": 0, "check_eft": "",
 "copies": true, "limit_files": 0, "ecw": true}
```

`blue_shield:true` walks the portal first (`limit_checks` caps the cheques,
`check_eft` names one; `force` defaults to true on such a targeted run so
the cheque is re-opened for its status today). `copies:false` skips the
images (reconcile what is already read); `limit_files` caps the images read.
`ecw:false` reuses the last Payments list read.

## The test of 1

The dashboard's **🧪 Test of 1** task is the same `check_reconcile` with

```json
{"since": "07/01/2025", "blue_shield": true, "limit_checks": 1, "check_eft": "",
 "copies": true, "limit_files": 10, "ecw": true}
```

One cheque, end to end, on the live screen:

1. Blue Shield: the first cheque in the results (or `check_eft`) is opened —
   amount, status, cashed date.
2. The copies: files named after that cheque are read first, then up to
   `limit_files` more.
3. eCW: Billing → Payments, Rcvd Pmt Dts from `since`, **Check # = the
   cheque**, Lookup — the operator's own step, one cheque at a time (a full
   run lists every payment once instead).
4. Its row alone is written; the tab opens on the **🧪 Last run** filter and
   the steps show above the tiles as they happen (`_run` item). The tiles
   keep counting the whole table.
