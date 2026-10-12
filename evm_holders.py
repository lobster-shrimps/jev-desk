"""
EVM holder concentration module for BSC (56), Robinhood (4663), and Base (8453).

Computes real (non-pool) top_wallet and top_10 percentages from free keyless sources:
- Honeypot.is TopHolders API for BSC and Base
- Robinhood Transfer-log fold over public RPC
- GoPlus as secondary fallback for all chains

Units:
- top_wallet: 0-1 fraction (compared to 0.05 in filter.py)
- top_10: 0-100 percent (divided by 100 in filter.py)
"""
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from collections import defaultdict
from typing import Any
from datetime import datetime

import requests
from eth_abi import encode, decode

from secret_utils import safe_err


log = logging.getLogger("evm_holders")


# Result cache duration (10 minutes)
CACHE_DURATION = 600

# Per-token time budget (20 seconds) - hard deadline, never sleep/block beyond this
TOKEN_TIMEOUT = 20

# Max RPC calls per Robinhood fold
MAX_RPC_CALLS_PER_FOLD = 40

# Burn addresses
BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0xdead000000000000000042069420694206942069",
}

# Shared pool contracts (lowercase)
POOL_CONTRACTS = {
    # BSC Uniswap v4 PoolManager
    "0x28e2ea090877bf75740558f6bfb36a5ffee9e9df",
    # BSC PancakeSwap Infinity Vault
    "0x238a358808379702088667322f80ac48bad5e6c4",
    # Robinhood Uniswap v4 PoolManager
    "0x8366a39cc670b4001a1121b8f6a443a643e40951",
}

# Launchpad curve contracts (lowercase)
LAUNCHPAD_CURVES = {
    # Flap Portal BSC
    "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0",
    # Flap Portal Robinhood
    "0x26605f322f7ff986f381bb9a6e3f5dab0beaeb09",
    # four.meme TokenManager2
    "0x5c952063c7fc8610ffdb798152d69f0b9550762b",
}

# Permanent lockers (lowercase)
PERMANENT_LOCKERS = {
    # RobinFunFi V2 LaunchLocker
    "0x267444d099b10fb5ed7c3cc7b7c767adca574952",
}

# Time-based lockers that need GoPlus check (lowercase)
CONDITIONAL_LOCKERS = {
    # PinkLock02
    "0x407993575c91ce7643a4d4ccacc9a98c36ee1bbe",
}

# Multicall3 address (same on all EVM chains)
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"

# Rate limiter state
_rate_limiters = {}


@dataclass
class HolderResult:
    """Result of holder concentration analysis."""
    top_wallet: float | None  # 0-1 fraction
    top_10: float | None  # 0-100 percent
    source: str  # 'honeypot', 'rpc_fold', 'goplus', 'cache', 'unavailable'
    excluded: list[tuple[str, float, str]]  # [(address, pct, reason), ...]
    ok: bool  # False if result is unavailable
    error: str | None  # Error code if not ok
    is_transient: bool = False  # True if error is transient (holders_pending vs top_wallet_unverified)
    # Raw values before exclusions (for logging)
    raw_top_wallet: float | None = None  # 0-1 fraction
    raw_top_10: float | None = None  # 0-100 percent
    # Present only when Honeypot.is holder data already named the creator.
    creator: str | None = None
    creator_balance: int | None = None
    creator_initial_balance: int | None = None


class RateLimiter:
    """Simple rate limiter with 429 backoff."""
    
    def __init__(self, max_per_sec: float):
        self.max_per_sec = max_per_sec
        self.min_interval = 1.0 / max_per_sec
        self.last_call = 0.0
        self.backoff_until = 0.0
    
    def is_in_backoff(self) -> bool:
        """Check if currently in 429 backoff without sleeping."""
        return time.time() < self.backoff_until
    
    def wait(self, deadline: float | None = None):
        """Wait if needed to respect rate limit and backoff.
        
        If deadline is provided and we'd exceed it, raises TimeoutError.
        """
        now = time.time()
        
        # Check deadline first
        if deadline and now >= deadline:
            raise TimeoutError("deadline_exceeded")
        
        # Wait for 429 backoff
        if self.backoff_until > now:
            sleep_time = self.backoff_until - now
            if deadline and now + sleep_time > deadline:
                raise TimeoutError("deadline_exceeded")
            time.sleep(sleep_time)
            now = time.time()
        
        # Wait for rate limit
        elapsed = now - self.last_call
        if elapsed < self.min_interval:
            sleep_time = self.min_interval - elapsed
            if deadline and now + sleep_time > deadline:
                raise TimeoutError("deadline_exceeded")
            time.sleep(sleep_time)
        
        self.last_call = time.time()
    
    def record_429(self, retry_after: int | None = None):
        """Record 429 and set backoff."""
        backoff = retry_after if retry_after else 60
        self.backoff_until = time.time() + backoff
        # Next callers see is_in_backoff() and return rate_limited immediately.
        # No sleep here (TOKEN_TIMEOUT is 20s; a 60s sleep would blow the budget).
        log.warning("429 received, skipping source for %ds (no sleep)", backoff)


def _get_limiter(name: str, rate: float) -> RateLimiter:
    """Get or create rate limiter."""
    if name not in _rate_limiters:
        _rate_limiters[name] = RateLimiter(rate)
    return _rate_limiters[name]


