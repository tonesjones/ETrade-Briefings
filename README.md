# E*TRADE → Daily Portfolio Action Briefing

Pulls live E*TRADE positions (all active accounts), builds a **decision-support** briefing prompt (executive recommendations first, tax/lot hierarchy, factor sleeves), **copies it to your clipboard**, and saves a dated file. Paste it into your preferred capable model, such as ChatGPT, Grok, or Claude.

Each holding includes E\*TRADE **price paid**, **cost per share**, **total cost**, **unrealized P/L**, and **date acquired**. Taxable lots are labeled **ST** (held ≤ 1 year) or **LT** (held > 1 year). IRA/Roth show economic P/L but are **not** treated as capital-gains events. The prompt also pre-computes **direct AI/semi** vs **broad AI-cycle** weights so concentration is not just the six chip tickers.

No xAI API key or API credits required.

---

## Daily workflow (recommended)

E\*TRADE access tokens **expire every day at midnight US Eastern**. You must re-authorize in the browser **once per calendar day** (Eastern) before pulling data. Same-day re-runs of the portfolio script do **not** need another browser login.

### 1. Re-auth (required after midnight ET, or if API rejects tokens)

```powershell
cd path\to\etrade-briefings
python etrade_auth.py
```

1. Open the printed URL in your browser, log in, and authorize the app
2. Paste the verification code into the terminal
3. The script atomically updates the two daily token entries and `ETRADE_AUTH_DATE` in .env (token secrets are not printed by default)

### 2. Build the briefing prompt

```powershell
python build_briefing_prompt.py
```

To build offline from the portable portfolio that `get_portfolio.py` copied to
the clipboard, run the shortcut:

```powershell
.\briefing.cmd
```

The shortcut runs `build_briefing_prompt.py --from-clipboard` with the project
virtual environment. Use the Python command directly when you need to call the
builder from another script.

The offline path validates a versioned JSON payload embedded after the readable
portfolio dump. It does not authenticate or contact E*TRADE. Legacy text-only
dumps fail closed because they do not preserve exact account and lot identity.

If another command overwrote the clipboard, build from the saved portfolio file
instead:

```powershell
python build_briefing_prompt.py --input-file .\briefings\portfolio_YYYY-MM-DD.txt
```

Replace `YYYY-MM-DD` with the date in the saved filename. This is a recovery
path, not the normal daily command.

Then:

1. Open your preferred model
2. **Ctrl+V** — the full prompt is already on the clipboard
3. Send

**Portfolio dump only** (local diagnostic, or if you keep a fixed system prompt in your model):

```powershell
python get_portfolio.py
```

The daily briefing prompt does **not** reprint that dump. Lots appear once, in a single taxable + IRA + Roth table.

### Outputs each run

| Command | Output |
|---------|--------|
| `build_briefing_prompt.py` | Compact decision-support prompt → clipboard + dated prompt + `weights_*.json` portfolio deltas + `observations_*.json` account/quantity/lot reconciliation |
| `briefing.cmd` | Offline compact decision-support prompt from the structured clipboard payload → clipboard + dated prompt + snapshots |
| `get_portfolio.py` | Full per-account dump (cost / P/L / ST-LT) → clipboard + `briefings/portfolio_YYYY-MM-DD.txt` — local diagnostic, not pasted into the model |

### What the briefing asks the model to analyze

The prompt is a **decision-support engine**, not a holdings dump. It is written for a model with web search (for example Codex). The model has no trading authority and its output is an analytical proposal, not an approved or submitted trade. It is asked to:

- **Research first**: search for dated news on each focus name since the last snapshot, check the market backdrop, and scan the monitor-only names for material events. "No material news" counts as evidence.
- Work from **one focus list**: each name carries its reason (limit breach, taxable loss review, weight or position change, or size and loss on a decision day). Other sized holdings are monitor-only.
- Give each focus name a **Fundamental View** and, separately, one **Portfolio Action** (KEEP / REDUCE / REPLACE / DEPLOY / NO ACTION). NO ACTION is reserved for a named missing fact that would change the decision.
- Treat a soft concentration breach as **review + no-add**, not an automatic sale, and start any limit-driven cut from the pre-computed **smallest dollar cut**
- Name the account and **exact taxable lot** for any sale (lots for focus names are included) and respect **wash-sale** rules on taxable losses
- Cite every company-specific fact with its **source and date**, and replace vague horizons with observable triggers
- Reconcile prior proposals when a decision history is supplied

Blocks with nothing to say (no research notes, no decision history, the $10k example on routine days) are left out of the prompt.

It injects two independent deltas:

- A **portfolio delta** vs the prior pull (weight moves ≥ 0.5 pp, factor sleeves, 15% breaches)
- An **observed account delta** using per-account quantities and lot identity (position appeared/disappeared, quantity increased/decreased, lot identity changed, or no execution detected)

Observed E*TRADE changes establish that account values changed, but without transaction or order evidence they do not prove a buy or sale; transfers, splits, and other corporate actions can also change positions. They do not establish motive or whether a prior model proposal was approved. The clipboard workflow also cannot capture the model response automatically, so prior model proposals remain `NOT CAPTURED` unless a future response-import/API layer supplies them.

---

## Token lifetime (why re-auth is daily)

| Situation | What you do |
|-----------|-------------|
| **First use, or any time after midnight US Eastern** | Run python etrade_auth.py, authorize, and paste the code; tokens are saved automatically |
| **Same day, tokens still working** | Just run `build_briefing_prompt.py` / `get_portfolio.py` — no browser |
| **API rejects tokens** (expired or invalid) | The scripts stop with a "run etrade_auth.py" message — run it again |

This is an E\*TRADE platform rule, not something this repo can skip. Consumer key/secret stay in `.env` permanently; only the **access** token pair must be refreshed after daily expiry.

---

## One-time setup

### 1. Install

```powershell
cd path\to\etrade-briefings
pip install -r requirements.txt
```

### 2. Configure secrets

```powershell
copy .env.example .env
```

Edit `.env` with your E*TRADE app keys:

```env
ETRADE_CONSUMER_KEY=...
ETRADE_CONSUMER_SECRET=...
ETRADE_DEV=true          # sandbox; use false for production
```

### 3. First OAuth (then again after each midnight ET)

```powershell
python etrade_auth.py
```

