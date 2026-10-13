"""Queryable research store for time-sensitive desk patterns.

SQLite lives at outbox/research.db (WAL). Captures per-cycle token snapshots,
a 60s DexScreener fast-mark sampler, and computed labels. LOG/ANALYSIS ONLY:
never called from filter, judge, pick, or order paths. No GT. No live orders.
Does not change young_trail or variant exit rules. Fail open. Exceptions go
through safe_err. Never logs secrets.

CLI:
  python research.py sql "SELECT ..."
  python research.py report rugs|runners|features
  python research.py backfill [run.log]
  python research.py labels

Rows carry origin='live' or origin='backfill:<source>'. Backfill reads all
available history (rotated run.logs, cycle JSON, shadow_kills, shadow ledger,
outcomes candles, judge lines, young_trail+variants, x_cache, coinalyze).
Tokens still missing post-sighting prices are queued for the off-peak
outcomes fetcher.
"""
from __future__ import annotations

import argparse
import bz2
import gzip
import json
import logging
import os
import pathlib
import re
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime

import requests

from secret_utils import safe_err

log = logging.getLogger("research")

DEX_BATCH = "https://api.dexscreener.com/latest/dex/tokens"
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) desk/research",
    "Accept": "application/json",
}
CHAIN_NAMES = {
    1399811149: "solana",
    56: "bsc",
    8453: "base",
    4663: "robinhood",
    143: "monad",
}

HTTP_TIMEOUT = 5.0
BATCH_CAP = 30
SAMPLE_SEC = 60.0
YOUNG_AGE_MAX_MIN = 120.0
SEEN_WINDOW_SEC = 3 * 3600
LOCK_TIMEOUT_SEC = 0.2
VACUUM_INTERVAL_SEC = 7 * 86400
SQL_ROW_LIMIT = 500
LABELS_INTERVAL_SEC = 600.0
QUEUE_MAX_DEFAULT = 200
HORIZONS_SEC = (("ret_15m", 15 * 60), ("ret_1h", 3600),
                ("ret_3h", 3 * 3600), ("ret_6h", 6 * 3600))
FREE_STAGES = frozenset({
    "trade", "chain", "soft", "judge", "judged", "judge_error",
})
WRITE_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|"
    r"VACUUM|REINDEX|INTO)\b",
    re.I,
)
X_LOG_RE = re.compile(
    r"x tid=(\S+) handle=(\S+) ok=(\S+) followers=(\S+) "
    r"own_posts=(\S+) source=(\S+) err=(\S+)(?: x_kind=(\S+))?"
)
TRADE_BUYS_RE = re.compile(
    r"trade tid=(\S+)\b.*?buys_h1=(\S+)"
)

_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_get_fn = None
_sleep_fn = None
_time_fn = time.time
_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_db_lock = threading.RLock()
_last_labels_at = 0.0


def set_get_fn(fn):
    """Inject HTTP get(url, timeout=, headers=) for tests. None restores."""
    global _get_fn
    _get_fn = fn


def set_sleep_fn(fn):
    global _sleep_fn
    _sleep_fn = fn


def set_time_fn(fn):
    global _time_fn
    _time_fn = time.time if fn is None else fn


def current_time() -> float:
    return float(_time_fn())


def outbox_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))


def db_path() -> pathlib.Path:
    override = os.environ.get("RESEARCH_DB")
    if override:
        return pathlib.Path(override)
    return outbox_dir() / "research.db"


def reset() -> None:
    """Close the cached connection. Tests call this between cases."""
    global _conn, _conn_path, _worker_thread, _last_labels_at
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None
    _conn_path = None
    _last_labels_at = 0.0
    with _worker_lock:
        _worker_thread = None


@contextmanager
def _locked():
    """Serialize research.db writes across the cycle thread and sampler thread."""
    with _db_lock:
        yield _connect()


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
    _conn = sqlite3.connect(path, check_same_thread=False, timeout=LOCK_TIMEOUT_SEC)
    _conn.row_factory = sqlite3.Row
    try:
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    except Exception as e:
        log.warning("research wal: %s", safe_err(e))
    _init_schema(_conn)
    _conn_path = path
    return _conn