def _classify_error(error: str) -> bool:
    """
    Classify error as transient (True) or definitive (False).
    
    Transient (15m holders_pending): rate_limited, timeout, call_limit_exceeded (with progress),
    connection errors, 5xx errors
    
    Definitive (6h top_wallet_unverified): Invalid chain, 400, zero supply, all sources empty,
    mint_not_found, fold reached head but != totalSupply
    """
    if not error:
        return False
    
    # Transient errors
    transient_keywords = [
        "rate_limited",
        "timeout",
        "deadline",
        "connection",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "http_520",
        "http_521",
        "http_522",
        "http_523",
        "http_524",
    ]
    
    for keyword in transient_keywords:
        if keyword in error.lower():
            return True
    
    # call_limit_exceeded is transient if we have progress (handled by caller)
    if "call_limit_exceeded" in error:
        return True
    
    # Definitive errors
    definitive_keywords = [
        "bad_request",
        "http_400",
        "http_401",
        "http_403",
        "http_404",
        "unsupported_chain",
        "zero_supply",
        "mint_not_found",
        "no_data",
        "all_excluded",
        "zero_supply_after_burns",
        "incomplete_at_head",  # Reached head but != totalSupply
    ]
    
    for keyword in definitive_keywords:
        if keyword in error.lower():
            return False
    
    # Default: treat as transient
    return True


def _creator_from_honeypot(data: dict) -> tuple[str | None, int | None, int | None]:
    """Creator address if the TopHolders payload already carries it. No extra calls."""
    if not isinstance(data, dict):
        return None, None, None
    creator = (data.get("creator") or data.get("creatorAddress")
               or data.get("creator_address") or data.get("deployer")
               or data.get("deployerAddress") or data.get("createdBy"))
    if isinstance(creator, str):
        creator = creator.strip() or None
    else:
        creator = None
    initial = data.get("creatorInitialBalance") or data.get("creator_initial_balance")
    try:
        initial = int(initial) if initial is not None else None
    except (TypeError, ValueError):
        initial = None
    balance = None
    for h in data.get("holders") or []:
        if not isinstance(h, dict):
            continue
        if not creator and (h.get("isCreator") or h.get("isDeployer") or h.get("isOwner")):
            creator = (h.get("address") or "").strip() or None
        if creator and str(h.get("address") or "").lower() == creator.lower():
            try:
                balance = int(h.get("balance"))
            except (TypeError, ValueError):
                balance = None
            break
    return creator, balance, initial


def _holders_honeypot(chain_id: int, token: str, deadline: float) -> tuple[dict | None, str | None]:
    """
    Fetch holder data from Honeypot.is API.
    
    Returns (data, error) where data is {"totalSupply": int, "holders": [{address, balance, isContract}, ...]}.
    Returns (None, error_msg) on failure.
    """
    limiter = _get_limiter("honeypot", 1.0)  # 1 req/s
    
    # Check if in backoff - return immediately without sleeping
    if limiter.is_in_backoff():
        return None, "rate_limited"
    
    try:
        limiter.wait(deadline)
    except TimeoutError:
        return None, "timeout"
    
    url = "https://api.honeypot.is/v1/TopHolders"
    params = {"address": token, "chainID": chain_id}
    
    try:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None, "timeout"
        
        resp = requests.get(url, params=params, timeout=min(10, remaining))
        
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            limiter.record_429(int(retry_after) if retry_after else None)
            return None, "rate_limited"
        
        if resp.status_code == 400:
            return None, "bad_request"
        
        if resp.status_code >= 500:
            return None, f"http_{resp.status_code}"
        
        if resp.status_code != 200:
            return None, f"http_{resp.status_code}"
        
        data = resp.json()
        
        # Validate response
        if "totalSupply" not in data or "holders" not in data:
            return None, "malformed_response"
        
        total_supply = data.get("totalSupply")
        
        # Handle string supply
        if isinstance(total_supply, str):
            if total_supply == "0":
                return None, "zero_supply"
            try:
                total_supply = int(total_supply)
            except (ValueError, TypeError):
                return None, "invalid_supply"
        
        if total_supply == 0 or total_supply is None:
            return None, "zero_supply"
        
        # Update data with parsed supply
        data["totalSupply"] = total_supply
        
        holders = data.get("holders", [])
        if not holders:
            return None, "no_holders"
        
        # Validate and parse holder balances (item #9: unparseable -> fail closed)
        for holder in holders:
            balance = holder.get("balance")
            if balance is None:
                return None, "unparseable_balance"
            
            # Try to parse balance
            try:
                if isinstance(balance, str):
                    holder["balance"] = int(balance)
                elif not isinstance(balance, int):
                    return None, "unparseable_balance"
            except (ValueError, TypeError):
                return None, "unparseable_balance"
        
        return data, None
        
    except requests.Timeout:
        return None, "timeout"
    except requests.ConnectionError:
        return None, "connection_error"
    except Exception as e:
        return None, safe_err(e)


