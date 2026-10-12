"""In-desk fxtwitter X lookup. Mocked HTTP, no network, no keys."""
import logging
import os
import pathlib
import subprocess
import sys
import time
from unittest.mock import Mock, patch
from datetime import datetime, timezone

import pytest
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DESK_DB", ":memory:")
os.environ.setdefault("DESK_OUTBOX", str(ROOT / "tests" / "_outbox"))

import book                                           # noqa: E402
import collect                                        # noqa: E402
import x_lookup                                       # noqa: E402
from desk import Desk                                 # noqa: E402
from pick import summary, size_factor_for             # noqa: E402
from thresholds import NO_SOCIAL_CUT                  # noqa: E402
import main as shift                                  # noqa: E402


def _profile_body(handle="tokacct", followers=1200, **user):
    body = {
        "code": 200,
        "user": {
            "screen_name": handle,
            "name": "Token Acct",
            "description": "the official token",
            "followers": followers,
            "following": 10,
            "tweets": 40,
            "joined": "2020-01-01T00:00:00Z",
            "verification": {"verified": False, "type": None},
            "protected": False,
        },
    }
    body["user"].update(user)
    return body


def _statuses_body(handle="tokacct", n=10, extra_repost=False):
    results = []
    if extra_repost:
        results.append({
            "type": "status",
            "created_at": "2026-01-01T00:00:00Z",
            "text": "a repost",
            "likes": 0, "replies": 0, "views": 1,
            "author": {"screen_name": "someoneelse"},
        })
    for i in range(n):
        results.append({
            "type": "status",
            "created_at": "2026-01-02T00:00:00Z",
            "text": ("hello world " * 30) + str(i),
            "likes": i, "replies": 2, "views": 50,
            "author": {"screen_name": handle},
        })
    return {"code": 200, "results": results}


def _resp(status, body):
    m = Mock(status_code=status)
    if isinstance(body, Exception):
        m.json.side_effect = body
    else:
        m.json.return_value = body
    return m


def _route(profile=None, statuses=None, profile_exc=None, statuses_exc=None):
    calls = []

    def get(url, timeout=None, **kw):
        calls.append(url)
        if "/statuses" in url:
            if statuses_exc:
                raise statuses_exc
            return statuses if statuses is not None else _resp(200, _statuses_body())
        if profile_exc:
            raise profile_exc
        return profile if profile is not None else _resp(200, _profile_body())

    return get, calls


def _project_route(handle="tokacct", ticker="XTK", followers=1200, joined=None):
    """Profile that mentions the token — not a borrowed celebrity account."""
    joined = joined or "2026-10-11T00:00:00Z"
    posts = {
        "code": 200,
        "results": [{
            "type": "status",
            "created_at": "2026-10-12T00:00:00Z",
            "text": f"${ticker} launching now",
            "likes": 1, "replies": 1, "views": 10,
            "author": {"screen_name": handle},
        }],
    }
    return _route(
        profile=_resp(200, _profile_body(
            handle=handle, followers=followers, name=f"{ticker} Token",
            description=f"the official ${ticker} token", joined=joined,
        )),
        statuses=_resp(200, posts),
    )


# ---- handle normalization ----------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("tokacct", "tokacct"),
    ("@tokacct", "tokacct"),
    ("tokacct?s=20", "tokacct"),
    ("https://x.com/tokacct", "tokacct"),
    ("https://twitter.com/tokacct", "tokacct"),
    ("https://x.com/tokacct?s=20", "tokacct"),
    ("https://www.x.com/tokacct", "tokacct"),
    ("x.com/tokacct", "tokacct"),
    ("https://x.com/@tokacct", "tokacct"),
    ("", None),
    (None, None),
    ("https://example.com/tokacct", None),
])
def test_normalize_handle_basic(raw, want):
    assert x_lookup.normalize_handle(raw) == want


def test_normalize_rejects_community_and_logs(caplog):
    caplog.set_level(logging.INFO)
    assert x_lookup.normalize_handle("https://x.com/i/communities/123456") is None
    assert x_lookup.normalize_handle("https://twitter.com/i/communities/99") is None
    assert "x_kind=community" in caplog.text


def test_normalize_rejects_status_urls():
    assert x_lookup.normalize_handle(
        "https://x.com/LuffyX100X/status/2102659581109272876") is None
    assert x_lookup.normalize_handle(
        "LuffyX100X/status/2102659581109272876") is None
    assert x_lookup.normalize_handle(
        "https://twitter.com/foo/status/1") is None


def test_clean_handle_gt_path_unchanged():
    """GT's clean_handle still extracts the first path segment of a status blob."""
    assert collect.clean_handle("LuffyX100X/status/2102659581109272876") == "LuffyX100X"
    assert collect.clean_handle("https://x.com/foo") is None


# ---- success / errors / cache / cap ------------------------------------------

def test_success_builds_x_account_block():
    x_lookup.set_time_fn(lambda: datetime(2020, 1, 31, tzinfo=timezone.utc).timestamp())
    get, calls = _route(
        profile=_resp(200, _profile_body()),
        statuses=_resp(200, _statuses_body(n=8, extra_repost=True)),
    )
    with patch("x_lookup.requests.get", side_effect=get):
        block = x_lookup.read_x("tokacct")
    assert block is not None
    assert block["handle"] == "tokacct"
    assert block["screen_name"] == "tokacct"
    assert block["name"] == "Token Acct"
    assert block["followers"] == 1200
    assert block["x_kind"] == "official"
    assert block["following"] == 10
    assert block["tweets"] == 40
    assert block["protected"] is False
    assert block["verification"] == {"verified": False, "type": None}
    assert block["account_age_days"] == 30
    assert block["fetched_via"] == "fxtwitter"
    assert block["own_posts_count"] == 8
    assert len(block["recent_posts"]) == 9
    assert all(len(p["text"]) <= 200 for p in block["recent_posts"])
    assert block["recent_posts"][0]["author"]["screen_name"] == "someoneelse"
    assert any("api.fxtwitter.com/tokacct" in u for u in calls)
    assert any("/2/profile/tokacct/statuses" in u for u in calls)
    assert all(isinstance(c, str) for c in calls)


