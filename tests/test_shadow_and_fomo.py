"""
Tests for shadow PnL ledger and FOMO health tracking.

Covers:
- Shadow ledger entry, mark, close, PnL calculations
- Empty state handling
- FOMO health status (no token leakage)
- CDP activation (mocked)
- Mode flags in state
"""
import json
import os
import pathlib
import sys
import tempfile
import time
from unittest import mock

import pytest

# Add workspace to path for imports
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

# Set test outbox before imports
TEST_OUTBOX = pathlib.Path(tempfile.mkdtemp())
os.environ["DESK_OUTBOX"] = str(TEST_OUTBOX)

import shadow_ledger
from fomo_api import Fomo, activate_fomo_tab


@pytest.fixture(autouse=True)
def clean_ledger(monkeypatch):
    """Point the ledger at this module's temp outbox and clean it before each test.

    shadow_ledger may already have been imported by another test module with a
    different DESK_OUTBOX, so patch LEDGER_PATH instead of relying on import order.
    """
    ledger_path = TEST_OUTBOX / "shadow_ledger.jsonl"
    monkeypatch.setattr(shadow_ledger, "LEDGER_PATH", ledger_path)
    if ledger_path.exists():
        ledger_path.unlink()
    yield
    if ledger_path.exists():
        ledger_path.unlink()


def test_shadow_ledger_entry():
    """Test recording a hypothetical entry."""
    order = {
        "token": {
            "ticker": "TEST",
            "address": "abc123",
            "network_id": 1399811149,
            "chain": "solana"
        },
        "size_factor": 0.6,
        "confidence": 0.78,
        "model": "jev-1.13.0",
        "order_id": "2026-10-07T19:00:00Z"
    }
    fomo_data = {"price": 0.0001, "vol24": 50000, "mcap": 100000}
    
    entry = shadow_ledger.entry(order, fomo_data)
    
    assert entry is not None
    assert entry["action"] == "entry"
    assert entry["ticker"] == "TEST"
    assert entry["entry_price_usd"] == 0.0001
    assert entry["size_usd"] == 50.0  # SHADOW_TICKET_USD
    assert entry["size_tokens"] == 500000.0  # 50 / 0.0001
    assert entry["size_factor"] == 0.6
    
    # Check it's in the ledger file
    opens = shadow_ledger.open_positions()
    assert len(opens) == 1
    assert opens[0]["ticker"] == "TEST"


def test_shadow_ledger_entry_missing_price():
    """Test entry fails gracefully with missing price."""
    order = {
        "token": {"ticker": "TEST", "address": "abc", "network_id": 1399811149, "chain": "solana"}
    }
    fomo_data = {"price": None}
    
    entry = shadow_ledger.entry(order, fomo_data)
    assert entry is None
    
    # No position created
    opens = shadow_ledger.open_positions()
    assert len(opens) == 0


def _shadow_order(ticker="hellop"):
    return {
        "token": {
            "ticker": ticker,
            "address": "HelLoPAddr111",
            "network_id": 1399811149,
            "chain": "solana",
        },
        "size_factor": 1.0,
        "confidence": 0.7,
        "model": "jev-test",
        "order_id": "2026-10-13T09:03:27Z",
    }


def test_shadow_entry_uses_price_usd_from_shortlist(caplog):
    caplog.set_level("INFO")
    rec = shadow_ledger.entry(_shadow_order(), {"price_usd": 0.0042, "price": None})
    assert rec is not None
    assert rec["entry_price_usd"] == pytest.approx(0.0042)
    assert rec["price_source"] == "fomo"
    assert "source=fomo" in caplog.text


def test_shadow_entry_uses_dossier_priceusd():
    rec = shadow_ledger.entry(
        _shadow_order(),
        {"price": None},
        token_row={"tid": "HelLoPAddr111:1399811149", "priceUsd": "0.0015"},
    )
    assert rec is not None
    assert rec["entry_price_usd"] == pytest.approx(0.0015)
    assert rec["price_source"] == "dex"


def test_shadow_entry_dexscreener_fallback(monkeypatch):
    import young_trail
    class Resp:
        status_code = 200
        def json(self):
            return [{
                "liquidity": {"usd": 80_000},
                "priceUsd": "0.0099",
                "baseToken": {"address": "HelLoPAddr111"},
            }]
    young_trail.set_get_fn(lambda *a, **k: Resp())
    rec = shadow_ledger.entry(_shadow_order(), {"price": None, "price_usd": None})
    assert rec is not None
    assert rec["entry_price_usd"] == pytest.approx(0.0099)
    assert rec["price_source"] == "dexscreener"
    young_trail.set_get_fn(lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("young_trail tests must not use the network")
    ))


