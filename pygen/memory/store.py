"""File-system facade over the persistent memory layout.

Layout
------
::

    <root>/
    +-- episode/
    |   +-- episodes.jsonl            # append-only committed log
    |   +-- pending/
    |       +-- <task_id>.json        # drafts awaiting user feedback
    +-- site/
    |   +-- <domain>.json             # machine-readable profile
    |   +-- <domain>.md               # human-readable profile
    |   +-- _quarantine/<domain>.*    # quarantined profiles (kept for audit)

Concurrency
-----------
The store is *single-process* friendly. We rely on POSIX-ish atomicity:

* Writes happen via *write-temp-then-rename* so a crash never leaves a
  half-written profile.
* Appends to ``episodes.jsonl`` use a single ``write`` call with a flush
  at the end, which is atomic for sub-page-size payloads on most
  filesystems and tolerable for our single-writer model.
* If we ever need multi-writer support, swap ``_atomic_write_json`` for
  a ``portalocker``-based version.

Self-healing
------------
Reading a corrupted JSON renames it to ``<file>.bak.<ts>`` and returns
an empty profile / ``None``. We never silently overwrite — the .bak file
gives us forensic evidence after the fact.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .episode import Episode, is_valid_task_id
from .site_profile import SiteProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_DOMAIN_RE = re.compile(r"^[a-z0-9.\-]+$")


def _safe_domain(domain: str) -> str:
    """Validate a domain string is filesystem-safe."""
    d = (domain or "").strip().lower()
    if not d or not _DOMAIN_RE.match(d):
        raise ValueError(f"unsafe domain key: {domain!r}")
    return d


def _safe_task_id(task_id: str) -> str:
    if not is_valid_task_id(task_id):
        raise ValueError(f"unsafe task_id: {task_id!r}")
    return task_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (write to .tmp, fsync, rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    _atomic_write_text(path, text)


def _backup_corrupt(path: Path, log) -> None:
    bak = path.with_suffix(path.suffix + f".bak.{_utc_ts()}")
    try:
        shutil.move(str(path), str(bak))
        if log:
            log(f"[MEMORY] backed up corrupt file: {path.name} -> {bak.name}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------


class MemoryStore:
    """Facade over ``<root>/`` for the persistent memory subsystem.

    Construct one per process; method calls are thread-safe via an
    internal lock for the small subset of mutations that compete for
    the same file (jsonl appends, profile writes).
    """

    # Layout convention (see module docstring): everything episode-related
    # lives under ``episode/`` (committed jsonl + pending drafts), everything
    # site-related under ``site/`` (one json + one md per domain, plus a
    # ``_quarantine`` subfolder for sealed-away profiles).
    EPISODES_FILE = "episode/episodes.jsonl"
    PENDING_DIR = "episode/pending"
    SITES_DIR = "site"
    QUARANTINE_DIR = "site/_quarantine"

    def __init__(
        self,
        root: Path,
        *,
        log_callback=None,
        max_episodes: int = 1000,
    ) -> None:
        self.root = Path(root)
        self.log = log_callback or (lambda _msg: None)
        self.max_episodes = max(1, int(max_episodes))
        self._lock = threading.RLock()
        self._site_cache: Dict[str, tuple] = {}  # domain -> (mtime, profile)

        # Best-effort directory creation. Failures here will surface on
        # first write rather than at import time.
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / self.PENDING_DIR).mkdir(parents=True, exist_ok=True)
            (self.root / self.SITES_DIR).mkdir(parents=True, exist_ok=True)
            (self.root / self.QUARANTINE_DIR).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.log(f"[MEMORY] failed to create memory dirs under {self.root}: {exc}")

    # ----------------------------------------------------------- pending
    def _pending_path(self, task_id: str) -> Path:
        return self.root / self.PENDING_DIR / f"{_safe_task_id(task_id)}.json"

    def write_draft(self, episode: Dict[str, Any]) -> Optional[Path]:
        """Write or overwrite a draft episode (committed=False)."""
        if not isinstance(episode, dict):
            return None
        task_id = str(episode.get("task_id") or "").strip()
        if not is_valid_task_id(task_id):
            self.log(f"[MEMORY] skip write_draft: invalid task_id {task_id!r}")
            return None
        episode = dict(episode)
        episode["committed"] = False
        path = self._pending_path(task_id)
        with self._lock:
            try:
                _atomic_write_json(path, episode)
                return path
            except Exception as exc:
                self.log(f"[MEMORY] write_draft failed for {task_id}: {exc}")
                return None

    def read_draft(self, task_id: str) -> Optional[Episode]:
        try:
            path = self._pending_path(task_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Episode.from_json(data) if isinstance(data, dict) else None
        except Exception as exc:
            self.log(f"[MEMORY] read_draft({task_id}) failed: {exc}")
            _backup_corrupt(path, self.log)
            return None

    def delete_draft(self, task_id: str) -> bool:
        try:
            path = self._pending_path(task_id)
        except ValueError:
            return False
        try:
            if path.exists():
                path.unlink()
                return True
        except Exception as exc:
            self.log(f"[MEMORY] delete_draft({task_id}) failed: {exc}")
        return False

    def gc_pending(self, *, older_than_days: int = 7) -> int:
        """Remove pending drafts whose mtime is older than the threshold.

        Returns the number of files removed. Failures per-file are
        swallowed so a single bad file can't block the GC.
        """
        if older_than_days <= 0:
            return 0
        cutoff = time.time() - (older_than_days * 86400)
        removed = 0
        pending_dir = self.root / self.PENDING_DIR
        if not pending_dir.exists():
            return 0
        for path in pending_dir.iterdir():
            if not path.is_file() or path.suffix != ".json":
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except Exception:
                continue
        if removed:
            self.log(f"[MEMORY] GC removed {removed} pending draft(s) older than {older_than_days}d")
        return removed

    # --------------------------------------------------------- committed
    def _episodes_path(self) -> Path:
        return self.root / self.EPISODES_FILE

    def append_committed_episode(self, episode: Dict[str, Any]) -> bool:
        """Append a committed (verdict-bearing) episode to the jsonl log.

        Also enforces ``max_episodes`` ring buffer: when the file grows
        past the cap we rewrite it with the most-recent ``max_episodes``
        rows. The rewrite uses atomic-temp-then-rename.
        """
        if not isinstance(episode, dict):
            return False
        episode = dict(episode)
        episode["committed"] = True
        if not episode.get("committed_at"):
            episode["committed_at"] = _now_iso()
        path = self._episodes_path()
        line = json.dumps(episode, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as exc:
                self.log(f"[MEMORY] append_committed_episode failed: {exc}")
                return False
            # Cheap line-count check; only rewrite when meaningfully over cap.
            try:
                rows = self._read_episodes_lines()
                if len(rows) > self.max_episodes * 1.1:
                    keep = rows[-self.max_episodes:]
                    text = "\n".join(keep) + "\n" if keep else ""
                    _atomic_write_text(path, text)
                    self.log(
                        f"[MEMORY] episode/episodes.jsonl ring-buffer trimmed to {self.max_episodes} rows"
                    )
            except Exception:
                pass
        return True

    def _read_episodes_lines(self) -> List[str]:
        path = self._episodes_path()
        if not path.exists():
            return []
        try:
            return [
                ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()
            ]
        except Exception as exc:
            self.log(f"[MEMORY] _read_episodes_lines failed: {exc}")
            return []

    def iter_committed_episodes(self) -> Iterator[Episode]:
        for raw in self._read_episodes_lines():
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    yield Episode.from_json(obj)
            except Exception:
                continue

    # ------------------------------------------------------------- sites
    def _site_json_path(self, domain: str) -> Path:
        return self.root / self.SITES_DIR / f"{_safe_domain(domain)}.json"

    def _site_md_path(self, domain: str) -> Path:
        return self.root / self.SITES_DIR / f"{_safe_domain(domain)}.md"

    def lookup_site(self, domain: str) -> Optional[SiteProfile]:
        """Return the cached site profile or load from disk.

        ``mtime``-based cache: re-loaded only when the file changed
        since last read. Returns ``None`` if no profile exists yet.
        """
        try:
            domain = _safe_domain(domain)
        except ValueError:
            return None
        path = self._site_json_path(domain)
        if not path.exists():
            return None
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        cached = self._site_cache.get(domain)
        if cached and cached[0] == mtime:
            # Defensive copy so callers can mutate freely without
            # poisoning the cache.
            return SiteProfile.from_json(dict(cached[1]))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.log(f"[MEMORY] lookup_site({domain}) corrupt: {exc}")
            _backup_corrupt(path, self.log)
            self._site_cache.pop(domain, None)
            return None
        if not isinstance(data, dict):
            return None
        profile = SiteProfile.from_json(data)
        self._site_cache[domain] = (mtime, dict(profile))
        return SiteProfile.from_json(dict(profile))

    def write_site(self, profile: Dict[str, Any], *, also_render_md: bool = True) -> bool:
        """Persist a site profile and (optionally) regenerate its .md companion."""
        try:
            domain = _safe_domain(str(profile.get("domain") or ""))
        except ValueError:
            return False
        path = self._site_json_path(domain)
        with self._lock:
            try:
                _atomic_write_json(path, profile)
            except Exception as exc:
                self.log(f"[MEMORY] write_site({domain}) failed: {exc}")
                return False
            self._site_cache.pop(domain, None)
            if also_render_md:
                try:
                    md = render_site_profile_markdown(profile)
                    _atomic_write_text(self._site_md_path(domain), md)
                except Exception as exc:
                    self.log(f"[MEMORY] render_site_profile_markdown({domain}) failed: {exc}")
            # Quarantine handling: if the profile is quarantined, also drop a
            # marker copy under the _quarantine subdir so an operator can
            # see at a glance which sites went hot.
            if profile.get("quarantined"):
                try:
                    qpath = self.root / self.QUARANTINE_DIR / f"{domain}.json"
                    _atomic_write_json(qpath, profile)
                except Exception:
                    pass
        return True

    # ----------------------------------------------------------- helpers
    def episodes_for_domain(self, domain: str, *, limit: int = 5) -> List[Episode]:
        """Return up to ``limit`` most-recent committed episodes for ``domain``."""
        try:
            target = _safe_domain(domain)
        except ValueError:
            return []
        out: List[Episode] = []
        for ep in reversed(list(self.iter_committed_episodes())):
            if str(ep.get("domain") or "").lower() == target:
                out.append(ep)
                if len(out) >= limit:
                    break
        return out


# ---------------------------------------------------------------------------
# Markdown rendering for the human-readable companion file
# ---------------------------------------------------------------------------


def render_site_profile_markdown(profile: Dict[str, Any]) -> str:
    """Render ``site/<domain>.md`` from the JSON profile (for humans)."""
    if not isinstance(profile, dict):
        return ""
    domain = profile.get("domain", "(unknown)")
    lines = [
        f"# Site profile: `{domain}`",
        "",
        f"- version: {profile.get('version', 0)}",
        f"- first seen: {profile.get('first_seen_at', '?')}",
        f"- last updated: {profile.get('last_updated_at', '?')}",
        f"- last success: {profile.get('last_success_at') or '-'}",
        f"- last failure: {profile.get('last_failure_at') or '-'}",
        f"- wins / losses: {profile.get('wins', 0)} / {profile.get('losses', 0)}",
        f"- consecutive failures: {profile.get('consecutive_failures', 0)}",
        f"- quarantined: **{profile.get('quarantined', False)}**",
        f"- has drift (HTML fingerprint): {profile.get('has_drift', False)}",
        f"- confidence: {profile.get('confidence', 0):.3f}",
        "",
    ]
    stable = profile.get("stable_selectors") or {}
    if stable:
        lines.append("## Stable selectors (≥3 user-confirmed wins)")
        lines.append("")
        for slot, info in sorted(stable.items()):
            sel = info.get("selector", "")
            wr = info.get("winrate", 0)
            lines.append(f"- `{slot}` -> `{sel}`  (winrate {wr:.2f}, wins {info.get('wins', 0)})")
        lines.append("")
    traits = profile.get("site_traits") or {}
    if traits:
        lines.append("## Site traits (LLM-derived)")
        lines.append("")
        for k, v in sorted(traits.items()):
            lines.append(f"- **{k}**: {v}")
        lines.append("")
    pitfalls = profile.get("known_pitfalls") or []
    if pitfalls:
        lines.append("## Known pitfalls")
        lines.append("")
        for p in pitfalls:
            lines.append(f"- {p}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["MemoryStore", "render_site_profile_markdown"]
