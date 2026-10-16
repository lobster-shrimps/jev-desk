"""
Funnel tests. No network, no key: collectors are faked, the judge is the mock.
    pytest -q
"""
import logging
import os
import sys
import time
import pathlib
from unittest.mock import Mock
import requests
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DESK_DB", ":memory:")
os.environ.setdefault("DESK_OUTBOX", str(ROOT / "tests" / "_outbox"))

import book                                           # noqa: E402
from filter import free_kill, trade_kill, chain_kill, soft_kill   # noqa: E402
from pick import pick, summary                        # noqa: E402
from mock_judge import mock_judge_fn                  # noqa: E402
from questions import SETS                            # noqa: E402
from thresholds import PICK_MIN_WORTH, PICK_MIN_CONF, NO_SOCIAL_CUT   # noqa: E402
import main as shift                                  # noqa: E402
import collect                                        # noqa: E402
from fomo_api import Fomo                             # noqa: E402

JUDGE = mock_judge_fn()
NOW_MS = int(time.time() * 1000)


@pytest.fixture(autouse=True)
def reset_state():
    """Reset shared state before each test to ensure isolation."""
    # Clear carry table
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    yield


def _paced_limiter(calls_per_min=10, start=0.0, **kw):
    """GTRateLimiter whose sleep advances a fake clock (no real sleeping)."""
    box = [start]
    limiter = collect.GTRateLimiter(
        calls_per_min=calls_per_min,
        time_fn=lambda: box[0],
        sleep_fn=lambda d: box.__setitem__(0, box[0] + d),
        **kw,
    )
    return limiter, box


@pytest.fixture(autouse=True)
def reset_gt_limiter():
    """Reset the global GT rate limiter before each test to ensure isolation.
    
    Also installs a fake clock/sleep so spacing and window waits never real-sleep.
    Resets last_call_time and cycle stats (review T1)."""
    clock = [time.time()]
    shift._gt_limiter.reset_for_test()
    shift._gt_limiter.time_fn = lambda: clock[0]
    shift._gt_limiter.sleep_fn = lambda d: clock.__setitem__(0, clock[0] + d)
    yield
    shift._gt_limiter.reset_for_test()
    shift._gt_limiter.time_fn = time.time
    shift._gt_limiter.sleep_fn = None


def tok(i, net=1399811149, **over):
    t = {"addr": f"Addr{i}", "net": net, "tid": f"Addr{i}:{net}", "ticker": f"T{i}",
         "mcap_usd": 300_000, "liquidity_usd": 48_000, "volume_h24": 610_000,
         "price_usd": 0.001, "holder_count": 310,
         "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}, "age_minutes": 42}
    t.update(over)
    return t


def _pass_sol_wallet(mint):
    """Deterministic Solana concentration pass. No network."""
    return {"top_wallet": 0.02, "top_10": 0.30, "pools_excluded": True}, True, None


# ---- fomo_api parsing ---------------------------------------------------------
def test_filter_tokens_parses_bare_list():
    """_filter_tokens should handle a bare list response."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = [
        {"id": "addr1:56", "symbol": "TKN1", "marketCap": 100000},
        {"address": "addr2", "netId": 56, "symbol": "TKN2", "marketCap": 200000}
    ]
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["addr1:56", "addr2:56"])
    assert len(result) == 2
    assert "addr1:56" in result
    assert "addr2:56" in result
    assert result["addr1:56"]["symbol"] == "TKN1"


def test_filter_tokens_parses_id_map():
    """_filter_tokens should handle {id: row} dict response."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "addr1:56": {"symbol": "TKN1", "marketCap": 100000},
        "addr2:56": {"symbol": "TKN2", "marketCap": 200000}
    }
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["addr1:56", "addr2:56"])
    assert len(result) == 2
    assert result["addr1:56"]["symbol"] == "TKN1"


def test_filter_tokens_parses_data_envelope():
    """_filter_tokens should handle {"data": [...]} envelope."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "data": [
            {"id": "addr1:56", "symbol": "TKN1", "marketCap": 100000},
            {"address": "addr2", "netId": 56, "symbol": "TKN2", "marketCap": 200000}
        ]
    }
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["addr1:56", "addr2:56"])
    assert len(result) == 2
    assert result["addr1:56"]["symbol"] == "TKN1"


def test_filter_tokens_parses_response_object_envelope():
    """_filter_tokens should handle {"responseObject": [...]} envelope from live API."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "success": True,
        "statusCode": 200,
        "message": "Tokens filtered successfully",
        "responseObject": [
            {"id": "addr1:56", "symbol": "TKN1", "marketCap": 100000},
            {"address": "addr2", "netId": 56, "symbol": "TKN2", "marketCap": 200000}
        ]
    }
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["addr1:56", "addr2:56"])
    assert len(result) == 2
    assert result["addr1:56"]["symbol"] == "TKN1"
    assert result["addr2:56"]["symbol"] == "TKN2"


def test_filter_tokens_handles_empty_response_object():
    """_filter_tokens should return empty dict for empty responseObject."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "success": True,
        "statusCode": 200,
        "message": "No tokens found",
        "responseObject": []
    }
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["addr1:56"])
    assert result == {}


def test_filter_tokens_parses_nested_token_structure():
    """_filter_tokens should handle nested {token: {address, networkId, ...}} structure from live API."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "success": True,
        "statusCode": 200,
        "message": "Tokens filtered successfully",
        "responseObject": [
            {
                "token": {
                    "address": "So11111111111111111111111111111111111111112",
                    "networkId": 1399811149,
                    "symbol": "SOL",
                    "marketCap": 85000000000,
                    "liquidity": 45000000,
                    "volume24": 125000000,
                    "priceUSD": 150.45,
                    "holders": 2500000,
                    "createdAt": 1609459200000,
                    "change5m": 0.02,
                    "change1": 0.05,
                    "change4": 0.12,
                    "change24": 0.08
                },
                "someOtherField": "ignored"
            },
            {
                "token": {
                    "address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "networkId": 1399811149,
                    "symbol": "USDC",
                    "marketCap": 45000000000,
                    "liquidity": 120000000,
                    "volume24": 850000000,
                    "priceUSD": 1.00,
                    "holders": 1800000,
                    "createdAt": 1625097600000
                }
            }
        ]
    }
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["So11111111111111111111111111111111111111112:1399811149",
                                   "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v:1399811149"])
    assert len(result) == 2
    assert "So11111111111111111111111111111111111111112:1399811149" in result
    assert "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v:1399811149" in result
    # Verify flattened structure has expected fields
    sol_row = result["So11111111111111111111111111111111111111112:1399811149"]
    assert sol_row["symbol"] == "SOL"
    assert sol_row["marketCap"] == 85000000000
    assert sol_row["networkId"] == 1399811149