def _holders_rpc_fold(token: str, deadline: float, db: sqlite3.Connection, call_count: list[int]) -> tuple[dict | None, str | None]:
    """
    Fold Transfer logs on Robinhood RPC to compute holder balances.
    
    Returns (data, error) where data is {"balances": {address: int}, "supply": int, "complete": bool}.
    Uses incremental cache from evm_holder_cache table.
    
    call_count is a mutable list[int] tracking RPC calls for cap enforcement.
    """
    rpc_url = os.environ.get("ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
    limiter = _get_limiter("robinhood_rpc", 5.0)  # 5 req/s
    
    token_lower = token.lower()
    
    def rpc_call(method: str, params: list) -> tuple[dict | None, str | None]:
        """Make one RPC call with rate limiting and deadline."""
        if call_count[0] >= MAX_RPC_CALLS_PER_FOLD:
            return None, "call_limit_exceeded"
        
        if time.time() >= deadline:
            return None, "timeout"
        
        # Check if in backoff - return immediately
        if limiter.is_in_backoff():
            return None, "rate_limited"
        
        try:
            limiter.wait(deadline)
        except TimeoutError:
            return None, "timeout"
        
        call_count[0] += 1
        
        try:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None, "timeout"
            
            resp = requests.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=min(10, remaining)
            )
            
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                limiter.record_429(int(retry_after) if retry_after else None)
                return None, "rate_limited"
            
            if resp.status_code != 200:
                return None, f"http_{resp.status_code}"
            
            data = resp.json()
            
            if "error" in data:
                error = data["error"]
                if isinstance(error, dict):
                    msg = error.get("message", "rpc_error")
                    return None, safe_err(Exception(msg))
                return None, safe_err(Exception(str(error)))
            
            return data.get("result"), None
            
        except requests.Timeout:
            return None, "timeout"
        except requests.ConnectionError:
            return None, "connection_error"
        except Exception as e:
            return None, safe_err(e)
    
    # Get current block number
    block_result, error = rpc_call("eth_blockNumber", [])
    if error:
        return None, f"eth_blockNumber: {error}"
    
    try:
        current_block = int(block_result, 16)
    except (ValueError, TypeError):
        return None, "invalid_block_number"
    
    # Use blocks up to current - 20 for safety
    head_block = current_block - 20
    
    # Check cache
    cached = db.execute(
        "SELECT last_block, balances_json, supply, updated_at FROM evm_holder_cache "
        "WHERE chain_id=? AND token=?",
        (4663, token_lower)
    ).fetchone()
    
    if cached:
        last_block, balances_json, cached_supply, updated_at = cached
        balances = json.loads(balances_json) if balances_json else {}
        start_block = last_block + 1  # Resume from next block
        
        # Item #8: Cache-at-head path must check totalSupply
        if start_block > head_block:
            # No new blocks, but still need to verify completeness
            supply_result, error = rpc_call("eth_call", [
                {"to": token, "data": "0x18160ddd"},  # totalSupply()
                "latest"
            ])
            
            # Item 1 (Review 4): Fail safe when totalSupply can't be read
            if error:
                # RPC error, rate limit, etc -> transient
                return None, "totalSupply_unavailable"
            
            if not supply_result:
                # Empty response -> transient
                return None, "totalSupply_unavailable"
            
            try:
                actual_supply = int(supply_result, 16)
            except (ValueError, TypeError):
                # Parse failure -> transient
                return None, "totalSupply_unavailable"
            
            # Check for truly zero supply (definitive error)
            if actual_supply == 0:
                return None, "zero_supply"
            
            # Check completeness against cached balance sum
            total_balance = sum(balances.values())
            diff = abs(total_balance - actual_supply)
            complete = diff <= actual_supply * 1e-6
            
            if not complete:
                # Item #10: Cache-at-head with mismatch is definitive (6h)
                return None, "incomplete_at_head"
            
            return {
                "balances": balances,
                "supply": actual_supply,
                "complete": True
            }, None
        
        log.info("Robinhood fold for %s: resuming from block %d", token, last_block)
    else:
        balances = {}
        start_block = None
        log.info("Robinhood fold for %s: starting from mint", token)
    
    # Find mint block if not cached
    if start_block is None:
        search_start = max(1, head_block - 9_999_000)
        
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        zero_addr_padded = "0x" + "0" * 64
        
        # Try to find first mint in one query
        logs_result, error = rpc_call("eth_getLogs", [{
            "fromBlock": hex(search_start),
            "toBlock": hex(head_block),
            "address": token,
            "topics": [transfer_topic, zero_addr_padded]
        }])
        
        mint_block = None
        
        # Only treat specific "exceeds limit of 10000" message as a limit error
        if error and "exceeds limit of 10000" in error.lower():
            # Fall back to chunking
            log.info("Robinhood fold: mint search needs chunking")
            chunk_size = 500_000
            for chunk_start in range(search_start, head_block, chunk_size):
                chunk_end = min(chunk_start + chunk_size - 1, head_block)
                
                logs_result, error = rpc_call("eth_getLogs", [{
                    "fromBlock": hex(chunk_start),
                    "toBlock": hex(chunk_end),
                    "address": token,
                    "topics": [transfer_topic, zero_addr_padded]
                }])
                
                if error:
                    if "exceeds limit of 10000" in error.lower():
                        continue
                    return None, f"getLogs mint search: {error}"
                
                if logs_result:
                    try:
                        mint_block = int(logs_result[0]["blockNumber"], 16)
                        log.info("Robinhood fold: found mint at block %d", mint_block)
                        break
                    except (KeyError, ValueError, IndexError):
                        pass
        elif error:
            return None, f"getLogs mint search: {error}"
        elif logs_result:
            try:
                mint_block = int(logs_result[0]["blockNumber"], 16)
                log.info("Robinhood fold: found mint at block %d", mint_block)
            except (KeyError, ValueError, IndexError):
                pass
        
        if mint_block is None:
            return None, "mint_not_found"
        
        start_block = mint_block
    
    # Fetch Transfer logs incrementally with adaptive window
    window_size = 250_000
    current = start_block
    
    while current <= head_block:
        if time.time() >= deadline:
            # Persist partial progress before timeout
            db.execute(
                "INSERT OR REPLACE INTO evm_holder_cache (chain_id, token, last_block, balances_json, supply, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (4663, token_lower, current - 1, json.dumps(balances), str(sum(balances.values())), time.time())
            )
            db.commit()
            return None, "timeout"
        
        to_block = min(current + window_size - 1, head_block)
        
        logs_result, error = rpc_call("eth_getLogs", [{
            "fromBlock": hex(current),
            "toBlock": hex(to_block),
            "address": token,
            "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"]
        }])
        
        if error:
            # Only halve on exact "exceeds limit" message
            if "exceeds limit of 10000" in error.lower():
                window_size = max(10_000, window_size // 2)
                log.info("Robinhood fold: halving window to %d blocks", window_size)
                continue
            
            # Other errors: persist partial progress and fail
            db.execute(
                "INSERT OR REPLACE INTO evm_holder_cache (chain_id, token, last_block, balances_json, supply, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (4663, token_lower, current - 1, json.dumps(balances), str(sum(balances.values())), time.time())
            )
            db.commit()
            return None, f"eth_getLogs: {error}"
        
        # Process logs
        if logs_result:
            for log_entry in logs_result:
                try:
                    topics = log_entry["topics"]
                    data = log_entry["data"]
                    
                    from_addr = "0x" + topics[1][-40:] if len(topics) > 1 else None
                    to_addr = "0x" + topics[2][-40:] if len(topics) > 2 else None
                    value = int(data, 16) if data and data != "0x" else 0
                    
                    # Don't credit 0x0 (burn destination)
                    if from_addr and from_addr.lower() != "0x0000000000000000000000000000000000000000":
                        from_addr = from_addr.lower()
                        balances[from_addr] = balances.get(from_addr, 0) - value
                        if balances[from_addr] <= 0:
                            balances.pop(from_addr, None)
                    
                    if to_addr and to_addr.lower() != "0x0000000000000000000000000000000000000000":
                        to_addr = to_addr.lower()
                        balances[to_addr] = balances.get(to_addr, 0) + value
                        if balances[to_addr] <= 0:
                            balances.pop(to_addr, None)
                    
                except (KeyError, ValueError, IndexError) as e:
                    log.debug("Failed to parse log: %s", e)
                    continue
        
        # Double window if we got few logs
        if logs_result is not None and len(logs_result) < 4000 and window_size < 1_000_000:
            window_size = min(1_000_000, window_size * 2)
        
        # Only update current AFTER successful processing
        current = to_block + 1
    
    # Update cache with final state
    total_supply = sum(balances.values())
    
    db.execute(
        "INSERT OR REPLACE INTO evm_holder_cache (chain_id, token, last_block, balances_json, supply, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (4663, token_lower, head_block, json.dumps(balances), str(total_supply), time.time())
    )
    db.commit()
    
    # Get actual supply from token contract
    supply_result, error = rpc_call("eth_call", [
        {"to": token, "data": "0x18160ddd"},
        "latest"
    ])
    
    actual_supply = 0
    if supply_result:
        try:
            actual_supply = int(supply_result, 16)
        except ValueError:
            pass
    
    # Item #7: Check completeness
    # If fold reached head but != totalSupply, it's DEFINITIVE (not transient)
    complete = False
    if actual_supply > 0:
        diff = abs(total_supply - actual_supply)
        complete = diff <= actual_supply * 1e-6
        
        # Item #7: If reached head but incomplete, it's definitive
        if not complete:
            return None, "incomplete_at_head"
    
    return {
        "balances": balances,
        "supply": actual_supply if actual_supply > 0 else total_supply,
        "complete": complete
    }, None


def _holders_goplus(chain_id: int, token: str, deadline: float) -> tuple[dict | None, str | None]:
    """
    Fetch holder data from GoPlus API.
    
    Returns (data, error) where data includes holders list and lock info.
    """
    chain_map = {56: "56", 8453: "8453", 4663: "4663", 143: "143"}
    chain_str = chain_map.get(chain_id)
    
    if not chain_str:
        return None, "unsupported_chain"
    
    limiter = _get_limiter("goplus", 150/60)  # 150 CU/min
    
    if limiter.is_in_backoff():
        return None, "rate_limited"
    
    try:
        limiter.wait(deadline)
    except TimeoutError:
        return None, "timeout"
    
    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_str}"
    params = {"contract_addresses": token}
    
    try:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None, "timeout"
        
        resp = requests.get(url, params=params, timeout=min(10, remaining))
        
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            limiter.record_429(int(retry_after) if retry_after else None)
            return None, "rate_limited"
        
        if resp.status_code >= 500:
            return None, f"http_{resp.status_code}"
        
        if resp.status_code != 200:
            return None, f"http_{resp.status_code}"
        
        data = resp.json()
        
        result = data.get("result", {})
        token_lower = token.lower()
        token_data = result.get(token_lower)
        
        if not token_data:
            return None, "no_data"
        
        return token_data, None
        
    except requests.Timeout:
        return None, "timeout"
    except requests.ConnectionError:
        return None, "connection_error"
    except Exception as e:
        return None, safe_err(e)


