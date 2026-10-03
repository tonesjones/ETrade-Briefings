# Routing log

`date | task | model | inline/delegated | result | tokens in / out | cost | notes`

Prices: Opus 5.5 $4 in / $20 out / $0.20 cache read; Sonnet 5.5 $2 / $10 / $0.20; Haiku 4.5 $1 / $5 / $0.10 (per MTok, platform.claude.com pricing, 2026-10-02). Subagent output tokens are estimated from content size (~3.5 chars/token): their transcripts record usage at message start.

2026-10-02 | T1+T2+T6 P0 fixes, term/LT logic, reply contract | Opus 5.5 | inline | pass | 3,310k / 23.6k | $1.40 | small edits, context already loaded
2026-10-02 | T3 lot-level harvest flags | Sonnet 5.5 | delegated | pass | 256k / ~3.2k | $0.21 | 
2026-10-02 | T4 options kept separate | Sonnet 5.5 | delegated | fix | 315k / ~3.8k | $0.24 | Opus fixed a cross-file gap: short options hidden by the table's $50 floor
2026-10-02 | T5 P0 regression tests | Sonnet 5.5 | delegated | pass | 441k / ~4.7k | $0.31 | 22/23 fail on master, independently verified
2026-10-02 | T7 import_response validator | Sonnet 5.5 | delegated | fix | 668k / ~16.8k | $0.53 | Opus froze a per-briefing portfolio copy (spec gap, not agent error)
2026-10-02 | T8 README + .gitignore | Haiku 4.5 | delegated | pass | 394k / ~2.1k | $0.10 | should have stayed inline: the brief contained the full text
2026-10-02 | orchestration + checking delegated work | Opus 5.5 | inline | n/a | 4,957k / 19.8k | $1.93 | briefs, diff review, verification runs
2026-10-02 | T9 /code-review high | Opus 5.5 | forked | 10 findings | 2,269k / ~3.0k | $1.12 | 7 fixed, 3 kept/skipped with reasons
2026-10-02 | T9 triage + fixes + cost accounting | Opus 5.5 | inline | pass | 3,007k / 14.0k | $1.03 | 
2026-10-03 | README technical-writing + unslop pass | Opus 5.5 | inline | pass | n/a | n/a | gate: one file already in context, no objective done-when

Lessons: Opus 5.5 cache reads cost the same as Sonnet's ($0.20/MTok), so delegation saves only on output and cache writes. Each main-session turn re-read ~200k tokens of context. Doc-only edits are not worth delegating. Parallel agents can't see each other's changes (T4 options x T7 pricing), so an integration review is required.
