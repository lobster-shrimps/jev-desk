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
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import sqlite3
import sys
import threading
import time

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

_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_get_fn = None
_sleep_fn = None
_time_fn = time.time
_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None


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
    global _conn, _conn_path, _worker_thread
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None
    _conn_path = None
    with _worker_lock:
        _worker_thread = None


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
      regime TEXT
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
      updated_ts REAL
    );
    CREATE INDEX IF NOT EXISTS idx_labels_runner ON token_labels(runner);
    CREATE INDEX IF NOT EXISTS idx_labels_rugged ON token_labels(rugged);

    CREATE TABLE IF NOT EXISTS research_meta (
      key TEXT PRIMARY KEY,
      value TEXT
    );
    """)
    db.commit()


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
    return handle, _int(followers), kind


def _row_from_token(token: dict, ts: float, *, stage=None, regime=None) -> dict:
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
        "vol_5m": _num(t.get("vol_5m") or t.get("volume_m5")),
        "vol_1h": _num(t.get("vol_1h") or t.get("volume_h1")),
        "buys_5m": _num(t.get("buys_m5") or t.get("buys_5m")),
        "sells_5m": _num(t.get("sells_m5") or t.get("sells_5m")),
        "buys_1h": _num(t.get("buys_h1") or t.get("buys_1h")),
        "sells_1h": _num(t.get("sells_h1") or t.get("sells_1h")),
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
             kill_reason, noul, judge_score, judge_confidence, judge_verdict, regime
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            row.get("regime"),
        ),
    )
    return bool(cur.rowcount)


def _regime_now() -> str | None:
    try:
        import coinalyze
        snap = coinalyze.latest()
        if isinstance(snap, dict):
            return snap.get("regime")
    except Exception as e:
        log.info("research regime: %s", safe_err(e))
    return None


def on_cycle(stats: dict | None = None, tokens=None, now: float | None = None,
             source: str = "live") -> int:
    """Write one snapshot per recorded token this cycle. Never raises; never blocks."""
    if not stats or "held" in stats:
        return 0
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
        db = _connect()
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
            if _insert_snapshot(db, _row_from_token(src, ts, stage=stage, regime=regime)):
                n += 1
        db.commit()
        try:
            tids = [tid for (_ts, tid, _st) in seen]
            if tids:
                compute_labels(tids=tids, now=ts)
        except Exception as e:
            log.warning("research labels: %s", safe_err(e))
        if source != "backfill":
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
             vol_5m, vol_1h, vol_h24, source
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["ts"], row["ts_sec"], row["tid"], row.get("chain"), row.get("ticker"),
            row.get("price_usd"), row.get("liq_usd"), row.get("fdv"),
            row.get("vol_5m"), row.get("vol_1h"), row.get("vol_h24"),
            row.get("source") or "dex_batch",
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
        db = _connect()
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
        db = _connect()
        n = 0
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
            }):
                n += 1
        db.commit()
        log.info("research sampler n=%d marks=%d", len(tids), n)
        try:
            compute_labels(tids=tids, now=ts)
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


def compute_labels(tids: list[str] | None = None, now: float | None = None) -> int:
    """Peak, horizons, rug flag from snapshots + fast_marks + outcomes.db."""
    now = current_time() if now is None else float(now)
    db = _connect()
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
                     rugged, rug_ts, time_to_rug_min, runner, updated_ts
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                ),
            )
            n += 1
        except Exception as e:
            log.warning("research label %s: %s", tid, safe_err(e))
    db.commit()
    return n


def _meta_get(key: str) -> str | None:
    row = _connect().execute(
        "SELECT value FROM research_meta WHERE key = ?", (key,),
    ).fetchone()
    return None if row is None else row["value"]


def _meta_set(key: str, value) -> None:
    db = _connect()
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
    db = _connect()
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


def backfill_from_cycle_history() -> int:
    n = 0
    try:
        import cycle_history
        src = _open_foreign(cycle_history.db_path())
        if src is None:
            return 0
        dst = _connect()
        try:
            if _table_exists(src, "tokens"):
                for r in src.execute("SELECT * FROM tokens"):
                    row = dict(r)
                    token = {
                        "tid": row.get("tid"),
                        "chain": row.get("chain"),
                        "net": row.get("chain_id"),
                        "ticker": row.get("ticker"),
                        "age_minutes": row.get("age_minutes"),
                        "liquidity_usd": row.get("liquidity_usd"),
                        "mcap_usd": row.get("mcap_usd"),
                        "top_wallet_percent": row.get("top_wallet_percent"),
                        "top_10_percent": row.get("top_10_percent"),
                        "developer_holding_percentage": row.get("developer_holding_percentage"),
                        "holder_count": row.get("holder_count"),
                        "reason": row.get("reason"),
                        "soft_noul": row.get("noul"),
                        "judge_score": row.get("judge_score"),
                        "judge_confidence": row.get("judge_confidence"),
                        "judge_verdict": row.get("judge_verdict"),
                        "stage": row.get("kind") or "",
                    }
                    ts = float(row["ts"]) if row.get("ts") is not None else current_time()
                    if _insert_snapshot(dst, _row_from_token(token, ts, stage=token["stage"])):
                        n += 1
            if _table_exists(src, "young_free_pass"):
                for r in src.execute("SELECT * FROM young_free_pass"):
                    row = dict(r)
                    token = {
                        "tid": row.get("tid"), "chain": row.get("chain"),
                        "net": row.get("chain_id"), "ticker": row.get("ticker"),
                        "age_minutes": row.get("age_minutes"),
                        "liquidity_usd": row.get("liquidity_usd"),
                        "mcap_usd": row.get("mcap_usd"),
                        "reason": row.get("reason") or "pass",
                        "stage": "trade",
                    }
                    ts = float(row["ts"]) if row.get("ts") is not None else current_time()
                    if _insert_snapshot(dst, _row_from_token(token, ts, stage="trade")):
                        n += 1
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
                    }
                    ts = float(row["ts"]) if row.get("ts") is not None else current_time()
                    if _insert_snapshot(dst, _row_from_token(token, ts, stage=token["stage"],
                                                            regime=row.get("regime"))):
                        n += 1
            dst.commit()
        finally:
            src.close()
    except Exception as e:
        log.warning("research backfill history: %s", safe_err(e))
    return n


def backfill_from_run_log(path=None) -> int:
    n = 0
    try:
        import cycle_history
        log_path = pathlib.Path(path) if path else outbox_dir() / "run.log"
        if not log_path.exists():
            return 0
        parsed, _meta = cycle_history.parse_run_log(log_path.read_text(errors="replace"))
        for item in parsed:
            n += on_cycle(item.get("stats") or {}, now=item.get("ts"), source="backfill")
    except Exception as e:
        log.warning("research backfill log: %s", safe_err(e))
    return n


def backfill_from_young_trail() -> int:
    n = 0
    try:
        import young_trail
        src = _open_foreign(young_trail.db_path())
        if src is None:
            return 0
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
                    if _insert_snapshot(dst, _row_from_token(token, ts, stage="trade")):
                        n += 1
                    if row.get("last_mark_ts") is not None and row.get("last_price"):
                        _insert_mark(dst, {
                            "ts": float(row["last_mark_ts"]),
                            "ts_sec": int(row["last_mark_ts"]),
                            "tid": tid, "chain": row.get("chain"), "ticker": row.get("ticker"),
                            "price_usd": _num(row["last_price"]),
                            "liq_usd": None, "fdv": None, "vol_5m": None, "vol_1h": None,
                            "vol_h24": None, "source": "young_trail",
                        })
            if _table_exists(src, "young_trail_marks"):
                for r in src.execute("SELECT * FROM young_trail_marks"):
                    _insert_mark(dst, {
                        "ts": float(r["ts"]), "ts_sec": int(r["ts"]),
                        "tid": r["tid"], "price_usd": _num(r["price"]),
                        "liq_usd": None, "fdv": None, "vol_5m": None, "vol_1h": None,
                        "vol_h24": None, "source": "young_trail_marks",
                    })
            dst.commit()
        finally:
            src.close()
    except Exception as e:
        log.warning("research backfill young_trail: %s", safe_err(e))
    return n


def backfill(log_path=None) -> dict:
    """Idempotent ingest from cycle_history, run.log, young_trail, outcomes."""
    hist = backfill_from_cycle_history()
    logn = backfill_from_run_log(log_path)
    yt = backfill_from_young_trail()
    try:
        labels = compute_labels()
    except Exception as e:
        log.warning("research backfill labels: %s", safe_err(e))
        labels = 0
    counts = counts_now()
    result = {
        "history_snapshots": hist,
        "log_snapshots": logn,
        "young_trail": yt,
        "labels": labels,
        **counts,
    }
    log.info("research backfill %s", result)
    return result


def counts_now() -> dict:
    try:
        db = _connect()
        def n(table):
            return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return {
            "token_snapshots": n("token_snapshots"),
            "fast_marks": n("fast_marks"),
            "token_labels": n("token_labels"),
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
    p_bf = sub.add_parser("backfill", help="Ingest history / run.log / young_trail")
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