def _check_pools_via_multicall(holders: list[tuple[str, int, float]], token: str, chain_id: int, deadline: float) -> list[str]:
    """
    Check if holders are pools by calling token0()/token1() via Multicall3.
    
    Item #1: Use eth_abi for encoding/decoding.
    Item #2: Run on top-20 by balance regardless of is_contract.
    
    Returns list of addresses that are pools (return our token).
    """
    if time.time() >= deadline:
        return []
    
    # Get RPC URL
    if chain_id == 56:
        rpc_url = os.environ.get("BSC_RPC_URL", "https://bsc-dataseed.binance.org")
    elif chain_id == 4663:
        rpc_url = os.environ.get("ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
    elif chain_id == 8453:
        rpc_url = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
    else:
        return []
    
    # Build calls for top-20 holders by balance (item #2: regardless of is_contract)
    addresses = [h[0] for h in holders[:20]]
    
    calls = []
    for addr in addresses:
        # token0() and token1()
        calls.append((addr, True, bytes.fromhex("0dfe1681")))  # token0()
        calls.append((addr, True, bytes.fromhex("d21220a7")))  # token1()
    
    # Encode using eth_abi (item #1)
    try:
        # aggregate3((address,bool,bytes)[]) signature
        call_data = "0x82ad56cb" + encode(
            ['(address,bool,bytes)[]'],
            [calls]
        ).hex()
    except Exception as e:
        log.warning("Multicall3 encode failed: %s", safe_err(e))
        return []
    
    # Make the call
    try:
        remaining = deadline - time.time()
        if remaining <= 0:
            return []
        
        resp = requests.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [
                {"to": MULTICALL3, "data": call_data},
                "latest"
            ]},
            timeout=min(10, remaining)
        )
        
        if resp.status_code != 200:
            log.warning("Multicall3 call HTTP %d", resp.status_code)
            return []
        
        result = resp.json().get("result")
        if not result:
            return []
        
        # Decode using eth_abi (item #1)
        decoded = decode(['(bool,bytes)[]'], bytes.fromhex(result[2:]))[0]
        
        pools = []
        token_lower = token.lower()
        
        # Check each address (2 calls per address: token0, token1)
        for i, addr in enumerate(addresses):
            token0_idx = i * 2
            token1_idx = i * 2 + 1
            
            if token0_idx < len(decoded):
                success0, data0 = decoded[token0_idx]
                if success0 and len(data0) == 32:
                    token_addr = "0x" + data0[-20:].hex()
                    if token_addr.lower() == token_lower:
                        pools.append(addr)
                        continue
            
            if token1_idx < len(decoded):
                success1, data1 = decoded[token1_idx]
                if success1 and len(data1) == 32:
                    token_addr = "0x" + data1[-20:].hex()
                    if token_addr.lower() == token_lower:
                        pools.append(addr)
        
        return pools
        
    except Exception as e:
        log.warning("Multicall pool check failed: %s", safe_err(e))
        return []


