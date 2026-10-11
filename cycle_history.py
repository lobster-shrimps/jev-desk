"""
Persistent cycle-history store and morning briefing.

SQLite lives under outbox/ (default outbox/cycle_history.db). Capture hooks sit
next to the existing `cycle:` and `soft tid=` log lines in main.py; those log
lines are not changed. Shadow-only: this module never takes the book, never
prints .env, and routes exception text through safe_err.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import pathlib
import re
import sqlite3
import time
from typing import Callable, Iterable

from secret_utils import safe_err

log = logging.getLogger("cycle_history")

# Network ids the desk actually sees. 8453 is Base (CHAIN_SET still maps it to
# the bsc judge set on purpose; history records the real chain, not the seat).
CHAIN_NAMES = {
    1399811149: "solana",
    56: "bsc",
    8453: "base",
    4663: "robinhood",
    143: "monad",
}
SOLANA_ID = 1399811149

GT_DEFER_REASONS = frozenset({
    "requeued",
    "requeued_after_retry",
    "requeued_429_backoff",
})

CYCLE_SECONDS = 900
BRIEFING_HOUR_LOCAL = 7
TREND_DAYS = 7
TREND_MIN_PRIOR_DAYS = 3
MOMENTUM_KEY = "momentum_already_spent"
NON_KILL_REASONS = frozenset({"pass", "free_passed"})
CYCLE_DEDUP_SEC = 1.5

# Notable-change gates for the trends section (relative or absolute).
NOTABLE_JUDGED_REL = 0.30
NOTABLE_MOMENTUM_ABS = 0.10
NOTABLE_YOUNG_RATE_ABS = 0.10
NOTABLE_GT_DEFERS_REL = 0.50
NOTABLE_MIX_PP = 10.0
NOTABLE_CHAIN_PP = 10.0

_pending_soft: list[dict] = []
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None


def outbox_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))


def db_path() -> pathlib.Path:
    override = os.environ.get("CYCLE_HISTORY_DB")
    if override:
        return pathlib.Path(override)
    return outbox_dir() / "cycle_history.db"


def briefings_dir() -> pathlib.Path:
    return outbox_dir() / "briefings"


def reset() -> None:
    """Close the cached connection and drop in-memory soft-token buffer."""
    global _conn, _conn_path
    _pending_soft.clear()
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None
    _conn_path = None
    try:
        import coinalyze
        coinalyze.reset(hooks=False)
    except Exception:
        pass


def _connect() -> sqlite3.Connection:
    global _conn, _conn_path
    path = str(db_path())
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _init_schema(_conn)
    _conn_path = path
    return _conn


def _init_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS cycles (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts REAL NOT NULL,
      ts_iso TEXT NOT NULL,
      mode TEXT NOT NULL,
      seen INTEGER NOT NULL DEFAULT 0,
      benched INTEGER NOT NULL DEFAULT 0,
      judged INTEGER NOT NULL DEFAULT 0,
      requeued INTEGER NOT NULL DEFAULT 0,
      free_json TEXT NOT NULL DEFAULT '{}',
      trade_json TEXT NOT NULL DEFAULT '{}',
      chain_json TEXT NOT NULL DEFAULT '{}',
      soft_json TEXT NOT NULL DEFAULT '{}',
      carry INTEGER NOT NULL DEFAULT 0,
      unevaluated INTEGER NOT NULL DEFAULT 0,
      gt_429 INTEGER NOT NULL DEFAULT 0,
      gt_defer INTEGER NOT NULL DEFAULT 0,
      gt_cache_hits INTEGER NOT NULL DEFAULT 0,
      gt_cache_misses INTEGER NOT NULL DEFAULT 0,
      gt_dossier_attempts INTEGER NOT NULL DEFAULT 0,
      gt_dossier_ok INTEGER NOT NULL DEFAULT 0,
      source TEXT NOT NULL DEFAULT 'live'
    );
    CREATE INDEX IF NOT EXISTS idx_cycles_ts ON cycles(ts);

    CREATE TABLE IF NOT EXISTS tokens (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      cycle_id INTEGER,
      ts REAL NOT NULL,
      kind TEXT NOT NULL,
      tid TEXT,
      chain_id INTEGER,
      chain TEXT,
      ticker TEXT,
      reason TEXT,
      noul REAL,
      age_minutes REAL,
      top_10_percent REAL,
      top_wallet_percent REAL,
      developer_holding_percentage REAL,
      holder_count INTEGER,
      soft_scores_json TEXT,
      judge_verdict TEXT,
      judge_reason TEXT,
      judge_score REAL,
      judge_confidence REAL,
      FOREIGN KEY (cycle_id) REFERENCES cycles(id)
    );
    CREATE INDEX IF NOT EXISTS idx_tokens_ts ON tokens(ts);
    CREATE INDEX IF NOT EXISTS idx_tokens_kind ON tokens(kind);
    CREATE INDEX IF NOT EXISTS idx_tokens_chain ON tokens(chain_id);

    CREATE TABLE IF NOT EXISTS young_free_pass (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      cycle_id INTEGER,
      ts REAL NOT NULL,
      tid TEXT,
      chain_id INTEGER,
      chain TEXT,
      ticker TEXT,
      age_minutes REAL,
      outcome TEXT NOT NULL,
      reason TEXT,
      FOREIGN KEY (cycle_id) REFERENCES cycles(id)
    );
    CREATE INDEX IF NOT EXISTS idx_yfp_ts ON young_free_pass(ts);

    CREATE TABLE IF NOT EXISTS briefings (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      date_local TEXT NOT NULL UNIQUE,
      generated_at REAL NOT NULL,
      generated_at_iso TEXT NOT NULL,
      markdown TEXT NOT NULL,
      payload_json TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS market_regimes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      cycle_id INTEGER,
      ts REAL NOT NULL,
      ts_iso TEXT NOT NULL,
      regime TEXT NOT NULL,
      metrics_json TEXT NOT NULL,
      symbols_json TEXT NOT NULL,
      FOREIGN KEY (cycle_id) REFERENCES cycles(id)
    );
    CREATE INDEX IF NOT EXISTS idx_regimes_ts ON market_regimes(ts);
    CREATE INDEX IF NOT EXISTS idx_regimes_cycle ON market_regimes(cycle_id);

    CREATE TABLE IF NOT EXISTS coinalyze_symbol_cache (
      ticker TEXT PRIMARY KEY,
      symbol TEXT NOT NULL,
      exchange TEXT,
      resolved_at REAL NOT NULL
    );
    """)
    cols = {row[1] for row in db.execute("PRAGMA table_info(cycles)")}
    if "ts_sec" not in cols:
        db.execute("ALTER TABLE cycles ADD COLUMN ts_sec INTEGER")
        db.execute("UPDATE cycles SET ts_sec = CAST(ts AS INTEGER) WHERE ts_sec IS NULL")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cycles_ts_sec ON cycles(ts_sec)")
    for name, decl in (
        ("gt_cache_hits", "INTEGER NOT NULL DEFAULT 0"),
        ("gt_cache_misses", "INTEGER NOT NULL DEFAULT 0"),
        ("gt_dossier_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("gt_dossier_ok", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in cols:
            db.execute(f"ALTER TABLE cycles ADD COLUMN {name} {decl}")
    token_cols = {row[1] for row in db.execute("PRAGMA table_info(tokens)")}
    for name, decl in (
        ("judge_verdict", "TEXT"),
        ("judge_reason", "TEXT"),
        ("judge_score", "REAL"),
        ("judge_confidence", "REAL"),
    ):
        if name not in token_cols:
            db.execute(f"ALTER TABLE tokens ADD COLUMN {name} {decl}")
    db.commit()


def chain_id_of(tid=None, net=None) -> int | None:
    if net is not None:
        try:
            return int(net)
        except (TypeError, ValueError):
            pass
    if tid and ":" in str(tid):
        tail = str(tid).rsplit(":", 1)[-1]
        try:
            return int(tail)
        except ValueError:
            return None
    return None


def chain_name(net=None, tid=None, fallback=None) -> str | None:
    cid = chain_id_of(tid=tid, net=net)
    if cid in CHAIN_NAMES:
        return CHAIN_NAMES[cid]
    return fallback


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _local_date(ts: float, localtime: Callable | None = None) -> str:
    lt = (localtime or time.localtime)(ts)
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


def _local_hour(ts: float, localtime: Callable | None = None) -> int:
    return (localtime or time.localtime)(ts).tm_hour


def _json(obj) -> str:
    return json.dumps(obj if obj is not None else {}, default=str, sort_keys=True)


def _loads(text, default=None):
    if not text:
        return {} if default is None else default
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {} if default is None else default


def _num(value):
    if value is None or value == "" or value == "None":
        return None
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, str) and "." not in value and value.lstrip("-").isdigit():
            return int(value)
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    n = _num(value)
    if n is None:
        return None
    return int(n)


def _median(values: list[float]) -> float | None:
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return None
    nums.sort()
    n = len(nums)
    mid = n // 2
    if n % 2:
        return nums[mid]
    return (nums[mid - 1] + nums[mid]) / 2.0


def _fmt_mom(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):.3f}"


