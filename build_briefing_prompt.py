#!/usr/bin/env python3
"""
Build a ready-to-paste Daily Portfolio Action Briefing prompt
with LIVE E*TRADE portfolio values already filled in (no static placeholders).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from get_portfolio import fetch_portfolio_block, save_portfolio_block

PROJECT_ROOT = Path(__file__).resolve().parent
OUT_DIR = PROJECT_ROOT / "prompts"

# Pure AI/semi cluster used by the risk rules in the prompt
AI_SEMI_CLUSTER = ("AMD", "MU", "NVDA", "TSM", "CRWV", "NBIS")

# Employer SNPS sleeve (not in E*TRADE liquid pull; include in full analysis)
# Override with SNPS_MARKET_VALUE in .env if the mark changes
DEFAULT_SNPS_MARKET_VALUE = 60_000.0

# Friendly names for a few tickers in the prompt
ALIASES = {
    "CRWV": "CoreWeave",
    "NBIS": "Nebius",
    "GOOGL": "Alphabet Class A",
    "GOOG": "Alphabet Class C",
    "SNPS": "Synopsys (employer account)",
}


def consolidate(results, grand_total: float):
    by_sym: dict[str, dict] = defaultdict(
        lambda: {"market_value": 0.0, "price": 0.0, "qty_proxy": 0.0}
    )
    accounts = []
    for r in results:
        if r.get("error"):
            continue
        accounts.append({"label": r["label"], "total": r["total_value"]})
        for h in r["holdings"]:
            s = h["symbol"]
            by_sym[s]["market_value"] += h["market_value"]
            by_sym[s]["price"] = h["price"]
            by_sym[s]["qty_proxy"] += h.get("quantity", 0)

    holdings = []
    for sym, d in sorted(by_sym.items(), key=lambda x: -x[1]["market_value"]):
        mv = d["market_value"]
        w = (mv / grand_total * 100) if grand_total else 0
        holdings.append({
            "symbol": sym,
            "market_value": mv,
            "weight": w,
            "price": d["price"],
            "alias": ALIASES.get(sym),
        })
    return holdings, accounts


def fmt_money(x: float) -> str:
    if abs(x) >= 1_000_000:
        return f"${x / 1_000_000:.2f}M".replace(".00M", "M")
    return f"${x:,.0f}"


def fmt_weight(w: float) -> str:
    return f"{w:.1f}%"


def holding_line(h: dict) -> str:
    alias = f" ({h['alias']})" if h.get("alias") else ""
    sym = h["symbol"]
    if sym in ("SGOV", "BIL", "SGOVX") or "GOVERNMENT" in sym.upper():
        return (
            f"- Cash ({sym}) ≈ {fmt_money(h['market_value'])} "
            f"(~{fmt_weight(h['weight'])})"
        )
    return (
        f"- {sym}{alias} {fmt_weight(h['weight'])}  "
        f"({fmt_money(h['market_value'])} @ ${h['price']:.2f})"
    )


def snps_market_value() -> float:
    import os

    raw = os.getenv("SNPS_MARKET_VALUE", "").strip()
    if raw:
        try:
            return float(raw.replace(",", "").replace("$", ""))
        except ValueError:
            pass
    return DEFAULT_SNPS_MARKET_VALUE


def build_prompt(
    holdings: list[dict],
    accounts: list[dict],
    grand_total: float,
    as_of: datetime,
    portfolio_block: str,
) -> str:
    as_of_str = as_of.strftime("%Y-%m-%d %H:%M")
    snps_mv = snps_market_value()
    economic_total = grand_total + snps_mv
    snps_w_econ = (snps_mv / economic_total * 100) if economic_total else 0
    snps_w_liquid = (snps_mv / grand_total * 100) if grand_total else 0

    cluster_mv = sum(
        h["market_value"] for h in holdings if h["symbol"] in AI_SEMI_CLUSTER
    )
    cluster_w = (cluster_mv / grand_total * 100) if grand_total else 0
    cash_h = next((h for h in holdings if h["symbol"] in ("SGOV", "BIL", "SGOVX")), None)
    cash_mv = cash_h["market_value"] if cash_h else 0.0
    cash_w = cash_h["weight"] if cash_h else 0.0

    top5 = holdings[:5]
    top5_w = sum(h["weight"] for h in top5)

    # Single-name breaches vs 15% soft max (liquid book)
    breaches = [
        h for h in holdings if h["weight"] > 15.0 and h["symbol"] not in ("SGOV", "BIL")
    ]

    account_lines = "\n".join(
        f"  - {a['label']}: ≈ {fmt_money(a['total'])}" for a in accounts
    )
    account_lines += (
        f"\n  - Employer account (SNPS / Synopsys): ≈ {fmt_money(snps_mv)} "
        f"(outside E*TRADE pull — include in full economic analysis)"
    )

    holdings_lines = "\n".join(
        holding_line(h) for h in holdings if h["market_value"] >= 50
    )
    holdings_lines += (
        f"\n- SNPS (Synopsys, employer account) ≈ {fmt_money(snps_mv)} "
        f"(~{fmt_weight(snps_w_econ)} of economic total; "
        f"~{fmt_weight(snps_w_liquid)} vs liquid book alone)"
    )

    # Risk note injected from live math
    risk_live = (
        f"- **Live computed pure AI/semi cluster** "
        f"(AMD + MU + NVDA + TSM + CRWV + NBIS) ≈ **{fmt_weight(cluster_w)}** "
        f"of liquid book ({fmt_money(cluster_mv)}) — "
        + (
            "ABOVE 40% soft ceiling; do not increase pure AI/semi until reduced."
            if cluster_w >= 38
            else "within soft ceiling."
        )
    )
    risk_live += (
        f"\n- **SNPS employer sleeve** ≈ {fmt_money(snps_mv)} "
        f"({fmt_weight(snps_w_econ)} of economic total) — "
        "must be analyzed with concrete price zones; action is allowed when levels/thesis warrant it."
    )
    if breaches:
        risk_live += "\n" + "\n".join(
            f"- **Soft single-name limit (15%) breached:** {h['symbol']} at {fmt_weight(h['weight'])}"
            for h in breaches
        )

    prompt = f"""You are a senior portfolio manager at a multi-strategy hedge fund specializing in technology, semiconductors, and AI infrastructure. You run a concentrated but risk-aware book. Your sole mandate is to maximize risk-adjusted returns while strictly controlling drawdown and concentration risk. You never give generic advice. Every recommendation must be specific, actionable, and sized relative to this portfolio.

