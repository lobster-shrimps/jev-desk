"""
SCAN and VET — the collector. Jev fetches nothing; this is the code that goes and gets everything.

SCAN = universe() + shortlist()      (GeckoTerminal new_pools -> FOMO filterTokens, 20 per call)
VET  = trade_counts() + dossier()    (DexScreener per token -> GeckoTerminal info + chain RPC)

NEVER invent a number. A field that came back null stays null and the question sees it.
NEVER let null mean fine. Missing data gets its own option and its own consequence.
NEVER pull a dossier for a token stage 2 killed. That is a wasted rate limit slot.
"""
import logging
import os
import time
from collections import deque

import requests

from fomo_api import Fomo                      # Privy bearer out of Chrome over CDP
from secret_utils import safe_err
import gt_api
import gt_info_cache
import x_lookup

log = logging.getLogger("collect")

# Per-cycle GT /info accounting. attempts/ok count real HTTP /info calls only.
# Cache hits skip the call and the limiter spend; concentration kills never look up.
_gt_info_cycle = {"attempts": 0, "ok": 0, "hits": 0, "misses": 0}


def reset_gt_info_cycle_stats() -> None:
    _gt_info_cycle.update(attempts=0, ok=0, hits=0, misses=0)


def gt_info_cycle_stats() -> dict:
    return dict(_gt_info_cycle)


class GTRateLimiter:
    """Rolling-window rate limiter for GeckoTerminal API calls.
    
    Tracks call timestamps in a 60s window. Before each call, blocks until
    fewer than N calls have been made in the last 60s. Prioritizes dossier
    calls over universe pagination via reservation.
    
    Also tracks 429 responses and enforces backoff based on Retry-After header.
    
    Supports separate budgets for universe scan vs dossiers to prevent bunching.
    
    Enforces minimum spacing between ALL calls (universe + dossier) to avoid bursts.
    
    Shared across the entire process, not per-cycle."""
    
    def __init__(self, calls_per_min: int = 8, window_sec: float = 60.0, time_fn=None,
                 backoff_floor_sec: float = 25.0, backoff_max_sec: float = 120.0,
                 min_spacing_sec: float | None = None, sleep_fn=None):
        self.calls_per_min = calls_per_min
        self.window_sec = window_sec
        self.time_fn = time_fn or time.time
        # None means resolve time.sleep at call time so tests can monkeypatch it
        self.sleep_fn = sleep_fn
        self.calls = deque()  # timestamps of calls in the window
        self.reserved = 0     # slots reserved for dossiers
        self.backoff_until = 0.0  # timestamp until which we must wait due to 429
        self.backoff_floor_sec = backoff_floor_sec  # minimum backoff for 0/missing Retry-After
        self.backoff_max_sec = backoff_max_sec  # cap on backoff duration
        self.consecutive_429s = 0  # track consecutive 429s for adaptive backoff
        self.stats_429_count = 0  # per-cycle GT 429s; reset in set_universe_budget
        self.saturated = False  # window is saturated after a 429
        
        # Minimum spacing between calls to avoid bursts (default: 60s / calls_per_min)
        # Configurable via parameter or env var GT_MIN_SPACING_SEC
        if min_spacing_sec is None:
            min_spacing_sec = 60.0 / calls_per_min
        self.min_spacing_sec = min_spacing_sec
        self.last_call_time = None  # timestamp of last call; None = no call yet
        self.cycle_deadline = None  # optional per-cycle wait deadline (F5)
        
        # Universe scan budget tracking (separate from dossier budget)
        # Default to unlimited (999999) so tests without set_universe_budget() don't break
        self.universe_budget = 999999  # set via set_universe_budget() per cycle
        self.universe_calls_used = 0  # reset per cycle
        
        # Stats for logging
        self.stats_429_count = 0  # 429s this cycle
        self.stats_wait_time = 0.0  # time waited for slots this cycle
        self.last_wait_reasons = []  # "429_backoff" | "pacing" | "rate_limit" from last wait_if_needed
    
    def available(self) -> int:
        """How many GT calls can be made without waiting."""
        self._expire_old_calls()
        return max(0, self.calls_per_min - len(self.calls))
    
    def _expire_old_calls(self):
        """Remove calls that fell outside the rolling window."""
        now = self.time_fn()
        cutoff = now - self.window_sec
        while self.calls and self.calls[0] < cutoff:
            self.calls.popleft()
    
    def record_429(self, retry_after_sec: float | None = None):
        """Record a 429 response and set backoff period.
        
        Args:
            retry_after_sec: Value from Retry-After header, or None if missing/unparseable.
                            When 0, missing, or unparseable, applies backoff_floor_sec.
                            Grows on consecutive 429s, capped at backoff_max_sec.
        """
        now = self.time_fn()
        self.consecutive_429s += 1
        self.stats_429_count += 1  # Track for cycle stats
        self.saturated = True  # mark window as saturated
        
        # Apply floor backoff when Retry-After is 0, missing, or unparseable
        # Grow on consecutive 429s: floor * (1.5 ** (consecutive - 1)), capped
        if retry_after_sec is None or retry_after_sec <= 0:
            growth_factor = 1.5 ** (self.consecutive_429s - 1)
            backoff_duration = min(self.backoff_floor_sec * growth_factor, self.backoff_max_sec)
            log.info("GT 429 received with Retry-After=%s, applying floor backoff: %.1fs "
                     "(consecutive_429s=%d, floor=%.1fs, max=%.1fs)", 
                     retry_after_sec, backoff_duration, self.consecutive_429s,
                     self.backoff_floor_sec, self.backoff_max_sec)
        else:
            backoff_duration = min(retry_after_sec, self.backoff_max_sec)
            log.info("GT 429 received, backing off for %.1fs (Retry-After: %.1fs, capped at %.1fs)", 
                     backoff_duration, retry_after_sec, self.backoff_max_sec)
        
        self.backoff_until = now + backoff_duration
    
    def reset_cycle_stats(self):
        """Reset per-cycle statistics. Called at start of each cycle."""
        self.stats_429_count = 0
        self.stats_wait_time = 0.0
        self.last_wait_reasons = []
    
    def reset_for_test(self):
        """Reset limiter state for test isolation. Clears calls, stats, and last_call_time."""
        self.calls.clear()
        self.reserved = 0
        self.backoff_until = 0.0
        self.consecutive_429s = 0
        self.saturated = False
        self.last_call_time = None
        self.cycle_deadline = None
        self.universe_budget = 999999
        self.universe_calls_used = 0
        self.stats_429_count = 0
        self.stats_wait_time = 0.0
        self.last_wait_reasons = []
    
    def _do_sleep(self, sec: float):
        """Sleep via injectable sleep_fn, or time.sleep looked up at call time."""
        (self.sleep_fn or time.sleep)(sec)

    def wait_if_needed(self, priority: bool = False, deadline: float | None = None):
        """Block until a call can be made within rate limits.
        
        If priority=True (dossier), can use reserved slots.
        If priority=False (universe), cannot use reserved slots.
        
        Also waits out any 429 backoff period and enforces minimum spacing between calls.
        If deadline is provided, returns False if waiting would exceed deadline; caller should defer token.
        Returns True if a slot was acquired, False if deadline would be exceeded."""
        now = self.time_fn()
        wait_start = now
        self.last_wait_reasons = []
        
        # First, wait out any 429 backoff period
        if self.backoff_until > now:
            wait_sec = self.backoff_until - now
            # Check deadline
            if deadline is not None and (now + wait_sec) > deadline:
                return False
            log.info("GT 429 backoff: waiting %.1fs before retry", wait_sec)
            self.last_wait_reasons.append("429_backoff")
            self._do_sleep(wait_sec)
            now = self.time_fn()
        
        # Second, enforce minimum spacing between calls
        if self.last_call_time is not None:
            time_since_last = now - self.last_call_time
            if time_since_last < self.min_spacing_sec:
                spacing_wait = self.min_spacing_sec - time_since_last
                # Check deadline
                if deadline is not None and (now + spacing_wait) > deadline:
                    return False
                log.debug("GT pacing: waiting %.1fs for min spacing (%.1fs between calls)", 
                         spacing_wait, self.min_spacing_sec)
                self.last_wait_reasons.append("pacing")
                self._do_sleep(spacing_wait)
                now = self.time_fn()
        
        # Then check rolling window rate limits - loop until slot is free
        self._expire_old_calls()
        
        # Determine effective limit based on priority
        effective_limit = self.calls_per_min if priority else (self.calls_per_min - self.reserved)
        
        while len(self.calls) >= effective_limit:
            # Wait until the oldest call expires, with small buffer to avoid boundary issues
            wait_until = self.calls[0] + self.window_sec + 0.01
            wait_sec = max(0, wait_until - now)
            if wait_sec > 0:
                # Check deadline
                if deadline is not None and (now + wait_sec) > deadline:
                    return False
                log.info("GT rate limit: waiting %.1fs for slot", wait_sec)
                self.last_wait_reasons.append("rate_limit")
                prev = now
                self._do_sleep(wait_sec)
                now = self.time_fn()
                self._expire_old_calls()
                # Frozen test clock that does not advance: stop looping, let spend() fail
                if now <= prev:
                    break
        
        # Track total wait time for stats
        total_wait = now - wait_start
        if total_wait > 0.01:
            self.stats_wait_time += total_wait
        
        return True
    
    def record_success(self):
        """Record a successful API call. Clears saturated flag and resets consecutive 429 counter."""
        self.saturated = False
        self.consecutive_429s = 0
    
    def spend(self, cost: int = 1, priority: bool = False) -> bool:
        """Try to spend `cost` slots. Returns True if budget available, False otherwise.
        
        Does NOT block - use wait_if_needed() before calling this if you want blocking behavior.
        This is for backwards compatibility with tests that check budget without waiting.
        
        Records last_call_time for spacing enforcement."""
        self._expire_old_calls()
        effective_limit = self.calls_per_min if priority else (self.calls_per_min - self.reserved)
        
        if len(self.calls) + cost <= effective_limit:
            now = self.time_fn()
            for _ in range(cost):
                self.calls.append(now)
            self.last_call_time = now  # Record for spacing enforcement
            log.debug("GT budget: spent %d, %d available", cost, self.available())
            return True
        log.warning("GT budget exhausted: tried to spend %d, only %d available", cost, self.available())
        return False
    
    def reserve(self, amount: int) -> int:
        """Reserve `amount` slots for priority use (e.g. dossiers). Returns actual reserved."""
        reserved = min(amount, self.calls_per_min)
        self.reserved = reserved
        if reserved > 0:
            log.info("GT budget: reserved %d for dossiers, %d remain for universe", 
                     reserved, self.calls_per_min - reserved)
        return reserved
    
    def set_universe_budget(self, budget: int):
        """Set the universe scan budget for this cycle and reset usage counter.
        
        Called at cycle start to allocate a fixed number of calls for universe scan.
        When exhausted, universe scan stops paging instead of waiting."""
        self.universe_budget = budget
        self.universe_calls_used = 0
        self.stats_429_count = 0
        log.info("GT universe budget: allocated %d calls for this cycle", budget)
    
    def spend_universe(self, cost: int = 1) -> bool:
        """Try to spend `cost` from the universe budget.
        
        Returns True if spent, False if universe budget exhausted.
        Does NOT block or wait - universe scan should stop when False.
        Does NOT check rolling window budget - that's checked separately."""
        if self.universe_calls_used + cost > self.universe_budget:
            log.info("GT universe budget exhausted: tried to spend %d, used %d/%d", 
                     cost, self.universe_calls_used, self.universe_budget)
            return False
        self.universe_calls_used += cost
        log.debug("GT universe budget: spent %d, used %d/%d", 
                  cost, self.universe_calls_used, self.universe_budget)
        return True