def _init_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS token_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts REAL NOT NULL,
      ts_sec INTEGER NOT NULL,
      tid TEXT NOT NULL,
      chain TEXT,
      ticker TEXT,
      age_min REAL,
      price_usd REAL,
      liq_usd REAL,
      mcap_usd REAL,
      vol_5m REAL,
      vol_1h REAL,
      buys_5m REAL,
      sells_5m REAL,
      buys_1h REAL,
      sells_1h REAL,
      holders INTEGER,
      holder_growth TEXT,
      top_wallet_pct REAL,
      top10_pct REAL,
      dev_pct REAL,
      dev_sold_pct REAL,
      x_handle TEXT,
      x_followers INTEGER,
      x_kind TEXT,
      source TEXT,
      trades_source TEXT,
      stage TEXT NOT NULL DEFAULT '',
      kill_reason TEXT,
      noul REAL,
      judge_score REAL,
      judge_confidence REAL,
      judge_verdict TEXT,
      regime TEXT,
      origin TEXT NOT NULL DEFAULT 'live'
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_dedup
      ON token_snapshots(ts_sec, tid, stage);
    CREATE INDEX IF NOT EXISTS idx_snap_tid ON token_snapshots(tid);
    CREATE INDEX IF NOT EXISTS idx_snap_ts ON token_snapshots(ts);

    CREATE TABLE IF NOT EXISTS fast_marks (
      ts REAL NOT NULL,
      ts_sec INTEGER NOT NULL,
      tid TEXT NOT NULL,
      chain TEXT,
      ticker TEXT,
      price_usd REAL,
      liq_usd REAL,
      fdv REAL,
      vol_5m REAL,
      vol_1h REAL,
      vol_h24 REAL,
      source TEXT NOT NULL DEFAULT 'dex_batch',
      origin TEXT NOT NULL DEFAULT 'live',
      PRIMARY KEY (ts_sec, tid)
    );
    CREATE INDEX IF NOT EXISTS idx_marks_tid ON fast_marks(tid);
    CREATE INDEX IF NOT EXISTS idx_marks_ts ON fast_marks(ts);

    CREATE TABLE IF NOT EXISTS token_labels (
      tid TEXT PRIMARY KEY,
      ticker TEXT,
      chain TEXT,
      first_seen_ts REAL,
      first_free_ts REAL,
      minutes_from_first_sighting REAL,
      entry_price REAL,
      peak_x REAL,
      peak_ts REAL,
      time_to_peak_min REAL,
      ret_15m REAL,
      ret_1h REAL,
      ret_3h REAL,
      ret_6h REAL,
      rugged INTEGER NOT NULL DEFAULT 0,
      rug_ts REAL,
      time_to_rug_min REAL,
      runner INTEGER NOT NULL DEFAULT 0,
      updated_ts REAL,
      origin TEXT NOT NULL DEFAULT 'live'
    );
    CREATE INDEX IF NOT EXISTS idx_labels_runner ON token_labels(runner);
    CREATE INDEX IF NOT EXISTS idx_labels_rugged ON token_labels(rugged);

    CREATE TABLE IF NOT EXISTS research_meta (
      key TEXT PRIMARY KEY,
      value TEXT
    );
    """)
    db.commit()
    _ensure_origin_cols(db)


def _ensure_origin_cols(db: sqlite3.Connection) -> None:
    for table in ("token_snapshots", "fast_marks", "token_labels"):
        try:
            cols = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        except Exception:
            continue
        if "origin" not in cols:
            db.execute(
                f"ALTER TABLE {table} ADD COLUMN origin TEXT NOT NULL DEFAULT 'live'"
            )
    db.commit()


def _as_origin(origin: str | None, *, default: str = "live") -> str:
    raw = (origin or default or "live").strip() or default
    if raw == "backfill":
        return "backfill:run.log"
    return raw


def _sleep(seconds: float) -> None:
    fn = _sleep_fn
    if fn is not None:
        fn(seconds)
        return
    time.sleep(max(0.0, float(seconds)))


def _http_get(url: str, timeout=HTTP_TIMEOUT):
    fn = _get_fn or requests.get
    return fn(url, timeout=timeout, headers=UA)


def _num(v):
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    n = _num(v)
    if n is None:
        return None
    try:
        return int(n)
    except (TypeError, OverflowError):
        return None


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def parse_tid(tid) -> tuple[str | None, int | None]:
    if not tid:
        return None, None
    s = str(tid)
    if ":" not in s:
        return s, None
    addr, tail = s.rsplit(":", 1)
    try:
        return addr, int(tail)
    except ValueError:
        return s, None


def chain_of(tid=None, net=None, fallback=None) -> str | None:
    if net is not None:
        try:
            name = CHAIN_NAMES.get(int(net))
            if name:
                return name
        except (TypeError, ValueError):
            pass
    _, n = parse_tid(tid)
    if n in CHAIN_NAMES:
        return CHAIN_NAMES[n]
    return fallback


def _pct_from_text(text) -> float | None:
    if not isinstance(text, str):
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", text)
    if not m:
        return None
    return _num(m.group(1))


def _window_block(token: dict, *names):
    for name in names:
        block = token.get(name)
        if isinstance(block, dict):
            return block
    return None


def _vol_from_token(token: dict, window: str) -> float | None:
    alias = {"h1": ("vol_1h", "volume_h1"), "m5": ("vol_5m", "volume_m5")}
    for key in alias.get(window, ()):
        n = _num(token.get(key))
        if n is not None:
            return n
    vol = _window_block(token, "volume")
    if vol:
        raw = vol.get(window) or vol.get("1h" if window == "h1" else window)
        if isinstance(raw, dict):
            return _num(raw.get("usd") or raw.get("h1"))
        n = _num(raw)
        if n is not None:
            return n
    pair = _window_block(token, "pair", "best_pair", "dex_pair")
    if pair:
        inner = pair.get("volume")
        if isinstance(inner, dict):
            return _num(inner.get(window))
    return None


def _buys_from_token(token: dict, window: str) -> float | None:
    alias = {"h1": ("buys_h1", "buys_1h"), "m5": ("buys_m5", "buys_5m")}
    for key in alias.get(window, ()):
        n = _num(token.get(key))
        if n is not None:
            return n
    txns = _window_block(token, "txns", "transactions")
    if txns:
        raw = txns.get(window) or txns.get("1h" if window == "h1" else window)
        if isinstance(raw, dict):
            return _num(raw.get("buys"))
    return None


def _sells_from_token(token: dict, window: str) -> float | None:
    alias = {"h1": ("sells_h1", "sells_1h"), "m5": ("sells_m5", "sells_5m")}
    for key in alias.get(window, ()):
        n = _num(token.get(key))
        if n is not None:
            return n
    txns = _window_block(token, "txns", "transactions")
    if txns:
        raw = txns.get(window) or txns.get("1h" if window == "h1" else window)
        if isinstance(raw, dict):
            return _num(raw.get("sells"))
    return None


def _x_cache_lookup(handle: str) -> tuple:
    """(followers, x_kind) from x_cache.db. No HTTP, ignore TTL. Fail open."""
    key = (handle or "").strip().lstrip("@").lower()
    if not key:
        return None, None
    try:
        import x_lookup
        db = x_lookup._connect()
        row = db.execute(
            "SELECT payload_json FROM x_cache WHERE handle = ? AND ok = 1",
            (key,),
        ).fetchone()
        if not row or not row["payload_json"]:
            return None, None
        payload = json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else row["payload_json"]
        if not isinstance(payload, dict):
            return None, None
        return _int(payload.get("followers")), payload.get("x_kind")
    except Exception as e:
        log.info("research x_cache: %s", safe_err(e))
        return None, None


def _x_fields(token: dict) -> tuple:
    handle = token.get("x_handle") or token.get("fomo_twitter") or token.get("dex_twitter")
    followers = token.get("x_followers")
    kind = token.get("x_kind")
    xa = token.get("x_account")
    if isinstance(xa, dict):
        handle = handle or xa.get("handle") or xa.get("screen_name")
        if followers is None:
            followers = xa.get("followers")
        kind = kind or xa.get("x_kind")
    if isinstance(handle, str):
        handle = handle.lstrip("@") or None
    if followers is None and handle:
        cached_followers, cached_kind = _x_cache_lookup(handle)
        if followers is None:
            followers = cached_followers
        kind = kind or cached_kind
    return handle, _int(followers), kind


def _row_from_token(token: dict, ts: float, *, stage=None, regime=None,
                    origin: str | None = None) -> dict:
    t = token or {}
    tid = t.get("tid")
    stage = stage if stage is not None else (t.get("stage") or "")
    if stage == "judge":
        stage = "judged"
    handle, followers, x_kind = _x_fields(t)
    sold = _num(t.get("dev_sold_pct"))
    if sold is None:
        sold = _pct_from_text(t.get("dev_sells") or "")
    return {
        "ts": ts,
        "ts_sec": int(ts),
        "tid": tid,
        "chain": t.get("chain") or chain_of(tid=tid, net=t.get("net"), fallback=t.get("chain")),
        "ticker": t.get("ticker"),
        "age_min": _num(t.get("age_minutes") if t.get("age_minutes") is not None else t.get("age_min")),
        "price_usd": _num(t.get("price_usd") or t.get("price") or t.get("priceUsd")),
        "liq_usd": _num(t.get("liquidity_usd") or t.get("liq_usd") or t.get("liq")),
        "mcap_usd": _num(t.get("mcap_usd") or t.get("mcap")),
        "vol_5m": _vol_from_token(t, "m5"),
        "vol_1h": _vol_from_token(t, "h1"),
        "buys_5m": _buys_from_token(t, "m5"),
        "sells_5m": _sells_from_token(t, "m5"),
        "buys_1h": _buys_from_token(t, "h1"),
        "sells_1h": _sells_from_token(t, "h1"),
        "holders": _int(t.get("holder_count") or t.get("holders")),
        "holder_growth": t.get("holder_growth"),
        "top_wallet_pct": _num(t.get("top_wallet_percent") or t.get("top_wallet_pct")),
        "top10_pct": _num(t.get("top_10_percent") or t.get("top10_pct")),
        "dev_pct": _num(t.get("developer_holding_percentage") or t.get("dev_pct")),
        "dev_sold_pct": sold,
        "x_handle": handle,
        "x_followers": followers,
        "x_kind": x_kind,
        "source": t.get("source") or t.get("fetched_via"),
        "trades_source": t.get("trades_source") or t.get("dex_status"),
        "stage": stage or "",
        "kill_reason": t.get("reason") if t.get("reason") not in (None, "pass", "free_passed") else None,
        "noul": _num(t.get("soft_noul") or t.get("noul")),
        "judge_score": _num(t.get("judge_score")),
        "judge_confidence": _num(t.get("judge_confidence")),
        "judge_verdict": t.get("judge_verdict"),
        "regime": regime or t.get("regime"),
        "origin": _as_origin(t.get("origin") or origin, default="live"),
    }


def _insert_snapshot(db: sqlite3.Connection, row: dict) -> bool:
    if not row.get("tid"):
        return False
    cur = db.execute(
        """INSERT OR IGNORE INTO token_snapshots (
             ts, ts_sec, tid, chain, ticker, age_min, price_usd, liq_usd, mcap_usd,
             vol_5m, vol_1h, buys_5m, sells_5m, buys_1h, sells_1h,
             holders, holder_growth, top_wallet_pct, top10_pct, dev_pct, dev_sold_pct,
             x_handle, x_followers, x_kind, source, trades_source, stage,
             kill_reason, noul, judge_score, judge_confidence, judge_verdict, regime,
             origin
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["ts"], row["ts_sec"], row["tid"], row.get("chain"), row.get("ticker"),
            row.get("age_min"), row.get("price_usd"), row.get("liq_usd"), row.get("mcap_usd"),
            row.get("vol_5m"), row.get("vol_1h"), row.get("buys_5m"), row.get("sells_5m"),
            row.get("buys_1h"), row.get("sells_1h"), row.get("holders"), row.get("holder_growth"),
            row.get("top_wallet_pct"), row.get("top10_pct"), row.get("dev_pct"),
            row.get("dev_sold_pct"), row.get("x_handle"), row.get("x_followers"),
            row.get("x_kind"), row.get("source"), row.get("trades_source"),
            row.get("stage") or "", row.get("kill_reason"), row.get("noul"),
            row.get("judge_score"), row.get("judge_confidence"), row.get("judge_verdict"),
            row.get("regime"), row.get("origin") or "live",
        ),
    )
    return bool(cur.rowcount)


def _regime_now() -> str | None:
    """Last Coinalyze tag: in-process snap, else cycle_history.market_regimes."""
    try:
        import coinalyze
        tag = coinalyze.last_regime()
        if tag:
            return tag
    except Exception as e:
        log.info("research regime: %s", safe_err(e))
    try:
        import cycle_history
        tag = cycle_history.latest_regime()
        if tag:
            return tag
    except Exception as e:
        log.info("research regime: %s", safe_err(e))
    return None


def on_cycle(stats: dict | None = None, tokens=None, now: float | None = None,
             source: str = "live", origin: str | None = None,
             skip_labels: bool = False) -> int:
    """Write one snapshot per recorded token this cycle. Never raises; never blocks.

    skip_labels is accepted for backfill callers; labels never run on this thread.
    """
    if not stats or "held" in stats:
        return 0
    origin = _as_origin(origin or source, default="live")
    _ = skip_labels
    ts = current_time() if now is None else float(now)
    shortlist = {}
    for t in tokens or []:
        if isinstance(t, dict) and t.get("tid"):
            shortlist[t["tid"]] = t
    regime = None
    try:
        regime = _regime_now()
    except Exception:
        regime = None
    n = 0
    try:
        with _locked() as db:
            seen = set()
            rows = list(stats.get("tokens") or [])
            for y in stats.get("young_free") or []:
                if isinstance(y, dict) and y.get("tid"):
                    rows.append({**y, "stage": y.get("stage") or "trade", "reason": y.get("reason") or "pass"})
            for raw in rows:
                if not isinstance(raw, dict) or not raw.get("tid"):
                    continue
                tid = raw["tid"]
                src = dict(shortlist.get(tid) or {})
                src.update({k: v for k, v in raw.items() if v is not None})
                stage = src.get("stage") or ""
                key = (int(ts), tid, stage)
                if key in seen:
                    continue
                seen.add(key)
                if _insert_snapshot(db, _row_from_token(
                    src, ts, stage=stage, regime=regime, origin=origin,
                )):
                    n += 1
            db.commit()
        # Labels run on the sampler thread (maybe_compute_labels), not the desk cycle.
        if not str(origin).startswith("backfill"):
            try:
                maybe_vacuum(now=ts)
            except Exception as e:
                log.warning("research vacuum: %s", safe_err(e))
    except Exception as e:
        log.warning("research on_cycle: %s", safe_err(e))
        return 0
    return n


