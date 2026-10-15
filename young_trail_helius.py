"""Paper Helius rug-signal ledger beside young_trail.

Copies Solana young_trail entries from deploy onward (same tid + entry price).
At entry, snapshots the FOMO creator (if already known), top-10 holders, and
the LP/pool account. On each 60s sampler tick, budgeted Helius RPC checks
for: dev sold ≥20%, any top-3 holder sold ≥30%, pool SOL reserve dropped ≥25%.
First fire records a paper exit. Signals are joined to token_labels / outcomes
for precision, recall, and lead time.

Does NOT change young_trail, fast, variants, judging, or live orders.
Fail open. Exceptions through safe_err. Never logs SOLANA_RPC_URL.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sqlite3
import threading
import time

from secret_utils import safe_err

log = logging.getLogger("young_trail_helius")

TICKET_USD = 50.0
DEV_SELL_PCT = 0.20
TOP3_SELL_PCT = 0.30
POOL_SOL_DROP_PCT = 0.25
WINDOW_SEC = 6 * 3600
FRESH_MARK_SEC = 15 * 60
LOCK_TIMEOUT_SEC = 0.2
RPC_TIMEOUT_SEC = 8.0
DEFAULT_UNTIL_ISO = "2026-10-20T11:38:07Z"
SOL_NET = 1399811149
WSOL_MINT = "So11111111111111111111111111111111111111112"
# Helius free ~1M credits/month. Standard JSON-RPC is typically 1 credit.
# 3000/day ≈ 90k/month leaves headroom; 12/tick keeps a 60s sample short.
DEFAULT_CYCLE_CAP = 12
DEFAULT_DAY_CAP = 3000
RENT_ONLY_LAMPORTS = 50_000_000  # 0.05 SOL — below this, try WSOL vault
SIGNAL_DEV = "dev_sell_20"
SIGNAL_TOP3 = "top3_sell_30"
SIGNAL_POOL = "pool_sol_drop_25"

# Known AMM programs (copied, not imported — collect.py stays untouched).
AMM_PROGRAMS = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
}
INCINERATOR = "1nc1nerator11111111111111111111111111111111"

_tls = threading.local()
_schema_lock = threading.Lock()
_time_fn = time.time
_rpc_fn = None
_cycle_calls = 0
_creator_mem: dict[str, str] = {}


def set_time_fn(fn) -> None:
    global _time_fn
    _time_fn = time.time if fn is None else fn


def set_rpc_fn(fn) -> None:
    """Inject rpc(method, params, timeout) for tests. None restores."""
    global _rpc_fn
    _rpc_fn = fn


def current_time() -> float:
    return float(_time_fn())


def db_path() -> pathlib.Path:
    override = os.environ.get("OUTCOMES_DB")
    if override:
        return pathlib.Path(override)
    outbox = pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))
    return outbox / "outcomes.db"


def _close_local() -> None:
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _tls.conn = None
    _tls.path = None


def reset() -> None:
    """Close this thread's cached connection. Tests call this."""
    global _cycle_calls, _creator_mem
    _close_local()
    _cycle_calls = 0
    _creator_mem = {}


def _schema_step(db: sqlite3.Connection, name: str, fn) -> None:
    """One schema change. Failure is logged and does not skip later steps."""
    try:
        fn()
        db.commit()
    except Exception as e:
        log.warning("young_trail_helius schema %s: %s", name, safe_err(e))
        try:
            db.rollback()
        except Exception:
            pass