GT  = "https://api.geckoterminal.com/api/v2"
DEX = "https://api.dexscreener.com/token-pairs/v1"  # New documented endpoint
SOL_RPC = "https://api.mainnet-beta.solana.com"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) desk/1.0",
      "Accept": "application/json"}

# the three chains the desk trades, plus Base which shares the BSC question set
GT_NET   = {1399811149: "solana", 4663: "robinhood", 56: "bsc", 8453: "base"}
FOMO_NET = {v: k for k, v in GT_NET.items()}

# Map network IDs to DexScreener chain IDs
DEX_CHAIN_ID = {1399811149: "solana", 56: "bsc", 8453: "base", 4663: "robinhood"}

# Special marker for dossier failures that should trigger retry
class DossierRetryNeeded(Exception):
    """Dossier failed due to rate limit / transient error and should be retried."""
    pass


class DossierDeadlineExceeded(DossierRetryNeeded):
    """Dossier wait would cross the per-cycle deadline; caller should carry the token."""
    pass


def age_minutes(created) -> float:
    """createdAt comes back as epoch seconds or milliseconds depending on the row."""
    if not created:
        return 0.0
    c = float(created)
    if c > 1e11:                                # milliseconds
        c /= 1000
    return max(0.0, (time.time() - c) / 60)


def _gt_call_with_retry(url: str, params: dict, limiter: GTRateLimiter | None, 
                        priority: bool = False, retry_on_429: bool = False,
                        is_universe: bool = False) -> tuple[dict | None, bool]:
    """Make a rate-limited GT API call with optional 429 retry.
    
    Returns (response_json, should_continue):
      - (None, False): hard error, stop this feed
      - (None, True): budget exhausted, skip but continue other feeds
      - (data, True): success
    
    If retry_on_429=True (trending feeds), retries once on 429 with backoff.
    If is_universe=True, checks universe budget before spending rolling window budget."""
    # Check universe budget first if this is a universe call
    if is_universe and limiter and not limiter.spend_universe(1):
        # Universe budget exhausted, stop universe scan
        return None, True
    
    if limiter:
        # Wait BEFORE spending to avoid burst after backoff
        if not limiter.wait_if_needed(priority=priority):
            return None, True  # Would exceed deadline
        if not limiter.spend(1, priority=priority):
            return None, True  # budget exhausted, but continue other feeds
    
    try:
        url = gt_api.prefer_onchain_url(url, purpose="desk")
        resp = requests.get(
            url, params=params, headers=gt_api.request_headers(url, purpose="desk"), timeout=20,
        )
        if resp.status_code in (401, 403):
            free = gt_api.fallback_free_url(url)
            if free:
                gt_api.note_key_failed()
                log.info("coingecko HTTP %s, falling back to free GT", resp.status_code)
                resp = requests.get(free, params=params, headers=gt_api.UA, timeout=20)
        if resp.status_code == 429:
            # Extract Retry-After and record with limiter
            retry_after_sec = None
            if resp.headers:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        retry_after_sec = float(retry_after)
                    except (ValueError, TypeError):
                        retry_after_sec = None
            
            if limiter:
                limiter.record_429(retry_after_sec)
            
            if not retry_on_429:
                return None, False  # non-retryable feed, stop
            
            # Retry once after backoff (F4: pace the retry through spend)
            if limiter:
                if not limiter.wait_if_needed(priority=priority):
                    return None, False  # Would exceed deadline
                if not limiter.spend(1, priority=priority):
                    return None, False  # budget exhausted after backoff
            else:
                # No limiter, manual backoff
                backoff = retry_after_sec if retry_after_sec is not None else 60.0
                log.info("GT 429 (no limiter), backing off %.1fs before retry", backoff)
                time.sleep(backoff)
            
            # Retry
            url = gt_api.prefer_onchain_url(url, purpose="desk")
            resp = requests.get(
                url, params=params, headers=gt_api.request_headers(url, purpose="desk"), timeout=20,
            )
            if resp.status_code == 429:
                return None, False  # still 429 after retry, give up
        
        if limiter:
            limiter.record_success()  # clear saturated flag on success
        return resp.json(), True
    except Exception as e:
        log.warning("GT call failed: %s", safe_err(e))
        return None, False