def test_nested_token_yields_parsed_tokens():
    """Nested token structure should successfully parse through _row and produce valid token data."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "responseObject": [
            {
                "token": {
                    "address": "TestAddr1",
                    "networkId": 56,
                    "symbol": "TEST",
                    "marketCap": 500000,
                    "liquidity": 80000,
                    "volume24": 250000,
                    "priceUSD": 0.05,
                    "holders": 1500,
                    "createdAt": int(time.time() * 1000 - 3600000),  # 1 hour ago
                    "change5m": 0.03,
                    "change1": 0.08,
                    "change4": 0.15,
                    "change24": 0.25
                }
            }
        ]
    }
    fomo.s.post = Mock(return_value=mock_resp)
    
    # Full flow: _filter_tokens -> tokens() -> _row()
    tokens = fomo.tokens(["TestAddr1:56"])
    assert len(tokens) == 1
    assert "TestAddr1:56" in tokens
    
    token = tokens["TestAddr1:56"]
    assert token["symbol"] == "TEST"
    assert token["mcap"] == 500000.0
    assert token["liq"] == 80000.0
    assert token["vol24"] == 250000.0
    assert token["price"] == 0.05
    assert token["holders"] == 1500
    assert token["change"][300] == 0.03  # 5m
    assert token["change"][3600] == 0.08  # 1h
    assert token["created"] is not None


def test_filter_tokens_retries_on_connection_error():
    """_filter_tokens should retry up to 4 times on ConnectionError."""
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"responseObject": [{"id": "addr1:56", "symbol": "TKN"}]}
    
    # First call raises ConnectionError, second succeeds
    call_count = [0]
    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise requests.exceptions.ConnectionError("Connection reset by peer")
        return mock_resp
    
    fomo.s.post = Mock(side_effect=side_effect)
    fomo.s.adapters = {}  # Mock empty adapters dict so close() doesn't fail
    result = fomo._filter_tokens(["addr1:56"])
    
    assert call_count[0] == 2  # Should have retried once (1 failure + 1 success)
    assert len(result) == 1
    assert "addr1:56" in result


def test_filter_tokens_retries_on_502():
    """_filter_tokens should retry on HTTP 502 Bad Gateway with exponential backoff."""
    fomo = Fomo(bearer="fake-token")
    mock_502 = Mock()
    mock_502.status_code = 502
    mock_200 = Mock()
    mock_200.status_code = 200
    mock_200.json.return_value = {"responseObject": [{"id": "addr1:56", "symbol": "TKN"}]}
    
    # First call returns 502, second succeeds
    fomo.s.post = Mock(side_effect=[mock_502, mock_200])
    result = fomo._filter_tokens(["addr1:56"])
    
    assert fomo.s.post.call_count == 2  # Should have retried once (1 failure + 1 success)
    assert len(result) == 1


def test_filter_tokens_retries_multiple_502s():
    """_filter_tokens should retry up to 4 times on persistent 502s."""
    fomo = Fomo(bearer="fake-token")
    mock_502 = Mock()
    mock_502.status_code = 502
    mock_200 = Mock()
    mock_200.status_code = 200
    mock_200.json.return_value = {"responseObject": [{"id": "addr1:56", "symbol": "TKN"}]}
    
    # Three 502s, then success on 4th attempt
    fomo.s.post = Mock(side_effect=[mock_502, mock_502, mock_502, mock_200])
    result = fomo._filter_tokens(["addr1:56"])
    
    assert fomo.s.post.call_count == 4  # 3 failures + 1 success
    assert len(result) == 1
    assert "addr1:56" in result


def test_filter_tokens_exhausts_retries_on_persistent_502():
    """_filter_tokens should return empty dict after 5 failed attempts (graceful degrade)."""
    fomo = Fomo(bearer="fake-token")
    mock_502 = Mock()
    mock_502.status_code = 502
    mock_502.raise_for_status = Mock(side_effect=requests.exceptions.HTTPError("502 Server Error"))
    
    fomo.s.post = Mock(return_value=mock_502)
    
    result = fomo._filter_tokens(["addr1:56"])
    
    # Should return empty dict instead of raising (graceful degrade)
    assert result == {}
    
    # Should have tried 5 times (initial + 4 retries)
    assert fomo.s.post.call_count == 5


def test_filter_tokens_retries_on_503_and_504():
    """_filter_tokens should retry on 503 and 504 gateway errors."""
    fomo = Fomo(bearer="fake-token")
    mock_503 = Mock()
    mock_503.status_code = 503
    mock_504 = Mock()
    mock_504.status_code = 504
    mock_200 = Mock()
    mock_200.status_code = 200
    mock_200.json.return_value = {"responseObject": [{"id": "addr1:56", "symbol": "TKN"}]}
    
    # 503, then 504, then success
    fomo.s.post = Mock(side_effect=[mock_503, mock_504, mock_200])
    result = fomo._filter_tokens(["addr1:56"])
    
    assert fomo.s.post.call_count == 3
    assert len(result) == 1
    assert "addr1:56" in result


def test_filter_tokens_exponential_backoff():
    """_filter_tokens should use exponential backoff (0.5s, 1s, 2s, 4s)."""
    import time as time_module
    
    fomo = Fomo(bearer="fake-token")
    mock_502 = Mock()
    mock_502.status_code = 502
    mock_200 = Mock()
    mock_200.status_code = 200
    mock_200.json.return_value = {"responseObject": [{"id": "addr1:56", "symbol": "TKN"}]}
    
    sleep_calls = []
    original_sleep = time_module.sleep
    time_module.sleep = lambda x: sleep_calls.append(x)
    
    try:
        # Three 502s, then success
        fomo.s.post = Mock(side_effect=[mock_502, mock_502, mock_502, mock_200])
        result = fomo._filter_tokens(["addr1:56"])
        
        assert len(sleep_calls) == 3
        assert sleep_calls[0] == 0.5   # 0.5 * 2^0
        assert sleep_calls[1] == 1.0   # 0.5 * 2^1
        assert sleep_calls[2] == 2.0   # 0.5 * 2^2
        assert len(result) == 1
    finally:
        time_module.sleep = original_sleep


def test_flat_and_nested_shapes_both_work():
    """Verify backward compatibility: both flat and nested structures parse correctly."""
    fomo = Fomo(bearer="fake-token")
    
    # Test with mixed response
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "responseObject": [
            # Flat structure (old format)
            {"id": "addr1:56", "symbol": "FLAT", "marketCap": 100000},
            # Nested structure (new format)
            {"token": {"address": "addr2", "networkId": 56, "symbol": "NESTED", "marketCap": 200000}}
        ]
    }
    fomo.s.post = Mock(return_value=mock_resp)
    result = fomo._filter_tokens(["addr1:56", "addr2:56"])
    
    assert len(result) == 2
    assert "addr1:56" in result
    assert "addr2:56" in result
    assert result["addr1:56"]["symbol"] == "FLAT"
    assert result["addr2:56"]["symbol"] == "NESTED"


def test_top_level_metrics_with_nested_token_structure():
    """BUG REPRODUCTION: Market metrics at top level + nested token object should preserve top-level values.
    
    This is the exact shape that causes the all-zero liquidity/mcap bug from issue evidence.
    FOMO returns: {marketCap: X, liquidity: Y, token: {address, networkId}} 
    The bug: _flatten_nested_token overwrites X and Y with None from token_data.
    """
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "responseObject": [
            {
                # Market metrics at TOP level (the real values)
                "marketCap": 500000,
                "liquidity": 80000,
                "volume24": 250000,
                "priceUSD": 0.05,
                "holders": 1500,
                "createdAt": int(time.time() * 1000 - 3600000),  # 1 hour ago
                "change5m": 0.03,
                "change1": 0.08,
                # Nested token object with ONLY address/network (NOT metrics)
                "token": {
                    "address": "TopLevelAddr1",
                    "networkId": 56
                }
            }
        ]
    }
    fomo.s.post = Mock(return_value=mock_resp)
    
    # Full flow: _filter_tokens -> tokens() -> _row()
    tokens = fomo.tokens(["TopLevelAddr1:56"])
    assert len(tokens) == 1
    assert "TopLevelAddr1:56" in tokens
    
    token = tokens["TopLevelAddr1:56"]
    assert token["symbol"] is not None
    # BUG: These should be 500000/80000 but were being overwritten to None -> 0.0
    assert token["mcap"] == 500000.0, f"Expected mcap=500000, got {token['mcap']}"
    assert token["liq"] == 80000.0, f"Expected liq=80000, got {token['liq']}"
    assert token["vol24"] == 250000.0
    assert token["price"] == 0.05
    assert token["holders"] == 1500


def test_normalise_preserves_none_for_missing_metrics():
    """normalise should preserve None for truly missing data, not invent 0.0.
    
    Per collect.py contract: 'NEVER invent a number. A field that came back null stays null.'
    """
    # Case 1: FOMO returned None for mcap/liq (truly missing data)
    m = {"symbol": "TEST", "mcap": None, "liq": None, "vol24": None, "price": None,
         "holders": None, "change": {300: None, 3600: None, 14400: None, 86400: None},
         "created": int(time.time() * 1000)}
    
    t = collect.normalise("addr1:56", m)
    
    # Should preserve None, not invent 0.0
    assert t["mcap_usd"] is None, f"Expected None for missing mcap, got {t['mcap_usd']}"
    assert t["liquidity_usd"] is None, f"Expected None for missing liq, got {t['liquidity_usd']}"
    assert t["volume_h24"] is None, f"Expected None for missing vol24, got {t['volume_h24']}"


def test_shortlist_ranking_survives_none_metrics():
    """shortlist should sort without crashing when mcap/vol are None.
    
    BUG: After removing 'or 0.0' coercion, the ranking `vol / max(mcap, 1)` would
    TypeError on None values. Missing data should sort to lowest priority (turnover=0).
    """
    from fomo_api import Fomo
    
    fomo = Fomo(bearer="fake-token")
    mock_resp = Mock()
    mock_resp.json.return_value = {
        "responseObject": [
            # Token 1: All real values, high turnover
            {"token": {"address": "addr1", "networkId": 56},
             "marketCap": 100000, "liquidity": 50000, "volume24": 80000,
             "priceUSD": 0.01, "holders": 500, "createdAt": int(time.time() * 1000 - 3600000)},
            # Token 2: None mcap (should rank lowest)
            {"token": {"address": "addr2", "networkId": 56},
             "marketCap": None, "liquidity": 50000, "volume24": 60000,
             "priceUSD": 0.01, "holders": 500, "createdAt": int(time.time() * 1000 - 3600000)},
            # Token 3: None volume (should rank lowest)
            {"token": {"address": "addr3", "networkId": 56},
             "marketCap": 100000, "liquidity": 50000, "volume24": None,
             "priceUSD": 0.01, "holders": 500, "createdAt": int(time.time() * 1000 - 3600000)},
            # Token 4: Real values, low turnover
            {"token": {"address": "addr4", "networkId": 56},
             "marketCap": 500000, "liquidity": 50000, "volume24": 10000,
             "priceUSD": 0.01, "holders": 500, "createdAt": int(time.time() * 1000 - 3600000)},
        ]
    }
    fomo.s.post = Mock(return_value=mock_resp)
    
    # Should not crash
    tokens = collect.shortlist(fomo, ["addr1:56", "addr2:56", "addr3:56", "addr4:56"])
    
    # Should return all 4 tokens
    assert len(tokens) == 4
    
    # High turnover (token 1) should rank first
    assert tokens[0]["addr"] == "addr1"
    
    # None values (tokens 2, 3) should rank lowest (after token 4 with low but real turnover)
    # Exact order of None values doesn't matter, but they should be at the end
    none_addrs = {tokens[2]["addr"], tokens[3]["addr"]}
    assert none_addrs == {"addr2", "addr3"}, f"Expected None tokens at end, got order: {[t['addr'] for t in tokens]}"


# ---- filter -------------------------------------------------------------------
def test_free_kill_order_and_reasons():
    assert free_kill(tok(1)) is None
    assert free_kill(tok(1, age_minutes=5)) == "age"
    assert free_kill(tok(1, age_minutes=80 * 60)) == "age"
    assert free_kill(tok(1, liquidity_usd=1000)) == "liquidity"
    assert free_kill(tok(1, volume_h24=10)) == "volume"
    assert free_kill(tok(1, mcap_usd=10)) == "mcap"


def test_free_kill_distinguishes_none_from_threshold():
    """None metrics get distinct kill reasons (no_liq, no_vol, no_mcap) vs threshold failures."""
    # Real values below threshold
    assert free_kill(tok(1, liquidity_usd=1000)) == "liquidity"
    assert free_kill(tok(1, volume_h24=100)) == "volume"
    assert free_kill(tok(1, mcap_usd=1000)) == "mcap"
    
    # None values (missing data)
    assert free_kill(tok(1, liquidity_usd=None)) == "no_liq"
    assert free_kill(tok(1, volume_h24=None)) == "no_vol"
    assert free_kill(tok(1, mcap_usd=None)) == "no_mcap"
    
    # Mix: Some real, some None - first failure wins
    assert free_kill(tok(1, liquidity_usd=None, volume_h24=100)) == "no_liq"
    assert free_kill(tok(1, liquidity_usd=50000, volume_h24=None)) == "no_vol"


def test_trade_kill():
    assert trade_kill({"trades_h24": None}) == "no_pair"
    assert trade_kill({"trades_h24": 10, "sells_h1": 3, "buys_h1": 5}) == "trades"
    assert trade_kill({"trades_h24": 500, "sells_h1": 0, "buys_h1": 50}) == "no_sells"
    assert trade_kill({"trades_h24": 500, "sells_h1": 20, "buys_h1": 50}) is None


def test_chain_kill_facts_before_judgements():
    base = {"chain": "solana", "top_wallet_percent": 0.01, "top_10_percent": 30,
            "holder_count": 300, "mint_authority": None, "freeze_authority": None}
    assert chain_kill(base) is None
    assert chain_kill({**base, "top_wallet_percent": 0.2}) == "top_wallet"
    assert chain_kill({**base, "top_10_percent": 80}) == "top_10"
    assert chain_kill({**base, "holder_count": 10}) == "holders"
    # After normalization, authority fields are True (open), False (revoked), or None (unknown)
    assert chain_kill({**base, "mint_authority": True}) == "authority_open"
    assert chain_kill({**base, "freeze_authority": True}) == "authority_open"
    assert chain_kill({**base, "mint_authority": False, "freeze_authority": False}) is None
    # EVM chains: honeypot check comes after top_wallet verification
    assert chain_kill({"chain": "bsc", "top_wallet_percent": 0.03, "is_honeypot": True}) == "honeypot"
    # EVM chains without top_wallet verification fail closed
    assert chain_kill({"chain": "bsc", "top_wallet_percent": None, "holder_count": 100}) == "top_wallet_unverified"
    assert chain_kill({"chain": "bsc", "top_wallet_percent": 0.03, "holder_count": 100}) is None  # verified is ok


def test_soft_kill_reads_noul_and_score_and_shape():
    good = {"concentration_is_exit_risk": {"noul": 0.1},
            "shape": {"choice": "crowd", "probabilities": {"crowd": 0.8}},
            "effort": {"score": 2.0}}
    assert soft_kill(good) is None
    result = soft_kill({**good, "concentration_is_exit_risk": {"noul": 0.9}})
    assert result == ("concentration_is_exit_risk", 0.9)
    result = soft_kill({**good, "effort": {"score": 0.2}})
    assert result == ("effort", 0.2)
    result = soft_kill({**good, "shape": {"choice": "fading", "probabilities": {"crowd": 0.1}}})
    assert result == ("shape", 0.1)
    result = soft_kill({**good, "shape": {"choice": "too_early", "probabilities": {"crowd": 0.3}}})
    assert result == ("shape_weak", 0.3)
    result = soft_kill({**good, "sell_side_risk": {"choice": "flagged"}})
    assert result == ("sell_side", None)


# ---- questions / judge contract ----------------------------------------------
def test_every_set_answers_in_wire_shape():
    state = {**tok(1), "chain": "solana", "intended_ticket_usd": 60,
             "top_10_percent": 30, "top_wallet_percent": 0.02,
             "mint_authority": None, "freeze_authority": None}
    for name in ("market", "solana", "bsc", "robinhood"):
        r = JUDGE(name, state)
        assert r["model"] and "answers" in r and "usage" in r
        for q, a in r["answers"].items():
            assert a["type"] in ("noul", "choice", "score")
            if a["type"] == "choice":
                assert a["choice"] in SETS[name][q].criteria
                assert abs(sum(a["probabilities"].values()) - 1) < 0.01   # "approximately 1"
            if a["type"] == "noul":
                assert 0 <= a["noul"] <= 1


def _survivors(n=3):
    out = []
    for i in range(n):
        d = {**tok(i), "chain": "solana"}
        ans = {"shape": {"choice": "crowd", "probabilities": {"crowd": 0.7 + i / 10}},
               "concentration_is_exit_risk": {"noul": 0.2},
               "authority_risk": {"choice": "renounced"}}
        out.append((d, ans))
    return out


def _pick_judge(worth, conf, choice="T1", tickers=("T0", "T1", "T2"), seen=None):
    """A stand-in for the pick call with both gate inputs set explicitly.

    mock_judge is deterministic but its worth noul lands at 0.584, just under
    PICK_MIN_WORTH, so driving these tests through it asserts nothing. Set the
    two gate values here and the outcome is a fact, not a coin toss.
    """
    def judge(question_set, state):
        assert question_set == "pick"
        if seen is not None:
            seen.append(state)
        rest = round((1 - conf) / max(len(tickers) - 1, 1), 3)
        return {"model": "stub-pick",
                "answers": {
                    "best": {"type": "choice", "choice": choice, "confidence": conf,
                             "probabilities": {t: (conf if t == choice else rest)
                                               for t in tickers}},
                    "worth_trading_at_all": {"type": "noul", "noul": worth}},
                "usage": {}}
    return judge


def test_summary_is_built_from_answers_not_the_dossier():
    s = summary(*_survivors(1)[0])
    assert "solana" in s and "no usable X account" in s
    assert "crowd 0.70" in s and "concentration risk 0.20" in s
    assert "authority renounced" in s


def test_pick_builds_one_option_per_candidate():
    seen = []
    pick(_pick_judge(worth=0.9, conf=0.9, seen=seen), _survivors())
    assert len(seen) == 1                            # one call, all candidates at once
    assert [c["ticker"] for c in seen[0]["candidates"]] == ["T0", "T1", "T2"]
    assert all(c["summary"] for c in seen[0]["candidates"])


def test_pick_returns_the_chosen_token_when_both_gates_pass():
    order = pick(_pick_judge(worth=PICK_MIN_WORTH, conf=PICK_MIN_CONF), _survivors())
    assert order is not None                         # both gates are inclusive at the limit
    assert order["token"]["ticker"] == "T1"
    assert order["token"]["address"] == "Addr1" and order["token"]["chain"] == "solana"
    assert order["size_factor"] == NO_SOCIAL_CUT     # no X account on any candidate
    assert order["confidence"] == PICK_MIN_CONF
    assert order["runner_up"][0][0] in {"T0", "T2"}


def test_pick_declines_below_either_gate():
    s = _survivors()
    assert pick(_pick_judge(worth=PICK_MIN_WORTH - 0.01, conf=0.9), s) is None
    assert pick(_pick_judge(worth=0.9, conf=PICK_MIN_CONF - 0.01), s) is None


def test_pick_declines_when_the_choice_is_not_a_candidate():
    assert pick(_pick_judge(worth=0.9, conf=0.9, choice="GHOST"), _survivors()) is None


def test_pick_declines_on_no_survivors():
    assert pick(_pick_judge(worth=0.9, conf=0.9), []) is None


def test_unknown_set_is_422_not_retry():
    try:
        JUDGE("nonsense", {})
        assert False, "should raise"
    except RuntimeError as e:
        assert "malformed" in str(e)


# ---- book ---------------------------------------------------------------------
def test_book_one_position_and_reasoned_bench():
    book.release()
    assert book.held() is None
    book.take({"token": {"ticker": "X", "address": "a", "network_id": 56}})
    assert book.held()["ticker"] == "X"
    book.release()
    assert book.held() is None
    book.sit("a:56", "honeypot")
    book.sit("b:56", "age")
    assert book.benched("a:56") and book.benched("b:56") and not book.benched("c:56")
    rows = {tid: reason for tid, reason, _ in book.bench_report()}
    assert rows["a:56"] == "honeypot"


# ---- the whole shift, faked collectors ---------------------------------------
# The cycle fixture is deliberately mixed so that every stage of the funnel kills
# something. A uniform fixture leaves the free/trade/chain counters at zero, and an
# accounting assertion over three always-zero counters proves nothing.
YOUNG   = (1, 2)      # -> free kill, "age"
THIN    = (3,)        # -> free kill, "liquidity"
NO_SELL = "T4"        # -> trade kill, "no_sells"
WHALE   = "T5"        # -> chain kill, "top_wallet"


class FakeFomo:
    def token(self): return "x"
    def tokens(self, ids):
        rows = {}
        for i, tid in enumerate(ids):
            rows[tid] = {"symbol": f"T{i}", "mcap": 300_000 + i * 1000,
                         "liq": 1_000 if i in THIN else 48_000,
                         "vol24": 610_000, "price": 0.001, "holders": 310,
                         "change": {300: 0.04, 3600: 0.22, 14400: 0.4, 86400: 0.61},
                         "created": NOW_MS - (5 if i in YOUNG else 42) * 60_000}
        return rows


class FakeDesk:
    def __init__(self): self.shadow, self.reports, self.sent = [], [], []
    def bank(self): return 1000.0
    def read_x(self, h, tid=None, token=None): return None
    def log_shadow(self, o, s, fomo_data=None): self.shadow.append((o, s))
    def report(self, o, s): self.reports.append((o, s))
    def send_to_seats(self, o): self.sent.append(o)


def test_run_once_shadow_never_takes_book(monkeypatch):
    book.release()
    ids = [f"Addr{i}:1399811149" for i in range(12)]
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: (
        {"buys_h1": 540, "sells_h1": 0 if t["ticker"] == NO_SELL else 120,
        "buys_h6": 900, "sells_h6": 400, "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", lambda t, limiter=None: {
        **t, "chain": "solana", "top_10_percent": 30,
        "top_wallet_percent": 0.2 if t["ticker"] == WHALE else 0.02,
        "developer_holding_percentage": 2,
        "gt_score_details": None, "is_honeypot": None,
        "mint_authority": None, "freeze_authority": None,
        "description": "a token", "x_handle": None})
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    assert order is None                                   # shadow never returns an order
    assert book.held() is None                             # and never takes the book
    assert stats["seen"] == 12 and stats["judged"] >= 1
    # each stage kills the tokens built to die there, and names the reason
    assert stats["free"] == {"age": len(YOUNG), "liquidity": len(THIN)}
    assert stats["trade"] == {"no_sells": 1}
    assert stats["chain"] == {"top_wallet": 1}
    # Every token leaves the funnel exactly once: benched on arrival, killed with a
    # named reason at one of the three pre-judge stages, or judged. A judged token
    # then either soft-kills or survives. No token may vanish unaccounted for.
    killed = {k: sum(stats[k].values()) for k in ("free", "trade", "chain", "soft")}
    assert (stats["benched"] + killed["free"] + killed["trade"] + killed["chain"]
            + stats["judged"]) == stats["seen"]
    assert killed["soft"] <= stats["judged"]
    # shadow writes one row per would-be trade and sends nothing to the seats
    assert desk.sent == []
    assert len(desk.shadow) <= 1


def test_held_position_means_no_scan(monkeypatch):
    book.take({"token": {"ticker": "HELD", "address": "a", "network_id": 56}})
    called = []
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (called.append(1) or [], {}))
    order, stats = shift.run_once(FakeFomo(), JUDGE, FakeDesk(), 1000.0)
    assert order is None and stats["held"] == "HELD" and not called
    book.release()


# ---- ops panel state.json -----------------------------------------------------
def test_write_state_produces_expected_keys():
    """write_state should create state.json with all required keys."""
    import desk as desk_mod
    desk = desk_mod.Desk()
    order = {
        "order_id": "2026-01-01T00:00:00Z",
        "token": {"ticker": "TEST", "address": "addr1", "network_id": 56, "chain": "bsc"},
        "size_factor": 0.6,
        "confidence": 0.82,
        "model": "jev-3.1.4"
    }
    stats = {
        "seen": 50,
        "benched": 10,
        "judged": 5,
        "free": {"age": 20, "liquidity": 5},
        "trade": {"no_sells": 2},
        "chain": {"honeypot": 1},
        "soft": {"momentum_already_spent": 3},
        "tokens": [
            {"tid": "addr1:56", "ticker": "T1", "chain": "bsc", "stage": "free",
             "reason": "age", "verdict": "DROP", "mcap_usd": 100000,
             "liquidity_usd": 5000, "age_minutes": 5},
            {"tid": "addr2:56", "ticker": "T2", "chain": "bsc", "stage": "judged",
             "reason": None, "verdict": "PASS", "mcap_usd": 500000,
             "liquidity_usd": 50000, "age_minutes": 42}
        ]
    }
    
    desk.write_state(order, stats)
    
    state_path = desk_mod.OUTBOX / "state.json"
    assert state_path.exists()
    
    import json
    state = json.loads(state_path.read_text())
    
    assert state["demo"] is False
    assert state["mode"] in ("shadow", "live")
    assert "updated_at" in state
    assert "cycle" in state
    assert state["cycle"]["seen"] == 50
    assert state["cycle"]["benched"] == 10
    assert state["cycle"]["judged"] == 5
    assert state["cycle"]["outcome"] in ("SHADOW", "ORDER", "NO TRADE")
    assert "killed" in state["cycle"]
    assert state["cycle"]["killed"]["free"] == {"age": 20, "liquidity": 5}
    assert state["cycle"]["killed"]["trade"] == {"no_sells": 2}
    assert state["cycle"]["killed"]["chain"] == {"honeypot": 1}
    assert state["cycle"]["killed"]["soft"] == {"momentum_already_spent": 3}
    assert "tokens" in state
    assert len(state["tokens"]) == 2
    assert state["tokens"][0]["ticker"] == "T1"
    assert state["tokens"][0]["stage"] == "free"
    assert state["tokens"][0]["reason"] == "age"
    assert state["tokens"][0]["verdict"] == "DROP"
    assert state["tokens"][1]["verdict"] == "PASS"
    assert "held" in state
    assert "bench" in state
    assert state["pick"] == order


def test_write_state_no_trade_cycle():
    """write_state should handle NO TRADE cycles correctly."""
    import desk as desk_mod
    desk = desk_mod.Desk()
    stats = {
        "seen": 100,
        "benched": 50,
        "judged": 0,
        "free": {"age": 30, "liquidity": 10, "volume": 5},
        "trade": {},
        "chain": {},
        "soft": {},
        "tokens": [
            {"tid": "addr1:56", "ticker": "T1", "chain": "bsc", "stage": "free",
             "reason": "age", "verdict": "DROP", "mcap_usd": 50000,
             "liquidity_usd": 2000, "age_minutes": 8}
        ]
    }
    
    desk.write_state(None, stats)
    
    import json
    state = json.loads((desk_mod.OUTBOX / "state.json").read_text())
    
    assert state["demo"] is False
    assert state["cycle"]["outcome"] == "NO TRADE"
    assert state["cycle"]["error"] is None
    assert state["pick"] is None


def test_write_state_held_position():
    """write_state should handle HOLDING cycles correctly."""
    import desk as desk_mod
    desk = desk_mod.Desk()
    stats = {"held": "TICKER", "minutes": 25}
    
    desk.write_state(None, stats)
    
    import json
    state = json.loads((desk_mod.OUTBOX / "state.json").read_text())
    
    assert state["demo"] is False
    assert state["cycle"]["outcome"] == "HOLDING"
    assert state["cycle"]["error"] is None


def test_api_state_returns_file(tmp_path, monkeypatch):
    """GET /api/state should return the state.json file."""
    import os
    os.environ.setdefault("DESK_SECRET", "test-secret-for-ops-panel-test")
    os.environ.setdefault("JUDGE_MOCK", "1")
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    
    import desk as desk_mod
    monkeypatch.setattr(desk_mod, "OUTBOX", tmp_path)
    desk = desk_mod.Desk()
    stats = {
        "seen": 10,
        "benched": 2,
        "judged": 1,
        "free": {"age": 5},
        "trade": {},
        "chain": {},
        "soft": {},
        "tokens": []
    }
    desk.write_state(None, stats)
    
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    
    response = client.get("/api/state")
    assert response.status_code == 200
    data = response.json()
    assert data["demo"] is False
    assert data["cycle"]["seen"] == 10


def test_api_state_404_when_missing(tmp_path, monkeypatch):
    """GET /api/state should return 404 with demo:false when state.json doesn't exist."""
    import os
    os.environ.setdefault("DESK_SECRET", "test-secret-for-ops-panel-test")
    os.environ.setdefault("JUDGE_MOCK", "1")
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    
    import desk as desk_mod
    monkeypatch.setattr(desk_mod, "OUTBOX", tmp_path)
    state_path = tmp_path / "state.json"
    if state_path.exists():
        state_path.unlink()
    
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    
    response = client.get("/api/state")
    assert response.status_code == 404
    data = response.json()
    assert data["demo"] is False
    assert data["tokens"] == []
    assert data["cycle"] is None


def test_ops_endpoint_returns_html():
    """GET /ops should return 200 and HTML."""
    import os
    os.environ.setdefault("DESK_SECRET", "test-secret-for-ops-panel-test")
    os.environ.setdefault("JUDGE_MOCK", "1")
    
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    
    response = client.get("/ops")
    assert response.status_code == 200
    assert "JEV Ops" in response.text
    assert "/ops/summary" in response.text
    assert 'id="desk"' in response.text
    assert "Judge scores" in response.text
    assert "young_trail" in response.text


def test_ops_panel_has_summary_and_judge_health():
    """Ops panel should include summary box and judge health display."""
    import os
    os.environ.setdefault("DESK_SECRET", "test-secret-for-ops-panel-test")
    os.environ.setdefault("JUDGE_MOCK", "1")
    
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    
    response = client.get("/ops")
    assert response.status_code == 200
    html = response.text
    
    assert 'id="desk"' in html
    assert 'id="judge-t"' in html
    assert 'id="yt"' in html
    assert 'id="briefing"' in html
    assert "/ops/summary" in html
    assert "/ops/briefing?compact=1" in html
    assert "prefers-color-scheme" in html
    assert "fomo-fix-button" in html