def _insert_mark(db: sqlite3.Connection, row: dict) -> bool:
    if not row.get("tid"):
        return False
    cur = db.execute(
        """INSERT OR REPLACE INTO fast_marks (
             ts, ts_sec, tid, chain, ticker, price_usd, liq_usd, fdv,
             vol_5m, vol_1h, vol_h24, source, origin
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["ts"], row["ts_sec"], row["tid"], row.get("chain"), row.get("ticker"),
            row.get("price_usd"), row.get("liq_usd"), row.get("fdv"),
            row.get("vol_5m"), row.get("vol_1h"), row.get("vol_h24"),
            row.get("source") or "dex_batch",
            row.get("origin") or "live",
        ),
    )
    return bool(cur.rowcount)


def _open_young_trail_tids() -> list[str]:
    try:
        import young_trail
        return [r["tid"] for r in young_trail.open_rows() if r.get("tid")]
    except Exception as e:
        log.warning("research young_trail open: %s", safe_err(e))
        return []


def sampler_candidates(now: float | None = None, cap: int = BATCH_CAP) -> list[str]:
    """Open young_trail first, then tokens <120m old seen in the last 3h. Cap 30."""
    now = current_time() if now is None else float(now)
    ordered: list[str] = []
    seen: set[str] = set()

    def add(tid):
        if tid and tid not in seen:
            seen.add(tid)
            ordered.append(tid)

    for tid in _open_young_trail_tids():
        add(tid)
    try:
        with _locked() as db:
            rows = db.execute(
                """SELECT tid, MIN(age_min) AS age
                   FROM token_snapshots
                   WHERE ts >= ?
                   GROUP BY tid
                   HAVING age IS NULL OR age < ?
                   ORDER BY MAX(ts) DESC""",
                (now - SEEN_WINDOW_SEC, YOUNG_AGE_MAX_MIN),
            ).fetchall()
        for r in rows:
            add(r["tid"])
    except Exception as e:
        log.warning("research sampler candidates: %s", safe_err(e))
    return ordered[: max(0, int(cap))]


def _pairs_from_payload(payload) -> list:
    if isinstance(payload, dict):
        pairs = payload.get("pairs") or payload.get("data") or []
        return pairs if isinstance(pairs, list) else []
    if isinstance(payload, list):
        return payload
    return []


def _pair_liq(pair: dict) -> float:
    block = pair.get("liquidity") if isinstance(pair, dict) else None
    if isinstance(block, dict):
        return _num(block.get("usd")) or 0.0
    return 0.0


def _pair_vol(pair: dict, key: str) -> float | None:
    vol = pair.get("volume") if isinstance(pair, dict) else None
    if isinstance(vol, dict):
        return _num(vol.get(key))
    return None


def _best_pair_by_addr(pairs: list) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for p in pairs:
        if not isinstance(p, dict):
            continue
        base = p.get("baseToken") if isinstance(p.get("baseToken"), dict) else {}
        quote = p.get("quoteToken") if isinstance(p.get("quoteToken"), dict) else {}
        for tok in (base, quote):
            addr = (tok.get("address") or "").lower()
            if not addr:
                continue
            prev = best.get(addr)
            if prev is None or _pair_liq(p) > _pair_liq(prev):
                best[addr] = p
    return best


def fetch_dex_batch(tids: list[str]) -> dict[str, dict]:
    """DexScreener /latest/dex/tokens/<comma list up to 30>. 5s timeout. No GT."""
    addrs = []
    tid_by_addr: dict[str, list[str]] = {}
    for tid in tids[:BATCH_CAP]:
        addr, _net = parse_tid(tid)
        if not addr:
            continue
        key = addr.lower()
        if key not in tid_by_addr:
            addrs.append(addr)
            tid_by_addr[key] = []
        tid_by_addr[key].append(tid)
    if not addrs:
        return {}
    url = f"{DEX_BATCH}/{','.join(addrs)}"
    try:
        resp = _http_get(url, timeout=HTTP_TIMEOUT)
        status = getattr(resp, "status_code", None)
        if status != 200:
            log.info("research dex HTTP %s", status)
            return {}
        payload = resp.json()
    except Exception as e:
        log.warning("research dex batch: %s", safe_err(e))
        return {}
    best = _best_pair_by_addr(_pairs_from_payload(payload))
    out = {}
    for addr, tid_list in tid_by_addr.items():
        pair = best.get(addr)
        if not pair:
            continue
        px = _num(pair.get("priceUsd") or pair.get("price_usd") or pair.get("price"))
        mark = {
            "price_usd": px,
            "liq_usd": _num((pair.get("liquidity") or {}).get("usd")) if isinstance(pair.get("liquidity"), dict) else None,
            "fdv": _num(pair.get("fdv") or pair.get("marketCap")),
            "vol_5m": _pair_vol(pair, "m5"),
            "vol_1h": _pair_vol(pair, "h1"),
            "vol_h24": _pair_vol(pair, "h24"),
            "chain": pair.get("chainId"),
            "ticker": ((pair.get("baseToken") or {}) if isinstance(pair.get("baseToken"), dict) else {}).get("symbol"),
        }
        for tid in tid_list:
            out[tid] = mark
    return out


def tick_sampler(now: float | None = None) -> dict:
    """One 60s sample: up to 30 Dex batch marks. Never touches young_trail exits."""
    ts = current_time() if now is None else float(now)
    empty = {"n": 0, "marks": 0}
    try:
        tids = sampler_candidates(now=ts)
        if not tids:
            return empty
        prices = fetch_dex_batch(tids)
        n = 0
        with _locked() as db:
            for tid in tids:
                mark = prices.get(tid)
                if not mark or not mark.get("price_usd"):
                    continue
                if _insert_mark(db, {
                    "ts": ts, "ts_sec": int(ts), "tid": tid,
                    "chain": mark.get("chain") or chain_of(tid=tid),
                    "ticker": mark.get("ticker"),
                    "price_usd": mark.get("price_usd"),
                    "liq_usd": mark.get("liq_usd"),
                    "fdv": mark.get("fdv"),
                    "vol_5m": mark.get("vol_5m"),
                    "vol_1h": mark.get("vol_1h"),
                    "vol_h24": mark.get("vol_h24"),
                    "source": "dex_batch",
                    "origin": "live",
                }):
                    n += 1
            db.commit()
        log.info("research sampler n=%d marks=%d", len(tids), n)
        try:
            maybe_compute_labels(tids=tids, now=ts)
        except Exception as e:
            log.warning("research labels: %s", safe_err(e))
        return {"n": len(tids), "marks": n}
    except Exception as e:
        log.warning("research sampler: %s", safe_err(e))
        return empty


def sampler_enabled() -> bool:
    raw = os.environ.get("RESEARCH_SAMPLER_DISABLE")
    if raw is None or raw == "":
        return True
    return raw.strip().lower() not in ("1", "true", "yes", "on")


def maybe_start_sampler() -> threading.Thread | None:
    """Background Dex sampler. Fail open. Disabled by RESEARCH_SAMPLER_DISABLE=1."""
    if not sampler_enabled():
        return None
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return None

        def _loop():
            while True:
                try:
                    tick_sampler()
                except Exception as e:
                    log.warning("research sampler loop: %s", safe_err(e))
                _sleep(SAMPLE_SEC)

        t = threading.Thread(target=_loop, name="research-sampler", daemon=True)
        _worker_thread = t
        t.start()
        log.info("research sampler started")
        return t


def _price_series(tid: str) -> list[tuple[float, float | None, float | None]]:
    """(ts, price, liq) from snapshots + fast_marks + young_trail_marks, oldest first."""
    pts: list[tuple[float, float | None, float | None]] = []
    db = _connect()
    for r in db.execute(
        "SELECT ts, price_usd, liq_usd FROM token_snapshots WHERE tid = ? ORDER BY ts",
        (tid,),
    ):
        pts.append((float(r["ts"]), _num(r["price_usd"]), _num(r["liq_usd"])))
    for r in db.execute(
        "SELECT ts, price_usd, liq_usd FROM fast_marks WHERE tid = ? ORDER BY ts",
        (tid,),
    ):
        pts.append((float(r["ts"]), _num(r["price_usd"]), _num(r["liq_usd"])))
    try:
        import young_trail
        path = young_trail.db_path()
        if pathlib.Path(path).exists():
            other = sqlite3.connect(str(path), check_same_thread=False, timeout=LOCK_TIMEOUT_SEC)
            other.row_factory = sqlite3.Row
            try:
                if other.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='young_trail_marks'"
                ).fetchone():
                    for r in other.execute(
                        "SELECT ts, price FROM young_trail_marks WHERE tid = ? ORDER BY ts",
                        (tid,),
                    ):
                        pts.append((float(r["ts"]), _num(r["price"]), None))
                if other.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='young_trail'"
                ).fetchone():
                    row = other.execute(
                        "SELECT entry_ts, entry_price, peak, last_price, last_mark_ts, "
                        "exit_ts, exit_price FROM young_trail WHERE tid = ?",
                        (tid,),
                    ).fetchone()
                    if row:
                        pts.append((float(row["entry_ts"]), _num(row["entry_price"]), None))
                        if row["last_mark_ts"] is not None and row["last_price"] is not None:
                            pts.append((float(row["last_mark_ts"]), _num(row["last_price"]), None))
                        if row["exit_ts"] is not None and row["exit_price"] is not None:
                            pts.append((float(row["exit_ts"]), _num(row["exit_price"]), None))
            finally:
                other.close()
    except Exception as e:
        log.warning("research young_trail series: %s", safe_err(e))
    pts.sort(key=lambda x: x[0])
    return pts


def _price_at_or_before(series, horizon_ts: float, after_ts: float | None = None) -> float | None:
    last = None
    for ts, px, _liq in series:
        if after_ts is not None and ts < after_ts:
            continue
        if ts > horizon_ts:
            break
        if px is not None and px > 0:
            last = px
    return last


def _first_free(tid: str) -> sqlite3.Row | None:
    db = _connect()
    row = db.execute(
        """SELECT * FROM token_snapshots
           WHERE tid = ? AND stage IN ('trade','chain','soft','judged','judge','judge_error')
           ORDER BY ts ASC LIMIT 1""",
        (tid,),
    ).fetchone()
    if row:
        return row
    return db.execute(
        "SELECT * FROM token_snapshots WHERE tid = ? ORDER BY ts ASC LIMIT 1",
        (tid,),
    ).fetchone()


def _outcomes_overlay(tid: str) -> dict:
    out = {}
    try:
        import outcomes
        path = outcomes.db_path()
        if not pathlib.Path(path).exists():
            return out
        other = sqlite3.connect(str(path), check_same_thread=False, timeout=LOCK_TIMEOUT_SEC)
        other.row_factory = sqlite3.Row
        try:
            row = other.execute(
                """SELECT peak_x, trough_x, ret_6h, entry_ts FROM outcomes
                   WHERE tid = ? AND complete = 1 ORDER BY entry_ts DESC LIMIT 1""",
                (tid,),
            ).fetchone()
            if row:
                out["peak_x"] = _num(row["peak_x"])
                out["ret_6h"] = _num(row["ret_6h"])
                out["entry_ts"] = _num(row["entry_ts"])
        finally:
            other.close()
    except Exception as e:
        log.warning("research outcomes overlay: %s", safe_err(e))
    return out


def maybe_compute_labels(tids: list[str] | None = None,
                         now: float | None = None) -> int:
    """Throttle labels to every 10 min on the sampler thread. Logs elapsed."""
    global _last_labels_at
    now = current_time() if now is None else float(now)
    if _last_labels_at and (now - _last_labels_at) < LABELS_INTERVAL_SEC:
        return 0
    t0 = time.perf_counter()
    n = 0
    try:
        n = compute_labels(tids=tids, now=now)
    except Exception as e:
        log.warning("research labels: %s", safe_err(e))
    elapsed = time.perf_counter() - t0
    _last_labels_at = now
    log.info("research labels n=%d elapsed=%.3fs", n, elapsed)
    return n


def compute_labels(tids: list[str] | None = None, now: float | None = None) -> int:
    """Peak, horizons, rug flag from snapshots + fast_marks + outcomes.db."""
    now = current_time() if now is None else float(now)
    with _locked() as db:
        if tids is None:
            tids = [r["tid"] for r in db.execute("SELECT DISTINCT tid FROM token_snapshots")]
            extra = [r["tid"] for r in db.execute("SELECT DISTINCT tid FROM fast_marks")]
            for t in extra:
                if t not in tids:
                    tids.append(t)
        n = 0
        for tid in tids:
            try:
                first = db.execute(
                    "SELECT * FROM token_snapshots WHERE tid = ? ORDER BY ts ASC LIMIT 1",
                    (tid,),
                ).fetchone()
                free = _first_free(tid)
                if first is None and free is None:
                    continue
                first_seen = float((first or free)["ts"])
                first_free_ts = float(free["ts"]) if free is not None else first_seen
                ticker = (free or first)["ticker"] if (free or first) else None
                chain = (free or first)["chain"] if (free or first) else None
                series = _price_series(tid)
                entry = None
                if free is not None:
                    entry = _num(free["price_usd"])
                if entry is None:
                    entry = _price_at_or_before(series, first_free_ts + 1, after_ts=first_seen)
                overlay = _outcomes_overlay(tid)
                if entry is None and overlay.get("entry_ts"):
                    entry = _price_at_or_before(series, overlay["entry_ts"] + 1)
                peak_px = None
                peak_ts = None
                max_liq = None
                rug_ts = None
                for ts, px, liq in series:
                    if ts < first_free_ts:
                        if liq is not None:
                            max_liq = liq if max_liq is None else max(max_liq, liq)
                        continue
                    if liq is not None:
                        max_liq = liq if max_liq is None else max(max_liq, liq)
                        if max_liq and max_liq > 0 and liq <= 0.20 * max_liq and rug_ts is None:
                            rug_ts = ts
                    if px is not None and px > 0:
                        if peak_px is None or px > peak_px:
                            peak_px = px
                            peak_ts = ts
                        if entry and entry > 0 and px <= 0.01 * entry and rug_ts is None:
                            rug_ts = ts
                peak_x = (peak_px / entry) if (peak_px and entry and entry > 0) else overlay.get("peak_x")
                if peak_x is None:
                    peak_x = overlay.get("peak_x")
                rets = {}
                for name, delta in HORIZONS_SEC:
                    if now < first_free_ts + delta:
                        rets[name] = None
                        continue
                    px = _price_at_or_before(series, first_free_ts + delta, after_ts=first_free_ts)
                    if px is None or not entry or entry <= 0:
                        rets[name] = overlay.get(name) if name == "ret_6h" else None
                        if name == "ret_6h" and rets[name] is None:
                            rets[name] = overlay.get("ret_6h")
                    else:
                        rets[name] = (px / entry) - 1.0
                runner = 0
                if peak_x is not None and peak_x >= 2.0:
                    if rug_ts is None or (peak_ts is not None and peak_ts <= rug_ts):
                        runner = 1
                db.execute(
                    """INSERT OR REPLACE INTO token_labels (
                         tid, ticker, chain, first_seen_ts, first_free_ts,
                         minutes_from_first_sighting, entry_price, peak_x, peak_ts,
                         time_to_peak_min, ret_15m, ret_1h, ret_3h, ret_6h,
                         rugged, rug_ts, time_to_rug_min, runner, updated_ts, origin
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        tid, ticker, chain, first_seen, first_free_ts,
                        (first_free_ts - first_seen) / 60.0,
                        entry, peak_x, peak_ts,
                        ((peak_ts - first_free_ts) / 60.0) if peak_ts is not None else None,
                        rets.get("ret_15m"), rets.get("ret_1h"),
                        rets.get("ret_3h"), rets.get("ret_6h"),
                        1 if rug_ts is not None else 0,
                        rug_ts,
                        ((rug_ts - first_free_ts) / 60.0) if rug_ts is not None else None,
                        runner, now,
                        _label_origin(free or first),
                    ),
                )
                n += 1
            except Exception as e:
                log.warning("research label %s: %s", tid, safe_err(e))
        db.commit()
        return n