def universe(nets=("solana", "bsc", "robinhood"), pages=2, include_trending=True, 
             limiter: GTRateLimiter | None = None, fomo=None) -> tuple[list[str], dict]:
    """Where the whole thing starts. Fresh pools per chain -> (['<addr>:<netId>', ...], {tid: gt_txns}).
       Costs one GeckoTerminal slot per chain per page, so keep pages small.
       
       robinhood is capped at 1 page to avoid 429 rate limits every cycle.
       Other networks fetch 2 pages from new_pools.
       
       Fetch order (budget=5 slot allocation):
       1. Solana new_pools page 1
       2. Solana new_pools page 2
       3. Solana trending_pools page 1
       4. Robinhood trending_pools page 1
       5. Solana trending_pools page 2 (when budget allows)
       then: Other networks' trending (BSC drops at budget=5), then new_pools.
       
       This ensures Solana's high-quality feeds and Robinhood trending always
       reach the judge even on tight budgets.
       
       Universe scan respects its own budget (set via limiter.set_universe_budget()).
       When universe budget exhausted, stops paging and uses what it has.
       
       FOMO native feeds (trending_tokens, graduated_tokens) are merged into the universe
       with zero GT budget cost. Fail soft on errors.
       
       Also returns gt_txns_cache: {tid: {"h1": {"buys": N, "sells": N}, "h6": {...}, "h24": {...}}}
       for fallback when DexScreener is degraded."""
    ids, seen = [], set()
    gt_txns_cache = {}  # {tid: transaction data from GT}
    pages_fetched = 0  # Track total pages for logging
    budget_exhausted = False  # Track if we should stop GT phases early
    
    # Helper to process pool data
    def process_pool(pool, net):
        base = ((pool.get("relationships") or {}).get("base_token") or {})
        gid  = (base.get("data") or {}).get("id")      # 'solana_<addr>'
        if not gid or "_" not in gid:
            return
        addr = gid.split("_", 1)[1]
        tid  = f"{addr}:{FOMO_NET[net]}"
        if tid not in seen:
            seen.add(tid)
            ids.append(tid)
        
        # Cache GT transaction data for DexScreener fallback
        # Prefer highest liquidity pool if multiple pools for same token
        attrs = pool.get("attributes", {})
        txns = attrs.get("transactions", {})
        if txns:
            # GT API returns reserve_in_usd as a string
            reserve_str = attrs.get("reserve_in_usd")
            try:
                liq_usd = float(reserve_str) if reserve_str else 0.0
            except (ValueError, TypeError):
                liq_usd = 0.0
            # Only cache if no existing data OR this pool has higher liquidity
            existing = gt_txns_cache.get(tid)
            if not existing or liq_usd > existing.get("_liq_usd", 0):
                gt_txns_cache[tid] = {
                    "h1": txns.get("h1", {}),
                    "h6": txns.get("h6", {}),
                    "h24": txns.get("h24", {}),
                    "m5": txns.get("m5", {}),
                    "_liq_usd": liq_usd  # track for comparison
                }
    
    # Phase 1: Solana new_pools (highest priority)
    if "solana" in nets:
        for page in range(1, pages + 1):
            r, should_continue = _gt_call_with_retry(
                f"{GT}/networks/solana/new_pools",
                {"page": page},
                limiter,
                priority=False,
                retry_on_429=False,
                is_universe=True
            )
            if r is None:
                if should_continue:
                    # Universe budget exhausted
                    log.info("universe scan budget exhausted after %d pages (during solana new_pools page %d)", 
                             pages_fetched, page)
                    budget_exhausted = True
                    break
                else:
                    log.warning("GeckoTerminal 429 on solana new_pools page %s, stopping Solana pagination", page)
                break  # stop paging Solana
            
            pages_fetched += 1
            
            for pool in r.get("data", []):
                process_pool(pool, "solana")
    
    # Check if budget exhausted in phase 1
    if budget_exhausted:
        pass  # Skip remaining GT phases, go to FOMO merge
    
    # Phase 2: Solana trending_pools page 1 (second priority - liquid tokens that reach judge)
    solana_trending_p2_skip = False  # track if we should skip page 2 later
    if not budget_exhausted and "solana" in nets and include_trending:
        r, should_continue = _gt_call_with_retry(
            f"{GT}/networks/solana/trending_pools",
            {"page": 1},
            limiter,
            priority=False,
            retry_on_429=True,  # trending feeds retry once on 429
            is_universe=True
        )
        if r is None:
            if should_continue:
                # Universe budget exhausted
                log.info("universe scan budget exhausted after %d pages (during solana trending_pools p1)", 
                         pages_fetched)
                budget_exhausted = True
            else:
                log.warning("GeckoTerminal 429 on solana trending_pools p1, skipping Solana trending")
                solana_trending_p2_skip = True  # also skip page 2
        else:
            pages_fetched += 1
            
            for pool in r.get("data", []):
                process_pool(pool, "solana")
    
    # Phase 3: Robinhood trending_pools (third priority)
    if not budget_exhausted and include_trending and "robinhood" in nets:
        r, should_continue = _gt_call_with_retry(
            f"{GT}/networks/robinhood/trending_pools",
            {"page": 1},
            limiter,
            priority=False,
            retry_on_429=True,  # trending feeds retry once
            is_universe=True
        )
        if r is None:
            if should_continue:
                # Universe budget exhausted
                log.info("universe scan budget exhausted after %d pages (during robinhood trending_pools)", 
                         pages_fetched)
                budget_exhausted = True
            else:
                log.warning("GeckoTerminal 429 on robinhood trending_pools, skipping Robinhood trending")
        else:
            pages_fetched += 1
            
            for pool in r.get("data", []):
                process_pool(pool, "robinhood")
    
    # Phase 3.5: Solana trending_pools page 2 (fourth priority, after robinhood)
    if not budget_exhausted and "solana" in nets and include_trending and not solana_trending_p2_skip:
        r, should_continue = _gt_call_with_retry(
            f"{GT}/networks/solana/trending_pools",
            {"page": 2},
            limiter,
            priority=False,
            retry_on_429=False,  # no retry on page 2
            is_universe=True
        )
        if r is None:
            if should_continue:
                # Universe budget exhausted
                log.info("universe scan budget exhausted after %d pages (during solana trending_pools p2)", 
                         pages_fetched)
                budget_exhausted = True
            else:
                log.warning("GeckoTerminal 429 on solana trending_pools p2, skipping")
        else:
            pages_fetched += 1
            
            for pool in r.get("data", []):
                process_pool(pool, "solana")
    
    # Phase 4: Other networks' trending_pools (BSC, Base, etc - lower priority, drops when budget=5)
    other_trending_nets = [n for n in nets if n not in ("solana", "robinhood")]
    if not budget_exhausted and include_trending and other_trending_nets:
        for net in other_trending_nets:
            r, should_continue = _gt_call_with_retry(
                f"{GT}/networks/{net}/trending_pools",
                {"page": 1},
                limiter,
                priority=False,
                retry_on_429=True,  # trending feeds retry once
                is_universe=True
            )
            if r is None:
                if should_continue:
                    # Universe budget exhausted
                    log.info("universe scan budget exhausted after %d pages (during %s trending_pools)", 
                             pages_fetched, net)
                    budget_exhausted = True
                    break
                else:
                    log.warning("GeckoTerminal 429 on %s trending_pools, skipping trending for this network", net)
                    continue
            
            pages_fetched += 1
            
            for pool in r.get("data", []):
                process_pool(pool, net)
    
    # Phase 5: Other networks' new_pools (lowest priority - BSC, Robinhood after trending)
    other_nets = [n for n in nets if n != "solana"]
    if not budget_exhausted:
        for net in other_nets:
            net_pages = 1 if net == "robinhood" else pages
            for page in range(1, net_pages + 1):
                r, should_continue = _gt_call_with_retry(
                    f"{GT}/networks/{net}/new_pools",
                    {"page": page},
                    limiter,
                    priority=False,
                    retry_on_429=False,
                    is_universe=True
                )
                if r is None:
                    if should_continue:
                        # Universe budget exhausted
                        log.info("universe scan budget exhausted after %d pages (during %s new_pools page %d)", 
                                 pages_fetched, net, page)
                        budget_exhausted = True
                        break
                    else:
                        log.warning("GeckoTerminal 429 on %s new_pools page %s, stopping pagination for this network", net, page)
                    break  # stop paging this network
                
                pages_fetched += 1
                
                for pool in r.get("data", []):
                    process_pool(pool, net)
            
            if budget_exhausted:
                break  # Exit outer loop too
    
    gt_count = len(ids)
    
    # Merge FOMO native feeds (trending, graduated) - zero GT budget cost
    # Always runs regardless of GT budget exhaustion
    fomo_trending_count = 0
    fomo_trending_dupes = 0
    fomo_graduated_count = 0
    fomo_graduated_dupes = 0
    if fomo:
        try:
            trending_ids = fomo.trending_tokens()
            for tid in trending_ids:
                if tid not in seen:
                    seen.add(tid)
                    ids.append(tid)
                    fomo_trending_count += 1
                else:
                    fomo_trending_dupes += 1
        except Exception as e:
            log.warning("FOMO trending_tokens failed, continuing with GT ids: %s", safe_err(e))
        
        try:
            graduated_ids = fomo.graduated_tokens()
            for tid in graduated_ids:
                if tid not in seen:
                    seen.add(tid)
                    ids.append(tid)
                    fomo_graduated_count += 1
                else:
                    fomo_graduated_dupes += 1
        except Exception as e:
            log.warning("FOMO graduated_tokens failed, continuing with GT ids: %s", safe_err(e))
    
    # Log comprehensive source breakdown
    total_dupes = fomo_trending_dupes + fomo_graduated_dupes
    log.info("universe scan: GT %d (%d pages), FOMO trending +%d, FOMO graduated +%d, %d dupes, total %d",
             gt_count, pages_fetched, fomo_trending_count, fomo_graduated_count, total_dupes, len(ids))
    return ids, gt_txns_cache


