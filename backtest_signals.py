"""
Weekly signal backtest: join cycle history / shadow kills with forward outcomes.

LOG/ANALYSIS ONLY. Does not change a kill, judge, pick, or order.
CLI: python backtest_signals.py --days 7
Weekly: first cycle after Sunday 07:00 local, time-capped background thread.
Fail open; exceptions go through safe_err; never logs secrets.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import pathlib
import threading
import time
from typing import Callable

from secret_utils import safe_err

log = logging.getLogger("backtest_signals")

TICKET_USD = 50.0
N_CAVEAT = "n is the cell count. Cells with n<10 are noisy; do not retune thresholds on them."
DEFAULT_DAYS = 7
DEFAULT_TIME_CAP_SEC = 480.0
WIN_PEAK_X = 2.0
JUDGE_BUCKETS = (
    ("<0.3", 0.0, 0.3),
    ("0.3–0.5", 0.3, 0.5),
    ("0.5–0.7", 0.5, 0.7),
    (">=0.7", 0.7, 10.0),
)
FEATURE_KEYS = (
    "age", "liq", "mcap", "holders", "top_10", "top_wallet", "dev_pct",
    "crowd_prob", "momentum_noul", "x_followers", "x_own_posts", "judge_score",
)

_weekly_lock = threading.Lock()
_weekly_thread: threading.Thread | None = None
_started_weeks: set[str] = set()
_time_fn = time.time


def set_time_fn(fn):
    global _time_fn
    _time_fn = time.time if fn is None else fn


def current_time() -> float:
    return float(_time_fn())


def reset() -> None:
    """Test helper: drop weekly-thread bookkeeping."""
    global _weekly_thread
    with _weekly_lock:
        _weekly_thread = None
        _started_weeks.clear()


def outbox_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))


def backtests_dir() -> pathlib.Path:
    return outbox_dir() / "backtests"


def time_cap_sec() -> float:
    raw = os.environ.get("BACKTEST_TIME_CAP_SEC")
    if raw is None or raw == "":
        return DEFAULT_TIME_CAP_SEC
    try:
        return max(5.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_TIME_CAP_SEC


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _week_id_of(dt: datetime.datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]:04d}-{iso[1]:02d}"


def trigger_sunday_0700(now: float, localtime: Callable | None = None) -> tuple[str, datetime.datetime] | None:
    """Most recent Sunday 07:00 local that has already passed, plus its ISO week id."""
    lt = (localtime or time.localtime)(now)
    dt = datetime.datetime(lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec)
    days_since_sunday = (dt.weekday() + 1) % 7
    sunday = dt.replace(hour=7, minute=0, second=0, microsecond=0) - datetime.timedelta(
        days=days_since_sunday
    )
    if dt < sunday:
        sunday -= datetime.timedelta(days=7)
    if dt < sunday:
        return None
    return _week_id_of(sunday), sunday


def latest_path() -> pathlib.Path | None:
    dest = backtests_dir()
    if not dest.exists():
        return None
    files = sorted(dest.glob("*.json"))
    if not files:
        md = sorted(dest.glob("*.md"))
        return md[-1] if md else None
    return files[-1]


def latest_payload() -> dict | None:
    path = latest_path()
    if path is None:
        return None
    try:
        if path.suffix == ".json":
            return json.loads(path.read_text())
        sibling = path.with_suffix(".json")
        if sibling.exists():
            return json.loads(sibling.read_text())
        return {"markdown_path": str(path), "markdown": path.read_text()}
    except Exception as e:
        log.warning("latest backtest read failed: %s", safe_err(e))
        return None


def _mean(values) -> float | None:
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _pct(n, d) -> float | None:
    if not d:
        return None
    return n / d * 100.0


def _fmt(v, digits=2, signed=False) -> str:
    if v is None:
        return "—"
    if signed:
        return f"{float(v):+.{digits}f}"
    return f"{float(v):.{digits}f}"


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{float(v):.0f}%"


def _is_win(row: dict) -> bool:
    peak = row.get("peak_x")
    return peak is not None and float(peak) >= WIN_PEAK_X


def _quartile_index(value, xs: list[float]) -> int | None:
    if value is None or len(xs) < 4:
        return None
    n = len(xs)
    def pctile(p):
        i = min(n - 1, max(0, int(round((n - 1) * p))))
        return xs[i]
    c1, c2, c3 = pctile(0.25), pctile(0.50), pctile(0.75)
    v = float(value)
    if v <= c1:
        return 0
    if v <= c2:
        return 1
    if v <= c3:
        return 2
    return 3


def _is_measured(row: dict) -> bool:
    """Same definition as log complete=: a finalized 6h outcome window."""
    return bool(row.get("outcome_complete"))


def _cell(rows: list[dict]) -> dict:
    n = len(rows)
    measured = [r for r in rows if _is_measured(r)]
    n_meas = len(measured)
    wins = sum(1 for r in measured if _is_win(r))
    return {
        "n": n,
        "n_measured": n_meas,
        "wins": wins,
        "win_pct": _pct(wins, n_meas),
        "mean_sim_current": _mean([r.get("sim_current_pnl") for r in measured]),
        "mean_sim_trail": _mean([r.get("sim_trail_pnl") for r in measured]),
        "mean_peak_x": _mean([r.get("peak_x") for r in measured]),
    }


def _enrich(shadow_row: dict, token: dict | None, regime: str | None) -> dict:
    t = token or {}
    scores = shadow_row.get("soft_scores") or t.get("soft_scores") or {}
    crowd = shadow_row.get("crowd_prob")
    if crowd is None:
        crowd = scores.get("crowd") if isinstance(scores, dict) else None
        if crowd is None and (shadow_row.get("reason") or "") in ("shape", "shape_weak"):
            crowd = shadow_row.get("noul") or t.get("noul")
    mom = shadow_row.get("momentum_noul")
    if mom is None:
        mom = scores.get("momentum_already_spent") if isinstance(scores, dict) else None
        if mom is None:
            mom = t.get("noul") if (shadow_row.get("reason") == "momentum_already_spent") else None
    x_present = shadow_row.get("x_present")
    import cycle_history
    liq = cycle_history._liq_of(shadow_row, t)
    mcap = cycle_history._mcap_of(shadow_row, t)
    return {
        **shadow_row,
        "age": shadow_row.get("age_minutes") if shadow_row.get("age_minutes") is not None else t.get("age_minutes"),
        "liq": liq,
        "mcap": mcap,
        "holders": shadow_row.get("holders") if shadow_row.get("holders") is not None else t.get("holder_count"),
        "top_10": shadow_row.get("top_10_percent") if shadow_row.get("top_10_percent") is not None else t.get("top_10_percent"),
        "top_wallet": shadow_row.get("top_wallet_percent") if shadow_row.get("top_wallet_percent") is not None else t.get("top_wallet_percent"),
        "dev_pct": (
            shadow_row.get("developer_holding_percentage")
            if shadow_row.get("developer_holding_percentage") is not None
            else t.get("developer_holding_percentage")
        ),
        "crowd_prob": crowd,
        "momentum_noul": mom,
        "x_present": x_present,
        "x_followers": shadow_row.get("x_followers"),
        "x_own_posts": shadow_row.get("x_own_posts"),
        "regime": shadow_row.get("regime") or regime,
        "judge_score": shadow_row.get("judge_score") if shadow_row.get("judge_score") is not None else t.get("judge_score"),
        "kill_stage": shadow_row.get("stage"),
        "kill_reason": shadow_row.get("reason"),
    }


def load_rows(since_ts: float, until_ts: float) -> list[dict]:
    import cycle_history
    shadow = cycle_history.shadow_kills_since(since_ts, until_ts)
    tokens = cycle_history._tokens_since(since_ts, until_ts=until_ts)
    young = cycle_history._young_since(since_ts, until_ts)
    regimes = cycle_history._regimes_since(since_ts, until_ts)
    tokens_by_tid: dict[str, list] = {}
    for t in tokens:
        if t.get("tid"):
            tokens_by_tid.setdefault(t["tid"], []).append(t)

    def nearest_token(tid, ts):
        matches = tokens_by_tid.get(tid) or []
        if not matches:
            return None
        return min(matches, key=lambda row: abs((row.get("ts") or 0) - (ts or 0)))

    def nearest_regime(ts):
        if not regimes:
            return None
        best = min(regimes, key=lambda r: abs((r.get("ts") or 0) - (ts or 0)))
        return best.get("regime")

    rows = []
    seen = set()
    for s in shadow:
        key = (s.get("tid"), round(float(s.get("entry_ts") or s.get("ts") or 0), 3),
               s.get("stage"), s.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        tok = nearest_token(s.get("tid"), s.get("ts"))
        rows.append(_enrich(s, tok, nearest_regime(s.get("ts"))))
    # young_free_pass / tokens already feed shadow_kills (live + backfill).
    _ = young  # joined via backfill; kept so the spec's join list is loaded
    return rows


def attach_outcomes(rows: list[dict], *, now: float | None = None,
                    deadline: float | None = None, fetch: bool = True) -> list[dict]:
    import outcomes
    now = current_time() if now is None else float(now)
    out = []
    for row in rows:
        if deadline is not None and current_time() >= deadline:
            fetch = False
        tid = row.get("tid")
        entry_ts = row.get("entry_ts") or row.get("ts")
        payload = None
        try:
            if fetch and tid and entry_ts is not None:
                payload = outcomes.fetch_outcome(tid, entry_ts, now=now, deadline=deadline)
            elif tid and entry_ts is not None:
                payload = outcomes.get_cached(tid, entry_ts)
        except Exception as e:
            log.warning("outcome attach failed: %s", safe_err(e))
            payload = None
        merged = dict(row)
        complete = bool(payload and payload.get("complete"))
        merged["outcome_complete"] = complete
        if complete:
            merged["peak_x"] = payload.get("peak_x")
            merged["trough_x"] = payload.get("trough_x")
            merged["ret_6h"] = payload.get("ret_6h")
            merged["sim_current_pnl"] = payload.get("sim_current_pnl")
            merged["sim_trail_pnl"] = payload.get("sim_trail_pnl")
            merged["sim_current_reason"] = payload.get("sim_current_reason")
            merged["sim_trail_reason"] = payload.get("sim_trail_reason")
        else:
            # Incomplete candles must not count as "measured" (log complete=).
            merged["peak_x"] = None
            merged["trough_x"] = None
            merged["ret_6h"] = None
            merged["sim_current_pnl"] = None
            merged["sim_trail_pnl"] = None
            merged["sim_current_reason"] = None
            merged["sim_trail_reason"] = None
        out.append(merged)
    return out


def _winners_table(rows: list[dict]) -> dict:
    measured = [r for r in rows if _is_measured(r)]
    both = [
        r for r in measured
        if _is_win(r)
        and (r.get("sim_current_pnl") or 0) > 0
        and (r.get("sim_trail_pnl") or 0) > 0
    ]
    defs = [
        ("peak_x >= 2", [r for r in measured if _is_win(r)]),
        ("sim_current P&L > 0", [r for r in measured if (r.get("sim_current_pnl") or 0) > 0]),
        ("sim_trail P&L > 0", [r for r in measured if (r.get("sim_trail_pnl") or 0) > 0]),
        ("peak_x>=2 and both sims > 0", both),
        ("losers (peak_x < 2)", [r for r in measured if not _is_win(r)]),
    ]
    return {
        "n_measured": len(measured),
        "n_total": len(rows),
        "rows": [{"definition": name, **_cell(group)} for name, group in defs],
    }


def _quartile_tables(rows: list[dict]) -> dict:
    tables = {}
    labels = ("Q1 (low)", "Q2", "Q3", "Q4 (high)")
    for key in FEATURE_KEYS:
        xs = sorted(float(r[key]) for r in rows if r.get(key) is not None)
        buckets = [[] for _ in range(4)]
        unbinned = []
        for r in rows:
            idx = _quartile_index(r.get(key), xs)
            if idx is None:
                unbinned.append(r)
            else:
                buckets[idx].append(r)
        tables[key] = {
            "n_with_value": len(xs),
            "quartiles": [
                {"quartile": labels[i], **_cell(buckets[i])} for i in range(4)
            ],
            "missing": _cell(unbinned),
        }
    # Boolean x_present
    present = [r for r in rows if r.get("x_present") in (1, True)]
    absent = [r for r in rows if r.get("x_present") in (0, False, None)]
    tables["x_account_present"] = {
        "n_with_value": len(present) + len(absent),
        "quartiles": [
            {"quartile": "present", **_cell(present)},
            {"quartile": "absent/unknown", **_cell(absent)},
        ],
        "missing": _cell([]),
    }
    return tables


def _reason_table(rows: list[dict]) -> list[dict]:
    by: dict[str, list] = {}
    for r in rows:
        key = r.get("kill_reason") or r.get("reason") or (r.get("kill_stage") or "other")
        by.setdefault(str(key), []).append(r)
    out = []
    for reason, group in sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        cell = _cell(group)
        cell["reason"] = reason
        cell["stage"] = (group[0].get("kill_stage") or group[0].get("stage")) if group else None
        cell["total_sim_current"] = sum(
            (g.get("sim_current_pnl") or 0) for g in group if g.get("sim_current_pnl") is not None
        )
        cell["total_sim_trail"] = sum(
            (g.get("sim_trail_pnl") or 0) for g in group if g.get("sim_trail_pnl") is not None
        )
        out.append(cell)
    return out


def _judge_table(rows: list[dict]) -> list[dict]:
    buckets = {label: [] for label, _, _ in JUDGE_BUCKETS}
    buckets["none"] = []
    for r in rows:
        score = r.get("judge_score")
        if score is None:
            buckets["none"].append(r)
            continue
        placed = False
        for label, lo, hi in JUDGE_BUCKETS:
            if lo <= float(score) < hi or (label.startswith(">=") and float(score) >= lo):
                buckets[label].append(r)
                placed = True
                break
        if not placed:
            buckets["none"].append(r)
    out = []
    for label, _, _ in JUDGE_BUCKETS:
        cell = _cell(buckets[label])
        cell["bucket"] = label
        out.append(cell)
    none = _cell(buckets["none"])
    none["bucket"] = "none"
    out.append(none)
    return out


def _regime_table(rows: list[dict]) -> list[dict]:
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(str(r.get("regime") or "unknown"), []).append(r)
    out = []
    for name, group in sorted(by.items()):
        cell = _cell(group)
        cell["regime"] = name
        out.append(cell)
    return out


def render_markdown(report: dict) -> str:
    lines = [
        f"# Signal backtest — {report.get('week_id', '')}",
        "",
        f"Generated {report.get('generated_at_iso', '')} (UTC). "
        f"Window: last {report.get('days', DEFAULT_DAYS)} days. "
        f"${TICKET_USD:.0f} ticket, 3% cost, SL -30 / TP +100 / 6h. Shadow only.",
        "Does not change kills, judge, pick, or orders.",
        "",
        N_CAVEAT,
        "",
        "## Winners vs losers",
        "",
        "| definition | n | measured | win% | mean sim_current $ | mean sim_trail $ |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    winners = report.get("winners") or {}
    for row in winners.get("rows") or []:
        lines.append(
            f"| {row.get('definition')} | {row.get('n', 0)} | {row.get('n_measured', 0)} "
            f"| {_fmt_pct(row.get('win_pct'))} | {_fmt(row.get('mean_sim_current'), signed=True)} "
            f"| {_fmt(row.get('mean_sim_trail'), signed=True)} |"
        )
    lines += [
        N_CAVEAT,
        "",
        "## Per-feature quartiles",
        "",
        "win% = share with peak_x ≥ 2 among complete 6h outcomes (measured = complete).",
        "",
    ]
    for key, table in (report.get("features") or {}).items():
        lines.append(f"### {key}")
        lines.append("| bucket | n | measured | win% | mean sim_current $ | mean sim_trail $ |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for row in table.get("quartiles") or []:
            lines.append(
                f"| {row.get('quartile')} | {row.get('n', 0)} | {row.get('n_measured', 0)} "
                f"| {_fmt_pct(row.get('win_pct'))} | {_fmt(row.get('mean_sim_current'), signed=True)} "
                f"| {_fmt(row.get('mean_sim_trail'), signed=True)} |"
            )
        lines.append(N_CAVEAT)
        lines.append("")
    lines += [
        "## Per kill reason",
        "",
        "| reason | n | measured | win% | mean $50 sim_current | mean $50 sim_trail | sum sim_current |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report.get("kill_reasons") or []:
        lines.append(
            f"| {row.get('reason')} | {row.get('n', 0)} | {row.get('n_measured', 0)} "
            f"| {_fmt_pct(row.get('win_pct'))} | {_fmt(row.get('mean_sim_current'), signed=True)} "
            f"| {_fmt(row.get('mean_sim_trail'), signed=True)} "
            f"| {_fmt(row.get('total_sim_current'), signed=True)} |"
        )
    lines += [
        N_CAVEAT,
        "",
        "## Judge score buckets",
        "",
        "| bucket | n | measured | win% | mean sim_current $ | mean sim_trail $ |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report.get("judge_buckets") or []:
        lines.append(
            f"| {row.get('bucket')} | {row.get('n', 0)} | {row.get('n_measured', 0)} "
            f"| {_fmt_pct(row.get('win_pct'))} | {_fmt(row.get('mean_sim_current'), signed=True)} "
            f"| {_fmt(row.get('mean_sim_trail'), signed=True)} |"
        )
    lines += [
        N_CAVEAT,
        "",
        "## Regime",
        "",
        "| regime | n | measured | win% | mean sim_current $ | mean sim_trail $ |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report.get("regimes") or []:
        lines.append(
            f"| {row.get('regime')} | {row.get('n', 0)} | {row.get('n_measured', 0)} "
            f"| {_fmt_pct(row.get('win_pct'))} | {_fmt(row.get('mean_sim_current'), signed=True)} "
            f"| {_fmt(row.get('mean_sim_trail'), signed=True)} |"
        )
    lines += [N_CAVEAT, ""]
    return "\n".join(lines)


def build_report(rows: list[dict], *, week_id: str, days: int, now: float,
                 extra: dict | None = None) -> dict:
    n_complete = sum(1 for r in rows if _is_measured(r))
    report = {
        "week_id": week_id,
        "days": days,
        "generated_at": now,
        "generated_at_iso": _iso(now),
        "ticket_usd": TICKET_USD,
        "cost": 0.03,
        "n": len(rows),
        "n_complete": n_complete,
        "n_measured": n_complete,
        "winners": _winners_table(rows),
        "features": _quartile_tables(rows),
        "kill_reasons": _reason_table(rows),
        "judge_buckets": _judge_table(rows),
        "regimes": _regime_table(rows),
        "shadow_kills_by_reason": {
            (r.get("reason") or "other"): r for r in _reason_table(rows)
        },
        "n_caveat": N_CAVEAT,
        "shadow_only": True,
    }
    if extra:
        report.update(extra)
    report["markdown"] = render_markdown(report)
    return report


def persist_report(report: dict) -> pathlib.Path:
    dest = backtests_dir()
    dest.mkdir(parents=True, exist_ok=True)
    week = report.get("week_id") or "unknown"
    md_path = dest / f"{week}.md"
    json_path = dest / f"{week}.json"
    md_path.write_text(report.get("markdown") or render_markdown(report))
    payload = dict(report)
    payload.pop("rows", None)
    json_path.write_text(json.dumps(payload, default=str, indent=2, sort_keys=True))
    report["path"] = str(md_path)
    report["json_path"] = str(json_path)
    return md_path


def run_backtest(*, days: int = DEFAULT_DAYS, now: float | None = None,
                 week_id: str | None = None, deadline: float | None = None,
                 fetch: bool = True, persist: bool = True) -> dict:
    """Join history with outcomes and write YYYY-WW.md + JSON. Fail open."""
    now = current_time() if now is None else float(now)
    if week_id is None:
        lt = time.localtime(now)
        week_id = _week_id_of(datetime.datetime(
            lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec
        ))
    since = now - float(days) * 86400
    try:
        import cycle_history
        try:
            # Re-parse run.log so skipped cycles still pick up liq/mcap that
            # were missing when tokens was first written (week 2026-41).
            cycle_history.backfill_log()
        except Exception as e:
            log.warning("backtest log backfill: %s", safe_err(e))
        try:
            cycle_history.backfill_shadow_kills()
        except Exception as e:
            log.warning("shadow kills backfill: %s", safe_err(e))
        rows = load_rows(since, now)
        rows = attach_outcomes(rows, now=now, deadline=deadline, fetch=fetch)
        report = build_report(rows, week_id=week_id, days=days, now=now)
        if persist:
            persist_report(report)
        log.info(
            "backtest %s n=%d complete=%d measured=%d path=%s",
            week_id, report.get("n"), report.get("n_complete"),
            report.get("n_measured"), report.get("path"),
        )
        return report
    except Exception as e:
        log.warning("backtest failed: %s", safe_err(e))
        return {
            "week_id": week_id, "days": days, "error": safe_err(e),
            "n": 0, "winners": {"rows": []}, "features": {},
            "kill_reasons": [], "judge_buckets": [], "markdown": "",
        }


def maybe_start_weekly(now: float | None = None, *, localtime: Callable | None = None,
                       days: int = DEFAULT_DAYS) -> threading.Thread | None:
    """First cycle after Sunday 07:00 local: run the backtest in a capped thread.

    Wired from run.py / main.main(). Fail open. Never touches the cycle GT limiter.
    """
    if os.environ.get("BACKTEST_DISABLE") == "1":
        return None
    now = current_time() if now is None else float(now)
    try:
        trig = trigger_sunday_0700(now, localtime=localtime)
        if trig is None:
            return None
        week_id, _sunday = trig
        dest = backtests_dir() / f"{week_id}.json"
        if dest.exists():
            return None
        global _weekly_thread
        with _weekly_lock:
            if week_id in _started_weeks:
                return None
            if _weekly_thread is not None and _weekly_thread.is_alive():
                return None
            _started_weeks.add(week_id)
            cap = time_cap_sec()
            deadline = current_time() + cap

            def _run():
                try:
                    # Cache only: outcome HTTP is the off-peak / trickle worker.
                    run_backtest(days=days, now=now, week_id=week_id,
                                 deadline=deadline, fetch=False, persist=True)
                except Exception as e:
                    log.warning("weekly backtest: %s", safe_err(e))

            t = threading.Thread(target=_run, name=f"backtest-{week_id}", daemon=True)
            _weekly_thread = t
            t.start()
            log.info("weekly backtest thread started for %s (cap %.0fs)", week_id, cap)
            return t
    except Exception as e:
        log.warning("weekly backtest skip: %s", safe_err(e))
        return None


def main(argv=None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    ap = argparse.ArgumentParser(description="Weekly signal backtest (log/analysis only)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--fetch", action="store_true",
                    help="drain pending outcome jobs (network) before the cache-only report")
    args = ap.parse_args(argv)
    try:
        if args.fetch:
            import outcomes
            try:
                outcomes.enqueue_from_history(days=args.days)
                outcomes.drain_jobs(trickle=False)
            except Exception as e:
                log.warning("outcomes fetch: %s", safe_err(e))
        report = run_backtest(days=args.days, fetch=False, persist=True)
        path = report.get("path") or ""
        log.info("wrote %s", path)
        return 0 if not report.get("error") else 1
    except Exception as e:
        log.error("backtest failed: %s", safe_err(e))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