def _fmt_mom_map(mapping) -> str:
    if not mapping:
        return "—"
    parts = [f"{name} {_fmt_mom(val)}" for name, val in sorted(mapping.items())]
    return ", ".join(parts)


def _kill_counts(d: dict | None) -> dict:
    """Kill-reason histogram without pass / other non-kill keys."""
    out = {}
    for k, v in (d or {}).items():
        if k in NON_KILL_REASONS or not isinstance(v, (int, float)):
            continue
        out[str(k)] = int(v)
    return out


def _sum_counts(d: dict | None) -> int:
    return int(sum(_kill_counts(d).values()))


def infer_gt_counts(stats: dict) -> tuple[int, int]:
    """gt_429 is explicit (limiter / young-429 lines). gt_defer is separate."""
    tokens = stats.get("tokens") or []
    inferred_defer = 0
    for row in tokens:
        reason = row.get("reason") or ""
        if reason in GT_DEFER_REASONS or "429" in str(reason):
            inferred_defer += 1
    gt_429 = stats.get("gt_429")
    gt_defer = stats.get("gt_defer")
    if gt_429 is None:
        gt_429 = 0
    if gt_defer is None:
        gt_defer = inferred_defer
    return int(gt_429), int(gt_defer)


def cycle_exists(ts: float) -> bool:
    """True if a live or backfill cycle is already stored at this timestamp."""
    try:
        row = _connect().execute(
            "SELECT 1 FROM cycles WHERE ts_sec = ? OR ABS(ts - ?) < ? LIMIT 1",
            (int(ts), float(ts), CYCLE_DEDUP_SEC),
        ).fetchone()
        return bool(row)
    except Exception as e:
        log.warning("cycle exists check failed: %s", safe_err(e))
        return False


def classify_young_outcome(row: dict | None) -> tuple[str, str | None]:
    """Map a later funnel row to deferred_429 / chain / soft / judged / …"""
    if not row:
        return "unevaluated", None
    reason = row.get("reason")
    stage = row.get("stage")
    if reason in GT_DEFER_REASONS or (reason and "429" in str(reason)):
        return "deferred_429", reason
    if stage == "chain":
        return "chain", reason
    if stage == "soft":
        return "soft", reason
    if stage == "judged":
        return "judged", reason
    if stage == "judge_error":
        return "judge_error", reason
    if stage == "trade":
        return "trade", reason
    if stage:
        return str(stage), reason
    return "other", reason


def on_soft_token(
    *,
    tid=None,
    ticker=None,
    net=None,
    chain=None,
    reason=None,
    noul=None,
    age_minutes=None,
    top_10_percent=None,
    top_wallet_percent=None,
    developer_holding_percentage=None,
    holder_count=None,
    soft_scores=None,
    ts=None,
) -> None:
    """Buffer one soft-killed token. Flushed by on_cycle. Hook next to `soft tid=`."""
    cid = chain_id_of(tid=tid, net=net)
    _pending_soft.append({
        "ts": float(ts) if ts is not None else time.time(),
        "kind": "soft",
        "tid": tid,
        "chain_id": cid,
        "chain": chain_name(net=cid, tid=tid, fallback=chain),
        "ticker": ticker,
        "reason": reason,
        "noul": _num(noul),
        "age_minutes": _num(age_minutes),
        "top_10_percent": _num(top_10_percent),
        "top_wallet_percent": _num(top_wallet_percent),
        "developer_holding_percentage": _num(developer_holding_percentage),
        "holder_count": _int(holder_count),
        "soft_scores": soft_scores or {},
    })


def on_cycle(stats: dict, *, shadow: bool = True, now: float | None = None,
             localtime: Callable | None = None, source: str = "live") -> int | None:
    """Persist one cycle plus judged/soft/young-free rows. Hook next to `cycle:`."""
    if not stats or "held" in stats:
        _pending_soft.clear()
        return None
    now = time.time() if now is None else float(now)
    mode = "shadow" if shadow else "live"
    if cycle_exists(now):
        log.info("cycle history: skip duplicate ts=%.3f source=%s", now, source)
        _pending_soft.clear()
        return None
    gt_429, gt_defer = infer_gt_counts(stats)
    tokens = list(stats.get("tokens") or [])
    try:
        db = _connect()
        cur = db.execute(
            """INSERT OR IGNORE INTO cycles (
                 ts, ts_iso, mode, seen, benched, judged, requeued,
                 free_json, trade_json, chain_json, soft_json,
                 carry, unevaluated, gt_429, gt_defer,
                 gt_cache_hits, gt_cache_misses, gt_dossier_attempts, gt_dossier_ok,
                 source, ts_sec
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now, _iso(now), mode,
                int(stats.get("seen") or 0),
                int(stats.get("benched") or 0),
                int(stats.get("judged") or 0),
                int(stats.get("requeued") or 0),
                _json(stats.get("free") or {}),
                _json(stats.get("trade") or {}),
                _json(stats.get("chain") or {}),
                _json(stats.get("soft") or {}),
                int(stats.get("carry") or 0),
                int(stats.get("unevaluated") or 0),
                gt_429, gt_defer,
                int(stats.get("gt_cache_hits") or 0),
                int(stats.get("gt_cache_misses") or 0),
                int(stats.get("gt_dossier_attempts") or 0),
                int(stats.get("gt_dossier_ok") or 0),
                source, int(now),
            ),
        )
        if cur.rowcount == 0 or cur.lastrowid == 0:
            log.info("cycle history: skip duplicate ts=%.3f source=%s", now, source)
            _pending_soft.clear()
            return None
        cycle_id = int(cur.lastrowid)
        written_tids = set()
        for row in _pending_soft:
            _insert_token(db, cycle_id, row)
            if row.get("tid"):
                written_tids.add(row["tid"])
        for row in tokens:
            stage = row.get("stage")
            if stage == "soft" and row.get("tid") in written_tids:
                continue
            if stage not in ("judged", "soft", "judge_error"):
                continue
            _insert_token(db, cycle_id, _token_from_stats_row(row, now, stage))
        later_by_tid = {r.get("tid"): r for r in tokens if r.get("tid")}
        for y in stats.get("young_free") or []:
            later = later_by_tid.get(y.get("tid"))
            outcome, reason = classify_young_outcome(later)
            cid = chain_id_of(tid=y.get("tid"), net=y.get("net"))
            db.execute(
                """INSERT INTO young_free_pass (
                     cycle_id, ts, tid, chain_id, chain, ticker,
                     age_minutes, outcome, reason
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    cycle_id, now, y.get("tid"), cid,
                    chain_name(net=cid, tid=y.get("tid"), fallback=y.get("chain")),
                    y.get("ticker"), _num(y.get("age_minutes")),
                    outcome, reason,
                ),
            )
        db.commit()
    except Exception as e:
        log.warning("cycle history write failed: %s", safe_err(e))
        _pending_soft.clear()
        return None
    _pending_soft.clear()
    if source != "backfill":
        try:
            _store_regime(cycle_id, now)
        except Exception as e:
            log.info("coinalyze: skipped (%s)", safe_err(e))
        try:
            maybe_persist_briefing(now=now, localtime=localtime)
        except Exception as e:
            log.warning("morning briefing failed: %s", safe_err(e))
    return cycle_id


