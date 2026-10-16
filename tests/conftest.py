"""
Pytest configuration for EVM holder tests.

Ensures proper isolation between EVM tests by clearing all caches and using temporary databases.
"""
import pytest
import tempfile
import os
import sqlite3


@pytest.fixture(autouse=True)
def isolate_coinalyze(monkeypatch):
    """No real Coinalyze key or HTTP. Any accidental get() fails the test."""
    monkeypatch.delenv("COINALYZE_API_KEY", raising=False)
    import coinalyze
    coinalyze.reset()
    def _no_network(*_a, **_k):
        raise AssertionError("coinalyze tests must not use the network")
    coinalyze.set_get_fn(_no_network)
    yield
    coinalyze.reset()


@pytest.fixture(autouse=True)
def isolate_outcomes(tmp_path, monkeypatch):
    """Point outcomes cache at a per-test SQLite file. No network, no weekly thread."""
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.setenv("BACKTEST_DISABLE", "1")
    monkeypatch.setenv("OUTCOMES_FETCH_DISABLE", "1")
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    monkeypatch.delenv("COINGECKO_PLAN", raising=False)
    import gt_api
    import outcomes
    import backtest_signals
    gt_api.reset()
    outcomes.reset()
    outcomes.set_time_fn(None)
    outcomes.set_sleep_fn(lambda _d: None)

    def _no_network(*_a, **_k):
        raise AssertionError("outcomes tests must not use the network")

    outcomes.set_get_fn(_no_network)
    backtest_signals.reset()
    backtest_signals.set_time_fn(None)
    yield
    outcomes.reset()
    outcomes.set_get_fn(None)
    outcomes.set_sleep_fn(None)
    outcomes.set_time_fn(None)
    backtest_signals.reset()
    backtest_signals.set_time_fn(None)


@pytest.fixture(autouse=True)
def isolate_young_trail(tmp_path, monkeypatch):
    """Paper young-trail ledger on the per-test outcomes.db. No network."""
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.delenv("YOUNG_TRAIL_UNTIL", raising=False)
    monkeypatch.delenv("YOUNG_TRAIL_ENABLED", raising=False)
    monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    monkeypatch.delenv("RUGCHECK_API_KEY", raising=False)
    import price_guard
    import young_trail
    price_guard.reset()
    young_trail.reset()
    young_trail.set_time_fn(None)
    young_trail.set_sleep_fn(lambda _d: None)

    def _no_network(*_a, **_k):
        raise AssertionError("young_trail tests must not use the network")

    young_trail.set_get_fn(_no_network)
    import young_trail_fast
    import young_trail_helius
    import young_trail_rugrisk
    import young_trail_signals
    young_trail_fast.reset()
    young_trail_fast.set_time_fn(None)
    young_trail_helius.reset()
    young_trail_helius.set_time_fn(None)
    young_trail_helius.set_rpc_fn(None)
    young_trail_rugrisk.reset()
    young_trail_rugrisk.set_time_fn(None)
    young_trail_rugrisk.set_rpc_fn(None)
    young_trail_rugrisk.set_overnight_fn(None)
    young_trail_signals.reset()
    young_trail_signals.set_time_fn(None)
    young_trail_signals.set_http_fn(_no_network)
    young_trail_signals.set_sleep_fn(lambda _d: None)
    yield
    price_guard.reset()
    young_trail.reset()
    young_trail.set_get_fn(None)
    young_trail.set_sleep_fn(None)
    young_trail.set_time_fn(None)
    young_trail_fast.reset()
    young_trail_fast.set_time_fn(None)
    young_trail_helius.reset()
    young_trail_helius.set_time_fn(None)
    young_trail_helius.set_rpc_fn(None)
    young_trail_rugrisk.reset()
    young_trail_rugrisk.set_time_fn(None)
    young_trail_rugrisk.set_rpc_fn(None)
    young_trail_rugrisk.set_overnight_fn(None)
    young_trail_signals.reset()
    young_trail_signals.set_time_fn(None)
    young_trail_signals.set_http_fn(None)
    young_trail_signals.set_sleep_fn(None)


