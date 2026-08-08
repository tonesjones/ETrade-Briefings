# E*TRADE → Daily Portfolio Action Briefing

Pulls live E*TRADE positions (all active accounts), builds a **fully processed** briefing prompt with current weights/values, **copies it to your clipboard**, and saves a dated file. You paste into **grok.com** using your normal Grok/SuperGrok plan.

No xAI API key or API credits required.

---

## Daily workflow (option 1 — recommended)

**Best path:** build a **fully processed** briefing prompt (your hedge-fund instructions + live weights already filled in):

```powershell
cd C:\TestCode\Etrade-Grok
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

### 3. One-time OAuth

```powershell
python etrade_auth.py
```

Authorize in the browser, paste the verification code, then put the printed access tokens into `.env`.

### 4. Accounts

By default the script pulls **all ACTIVE** accounts.  
Optional: pin one account with `ETRADE_ACCOUNT_ID_KEY=...` in `.env`.

---

## Files

| File | Role |
|------|------|
| `build_briefing_prompt.py` | **Daily command** — live portfolio + full processed prompt → clipboard |
| `get_portfolio.py` | Portfolio block only (fetch, format, clipboard, save) |
| `etrade_auth.py` | One-time OAuth (tokens only) |
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
| `get_account_portfolio` | Any auto-submit of buys/sells |

- Grok recommendations in the briefing prompt are **text only**. Nothing in this repo sends those actions to E\*TRADE.
- You would have to place trades yourself in the E\*TRADE UI (or deliberately add order code later).
- OAuth tokens can technically authorize trading APIs if misused, but **this codebase never calls order endpoints**.

---

## Tips

- Re-run OAuth if E*TRADE starts rejecting tokens.
- Production: `ETRADE_DEV=false` with LIVE consumer key/secret in `.env`.
- Daily habit: `python build_briefing_prompt.py` → grok.com → Ctrl+V.
- Optional SNPS mark: `SNPS_MARKET_VALUE=60000` in `.env`.

---

## Security

- **`.env` is gitignored** (and `.env.*` except `.env.example`) — safe to push the repo to GitHub without uploading keys
- Before first push, confirm: `git check-ignore -v .env` should print a match
- Never hard-code API keys
- `briefings/` and `prompts/` are gitignored (position data is sensitive)
- Commit `.env.example` only (placeholders), not real values