@pytest.mark.parametrize("status", [404, 429])
def test_http_error_returns_none_and_logs_safe_err(status, caplog):
    caplog.set_level(logging.INFO)
    get, calls = _route(profile=_resp(status, {"code": status, "message": "no"}))
    with patch("x_lookup.requests.get", side_effect=get):
        assert x_lookup.read_x("missing") is None
    assert any("ok=False" in r.message or "ok=False" in r.message.replace(" ", "")
               for r in caplog.records if r.message.startswith("x "))
    assert f"HTTP {status}" in caplog.text
    assert "err=RuntimeError: HTTP" in caplog.text
    assert not any("/statuses" in u for u in calls)
    profile_calls = [u for u in calls if "/statuses" not in u]
    if status == 404:
        assert len(profile_calls) == 1 + x_lookup.NOT_FOUND_RETRIES
    else:
        assert len(profile_calls) == 1


def test_timeout_returns_none_and_logs_safe_err(caplog):
    caplog.set_level(logging.INFO)
    attempts = []

    def get(url, timeout=None, **kw):
        attempts.append(url)
        raise requests.Timeout("timed out")

    with patch("x_lookup.requests.get", side_effect=get):
        assert x_lookup.read_x("slow") is None
    assert len(attempts) == 2  # one retry on timeout
    assert "Timeout" in caplog.text
    assert "err=" in caplog.text
    line = [r.message for r in caplog.records if r.message.startswith("x ")][0]
    assert "ok=False" in line
    assert "source=none" in line


def test_malformed_json_returns_none_and_logs_safe_err(caplog):
    caplog.set_level(logging.INFO)
    get, _ = _route(profile=_resp(200, ValueError("not json")))
    with patch("x_lookup.requests.get", side_effect=get):
        assert x_lookup.read_x("badjson") is None
    assert "malformed JSON" in caplog.text
    assert "err=" in caplog.text


def test_malformed_envelope_returns_none(caplog):
    caplog.set_level(logging.INFO)
    get, _ = _route(profile=_resp(200, {"code": 200, "user": "nope"}))
    with patch("x_lookup.requests.get", side_effect=get):
        assert x_lookup.read_x("baduser") is None
    assert "malformed JSON" in caplog.text


def test_5xx_retries_once_then_succeeds():
    calls = []

    def get(url, timeout=None, **kw):
        calls.append(url)
        if "/statuses" in url:
            return _resp(200, _statuses_body(n=2))
        if calls.count(url) == 1:
            return _resp(503, {"code": 503})
        return _resp(200, _profile_body())

    with patch("x_lookup.requests.get", side_effect=get):
        block = x_lookup.read_x("tokacct")
    assert block is not None
    assert block["followers"] == 1200
    profile_calls = [u for u in calls if "/statuses" not in u]
    assert len(profile_calls) == 2


def test_cache_hit_makes_no_http_call():
    get, calls = _route()
    with patch("x_lookup.requests.get", side_effect=get):
        first = x_lookup.read_x("tokacct")
        n = len(calls)
        second = x_lookup.read_x("TokAcct")  # case-insensitive cache key
    assert first["followers"] == 1200
    assert second["followers"] == 1200
    assert second["fetched_via"] == "fxtwitter"
    assert len(calls) == n  # no further HTTP


def test_negative_cache_skips_http(caplog):
    caplog.set_level(logging.INFO)
    get, calls = _route(profile=_resp(404, {"code": 404}))
    with patch("x_lookup.requests.get", side_effect=get):
        assert x_lookup.read_x("gone") is None
        n = len(calls)
        assert x_lookup.read_x("gone") is None
    assert len(calls) == n
    cache_lines = [r.message for r in caplog.records
                   if r.message.startswith("x ") and "source=cache" in r.message]
    assert cache_lines


def test_per_cycle_cap_is_respected():
    monkey_calls = []

    def get(url, timeout=None, **kw):
        monkey_calls.append(url)
        if "/statuses" in url:
            return _resp(200, _statuses_body(n=1))
        handle = url.rstrip("/").split("/")[-1]
        return _resp(200, _profile_body(handle=handle))

    with patch.dict(os.environ, {"X_LOOKUPS_PER_CYCLE": "1"}):
        x_lookup.reset_cycle()
        with patch("x_lookup.requests.get", side_effect=get):
            a = x_lookup.read_x("one")
            b = x_lookup.read_x("two")
    assert a is not None
    assert b is None
    assert not any(u.endswith("/two") or "/two?" in u or u.endswith("/two/statuses")
                   or "/profile/two/" in u for u in monkey_calls)