def _label_origin(row) -> str:
    try:
        return _as_origin(row["origin"] if row is not None else None, default="live")
    except Exception:
        return "live"


def _meta_get(key: str) -> str | None:
    with _locked() as db:
        row = db.execute(
            "SELECT value FROM research_meta WHERE key = ?", (key,),
        ).fetchone()
    return None if row is None else row["value"]


def _meta_set(key: str, value) -> None:
    with _locked() as db:
        db.execute(
            "INSERT OR REPLACE INTO research_meta (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        db.commit()


def maybe_vacuum(now: float | None = None) -> bool:
    """Keep everything; vacuum weekly. Fail open."""
    now = current_time() if now is None else float(now)
    raw = _meta_get("last_vacuum")
    last = _num(raw)
    if last is not None and now - last < VACUUM_INTERVAL_SEC:
        return False
    with _locked() as db:
        db.commit()
        isolation = db.isolation_level
        db.isolation_level = None
        try:
            db.execute("VACUUM")
        finally:
            db.isolation_level = isolation
    _meta_set("last_vacuum", now)
    log.info("research vacuum ok")
    return True


def _open_foreign(path: pathlib.Path) -> sqlite3.Connection | None:
    if not path or not pathlib.Path(path).exists():
        return None
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=LOCK_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,),
    ).fetchone()
    return bool(row)