@pytest.fixture(autouse=True)
def isolate_gt_info_cache(tmp_path, monkeypatch):
    """Point the GT /info cache at a per-test SQLite file. No leftover hits."""
    monkeypatch.setenv("GT_INFO_CACHE_DB", str(tmp_path / "gt_info_cache.db"))
    import gt_info_cache
    gt_info_cache.reset()
    gt_info_cache.set_time_fn(None)
    yield
    gt_info_cache.reset()
    gt_info_cache.set_time_fn(None)


@pytest.fixture(autouse=True)
def isolate_cycle_history(tmp_path, monkeypatch):
    """Point cycle-history at a per-test SQLite file so holder-growth cannot leak."""
    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "cycle_history.db"))
    import cycle_history
    cycle_history.reset()
    yield
    cycle_history.reset()


@pytest.fixture(autouse=True)
def isolate_judge_inputs(monkeypatch):
    """Reset per-cycle RPC cap and injected RPC/clock between tests."""
    import judge_inputs
    judge_inputs.reset_cycle()
    judge_inputs.set_rpc_fn(None)
    judge_inputs.set_time_fn(None)
    yield
    judge_inputs.reset_cycle()
    judge_inputs.set_rpc_fn(None)
    judge_inputs.set_time_fn(None)


@pytest.fixture(autouse=True)
def isolate_research(tmp_path, monkeypatch):
    """Point research.db at a per-test SQLite file. No Dex sampler thread."""
    monkeypatch.setenv("RESEARCH_DB", str(tmp_path / "research.db"))
    monkeypatch.setenv("RESEARCH_SAMPLER_DISABLE", "1")
    import research
    research.reset()
    research.set_time_fn(None)
    research.set_sleep_fn(lambda _d: None)

    def _no_network(*_a, **_k):
        raise AssertionError("research tests must not use the network")

    research.set_get_fn(_no_network)
    yield
    research.reset()
    research.set_get_fn(None)
    research.set_sleep_fn(None)
    research.set_time_fn(None)


@pytest.fixture(autouse=True)
def isolate_x_cache(tmp_path, monkeypatch):
    """Point the X lookup cache at a per-test SQLite file. Reset the cycle cap."""
    monkeypatch.setenv("X_CACHE_DB", str(tmp_path / "x_cache.db"))
    import x_lookup
    x_lookup.reset()
    x_lookup.set_time_fn(None)
    x_lookup.set_sleep_fn(lambda _d: None)
    yield
    x_lookup.reset()
    x_lookup.set_time_fn(None)
    x_lookup.set_sleep_fn(None)


@pytest.fixture
def isolate_evm_state(monkeypatch):
    """
    Clear all EVM holder state before and after each test.
    
    - Clears in-memory result cache
    - Clears rate limiter state
    - Uses temporary database for evm_holder_cache
    
    This fixture is NOT autouse; tests must request it explicitly.
    """
    import evm_holders
    
    # Clear in-memory caches
    evm_holders._cache.clear()
    evm_holders._rate_limiters.clear()
    
    # Create temporary database for this test
    temp_db_fd, temp_db_path = tempfile.mkstemp(suffix='.db')
    os.close(temp_db_fd)
    
    temp_db = sqlite3.connect(temp_db_path)
    temp_db.executescript("""
    CREATE TABLE IF NOT EXISTS evm_holder_cache(
      chain_id INTEGER NOT NULL,
      token TEXT NOT NULL,
      last_block INTEGER NOT NULL,
      balances_json TEXT NOT NULL,
      supply TEXT NOT NULL,
      updated_at REAL NOT NULL,
      PRIMARY KEY (chain_id, token)
    );
    """)
    temp_db.commit()
    
    # Monkeypatch book.DB to use temp database for evm_holders tests
    import book
    original_db = book.DB
    monkeypatch.setattr(book, 'DB', temp_db)
    
    yield temp_db
    
    # Cleanup after test
    evm_holders._cache.clear()
    evm_holders._rate_limiters.clear()
    
    try:
        temp_db.close()
        os.unlink(temp_db_path)
    except:
        pass
    
    # Restore original DB
    monkeypatch.setattr(book, 'DB', original_db)