def normalise(tid: str, m: dict) -> dict:
    """FOMO's field names become the desk's field names, once, here.
       Every file downstream reads these names and only these.
       
       NEVER invent a number. Preserve None for truly missing data.
       The `or 0.0` pattern is REMOVED: missing market metrics stay None
       so downstream filters can distinguish 'no data' from 'zero liquidity'.
       """
    addr, net = tid.split(":")
    return {"addr": addr, "net": int(net), "tid": tid, "ticker": m["symbol"],
            "mcap_usd": m["mcap"], "liquidity_usd": m["liq"],
            "volume_h24": m["vol24"], "price_usd": m["price"],
            "holder_count": m["holders"],
            "fomo_top10_holders_percent": m.get("top10_holders_percent"),
            "change": {"5m": m["change"].get(300), "1h": m["change"].get(3600),
                       "4h": m["change"].get(14400), "24h": m["change"].get(86400)},
            "age_minutes": age_minutes(m["created"]),
            "fomo_twitter": m.get("twitter"),
            "creator": m.get("creator")}


def shortlist(fomo: Fomo, ids: list[str]) -> list[dict]:
    """Pass one over everything FOMO knows. No network beyond FOMO itself:
       one call per twenty tokens, and not a single request per token."""
    out = []
    dropped = 0
    fomo_rows = fomo.tokens(ids)
    for tid, m in fomo_rows.items():             # 20 per call
        try:
            t = normalise(tid, m)
        except (KeyError, TypeError) as e:
            log.debug("skip %s, malformed row: %s", tid, e)
            dropped += 1
            continue
        if t["net"] in GT_NET:
            out.append(t)
        else:
            dropped += 1
    
    # Log FOMO shortlist stats
    log.info("FOMO shortlist: %d ids requested, %d returned by FOMO, %d kept, %d dropped (malformed or unsupported net)",
             len(ids), len(fomo_rows), len(out), dropped)
    
    # turnover ranks the queue. It orders work, it does not decide anything.
    # Missing data (None) gets lowest priority (treat as turnover = 0).
    def turnover(t):
        vol, mcap = t["volume_h24"], t["mcap_usd"]
        if vol is None or mcap is None:
            return 0.0  # lowest priority for missing data
        return vol / max(mcap, 1)
    out.sort(key=turnover, reverse=True)
    return out


_EMPTY_TRADES = {"buys_h1": None, "sells_h1": None, "buys_h6": None, "sells_h6": None,
                 "buys_m5": None, "sells_m5": None,
                 "trades_h24": None, "pair_address": None}


def _trade_fields_from_txns(txns: dict) -> dict:
    """buys/sells for h1, h6, m5 plus h24 trade count. Missing windows stay None."""
    txns = txns or {}
    h1 = txns.get("h1") or {}
    h6 = txns.get("h6") or {}
    h24 = txns.get("h24") or {}
    m5 = txns.get("m5") or {}
    buys_h24 = h24.get("buys") or 0
    sells_h24 = h24.get("sells") or 0
    return {
        "buys_h1": h1.get("buys"),
        "sells_h1": h1.get("sells"),
        "buys_h6": h6.get("buys"),
        "sells_h6": h6.get("sells"),
        "buys_m5": m5.get("buys"),
        "sells_m5": m5.get("sells"),
        "trades_h24": buys_h24 + sells_h24,
    }


# Known liquid tokens for canary checks (Solana addresses)
CANARY_TOKENS = {
    1399811149: "So11111111111111111111111111111111111111112",  # Wrapped SOL
}