def test_run_once_records_tokens(monkeypatch):
    """run_once should populate stats['tokens'] with stage/reason for each token."""
    book.release()
    
    # Use unique addresses to avoid collisions with bench entries from earlier tests
    import time
    unique_suffix = int(time.time() * 1000000) % 1000000
    
    # Create tokens with unique addresses and ensure at least one passes free_kill
    def fake_shortlist(fomo, id_list):
        tokens = []
        for i in range(5):
            addr = f"UniqueAddr{i}_{unique_suffix}"
            tid = f"{addr}:1399811149"
            # Make token 0-1 fail free kill (young), token 2+ pass
            t = tok(i, tid=tid, addr=addr, age_minutes=10 if i < 2 else 42)
            tokens.append(t)
        return tokens
    
    ids = [f"UniqueAddr{i}_{unique_suffix}:1399811149" for i in range(5)]
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(collect, "shortlist", fake_shortlist)
    
    # Monkeypatch book.benched to ensure tokens aren't skipped
    monkeypatch.setattr(book, "benched", lambda tid: False)
    
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: (
        {"buys_h1": 540, "sells_h1": 120,
        "buys_h6": 900, "sells_h6": 400, "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", lambda t, limiter=None: {
        **t, "chain": "solana", "top_10_percent": 30,
        "top_wallet_percent": 0.02, "developer_holding_percentage": 2,
        "gt_score_details": None, "is_honeypot": None,
        "mint_authority": None, "freeze_authority": None,
        "description": "a token", "x_handle": None})
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    assert "tokens" in stats
    assert len(stats["tokens"]) >= 1, f"Expected at least 1 token record, got {len(stats['tokens'])}"
    
    for t in stats["tokens"]:
        assert "tid" in t or "ticker" in t
        assert "stage" in t
        assert "verdict" in t
        if t["verdict"] == "DROP":
            assert t["reason"] is not None


def test_run_once_pick_failure_scrubs_secrets_and_no_unbound_error(monkeypatch, caplog):
    """Pick failure should return (None, stats) without UnboundLocalError and scrub secrets."""
    import logging
    
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    # Test both one and two survivors (pick should fail for both)
    for num_survivors in [1, 2]:
        caplog.clear()
        
        def fake_shortlist(fomo, id_list):
            tokens = []
            for i in range(num_survivors):
                tid = f"SurvivorToken{i}:1399811149"
                t = tok(i, tid=tid, addr=f"SurvivorToken{i}", age_minutes=45)
                tokens.append(t)
            return tokens
        
        ids = [f"SurvivorToken{i}:1399811149" for i in range(num_survivors)]
        
        monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
        monkeypatch.setattr(collect, "shortlist", fake_shortlist)
        monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: (
            {"buys_h1": 540, "sells_h1": 120,
            "buys_h6": 900, "sells_h6": 400, "trades_h24": 4000}, 'ok'))
        monkeypatch.setattr(shift, "dossier", lambda t, limiter=None: {
            **t, "chain": "solana", "top_10_percent": 30,
            "top_wallet_percent": 0.02, "developer_holding_percentage": 2,
            "gt_score_details": None, "is_honeypot": None,
            "mint_authority": None, "freeze_authority": None,
            "description": "a token", "x_handle": None})
        
        # Monkeypatch pick to raise exception with secret
        def fake_pick_with_secret(judge, survivors):
            raise Exception("boom api-key=SECRET999")
        
        monkeypatch.setattr(shift, "pick", fake_pick_with_secret)
        
        # Use a fake judge that always passes
        def fake_judge(question_set, state):
            return {"model": "test", "answers": {
                "concentration_is_exit_risk": {"type": "noul", "noul": 0.3},
                "momentum_already_spent": {"type": "noul", "noul": 0.3}
            }, "usage": {}}
        
        desk = FakeDesk()
        
        # Should return (None, stats) without raising UnboundLocalError
        with caplog.at_level(logging.WARNING):
            order, stats = shift.run_once(FakeFomo(), fake_judge, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
        
        assert order is None, f"Expected None order on pick failure, got {order}"
        assert stats is not None, "Expected stats dict even on pick failure"
        
        # Check logs contain NO PICK message
        pick_failed_logs = [r for r in caplog.records if "NO PICK" in r.message]
        assert len(pick_failed_logs) > 0, \
            f"Expected 'NO PICK' in logs for {num_survivors} survivors. All logs: {[r.message for r in caplog.records]}"
        
        # Check logs do NOT contain the secret
        all_log_text = " ".join([r.message for r in caplog.records])
        assert "SECRET999" not in all_log_text, \
            f"Secret leaked in logs for {num_survivors} survivors: {all_log_text}"
        
        # Check logs contain scrubbed version
        assert "REDACTED" in all_log_text or "boom" in all_log_text, \
            f"Expected scrubbed error in logs for {num_survivors} survivors: {all_log_text}"


def test_normalise_and_clean_handle():
    m = FakeFomo().tokens(["Addr1:56"])["Addr1:56"]
    t = collect.normalise("Addr1:56", m)
    assert t["net"] == 56 and 41 < t["age_minutes"] < 44 and t["change"]["1h"] == 0.22
    assert collect.clean_handle("LuffyX100X/status/2102659581109272876") == "LuffyX100X"
    assert collect.clean_handle("@good_handle?x=1") == "good_handle"
    assert collect.clean_handle("https://x.com/foo") is None
    assert collect.clean_handle("") is None


def test_universe_429_stops_network_not_all():
    """GeckoTerminal 429 should stop pagination for that network only, not wipe entire universe."""
    import requests
    
    # Mock responses: trending first, then new_pools
    # solana trending succeeds, bsc trending 429, then new_pools
    mock_responses = [
        # Solana trending - success
        Mock(status_code=200, headers={}, json=lambda: {
            "data": [
                {"relationships": {"base_token": {"data": {"id": "solana_addr_t1"}}}}
            ]
        }),
        # BSC trending - 429 (with retry, so 2 attempts)
        Mock(status_code=429, headers={"Retry-After": "0.01"}),
        Mock(status_code=429, headers={}),  # Still 429 after retry
        # Solana new_pools page 1 - success
        Mock(status_code=200, headers={}, json=lambda: {
            "data": [
                {"relationships": {"base_token": {"data": {"id": "solana_addr1"}}}}
            ]
        }),
        # Solana new_pools page 2 - 429
        Mock(status_code=429, headers={}),
        # BSC new_pools page 1 - success (should still run despite trending 429)
        Mock(status_code=200, headers={}, json=lambda: {
            "data": [
                {"relationships": {"base_token": {"data": {"id": "bsc_addr2"}}}}
            ]
        }),
        # BSC new_pools page 2 - success
        Mock(status_code=200, headers={}, json=lambda: {
            "data": [
                {"relationships": {"base_token": {"data": {"id": "bsc_addr3"}}}}
            ]
        })
    ]
    
    original_get = requests.get
    mock_get = Mock(side_effect=mock_responses)
    requests.get = mock_get
    
    try:
        limiter, _ = _paced_limiter(calls_per_min=30)
        ids, gt_cache = collect.universe(nets=("solana", "bsc"), pages=2, include_trending=True, limiter=limiter)
        
        # Should have IDs from solana trending, solana page 1, and both bsc new_pools pages
        # (bsc trending 429'd, solana new_pools stopped at page 2 due to 429, but BSC new_pools continued)
        assert len(ids) >= 2
        assert any("1399811149" in tid for tid in ids)  # Solana network
        assert any("56" in tid for tid in ids)  # BSC network
        
        # Verify we collected from multiple networks despite 429 on one
        networks = {tid.split(":")[1] for tid in ids}
        assert len(networks) >= 2 or len(ids) >= 1  # Got IDs from at least one network
    finally:
        requests.get = original_get


def test_universe_robinhood_one_page():
    """robinhood fetches only page 1 to avoid 429 rate limits, other networks fetch 2 pages."""
    import requests
    
    # Mock responses: 2 pages for solana, 2 for bsc, 1 for robinhood
    mock_responses = [
        # Solana page 1
        Mock(status_code=200, json=lambda: {
            "data": [{"relationships": {"base_token": {"data": {"id": "solana_addr1"}}}}]
        }),
        # Solana page 2
        Mock(status_code=200, json=lambda: {
            "data": [{"relationships": {"base_token": {"data": {"id": "solana_addr2"}}}}]
        }),
        # BSC page 1
        Mock(status_code=200, json=lambda: {
            "data": [{"relationships": {"base_token": {"data": {"id": "bsc_addr3"}}}}]
        }),
        # BSC page 2
        Mock(status_code=200, json=lambda: {
            "data": [{"relationships": {"base_token": {"data": {"id": "bsc_addr4"}}}}]
        }),
        # Robinhood page 1 (only page requested)
        Mock(status_code=200, json=lambda: {
            "data": [{"relationships": {"base_token": {"data": {"id": "robinhood_addr5"}}}}]
        })
    ]
    
    original_get = requests.get
    call_info = []  # Track (url, params) tuples
    
    def tracked_get(url, **kwargs):
        call_info.append((url, kwargs.get("params", {})))
        return mock_responses.pop(0)
    
    requests.get = tracked_get
    
    try:
        ids, gt_cache = collect.universe(nets=("solana", "bsc", "robinhood"), pages=2, include_trending=False)
        
        # Should have collected 5 IDs total (2 solana + 2 bsc + 1 robinhood)
        assert len(ids) == 5
        
        # Verify all three networks represented
        assert any("1399811149" in tid for tid in ids)  # Solana
        assert any("56" in tid for tid in ids)          # BSC
        assert any("4663" in tid for tid in ids)        # Robinhood
        
        # Verify call pattern: 2 solana pages, 2 bsc pages, 1 robinhood page
        assert len(call_info) == 5
        
        # Count calls per network
        solana_calls = [c for c in call_info if "solana" in c[0]]
        bsc_calls = [c for c in call_info if "bsc" in c[0]]
        robinhood_calls = [c for c in call_info if "robinhood" in c[0]]
        
        assert len(solana_calls) == 2
        assert len(bsc_calls) == 2
        assert len(robinhood_calls) == 1
        
        # Verify robinhood only requested page 1
        assert robinhood_calls[0][1]["page"] == 1
        
        # Verify other networks requested both pages
        assert solana_calls[0][1]["page"] == 1
        assert solana_calls[1][1]["page"] == 2
        assert bsc_calls[0][1]["page"] == 1
        assert bsc_calls[1][1]["page"] == 2
    finally:
        requests.get = original_get


def test_universe_includes_trending_pools():
    """universe should fetch trending_pools in addition to new_pools and dedupe."""
    import requests
    
    # Mock responses in correct order: sol new p1, sol new p2, sol trending p1, sol trending p2, bsc trending, bsc new p1, bsc new p2
    mock_responses = [
        # Solana new_pools page 1 (highest priority)
        Mock(status_code=200, headers={}, json=lambda: {
            "data": [
                {"relationships": {"base_token": {"data": {"id": "solana_new1"}}}},
                {"relationships": {"base_token": {"data": {"id": "solana_new2"}}}}
            ]
        }),
        # Solana new_pools page 2
        Mock(status_code=200, headers={}, json=lambda: {
            "data": [{"relationships": {"base_token": {"data": {"id": "solana_new3"}}}}]
        }),
        # Solana trending_pools page 1
        Mock(status_code=200, headers={}, json=lambda: {
            "data": [
                {"relationships": {"base_token": {"data": {"id": "solana_trend1"}}}},
                {"relationships": {"base_token": {"data": {"id": "solana_new1"}}}}  # duplicate
            ]
        }),
        # Solana trending_pools page 2 (after robinhood trending priority)
        Mock(status_code=200, headers={}, json=lambda: {
            "data": [{"relationships": {"base_token": {"data": {"id": "solana_trend2"}}}}]
        }),
        # BSC trending_pools page 1
        Mock(status_code=200, headers={}, json=lambda: {
            "data": [{"relationships": {"base_token": {"data": {"id": "bsc_trend1"}}}}]
        }),
        # BSC new_pools page 1
        Mock(status_code=200, headers={}, json=lambda: {
            "data": [{"relationships": {"base_token": {"data": {"id": "bsc_new1"}}}}]
        }),
        # BSC new_pools page 2
        Mock(status_code=200, headers={}, json=lambda: {
            "data": [{"relationships": {"base_token": {"data": {"id": "bsc_new2"}}}}]
        })
    ]
    
    original_get = requests.get
    call_info = []
    
    def tracked_get(url, **kwargs):
        call_info.append((url, kwargs.get("params", {})))
        return mock_responses.pop(0)
    
    requests.get = tracked_get
    
    try:
        ids, gt_cache = collect.universe(nets=("solana", "bsc"), pages=2, include_trending=True)
        
        # Should have 8 unique IDs (3 solana new + 2 solana trend + 1 bsc trend + 2 bsc new - 1 duplicate)
        assert len(ids) == 8, f"Expected 8 unique tokens, got {len(ids)}: {ids}"
        
        # Verify no duplicates
        assert len(ids) == len(set(ids))
        
        # Verify solana trending IDs are included (both pages)
        assert "trend1:1399811149" in ids, f"Expected solana trend1 in {ids}"
        assert "trend2:1399811149" in ids, f"Expected solana trend2 in {ids}"
        assert "trend1:56" in ids, f"Expected bsc trend1 in {ids}"
        
        # Verify we made calls to both new_pools and trending_pools
        new_pool_calls = [c for c in call_info if "new_pools" in c[0]]
        trending_calls = [c for c in call_info if "trending_pools" in c[0]]
        
        assert len(new_pool_calls) == 4, f"Expected 4 new_pools calls, got {len(new_pool_calls)}"  # 2 solana + 2 bsc
        assert len(trending_calls) == 3, f"Expected 3 trending_pools calls, got {len(trending_calls)}"  # 2 solana + 1 bsc
        
        # Verify total call count: 4 new_pools + 3 trending_pools = 7
        assert len(call_info) == 7, f"Expected 7 total calls, got {len(call_info)}"
    finally:
        requests.get = original_get


def test_universe_trending_429_continues():
    """GeckoTerminal 429 on trending_pools should skip that network's trending but continue."""
    import requests
    
    original_get = requests.get
    
    def mock_get(url, **kwargs):
        """Return different responses based on URL."""
        if "solana" in url and "new_pools" in url:
            page = kwargs.get("params", {}).get("page", 1)
            if page == 1:
                return Mock(status_code=200, json=lambda: {
                    "data": [{"relationships": {"base_token": {"data": {"id": "solana_new1"}}}}]
                })
            elif page == 2:
                return Mock(status_code=200, json=lambda: {
                    "data": [{"relationships": {"base_token": {"data": {"id": "solana_new2"}}}}]
                })
        elif "solana" in url and "trending_pools" in url:
            return Mock(status_code=429)  # Solana trending gets 429
        elif "bsc" in url and "new_pools" in url:
            page = kwargs.get("params", {}).get("page", 1)
            if page == 1:
                return Mock(status_code=200, json=lambda: {
                    "data": [{"relationships": {"base_token": {"data": {"id": "bsc_new1"}}}}]
                })
            elif page == 2:
                return Mock(status_code=200, json=lambda: {
                    "data": [{"relationships": {"base_token": {"data": {"id": "bsc_new2"}}}}]
                })
        elif "bsc" in url and "trending_pools" in url:
            return Mock(status_code=200, json=lambda: {
                "data": [{"relationships": {"base_token": {"data": {"id": "bsc_trend1"}}}}]
            })
        return Mock(status_code=404, json=lambda: {})
    
    requests.get = mock_get
    
    try:
        limiter, _ = _paced_limiter(calls_per_min=30)
        ids, gt_cache = collect.universe(nets=("solana", "bsc"), pages=2, include_trending=True, limiter=limiter)
        
        # Should have collected: 2 solana new + 2 bsc new + 1 bsc trending = 5
        # (solana trending skipped due to 429)
        assert len(ids) == 5
        
        # Verify we have IDs from both networks' new_pools
        assert any("1399811149" in tid for tid in ids)  # Solana
        assert any("56" in tid for tid in ids)          # BSC
        
        # Verify BSC trending was included (despite solana trending 429)
        # "bsc_trend1" becomes "trend1:56" after splitting on "_"
        assert any("trend1:56" in tid for tid in ids)
        
        # Verify solana trending is NOT included (429)
        # Solana should only have new1 and new2, no trend
        solana_ids = [tid for tid in ids if "1399811149" in tid]
        assert len(solana_ids) == 2
        assert all("new" in tid for tid in solana_ids)
    finally:
        requests.get = original_get


def test_free_kill_logs_fomo_metrics(monkeypatch, caplog):
    """free_kill should log age_minutes, liquidity_usd, volume_usd, mcap_usd on every kill."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"LogTestAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_low_liq(fomo, id_list):
        return [tok(0, tid=tid, addr="LogTestAddr1", age_minutes=42, liquidity_usd=5000,
                    volume_h24=20000, mcap_usd=100000)]
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_low_liq)
    
    desk = FakeDesk()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    # Find the free kill log line
    free_logs = [r for r in caplog.records if "free tid=" in r.message and "reason=liquidity" in r.message]
    assert len(free_logs) >= 1, f"Expected free kill log, got: {[r.message for r in caplog.records]}"
    
    log_msg = free_logs[0].message
    assert "age_minutes=42" in log_msg or "age_minutes=42.0" in log_msg
    assert "liquidity_usd=5000" in log_msg or "liquidity_usd=5000.0" in log_msg
    assert "volume_usd=20000" in log_msg or "volume_usd=20000.0" in log_msg
    assert "mcap_usd=100000" in log_msg or "mcap_usd=100000.0" in log_msg


def test_free_pass_logs_fomo_metrics(monkeypatch, caplog):
    """free pass should log age_minutes, liquidity_usd, volume_usd, mcap_usd."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"PassTestAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_pass(fomo, id_list):
        # Token that passes free_kill (good values)
        return [tok(0, tid=tid, addr="PassTestAddr1", age_minutes=42, liquidity_usd=50000,
                    volume_h24=100000, mcap_usd=500000)]
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_pass)
    # Make it fail at trade stage so we can see the free pass log
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 0, "sells_h1": 0, 
                                                            "buys_h6": 0, "sells_h6": 0, 
                                                            "trades_h24": 0}, 'ok'))
    
    desk = FakeDesk()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    # Find the free pass log line
    free_logs = [r for r in caplog.records if "free tid=" in r.message and "reason=pass" in r.message]
    assert len(free_logs) >= 1, f"Expected free pass log, got: {[r.message for r in caplog.records]}"
    
    log_msg = free_logs[0].message
    assert "age_minutes=42" in log_msg or "age_minutes=42.0" in log_msg
    assert "liquidity_usd=50000" in log_msg or "liquidity_usd=50000.0" in log_msg
    assert "volume_usd=100000" in log_msg or "volume_usd=100000.0" in log_msg
    assert "mcap_usd=500000" in log_msg or "mcap_usd=500000.0" in log_msg


def test_free_kill_logs_none_values(monkeypatch, caplog):
    """free_kill logs should preserve None for missing data, not convert to 0."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"NoneTestAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_none(fomo, id_list):
        # Token with None liquidity (should be no_liq kill)
        return [tok(0, tid=tid, addr="NoneTestAddr1", age_minutes=42, liquidity_usd=None,
                    volume_h24=None, mcap_usd=None)]
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_none)
    
    desk = FakeDesk()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    # Find the free kill log line
    free_logs = [r for r in caplog.records if "free tid=" in r.message and "reason=no_liq" in r.message]
    assert len(free_logs) >= 1, f"Expected free no_liq log, got: {[r.message for r in caplog.records]}"
    
    log_msg = free_logs[0].message
    # None values should appear as "None" in the log, not "0" or "0.0"
    assert "liquidity_usd=None" in log_msg
    assert "volume_usd=None" in log_msg
    assert "mcap_usd=None" in log_msg


# ---- rate limiter -------------------------------------------------------------
def test_rate_limiter_spends_budget():
    """GTRateLimiter should track and spend tokens using rolling window."""
    fake_time = [0.0]
    limiter = collect.GTRateLimiter(calls_per_min=10, time_fn=lambda: fake_time[0])
    assert limiter.available() == 10
    
    assert limiter.spend(3) is True
    assert limiter.available() == 7
    
    assert limiter.spend(7) is True
    assert limiter.available() == 0
    
    # Budget exhausted
    assert limiter.spend(1) is False
    assert limiter.available() == 0
    
    # After 60.1s, oldest calls expire and budget refills
    fake_time[0] += 60.1
    assert limiter.available() == 10


def test_rate_limiter_reserve():
    """GTRateLimiter.reserve() should reserve budget for priority use."""
    fake_time = [0.0]
    limiter = collect.GTRateLimiter(calls_per_min=10, time_fn=lambda: fake_time[0])
    reserved = limiter.reserve(3)
    assert reserved == 3
    
    # Non-priority calls can't use reserved slots
    for _ in range(7):
        assert limiter.spend(1, priority=False) is True
    assert limiter.spend(1, priority=False) is False  # 7 spent, 3 reserved
    
    # Priority calls can use reserved slots
    assert limiter.spend(1, priority=True) is True
    assert limiter.available() == 2


def test_rolling_window_no_more_than_n_calls_per_minute():
    """GTRateLimiter enforces no more than N calls in any 60s window."""
    fake_time = [0.0]
    limiter = collect.GTRateLimiter(calls_per_min=5, time_fn=lambda: fake_time[0])
    
    # Make 5 calls at different times
    for i in range(5):
        fake_time[0] = float(i)
        assert limiter.spend(1) is True
    assert limiter.available() == 0
    
    # Can't make more calls until window expires
    assert limiter.spend(1) is False
    
    # Advance to t=30 - oldest call at t=0 still in window (30-60=-30, so cutoff=0-60=-60)
    fake_time[0] = 30.0
    assert limiter.spend(1) is False
    
    # Advance to 60.1s - first call at t=0 expired (cutoff = 60.1-60 = 0.1, so 0.0 < 0.1)
    fake_time[0] = 60.1
    assert limiter.available() == 1
    assert limiter.spend(1) is True
    
    # Advance to 61s - second call at t=1 expired (cutoff = 61-60 = 1, so 1.0 < 1 is False, but next check)
    fake_time[0] = 61.1
    assert limiter.available() == 1


def test_rate_limiter_trending_before_new_pools():
    """universe should prioritize solana new_pools p1-2, then solana trending p1-2, then other networks' trending."""
    import requests
    
    original_get = requests.get
    call_order = []
    
    def mock_get(url, **kwargs):
        if "solana" in url and "new_pools" in url:
            page = kwargs.get("params", {}).get("page", 1)
            call_order.append(f"solana_new_p{page}")
        elif "solana" in url and "trending_pools" in url:
            page = kwargs.get("params", {}).get("page", 1)
            call_order.append(f"solana_trending_p{page}")
        elif "bsc" in url and "trending_pools" in url:
            call_order.append("bsc_trending")
        elif "bsc" in url and "new_pools" in url:
            call_order.append("bsc_new")
        return Mock(status_code=200, json=lambda: {
            "data": [{"relationships": {"base_token": {"data": {"id": f"solana_tok1"}}}}]
        })
    
    requests.get = mock_get
    
    try:
        limiter, fake_time = _paced_limiter(calls_per_min=10)
        collect.universe(nets=("solana", "bsc"), pages=2, include_trending=True, limiter=limiter)
        
        # Order: sol_new p1, sol_new p2, sol_trending p1, sol_trending p2, bsc_trending, bsc_new...
        assert call_order[0] == "solana_new_p1", f"Expected solana_new_p1 first, got {call_order}"
        assert call_order[1] == "solana_new_p2", f"Expected solana_new_p2 second, got {call_order}"
        assert call_order[2] == "solana_trending_p1", f"Expected solana_trending_p1 third, got {call_order}"
        assert call_order[3] == "solana_trending_p2", f"Expected solana_trending_p2 fourth, got {call_order}"
        assert call_order[4] == "bsc_trending", f"Expected bsc_trending fifth, got {call_order}"
        assert "bsc_new" in call_order[5:], f"Expected bsc_new after trending, got {call_order}"
    finally:
        requests.get = original_get


def test_rate_limiter_429_retry_with_backoff():
    """GT 429 on solana trending should retry once with backoff."""
    import requests
    
    original_get = requests.get
    call_count = [0]
    
    def mock_get(url, **kwargs):
        if "trending_pools" in url and "solana" in url:
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: 429 with Retry-After
                return Mock(status_code=429, headers={"Retry-After": "0.1"})
            else:
                # Second call: success
                return Mock(status_code=200, json=lambda: {"data": []})
        return Mock(status_code=200, json=lambda: {"data": []})
    
    requests.get = mock_get
    
    try:
        limiter, fake_time = _paced_limiter(calls_per_min=10)
        collect.universe(nets=("solana",), pages=1, include_trending=True, limiter=limiter)
        
        # Should have retried (2 calls total for solana trending: 429 + retry)
        assert call_count[0] >= 2, f"Expected retry, got {call_count[0]} calls"
    finally:
        requests.get = original_get


