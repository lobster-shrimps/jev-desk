"""Read-only /ops/summary aggregate.

Cached 30s in-process. Fail open with partial JSON. Never logs secrets.
launchctl list only (no kickstart). Log tails are bounded to ~5 MB.
"""
from __future__ import annotations

import calendar
import logging
import os
import pathlib
import re
import subprocess
import time
from typing import Callable

from secret_utils import safe_err

log = logging.getLogger("ops_summary")

CACHE_TTL_SEC = 30.0
TAIL_BYTES = 5 * 1024 * 1024
JUDGE_HI_MIN = 0.6
JUDGE_HI_MAX = 50
JUDGE_HI_DAYS = 7
REGIME_HISTORY = 12
LAUNCHCTL_PREFIX = "com.robbrown.jevdesk."
DESK_LABEL = "com.robbrown.jevdesk.desk"
OPS_LABEL = "com.robbrown.jevdesk.ops"

TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\):")
SLEEP_RE = re.compile(
    r"sleeping ([0-9.]+)s until next cycle \(start-to-start: ([0-9.]+)s\)"
)
CYCLE_START_RE = re.compile(r"cycle start: mode=")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
JUDGE_LINE_RE = re.compile(
    r"judge tid=(\S+) ticker=(.+?) verdict=(\S+) score=(\S+) "
    r"confidence=(\S+) reason=(.*) mode=(\S+)\s*$"
)

_cache: dict = {"ts": 0.0, "payload": None}
_launchctl_fn: Callable[[], str] | None = None


def reset() -> None:
    """Tests: drop the in-process cache and injected launchctl helper."""
    global _cache, _launchctl_fn
    _cache = {"ts": 0.0, "payload": None}
    _launchctl_fn = None


def set_launchctl_fn(fn: Callable[[], str] | None) -> None:
    global _launchctl_fn
    _launchctl_fn = fn


def outbox_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_ts(text: str) -> float | None:
    m = TS_RE.match(text or "")
    if not m:
        return None
    raw = m.group(1).replace("T", " ")
    try:
        return time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def _num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_SECRET_ENV = (
    "COINGECKO_API_KEY", "DESK_SECRET", "TYPESAFE_API_KEY",
    "COINALYZE_API_KEY", "FOMO_BEARER",
)


def _scrub(text: str | None) -> str | None:
    """Drop env secret values from any string that lands in the payload."""
    if text is None:
        return None
    msg = str(text)
    for env_name in _SECRET_ENV:
        val = os.environ.get(env_name)
        if val and len(val) >= 8 and val in msg:
            msg = msg.replace(val, "REDACTED")
    for key, value in os.environ.items():
        if key.endswith("_RPC_URL") and value and len(value) >= 8 and value in msg:
            msg = msg.replace(value, "REDACTED")
    return msg


def read_log_tail(path, max_bytes: int = TAIL_BYTES) -> str:
    """Read the last ~max_bytes of a log. Never slurp the whole file."""
    try:
        p = pathlib.Path(path)
        if not p.is_file():
            return ""
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("ops summary log tail %s: %s", path, safe_err(e))
        return ""


def discover_logs() -> dict[str, pathlib.Path]:
    """run.log / uvicorn.log under outbox (and logs/). First existing wins."""
    root = outbox_dir()
    found: dict[str, pathlib.Path] = {}
    for key, names in (
        ("run_log", ("run.log", "logs/run.log")),
        ("uvicorn_log", ("uvicorn.log", "logs/uvicorn.log")),
    ):
        for name in names:
            p = root / name
            if p.is_file():
                found[key] = p
                break
    return found


def scan_tracebacks(text: str, now: float, window_s: float = 86400.0
                    ) -> tuple[int, dict | None]:
    """Count Traceback blocks in `text` whose last-seen stamp is within window."""
    count = 0
    last = None
    last_ts = None
    cutoff = now - window_s
    for raw in (text or "").splitlines():
        ts = _parse_ts(raw)
        if ts is not None:
            last_ts = ts
        if not TRACEBACK_RE.search(raw):
            continue
        ts = last_ts
        if ts is not None and ts < cutoff:
            continue
        count += 1
        last = {
            "ts_iso": _iso(ts) if ts is not None else None,
            "first_line": _scrub(raw.strip()[:240]),
        }
    return count, last