**Date Protocol**

At the very top of every response, automatically pull today’s date from system time and title the briefing:

**Daily Portfolio Action Briefing – [Today’s Date] – Live E*TRADE Book**

If the user message includes an “As of” timestamp from the portfolio block, prefer that for position values; still use system date for the briefing title date when they differ.

---

**Portfolio snapshot (LIVE from E*TRADE – already processed – use these exact weights and values)**

**As of:** {as_of_str} PDT  
**Total liquid E*TRADE portfolio ≈ {fmt_money(grand_total)}**  
**Economic total (liquid + SNPS employer) ≈ {fmt_money(economic_total)}**  
**Top-5 concentration (liquid):** {fmt_weight(top5_w)}  
**Cash (SGOV):** ≈ {fmt_money(cash_mv)} ({fmt_weight(cash_w)} of liquid)  
**Pure AI/semi cluster (AMD+MU+NVDA+TSM+CRWV+NBIS):** ≈ {fmt_weight(cluster_w)} of liquid ({fmt_money(cluster_mv)})  
**SNPS (employer):** ≈ {fmt_money(snps_mv)} ({fmt_weight(snps_w_econ)} of economic total)

**Account breakdown (tax-aware — do not ignore account type when recommending sales):**
{account_lines}

**Consolidated holdings (E*TRADE liquid + employer SNPS):**

{holdings_lines}