def test_universe_budget_5_three_networks(caplog):
    """With budget=5 and 3 networks, should fetch exactly sol_new p1, sol_new p2, sol_trending p1, rh_trending, sol_trending p2."""
    import requests
    import logging
    
    caplog.set_level(logging.INFO)
    
    original_get = requests.get
    call_order = []
    
    def mock_get(url, **kwargs):
        if "solana" in url and "new_pools" in url:
            page = kwargs.get("params", {}).get("page", 1)
            call_order.append(f"solana_new_p{page}")
        elif "solana" in url and "trending_pools" in url:
            page = kwargs.get("params", {}).get("page", 1)
            call_order.append(f"solana_trending_p{page}")
        elif "bsc" in url and "trending_pools" in url:
            call_order.append("bsc_trending")
        elif "robinhood" in url and "trending_pools" in url:
            call_order.append("robinhood_trending")
        elif "bsc" in url and "new_pools" in url:
            call_order.append("bsc_new")
        elif "robinhood" in url and "new_pools" in url:
            call_order.append("robinhood_new")
        return Mock(status_code=200, json=lambda: {"data": []})
    
    requests.get = mock_get
    
    try:
        limiter, fake_time = _paced_limiter(calls_per_min=30)
        limiter.set_universe_budget(5)  # Default budget
        
        ids, _ = collect.universe(nets=("solana", "bsc", "robinhood"), pages=2, include_trending=True, limiter=limiter)
        
        # Should have stopped at exactly 5 calls
        assert len(call_order) == 5, f"Expected exactly 5 calls with budget=5, got {len(call_order)}: {call_order}"
        
        # Verify exact order per spec: sol_new p1, sol_new p2, sol_trending p1, robinhood_trending, sol_trending p2
        # BSC trending drops at budget=5 (comes after sol_trending p2 which exhausts budget)
        assert call_order == [
            "solana_new_p1",
            "solana_new_p2",
            "solana_trending_p1",
            "robinhood_trending",
            "solana_trending_p2"
        ], f"Expected exact sequence, got {call_order}"
        
        # Verify budget exhaustion log appeared
        exhaustion_logs = [r for r in caplog.records if "budget exhausted after" in r.message.lower()]
        assert len(exhaustion_logs) > 0, "Expected budget exhaustion log"
        assert "after 5 pages" in exhaustion_logs[0].message.lower(), f"Expected 'after 5 pages', got {exhaustion_logs[0].message}"
    finally:
        requests.get = original_get


def test_rate_limiter_dossier_priority():
    """Dossier calls (priority=True) can use reserved slots."""
    fake_time = [0.0]
    limiter = collect.GTRateLimiter(calls_per_min=10, time_fn=lambda: fake_time[0])
    limiter.reserve(3)
    
    # Use up non-priority budget (7 calls)
    for _ in range(7):
        assert limiter.spend(1, priority=False) is True
    
    # Non-priority exhausted
    assert limiter.spend(1, priority=False) is False
    
    # Priority can still make 3 calls
    for _ in range(3):
        assert limiter.spend(1, priority=True) is True
    
    # Now fully exhausted
    assert limiter.spend(1, priority=True) is False


def test_universe_uses_limiter():
    """universe should spend budget from limiter and skip pages when exhausted."""
    import requests
    
    original_get = requests.get
    call_count = [0]
    
    def mock_get(url, **kwargs):
        call_count[0] += 1
        return Mock(status_code=200, json=lambda: {
            "data": [{"relationships": {"base_token": {"data": {"id": f"solana_tok{call_count[0]}"}}}}]
        })
    
    requests.get = mock_get
    
    try:
        # Universe budget of 3 pages (rolling window will wait, not hard-stop)
        limiter, _ = _paced_limiter(calls_per_min=10)
        limiter.set_universe_budget(3)
        ids, gt_cache = collect.universe(nets=("solana",), pages=5, include_trending=True, limiter=limiter)
        
        # Should have made at most 3 calls (universe budget exhausted)
        assert call_count[0] <= 3
    finally:
        requests.get = original_get


def test_dossier_429_raises_retry_needed(monkeypatch):
    """dossier on GT 429 should raise DossierRetryNeeded, not RuntimeError."""
    import requests
    monkeypatch.setattr(collect, "sol_top_wallet", _pass_sol_wallet)
    
    original_get = requests.get
    mock_resp = Mock(status_code=429)
    requests.get = lambda *args, **kwargs: mock_resp
    
    try:
        t = tok(1)
        try:
            collect.dossier(t)
            assert False, "should have raised DossierRetryNeeded"
        except collect.DossierRetryNeeded as e:
            assert "429" in str(e)
    finally:
        requests.get = original_get


def test_dossier_budget_exhausted_raises_retry(monkeypatch):
    """dossier with exhausted limiter should raise DossierRetryNeeded without calling GT."""
    import requests
    monkeypatch.setattr(collect, "sol_top_wallet", _pass_sol_wallet)
    
    original_get = requests.get
    call_count = [0]

    def fake_get(*args, **kwargs):
        call_count[0] += 1
        return Mock(status_code=200, json=lambda: {"data": {"attributes": {}}})

    requests.get = fake_get
    
    try:
        # Frozen clock: wait cannot free a slot, so spend fails and GT is not called
        limiter = collect.GTRateLimiter(calls_per_min=5, time_fn=lambda: 0.0, sleep_fn=lambda d: None)
        for _ in range(5):
            limiter.spend(1, priority=True)
        
        t = tok(1)
        
        try:
            collect.dossier(t, limiter=limiter)
            assert False, "should have raised DossierRetryNeeded"
        except collect.DossierRetryNeeded as e:
            assert "budget" in str(e).lower()
        
        # Should NOT have called GT (budget check happens first)
        assert call_count[0] == 0
    finally:
        requests.get = original_get


def test_run_once_requeues_on_dossier_retry(monkeypatch, caplog):
    """When dossier raises DossierRetryNeeded, token should be requeued via defer."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"RetryAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_pass(fomo, id_list):
        return [tok(0, tid=tid, addr="RetryAddr1", age_minutes=20, liquidity_usd=50000,
                    volume_h24=100000, mcap_usd=500000)]
    
    def fake_dossier_429(t, limiter=None):
        raise collect.DossierRetryNeeded(f"GT 429 for {t['ticker']}")
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(collect, "shortlist", fake_shortlist_pass)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400, 
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_429)
    
    desk = FakeDesk()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=3)
    
    # Should have requeued the token
    assert stats["requeued"] == 1
    
    # Check defer table has the token
    defer_rows = book.DB.execute("SELECT tid FROM defer WHERE tid=?", (tid,)).fetchall()
    assert len(defer_rows) == 1, f"Expected token in defer table, got {len(defer_rows)} rows"
    
    # Check log
    assert any("dossier retry needed" in rec.message and "T0" in rec.message 
               for rec in caplog.records), "Expected dossier retry log"


def test_requeued_token_retried_next_cycle(monkeypatch):
    """Young token requeued after in-cycle 429 retry should be retried next cycle."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"RetryAddr2:{1399811149}"
    
    # Cycle 1: young token hits 429 twice (initial + in-cycle retry), then requeued
    def fake_shortlist_cycle1(fomo, id_list):
        return [tok(0, tid=tid, addr="RetryAddr2", age_minutes=20, liquidity_usd=50000,
                    volume_h24=100000, mcap_usd=500000)]
    
    dossier_calls = [0]
    def fake_dossier_cycle1(t, limiter=None):
        dossier_calls[0] += 1
        raise collect.DossierRetryNeeded(f"GT 429 for {t['ticker']}")
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([tid], {}))
    monkeypatch.setattr(collect, "shortlist", fake_shortlist_cycle1)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400, 
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_cycle1)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=3)
    
    assert stats["requeued"] == 1
    # Young tokens get initial attempt + one in-cycle retry before defer
    assert dossier_calls[0] == 2, f"Expected 2 dossier calls in cycle 1, got {dossier_calls[0]}"
    
    # Cycle 2: universe returns empty, but defer_due should include our token
    def fake_dossier_cycle2(t, limiter=None):
        dossier_calls[0] += 1
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None, 
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a token", "x_handle": None}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([], {}))  # Empty universe
    monkeypatch.setattr(shift, "dossier", fake_dossier_cycle2)
    
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=3)
    
    # Cycle 2 adds one successful dossier call (2 from cycle 1 + 1)
    assert dossier_calls[0] == 3, f"Expected dossier called 3 times total, got {dossier_calls[0]}"
    
    # Token should no longer be in defer (either passed or failed for real)
    defer_rows = book.DB.execute("SELECT tid FROM defer WHERE tid=?", (tid,)).fetchall()
    assert len(defer_rows) == 0, f"Token should be cleared from defer after retry"


def test_limiter_prioritizes_dossier_over_universe(monkeypatch, caplog):
    """Limiter should reserve budget for dossiers, limiting universe pagination."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    # Use fake clock to avoid real sleeping
    fake_time = [0.0]
    fake_limiter = collect.GTRateLimiter(calls_per_min=10, min_spacing_sec=1.0,
                                          time_fn=lambda: fake_time[0],
                                          sleep_fn=lambda d: fake_time.__setitem__(0, fake_time[0] + d))
    
    # Track GT calls
    gt_calls = []
    tid1 = f"PrioAddr1:{1399811149}"
    tid2 = f"PrioAddr2:{1399811149}"
    
    def spy_universe(nets=("solana", "bsc", "robinhood"), pages=2, include_trending=True, limiter=None, fomo=None):
        if limiter:
            gt_calls.append(("universe", limiter.available()))
        return ([tid1, tid2], {})
    
    def spy_dossier(t, limiter=None):
        if limiter:
            limiter.wait_if_needed(priority=True)
            limiter.spend(1, priority=True)
            gt_calls.append(("dossier", limiter.available()))
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "x_handle": None, "description": "test", "mint_authority": None,
                "freeze_authority": None, "is_honeypot": None, "gt_score_details": None,
                "developer_holding_percentage": None}
    
    monkeypatch.setattr(shift, "universe", spy_universe)
    monkeypatch.setattr(shift, "dossier", spy_dossier)
    monkeypatch.setattr(shift, "_now", lambda: fake_time[0])
    monkeypatch.setattr(shift, "_sleep", lambda d: fake_time.__setitem__(0, fake_time[0] + d))
    
    # Create tokens that pass free and trade
    tid1 = f"PrioAddr1:{1399811149}"
    tid2 = f"PrioAddr2:{1399811149}"
    
    def fake_shortlist_prio(fomo, id_list):
        tokens = []
        if tid1 in id_list:
            tokens.append(tok(0, tid=tid1, addr="PrioAddr1", age_minutes=20, liquidity_usd=50000,
                            volume_h24=100000, mcap_usd=500000))
        if tid2 in id_list:
            tokens.append(tok(1, tid=tid2, addr="PrioAddr2", age_minutes=25, liquidity_usd=60000,
                            volume_h24=120000, mcap_usd=600000))
        return tokens
    
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_prio)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400, 
                                                            "trades_h24": 4000}, 'ok'))
    
    desk = FakeDesk()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        # Reserve 3 for dossiers, leaving 7 for universe; use fake limiter
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, 
                                       gt_dossier_reserve=3, gt_limiter=fake_limiter)
    
    # Check that reserve was logged
    assert any("reserved 3 for dossiers" in rec.message for rec in caplog.records), \
        "Expected reserve log"
    
    # Verify gt_calls shows universe ran before dossiers
    assert len(gt_calls) > 0
    assert gt_calls[0][0] == "universe", "Universe should be called first"
    
    # Check that reserve was logged
    assert any("reserved 3 for dossiers" in rec.message for rec in caplog.records), \
        "Expected reserve log"
    
    # Verify gt_calls shows universe ran before dossiers
    assert len(gt_calls) > 0
    assert gt_calls[0][0] == "universe", "Universe should be called first"



# ---- defer --------------------------------------------------------------------
def test_defer_young_token_not_benched(monkeypatch):
    """A token at 2 minutes calls defer and does not call sit. benched is false."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"DeferAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_young(fomo, id_list):
        t = tok(0, tid=tid, addr="DeferAddr1", age_minutes=2, liquidity_usd=50000)
        return [t]
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_young)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    assert not book.benched(tid), f"Token {tid} should not be benched"
    defer_rows = book.DB.execute("SELECT tid FROM defer WHERE tid=?", (tid,)).fetchall()
    assert len(defer_rows) == 1, f"Expected 1 defer row, got {len(defer_rows)}"
    assert stats["free"].get("age", 0) == 1, f"Expected 1 age kill, got {stats['free'].get('age', 0)}"


def test_defer_due_token_passed_to_fomo(monkeypatch):
    """Next run_once: universe returns [], a due row exists, FakeFomo.tokens is called with that tid."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()
    
    tid = f"DueAddr1:{1399811149}"
    now = time.time()
    book.DB.execute("INSERT INTO defer VALUES (?,?,?)", (tid, now - 1, now + 3600))
    book.DB.commit()
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([], {}))
    
    fomo_calls = []
    original_shortlist = collect.shortlist
    def spy_shortlist(fomo, ids):
        fomo_calls.append(ids)
        return original_shortlist(fomo, ids)
    monkeypatch.setattr(shift, "shortlist", spy_shortlist)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    assert len(fomo_calls) > 0
    assert tid in fomo_calls[0]


def test_free_kill_age_boundaries():
    """free_kill at 16 minutes is not age, at 5 minutes is age, at 80 hours is age."""
    assert free_kill(tok(1, age_minutes=16)) is None
    assert free_kill(tok(1, age_minutes=5)) == "age"
    assert free_kill(tok(1, age_minutes=80 * 60)) == "age"


def test_old_token_benched_not_deferred(monkeypatch):
    """A token at 80 hours is benched with reason age and has no defer row."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"OldAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_old(fomo, id_list):
        return [tok(0, tid=tid, addr="OldAddr1", age_minutes=80 * 60)]
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_old)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    assert book.benched(tid)
    defer_rows = book.DB.execute("SELECT tid FROM defer WHERE tid=?", (tid,)).fetchall()
    assert len(defer_rows) == 0
    bench_rows = book.DB.execute("SELECT reason FROM bench WHERE tid=?", (tid,)).fetchall()
    assert len(bench_rows) == 1
    assert bench_rows[0][0] == "age"


def test_judge_not_called_for_young_token(monkeypatch):
    """The judge stand-in is not called for a token under 15 minutes."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()
    
    tid = f"YoungAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_young(fomo, id_list):
        return [tok(0, tid=tid, addr="YoungAddr1", age_minutes=5)]
    
    judge_calls = []
    def spy_judge(question_set, state):
        judge_calls.append((question_set, state))
        return JUDGE(question_set, state)
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_young)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), spy_judge, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    assert len(judge_calls) == 0


def test_defer_miss_forgotten_and_logged(monkeypatch, caplog):
    """A due id missing from FOMO response is deleted and logged as defer miss."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()
    
    tid = f"MissAddr1:{1399811149}"
    now = time.time()
    book.DB.execute("INSERT INTO defer VALUES (?,?,?)", (tid, now - 1, now + 3600))
    book.DB.commit()
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([], {}))
    
    def fake_tokens_miss(self, ids):
        return {}
    monkeypatch.setattr(FakeFomo, "tokens", fake_tokens_miss)
    
    desk = FakeDesk()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    assert any("defer outcome" in rec.message and tid in rec.message and "miss" in rec.message 
               for rec in caplog.records)
    defer_rows = book.DB.execute("SELECT tid FROM defer WHERE tid=?", (tid,)).fetchall()
    assert len(defer_rows) == 0


def test_defer_cap_200(monkeypatch, caplog):
    """The 201st new id is not inserted, and the log contains defer full."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()
    
    now = time.time()
    for i in range(book.DEFER_CAP):
        tid = f"CapAddr{i}:1399811149"
        book.DB.execute("INSERT INTO defer VALUES (?,?,?)", (tid, now + 3600, now + 7200))
    book.DB.commit()
    
    new_tid = f"CapAddr{book.DEFER_CAP}:1399811149"
    ids = [new_tid]
    
    def fake_shortlist_cap(fomo, id_list):
        return [tok(0, tid=new_tid, addr=f"CapAddr{book.DEFER_CAP}", age_minutes=5)]
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_cap)
    
    desk = FakeDesk()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    defer_count = book.DB.execute("SELECT COUNT(*) FROM defer").fetchone()[0]
    assert defer_count == book.DEFER_CAP
    
    new_tid_rows = book.DB.execute("SELECT tid FROM defer WHERE tid=?", (new_tid,)).fetchall()
    assert len(new_tid_rows) == 0


def test_defer_gate_below_threshold(monkeypatch, caplog):
    """Young token with liquidity below $12k is not deferred, stays benched as age."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"BelowGateAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_below_gate(fomo, id_list):
        # $8k liquidity, below the $12k threshold
        return [tok(0, tid=tid, addr="BelowGateAddr1", age_minutes=5, liquidity_usd=8000)]
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_below_gate)
    
    desk = FakeDesk()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    # Should be benched, not deferred
    assert book.benched(tid), f"Token {tid} should be benched"
    defer_rows = book.DB.execute("SELECT tid FROM defer WHERE tid=?", (tid,)).fetchall()
    assert len(defer_rows) == 0, f"Expected 0 defer rows, got {len(defer_rows)}"
    
    # Check for gate rejection log
    assert any("defer gate_reject" in rec.message and tid in rec.message 
               for rec in caplog.records), "Expected defer gate_reject log"


def test_defer_gate_at_threshold(monkeypatch, caplog):
    """Young token with liquidity at exactly $12k is deferred."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"AtGateAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_at_gate(fomo, id_list):
        # Exactly $12k liquidity, at the threshold
        return [tok(0, tid=tid, addr="AtGateAddr1", age_minutes=5, liquidity_usd=12000)]
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_at_gate)
    
    desk = FakeDesk()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    # Should be deferred, not benched
    assert not book.benched(tid), f"Token {tid} should not be benched"
    defer_rows = book.DB.execute("SELECT tid FROM defer WHERE tid=?", (tid,)).fetchall()
    assert len(defer_rows) == 1, f"Expected 1 defer row, got {len(defer_rows)}"
    
    # Check for defer insert log
    assert any("defer insert" in rec.message and tid in rec.message 
               for rec in caplog.records), "Expected defer insert log"


def test_defer_gate_null_liquidity(monkeypatch, caplog):
    """Young token with null liquidity is not deferred, stays benched as age."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"NullLiqAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_null_liq(fomo, id_list):
        # liquidity_usd is None
        return [tok(0, tid=tid, addr="NullLiqAddr1", age_minutes=5, liquidity_usd=None)]
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_null_liq)
    
    desk = FakeDesk()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    # Should be benched, not deferred
    assert book.benched(tid), f"Token {tid} should be benched"
    defer_rows = book.DB.execute("SELECT tid FROM defer WHERE tid=?", (tid,)).fetchall()
    assert len(defer_rows) == 0, f"Expected 0 defer rows, got {len(defer_rows)}"
    
    # Check for gate rejection log with "missing" reason
    assert any("defer gate_reject" in rec.message and tid in rec.message and "missing" in rec.message
               for rec in caplog.records), "Expected defer gate_reject log with missing reason"