def parse_launchctl(text: str) -> dict:
    """Parse `launchctl list` for com.robbrown.jevdesk.{desk,ops}. No kickstart."""
    desk = {"loaded": False, "pid": None}
    ops = {"loaded": False, "pid": None}
    for raw in (text or "").splitlines():
        parts = raw.split()
        if len(parts) < 2:
            continue
        label = parts[-1]
        if not label.startswith(LAUNCHCTL_PREFIX):
            continue
        pid_s = parts[0]
        loaded = pid_s not in ("-", "")
        pid = None
        if loaded:
            try:
                pid = int(pid_s)
            except ValueError:
                pid = None
                loaded = False
        row = {"loaded": loaded, "pid": pid}
        if label == DESK_LABEL or label.endswith(".desk"):
            desk = row
        elif label == OPS_LABEL or label.endswith(".ops"):
            ops = row
    return {"desk": desk, "ops": ops}


def launchctl_health() -> dict:
    try:
        if _launchctl_fn is not None:
            text = _launchctl_fn() or ""
        else:
            proc = subprocess.run(
                ["launchctl", "list"],
                capture_output=True, text=True, timeout=2,
            )
            text = proc.stdout or ""
        return parse_launchctl(text)
    except Exception as e:
        log.warning("ops summary launchctl: %s", safe_err(e))
        return {
            "desk": {"loaded": False, "pid": None},
            "ops": {"loaded": False, "pid": None},
        }


def _state() -> dict:
    try:
        path = outbox_dir() / "state.json"
        if not path.is_file():
            return {}
        import json
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning("ops summary state: %s", safe_err(e))
        return {}


def _health(now: float, cycles_24h: int, no_trade_24h: int, shadow_24h: int,
            last_cycle: dict | None, log_bits: dict) -> dict:
    state = _state()
    lc = launchctl_health()
    mode = state.get("mode")
    if mode not in ("shadow", "live"):
        mode = "live" if os.environ.get("CONFIRM_LIVE") == "yes" else "shadow"
    judge_mode = state.get("judge_mode")
    if judge_mode not in ("live", "mock"):
        judge_mode = "mock" if os.environ.get("JUDGE_MOCK") == "1" else "live"
    fomo = state.get("fomo_health") or {}
    bearer_ok = bool(fomo.get("bearer_present"))
    last_start_iso = log_bits.get("last_cycle_start_iso")
    last_secs = log_bits.get("last_cycle_secs")
    if last_cycle and not last_start_iso:
        last_start_iso = last_cycle.get("ts_iso")
    next_in = None
    start_ts = _parse_iso(last_start_iso) if last_start_iso else None
    if start_ts is None and last_cycle and last_cycle.get("ts") is not None:
        start_ts = float(last_cycle["ts"])
    if start_ts is not None:
        try:
            import main as shift
            period = float(getattr(shift, "CYCLE_SECONDS", 900) or 900)
        except Exception:
            period = 900.0
        next_in = max(0.0, start_ts + period - now)
        if last_secs is None and last_cycle and last_cycle.get("ts") is not None:
            last_secs = max(0.0, float(last_cycle["ts"]) - start_ts)
    return {
        "desk": lc["desk"],
        "ops": lc["ops"],
        "mode": mode,
        "judge_mode": judge_mode,
        "last_cycle_start_iso": last_start_iso,
        "last_cycle_secs": last_secs,
        "next_cycle_in_s": next_in,
        "cycles_24h": cycles_24h,
        "no_trade_24h": no_trade_24h,
        "shadow_24h": shadow_24h,
        "tracebacks_24h": {
            "run_log": log_bits.get("tb_run") or 0,
            "uvicorn_log": log_bits.get("tb_uv") or 0,
        },
        "last_traceback": log_bits.get("last_tb"),
        "fomo_bearer_ok": bearer_ok,
        "fomo_auth_down": bool(fomo.get("auth_down")),
        "fomo_consecutive_auth_failures": int(fomo.get("consecutive_auth_failures") or 0),
        "soft_judged": (last_cycle.get("soft_judged", last_cycle.get("judged"))
                        if last_cycle else None),
        "pick_judged": (last_cycle.get("pick_judged") if last_cycle else None),
    }


