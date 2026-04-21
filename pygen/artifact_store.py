"""
Artifact storage for large tool payloads to keep LLM context compact.

Layout (v2):

    {root_dir}/
        _global/                 # task_id missing -> fallback bucket
            page_html_xxx.txt
        <task_id>/               # one directory per task
            page_html_xxx.json
            ...

Highlights vs v1:
    * ``put_with_summary``: writes the full payload to disk and stores a
      compact rule-based ``summary`` dict in the returned ``ArtifactRef``
      so the LLM gets candidate signals (not raw HTML) in the message.
    * ``read(artifact_id, scope)``: lets the planner LLM lazily fetch a
      slice of the original artifact via the new ``read_artifact`` tool.
    * ``cleanup_expired``: TTL-based GC over the entire ``root_dir``.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional


_GLOBAL_BUCKET = "_global"
_DEFAULT_READ_LIMIT = 8000


@dataclass
class ArtifactRef:
    artifact_id: str
    path: str
    media_type: str
    size_bytes: int
    preview: str
    summary: Optional[Dict[str, Any]] = None
    task_id: Optional[str] = None
    fallback_hints: list = field(default_factory=list)

    def to_prompt_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "preview": self.preview,
        }
        if self.summary is not None:
            out["summary"] = self.summary
        if self.fallback_hints:
            out["fallback_hints"] = self.fallback_hints
        # path/task_id intentionally omitted from the prompt to avoid
        # leaking absolute filesystem paths to the LLM. They remain
        # accessible on the dataclass for internal use.
        return out


class ArtifactStore:
    """File-based artifact store with per-task subdirectories and TTL GC."""

    def __init__(
        self,
        root_dir: str | Path,
        max_preview_chars: int = 300,
        per_task_subdir: bool = True,
    ):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.max_preview_chars = max_preview_chars
        self.per_task_subdir = per_task_subdir

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _bucket_dir(self, task_id: Optional[str]) -> Path:
        if not self.per_task_subdir:
            return self.root_dir
        bucket = (task_id or "").strip() or _GLOBAL_BUCKET
        bucket = re.sub(r"[^A-Za-z0-9_\-]", "_", bucket)[:80] or _GLOBAL_BUCKET
        d = self.root_dir / bucket
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _build_path(
        self,
        prefix: str,
        suffix: str,
        task_id: Optional[str] = None,
    ) -> tuple[str, Path]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        artifact_id = f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}"
        filename = f"{artifact_id}{suffix}"
        return artifact_id, self._bucket_dir(task_id) / filename

    def _locate(self, artifact_id: str) -> Optional[Path]:
        """Find an artifact file across all task buckets."""
        # Try root_dir first (legacy / per_task_subdir=False case).
        candidate = next(self.root_dir.glob(f"{artifact_id}.*"), None)
        if candidate and candidate.exists():
            return candidate
        # Then scan per-task buckets.
        for sub in self.root_dir.iterdir():
            if not sub.is_dir():
                continue
            candidate = next(sub.glob(f"{artifact_id}.*"), None)
            if candidate and candidate.exists():
                return candidate
        return None

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------

    def put_text(
        self,
        content: str,
        prefix: str = "text",
        task_id: Optional[str] = None,
    ) -> ArtifactRef:
        artifact_id, path = self._build_path(prefix, ".txt", task_id=task_id)
        path.write_text(content, encoding="utf-8")
        return ArtifactRef(
            artifact_id=artifact_id,
            path=str(path),
            media_type="text/plain",
            size_bytes=path.stat().st_size,
            preview=content[: self.max_preview_chars],
            task_id=task_id,
        )

    def put_json(
        self,
        data: Any,
        prefix: str = "json",
        task_id: Optional[str] = None,
    ) -> ArtifactRef:
        artifact_id, path = self._build_path(prefix, ".json", task_id=task_id)
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        path.write_text(text, encoding="utf-8")
        return ArtifactRef(
            artifact_id=artifact_id,
            path=str(path),
            media_type="application/json",
            size_bytes=path.stat().st_size,
            preview=text[: self.max_preview_chars],
            task_id=task_id,
        )

    def put_bytes(
        self,
        blob: bytes,
        prefix: str = "blob",
        suffix: str = ".bin",
        media_type: str = "application/octet-stream",
        task_id: Optional[str] = None,
    ) -> ArtifactRef:
        artifact_id, path = self._build_path(prefix, suffix, task_id=task_id)
        path.write_bytes(blob)
        return ArtifactRef(
            artifact_id=artifact_id,
            path=str(path),
            media_type=media_type,
            size_bytes=path.stat().st_size,
            preview=f"{len(blob)} bytes",
            task_id=task_id,
        )

    def put_with_summary(
        self,
        content: Any,
        *,
        prefix: str,
        summarizer: Optional[Callable[[Any], Dict[str, Any]]] = None,
        task_id: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> ArtifactRef:
        """Persist ``content`` and attach a rule-based summary dict.

        - If ``content`` is ``str`` -> stored as ``.txt``.
        - Otherwise -> serialized as ``.json``.
        - When ``summarizer`` is None, behavior is identical to ``put_text`` /
          ``put_json`` (no ``summary`` is attached).
        """
        if isinstance(content, str):
            ref = self.put_text(content, prefix=prefix, task_id=task_id)
        else:
            ref = self.put_json(content, prefix=prefix, task_id=task_id)
            if media_type:
                ref.media_type = media_type

        if summarizer is None:
            return ref

        try:
            summary = summarizer(content) or {}
            if not isinstance(summary, dict):
                summary = {"_invalid_summary_type": type(summary).__name__}
        except Exception as exc:  # never let a summarizer break a write
            summary = {"_summary_error": f"{type(exc).__name__}: {exc}"}

        ref.summary = summary
        # The summarizer may suggest pre-built ``read_artifact`` invocations.
        hints = summary.get("fallback_hints") if isinstance(summary, dict) else None
        if isinstance(hints, list):
            ref.fallback_hints = [str(h) for h in hints if h]
        return ref

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    def read_text(self, artifact_id: str) -> Optional[str]:
        """Backward-compatible full-text read (legacy callers)."""
        candidate = self._locate(artifact_id)
        if not candidate:
            return None
        return candidate.read_text(encoding="utf-8", errors="replace")

    def read(
        self,
        artifact_id: str,
        scope: Optional[str] = None,
        max_chars: int = _DEFAULT_READ_LIMIT,
    ) -> Optional[str]:
        """Read an artifact with an optional ``scope`` slice.

        ``scope`` accepted forms:
            * ``None`` / ``""`` -> full content (capped to ``max_chars``)
            * ``"head:N"``      -> first N characters
            * ``"tail:N"``      -> last N characters
            * ``"css:<sel>"``   -> HTML only: outerHTML of all matches
                                  (joined, capped to ``max_chars``)
            * ``"jsonpath:<p>"`` / ``"json:<p>"`` -> JSON only: dotted path
                                  walk (e.g. ``$.api_requests[0].url``)
        Returns ``None`` if the artifact is not found.
        """
        candidate = self._locate(artifact_id)
        if not candidate:
            return None

        try:
            raw = candidate.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"<read error: {exc}>"

        scope = (scope or "").strip()
        if not scope:
            return _truncate(raw, max_chars)

        # head:N / tail:N
        m = re.match(r"^(head|tail)\s*:\s*(\d+)\s*$", scope, re.IGNORECASE)
        if m:
            n = int(m.group(2))
            if m.group(1).lower() == "head":
                return raw[:n]
            return raw[-n:]

        # css:<selector> -> only meaningful for HTML artifacts
        if scope.lower().startswith("css:"):
            selector = scope[len("css:"):].strip()
            return _select_html(raw, selector, max_chars)

        # jsonpath:<path> -> only meaningful for JSON artifacts
        if scope.lower().startswith(("jsonpath:", "json:")):
            path = scope.split(":", 1)[1].strip()
            return _select_jsonpath(raw, path, max_chars)

        return f"<unknown scope: {scope!r}; supported: head:N, tail:N, css:<sel>, jsonpath:<path>>"

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def cleanup_expired(self, ttl_seconds: int) -> int:
        """Delete files older than ``ttl_seconds`` (mtime). Returns count.

        Empty per-task subdirectories are removed afterwards. Never raises.
        """
        if ttl_seconds <= 0:
            return 0
        cutoff = time.time() - ttl_seconds
        removed = 0
        try:
            for sub in self.root_dir.iterdir():
                if sub.is_file():
                    if _safe_mtime(sub) < cutoff:
                        _safe_unlink(sub)
                        removed += 1
                elif sub.is_dir():
                    for f in sub.iterdir():
                        if f.is_file() and _safe_mtime(f) < cutoff:
                            _safe_unlink(f)
                            removed += 1
                    # remove now-empty bucket
                    try:
                        if not any(sub.iterdir()):
                            sub.rmdir()
                    except OSError:
                        pass
        except Exception:
            pass
        return removed


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated, total {len(text)} chars; narrow scope to see more)"


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return time.time()


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _select_html(raw: str, selector: str, limit: int) -> str:
    """Best-effort CSS selection. May receive a JSON-wrapped HTML payload."""
    try:
        from bs4 import BeautifulSoup  # local import keeps startup light
    except Exception as exc:
        return f"<bs4 unavailable: {exc}>"

    html = _maybe_unwrap_html(raw)
    try:
        soup = BeautifulSoup(html, "html.parser")
        matches = soup.select(selector)
    except Exception as exc:
        return f"<css select error: {exc}>"

    if not matches:
        return f"<no matches for selector {selector!r}>"

    parts = [str(m) for m in matches[:50]]
    joined = "\n\n".join(parts)
    return _truncate(joined, limit) + f"\n[matched {len(matches)} elements; showing up to 50]"


def _maybe_unwrap_html(raw: str) -> str:
    """If the artifact was stored as JSON ``{"html": "..."}``, unwrap it."""
    s = raw.lstrip()
    if not s.startswith("{"):
        return raw
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    if isinstance(obj, dict):
        for key in ("html", "page_html", "content", "body"):
            v = obj.get(key)
            if isinstance(v, str):
                return v
    return raw


def _select_jsonpath(raw: str, path: str, limit: int) -> str:
    try:
        obj = json.loads(raw)
    except Exception as exc:
        return f"<not valid json: {exc}>"

    # Strip leading $/. and split on . / [N]
    p = path.lstrip("$").lstrip(".").strip()
    if not p:
        return _truncate(json.dumps(obj, ensure_ascii=False, indent=2, default=str), limit)

    cur: Any = obj
    tokens = re.findall(r"[^.\[\]]+|\[\d+\]", p)
    for tok in tokens:
        try:
            if tok.startswith("[") and tok.endswith("]"):
                idx = int(tok[1:-1])
                cur = cur[idx]
            elif isinstance(cur, dict):
                cur = cur[tok]
            elif isinstance(cur, list):
                cur = cur[int(tok)]
            else:
                return f"<cannot traverse {tok!r} on {type(cur).__name__}>"
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            return f"<path error at {tok!r}: {exc}>"

    if isinstance(cur, str):
        return _truncate(cur, limit)
    try:
        return _truncate(json.dumps(cur, ensure_ascii=False, indent=2, default=str), limit)
    except Exception:
        return _truncate(str(cur), limit)
