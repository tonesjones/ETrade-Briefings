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
3. Copy the two printed lines into `.env` (replace the previous `ETRADE_ACCESS_TOKEN` / `ETRADE_ACCESS_TOKEN_SECRET`)

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

### Outputs each run

| Command | Output |
|---------|--------|
| `build_briefing_prompt.py` | Full prompt → clipboard + `prompts/daily_briefing_prompt_YYYY-MM-DD.md` |
| `get_portfolio.py` | Portfolio block → clipboard + `briefings/portfolio_YYYY-MM-DD.txt` |

---

## Token lifetime (why re-auth is daily)

| Situation | What you do |
|-----------|-------------|
| **First use, or any time after midnight US Eastern** | Full OAuth: `python etrade_auth.py` → browser → paste code → update `.env` |
| **Same day, tokens still working** | Just run `build_briefing_prompt.py` / `get_portfolio.py` — no browser |
| **API rejects tokens** (expired or invalid) | Run `etrade_auth.py` again and update `.env` |

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

Authorize in the browser, paste the verification code, then put the printed access tokens into `.env`.  
See [Daily workflow](#daily-workflow-recommended) — you will repeat this step **once per day** after tokens expire.

### 4. Accounts

By default the script pulls **all ACTIVE** accounts.  
Optional: pin one account with `ETRADE_ACCOUNT_ID_KEY=...` in `.env`.

---

## Files

| File | Role |
|------|------|
| `build_briefing_prompt.py` | **Daily command** — live portfolio + full processed prompt → clipboard |
| `get_portfolio.py` | Portfolio block only (fetch, format, clipboard, save) |
| `etrade_auth.py` | OAuth (browser + code) — **once per day after midnight ET** |
| `.env` | Secrets (gitignored) |
| `.env.example` | Template for secrets |
| `briefings/` | Dated portfolio blocks (gitignored) |
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

- **Daily habit:** after midnight ET → `python etrade_auth.py` (browser + code → update `.env`) → `python build_briefing_prompt.py` → grok.com → Ctrl+V.
- Cost basis: the script pulls E\*TRADE average cost, total cost, unrealized P/L, and **tax lots** (so mixed ST/LT names are split correctly). That adds a few extra read-only lot calls and a few seconds.
- Same calendar day (US Eastern): re-run the portfolio script without re-auth unless the API rejects tokens.
- Production: `ETRADE_DEV=false` with LIVE consumer key/secret in `.env`.
- Optional SNPS mark: `SNPS_MARKET_VALUE=60000` in `.env`. Optional tradable sleeve: `SNPS_TRADABLE_VALUE=...` and `SNPS_RESTRICTIONS=blackout` — otherwise the prompt treats SNPS tradability as unknown.

---

## Security

- **`.env` is gitignored** (and `.env.*` except `.env.example`) — safe to push the repo to GitHub without uploading keys
- Before first push, confirm: `git check-ignore -v .env` should print a match
- Never hard-code API keys
- `briefings/` and `prompts/` are gitignored (position data is sensitive)
- Commit `.env.example` only (placeholders), not real values