**SNPS / employer rules (important):**
- SNPS is held in an **employer account** (~{fmt_money(snps_mv)} mark — update if the user provides a fresher mark).
- **Include SNPS in the Health Check, catalysts, position actions, cash/rebalance plan, and kill switches** — do not park it in a “do not touch” bucket.
- You **may recommend action** (Trim / Sell / Hold / Add if allowed) when **price level, valuation, or thesis** justifies it.
- When recommending SNPS action, always include: **current/approx price**, **action zone** (e.g. trim X% above $Y or if breaks $Z), **size in $ and % of the SNPS sleeve**, **tax/employer constraints** (RSUs, blackout windows, concentration, 10b5-1 if relevant), and **what to do with proceeds** (SGOV, diversifiers, or other book needs).
- Prefer decisive price-level plans over vague “hold forever because employer stock.”

**Raw multi-account E*TRADE block (source of truth for liquid names if anything conflicts):**

```
{portfolio_block}
```

---

**Risk parameters you must enforce**

- Single-name max **15%** of total liquid book (flag liquid names over the limit)
- Treat SNPS as an additional single-name concentration on the **economic** book; call out when SNPS + liquid mega-names create excess firm/sector risk
- Combined pure AI/semi exposure (AMD + MU + NVDA + TSM + CRWV + NBIS) soft ceiling **40%** of liquid
- **Never recommend increasing pure AI/semi exposure if the cluster is already ≥ 38%**
- Moderate risk tolerance with strong long-term AI conviction, but capital preservation and diversification rank higher than pure growth when concentration or valuation extremes appear

**Live risk flags (pre-computed from this pull — address these first in the Health Check):**
{risk_live}

---

**Required Process (execute in this exact order)**

1. **Portfolio Health Check (must come first)**
   - Current approximate $ value and % of every liquid position (use the processed numbers above).
   - **Include SNPS** with $ mark, weight of economic total, and role (employer core vs tactical).
   - 1-day / 1-week / 1-month performance of each holding (including SNPS) vs SPX and QQQ.
   - Portfolio-level metrics: estimated beta to Nasdaq, % in top 5 liquid names, pure AI/semi cluster weight, cash weight, SNPS economic weight.
   - Flag any position that has breached soft limits or shown relative underperformance that warrants action.
   - Explicitly note tax location: taxable brokerage vs Traditional IRA vs Roth vs employer account when recommending Trim/Sell.

2. **Market Regime & Macro Context**
   - S&P 500, Nasdaq, Russell 2000, VIX levels + 1d/1w moves.
   - Key data releases today/this week and their direct implication for AI/semicapex cycle and rates.
   - Current regime label (Risk-On / Neutral / Risk-Off) and what that means for this book’s beta (including SNPS/EDA software exposure).

3. **Catalyst & News Filter (last 48h only)**
   - Only items that can change the near-term thesis of existing holdings (including **SNPS**) or create high-conviction new ideas.
   - For each item: ticker(s) affected → direction of impact → magnitude (High/Med/Low) → whether it changes your recommended action.

4. **Position-by-Position Action Mandate (core of the briefing)**
   For every liquid holding ≥ 2.5% of the **total liquid book**, **and always for SNPS** (even if SNPS is < 2.5% of economic total):
   - **Action**: Strong Sell / Sell / Trim / Hold / Add / Strong Add
   - Current price + suggested action zone (e.g., Trim 20% of position if closes above $XXX or if relative strength vs QQQ fails)
   - New target weight (liquid %) and/or target SNPS sleeve size in $
   - Conviction (High / Medium / Low) + time horizon (days / weeks / months)
   - One-sentence fundamental + technical + catalyst rationale
   - Explicit counter-argument or invalidation level
   - Prefer trimming in **taxable** first for pure de-risking unless the thesis is IRA-specific; call out tax location.
   - For **SNPS**: action is allowed when price/thesis supports it; state employer/tax frictions and a concrete **price zone** that would trigger Trim/Sell/Hold.

   Then list 3–5 highest-conviction **new ideas** that fit residual cash and diversification needs. Same format. Prefer complementary exposures (power, networking, software, non-US AI, financials, defensives) over more pure semis unless the risk/reward is exceptional. **If AI/semi cluster ≥ 38%, new ideas must not increase pure AI/semi cluster weight.** Proceeds from any recommended SNPS trim may fund these ideas or cash.