def test_shadow_entry_gives_up_only_when_all_fail():
    rec = shadow_ledger.entry(_shadow_order(), {"price": None})
    assert rec is None
    assert shadow_ledger.open_positions() == []


def test_report_shadow_traded_not_no_trade(caplog, tmp_path, monkeypatch):
    import desk as desk_mod
    monkeypatch.setattr(desk_mod, "OUTBOX", tmp_path)
    caplog.set_level("INFO")
    d = desk_mod.Desk()
    order = _shadow_order()
    stats = {"seen": 10, "benched": 0, "judged": 1, "tokens": [],
             "shadow_order": order, "shadow_recorded": True}
    d.report(None, stats)
    assert any("SHADOW " in r.message and "hellop" in r.message and "NO TRADE" not in r.message
               for r in caplog.records)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["cycle"]["outcome"] == "SHADOW"
    assert state["pick"]["token"]["ticker"] == "hellop"


def test_main_shadow_path_passes_shortlist_row_and_records_flag():
    src = pathlib.Path(__file__).resolve().parent.parent.joinpath("main.py").read_text()
    assert "fomo_data=picked_row" in src
    assert 'stats["shadow_recorded"]' in src
    assert 'stats["shadow_order"]' in src
    # Must not rebuild a dict that only reads t.get("price") (the hellop miss).
    shadow_block = src.split("if shadow:")[1].split("book.take")[0]
    assert 't.get("price")' not in shadow_block
    for name in ("thresholds.py", "filter.py"):
        r = pathlib.Path(__file__).resolve().parent.parent.joinpath(name).read_text()
        if name == "thresholds.py":
            assert "PICK_MIN_WORTH  = 0.60" in r


def test_report_shadow_pick_lost_not_no_trade(caplog, tmp_path, monkeypatch):
    import desk as desk_mod
    monkeypatch.setattr(desk_mod, "OUTBOX", tmp_path)
    caplog.set_level("INFO")
    d = desk_mod.Desk()
    stats = {"seen": 10, "benched": 0, "judged": 1, "tokens": [],
             "shadow_order": _shadow_order(), "shadow_recorded": False}
    d.report(None, stats)
    assert any("SHADOW pick lost: hellop no price" in r.message for r in caplog.records)
    assert not any(r.message.startswith("NO TRADE") for r in caplog.records)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["cycle"]["outcome"] == "SHADOW"
    assert "no price" in (state["cycle"].get("error") or "")


def test_shadow_ledger_mark_and_pnl():
    """Test marking a position to market and calculating PnL."""
    # Create entry
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0,
        "confidence": 0.8,
        "model": "jev-1.13.0",
        "order_id": "2026-10-07T19:00:00Z"
    }
    entry_price = 0.0001
    fomo_data = {"price": entry_price}
    shadow_ledger.entry(order, fomo_data)
    
    # New fills use tp_50: +60% is an exit. Legacy +100% is logged alongside.
    current_price = 0.00016  # 60% gain
    mark_record = shadow_ledger.mark("abc123", 1399811149, current_price=current_price, volume_h24=4000)
    
    assert mark_record is not None
    assert mark_record["action"] == "close"
    assert "tp_50" in mark_record["reason"]
    assert mark_record["realized_pnl_pct"] == pytest.approx(60.0, rel=0.01)
    assert mark_record["realized_pnl_usd"] == pytest.approx(30.0, rel=0.01)
    assert mark_record.get("exit_policy") == "tp_50"
    assert mark_record.get("legacy_would_exit") is False
    assert mark_record.get("legacy_take_profit_pct") == 100.0
    assert shadow_ledger.open_positions() == []


def test_shadow_ledger_exit_on_stop_loss():
    """Test exit when stop-loss threshold hit."""
    # Create entry
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0,
        "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001})
    
    # Mark at much lower price (triggers stop-loss at -30%)
    current_price = 0.00006  # 40% loss (stop-loss is -30%)
    
    close_record = shadow_ledger.mark("abc123", 1399811149, current_price=current_price, volume_h24=10000)
    
    assert close_record is not None
    assert close_record["action"] == "close"
    assert "stop_loss" in close_record["reason"]
    assert close_record["realized_pnl_pct"] == pytest.approx(-40.0, rel=0.01)
    assert close_record["realized_pnl_usd"] == pytest.approx(-20.0, rel=0.01)
    
    # Position closed
    opens = shadow_ledger.open_positions()
    assert len(opens) == 0
    
    closes = shadow_ledger.closed_positions()
    assert len(closes) == 1
    assert closes[0]["ticker"] == "TEST"


