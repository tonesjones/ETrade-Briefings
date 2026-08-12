# E*TRADE → Daily Portfolio Action Briefing

Pulls live E*TRADE positions (all active accounts), builds a **decision-engine** briefing prompt (executive trades first, tax/lot hierarchy, factor sleeves), **copies it to your clipboard**, and saves a dated file. You paste into **grok.com** using your normal Grok/SuperGrok plan.

Each holding includes E\*TRADE **price paid**, **cost per share**, **total cost**, **unrealized P/L**, and **date acquired**. Taxable lots are labeled **ST** (held ≤ 1 year) or **LT** (held > 1 year). IRA/Roth show economic P/L but are **not** treated as capital-gains events. The prompt also pre-computes **direct AI/semi** vs **broad AI-cycle** weights so concentration is not just the six chip tickers.

No xAI API key or API credits required.

---

## Daily workflow (recommended)

E\*TRADE access tokens **expire every day at midnight US Eastern**. You must re-authorize in the browser **once per calendar day** (Eastern) before pulling data. Same-day re-runs of the portfolio script do **not** need another browser login.

### 1. Re-auth (required after midnight ET, or if API rejects tokens)

```powershell
cd C:\TestCode\Etrade-Grok
python etrade_auth.py
```

1. Open the printed URL in your browser, log in, and authorize the app
2. Paste the verification code into the terminal
3. The script atomically updates the two daily token entries in .env (token secrets are not printed by default)

### 2. Build the briefing prompt

```powershell
python build_briefing_prompt.py
```

Then:

1. Open [grok.com](https://grok.com)
2. **Ctrl+V** — the full prompt is already on the clipboard
3. Send

**Portfolio block only** (if you keep a fixed system prompt on grok.com):

```powershell
python get_portfolio.py
```

That block is also tax-aware: per-account cost, unrealized P/L, date acquired, and **ST / LT** (mixed lots split). IRA/Roth show economic P/L with no capital-gains label.

### Outputs each run

| Command | Output |
|---------|--------|
| `build_briefing_prompt.py` | Full prompt → clipboard + `prompts/daily_briefing_prompt_YYYY-MM-DD.md` + `briefings/weights_YYYY-MM-DD.json` (for the next day’s delta) |
| `get_portfolio.py` | Portfolio block (with cost / P/L / ST-LT) → clipboard + `briefings/portfolio_YYYY-MM-DD.txt` |

### What the briefing asks Grok to decide

The prompt is a **decision engine**, not a holdings dump. Grok is required to:

- Lead with **Trade / No trade** and a **confidence** (no-trade can be high conviction)
- Prefer the **smallest dollar cut** that actually fixes a limit (e.g. restore 15%, not “sell 15% of the name”)
- Show the **$10k marginal** effect of any buy on direct AI/semi, broad AI-cycle, cash, and top-5
- On Trim/Sell, **compare at least two account/lot choices** — no fixed IRA-first or taxable-first waterfall
- Treat **Replace** as sell + a named replacement (cash is Reduce / Deploy, not Replace)
- Respect **wash-sale** on taxable losses
- Include a **horizon** on every action and a directional **stress** (Nasdaq −10%, semi −15%, etc.) — no fake VaR

It also injects a **daily delta** vs the prior pull (weight moves ≥ 0.5 pp, factor sleeves, 15% breaches). The first run after a gap has no snapshot and says so.

---

## Token lifetime (why re-auth is daily)

| Situation | What you do |
|-----------|-------------|
| **First use, or any time after midnight US Eastern** | Run python etrade_auth.py, authorize, and paste the code; tokens are saved automatically |
| **Same day, tokens still working** | Just run `build_briefing_prompt.py` / `get_portfolio.py` — no browser |
| **API rejects tokens** (expired or invalid) | Run python etrade_auth.py again |

This is an E\*TRADE platform rule, not something this repo can skip. Consumer key/secret stay in `.env` permanently; only the **access** token pair must be refreshed after daily expiry.

---

## One-time setup

### 1. Install

```powershell
cd C:\TestCode\Etrade-Grok
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
| `get_portfolio.py` | Portfolio block only, including cost / P/L / ST-LT |
| etrade_auth.py | OAuth plus atomic .env token update — once per day after midnight ET |
| `.env` | Secrets (gitignored) |
| `.env.example` | Template for secrets |
| `briefings/` | Dated portfolio blocks + `weights_YYYY-MM-DD.json` snapshots (gitignored) |
| `prompts/` | Dated full briefing prompts (gitignored) |

---

## Read-only / no trading without consent

This project is intentionally **read-only** against E\*TRADE:

| Allowed | Not present in this repo |
|---------|---------------------------|
| OAuth login (`etrade_auth.py`) | `ETradeOrder` (place / preview / cancel orders) |
| `list_accounts` | Market order helpers used to trade |
| `get_account_portfolio` (including cost basis / lots) | Any auto-submit of buys/sells |

- Grok recommendations in the briefing prompt are **text only**. Nothing in this repo sends those actions to E\*TRADE.
- You would have to place trades yourself in the E\*TRADE UI (or deliberately add order code later).
- OAuth tokens can technically authorize trading APIs if misused, but **this codebase never calls order endpoints**.

---

## Tips

- **Daily habit:** after midnight ET → run etrade_auth.py → run build_briefing_prompt.py → open grok.com → Ctrl+V.
- Cost basis: both scripts pull E\*TRADE average cost, total cost, unrealized P/L, and **tax lots** (so mixed ST/LT names are split correctly). That adds a few extra read-only lot calls and a few seconds.
- Daily delta needs yesterday’s `briefings/weights_*.json` (or a prior dated prompt). Same-day re-runs compare to the last *previous calendar day*, not the earlier run today.
- Same calendar day (US Eastern): re-run the portfolio script without re-auth unless the API rejects tokens.
- Production: `ETRADE_DEV=false` with LIVE consumer key/secret in `.env`.

---

## Data completeness and safety

The scripts fetch both portfolio positions and the read-only E*TRADE account balance. The account value—including residual cash—is the weight denominator. Response-shape errors, missing required tax lots, or a failed selected account stop prompt generation rather than silently shrinking the portfolio.

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