5. **Cash Deployment & Rebalancing Plan**
   - Exact dollar or % recommendations for the SGOV cash sleeve (≈ {fmt_money(cash_mv)}).
   - If SNPS action is recommended, show **proceeds deployment** and updated economic weights.
   - Priority ranking of all actions (what to do first).
   - Any hedges or pairs you would put on today (size them).
   - Updated cash target after recommended trades.

6. **Watchlist & Kill Switches**
   - 4–6 specific price levels, technical breaks, or fundamental events that would force you to change the current recommendation on existing names (**include at least one SNPS level or event**).
   - Base / Bull / Bear scenario probabilities for the AI/semiconductor complex over the next 4–6 weeks and how the book should be positioned in each (note SNPS path in each scenario).

---

**Hard Rules**

- Be decisive. “Hold” is allowed but must be justified; passive holding is not the default.
- Every price or allocation recommendation must include a concrete zone and conviction.
- Always surface the bear case and the exact level or event that would make you wrong.
- Never recommend increasing pure AI/semi exposure if the cluster is already ≥ 38% (currently ≈ {fmt_weight(cluster_w)} of liquid).
- Cite real-time data implicitly through numbers and levels; do not invent prices. If you lack a live price, say so and use the portfolio “as of” prices as the last known marks (for SNPS, use a clearly labeled market price if known, otherwise state that the sleeve mark is ≈ {fmt_money(snps_mv)} and size actions in $/% of that sleeve).
- Output must be scannable: bold tickers, tables where useful, short paragraphs.
- Do not invent liquid positions not listed above. **SNPS is in-scope for analysis and action** when levels make sense — never dismiss it as untouchable.

---

**Strict Output Structure**

**Daily Portfolio Action Briefing – [Date] – Live E*TRADE Book**

**1. Portfolio Health Check**

[metrics + any red flags — include SNPS]

**2. Market Regime**

[concise]

**3. Material Catalysts (last 48h)**

[only high-signal items — include SNPS if relevant]

**4. Position Actions**

[table or structured list for every current liquid holding ≥2.5% + **SNPS always** + new ideas]

**5. Cash & Rebalancing Plan**

[exact moves — include SNPS proceeds if any]

**6. Kill Switches & Scenario Map**

[levels + probabilities — include SNPS]
"""
    return prompt.strip() + "\n"


def main() -> int:
    print("Fetching live E*TRADE portfolio...")
    formatted, grand_total, npos, results, as_of = fetch_portfolio_block(verbose=True)
    save_portfolio_block(formatted, as_of=as_of)

    holdings, accounts = consolidate(results, grand_total)
    prompt = build_prompt(holdings, accounts, grand_total, as_of, formatted)

    OUT_DIR.mkdir(exist_ok=True)
    date_str = as_of.strftime("%Y-%m-%d")
    dated = OUT_DIR / f"daily_briefing_prompt_{date_str}.md"
    latest = OUT_DIR / "daily_briefing_prompt_latest.md"
    dated.write_text(prompt, encoding="utf-8")
    latest.write_text(prompt, encoding="utf-8")

    # Clipboard for quick paste into grok.com
    try:
        import subprocess

        subprocess.run(
            ["clip"],
            input=prompt.encode("utf-16-le"),
            check=True,
        )
        clipped = True
    except Exception:
        clipped = False

    print()
    print("=" * 60)
    print(f"Processed prompt written: {dated}")
    print(f"Also:                      {latest}")
    print(f"Total liquid:              ${grand_total:,.0f}")
    print(f"Positions:                 {npos}")
    if clipped:
        print("Clipboard:                 FULL PROMPT copied — paste into grok.com")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