def test_shadow_ledger_exit_on_take_profit():
    """Test exit when take-profit threshold hit."""
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0,
        "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001})
    
    # New tp_50 fills also close well above +50%.
    current_price = 0.00025  # 150% gain
    
    close_record = shadow_ledger.mark("abc123", 1399811149, current_price=current_price, volume_h24=50000)
    
    assert close_record is not None
    assert close_record["action"] == "close"
    assert "tp_50" in close_record["reason"] or "take_profit" in close_record["reason"]
    assert close_record["realized_pnl_pct"] == pytest.approx(150.0, rel=0.01)
    assert close_record.get("exit_policy") == "tp_50"
    
    # Position closed
    opens = shadow_ledger.open_positions()
    assert len(opens) == 0


def test_shadow_ledger_legacy_open_keeps_old_exit(clean_ledger):
    """Existing positions without exit_policy keep +100% take-profit."""
    ledger = shadow_ledger.LEDGER_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "action": "entry",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ticker": "OLD",
        "address": "legacy1",
        "network_id": 1399811149,
        "chain": "solana",
        "entry_price_usd": 0.0001,
        "last_price_usd": 0.0001,
        "size_usd": 50.0,
        "size_tokens": 500000.0,
    }
    with ledger.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    mark = shadow_ledger.mark("legacy1", 1399811149, current_price=0.00016)
    assert mark["action"] == "mark"
    assert mark["unrealized_pnl_pct"] == pytest.approx(60.0, rel=0.01)
    assert mark.get("tp50_would_exit") is True
    assert mark.get("legacy_would_exit") is False
    assert mark.get("exit_policy") == "legacy"
    assert len(shadow_ledger.open_positions()) == 1


def test_new_pick_exits_tp50_and_logs_legacy(clean_ledger):
    order = {
        "token": {"ticker": "NEW", "address": "new1", "network_id": 1399811149,
                  "chain": "solana"},
        "size_factor": 1.0, "model": "jev-1.13.0",
    }
    entry = shadow_ledger.entry(order, {"price": 0.001})
    assert entry["exit_policy"] == "tp_50"
    mid = shadow_ledger.mark("new1", 1399811149, current_price=0.0014)
    assert mid["action"] == "mark"
    assert mid["tp50_would_exit"] is False
    assert mid["legacy_would_exit"] is False
    hit = shadow_ledger.mark("new1", 1399811149, current_price=0.0016)
    assert hit["action"] == "close"
    assert "tp_50" in hit["reason"]
    assert hit["realized_pnl_usd"] == pytest.approx(30.0, rel=0.01)
    assert hit.get("legacy_would_exit") is False


def test_shadow_ledger_manual_close():
    """Test manually closing a position."""
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0,
        "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001})
    
    # Manual close
    close_record = shadow_ledger.close("abc123", 1399811149, current_price=0.00015, reason="manual")
    
    assert close_record is not None
    assert close_record["action"] == "close"
    assert close_record["reason"] == "manual"
    assert close_record["realized_pnl_usd"] == 25.0


def test_shadow_ledger_stale_stays_open():
    """Test that stale price keeps position open, doesn't close it."""
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0,
        "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001})
    
    # Mark with None price (stale)
    mark_record = shadow_ledger.mark("abc123", 1399811149, current_price=None)
    
    assert mark_record is not None
    assert mark_record["action"] == "mark"
    assert mark_record.get("stale") is True
    assert "last_price_usd" in mark_record
    
    # Position stays open
    opens = shadow_ledger.open_positions()
    assert len(opens) == 1
    
    closes = shadow_ledger.closed_positions()
    assert len(closes) == 0


def test_shadow_ledger_summary():
    """Test summary statistics."""
    # Create and close multiple positions
    for i in range(5):
        order = {
            "token": {"ticker": f"TEST{i}", "address": f"abc{i}", "network_id": 1399811149, "chain": "solana"},
            "size_factor": 1.0,
            "model": "jev-1.13.0"
        }
        shadow_ledger.entry(order, {"price": 0.0001})
    
    # Close 3 winners, 1 loser, leave 1 open
    shadow_ledger.close("abc0", 1399811149, 0.00015, reason="winner")  # +$25 (50% gain on $50)
    shadow_ledger.close("abc1", 1399811149, 0.00012, reason="winner")  # +$10 (20% gain on $50)
    shadow_ledger.close("abc2", 1399811149, 0.00011, reason="winner")  # +$5 (10% gain on $50)
    shadow_ledger.close("abc3", 1399811149, 0.00008, reason="loser")   # -$10 (20% loss on $50)
    # abc4 stays open
    # Total: 25 + 10 + 5 - 10 = 30
    
    summary = shadow_ledger.summary()
    
    assert summary["open_positions_count"] == 1
    assert summary["closed_trades_count"] == 4
    assert summary["winning_trades"] == 3
    assert summary["losing_trades"] == 1
    assert summary["win_rate"] == 0.75
    assert summary["total_realized_pnl_usd"] == pytest.approx(30.0, rel=0.01)


