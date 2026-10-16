"""
FOMO client. FOMO has no public API, so the collector reads your own logged-in session.

The bearer is a Privy access token that lives about an hour. Two ways to get it:

  1. Chrome over CDP (the guide's way). Launch the Chrome profile you log into FOMO with:
        open -a "Google Chrome" --args --remote-debugging-port=9222      # mac
        google-chrome --remote-debugging-port=9222                       # linux
     keep a fomo.family tab open, and leave that profile alone. Fomo.token() pulls the
     token out of that tab's localStorage and refreshes it on its own.

  2. FOMO_BEARER env var. Paste the token yourself (DevTools -> Application ->
     Local Storage -> privy:token). Dies hourly, fine for a one-off test.

    POST prod-api.fomo.family/proxy/filterTokens
    body: ["<address>:<netId>", ...]      20 at a time
    -> marketCap, liquidity, volume24, holders, priceUSD,
       change5m / change1 / change4 / change12 / change24, createdAt

netIds: Solana 1399811149 · Robinhood 4663 · BSC 56 · Base 8453 · ETH 1 · Monad 143
"""
import base64
import json
import logging
import os
import time

import requests

from secret_utils import safe_err

log = logging.getLogger("fomo")

FOMO_API   = os.environ.get("FOMO_API", "https://prod-api.fomo.family")
CDP_URL    = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
BATCH      = 20
TOKEN_TTL  = 50 * 60            # fallback TTL when JWT exp is unreadable
JWT_SKEW_SEC = 300              # refresh when exp - now < 5 min
RELOAD_WAIT_SEC = 15            # after a 401 page reload, poll this long
RELOAD_POLL_SEC = 2
AUTH_DOWN_AFTER = 2             # consecutive failed cycles before the banner
# Privy keeps the access token in localStorage under these keys (first hit wins)
PRIVY_KEYS = ("privy:token", "privy:access_token")
# Multi-chain support: header required for BSC, Robinhood, etc. Without it, FOMO returns Solana only
FOMO_SUPPORTED_CHAINS = os.environ.get("FOMO_SUPPORTED_CHAINS", "56,143,4663,8453,1399811149")

# FOMO -> desk field names. normalise() in collect.py reads exactly these.
CHANGE_WINDOWS = {"change5m": 300, "change1": 3600, "change4": 14400,
                  "change12": 43200, "change24": 86400}


class FomoAuthError(RuntimeError):
    pass


_time_fn = time.time
_sleep_fn = time.sleep


def set_time_fn(fn):
    global _time_fn
    _time_fn = time.time if fn is None else fn


def set_sleep_fn(fn):
    global _sleep_fn
    _sleep_fn = time.sleep if fn is None else fn


def current_time() -> float:
    return float(_time_fn())


def _sleep(seconds: float) -> None:
    fn = _sleep_fn
    if fn is not None:
        fn(max(0.0, float(seconds)))
        return
    time.sleep(max(0.0, float(seconds)))


def jwt_exp(token: str | None) -> float | None:
    """JWT `exp` claim only. Never logs the token. None if unreadable."""
    if not token or not isinstance(token, str) or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        pad = "=" * ((4 - len(payload) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload + pad))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp")
    try:
        return float(exp)
    except (TypeError, ValueError):
        return None


