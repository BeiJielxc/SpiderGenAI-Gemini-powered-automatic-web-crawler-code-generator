"""URL-keyed full-HTML page cache.

A simple write-through cache that lets the rerun-validator (and the
``tool_get_page_html`` tool) reuse the HTML of a page that was already
fetched in a prior run, without spinning up the browser again.

Design (deliberately minimalist — see plan "极简版"):

* Single store, single content-type (full HTML). No structure-vs-content
  split, no per-page-type TTL knobs. Disk is cheap; correctness is hard.
* **URL is the key.** ``open_page("/news/?p=2")`` and
  ``open_page("/news/?p=3")`` are different cache rows. We sha256 the
  URL and use the first 16 hex chars as the on-disk filename so we don't
  trip on filesystem-illegal characters (``?``, ``=``, ``&``, ``:``).
* **Storage layout:** ``<cache_root>/<domain>/<urlhash>.html`` (the
  HTML payload) and ``<cache_root>/<domain>/<urlhash>.meta.json`` (a
  small JSON sidecar with url, fetched_at, html_fingerprint, byte_size).
  Splitting metadata out keeps reads cheap when we just need to check
  "is this fresh enough?" without having to read megabytes of HTML.
* **Freshness:** TTL (default 24h). Past TTL → cache miss. We do NOT
  re-fetch automatically; the caller's normal "open page" path runs and
  then *write_through* refreshes the row.
* **Drift detection:** when a write happens and we *had* a previous
  row for the same URL, we compare ``html_fingerprint``. A mismatch
  means the page structure shifted — we still store the new row, but
  the metadata flips ``last_drift=True`` so callers (e.g. the
  rerun-validator) can refuse to trust pre-validation results.
* **Capacity management:** TTL prunes old rows on every ``put``. A
  cheap LRU bound (``max_total_mb``) is enforced after that — oldest
  ``mtime`` first. Both walks are O(domain-rows), which is tiny for
  realistic crawler workloads.

This module deliberately has **no async / browser / network code**.
It is a pure filesystem KV store + a fingerprint helper. All
browser-side wiring lives in ``pygen/tools.py`` (which calls
``put_html`` after a successful navigation/HTML capture).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

try:
    from memory.fingerprint import compute_list_page_fingerprint
except ImportError:  # pragma: no cover
    from .memory.fingerprint import compute_list_page_fingerprint  # type: ignore


_log = logging.getLogger(__name__)


# Filenames look like 0123abcd9876ef01.html (16 hex of sha256(url)).
_HASH_LEN = 16


# ---------------------------------------------------------------------------
# URL → on-disk key helpers
# ---------------------------------------------------------------------------


def _safe_domain(url: str) -> str:
    """Return a filesystem-safe domain folder name for ``url``.

    Falls back to ``"_unknown"`` if the URL has no parseable host (so
    we never write into the cache root directly, which would make
    domain-scoped invalidation impossible).
    """
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:
        host = ""
    if not host:
        return "_unknown"
    # Strip port, keep dots/dashes (valid on every fs we target).
    host = host.split(":", 1)[0]
    # Defensive: replace any character that is NOT one of the safe set.
    return re.sub(r"[^a-z0-9.\-_]", "_", host) or "_unknown"


def _url_hash(url: str) -> str:
    """sha256(url)[:_HASH_LEN] — short enough for filenames, long enough
    for collision-free for any realistic single-domain working set."""
    h = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()
    return h[:_HASH_LEN]


# ---------------------------------------------------------------------------
# Public dataclass surfaced to callers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheEntry:
    """A single read-out row from the page cache.

    ``html`` is the FULL captured HTML payload (we deliberately don't
    truncate at this layer — the caller is free to do its own
    summarisation via ``ArtifactStore`` after reading).
    """

    url: str
    html: str
    fetched_at: float
    html_fingerprint: str
    byte_size: int
    age_sec: float
    last_drift: bool


# ---------------------------------------------------------------------------
# PageCache
# ---------------------------------------------------------------------------


class PageCache:
    """URL-keyed full-HTML cache, on the local filesystem.

    Thread-safe (a single ``threading.Lock`` serialises put/get/delete).
    The lock granularity is "the whole cache", which is fine because
    every operation is a tiny number of file I/Os.

    Disabled instances (``enabled=False``) become silent no-ops: ``get``
    returns ``None`` and ``put_html`` is a successful no-op. This lets
    callers wire the cache unconditionally and let config decide whether
    it actually does anything.
    """

    def __init__(
        self,
        root: Path,
        *,
        ttl_sec: int = 86400,
        max_total_mb: int = 500,
        invalidate_on_fingerprint_mismatch: bool = True,
        enabled: bool = True,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._root = Path(root)
        self._ttl_sec = max(0, int(ttl_sec))
        self._max_bytes = max(0, int(max_total_mb)) * 1024 * 1024
        self._invalidate_on_drift = bool(invalidate_on_fingerprint_mismatch)
        self._enabled = bool(enabled)
        self._log = log_callback or (lambda _msg: None)
        self._lock = threading.Lock()
        if self._enabled:
            try:
                self._root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # If we can't create the root we silently degrade to disabled.
                # Crawler must keep running even if the user pointed cache at
                # a read-only path.
                _log.warning("PageCache: cannot create root %s: %s", self._root, exc)
                self._enabled = False

    # ---- public API -------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def root(self) -> Path:
        return self._root

    def get(self, url: str) -> Optional[CacheEntry]:
        """Return the cached entry for ``url`` if one exists AND is fresh
        (within TTL). Returns ``None`` on miss / stale / disabled.

        We **do not** delete stale rows here — that's done lazily by
        the next ``put_html`` (cheaper to batch).
        """
        if not self._enabled or not url:
            return None
        with self._lock:
            paths = self._paths_for(url)
            return self._read_entry(url, paths)

    def put_html(self, url: str, html: str) -> Optional[CacheEntry]:
        """Write ``html`` to the cache row for ``url`` (write-through).

        Returns the freshly written ``CacheEntry`` so callers that just
        captured HTML can hand it to anything that expects a CacheEntry
        without an extra disk read. Returns ``None`` on failure or when
        the cache is disabled.
        """
        if not self._enabled or not url or not isinstance(html, str):
            return None
        try:
            with self._lock:
                paths = self._paths_for(url)
                # Detect drift vs. previous row (still on disk and still
                # within TTL — past-TTL rows are treated as "no prior").
                prior = self._read_entry(url, paths) if paths.html.exists() else None
                fp = compute_list_page_fingerprint(html)
                drift = bool(
                    prior is not None
                    and self._invalidate_on_drift
                    and prior.html_fingerprint != fp
                )

                paths.parent.mkdir(parents=True, exist_ok=True)
                # Write HTML first then meta — if we crash between, the meta
                # is just stale and the next get() returns None (safer than
                # the inverse).
                paths.html.write_text(html, encoding="utf-8", errors="ignore")
                meta = {
                    "url": url,
                    "fetched_at": time.time(),
                    "html_fingerprint": fp,
                    "byte_size": len(html.encode("utf-8", errors="ignore")),
                    "last_drift": drift,
                }
                paths.meta.write_text(
                    json.dumps(meta, ensure_ascii=False), encoding="utf-8"
                )

                self._evict_stale_locked()
                self._evict_lru_locked()

                if drift:
                    self._log(
                        f"[PAGE_CACHE] drift detected for {_short(url)} — fingerprint changed"
                    )
                else:
                    self._log(
                        f"[PAGE_CACHE] cached {_short(url)} ({meta['byte_size']:,} bytes)"
                    )
                return CacheEntry(
                    url=url,
                    html=html,
                    fetched_at=meta["fetched_at"],
                    html_fingerprint=fp,
                    byte_size=meta["byte_size"],
                    age_sec=0.0,
                    last_drift=drift,
                )
        except Exception as exc:
            _log.warning("PageCache.put_html failed for %s: %s", url, exc)
            return None

    def invalidate(self, *, url: Optional[str] = None, domain: Optional[str] = None) -> int:
        """Best-effort delete of cache rows.

        * ``url`` set → drop just that row.
        * ``domain`` set (or ``url`` carries a parseable host) → drop
          every row under that domain folder.
        * Both unset → no-op (we refuse to nuke the whole cache from a
          single API call).

        Returns the number of HTML files removed.
        """
        if not self._enabled:
            return 0
        with self._lock:
            removed = 0
            try:
                if url:
                    paths = self._paths_for(url)
                    if paths.html.exists():
                        paths.html.unlink()
                        removed += 1
                    if paths.meta.exists():
                        paths.meta.unlink()
                if domain:
                    safe = _safe_domain(f"http://{domain}")
                    folder = self._root / safe
                    if folder.exists():
                        for f in folder.glob("*.html"):
                            f.unlink(missing_ok=True)
                            removed += 1
                        for f in folder.glob("*.meta.json"):
                            f.unlink(missing_ok=True)
            except Exception as exc:
                _log.warning("PageCache.invalidate failed: %s", exc)
            if removed:
                self._log(
                    f"[PAGE_CACHE] invalidated {removed} row(s) "
                    f"(url={_short(url) if url else '-'}, domain={domain or '-'})"
                )
            return removed

    # ---- internals --------------------------------------------------

    def _paths_for(self, url: str) -> "_RowPaths":
        domain = _safe_domain(url)
        h = _url_hash(url)
        folder = self._root / domain
        return _RowPaths(
            parent=folder,
            html=folder / f"{h}.html",
            meta=folder / f"{h}.meta.json",
        )

    def _read_entry(self, url: str, paths: "_RowPaths") -> Optional[CacheEntry]:
        """Internal — read a CacheEntry honoring TTL. Caller holds the
        lock.

        Returns ``None`` if any of html/meta is missing, JSON is
        corrupt, or the row is older than ``ttl_sec``.
        """
        if not paths.html.exists() or not paths.meta.exists():
            return None
        try:
            meta = json.loads(paths.meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        fetched_at = float(meta.get("fetched_at") or 0.0)
        age = max(0.0, time.time() - fetched_at)
        if self._ttl_sec and age > self._ttl_sec:
            return None
        try:
            html = paths.html.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        return CacheEntry(
            url=url,
            html=html,
            fetched_at=fetched_at,
            html_fingerprint=str(meta.get("html_fingerprint") or ""),
            byte_size=int(meta.get("byte_size") or 0),
            age_sec=age,
            last_drift=bool(meta.get("last_drift")),
        )

    def _evict_stale_locked(self) -> None:
        """Drop rows past TTL. Caller holds the lock."""
        if not self._ttl_sec:
            return
        cutoff = time.time() - self._ttl_sec
        try:
            for meta_path in self._root.rglob("*.meta.json"):
                try:
                    if meta_path.stat().st_mtime < cutoff:
                        html_path = meta_path.with_suffix("").with_suffix(".html")
                        meta_path.unlink(missing_ok=True)
                        html_path.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            return

    def _evict_lru_locked(self) -> None:
        """If total HTML payload exceeds ``max_total_mb``, drop oldest
        rows by mtime until we're back under budget. Caller holds the
        lock.
        """
        if not self._max_bytes:
            return
        try:
            rows: List[Dict[str, Any]] = []
            total = 0
            for html_path in self._root.rglob("*.html"):
                try:
                    st = html_path.stat()
                except OSError:
                    continue
                rows.append({"path": html_path, "size": st.st_size, "mtime": st.st_mtime})
                total += st.st_size
            if total <= self._max_bytes:
                return
            rows.sort(key=lambda r: r["mtime"])  # oldest first
            for row in rows:
                if total <= self._max_bytes:
                    break
                p: Path = row["path"]
                meta_p = p.with_suffix("").with_suffix(".meta.json")
                try:
                    p.unlink(missing_ok=True)
                    meta_p.unlink(missing_ok=True)
                    total -= row["size"]
                except OSError:
                    continue
        except OSError:
            return


@dataclass(frozen=True)
class _RowPaths:
    parent: Path
    html: Path
    meta: Path


def _short(url: Optional[str]) -> str:
    """One-line truncated URL for log lines."""
    if not url:
        return "-"
    return url if len(url) <= 80 else url[:77] + "..."


# ---------------------------------------------------------------------------
# Module-level singleton (lazily configured by api.py / runner.py via
# build_default_page_cache). Default: disabled until configured, so
# unit tests that don't care about the cache behave as before.
# ---------------------------------------------------------------------------


_DEFAULT_CACHE: Optional[PageCache] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_page_cache() -> Optional[PageCache]:
    """Return the process-wide default PageCache, or ``None`` if it
    hasn't been configured yet (or was explicitly disabled)."""
    return _DEFAULT_CACHE


