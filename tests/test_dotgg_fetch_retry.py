"""Retry/backoff behaviour for the dotgg JSON fetchers.

dotgg.gg occasionally returns a 504 Gateway Timeout mid-pagination. Before this
fix a single blip raised straight out of urlopen and failed the whole weekly
price refresh (run 27544367223, 2026-06-15). These tests pin the behaviour:
transient errors are retried with exponential backoff, non-transient HTTP
errors fail fast, and a real outage still surfaces loudly after max retries.

Mirrors the retry convention already established in scripts.ebay_client.search.
"""

import json
import urllib.error

import pytest

import scripts.backfill_prices_dotgg as dotgg
import scripts.fetch_dotgg_catalog as catalog


class _FakeResp:
    """Minimal stand-in for the urlopen context-manager return value."""

    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code):
    return urllib.error.HTTPError("http://dotgg.test", code, "boom", {}, None)


def _sequenced_urlopen(*outcomes):
    """Return a fake urlopen that yields each outcome in turn.

    An outcome that is an Exception is raised; anything else is returned.
    """
    it = iter(outcomes)

    def _urlopen(req, timeout=None):
        outcome = next(it)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return _urlopen


# ── scripts.backfill_prices_dotgg._fetch_json ──────────────────────────────

def test_backfill_fetch_retries_on_504_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(dotgg.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        dotgg.urllib.request, "urlopen",
        _sequenced_urlopen(_http_error(504), _http_error(504), _FakeResp({"ok": 1})),
    )
    assert dotgg._fetch_json("http://dotgg.test") == {"ok": 1}
    # Exponential backoff between the three attempts: 2**0, 2**1.
    assert sleeps == [1, 2]


def test_backfill_fetch_gives_up_loudly_after_max_retries(monkeypatch):
    monkeypatch.setattr(dotgg.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        dotgg.urllib.request, "urlopen",
        _sequenced_urlopen(*[_http_error(504)] * 8),
    )
    with pytest.raises(RuntimeError, match="after 4 attempts"):
        dotgg._fetch_json("http://dotgg.test")


def test_backfill_fetch_does_not_retry_on_404(monkeypatch):
    sleeps = []
    monkeypatch.setattr(dotgg.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        dotgg.urllib.request, "urlopen",
        _sequenced_urlopen(_http_error(404), _FakeResp({"never": "reached"})),
    )
    with pytest.raises(urllib.error.HTTPError):
        dotgg._fetch_json("http://dotgg.test")
    assert sleeps == []  # 404 means the endpoint moved/blocked — fail fast


def test_backfill_fetch_retries_on_connection_error(monkeypatch):
    sleeps = []
    monkeypatch.setattr(dotgg.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        dotgg.urllib.request, "urlopen",
        _sequenced_urlopen(urllib.error.URLError("conn reset"), _FakeResp({"ok": 2})),
    )
    assert dotgg._fetch_json("http://dotgg.test") == {"ok": 2}
    assert sleeps == [1]


# ── scripts.fetch_dotgg_catalog.fetch_json (same hardening) ────────────────

def test_catalog_fetch_retries_on_504_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(catalog.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        catalog.urllib.request, "urlopen",
        _sequenced_urlopen(_http_error(504), _FakeResp({"ok": 3})),
    )
    assert catalog.fetch_json("http://dotgg.test") == {"ok": 3}
    assert sleeps == [1]


def test_catalog_fetch_gives_up_loudly_after_max_retries(monkeypatch):
    monkeypatch.setattr(catalog.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        catalog.urllib.request, "urlopen",
        _sequenced_urlopen(*[_http_error(503)] * 8),
    )
    with pytest.raises(RuntimeError, match="after 4 attempts"):
        catalog.fetch_json("http://dotgg.test")


# ── IncompleteRead: stream dropped mid-body (run 42639473174, 2026-11-09) ──

def _incomplete():
    import http.client
    return http.client.IncompleteRead(b"", 100)


def test_catalog_fetch_retries_on_incomplete_read(monkeypatch):
    monkeypatch.setattr(catalog.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        catalog.urllib.request, "urlopen",
        _sequenced_urlopen(_incomplete(), _FakeResp({"ok": 1})),
    )
    assert catalog.fetch_json("http://dotgg.test") == {"ok": 1}


def test_backfill_fetch_retries_on_incomplete_read(monkeypatch):
    monkeypatch.setattr(dotgg.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        dotgg.urllib.request, "urlopen",
        _sequenced_urlopen(_incomplete(), _FakeResp({"ok": 1})),
    )
    assert dotgg._fetch_json("http://dotgg.test") == {"ok": 1}