def test_shadow_ledger_empty_state():
    """Test empty ledger returns sensible defaults."""
    opens = shadow_ledger.open_positions()
    closes = shadow_ledger.closed_positions()
    summary = shadow_ledger.summary()
    
    assert opens == []
    assert closes == []
    assert summary["open_positions_count"] == 0
    assert summary["closed_trades_count"] == 0
    assert summary["total_realized_pnl_usd"] == 0
    assert summary["win_rate"] == 0


def test_fomo_health_no_token_leakage():
    """Test FOMO health() never exposes the bearer token."""
    # Create Fomo with a token
    fomo = Fomo(bearer="fake.bearer.token")
    
    health = fomo.health()
    
    # Check all health fields are safe
    assert "bearer" not in str(health).lower() or "bearer_present" in str(health)
    assert "fake.bearer.token" not in str(health)
    assert "fake" not in str(health).lower() or "bearer" not in str(health).get("last_error", "")
    
    # Check expected fields are present
    assert "bearer_present" in health
    assert health["bearer_present"] is True
    assert "bearer_age_seconds" in health
    assert "bearer_source" in health
    assert "last_refresh_ts" in health


def test_fomo_health_tracks_errors():
    """Test FOMO health tracks errors without exposing tokens."""
    fomo = Fomo()
    
    # Simulate error by trying to get token without bearer or CDP
    with mock.patch.dict(os.environ, {"FOMO_BEARER": "", "CDP_URL": "http://localhost:99999"}):
        try:
            fomo.token()
        except Exception:
            pass
    
    health = fomo.health()
    
    assert "last_error" in health
    # Error message should exist but not contain any token
    if health["last_error"]:
        assert "bearer" not in health["last_error"] or "FOMO bearer" in health["last_error"]


def test_fomo_health_cdp_reachable():
    """Test FOMO health tracks CDP reachability."""
    fomo = Fomo()
    
    # Mock CDP unreachable
    with mock.patch("fomo_api.requests.get", side_effect=Exception("Connection refused")):
        result = fomo._from_chrome()
    
    assert result is None
    assert fomo._cdp_reachable is False
    
    health = fomo.health()
    assert health["cdp_reachable"] is False


@mock.patch("fomo_api.requests.get")
def test_activate_fomo_tab_existing(mock_get):
    """Test activating an existing fomo.family tab."""
    # Mock CDP /json response with existing tab
    mock_get.return_value.json.return_value = [
        {"type": "page", "url": "https://fomo.family/token/abc", "id": "tab123"}
    ]
    mock_get.return_value.raise_for_status = mock.Mock()
    
    result = activate_fomo_tab()
    
    assert result["success"] is True
    assert result["action"] == "activated"
    assert "brought" in result["message"].lower() or "front" in result["message"].lower()
    
    # Check CDP activate was called
    calls = [str(c) for c in mock_get.call_args_list]
    assert any("activate" in c and "tab123" in c for c in calls)


@mock.patch("fomo_api.requests.get")
def test_activate_fomo_tab_create_new(mock_get):
    """Test creating a new fomo.family tab when none exists."""
    # Mock CDP /json response with no fomo.family tab
    mock_get.return_value.json.return_value = [
        {"type": "page", "url": "https://google.com", "id": "tab456"}
    ]
    mock_get.return_value.raise_for_status = mock.Mock()
    
    result = activate_fomo_tab()
    
    assert result["success"] is True
    assert result["action"] == "created"
    assert "opened" in result["message"].lower()
    
    # Check CDP /json/new was called
    calls = [str(c) for c in mock_get.call_args_list]
    assert any("new" in c and "fomo.family" in c for c in calls)


@mock.patch("fomo_api.requests.get")
def test_activate_fomo_tab_cdp_unreachable(mock_get):
    """Test activation fails gracefully when CDP is unreachable."""
    mock_get.side_effect = Exception("Connection refused")
    
    result = activate_fomo_tab()
    
    assert result["success"] is False
    assert "not reachable" in result["message"].lower()


