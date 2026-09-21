# Reconciling checks: copies · Blue Shield · eCW

The operator's procedure (2026-09-16), as the Remittance bot runs it
(`check_reconcile`, src/checks/run.py). Read-only against all three systems.

1. **SharePoint** — `Shared Documents / Insurance Checks` on the
   BillingDepartment site holds an image of every check the clinic received:
   `Posted Checks / <year> / <month>` and `Unposted Checks / <date>`
   (`Insurance Check Tracker` is the team's spreadsheet and `Posted
   Checks/2025` is before the period; both are left out). Each new file is
   read **once** for its check number and amount (the PDF's text layer when
   it has one, else Claude looking at the image) and stored with the folder
   it was found in; later runs only read files that are new or changed.
2. **Blue Shield** — the check's `Check/EFT status`: `Check Cashed` or not.
   With `blue_shield:true` the run walks the portal itself (Claims → Check
   claim status → each Check/EFT, check data only); otherwise it takes the
   checks an earlier run with `blue_shield:true` stored (helixona-eobs).
3. **No copy** — every cashed check we hold no image of is flagged.
4. **eCW** — Billing → Payments since 07/01/2025, every check. A payment
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
| `not in eCW` | eCW has no payment under the number — a cashed Blue Shield check, or a scanned check from any payer. eCW is the source of truth: not in eCW means we do not have it |
| `not cashed` | a Blue Shield check the bank has not cashed — nothing to enter yet |
**Deposits.** A scan may be a bank deposit: page 1 the deposit slip (the checks listed by hand with the total) and one check per page after it. The reader classifies every page; a slip on page 1 makes the file a deposit, stored as a `deposit:<file>` row (total, date, the check numbers) with one row per check carrying `deposit_file`, `copy_page` and `copy_from_slip` (a line of the slip with no page of its own). Each check is compared with Blue Shield and looked up in eCW on its own; the team page shows the deposit as one accordion with its checks under it. PDFs read before deposits were known are counted once (a download, no model) and read again page by page only when they have three pages or more; the one-check row they had produced is removed.

The `since` here is the remittance window (payments in eCW, default 07/01/2025). The claims bot has its own, separate floor — `CLAIMS_SINCE` in `src/main.py`, 06/01/2025 — for how far back it looks in eCW's Claims lookup. They are not the same setting.

Check numbers are keyed bare (231282015) so Blue Shield, the copies and eCW meet on one row, but the number is also kept as printed on the check (`copy_check_raw`, 0231282015) and shown that way. eCW matches Check # exactly and the team types it as printed, so the lookup tries the printed form first, then one and two leading zeros, then the bare number.

| `eCW not checked` | eCW has never answered for this check; nothing can be said. An answer from an earlier run (posted, unposted, not in eCW) is kept when this run's lookup fails — a failed lookup is not a new answer |

Flags alongside: `no copy of the check`; `other payer` (Blue Shield does not
list it — informational); `amounts differ: copy 275.09, bs 257.09`.

Everything lands in the **✅ Checks** table on the Remittance tab (filters
per verdict, CSV export) and in the `helixona-checks` table, one item per
check number.

## SharePoint access

Two ways in. The folder is shared inside Helixona only.

### A. Through the bot's browser (no admin needed — the default)

The bot's Chrome keeps a persistent profile, so a Helixona account signed
in once stays signed in. On the first run the log says "SharePoint wants a
sign-in — finish it on the live screen": open the Remittance bot's noVNC
window, sign in with the Helixona account, answer Microsoft's MFA, and tick
**Stay signed in**. The run then reads the folder through SharePoint's REST
API with that session (src/checks/sharepoint_browser.py) and continues.
Later runs need nothing.

The folder comes from its sharing link (the one in the code, or
`share_link` in the secret or the task body). Optional secret, so the bot
types the e-mail/password itself and only the MFA is left to the person:

```json
{"share_link": "https://helixona.sharepoint.com/:f:/s/BillingDepartment/...",
 "username": "someone@helixona.com", "password": "..."}
```

### B. App registration (Microsoft Graph, app-only)

A user login would need Microsoft's MFA on every run; an app registration
needs none — but it takes an Entra admin.

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

With `"source": "s3"` the run reads `s3://<claims bucket>/checks/inbox/`
instead — check images dropped there by hand.

## The image reader

The images are read by Claude through the Anthropic API directly (not
Bedrock). The key comes from `ANTHROPIC_API_KEY` in the bot's environment
or, failing that, the Secrets Manager secret
`helixona-prod-anthropic-api-key` (provisioned with the account in
us-east-1 — the bot looks in its own region, then there; the bare
`sk-ant-...` string, or JSON with `api_key` / `ANTHROPIC_API_KEY`; it is
encrypted with the KMS key `helixona-prod-phi-data`, whose key policy must
let `helixona-agent-role` decrypt), or
`prod/helixona/anthropic_credentials` `{"api_key": "sk-ant-..."}`.

`VISION_MODEL_ID` (environment) names the model; the default is
`claude-sonnet-5` (about $0.01 an image; each file is read once and kept). A run without a key logs "no Anthropic API key". Files that
could not be read are tried again on every run and shown on the tab as
"files not read". PDFs with a text layer are read without the model.

## Task body

```json
{"since": "07/01/2025", "blue_shield": false, "limit_checks": 0, "check_eft": "",
 "copies": true, "limit_files": 0, "ecw": true}
```

`blue_shield:true` walks the portal first (`limit_checks` caps the checks,
`check_eft` names one; `force` defaults to true on such a targeted run so
the check is re-opened for its status today). `copies:false` skips the
images (reconcile what is already read); `limit_files` caps the images read.
`ecw:false` reuses the last Payments list read.

## The test of 1

The dashboard's **🧪 Test of 1** task is the same `check_reconcile` with

```json
{"since": "07/01/2025", "blue_shield": true, "limit_checks": 1, "check_eft": "",
 "copies": true, "limit_files": 10, "ecw": true}
```

One check, end to end, on the live screen:

1. Blue Shield: the first check in the results (or `check_eft`) is opened —
   amount, status, cashed date.
2. The copies: files named after that check are read first, then up to
   `limit_files` more.
3. eCW: Billing → Payments, Rcvd Pmt Dts from `since`, **Check # = the
   check**, Lookup — the operator's own step, one check at a time (a full
   run lists every payment once instead).
4. Its row alone is written; the tab opens on the **🧪 Last run** filter and
   the steps show above the tiles as they happen (`_run` item). The tiles
   keep counting the whole table.
