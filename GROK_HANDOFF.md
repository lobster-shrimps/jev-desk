# Grok Bot handoff

Updated 2026-10-14. Operating brief for whoever runs the desk. `prompts/HANDOFF.txt`
is different: that text is pasted above each judging seat. This file is for the person
or agent on the machine. Depth lives in `README.md`.

## 1. What this is

A memecoin launch desk. Code fetches, Jev (TypeSafe) judges, code decides.

```
0. UNIVERSE  GeckoTerminal new_pools                    -> fresh launches
1. LIST      FOMO filterTokens, 20 per call             -> hundreds, one batch
2. FREE CUT  age, liquidity, volume, mcap. No network   -> tens
3. TRADE CUT DexScreener buys and sells, one per token  -> a handful
4. DOSSIER   GeckoTerminal info + chain RPC + X         -> a few per cycle
5. JUDGE     market + chain + social per token          -> scored shortlist
6. PICK      one choice over the shortlist              -> one token, or none
```

A finished order then goes SIZE -> FILLS -> RISK, with CHIEF logging. Every threshold is
in `thresholds.py` and is the original guide author's, not tuned to you.

Beside the funnel: paper `young_trail` (and variants / 60s fast / Helius rug signals /
rug-risk skip counterfactuals), `research.db`, an outcomes drain, Coinalyze mood, and a
redesigned `/ops` page. None of those change filter, pick, questions, collect, or live
orders.

## 2. Status

The desk runs on the Mac under launchd. Default is shadow. Paper ledgers and research
are live and queryable.

**Current finding: a week of paper data shows no profitable entry rule yet.** Do not
promote a threshold or entry cut from that week. Keep collecting; treat `thresholds.py`
as a starting point, not an edge.

`python -m pytest -q` is offline and keyless. CI covers 3.10–3.14. Live `book.take` and
the FOMO fill path are still the unproven money path — `--live` needs `CONFIRM_LIVE=yes`
after you have a rule that actually pays.

## 3. What runs where

| piece | where | label / how |
|---|---|---|
| ops / judge | this Mac | launchd `com.robbrown.jevdesk.ops` — `uvicorn server:app` ( `/ops`+`/api` are sync/threadpool so they cannot stall `/judge`). Optional split: `uvicorn judge:app` on 8081, desk `JUDGE_URL=http://127.0.0.1:8081/judge` |
| shift | this Mac | launchd `com.robbrown.jevdesk.desk` — `run.py` every 15 min |
| FomoDesk Chrome | this Mac | dedicated profile, CDP on 9222, `fomo.family` tab logged in |
| `desk.db` + `outbox/` | this Mac | book, state, research, outcomes, paper ledgers. Same working directory (or absolute `DESK_DB` / `DESK_OUTBOX`) for both jobs |
| SOCIAL, CHIEF, SIZE, FILLS, RISK | xAI cloud | reach `/judge` and `/book/*` over the tunnel. Never see the TypeSafe key |

## 4. Launchd — status and restart

```bash
launchctl list | grep jevdesk
launchctl kickstart -k "gui/$(id -u)/com.robbrown.jevdesk.desk"
launchctl kickstart -k "gui/$(id -u)/com.robbrown.jevdesk.ops"
```

Plists are on the operator machine (`~/Library/LaunchAgents/`), not in the repo.
`/ops/summary` reads `launchctl list` only and never kickstarts.

## 5. FomoDesk Chrome token flow

FOMO has no public API. A dedicated **FomoDesk** Chrome profile is started with
`--remote-debugging-port=9222`. Stay logged into `fomo.family` in **that** window.

Each cycle `Fomo.token()` reads the Privy bearer from the tab's `localStorage`
(`privy:token` / `privy:access_token`) over `CDP_URL`. It dies about hourly; refresh is
automatic. Never log the bearer.

`/ops` **Fix FOMO Login** (`POST /api/fomo_activate`) brings that tab forward or opens
`fomo.family` in the CDP Chrome. Alternative: set `FOMO_BEARER` (still hourly).

## 6. /ops

Page: `GET /ops` (`ops.html`, no external URLs, no secrets). JSON:

| endpoint | what |
|---|---|
| `GET /ops/summary` | 30s cache: launchd, health, judge-hi, outcomes, budget, Coinalyze, young_trail curve |
| `GET /ops/briefing` | last-24h + 7-day trends |
| `GET /ops/briefing?compact=1` | markdown + morning only |
| `GET /ops/young_trail` | paper ledger |
| `GET /ops/young_trail_variants` | exit-variant ranking |
| `GET /ops/young_trail_fast` | 60s marks vs per-cycle |
| `GET /ops/young_trail_helius` | Helius vs fast vs cycle |
| `GET /ops/young_trail_rugrisk` | paper skip-rule counterfactuals (no gating) |
| `GET /ops/research/query?sql=` | read-only SELECT on `research.db` (limit 500) |
| `GET /ops/research/report?kind=` | rugs / runners / features |
| `GET /ops/backtest` | weekly signal-backtest JSON |

## 7. Paper young_trail

$50 ticket, first tid, free-pass and `age_minutes < 60`. Dex marks only. Ends
2026-10-20 11:59 PM ET.