def test_defer_outcome_logging(monkeypatch, caplog):
    """Defer outcome is logged when a deferred token is rescored and killed by liquidity."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"OutcomeAddr1:{1399811149}"
    now = time.time()
    
    # First cycle: insert a deferred token
    ids1 = [tid]
    def fake_shortlist_defer(fomo, id_list):
        return [tok(0, tid=tid, addr="OutcomeAddr1", age_minutes=5, liquidity_usd=20000)]
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids1, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_defer)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    # Verify it was deferred
    defer_rows = book.DB.execute("SELECT tid FROM defer WHERE tid=?", (tid,)).fetchall()
    assert len(defer_rows) == 1, f"Expected 1 defer row, got {len(defer_rows)}"
    
    # Second cycle: token is due but now fails liquidity check
    book.DB.execute("UPDATE defer SET ready=? WHERE tid=?", (now - 1, tid))
    book.DB.commit()
    
    def fake_shortlist_fail_liq(fomo, id_list):
        # Now has low liquidity
        return [tok(0, tid=tid, addr="OutcomeAddr1", age_minutes=16, liquidity_usd=5000)]
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_fail_liq)
    
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    # Should be forgotten with liquidity reason
    defer_rows = book.DB.execute("SELECT tid FROM defer WHERE tid=?", (tid,)).fetchall()
    assert len(defer_rows) == 0, f"Expected 0 defer rows after liquidity kill, got {len(defer_rows)}"
    
    # Check for defer outcome log
    assert any("defer outcome" in rec.message and tid in rec.message and "liquidity" in rec.message
               for rec in caplog.records), "Expected defer outcome log with liquidity reason"


# ---- authority normalization --------------------------------------------------
def test_normalize_authority_revoked_strings():
    """_normalize_authority should treat 'no', 'false', etc. as False (revoked)."""
    from collect import _normalize_authority
    
    # All these should normalize to False (revoked)
    for value in ['no', 'NO', 'No', 'false', 'FALSE', 'False', '', 'null', 'NULL', 'none', 'NONE', '0']:
        normalized, raw = _normalize_authority(value)
        assert normalized is False, f"Expected False for '{value}', got {normalized}"
        assert raw == value


def test_normalize_authority_open_strings():
    """_normalize_authority should treat 'yes', 'true', or base58 addresses as True (open)."""
    from collect import _normalize_authority
    
    # All these should normalize to True (open/set)
    for value in ['yes', 'YES', 'Yes', 'true', 'TRUE', 'True', 
                  'CBLx6CRcCTtbmgTdxpqnF2dP1MpWbMUjngtNbFTApump',  # base58 address
                  '5VnbrKandP1dP1MpWbMUjngtNbFTApump']:
        normalized, raw = _normalize_authority(value)
        assert normalized is True, f"Expected True for '{value}', got {normalized}"
        assert raw == value


def test_normalize_authority_none_unknown():
    """_normalize_authority should treat None as None (unknown)."""
    from collect import _normalize_authority
    
    normalized, raw = _normalize_authority(None)
    assert normalized is None
    assert raw is None


def test_normalize_authority_boolean_values():
    """_normalize_authority should preserve boolean values."""
    from collect import _normalize_authority
    
    normalized, raw = _normalize_authority(True)
    assert normalized is True
    assert raw == "True"
    
    normalized, raw = _normalize_authority(False)
    assert normalized is False
    assert raw == "False"


def test_chain_kill_authority_open_only_on_true():
    """chain_kill should only return authority_open when mint or freeze is True, not truthy strings."""
    from filter import chain_kill
    
    base = {"chain": "solana", "addr": "test", "top_wallet_percent": 0.01, "top_10_percent": 30,
            "holder_count": 300}
    
    # Both False (revoked): should pass
    assert chain_kill({**base, "mint_authority": False, "freeze_authority": False}) is None
    
    # Both None (unknown): should pass (no kill on GT alone)
    assert chain_kill({**base, "mint_authority": None, "freeze_authority": None}) is None
    
    # mint_authority True: should kill
    assert chain_kill({**base, "mint_authority": True, "freeze_authority": False}) == "authority_open"
    
    # freeze_authority True: should kill
    assert chain_kill({**base, "mint_authority": False, "freeze_authority": True}) == "authority_open"
    
    # Both True: should kill
    assert chain_kill({**base, "mint_authority": True, "freeze_authority": True}) == "authority_open"


def test_chain_kill_preserves_old_behavior_for_other_chains():
    """chain_kill should not change behavior for bsc/base/robinhood (but now requires top_wallet_percent)."""
    from filter import chain_kill
    
    # BSC honeypot still kills (but needs top_wallet_percent to pass unverified check first)
    assert chain_kill({"chain": "bsc", "top_wallet_percent": 0.03, "is_honeypot": True}) == "honeypot"
    assert chain_kill({"chain": "base", "top_wallet_percent": 0.03, "is_honeypot": True}) == "honeypot"
    
    # Top wallet still kills
    assert chain_kill({"chain": "bsc", "top_wallet_percent": 0.2, "holder_count": 300}) == "top_wallet"
    
    # EVM without top_wallet_percent fails closed
    assert chain_kill({"chain": "bsc", "top_wallet_percent": None, "holder_count": 300}) == "top_wallet_unverified"


def test_dossier_normalizes_authority_fields(monkeypatch):
    """dossier should normalize mint_authority and freeze_authority and preserve raw values."""
    import requests
    from collect import dossier
    monkeypatch.setattr(collect, "sol_top_wallet", _pass_sol_wallet)
    
    original_get = requests.get
    
    def mock_gt_response(*args, **kwargs):
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "attributes": {
                    "mint_authority": "no",      # GeckoTerminal returns 'no' as string
                    "freeze_authority": "no",
                    "holders": {"count": 500},
                    "is_honeypot": None
                }
            }
        }
        return mock_resp
    
    requests.get = mock_gt_response
    
    try:
        t = tok(1)
        result = dossier(t)
        
        # Should normalize 'no' to False
        assert result["mint_authority"] is False, f"Expected False, got {result['mint_authority']}"
        assert result["freeze_authority"] is False, f"Expected False, got {result['freeze_authority']}"
        
        # Should preserve raw values
        assert result["mint_authority_raw"] == "no"
        assert result["freeze_authority_raw"] == "no"
    finally:
        requests.get = original_get


def test_migration_clears_authority_open_bench():
    """Migration should remove bench entries with reason='authority_open'."""
    # Insert fake authority_open bench entries
    now = time.time()
    book.DB.execute("DELETE FROM bench")
    book.DB.execute("INSERT INTO bench VALUES (?,?,?)", ("fake1:1399811149", "authority_open", now + 3600))
    book.DB.execute("INSERT INTO bench VALUES (?,?,?)", ("fake2:1399811149", "authority_open", now + 3600))
    book.DB.execute("INSERT INTO bench VALUES (?,?,?)", ("fake3:1399811149", "liquidity", now + 3600))
    book.DB.commit()
    
    # Check that they exist
    rows = book.DB.execute("SELECT COUNT(*) FROM bench WHERE reason='authority_open'").fetchone()
    assert rows[0] == 2, f"Expected 2 authority_open rows before migration, got {rows[0]}"
    
    # Run migration
    book._clear_authority_open_bench()
    
    # Check that authority_open rows are gone
    rows = book.DB.execute("SELECT COUNT(*) FROM bench WHERE reason='authority_open'").fetchone()
    assert rows[0] == 0, f"Expected 0 authority_open rows after migration, got {rows[0]}"
    
    # Check that other reason rows remain
    rows = book.DB.execute("SELECT COUNT(*) FROM bench WHERE reason='liquidity'").fetchone()
    assert rows[0] == 1, f"Expected 1 liquidity row after migration, got {rows[0]}"


def test_end_to_end_authority_no_strings_pass(monkeypatch):
    """End-to-end: token with 'no'/'no' from GT should pass authority check and not be benched."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"NoNoAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_pass(fomo, id_list):
        return [tok(0, tid=tid, addr="NoNoAddr1", age_minutes=20, liquidity_usd=50000,
                    volume_h24=100000, mcap_usd=500000)]
    
    def fake_dossier_no_no(t, limiter=None):
        # Simulate GT returning 'no' strings
        from collect import _normalize_authority
        mint_norm, mint_raw = _normalize_authority("no")
        freeze_norm, freeze_raw = _normalize_authority("no")
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None, 
                "is_honeypot": None, 
                "mint_authority": mint_norm, "mint_authority_raw": mint_raw,
                "freeze_authority": freeze_norm, "freeze_authority_raw": freeze_raw,
                "description": "a token", "x_handle": None}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(collect, "shortlist", fake_shortlist_pass)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400, 
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_no_no)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=3)
    
    # Token should NOT be killed by chain_kill
    assert stats.get("chain", {}).get("authority_open", 0) == 0, \
        f"Expected no authority_open kills, got {stats.get('chain', {})}"
    
    # Token should NOT be benched as authority_open
    bench_rows = book.DB.execute("SELECT reason FROM bench WHERE tid=?", (tid,)).fetchall()
    authority_open_benched = any(row[0] == "authority_open" for row in bench_rows)
    assert not authority_open_benched, \
        f"Token should not be benched as authority_open, bench reasons: {[row[0] for row in bench_rows]}"


def test_soft_kill_logs_noul_and_records_in_state(monkeypatch, caplog):
    """Soft kill should log noul and ticker, and record should store soft_noul and soft_scores."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"SoftKillAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_pass(fomo, id_list):
        return [tok(0, tid=tid, addr="SoftKillAddr1", age_minutes=20, liquidity_usd=50000,
                    volume_h24=100000, mcap_usd=500000)]
    
    # Mock judge to return a soft kill answer
    def fake_judge(question_set, state):
        if question_set == "market":
            return {"model": "test", "answers": {
                "concentration_is_exit_risk": {"type": "noul", "noul": 0.75},  # Will kill (max 0.55)
                "momentum_already_spent": {"type": "noul", "noul": 0.40},
                "shape": {"type": "choice", "choice": "crowd", "probabilities": {"crowd": 0.8}}
            }, "usage": {}}
        elif question_set == "solana":
            return {"model": "test", "answers": {}, "usage": {}}
        return {"model": "test", "answers": {}, "usage": {}}
    
    def fake_dossier(t, limiter=None):
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None, 
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a token", "x_handle": None, "net": 1399811149}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(collect, "shortlist", fake_shortlist_pass)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400, 
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier)
    
    desk = FakeDesk()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), fake_judge, desk, desk.bank(), shadow=True, gt_dossier_reserve=3)
    
    # Should have soft-killed
    assert stats.get("soft", {}).get("concentration_is_exit_risk", 0) == 1
    
    # Check log contains detailed soft kill info
    soft_logs = [r for r in caplog.records if "soft tid=" in r.message and "noul=" in r.message]
    assert len(soft_logs) >= 1, f"Expected soft kill log, got: {[r.message for r in caplog.records if 'soft' in r.message]}"
    
    log_msg = soft_logs[0].message
    assert "ticker=T0" in log_msg
    assert "reason=concentration_is_exit_risk" in log_msg
    assert "noul=0.75" in log_msg
    assert "age_minutes=" in log_msg  # age is computed from timestamp, just verify it's logged
    assert "soft_scores=" in log_msg
    
    # Check state.json includes soft_noul and soft_scores
    token_rows = stats.get("tokens", [])
    soft_killed_rows = [t for t in token_rows if t.get("stage") == "soft"]
    assert len(soft_killed_rows) == 1, f"Expected 1 soft-killed row, got {len(soft_killed_rows)}"
    
    soft_row = soft_killed_rows[0]
    assert soft_row["reason"] == "concentration_is_exit_risk"
    assert soft_row.get("soft_noul") == 0.75
    assert "soft_scores" in soft_row
    assert soft_row["soft_scores"]["concentration_is_exit_risk"] == 0.75
    assert soft_row["soft_scores"]["momentum_already_spent"] == 0.40


def test_end_to_end_authority_yes_kills(monkeypatch):
    """End-to-end: token with 'yes' from GT should be killed as authority_open."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"YesAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_pass(fomo, id_list):
        return [tok(0, tid=tid, addr="YesAddr1", age_minutes=20, liquidity_usd=50000,
                    volume_h24=100000, mcap_usd=500000)]
    
    def fake_dossier_yes(t, limiter=None):
        # Simulate GT returning 'yes' for mint_authority
        from collect import _normalize_authority
        mint_norm, mint_raw = _normalize_authority("yes")
        freeze_norm, freeze_raw = _normalize_authority("no")
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None, 
                "is_honeypot": None,
                "mint_authority": mint_norm, "mint_authority_raw": mint_raw,
                "freeze_authority": freeze_norm, "freeze_authority_raw": freeze_raw,
                "description": "a token", "x_handle": None}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(collect, "shortlist", fake_shortlist_pass)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400, 
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_yes)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=3)
    
    # Token SHOULD be killed by chain_kill as authority_open
    assert stats.get("chain", {}).get("authority_open", 0) == 1, \
        f"Expected 1 authority_open kill, got {stats.get('chain', {})}"
    
    # Token should be benched as authority_open
    assert book.benched(tid), f"Token should be benched"
    bench_rows = book.DB.execute("SELECT reason FROM bench WHERE tid=?", (tid,)).fetchall()
    assert len(bench_rows) == 1
    assert bench_rows[0][0] == "authority_open"


    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    