def _parse_iso(text: str) -> float | None:
    if not text:
        return None
    raw = str(text).strip()
    utc = raw.endswith("Z")
    raw = raw.replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            parsed = time.strptime(raw, fmt)
        except ValueError:
            continue
        if utc:
            return calendar.timegm(parsed)
        return time.mktime(parsed)
    return None


def _scan_logs(now: float) -> dict:
    bits = {
        "tb_run": 0, "tb_uv": 0, "last_tb": None,
        "last_cycle_start_iso": None, "last_cycle_secs": None,
        "judge_hi": [],
    }
    try:
        logs = discover_logs()
        last_tb = None
        last_tb_ts = None
        for key, path in logs.items():
            text = read_log_tail(path)
            n, tb = scan_tracebacks(text, now)
            if key == "run_log":
                bits["tb_run"] = n
            else:
                bits["tb_uv"] = n
            if tb:
                ts = _parse_iso(tb.get("ts_iso") or "") if tb.get("ts_iso") else None
                if last_tb is None or (ts is not None and (last_tb_ts is None or ts >= last_tb_ts)):
                    last_tb = tb
                    last_tb_ts = ts
            if key == "run_log":
                bits.update(_scan_cycle_timing(text, now))
                bits["judge_hi"] = _judge_hi_from_log(text, now)
        bits["last_tb"] = last_tb
    except Exception as e:
        log.warning("ops summary logs: %s", safe_err(e))
    return bits


def _scan_cycle_timing(text: str, now: float) -> dict:
    start_iso = None
    last_secs = None
    last_ts = None
    for raw in (text or "").splitlines():
        ts = _parse_ts(raw)
        if ts is not None:
            last_ts = ts
        if CYCLE_START_RE.search(raw) and last_ts is not None:
            start_iso = _iso(last_ts)
        m = SLEEP_RE.search(raw)
        if m:
            try:
                sleep_s = float(m.group(1))
                period = float(m.group(2))
                last_secs = max(0.0, period - sleep_s)
            except (TypeError, ValueError):
                pass
    return {"last_cycle_start_iso": start_iso, "last_cycle_secs": last_secs}


def _judge_hi_from_log(text: str, now: float) -> list[dict]:
    cutoff = now - JUDGE_HI_DAYS * 86400
    out = []
    last_ts = None
    for raw in (text or "").splitlines():
        ts = _parse_ts(raw)
        if ts is not None:
            last_ts = ts
        m = JUDGE_LINE_RE.search(raw)
        if not m:
            continue
        if last_ts is not None and last_ts < cutoff:
            continue
        score = _num(m.group(4))
        if score is None or score < JUDGE_HI_MIN:
            continue
        tid = m.group(1)
        chain = None
        if ":" in tid:
            try:
                net = int(tid.rsplit(":", 1)[-1])
                chain = {1399811149: "solana", 56: "bsc", 8453: "base"}.get(net)
            except ValueError:
                chain = None
        out.append({
            "ts_iso": _iso(last_ts) if last_ts is not None else None,
            "ticker": m.group(2),
            "chain": chain,
            "score": score,
            "confidence": _num(m.group(5)),
            "verdict": m.group(3),
        })
    out.sort(key=lambda r: r.get("ts_iso") or "", reverse=True)
    return out[:JUDGE_HI_MAX]