- **Baseline:** −30% stop, 30% trail from peak, 6h.
- **Variants (per-cycle marks):** `tp_50`, `tp_100`, `half_50_trail`, `trail_20`,
  `trail_40`, `sl_20`, `time_1h` / `time_2h` / `time_3h`, `be_after_30`.
- **Fast:** same entries, baseline + `tp_50` on the 60s Dex sampler.
- **Helius (Solana):** FOMO creator + top 10 + pool at entry; budgeted RPC for a 20%+
  dev sell, 30%+ top-3 sell, or 25%+ pool SOL drop. Paper exit; precision / recall /
  lead time vs `token_labels` and outcomes.
- **Rug-risk (paper):** same entries; sniper / bundled / prior-rug features via
  budgeted Helius. Skip rules are counterfactual only (yt / fast / tp_50 SKIPPED vs
  KEPT). Does not gate. Until 20 Oct 2026.

## 8. research.db and outcomes

`outbox/research.db` (WAL): `token_snapshots`, `fast_marks`, `token_labels`,
`research_meta`. Analysis only.

```bash
python research.py sql "SELECT ..."
python research.py report rugs|runners|features
python research.py backfill
python research.py labels
```

Backfill reads cycle history, run logs, cycle JSON, shadow rows, young_trail, outcomes
candles, X cache, Coinalyze. Missing prices queue into the outcomes drain.

Outcomes (`outcomes.db`): 6h OHLCV. Bulk in `OUTCOMES_WINDOW` (default 01:00–06:00
local); daytime trickle (`OUTCOMES_TRICKLE_SEC`, default 20s). CoinGecko Demo
(`COINGECKO_API_KEY`, `COINGECKO_PLAN=demo`) is 9000 calls/month then free
GeckoTerminal. Desk scan stays on free GT.

## 9. Coinalyze and X

- **Coinalyze** (`COINALYZE_API_KEY`): log-only mood — `long_flush` / `risk_on` /
  `risk_off` / `neutral`. Missing key skips. Never feeds filter or pick.
- **X:** `SOCIAL_URL` if the bot can serve HTTP; else `x_lookup.py` (fxtwitter, no key),
  capped by `X_LOOKUPS_PER_CYCLE` and `X_LOOKUP_BUDGET_SEC`. Failure is `NO_SOCIAL_CUT`.

## 10. Env var names only

Never log or commit values, especially `SOLANA_RPC_URL`, `COINALYZE_API_KEY`,
`COINGECKO_API_KEY`.

Also used by name: `TYPESAFE_API_KEY`, `DESK_SECRET`, `JUDGE_URL`, `FOMO_BEARER`,
`CDP_URL`, `BANK_USD`, `DESK_DB`, `DESK_OUTBOX`, `SOCIAL_URL`, `SEATS_WEBHOOK_URL`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `CONFIRM_LIVE`, `JUDGE_MOCK`,
`COINGECKO_PLAN`, `OUTCOMES_WINDOW`, `OUTCOMES_TRICKLE_SEC`, `X_LOOKUPS_PER_CYCLE`,
`X_LOOKUP_BUDGET_SEC`.

`.env` is gitignored. `.env.example` holds placeholders only.

## 11. Shadow first, then live

`python run.py` (default) does everything except take the book. Would-be trades go to
`outbox/shadow.jsonl`. The paper week so far has **not** produced a profitable entry
rule — do not go live on that evidence.

`JUDGE_MOCK=1` / `--mock-judge` are funnel tests, never for tuning.

Live only with `CONFIRM_LIVE=yes python run.py --live`.

## 12. Book control

```bash
curl $BASE/book/held    -H "Authorization: Bearer $DESK_SECRET"
curl -X POST $BASE/book/release -H "Authorization: Bearer $DESK_SECRET"
```

If a close happened and nothing reported it, the desk is stalled. Release by hand after
confirming in FOMO that the position is actually closed.

## 13. If something breaks

| symptom | meaning |
|---|---|
| launchd PID missing on `/ops` | `launchctl kickstart` that label |
| 401 from `/judge` | wrong `DESK_SECRET` |
| 422 from `/judge` | malformed question. Never retry (`JudgeDown`) |
| 429 GeckoTerminal | back off; persist → `universe(pages=1)` or wait for trickle |
| Demo month exhausted | outcomes on free GT until rollover |
| FOMO 401/403 | bearer expired; refresh from FomoDesk Chrome, or **Fix FOMO Login** |
| "HOLDING ... no scan" forever | close never released |
| bots connection errors | tunnel URL changed |
| judge unreachable | cycle stands down. No guessing |
| `/judge` times out while `/ops` tabs are open | ops SQLite was on the event loop; handlers are now `def`. Optional: judge on 8081 |

`python run.py --bench` prints what is benched and why.

## 14. Secrets

- `TYPESAFE_API_KEY` lives on the judge machine only. No bot gets it.
- `DESK_SECRET` is what bots get. Rotate: restart ops + desk, update every bot skill.
- `SOLANA_RPC_URL`, `COINALYZE_API_KEY`, `COINGECKO_API_KEY`, `FOMO_BEARER`: names in
  docs and logs, never values. Route errors through `safe_err`.