def init_schema(db: sqlite3.Connection | None = None) -> None:
    """Idempotent. Each step commits on its own (live-DB safe, lesson from #60)."""
    conn = db if db is not None else _connect()
    _schema_step(conn, "young_trail_helius", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_helius (
      tid TEXT PRIMARY KEY,
      ticker TEXT,
      chain TEXT,
      entry_ts REAL NOT NULL,
      entry_price REAL NOT NULL,
      last_price REAL,
      last_mark_ts REAL,
      signal_type TEXT,
      signal_ts REAL,
      signal_price REAL,
      pnl_usd REAL,
      exit_ts REAL,
      exit_price REAL,
      rugged INTEGER,
      rug_ts REAL,
      lead_min REAL,
      outcome_source TEXT
    )"""))
    _schema_step(conn, "idx_yth_exit", lambda: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_yth_exit ON young_trail_helius(exit_ts)"
    ))
    _schema_step(conn, "idx_yth_signal", lambda: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_yth_signal ON young_trail_helius(signal_type)"
    ))
    _schema_step(conn, "young_trail_helius_watch", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_helius_watch (
      tid TEXT PRIMARY KEY,
      mint TEXT,
      dev_wallet TEXT,
      holders_json TEXT,
      top3_json TEXT,
      pool_account TEXT,
      pool_sol_lamports REAL,
      dev_amount REAL,
      snapshot_ts REAL,
      snapshot_ok INTEGER NOT NULL DEFAULT 0
    )"""))
    _schema_step(conn, "young_trail_helius_signals", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_helius_signals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      tid TEXT NOT NULL,
      signal_type TEXT NOT NULL,
      ts REAL NOT NULL,
      mark_price REAL,
      pnl_usd REAL,
      rugged INTEGER,
      rug_ts REAL,
      lead_min REAL,
      UNIQUE (tid, signal_type)
    )"""))
    _schema_step(conn, "idx_yths_tid", lambda: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_yths_tid ON young_trail_helius_signals(tid)"
    ))
    _schema_step(conn, "young_trail_helius_creators", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_helius_creators (
      tid TEXT PRIMARY KEY,
      creator TEXT NOT NULL,
      source TEXT,
      noted_ts REAL
    )"""))
    _schema_step(conn, "young_trail_helius_meta", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_helius_meta (
      key TEXT PRIMARY KEY,
      value TEXT
    )"""))
    _schema_step(conn, "yth_exit_reason", lambda: conn.execute(
        "ALTER TABLE young_trail_helius ADD COLUMN exit_reason TEXT"
    ))


def _connect() -> sqlite3.Connection:
    """Per-thread connection. Default check_same_thread (this thread only)."""
    path = str(db_path())
    conn = getattr(_tls, "conn", None)
    if conn is not None and getattr(_tls, "path", None) == path:
        return conn
    _close_local()
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=LOCK_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    with _schema_lock:
        init_schema(conn)
    _tls.conn = conn
    _tls.path = path
    return conn


def enabled() -> bool:
    raw = os.environ.get("YOUNG_TRAIL_HELIUS_ENABLED")
    if raw is None or raw == "":
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _parse_until(raw: str) -> float | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            t = time.strptime(s, fmt)
            if fmt == "%Y-%m-%dT%H:%M:%SZ":
                import calendar
                return float(calendar.timegm(t))
            ts = time.mktime(t)
            if fmt == "%Y-%m-%d":
                ts += 86400 - 1
            return float(ts)
        except ValueError:
            continue
    return None


def _meta_get(key: str) -> str | None:
    try:
        row = _connect().execute(
            "SELECT value FROM young_trail_helius_meta WHERE key = ?", (key,),
        ).fetchone()
        return None if row is None else row["value"]
    except Exception as e:
        log.warning("young_trail_helius meta read: %s", safe_err(e))
        return None


def _meta_set(key: str, value: str) -> None:
    try:
        db = _connect()
        db.execute(
            """INSERT INTO young_trail_helius_meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        db.commit()
    except Exception as e:
        log.warning("young_trail_helius meta write: %s", safe_err(e))


def deploy_ts(now: float | None = None) -> float:
    stored = _meta_get("deploy_ts")
    if stored:
        try:
            return float(stored)
        except (TypeError, ValueError):
            pass
    ts = current_time() if now is None else float(now)
    _meta_set("deploy_ts", str(ts))
    return ts


def until_ts(now: float | None = None) -> float:
    raw = os.environ.get("YOUNG_TRAIL_HELIUS_UNTIL")
    parsed = _parse_until(raw) if raw else None
    if parsed is not None:
        return parsed
    stored = _meta_get("until_ts")
    if stored:
        try:
            return float(stored)
        except (TypeError, ValueError):
            pass
    try:
        import young_trail
        ts = float(young_trail.until_ts(now))
        _meta_set("until_ts", str(ts))
        return ts
    except Exception as e:
        log.warning("young_trail_helius until from young_trail: %s", safe_err(e))
    fallback = _parse_until(DEFAULT_UNTIL_ISO) or 0.0
    _meta_set("until_ts", str(fallback))
    return fallback


def accepting_entries(now: float | None = None) -> bool:
    ts = current_time() if now is None else float(now)
    return enabled() and ts < until_ts(ts)


def cycle_cap() -> int:
    raw = os.environ.get("YOUNG_TRAIL_HELIUS_CYCLE_CAP")
    try:
        n = int(raw) if raw not in (None, "") else DEFAULT_CYCLE_CAP
    except (TypeError, ValueError):
        n = DEFAULT_CYCLE_CAP
    return max(0, n)


def day_cap() -> int:
    raw = os.environ.get("YOUNG_TRAIL_HELIUS_DAY_CAP")
    try:
        n = int(raw) if raw not in (None, "") else DEFAULT_DAY_CAP
    except (TypeError, ValueError):
        n = DEFAULT_DAY_CAP
    return max(0, n)


def _day_key(now: float | None = None) -> str:
    ts = current_time() if now is None else float(now)
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _day_used(now: float | None = None) -> int:
    key = _day_key(now)
    if _meta_get("budget_day") != key:
        return 0
    raw = _meta_get("budget_day_calls")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def begin_cycle(now: float | None = None) -> None:
    """Reset the per-tick call counter. Persist a new UTC day if needed."""
    global _cycle_calls
    _cycle_calls = 0
    key = _day_key(now)
    if _meta_get("budget_day") != key:
        _meta_set("budget_day", key)
        _meta_set("budget_day_calls", "0")


def budget_remaining(now: float | None = None) -> dict:
    day_left = max(0, day_cap() - _day_used(now))
    cycle_left = max(0, cycle_cap() - _cycle_calls)
    return {
        "cycle_used": _cycle_calls,
        "cycle_cap": cycle_cap(),
        "cycle_left": cycle_left,
        "day_used": _day_used(now),
        "day_cap": day_cap(),
        "day_left": day_left,
        "day": _day_key(now),
    }


def _can_charge(n: int = 1, now: float | None = None) -> bool:
    if n <= 0:
        return True
    left = budget_remaining(now)
    return left["cycle_left"] >= n and left["day_left"] >= n


def _charge(n: int = 1, now: float | None = None) -> bool:
    """Count n Helius calls against both caps. False if the budget is gone."""
    global _cycle_calls
    if n <= 0:
        return True
    if not _can_charge(n, now):
        return False
    _cycle_calls += n
    used = _day_used(now) + n
    _meta_set("budget_day", _day_key(now))
    _meta_set("budget_day_calls", str(used))
    return True


def pnl_usd(exit_price: float, entry_price: float, ticket: float = TICKET_USD) -> float:
    if not entry_price:
        return 0.0
    return float(ticket) * (float(exit_price) / float(entry_price) - 1.0)


def _num(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _price_of(mark) -> float | None:
    if mark is None:
        return None
    if isinstance(mark, dict):
        for key in ("price_usd", "price", "priceUsd"):
            px = _num(mark.get(key))
            if px is not None and px >= 0:
                return px
        return None
    px = _num(mark)
    return px if px is not None and px >= 0 else None


def _row_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _loads(raw, default=None):
    if raw in (None, ""):
        return default if default is not None else []
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default if default is not None else []


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


def is_solana(tid: str | None = None, chain=None, net=None) -> bool:
    if net == SOL_NET:
        return True
    if str(chain or "").strip().lower() == "solana":
        return True
    if tid:
        _, parsed_net = parse_tid(tid)
        if parsed_net == SOL_NET:
            return True
    return False


def note_creator(tid: str, creator: str, source: str = "fomo",
                 now: float | None = None) -> None:
    """Remember a FOMO (or other) creator so entry snapshots can use it."""
    if not tid or not creator:
        return
    wallet = str(creator).strip()
    if not wallet:
        return
    _creator_mem[tid] = wallet
    try:
        db = _connect()
        db.execute(
            """INSERT INTO young_trail_helius_creators (tid, creator, source, noted_ts)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(tid) DO UPDATE SET
                 creator = excluded.creator,
                 source = excluded.source,
                 noted_ts = excluded.noted_ts""",
            (tid, wallet, source, current_time() if now is None else float(now)),
        )
        db.commit()
    except Exception as e:
        log.warning("young_trail_helius note creator: %s", safe_err(e))


def remember_creators(tokens, now: float | None = None) -> int:
    """Pull FOMO creator off already-fetched desk tokens. No extra HTTP."""
    n = 0
    try:
        for t in tokens or []:
            if not isinstance(t, dict):
                continue
            tid = t.get("tid")
            creator = (
                t.get("creator") or t.get("creatorAddress")
                or t.get("creator_address") or t.get("creatorWallet")
                or t.get("deployer")
            )
            if not tid or not creator:
                continue
            if not is_solana(tid, t.get("chain"), t.get("net")):
                continue
            note_creator(tid, str(creator), source="fomo", now=now)
            n += 1
    except Exception as e:
        log.warning("young_trail_helius remember creators: %s", safe_err(e))
    return n


def lookup_creator(tid: str) -> str | None:
    if not tid:
        return None
    if tid in _creator_mem:
        return _creator_mem[tid]
    try:
        row = _connect().execute(
            "SELECT creator FROM young_trail_helius_creators WHERE tid = ?",
            (tid,),
        ).fetchone()
        if row and row["creator"]:
            _creator_mem[tid] = row["creator"]
            return row["creator"]
    except Exception as e:
        log.warning("young_trail_helius creator lookup: %s", safe_err(e))
    return None


def _rpc(method: str, params, timeout: float = RPC_TIMEOUT_SEC, now: float | None = None):
    """One Helius JSON-RPC call. Charges the budget first. Never logs the URL."""
    if not _charge(1, now):
        raise RuntimeError("helius budget exhausted")
    if _rpc_fn is not None:
        return _rpc_fn(method, params, timeout)
    import requests
    rpc_url = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
    try:
        resp = requests.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=max(0.5, float(timeout)),
        )
        data = resp.json()
    except Exception as e:
        log.warning("young_trail_helius rpc %s: %s", method, safe_err(e))
        raise
    if not isinstance(data, dict):
        raise RuntimeError("solana rpc: non-object response")
    if "error" in data:
        err = data["error"]
        if isinstance(err, dict):
            raise RuntimeError(err.get("message") or "solana rpc error")
        raise RuntimeError(str(err))
    if "result" not in data:
        raise RuntimeError("solana rpc: missing result")
    return data["result"]


def _largest_accounts(mint: str, now: float | None = None) -> list[dict]:
    result = _rpc("getTokenLargestAccounts", [mint], now=now)
    rows = result.get("value") if isinstance(result, dict) else result
    out = []
    for acc in rows or []:
        if not isinstance(acc, dict) or not acc.get("address"):
            continue
        amt = _num(acc.get("amount") or (acc.get("uiAmount") if False else None), 0.0)
        if amt is None:
            amt = 0.0
        out.append({"address": acc["address"], "amount": float(amt)})
    return out


def _owners_for(accounts: list[str], now: float | None = None) -> dict[str, dict]:
    """Map token-account pubkey → {owner, program} via getMultipleAccounts."""
    if not accounts:
        return {}
    addrs = accounts[:20]
    result = _rpc(
        "getMultipleAccounts",
        [addrs, {"encoding": "jsonParsed"}],
        now=now,
    )
    values = result.get("value") if isinstance(result, dict) else result
    out: dict[str, dict] = {}
    for i, acc in enumerate(values or []):
        if not acc or i >= len(addrs):
            continue
        owner = None
        program = acc.get("owner")
        parsed = acc.get("data")
        if isinstance(parsed, dict):
            info = ((parsed.get("parsed") or {}).get("info") or {})
            owner = info.get("owner")
        out[addrs[i]] = {"owner": owner, "program": program}
    return out


def _account_lamports(pubkey: str, now: float | None = None) -> float | None:
    result = _rpc(
        "getAccountInfo",
        [pubkey, {"encoding": "jsonParsed"}],
        now=now,
    )
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    lamports = _num(value.get("lamports"))
    return lamports


def _token_amount_for_owner(owner: str, mint: str, now: float | None = None) -> float | None:
    result = _rpc(
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        now=now,
    )
    value = result.get("value") if isinstance(result, dict) else result
    total = 0.0
    found = False
    for acc in value or []:
        if not isinstance(acc, dict):
            continue
        info = ((((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
        tok = info.get("tokenAmount") or {}
        amt = _num(tok.get("amount"))
        if amt is not None:
            total += amt
            found = True
    return total if found else 0.0


def _wsol_amount(owner: str, now: float | None = None) -> float | None:
    if not _can_charge(1, now):
        return None
    try:
        return _token_amount_for_owner(owner, WSOL_MINT, now=now)
    except Exception as e:
        log.warning("young_trail_helius wsol: %s", safe_err(e))
        return None


def _source_entries() -> list[dict]:
    try:
        import young_trail
        rows = list(young_trail.open_rows())
        rows.extend(young_trail.closed_rows(limit=None))
        out = []
        for r in rows:
            if not r.get("tid") or _num(r.get("entry_price"), 0) <= 0:
                continue
            if not is_solana(r.get("tid"), r.get("chain")):
                continue
            out.append(r)
        return out
    except Exception as e:
        log.warning("young_trail_helius source entries: %s", safe_err(e))
        return []


def sync_entries(now: float | None = None) -> int:
    """Copy Solana young_trail tids/entry prices from deploy onward. Fail open."""
    if not enabled():
        return 0
    ts = current_time() if now is None else float(now)
    if not accepting_entries(ts):
        return 0
    since = deploy_ts()
    n = 0
    try:
        db = _connect()
        for src in _source_entries():
            entry_ts = _num(src.get("entry_ts"))
            if entry_ts is None or entry_ts < since - 1e-9:
                continue
            px = float(src["entry_price"])
            mint, _net = parse_tid(src["tid"])
            cur = db.execute(
                """INSERT OR IGNORE INTO young_trail_helius (
                     tid, ticker, chain, entry_ts, entry_price,
                     last_price, last_mark_ts
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    src["tid"], src.get("ticker"), src.get("chain") or "solana",
                    float(entry_ts), px, px, float(entry_ts),
                ),
            )
            if cur.rowcount:
                n += 1
            db.execute(
                """INSERT OR IGNORE INTO young_trail_helius_watch (tid, mint)
                   VALUES (?, ?)""",
                (src["tid"], mint),
            )
        db.commit()
        if n:
            log.info("young_trail_helius sync entries=%d paper=1", n)
    except Exception as e:
        log.warning("young_trail_helius sync: %s", safe_err(e))
    return n


def _snapshot_watch(tid: str, now: float | None = None) -> bool:
    """Budgeted entry snapshot: creator, top 10 holders, LP/pool + SOL reserve."""
    ts = current_time() if now is None else float(now)
    db = _connect()
    row = db.execute(
        "SELECT * FROM young_trail_helius_watch WHERE tid = ?", (tid,),
    ).fetchone()
    if row is None:
        return False
    if row["snapshot_ok"]:
        return True
    mint = row["mint"] or parse_tid(tid)[0]
    if not mint:
        return False
    # Snapshot costs up to 4 calls (largest + owners + pool SOL + optional WSOL/dev).
    if not _can_charge(2, ts):
        return False
    try:
        largest = _largest_accounts(mint, now=ts)
        addrs = [a["address"] for a in largest]
        owners = _owners_for(addrs, now=ts) if addrs and _can_charge(1, ts) else {}
        holders = []
        pools = []
        for acc in largest:
            meta = owners.get(acc["address"]) or {}
            owner = meta.get("owner")
            program = meta.get("program")
            is_pool = bool(
                (program and program in AMM_PROGRAMS)
                or (owner and owner in AMM_PROGRAMS)
                or owner == INCINERATOR
            )
            item = {
                "account": acc["address"],
                "owner": owner,
                "amount": acc["amount"],
                "is_pool": is_pool,
            }
            if is_pool:
                pools.append(item)
            else:
                holders.append(item)
        holders.sort(key=lambda h: h["amount"], reverse=True)
        top10 = holders[:10]
        dev = lookup_creator(tid)
        # Top-3 is the next three wallets after the known FOMO creator.
        top3 = [h for h in top10 if not (dev and h.get("owner") == dev)][:3]
        pool = pools[0] if pools else None
        # Prefer the pool PDA (token-account owner) as the LP/pool account.
        pool_account = (pool or {}).get("owner") or (pool or {}).get("account")
        pool_sol = None
        if pool_account and _can_charge(1, ts):
            try:
                pool_sol = _account_lamports(pool_account, now=ts)
            except Exception as e:
                log.warning("young_trail_helius pool sol %s: %s", tid, safe_err(e))
            if (
                pool_sol is not None
                and pool_sol < RENT_ONLY_LAMPORTS
                and _can_charge(1, ts)
            ):
                wsol = _wsol_amount(pool_account, now=ts)
                if wsol is not None:
                    pool_sol = float(pool_sol) + float(wsol)
        dev_amount = None
        if dev:
            for h in holders:
                if h.get("owner") and str(h["owner"]) == str(dev):
                    dev_amount = h["amount"]
                    break
            if dev_amount is None and _can_charge(1, ts):
                try:
                    dev_amount = _token_amount_for_owner(dev, mint, now=ts)
                except Exception as e:
                    log.warning("young_trail_helius dev bal %s: %s", tid, safe_err(e))
        db.execute(
            """UPDATE young_trail_helius_watch
               SET mint = ?, dev_wallet = ?, holders_json = ?, top3_json = ?,
                   pool_account = ?, pool_sol_lamports = ?, dev_amount = ?,
                   snapshot_ts = ?, snapshot_ok = 1
               WHERE tid = ?""",
            (
                mint, dev, json.dumps(top10), json.dumps(top3),
                pool_account, pool_sol, dev_amount, ts, tid,
            ),
        )
        db.commit()
        log.info(
            "young_trail_helius snapshot tid=%s holders=%d pool=%s paper=1",
            tid, len(top10), "yes" if pool_account else "no",
        )
        return True
    except Exception as e:
        log.warning("young_trail_helius snapshot %s: %s", tid, safe_err(e))
        return False


def snapshot_pending(now: float | None = None, limit: int | None = None) -> int:
    """Snapshot watches that still need an entry picture. Budget-aware."""
    n = 0
    try:
        rows = _connect().execute(
            """SELECT w.tid FROM young_trail_helius_watch w
               JOIN young_trail_helius h ON h.tid = w.tid
               WHERE w.snapshot_ok = 0
               ORDER BY h.entry_ts"""
        ).fetchall()
        cap = limit if limit is not None else 8
        for row in rows[: max(0, cap)]:
            if not _can_charge(2, now):
                break
            if _snapshot_watch(row["tid"], now=now):
                n += 1
    except Exception as e:
        log.warning("young_trail_helius snapshot pending: %s", safe_err(e))
    return n


def _amount_in_largest(largest: list[dict], account: str | None) -> float | None:
    if not account:
        return None
    for acc in largest:
        if acc.get("address") == account:
            return float(acc.get("amount") or 0.0)
    return 0.0


def detect_signals(watch: dict, largest: list[dict], pool_sol: float | None) -> list[str]:
    """Pure thresholds. No I/O. Used by tests and the tick path."""
    fired: list[str] = []
    snap_dev = _num(watch.get("dev_amount"))
    if snap_dev is not None and snap_dev > 0 and watch.get("dev_wallet"):
        current = None
        holders = _loads(watch.get("holders_json"), [])
        dev_acc = None
        for h in holders:
            if str(h.get("owner") or "") == str(watch["dev_wallet"]):
                dev_acc = h.get("account")
                break
        if dev_acc:
            current = _amount_in_largest(largest, dev_acc)
        if current is not None and (snap_dev - current) / snap_dev >= DEV_SELL_PCT - 1e-12:
            fired.append(SIGNAL_DEV)
    top3 = _loads(watch.get("top3_json"), [])
    dev_wallet = watch.get("dev_wallet")
    for h in top3[:3]:
        if dev_wallet and str(h.get("owner") or "") == str(dev_wallet):
            continue
        snap = _num(h.get("amount"), 0.0) or 0.0
        if snap <= 0:
            continue
        current = _amount_in_largest(largest, h.get("account"))
        if current is None:
            continue
        if (snap - current) / snap >= TOP3_SELL_PCT - 1e-12:
            fired.append(SIGNAL_TOP3)
            break
    snap_sol = _num(watch.get("pool_sol_lamports"))
    if (
        snap_sol is not None and snap_sol > 0
        and pool_sol is not None
        and (snap_sol - pool_sol) / snap_sol >= POOL_SOL_DROP_PCT - 1e-12
    ):
        fired.append(SIGNAL_POOL)
    return fired


def _outcome_for(tid: str) -> dict:
    """Rug flag + ts from token_labels, with an outcomes.trough_x fallback."""
    out = {"rugged": None, "rug_ts": None, "source": None}
    try:
        import research
        path = research.db_path()
        if path.exists():
            db = sqlite3.connect(str(path), timeout=LOCK_TIMEOUT_SEC)
            db.row_factory = sqlite3.Row
            try:
                row = db.execute(
                    "SELECT rugged, rug_ts FROM token_labels WHERE tid = ?",
                    (tid,),
                ).fetchone()
            finally:
                db.close()
            if row is not None:
                out["rugged"] = int(row["rugged"] or 0)
                out["rug_ts"] = _num(row["rug_ts"])
                out["source"] = "token_labels"
                if out["rugged"]:
                    return out
    except Exception as e:
        log.warning("young_trail_helius labels %s: %s", tid, safe_err(e))
    try:
        import outcomes
        path = outcomes.db_path()
        if path.exists():
            db = sqlite3.connect(str(path), timeout=LOCK_TIMEOUT_SEC)
            db.row_factory = sqlite3.Row
            try:
                have = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcomes'"
                ).fetchone()
                row = None
                if have:
                    row = db.execute(
                        "SELECT trough_x, entry_ts FROM outcomes WHERE tid = ?",
                        (tid,),
                    ).fetchone()
            finally:
                db.close()
            if row is not None:
                trough = _num(row["trough_x"])
                if trough is not None and trough <= 0.20:
                    out["rugged"] = 1
                    out["source"] = "outcomes"
                elif out["rugged"] is None:
                    out["rugged"] = 0
                    out["source"] = out["source"] or "outcomes"
    except Exception as e:
        log.warning("young_trail_helius outcomes %s: %s", tid, safe_err(e))
    return out


def refresh_outcomes(tids: list[str] | None = None) -> int:
    """Join signals / exits to token_labels + outcomes. Fail open."""
    n = 0
    try:
        db = _connect()
        if tids is None:
            rows = db.execute("SELECT tid FROM young_trail_helius").fetchall()
            tids = [r["tid"] for r in rows]
        for tid in tids:
            oc = _outcome_for(tid)
            if oc.get("rugged") is None:
                continue
            lead = None
            row = db.execute(
                "SELECT signal_ts FROM young_trail_helius WHERE tid = ?", (tid,),
            ).fetchone()
            if row and row["signal_ts"] is not None and oc.get("rug_ts") is not None:
                lead = (float(oc["rug_ts"]) - float(row["signal_ts"])) / 60.0
            db.execute(
                """UPDATE young_trail_helius
                   SET rugged = ?, rug_ts = ?, lead_min = ?, outcome_source = ?
                   WHERE tid = ?""",
                (int(oc["rugged"]), oc.get("rug_ts"), lead, oc.get("source"), tid),
            )
            for sig in db.execute(
                "SELECT id, ts FROM young_trail_helius_signals WHERE tid = ?",
                (tid,),
            ).fetchall():
                sig_lead = None
                if oc.get("rug_ts") is not None:
                    sig_lead = (float(oc["rug_ts"]) - float(sig["ts"])) / 60.0
                db.execute(
                    """UPDATE young_trail_helius_signals
                       SET rugged = ?, rug_ts = ?, lead_min = ?
                       WHERE id = ?""",
                    (int(oc["rugged"]), oc.get("rug_ts"), sig_lead, sig["id"]),
                )
            n += 1
        db.commit()
    except Exception as e:
        log.warning("young_trail_helius outcomes refresh: %s", safe_err(e))
    return n


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    try:
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _is_real_mark(row) -> bool:
    """True when last_mark_ts is after entry (not the entry seed)."""
    entry_ts = _num(row["entry_ts"] if not isinstance(row, dict) else row.get("entry_ts"))
    last_mark_ts = _num(
        row["last_mark_ts"] if not isinstance(row, dict) else row.get("last_mark_ts"),
    )
    if entry_ts is None or last_mark_ts is None:
        return False
    return last_mark_ts > entry_ts + 1e-9


def _mint_like(tid: str) -> tuple[str, str]:
    mint, _net = parse_tid(tid)
    mint = mint or str(tid)
    return str(tid), mint + ":%"


def _latest_stored_mark(
    tid: str,
    *,
    after_ts: float | None = None,
    before_ts: float | None = None,
    db: sqlite3.Connection | None = None,
) -> tuple[float | None, float | None]:
    """Freshest stored guarded price for this tid/mint. No network. Fail open.

    Sources: young_trail, young_trail_fast, young_trail_marks, research.fast_marks.
    Entry-seed stamps (ts <= after_ts) are ignored.
    """
    candidates: list[tuple[float, float]] = []

    def add(px, ts) -> None:
        if px is None or ts is None:
            return
        try:
            price = float(px)
            stamp = float(ts)
        except (TypeError, ValueError):
            return
        if price < 0:
            return
        if after_ts is not None and stamp <= float(after_ts) + 1e-9:
            return
        if before_ts is not None and stamp > float(before_ts) + 1e-9:
            return
        candidates.append((stamp, price))

    exact, like = _mint_like(tid)
    try:
        conn = db if db is not None else _connect()
        if _table_exists(conn, "young_trail"):
            for r in conn.execute(
                """SELECT last_price, last_mark_ts, entry_ts FROM young_trail
                   WHERE tid = ? OR tid LIKE ?""",
                (exact, like),
            ).fetchall():
                if (
                    r["last_mark_ts"] is not None
                    and r["entry_ts"] is not None
                    and float(r["last_mark_ts"]) > float(r["entry_ts"]) + 1e-9
                ):
                    add(r["last_price"], r["last_mark_ts"])
        if _table_exists(conn, "young_trail_fast"):
            for r in conn.execute(
                """SELECT last_price, last_mark_ts, entry_ts FROM young_trail_fast
                   WHERE tid = ? OR tid LIKE ?""",
                (exact, like),
            ).fetchall():
                if (
                    r["last_mark_ts"] is not None
                    and r["entry_ts"] is not None
                    and float(r["last_mark_ts"]) > float(r["entry_ts"]) + 1e-9
                ):
                    add(r["last_price"], r["last_mark_ts"])
        if _table_exists(conn, "young_trail_marks"):
            for r in conn.execute(
                """SELECT ts, price FROM young_trail_marks
                   WHERE tid = ? OR tid LIKE ?""",
                (exact, like),
            ).fetchall():
                add(r["price"], r["ts"])
    except Exception as e:
        log.warning("young_trail_helius stored mark outcomes %s: %s", tid, safe_err(e))
    try:
        import research
        path = research.db_path()
        if path.exists():
            rdb = sqlite3.connect(str(path), timeout=LOCK_TIMEOUT_SEC)
            rdb.row_factory = sqlite3.Row
            try:
                if _table_exists(rdb, "fast_marks"):
                    for r in rdb.execute(
                        """SELECT ts, price_usd FROM fast_marks
                           WHERE (tid = ? OR tid LIKE ?) AND price_usd IS NOT NULL""",
                        (exact, like),
                    ).fetchall():
                        add(r["price_usd"], r["ts"])
            finally:
                rdb.close()
    except Exception as e:
        log.warning("young_trail_helius stored mark research %s: %s", tid, safe_err(e))
    if not candidates:
        return None, None
    candidates.sort()
    stamp, price = candidates[-1]
    return price, stamp


def _close_price_for(
    row,
    now: float | None = None,
    *,
    trust_self: bool = True,
) -> tuple[float | None, float | None]:
    """Freshest real mark on this row or from other ledgers. Never the entry seed."""
    entry_ts = _num(row["entry_ts"] if not isinstance(row, dict) else row.get("entry_ts"))
    if trust_self and _is_real_mark(row):
        last_px = _num(row["last_price"] if not isinstance(row, dict) else row.get("last_price"))
        last_ts = _num(
            row["last_mark_ts"] if not isinstance(row, dict) else row.get("last_mark_ts"),
        )
        if last_px is not None:
            return last_px, last_ts
    tid = row["tid"] if not isinstance(row, dict) else row.get("tid")
    before = float(now) if now is not None else None
    return _latest_stored_mark(tid, after_ts=entry_ts, before_ts=before)


def _mark_is_fresh(mark_ts: float | None, now: float) -> bool:
    if mark_ts is None:
        return False
    return float(now) - float(mark_ts) <= FRESH_MARK_SEC + 1e-9


def _apply_tick_prices(db: sqlite3.Connection, rows, prices: dict, ts: float) -> int:
    """Update last_price on every open row from this tick or stored marks. No RPC."""
    import price_guard
    marked = 0
    for row in rows:
        tid = row["tid"]
        entry_px = float(row["entry_price"])
        last_px = _num(row["last_price"]) or entry_px
        px = price_guard.accept_price(
            _price_of(prices.get(tid)),
            entry=entry_px, prev=last_px, tid=tid, now=ts,
        )
        mark_ts = ts
        if px is None:
            stored, stored_ts = _latest_stored_mark(
                tid, after_ts=float(row["entry_ts"]), before_ts=ts,
            )
            if stored is None or stored_ts is None:
                continue
            cur_ts = _num(row["last_mark_ts"]) or 0.0
            if _is_real_mark(row) and stored_ts <= cur_ts + 1e-9:
                continue
            px = stored
            mark_ts = stored_ts
        db.execute(
            """UPDATE young_trail_helius
               SET last_price = ?, last_mark_ts = ?
               WHERE tid = ? AND exit_ts IS NULL""",
            (float(px), float(mark_ts), tid),
        )
        marked += 1
    return marked


def _close(db: sqlite3.Connection, tid: str, exit_ts: float, exit_price: float | None,
           signal_type: str | None, *, exit_reason: str,
           unmarked: bool = False) -> None:
    entry = db.execute(
        "SELECT * FROM young_trail_helius WHERE tid = ?", (tid,),
    ).fetchone()
    if entry is None or entry["exit_ts"] is not None:
        return
    if unmarked or exit_price is None:
        reason = "unmarked"
        db.execute(
            """UPDATE young_trail_helius
               SET signal_type = ?, signal_ts = ?, signal_price = ?,
                   pnl_usd = NULL, exit_ts = ?, exit_price = NULL, exit_reason = ?
               WHERE tid = ? AND exit_ts IS NULL""",
            (
                signal_type, float(exit_ts) if signal_type else None, None,
                float(exit_ts), reason, tid,
            ),
        )
        if signal_type:
            db.execute(
                """INSERT OR IGNORE INTO young_trail_helius_signals (
                     tid, signal_type, ts, mark_price, pnl_usd
                   ) VALUES (?, ?, ?, NULL, NULL)""",
                (tid, signal_type, float(exit_ts)),
            )
        log.info(
            "young_trail_helius exit tid=%s reason=%s signal=%s pnl_usd=null paper=1",
            tid, reason, signal_type,
        )
        return
    pnl = pnl_usd(float(exit_price), float(entry["entry_price"]))
    db.execute(
        """UPDATE young_trail_helius
           SET last_price = ?, last_mark_ts = ?,
               signal_type = ?, signal_ts = ?, signal_price = ?,
               pnl_usd = ?, exit_ts = ?, exit_price = ?, exit_reason = ?
           WHERE tid = ? AND exit_ts IS NULL""",
        (
            float(exit_price), float(exit_ts),
            signal_type, float(exit_ts) if signal_type else None,
            float(exit_price) if signal_type else None,
            pnl, float(exit_ts), float(exit_price), exit_reason, tid,
        ),
    )
    if signal_type:
        db.execute(
            """INSERT OR IGNORE INTO young_trail_helius_signals (
                 tid, signal_type, ts, mark_price, pnl_usd
               ) VALUES (?, ?, ?, ?, ?)""",
            (tid, signal_type, float(exit_ts), float(exit_price), pnl),
        )
    log.info(
        "young_trail_helius exit tid=%s reason=%s signal=%s pnl_usd=%.2f paper=1",
        tid, exit_reason, signal_type, pnl,
    )


def _record_extra_signal(db: sqlite3.Connection, tid: str, signal_type: str,
                         ts: float, price: float | None) -> None:
    entry = db.execute(
        "SELECT entry_price FROM young_trail_helius WHERE tid = ?", (tid,),
    ).fetchone()
    if entry is None:
        return
    px = _num(price)
    pnl = None if px is None else pnl_usd(px, float(entry["entry_price"]))
    db.execute(
        """INSERT OR IGNORE INTO young_trail_helius_signals (
             tid, signal_type, ts, mark_price, pnl_usd
           ) VALUES (?, ?, ?, ?, ?)""",
        (tid, signal_type, float(ts), px, pnl),
    )


def _rr_tids(open_tids: list[str]) -> list[str]:
    """Rotate so a daily cap does not starve later entries."""
    if not open_tids:
        return []
    raw = _meta_get("rr_index")
    try:
        idx = int(raw or 0) % len(open_tids)
    except (TypeError, ValueError):
        idx = 0
    rotated = open_tids[idx:] + open_tids[:idx]
    _meta_set("rr_index", str((idx + 1) % len(open_tids)))
    return rotated


def on_marks(prices: dict | None, now: float | None = None) -> dict:
    """Mark every open row from this tick, time-exit at 6h, then budgeted RPC.

    last_price updates even when the Helius budget is gone. Fail open.
    """
    prices = prices or {}
    ts = current_time() if now is None else float(now)
    checked = 0
    closed = 0
    signals = 0
    skipped = 0
    marked = 0
    try:
        db = _connect()
        open_now = db.execute(
            "SELECT * FROM young_trail_helius WHERE exit_ts IS NULL ORDER BY entry_ts",
        ).fetchall()
        marked = _apply_tick_prices(db, open_now, prices, ts)

        still_open = db.execute(
            "SELECT * FROM young_trail_helius WHERE exit_ts IS NULL ORDER BY entry_ts",
        ).fetchall()
        for row in still_open:
            if ts < float(row["entry_ts"]) + WINDOW_SEC:
                continue
            px, _mark_ts = _close_price_for(row, ts, trust_self=True)
            if px is None:
                _close(db, row["tid"], ts, None, None, exit_reason="unmarked",
                       unmarked=True)
            else:
                _close(db, row["tid"], ts, px, None, exit_reason="time")
            closed += 1

        check_rows = db.execute(
            """SELECT h.*, w.mint, w.dev_wallet, w.holders_json, w.top3_json,
                      w.pool_account, w.pool_sol_lamports, w.dev_amount,
                      w.snapshot_ok
               FROM young_trail_helius h
               JOIN young_trail_helius_watch w ON w.tid = h.tid
               WHERE w.snapshot_ok = 1
                 AND (h.exit_ts IS NULL OR h.exit_reason = 'time')
                 AND h.signal_type IS NULL
               ORDER BY h.entry_ts"""
        ).fetchall()
        tids = _rr_tids([r["tid"] for r in check_rows])
        by_tid = {r["tid"]: r for r in check_rows}
        for tid in tids:
            if not _can_charge(1, ts):
                skipped += 1
                continue
            row = by_tid[tid]
            mint = row["mint"]
            if not mint:
                continue
            try:
                largest = _largest_accounts(mint, now=ts)
            except Exception as e:
                log.warning("young_trail_helius mark %s: %s", tid, safe_err(e))
                continue
            pool_sol = None
            if row["pool_account"] and _can_charge(1, ts):
                try:
                    pool_sol = _account_lamports(row["pool_account"], now=ts)
                except Exception as e:
                    log.warning("young_trail_helius pool %s: %s", tid, safe_err(e))
            watch = {
                "dev_wallet": row["dev_wallet"],
                "dev_amount": row["dev_amount"],
                "holders_json": row["holders_json"],
                "top3_json": row["top3_json"],
                "pool_sol_lamports": row["pool_sol_lamports"],
            }
            fired = detect_signals(watch, largest, pool_sol)
            checked += 1
            if not fired:
                continue
            live = db.execute(
                "SELECT * FROM young_trail_helius WHERE tid = ?", (tid,),
            ).fetchone()
            already_closed = live is not None and live["exit_ts"] is not None
            rugged = int((live["rugged"] if live is not None else 0) or 0)
            if already_closed or rugged:
                log.info(
                    "young_trail_helius signal after %s tid=%s types=%s paper=1",
                    "rug" if rugged and not already_closed else (live["exit_reason"] if live else "exit"),
                    tid, ",".join(fired),
                )
                px_log = None
                if live is not None and _is_real_mark(live):
                    px_log = _num(live["last_price"])
                for sig in fired:
                    _record_extra_signal(db, tid, sig, ts, px_log)
                signals += len(fired)
                continue
            px, mark_ts = _close_price_for(live, ts, trust_self=True)
            if px is None or not _mark_is_fresh(mark_ts, ts):
                _close(db, tid, ts, None, fired[0], exit_reason="unmarked",
                       unmarked=True)
                closed += 1
            else:
                _close(db, tid, ts, px, fired[0], exit_reason=fired[0])
                closed += 1
            for sig in fired:
                _record_extra_signal(db, tid, sig, ts, px)
            signals += len(fired)
        db.commit()
        if open_now or check_rows:
            log.info(
                "young_trail_helius mark: %d marked, %d checked, %d closed, "
                "%d signals paper=1",
                marked, checked, closed, signals,
            )
    except Exception as e:
        log.warning("young_trail_helius marks: %s", safe_err(e))
    return {
        "checked": checked, "closed": closed, "signals": signals,
        "skipped": skipped, "marked": marked,
    }


def on_sampler_tick(prices: dict | None = None, now: float | None = None) -> dict:
    """Sampler hook: copy Solana entries, snapshot, then check. Fail open."""
    empty = {
        "entries": 0, "snapshots": 0, "checked": 0, "closed": 0,
        "signals": 0, "marked": 0, "skipped": 0,
    }
    try:
        if not enabled():
            return empty
        ts = current_time() if now is None else float(now)
        begin_cycle(ts)
        until_ts(ts)
        deploy_ts()
        n = sync_entries(ts)
        snaps = snapshot_pending(ts)
        marks = on_marks(prices or {}, ts)
        try:
            refresh_outcomes()
        except Exception as e:
            log.warning("young_trail_helius outcomes: %s", safe_err(e))
        return {"entries": n, "snapshots": snaps, **marks}
    except Exception as e:
        log.warning("young_trail_helius sampler: %s", safe_err(e))
        return empty


def open_rows() -> list[dict]:
    try:
        return [_row_dict(r) for r in _connect().execute(
            "SELECT * FROM young_trail_helius WHERE exit_ts IS NULL ORDER BY entry_ts"
        ).fetchall()]
    except Exception as e:
        log.warning("young_trail_helius open: %s", safe_err(e))
        return []


def closed_rows(limit: int | None = 50) -> list[dict]:
    try:
        sql = "SELECT * FROM young_trail_helius WHERE exit_ts IS NOT NULL ORDER BY exit_ts DESC"
        args: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            args = (int(limit),)
        return [_row_dict(r) for r in _connect().execute(sql, args).fetchall()]
    except Exception as e:
        log.warning("young_trail_helius closed: %s", safe_err(e))
        return []


def signal_rows(limit: int | None = 80) -> list[dict]:
    try:
        sql = """SELECT s.*, h.ticker, h.chain, h.entry_price
                 FROM young_trail_helius_signals s
                 LEFT JOIN young_trail_helius h ON h.tid = s.tid
                 ORDER BY s.ts DESC"""
        args: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            args = (int(limit),)
        return [_row_dict(r) for r in _connect().execute(sql, args).fetchall()]
    except Exception as e:
        log.warning("young_trail_helius signals: %s", safe_err(e))
        return []


def watch_rows() -> list[dict]:
    try:
        return [_row_dict(r) for r in _connect().execute(
            "SELECT * FROM young_trail_helius_watch ORDER BY snapshot_ts"
        ).fetchall()]
    except Exception as e:
        log.warning("young_trail_helius watch: %s", safe_err(e))
        return []


def _median(values: list[float]) -> float | None:
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    if len(xs) % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


def quality() -> dict:
    """Precision / recall / lead time against token_labels + outcomes."""
    empty = {
        "signals": 0, "true_positives": 0, "false_positives": 0,
        "rugs_watched": 0, "rugs_caught": 0,
        "precision": None, "recall": None,
        "lead_min_median": None, "lead_min_mean": None, "lead_n": 0,
        "by_type": {},
    }
    try:
        refresh_outcomes()
        db = _connect()
        unmarked = {
            r["tid"] for r in db.execute(
                "SELECT tid FROM young_trail_helius WHERE exit_reason = 'unmarked'",
            ).fetchall()
        }
        sigs = [
            r for r in db.execute("SELECT * FROM young_trail_helius_signals").fetchall()
            if r["tid"] not in unmarked
        ]
        watched = db.execute("SELECT tid FROM young_trail_helius").fetchall()
        tids = [r["tid"] for r in watched]
        rug_tids = set()
        for tid in tids:
            oc = _outcome_for(tid)
            if oc.get("rugged"):
                rug_tids.add(tid)

        def _lead_ok(r) -> bool:
            lead = _num(r["lead_min"])
            if lead is None:
                return True
            return lead >= 0.0

        caught = {r["tid"] for r in sigs if r["tid"] in rug_tids and _lead_ok(r)}
        tp = sum(1 for r in sigs if r["tid"] in rug_tids and _lead_ok(r))
        fp = sum(1 for r in sigs if r["tid"] not in rug_tids and r["rugged"] == 0)
        # Signals on unlabeled tids do not count as FP until an outcome exists.
        # A negative lead (rug before the signal) is a miss, not a true positive.
        labeled = {r["tid"] for r in sigs if r["rugged"] is not None}
        labeled_pos = sum(
            1 for r in sigs
            if r["tid"] in labeled and r["tid"] in rug_tids and _lead_ok(r)
        )
        labeled_n = sum(1 for r in sigs if r["tid"] in labeled)
        leads = [
            _num(r["lead_min"]) for r in sigs
            if r["tid"] in rug_tids and r["lead_min"] is not None
        ]
        leads = [x for x in leads if x is not None and x >= 0.0]
        by_type: dict[str, dict] = {}
        for name in (SIGNAL_DEV, SIGNAL_TOP3, SIGNAL_POOL):
            rows = [r for r in sigs if r["signal_type"] == name]
            by_type[name] = {
                "n": len(rows),
                "true_positives": sum(
                    1 for r in rows if r["tid"] in rug_tids and _lead_ok(r)
                ),
            }
        precision = (labeled_pos / labeled_n) if labeled_n else None
        recall = (len(caught) / len(rug_tids)) if rug_tids else None
        return {
            "signals": len(sigs),
            "true_positives": tp,
            "false_positives": fp,
            "rugs_watched": len(rug_tids),
            "rugs_caught": len(caught),
            "precision": precision,
            "recall": recall,
            "lead_min_median": _median(leads),
            "lead_min_mean": (sum(leads) / len(leads)) if leads else None,
            "lead_n": len(leads),
            "by_type": by_type,
        }
    except Exception as e:
        log.warning("young_trail_helius quality: %s", safe_err(e))
        return empty


def _summary() -> dict:
    try:
        opens = open_rows()
        closes = closed_rows(limit=None)
        counted = [
            c for c in closes
            if c.get("exit_reason") != "unmarked" and c.get("pnl_usd") is not None
        ]
        wins = sum(1 for c in counted if (c.get("pnl_usd") or 0) > 0)
        n = len(counted)
        return {
            "open_count": len(opens),
            "closed_count": n,
            "winning_trades": wins,
            "losing_trades": n - wins,
            "win_rate": (wins / n) if n else 0.0,
            "realized_pnl_usd": sum(c.get("pnl_usd") or 0 for c in counted),
            "signals": len(signal_rows(limit=None)),
            "snapshotted": sum(1 for w in watch_rows() if w.get("snapshot_ok")),
        }
    except Exception as e:
        log.warning("young_trail_helius summary: %s", safe_err(e))
        return {
            "open_count": 0, "closed_count": 0, "winning_trades": 0,
            "losing_trades": 0, "win_rate": 0.0, "realized_pnl_usd": 0.0,
            "signals": 0, "snapshotted": 0,
        }


def comparison(limit: int = 80) -> list[dict]:
    """Same-entry P&L: helius vs 60s fast vs per-cycle young_trail."""
    try:
        db = _connect()
        rows = db.execute(
            "SELECT * FROM young_trail_helius ORDER BY entry_ts DESC"
        ).fetchall()
        tids = [r["tid"] for r in rows][: max(0, int(limit))]
        if not tids:
            return []
        helius_by = {r["tid"]: _row_dict(r) for r in rows}
        cycle_by: dict[str, dict] = {}
        try:
            import young_trail
            for r in young_trail.open_rows() + young_trail.closed_rows(limit=None):
                cycle_by[r["tid"]] = r
        except Exception as e:
            log.warning("young_trail_helius cycle join: %s", safe_err(e))
        fast_by: dict[str, dict] = {}
        try:
            import young_trail_fast
            for r in young_trail_fast.open_rows("baseline") + young_trail_fast.closed_rows("baseline", limit=None):
                fast_by[r["tid"]] = r
        except Exception as e:
            log.warning("young_trail_helius fast join: %s", safe_err(e))
        out = []
        for tid in tids:
            h = helius_by.get(tid) or {}
            cycle = cycle_by.get(tid) or {}
            fast = fast_by.get(tid) or {}
            hp = _num(h.get("pnl_usd"))
            cp = _num(cycle.get("pnl_usd"))
            fp = _num(fast.get("pnl_usd"))
            out.append({
                "tid": tid,
                "ticker": h.get("ticker") or cycle.get("ticker") or fast.get("ticker"),
                "chain": h.get("chain") or cycle.get("chain") or "solana",
                "entry_ts": h.get("entry_ts") or cycle.get("entry_ts"),
                "entry_price": h.get("entry_price") or cycle.get("entry_price"),
                "helius_signal": h.get("signal_type"),
                "helius_exit_ts": h.get("exit_ts"),
                "helius_pnl_usd": hp,
                "helius_rugged": h.get("rugged"),
                "helius_lead_min": h.get("lead_min"),
                "fast_baseline_exit_ts": fast.get("exit_ts"),
                "fast_baseline_exit_reason": fast.get("exit_reason"),
                "fast_baseline_pnl_usd": fp,
                "cycle_exit_ts": cycle.get("exit_ts"),
                "cycle_exit_reason": cycle.get("exit_reason"),
                "cycle_pnl_usd": cp,
                "delta_vs_fast_usd": (hp - fp) if (hp is not None and fp is not None) else None,
                "delta_vs_cycle_usd": (hp - cp) if (hp is not None and cp is not None) else None,
            })
        return out
    except Exception as e:
        log.warning("young_trail_helius comparison: %s", safe_err(e))
        return []


def _open_repair_db(path) -> sqlite3.Connection:
    """Open an explicit copy. Enforces the live-DB / location gate first."""
    import repair_helius_zero_exits as repair
    resolved = repair.assert_repair_db_allowed(path)
    if not resolved.exists():
        raise repair.RepairDbRefused(f"db not found: {resolved}")
    conn = sqlite3.connect(str(resolved), timeout=LOCK_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    return conn


def plan_zero_exit_repairs(path) -> list[dict]:
    """Closed rows with exit ≈ entry, recomputed from stored marks.

    `path` is required. Refuses the live outcomes.db.
    """
    conn = _open_repair_db(path)
    try:
        rows = conn.execute(
            """SELECT * FROM young_trail_helius
               WHERE exit_ts IS NOT NULL
                 AND exit_price IS NOT NULL
                 AND entry_price IS NOT NULL
                 AND abs(exit_price - entry_price)
                     <= 1e-9 * max(abs(entry_price), 1e-18)
               ORDER BY exit_ts"""
        ).fetchall()
        plan = []
        for row in rows:
            px, mark_ts = _latest_stored_mark(
                row["tid"],
                after_ts=float(row["entry_ts"]),
                before_ts=float(row["exit_ts"]),
                db=conn,
            )
            old_pnl = _num(row["pnl_usd"])
            if px is None:
                plan.append({
                    "tid": row["tid"],
                    "ticker": row["ticker"],
                    "entry_price": float(row["entry_price"]),
                    "old_exit_price": float(row["exit_price"]),
                    "old_pnl_usd": old_pnl,
                    "new_exit_price": None,
                    "new_pnl_usd": None,
                    "new_exit_reason": "unmarked",
                    "mark_ts": None,
                })
                continue
            plan.append({
                "tid": row["tid"],
                "ticker": row["ticker"],
                "entry_price": float(row["entry_price"]),
                "old_exit_price": float(row["exit_price"]),
                "old_pnl_usd": old_pnl,
                "new_exit_price": px,
                "new_pnl_usd": pnl_usd(px, float(row["entry_price"])),
                "new_exit_reason": row["exit_reason"] or row["signal_type"] or "signal",
                "mark_ts": mark_ts,
            })
        return plan
    finally:
        conn.close()


def apply_zero_exit_repairs(path, plan: list[dict]) -> int:
    """Write a repair plan to `path`. Returns rows updated. Paper ledger only."""
    conn = _open_repair_db(path)
    try:
        if not plan:
            return 0
        n = 0
        for item in plan:
            tid = item["tid"]
            if item.get("new_exit_price") is None:
                conn.execute(
                    """UPDATE young_trail_helius
                       SET pnl_usd = NULL, exit_price = NULL, exit_reason = 'unmarked',
                           signal_price = NULL
                       WHERE tid = ?""",
                    (tid,),
                )
                conn.execute(
                    """UPDATE young_trail_helius_signals
                       SET mark_price = NULL, pnl_usd = NULL
                       WHERE tid = ?""",
                    (tid,),
                )
            else:
                px = float(item["new_exit_price"])
                pnl = item.get("new_pnl_usd")
                reason = item.get("new_exit_reason") or "signal"
                conn.execute(
                    """UPDATE young_trail_helius
                       SET last_price = ?, exit_price = ?, pnl_usd = ?,
                           signal_price = ?, exit_reason = ?
                       WHERE tid = ?""",
                    (px, px, pnl, px, reason, tid),
                )
                conn.execute(
                    """UPDATE young_trail_helius_signals
                       SET mark_price = ?, pnl_usd = ?
                       WHERE tid = ?""",
                    (px, pnl, tid),
                )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def format_repair_diff(plan: list[dict]) -> str:
    """Plain-text diff for a dry-run. No secrets."""
    lines = ["tid ticker old_exit old_pnl -> new_exit new_pnl reason"]
    old_sum = 0.0
    new_sum = 0.0
    new_counted = 0
    for item in plan:
        old_pnl = item.get("old_pnl_usd")
        new_pnl = item.get("new_pnl_usd")
        if old_pnl is not None:
            old_sum += float(old_pnl)
        if new_pnl is not None:
            new_sum += float(new_pnl)
            new_counted += 1
        lines.append(
            "{tid} {ticker} {old_ex} {old_pnl} -> {new_ex} {new_pnl} {reason}".format(
                tid=item.get("tid"),
                ticker=item.get("ticker") or "",
                old_ex=item.get("old_exit_price"),
                old_pnl=old_pnl,
                new_ex=item.get("new_exit_price"),
                new_pnl=new_pnl,
                reason=item.get("new_exit_reason"),
            )
        )
    lines.append(
        "rows=%d old_pnl_sum=%.2f new_pnl_sum=%.2f (counted=%d)"
        % (len(plan), old_sum, new_sum, new_counted)
    )
    return "\n".join(lines)


def ops_payload() -> dict:
    """JSON for GET /ops/young_trail_helius. Paper/simulated; no live orders."""
    try:
        now = current_time()
        until = until_ts(now) if enabled() else None
        refresh_outcomes()
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "enabled": enabled(),
            "accepting_entries": accepting_entries(now),
            "until_ts": until,
            "until_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)) if until else None,
            "ticket_usd": TICKET_USD,
            "mark_interval_sec": 60,
            "caveat": (
                "Paper Helius rug signals on Solana young_trail entries. "
                "No live orders. Budgeted RPC; FOMO creator used when already known."
            ),
            "rules": {
                "dev_sell_pct": DEV_SELL_PCT,
                "top3_sell_pct": TOP3_SELL_PCT,
                "pool_sol_drop_pct": POOL_SOL_DROP_PCT,
                "window_sec": WINDOW_SEC,
                "fresh_mark_sec": FRESH_MARK_SEC,
            },
            "budget": budget_remaining(now),
            "summary": _summary(),
            "quality": quality(),
            "comparison": comparison(limit=80),
            "signals": signal_rows(limit=80),
            "open_positions": open_rows(),
            "closed_trades": closed_rows(limit=80),
        }
    except Exception as e:
        log.warning("young_trail_helius ops: %s", safe_err(e))
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "error": "unavailable",
            "summary": _summary(),
            "quality": quality(),
            "comparison": [],
            "signals": [],
            "open_positions": [],
            "closed_trades": [],
        }