def dex_canary_check(net: int) -> bool:
    """Check if DexScreener is healthy by querying a known liquid token.
    
    Returns True if canary is healthy (has pairs), False if degraded (empty/error).
    """
    canary_addr = CANARY_TOKENS.get(net)
    chain_id = DEX_CHAIN_ID.get(net)
    
    if not canary_addr or not chain_id:
        log.debug("No canary token/chain configured for net %s, skipping canary check", net)
        return True  # no canary available, assume healthy
    
    try:
        resp = requests.get(f"{DEX}/{chain_id}/{canary_addr}", headers=UA, timeout=20)
        if resp.status_code != 200:
            log.warning("DexScreener canary HTTP %d for net %s", resp.status_code, net)
            return False
        
        try:
            data = resp.json()
        except Exception:
            log.warning("DexScreener canary JSON parse error for net %s", net)
            return False
        
        # New endpoint returns array directly - null or empty = degraded
        if data is None or (isinstance(data, list) and len(data) == 0):
            log.warning("DexScreener canary empty/null for net %s (known liquid token %s has no pairs)", 
                        net, canary_addr)
            return False
        
        log.info("DexScreener canary healthy for net %s (%d pairs)", net, len(data) if isinstance(data, list) else 0)
        return True
        
    except Exception as e:
        log.warning("DexScreener canary check failed for net %s: %s", net, e)
        return False


def trade_counts(t: dict, gt_txns_cache: dict = None) -> tuple[dict, str]:
    """buys and sells per window. FOMO does not return them, DexScreener does.
       Called ONLY for tokens that already cleared the free checks. One per token,
       so this runs on tens, never on the whole universe.
       
       When DexScreener fails/degrades, falls back to GT transaction data from universe scan.
       
       Returns (trade_data, status):
         status: 'ok' (got data), 'empty' (no pairs found), 'error' (HTTP/network error),
                 'gt_fallback' (used GT data due to Dex error/empty)
    """
    gt_txns_cache = gt_txns_cache or {}
    chain_id = DEX_CHAIN_ID.get(t["net"])
    
    if not chain_id:
        log.warning("No DexScreener chain_id for net %s, using GT fallback", t["net"])
        return _try_gt_fallback(t, gt_txns_cache, genuine_empty=False)
    
    try:
        resp = requests.get(f"{DEX}/{chain_id}/{t['addr']}", headers=UA, timeout=20)
        
        # Check HTTP status codes - only 200 is ok
        if resp.status_code != 200:
            log.warning("DexScreener HTTP %d for %s (%s)", resp.status_code, t["ticker"], t["addr"])
            # Try GT fallback on HTTP errors
            return _try_gt_fallback(t, gt_txns_cache)
        
        try:
            data = resp.json()
        except Exception as e:
            log.warning("DexScreener JSON parse error for %s (%s): %s", t["ticker"], t["addr"], e)
            return _try_gt_fallback(t, gt_txns_cache)
        
        # New endpoint returns array directly (not wrapped in {"pairs": ...})
        # null or [] = genuinely no pairs found (no_pair)
        if data is None or (isinstance(data, list) and len(data) == 0):
            # Genuinely empty - try GT fallback first
            log.info("DexScreener empty/null for %s, trying GT fallback", t["ticker"])
            return _try_gt_fallback(t, gt_txns_cache, genuine_empty=True)
        
        if not isinstance(data, list):
            log.warning("DexScreener unexpected response type for %s: %s", t["ticker"], type(data))
            return _try_gt_fallback(t, gt_txns_cache)
        
    except requests.exceptions.Timeout:
        log.warning("DexScreener timeout for %s (%s)", t["ticker"], t["addr"])
        return _try_gt_fallback(t, gt_txns_cache)
    except requests.exceptions.RequestException as e:
        log.warning("DexScreener network error for %s (%s): %s", t["ticker"], t["addr"], e)
        return _try_gt_fallback(t, gt_txns_cache)
    except Exception as e:
        log.warning("DexScreener unexpected error for %s (%s): %s", t["ticker"], t["addr"], e)
        return _try_gt_fallback(t, gt_txns_cache)
    
    # Parse the pairs data - pick by highest h24 txns (buys+sells), tie-break by liquidity
    try:
        if not data:  # Empty list already handled above, but be defensive
            return _try_gt_fallback(t, gt_txns_cache, genuine_empty=True)
        
        # Sort by h24 txns (primary), then liquidity (tie-breaker)
        def pair_score(p):
            txns = p.get("txns") or {}
            h24 = txns.get("h24") or {}
            buys = h24.get("buys") or 0
            sells = h24.get("sells") or 0
            total_txns = buys + sells
            liq = (p.get("liquidity") or {}).get("usd") or 0
            return (total_txns, liq)  # tuple sorts by first element, then second
        
        best_pair = max(data, key=pair_score)
        t["dex_twitter"] = _dex_twitter_from_pair(best_pair)
        
        txns = best_pair.get("txns") or {}
        fields = _trade_fields_from_txns(txns)
        # Stash DexScreener windows on t so pick.summary / market state can use them
        # without another call. m5 is omitted downstream when missing.
        t.update({k: fields[k] for k in ("buys_h1", "sells_h1", "buys_h6", "sells_h6",
                                         "buys_m5", "sells_m5")})
        vol = best_pair.get("volume") if isinstance(best_pair.get("volume"), dict) else {}
        if vol.get("m5") is not None and t.get("vol_5m") is None:
            t["vol_5m"] = vol.get("m5")
        if vol.get("h1") is not None and t.get("vol_1h") is None:
            t["vol_1h"] = vol.get("h1")
        return {
            **fields,
            "pair_address": best_pair.get("pairAddress"),  # Store for EVM holder checks
            "dex_twitter": t.get("dex_twitter"),
        }, 'ok'
    except (TypeError, AttributeError, KeyError, ValueError) as e:
        log.warning("DexScreener data parse error for %s (%s): %s", t["ticker"], t["addr"], e)
        return _try_gt_fallback(t, gt_txns_cache)


def _try_gt_fallback(t: dict, gt_txns_cache: dict, genuine_empty: bool = False) -> tuple[dict, str]:
    """Try to use GT transaction data as fallback for DexScreener.
    
    Args:
        t: token dict
        gt_txns_cache: {tid: {"h1": {"buys": N, "sells": N}, ...}} from universe()
        genuine_empty: True if Dex returned empty/null (not error), False for errors
    
    Returns (trade_data, status) where status is 'gt_fallback', 'empty', or 'error'
    """
    tid = t.get("tid")
    gt_txns = gt_txns_cache.get(tid) if tid else None
    
    if gt_txns and gt_txns.get("h24"):
        # GT has data, use it
        fields = _trade_fields_from_txns(gt_txns)
        log.info("Using GT fallback for %s: h24 buys=%s sells=%s (DexScreener %s)",
                 t["ticker"],
                 (gt_txns.get("h24") or {}).get("buys") or 0,
                 (gt_txns.get("h24") or {}).get("sells") or 0,
                 "empty" if genuine_empty else "error")
        t.update({k: fields[k] for k in ("buys_h1", "sells_h1", "buys_h6", "sells_h6",
                                         "buys_m5", "sells_m5")})
        return {
            **fields,
            "pair_address": None  # GT fallback has no pair info
        }, 'gt_fallback'
    
    # No GT fallback available
    if genuine_empty:
        result = dict(_EMPTY_TRADES)
        result["pair_address"] = None
        return result, 'empty'
    else:
        result = dict(_EMPTY_TRADES)
        result["pair_address"] = None
        return result, 'error'


