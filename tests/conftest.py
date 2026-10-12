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
def isolate_x_cache(tmp_path, monkeypatch):
    """Point the X lookup cache at a per-test SQLite file. Reset the cycle cap."""
    monkeypatch.setenv("X_CACHE_DB", str(tmp_path / "x_cache.db"))
    import x_lookup
    x_lookup.reset()
    x_lookup.set_time_fn(None)
    yield
    x_lookup.reset()
    x_lookup.set_time_fn(None)


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