def test_fomo_502_after_retries_does_not_crash_cycle(monkeypatch, caplog):
    """FOMO 502 after all retries should degrade gracefully: empty shortlist, cycle completes as NO TRADE."""
    ids = [f"Addr{i}:1399811149" for i in range(5)]
    
    # Mock FOMO to always return 502
    class Fomo502:
        def token(self): return "fake-token"
        def tokens(self, ids):
            # Simulate _filter_tokens returning empty dict after 502 retries
            from fomo_api import Fomo
            fomo = Fomo(bearer="fake-token")
            mock_502 = Mock()
            mock_502.status_code = 502
            fomo.s.post = Mock(return_value=mock_502)
            # This will now return {} instead of raising
            return fomo._filter_tokens(ids)
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    
    desk = FakeDesk()
    caplog.clear()
    
    # Should NOT raise, cycle should complete
    with caplog.at_level(logging.ERROR):
        order, stats = shift.run_once(Fomo502(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=3)
    
    # Order should be None (NO TRADE outcome)
    assert order is None, "Expected NO TRADE outcome"
    
    # Stats should show seen=0 (empty shortlist after FOMO failure)
    assert stats["seen"] == 0, f"Expected seen=0, got {stats['seen']}"
    
    # Should have logged FOMO error
    error_logs = [r for r in caplog.records if "FOMO" in r.message and "502" in r.message and "gateway" in r.message]
    assert len(error_logs) > 0, "Expected FOMO 502 error log"
    
    # Should NOT have "cycle blew up" in logs (that's the bug we're fixing)
    blew_up_logs = [r for r in caplog.records if "cycle blew up" in r.message]
    assert len(blew_up_logs) == 0, f"Expected no 'cycle blew up', got: {[r.message for r in blew_up_logs]}"


def test_fomo_partial_502_continues_with_partial_data(monkeypatch):
    """If FOMO fails on some chunks but not others, cycle continues with partial shortlist."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    # Test that FOMO API returns partial results when one chunk fails
    from fomo_api import Fomo
    
    # Track call count across chunks
    call_tracker = {"count": 0}
    
    def mock_filter_tokens(self, chunk):
        call_tracker["count"] += 1
        if call_tracker["count"] == 1:
            # First chunk succeeds with 10 tokens
            return {
                f"Addr{i}:1399811149": {
                    "symbol": f"T{i}", "mcap": 500000, "liq": 50000, 
                    "vol24": 100000, "price": 0.01, "holders": 500,
                    "change": {300: 0.02, 3600: 0.05, 14400: 0.1, 86400: 0.15},
                    "created": int(time.time() * 1000 - 3600000)
                } for i in range(10)
            }
        else:
            # Second chunk 502s - returns empty dict (graceful degrade)
            logging.getLogger("fomo").error("FOMO 502 gateway error persisted after retries, returning empty result for this chunk")
            return {}
    
    monkeypatch.setattr(Fomo, "_filter_tokens", mock_filter_tokens)
    
    ids = [f"Addr{i}:1399811149" for i in range(25)]
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    
    # Make tokens pass through to judging
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400, 
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", lambda t, limiter=None: {
        **t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
        "developer_holding_percentage": 2, "gt_score_details": None, 
        "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
        "description": "a token", "x_handle": None})
    
    fomo = Fomo(bearer="fake-token")
    desk = FakeDesk()
    order, stats = shift.run_once(fomo, JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=25)
    
    # Should have seen 10 tokens (first chunk), not all 25
    assert stats["seen"] == 10, f"Expected 10 tokens from successful chunk, got {stats['seen']}"
    
    # Should have completed successfully (not crashed)
    assert "error" not in stats or stats.get("error") is None

# ---- age-prioritized dossier work --------------------------------------------
def test_young_tokens_get_dossier_before_old_with_budget_constraint(monkeypatch):
    """When dossier budget is tight, young tokens (<60m) should get dossier attempts before old (≥60m)."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    # Create 4 tokens: 2 young (<60m), 2 old (≥60m)
    # Old tokens have HIGHER turnover, so they'd normally be prioritized
    # Young tokens have LOWER turnover, but should still get dossier first due to age
    young1_tid = f"YoungAddr1:{1399811149}"
    young2_tid = f"YoungAddr2:{1399811149}"
    old1_tid = f"OldAddr1:{1399811149}"
    old2_tid = f"OldAddr2:{1399811149}"
    
    # shortlist returns tokens sorted by turnover (high to low): [old1, old2, young1, young2]
    # After age-prioritization in main.py: [young1, young2, old1, old2]
    def fake_shortlist_mixed(fomo, id_list):
        # Return in turnover order (high to low)
        tokens = []
        # Old tokens with high turnover come first in turnover-sorted list
        tokens.append({"tid": old1_tid, "addr": "OldAddr1", "net": 1399811149, "ticker": "OLD1",
                      "age_minutes": 75.0, "liquidity_usd": 50000, "volume_h24": 500000, 
                      "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
                      "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}})
        tokens.append({"tid": old2_tid, "addr": "OldAddr2", "net": 1399811149, "ticker": "OLD2",
                      "age_minutes": 90.0, "liquidity_usd": 50000, "volume_h24": 400000,
                      "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
                      "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}})
        # Young tokens with lower turnover come after in turnover-sorted list
        tokens.append({"tid": young1_tid, "addr": "YoungAddr1", "net": 1399811149, "ticker": "YOUNG1",
                      "age_minutes": 25.0, "liquidity_usd": 50000, "volume_h24": 200000,
                      "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
                      "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}})
        tokens.append({"tid": young2_tid, "addr": "YoungAddr2", "net": 1399811149, "ticker": "YOUNG2",
                      "age_minutes": 40.0, "liquidity_usd": 50000, "volume_h24": 150000,
                      "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
                      "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}})
        return tokens
    
    dossier_calls = []
    def fake_dossier_track(t, limiter=None):
        dossier_calls.append((t["tid"], t["age_minutes"]))
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a token", "x_handle": None}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([old1_tid, old2_tid, young1_tid, young2_tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_mixed)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400,
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_track)
    
    desk = FakeDesk()
    # Large budget to ensure all tokens can get through dossier stage
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=10)
    
    # Verify that young tokens were dossier'd before old ones
    assert len(dossier_calls) >= 2, f"Expected at least 2 dossier calls, got {len(dossier_calls)}: {dossier_calls}"
    
    # First two dossier calls should be young tokens (ages 25, 40)
    first_call_age = dossier_calls[0][1]
    second_call_age = dossier_calls[1][1]
    
    assert first_call_age < 60, f"First dossier call should be young (<60m), got {first_call_age}m for {dossier_calls[0][0]}"
    assert second_call_age < 60, f"Second dossier call should be young (<60m), got {second_call_age}m for {dossier_calls[1][0]}"


def test_old_requeue_does_not_starve_young_first_timer(monkeypatch):
    """An old token (≥60m) requeued due to GT 429 should not prevent a young first-timer from getting dossier."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    young_tid = f"YoungFirstTimer:{1399811149}"
    old_requeue_tid = f"OldRequeue:{1399811149}"
    
    # Requeue an old token (simulating a previous GT 429)
    now = time.time()
    book.defer(old_requeue_tid, ready=now - 1, drop_at=now + 3600)
    
    def fake_shortlist_mixed(fomo, id_list):
        tokens = []
        # Old requeue with high turnover (would normally be prioritized, comes first in turnover order)
        tokens.append({"tid": old_requeue_tid, "addr": "OldRequeue", "net": 1399811149, "ticker": "OLDREQ",
                      "age_minutes": 75.0, "liquidity_usd": 50000, "volume_h24": 600000,
                      "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
                      "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}})
        # Young first-timer with lower turnover (comes after in turnover order)
        tokens.append({"tid": young_tid, "addr": "YoungFirstTimer", "net": 1399811149, "ticker": "YOUNGFT",
                      "age_minutes": 30.0, "liquidity_usd": 50000, "volume_h24": 200000,
                      "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
                      "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}})
        return tokens
    
    dossier_calls = []
    def fake_dossier_track(t, limiter=None):
        dossier_calls.append((t["tid"], t["age_minutes"]))
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a token", "x_handle": None}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([young_tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_mixed)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400,
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_track)
    
    desk = FakeDesk()
    # Budget for only 1 dossier
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=1)
    
    # The young token should get the dossier slot, not the old requeue
    assert len(dossier_calls) >= 1, "Expected at least 1 dossier call"
    first_call_tid, first_call_age = dossier_calls[0]
    
    assert first_call_age < 60, f"First dossier should be young (<60m), got {first_call_age}m for {first_call_tid}"
    assert first_call_tid == young_tid, f"Young token should get dossier before old requeue"


def test_age_prioritization_preserves_turnover_within_groups(monkeypatch):
    """Within young and old groups, turnover ordering should be preserved."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    # 3 young tokens with different turnovers
    young_high_tid = f"YoungHigh:{1399811149}"
    young_mid_tid = f"YoungMid:{1399811149}"
    young_low_tid = f"YoungLow:{1399811149}"
    
    def fake_shortlist_young_group(fomo, id_list):
        # Already sorted by turnover (high to low)
        return [
            {"tid": young_high_tid, "addr": "YoungHigh", "net": 1399811149, "ticker": "YHIGH",
             "age_minutes": 30.0, "liquidity_usd": 50000, "volume_h24": 500000,
             "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
             "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}},
            {"tid": young_mid_tid, "addr": "YoungMid", "net": 1399811149, "ticker": "YMID",
             "age_minutes": 40.0, "liquidity_usd": 50000, "volume_h24": 300000,
             "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
             "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}},
            {"tid": young_low_tid, "addr": "YoungLow", "net": 1399811149, "ticker": "YLOW",
             "age_minutes": 50.0, "liquidity_usd": 50000, "volume_h24": 100000,
             "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
             "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}},
        ]
    
    dossier_calls = []
    def fake_dossier_track(t, limiter=None):
        dossier_calls.append(t["tid"])
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a token", "x_handle": None}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([young_high_tid, young_mid_tid, young_low_tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_young_group)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400,
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_track)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=10)
    
    # All young tokens should be processed in their turnover order
    assert len(dossier_calls) == 3
    assert dossier_calls[0] == young_high_tid, "Highest turnover young token should be first"
    assert dossier_calls[1] == young_mid_tid, "Mid turnover young token should be second"
    assert dossier_calls[2] == young_low_tid, "Low turnover young token should be third"


def test_young_token_429_gets_in_cycle_retry(monkeypatch):
    """Young token (<60m) that hits GT 429 should wait and retry once in the same cycle."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    young_tid = f"YoungRetry:{1399811149}"
    
    def fake_shortlist(fomo, id_list):
        return [{"tid": young_tid, "addr": "YoungRetry", "net": 1399811149, "ticker": "YRETRY",
                "age_minutes": 30.0, "liquidity_usd": 50000, "volume_h24": 200000,
                "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
                "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}}]
    
    dossier_call_count = [0]
    wait_called = [False]
    
    def fake_dossier_429_then_success(t, limiter=None):
        dossier_call_count[0] += 1
        if dossier_call_count[0] == 1:
            # First call: raise 429
            raise collect.DossierRetryNeeded("GT 429 first attempt")
        
        # Second call: wait for backoff (dossier now waits before making request)
        if limiter:
            limiter.wait_if_needed(priority=True)
        
        # Then succeed
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a token", "x_handle": None}
    
    original_wait = collect.GTRateLimiter.wait_if_needed
    def fake_wait_if_needed(self, priority=False, deadline=None):
        if priority:
            wait_called[0] = True
        return original_wait(self, priority, deadline=deadline)
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([young_tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400,
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_429_then_success)
    monkeypatch.setattr(collect.GTRateLimiter, "wait_if_needed", fake_wait_if_needed)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=2)
    
    # Verify that dossier was called twice (initial + in-cycle retry)
    assert dossier_call_count[0] == 2, f"Expected 2 dossier calls (initial + retry), got {dossier_call_count[0]}"
    
    # Verify that wait_if_needed was called with priority=True
    assert wait_called[0], "wait_if_needed should be called for in-cycle retry"
    
    # Verify the token was not deferred (successful retry)
    deferred = book.defer_due()
    assert young_tid not in deferred, f"Token should not be deferred after successful retry"
    
    # Verify token was judged (successful dossier on retry)
    assert stats.get("judged", 0) >= 1, "Token should be judged after successful retry"


def test_young_token_429_twice_defers_to_next_cycle(monkeypatch):
    """Young token that hits GT 429 twice (initial + retry) should defer to next cycle."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    young_tid = f"YoungRetryFail:{1399811149}"
    
    def fake_shortlist(fomo, id_list):
        return [{"tid": young_tid, "addr": "YoungRetryFail", "net": 1399811149, "ticker": "YFAIL",
                "age_minutes": 35.0, "liquidity_usd": 50000, "volume_h24": 200000,
                "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
                "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}}]
    
    dossier_call_count = [0]
    
    def fake_dossier_always_429(t, limiter=None):
        dossier_call_count[0] += 1
        raise collect.DossierRetryNeeded("GT 429 persistent")
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([young_tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400,
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_always_429)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=2)
    
    # Verify that dossier was called twice (initial + in-cycle retry)
    assert dossier_call_count[0] == 2, f"Expected 2 dossier calls, got {dossier_call_count[0]}"
    
    # Verify the token was deferred (failed on retry)
    deferred = book.defer_due()
    assert young_tid in deferred, f"Token should be deferred after failed retry"
    
    # Verify token was NOT judged (failed dossier)
    assert stats.get("judged", 0) == 0, "Token should not be judged after failed dossier"
    
    # Verify requeued count
    assert stats.get("requeued", 0) >= 1, "Token should be marked as requeued"


def test_young_token_429_backoff_actually_waits(monkeypatch):
    """Verify that after a GT 429, in-cycle retry waits for proper backoff (not ~50ms)."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    young_tid = f"BackoffTest:{1399811149}"
    
    def fake_shortlist(fomo, id_list):
        return [{"tid": young_tid, "addr": "BackoffTest", "net": 1399811149, "ticker": "BKOFF",
                "age_minutes": 25.0, "liquidity_usd": 50000, "volume_h24": 200000,
                "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
                "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}}]
    
    # Use fake time to track the backoff
    fake_time = [1000.0]  # start at t=1000
    
    def time_fn():
        return fake_time[0]
    
    # Track sleep calls and their durations
    sleep_calls = []
    original_sleep = time.sleep
    def fake_sleep(duration):
        sleep_calls.append(duration)
        fake_time[0] += duration  # advance fake time
    
    # Create limiter with fake time
    test_limiter = collect.GTRateLimiter(calls_per_min=8, time_fn=time_fn)
    
    dossier_call_count = [0]
    dossier_call_times = []
    
    def fake_dossier_429_then_success(t, limiter=None):
        dossier_call_count[0] += 1
        
        if dossier_call_count[0] == 1:
            # First call: record time, simulate 429 with 5-second Retry-After
            dossier_call_times.append(fake_time[0])
            if limiter:
                limiter.record_429(5.0)
            raise collect.DossierRetryNeeded("GT 429 first attempt")
        
        # Second call: wait for backoff (dossier now waits before making request)
        if limiter:
            limiter.wait_if_needed(priority=True)
        
        # Record time after wait (simulates HTTP request time)
        dossier_call_times.append(fake_time[0])
        
        # Then succeed
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a token", "x_handle": None}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([young_tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400,
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_429_then_success)
    monkeypatch.setattr(time, "sleep", fake_sleep)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, 
                                  gt_dossier_reserve=2, gt_limiter=test_limiter)
    
    # Verify that dossier was called twice
    assert dossier_call_count[0] == 2, f"Expected 2 dossier calls, got {dossier_call_count[0]}"
    
    # Verify that sleep was called with a reasonable backoff (should be 5 seconds from record_429)
    assert len(sleep_calls) > 0, "Expected at least one sleep call for backoff"
    
    # Find sleep calls >= 1 second (backoff sleeps, not tiny waits)
    backoff_sleeps = [s for s in sleep_calls if s >= 1.0]
    assert len(backoff_sleeps) > 0, f"Expected backoff sleep >= 1s, got sleep_calls={sleep_calls}"
    
    # Verify the backoff was reasonable (should be ~5 seconds, allow some tolerance)
    total_backoff = sum(backoff_sleeps)
    assert total_backoff >= 4.0, f"Expected backoff >= 4s, got {total_backoff:.2f}s"
    
    # Verify calls were spaced apart (not ~50ms)
    if len(dossier_call_times) == 2:
        time_between_calls = dossier_call_times[1] - dossier_call_times[0]
        assert time_between_calls >= 4.0, \
            f"Expected >=4s between dossier calls after 429, got {time_between_calls:.2f}s"
    
    # Token should succeed after proper backoff
    assert stats.get("judged", 0) >= 1, "Token should be judged after backoff and successful retry"


def test_old_token_429_skips_while_young_pending(monkeypatch):
    """Old token (≥60m) gets dossier after young token's retry fails and is removed from pending."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    young_tid = f"YoungPending:{1399811149}"
    old_tid = f"OldSkipped:{1399811149}"
    
    def fake_shortlist(fomo, id_list):
        # OLD token first with HIGHER turnover, young second with lower turnover
        # Sorting by (not is_young, tier) will prioritize young first
        return [
            {"tid": old_tid, "addr": "OldSkipped", "net": 1399811149, "ticker": "OSKIP",
             "age_minutes": 75.0, "liquidity_usd": 50000, "volume_h24": 500000,
             "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
             "change": {"5m": 0.05, "1h": 0.25, "4h": 0.5, "24h": 0.7}},
            {"tid": young_tid, "addr": "YoungPending", "net": 1399811149, "ticker": "YPEND",
             "age_minutes": 30.0, "liquidity_usd": 50000, "volume_h24": 300000,
             "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
             "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}}
        ]
    
    dossier_calls = []
    
    def fake_dossier_young_429(t, limiter=None):
        dossier_calls.append(t["tid"])
        if t["tid"] == young_tid:
            # Young token hits 429
            raise collect.DossierRetryNeeded("GT 429 for young")
        # Old token should now reach here after young token fails and is removed from pending
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a token", "x_handle": None}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([young_tid, old_tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400,
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_young_429)
    
    desk = FakeDesk()
    # Limited budget: young hits 429 twice (initial + retry), then old gets dossier
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=3)
    
    # Verify ordering: young attempts (2x), then old attempt (1x)
    young_idx = [i for i, tid in enumerate(dossier_calls) if tid == young_tid]
    old_idx = [i for i, tid in enumerate(dossier_calls) if tid == old_tid]
    assert len(young_idx) == 2 and len(old_idx) == 1, \
        f"Expected young=2, old=1, got young={len(young_idx)}, old={len(old_idx)}: {dossier_calls}"
    assert old_idx[0] > max(young_idx), \
        f"old dossier ran before young's in-cycle retry finished: {dossier_calls}"
    
    # Verify young token was deferred
    deferred = book.defer_due()
    assert young_tid in deferred, "Young token should be deferred after retry failure"
    
    # Verify requeued count includes young (old may or may not be requeued depending on path)
    assert stats.get("requeued", 0) >= 1, "Young token should be requeued after retry fail"


def test_old_token_429_backoff_path_ordering(monkeypatch):
    """Old token gets dossier after young token's backoff+retry fails (wait-and-retry branch)."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    young_tid = f"YoungBackoff:{1399811149}"
    old_tid = f"OldAfterWait:{1399811149}"
    
    def fake_shortlist(fomo, id_list):
        # OLD token first with HIGHER turnover, young second with lower turnover
        return [
            {"tid": old_tid, "addr": "OldAfterWait", "net": 1399811149, "ticker": "OWAIT",
             "age_minutes": 75.0, "liquidity_usd": 50000, "volume_h24": 500000,
             "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
             "change": {"5m": 0.05, "1h": 0.25, "4h": 0.5, "24h": 0.7}},
            {"tid": young_tid, "addr": "YoungBackoff", "net": 1399811149, "ticker": "YBACK",
             "age_minutes": 30.0, "liquidity_usd": 50000, "volume_h24": 300000,
             "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
             "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}}
        ]
    
    dossier_calls = []
    sleep_calls = []
    
    import time as real_time
    original_sleep = real_time.sleep
    
    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        # Don't actually sleep in tests
    
    def fake_dossier_young_429(t, limiter=None):
        dossier_calls.append(t["tid"])
        if t["tid"] == young_tid:
            # Young token hits 429
            raise collect.DossierRetryNeeded("GT 429 for young")
        # Old token should reach here after young token's backoff+retry fails
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a token", "x_handle": None}
    
    # Patch time.sleep
    monkeypatch.setattr("time.sleep", fake_sleep)
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([young_tid, old_tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400,
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_young_429)
    
    desk = FakeDesk()
    
    # Mock GT limiter with backoff_until set to now + 5s (within 30s cap for in-cycle retry)
    from unittest.mock import Mock, MagicMock
    import time
    
    # Create a mock GT limiter with backoff_until set to now + 5s
    mock_limiter = Mock()
    mock_limiter.available.return_value = 100
    mock_limiter.backoff_until = time.time() + 5.0  # Within 30s cap for in-cycle retry
    mock_limiter.wait_if_needed.return_value = None
    
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, 
                                  gt_dossier_reserve=3, gt_limiter=mock_limiter)
    
    # Verify sleep was called (backoff wait)
    assert len(sleep_calls) > 0, "Expected time.sleep to be called for backoff wait"
    assert any(s >= 4.5 and s <= 5.5 for s in sleep_calls), f"Expected ~5s sleep for backoff, got {sleep_calls}"
    
    # Verify ordering: young attempts (2x with backoff+retry), then old attempt (1x)
    young_idx = [i for i, tid in enumerate(dossier_calls) if tid == young_tid]
    old_idx = [i for i, tid in enumerate(dossier_calls) if tid == old_tid]
    assert len(young_idx) == 2 and len(old_idx) == 1, \
        f"Expected young=2, old=1, got young={len(young_idx)}, old={len(old_idx)}: {dossier_calls}"
    assert old_idx[0] > max(young_idx), \
        f"old dossier ran before young's backoff+retry finished: {dossier_calls}"
    
    # Verify young token was deferred
    deferred = book.defer_due()
    assert young_tid in deferred, "Young token should be deferred after backoff+retry failure"


def test_old_token_gets_dossier_after_young_clears(monkeypatch):
    """Old token should get dossier after young token successfully completes."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    young_tid = f"YoungClears:{1399811149}"
    old_tid = f"OldFollows:{1399811149}"
    
    def fake_shortlist(fomo, id_list):
        # Young first, old second (age-prioritized)
        return [
            {"tid": young_tid, "addr": "YoungClears", "net": 1399811149, "ticker": "YCLR",
             "age_minutes": 30.0, "liquidity_usd": 50000, "volume_h24": 400000,
             "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
             "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}},
            {"tid": old_tid, "addr": "OldFollows", "net": 1399811149, "ticker": "OFOL",
             "age_minutes": 75.0, "liquidity_usd": 50000, "volume_h24": 300000,
             "mcap_usd": 100000, "holder_count": 300, "price_usd": 0.001,
             "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61}}
        ]
    
    dossier_calls = []
    
    def fake_dossier_success(t, limiter=None):
        dossier_calls.append(t["tid"])
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a token", "x_handle": None}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([young_tid, old_tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400,
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_success)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=3)
    
    # Both tokens should get dossier
    assert len(dossier_calls) == 2, f"Expected 2 dossier calls, got {len(dossier_calls)}"
    
    # Young should be first, old second
    assert dossier_calls[0] == young_tid, "Young token should get dossier first"
    assert dossier_calls[1] == old_tid, "Old token should get dossier after young clears"
    
    # Neither should be deferred
    deferred = book.defer_due()
    assert young_tid not in deferred and old_tid not in deferred, "No tokens should be deferred on success"


def test_soft_kill_age_aware_momentum_young_pass():
    """Young token (<60m) with momentum noul 0.74 should pass (below young threshold 0.85)."""
    from filter import soft_kill
    
    ans = {
        "momentum_already_spent": {"type": "noul", "noul": 0.74},
        "concentration_is_exit_risk": {"type": "noul", "noul": 0.45},
    }
    
    # Young token: should pass with 0.74 (< 0.85 young threshold)
    result = soft_kill(ans, age_minutes=22.7)
    assert result is None, f"Young token with momentum 0.74 should pass, got {result}"


def test_soft_kill_age_aware_momentum_young_kill():
    """Young token (<60m) with very high momentum noul 0.90 should still be killed."""
    from filter import soft_kill
    
    ans = {
        "momentum_already_spent": {"type": "noul", "noul": 0.90},
        "concentration_is_exit_risk": {"type": "noul", "noul": 0.45},
    }
    
    # Young token but extremely high momentum: should kill (> 0.85)
    result = soft_kill(ans, age_minutes=20.0)
    assert result is not None, "Young token with momentum 0.90 should be killed"
    assert result[0] == "momentum_already_spent"
    assert result[1] == 0.90


def test_soft_kill_age_aware_momentum_old_kill():
    """Old token (>=60m) with momentum noul 0.65 should be killed (above old threshold 0.60)."""
    from filter import soft_kill
    
    ans = {
        "momentum_already_spent": {"type": "noul", "noul": 0.65},
        "concentration_is_exit_risk": {"type": "noul", "noul": 0.45},
    }
    
    # Old token: should kill with 0.65 (> 0.60 old threshold)
    result = soft_kill(ans, age_minutes=120.0)
    assert result is not None, "Old token with momentum 0.65 should be killed"
    assert result[0] == "momentum_already_spent"
    assert result[1] == 0.65


def test_soft_kill_age_aware_momentum_old_pass():
    """Old token (>=60m) with momentum noul 0.55 should pass (below old threshold 0.60)."""
    from filter import soft_kill
    
    ans = {
        "momentum_already_spent": {"type": "noul", "noul": 0.55},
        "concentration_is_exit_risk": {"type": "noul", "noul": 0.45},
    }
    
    # Old token: should pass with 0.55 (< 0.60 old threshold)
    result = soft_kill(ans, age_minutes=1440.0)  # 24 hours
    assert result is None, f"Old token with momentum 0.55 should pass, got {result}"


def test_soft_kill_age_aware_momentum_boundary():
    """Test age boundary at 60 minutes exactly."""
    from filter import soft_kill
    
    ans_young = {
        "momentum_already_spent": {"type": "noul", "noul": 0.74},
    }
    ans_old = {
        "momentum_already_spent": {"type": "noul", "noul": 0.74},
    }
    
    # 59.9 minutes: young threshold (0.85), should pass
    result_young = soft_kill(ans_young, age_minutes=59.9)
    assert result_young is None, "Token at 59.9m with momentum 0.74 should pass (young threshold)"
    
    # 60.0 minutes: old threshold (0.60), should kill
    result_old = soft_kill(ans_old, age_minutes=60.0)
    assert result_old is not None, "Token at 60m with momentum 0.74 should be killed (old threshold)"
    assert result_old[0] == "momentum_already_spent"
    assert result_old[1] == 0.74


def test_soft_kill_other_checks_unaffected():
    """Age-aware logic should only affect momentum_already_spent, not other soft kills."""
    from filter import soft_kill
    
    # concentration_is_exit_risk should use same threshold regardless of age
    ans_conc = {
        "concentration_is_exit_risk": {"type": "noul", "noul": 0.58},
        "momentum_already_spent": {"type": "noul", "noul": 0.30},
    }
    
    # Should kill on concentration for both young and old (max 0.55)
    result_young = soft_kill(ans_conc, age_minutes=20.0)
    assert result_young is not None
    assert result_young[0] == "concentration_is_exit_risk"
    
    result_old = soft_kill(ans_conc, age_minutes=120.0)
    assert result_old is not None
    assert result_old[0] == "concentration_is_exit_risk"


def test_end_to_end_young_momentum_pass(monkeypatch):
    """End-to-end: Young token with momentum 0.74 should pass soft and reach pick."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    import main as shift
    import collect
    
    tid = f"YoungMomentum:{1399811149}"
    
    def fake_shortlist_young(fomo, id_list):
        return [{"tid": tid, "addr": "YoungMomentum", "net": 1399811149, "ticker": "YMTM",
                 "age_minutes": 22.7, "liquidity_usd": 50000, "volume_h24": 100000,
                 "mcap_usd": 500000, "holder_count": 200, "price_usd": 0.005,
                 "change": {"5m": 0.05, "1h": 0.15, "4h": 0.30, "24h": 0.50}}]
    
    def fake_judge_momentum(question_set, state):
        if question_set == "market":
            return {"model": "test", "answers": {
                "momentum_already_spent": {"type": "noul", "noul": 0.74},  # Would kill old, passes young
                "concentration_is_exit_risk": {"type": "noul", "noul": 0.45},
                "liquidity_fits_ticket": {"type": "noul", "noul": 0.70},
                "shape": {"type": "choice", "choice": "crowd", "probabilities": {"crowd": 0.8}}
            }, "usage": {}}
        elif question_set == "solana":
            return {"model": "test", "answers": {}, "usage": {}}
        elif question_set == "pick":
            # Single survivor now goes through pick gates
            return {"model": "test", "answers": {
                "best": {"type": "choice", "choice": "YMTM", "confidence": 0.85, 
                        "probabilities": {"YMTM": 0.85}},
                "worth_trading_at_all": {"type": "noul", "noul": 0.75}
            }, "usage": {}}
        return {"model": "test", "answers": {}, "usage": {}}
    
    def fake_dossier_young(t, limiter=None):
        return {**t, "chain": "solana", "top_10_percent": 35, "top_wallet_percent": 0.03,
                "developer_holding_percentage": 3, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a young token", "x_handle": None}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_young)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400,
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_young)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), fake_judge_momentum, desk, desk.bank(), shadow=True, gt_dossier_reserve=3)
    
    # Should NOT be soft-killed on momentum
    assert stats.get("soft", {}).get("momentum_already_spent", 0) == 0, \
        "Young token with momentum 0.74 should not be soft-killed"
    assert stats.get("judged", 0) == 1, "Token should reach judged stage"
    
    # Check token record shows it passed soft stage
    token_rows = stats.get("tokens", [])
    judged_rows = [t for t in token_rows if t.get("stage") == "judged"]
    assert len(judged_rows) == 1, f"Expected 1 judged token, got {len(judged_rows)}"


def test_end_to_end_old_momentum_kill(monkeypatch):
    """End-to-end: Old token with momentum 0.74 should be soft-killed."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    import main as shift
    import collect
    
    tid = f"OldMomentum:{1399811149}"
    
    def fake_shortlist_old(fomo, id_list):
        return [{"tid": tid, "addr": "OldMomentum", "net": 1399811149, "ticker": "OMTM",
                 "age_minutes": 120.0, "liquidity_usd": 50000, "volume_h24": 100000,
                 "mcap_usd": 500000, "holder_count": 200, "price_usd": 0.005,
                 "change": {"5m": 0.02, "1h": 0.08, "4h": 0.20, "24h": 0.45}}]
    
    def fake_judge_momentum_old(question_set, state):
        if question_set == "market":
            return {"model": "test", "answers": {
                "momentum_already_spent": {"type": "noul", "noul": 0.74},  # Kills old (> 0.60)
                "concentration_is_exit_risk": {"type": "noul", "noul": 0.45},
                "liquidity_fits_ticket": {"type": "noul", "noul": 0.70},
                "shape": {"type": "choice", "choice": "crowd", "probabilities": {"crowd": 0.8}}
            }, "usage": {}}
        elif question_set == "solana":
            return {"model": "test", "answers": {}, "usage": {}}
        return {"model": "test", "answers": {}, "usage": {}}
    
    def fake_dossier_old(t, limiter=None):
        return {**t, "chain": "solana", "top_10_percent": 35, "top_wallet_percent": 0.03,
                "developer_holding_percentage": 3, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "an old token", "x_handle": None}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_old)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: ({"buys_h1": 540, "sells_h1": 120,
                                                            "buys_h6": 900, "sells_h6": 400,
                                                            "trades_h24": 4000}, 'ok'))
    monkeypatch.setattr(shift, "dossier", fake_dossier_old)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), fake_judge_momentum_old, desk, desk.bank(), shadow=True, gt_dossier_reserve=3)
    
    # Should be soft-killed on momentum
    assert stats.get("soft", {}).get("momentum_already_spent", 0) == 1, \
        "Old token with momentum 0.74 should be soft-killed"
    
    # Check token record shows soft kill
    token_rows = stats.get("tokens", [])
    soft_rows = [t for t in token_rows if t.get("stage") == "soft" and t.get("reason") == "momentum_already_spent"]
    assert len(soft_rows) == 1, f"Expected 1 soft-killed token on momentum, got {len(soft_rows)}"
    assert soft_rows[0].get("soft_noul") == 0.74


def test_trade_kill_logs_ticker_and_age(monkeypatch, caplog):
    """Trade kills should log ticker, age_minutes, and tid for visibility."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"TradeKillAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_trade(fomo, id_list):
        # Token that passes free_kill but fails trade_kill
        return [tok(0, tid=tid, addr="TradeKillAddr1", ticker="LOWTR", age_minutes=35,
                    liquidity_usd=50000, volume_h24=100000, mcap_usd=500000)]
    
    def fake_trade_counts(t, gt_txns_cache=None):
        return {"buys_h1": 10, "sells_h1": 2, "buys_h6": 20, "sells_h6": 5, "trades_h24": 50}, 'ok'
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_trade)
    monkeypatch.setattr(shift, "trade_counts", fake_trade_counts)
    
    desk = FakeDesk()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    # Find the trade kill log line
    trade_logs = [r for r in caplog.records if "trade tid=" in r.message and "reason=trades" in r.message]
    assert len(trade_logs) >= 1, f"Expected trade kill log, got: {[r.message for r in caplog.records if 'trade' in r.message]}"
    
    log_msg = trade_logs[0].message
    assert "ticker=LOWTR" in log_msg, f"Expected ticker=LOWTR in log, got: {log_msg}"
    assert "age_minutes=35" in log_msg or "age_minutes=35.0" in log_msg, f"Expected age_minutes=35 in log, got: {log_msg}"
    assert tid in log_msg, f"Expected tid {tid} in log, got: {log_msg}"
    
    # Verify stats tracked the kill
    assert stats.get("trade", {}).get("trades", 0) == 1


def test_chain_kill_logs_ticker_and_age(monkeypatch, caplog):
    """Chain kills should log ticker, age_minutes, and tid for visibility."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"ChainKillAddr1:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_chain(fomo, id_list):
        # Token that passes free_kill and trade_kill
        return [tok(0, tid=tid, addr="ChainKillAddr1", ticker="TOPWL", age_minutes=45,
                    liquidity_usd=50000, volume_h24=100000, mcap_usd=500000)]
    
    def fake_trade_counts(t, gt_txns_cache=None):
        return {"buys_h1": 100, "sells_h1": 50, "buys_h6": 500, "sells_h6": 200, "trades_h24": 2000}, 'ok'
    
    def fake_dossier_top_wallet(t, limiter=None):
        # Token with excessive top wallet concentration
        return {**t, "chain": "solana", "top_wallet_percent": 0.15, "top_10_percent": 40,
                "holder_count": 200, "mint_authority": False, "freeze_authority": False}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_chain)
    monkeypatch.setattr(shift, "trade_counts", fake_trade_counts)
    monkeypatch.setattr(shift, "dossier", fake_dossier_top_wallet)
    
    desk = FakeDesk()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    # Find the chain kill log line
    chain_logs = [r for r in caplog.records if "chain tid=" in r.message and "reason=top_wallet" in r.message]
    assert len(chain_logs) >= 1, f"Expected chain kill log, got: {[r.message for r in caplog.records if 'chain' in r.message]}"
    
    log_msg = chain_logs[0].message
    assert "ticker=TOPWL" in log_msg, f"Expected ticker=TOPWL in log, got: {log_msg}"
    assert "age_minutes=45" in log_msg or "age_minutes=45.0" in log_msg, f"Expected age_minutes=45 in log, got: {log_msg}"
    assert tid in log_msg, f"Expected tid {tid} in log, got: {log_msg}"
    
    # Verify stats tracked the kill
    assert stats.get("chain", {}).get("top_wallet", 0) == 1


def test_chain_kill_logs_young_token_top_10(monkeypatch, caplog):
    """Chain kills on young tokens should show age clearly in logs (historical CATCRAFT case)."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    tid = f"YoungTop10:{1399811149}"
    ids = [tid]
    
    def fake_shortlist_young(fomo, id_list):
        # Young token (30 minutes old)
        return [tok(0, tid=tid, addr="YoungTop10", ticker="CATCRAFT", age_minutes=30,
                    liquidity_usd=50000, volume_h24=100000, mcap_usd=500000)]
    
    def fake_trade_counts(t, gt_txns_cache=None):
        return {"buys_h1": 100, "sells_h1": 50, "buys_h6": 500, "sells_h6": 200, "trades_h24": 2000}, 'ok'
    
    def fake_dossier_top10(t, limiter=None):
        # Token with excessive top 10 concentration
        return {**t, "chain": "solana", "top_wallet_percent": 0.03, "top_10_percent": 70,
                "holder_count": 200, "mint_authority": False, "freeze_authority": False}
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (ids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist_young)
    monkeypatch.setattr(shift, "trade_counts", fake_trade_counts)
    monkeypatch.setattr(shift, "dossier", fake_dossier_top10)
    
    desk = FakeDesk()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    # Find the chain kill log line
    chain_logs = [r for r in caplog.records if "chain tid=" in r.message and "reason=top_10" in r.message]
    assert len(chain_logs) >= 1, f"Expected chain kill log for young CATCRAFT, got: {[r.message for r in caplog.records if 'chain' in r.message]}"
    
    log_msg = chain_logs[0].message
    assert "ticker=CATCRAFT" in log_msg, f"Expected ticker=CATCRAFT in log, got: {log_msg}"
    # Young token, age should be clearly visible as ~30m (wall before soft)
    assert "age_minutes=30" in log_msg or "age_minutes=30.0" in log_msg, f"Expected age_minutes=30 in log (wall before soft), got: {log_msg}"
    assert tid in log_msg, f"Expected tid {tid} in log, got: {log_msg}"
    
    # Verify stats tracked the kill
    assert stats.get("chain", {}).get("top_10", 0) == 1


# ============================================================================
# GTRateLimiter 429 backoff tests
# ============================================================================

def test_gt_rate_limiter_record_429_with_retry_after():
    """GTRateLimiter should record 429 and set backoff based on Retry-After."""
    fake_time = [1000.0]
    
    def time_fn():
        return fake_time[0]
    
    limiter = collect.GTRateLimiter(calls_per_min=8, time_fn=time_fn)
    
    # Record 429 with 10-second Retry-After
    limiter.record_429(retry_after_sec=10.0)
    
    # Backoff should be set to now + 10 seconds
    assert limiter.backoff_until == 1010.0, f"Expected backoff_until=1010.0, got {limiter.backoff_until}"


def test_gt_rate_limiter_record_429_default_backoff():
    """GTRateLimiter should use 5s default when Retry-After is None."""
    fake_time = [1000.0]
    
    def time_fn():
        return fake_time[0]
    
    limiter = collect.GTRateLimiter(calls_per_min=8, time_fn=time_fn)
    
    # Record 429 without Retry-After
    limiter.record_429(retry_after_sec=None)
    
    # Should use floor backoff (25s default)
    assert limiter.backoff_until == 1025.0, f"Expected backoff_until=1025.0 (floor backoff), got {limiter.backoff_until}"


def test_gt_rate_limiter_wait_if_needed_enforces_429_backoff():
    """wait_if_needed should sleep until backoff period expires."""
    fake_time = [1000.0]
    sleep_calls = []
    
    def time_fn():
        return fake_time[0]
    
    def fake_sleep(duration):
        sleep_calls.append(duration)
        fake_time[0] += duration
    
    limiter = collect.GTRateLimiter(calls_per_min=8, time_fn=time_fn)
    
    # Record 429 with 8-second backoff
    limiter.record_429(retry_after_sec=8.0)
    
    # Monkey-patch time.sleep
    import time as time_module
    original_sleep = time_module.sleep
    try:
        time_module.sleep = fake_sleep
        
        # wait_if_needed should sleep for the backoff period
        limiter.wait_if_needed(priority=True)
        
        # Should have slept for ~8 seconds
        assert len(sleep_calls) > 0, "Expected at least one sleep call"
        assert sleep_calls[0] >= 7.9, f"Expected sleep ~8s, got {sleep_calls[0]:.2f}s"
        
        # After backoff, time should have advanced
        assert fake_time[0] >= 1008.0, f"Expected time >= 1008, got {fake_time[0]}"
        
    finally:
        time_module.sleep = original_sleep


def test_gt_rate_limiter_wait_if_needed_no_backoff_if_expired():
    """wait_if_needed should not sleep if backoff period has already expired."""
    fake_time = [1000.0]
    sleep_calls = []
    
    def time_fn():
        return fake_time[0]
    
    def fake_sleep(duration):
        sleep_calls.append(duration)
        fake_time[0] += duration
    
    limiter = collect.GTRateLimiter(calls_per_min=8, time_fn=time_fn)
    
    # Record 429 with 5-second backoff
    limiter.record_429(retry_after_sec=5.0)
    
    # Advance time past the backoff period
    fake_time[0] = 1006.0
    
    # Monkey-patch time.sleep
    import time as time_module
    original_sleep = time_module.sleep
    try:
        time_module.sleep = fake_sleep
        
        # wait_if_needed should not sleep (backoff expired)
        limiter.wait_if_needed(priority=True)
        
        # Should not have slept for backoff (backoff already expired)
        backoff_sleeps = [s for s in sleep_calls if s >= 1.0]
        assert len(backoff_sleeps) == 0, f"Expected no backoff sleep, got {backoff_sleeps}"
        
    finally:
        time_module.sleep = original_sleep


def test_dossier_429_calls_record_429_on_limiter(monkeypatch):
    """dossier() should call limiter.record_429() when it gets a 429 response."""
    monkeypatch.setattr(collect, "sol_top_wallet", _pass_sol_wallet)
    fake_time = [1000.0]
    
    def time_fn():
        return fake_time[0]
    
    limiter = collect.GTRateLimiter(calls_per_min=8, time_fn=time_fn)
    
    # Create a fake 429 response
    class FakeResponse:
        status_code = 429
        headers = {"Retry-After": "7"}
    
    def fake_get(url, headers=None, timeout=None):
        return FakeResponse()
    
    # Monkey-patch requests.get
    original_get = requests.get
    try:
        monkeypatch.setattr(requests, "get", fake_get)
        
        token = {"tid": "test:123", "ticker": "TEST", "addr": "testaddr", "net": 1399811149}
        
        try:
            collect.dossier(token, limiter=limiter)
            assert False, "Expected DossierRetryNeeded"
        except collect.DossierRetryNeeded:
            pass
        
        # Limiter should have recorded the 429 with Retry-After
        assert limiter.backoff_until == 1007.0, \
            f"Expected limiter.backoff_until=1007.0 (7s backoff), got {limiter.backoff_until}"
        
    finally:
        requests.get = original_get


def test_sol_top_wallet_success(monkeypatch):
    """sol_top_wallet should return concentration on success."""
    calls = []
    
    def fake_post(url, json=None, timeout=None):
        calls.append((url, json["method"]))
        method = json["method"]
        
        if method == "getTokenSupply":
            return Mock(json=lambda: {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"value": {"amount": "1000000"}}
            })
        elif method == "getTokenLargestAccounts":
            return Mock(json=lambda: {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"value": [{"address": "test_account_1", "amount": "100000"}]}
            })
        elif method == "getMultipleAccounts":
            # Mock owner resolution - return None to simulate uncached/unresolved owners
            # This will make the code fall back to including the account
            return Mock(json=lambda: {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"value": [None]}  # Failed to resolve owner
            })
    
    monkeypatch.setattr(requests, "post", fake_post)
    
    holder_data, rpc_ok, error = collect.sol_top_wallet("test_mint")
    
    assert holder_data["top_wallet"] == 0.1
    assert rpc_ok is True
    assert error is None
    # With pool-aware logic: getTokenSupply, getTokenLargestAccounts, getMultipleAccounts (for owner resolution)
    assert len(calls) >= 2  # At least supply and largest accounts
    assert calls[0][1] == "getTokenSupply"
    assert calls[1][1] == "getTokenLargestAccounts"
    # May include getMultipleAccounts for owner resolution
    if len(calls) > 2:
        assert calls[2][1] == "getMultipleAccounts"


def test_sol_top_wallet_json_rpc_error(monkeypatch):
    """sol_top_wallet should handle JSON-RPC error responses."""
    def fake_post(url, json=None, timeout=None):
        return Mock(json=lambda: {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32600, "message": "Invalid request"}
        })
    
    monkeypatch.setattr(requests, "post", fake_post)
    
    holder_data, rpc_ok, error = collect.sol_top_wallet("test_mint")
    
    assert holder_data["top_wallet"] is None
    assert rpc_ok is False
    assert "Invalid request" in error
    assert "code -32600" in error


def test_sol_top_wallet_429_retry_success(monkeypatch):
    """sol_top_wallet should retry once on 429 and succeed."""
    call_count = [0]
    
    def fake_post(url, json=None, timeout=None):
        method = json["method"]
        
        if method == "getTokenSupply":
            return Mock(json=lambda: {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"value": {"amount": "1000000"}}
            })
        elif method == "getTokenLargestAccounts":
            call_count[0] += 1
            if call_count[0] == 1:
                return Mock(
                    status_code=429,
                    json=lambda: {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "error": {"code": 429, "message": "Too Many Requests"}
                    }
                )
            else:
                return Mock(json=lambda: {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"value": [{"address": "test_account_1", "amount": "100000"}]}
                })
    
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(time, "sleep", lambda x: None)
    
    holder_data, rpc_ok, error = collect.sol_top_wallet("test_mint")
    
    assert holder_data["top_wallet"] == 0.1
    assert rpc_ok is True
    assert error is None
    assert call_count[0] == 2


def test_sol_top_wallet_429_retry_failure(monkeypatch):
    """sol_top_wallet should return error after 429 retry fails."""
    def fake_post(url, json=None, timeout=None):
        method = json["method"]
        
        if method == "getTokenSupply":
            return Mock(json=lambda: {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"value": {"amount": "1000000"}}
            })
        elif method == "getTokenLargestAccounts":
            return Mock(
                status_code=429,
                json=lambda: {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": 429, "message": "Too Many Requests"}
                }
            )
    
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(time, "sleep", lambda x: None)
    
    holder_data, rpc_ok, error = collect.sol_top_wallet("test_mint")
    
    assert holder_data["top_wallet"] is None
    assert rpc_ok is False
    assert "Too Many Requests" in error


def test_sol_top_wallet_missing_result_field(monkeypatch):
    """sol_top_wallet should handle missing result field."""
    def fake_post(url, json=None, timeout=None):
        return Mock(json=lambda: {
            "jsonrpc": "2.0",
            "id": 1
        })
    
    monkeypatch.setattr(requests, "post", fake_post)
    
    holder_data, rpc_ok, error = collect.sol_top_wallet("test_mint")
    
    assert holder_data["top_wallet"] is None
    assert rpc_ok is False
    assert "missing result field" in error


def test_sol_top_wallet_uses_env_var(monkeypatch):
    """sol_top_wallet should read RPC URL from SOLANA_RPC_URL env var."""
    test_url = "https://custom-rpc.example.com"
    monkeypatch.setenv("SOLANA_RPC_URL", test_url)
    
    calls = []
    
    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        return Mock(json=lambda: {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"value": {"amount": "1000000"}} if json["method"] == "getTokenSupply" else {"value": [{"address": "test_account_1", "amount": "100000"}]}
        })
    
    monkeypatch.setattr(requests, "post", fake_post)
    
    collect.sol_top_wallet("test_mint")
    
    assert all(url == test_url for url in calls)


def test_dossier_captures_rpc_status(monkeypatch):
    """dossier should capture rpc_ok and rpc_error for Solana tokens."""
    def fake_sol_top_wallet(mint):
        return {"top_wallet": None, "top_10": None, "pools_excluded": False}, False, "getTokenLargestAccounts: Too Many Requests (code 429)"
    
    def fake_get(url, headers=None, timeout=None):
        return Mock(
            status_code=200,
            json=lambda: {
                "data": {
                    "attributes": {
                        "holders": {"count": 100, "distribution_percentage": {"top_10": 30}},
                        "developer_holding_percentage": 5.0,
                        "gt_score_details": {},
                        "is_honeypot": False,
                        "mint_authority": None,
                        "freeze_authority": None,
                        "description": "Test token",
                        "twitter_handle": "test"
                    }
                }
            }
        )
    
    monkeypatch.setattr(collect, "sol_top_wallet", fake_sol_top_wallet)
    monkeypatch.setattr(requests, "get", fake_get)
    
    token = {
        "tid": "test:1399811149",
        "ticker": "TEST",
        "addr": "testaddr",
        "net": 1399811149,
        "holder_count": 100
    }
    
    result = collect.dossier(token)
    
    assert result["top_wallet_percent"] is None
    assert result["rpc_ok"] is False
    assert "Too Many Requests" in result["rpc_error"]
def test_dossier_waits_before_spending_on_retry(monkeypatch):
    """dossier() should wait for backoff BEFORE spending a slot (verifies fix for ~50ms bug).
    
    This test verifies the root cause fix: wait_if_needed() is called BEFORE spend()
    in dossier(), ensuring that retry attempts honor the backoff period set by record_429().
    
    Before the fix, spend() was called first, adding a timestamp to the rolling window
    before waiting, which could cause the retry to happen ~50ms after the first 429
    instead of honoring the Retry-After value.
    """
    monkeypatch.setattr(collect, "sol_top_wallet", _pass_sol_wallet)
    fake_time = [1000.0]
    sleep_calls = []
    spend_calls = []
    
    def time_fn():
        return fake_time[0]
    
    def fake_sleep(duration):
        sleep_calls.append(("sleep", duration, fake_time[0]))
        fake_time[0] += duration
    
    # Track when spend() is called relative to sleep
    limiter = collect.GTRateLimiter(calls_per_min=8, time_fn=time_fn)
    original_spend = limiter.spend
    def tracked_spend(cost=1, priority=False):
        spend_calls.append(("spend", fake_time[0]))
        return original_spend(cost, priority)
    limiter.spend = tracked_spend
    
    # First call: 429 with 5-second Retry-After
    class FakeResponse429:
        status_code = 429
        headers = {"Retry-After": "5"}
    
    # Second call: success
    class FakeResponseOK:
        status_code = 200
        def json(self):
            return {"data": {"attributes": {}}}
    
    call_count = [0]
    def fake_get(url, headers=None, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return FakeResponse429()
        return FakeResponseOK()
    
    import time as time_module
    original_sleep = time_module.sleep
    original_get = requests.get
    
    try:
        time_module.sleep = fake_sleep
        requests.get = fake_get
        
        token = {"tid": "test:123", "ticker": "TEST", "addr": "testaddr", "net": 1399811149,
                 "holder_count": 500, "age_minutes": 30}
        
        # First call: should hit 429 and set backoff
        try:
            collect.dossier(token, limiter=limiter)
            assert False, "Expected DossierRetryNeeded on first call"
        except collect.DossierRetryNeeded:
            pass
        
        # Verify backoff was set to T0 + 5
        assert limiter.backoff_until == 1005.0, \
            f"Expected backoff_until=1005.0, got {limiter.backoff_until}"
        
        # Reset counters for second call
        sleep_calls.clear()
        spend_calls.clear()
        
        # Second call: should wait for backoff BEFORE spending
        d = collect.dossier(token, limiter=limiter)
        
        # Verify sequence: sleep BEFORE spend
        assert len(sleep_calls) > 0, "Expected at least one sleep call"
        assert len(spend_calls) > 0, "Expected at least one spend call"
        
        # Find the backoff sleep (>= 4 seconds)
        backoff_sleep = next((s for s in sleep_calls if s[1] >= 4.0), None)
        assert backoff_sleep is not None, f"Expected backoff sleep >= 4s, got {sleep_calls}"
        
        # Find the first spend call
        first_spend = spend_calls[0]
        
        # Verify spend happened AFTER the backoff sleep
        backoff_sleep_time = backoff_sleep[2]  # time when sleep started
        backoff_duration = backoff_sleep[1]
        spend_time = first_spend[1]
        
        assert spend_time >= backoff_sleep_time + backoff_duration, \
            f"spend() should be called AFTER backoff sleep completes. " \
            f"Backoff: {backoff_duration}s starting at T={backoff_sleep_time}, " \
            f"spend() at T={spend_time}"
        
        # Verify the dossier succeeded
        assert d["ticker"] == "TEST"
        
    finally:
        time_module.sleep = original_sleep
        requests.get = original_get


# ---- BUG 1: GT 429 retry with 0s Retry-After floor backoff -------------------
def test_gt_429_retry_after_zero_applies_floor_backoff():
    """GT 429 with Retry-After: 0 should apply floor backoff (25s default), not 0s."""
    limiter = collect.GTRateLimiter(calls_per_min=8, backoff_floor_sec=25.0, time_fn=lambda: 1000.0)
    
    # Record 429 with Retry-After=0
    limiter.record_429(0.0)
    
    # Should apply floor backoff, not 0
    assert limiter.backoff_until == 1025.0, f"Expected backoff_until=1025.0, got {limiter.backoff_until}"
    assert limiter.consecutive_429s == 1
    assert limiter.saturated is True


def test_gt_429_missing_retry_after_applies_floor_backoff():
    """GT 429 with missing Retry-After should apply floor backoff."""
    limiter = collect.GTRateLimiter(calls_per_min=8, backoff_floor_sec=25.0, time_fn=lambda: 1000.0)
    
    # Record 429 with None (missing Retry-After)
    limiter.record_429(None)
    
    # Should apply floor backoff
    assert limiter.backoff_until == 1025.0
    assert limiter.consecutive_429s == 1
    assert limiter.saturated is True


def test_gt_429_consecutive_grows_backoff():
    """Consecutive 429s should grow backoff: floor * (1.5^(n-1)), capped at max."""
    now = [1000.0]
    limiter = collect.GTRateLimiter(calls_per_min=8, backoff_floor_sec=20.0, 
                                    backoff_max_sec=120.0, time_fn=lambda: now[0])
    
    # First 429: floor = 20s
    limiter.record_429(0.0)
    assert limiter.backoff_until == 1020.0
    assert limiter.consecutive_429s == 1
    
    # Second 429: floor * 1.5 = 30s
    now[0] = 1020.0
    limiter.record_429(0.0)
    assert limiter.backoff_until == 1050.0  # 1020 + 30
    assert limiter.consecutive_429s == 2
    
    # Third 429: floor * 1.5^2 = 45s
    now[0] = 1050.0
    limiter.record_429(0.0)
    assert limiter.backoff_until == 1095.0  # 1050 + 45
    assert limiter.consecutive_429s == 3
    
    # Fourth 429: floor * 1.5^3 = 67.5s
    now[0] = 1095.0
    limiter.record_429(0.0)
    assert limiter.backoff_until == 1162.5  # 1095 + 67.5
    
    # Fifth 429: floor * 1.5^4 = 101.25s, within cap
    now[0] = 1162.5
    limiter.record_429(0.0)
    assert limiter.backoff_until == 1263.75  # 1162.5 + 101.25
    
    # Sixth 429: floor * 1.5^5 = 151.875s, capped at 120s
    now[0] = 1263.75
    limiter.record_429(0.0)
    assert limiter.backoff_until == 1383.75  # 1263.75 + 120 (capped)


def test_gt_429_saturated_cleared_on_success():
    """Successful GT call should clear saturated flag and reset consecutive_429s."""
    limiter = collect.GTRateLimiter(calls_per_min=8, time_fn=lambda: 1000.0)
    
    # Trigger 429
    limiter.record_429(0.0)
    assert limiter.saturated is True
    assert limiter.consecutive_429s == 1
    
    # Success should clear
    limiter.record_success()
    assert limiter.saturated is False
    assert limiter.consecutive_429s == 0


def test_gt_wait_before_spend_not_spend_then_wait():
    """Universe scan should wait-before-spend to avoid post-backoff burst."""
    now = [1000.0]
    sleep_calls = []
    original_sleep = time.sleep
    time.sleep = lambda x: sleep_calls.append(x)
    
    try:
        limiter = collect.GTRateLimiter(calls_per_min=8, time_fn=lambda: now[0])
        
        # Fill budget
        for i in range(8):
            limiter.calls.append(now[0])
        
        # Try to spend another - should wait for window to expire
        now[0] = 1030.0  # 30s later, oldest call still in window
        
        result = limiter.spend(1, priority=False)
        
        # Old behavior: spend would fail silently (return False)
        # New behavior: wait_if_needed() must be called first by caller
        # Since we're testing spend alone, it should return False (budget full)
        assert result is False
        
    finally:
        time.sleep = original_sleep


# ---- BUG 2: DexScreener failures as no_pair kills ----------------------------
def test_trade_counts_distinguishes_error_from_empty():
    """trade_counts should return 'error' for HTTP errors, 'empty' for no pairs."""
    original_get = requests.get
    
    try:
        # HTTP 500 error
        mock_500 = Mock()
        mock_500.status_code = 500
        requests.get = Mock(return_value=mock_500)
        
        token = {"addr": "test", "ticker": "TEST", "net": 1399811149}
        data, status = collect.trade_counts(token)
        assert status == "error"
        assert data["trades_h24"] is None
        
        # HTTP 200 with empty array (genuine no_pair)
        mock_200_empty = Mock()
        mock_200_empty.status_code = 200
        mock_200_empty.json.return_value = []
        requests.get = Mock(return_value=mock_200_empty)
        
        data, status = collect.trade_counts(token)
        assert status == "empty"
        assert data["trades_h24"] is None
        
        # HTTP 200 with null (genuine no_pair)
        mock_200_null = Mock()
        mock_200_null.status_code = 200
        mock_200_null.json.return_value = None
        requests.get = Mock(return_value=mock_200_null)
        
        data, status = collect.trade_counts(token)
        assert status == "empty"  # null is genuine empty
        
        # Network error
        requests.get = Mock(side_effect=requests.exceptions.ConnectionError("Network error"))
        data, status = collect.trade_counts(token)
        assert status == "error"
        
    finally:
        requests.get = original_get


def test_trade_counts_ok_status_with_pairs():
    """trade_counts should return 'ok' when pairs data is present."""
    original_get = requests.get
    
    try:
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{
            "liquidity": {"usd": 50000},
            "txns": {
                "h1": {"buys": 10, "sells": 8},
                "h6": {"buys": 50, "sells": 45},
                "h24": {"buys": 200, "sells": 180}
            }
        }]
        requests.get = Mock(return_value=mock_resp)
        
        token = {"addr": "test", "ticker": "TEST", "net": 1399811149}
        data, status = collect.trade_counts(token)
        
        assert status == "ok"
        assert data["trades_h24"] == 380
        assert data["buys_h1"] == 10
        assert data["sells_h1"] == 8
        
    finally:
        requests.get = original_get


def test_dex_canary_check_healthy():
    """Canary check should return True when known liquid token has pairs."""
    original_get = requests.get
    
    try:
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "pairs": [{"liquidity": {"usd": 1000000}}]
        }
        requests.get = Mock(return_value=mock_resp)
        
        healthy = collect.dex_canary_check(1399811149)  # Solana
        assert healthy is True
        
    finally:
        requests.get = original_get


def test_dex_canary_check_degraded():
    """Canary check should return False when known liquid token has no pairs."""
    original_get = requests.get
    
    try:
        # Empty array
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        requests.get = Mock(return_value=mock_resp)
        
        healthy = collect.dex_canary_check(1399811149)
        assert healthy is False
        
        # Null response
        mock_resp_null = Mock()
        mock_resp_null.status_code = 200
        mock_resp_null.json.return_value = None
        requests.get = Mock(return_value=mock_resp_null)
        
        healthy = collect.dex_canary_check(1399811149)
        assert healthy is False
        
        # HTTP error
        mock_resp_err = Mock()
        mock_resp_err.status_code = 500
        requests.get = Mock(return_value=mock_resp_err)
        
        healthy = collect.dex_canary_check(1399811149)
        assert healthy is False
        
    finally:
        requests.get = original_get


def test_filter_trade_kill_dex_error_reason():
    """trade_kill should return 'dex_error' for error status, not 'no_pair'."""
    # Error status
    token_error = {
        "dex_status": "error",
        "trades_h24": None,
        "buys_h1": None,
        "sells_h1": None
    }
    assert trade_kill(token_error) == "dex_error"
    
    # Empty status (genuine no pair)
    token_empty = {
        "dex_status": "empty",
        "trades_h24": None,
        "buys_h1": None,
        "sells_h1": None
    }
    assert trade_kill(token_empty) == "no_pair"
    
    # OK status with data
    token_ok = {
        "dex_status": "ok",
        "trades_h24": 200,
        "buys_h1": 10,
        "sells_h1": 8
    }
    assert trade_kill(token_ok) is None  # pass


def test_no_pair_bench_time():
    """no_pair should have short bench time (12 min) in BENCH_MINUTES."""
    assert book.BENCH_MINUTES.get("no_pair") == 12


# ---- GT fallback for DexScreener degradation ----------------------------------
def test_trade_counts_gt_fallback_on_dex_error():
    """trade_counts should use GT fallback when DexScreener errors."""
    original_get = requests.get
    
    try:
        # DexScreener errors
        mock_500 = Mock()
        mock_500.status_code = 500
        requests.get = Mock(return_value=mock_500)
        
        token = {"addr": "test", "ticker": "TEST", "tid": "test:1399811149", "net": 1399811149}
        gt_cache = {
            "test:1399811149": {
                "h1": {"buys": 10, "sells": 5},
                "h6": {"buys": 50, "sells": 25},
                "h24": {"buys": 200, "sells": 100},
                "_liq_usd": 1000
            }
        }
        
        data, status = collect.trade_counts(token, gt_txns_cache=gt_cache)
        assert status == "gt_fallback"
        assert data["trades_h24"] == 300
        assert data["buys_h1"] == 10
        assert data["sells_h6"] == 25
        
    finally:
        requests.get = original_get


def test_trade_counts_gt_fallback_on_dex_null_pairs():
    """trade_counts should use GT fallback when DexScreener returns null."""
    original_get = requests.get
    
    try:
        # DexScreener returns null (degradation)
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = None
        requests.get = Mock(return_value=mock_resp)
        
        token = {"addr": "test", "ticker": "TEST", "tid": "test:1399811149", "net": 1399811149}
        gt_cache = {
            "test:1399811149": {
                "h1": {"buys": 15, "sells": 8},
                "h6": {"buys": 60, "sells": 30},
                "h24": {"buys": 250, "sells": 120},
                "_liq_usd": 1000
            }
        }
        
        data, status = collect.trade_counts(token, gt_txns_cache=gt_cache)
        assert status == "gt_fallback"
        assert data["trades_h24"] == 370
        
    finally:
        requests.get = original_get


def test_trade_counts_gt_fallback_on_dex_empty_with_gt_data():
    """trade_counts should use GT fallback when Dex returns empty array but GT has data."""
    original_get = requests.get
    
    try:
        # DexScreener returns empty array
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        requests.get = Mock(return_value=mock_resp)
        
        token = {"addr": "test", "ticker": "TEST", "tid": "test:1399811149", "net": 1399811149}
        gt_cache = {
            "test:1399811149": {
                "h1": {"buys": 20, "sells": 10},
                "h6": {"buys": 80, "sells": 40},
                "h24": {"buys": 300, "sells": 150},
                "_liq_usd": 1000
            }
        }
        
        data, status = collect.trade_counts(token, gt_txns_cache=gt_cache)
        assert status == "gt_fallback"
        assert data["trades_h24"] == 450
        
    finally:
        requests.get = original_get


def test_trade_counts_empty_when_no_gt_fallback():
    """trade_counts should return error when Dex errors and no GT data available."""
    original_get = requests.get
    
    try:
        mock_500 = Mock()
        mock_500.status_code = 500
        requests.get = Mock(return_value=mock_500)
        
        token = {"addr": "test", "ticker": "TEST", "tid": "test:1399811149", "net": 1399811149}
        # No GT cache
        data, status = collect.trade_counts(token, gt_txns_cache={})
        assert status == "error"
        assert data["trades_h24"] is None
        
    finally:
        requests.get = original_get


def test_universe_returns_gt_txns_cache():
    """universe() should return tuple (ids, gt_txns_cache) with transaction data."""
    original_get = requests.get
    
    try:
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [{
                "relationships": {
                    "base_token": {
                        "data": {"id": "solana_TestAddr1"}
                    }
                },
                "attributes": {
                    "transactions": {
                        "h1": {"buys": 10, "sells": 5},
                        "h6": {"buys": 50, "sells": 25},
                        "h24": {"buys": 200, "sells": 100}
                    }
                }
            }]
        }
        requests.get = Mock(return_value=mock_resp)
        
        limiter = collect.GTRateLimiter(calls_per_min=8, time_fn=lambda: 1000.0)
        ids, gt_cache = collect.universe(nets=("solana",), pages=1, include_trending=False, limiter=limiter)
        
        assert len(ids) == 1
        assert "TestAddr1:1399811149" in ids
        assert "TestAddr1:1399811149" in gt_cache
        assert gt_cache["TestAddr1:1399811149"]["h24"]["buys"] == 200
        assert gt_cache["TestAddr1:1399811149"]["h1"]["sells"] == 5
        
    finally:
        requests.get = original_get


def test_dex_chain_id_robinhood():
    """DEX_CHAIN_ID should map Robinhood (4663) to 'robinhood' chainId."""
    assert collect.DEX_CHAIN_ID[4663] == "robinhood"
    
    # Test that trade_counts uses correct chainId for Robinhood
    original_get = requests.get
    
    try:
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{
            "liquidity": {"usd": 50000},
            "txns": {
                "h1": {"buys": 10, "sells": 8},
                "h6": {"buys": 50, "sells": 45},
                "h24": {"buys": 200, "sells": 180}
            }
        }]
        requests.get = Mock(return_value=mock_resp)
        
        token = {"addr": "test", "ticker": "TEST", "net": 4663}
        data, status = collect.trade_counts(token)
        
        # Verify it called with robinhood chainId
        requests.get.assert_called_once()
        call_args = requests.get.call_args
        assert "robinhood" in call_args[0][0]  # URL should contain 'robinhood'
        
    finally:
        requests.get = original_get


def test_gt_cache_prefers_higher_reserve_in_usd():
    """GT cache should prefer pool with higher reserve_in_usd when multiple pools exist for same token."""
    original_get = requests.get
    
    try:
        # Return two pools for same token with different reserve_in_usd
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {
                    "relationships": {
                        "base_token": {"data": {"id": "solana_TokenA"}}
                    },
                    "attributes": {
                        "reserve_in_usd": "50000.50",  # Lower liquidity
                        "transactions": {
                            "h1": {"buys": 10, "sells": 5},
                            "h6": {"buys": 50, "sells": 25},
                            "h24": {"buys": 100, "sells": 50}
                        }
                    }
                },
                {
                    "relationships": {
                        "base_token": {"data": {"id": "solana_TokenA"}}
                    },
                    "attributes": {
                        "reserve_in_usd": "150000.75",  # Higher liquidity
                        "transactions": {
                            "h1": {"buys": 20, "sells": 10},
                            "h6": {"buys": 100, "sells": 50},
                            "h24": {"buys": 300, "sells": 150}
                        }
                    }
                }
            ]
        }
        requests.get = Mock(return_value=mock_resp)
        
        limiter = collect.GTRateLimiter(calls_per_min=8, time_fn=lambda: 1000.0)
        ids, gt_cache = collect.universe(nets=("solana",), pages=1, include_trending=False, limiter=limiter)
        
        # Should use the pool with higher reserve_in_usd (150000.75)
        assert "TokenA:1399811149" in gt_cache
        assert gt_cache["TokenA:1399811149"]["h24"]["buys"] == 300  # From higher liquidity pool
        assert gt_cache["TokenA:1399811149"]["h1"]["buys"] == 20
        assert gt_cache["TokenA:1399811149"]["_liq_usd"] == 150000.75
        
    finally:
        requests.get = original_get


def test_filter_trade_kill_gt_fallback_treated_as_ok():
    """trade_kill should treat gt_fallback status like ok (has data), not error."""
    # GT fallback with good data should pass
    token_gt = {
        "dex_status": "gt_fallback",
        "trades_h24": 300,
        "buys_h1": 20,
        "sells_h1": 10
    }
    assert trade_kill(token_gt) is None  # pass
    
    # GT fallback with too few trades should kill
    token_low = {
        "dex_status": "gt_fallback",
        "trades_h24": 50,  # below min_trades_h24=150
        "buys_h1": 5,
        "sells_h1": 2
    }
    assert trade_kill(token_low) == "trades"
    
    # GT fallback with zero trades should kill
    token_zero = {
        "dex_status": "gt_fallback",
        "trades_h24": 0,  # zero trades
        "buys_h1": 0,
        "sells_h1": 0
    }
    assert trade_kill(token_zero) == "trades"


def test_trade_kill_none_trades_always_no_pair():
    """trade_kill should treat None trades_h24 as no_pair regardless of status."""
    # None with ok status
    token_none_ok = {
        "dex_status": "ok",
        "trades_h24": None,
        "buys_h1": None,
        "sells_h1": None
    }
    assert trade_kill(token_none_ok) == "no_pair"
    
    # None with gt_fallback status (GT had no data)
    token_none_gt = {
        "dex_status": "gt_fallback",
        "trades_h24": None,
        "buys_h1": None,
        "sells_h1": None
    }
    assert trade_kill(token_none_gt) == "no_pair"
    
    # None with empty status
    token_none_empty = {
        "dex_status": "empty",
        "trades_h24": None,
        "buys_h1": None,
        "sells_h1": None
    }
    assert trade_kill(token_none_empty) == "no_pair"


def test_gt_fallback_zero_trades_returns_zero_not_none():
    """GT fallback with 0 buys/0 sells should return trades_h24=0, not None."""
    original_get = requests.get
    
    try:
        # DexScreener errors
        mock_500 = Mock()
        mock_500.status_code = 500
        requests.get = Mock(return_value=mock_500)
        
        token = {"addr": "test", "ticker": "TEST", "tid": "test:1399811149", "net": 1399811149}
        gt_cache = {
            "test:1399811149": {
                "h1": {"buys": 0, "sells": 0},
                "h6": {"buys": 0, "sells": 0},
                "h24": {"buys": 0, "sells": 0},
                "_liq_usd": 1000
            }
        }
        
        data, status = collect.trade_counts(token, gt_txns_cache=gt_cache)
        assert status == "gt_fallback"
        assert data["trades_h24"] == 0  # Must be 0, not None
        assert data["buys_h1"] == 0
        
    finally:
        requests.get = original_get


def test_early_canary_check_triggers_on_consecutive_empties(monkeypatch):
    """Canary check should trigger after 2+ consecutive empties and requeue on degradation."""
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    # Create 3 tokens that will all return empty from DexScreener
    tids = [f"Empty{i}:1399811149" for i in range(3)]
    
    def fake_shortlist(fomo, id_list):
        return [tok(i, tid=tids[i], addr=f"Empty{i}", age_minutes=30, 
                    liquidity_usd=50000, volume_h24=100000, mcap_usd=500000) 
                for i in range(3)]
    
    # Track canary calls
    canary_calls = []
    def mock_canary(net):
        canary_calls.append(net)
        return False  # Canary fails (degraded)
    
    # Mock trade_counts to return empty
    def mock_trade_counts(t, gt_txns_cache=None):
        return {"buys_h1": None, "sells_h1": None, "buys_h6": None, "sells_h6": None, "trades_h24": None}, 'empty'
    
    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: (tids, {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", mock_trade_counts)
    monkeypatch.setattr(shift, "dex_canary_check", mock_canary)
    
    desk = FakeDesk()
    order, stats = shift.run_once(FakeFomo(), JUDGE, desk, desk.bank(), shadow=True, gt_dossier_reserve=12)
    
    # Canary should have been called after 2nd empty
    assert len(canary_calls) >= 1, "Canary should be called after 2+ empties"
    
    # Should mark as degraded
    assert stats.get("dex_degraded") is True
    
    # Tokens should be requeued, not benched
    assert stats.get("requeued", 0) >= 2  # At least the 2nd and 3rd tokens requeued