def test_mode_flags_in_desk_state(monkeypatch):
    """Test mode and judge_mode flags appear in desk state."""
    import tempfile
    from desk import Desk
    
    # Create temporary directory for this test
    tmp_path = pathlib.Path(tempfile.mkdtemp())
    
    # Set up test environment
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    monkeypatch.setenv("CONFIRM_LIVE", "no")
    monkeypatch.setenv("JUDGE_MOCK", "1")
    
    # Reimport desk module to pick up new env vars
    import importlib
    import desk as desk_module
    importlib.reload(desk_module)
    Desk = desk_module.Desk
    
    desk = Desk()
    
    # Write state
    stats = {
        "seen": 10,
        "benched": 2,
        "judged": 3,
        "requeued": 0,
        "free": {"age": 5},
        "trade": {},
        "chain": {},
        "soft": {},
        "tokens": []
    }
    desk.write_state(None, stats)
    
    # Read back
    state_path = tmp_path / "state.json"
    assert state_path.exists(), f"state.json not found at {state_path}"
    
    state = json.loads(state_path.read_text())
    
    assert state["mode"] == "shadow"
    assert state["judge_mode"] == "mock"
    assert state["cycle"]["outcome"] == "NO TRADE"


def test_mode_flags_live_mode(monkeypatch):
    """Test mode flags with CONFIRM_LIVE=yes."""
    import tempfile
    from desk import Desk
    
    tmp_path = pathlib.Path(tempfile.mkdtemp())
    
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    monkeypatch.setenv("CONFIRM_LIVE", "yes")
    monkeypatch.setenv("JUDGE_MOCK", "")
    
    # Reimport to pick up env vars
    import importlib
    import desk as desk_module
    importlib.reload(desk_module)
    Desk = desk_module.Desk
    
    desk = Desk()
    
    stats = {"seen": 0, "benched": 0, "judged": 0, "requeued": 0,
             "free": {}, "trade": {}, "chain": {}, "soft": {}, "tokens": []}
    desk.write_state(None, stats)
    
    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text())
    
    assert state["mode"] == "live"
    assert state["judge_mode"] == "live"


def test_fomo_error_tracking_in_state(monkeypatch):
    """Test FomoAuthError gets tracked in cycle state."""
    import tempfile
    from desk import Desk
    
    tmp_path = pathlib.Path(tempfile.mkdtemp())
    
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    
    # Reimport to pick up env vars
    import importlib
    import desk as desk_module
    importlib.reload(desk_module)
    Desk = desk_module.Desk
    
    desk = Desk()
    
    stats = {
        "error": "FomoAuthError: bearer expired",
        "fomo_error": "bearer expired",
        "seen": 0, "benched": 0, "judged": 0, "requeued": 0,
        "free": {}, "trade": {}, "chain": {}, "soft": {}, "tokens": []
    }
    desk.write_state(None, stats)
    
    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text())
    
    assert state["cycle"]["fomo_error"] == "bearer expired"
    assert state["cycle"]["outcome"] == "ERROR"


