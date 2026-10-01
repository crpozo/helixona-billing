# Claude credits — the super admin's meter

**Why.** The bots pay the Anthropic API for every check image Claude reads.
The organization's balance lives in the Anthropic console; reading it from
code needs an Admin API key the clinic does not have. So the dashboard keeps
its own meter: each response's `usage` (tokens) is priced at the list rate of
the model that answered and added to the table `helixona-usage`.

**Who sees it.** Only the super admin, carlos@mindfultech.ec. The dashboard
has no sign-in, so the proof is a key:

1. On the host, the dashboard log prints the link at every start:
   `journalctl -u helixona-dashboard | grep 'Super admin'`. The key is
   `DASHBOARD_ADMIN_KEY` in `/opt/helixona-agent/.env` or, when that is
   unset, a key generated once into `/opt/helixona-agent/data/.admin_key`
   (`sudo cat` it). Add the generated one to `.env` so a redeploy of `.env`
   does not make a new key.
2. Open `http://54.189.175.233:5050/admin?key=…` once in the browser. That
   sets a signed cookie for a year; `/admin/logout` removes it.
3. The top bar then shows **💳 Claude credits**. Click it for the panel:
   remaining, days left at the last 14 days' pace, spent today / this month
   / all time, by model, and the last 14 days.

Everyone else sees nothing: `/api/usage` answers 403 without the cookie,
and the pill stays hidden.

**The balance.** The meter cannot see the console, so the admin enters the
balance the console shows today. Remaining = that balance − what the meter
adds from then on. Enter it again after a top-up.

**Prices** (2026-09 list, $ per million tokens — input / output / cache
write / cache read): Sonnet 5 and 5.5 2 / 10 / 2.5 / 0.20 · Opus 5.5
4 / 20 / 5 / 0.20 · Opus 5 and 4.x 5 / 25 / 6.25 / 0.50 · Fable 5.1
10 / 50 / 12.5 / 0.25 · Haiku 4.5 1 / 5 / 1.25 / 0.10. A model not in the
list is priced as Sonnet 5. The reader asks for Sonnet 5 and lets the API
fall back when a call is declined, so the model that answered is what gets
priced.

**Table.** `helixona-usage`, key `k`: `_total`, `day:YYYY-MM-DD` (calls,
tokens, `cost_usd`, `cost_<model>`), `_credits` (balance, `spent_before`,
`as_of`, `set_by`). Created on first use by the bot; the dashboard reads
only. A tally that cannot be written is logged and skipped — it never fails
the read it was counting.