def _parse_ts(v) -> float | None:
    if v is None or v == "" or v == "-":
        return None
    if isinstance(v, (int, float)):
        n = float(v)
        return n / 1000.0 if n > 1e12 else n
    if isinstance(v, str):
        try:
            n = float(v)
            return n / 1000.0 if n > 1e12 else n
        except ValueError:
            pass
        s = v.strip()
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return None
    return None


def _read_text_file(path: pathlib.Path) -> str:
    name = path.name.lower()
    if name.endswith(".gz"):
        with gzip.open(path, "rt", errors="replace") as f:
            return f.read()
    if name.endswith(".bz2"):
        with bz2.open(path, "rt", errors="replace") as f:
            return f.read()
    return path.read_text(errors="replace")


def _iter_log_lines(path: pathlib.Path):
    """Stream a run.log (plain, .gz, or .bz2) line by line. Never slurp."""
    name = path.name.lower()
    if name.endswith(".gz"):
        ctx = gzip.open(path, "rt", errors="replace")
    elif name.endswith(".bz2"):
        ctx = bz2.open(path, "rt", errors="replace")
    else:
        ctx = open(path, "rt", errors="replace")
    with ctx as f:
        for line in f:
            yield line


def _snap(dst, token, ts, *, stage, origin, regime=None) -> bool:
    return _insert_snapshot(
        dst, _row_from_token(token, ts, stage=stage, regime=regime, origin=origin),
    )


def _mark(dst, **row) -> bool:
    if row.get("ts") is None or not row.get("tid"):
        return False
    row.setdefault("ts_sec", int(row["ts"]))
    return _insert_mark(dst, row)


def backfill_from_cycle_history() -> dict:
    counts = {"cycle_history": 0, "young_free_pass": 0, "shadow_kills": 0, "coinalyze": 0}
    try:
        import cycle_history
        src = _open_foreign(cycle_history.db_path())
        if src is None:
            return counts
        dst = _connect()
        try:
            if _table_exists(src, "tokens"):
                for r in src.execute("SELECT * FROM tokens"):
                    row = dict(r)
                    token = {
                        "tid": row.get("tid"), "chain": row.get("chain"),
                        "net": row.get("chain_id"), "ticker": row.get("ticker"),
                        "age_minutes": row.get("age_minutes"),
                        "liquidity_usd": row.get("liquidity_usd"),
                        "mcap_usd": row.get("mcap_usd"),
                        "top_wallet_percent": row.get("top_wallet_percent"),
                        "top_10_percent": row.get("top_10_percent"),
                        "developer_holding_percentage": row.get("developer_holding_percentage"),
                        "holder_count": row.get("holder_count"),
                        "reason": row.get("reason"), "soft_noul": row.get("noul"),
                        "judge_score": row.get("judge_score"),
                        "judge_confidence": row.get("judge_confidence"),
                        "judge_verdict": row.get("judge_verdict"),
                        "stage": row.get("kind") or "",
                    }
                    ts = float(row["ts"]) if row.get("ts") is not None else current_time()
                    if _snap(dst, token, ts, stage=token["stage"],
                             origin="backfill:cycle_history"):
                        counts["cycle_history"] += 1
            if _table_exists(src, "young_free_pass"):
                for r in src.execute("SELECT * FROM young_free_pass"):
                    row = dict(r)
                    token = {
                        "tid": row.get("tid"), "chain": row.get("chain"),
                        "net": row.get("chain_id"), "ticker": row.get("ticker"),
                        "age_minutes": row.get("age_minutes"),
                        "liquidity_usd": row.get("liquidity_usd"),
                        "mcap_usd": row.get("mcap_usd"),
                        "reason": row.get("reason") or "pass", "stage": "trade",
                    }
                    ts = float(row["ts"]) if row.get("ts") is not None else current_time()
                    if _snap(dst, token, ts, stage="trade", origin="backfill:young_free_pass"):
                        counts["young_free_pass"] += 1
            if _table_exists(src, "shadow_kills"):
                for r in src.execute("SELECT * FROM shadow_kills"):
                    row = dict(r)
                    token = {
                        "tid": row.get("tid"), "chain": row.get("chain"),
                        "ticker": row.get("ticker"), "age_minutes": row.get("age_minutes"),
                        "liquidity_usd": row.get("liq"), "mcap_usd": row.get("mcap"),
                        "holder_count": row.get("holders"),
                        "top_wallet_percent": row.get("top_wallet_percent"),
                        "top_10_percent": row.get("top_10_percent"),
                        "developer_holding_percentage": row.get("developer_holding_percentage"),
                        "judge_score": row.get("judge_score"),
                        "reason": row.get("reason"),
                        "stage": row.get("stage") or "chain",
                        "regime": row.get("regime"),
                        "x_followers": row.get("x_followers"),
                        "x_handle": row.get("x_handle"),
                    }
                    ts = float(row["ts"]) if row.get("ts") is not None else current_time()
                    if _snap(dst, token, ts, stage=token["stage"],
                             origin="backfill:shadow_kills", regime=row.get("regime")):
                        counts["shadow_kills"] += 1
            dst.commit()
            counts["coinalyze"] = _stamp_coinalyze(src, dst)
            dst.commit()
        finally:
            src.close()
    except Exception as e:
        log.warning("research backfill history: %s", safe_err(e))
    return counts


def _stamp_coinalyze(src: sqlite3.Connection, dst: sqlite3.Connection) -> int:
    if not _table_exists(src, "market_regimes"):
        return 0
    n = 0
    try:
        for r in src.execute("SELECT ts, regime FROM market_regimes WHERE regime IS NOT NULL"):
            cur = dst.execute(
                """UPDATE token_snapshots SET regime = ?
                   WHERE regime IS NULL AND ABS(ts - ?) < 900""",
                (r["regime"], float(r["ts"])),
            )
            n += int(cur.rowcount or 0)
    except Exception as e:
        log.warning("research backfill coinalyze: %s", safe_err(e))
    return n


def discover_run_logs(outbox: pathlib.Path | None = None, extra=None) -> list[pathlib.Path]:
    """run.log plus rotated siblings (run.log.1, .gz, dated names). Oldest first."""
    root = pathlib.Path(outbox) if outbox else outbox_dir()
    found: dict[str, pathlib.Path] = {}

    def add(p: pathlib.Path):
        if p.is_file():
            found[str(p.resolve())] = p

    if extra:
        add(pathlib.Path(extra))
    patterns = (
        "run.log", "run.log.*", "run-*.log*", "run_*.log*",
        "desk.log", "desk.log.*",
        "logs/run.log", "logs/run.log.*",
    )
    for pat in patterns:
        for p in root.glob(pat):
            add(p)
    return sorted(found.values(), key=lambda p: (p.stat().st_mtime if p.exists() else 0, p.name))


def backfill_from_run_log(path=None) -> dict:
    """Ingest every discovered run.log / rotated log. Returns {origin: n}."""
    counts: dict[str, int] = {}
    try:
        import cycle_history
        paths = discover_run_logs(extra=path)
        seen_sig = set()
        for log_path in paths:
            try:
                st = log_path.stat()
                sig = (log_path.name, st.st_size, int(st.st_mtime))
            except OSError as e:
                log.warning("research read log %s: %s", log_path.name, safe_err(e))
                continue
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            origin = f"backfill:{log_path.name}"
            n = 0
            try:
                parsed, _meta = cycle_history.parse_run_log_lines(
                    _iter_log_lines(log_path),
                )
            except Exception as e:
                log.warning("research parse log %s: %s", log_path.name, safe_err(e))
                continue
            for item in parsed:
                n += on_cycle(
                    item.get("stats") or {}, now=item.get("ts"),
                    origin=origin, skip_labels=True,
                )
            n += backfill_judge_from_lines(_iter_log_lines(log_path), origin=origin)
            n += backfill_x_and_trades_from_lines(
                _iter_log_lines(log_path), origin=origin,
            )
            counts[origin] = counts.get(origin, 0) + n
    except Exception as e:
        log.warning("research backfill log: %s", safe_err(e))
    return counts


def backfill_x_and_trades_from_lines(lines, origin: str) -> int:
    """Fill x_followers / buys_1h on existing snapshots from desk log lines."""
    n = 0
    try:
        dst = _connect()
        for raw in lines:
            xm = X_LOG_RE.search(raw)
            if xm:
                tid = xm.group(1)
                handle = (xm.group(2) or "").lstrip("@")
                if handle in ("-", "", "None"):
                    handle = None
                ok = str(xm.group(3)).lower() in ("true", "1", "yes")
                followers = _int(xm.group(4)) if ok else None
                kind = xm.group(8)
                if kind in ("-", "None", ""):
                    kind = None
                if tid and tid != "-":
                    cur = dst.execute(
                        """UPDATE token_snapshots
                           SET x_followers = COALESCE(x_followers, ?),
                               x_handle = COALESCE(x_handle, ?),
                               x_kind = COALESCE(x_kind, ?)
                           WHERE tid = ?""",
                        (followers, handle, kind, tid),
                    )
                    n += int(cur.rowcount or 0)
            tm = TRADE_BUYS_RE.search(raw)
            if tm:
                tid, buys = tm.group(1), _num(tm.group(2))
                if tid and buys is not None:
                    cur = dst.execute(
                        """UPDATE token_snapshots
                           SET buys_1h = COALESCE(buys_1h, ?)
                           WHERE tid = ?""",
                        (buys, tid),
                    )
                    n += int(cur.rowcount or 0)
        dst.commit()
    except Exception as e:
        log.warning("research backfill x/trades log: %s", safe_err(e))
    return n