def _classify(holders: list[tuple[str, int, bool]], supply: int, pair_addrs: list[str],
              chain_id: int, token: str, goplus_data: dict | None, deadline: float,
              pools_found: set[str] | None = None) -> tuple[list[tuple[str, int]], list[tuple[str, float, str]], list[tuple[str, int, float]], set[str]]:
    """
    Classify and exclude holders.
    
    Args:
        holders: [(address, balance, is_contract), ...]
        supply: total supply
        pair_addrs: pair addresses from DexScreener
        chain_id: chain ID
        token: token address
        goplus_data: GoPlus data for lock checks
        deadline: absolute deadline timestamp
        pools_found: (optional) pre-computed Multicall pool set to reuse
    
    Returns:
        (valid_holders, excluded, unidentified_contracts, pools_found) where:
        - valid_holders: [(address, balance), ...]
        - excluded: [(address, pct, reason), ...]
        - unidentified_contracts: [(address, balance, pct), ...]
        - pools_found: set of pool addresses found by Multicall
    """
    excluded = []
    valid = []
    
    # Normalize addresses
    pair_addrs_set = {p.lower() for p in pair_addrs if p}
    token_lower = token.lower()
    
    # Get GoPlus pairs and locked holders
    goplus_pairs = set()
    goplus_locked = {}  # {address: (unlock_time_timestamp, is_permanent)}
    
    if goplus_data:
        # Extract pairs from dex field
        dex_list = goplus_data.get("dex", [])
        if isinstance(dex_list, list):
            for dex_entry in dex_list:
                if isinstance(dex_entry, dict):
                    pair_addr = dex_entry.get("pair")
                    if pair_addr:
                        goplus_pairs.add(pair_addr.lower())
        
        # Extract locked holders using REAL schema (item #3: accept int or string)
        holders_list = goplus_data.get("holders", [])
        if isinstance(holders_list, list):
            for holder_entry in holders_list:
                if isinstance(holder_entry, dict):
                    holder_addr = holder_entry.get("address", "").lower()
                    is_locked = holder_entry.get("is_locked")
                    
                    # Item #3: Accept int or string
                    is_locked_bool = False
                    if isinstance(is_locked, int):
                        is_locked_bool = is_locked == 1
                    elif isinstance(is_locked, str):
                        is_locked_bool = is_locked == "1"
                    
                    if is_locked_bool and holder_addr:
                        locked_details = holder_entry.get("locked_detail", [])
                        if isinstance(locked_details, list) and locked_details:
                            for detail in locked_details:
                                end_time_str = detail.get("end_time")
                                if end_time_str:
                                    try:
                                        # Parse ISO timestamp
                                        dt = datetime.fromisoformat(end_time_str.replace("+00:00", "+00:00"))
                                        unlock_timestamp = dt.timestamp()
                                        goplus_locked[holder_addr] = (unlock_timestamp, False)
                                    except Exception:
                                        pass
    
    # Item #5: Collect unidentified contracts >= 3% for GoPlus lock check
    unidentified_contracts = []
    
    # Collect top-20 holders for Multicall check (item #2: by balance, regardless of is_contract)
    top_20_by_balance = sorted(holders, key=lambda x: x[1], reverse=True)[:20]
    
    for addr, balance, is_contract in holders:
        if not addr:
            continue
        
        addr_lower = addr.lower()
        pct = (balance / supply * 100) if supply > 0 else 0
        
        # Burn addresses
        if addr_lower in BURN_ADDRESSES:
            excluded.append((addr, pct, "burn"))
            continue
        
        # Known pool contracts
        if addr_lower in POOL_CONTRACTS:
            excluded.append((addr, pct, "pool_contract"))
            continue
        
        # Launchpad curves
        if addr_lower in LAUNCHPAD_CURVES:
            excluded.append((addr, pct, "launchpad_curve"))
            continue
        
        # Permanent lockers
        if addr_lower in PERMANENT_LOCKERS:
            excluded.append((addr, pct, "permanent_locker"))
            continue
        
        # Pair addresses from DexScreener
        if addr_lower in pair_addrs_set:
            excluded.append((addr, pct, "pair_dexscreener"))
            continue
        
        # GoPlus pairs
        if addr_lower in goplus_pairs:
            excluded.append((addr, pct, "pair_goplus"))
            continue
        
        # Conditional lockers (need GoPlus lock check)
        if addr_lower in CONDITIONAL_LOCKERS:
            lock_info = goplus_locked.get(addr_lower)
            if lock_info:
                unlock_time, is_permanent = lock_info
                
                if is_permanent:
                    excluded.append((addr, pct, "locker_permanent"))
                    continue
                
                # Check if unlock > 7 days away
                if unlock_time:
                    days_until_unlock = (unlock_time - time.time()) / 86400
                    
                    if days_until_unlock > 7:
                        excluded.append((addr, pct, "locker_locked_7d"))
                        continue
        
        # GoPlus locked holders (>= 3% only)
        if pct >= 3 and addr_lower in goplus_locked:
            unlock_time, is_permanent = goplus_locked[addr_lower]
            
            if is_permanent:
                excluded.append((addr, pct, "goplus_locker_permanent"))
                continue
            
            if unlock_time:
                days_until_unlock = (unlock_time - time.time()) / 86400
                
                if days_until_unlock > 7:
                    excluded.append((addr, pct, "goplus_locker_7d"))
                    continue
        
        # Not excluded yet - add to valid list for Multicall check
        valid.append((addr, balance, pct))
    
    # Item #2 (Review 4): Multicall check for top-20 by balance
    # Do this BEFORE building unidentified_contracts
    if pools_found is None and top_20_by_balance and time.time() < deadline:
        pools_found = _check_pools_via_multicall(top_20_by_balance, token_lower, chain_id, deadline)
    
    pools_set = {p.lower() for p in (pools_found or set())}
    new_valid = []
    
    for addr, balance, pct in valid:
        if addr.lower() in pools_set:
            excluded.append((addr, pct, "pair_multicall"))
        else:
            new_valid.append((addr, balance, pct))
    
    valid = new_valid
    
    # Item #2 (Review 4): Build unidentified_contracts AFTER Multicall
    # Exclude: burns, static pools/curves/lockers, DexScreener pairs, GoPlus pairs, AND Multicall-found pools
    unidentified_contracts = []
    burns_set = BURN_ADDRESSES
    
    for addr, balance, is_contract in holders:
        addr_lower = addr.lower()
        pct = (balance / supply) * 100 if supply > 0 else 0
        
        if not is_contract or pct < 3:
            continue
        
        # Skip if already classified
        if (addr_lower in burns_set or
            addr_lower in POOL_CONTRACTS or
            addr_lower in LAUNCHPAD_CURVES or
            addr_lower in PERMANENT_LOCKERS or
            addr_lower in CONDITIONAL_LOCKERS or
            addr_lower in pair_addrs_set or
            addr_lower in goplus_pairs or
            addr_lower in pools_set):
            continue
        
        unidentified_contracts.append((addr, balance, pct))
    
    # Convert valid to final format
    valid_holders = [(addr, balance) for addr, balance, _ in valid]
    
    return valid_holders, excluded, unidentified_contracts, pools_found or set()