Authorize in the browser and paste the verification code. The script saves the daily tokens to .env without printing them.
See [Daily workflow](#daily-workflow-recommended) — you will repeat this step **once per day** after tokens expire.

### 4. Accounts

By default the script pulls **all ACTIVE** accounts.  
Optional: pin one account with `ETRADE_ACCOUNT_ID_KEY=...` in `.env`.

---

## Files

| File | Role |
|------|------|
| `build_briefing_prompt.py` | **Daily command** — live portfolio + decision-engine prompt → clipboard |
| `briefing.cmd` | One-command offline briefing shortcut using the structured clipboard payload |
| `get_portfolio.py` | Portfolio block only, including cost / P/L / ST-LT |
| etrade_auth.py | OAuth plus atomic .env token update — once per day after midnight ET |
| `templates/briefing_prompt.md` | Wording of the briefing prompt (`{placeholders}` are filled by `build_briefing_prompt.py`) |
| `briefing_snapshots.py` | Daily weight / observation snapshots and the two deltas |
| `briefing_formatting.py` | Shared money / weight formatting |
| `portfolio_policy.json` | Decision limits, sleeves, harvest floors, $10k examples |
| `portfolio_policy.py` | Loads and validates that JSON (`--policy path.json` on either script uses another file) |
| `docs/history-retention.md` | Notes on how much briefing history to keep |
| `portfolio_context.example.json` | Template for local owner constraints, dated research, and prior analytical proposals |
| `.env` | Secrets (gitignored) |
| `.env.example` | Template for secrets |
| `briefings/` | Dated portfolio blocks + weight and observation snapshots (gitignored) |
| `prompts/` | Dated full briefing prompts (gitignored) |

---

## Editing the decision policy

All briefing math and most instruction text read `portfolio_policy.json`. Edit that file — not the Python — when a cap, sleeve, or analysis floor changes. Then run:

    python -m unittest discover -s tests -v

To change the wording of the prompt itself, edit `templates/briefing_prompt.md`; placeholders in `{braces}` are filled with computed values.

| Key | What it does |
|-----|----------------|
| `single_name_cap_pct` | Soft max weight for one liquid name. Breaches get a minimum-dollar cut. |
| `cluster_do_not_increase_pct` | Direct AI/semi: do not add at or above this weight. |
| `cluster_soft_cap_pct` | Direct AI/semi soft ceiling. Holding above it is allowed only when cutting would realize punitive ST tax. |
| `analyze_weight_floor_pct` | Names at or above this weight must appear in the position table. |
| `analyze_market_value_floor` | Dollar floor before harvest / material-loss names are forced into the table. |
| `material_loss_dollars` / `material_loss_pct` | Sized names past either loss threshold stay in the table even under the weight floor. |
| `harvest_loss_dollars` | Taxable unrealized-loss flag (negative number). |
| `risk_off_glide_pct` | Direct-stack weight to work *toward* in Risk-Off, via tax-aware lots. |
| `st_gain_flag_dollars` / `lt_gain_flag_dollars` | Taxable ST / LT unrealized-gain flags in the live tax block. |
| `cash_symbols` | Tickers treated as cash (plus any symbol containing `GOVERNMENT`). |
| `sleeves` | Factor membership. Direct AI/semi is a subset of broad AI-cycle. Changing membership changes both the math and the prompt labels. |
| `aliases` | Optional display names (`CRWV` → CoreWeave). |
| `marginal_examples` | Rows in the pre-computed $10k table. `symbol` may be `null` for a generic diversifier. |

`schema_version` must stay `1`. Missing required sleeves abort load.

## Research continuity

Copy `portfolio_context.example.json` to `portfolio_context.json` and fill it in locally. The daily prompt reads this optional file and keeps it out of Git. It has three deliberately small sections:

- `owner_profile`: horizon, cash reserve, withdrawal, tax, and hard-limit facts that the broker cannot supply.
- `research`: one dated record per held name or recurring candidate, including the valuation or entry condition, decision trigger, metrics, and primary sources.
- `decision_history`: prior analytical proposals and their conditions. Record approval or confirmed execution only when you have that evidence; a model recommendation itself is not approval.

All three sections are optional. A blank owner profile is reported as "not supplied", and the model flags a missing field only where it would change a trade. Research notes give the model a starting point; without them it researches each focus name itself. Keep notes concise and update them after earnings or a material event.

---

## Read-only / no trading without consent

This project is intentionally **read-only** against E\*TRADE:

| Allowed | Not present in this repo |
|---------|---------------------------|
| OAuth login (`etrade_auth.py`) | `ETradeOrder` (place / preview / cancel orders) |
| `list_accounts` | Market order helpers used to trade |
| `get_account_portfolio` (including cost basis / lots) | Any auto-submit of buys/sells |

- Model recommendations from the briefing prompt are **text only**. Nothing in this repo sends those actions to E\*TRADE.
- You would have to place trades yourself in the E\*TRADE UI (or deliberately add order code later).
- OAuth tokens can technically authorize trading APIs if misused, but **this codebase never calls order endpoints**.

---

## Tips

- **Daily live habit:** after midnight ET, run `etrade_auth.py`, run `build_briefing_prompt.py`, open your preferred model, and press Ctrl+V.
- **Daily offline habit:** run `get_portfolio.py`, run `briefing.cmd`, open your preferred model, and press Ctrl+V.
- Cost basis: both scripts pull E\*TRADE average cost, total cost, unrealized P/L, and **tax lots** (so mixed ST/LT names are split correctly). That adds a few extra read-only lot calls and a few seconds.
- Daily delta needs yesterday’s `briefings/weights_*.json` (or a prior dated prompt). Same-day re-runs compare to the last *previous calendar day*, not the earlier run today.
- Same calendar day (US Eastern): re-run the portfolio script without re-auth unless the API rejects tokens.
- Production: `ETRADE_DEV=false` with LIVE consumer key/secret in `.env`.

---

## Data completeness and safety

The scripts fetch every page of portfolio positions and the read-only E*TRADE account balance. The account value—including residual cash—is the weight denominator. Each account must reconcile: positions plus cash must match the reported account value (within five basis points for mark timing), and each holding's tax lots must add up to its share quantity. Response-shape errors, missing or mismatched tax lots, an unreconciled account, or a failed selected account stop prompt generation rather than silently shrinking the portfolio.

Only network errors, throttling (429), and server errors (5xx) are retried. An expired token (401), or an `ETRADE_AUTH_DATE` from an earlier Eastern day, stops immediately with a re-authorize message.

For diagnostics only, use the --allow-partial option with either portfolio command to produce a prominently marked incomplete result. Do not use partial output for trade sizing.

Decision rules live in portfolio_policy.json, including concentration limits, factor sleeves, analysis thresholds, and aliases.

Run the offline regression suite with:

    python -m unittest discover -s tests -v

OAuth tokens are saved automatically. Use --no-write-env together with --print-tokens only when manual token handling is explicitly needed.

## Security

- **`.env` is gitignored** (and `.env.*` except `.env.example`) — safe to push the repo to GitHub without uploading keys
- Before first push, confirm: `git check-ignore -v .env` should print a match
- Never hard-code API keys
- `briefings/` and `prompts/` are gitignored (position data is sensitive)
- Commit `.env.example` only (placeholders), not real values
- Keep the GitHub remote **private**. Never `git add -f` `.env`, `briefings/`, or `prompts/`
- Local artifacts are the real book: dollars, cost, lots, and account last-4s. Do not zip `briefings/` or `prompts/` into email, a public gist, or a cloud chat
- The model you paste into sees the full prompt — that is the workflow, not a leak in this repo
- Lint: `pip install ruff` then `ruff check .` (CI runs lint and tests on every push)
