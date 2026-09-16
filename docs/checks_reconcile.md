# Reconciling cheques: copies · Blue Shield · eCW

The operator's procedure (2026-09-16), as the Remittance bot runs it
(`check_reconcile`, src/checks/run.py). Read-only against all three systems.

1. **SharePoint** — `Shared Documents / Insurance Checks` on the
   BillingDepartment site holds an image of every cheque the clinic received.
   Each new file is read for its cheque number and amount (the PDF's text
   layer when it has one, else Claude on Bedrock looking at the image).
2. **Blue Shield** — the cheque's `Check/EFT status`: `Check Cashed` or not.
   Taken from the cheques Remittance's capture already stored
   (helixona-eobs); this run does not open the portal, so run
   `eob_capture` first.
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
{"since": "07/01/2025", "copies": true, "ecw": true, "limit_files": 0}
```

`copies:false` skips the images (reconcile what is already read);
`ecw:false` reuses the last Payments list read; `limit_files` caps the
images read in a test run.