def _cycle_health(now: float) -> tuple[dict | None, int, int, int, list[dict]]:
    last_cycle = None
    cycles_24h = 0
    no_trade_24h = 0
    shadow_24h = 0
    judge_hi: list[dict] = []
    try:
        import cycle_history
        db = cycle_history._connect()
        row = db.execute(
            "SELECT * FROM cycles ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row:
            last_cycle = cycle_history._cycle_dict(row)
        since = now - 86400
        cycles_24h = int(db.execute(
            "SELECT COUNT(*) FROM cycles WHERE ts >= ?", (since,),
        ).fetchone()[0] or 0)
        pick_cycles = db.execute(
            """SELECT DISTINCT cycle_id FROM tokens
               WHERE ts >= ? AND judge_score IS NOT NULL
                 AND lower(COALESCE(judge_verdict, '')) IN ('pick', 'yes')""",
            (since,),
        ).fetchall()
        shadow_24h = len(pick_cycles)
        if shadow_24h == 0:
            try:
                import shadow_ledger
                closed = shadow_ledger.closed_positions(limit=200)
                opens = shadow_ledger.open_positions()
                n = 0
                for p in list(closed) + list(opens):
                    ts = p.get("ts") or p.get("entry_ts")
                    if isinstance(ts, str):
                        tsv = _parse_iso(ts)
                    else:
                        tsv = _num(ts)
                    if tsv is not None and tsv >= since:
                        n += 1
                shadow_24h = n
            except Exception as e:
                log.warning("ops summary shadow_24h: %s", safe_err(e))
        no_trade_24h = max(0, cycles_24h - shadow_24h)
        since7 = now - JUDGE_HI_DAYS * 86400
        keys = {row[1] for row in db.execute("PRAGMA table_info(tokens)")}
        extra = ""
        if "dev_sold_pct" in keys:
            extra += ", dev_sold_pct"
        if "holder_source" in keys:
            extra += ", holder_source"
        rows = db.execute(
            f"""SELECT ts, ticker, chain, tid, judge_score, judge_confidence, judge_verdict,
                       holder_count{extra}
               FROM tokens
               WHERE judge_score IS NOT NULL AND judge_score >= ?
                 AND ts >= ?
               ORDER BY ts DESC LIMIT ?""",
            (JUDGE_HI_MIN, since7, JUDGE_HI_MAX),
        ).fetchall()
        for r in rows:
            keys_r = r.keys()
            holders = r["holder_count"] if "holder_count" in keys_r else None
            age_s = (now - float(r["ts"])) if r["ts"] is not None else None
            src = r["holder_source"] if "holder_source" in keys_r else None
            if not src and holders is not None:
                src = "trade snapshot"
            sold = r["dev_sold_pct"] if "dev_sold_pct" in keys_r else None
            judge_hi.append({
                "ts_iso": _iso(r["ts"]),
                "ticker": r["ticker"],
                "chain": r["chain"],
                "tid": r["tid"] if "tid" in keys_r else None,
                "score": float(r["judge_score"]),
                "confidence": _num(r["judge_confidence"]),
                "verdict": r["judge_verdict"] or "",
                "holder_count": holders,
                "holders_source": src,
                "holders_age_s": age_s,
                "dev_sold_pct": _num(sold),
            })
    except Exception as e:
        log.warning("ops summary cycles: %s", safe_err(e))
    return last_cycle, cycles_24h, no_trade_24h, shadow_24h, judge_hi


def _merge_judge_hi(from_db: list[dict], from_log: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in list(from_db) + list(from_log):
        key = (row.get("ts_iso"), row.get("ticker"), row.get("score"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    out.sort(key=lambda r: r.get("ts_iso") or "", reverse=True)
    return out[:JUDGE_HI_MAX]


def _outcomes(now: float) -> dict:
    payload = {
        "complete": 0, "pending_live": 0, "pending_backfill": 0, "dead": 0,
        "complete_24h": 0, "window": "01:00-06:00", "trickle": True, "paused": False,
    }
    try:
        import gt_api
        import outcomes
        raw = os.environ.get("OUTCOMES_WINDOW") or gt_api.DEFAULT_WINDOW
        payload["window"] = str(raw).strip() or gt_api.DEFAULT_WINDOW
        in_window = bool(gt_api.in_outcomes_window(now))
        payload["trickle"] = not in_window
        try:
            payload["paused"] = bool(outcomes.cycle_gt_paused(now))
        except Exception as e:
            log.warning("ops summary outcomes pause: %s", safe_err(e))
        db = outcomes.readonly_connect()
        try:
            for status, pri, n in db.execute(
                """SELECT status, COALESCE(priority, 0), COUNT(*)
                   FROM fetch_jobs GROUP BY 1, 2"""
            ):
                n = int(n or 0)
                if status == "complete":
                    payload["complete"] += n
                elif status == "dead":
                    payload["dead"] += n
                elif status == "pending":
                    if int(pri or 0) >= 1:
                        payload["pending_backfill"] += n
                    else:
                        payload["pending_live"] += n
            row = db.execute(
                """SELECT COUNT(*) FROM fetch_jobs
                   WHERE status = 'complete' AND last_attempt IS NOT NULL
                     AND last_attempt >= ?""",
                (now - 86400,),
            ).fetchone()
            payload["complete_24h"] = int(row[0] or 0) if row else 0
        finally:
            db.close()
    except Exception as e:
        log.warning("ops summary outcomes: %s", safe_err(e))
    return payload


def _cache_hit_rate(hits: int, misses: int):
    total = int(hits) + int(misses)
    if total <= 0:
        return None
    return round(int(hits) / total, 4)


def _budget(now: float) -> dict:
    payload = {
        "cg_plan": None,
        "cg_key_present": False,
        "cg_month_calls": 0,
        "cg_month_cap": 9000,
        "cg_daily_cap": 0,
        "cg_today": 0,
        "demo_active": False,
        "gt_calls_per_min": 5,
        "gt_429_24h": 0,
        "outcomes_spacing_s": 20.0,
        "gt_429_by_source": {"desk": 0, "desk_cycle": 0, "outcomes": 0},
        "gt_info_cache": {
            "hits": 0, "misses": 0, "hit_rate": None,
            "hits_24h": 0, "misses_24h": 0, "hit_rate_24h": None,
        },
    }
    try:
        import gt_api
        payload["cg_plan"] = gt_api.plan()
        payload["cg_key_present"] = bool(gt_api.coingecko_api_key())
        payload["cg_month_cap"] = int(gt_api.DEMO_MONTHLY_CAP)
        payload["demo_active"] = bool(gt_api.using_demo())
        try:
            import outcomes
            payload["cg_month_calls"] = int(outcomes.demo_calls_this_month(now))
            payload["cg_today"] = int(outcomes.demo_calls_today(now))
            payload["cg_daily_cap"] = int(outcomes.demo_daily_cap(now))
        except Exception as e:
            log.warning("ops summary demo budget: %s", safe_err(e))
        trickle = True
        try:
            trickle = not gt_api.in_outcomes_window(now)
        except Exception:
            pass
        payload["outcomes_spacing_s"] = float(gt_api.outcomes_spacing_sec(trickle=trickle))
        cpm = None
        try:
            import main as shift
            lim = getattr(shift, "_gt_limiter", None)
            if lim is not None and getattr(lim, "calls_per_min", None):
                cpm = int(lim.calls_per_min)
        except Exception:
            cpm = None
        if cpm is None:
            raw = os.environ.get("GT_CALLS_PER_MIN")
            try:
                cpm = int(raw) if raw else None
            except (TypeError, ValueError):
                cpm = None
        if cpm is None:
            cpm = (gt_api.PRO_GT_CALLS_PER_MIN if gt_api.using_pro()
                   else gt_api.FREE_GT_CALLS_PER_MIN)
        payload["gt_calls_per_min"] = int(cpm)
    except Exception as e:
        log.warning("ops summary budget: %s", safe_err(e))
    try:
        import cycle_history
        row = cycle_history._connect().execute(
            "SELECT COALESCE(SUM(gt_429), 0) FROM cycles WHERE ts >= ?",
            (now - 86400,),
        ).fetchone()
        payload["gt_429_24h"] = int(row[0] or 0) if row else 0
    except Exception as e:
        log.warning("ops summary gt_429: %s", safe_err(e))
    try:
        desk_429 = int(payload.get("gt_429_24h") or 0)
        desk_cycle = 0
        try:
            import main as shift
            lim = getattr(shift, "_gt_limiter", None)
            if lim is not None:
                desk_cycle = int(getattr(lim, "stats_429_count", 0) or 0)
        except Exception:
            pass
        outcomes_429 = 0
        try:
            import outcomes
            outcomes_429 = int(outcomes.outcomes_429_count())
        except Exception:
            pass
        try:
            import gt_api
            counted = gt_api.gt_429_by_source()
            outcomes_429 = max(outcomes_429, int(counted.get("outcomes") or 0))
        except Exception:
            pass
        payload["gt_429_by_source"] = {
            "desk": desk_429, "desk_cycle": desk_cycle, "outcomes": outcomes_429,
        }
    except Exception as e:
        log.warning("ops summary 429 by source: %s", safe_err(e))
    try:
        hits = misses = 0
        try:
            from collect import gt_info_cycle_stats
            info = gt_info_cycle_stats()
            hits = int(info.get("hits") or 0)
            misses = int(info.get("misses") or 0)
        except Exception:
            pass
        hits_24h = misses_24h = 0
        try:
            import cycle_history
            row = cycle_history._connect().execute(
                "SELECT COALESCE(SUM(gt_cache_hits), 0), "
                "COALESCE(SUM(gt_cache_misses), 0) FROM cycles WHERE ts >= ?",
                (now - 86400,),
            ).fetchone()
            if row:
                hits_24h = int(row[0] or 0)
                misses_24h = int(row[1] or 0)
        except Exception as e:
            log.warning("ops summary gt info cache 24h: %s", safe_err(e))
        payload["gt_info_cache"] = {
            "hits": hits,
            "misses": misses,
            "hit_rate": _cache_hit_rate(hits, misses),
            "hits_24h": hits_24h,
            "misses_24h": misses_24h,
            "hit_rate_24h": _cache_hit_rate(hits_24h, misses_24h),
        }
    except Exception as e:
        log.warning("ops summary gt info cache: %s", safe_err(e))
    return payload


def _regime() -> dict:
    payload = {
        "tag": None, "ts_iso": None, "reason": None,
        "long_liq_usd": None, "short_liq_usd": None,
        "majors_funding": None, "memes_funding": None,
        "oi_change": None, "history": [],
    }
    try:
        import cycle_history
        db = cycle_history._connect()
        rows = db.execute(
            "SELECT ts, ts_iso, regime, metrics_json FROM market_regimes "
            "ORDER BY ts DESC LIMIT ?",
            (REGIME_HISTORY,),
        ).fetchall()
        if not rows:
            try:
                import coinalyze
                tag = coinalyze.last_regime()
                if tag:
                    payload["tag"] = tag
                snap = coinalyze.last_snapshot()
                if snap:
                    payload["ts_iso"] = _iso(snap.get("ts")) if snap.get("ts") else None
                    metrics = snap.get("metrics") or {}
                    _fill_regime_metrics(payload, metrics)
                    if snap.get("regime"):
                        payload["tag"] = snap.get("regime")
            except Exception as e:
                log.warning("ops summary coinalyze snap: %s", safe_err(e))
            return payload
        latest = rows[0]
        payload["tag"] = latest["regime"]
        payload["ts_iso"] = latest["ts_iso"]
        try:
            import json
            metrics = json.loads(latest["metrics_json"] or "{}")
        except Exception:
            metrics = {}
        _fill_regime_metrics(payload, metrics)
        hist = []
        for r in reversed(rows):
            hist.append([r["ts_iso"], r["regime"]])
        payload["history"] = hist
    except Exception as e:
        log.warning("ops summary regime: %s", safe_err(e))
    return payload


def _fill_regime_metrics(payload: dict, metrics: dict) -> None:
    if not isinstance(metrics, dict):
        return
    agg = metrics.get("aggregates") if isinstance(metrics.get("aggregates"), dict) else {}
    src = {**agg, **metrics}
    payload["reason"] = metrics.get("reason") or agg.get("reason")
    payload["long_liq_usd"] = _num(src.get("long_liq_usd"))
    payload["short_liq_usd"] = _num(src.get("short_liq_usd"))
    payload["majors_funding"] = _num(
        src.get("majors_funding") or src.get("majors_funding_median")
    )
    payload["memes_funding"] = _num(
        src.get("memes_funding") or src.get("memes_funding_median")
    )
    payload["oi_change"] = _num(
        src.get("oi_change") or src.get("oi_change_median") or src.get("oi_change_pct")
    )


def _young_trail_curve() -> list:
    try:
        import young_trail
        rows = young_trail.closed_rows(limit=None)
        rows = sorted(
            (r for r in rows if r.get("exit_ts") is not None),
            key=lambda r: float(r["exit_ts"]),
        )
        curve = []
        total = 0.0
        for r in rows:
            total += float(r.get("pnl_usd") or 0.0)
            curve.append([float(r["exit_ts"]), round(total, 2)])
        return curve
    except Exception as e:
        log.warning("ops summary young_trail curve: %s", safe_err(e))
        return []


def _empty_payload(now: float, extra: dict | None = None) -> dict:
    out = {
        "generated_at_iso": _iso(now),
        "health": {
            "desk": {"loaded": False, "pid": None},
            "ops": {"loaded": False, "pid": None},
            "mode": "shadow",
            "judge_mode": "live",
            "last_cycle_start_iso": None,
            "last_cycle_secs": None,
            "next_cycle_in_s": None,
            "cycles_24h": 0,
            "no_trade_24h": 0,
            "shadow_24h": 0,
            "tracebacks_24h": {"run_log": 0, "uvicorn_log": 0},
            "last_traceback": None,
            "fomo_bearer_ok": False,
            "fomo_auth_down": False,
            "fomo_consecutive_auth_failures": 0,
            "soft_judged": None,
            "pick_judged": None,
        },
        "judge_hi": [],
        "outcomes": {
            "complete": 0, "pending_live": 0, "pending_backfill": 0, "dead": 0,
            "complete_24h": 0, "window": "01:00-06:00", "trickle": True, "paused": False,
        },
        "budget": {
            "cg_plan": None, "cg_key_present": False,
            "cg_month_calls": 0, "cg_month_cap": 9000,
            "cg_daily_cap": 0, "cg_today": 0, "demo_active": False,
            "gt_calls_per_min": 5, "gt_429_24h": 0, "outcomes_spacing_s": 20.0,
            "gt_429_by_source": {"desk": 0, "desk_cycle": 0, "outcomes": 0},
            "gt_info_cache": {
                "hits": 0, "misses": 0, "hit_rate": None,
                "hits_24h": 0, "misses_24h": 0, "hit_rate_24h": None,
            },
        },
        "regime": {
            "tag": None, "ts_iso": None, "reason": None,
            "long_liq_usd": None, "short_liq_usd": None,
            "majors_funding": None, "memes_funding": None,
            "oi_change": None, "history": [],
        },
        "young_trail_curve": [],
    }
    if extra:
        out.update(extra)
    return out


def build_summary(now: float | None = None) -> dict:
    """Assemble the /ops/summary body. Each section fails open."""
    now = time.time() if now is None else float(now)
    try:
        log_bits = _scan_logs(now)
        last_cycle, cycles_24h, no_trade_24h, shadow_24h, judge_db = _cycle_health(now)
        payload = {
            "generated_at_iso": _iso(now),
            "health": _health(
                now, cycles_24h, no_trade_24h, shadow_24h, last_cycle, log_bits,
            ),
            "judge_hi": _merge_judge_hi(judge_db, log_bits.get("judge_hi") or []),
            "outcomes": _outcomes(now),
            "budget": _budget(now),
            "regime": _regime(),
            "young_trail_curve": _young_trail_curve(),
        }
        return payload
    except Exception as e:
        log.warning("ops summary: %s", safe_err(e))
        return _empty_payload(now, {"error": "summary unavailable"})


def summary_payload(now: float | None = None, *, force: bool = False) -> dict:
    """Cached 30s in-process. `force` bypasses the cache (tests)."""
    wall = time.time()
    if not force and _cache["payload"] is not None and (wall - _cache["ts"]) < CACHE_TTL_SEC:
        return _cache["payload"]
    payload = build_summary(now)
    _cache["ts"] = wall
    _cache["payload"] = payload
    return payload