def test_profile_404_retries_then_succeeds():
    """fxtwitter profile flaps 404; retry up to 2 more times with a 1.5s gap."""
    slept = []
    x_lookup.set_sleep_fn(lambda d: slept.append(d))
    profile_calls = []

    def get(url, timeout=None, **kw):
        if "/statuses" in url:
            return _resp(200, _statuses_body(n=3))
        profile_calls.append(url)
        if len(profile_calls) < 3:
            return _resp(404, {"code": 404, "message": "NOT_FOUND"})
        return _resp(200, _profile_body())

    with patch("x_lookup.requests.get", side_effect=get):
        block = x_lookup.read_x("tokacct")
    assert block is not None
    assert block["followers"] == 1200
    assert block["own_posts_count"] == 3
    assert len(profile_calls) == 3
    assert slept == [x_lookup.NOT_FOUND_GAP_SEC, x_lookup.NOT_FOUND_GAP_SEC]
    assert 1.0 <= x_lookup.NOT_FOUND_GAP_SEC <= 2.0


def test_profile_404_budget_abort_does_not_negative_cache():
    """Stopping a profile 404 retry for budget must fail open, not cache the miss."""
    clock = [1_000.0]
    x_lookup.set_time_fn(lambda: clock[0])

    def sleeper(gap):
        clock[0] += float(gap)

    x_lookup.set_sleep_fn(sleeper)
    calls = []

    def get(url, timeout=None, **kw):
        calls.append(url)
        if "/statuses" in url:
            return _resp(200, _statuses_body(n=1))
        return _resp(404, {"code": 404, "message": "NOT_FOUND"})

    with patch.dict(os.environ, {"X_LOOKUP_BUDGET_SEC": "2"}):
        x_lookup.reset_cycle()
        with patch("x_lookup.requests.get", side_effect=get):
            first = x_lookup.read_x("flappy")
            n = len(calls)
            x_lookup.reset_cycle()
            clock[0] = 2_000.0
            second = x_lookup.read_x("flappy")
    assert first is None
    assert second is None
    assert n >= 2  # first attempt + at least one retry before budget
    assert len(calls) > n  # second cycle hits HTTP again: not negative-cached
    assert all("/statuses" not in u for u in calls)


def test_empty_posts_cached_10_min_not_30():
    """Profile ok + 0 posts after retries: 10 min TTL. Posts present: 30 min."""
    clock = [5_000.0]
    x_lookup.set_time_fn(lambda: clock[0])

    def empty_then_full(url, timeout=None, **kw):
        if "/statuses" in url:
            return _resp(404, {"code": 404})
        return _resp(200, _profile_body(followers=50))

    with patch("x_lookup.requests.get", side_effect=empty_then_full) as mock_get:
        first = x_lookup.read_x("quiet")
        assert first is not None and first["own_posts_count"] == 0
        n = mock_get.call_count
        clock[0] += 10 * 60 - 1
        assert x_lookup.read_x("quiet") is not None  # still within 10 min
        assert mock_get.call_count == n
        clock[0] += 2
        x_lookup.reset_cycle()  # cache TTL, not the cycle budget window
        x_lookup.read_x("quiet")
        assert mock_get.call_count > n

    x_lookup.reset()
    clock[0] = 20_000.0
    x_lookup.set_time_fn(lambda: clock[0])
    get_ok, calls = _route()
    with patch("x_lookup.requests.get", side_effect=get_ok):
        assert x_lookup.read_x("tokacct") is not None
        n = len(calls)
        clock[0] += 10 * 60 + 5  # past the empty-post TTL, still inside 30 min
        assert x_lookup.read_x("tokacct") is not None
        assert len(calls) == n
        clock[0] += 20 * 60  # past 30 min
        x_lookup.reset_cycle()
        x_lookup.read_x("tokacct")
        assert len(calls) > n


def test_statuses_404_retries_then_succeeds():
    """fxtwitter statuses flaps 404; retry up to 2 more times with a gap."""
    slept = []
    x_lookup.set_sleep_fn(lambda d: slept.append(d))
    status_calls = []

    def get(url, timeout=None, **kw):
        if "/statuses" in url:
            status_calls.append(url)
            if len(status_calls) < 3:
                return _resp(404, {"code": 404, "message": "NOT_FOUND"})
            return _resp(200, _statuses_body(n=4))
        return _resp(200, _profile_body())

    with patch("x_lookup.requests.get", side_effect=get):
        block = x_lookup.read_x("tokacct")
    assert block is not None
    assert block["followers"] == 1200
    assert block["own_posts_count"] == 4
    assert len(status_calls) == 3
    assert slept == [x_lookup.STATUSES_404_GAP_SEC, x_lookup.STATUSES_404_GAP_SEC]
    assert 1.0 <= x_lookup.STATUSES_404_GAP_SEC <= 2.0


def test_statuses_404_does_not_negative_cache_profile(caplog):
    """Statuses 404 after retries still ships the profile and positive-caches it."""
    caplog.set_level(logging.INFO)
    calls = []

    def get(url, timeout=None, **kw):
        calls.append(url)
        if "/statuses" in url:
            return _resp(404, {"code": 404, "message": "NOT_FOUND"})
        return _resp(200, _profile_body(followers=777))

    with patch("x_lookup.requests.get", side_effect=get):
        first = x_lookup.read_x("tokacct")
        n = len(calls)
        second = x_lookup.read_x("tokacct")
    assert first is not None
    assert first["followers"] == 777
    assert first["own_posts_count"] == 0
    assert second is not None
    assert second["followers"] == 777
    assert len(calls) == n  # cache hit, not a negative miss
    assert any("source=cache" in r.message and "ok=True" in r.message
               for r in caplog.records if r.message.startswith("x "))
    statuses = [u for u in calls if "/statuses" in u]
    assert len(statuses) == 1 + x_lookup.STATUSES_404_RETRIES