def backfill_judge_from_text(text: str, origin: str) -> int:
    """Catch judge tid= lines even when a cycle: summary was missing."""
    return backfill_judge_from_lines(str(text or "").splitlines(), origin)


def backfill_judge_from_lines(lines, origin: str) -> int:
    """Catch judge tid= lines even when a cycle: summary was missing."""
    n = 0
    try:
        import cycle_history
        last_ts = None
        dst = _connect()
        for raw in lines:
            ts = cycle_history.parse_log_ts(raw)
            if ts is not None:
                last_ts = ts
            m = cycle_history.JUDGE_RE.search(raw)
            if not m:
                continue
            when = last_ts if last_ts is not None else current_time()
            tid = m.group(1)
            verdict = m.group(3)
            token = {
                "tid": tid, "ticker": m.group(2),
                "stage": "judge_error" if verdict == "error" else "judged",
                "judge_verdict": verdict,
                "judge_score": _num(m.group(4)),
                "judge_confidence": _num(m.group(5)),
                "net": None,
            }
            if _snap(dst, token, when, stage=token["stage"], origin=origin):
                n += 1
        dst.commit()
    except Exception as e:
        log.warning("research backfill judge: %s", safe_err(e))
    return n


def _iter_json_objs(path: pathlib.Path):
    raw = _read_text_file(path).strip()
    if not raw:
        return
    if path.suffix == ".jsonl" or path.name.endswith(".jsonl"):
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj
        return
    try:
        obj = json.loads(raw)
    except Exception:
        return
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                yield item
    elif isinstance(obj, dict):
        yield obj


def _tokens_from_json_obj(obj: dict) -> tuple[float | None, list[dict]]:
    ts = _parse_ts(obj.get("ts") or obj.get("updated_at") or obj.get("generated_at"))
    tokens = obj.get("tokens")
    if not isinstance(tokens, list):
        stats = obj.get("stats") if isinstance(obj.get("stats"), dict) else {}
        tokens = stats.get("tokens") if isinstance(stats.get("tokens"), list) else []
        if ts is None:
            ts = _parse_ts(stats.get("ts"))
        cycle = obj.get("cycle") if isinstance(obj.get("cycle"), dict) else {}
        if ts is None:
            ts = _parse_ts(cycle.get("ts") or obj.get("updated_at"))
    out = [t for t in (tokens or []) if isinstance(t, dict)]
    pick = obj.get("pick") or obj.get("order")
    if isinstance(pick, dict) and pick.get("token"):
        tok = pick["token"]
        if isinstance(tok, dict):
            addr = tok.get("address") or tok.get("addr")
            net = tok.get("network_id") if tok.get("network_id") is not None else tok.get("net")
            tid = tok.get("tid") or (f"{addr}:{net}" if addr is not None and net is not None else None)
            out.append({
                **tok, "tid": tid, "stage": tok.get("stage") or "judged",
                "price_usd": tok.get("price_usd") or tok.get("price"),
            })
    return ts, out


def discover_cycle_json(outbox: pathlib.Path | None = None) -> list[pathlib.Path]:
    root = pathlib.Path(outbox) if outbox else outbox_dir()
    found: dict[str, pathlib.Path] = {}

    def add(p: pathlib.Path):
        if p.is_file() and p.suffix.lower() in (".json", ".jsonl"):
            found[str(p.resolve())] = p

    add(root / "state.json")
    for pat in ("history/*.json", "history/**/*.json", "cycles/*.json",
                "cycle*.json", "history*.json"):
        for p in root.glob(pat):
            add(p)
    return sorted(found.values(), key=lambda p: p.name)


def backfill_from_cycle_json() -> dict:
    counts: dict[str, int] = {}
    dst = _connect()
    try:
        for path in discover_cycle_json():
            origin = f"backfill:{path.name}"
            n = 0
            try:
                for obj in _iter_json_objs(path):
                    ts, tokens = _tokens_from_json_obj(obj)
                    when = ts if ts is not None else current_time()
                    for t in tokens:
                        tid = t.get("tid")
                        if not tid:
                            addr = t.get("addr") or t.get("address")
                            net = t.get("net") if t.get("net") is not None else t.get("network_id")
                            if addr is not None and net is not None:
                                tid = f"{addr}:{net}"
                                t = {**t, "tid": tid}
                        if not tid:
                            continue
                        stage = t.get("stage") or "free"
                        if _snap(dst, t, when, stage=stage, origin=origin):
                            n += 1
            except Exception as e:
                log.warning("research backfill json %s: %s", path.name, safe_err(e))
            if n:
                counts[origin] = n
        dst.commit()
    except Exception as e:
        log.warning("research backfill json: %s", safe_err(e))
    return counts


def backfill_from_shadow_jsonl() -> int:
    n = 0
    dst = _connect()
    try:
        for name in ("shadow.jsonl", "shadow_ledger.jsonl"):
            path = outbox_dir() / name
            env = os.environ.get("SHADOW_LOG") if name == "shadow.jsonl" else None
            if env:
                path = pathlib.Path(env)
            if not path.exists():
                continue
            origin = f"backfill:{path.name}"
            for obj in _iter_json_objs(path):
                try:
                    ts = _parse_ts(obj.get("ts")) or current_time()
                    if obj.get("order"):
                        order = obj["order"]
                        tok = (order or {}).get("token") or {}
                        addr = tok.get("address") or tok.get("addr")
                        net = tok.get("network_id") if tok.get("network_id") is not None else tok.get("net")
                        tid = tok.get("tid") or (
                            f"{addr}:{net}" if addr is not None and net is not None else None
                        )
                        token = {
                            **tok, "tid": tid, "stage": "judged",
                            "judge_confidence": order.get("confidence"),
                            "price_usd": tok.get("price_usd") or tok.get("price"),
                        }
                        if _snap(dst, token, ts, stage="judged", origin=origin):
                            n += 1
                    action = obj.get("action")
                    addr = obj.get("address")
                    net = obj.get("network_id")
                    tid = obj.get("tid") or (
                        f"{addr}:{net}" if addr is not None and net is not None else None
                    )
                    if not tid:
                        continue
                    px = _num(obj.get("entry_price_usd") or obj.get("current_price_usd")
                              or obj.get("exit_price_usd") or obj.get("last_price_usd"))
                    token = {
                        "tid": tid, "ticker": obj.get("ticker"), "chain": obj.get("chain"),
                        "net": net, "price_usd": px,
                        "stage": "judged" if action == "entry" else "trade",
                    }
                    if action in ("entry", None) and _snap(
                        dst, token, ts, stage=token["stage"], origin=origin,
                    ):
                        n += 1
                    if px and action in ("mark", "close", "entry"):
                        _mark(dst, ts=ts, tid=tid, chain=obj.get("chain"),
                              ticker=obj.get("ticker"), price_usd=px,
                              source="shadow_ledger", origin=origin)
                except Exception as e:
                    log.warning("research shadow jsonl: %s", safe_err(e))
        dst.commit()
    except Exception as e:
        log.warning("research backfill shadow jsonl: %s", safe_err(e))
    return n


def backfill_from_young_trail() -> dict:
    counts = {"young_trail": 0, "young_trail_variants": 0}
    try:
        import young_trail
        src = _open_foreign(young_trail.db_path())
        if src is None:
            return counts
        dst = _connect()
        try:
            if _table_exists(src, "young_trail"):
                for r in src.execute("SELECT * FROM young_trail"):
                    row = dict(r)
                    tid = row.get("tid")
                    ts = float(row["entry_ts"])
                    token = {
                        "tid": tid, "ticker": row.get("ticker"), "chain": row.get("chain"),
                        "price_usd": row.get("entry_price"), "stage": "trade", "reason": "pass",
                    }
                    if _snap(dst, token, ts, stage="trade", origin="backfill:young_trail"):
                        counts["young_trail"] += 1
                    if row.get("last_mark_ts") is not None and row.get("last_price"):
                        _mark(dst, ts=float(row["last_mark_ts"]), tid=tid,
                              chain=row.get("chain"), ticker=row.get("ticker"),
                              price_usd=_num(row["last_price"]),
                              source="young_trail", origin="backfill:young_trail")
                    if row.get("exit_ts") is not None and row.get("exit_price"):
                        _mark(dst, ts=float(row["exit_ts"]), tid=tid,
                              chain=row.get("chain"), ticker=row.get("ticker"),
                              price_usd=_num(row["exit_price"]),
                              source="young_trail_exit", origin="backfill:young_trail")
            if _table_exists(src, "young_trail_marks"):
                for r in src.execute("SELECT * FROM young_trail_marks"):
                    _mark(dst, ts=float(r["ts"]), tid=r["tid"],
                          price_usd=_num(r["price"]),
                          source="young_trail_marks", origin="backfill:young_trail")
            if _table_exists(src, "young_trail_variants"):
                for r in src.execute("SELECT * FROM young_trail_variants"):
                    row = dict(r)
                    tid = row.get("tid")
                    if row.get("exit_ts") and row.get("exit_price"):
                        _mark(dst, ts=float(row["exit_ts"]), tid=tid,
                              price_usd=_num(row["exit_price"]),
                              source=f"ytv:{row.get('variant')}",
                              origin="backfill:young_trail_variants")
                        counts["young_trail_variants"] += 1
                    elif row.get("last_price"):
                        counts["young_trail_variants"] += 1
            if _table_exists(src, "young_trail_entry_stats"):
                for r in src.execute("SELECT * FROM young_trail_entry_stats"):
                    if r["peak_ts"] is not None and r["peak"] is not None:
                        _mark(dst, ts=float(r["peak_ts"]), tid=r["tid"],
                              price_usd=_num(r["peak"]),
                              source="ytv_peak", origin="backfill:young_trail_variants")
            dst.commit()
        finally:
            src.close()
    except Exception as e:
        log.warning("research backfill young_trail: %s", safe_err(e))
    return counts


