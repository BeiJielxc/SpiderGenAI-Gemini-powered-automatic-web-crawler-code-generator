"""Tests for the URL-keyed PageCache (Module A).

Covers:

* Round-trip: ``put_html`` then ``get`` returns the exact bytes within
  TTL and ``None`` past TTL.
* Per-URL hashing: same domain, different URLs → independent rows.
* Drift detection: putting a structurally different HTML for the same
  URL flips ``last_drift=True`` on the new entry.
* Invalidation by URL and by domain.
* Disabled cache silently no-ops.
* LRU eviction kicks in when ``max_total_mb`` is exceeded.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PYGEN_DIR = Path(__file__).resolve().parent.parent
if str(PYGEN_DIR) not in sys.path:
    sys.path.insert(0, str(PYGEN_DIR))

from page_cache import (  # noqa: E402
    PageCache,
    _safe_domain,
    _url_hash,
    get_default_page_cache,
    set_default_page_cache,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _html(structure: str = "default", body_text: str = "") -> str:
    """Build a tiny HTML doc with a controllable structure for fingerprint
    tests. Different ``structure`` values produce different fingerprints;
    different ``body_text`` keeps the same fingerprint (text-content
    bucketing only)."""
    if structure == "default":
        return f"<html><body><div class='news'><h1>{body_text}</h1></div></body></html>"
    if structure == "drifted":
        return f"<html><body><article><section><h2>{body_text}</h2></section></article></body></html>"
    raise ValueError(f"unknown structure: {structure}")


# ---------------------------------------------------------------------------
# Basic round-trip
# ---------------------------------------------------------------------------


def test_put_get_roundtrip(tmp_path):
    cache = PageCache(tmp_path, ttl_sec=60)
    url = "https://example.com/news/?page=1"
    cache.put_html(url, "<html><body>hi</body></html>")
    entry = cache.get(url)
    assert entry is not None
    assert entry.url == url
    assert entry.html == "<html><body>hi</body></html>"
    assert entry.byte_size == len("<html><body>hi</body></html>".encode("utf-8"))
    assert entry.last_drift is False


def test_get_returns_none_past_ttl(tmp_path):
    """ttl_sec=0 disables the TTL entirely (sentinel) so we use a tiny
    positive TTL and lie about ``fetched_at`` instead.
    """
    cache = PageCache(tmp_path, ttl_sec=1)
    url = "https://example.com/x"
    cache.put_html(url, "<html><body>x</body></html>")
    # Manually backdate the meta file so the entry is past TTL without
    # waiting on real time.
    paths = cache._paths_for(url)
    import json
    meta = json.loads(paths.meta.read_text(encoding="utf-8"))
    meta["fetched_at"] = time.time() - 10  # 10s old, ttl=1s → stale
    paths.meta.write_text(json.dumps(meta), encoding="utf-8")
    assert cache.get(url) is None


def test_disabled_cache_is_noop(tmp_path):
    cache = PageCache(tmp_path, enabled=False)
    assert cache.put_html("https://x.com", "<html/>") is None
    assert cache.get("https://x.com") is None
    assert cache.invalidate(url="https://x.com") == 0


# ---------------------------------------------------------------------------
# URL hashing — different URLs must not collide
# ---------------------------------------------------------------------------


def test_per_url_isolation(tmp_path):
    cache = PageCache(tmp_path, ttl_sec=60)
    cache.put_html("https://example.com/p?x=1", "ONE")
    cache.put_html("https://example.com/p?x=2", "TWO")
    assert cache.get("https://example.com/p?x=1").html == "ONE"
    assert cache.get("https://example.com/p?x=2").html == "TWO"


def test_url_hash_stable_and_short():
    h1 = _url_hash("https://x.com/a")
    h2 = _url_hash("https://x.com/a")
    h3 = _url_hash("https://x.com/b")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 16
    assert all(c in "0123456789abcdef" for c in h1)


def test_safe_domain_strips_port_and_invalids():
    assert _safe_domain("https://Example.COM:8080/a") == "example.com"
    assert _safe_domain("not a url") == "_unknown"
    assert _safe_domain("") == "_unknown"


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


def test_drift_detected_on_structure_change(tmp_path):
    cache = PageCache(tmp_path, ttl_sec=60, invalidate_on_fingerprint_mismatch=True)
    url = "https://news.example.com/today"
    cache.put_html(url, _html("default", "hello"))
    first = cache.get(url)
    assert first.last_drift is False

    # Same URL, structurally different markup → drift.
    cache.put_html(url, _html("drifted", "world"))
    second = cache.get(url)
    assert second.last_drift is True
    assert second.html_fingerprint != first.html_fingerprint


def test_drift_not_flagged_when_only_text_changes(tmp_path):
    cache = PageCache(tmp_path, ttl_sec=60)
    url = "https://news.example.com/today"
    cache.put_html(url, _html("default", "yesterday"))
    cache.put_html(url, _html("default", "today"))
    second = cache.get(url)
    # Text-only change keeps fingerprint stable (same length bucket).
    assert second.last_drift is False


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


def test_invalidate_single_url(tmp_path):
    cache = PageCache(tmp_path)
    cache.put_html("https://a.com/x", "x")
    cache.put_html("https://a.com/y", "y")
    assert cache.invalidate(url="https://a.com/x") == 1
    assert cache.get("https://a.com/x") is None
    assert cache.get("https://a.com/y") is not None


def test_invalidate_whole_domain(tmp_path):
    cache = PageCache(tmp_path)
    cache.put_html("https://a.com/x", "1")
    cache.put_html("https://a.com/y", "2")
    cache.put_html("https://b.com/z", "3")
    removed = cache.invalidate(domain="a.com")
    assert removed == 2
    assert cache.get("https://a.com/x") is None
    assert cache.get("https://a.com/y") is None
    assert cache.get("https://b.com/z") is not None


def test_invalidate_without_args_is_noop(tmp_path):
    """Refuse to wipe everything from a single API call."""
    cache = PageCache(tmp_path)
    cache.put_html("https://a.com/x", "1")
    assert cache.invalidate() == 0
    assert cache.get("https://a.com/x") is not None


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


def test_lru_eviction_when_over_budget(tmp_path):
    """Force a tiny budget and check the oldest row gets dropped."""
    cache = PageCache(tmp_path, ttl_sec=600, max_total_mb=0)  # 0 MB → eviction skipped
    cache.put_html("https://x.com/a", "A" * 100)
    assert cache.get("https://x.com/a") is not None  # not enforced

    # Now build a cache with a real but tiny budget. We use bytes-level
    # access by setting max_total_mb=0 then poking _max_bytes directly.
    cache2 = PageCache(tmp_path / "tight", ttl_sec=600, max_total_mb=10)
    cache2._max_bytes = 200  # bytes — accept only ~one row
    cache2.put_html("https://x.com/a", "A" * 150)
    time.sleep(0.05)  # ensure mtime ordering
    cache2.put_html("https://x.com/b", "B" * 150)
    # Total is 300 bytes > 200 → oldest (a) must have been dropped.
    assert cache2.get("https://x.com/a") is None
    assert cache2.get("https://x.com/b") is not None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def test_default_singleton_install_and_clear(tmp_path):
    assert get_default_page_cache() is None or True  # may have been set by prior test
    c = PageCache(tmp_path)
    set_default_page_cache(c)
    assert get_default_page_cache() is c
    set_default_page_cache(None)
    assert get_default_page_cache() is None


def test_build_default_from_minimal_config(tmp_path):
    """A config-shaped object with the minimum surface area should work."""
    from page_cache import build_default_page_cache

    class _Cfg:
        page_cache_enabled = True
        page_cache_root = tmp_path / "cache"
        page_cache_ttl_sec = 30
        page_cache_max_total_mb = 1
        page_cache_invalidate_on_fingerprint_mismatch = True

    cache = build_default_page_cache(_Cfg())
    try:
        assert cache is not None
        assert cache.enabled is True
        assert cache.root == tmp_path / "cache"
    finally:
        set_default_page_cache(None)


def test_build_default_disabled_returns_none(tmp_path):
    from page_cache import build_default_page_cache

    class _Cfg:
        page_cache_enabled = False

    cache = build_default_page_cache(_Cfg())
    try:
        assert cache is None
        assert get_default_page_cache() is None
    finally:
        set_default_page_cache(None)