def _normalize_authority(raw_value) -> tuple[bool | None, str | None]:
    """Normalize GT authority field to tri-state: True=open, False=revoked, None=unknown.
    
    Returns (normalized, raw) where:
    - True means authority is set (open/dangerous)
    - False means authority is revoked or explicitly disabled
    - None means unknown/missing data
    - raw is the original value for debugging
    """
    if raw_value is None:
        return (None, None)
    if isinstance(raw_value, bool):
        return (raw_value, str(raw_value))
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        # Revoked/disabled: 'no', 'false', '', 'null', 'none', '0'
        if normalized in ('no', 'false', '', 'null', 'none', '0'):
            return (False, raw_value)
        # Open/set: 'yes', 'true', or any base58 address (non-empty after stripping)
        if normalized in ('yes', 'true') or (normalized and normalized not in ('no', 'false', 'null', 'none', '0')):
            return (True, raw_value)
        return (None, raw_value)
    # Unexpected type
    return (None, str(raw_value))


def _dossier_wait_log(ticker: str, wait_duration: float, limiter: "GTRateLimiter") -> None:
    """Log a dossier wait, distinguishing pacing from a real 429 backoff."""
    reasons = getattr(limiter, "last_wait_reasons", None) or []
    parts = []
    if "429_backoff" in reasons:
        parts.append("429 backoff")
    if "pacing" in reasons:
        parts.append("pacing")
    if "rate_limit" in reasons:
        parts.append("rate limit")
    label = " + ".join(parts) if parts else "pacing"
    log.info("dossier for %s waited %.1fs for GT %s", ticker, wait_duration, label)


def apply_wallet_concentration(t: dict) -> dict:
    """Fill top_wallet / top_10 from Solana RPC or EVM sources. No GT call.

    Uses t['net'] (56 / 8453 / 4663 / 1399811149), never CHAIN_SET's judge-seat
    name, so Base tokens query chain 8453 rather than BSC 56.

    Idempotent within a token dict (young in-cycle retry must not re-hit RPC).
    Mutates and returns t.
    """
    if t.get("_wallet_concentration_done"):
        return t
    t["_wallet_concentration_done"] = True

    # GT_NET is the on-chain name (8453 -> base). CHAIN_SET maps 8453 -> bsc
    # only for the judge question set; do not use it here.
    if t.get("net") in GT_NET:
        t["chain"] = GT_NET[t["net"]]

    if t["net"] == 1399811149:
        # Solana: pool-aware top wallet and top_10, free, off SOLANA_RPC_URL
        try:
            holder_data, rpc_ok, rpc_error = sol_top_wallet(t["addr"])
            t["top_wallet_percent"] = holder_data.get("top_wallet")
            t["pools_excluded"] = holder_data.get("pools_excluded", False)
            rpc_top_10 = holder_data.get("top_10")
            if rpc_top_10 is not None:
                t["top_10_percent"] = rpc_top_10 * 100  # fraction -> percent
            t["rpc_ok"] = rpc_ok
            t["rpc_error"] = rpc_error
            if rpc_error:
                log.warning("solana rpc failed for %s: %s", t["ticker"], rpc_error)
        except Exception as e:
            safe_msg = safe_err(e)
            log.warning("solana rpc failed for %s: %s", t["ticker"], safe_msg)
            t["top_wallet_percent"] = None
            t["pools_excluded"] = False
            t["rpc_ok"] = False
            t["rpc_error"] = safe_msg
        return t

    # EVM: Honeypot / GoPlus / Robinhood fold / Multicall. chain_id is t["net"].
    import evm_holders
    import book

    pair_addrs = []
    if "pair_address" in t:
        pair_addrs = [t["pair_address"]]

    result = evm_holders.evm_holder_concentration(
        chain_id=t["net"],
        token=t["addr"],
        pair_addrs=pair_addrs,
        age_min=t.get("age_minutes", 0),
        db=book.DB
    )

    if result.ok:
        t["top_wallet_percent"] = result.top_wallet  # 0-1 fraction
        t["top_10_percent"] = result.top_10  # 0-100 percent
        t["evm_holder_source"] = result.source
        t["evm_holder_excluded"] = result.excluded
        t["evm_holder_raw_top_wallet"] = result.raw_top_wallet
        t["evm_holder_raw_top_10"] = result.raw_top_10
        if getattr(result, "creator", None) and not t.get("creator"):
            t["creator"] = result.creator
            if result.creator_balance is not None:
                t["creator_balance"] = result.creator_balance
            if result.creator_initial_balance is not None:
                t["creator_initial_balance"] = result.creator_initial_balance

        if result.top_wallet is not None and result.top_wallet > 0.05:
            exclusion_reasons = ", ".join(set(reason for _, _, reason in result.excluded[:5]))
            log.info("EVM top_wallet KILL for %s: raw %.1f%%, post-exclusion %.1f%% (source: %s, excluded: %s)",
                    t["ticker"],
                    (result.raw_top_wallet * 100) if result.raw_top_wallet else 0,
                    result.top_wallet * 100,
                    result.source,
                    exclusion_reasons or "none")
        elif result.source:
            log.info("EVM holder check pass for %s: top_wallet=%s top_10=%s evm_source=%s",
                     t["ticker"], result.top_wallet, result.top_10, result.source)
        if result.top_10 is not None and result.top_10 > 60:
            exclusion_reasons = ", ".join(set(reason for _, _, reason in result.excluded[:5]))
            log.info("EVM top_10 KILL for %s: raw %.1f%%, post-exclusion %.1f%% (source: %s, excluded: %s)",
                    t["ticker"],
                    result.raw_top_10 if result.raw_top_10 else 0,
                    result.top_10,
                    result.source,
                    exclusion_reasons or "none")
    else:
        t["top_wallet_percent"] = None
        t["top_10_percent"] = None
        t["evm_holder_source"] = result.source
        t["evm_holder_error"] = result.error
        t["evm_holder_transient"] = result.is_transient
        log.info("EVM holder check unavailable for %s: %s (source: %s, transient: %s)",
                t["ticker"], result.error, result.source, result.is_transient)
    return t


def _helius_creator(tid: str | None) -> str | None:
    if not tid:
        return None
    try:
        import young_trail_helius
        return young_trail_helius.lookup_creator(tid)
    except Exception as e:
        log.warning("helius creator lookup: %s", safe_err(e))
        return None


def _apply_gt_info_attributes(d: dict, t: dict, net: str, a: dict,
                              *, from_cache: bool = False) -> dict:
    """Merge a GT /info attributes object onto the token. Used for live and cache.

    Cache hits keep static metadata (authority, honeypot, handle) but take
    holder_count from the trade-stage snapshot when it is present, so a 90-min
    /info cache cannot freeze judged holder counts.
    """
    mint_auth_normalized, mint_auth_raw = _normalize_authority(a.get("mint_authority"))
    freeze_auth_normalized, freeze_auth_raw = _normalize_authority(a.get("freeze_authority"))

    holders = a.get("holders") or {}
    # Preserve RPC/EVM concentration — GT must not overwrite a real top_10 / top_wallet.
    preserved_top_wallet = d.get("top_wallet_percent")
    preserved_top_10 = d.get("top_10_percent")
    gt_creator = (a.get("creator") or a.get("creator_address") or a.get("creatorAddress")
                  or a.get("deployer") or a.get("deployer_address") or a.get("createdBy"))
    gt_holders = holders.get("count")
    snap_holders = t.get("holder_count") if t.get("holder_count") is not None else d.get("holder_count")
    if from_cache:
        holder_count = snap_holders if snap_holders is not None else gt_holders
    else:
        holder_count = gt_holders if gt_holders is not None else snap_holders
    d = {**d, "chain": net,
         "holder_count": holder_count,
         "top_10_percent": (holders.get("distribution_percentage") or {}).get("top_10"),
         "developer_holding_percentage": a.get("developer_holding_percentage"),
         "gt_score_details": a.get("gt_score_details"),
         "is_honeypot": a.get("is_honeypot"),
         "mint_authority": mint_auth_normalized,
         "mint_authority_raw": mint_auth_raw,
         "freeze_authority": freeze_auth_normalized,
         "freeze_authority_raw": freeze_auth_raw,
         "description": a.get("description"),
         "x_handle": clean_handle(a.get("twitter_handle")),
         "creator": gt_creator or t.get("creator") or d.get("creator") or _helius_creator(t.get("tid") or d.get("tid"))}
    if preserved_top_wallet is not None:
        d["top_wallet_percent"] = preserved_top_wallet
    if preserved_top_10 is not None:
        d["top_10_percent"] = preserved_top_10
    fill_x_handle(d)
    return d