def test_lookup_budget_skips_remaining_and_logs(caplog):
    caplog.set_level(logging.INFO)
    clock = [1_000.0]
    x_lookup.set_time_fn(lambda: clock[0])
    calls = []

    def get(url, timeout=None, **kw):
        calls.append(url)
        if "/statuses" not in url:
            clock[0] += 35.0  # one lookup ≈ 35s wall-clock
        if "/statuses" in url:
            return _resp(200, _statuses_body(n=1))
        handle = url.rstrip("/").split("/")[-1]
        return _resp(200, _profile_body(handle=handle))

    with patch.dict(os.environ, {"X_LOOKUP_BUDGET_SEC": "60"}):
        x_lookup.reset_cycle()
        with patch("x_lookup.requests.get", side_effect=get):
            a = x_lookup.read_x("one", tid="t1")
            b = x_lookup.read_x("two", tid="t2")
            c = x_lookup.read_x("three", tid="t3")
    assert a is not None
    assert b is not None
    assert c is None
    assert not any("three" in u for u in calls)
    assert any("x budget exceeded" in r.message for r in caplog.records)
    skip = [r.message for r in caplog.records if r.message.startswith("x tid=t3")]
    assert skip and "err=budget" in skip[0] and "ok=False" in skip[0]


def test_lookup_budget_zero_skips_all_http():
    calls = []

    def get(url, timeout=None, **kw):
        calls.append(url)
        return _resp(200, _profile_body())

    with patch.dict(os.environ, {"X_LOOKUP_BUDGET_SEC": "0"}):
        x_lookup.reset_cycle()
        with patch("x_lookup.requests.get", side_effect=get):
            assert x_lookup.read_x("one") is None
    assert calls == []


def test_cache_hit_does_not_consume_cap():
    get, calls = _route()
    with patch.dict(os.environ, {"X_LOOKUPS_PER_CYCLE": "1"}):
        x_lookup.reset_cycle()
        with patch("x_lookup.requests.get", side_effect=get):
            assert x_lookup.read_x("tokacct") is not None
            assert x_lookup.read_x("tokacct") is not None  # cache
            assert x_lookup.read_x("other") is None       # cap
    assert not any("other" in u for u in calls)


def test_exception_text_goes_through_safe_err(caplog):
    caplog.set_level(logging.INFO)

    def boom(url, timeout=None, **kw):
        raise RuntimeError("retry url: https://x.com/x?api-key=SECRET123")

    with patch("x_lookup.requests.get", side_effect=boom):
        assert x_lookup.read_x("abc") is None
    assert "SECRET123" not in caplog.text
    assert "REDACTED" in caplog.text
    assert "err=RuntimeError:" in caplog.text


def test_info_line_format(caplog):
    caplog.set_level(logging.INFO)
    get, _ = _route()
    with patch("x_lookup.requests.get", side_effect=get):
        x_lookup.read_x("tokacct", tid="Addr:1399811149")
    lines = [r.message for r in caplog.records if r.message.startswith("x tid=")]
    assert lines
    line = lines[0]
    assert "handle=tokacct" in line
    assert "ok=True" in line
    assert "followers=1200" in line
    assert "own_posts=" in line
    assert "source=fxtwitter" in line
    assert "err=-" in line
    assert "tid=Addr:1399811149" in line
    assert " x_kind=" in line
    assert line.index(" err=") < line.index(" x_kind=")
    prefix = line.split(" x_kind=")[0]
    assert prefix.startswith("x tid=")
    assert all(k in prefix for k in (
        "handle=", "ok=", "followers=", "own_posts=", "source=", "err=",
    ))


# ---- handle sources: GT, DexScreener, FOMO -----------------------------------

def test_fill_x_handle_gt_wins():
    d = {"x_handle": collect.clean_handle("gthandle"),
         "dex_twitter": "https://x.com/dexhandle",
         "fomo_twitter": "fomohandle"}
    collect.fill_x_handle(d)
    assert d["x_handle"] == "gthandle"


def test_fill_x_handle_dex_then_fomo():
    d = {"x_handle": None,
         "dex_twitter": "https://x.com/dexhandle",
         "fomo_twitter": "fomohandle"}
    collect.fill_x_handle(d)
    assert d["x_handle"] == "dexhandle"

    d = {"x_handle": None, "dex_twitter": None,
         "fomo_twitter": "https://twitter.com/fomohandle"}
    collect.fill_x_handle(d)
    assert d["x_handle"] == "fomohandle"

    d = {"x_handle": None, "dex_twitter": "https://x.com/i/communities/1",
         "fomo_twitter": "fomohandle"}
    collect.fill_x_handle(d)
    assert d["x_handle"] == "fomohandle"


def test_trade_counts_stashes_dex_twitter():
    pairs = [{
        "liquidity": {"usd": 50_000},
        "txns": {"h1": {"buys": 10, "sells": 8},
                 "h6": {"buys": 50, "sells": 45},
                 "h24": {"buys": 200, "sells": 180}},
        "info": {"socials": [{"type": "twitter", "url": "https://x.com/pairacct"}]},
    }]
    mock_resp = Mock(status_code=200, json=lambda: pairs)
    token = {"addr": "test", "ticker": "TEST", "net": 1399811149}
    with patch("collect.requests.get", return_value=mock_resp):
        data, status = collect.trade_counts(token)
    assert status == "ok"
    assert token["dex_twitter"] == "https://x.com/pairacct"
    assert data["dex_twitter"] == "https://x.com/pairacct"


def test_normalise_keeps_fomo_twitter():
    t = collect.normalise("Addr1:56", {
        "symbol": "T0", "mcap": 300_000, "liq": 48_000, "vol24": 610_000,
        "price": 0.001, "holders": 310,
        "change": {300: 0.04, 3600: 0.22, 14400: 0.4, 86400: 0.61},
        "created": int(time.time() * 1000) - 42 * 60_000,
        "twitter": "https://x.com/fomoacct",
    })
    assert t["fomo_twitter"] == "https://x.com/fomoacct"


