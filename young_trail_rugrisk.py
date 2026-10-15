"""Paper rug-avoidance features on young_trail entries. Analysis only.

Copies the same tids as the young_trail / fast / helius ledgers. For Solana
tokens, budgeted Helius RPC (SOLANA_RPC_URL) computes entry-time rug-risk
features: sniper share, bundled launch buys, creator prior rugs, and fresh
wallets among top holders. Non-Solana rows are stored as status='n/a'.

Skip rules are counterfactual only: they never gate entries, picks, or the
judge. Fail open. Exceptions through safe_err. Never logs SOLANA_RPC_URL.

Does NOT change young_trail, fast, variants, helius, judging, or live orders.
Runs after young_trail_helius on the sampler so helius keeps first claim on
the shared Helius day budget.
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

log = logging.getLogger("young_trail_rugrisk")

TICKET_USD = 50.0
LOCK_TIMEOUT_SEC = 0.2
RPC_TIMEOUT_SEC = 8.0
DEFAULT_UNTIL_ISO = "2026-10-20T11:38:07Z"
SOL_NET = 1399811149

# Own caps (same per-tick shape as helius, smaller day cap). Sit out when
# helius day_left drops below the reserve so the rug-signal ledger is not starved.
DEFAULT_CYCLE_CAP = 12
DEFAULT_DAY_CAP = 800
DEFAULT_OVERNIGHT_CYCLE_CAP = 24
DEFAULT_HELIUS_DAY_RESERVE = 400

SNIPER_SLOTS = 32
SNIPER_SKIP_PCT = 0.30
BUNDLED_MIN_BUYERS = 3
FRESH_MAX_SIGS = 2
DEV_RUG_SKIP = 1
TOP_HOLDERS = 10
MINT_SIG_LIMIT = 40
WALLET_SIG_LIMIT = 8
CREATE_TX_FETCH = 8
DEV_SIG_LIMIT = 25
FRESH_WALLET_RPC_CAP = 6

STATUS_OK = "ok"
STATUS_NA = "n/a"
STATUS_PENDING = "pending"
STATUS_ERROR = "error"
STATUS_UNAVAILABLE = "unavailable"
STATUS_BUDGET = "budget"

RULE_SNIPERS = "snipers_gt_30"
RULE_BUNDLED = "bundled"
RULE_DEV_RUGS = "dev_prior_rugs"

SKIP_RULES = (
    {
        "id": RULE_SNIPERS,
        "label": "snipers > 30%",
        "field": "skip_snipers",
        "threshold": SNIPER_SKIP_PCT,
    },
    {
        "id": RULE_BUNDLED,
        "label": "bundled launch buys",
        "field": "skip_bundled",
        "threshold": None,
    },
    {
        "id": RULE_DEV_RUGS,
        "label": "dev with prior rugs",
        "field": "skip_dev_rugs",
        "threshold": DEV_RUG_SKIP,
    },
)

CREATE_IX = {
    "initializeMint", "initializeMint2", "InitializeMint", "InitializeMint2",
    "create", "Create", "create_v2", "CreateV2", "createMetadataAccountV3",
}
RUG_IX = {
    "removeLiquidity", "remove_liquidity", "withdraw", "withdrawAll",
    "closeAccount", "sell", "withdrawProtocolFee",
}

_tls = threading.local()
_schema_lock = threading.Lock()
_time_fn = time.time
_rpc_fn = None
_overnight_fn = None
_cycle_calls = 0


def set_time_fn(fn) -> None:
    global _time_fn
    _time_fn = time.time if fn is None else fn


def set_rpc_fn(fn) -> None:
    """Inject rpc(method, params, timeout) for tests. None restores."""
    global _rpc_fn
    _rpc_fn = fn


def set_overnight_fn(fn) -> None:
    """Inject overnight predicate for tests. None restores OUTCOMES_WINDOW."""
    global _overnight_fn
    _overnight_fn = fn


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
    global _cycle_calls
    _close_local()
    _cycle_calls = 0


def _schema_step(db: sqlite3.Connection, name: str, fn) -> None:
    """One schema change. Failure is logged and does not skip later steps."""
    try:
        fn()
        db.commit()
    except Exception as e:
        log.warning("young_trail_rugrisk schema %s: %s", name, safe_err(e))
        try:
            db.rollback()
        except Exception:
            pass


def init_schema(db: sqlite3.Connection | None = None) -> None:
    """Idempotent. Each step commits on its own (live-DB safe)."""
    conn = db if db is not None else _connect()
    _schema_step(conn, "young_trail_rugrisk", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_rugrisk (
      tid TEXT PRIMARY KEY,
      ticker TEXT,
      chain TEXT,
      entry_ts REAL NOT NULL,
      entry_price REAL,
      status TEXT NOT NULL DEFAULT 'pending',
      sniper_share REAL,
      sniper_wallets INTEGER,
      sniper_slots INTEGER,
      bundled INTEGER,
      bundled_buys INTEGER,
      dev_wallet TEXT,
      dev_prior_launches INTEGER,
      dev_prior_rugs INTEGER,
      fresh_holder_share REAL,
      top_holder_n INTEGER,
      skip_snipers INTEGER,
      skip_bundled INTEGER,
      skip_dev_rugs INTEGER,
      features_json TEXT,
      error TEXT,
      source TEXT,
      computed_ts REAL
    )"""))
    _schema_step(conn, "idx_ytr_status", lambda: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ytr_status ON young_trail_rugrisk(status)"
    ))
    _schema_step(conn, "idx_ytr_entry", lambda: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ytr_entry ON young_trail_rugrisk(entry_ts)"
    ))
    _schema_step(conn, "young_trail_rugrisk_meta", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_rugrisk_meta (
      key TEXT PRIMARY KEY,
      value TEXT
    )"""))


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
    raw = os.environ.get("YOUNG_TRAIL_RUGRISK_ENABLED")
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
            "SELECT value FROM young_trail_rugrisk_meta WHERE key = ?", (key,),
        ).fetchone()
        return None if row is None else row["value"]
    except Exception as e:
        log.warning("young_trail_rugrisk meta read: %s", safe_err(e))
        return None


def _meta_set(key: str, value: str) -> None:
    try:
        db = _connect()
        db.execute(
            """INSERT INTO young_trail_rugrisk_meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        db.commit()
    except Exception as e:
        log.warning("young_trail_rugrisk meta write: %s", safe_err(e))


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
    raw = os.environ.get("YOUNG_TRAIL_RUGRISK_UNTIL")
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
        log.warning("young_trail_rugrisk until from young_trail: %s", safe_err(e))
    fallback = _parse_until(DEFAULT_UNTIL_ISO) or 0.0
    _meta_set("until_ts", str(fallback))
    return fallback


def accepting(now: float | None = None) -> bool:
    ts = current_time() if now is None else float(now)
    return enabled() and ts < until_ts(ts)


def is_overnight(now: float | None = None) -> bool:
    """True in OUTCOMES_WINDOW (default 01:00–06:00 local). Tests inject."""
    if _overnight_fn is not None:
        try:
            return bool(_overnight_fn(now))
        except Exception:
            return False
    try:
        import gt_api
        return bool(gt_api.in_outcomes_window(now))
    except Exception as e:
        log.warning("young_trail_rugrisk overnight: %s", safe_err(e))
        return False


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        n = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        n = default
    return max(0, n)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        n = float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        n = default
    return n


def cycle_cap(now: float | None = None) -> int:
    if is_overnight(now):
        return _env_int(
            "YOUNG_TRAIL_RUGRISK_OVERNIGHT_CYCLE_CAP",
            DEFAULT_OVERNIGHT_CYCLE_CAP,
        )
    return _env_int("YOUNG_TRAIL_RUGRISK_CYCLE_CAP", DEFAULT_CYCLE_CAP)


def day_cap() -> int:
    return _env_int("YOUNG_TRAIL_RUGRISK_DAY_CAP", DEFAULT_DAY_CAP)


def helius_day_reserve() -> int:
    return _env_int(
        "YOUNG_TRAIL_RUGRISK_HELIUS_DAY_RESERVE", DEFAULT_HELIUS_DAY_RESERVE,
    )


def sniper_skip_pct() -> float:
    n = _env_float("YOUNG_TRAIL_RUGRISK_SNIPER_PCT", SNIPER_SKIP_PCT)
    return min(1.0, max(0.0, n))


def sniper_slots() -> int:
    return _env_int("YOUNG_TRAIL_RUGRISK_SNIPER_SLOTS", SNIPER_SLOTS)


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
    """Reset the per-tick call counter. Does not touch young_trail_helius."""
    global _cycle_calls
    _cycle_calls = 0
    key = _day_key(now)
    if _meta_get("budget_day") != key:
        _meta_set("budget_day", key)
        _meta_set("budget_day_calls", "0")


def budget_remaining(now: float | None = None) -> dict:
    day_left = max(0, day_cap() - _day_used(now))
    cap = cycle_cap(now)
    cycle_left = max(0, cap - _cycle_calls)
    return {
        "cycle_used": _cycle_calls,
        "cycle_cap": cap,
        "cycle_left": cycle_left,
        "day_used": _day_used(now),
        "day_cap": day_cap(),
        "day_left": day_left,
        "day": _day_key(now),
        "overnight": is_overnight(now),
        "helius_day_reserve": helius_day_reserve(),
    }


def _helius_day_left(now: float | None = None) -> int | None:
    """Leftover helius day budget. None if unreadable (fail open)."""
    try:
        import young_trail_helius as helius
        left = helius.budget_remaining(now)["day_left"]
        return int(left)
    except Exception as e:
        log.warning("young_trail_rugrisk helius budget: %s", safe_err(e))
        return None


def helius_allows(now: float | None = None) -> bool:
    """False when the helius ledger is low on its day cap (don't starve it)."""
    left = _helius_day_left(now)
    if left is None:
        return True
    return left >= helius_day_reserve()


def _can_charge(n: int = 1, now: float | None = None) -> bool:
    if n <= 0:
        return True
    if not helius_allows(now):
        return False
    left = budget_remaining(now)
    return left["cycle_left"] >= n and left["day_left"] >= n


def _charge(n: int = 1, now: float | None = None) -> bool:
    """Count n Helius calls against rugrisk caps only. False if gone."""
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


def _num(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _row_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


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


def _rpc(method: str, params, timeout: float = RPC_TIMEOUT_SEC, now: float | None = None):
    """One Helius JSON-RPC call. Charges rugrisk budget first. Never logs the URL."""
    if not _charge(1, now):
        raise RuntimeError("rugrisk budget exhausted")
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
        log.warning("young_trail_rugrisk rpc %s: %s", method, safe_err(e))
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


def _tok_amt(obj) -> float:
    if not isinstance(obj, dict):
        return 0.0
    ui = _num(obj.get("uiAmount"))
    if ui is not None:
        return float(ui)
    amt = _num(obj.get("amount"))
    return float(amt) if amt is not None else 0.0


# --- pure feature math (no I/O) -------------------------------------------

def sniper_share_from_buys(buys: list[dict], supply: float, create_slot: int,
                           n_slots: int) -> dict:
    """% of supply bought in [create_slot, create_slot+n_slots]. Pure."""
    if not supply or supply <= 0 or create_slot is None:
        return {"sniper_share": None, "sniper_wallets": 0, "sniper_amount": 0.0}
    end = int(create_slot) + max(0, int(n_slots))
    wallets: dict[str, float] = {}
    for b in buys or []:
        slot = b.get("slot")
        owner = b.get("owner")
        delta = _num(b.get("delta"), 0.0) or 0.0
        if not owner or slot is None or delta <= 0:
            continue
        if int(create_slot) <= int(slot) <= end:
            wallets[str(owner)] = wallets.get(str(owner), 0.0) + float(delta)
    amount = sum(wallets.values())
    return {
        "sniper_share": amount / float(supply),
        "sniper_wallets": len(wallets),
        "sniper_amount": amount,
    }


def bundled_from_buys(buys: list[dict], create_slot: int,
                      fresh_owners: set | None = None) -> dict:
    """Multiple creation-slot buys from distinct (often fresh) wallets. Pure."""
    if create_slot is None:
        return {"bundled": False, "bundled_buys": 0, "create_slot_buyers": 0}
    buyers = set()
    fresh = set()
    known_fresh = {str(x) for x in (fresh_owners or set())}
    for b in buys or []:
        if b.get("slot") is None or int(b["slot"]) != int(create_slot):
            continue
        owner = b.get("owner")
        delta = _num(b.get("delta"), 0.0) or 0.0
        if not owner or delta <= 0:
            continue
        buyers.add(str(owner))
        if str(owner) in known_fresh:
            fresh.add(str(owner))
    n = len(buyers)
    bundled = n >= BUNDLED_MIN_BUYERS or (n >= 2 and len(fresh) >= 2)
    return {
        "bundled": bool(bundled),
        "bundled_buys": n,
        "create_slot_buyers": n,
        "create_slot_fresh": len(fresh),
    }


def fresh_holder_share(holders: list[dict], fresh_owners: set) -> dict:
    """Fresh-wallet share of non-pool top-holder amounts. Pure."""
    fresh = {str(x) for x in (fresh_owners or set())}
    held = []
    for h in holders or []:
        if h.get("is_pool"):
            continue
        owner = h.get("owner")
        amt = _num(h.get("amount"), 0.0) or 0.0
        if not owner or amt <= 0:
            continue
        held.append((str(owner), float(amt)))
    total = sum(a for _o, a in held)
    if total <= 0:
        return {"fresh_holder_share": None, "top_holder_n": len(held)}
    fresh_amt = sum(a for o, a in held if o in fresh)
    return {
        "fresh_holder_share": fresh_amt / total,
        "top_holder_n": len(held),
    }


def skip_flags(features: dict) -> dict:
    """Pure skip-rule evaluation. None = unknown (fail open: do not skip)."""
    status = features.get("status")
    if status == STATUS_NA:
        return {
            "skip_snipers": None, "skip_bundled": None, "skip_dev_rugs": None,
        }
    if status not in (STATUS_OK, None) and status != STATUS_PENDING:
        # error/unavailable/budget: unknown, never skip
        if status in (STATUS_ERROR, STATUS_UNAVAILABLE, STATUS_BUDGET):
            return {
                "skip_snipers": None, "skip_bundled": None, "skip_dev_rugs": None,
            }
    sniper = _num(features.get("sniper_share"))
    skip_s = None if sniper is None else bool(sniper > sniper_skip_pct())
    bundled = features.get("bundled")
    skip_b = None if bundled is None else bool(bundled)
    rugs = features.get("dev_prior_rugs")
    skip_d = None if rugs is None else bool(int(rugs) >= DEV_RUG_SKIP)
    return {
        "skip_snipers": skip_s,
        "skip_bundled": skip_b,
        "skip_dev_rugs": skip_d,
    }


def would_skip(row: dict, rule_id: str) -> bool | None:
    """True if this rule would have skipped the entry. None = unknown/n/a."""
    if not row or row.get("status") == STATUS_NA:
        return None
    flags = skip_flags(row)
    by_id = {
        RULE_SNIPERS: flags["skip_snipers"],
        RULE_BUNDLED: flags["skip_bundled"],
        RULE_DEV_RUGS: flags["skip_dev_rugs"],
    }
    val = by_id.get(rule_id)
    if val is None:
        field = None
        for rule in SKIP_RULES:
            if rule["id"] == rule_id:
                field = rule["field"]
                break
        if field and row.get(field) is not None:
            return bool(row[field])
        return None
    return bool(val)


def counterfactual_for_rule(rule_id: str, rows: list[dict],
                            pnl_maps: dict) -> dict:
    """SKIPPED vs KEPT realized P&L. Unmarked / null pnl excluded. Pure."""
    skipped, kept, unknown = [], [], []
    for row in rows or []:
        decision = would_skip(row, rule_id)
        tid = row.get("tid")
        if not tid:
            continue
        if decision is True:
            skipped.append(tid)
        elif decision is False:
            kept.append(tid)
        else:
            unknown.append(tid)

    def side(tids: list[str]) -> dict:
        out = {"n": len(tids), "n_with_pnl": {}}
        for name, mapping in (pnl_maps or {}).items():
            pnls = []
            for tid in tids:
                v = mapping.get(tid)
                if v is None:
                    continue
                pnls.append(float(v))
            out[f"{name}_pnl"] = sum(pnls) if pnls else 0.0
            out["n_with_pnl"][name] = len(pnls)
        return out

    rule = next((r for r in SKIP_RULES if r["id"] == rule_id), {"id": rule_id})
    return {
        "id": rule_id,
        "label": rule.get("label") or rule_id,
        "threshold": rule.get("threshold"),
        "skipped": side(skipped),
        "kept": side(kept),
        "unknown_n": len(unknown),
    }


# --- RPC fetch helpers ----------------------------------------------------

def _supply(mint: str, now: float | None = None) -> float | None:
    result = _rpc("getTokenSupply", [mint], now=now)
    value = result.get("value") if isinstance(result, dict) else result
    if not isinstance(value, dict):
        return _num(value)
    amt = _tok_amt(value)
    return amt if amt > 0 else _num(value.get("amount"))


def _largest_accounts(mint: str, now: float | None = None) -> list[dict]:
    result = _rpc("getTokenLargestAccounts", [mint], now=now)
    rows = result.get("value") if isinstance(result, dict) else result
    out = []
    for acc in rows or []:
        if not isinstance(acc, dict) or not acc.get("address"):
            continue
        amt = _tok_amt(acc)
        if amt == 0:
            amt = _num(acc.get("amount"), 0.0) or 0.0
        out.append({"address": acc["address"], "amount": float(amt)})
    return out


def _owners_for(accounts: list[str], now: float | None = None) -> dict[str, dict]:
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


def _signatures(address: str, limit: int = 20, before: str | None = None,
                now: float | None = None) -> list[dict]:
    opts: dict = {"limit": max(1, min(int(limit), 1000))}
    if before:
        opts["before"] = before
    result = _rpc("getSignaturesForAddress", [address, opts], now=now)
    rows = result if isinstance(result, list) else (result or [])
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("signature"):
            continue
        if row.get("err"):
            continue
        out.append({
            "signature": row["signature"],
            "slot": row.get("slot"),
            "blockTime": row.get("blockTime"),
        })
    return out


def _transaction(sig: str, now: float | None = None) -> dict | None:
    result = _rpc(
        "getTransaction",
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        now=now,
    )
    return result if isinstance(result, dict) else None


def _iter_parsed_ix(tx: dict):
    msg = ((tx.get("transaction") or {}).get("message") or {})
    for ix in msg.get("instructions") or []:
        if isinstance(ix, dict):
            yield ix
    meta = tx.get("meta") or {}
    for inner in meta.get("innerInstructions") or []:
        for ix in (inner.get("instructions") or []):
            if isinstance(ix, dict):
                yield ix


def _ix_type(ix: dict) -> str:
    parsed = ix.get("parsed")
    if isinstance(parsed, dict):
        return str(parsed.get("type") or "")
    return ""


def _ix_mint(ix: dict) -> str | None:
    parsed = ix.get("parsed")
    if not isinstance(parsed, dict):
        return None
    info = parsed.get("info") or {}
    mint = info.get("mint") or info.get("tokenMint")
    return str(mint) if mint else None


def _buyers_from_tx(tx: dict, mint: str) -> list[dict]:
    """Wallets whose balance of `mint` rose in this transaction."""
    if not tx or not mint:
        return []
    slot = tx.get("slot")
    meta = tx.get("meta") or {}
    pre = {}
    for row in meta.get("preTokenBalances") or []:
        if not isinstance(row, dict) or row.get("mint") != mint:
            continue
        owner = row.get("owner")
        if owner:
            pre[owner] = _tok_amt(row.get("uiTokenAmount") or row)
    post = {}
    for row in meta.get("postTokenBalances") or []:
        if not isinstance(row, dict) or row.get("mint") != mint:
            continue
        owner = row.get("owner")
        if owner:
            post[owner] = _tok_amt(row.get("uiTokenAmount") or row)
    out = []
    for owner, after in post.items():
        before = pre.get(owner, 0.0)
        delta = after - before
        if delta > 0:
            out.append({"owner": owner, "delta": delta, "slot": slot})
    return out


def _lookup_creator(tid: str) -> str | None:
    try:
        import young_trail_helius as helius
        return helius.lookup_creator(tid)
    except Exception as e:
        log.warning("young_trail_rugrisk creator: %s", safe_err(e))
        return None


def _label_rugged(mint: str) -> bool | None:
    if not mint:
        return None
    tid = f"{mint}:{SOL_NET}"
    try:
        import research
        row = research._connect().execute(
            "SELECT rugged FROM token_labels WHERE tid = ?", (tid,),
        ).fetchone()
        if row is None:
            return None
        return bool(row["rugged"])
    except Exception:
        return None


def _dev_history(creator: str, current_mint: str,
                 now: float | None = None) -> dict:
    """Prior launches by creator + cheap rug flags (LP pull / labels)."""
    empty = {"dev_prior_launches": 0, "dev_prior_rugs": 0, "prior_mints": []}
    if not creator or not _can_charge(1, now):
        return {**empty, "dev_prior_launches": None, "dev_prior_rugs": None}
    try:
        sigs = _signatures(creator, limit=DEV_SIG_LIMIT, now=now)
    except Exception as e:
        log.warning("young_trail_rugrisk dev sigs: %s", safe_err(e))
        return {**empty, "dev_prior_launches": None, "dev_prior_rugs": None}
    launches: dict[str, dict] = {}
    rug_hits: set[str] = set()
    fetched = 0
    for sig in sigs:
        if fetched >= 4 or not _can_charge(1, now):
            break
        try:
            tx = _transaction(sig["signature"], now=now)
        except Exception as e:
            log.warning("young_trail_rugrisk dev tx: %s", safe_err(e))
            continue
        fetched += 1
        if not tx:
            continue
        slot = tx.get("slot")
        for ix in _iter_parsed_ix(tx):
            kind = _ix_type(ix)
            mint = _ix_mint(ix)
            if not mint or mint == current_mint:
                continue
            if kind in CREATE_IX:
                launches.setdefault(mint, {"mint": mint, "slot": slot, "rugged": False})
            if kind in RUG_IX:
                rug_hits.add(mint)
                if mint in launches:
                    launches[mint]["rugged"] = True
                else:
                    launches[mint] = {"mint": mint, "slot": slot, "rugged": True}
    for mint, rec in list(launches.items()):
        labeled = _label_rugged(mint)
        if labeled:
            rec["rugged"] = True
        if mint in rug_hits:
            rec["rugged"] = True
    n_launch = len(launches)
    n_rug = sum(1 for r in launches.values() if r.get("rugged"))
    return {
        "dev_prior_launches": n_launch,
        "dev_prior_rugs": n_rug,
        "prior_mints": list(launches.values()),
    }


def compute_features(mint: str, creator: str | None = None,
                     now: float | None = None) -> dict:
    """Entry-time rug-risk features via budgeted RPC. Mock-friendly."""
    ts = current_time() if now is None else float(now)
    if not mint:
        return {"status": STATUS_UNAVAILABLE, "error": "no mint"}
    # Core path: supply + largest + owners + mint sigs ≈ 4 calls.
    if not _can_charge(4, ts):
        return {"status": STATUS_BUDGET, "error": "budget"}
    try:
        supply = _supply(mint, now=ts)
        largest = _largest_accounts(mint, now=ts)
        addrs = [a["address"] for a in largest]
        owners = _owners_for(addrs, now=ts) if addrs and _can_charge(1, ts) else {}
        holders = []
        for acc in largest:
            meta = owners.get(acc["address"]) or {}
            owner = meta.get("owner")
            program = meta.get("program")
            is_pool = bool(program and _is_amm(program))
            holders.append({
                "account": acc["address"],
                "owner": owner,
                "amount": acc["amount"],
                "is_pool": is_pool,
            })
        holders.sort(key=lambda h: h["amount"], reverse=True)
        top = [h for h in holders if not h.get("is_pool")][:TOP_HOLDERS]

        mint_sigs = _signatures(mint, limit=MINT_SIG_LIMIT, now=ts) if _can_charge(1, ts) else []
        # Newest first. Oldest in this window is a creation proxy for young tokens.
        oldest_first = list(reversed(mint_sigs))
        history_truncated = len(mint_sigs) >= MINT_SIG_LIMIT
        txs = []
        for sig in oldest_first[:CREATE_TX_FETCH]:
            if not _can_charge(1, ts):
                break
            try:
                tx = _transaction(sig["signature"], now=ts)
            except Exception as e:
                log.warning("young_trail_rugrisk tx: %s", safe_err(e))
                continue
            if tx:
                txs.append(tx)
        buys: list[dict] = []
        for tx in txs:
            buys.extend(_buyers_from_tx(tx, mint))
        create_slot = None
        slots = [b["slot"] for b in buys if b.get("slot") is not None]
        if slots:
            create_slot = min(int(s) for s in slots)
        elif oldest_first and oldest_first[0].get("slot") is not None:
            create_slot = int(oldest_first[0]["slot"])

        # Dev history before freshness: it feeds a skip rule; leftover then
        # covers fresh-wallet share and the bundled-fresh OR-clause.
        dev = {"dev_prior_launches": None, "dev_prior_rugs": None, "prior_mints": []}
        if creator:
            dev = _dev_history(creator, mint, now=ts)

        candidates = []
        seen = set()
        for b in buys:
            if create_slot is not None and b.get("slot") == create_slot:
                o = b.get("owner")
                if o and o not in seen:
                    seen.add(o)
                    candidates.append(o)
        for h in top:
            o = h.get("owner")
            if o and o not in seen:
                seen.add(o)
                candidates.append(o)
        fresh_owners: set[str] = set()
        checked = 0
        for owner in candidates:
            if checked >= FRESH_WALLET_RPC_CAP or not _can_charge(1, ts):
                break
            try:
                wsigs = _signatures(owner, limit=WALLET_SIG_LIMIT, now=ts)
            except Exception as e:
                log.warning("young_trail_rugrisk wallet sigs: %s", safe_err(e))
                continue
            checked += 1
            if len(wsigs) <= FRESH_MAX_SIGS:
                fresh_owners.add(owner)

        sniper = sniper_share_from_buys(
            buys, supply or 0.0, create_slot, sniper_slots(),
        )
        bundled = bundled_from_buys(buys, create_slot, fresh_owners)
        fresh = fresh_holder_share(top, fresh_owners)

        status = STATUS_OK
        if supply is None or create_slot is None:
            status = STATUS_UNAVAILABLE if not buys else STATUS_OK
        features = {
            "status": status,
            "sniper_share": sniper["sniper_share"],
            "sniper_wallets": sniper["sniper_wallets"],
            "sniper_slots": sniper_slots(),
            "bundled": bundled["bundled"],
            "bundled_buys": bundled["bundled_buys"],
            "dev_wallet": creator,
            "dev_prior_launches": dev.get("dev_prior_launches"),
            "dev_prior_rugs": dev.get("dev_prior_rugs"),
            "fresh_holder_share": fresh["fresh_holder_share"],
            "top_holder_n": fresh["top_holder_n"],
            "create_slot": create_slot,
            "history_truncated": history_truncated,
            "supply": supply,
        }
        flags = skip_flags(features)
        features.update(flags)
        features["features_json"] = {
            "create_slot": create_slot,
            "history_truncated": history_truncated,
            "sniper_amount": sniper.get("sniper_amount"),
            "create_slot_buyers": bundled.get("create_slot_buyers"),
            "create_slot_fresh": bundled.get("create_slot_fresh"),
            "fresh_checked": checked,
            "prior_mints": (dev.get("prior_mints") or [])[:8],
        }
        return features
    except Exception as e:
        log.warning("young_trail_rugrisk features %s: %s", mint, safe_err(e))
        return {"status": STATUS_ERROR, "error": safe_err(e)}


def _is_amm(program: str | None) -> bool:
    if not program:
        return False
    # Copied, not imported — collect.py stays untouched.
    amms = {
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
    return program in amms


# --- ledger ---------------------------------------------------------------

def _source_entries() -> list[dict]:
    try:
        import young_trail
        rows = list(young_trail.open_rows())
        rows.extend(young_trail.closed_rows(limit=None))
        return [r for r in rows if r.get("tid") and _num(r.get("entry_price"), 0) > 0]
    except Exception as e:
        log.warning("young_trail_rugrisk source entries: %s", safe_err(e))
        return []


def sync_entries(now: float | None = None) -> int:
    """Copy every young_trail tid (including pre-deploy, for overnight backfill).

    Non-Solana rows are stored as status='n/a' with no RPC. Fail open.
    """
    if not enabled():
        return 0
    ts = current_time() if now is None else float(now)
    if ts >= until_ts(ts):
        return 0
    n = 0
    try:
        db = _connect()
        for src in _source_entries():
            entry_ts = _num(src.get("entry_ts"))
            if entry_ts is None:
                continue
            sol = is_solana(src.get("tid"), src.get("chain"))
            status = STATUS_PENDING if sol else STATUS_NA
            cur = db.execute(
                """INSERT OR IGNORE INTO young_trail_rugrisk (
                     tid, ticker, chain, entry_ts, entry_price, status, source
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    src["tid"], src.get("ticker"),
                    src.get("chain") or ("solana" if sol else src.get("chain")),
                    float(entry_ts), float(src["entry_price"]),
                    status, "sync",
                ),
            )
            if cur.rowcount:
                n += 1
        db.commit()
        if n:
            log.info("young_trail_rugrisk sync entries=%d paper=1", n)
    except Exception as e:
        log.warning("young_trail_rugrisk sync: %s", safe_err(e))
    return n


def _store_features(tid: str, feat: dict, now: float, source: str) -> None:
    flags = skip_flags(feat)
    extra = feat.get("features_json")
    extra_s = json.dumps(extra) if extra is not None else None
    err = feat.get("error")
    if err is not None:
        err = str(err)[:400]
    db = _connect()
    db.execute(
        """UPDATE young_trail_rugrisk SET
             status = ?, sniper_share = ?, sniper_wallets = ?, sniper_slots = ?,
             bundled = ?, bundled_buys = ?, dev_wallet = ?,
             dev_prior_launches = ?, dev_prior_rugs = ?,
             fresh_holder_share = ?, top_holder_n = ?,
             skip_snipers = ?, skip_bundled = ?, skip_dev_rugs = ?,
             features_json = ?, error = ?, source = ?, computed_ts = ?
           WHERE tid = ?""",
        (
            feat.get("status") or STATUS_ERROR,
            feat.get("sniper_share"), feat.get("sniper_wallets"),
            feat.get("sniper_slots"),
            None if feat.get("bundled") is None else int(bool(feat.get("bundled"))),
            feat.get("bundled_buys"), feat.get("dev_wallet"),
            feat.get("dev_prior_launches"), feat.get("dev_prior_rugs"),
            feat.get("fresh_holder_share"), feat.get("top_holder_n"),
            None if flags["skip_snipers"] is None else int(flags["skip_snipers"]),
            None if flags["skip_bundled"] is None else int(flags["skip_bundled"]),
            None if flags["skip_dev_rugs"] is None else int(flags["skip_dev_rugs"]),
            extra_s, err, source, float(now), tid,
        ),
    )
    db.commit()


def compute_pending(now: float | None = None, limit: int | None = None,
                    source: str = "live") -> int:
    """Compute features for pending Solana rows. Budget-aware. Fail open."""
    ts = current_time() if now is None else float(now)
    n = 0
    try:
        if not helius_allows(ts):
            return 0
        db = _connect()
        sql = """SELECT tid, chain FROM young_trail_rugrisk
                 WHERE status = 'pending' ORDER BY entry_ts ASC"""
        if source != "backfill":
            sql = """SELECT tid, chain FROM young_trail_rugrisk
                     WHERE status = 'pending' ORDER BY entry_ts DESC"""
        rows = db.execute(sql).fetchall()
        cap = limit if limit is not None else (8 if is_overnight(ts) else 2)
        for row in rows[: max(0, cap)]:
            if not _can_charge(4, ts):
                break
            tid = row["tid"]
            mint, _net = parse_tid(tid)
            creator = _lookup_creator(tid)
            feat = compute_features(mint or "", creator=creator, now=ts)
            if feat.get("status") == STATUS_BUDGET:
                break
            _store_features(tid, feat, ts, source)
            n += 1
    except Exception as e:
        log.warning("young_trail_rugrisk compute: %s", safe_err(e))
    return n


def backfill_pending(now: float | None = None, limit: int | None = None,
                     force: bool = False) -> int:
    """Overnight (or CLI) backfill of existing pending entries. Budget-limited."""
    ts = current_time() if now is None else float(now)
    if not enabled():
        return 0
    if ts >= until_ts(ts) and not force:
        return 0
    if not force and not is_overnight(ts):
        return 0
    sync_entries(ts)
    cap = limit if limit is not None else 12
    n = compute_pending(now=ts, limit=cap, source="backfill")
    if n:
        log.info("young_trail_rugrisk backfill n=%d paper=1", n)
    return n


def on_sampler_tick(prices: dict | None = None, now: float | None = None) -> dict:
    """Sampler hook AFTER helius. Copies entries, computes a few features.

    `prices` is ignored (entry-time on-chain features). Fail open. Never gates.
    """
    empty = {"entries": 0, "computed": 0, "backfill": 0, "overnight": False}
    try:
        if not enabled():
            return empty
        ts = current_time() if now is None else float(now)
        begin_cycle(ts)
        until_ts(ts)
        deploy_ts()
        n = sync_entries(ts)
        overnight = is_overnight(ts)
        cap = 8 if overnight else 2
        src = "backfill" if overnight else "live"
        computed = compute_pending(now=ts, limit=cap, source=src)
        return {
            "entries": n, "computed": computed,
            "backfill": computed if overnight else 0,
            "overnight": overnight,
        }
    except Exception as e:
        log.warning("young_trail_rugrisk sampler: %s", safe_err(e))
        return empty


def all_rows() -> list[dict]:
    try:
        return [_row_dict(r) for r in _connect().execute(
            "SELECT * FROM young_trail_rugrisk ORDER BY entry_ts DESC"
        ).fetchall()]
    except Exception as e:
        log.warning("young_trail_rugrisk rows: %s", safe_err(e))
        return []


def pending_rows() -> list[dict]:
    try:
        return [_row_dict(r) for r in _connect().execute(
            "SELECT * FROM young_trail_rugrisk WHERE status = 'pending' ORDER BY entry_ts"
        ).fetchall()]
    except Exception as e:
        log.warning("young_trail_rugrisk pending: %s", safe_err(e))
        return []


def _counted_pnl(rows) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows or []:
        tid = r.get("tid")
        if not tid:
            continue
        if r.get("exit_reason") == "unmarked":
            continue
        if r.get("exit_ts") is None:
            continue
        pnl = _num(r.get("pnl_usd"))
        if pnl is None:
            continue
        out[tid] = float(pnl)
    return out


def _pnl_maps() -> dict[str, dict[str, float]]:
    maps = {
        "young_trail": {},
        "fast_baseline": {},
        "fast_tp50": {},
    }
    try:
        import young_trail
        maps["young_trail"] = _counted_pnl(
            young_trail.open_rows() + young_trail.closed_rows(limit=None)
        )
    except Exception as e:
        log.warning("young_trail_rugrisk cycle pnl: %s", safe_err(e))
    try:
        import young_trail_fast as fast
        maps["fast_baseline"] = _counted_pnl(
            fast.open_rows("baseline") + fast.closed_rows("baseline", limit=None)
        )
        maps["fast_tp50"] = _counted_pnl(
            fast.open_rows("tp_50") + fast.closed_rows("tp_50", limit=None)
        )
    except Exception as e:
        log.warning("young_trail_rugrisk fast pnl: %s", safe_err(e))
    return maps


def counterfactuals(rows: list[dict] | None = None) -> list[dict]:
    """Per-rule SKIPPED vs KEPT P&L on young_trail / fast baseline / fast tp_50."""
    try:
        data = rows if rows is not None else all_rows()
        maps = _pnl_maps()
        return [counterfactual_for_rule(rule["id"], data, maps) for rule in SKIP_RULES]
    except Exception as e:
        log.warning("young_trail_rugrisk counterfactual: %s", safe_err(e))
        return []


def _summary() -> dict:
    try:
        rows = all_rows()
        n_ok = sum(1 for r in rows if r.get("status") == STATUS_OK)
        n_na = sum(1 for r in rows if r.get("status") == STATUS_NA)
        n_pending = sum(1 for r in rows if r.get("status") == STATUS_PENDING)
        n_err = sum(1 for r in rows if r.get("status") in (STATUS_ERROR, STATUS_UNAVAILABLE))
        return {
            "n": len(rows),
            "ok": n_ok,
            "n_a": n_na,
            "pending": n_pending,
            "error": n_err,
            "would_skip_snipers": sum(1 for r in rows if r.get("skip_snipers") == 1),
            "would_skip_bundled": sum(1 for r in rows if r.get("skip_bundled") == 1),
            "would_skip_dev_rugs": sum(1 for r in rows if r.get("skip_dev_rugs") == 1),
        }
    except Exception as e:
        log.warning("young_trail_rugrisk summary: %s", safe_err(e))
        return {
            "n": 0, "ok": 0, "n_a": 0, "pending": 0, "error": 0,
            "would_skip_snipers": 0, "would_skip_bundled": 0,
            "would_skip_dev_rugs": 0,
        }


def ops_payload() -> dict:
    """JSON for GET /ops/young_trail_rugrisk. Paper/simulated; no live orders."""
    try:
        now = current_time()
        until = until_ts(now) if enabled() else None
        rows = all_rows()
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "gates_entries": False,
            "enabled": enabled(),
            "accepting": accepting(now),
            "until_ts": until,
            "until_iso": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until))
                if until else None
            ),
            "ticket_usd": TICKET_USD,
            "caveat": (
                "Paper rug-risk features on young_trail entries. Skip rules are "
                "counterfactual only — they do not gate entries, picks, or the "
                "judge. Budgeted Helius RPC; helius ledger keeps first claim on "
                "the day cap. Non-Solana is n/a."
            ),
            "rules": [
                {
                    "id": r["id"], "label": r["label"],
                    "threshold": r["threshold"] if r["id"] != RULE_SNIPERS
                    else sniper_skip_pct(),
                }
                for r in SKIP_RULES
            ],
            "budget": budget_remaining(now),
            "helius_day_left": _helius_day_left(now),
            "summary": _summary(),
            "counterfactuals": counterfactuals(rows),
            "entries": rows[:80],
        }
    except Exception as e:
        log.warning("young_trail_rugrisk ops: %s", safe_err(e))
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "gates_entries": False,
            "error": "unavailable",
            "summary": _summary(),
            "counterfactuals": [],
            "entries": [],
        }


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Paper young_trail rug-risk features (no gating, no live orders)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_bf = sub.add_parser("backfill", help="Budget-limited backfill of pending entries")
    p_bf.add_argument("--limit", type=int, default=12)
    sub.add_parser("ops", help="Print ops payload JSON")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "backfill":
            n = backfill_pending(limit=args.limit, force=True)
            print(json.dumps({"backfill": n, "paper": True}, default=str))
            return 0
        if args.cmd == "ops":
            print(json.dumps(ops_payload(), default=str, indent=2))
            return 0
    except Exception as e:
        log.error("young_trail_rugrisk cli: %s", safe_err(e))
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    raise SystemExit(main())