def dossier(t: dict, limiter: GTRateLimiter | None = None, deadline: float | None = None) -> dict:
    """Wallet-concentration first, then cached or live GT /info if that does not kill.

    Cheap Solana RPC / EVM holder checks run before any GT spend so a top_wallet
    (or EVM unverified / pending) kill does not burn a rate-limit slot.
    authority and honeypot still come from GT /info (or its cache). holder_count
    on a cache hit prefers the trade-stage snapshot so judged counts are not frozen.

    A fresh cache hit skips the GT call and the limiter spend. Entries older than
    GT_INFO_CACHE_TTL_MIN are treated as a miss so filter fields are not stale.

    If limiter is provided and budget is exhausted, raises DossierRetryNeeded.
    If waiting for a slot would cross deadline, raises DossierDeadlineExceeded.
    On GT 429, records the backoff with the limiter and raises DossierRetryNeeded.
    Only HTTP 200 responses with holder_count are written to the cache."""
    from filter import concentration_kill

    d = apply_wallet_concentration(t)
    if concentration_kill(d):
        return d

    net = GT_NET[t["net"]]
    cached = gt_info_cache.get(net, t["addr"])
    if cached is not None:
        _gt_info_cycle["hits"] += 1
        return _apply_gt_info_attributes(d, t, net, cached, from_cache=True)

    _gt_info_cycle["misses"] += 1

    if limiter:
        # Wait for any 429 backoff BEFORE spending a slot (order matters!)
        wait_start = limiter.time_fn()
        if deadline is None:
            deadline = getattr(limiter, "cycle_deadline", None)
        if not limiter.wait_if_needed(priority=True, deadline=deadline):
            raise DossierDeadlineExceeded(f"GT deadline exceeded for {t['ticker']}")
        wait_duration = limiter.time_fn() - wait_start
        if wait_duration >= 0.1:
            _dossier_wait_log(t["ticker"], wait_duration, limiter)
        if not limiter.spend(1, priority=True):
            log.warning("GT budget exhausted, dossier for %s cannot run this cycle", t["ticker"])
            raise DossierRetryNeeded(f"GT budget exhausted for {t['ticker']}")

    _gt_info_cycle["attempts"] += 1
    info_url = gt_api.prefer_onchain_url(
        f"{GT}/networks/{net}/tokens/{t['addr']}/info", purpose="desk",
    )
    resp = requests.get(info_url, headers=gt_api.request_headers(info_url, purpose="desk"), timeout=20)
    if resp.status_code in (401, 403):
        free = gt_api.fallback_free_url(info_url)
        if free:
            gt_api.note_key_failed()
            log.info("coingecko HTTP %s on /info, falling back to free GT", resp.status_code)
            resp = requests.get(free, headers=gt_api.UA, timeout=20)
    if resp.status_code == 429:
        # Extract Retry-After header if present
        retry_after_sec = None
        if hasattr(resp, 'headers') and resp.headers:
            retry_after = resp.headers.get("Retry-After")
            if retry_after and isinstance(retry_after, (str, int, float)):
                try:
                    retry_after_sec = float(retry_after)
                except (ValueError, TypeError):
                    retry_after_sec = None

        # Record the 429 with the limiter so it enforces backoff
        if limiter:
            limiter.record_429(retry_after_sec)

        log.warning("GeckoTerminal 429 on dossier for %s (Retry-After: %s), will retry next cycle",
                    t["ticker"], retry_after_sec if retry_after_sec is not None else "missing/0")
        raise DossierRetryNeeded(f"GT 429 for {t['ticker']}")

    if limiter:
        limiter.record_success()  # clear saturated flag on success

    a = resp.json()["data"]["attributes"]
    if not gt_info_cache.is_attributes_object(a):
        raise ValueError("GT /info attributes missing")

    # Only 200 + complete holder data is cached. Incomplete holders (common
    # on young tokens) and non-200 bodies are applied live but not stored.
    if resp.status_code == 200:
        gt_info_cache.put(net, t["addr"], a, status_code=resp.status_code)
    _gt_info_cycle["ok"] += 1
    return _apply_gt_info_attributes(d, t, net, a)


def clean_handle(h):
    """GT returned 'LuffyX100X/status/2102659581109272876' on a Robinhood token.
       Take the first path segment, or treat the account as missing."""
    if not h:
        return None
    h = h.strip().lstrip("@").split("?")[0].split("/")[0]
    return h if h and h.replace("_", "").isalnum() and len(h) <= 15 else None


def _dex_twitter_from_pair(pair: dict):
    """DexScreener pair info.socials[type=twitter] url or handle, else None."""
    if not isinstance(pair, dict):
        return None
    info = pair.get("info") or {}
    if not isinstance(info, dict):
        return None
    socials = info.get("socials") or []
    if not isinstance(socials, list):
        return None
    for s in socials:
        if not isinstance(s, dict):
            continue
        if str(s.get("type") or "").lower() == "twitter":
            return s.get("url") or s.get("handle") or s.get("value")
    return None


def fill_x_handle(d: dict) -> dict:
    """GT handle first; else DexScreener pair social; else FOMO twitter field."""
    if d.get("x_handle"):
        return d
    for raw in (d.get("dex_twitter"), d.get("fomo_twitter")):
        h = x_lookup.normalize_handle(raw)
        if h:
            d["x_handle"] = h
            return d
    d["x_handle"] = None
    return d


# Known AMM/curve programs whose PDAs should be excluded from holder concentration checks
# These are pool vaults, not real individual holders
AMM_PROGRAMS = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",  # Meteora DLMM
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora DAMM v1
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",  # Meteora DAMM v2
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpool
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",  # Raydium LaunchLab
}

# Solana incinerator/burn address (also excluded)
INCINERATOR_ADDRESS = "1nc1nerator11111111111111111111111111111111"

# Cache for owner lookups - module-level, persists across calls within same cycle
_sol_owner_cache = {}


def _clear_sol_owner_cache():
    """Clear the owner cache. Called at the start of each cycle."""
    global _sol_owner_cache
    _sol_owner_cache = {}