def test_apply_gt_info_fills_from_dex_when_gt_has_no_handle():
    t = {"addr": "a", "net": 1399811149, "tid": "a:1399811149", "ticker": "T",
         "holder_count": 200, "dex_twitter": "https://x.com/fromdex",
         "fomo_twitter": "fromfomo"}
    d = collect._apply_gt_info_attributes(dict(t), t, "solana", {
        "holders": {"count": 200},
        "twitter_handle": None,
        "description": "x",
    })
    assert d["x_handle"] == "fromdex"


# ---- desk.read_x wiring ------------------------------------------------------

def test_desk_read_x_uses_fxtwitter_when_social_url_unset(monkeypatch):
    monkeypatch.delenv("SOCIAL_URL", raising=False)
    desk = Desk()
    get, _ = _route()
    with patch("x_lookup.requests.get", side_effect=get):
        block = desk.read_x("tokacct")
    assert block and block["fetched_via"] == "fxtwitter"
    assert block["followers"] == 1200


def test_desk_read_x_passes_tid_into_log(monkeypatch, caplog):
    monkeypatch.delenv("SOCIAL_URL", raising=False)
    desk = Desk()
    caplog.set_level(logging.INFO)
    get, _ = _route()
    with patch("x_lookup.requests.get", side_effect=get):
        desk.read_x("tokacct", tid="XTok:1399811149")
    lines = [r.message for r in caplog.records if r.message.startswith("x tid=")]
    assert lines
    assert "tid=XTok:1399811149" in lines[0]
    assert "handle=tokacct" in lines[0]


def test_desk_read_x_keeps_social_url_path(monkeypatch):
    monkeypatch.setenv("SOCIAL_URL", "http://social.test/x")
    desk = Desk()
    posted = []

    def fake_post(url, json=None, timeout=None):
        posted.append((url, json))
        r = Mock()
        r.raise_for_status = Mock()
        r.json.return_value = {"x_account": {"handle": "bot", "followers": 9}}
        return r

    with patch("desk.requests.post", side_effect=fake_post):
        with patch("x_lookup.requests.get", side_effect=AssertionError("no fxtwitter")):
            block = desk.read_x("tokacct")
    assert posted == [("http://social.test/x", {"x_handle": "tokacct"})]
    assert block["handle"] == "bot"
    assert block["followers"] == 9
    assert block["x_kind"] == "official"


def test_desk_social_url_marks_famous_borrowed(monkeypatch):
    monkeypatch.setenv("SOCIAL_URL", "http://social.test/x")
    desk = Desk()

    def fake_post(url, json=None, timeout=None):
        r = Mock()
        r.raise_for_status = Mock()
        r.json.return_value = {"x_account": {"handle": "elonmusk", "followers": 9}}
        return r

    with patch("desk.requests.post", side_effect=fake_post):
        block = desk.read_x("elonmusk", token=_casino_token())
    assert block["x_kind"] == "borrowed"


def test_desk_read_x_social_url_failure_uses_safe_err(monkeypatch, caplog):
    monkeypatch.setenv("SOCIAL_URL", "http://social.test/x")
    desk = Desk()
    caplog.set_level(logging.WARNING)

    def boom(*a, **k):
        raise RuntimeError("https://social.test/?token=LEAKED")

    with patch("desk.requests.post", side_effect=boom):
        assert desk.read_x("tokacct") is None
    assert "LEAKED" not in caplog.text
    assert "REDACTED" in caplog.text or "RuntimeError:" in caplog.text


# ---- social judge only when x_account is truthy; NO_SOCIAL_CUT on fail -------

def _pass_market():
    return {
        "concentration_is_exit_risk": {"type": "noul", "noul": 0.30},
        "momentum_already_spent": {"type": "noul", "noul": 0.25},
        "liquidity_fits_ticket": {"type": "noul", "noul": 0.72},
        "shape": {
            "type": "choice", "choice": "crowd", "confidence": 0.80,
            "probabilities": {"crowd": 0.80, "one_buyer": 0.10,
                              "fading": 0.05, "too_early": 0.05},
        },
        "authority_risk": {
            "type": "choice", "choice": "renounced", "confidence": 0.9,
            "probabilities": {"renounced": 0.9, "mint_open": 0.05,
                              "freeze_open": 0.03, "both_open": 0.02},
        },
    }


def _pass_social():
    return {
        "account_is_the_project": {"type": "noul", "noul": 0.85},
        "audience_is_real": {"type": "noul", "noul": 0.70},
        "recycled_account": {"type": "noul", "noul": 0.20},
        "effort": {"type": "score", "score": 2.5, "confidence": 0.8,
                   "legend": {}, "probabilities": {0: 0.1, 1: 0.2, 2: 0.5, 3: 0.2}},
    }


class _RecDesk(Desk):
    def __init__(self):
        super().__init__()
        self.shadow = []

    def log_shadow(self, order, stats, fomo_data=None):
        self.shadow.append(order)

    def report(self, o, s):
        pass

    def send_to_seats(self, o):
        pass


def _wire_judge_token(monkeypatch, x_handle="tokacct"):
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    tid = f"XTok:{1399811149}"

    def fake_shortlist(fomo, id_list):
        return [{
            "addr": "XTok", "net": 1399811149, "tid": tid, "ticker": "XTK",
            "mcap_usd": 300_000, "liquidity_usd": 48_000, "volume_h24": 610_000,
            "price_usd": 0.001, "holder_count": 310,
            "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61},
            "age_minutes": 42, "chain": "solana",
        }]

    def fake_dossier(t, limiter=None):
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": False, "freeze_authority": False,
                "description": "a token", "x_handle": x_handle, "net": 1399811149}

    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(collect, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: (
        {"buys_h1": 540, "sells_h1": 120, "buys_h6": 900, "sells_h6": 400,
         "trades_h24": 4000}, "ok"))
    monkeypatch.setattr(shift, "dossier", fake_dossier)
    return tid