def test_shadow_mark_all_open_positions():
    """Test marking all open positions via mark_all_open_positions."""
    # Create two open positions
    order1 = {
        "token": {"ticker": "TEST1", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0, "model": "jev-1.13.0"
    }
    order2 = {
        "token": {"ticker": "TEST2", "address": "def456", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0, "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order1, {"price": 0.0001})
    shadow_ledger.entry(order2, {"price": 0.0002})
    
    # Mock FOMO client
    # With the heuristic vol_h6 = vol24 * 0.15, ratio = 0.15 / 0.25 = 0.6 (healthy, stays open)
    # To trigger closure, we need very low vol24 or simulate it
    class MockFomo:
        def tokens(self, tids):
            return {
                "abc123:1399811149": {"price": 0.00012, "vol24": 50000},  # Healthy volume, stays open
                "def456:1399811149": {"price": 0.00015, "vol24": 100}     # Very low volume, stays open with 0.6 ratio
            }
    
    fomo = MockFomo()
    result = shadow_ledger.mark_all_open_positions(fomo)
    
    # Both should be marked (ratio is 0.6 > 0.20, so healthy)
    assert result["marked"] == 2
    assert result["closed"] == 0
    
    # Both positions should still be open
    opens = shadow_ledger.open_positions()
    assert len(opens) == 2


def test_shadow_mark_with_stale_price():
    """Test marking when FOMO returns no price (stale data) - position stays open."""
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0, "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001})
    
    # Mock FOMO with missing price
    class MockFomo:
        def tokens(self, tids):
            return {"abc123:1399811149": {"vol24": 50000}}  # No price
    
    fomo = MockFomo()
    result = shadow_ledger.mark_all_open_positions(fomo)
    
    assert result["stale"] == 1
    assert result["closed"] == 0
    
    # Position should stay open
    opens = shadow_ledger.open_positions()
    assert len(opens) == 1
    
    closes = shadow_ledger.closed_positions()
    assert len(closes) == 0


def test_shadow_ledger_time_stop():
    """Test exit on time stop (max hold exceeded)."""
    import time as time_mod
    
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0, "model": "jev-1.13.0"
    }
    
    # Create entry with old timestamp (mock it by modifying ledger)
    shadow_ledger.entry(order, {"price": 0.0001})
    
    # Manually update entry timestamp to 7 hours ago
    import pathlib
    ledger_path = pathlib.Path(shadow_ledger.LEDGER_PATH)
    lines = ledger_path.read_text().split('\n')
    if lines and lines[0]:
        entry = json.loads(lines[0])
        old_ts = time_mod.time() - 7 * 3600  # 7 hours ago
        entry["ts"] = time_mod.strftime("%Y-%m-%dT%H:%M:%SZ", time_mod.gmtime(old_ts))
        lines[0] = json.dumps(entry)
        ledger_path.write_text('\n'.join(lines))
    
    # Mark at current price (should trigger time stop)
    close_record = shadow_ledger.mark("abc123", 1399811149, current_price=0.00012, volume_h24=50000)
    
    assert close_record is not None
    assert close_record["action"] == "close"
    assert "time_stop" in close_record["reason"]
    
    # Position closed
    opens = shadow_ledger.open_positions()
    assert len(opens) == 0


def test_chain_preserved_in_close_record():
    """Test that chain is preserved from entry to close for proper links."""
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 56, "chain": "bsc"},
        "size_factor": 1.0, "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001})
    shadow_ledger.close("abc123", 56, current_price=0.00015, reason="test")
    
    closes = shadow_ledger.closed_positions()
    assert len(closes) == 1
    assert closes[0]["chain"] == "bsc"


def test_volume_surge_does_not_close():
    """Test that volume surges do NOT trigger an exit (volume exit removed)."""
    order = {
        "token": {"ticker": "SURGE", "address": "surge123", "network_id": 56, "chain": "bsc"},
        "size_factor": 1.0, "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001, "vol24": 100_000})
    
    # Mark with volume surge (100k -> 1M)
    result = shadow_ledger.mark("surge123", 56, current_price=0.00012, volume_h24=1_000_000)
    
    # Should stay open (no volume exit)
    assert result["action"] == "mark"
    assert not result.get("stale")
    
    # Position still open
    opens = shadow_ledger.open_positions()
    assert len(opens) == 1


def test_volume_collapse_does_not_close():
    """Test that volume collapses do NOT trigger an exit (volume exit removed)."""
    order = {
        "token": {"ticker": "COLLAPSE", "address": "collapse123", "network_id": 56, "chain": "bsc"},
        "size_factor": 1.0, "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001, "vol24": 1_000_000})
    
    # Mark with volume collapse (1M -> 100k)
    result = shadow_ledger.mark("collapse123", 56, current_price=0.00012, volume_h24=100_000)
    
    # Should stay open (no volume exit)
    assert result["action"] == "mark"
    assert not result.get("stale")
    
    # Position still open
    opens = shadow_ledger.open_positions()
    assert len(opens) == 1


def test_stale_position_time_stop_with_last_mark():
    """Test that stale position hits time stop at last good mark."""
    order = {
        "token": {"ticker": "STALE", "address": "stale123", "network_id": 56, "chain": "bsc"},
        "size_factor": 1.0, "model": "jev-1.13.0"
    }
    
    # Entry at T=0
    with mock.patch("time.time", return_value=1000.0):
        shadow_ledger.entry(order, {"price": 0.0001})
    
    # Mark with good price at T=1h
    with mock.patch("time.time", return_value=1000.0 + 3600):
        result = shadow_ledger.mark("stale123", 56, current_price=0.00012, volume_h24=500_000)
        assert result["action"] == "mark"
        assert result["current_price_usd"] == 0.00012
    
    # Mark with stale price at T=3h (stays open)
    with mock.patch("time.time", return_value=1000.0 + 3 * 3600):
        result = shadow_ledger.mark("stale123", 56, current_price=None)
        assert result["action"] == "mark"
        assert result.get("stale") is True
        assert result["last_price_usd"] == 0.00012  # Preserved from last mark
    
    # Mark with stale price at T=6.1h (time stop fires)
    with mock.patch("time.time", return_value=1000.0 + 6.1 * 3600):
        result = shadow_ledger.mark("stale123", 56, current_price=None)
        assert result["action"] == "close"
        assert "time_stop_6.1h_stale_mark" in result["reason"]
        assert result["exit_price_usd"] == 0.00012  # Closed at last good mark
        assert result.get("unmeasured") is False  # Had a good mark
    
    # Position closed
    opens = shadow_ledger.open_positions()
    assert len(opens) == 0


def test_never_marked_position_excluded_from_totals():
    """Test that never-marked position closed by time stop is excluded from PnL totals."""
    order = {
        "token": {"ticker": "NEVER", "address": "never123", "network_id": 56, "chain": "bsc"},
        "size_factor": 1.0, "model": "jev-1.13.0"
    }
    
    # Entry at T=0, never mark with good price
    with mock.patch("time.time", return_value=2000.0):
        shadow_ledger.entry(order, {"price": 0.0001})
    
    # Stale mark at T=3h
    with mock.patch("time.time", return_value=2000.0 + 3 * 3600):
        result = shadow_ledger.mark("never123", 56, current_price=None)
        assert result["action"] == "mark"
        assert result.get("stale") is True
    
    # Time stop at T=6.1h (never had good mark)
    with mock.patch("time.time", return_value=2000.0 + 6.1 * 3600):
        result = shadow_ledger.mark("never123", 56, current_price=None)
        assert result["action"] == "close"
        assert "time_stop" in result["reason"]
        assert result.get("unmeasured") is True
        assert result["realized_pnl_usd"] is None
    
    # Summary should exclude this from totals
    summary = shadow_ledger.summary()
    assert summary["closed_trades_count"] == 1
    assert summary["measured_trades_count"] == 0
    assert summary["unmeasured_trades"] == 1
    assert summary["total_realized_pnl_usd"] == 0  # Excluded
    assert summary["winning_trades"] == 0
    assert summary["losing_trades"] == 0


def test_csrf_protection_on_fomo_activate(monkeypatch):
    """Test that /api/fomo_activate rejects requests without custom header."""
    # Set required env vars before importing server
    monkeypatch.setenv("DESK_SECRET", "test-secret-for-csrf-test")
    monkeypatch.setenv("JUDGE_MOCK", "1")  # Use mock judge to avoid needing TYPESAFE_API_KEY
    
    from fastapi.testclient import TestClient
    
    # Reimport server and judge to pick up env vars
    import importlib
    import judge as judge_module
    import server as server_module
    importlib.reload(judge_module)
    importlib.reload(server_module)
    
    client = TestClient(server_module.app)
    
    # Request without header should fail
    response = client.post("/api/fomo_activate")
    assert response.status_code == 403
    assert "X-Ops-Action" in response.json()["detail"]
    
    # Request with wrong header value should fail
    response = client.post("/api/fomo_activate", headers={"X-Ops-Action": "wrong"})
    assert response.status_code == 403
    
    # Request with correct header should succeed (but CDP may not be reachable)
    response = client.post("/api/fomo_activate", headers={"X-Ops-Action": "activate-fomo"})
    # May be 500 if CDP not reachable, but shouldn't be 403
    assert response.status_code in (200, 500)


def _jwt(exp: float, secret: str = "sig") -> str:
    """Unsigned JWT with only an exp claim. Tests never log this."""
    import base64
    import json

    def b64(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'exp': int(exp)})}.{secret}"


class _Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body if body is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._body


def test_jwt_exp_triggers_early_refresh(monkeypatch):
    import fomo_api
    now = [1_700_000_000.0]
    fomo_api.set_time_fn(lambda: now[0])
    fomo_api.set_sleep_fn(lambda s: None)
    try:
        chrome = _jwt(now[0] + 3600, secret="chrome")
        calls = []

        def fake_chrome(self):
            calls.append(1)
            return chrome

        monkeypatch.setattr(fomo_api.Fomo, "_from_chrome", fake_chrome)
        fresh = _jwt(now[0] + 600, secret="fresh")
        fomo = fomo_api.Fomo(bearer=fresh)
        fomo._fetched_at = now[0] - 100
        assert fomo.token() == fresh
        assert calls == []

        soon = _jwt(now[0] + 200, secret="soon")
        fomo._bearer = soon
        fomo._fetched_at = now[0]
        assert fomo.token() == chrome
        assert calls == [1]

        fomo2 = fomo_api.Fomo(bearer="aaa.bbb.ccc")
        fomo2._fetched_at = now[0] - 100
        assert fomo2.token() == "aaa.bbb.ccc"
        fomo2._fetched_at = now[0] - fomo_api.TOKEN_TTL - 1
        assert fomo2.token() == chrome
    finally:
        fomo_api.set_time_fn(None)
        fomo_api.set_sleep_fn(None)


def test_401_stale_token_reloads_once_then_succeeds(monkeypatch, caplog):
    import logging
    import fomo_api
    now = [1_700_000_000.0]
    fomo_api.set_time_fn(lambda: now[0])
    fomo_api.set_sleep_fn(lambda s: now.__setitem__(0, now[0] + s))
    stale = _jwt(now[0] + 3600, secret="staleTOKEN")
    fresh = _jwt(now[0] + 7200, secret="freshTOKEN")
    fomo = fomo_api.Fomo(bearer=stale)
    fomo.begin_cycle()
    box = {"tok": stale, "reloads": 0}

    def fake_chrome():
        return box["tok"]

    def fake_reload():
        box["reloads"] += 1
        box["tok"] = fresh

    monkeypatch.setattr(fomo, "_from_chrome", fake_chrome)
    monkeypatch.setattr(fomo, "_reload_fomo_tab", fake_reload)
    posts = []

    def fake_post(path, payload, tok, timeout=30):
        posts.append(tok)
        if tok == stale:
            return _Resp(401)
        return _Resp(200, [{
            "token": {"address": "Mint1", "networkId": 1399811149, "symbol": "X",
                      "marketCap": 1, "liquidity": 1, "priceUSD": 1},
        }])

    monkeypatch.setattr(fomo, "_do_post", fake_post)
    caplog.set_level(logging.DEBUG)
    try:
        out = fomo.tokens(["Mint1:1399811149"])
        assert box["reloads"] == 1
        assert posts == [stale, fresh]
        assert "Mint1:1399811149" in out
        blob = caplog.text
        assert stale not in blob
        assert fresh not in blob
        assert "staleTOKEN" not in blob
        assert "freshTOKEN" not in blob
    finally:
        fomo_api.set_time_fn(None)
        fomo_api.set_sleep_fn(None)


def test_401_after_reload_no_second_reload_that_cycle(monkeypatch, caplog):
    import logging
    import fomo_api
    now = [1_700_000_000.0]
    fomo_api.set_time_fn(lambda: now[0])
    fomo_api.set_sleep_fn(lambda s: now.__setitem__(0, now[0] + s))
    stale = _jwt(now[0] + 3600, secret="stayTOKEN")
    fomo = fomo_api.Fomo(bearer=stale)
    fomo.begin_cycle()
    box = {"reloads": 0}

    monkeypatch.setattr(fomo, "_from_chrome", lambda: stale)

    def fake_reload():
        box["reloads"] += 1

    monkeypatch.setattr(fomo, "_reload_fomo_tab", fake_reload)
    monkeypatch.setattr(fomo, "_do_post", lambda *a, **k: _Resp(401))
    caplog.set_level(logging.DEBUG)
    try:
        with pytest.raises(fomo_api.FomoAuthError):
            fomo.tokens(["Mint1:1399811149"])
        assert box["reloads"] == 1
        with pytest.raises(fomo_api.FomoAuthError):
            fomo.trending_tokens()
        assert box["reloads"] == 1
        blob = caplog.text
        assert stale not in blob
        assert "stayTOKEN" not in blob
    finally:
        fomo_api.set_time_fn(None)
        fomo_api.set_sleep_fn(None)


def test_reload_sends_location_reload_via_cdp(monkeypatch):
    import json
    import sys
    import types
    import fomo_api
    sent = []

    class WS:
        def __init__(self, url, timeout=5):
            pass

        def send(self, payload):
            sent.append(json.loads(payload))

        def recv(self):
            return json.dumps({"result": {"result": {"value": None}}})

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "websocket", types.SimpleNamespace(
        create_connection=lambda url, timeout=5: WS(url, timeout),
    ))
    tab = {"type": "page", "url": "https://fomo.family/",
           "webSocketDebuggerUrl": "ws://127.0.0.1/devtools"}
    monkeypatch.setattr(fomo_api.requests, "get", mock.Mock(
        return_value=mock.Mock(json=lambda: [tab]),
    ))
    fomo = fomo_api.Fomo(bearer="aaa.bbb.ccc")
    fomo._reload_fomo_tab()
    assert sent
    assert sent[0]["method"] == "Runtime.evaluate"
    assert sent[0]["params"]["expression"] == "location.reload()"


def test_consecutive_auth_failures_banner_and_reset(caplog):
    import logging
    import fomo_api
    fomo = fomo_api.Fomo(bearer="aaa.bbb.ccc")
    caplog.set_level(logging.ERROR)
    h = fomo.health()
    assert h["consecutive_auth_failures"] == 0
    assert h["auth_down"] is False
    assert "aaa.bbb.ccc" not in str(h)
    fomo.note_auth_failure()
    h = fomo.health()
    assert h["consecutive_auth_failures"] == 1
    assert h["auth_down"] is False
    assert not any("FOMO AUTH DOWN" in (r.message or "") for r in caplog.records)
    fomo.note_auth_failure()
    h = fomo.health()
    assert h["consecutive_auth_failures"] == 2
    assert h["auth_down"] is True
    down = [r.message for r in caplog.records if "FOMO AUTH DOWN" in (r.message or "")]
    assert down and "2 consecutive cycles failed" in down[0]
    assert "aaa.bbb.ccc" not in caplog.text
    fomo.note_auth_ok()
    h = fomo.health()
    assert h["consecutive_auth_failures"] == 0
    assert h["auth_down"] is False