def sol_top_wallet(mint: str) -> tuple[dict, bool, str | None]:
    """Query Solana RPC for top wallet concentration, excluding AMM pool vaults.
    
    Returns (holder_data, rpc_ok, rpc_error):
      - holder_data: {"top_wallet": float|None, "top_10": float|None, "pools_excluded": bool}
      - rpc_ok: True on success, False on failure
      - rpc_error: error description or None
    
    Pool-aware: excludes token accounts owned by known AMM programs (PumpSwap, Meteora, 
    Raydium, Orca, pump.fun) and the incinerator address. Returns top_wallet as the 
    largest non-pool account / supply, and top_10 as sum of top 10 non-pool accounts / supply.
    """
    rpc_url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    
    def q(m, p, retry_on_429=False):
        """Make one RPC call. Returns (result, error_description)."""
        try:
            resp = requests.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p},
                timeout=20
            )
            data = resp.json()
            
            if "error" in data:
                error_msg = data["error"]
                if isinstance(error_msg, dict):
                    code = error_msg.get("code")
                    message = error_msg.get("message", "unknown error")
                    error_desc = f"{message} (code {code})" if code else message
                else:
                    error_desc = str(error_msg)
                
                if resp.status_code == 429 and retry_on_429:
                    log.info("Solana RPC 429 on %s for %s, retrying once", m, mint)
                    time.sleep(2)
                    resp_retry = requests.post(
                        rpc_url,
                        json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p},
                        timeout=20
                    )
                    data_retry = resp_retry.json()
                    if "error" in data_retry:
                        return None, error_desc
                    if "result" not in data_retry:
                        return None, "missing result field"
                    return data_retry["result"], None
                
                return None, error_desc
            
            if "result" not in data:
                return None, "missing result field"
            
            return data["result"], None
            
        except Exception as e:
            return None, safe_err(e)
    
    # Get token supply
    supply_result, supply_error = q("getTokenSupply", [mint])
    if supply_error:
        return {"top_wallet": None, "top_10": None, "pools_excluded": False}, False, f"getTokenSupply: {supply_error}"
    
    try:
        supply = float(supply_result["value"]["amount"])
    except (TypeError, KeyError, ValueError) as e:
        return {"top_wallet": None, "top_10": None, "pools_excluded": False}, False, f"getTokenSupply parse: {e}"
    
    # Get largest token accounts (top 20 to have buffer after filtering pools)
    top_result, top_error = q("getTokenLargestAccounts", [mint], retry_on_429=True)
    if top_error:
        return {"top_wallet": None, "top_10": None, "pools_excluded": False}, False, f"getTokenLargestAccounts: {top_error}"
    
    try:
        top_accounts = top_result["value"]
        if not supply or not top_accounts:
            return {"top_wallet": None, "top_10": None, "pools_excluded": False}, True, None
    except (TypeError, KeyError, ValueError) as e:
        return {"top_wallet": None, "top_10": None, "pools_excluded": False}, False, f"getTokenLargestAccounts parse: {e}"
    
    # Resolve token account owners to filter out pool vaults
    # Batch RPC call to get owner info for all token accounts
    token_account_addrs = [acc["address"] for acc in top_accounts]
    
    # Check cache first, collect uncached addresses
    uncached_addrs = [addr for addr in token_account_addrs if addr not in _sol_owner_cache]
    
    # Bound RPC calls: limit to top 15 token accounts (enough to find real top 10 after filtering)
    if len(uncached_addrs) > 15:
        uncached_addrs = uncached_addrs[:15]
    
    # Fetch uncached owner data
    if uncached_addrs:
        # getMultipleAccounts with jsonParsed encoding to get owner field
        accounts_result, accounts_error = q("getMultipleAccounts", [
            uncached_addrs,
            {"encoding": "jsonParsed"}
        ])
        
        if accounts_error:
            # RPC failure - fall back to current behavior (no filtering)
            log.warning("Solana RPC getMultipleAccounts failed for %s, using unfiltered top wallet: %s", 
                        mint, accounts_error)
            return {"top_wallet": float(top_accounts[0]["amount"]) / supply, 
                    "top_10": None, 
                    "pools_excluded": False}, True, None
        
        try:
            accounts_data = accounts_result["value"]
            for i, acc_data in enumerate(accounts_data):
                if acc_data is None:
                    continue
                token_addr = uncached_addrs[i]
                parsed = acc_data.get("data", {})
                if isinstance(parsed, dict) and "parsed" in parsed:
                    owner = parsed["parsed"].get("info", {}).get("owner")
                    if owner:
                        _sol_owner_cache[token_addr] = owner
        except Exception as e:
            log.warning("Failed to parse getMultipleAccounts response for %s: %s", mint, safe_err(e))
            # Fall back to unfiltered
            return {"top_wallet": float(top_accounts[0]["amount"]) / supply, 
                    "top_10": None, 
                    "pools_excluded": False}, True, None
    
    # Now resolve owner programs for cached owners (batch lookup)
    unique_owners = set(_sol_owner_cache.get(addr) for addr in token_account_addrs if addr in _sol_owner_cache)
    # Check if we already cached the owner's program (stored as "program:{owner}")
    uncached_owners = [owner for owner in unique_owners if f"program:{owner}" not in _sol_owner_cache]
    
    # Bound owner program lookups
    if len(uncached_owners) > 15:
        uncached_owners = uncached_owners[:15]
    
    # Fetch owner programs
    if uncached_owners:
        owner_accounts_result, owner_accounts_error = q("getMultipleAccounts", [uncached_owners])
        
        if not owner_accounts_error:
            try:
                owner_accounts_data = owner_accounts_result["value"]
                for i, owner_acc_data in enumerate(owner_accounts_data):
                    if owner_acc_data is None:
                        continue
                    owner_addr = uncached_owners[i]
                    program_owner = owner_acc_data.get("owner")
                    if program_owner:
                        # Cache the owner's program (store with special key)
                        _sol_owner_cache[f"program:{owner_addr}"] = program_owner
            except Exception as e:
                log.warning("Failed to parse owner programs for %s: %s", mint, safe_err(e))
    
    # Filter out pool vaults and incinerator
    non_pool_accounts = []
    pools_found = False
    
    for acc in top_accounts:
        token_addr = acc["address"]
        owner = _sol_owner_cache.get(token_addr)
        
        if not owner:
            # Owner not resolved - include it (fail open for RPC issues)
            non_pool_accounts.append(acc)
            continue
        
        # Check if owner is incinerator
        if owner == INCINERATOR_ADDRESS:
            pools_found = True
            continue
        
        # Check if owner's program is a known AMM
        owner_program = _sol_owner_cache.get(f"program:{owner}")
        if owner_program and owner_program in AMM_PROGRAMS:
            pools_found = True
            continue
        
        non_pool_accounts.append(acc)
    
    # Compute top_wallet and top_10 from non-pool accounts
    if not non_pool_accounts:
        # All top accounts were pools - real holders are all smaller than smallest fetched pool
        # Return 0.0 to indicate all large holders are pools (passes threshold check)
        return {"top_wallet": 0.0, "top_10": 0.0, "pools_excluded": True}, True, None
    
    top_wallet = float(non_pool_accounts[0]["amount"]) / supply
    
    # Compute top_10: sum of top 10 non-pool accounts
    top_10_accounts = non_pool_accounts[:10]
    top_10_sum = sum(float(acc["amount"]) for acc in top_10_accounts)
    top_10 = top_10_sum / supply if supply else None
    
    return {"top_wallet": top_wallet, "top_10": top_10, "pools_excluded": pools_found}, True, None


def social_state(d: dict) -> dict:
    """What SOCIAL hands the judge. The X block is filled by the bot's X plugin."""
    return {"x_account": d["x_account"],                 # collected by SOCIAL, not here
            "token": {"ticker": d["ticker"], "narrative": d.get("description")}}