def test_run_once_x_log_includes_token_tid(monkeypatch, caplog):
    monkeypatch.delenv("SOCIAL_URL", raising=False)
    _wire_judge_token(monkeypatch, x_handle="tokacct")
    caplog.set_level(logging.INFO)
    desk = _RecDesk()
    get, _ = _route()

    def judge(qs, state):
        return {"model": "test", "answers": _pass_market(), "usage": {}}

    with patch("x_lookup.requests.get", side_effect=get):
        shift.run_once(type("F", (), {"token": lambda self: None})(),
                       judge, desk, 1000.0, shadow=True, gt_dossier_reserve=3)
    lines = [r.message for r in caplog.records if r.message.startswith("x tid=")]
    assert lines
    assert "tid=XTok:1399811149" in lines[0]


def test_social_judge_only_when_x_account_truthy(monkeypatch):
    _wire_judge_token(monkeypatch, x_handle="tokacct")
    sets_ok = []
    sets_fail = []

    def judge_ok(qs, state):
        sets_ok.append(qs)
        if qs == "social":
            assert state.get("x_account")
            return {"model": "test", "answers": _pass_social(), "usage": {}}
        if qs == "pick":
            ticker = state["candidates"][0]["ticker"]
            return {"model": "test", "answers": {
                "best": {"type": "choice", "choice": ticker, "confidence": 0.9,
                         "probabilities": {ticker: 0.9}},
                "worth_trading_at_all": {"type": "noul", "noul": 0.8},
            }, "usage": {}}
        return {"model": "test", "answers": _pass_market(), "usage": {}}

    monkeypatch.delenv("SOCIAL_URL", raising=False)
    desk = _RecDesk()
    get, _ = _project_route(handle="tokacct", ticker="XTK")
    with patch("x_lookup.requests.get", side_effect=get):
        shift.run_once(type("F", (), {"token": lambda self: None})(),
                       judge_ok, desk, 1000.0, shadow=True, gt_dossier_reserve=3)
    assert "social" in sets_ok
    assert desk.shadow
    assert "account_is_the_project" in desk.shadow[0]["why"]
    assert desk.shadow[0]["size_factor"] == 1.0
    s = summary({"chain": "solana", "age_minutes": 42, "mcap_usd": 1,
                 "liquidity_usd": 1, "holder_count": 10, "ticker": "XTK"},
                desk.shadow[0]["why"])
    assert "official account" in s
    assert "no usable X account" not in s

    x_lookup.reset()
    _wire_judge_token(monkeypatch, x_handle="missingacct")

    def judge_fail(qs, state):
        sets_fail.append(qs)
        if qs == "pick":
            ticker = state["candidates"][0]["ticker"]
            return {"model": "test", "answers": {
                "best": {"type": "choice", "choice": ticker, "confidence": 0.9,
                         "probabilities": {ticker: 0.9}},
                "worth_trading_at_all": {"type": "noul", "noul": 0.8},
            }, "usage": {}}
        return {"model": "test", "answers": _pass_market(), "usage": {}}

    desk2 = _RecDesk()
    get404, _ = _route(profile=_resp(404, {"code": 404}))
    with patch("x_lookup.requests.get", side_effect=get404):
        monkeypatch.setattr(shift, "dossier", lambda t, limiter=None: {
            **t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
            "developer_holding_percentage": 2, "gt_score_details": None,
            "is_honeypot": None, "mint_authority": False, "freeze_authority": False,
            "description": "a token", "x_handle": "missingacct", "net": 1399811149})
        shift.run_once(type("F", (), {"token": lambda self: None})(),
                       judge_fail, desk2, 1000.0, shadow=True, gt_dossier_reserve=3)
    assert "social" not in sets_fail
    assert desk2.shadow
    assert "account_is_the_project" not in desk2.shadow[0]["why"]
    assert desk2.shadow[0]["size_factor"] == NO_SOCIAL_CUT
    s2 = summary({"chain": "solana", "age_minutes": 42, "mcap_usd": 1,
                  "liquidity_usd": 1, "holder_count": 10, "ticker": "XTK"},
                 desk2.shadow[0]["why"])
    assert "no usable X account" in s2


def test_no_social_cut_when_lookup_fails():
    ans = _pass_market()
    assert size_factor_for(ans) == NO_SOCIAL_CUT
    ans2 = {**ans, **_pass_social()}
    assert size_factor_for(ans2) == 1.0


def test_no_handle_skips_lookup_and_social(monkeypatch):
    monkeypatch.delenv("SOCIAL_URL", raising=False)
    _wire_judge_token(monkeypatch, x_handle=None)
    sets_called = []

    def judge(qs, state):
        sets_called.append(qs)
        if qs == "pick":
            ticker = state["candidates"][0]["ticker"]
            return {"model": "test", "answers": {
                "best": {"type": "choice", "choice": ticker, "confidence": 0.9,
                         "probabilities": {ticker: 0.9}},
                "worth_trading_at_all": {"type": "noul", "noul": 0.8},
            }, "usage": {}}
        return {"model": "test", "answers": _pass_market(), "usage": {}}

    desk = _RecDesk()
    with patch("x_lookup.requests.get", side_effect=AssertionError("no lookup without handle")):
        shift.run_once(type("F", (), {"token": lambda self: None})(),
                       judge, desk, 1000.0, shadow=True, gt_dossier_reserve=3)
    assert "social" not in sets_called
    assert desk.shadow[0]["size_factor"] == NO_SOCIAL_CUT


