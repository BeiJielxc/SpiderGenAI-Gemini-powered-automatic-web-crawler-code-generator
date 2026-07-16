"""File-only golden crawler registry.

The Python file is the sole executable asset. A canonical task signature
selects it, and its parent directory expresses lifecycle state:

``pending/`` -> waiting for user confirmation
``active/``  -> reusable without an LLM
``invalid/`` -> retained for audit but never executed
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


RUNTIME_CONTRACT_VERSION = 2
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_SIGNATURE = re.compile(r"^[0-9a-f]{64}$")
_LOCK = threading.RLock()


def _text(value: Any) -> str:
    value = "" if value is None else str(value)
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").split("\n")).strip()


def _normalized_url(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc = f"{host}:{port}"
        else:
            netloc = host
        path = parsed.path or "/"
        if path != "/":
            path = path.rstrip("/")
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        return urlunsplit((scheme, netloc, path, query, ""))
    except Exception:
        return raw


def _attachment_fingerprint(item: Any) -> Dict[str, str]:
    data = dict(item or {}) if isinstance(item, Mapping) else {}
    encoded = str(data.get("base64") or "")
    try:
        payload = base64.b64decode(encoded, validate=False)
    except Exception:
        payload = encoded.encode("utf-8", errors="ignore")
    return {
        "filename": _text(data.get("filename")),
        "mimeType": _text(data.get("mimeType") or data.get("mime_type")),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def canonical_task_config(request: Any) -> Dict[str, Any]:
    """Return only request fields that influence crawler behaviour."""
    if hasattr(request, "model_dump"):
        request = request.model_dump()
    data = dict(request or {}) if isinstance(request, Mapping) else {}
    selected_paths = sorted({_text(item) for item in (data.get("selectedPaths") or []) if _text(item)})
    attachments = [_attachment_fingerprint(item) for item in (data.get("attachments") or [])]
    attachments.sort(key=lambda item: (item["filename"], item["mimeType"], item["sha256"]))
    return {
        "runtimeContractVersion": RUNTIME_CONTRACT_VERSION,
        "url": _normalized_url(data.get("url")),
        "startDate": _text(data.get("startDate")),
        "endDate": _text(data.get("endDate")),
        "taskObjective": _text(data.get("taskObjective")),
        "extraRequirements": _text(data.get("extraRequirements")),
        "siteName": _text(data.get("siteName")),
        "listPageName": _text(data.get("listPageName")),
        "sourceCredibility": _text(data.get("sourceCredibility")),
        "runMode": _text(data.get("runMode")),
        "crawlMode": _text(data.get("crawlMode") or "agent"),
        "downloadReport": _text(data.get("downloadReport") or "yes"),
        "selectedPaths": selected_paths,
        "attachments": attachments,
    }


def compute_task_signature(request: Any) -> str:
    payload = json.dumps(
        canonical_task_config(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class GoldenCrawler:
    signature: str
    path: Path
    code: str


class GoldenCrawlerStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.pending_dir = self.root / "pending"
        self.active_dir = self.root / "active"
        self.invalid_dir = self.root / "invalid"
        for path in (self.pending_dir, self.active_dir, self.invalid_dir):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_signature(signature: str) -> str:
        signature = str(signature or "").lower()
        if not _SAFE_SIGNATURE.fullmatch(signature):
            raise ValueError(f"Unsafe task signature: {signature!r}")
        return signature

    @staticmethod
    def _validate_task_id(task_id: str) -> str:
        task_id = str(task_id or "")
        if not _SAFE_ID.fullmatch(task_id):
            raise ValueError(f"Unsafe task id: {task_id!r}")
        return task_id

    def active_path(self, signature: str) -> Path:
        signature = self._validate_signature(signature)
        return self.active_dir / f"{signature}.py"

    def pending_path(self, task_id: str, signature: str) -> Path:
        task_id = self._validate_task_id(task_id)
        signature = self._validate_signature(signature)
        return self.pending_dir / f"{task_id}--{signature}.py"

    def load_active(self, signature: str) -> Optional[GoldenCrawler]:
        path = self.active_path(signature)
        if not path.is_file():
            return None
        try:
            code = path.read_text(encoding="utf-8")
        except Exception:
            return None
        if not code.strip():
            return None
        return GoldenCrawler(signature=signature, path=path, code=code)

    def stage_pending(self, *, task_id: str, signature: str, code: str) -> Path:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("Golden crawler code must be non-empty")
        path = self.pending_path(task_id, signature)
        temp = path.with_suffix(".tmp")
        with _LOCK:
            temp.write_text(code, encoding="utf-8")
            os.replace(temp, path)
        return path

    def activate(self, *, task_id: str, signature: str) -> Optional[Path]:
        pending = self.pending_path(task_id, signature)
        active = self.active_path(signature)
        with _LOCK:
            if not pending.is_file():
                return active if active.is_file() else None
            if active.is_file():
                self._move_to_invalid(active, signature=signature, task_id=task_id, label="replaced")
            os.replace(pending, active)
        return active

    def reject_pending(self, *, task_id: str, signature: str) -> Optional[Path]:
        pending = self.pending_path(task_id, signature)
        with _LOCK:
            if not pending.is_file():
                return None
            return self._move_to_invalid(
                pending, signature=signature, task_id=task_id, label="rejected"
            )

    def invalidate_active(self, *, signature: str, task_id: str) -> Optional[Path]:
        active = self.active_path(signature)
        self._validate_task_id(task_id)
        with _LOCK:
            if not active.is_file():
                return None
            return self._move_to_invalid(
                active, signature=signature, task_id=task_id, label="invalid"
            )

    def _move_to_invalid(
        self,
        source: Path,
        *,
        signature: str,
        task_id: str,
        label: str,
    ) -> Path:
        signature = self._validate_signature(signature)
        task_id = self._validate_task_id(task_id)
        target_dir = self.invalid_dir / signature
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = target_dir / f"{stamp}--{label}--{task_id}.py"
        os.replace(source, target)
        return target


__all__ = [
    "RUNTIME_CONTRACT_VERSION",
    "GoldenCrawler",
    "GoldenCrawlerStore",
    "canonical_task_config",
    "compute_task_signature",
]