def set_default_page_cache(cache: Optional[PageCache]) -> None:
    """Install (or clear) the process-wide default PageCache."""
    global _DEFAULT_CACHE
    with _DEFAULT_LOCK:
        _DEFAULT_CACHE = cache


def build_default_page_cache(config: Any, *, log_callback: Optional[Callable[[str], None]] = None) -> Optional[PageCache]:
    """Construct a PageCache from a ``Config`` instance.

    Reads ``config.page_cache_enabled`` / ``page_cache_root`` /
    ``page_cache_ttl_sec`` / ``page_cache_max_total_mb`` /
    ``page_cache_invalidate_on_fingerprint_mismatch``. Missing fields
    get sensible defaults so this still works against a stripped-down
    test config.

    Returns ``None`` (and does NOT mutate the singleton) when
    ``page_cache_enabled`` is false.
    """
    enabled = bool(getattr(config, "page_cache_enabled", True))
    if not enabled:
        set_default_page_cache(None)
        return None
    root = getattr(config, "page_cache_root", None)
    if not root:
        # Fall back to <pygen>/output/page_cache/.
        root = Path(__file__).parent / "output" / "page_cache"
    cache = PageCache(
        Path(root),
        ttl_sec=int(getattr(config, "page_cache_ttl_sec", 86400) or 86400),
        max_total_mb=int(getattr(config, "page_cache_max_total_mb", 500) or 500),
        invalidate_on_fingerprint_mismatch=bool(
            getattr(config, "page_cache_invalidate_on_fingerprint_mismatch", True)
        ),
        enabled=True,
        log_callback=log_callback,
    )
    set_default_page_cache(cache)
    return cache


__all__ = [
    "PageCache",
    "CacheEntry",
    "get_default_page_cache",
    "set_default_page_cache",
    "build_default_page_cache",
]