# ---- hard rules: thresholds and log format strings ---------------------------

def test_thresholds_py_unchanged():
    r = subprocess.run(
        ["git", "diff", "--", "thresholds.py"],
        cwd=str(ROOT), capture_output=True, text=True, check=True,
    )
    assert r.stdout == ""
    src = (ROOT / "thresholds.py").read_text()
    assert "NO_SOCIAL_CUT   = 0.60" in src


def test_cycle_soft_unevaluated_judge_format_strings_unchanged():
    src = (ROOT / "main.py").read_text()
    assert (
        'log.info("cycle: %(seen)s seen, %(benched)s benched, free %(free)s, "\n'
        '             "trade %(trade)s, chain %(chain)s, soft %(soft)s, '
        'judged %(judged)s, requeued %(requeued)s", stats)'
    ) in src
    assert (
        'log.info("soft tid=%s ticker=%s reason=%s noul=%s age_minutes=%s '
        'top_10_percent=%s top_wallet_percent=%s developer_holding_percentage=%s '
        'holder_count=%s rpc_ok=%s soft_scores=%s"'
    ) in src
    assert 'log.info("unevaluated %d ids (dex_slots=%d, gt_available=%d)",' in src
    assert (
        'log.info(\n'
        '        "judge tid=%s ticker=%s verdict=%s score=%s confidence=%s reason=%s mode=%s",'
    ) in src or (
        '"judge tid=%s ticker=%s verdict=%s score=%s confidence=%s reason=%s mode=%s"'
    ) in src


def test_main_still_desk_read_x_with_tid():
    src = (ROOT / "main.py").read_text()
    assert (
        'd["x_account"] = desk.read_x(d["x_handle"], tid=d.get("tid"), token=d) '
        'if d["x_handle"] else None'
    ) in src
    xsrc = (ROOT / "x_lookup.py").read_text()
    assert (
        '"x tid=%s handle=%s ok=%s followers=%s own_posts=%s source=%s err=%s"'
    ) in xsrc
    assert "x_kind=%s" in xsrc


# ---- borrowed / celebrity accounts -------------------------------------------

def _casino_token(**extra):
    return {
        "ticker": "casino",
        "name": "Casino Coin",
        "addr": "CasAddr111111111111111111111111111111",
        "age_minutes": 42,
        **extra,
    }


def test_famous_handle_is_borrowed():
    get, _ = _route(
        profile=_resp(200, _profile_body(
            handle="elonmusk", followers=200_000_000, name="Elon Musk",
            description="Mars", joined="2009-06-02T00:00:00Z",
        )),
        statuses=_resp(200, _statuses_body(handle="elonmusk", n=2)),
    )
    with patch("x_lookup.requests.get", side_effect=get):
        block = x_lookup.read_x("elonmusk", token=_casino_token())
    assert block["x_kind"] == "borrowed"


def test_famous_handle_case_insensitive_and_without_token():
    get, _ = _route(
        profile=_resp(200, _profile_body(handle="CZ_Binance", followers=9000)),
    )
    with patch("x_lookup.requests.get", side_effect=get):
        block = x_lookup.read_x("CZ_Binance")
    assert block["x_kind"] == "borrowed"
    assert "cz_binance" in x_lookup.FAMOUS_HANDLES
    assert "elonmusk" in x_lookup.FAMOUS_HANDLES


def test_followers_over_1m_is_borrowed_even_if_mentions_token():
    get, _ = _project_route(handle="hugeacct", ticker="casino", followers=1_000_001)
    with patch("x_lookup.requests.get", side_effect=get):
        block = x_lookup.read_x("hugeacct", token=_casino_token())
    assert block["x_kind"] == "borrowed"
    get2, _ = _project_route(handle="almost", ticker="casino", followers=1_000_000)
    x_lookup.reset()
    with patch("x_lookup.requests.get", side_effect=get2):
        block2 = x_lookup.read_x("almost", token=_casino_token())
    assert block2["x_kind"] == "official"


def test_old_account_no_mention_is_borrowed():
    get, _ = _route(
        profile=_resp(200, _profile_body(
            handle="oldacct", followers=50_000, name="Random Person",
            description="just vibes", joined="2020-01-01T00:00:00Z",
        )),
        statuses=_resp(200, _statuses_body(handle="oldacct", n=3)),
    )
    with patch("x_lookup.requests.get", side_effect=get):
        block = x_lookup.read_x("oldacct", token=_casino_token())
    assert block["account_age_days"] is not None
    assert block["account_age_days"] >= 90
    assert block["x_kind"] == "borrowed"


def test_old_account_mentioning_ticker_is_official():
    get, _ = _route(
        profile=_resp(200, _profile_body(
            handle="oldproj", followers=8000, name="Casino Token",
            description="the official $casino launch", joined="2020-01-01T00:00:00Z",
        )),
        statuses=_resp(200, _statuses_body(handle="oldproj", n=2)),
    )
    with patch("x_lookup.requests.get", side_effect=get):
        block = x_lookup.read_x("oldproj", token=_casino_token())
    assert block["x_kind"] == "official"