# In-memory result cache
_cache = {}  # {(chain_id, token): (result, timestamp)}


def evm_holder_concentration(chain_id: int, token: str, pair_addrs: list[str],
                            age_min: float, db: sqlite3.Connection) -> HolderResult:
    """
    Compute holder concentration for EVM tokens.
    
    Wrapped in try/except to catch all exceptions and return fail-closed result.
    """
    try:
        return _evm_holder_concentration_impl(chain_id, token, pair_addrs, age_min, db)
    except Exception as e:
        error_msg = safe_err(e)
        log.warning("EVM holder check exception for %s: %s", token, error_msg)
        is_transient = _classify_error(error_msg)
        return HolderResult(None, None, "unavailable", [], False, error_msg, is_transient)


def _evm_holder_concentration_impl(chain_id: int, token: str, pair_addrs: list[str],
                                  age_min: float, db: sqlite3.Connection) -> HolderResult:
    """Internal implementation of evm_holder_concentration."""
    if os.environ.get("EVM_HOLDERS_ENABLED", "1") == "0":
        return HolderResult(None, None, "disabled", [], False, "disabled", False)
    
    token_lower = token.lower()
    cache_key = (chain_id, token_lower)
    
    # Check in-memory cache
    if cache_key in _cache:
        result, timestamp = _cache[cache_key]
        if time.time() - timestamp < CACHE_DURATION:
            cached_result = HolderResult(
                top_wallet=result.top_wallet,
                top_10=result.top_10,
                source="cache",
                excluded=result.excluded,
                ok=result.ok,
                error=result.error,
                is_transient=result.is_transient,
                raw_top_wallet=result.raw_top_wallet,
                raw_top_10=result.raw_top_10,
                creator=getattr(result, "creator", None),
                creator_balance=getattr(result, "creator_balance", None),
                creator_initial_balance=getattr(result, "creator_initial_balance", None),
            )
            return cached_result
    
    deadline = time.time() + TOKEN_TIMEOUT
    
    # Try primary source
    primary_result = None
    primary_error = None
    
    if chain_id in (56, 8453):
        # Honeypot.is
        data, error = _holders_honeypot(chain_id, token, deadline)
        
        if error:
            primary_error = error
        elif data:
            total_supply = data["totalSupply"]
            holders_raw = data["holders"]
            
            creator, creator_balance, creator_initial = _creator_from_honeypot(data)

            # Convert to format
            holders = []
            for h in holders_raw:
                addr = h.get("address")
                if not addr:
                    continue
                
                balance = h.get("balance", 0)
                if isinstance(balance, str):
                    try:
                        balance = int(balance)
                    except (ValueError, TypeError):
                        balance = 0
                
                # Item #3: Accept int or string for isContract
                is_contract_val = h.get("isContract", False)
                if isinstance(is_contract_val, str):
                    is_contract = is_contract_val == "1"
                else:
                    is_contract = bool(is_contract_val)
                
                holders.append((addr, balance, is_contract))
            
            # Calculate raw values
            if holders and total_supply > 0:
                sorted_holders = sorted(holders, key=lambda x: x[1], reverse=True)
                raw_top_wallet = sorted_holders[0][1] / total_supply
                raw_top_10_balance = sum(h[1] for h in sorted_holders[:10])
                raw_top_10 = (raw_top_10_balance / total_supply) * 100
            else:
                raw_top_wallet = None
                raw_top_10 = None
            
            # Classify first WITHOUT GoPlus to identify unidentified contracts and conditional lockers
            valid_holders, excluded, unidentified_contracts, pools_found = _classify(holders, total_supply, pair_addrs, chain_id, token, None, deadline)
            
            # Check if we need GoPlus: unidentified contract >= 3% OR any conditional locker
            needs_goplus = bool(unidentified_contracts)
            if not needs_goplus:
                # Check for conditional lockers
                for addr, balance, is_contract in holders:
                    if addr.lower() in CONDITIONAL_LOCKERS:
                        needs_goplus = True
                        break
            
            # Get GoPlus data if needed
            goplus_data = None
            if needs_goplus and age_min >= 120 and time.time() < deadline:
                goplus_data, _ = _holders_goplus(chain_id, token, deadline)
                
                # Re-classify WITH GoPlus data, reusing pools_found
                if goplus_data:
                    valid_holders, excluded, _, _ = _classify(holders, total_supply, pair_addrs, chain_id, token, goplus_data, deadline, pools_found)
            
            # Subtract burns from supply
            burn_amount = sum(balance for addr, balance, _ in holders if addr.lower() in BURN_ADDRESSES)
            adjusted_supply = total_supply - burn_amount
            
            if adjusted_supply <= 0:
                primary_result = HolderResult(None, None, "unavailable", [], False, "zero_supply_after_burns", False)
            elif not valid_holders:
                # Item #10: Keep primary_result.error
                primary_result = HolderResult(None, None, "unavailable", excluded, False, "all_excluded", False)
            else:
                sorted_valid = sorted(valid_holders, key=lambda x: x[1], reverse=True)
                
                top_wallet_balance = sorted_valid[0][1] if sorted_valid else 0
                top_wallet_pct = top_wallet_balance / adjusted_supply
                
                top_10_balance = sum(h[1] for h in sorted_valid[:10])
                top_10_pct = (top_10_balance / adjusted_supply) * 100
                
                primary_result = HolderResult(
                    top_wallet_pct, top_10_pct, "honeypot", excluded, True, None, False,
                    raw_top_wallet=raw_top_wallet, raw_top_10=raw_top_10,
                    creator=creator, creator_balance=creator_balance,
                    creator_initial_balance=creator_initial,
                )
    
    elif chain_id == 4663:
        # Robinhood RPC fold
        call_count = [0]
        data, error = _holders_rpc_fold(token, deadline, db, call_count)
        
        if error:
            primary_error = error
        elif data:
            balances = data["balances"]
            supply = data["supply"]
            complete = data["complete"]
            
            if not complete:
                # Incomplete but we have some data - check if it's progress or definitive
                # If we have balances, it's transient
                is_transient = len(balances) > 0
                primary_error = "incomplete_fold"
                # Will be handled below
            else:
                # Convert to holders list (mark all as is_contract=False for Multicall to run)
                holders = [(addr, balance, False) for addr, balance in balances.items()]
                
                # Calculate raw values
                if holders and supply > 0:
                    sorted_holders = sorted(holders, key=lambda x: x[1], reverse=True)
                    raw_top_wallet = sorted_holders[0][1] / supply
                    raw_top_10_balance = sum(h[1] for h in sorted_holders[:10])
                    raw_top_10 = (raw_top_10_balance / supply) * 100
                else:
                    raw_top_wallet = None
                    raw_top_10 = None
                
                # Classify first WITHOUT GoPlus to identify unidentified contracts and conditional lockers
                # NOTE (Review 4 Item 2): RH fold path marks all holders is_contract=False,
                # so unidentified_contracts is always empty and GoPlus lock tags never run.
                # This fails safe (stricter: unknown locker counts as holder -> possible kill).
                valid_holders, excluded, unidentified_contracts, pools_found = _classify(holders, supply, pair_addrs, chain_id, token, None, deadline)
                
                # Check if we need GoPlus: unidentified contract >= 3% OR any conditional locker
                needs_goplus = bool(unidentified_contracts)
                if not needs_goplus:
                    # Check for conditional lockers
                    for addr, balance, is_contract in holders:
                        if addr.lower() in CONDITIONAL_LOCKERS:
                            needs_goplus = True
                            break
                
                # Get GoPlus data if needed
                goplus_data = None
                if needs_goplus and age_min >= 120 and time.time() < deadline:
                    goplus_data, _ = _holders_goplus(chain_id, token, deadline)
                    
                    # Re-classify WITH GoPlus data, reusing pools_found
                    if goplus_data:
                        valid_holders, excluded, _, _ = _classify(holders, supply, pair_addrs, chain_id, token, goplus_data, deadline, pools_found)
                
                # Subtract burns
                burn_amount = sum(balance for addr, balance in balances.items() if addr.lower() in BURN_ADDRESSES)
                adjusted_supply = supply - burn_amount
                
                if adjusted_supply <= 0:
                    primary_result = HolderResult(None, None, "unavailable", [], False, "zero_supply_after_burns", False)
                elif not valid_holders:
                    primary_result = HolderResult(None, None, "unavailable", excluded, False, "all_excluded", False)
                else:
                    sorted_valid = sorted(valid_holders, key=lambda x: x[1], reverse=True)
                    
                    top_wallet_balance = sorted_valid[0][1] if sorted_valid else 0
                    top_wallet_pct = top_wallet_balance / adjusted_supply
                    
                    top_10_balance = sum(h[1] for h in sorted_valid[:10])
                    top_10_pct = (top_10_balance / adjusted_supply) * 100
                    
                    primary_result = HolderResult(
                        top_wallet_pct, top_10_pct, "rpc_fold", excluded, True, None, False,
                        raw_top_wallet=raw_top_wallet, raw_top_10=raw_top_10
                    )
    
    else:
        primary_error = "unsupported_chain"
    
    # If primary succeeded, cache and return
    if primary_result and primary_result.ok:
        _cache[cache_key] = (primary_result, time.time())
        return primary_result
    
    # Try GoPlus fallback (age >= 120m)
    if age_min >= 120 and time.time() < deadline:
        goplus_data, goplus_error = _holders_goplus(chain_id, token, deadline)
        
        if goplus_data:
            holders_list = goplus_data.get("holders", [])
            
            if holders_list and isinstance(holders_list, list) and len(holders_list) > 0:
                try:
                    # Parse holders with percent as FRACTION (0-1)
                    holder_data = []
                    total_pct = 0.0
                    
                    for h in holders_list[:50]:
                        if isinstance(h, dict):
                            addr = h.get("address")
                            pct_str = h.get("percent")
                            is_contract = h.get("is_contract")
                            
                            # Item #3: Accept int or string
                            is_contract_bool = False
                            if isinstance(is_contract, int):
                                is_contract_bool = is_contract == 1
                            elif isinstance(is_contract, str):
                                is_contract_bool = is_contract == "1"
                            
                            if addr and pct_str:
                                try:
                                    pct_fraction = float(pct_str)
                                    total_pct += pct_fraction
                                    balance = int(pct_fraction * 1e18)
                                    holder_data.append((addr, balance, is_contract_bool))
                                except ValueError:
                                    pass
                    
                    if holder_data:
                        # Supply = 1e18 (scaled)
                        supply = int(1e18)
                        
                        # Item #4: Subtract burn percentages from supply
                        burn_pct = 0.0
                        for h in holder_data:
                            if h[0].lower() in BURN_ADDRESSES:
                                burn_pct += h[1] / supply
                        
                        # Adjust supply
                        adjusted_supply = int(supply * (1 - burn_pct))
                        
                        # Calculate raw values
                        sorted_holders = sorted(holder_data, key=lambda x: x[1], reverse=True)
                        raw_top_wallet = sorted_holders[0][1] / supply
                        raw_top_10_balance = sum(h[1] for h in sorted_holders[:10])
                        raw_top_10 = (raw_top_10_balance / supply) * 100
                        
                        # Classify
                        valid_holders, excluded, _, _ = _classify(holder_data, supply, pair_addrs, chain_id, token, goplus_data, deadline)
                        
                        if not valid_holders:
                            result = HolderResult(None, None, "unavailable", excluded, False, "all_excluded", False)
                        else:
                            sorted_valid = sorted(valid_holders, key=lambda x: x[1], reverse=True)
                            
                            top_wallet_pct = sorted_valid[0][1] / adjusted_supply
                            top_10_balance = sum(h[1] for h in sorted_valid[:10])
                            top_10_pct = (top_10_balance / adjusted_supply) * 100
                            
                            result = HolderResult(
                                top_wallet_pct, top_10_pct, "goplus", excluded, True, None, False,
                                raw_top_wallet=raw_top_wallet, raw_top_10=raw_top_10
                            )
                            _cache[cache_key] = (result, time.time())
                            return result
                
                except Exception as e:
                    log.warning("GoPlus parse error for %s: %s", token, safe_err(e))
    
    # Fail closed - item #10: keep primary_result.error or use primary_error
    error_msg = (primary_result.error if primary_result else None) or primary_error or "no_data"
    
    # Item #6: Classify error as transient or definitive
    is_transient = _classify_error(error_msg)
    
    result = HolderResult(None, None, "unavailable", [], False, error_msg, is_transient)
    
    return result


def prune_old_cache(db: sqlite3.Connection):
    """Remove cache entries older than 72 hours."""
    cutoff = time.time() - 72 * 3600
    deleted = db.execute(
        "DELETE FROM evm_holder_cache WHERE updated_at < ?",
        (cutoff,)
    ).rowcount
    
    if deleted > 0:
        db.commit()
        log.info("Pruned %d old EVM holder cache entries", deleted)