def backfill_from_outcomes() -> int:
    n = 0
    try:
        import outcomes
        src = _open_foreign(outcomes.db_path())
        if src is None or not _table_exists(src, "outcomes"):
            if src:
                src.close()
            return 0
        dst = _connect()
        try:
            for r in src.execute("SELECT tid, entry_ts, candles_json, peak_x FROM outcomes"):
                tid = r["tid"]
                entry_ts = float(r["entry_ts"]) if r["entry_ts"] is not None else None
                token = {"tid": tid, "stage": "trade", "reason": "pass"}
                if entry_ts is not None and _snap(
                    dst, token, entry_ts, stage="trade", origin="backfill:outcomes",
                ):
                    n += 1
                raw = r["candles_json"]
                if not raw:
                    continue
                try:
                    candles = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    continue
                if not isinstance(candles, list):
                    continue
                for c in candles:
                    if not isinstance(c, dict):
                        continue
                    ts = _parse_ts(c.get("ts") or c.get("time"))
                    px = _num(c.get("close") or c.get("high") or c.get("open"))
                    if ts is None or not px:
                        continue
                    _mark(dst, ts=ts, tid=tid, price_usd=px,
                          source="outcomes_candle", origin="backfill:outcomes")
                    n += 1
            dst.commit()
        finally:
            src.close()
    except Exception as e:
        log.warning("research backfill outcomes: %s", safe_err(e))
    return n


def backfill_from_x_cache() -> int:
    n = 0
    try:
        import x_lookup
        src = _open_foreign(x_lookup.db_path())
        if src is None or not _table_exists(src, "x_cache"):
            if src:
                src.close()
            return 0
        dst = _connect()
        try:
            for r in src.execute("SELECT handle, payload_json, ok, fetched_at FROM x_cache WHERE ok = 1"):
                handle = (r["handle"] or "").lstrip("@").lower()
                if not handle:
                    continue
                try:
                    payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
                except Exception:
                    payload = {}
                if not isinstance(payload, dict):
                    continue
                followers = _int(payload.get("followers"))
                kind = payload.get("x_kind")
                cur = dst.execute(
                    """UPDATE token_snapshots
                       SET x_followers = COALESCE(x_followers, ?),
                           x_kind = COALESCE(x_kind, ?),
                           x_handle = COALESCE(x_handle, ?)
                       WHERE lower(COALESCE(x_handle, '')) = ?
                          OR lower(COALESCE(x_handle, '')) = ?""",
                    (followers, kind, handle, handle, "@" + handle),
                )
                n += int(cur.rowcount or 0)
            dst.commit()
        finally:
            src.close()
    except Exception as e:
        log.warning("research backfill x_cache: %s", safe_err(e))
    return n


def queue_max() -> int:
    raw = os.environ.get("RESEARCH_QUEUE_MAX")
    if raw is None or str(raw).strip() == "":
        return QUEUE_MAX_DEFAULT
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return QUEUE_MAX_DEFAULT


def queue_missing_prices(now: float | None = None) -> dict:
    """Enqueue backfill outcomes jobs for tids with no post-sighting price.

    Cap RESEARCH_QUEUE_MAX (default 200) newest-first. Skip tids that already
    have an outcomes row or fetch_jobs row. Jobs are priority=backfill.
    """
    now = current_time() if now is None else float(now)
    empty = {"queued": 0, "skipped_by_cap": 0, "skipped_existing": 0}
    try:
        import outcomes
        with _locked() as db:
            rows = db.execute(
                """SELECT tid, MIN(ts) AS first_ts FROM token_snapshots
                   GROUP BY tid ORDER BY first_ts DESC"""
            ).fetchall()
            missing = []
            for r in rows:
                tid, first = r["tid"], r["first_ts"]
                if not tid or first is None:
                    continue
                later = db.execute(
                    """SELECT 1 FROM token_snapshots
                       WHERE tid = ? AND ts > ? AND price_usd IS NOT NULL AND price_usd > 0
                       UNION ALL
                       SELECT 1 FROM fast_marks
                       WHERE tid = ? AND ts > ? AND price_usd IS NOT NULL AND price_usd > 0
                       LIMIT 1""",
                    (tid, float(first), tid, float(first)),
                ).fetchone()
                if later:
                    continue
                missing.append((str(tid), float(first)))
        existing = set()
        try:
            existing = outcomes.queued_or_fetched_tids()
        except Exception as e:
            log.warning("research existing jobs: %s", safe_err(e))
        skipped_existing = 0
        to_queue = []
        for tid, first in missing:
            if tid in existing:
                skipped_existing += 1
                continue
            to_queue.append((tid, first))
        cap = queue_max()
        skipped_by_cap = max(0, len(to_queue) - cap)
        to_queue = to_queue[:cap]
        n = 0
        for tid, first in to_queue:
            try:
                if outcomes.enqueue_job(
                    tid, first, now=now, priority=outcomes.PRIORITY_BACKFILL,
                ):
                    n += 1
                    existing.add(tid)
            except Exception as e:
                log.warning("research enqueue %s: %s", tid, safe_err(e))
        log.info(
            "research queued outcomes jobs=%d skipped_existing=%d skipped_by_cap=%d cap=%d",
            n, skipped_existing, skipped_by_cap, cap,
        )
        return {
            "queued": n,
            "skipped_by_cap": skipped_by_cap,
            "skipped_existing": skipped_existing,
        }
    except Exception as e:
        log.warning("research queue prices: %s", safe_err(e))
        return empty


def coverage_report() -> dict:
    """How far back data goes and row counts per origin."""
    empty = {
        "span": {"min_ts": None, "max_ts": None, "min_iso": None, "max_iso": None, "days": None},
        "snapshots_by_origin": {},
        "marks_by_origin": {},
        "token_snapshots": 0, "fast_marks": 0, "token_labels": 0,
    }
    try:
        db = _connect()
        def n(table):
            return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        span = db.execute(
            """SELECT MIN(ts) AS min_ts, MAX(ts) AS max_ts FROM (
                 SELECT ts FROM token_snapshots UNION ALL SELECT ts FROM fast_marks
               )"""
        ).fetchone()
        min_ts = span["min_ts"] if span else None
        max_ts = span["max_ts"] if span else None
        days = None
        if min_ts is not None and max_ts is not None:
            days = max(0.0, (float(max_ts) - float(min_ts)) / 86400.0)
        snaps = {r["origin"] or "live": r["n"] for r in db.execute(
            "SELECT origin, COUNT(*) AS n FROM token_snapshots GROUP BY origin"
        )}
        marks = {r["origin"] or "live": r["n"] for r in db.execute(
            "SELECT origin, COUNT(*) AS n FROM fast_marks GROUP BY origin"
        )}
        return {
            "span": {
                "min_ts": min_ts, "max_ts": max_ts,
                "min_iso": _iso(float(min_ts)) if min_ts is not None else None,
                "max_iso": _iso(float(max_ts)) if max_ts is not None else None,
                "days": days,
            },
            "snapshots_by_origin": snaps,
            "marks_by_origin": marks,
            "token_snapshots": n("token_snapshots"),
            "fast_marks": n("fast_marks"),
            "token_labels": n("token_labels"),
        }
    except Exception as e:
        log.warning("research coverage: %s", safe_err(e))
        return empty