def test_old_account_mentioning_contract_is_official():
    addr = "CasAddr111111111111111111111111111111"
    get, _ = _route(
        profile=_resp(200, _profile_body(
            handle="oldproj", followers=8000, name="A Wallet",
            description=f"ca {addr}", joined="2020-01-01T00:00:00Z",
        )),
        statuses=_resp(200, _statuses_body(handle="oldproj", n=1)),
    )
    with patch("x_lookup.requests.get", side_effect=get):
        block = x_lookup.read_x("oldproj", token=_casino_token())
    assert block["x_kind"] == "official"


def test_young_account_no_mention_is_official():
    get, _ = _route(
        profile=_resp(200, _profile_body(
            handle="newacct", followers=200, name="New Acct",
            description="just launched", joined="2026-10-11T12:00:00Z",
        )),
        statuses=_resp(200, _statuses_body(handle="newacct", n=2)),
    )
    with patch("x_lookup.requests.get", side_effect=get):
        block = x_lookup.read_x("newacct", token=_casino_token())
    assert block["x_kind"] == "official"


def test_x_kind_not_cached_is_token_specific():
    get, calls = _route(
        profile=_resp(200, _profile_body(
            handle="shared", followers=4000, name="Foo Desk",
            description="the official $FOO token", joined="2020-01-01T00:00:00Z",
        )),
        statuses=_resp(200, _statuses_body(handle="shared", n=2)),
    )
    with patch("x_lookup.requests.get", side_effect=get):
        foo = x_lookup.read_x("shared", token={
            "ticker": "FOO", "age_minutes": 42, "addr": "FooAddr11111111111111111111111111",
        })
        bar = x_lookup.read_x("shared", token=_casino_token())
    assert foo["x_kind"] == "official"
    assert bar["x_kind"] == "borrowed"
    assert sum("/statuses" in u for u in calls) == 1  # second read is cache


def test_x_tid_line_appends_x_kind_borrowed(caplog):
    caplog.set_level(logging.INFO)
    get, _ = _route(
        profile=_resp(200, _profile_body(
            handle="elonmusk", followers=200_000_000, name="Elon Musk",
            description="Mars",
        )),
    )
    with patch("x_lookup.requests.get", side_effect=get):
        x_lookup.read_x("elonmusk", tid="Cas:1399811149", token=_casino_token())
    lines = [r.message for r in caplog.records if r.message.startswith("x tid=")]
    assert lines
    line = lines[0]
    assert line.startswith(
        "x tid=Cas:1399811149 handle=elonmusk ok=True followers=200000000"
    )
    assert "source=fxtwitter" in line
    assert "err=-" in line
    assert line.endswith("x_kind=borrowed") or " x_kind=borrowed" in line
    assert line.index(" err=") < line.index(" x_kind=")


def test_summary_borrowed_not_official():
    d = {
        "chain": "solana", "age_minutes": 42, "mcap_usd": 1,
        "liquidity_usd": 1, "holder_count": 10, "ticker": "casino",
        "x_account": {"handle": "elonmusk", "x_kind": "borrowed"},
    }
    ans = {
        "shape": {"probabilities": {"crowd": 0.7}},
        "concentration_is_exit_risk": {"noul": 0.2},
    }
    s = summary(d, ans)
    assert "linked X is unrelated big account" in s
    assert "official account" not in s
    assert "no usable X account" not in s
    assert size_factor_for(ans) == NO_SOCIAL_CUT


def test_classify_fail_open_is_official():
    block = {"handle": "tokacct", "followers": "not-a-number",
             "account_age_days": "also-bad"}
    assert x_lookup.classify_x_kind(block, _casino_token()) == "official"
    stamped = x_lookup.apply_x_kind(None)
    assert stamped is None


def test_social_judge_skipped_for_borrowed_elonmusk(monkeypatch):
    _wire_judge_token(monkeypatch, x_handle="elonmusk")
    sets_called = []

    def judge(qs, state):
        sets_called.append(qs)
        if qs == "social":
            raise AssertionError("borrowed account must not go to social judge")
        if qs == "pick":
            ticker = state["candidates"][0]["ticker"]
            return {"model": "test", "answers": {
                "best": {"type": "choice", "choice": ticker, "confidence": 0.9,
                         "probabilities": {ticker: 0.9}},
                "worth_trading_at_all": {"type": "noul", "noul": 0.8},
            }, "usage": {}}
        return {"model": "test", "answers": _pass_market(), "usage": {}}

    monkeypatch.delenv("SOCIAL_URL", raising=False)
    desk = _RecDesk()
    get, _ = _route(
        profile=_resp(200, _profile_body(
            handle="elonmusk", followers=200_000_000, name="Elon Musk",
            description="Mars",
        )),
    )
    with patch("x_lookup.requests.get", side_effect=get):
        monkeypatch.setattr(shift, "dossier", lambda t, limiter=None: {
            **t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
            "developer_holding_percentage": 2, "gt_score_details": None,
            "is_honeypot": None, "mint_authority": False, "freeze_authority": False,
            "description": "casino coin", "x_handle": "elonmusk", "net": 1399811149,
            "ticker": "casino"})
        shift.run_once(type("F", (), {"token": lambda self: None})(),
                       judge, desk, 1000.0, shadow=True, gt_dossier_reserve=3)
    assert "social" not in sets_called
    assert desk.shadow
    assert "account_is_the_project" not in desk.shadow[0]["why"]
    assert desk.shadow[0]["size_factor"] == NO_SOCIAL_CUT
    s = summary({"chain": "solana", "age_minutes": 42, "mcap_usd": 1,
                 "liquidity_usd": 1, "holder_count": 10, "ticker": "casino",
                 "x_account": {"handle": "elonmusk", "x_kind": "borrowed"}},
                desk.shadow[0]["why"])
    assert "linked X is unrelated big account" in s
    assert "official account" not in s