def _token_from_stats_row(row: dict, now: float, kind: str) -> dict:
    cid = chain_id_of(tid=row.get("tid"), net=row.get("net"))
    return {
        "ts": now,
        "kind": kind,
        "tid": row.get("tid"),
        "chain_id": cid,
        "chain": chain_name(net=cid, tid=row.get("tid"), fallback=row.get("chain")),
        "ticker": row.get("ticker"),
        "reason": row.get("reason"),
        "noul": _num(row.get("soft_noul")),
        "age_minutes": _num(row.get("age_minutes")),
        "top_10_percent": _num(row.get("top_10_percent")),
        "top_wallet_percent": _num(row.get("top_wallet_percent")),
        "developer_holding_percentage": _num(row.get("developer_holding_percentage")),
        "holder_count": _int(row.get("holder_count")),
        "soft_scores": row.get("soft_scores") or {},
        "judge_verdict": row.get("judge_verdict"),
        "judge_reason": row.get("judge_reason"),
        "judge_score": _num(row.get("judge_score")),
        "judge_confidence": _num(row.get("judge_confidence")),
    }


def _insert_token(db: sqlite3.Connection, cycle_id: int, row: dict) -> None:
    scores = row.get("soft_scores") or {}
    db.execute(
        """INSERT INTO tokens (
             cycle_id, ts, kind, tid, chain_id, chain, ticker, reason, noul,
             age_minutes, top_10_percent, top_wallet_percent,
             developer_holding_percentage, holder_count, soft_scores_json,
             judge_verdict, judge_reason, judge_score, judge_confidence
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            cycle_id, row.get("ts"), row.get("kind"), row.get("tid"),
            row.get("chain_id"), row.get("chain"), row.get("ticker"),
            row.get("reason"), row.get("noul"), row.get("age_minutes"),
            row.get("top_10_percent"), row.get("top_wallet_percent"),
            row.get("developer_holding_percentage"), row.get("holder_count"),
            _json(scores),
            row.get("judge_verdict"), row.get("judge_reason"),
            _num(row.get("judge_score")), _num(row.get("judge_confidence")),
        ),
    )


def _load_symbol_cache() -> dict:
    out = {}
    try:
        for r in _connect().execute(
            "SELECT ticker, symbol, exchange, resolved_at FROM coinalyze_symbol_cache"
        ):
            out[r["ticker"]] = {
                "symbol": r["symbol"],
                "exchange": r["exchange"],
                "resolved_at": r["resolved_at"],
            }
    except Exception:
        return {}
    return out


def _save_symbol_cache(cache: dict) -> None:
    if not cache:
        return
    try:
        db = _connect()
        for ticker, row in cache.items():
            if not isinstance(row, dict) or not row.get("symbol"):
                continue
            db.execute(
                """INSERT OR REPLACE INTO coinalyze_symbol_cache
                   (ticker, symbol, exchange, resolved_at) VALUES (?,?,?,?)""",
                (
                    str(ticker), str(row["symbol"]), row.get("exchange"),
                    float(row.get("resolved_at") or time.time()),
                ),
            )
        db.commit()
    except Exception as e:
        log.info("coinalyze: skipped (%s)", safe_err(e))


def _store_regime(cycle_id: int, now: float) -> None:
    """LOG-ONLY. Capture and persist a Coinalyze snapshot. Never raises out."""
    import coinalyze
    snap = coinalyze.maybe_capture(now=now, cache=_load_symbol_cache())
    if not snap:
        return
    try:
        _save_symbol_cache(snap.get("symbols") or {})
        db = _connect()
        db.execute(
            """INSERT INTO market_regimes (
                 cycle_id, ts, ts_iso, regime, metrics_json, symbols_json
               ) VALUES (?,?,?,?,?,?)""",
            (
                cycle_id, float(snap.get("ts") or now), _iso(float(snap.get("ts") or now)),
                snap.get("regime") or "neutral",
                _json(snap.get("metrics") or {}),
                _json(snap.get("symbols") or {}),
            ),
        )
        db.commit()
    except Exception as e:
        log.info("coinalyze: skipped (%s)", safe_err(e))


def _regime_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "cycle_id": r["cycle_id"],
        "ts": r["ts"],
        "ts_iso": r["ts_iso"],
        "regime": r["regime"],
        "metrics": _loads(r["metrics_json"]),
        "symbols": _loads(r["symbols_json"]),
    }


def _regimes_since(since_ts: float, until_ts: float | None = None) -> list[dict]:
    try:
        if until_ts is None:
            rows = _rows("SELECT * FROM market_regimes WHERE ts >= ? ORDER BY ts", (since_ts,))
        else:
            rows = _rows(
                "SELECT * FROM market_regimes WHERE ts >= ? AND ts <= ? ORDER BY ts",
                (since_ts, until_ts),
            )
        return [_regime_dict(r) for r in rows]
    except Exception:
        return []


def _regime_window(since_ts: float, until_ts: float) -> dict:
    rows = _regimes_since(since_ts, until_ts)
    counts: dict[str, int] = {}
    for row in rows:
        tag = row.get("regime") or "neutral"
        counts[tag] = counts.get(tag, 0) + 1
    latest = rows[-1] if rows else None
    return {
        "latest": latest,
        "tag": None if latest is None else latest.get("regime"),
        "counts": counts,
        "n": len(rows),
    }


def _rows(sql: str, params=()) -> list[sqlite3.Row]:
    return _connect().execute(sql, params).fetchall()


def cycles_since(since_ts: float, until_ts: float | None = None) -> list[dict]:
    if until_ts is None:
        rows = _rows("SELECT * FROM cycles WHERE ts >= ? ORDER BY ts", (since_ts,))
    else:
        rows = _rows(
            "SELECT * FROM cycles WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (since_ts, until_ts),
        )
    return [_cycle_dict(r) for r in rows]


def _cycle_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "ts": r["ts"],
        "ts_iso": r["ts_iso"],
        "mode": r["mode"],
        "seen": r["seen"],
        "benched": r["benched"],
        "judged": r["judged"],
        "requeued": r["requeued"],
        "free": _loads(r["free_json"]),
        "trade": _loads(r["trade_json"]),
        "chain": _loads(r["chain_json"]),
        "soft": _loads(r["soft_json"]),
        "carry": r["carry"],
        "unevaluated": r["unevaluated"],
        "gt_429": r["gt_429"],
        "gt_defer": r["gt_defer"],
        "gt_cache_hits": r["gt_cache_hits"] if "gt_cache_hits" in r.keys() else 0,
        "gt_cache_misses": r["gt_cache_misses"] if "gt_cache_misses" in r.keys() else 0,
        "gt_dossier_attempts": r["gt_dossier_attempts"] if "gt_dossier_attempts" in r.keys() else 0,
        "gt_dossier_ok": r["gt_dossier_ok"] if "gt_dossier_ok" in r.keys() else 0,
        "source": r["source"],
    }


def _tokens_since(since_ts: float, kind: str | None = None,
                  until_ts: float | None = None) -> list[dict]:
    sql = "SELECT * FROM tokens WHERE ts >= ?"
    params: list = [since_ts]
    if until_ts is not None:
        sql += " AND ts <= ?"
        params.append(until_ts)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY ts"
    return [_token_dict(r) for r in _rows(sql, params)]


def _token_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "cycle_id": r["cycle_id"],
        "ts": r["ts"],
        "kind": r["kind"],
        "tid": r["tid"],
        "chain_id": r["chain_id"],
        "chain": r["chain"],
        "ticker": r["ticker"],
        "reason": r["reason"],
        "noul": r["noul"],
        "age_minutes": r["age_minutes"],
        "top_10_percent": r["top_10_percent"],
        "top_wallet_percent": r["top_wallet_percent"],
        "developer_holding_percentage": r["developer_holding_percentage"],
        "holder_count": r["holder_count"],
        "soft_scores": _loads(r["soft_scores_json"]),
        "judge_verdict": r["judge_verdict"] if "judge_verdict" in r.keys() else None,
        "judge_reason": r["judge_reason"] if "judge_reason" in r.keys() else None,
        "judge_score": r["judge_score"] if "judge_score" in r.keys() else None,
        "judge_confidence": r["judge_confidence"] if "judge_confidence" in r.keys() else None,
    }


def _young_since(since_ts: float, until_ts: float | None = None) -> list[dict]:
    if until_ts is None:
        rows = _rows("SELECT * FROM young_free_pass WHERE ts >= ? ORDER BY ts", (since_ts,))
    else:
        rows = _rows(
            "SELECT * FROM young_free_pass WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (since_ts, until_ts),
        )
    return [{
        "id": r["id"],
        "cycle_id": r["cycle_id"],
        "ts": r["ts"],
        "tid": r["tid"],
        "chain_id": r["chain_id"],
        "chain": r["chain"],
        "ticker": r["ticker"],
        "age_minutes": r["age_minutes"],
        "outcome": r["outcome"],
        "reason": r["reason"],
    } for r in rows]


def _merge_counts(*dicts: dict) -> dict:
    out: dict[str, int] = {}
    for d in dicts:
        for k, v in (d or {}).items():
            if isinstance(v, (int, float)):
                out[k] = out.get(k, 0) + int(v)
    return out


def _kill_mix(free, trade, chain, soft) -> dict:
    parts = {
        "free": _sum_counts(_kill_counts(free)),
        "trade": _sum_counts(_kill_counts(trade)),
        "chain": _sum_counts(_kill_counts(chain)),
        "soft": _sum_counts(_kill_counts(soft)),
    }
    total = sum(parts.values())
    shares = {k: (v / total * 100.0 if total else 0.0) for k, v in parts.items()}
    return {"counts": parts, "total": total, "shares": shares}


def _momentum(token: dict) -> float | None:
    scores = token.get("soft_scores") or {}
    if MOMENTUM_KEY in scores:
        return _num(scores.get(MOMENTUM_KEY))
    if token.get("kind") == "soft" and token.get("reason") == MOMENTUM_KEY:
        return _num(token.get("noul"))
    return None


def _per_chain(tokens: list[dict], young: list[dict], cycles: list[dict]) -> dict:
    """Counts grouped by chain name, plus a solana-only slice."""
    empty = {
        "judged": 0, "soft": 0, "chain_kills": 0, "young_free": 0,
        "young_outcomes": {}, "tokens": [],
    }
    by: dict[str, dict] = {}

    def bucket(name: str | None) -> dict:
        key = name or "unknown"
        if key not in by:
            by[key] = {k: ({} if k == "young_outcomes" else ([] if k == "tokens" else 0))
                       for k in empty}
            by[key]["tokens"] = []
            by[key]["young_outcomes"] = {}
        return by[key]

    for t in tokens:
        b = bucket(t.get("chain"))
        if t.get("kind") == "judged":
            b["judged"] += 1
            b["tokens"].append(t)
        elif t.get("kind") == "soft":
            b["soft"] += 1
    # Chain-kill totals live on cycle histograms (not per-chain). Approximate
    # per-chain chain kills from young-free outcomes tagged `chain`.
    for y in young:
        b = bucket(y.get("chain"))
        b["young_free"] += 1
        oc = y.get("outcome") or "other"
        b["young_outcomes"][oc] = b["young_outcomes"].get(oc, 0) + 1
        if oc == "chain":
            b["chain_kills"] += 1

    solana = by.get("solana", {k: ({} if k == "young_outcomes" else ([] if k == "tokens" else 0))
                               for k in empty})
    if "tokens" not in solana:
        solana = dict(empty)
        solana["tokens"] = []
        solana["young_outcomes"] = {}
    total_judged = sum(v["judged"] for v in by.values()) or 0
    share = {name: (v["judged"] / total_judged * 100.0 if total_judged else 0.0)
             for name, v in by.items()}
    return {"by_chain": by, "solana": solana, "judged_share": share}


def _shadow_window(since_ts: float, now: float) -> dict:
    """Picks / fills / shadow P&L for the window. Picks are not fills."""
    empty = {
        "picks": 0,
        "fills": 0,
        "fills_note": "shadow only — picks are not fills",
        "open_positions": 0,
        "closed_trades": 0,
        "realized_pnl_usd": 0.0,
        "win_rate": 0.0,
        "winning_trades": 0,
        "losing_trades": 0,
    }
    try:
        import shadow_ledger
        ledger = pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox")) / "shadow_ledger.jsonl"
        if os.environ.get("SHADOW_LEDGER"):
            ledger = pathlib.Path(os.environ.get("SHADOW_LEDGER"))
        # Prefer the module path when the test/runtime already pointed it.
        if getattr(shadow_ledger, "LEDGER_PATH", None) and shadow_ledger.LEDGER_PATH.exists():
            ledger = pathlib.Path(shadow_ledger.LEDGER_PATH)
        if not ledger.exists():
            empty.update(shadow_ledger.summary())
            empty["picks"] = 0
            empty["fills"] = 0
            empty["fills_note"] = "shadow only — picks are not fills"
            return empty

        picks = 0
        closed = []
        with ledger.open() as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                ts = _parse_iso_ts(rec.get("ts"))
                if ts is None or ts < since_ts or ts > now:
                    continue
                if rec.get("action") == "entry":
                    picks += 1
                elif rec.get("action") == "close":
                    closed.append(rec)
        measured = [c for c in closed if not c.get("unmeasured") and c.get("realized_pnl_usd") is not None]
        pnl = sum(c.get("realized_pnl_usd") or 0 for c in measured)
        wins = sum(1 for c in measured if (c.get("realized_pnl_usd") or 0) > 0)
        losses = sum(1 for c in measured if (c.get("realized_pnl_usd") or 0) < 0)
        summary = shadow_ledger.summary()
        return {
            "picks": picks,
            "fills": 0,
            "fills_note": "shadow only — picks are not fills",
            "open_positions": summary.get("open_positions_count", 0),
            "closed_trades": len(closed),
            "realized_pnl_usd": pnl,
            "win_rate": (wins / len(measured)) if measured else 0.0,
            "winning_trades": wins,
            "losing_trades": losses,
        }
    except Exception as e:
        log.warning("shadow window failed: %s", safe_err(e))
        return empty


def _parse_iso_ts(iso) -> float | None:
    if not iso:
        return None
    try:
        import calendar
        if str(iso).endswith("Z"):
            return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
        return time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def summarize_window(since_ts: float, now: float, *, localtime: Callable | None = None) -> dict:
    cycles = cycles_since(since_ts, now)
    tokens = _tokens_since(since_ts, until_ts=now)
    young = _young_since(since_ts, until_ts=now)
    judged = [t for t in tokens if t.get("kind") == "judged"]
    judge_errors = [t for t in tokens if t.get("kind") == "judge_error"]
    soft = [t for t in tokens if t.get("kind") == "soft"]
    free = _kill_counts(_merge_counts(*(c["free"] for c in cycles)))
    trade = _kill_counts(_merge_counts(*(c["trade"] for c in cycles)))
    chain = _kill_counts(_merge_counts(*(c["chain"] for c in cycles)))
    soft_kills = _kill_counts(_merge_counts(*(c["soft"] for c in cycles)))
    mix = _kill_mix(free, trade, chain, soft_kills)
    scored = judged + soft
    momenta = [m for m in (_momentum(t) for t in scored) if m is not None]
    soft_momenta = [m for m in (_momentum(t) for t in soft) if m is not None]
    mom_by_chain: dict[str, list[float]] = {}
    for t in scored:
        m = _momentum(t)
        if m is None:
            continue
        mom_by_chain.setdefault(t.get("chain") or "unknown", []).append(m)
    median_by_chain = {k: _median(v) for k, v in mom_by_chain.items()}
    median_solana = _median(mom_by_chain.get("solana") or [])
    young_outcomes: dict[str, int] = {}
    for y in young:
        young_outcomes[y["outcome"]] = young_outcomes.get(y["outcome"], 0) + 1
    seen = sum(c["seen"] for c in cycles)
    examined = seen - sum(c["benched"] for c in cycles)
    young_rate = (len(young) / examined) if examined > 0 else 0.0
    expected = max(1, int(round((now - since_ts) / CYCLE_SECONDS))) if now > since_ts else 1
    uptime = {
        "cycles": len(cycles),
        "expected_cycles": expected,
        "uptime_pct": (len(cycles) / expected * 100.0) if expected else 0.0,
        "first_ts": cycles[0]["ts"] if cycles else None,
        "last_ts": cycles[-1]["ts"] if cycles else None,
        "span_hours": ((cycles[-1]["ts"] - cycles[0]["ts"]) / 3600.0) if len(cycles) >= 2 else 0.0,
    }
    per_chain = _per_chain(tokens, young, cycles)
    for name, b in (per_chain.get("by_chain") or {}).items():
        b["median_momentum"] = median_by_chain.get(name)
    if per_chain.get("solana") is not None:
        per_chain["solana"]["median_momentum"] = median_solana
    return {
        "since_ts": since_ts,
        "until_ts": now,
        "cycles": len(cycles),
        "seen": seen,
        "benched": sum(c["benched"] for c in cycles),
        "judged": sum(c["judged"] for c in cycles),
        "requeued": sum(c["requeued"] for c in cycles),
        "carry": sum(c["carry"] for c in cycles),
        "unevaluated": sum(c["unevaluated"] for c in cycles),
        "gt_429": sum(c["gt_429"] for c in cycles),
        "gt_defer": sum(c["gt_defer"] for c in cycles),
        "gt_cache_hits": sum(c.get("gt_cache_hits") or 0 for c in cycles),
        "gt_cache_misses": sum(c.get("gt_cache_misses") or 0 for c in cycles),
        "gt_dossier_attempts": sum(c.get("gt_dossier_attempts") or 0 for c in cycles),
        "gt_dossier_ok": sum(c.get("gt_dossier_ok") or 0 for c in cycles),
        "kills": {"free": free, "trade": trade, "chain": chain, "soft": soft_kills},
        "kill_mix": mix,
        "judged_tokens": judged,
        "judge_error_tokens": judge_errors,
        "soft_tokens": soft,
        "median_momentum": _median(momenta),
        "median_momentum_soft": _median(soft_momenta),
        "median_momentum_by_chain": median_by_chain,
        "median_momentum_solana": median_solana,
        "young_free": young,
        "young_outcomes": young_outcomes,
        "young_free_pass_rate": young_rate,
        "per_chain": per_chain,
        "solana": per_chain["solana"],
        "uptime": uptime,
        "shadow": _shadow_window(since_ts, now),
        "regime": _regime_window(since_ts, now),
    }


def _date_bounds(date_str: str, localtime: Callable | None = None,
                 time_fn: Callable | None = None) -> tuple[float, float]:
    """Unix range [start, end) covering a local YYYY-MM-DD.

    Uses a noon probe so DST-safe local midnight can be recovered without
    knowing the zone name. Tests inject localtime.
    """
    year, month, day = [int(x) for x in date_str.split("-")]
    # Probe: interpret noon UTC and walk until local date matches. Deterministic
    # when localtime is injected; otherwise uses the process TZ.
    probe = time.mktime((year, month, day, 12, 0, 0, 0, 0, -1))
    # Refine to local midnight by subtracting hour/min/sec from localtime(probe).
    lt = (localtime or time.localtime)(probe)
    start = probe - lt.tm_hour * 3600 - lt.tm_min * 60 - lt.tm_sec
    # 24h later is the next local midnight in standard time; good enough for aggregates.
    return start, start + 86400


def daily_aggregates(dates: Iterable[str], *, localtime: Callable | None = None) -> list[dict]:
    out = []
    for date in dates:
        start, end = _date_bounds(date, localtime=localtime)
        # summarize_window is since-inclusive via ts >= since; clamp end with a
        # tight filter on the cycle rows we already have.
        window = summarize_window(start, end - 1e-6, localtime=localtime)
        # Drop tokens whose ts fell into the next day due to the -1e-6 clamp; already OK.
        judged = window["judged"]
        out.append({
            "date": date,
            "cycles": window["cycles"],
            "judged": judged,
            "kill_mix": window["kill_mix"],
            "median_momentum": window["median_momentum"],
            "young_free_count": len(window["young_free"]),
            "young_free_pass_rate": window["young_free_pass_rate"],
            "gt_defers": window["gt_defer"],
            "gt_429": window["gt_429"],
            "chain_share": window["per_chain"]["judged_share"],
            "seen": window["seen"],
            "regime": (window.get("regime") or {}).get("tag"),
            "regime_counts": (window.get("regime") or {}).get("counts") or {},
        })
    return out


def _prior_dates(today: str, n: int = TREND_DAYS) -> list[str]:
    y, m, d = [int(x) for x in today.split("-")]
    # Walk back by whole days using ordinal, TZ-independent.
    import datetime
    base = datetime.date(y, m, d)
    return [(base - datetime.timedelta(days=i)).isoformat() for i in range(n, 0, -1)]


def _mean(values: list[float | None]) -> float | None:
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _rel_delta(current, baseline) -> float | None:
    if current is None or baseline is None:
        return None
    if baseline == 0:
        return None if current == 0 else 1.0
    return (current - baseline) / abs(baseline)


def build_trends(today_window: dict, today: str, *, localtime: Callable | None = None) -> dict:
    dates = _prior_dates(today, TREND_DAYS)
    days = daily_aggregates(dates, localtime=localtime)
    history_days = sum(1 for d in days if (d.get("cycles") or 0) > 0)
    if history_days < TREND_MIN_PRIOR_DAYS:
        return {
            "today": today,
            "days": days,
            "flags": [],
            "notable": [],
            "insufficient_history": True,
            "history_days": history_days,
        }
    baseline_judged = _mean([d["judged"] for d in days])
    baseline_mom = _mean([d["median_momentum"] for d in days])
    baseline_young = _mean([d["young_free_pass_rate"] for d in days])
    baseline_gt = _mean([d["gt_defers"] for d in days])
    # Average kill-mix shares and chain shares across days that had kills/judged.
    mix_keys = ("free", "trade", "chain", "soft")
    baseline_mix = {}
    for k in mix_keys:
        baseline_mix[k] = _mean([d["kill_mix"]["shares"].get(k, 0.0) for d in days]) or 0.0
    chain_names = set()
    for d in days:
        chain_names.update(d["chain_share"].keys())
    chain_names.update((today_window.get("per_chain") or {}).get("judged_share", {}).keys())
    baseline_chain = {
        name: _mean([d["chain_share"].get(name, 0.0) for d in days]) or 0.0
        for name in sorted(chain_names)
    }

    current_judged = today_window.get("judged", 0)
    current_mom = today_window.get("median_momentum")
    current_young = today_window.get("young_free_pass_rate", 0.0)
    current_gt = today_window.get("gt_defer", 0)
    current_mix = (today_window.get("kill_mix") or {}).get("shares") or {}
    current_chain = (today_window.get("per_chain") or {}).get("judged_share") or {}

    flags = []

    def flag(metric, current, baseline, kind, threshold, unit=""):
        if current is None or baseline is None:
            return
        if kind == "rel":
            delta = _rel_delta(current, baseline)
            notable = delta is not None and abs(delta) >= threshold
            delta_disp = None if delta is None else delta * 100.0
        else:
            delta = current - baseline
            notable = abs(delta) >= threshold
            delta_disp = delta
        flags.append({
            "metric": metric,
            "current": current,
            "baseline": baseline,
            "delta": delta_disp,
            "notable": bool(notable),
            "unit": unit,
        })

    flag("judged_per_day", current_judged, baseline_judged, "rel", NOTABLE_JUDGED_REL, "%")
    flag("median_momentum", current_mom, baseline_mom, "abs", NOTABLE_MOMENTUM_ABS)
    flag("young_free_pass_rate", current_young, baseline_young, "abs", NOTABLE_YOUNG_RATE_ABS)
    flag("gt_defers", current_gt, baseline_gt, "rel", NOTABLE_GT_DEFERS_REL, "%")
    for k in mix_keys:
        cur = current_mix.get(k, 0.0)
        base = baseline_mix.get(k, 0.0)
        flags.append({
            "metric": f"kill_mix_{k}",
            "current": cur,
            "baseline": base,
            "delta": cur - base,
            "notable": abs(cur - base) >= NOTABLE_MIX_PP,
            "unit": "pp",
        })
    for name in sorted(chain_names):
        cur = current_chain.get(name, 0.0)
        base = baseline_chain.get(name, 0.0)
        flags.append({
            "metric": f"chain_share_{name}",
            "current": cur,
            "baseline": base,
            "delta": cur - base,
            "notable": abs(cur - base) >= NOTABLE_CHAIN_PP,
            "unit": "pp",
        })

    return {
        "today": today,
        "days": days,
        "flags": flags,
        "notable": [f for f in flags if f["notable"]],
        "insufficient_history": False,
        "history_days": history_days,
    }


def _fmt_signed(value, digits=3) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.{digits}f}"


def _render_regime_md(bundle: dict) -> list[str]:
    latest = (bundle or {}).get("latest") or {}
    tag = (bundle or {}).get("tag") or latest.get("regime") or "—"
    metrics = latest.get("metrics") or {}
    agg = metrics.get("aggregates") or {}
    assets = metrics.get("assets") or {}
    lines = [
        f"- Tag: {tag}",
        f"- Rules: {metrics.get('rules') or 'see coinalyze.REGIME_RULES'}",
        "- This does not affect filtering or judging.",
    ]
    if latest.get("ts_iso") or metrics.get("reason"):
        when = latest.get("ts_iso") or "—"
        reason = metrics.get("reason") or latest.get("reason") or ""
        extra = f" ({reason})" if reason else ""
        lines.append(f"- Latest capture: {when}{extra}")
    if agg:
        lines.append(
            f"- Majors funding median: {_fmt_signed(agg.get('majors_funding_median'))} · "
            f"meme funding median: {_fmt_signed(agg.get('memes_funding_median'))}"
        )
        lines.append(
            f"- OI change median: {_fmt_signed(agg.get('oi_change_median'))} · "
            f"long/short median: {_fmt_signed(agg.get('ls_ratio_median'))} · "
            f"long-liq share: {_fmt_signed(agg.get('long_liq_share'))}"
        )
    if assets:
        parts = []
        for name in sorted(assets):
            a = assets[name] or {}
            parts.append(
                f"{name} fr={_fmt_signed(a.get('funding'))} "
                f"oi={_fmt_signed(a.get('oi_change_pct'))}"
            )
        lines.append("- Perps: " + "; ".join(parts))
    if not latest:
        lines.append("- No Coinalyze snapshot in this window.")
    return lines


def render_briefing_md(payload: dict) -> str:
    w = payload.get("last_24h") or {}
    up = w.get("uptime") or {}
    sh = w.get("shadow") or {}
    kills = w.get("kills") or {}
    lines = [
        f"# Morning briefing — {payload.get('date_local', '')}",
        "",
        f"Generated {payload.get('generated_at_iso', '')} (UTC). Window: last 24h. Shadow only.",
        "",
        "## Overall",
        f"- Cycles: {w.get('cycles', 0)} "
        f"(uptime {up.get('uptime_pct', 0):.1f}% vs {up.get('expected_cycles', 0)} expected at 15 min)",
        f"- Seen {w.get('seen', 0)} · benched {w.get('benched', 0)} · reached judge {w.get('judged', 0)} "
        f"· requeued {w.get('requeued', 0)}",
        f"- Carry {w.get('carry', 0)} · unevaluated {w.get('unevaluated', 0)} "
        f"· GT 429s {w.get('gt_429', 0)} · GT defers {w.get('gt_defer', 0)}",
        "",
        "## Momentum",
        f"- Median (judged+soft): {_fmt_mom(w.get('median_momentum'))}",
        f"- Soft-only: {_fmt_mom(w.get('median_momentum_soft'))}",
        f"- Per chain: {_fmt_mom_map(w.get('median_momentum_by_chain'))}",
        f"- Solana: {_fmt_mom(w.get('median_momentum_solana'))}",
        "",
        "## Per chain",
        "",
        "| chain | reached judge | soft | young free | young 429 | young chain | young soft |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    by_chain = (w.get("per_chain") or {}).get("by_chain") or {}
    if not by_chain:
        lines.append("| — | 0 | 0 | 0 | 0 | 0 | 0 |")
    for name in sorted(by_chain):
        b = by_chain[name]
        yo = b.get("young_outcomes") or {}
        lines.append(
            f"| {name} | {b.get('judged', 0)} | {b.get('soft', 0)} | {b.get('young_free', 0)} "
            f"| {yo.get('deferred_429', 0)} | {yo.get('chain', 0)} | {yo.get('soft', 0)} |"
        )
    sol = w.get("solana") or {}
    lines += [
        "",
        "## Solana",
        f"- Reached judge: {sol.get('judged', 0)} · soft: {sol.get('soft', 0)} · young free: {sol.get('young_free', 0)}",
        f"- Median momentum: {_fmt_mom(w.get('median_momentum_solana') if w.get('median_momentum_solana') is not None else sol.get('median_momentum'))}",
        f"- Young outcomes: {sol.get('young_outcomes') or {}}",
        "",
        "## Kill reasons",
    ]
    for stage in ("free", "trade", "chain", "soft"):
        bag = kills.get(stage) or {}
        lines.append(f"### {stage}")
        if not bag:
            lines.append("- (none)")
            continue
        for reason, count in sorted(bag.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"- {reason}: {count}")
    lines += ["", "## Passed judge/picks", ""]
    judged = w.get("judged_tokens") or []
    picks = int((w.get("shadow") or {}).get("picks") or 0)
    lines.append(f"- passed judge/picks: {picks}")
    if not judged:
        lines.append("No tokens passed judge/picks in the window.")
    else:
        lines.append("| ticker | chain | age min | momentum | scores |")
        lines.append("| --- | --- | ---: | ---: | --- |")
        for t in judged:
            mom = _momentum(t)
            mom_s = "—" if mom is None else f"{mom:.3f}"
            age = t.get("age_minutes")
            age_s = "—" if age is None else f"{age:.1f}"
            scores = t.get("soft_scores") or {}
            compact = ", ".join(f"{k}={v:.3f}" if isinstance(v, (int, float)) else f"{k}={v}"
                                for k, v in sorted(scores.items()))
            lines.append(
                f"| {t.get('ticker') or '—'} | {t.get('chain') or '—'} | {age_s} | {mom_s} | {compact or '—'} |"
            )
    lines += ["", "## Judge verdicts", ""]
    verdict_rows = list(judged) + list(w.get("judge_error_tokens") or [])
    if not verdict_rows:
        lines.append("No judge verdicts in the window.")
    else:
        lines.append("| ticker | chain | verdict | score | conf | reason | scores |")
        lines.append("| --- | --- | --- | ---: | ---: | --- | --- |")
        for t in verdict_rows:
            scores = t.get("soft_scores") or {}
            compact = ", ".join(
                f"{k}={v:.3f}" if isinstance(v, (int, float)) else f"{k}={v}"
                for k, v in sorted(scores.items())
            )
            score = t.get("judge_score")
            conf = t.get("judge_confidence")
            score_s = "—" if score is None else (
                f"{score:.3f}" if isinstance(score, (int, float)) else str(score)
            )
            conf_s = "—" if conf is None else (
                f"{conf:.3f}" if isinstance(conf, (int, float)) else str(conf)
            )
            reason = (t.get("judge_reason") or "—").replace("|", "/")
            lines.append(
                f"| {t.get('ticker') or '—'} | {t.get('chain') or '—'} | "
                f"{t.get('judge_verdict') or '—'} | {score_s} | {conf_s} | "
                f"{reason} | {compact or '—'} |"
            )
    lines += ["", "## Young free-pass outcomes", ""]
    yo = w.get("young_outcomes") or {}
    if not yo:
        lines.append("No young free-passers in the window.")
    else:
        for key in ("deferred_429", "chain", "soft", "judged", "trade", "unevaluated", "other"):
            if key in yo:
                label = {
                    "deferred_429": "deferred on 429",
                    "chain": "chain kill",
                    "soft": "soft",
                    "judged": "young reached judge",
                    "trade": "trade",
                    "unevaluated": "unevaluated",
                    "other": "other",
                }[key]
                lines.append(f"- {label}: {yo[key]}")
        extra = [k for k in yo if k not in ("deferred_429", "chain", "soft", "judged", "trade", "unevaluated", "other")]
        for k in extra:
            lines.append(f"- {k}: {yo[k]}")
        lines.append(f"- young free-pass rate (of examined): {w.get('young_free_pass_rate', 0):.1%}")
    lines += [
        "",
        "## Shadow P&L (hypothetical)",
        f"- Picks: {sh.get('picks', 0)}",
        f"- Fills: {sh.get('fills', 0)} ({sh.get('fills_note', 'shadow only — picks are not fills')})",
        f"- Realized PnL: ${sh.get('realized_pnl_usd', 0):+.2f} "
        f"({sh.get('closed_trades', 0)} closed, {sh.get('winning_trades', 0)}W/{sh.get('losing_trades', 0)}L)",
        f"- Open positions: {sh.get('open_positions', 0)}",
        "",
        "## Market regime (log-only)",
    ]
    lines.extend(_render_regime_md(w.get("regime") or {}))
    lines += [
        "",
        "## Trends vs prior 7 days",
    ]
    trends = payload.get("trends") or {}
    notable = trends.get("notable") or []
    if trends.get("insufficient_history"):
        lines.append(
            f"insufficient history — need at least {TREND_MIN_PRIOR_DAYS} prior days "
            f"before trend flags ({trends.get('history_days', 0)} so far)."
        )
    elif not notable:
        lines.append("No notable shifts versus the prior 7 daily aggregates.")
    else:
        for f in notable:
            unit = f.get("unit") or ""
            delta = f.get("delta")
            if delta is None:
                lines.append(f"- {f['metric']}: current {f.get('current')} vs baseline {f.get('baseline')}")
            elif unit == "pp":
                lines.append(
                    f"- {f['metric']}: {f.get('current'):.1f} vs {f.get('baseline'):.1f} "
                    f"({delta:+.1f} pp)"
                )
            elif unit == "%":
                lines.append(
                    f"- {f['metric']}: {f.get('current')} vs {f.get('baseline')} "
                    f"({delta:+.1f}%)"
                )
            else:
                lines.append(
                    f"- {f['metric']}: {f.get('current')} vs {f.get('baseline')} "
                    f"({delta:+.3f})"
                )
    days = trends.get("days") or []
    if days:
        lines.append("- Regime per day: " + ", ".join(
            f"{d.get('date', '—')}={d.get('regime') or '—'}" for d in days
        ))
    lines.append("")
    return "\n".join(lines)


def build_briefing(now: float | None = None, *, localtime: Callable | None = None) -> dict:
    now = time.time() if now is None else float(now)
    today = _local_date(now, localtime=localtime)
    window = summarize_window(now - 86400, now, localtime=localtime)
    trends = build_trends(window, today, localtime=localtime)
    payload = {
        "date_local": today,
        "generated_at": now,
        "generated_at_iso": _iso(now),
        "last_24h": window,
        "trends": trends,
        "persisted": False,
    }
    payload["markdown"] = render_briefing_md(payload)
    return payload


def briefing_exists(date_local: str) -> bool:
    row = _connect().execute(
        "SELECT 1 FROM briefings WHERE date_local = ?", (date_local,)
    ).fetchone()
    return bool(row)


def persist_briefing(payload: dict) -> pathlib.Path:
    date = payload["date_local"]
    dest_dir = briefings_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{date}.md"
    path.write_text(payload.get("markdown") or render_briefing_md(payload))
    db = _connect()
    db.execute(
        """INSERT OR REPLACE INTO briefings
           (date_local, generated_at, generated_at_iso, markdown, payload_json)
           VALUES (?,?,?,?,?)""",
        (
            date, payload["generated_at"], payload["generated_at_iso"],
            payload.get("markdown") or "",
            _json({k: v for k, v in payload.items() if k != "markdown"}),
        ),
    )
    db.commit()
    payload["persisted"] = True
    payload["path"] = str(path)
    return path


def maybe_persist_briefing(now: float | None = None, *,
                           localtime: Callable | None = None) -> dict | None:
    """First cycle at or after 07:00 local writes today's briefing once."""
    now = time.time() if now is None else float(now)
    if _local_hour(now, localtime) < BRIEFING_HOUR_LOCAL:
        return None
    today = _local_date(now, localtime)
    if briefing_exists(today):
        return None
    payload = build_briefing(now, localtime=localtime)
    persist_briefing(payload)
    log.info("morning briefing persisted for %s", today)
    return payload


def latest_briefing() -> dict | None:
    row = _connect().execute(
        "SELECT * FROM briefings ORDER BY generated_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    payload = _loads(row["payload_json"], default={})
    payload["markdown"] = row["markdown"]
    payload["persisted"] = True
    payload["date_local"] = row["date_local"]
    payload["generated_at"] = row["generated_at"]
    payload["generated_at_iso"] = row["generated_at_iso"]
    return payload


def briefing_payload(now: float | None = None, *, localtime: Callable | None = None) -> dict:
    """Live snapshot for GET /ops/briefing, plus the persisted morning file if any."""
    snap = build_briefing(now, localtime=localtime)
    persisted = latest_briefing()
    snap["morning"] = None
    if persisted:
        snap["morning"] = {
            "date_local": persisted.get("date_local"),
            "generated_at_iso": persisted.get("generated_at_iso"),
            "markdown": persisted.get("markdown"),
        }
        snap["persisted"] = persisted.get("date_local") == snap.get("date_local")
    return snap


# ---------------------------------------------------------------------------
# Backfill from outbox/run.log
# ---------------------------------------------------------------------------

CYCLE_RE = re.compile(
    r"cycle: (\d+) seen, (\d+) benched, free (\{.*?\}), trade (\{.*?\}), "
    r"chain (\{.*?\}), soft (\{.*?\}), judged (\d+), requeued (\d+)"
)
SOFT_RE = re.compile(
    r"soft tid=(\S+) ticker=(.+?) reason=(\S+) noul=(\S+) age_minutes=(\S+) "
    r"top_10_percent=(\S+) top_wallet_percent=(\S+) "
    r"developer_holding_percentage=(\S+) holder_count=(\S+) "
    r"rpc_ok=(\S+) soft_scores=(.*)$"
)
FREE_PASS_RE = re.compile(
    r"free tid=(\S+) reason=pass age_minutes=(\S+)"
)
CHAIN_RE = re.compile(r"chain tid=(\S+) ticker=(.+?) reason=(\S+)")
TRADE_RE = re.compile(r"trade tid=(\S+) ticker=(.+?) reason=(\S+)")
YOUNG_429_RE = re.compile(
    r"young token (.+?) \(age ([0-9.]+)m\) hit 429"
)
SOFT_HINT_RE = re.compile(r"soft tid=")
CHAIN_HINT_RE = re.compile(r"chain tid=")
JUDGE_RE = re.compile(
    r"judge tid=(\S+) ticker=(.+?) verdict=(\S+) score=(\S+) "
    r"confidence=(\S+) reason=(.*) mode=(\S+)\s*$"
)
UNEVAL_RE = re.compile(r"unevaluated (\d+) ids")
CARRY_RE = re.compile(r"carrying (\d+) of (\d+) unevaluated ids")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def parse_log_ts(line: str) -> float | None:
    m = TS_RE.match(line)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def _literal(text: str, default=None):
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return {} if default is None else default


def parse_run_log(text: str) -> tuple[list[dict], dict]:
    """Parse run.log into cycle dicts ready for on_cycle / on_soft_token.

    Soft / free-pass / 429 lines that appear before a `cycle:` line belong to
    that cycle (the live logger emits the cycle summary last).
    """
    cycles: list[dict] = []
    pending_soft: list[dict] = []
    pending_young: dict[str, dict] = {}
    pending_later: dict[str, dict] = {}
    carry = 0
    unevaluated = 0
    pending_gt_429 = 0
    unparsed_soft = 0
    unparsed_chain = 0
    last_ts = None

    def flush_cycle(stats, ts, shadow=True):
        nonlocal pending_soft, pending_young, pending_later, carry, unevaluated
        nonlocal pending_gt_429
        young = []
        for tid, y in pending_young.items():
            later = pending_later.get(tid)
            young.append(y)
            # classify later via on_cycle using pending_later as tokens
            if later and later.get("tid") == tid:
                pass
        tokens = list(pending_later.values())
        # Soft rows that never got a later token still need a tokens entry.
        for s in pending_soft:
            tokens.append({
                "tid": s["tid"], "ticker": s["ticker"], "net": s.get("chain_id"),
                "chain": s.get("chain"), "stage": "soft", "reason": s.get("reason"),
                "verdict": "DROP", "age_minutes": s.get("age_minutes"),
                "soft_noul": s.get("noul"), "soft_scores": s.get("soft_scores"),
                "top_10_percent": s.get("top_10_percent"),
                "top_wallet_percent": s.get("top_wallet_percent"),
                "developer_holding_percentage": s.get("developer_holding_percentage"),
                "holder_count": s.get("holder_count"),
            })
        stats = dict(stats)
        stats["tokens"] = tokens
        stats["young_free"] = list(pending_young.values())
        stats["carry"] = carry
        stats["unevaluated"] = unevaluated
        stats["gt_429"] = pending_gt_429
        stats["gt_defer"] = infer_gt_counts({"tokens": tokens, "gt_429": 0})[1]
        cycles.append({
            "ts": ts,
            "stats": stats,
            "soft": list(pending_soft),
            "shadow": True,
        })
        pending_soft = []
        pending_young = {}
        pending_later = {}
        carry = 0
        unevaluated = 0
        pending_gt_429 = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        ts = parse_log_ts(line)
        if ts is not None:
            last_ts = ts
        ts = last_ts if last_ts is not None else time.time()

        m = CYCLE_RE.search(line)
        if m:
            stats = {
                "seen": int(m.group(1)),
                "benched": int(m.group(2)),
                "free": _literal(m.group(3)),
                "trade": _literal(m.group(4)),
                "chain": _literal(m.group(5)),
                "soft": _literal(m.group(6)),
                "judged": int(m.group(7)),
                "requeued": int(m.group(8)),
            }
            flush_cycle(stats, ts)
            continue

        m = SOFT_RE.search(line)
        if m:
            scores = _literal(m.group(11), default={})
            if not isinstance(scores, dict):
                scores = {}
            tid = m.group(1)
            row = {
                "tid": tid,
                "ticker": m.group(2),
                "reason": m.group(3),
                "noul": _num(m.group(4)),
                "age_minutes": _num(m.group(5)),
                "top_10_percent": _num(m.group(6)),
                "top_wallet_percent": _num(m.group(7)),
                "developer_holding_percentage": _num(m.group(8)),
                "holder_count": _int(m.group(9)),
                "soft_scores": scores,
                "chain_id": chain_id_of(tid=tid),
                "chain": chain_name(tid=tid),
                "ts": ts,
            }
            pending_soft.append(row)
            pending_later[tid] = {
                "tid": tid, "ticker": row["ticker"], "net": row["chain_id"],
                "chain": row["chain"], "stage": "soft", "reason": row["reason"],
                "verdict": "DROP", "age_minutes": row["age_minutes"],
                "soft_noul": row["noul"], "soft_scores": scores,
                "top_10_percent": row["top_10_percent"],
                "top_wallet_percent": row["top_wallet_percent"],
                "developer_holding_percentage": row["developer_holding_percentage"],
                "holder_count": row["holder_count"],
            }
            continue

        m = FREE_PASS_RE.search(line)
        if m:
            age = _num(m.group(2))
            if age is not None and age < 60:
                tid = m.group(1)
                pending_young[tid] = {
                    "tid": tid,
                    "ticker": None,
                    "net": chain_id_of(tid=tid),
                    "age_minutes": age,
                    "chain": chain_name(tid=tid),
                }
            continue

        m = YOUNG_429_RE.search(line)
        if m:
            pending_gt_429 += 1
            # Outcome hint; tid may only be known via later chain/trade lines.
            # We record a later-row keyed by ticker if we can match a young tid.
            ticker = m.group(1)
            for tid, y in pending_young.items():
                if y.get("ticker") in (None, ticker) or (y.get("tid") or "").startswith(ticker):
                    y["ticker"] = ticker
                    pending_later.setdefault(tid, {
                        "tid": tid, "ticker": ticker, "stage": "chain",
                        "reason": "requeued_429_backoff", "verdict": "DROP",
                        "age_minutes": _num(m.group(2)),
                        "net": y.get("net"), "chain": y.get("chain"),
                    })
                    pending_later[tid]["reason"] = "requeued_429_backoff"
                    pending_later[tid]["stage"] = "chain"
            continue

        m = CHAIN_RE.search(line)
        if m:
            tid, ticker, reason = m.group(1), m.group(2), m.group(3)
            pending_later[tid] = {
                "tid": tid, "ticker": ticker, "stage": "chain", "reason": reason,
                "verdict": "DROP", "net": chain_id_of(tid=tid),
                "chain": chain_name(tid=tid),
            }
            if tid in pending_young:
                pending_young[tid]["ticker"] = ticker
            continue

        m = TRADE_RE.search(line)
        if m:
            tid, ticker, reason = m.group(1), m.group(2), m.group(3)
            if reason == "pass":
                continue
            pending_later[tid] = {
                "tid": tid, "ticker": ticker, "stage": "trade", "reason": reason,
                "verdict": "DROP", "net": chain_id_of(tid=tid),
                "chain": chain_name(tid=tid),
            }
            continue

        m = JUDGE_RE.search(line)
        if m:
            tid, ticker, jverdict = m.group(1), m.group(2), m.group(3)
            existing = pending_later.get(tid) or {}
            if existing.get("stage") == "soft":
                continue
            pending_later[tid] = {
                **existing,
                "tid": tid, "ticker": ticker,
                "stage": "judge_error" if jverdict == "error" else "judged",
                "reason": existing.get("reason"),
                "verdict": existing.get("verdict") or "PASS",
                "net": chain_id_of(tid=tid),
                "chain": chain_name(tid=tid),
                "judge_verdict": jverdict,
                "judge_score": _num(m.group(4)),
                "judge_confidence": _num(m.group(5)),
                "judge_reason": (m.group(6) or "").strip(),
            }
            continue

        m = UNEVAL_RE.search(line)
        if m:
            unevaluated = int(m.group(1))
            continue
        m = CARRY_RE.search(line)
        if m:
            carry = int(m.group(2))
            continue

        if SOFT_HINT_RE.search(line) and not SOFT_RE.search(line):
            unparsed_soft += 1
        elif CHAIN_HINT_RE.search(line) and not CHAIN_RE.search(line):
            unparsed_chain += 1

    return cycles, {
        "unparsed_soft": unparsed_soft,
        "unparsed_chain": unparsed_chain,
        "unparsed": unparsed_soft + unparsed_chain,
    }


def backfill_text(text: str, *, source: str = "backfill") -> dict:
    """Write parsed log cycles into the store. Idempotent. Returns a summary dict."""
    parsed, meta = parse_run_log(text)
    inserted = 0
    skipped = 0
    for item in parsed:
        if cycle_exists(item["ts"]):
            skipped += 1
            _pending_soft.clear()
            continue
        for s in item["soft"]:
            on_soft_token(
                tid=s.get("tid"), ticker=s.get("ticker"), net=s.get("chain_id"),
                chain=s.get("chain"), reason=s.get("reason"), noul=s.get("noul"),
                age_minutes=s.get("age_minutes"),
                top_10_percent=s.get("top_10_percent"),
                top_wallet_percent=s.get("top_wallet_percent"),
                developer_holding_percentage=s.get("developer_holding_percentage"),
                holder_count=s.get("holder_count"),
                soft_scores=s.get("soft_scores"),
                ts=s.get("ts") or item["ts"],
            )
        cid = on_cycle(
            item["stats"], shadow=True, now=item["ts"], source=source,
        )
        if cid is not None:
            inserted += 1
        else:
            skipped += 1
    return {
        "inserted": inserted,
        "skipped": skipped,
        "parsed_cycles": len(parsed),
        "unparsed": meta["unparsed"],
        "unparsed_soft": meta["unparsed_soft"],
        "unparsed_chain": meta["unparsed_chain"],
    }


def backfill_log(path=None) -> dict:
    log_path = pathlib.Path(path) if path else outbox_dir() / "run.log"
    if not log_path.exists():
        log.warning("backfill: %s not found", log_path)
        return {
            "inserted": 0, "skipped": 0, "parsed_cycles": 0,
            "unparsed": 0, "unparsed_soft": 0, "unparsed_chain": 0,
        }
    return backfill_text(log_path.read_text(errors="replace"))