class Fomo:
    def __init__(self, bearer: str | None = None):
        self._bearer = bearer or os.environ.get("FOMO_BEARER")
        self._fetched_at = current_time() if self._bearer else 0.0
        self._last_error = None          # Last FomoAuthError message
        self._last_error_at = None       # When the last error occurred
        self._cdp_reachable = None       # Last CDP reachability check result
        self._reloaded_this_cycle = False
        self._consecutive_auth_failures = 0
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json",
                               "Origin": "https://fomo.family",
                               "Referer": "https://fomo.family/",
                               "X-Supported-Chains": FOMO_SUPPORTED_CHAINS,
                               "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                             "AppleWebKit/537.36 Chrome/128.0 Safari/537.36"})

    # ---- bearer ----------------------------------------------------------------
    def begin_cycle(self) -> None:
        """Reset the one-reload-per-cycle budget. main() calls this each cycle."""
        self._reloaded_this_cycle = False

    def note_auth_ok(self) -> None:
        self._consecutive_auth_failures = 0

    def note_auth_failure(self) -> None:
        self._consecutive_auth_failures = int(self._consecutive_auth_failures or 0) + 1
        n = self._consecutive_auth_failures
        if n >= AUTH_DOWN_AFTER:
            log.error(
                "FOMO AUTH DOWN: %d consecutive cycles failed; "
                "log into fomo.family in the FomoDesk Chrome (CDP 9222)",
                n,
            )

    def _accept_token(self, tok: str) -> str:
        self._bearer = tok
        self._fetched_at = current_time()
        self._last_error = None
        return tok

    def _jwt_expired(self, tok: str | None, now: float | None = None) -> bool:
        exp = jwt_exp(tok)
        if exp is None:
            return False
        ts = current_time() if now is None else float(now)
        return exp <= ts

    def _bearer_is_fresh(self, now: float | None = None) -> bool:
        if not self._bearer:
            return False
        ts = current_time() if now is None else float(now)
        exp = jwt_exp(self._bearer)
        if exp is not None:
            return (exp - ts) >= JWT_SKEW_SEC
        return (ts - float(self._fetched_at or 0.0)) < TOKEN_TTL

    def token(self, force: bool = False) -> str:
        """Current bearer, refreshed out of Chrome when stale. main() calls this every cycle."""
        if self._bearer and not force and self._bearer_is_fresh():
            return self._bearer
        fresh = self._from_chrome()
        if fresh:
            return self._accept_token(fresh)
        elif not self._bearer:
            err_msg = "no FOMO bearer: log into fomo.family in Chrome started with --remote-debugging-port=9222, or set FOMO_BEARER"
            self._last_error = err_msg
            self._last_error_at = current_time()
            raise FomoAuthError(err_msg)
        return self._bearer

    def health(self) -> dict:
        """
        Return FOMO auth health status. NEVER returns the bearer token itself.
        Safe for ops panel display and logging.
        """
        bearer_present = self._bearer is not None
        now = current_time()
        bearer_age_seconds = now - self._fetched_at if bearer_present else None
        bearer_source = "env" if os.environ.get("FOMO_BEARER") else "cdp"
        n_fail = int(self._consecutive_auth_failures or 0)

        return {
            "bearer_present": bearer_present,
            "bearer_age_seconds": bearer_age_seconds,
            "bearer_source": bearer_source,
            "last_refresh_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._fetched_at)) if bearer_present else None,
            "ttl_seconds": TOKEN_TTL,
            "needs_refresh": (not self._bearer_is_fresh(now)) if bearer_present else True,
            "last_error": self._last_error,
            "last_error_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._last_error_at)) if self._last_error_at else None,
            "cdp_reachable": self._cdp_reachable,
            "cdp_url": CDP_URL,
            "consecutive_auth_failures": n_fail,
            "auth_down": n_fail >= AUTH_DOWN_AFTER,
        }

    def _list_tabs(self) -> list | None:
        try:
            tabs = requests.get(f"{CDP_URL}/json", timeout=5).json()
            self._cdp_reachable = True
            return tabs if isinstance(tabs, list) else []
        except Exception as e:
            log.debug("CDP not reachable at %s: %s", CDP_URL, safe_err(e))
            self._cdp_reachable = False
            return None

    def _fomo_tab(self) -> dict | None:
        tabs = self._list_tabs()
        if not tabs:
            return None
        return next(
            (t for t in tabs if t.get("type") == "page" and "fomo.family" in t.get("url", "")),
            None,
        )

    def _cdp_eval(self, tab: dict, expression: str, timeout: float = 5):
        """Runtime.evaluate on an existing tab. Caller must not log the result."""
        try:
            import websocket                      # websocket-client
        except ImportError:
            log.error("pip install websocket-client to read the bearer out of Chrome")
            return None
        url = tab.get("webSocketDebuggerUrl")
        if not url:
            return None
        ws = websocket.create_connection(url, timeout=timeout)
        try:
            ws.send(json.dumps({
                "id": 1, "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True},
            }))
            return json.loads(ws.recv())
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _from_chrome(self) -> str | None:
        """Read the Privy token from the logged-in fomo.family tab via the DevTools protocol."""
        tab = self._fomo_tab()
        if tab is None and self._cdp_reachable is False:
            return None
        if not tab:
            log.warning("no fomo.family tab open in the CDP Chrome profile")
            return None
        expr = ("(() => { for (const k of %s) { const v = localStorage.getItem(k); "
                "if (v) return v; } return null; })()" % json.dumps(list(PRIVY_KEYS)))
        try:
            res = self._cdp_eval(tab, expr)
        except Exception as e:
            log.warning("CDP evaluate failed: %s", safe_err(e))
            return None
        if not isinstance(res, dict):
            return None
        raw = ((res.get("result") or {}).get("result") or {}).get("value")
        if not raw:
            return None
        try:
            raw = json.loads(raw)                 # Privy stores it JSON-encoded
        except (TypeError, ValueError):
            pass
        return raw if isinstance(raw, str) and raw.count(".") == 2 else None

    def _reload_fomo_tab(self) -> None:
        """Reload the existing fomo.family tab. No new tabs, no cookies stored."""
        tab = self._fomo_tab()
        if not tab:
            log.warning("FOMO reload skipped: no fomo.family tab")
            return
        try:
            self._cdp_eval(tab, "location.reload()")
        except Exception as e:
            log.warning("FOMO tab reload: %s", safe_err(e))

    def _poll_new_token(self, rejected: str | None) -> str | None:
        deadline = current_time() + RELOAD_WAIT_SEC
        while current_time() < deadline:
            _sleep(RELOAD_POLL_SEC)
            fresh = self._from_chrome()
            if fresh and fresh != rejected and not self._jwt_expired(fresh):
                return fresh
        return None

    def _recover_after_401(self, rejected: str | None) -> str:
        """Re-read the tab token. Reload once per cycle if it is stale or expired."""
        fresh = self._from_chrome()
        if fresh and fresh != rejected and not self._jwt_expired(fresh):
            return self._accept_token(fresh)
        if not self._reloaded_this_cycle:
            self._reload_fomo_tab()
            self._reloaded_this_cycle = True
            fresh = self._poll_new_token(rejected)
            if fresh and fresh != rejected and not self._jwt_expired(fresh):
                return self._accept_token(fresh)
        if self._bearer:
            return self._bearer
        err_msg = "FOMO 401: log into fomo.family again"
        self._last_error = err_msg
        self._last_error_at = current_time()
        raise FomoAuthError(err_msg)

    def _do_post(self, path: str, payload, tok: str, timeout: float = 30):
        return self.s.post(
            f"{FOMO_API}{path}", json=payload, timeout=timeout,
            headers={"Authorization": f"Bearer {tok}"},
        )

    def _authorized_post(self, path: str, payload, timeout: float = 30):
        tok = self.token()
        r = self._do_post(path, payload, tok, timeout)
        if r.status_code in (401, 403):
            log.info("FOMO bearer expired, refreshing out of Chrome")
            tok = self._recover_after_401(tok)
            r = self._do_post(path, payload, tok, timeout)
            if r.status_code in (401, 403):
                err_msg = f"FOMO {r.status_code}: log into fomo.family again"
                self._last_error = err_msg
                self._last_error_at = current_time()
                raise FomoAuthError(err_msg)
        return r

    # ---- data ------------------------------------------------------------------
    def tokens(self, ids: list[str]) -> dict[str, dict]:
        """{'<addr>:<netId>': {symbol, mcap, liq, vol24, price, holders, change{sec:pct}, created}}
           Twenty per call. Hundreds of candidates cost you a handful of requests.
           Failed batches are logged and skipped (only that batch is lost)."""
        from secret_utils import safe_err
        out = {}
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            try:
                rows = self._filter_tokens(chunk)
                for tid, m in rows.items():
                    out[tid] = self._row(m)
            except FomoAuthError:
                # Auth errors must propagate to main() for ops banner and forced-refresh handling
                raise
            except Exception as e:
                # Log batch failure with safe error scrubbing, continue with remaining batches
                log.warning("FOMO filterTokens batch %d-%d failed: %s (skipping %d ids)",
                           i, i + len(chunk), safe_err(e), len(chunk))
                continue
        return out

    def trending_tokens(self) -> list[str]:
        """Fetch FOMO trending tokens feed. Returns list of '<addr>:<netId>' token ids.
           Multi-chain with X-Supported-Chains header. Zero GT budget cost."""
        try:
            r = self._authorized_post("/proxy/trendingTokens", {})
            r.raise_for_status()
            rows = r.json()
            if isinstance(rows, dict) and "responseObject" in rows:
                rows = rows["responseObject"]
            elif isinstance(rows, dict) and "data" in rows:
                rows = rows["data"]
            if not isinstance(rows, list):
                log.warning("FOMO trending returned non-list: %s", type(rows))
                return []
            ids = []
            for item in rows:
                token_data = item.get("token") if isinstance(item.get("token"), dict) else item
                addr = token_data.get("address")
                netid = token_data.get("networkId") or token_data.get("netId")
                if addr and netid:
                    ids.append(f"{addr}:{netid}")
            return ids
        except FomoAuthError:
            raise
        except Exception as e:
            log.warning("FOMO trending_tokens failed: %s", safe_err(e))
            return []

    def graduated_tokens(self) -> list[str]:
        """Fetch FOMO graduated tokens feed. Returns list of '<addr>:<netId>' token ids.
           Multi-chain with X-Supported-Chains header. Zero GT budget cost."""
        try:
            r = self._authorized_post("/proxy/graduatedTokens", {})
            r.raise_for_status()
            rows = r.json()
            if isinstance(rows, dict) and "responseObject" in rows:
                rows = rows["responseObject"]
            elif isinstance(rows, dict) and "data" in rows:
                rows = rows["data"]
            if not isinstance(rows, list):
                log.warning("FOMO graduated returned non-list: %s", type(rows))
                return []
            ids = []
            for item in rows:
                token_data = item.get("token") if isinstance(item.get("token"), dict) else item
                addr = token_data.get("address")
                netid = token_data.get("networkId") or token_data.get("netId")
                if addr and netid:
                    ids.append(f"{addr}:{netid}")
            return ids
        except FomoAuthError:
            raise
        except Exception as e:
            log.warning("FOMO graduated_tokens failed: %s", safe_err(e))
            return []

    def _filter_tokens(self, chunk: list[str]) -> dict[str, dict]:
        # Retry logic for transient network errors with exponential backoff
        max_retries = 4
        base_delay = 0.5
        for attempt in range(max_retries + 1):
            try:
                r = self._authorized_post("/proxy/filterTokens", chunk)
                # Retry on 502 Bad Gateway and similar transient failures
                if r.status_code in (502, 503, 504) and attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    log.warning("FOMO %d gateway error, retrying in %.1fs (attempt %d/%d)", 
                               r.status_code, delay, attempt + 1, max_retries + 1)
                    time.sleep(delay)
                    continue
                
                # After exhausting retries, check for gateway errors one last time
                if r.status_code in (502, 503, 504):
                    log.error("FOMO %d gateway error persisted after %d retries, returning empty result for this chunk",
                             r.status_code, max_retries)
                    return {}
                
                r.raise_for_status()
                break
            except FomoAuthError:
                raise
            except (requests.exceptions.ConnectionError, ConnectionResetError) as e:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    log.warning("Connection error on filterTokens: %s, retrying in %.1fs (attempt %d/%d)", 
                               safe_err(e), delay, attempt + 1, max_retries + 1)
                    # Close adapters to reset connection pool; session will reconnect on next request
                    for adapter in self.s.adapters.values():
                        adapter.close()
                    time.sleep(delay)
                    continue
                else:
                    raise
        
        body = r.json()
        # be tolerant about the envelope: list of rows, or {id: row}, or {"data": [...]} or {"responseObject": [...]}
        if isinstance(body, dict):
            if "responseObject" in body:
                body = body["responseObject"]
            elif "data" in body:
                body = body["data"]
        if isinstance(body, dict):
            return {k: v for k, v in body.items() if isinstance(v, dict)}
        rows = {}
        for m in body or []:
            # Handle nested token structure: live API returns {token: {address, networkId, symbol, ...}}
            token_data = m.get("token") if isinstance(m.get("token"), dict) else None
            if token_data:
                # Nested structure - extract tid from token.address:token.networkId
                tid = (f"{token_data.get('address')}:{token_data.get('networkId') or token_data.get('netId')}"
                       if token_data.get('address') else None)
                if tid:
                    # Flatten the nested structure for _row processing
                    rows[tid] = self._flatten_nested_token(m, token_data)
            else:
                # Flat structure (backward compatibility)
                tid = m.get("id") or m.get("tokenId") or (
                    f"{m.get('address')}:{m.get('networkId') or m.get('netId')}"
                    if m.get("address") else None)
                if tid:
                    rows[tid] = m
        return rows

    @staticmethod
    def _flatten_nested_token(m: dict, token_data: dict) -> dict:
        """Flatten nested {token: {...}} structure to flat dict for _row processing.
        
        Preserve top-level market metrics (marketCap, liquidity, etc.) when they exist,
        only overwriting with nested values if the nested value is not None.
        This handles the case where FOMO returns metrics at top level with a nested
        token object containing only address/networkId.
        """
        flattened = dict(m)  # Copy top-level fields (may include market metrics)
        
        # Map nested token fields to flat structure, but ONLY if non-None
        # This preserves top-level values when nested object lacks them
        updates = {}
        
        # Always update address/networkId from token (these define the token identity)
        if token_data.get("address") is not None:
            updates["address"] = token_data["address"]
        netid = token_data.get("networkId") or token_data.get("netId")
        if netid is not None:
            updates["networkId"] = netid
        
        # For market metrics, only update if present in nested token
        # (preserves top-level values when nested lacks them)
        sym = token_data.get("symbol") or token_data.get("ticker")
        if sym is not None:
            updates["symbol"] = sym
        
        mcap = token_data.get("marketCap") or token_data.get("mcap")
        if mcap is not None:
            updates["marketCap"] = mcap
        
        liq = token_data.get("liquidity") or token_data.get("liq")
        if liq is not None:
            updates["liquidity"] = liq
        
        vol = token_data.get("volume24") or token_data.get("volume24h")
        if vol is not None:
            updates["volume24"] = vol
        
        price = token_data.get("priceUSD") or token_data.get("price")
        if price is not None:
            updates["priceUSD"] = price
        
        holders = token_data.get("holders") or token_data.get("holderCount")
        if holders is not None:
            updates["holders"] = holders
        
        created = token_data.get("createdAt") or token_data.get("created")
        if created is not None:
            updates["createdAt"] = created

        tw = (token_data.get("twitter") or token_data.get("twitterUrl")
              or token_data.get("twitter_url") or token_data.get("twitterHandle")
              or token_data.get("twitter_handle") or token_data.get("x")
              or token_data.get("xUrl"))
        if tw is not None:
            updates["twitter"] = tw

        creator = (token_data.get("creator") or token_data.get("creatorAddress")
                   or token_data.get("creator_address") or token_data.get("creatorWallet")
                   or token_data.get("deployer") or token_data.get("deployerAddress")
                   or token_data.get("createdBy"))
        if creator is not None:
            updates["creator"] = creator
        
        flattened.update(updates)
        
        # Copy change fields if present in nested token
        for change_key in CHANGE_WINDOWS.keys():
            if change_key in token_data and token_data[change_key] is not None:
                flattened[change_key] = token_data[change_key]
        
        return flattened

    @staticmethod
    def _row(m: dict) -> dict:
        g = lambda *ks: next((m[k] for k in ks if m.get(k) is not None), None)
        return {"symbol":  g("symbol", "ticker", "name") or "?",
                "mcap":    _f(g("marketCap", "mcap")),
                "liq":     _f(g("liquidity", "liq")),
                "vol24":   _f(g("volume24", "volume24h", "vol24")),
                "price":   _f(g("priceUSD", "price")),
                "holders": _i(g("holders", "holderCount")),
                "top10_holders_percent": _f(g("top10HoldersPercent")),
                "change":  {sec: _f(m.get(k)) for k, sec in CHANGE_WINDOWS.items()},
                "created": g("createdAt", "created", "pairCreatedAt"),
                "twitter": g("twitter", "twitterUrl", "twitter_url",
                             "twitterHandle", "twitter_handle", "x", "xUrl"),
                "creator": g("creator", "creatorAddress", "creator_address",
                             "creatorWallet", "deployer", "deployerAddress",
                             "createdBy")}


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def activate_fomo_tab() -> dict:
    """
    Bring the fomo.family tab to the front in the CDP Chrome instance.
    If no tab exists, open https://fomo.family/ in a new tab.
    
    Returns dict with success status and message for the operator.
    """
    try:
        tabs_resp = requests.get(f"{CDP_URL}/json", timeout=5)
        tabs_resp.raise_for_status()
        tabs = tabs_resp.json()
    except Exception as e:
        log.error("CDP not reachable at %s: %s", CDP_URL, safe_err(e))
        return {
            "success": False,
            "message": f"CDP not reachable at {CDP_URL}. Is Chrome running with --remote-debugging-port=9222?",
            "error": str(e)
        }
    
    # Find existing fomo.family tab
    fomo_tab = next((t for t in tabs if t.get("type") == "page" and "fomo.family" in t.get("url", "")), None)
    
    if fomo_tab:
        # Activate existing tab
        tab_id = fomo_tab["id"]
        try:
            activate_resp = requests.get(f"{CDP_URL}/json/activate/{tab_id}", timeout=5)
            activate_resp.raise_for_status()
            log.info("activated fomo.family tab %s", tab_id)
            return {
                "success": True,
                "message": "Brought fomo.family tab to front. Log in to FOMO in THIS window (the CDP Chrome instance).",
                "tab_url": fomo_tab.get("url"),
                "action": "activated"
            }
        except Exception as e:
            log.error("failed to activate tab %s: %s", tab_id, safe_err(e))
            return {
                "success": False,
                "message": f"Found fomo.family tab but could not activate it: {e}",
                "error": str(e)
            }
    else:
        # No fomo.family tab, open one
        try:
            new_resp = requests.get(f"{CDP_URL}/json/new?https://fomo.family/", timeout=5)
            new_resp.raise_for_status()
            log.info("opened new fomo.family tab in CDP Chrome")
            return {
                "success": True,
                "message": "Opened https://fomo.family/ in a new tab. Log in to FOMO in THIS window (the CDP Chrome instance).",
                "action": "created"
            }
        except Exception as e:
            log.error("failed to open new tab: %s", safe_err(e))
            return {
                "success": False,
                "message": f"Could not open fomo.family tab: {e}",
                "error": str(e)
            }