def backfill(log_path=None) -> dict:
    """Ingest all available history as far back as it goes. Idempotent."""
    per_source: dict[str, int] = {}

    def add(bag):
        if isinstance(bag, dict):
            for k, v in bag.items():
                if isinstance(v, int):
                    key = k if str(k).startswith("backfill:") or k in (
                        "cycle_history", "young_free_pass", "shadow_kills",
                        "coinalyze", "young_trail", "young_trail_variants",
                    ) else f"backfill:{k}"
                    if k in ("cycle_history", "young_free_pass", "shadow_kills",
                             "coinalyze", "young_trail", "young_trail_variants"):
                        key = f"backfill:{k}"
                    per_source[key] = per_source.get(key, 0) + int(v)
        elif isinstance(bag, int):
            return bag
        return 0

    add(backfill_from_cycle_history())
    add(backfill_from_run_log(log_path))
    add(backfill_from_cycle_json())
    shadow_n = backfill_from_shadow_jsonl()
    per_source["backfill:shadow_jsonl"] = per_source.get("backfill:shadow_jsonl", 0) + shadow_n
    add(backfill_from_young_trail())
    out_n = backfill_from_outcomes()
    per_source["backfill:outcomes"] = per_source.get("backfill:outcomes", 0) + out_n
    x_n = backfill_from_x_cache()
    per_source["backfill:x_cache"] = per_source.get("backfill:x_cache", 0) + x_n
    try:
        labels = compute_labels()
    except Exception as e:
        log.warning("research backfill labels: %s", safe_err(e))
        labels = 0
    queued = 0
    skipped_by_cap = 0
    try:
        q = queue_missing_prices()
        if isinstance(q, dict):
            queued = int(q.get("queued") or 0)
            skipped_by_cap = int(q.get("skipped_by_cap") or 0)
        else:
            queued = int(q or 0)
    except Exception as e:
        log.warning("research queue: %s", safe_err(e))
    cov = coverage_report()
    result = {
        "per_source": per_source,
        "span": cov.get("span"),
        "snapshots_by_origin": cov.get("snapshots_by_origin"),
        "marks_by_origin": cov.get("marks_by_origin"),
        "token_snapshots": cov.get("token_snapshots", 0),
        "fast_marks": cov.get("fast_marks", 0),
        "token_labels": labels if labels else cov.get("token_labels", 0),
        "queued_outcomes": queued,
        "skipped_by_cap": skipped_by_cap,
        "labels": labels,
    }
    log.info(
        "research backfill span=%s..%s days=%s snapshots=%s marks=%s labels=%s queued=%s skipped_by_cap=%s sources=%s",
        (cov.get("span") or {}).get("min_iso"), (cov.get("span") or {}).get("max_iso"),
        (cov.get("span") or {}).get("days"),
        result["token_snapshots"], result["fast_marks"], result["token_labels"],
        queued, skipped_by_cap, per_source,
    )
    return result


def counts_now() -> dict:
    try:
        cov = coverage_report()
        return {
            "token_snapshots": cov.get("token_snapshots", 0),
            "fast_marks": cov.get("fast_marks", 0),
            "token_labels": cov.get("token_labels", 0),
        }
    except Exception as e:
        log.warning("research counts: %s", safe_err(e))
        return {"token_snapshots": 0, "fast_marks": 0, "token_labels": 0}


def _assert_select(sql: str) -> str:
    if not isinstance(sql, str):
        raise ValueError("sql must be a string")
    stripped = re.sub(r"--.*?$", "", sql, flags=re.M)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    s = stripped.strip().rstrip(";").strip()
    if not s:
        raise ValueError("empty sql")
    if ";" in s:
        raise ValueError("one statement only")
    first = s.split(None, 1)[0].upper()
    if first not in ("SELECT", "WITH"):
        raise ValueError("SELECT only")
    if WRITE_RE.search(s):
        raise ValueError("SELECT only")
    return s


def readonly_connect() -> sqlite3.Connection:
    path = db_path()
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    _connect()  # ensure schema exists
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=LOCK_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def run_sql(sql: str, limit: int = SQL_ROW_LIMIT) -> list[dict]:
    """Read-only SELECT. Caps rows at 500."""
    s = _assert_select(sql)
    cap = max(1, min(int(limit), SQL_ROW_LIMIT))
    wrapped = f"SELECT * FROM ({s}) LIMIT {cap}"
    conn = readonly_connect()
    try:
        rows = conn.execute(wrapped).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _median(vals: list[float]) -> float | None:
    xs = sorted(v for v in vals if v is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    if len(xs) % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


def _first_free_features(tid: str) -> dict | None:
    row = _first_free(tid)
    return dict(row) if row is not None else None


FEATURE_KEYS = (
    "age_min", "liq_usd", "mcap_usd", "holders", "top_wallet_pct", "top10_pct",
    "dev_pct", "x_followers", "noul", "vol_1h", "buys_1h", "price_usd",
)


def report(kind: str) -> dict:
    """Canned rugs | runners | features. Features compared at first free-pass."""
    kind = (kind or "").strip().lower()
    compute_labels()
    db = _connect()
    if kind == "rugs":
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM token_labels WHERE rugged = 1 ORDER BY time_to_rug_min ASC"
        )]
        return {"kind": "rugs", "n": len(rows), "rows": rows[:200]}
    if kind == "runners":
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM token_labels WHERE runner = 1 ORDER BY peak_x DESC"
        )]
        return {"kind": "runners", "n": len(rows), "rows": rows[:200]}
    if kind == "features":
        runners, rugs = [], []
        for r in db.execute("SELECT tid, runner, rugged FROM token_labels"):
            feat = _first_free_features(r["tid"])
            if not feat:
                continue
            if r["runner"]:
                runners.append(feat)
            if r["rugged"]:
                rugs.append(feat)
        def agg(group):
            out = {"n": len(group)}
            for k in FEATURE_KEYS:
                out[k] = _median([_num(g.get(k)) for g in group])
            return out
        return {
            "kind": "features",
            "note": "Median features at first free-pass. Runners = peak>=2x before rug.",
            "runners": agg(runners),
            "rugs": agg(rugs),
        }
    raise ValueError("report kind must be rugs, runners, or features")


def early_signals() -> dict:
    """Briefing payload: runners vs rugs at first free-pass."""
    try:
        feat = report("features")
        rugs = report("rugs")
        runners = report("runners")
        return {
            "runners_n": runners.get("n", 0),
            "rugs_n": rugs.get("n", 0),
            "features": feat,
        }
    except Exception as e:
        log.warning("research early_signals: %s", safe_err(e))
        return {"runners_n": 0, "rugs_n": 0, "features": {}}


def briefing_lines(payload: dict | None = None) -> list[str]:
    p = payload if payload is not None else early_signals()
    feat = (p.get("features") or {})
    run = (feat.get("runners") or {})
    rug = (feat.get("rugs") or {})
    def fmt(v, money=False):
        if v is None:
            return "—"
        if money:
            return f"${v:,.0f}"
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)
    return [
        f"- Runners (peak≥2x before rug): {p.get('runners_n', 0)} · "
        f"Rugs (liq≤20% of max or price≤1% of entry): {p.get('rugs_n', 0)}",
        f"- At first free-pass, median liq runners {fmt(run.get('liq_usd'), True)} "
        f"vs rugs {fmt(rug.get('liq_usd'), True)}; holders "
        f"{fmt(run.get('holders'))} vs {fmt(rug.get('holders'))}; "
        f"top wallet {fmt(run.get('top_wallet_pct'))} vs {fmt(rug.get('top_wallet_pct'))}; "
        f"age {fmt(run.get('age_min'))}m vs {fmt(rug.get('age_min'))}m",
        "- Research only. Does not change kills, judge, pick, or ticket size.",
    ]


def ops_query(sql: str) -> dict:
    """GET /ops/research/query payload. Fail open."""
    try:
        rows = run_sql(sql)
        return {"rows": rows, "n": len(rows), "limit": SQL_ROW_LIMIT}
    except ValueError as e:
        return {"error": str(e), "rows": [], "n": 0, "limit": SQL_ROW_LIMIT}
    except Exception as e:
        log.warning("research query: %s", safe_err(e))
        return {"error": "query failed", "rows": [], "n": 0, "limit": SQL_ROW_LIMIT}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Query outbox/research.db (read-only SELECT)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_sql = sub.add_parser("sql", help="Run a read-only SELECT")
    p_sql.add_argument("query")
    p_rep = sub.add_parser("report", help="Canned rugs|runners|features")
    p_rep.add_argument("kind", choices=("rugs", "runners", "features"))
    p_bf = sub.add_parser("backfill", help="Ingest all available history (logs, JSON, ledgers, outcomes)")
    p_bf.add_argument("log_path", nargs="?", default=None)
    sub.add_parser("labels", help="Recompute token_labels")
    sub.add_parser("vacuum", help="VACUUM (weekly is automatic)")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "sql":
            rows = run_sql(args.query)
            print(json.dumps(rows, default=str))
            return 0
        if args.cmd == "report":
            print(json.dumps(report(args.kind), default=str, indent=2))
            return 0
        if args.cmd == "backfill":
            print(json.dumps(backfill(args.log_path), default=str, indent=2))
            return 0
        if args.cmd == "labels":
            print(json.dumps({"labels": compute_labels(), **counts_now()}, default=str))
            return 0
        if args.cmd == "vacuum":
            print(json.dumps({"vacuum": maybe_vacuum(), **counts_now()}, default=str))
            return 0
    except Exception as e:
        log.error("research cli: %s", safe_err(e))
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    sys.exit(main())
