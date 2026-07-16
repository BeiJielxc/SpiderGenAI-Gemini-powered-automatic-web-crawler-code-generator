"""Normalize file attachments carried by news detail pages.

The article body and its attachments are independent outputs.  This module
never rewrites ``content``; it only derives a deduplicated ``attachments``
list from explicit crawler fields and links embedded in the existing body.
That keeps older approved crawlers useful while new crawlers adopt the
first-class attachment contract.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag


FILE_TYPES = {
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "docx",
    ".xls": "xls",
    ".xlsx": "xlsx",
    ".csv": "csv",
    ".zip": "zip",
    ".rar": "rar",
    ".7z": "7z",
}
_FILE_LABEL_RE = re.compile(
    r"\b(pdf|download|attachment|document|file|report|circular|notice)\b|"
    r"下载|附件|文件|报告|公告",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_QUOTED_URL_RE = re.compile(r"['\"](https?://[^'\"]+|/[^'\"]+)['\"]", re.IGNORECASE)
_NOISE_RE = re.compile(
    r"nav|footer|header|menu|sidebar|breadcrumb|cookie|social|share|related",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _canonical_url(value: Any, base_url: str = "") -> str:
    raw = _text(value).rstrip(".,);]")
    if not raw or raw.startswith(("javascript:", "mailto:", "tel:", "#")):
        return ""
    absolute = urljoin(base_url, raw)
    try:
        parsed = urlsplit(absolute)
    except Exception:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def infer_file_type(url: str, *, mime_type: str = "", label: str = "") -> str:
    mime = _text(mime_type).lower()
    if "application/pdf" in mime or re.search(r"\bpdf\b", _text(label), re.IGNORECASE):
        return "pdf"
    try:
        path = unquote(urlsplit(url).path).lower()
    except Exception:
        path = url.lower()
    for suffix, file_type in FILE_TYPES.items():
        if path.endswith(suffix):
            return file_type
    return "file"


def _default_name(url: str, file_type: str, index: int) -> str:
    try:
        filename = unquote(Path(urlsplit(url).path).name)
    except Exception:
        filename = ""
    if filename:
        for suffix in FILE_TYPES:
            if filename.lower().endswith(suffix):
                filename = filename[: -len(suffix)]
                break
    return filename.strip() or f"Attachment {index}"


def _in_noise_region(tag: Tag) -> bool:
    current: Optional[Tag] = tag
    for _ in range(6):
        if current is None:
            break
        if current.name in {"nav", "footer", "header", "aside"}:
            return True
        marker = " ".join(current.get("class", []) or []) + " " + _text(current.get("id"))
        if _NOISE_RE.search(marker):
            return True
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return False


def _tag_url_candidates(tag: Tag) -> Iterable[str]:
    for attr in ("href", "data", "src", "data-href", "data-url", "data-download-url"):
        value = tag.get(attr)
        if value:
            yield _text(value)
    onclick = _text(tag.get("onclick"))
    if onclick:
        for match in _QUOTED_URL_RE.findall(onclick):
            yield match


def _tag_is_attachment(tag: Tag, raw_url: str, label: str) -> bool:
    file_type = infer_file_type(
        raw_url,
        mime_type=_text(tag.get("type")),
        label=label,
    )
    if file_type != "file":
        return True
    if tag.has_attr("download"):
        return True
    if tag.name in {"object", "embed", "iframe"} and (
        "pdf" in _text(tag.get("type")).lower() or _FILE_LABEL_RE.search(label)
    ):
        return True
    return bool(_FILE_LABEL_RE.search(label))


def _explicit_attachment_values(item: Mapping[str, Any]) -> Iterable[Any]:
    for key in ("attachments", "documents", "files", "pdfs"):
        value = item.get(key)
        if isinstance(value, list):
            yield from value
        elif value:
            yield value
    for key in (
        "attachmentUrl", "attachment_url", "documentUrl", "document_url",
        "downloadUrl", "download_url", "pdfUrl", "pdf_url", "pdfLink", "pdf_link",
    ):
        if item.get(key):
            yield item.get(key)


def normalize_news_attachments(item: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return first-class attachment objects without mutating ``item``."""
    source_url = _text(item.get("sourceUrl") or item.get("url") or item.get("link"))
    content = _text(item.get("content"))
    results: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add(
        value: Any,
        *,
        name: str = "",
        file_type: str = "",
        mime_type: str = "",
        local_path: str = "",
        is_local: bool = False,
    ) -> None:
        url = _canonical_url(value, source_url)
        if not url or url in seen:
            return
        declared_type = _text(file_type).lower().lstrip(".")
        supported_types = set(FILE_TYPES.values()) | {"file"}
        resolved_type = declared_type if declared_type in supported_types else ""
        if not resolved_type:
            resolved_type = infer_file_type(url, mime_type=mime_type, label=name)
        seen.add(url)
        index = len(results) + 1
        results.append({
            "id": str(index),
            "name": _text(name) or _default_name(url, resolved_type, index),
            "url": url,
            "fileType": resolved_type or "file",
            "localPath": _text(local_path) or None,
            "isLocal": bool(is_local and local_path),
        })

    for value in _explicit_attachment_values(item):
        if isinstance(value, Mapping):
            add(
                value.get("url") or value.get("downloadUrl") or value.get("pdfUrl") or value.get("href"),
                name=_text(value.get("name") or value.get("title") or value.get("filename")),
                file_type=_text(value.get("fileType") or value.get("file_type")),
                mime_type=_text(value.get("mimeType") or value.get("mime_type")),
                local_path=_text(value.get("localPath") or value.get("local_path")),
                is_local=bool(value.get("isLocal") or value.get("is_local")),
            )
        else:
            add(value)

    if content:
        try:
            soup = BeautifulSoup(content, "html.parser")
            for tag in soup.find_all(["a", "button", "object", "embed", "iframe"]):
                if _in_noise_region(tag):
                    continue
                label = " ".join(filter(None, [
                    tag.get_text(" ", strip=True),
                    _text(tag.get("aria-label")),
                    _text(tag.get("title")),
                    _text(tag.get("class")),
                ]))
                for raw_url in _tag_url_candidates(tag):
                    if _tag_is_attachment(tag, raw_url, label):
                        add(raw_url, name=tag.get_text(" ", strip=True), mime_type=_text(tag.get("type")))
            for raw_url in _URL_RE.findall(soup.get_text(" ", strip=True)):
                if infer_file_type(raw_url) != "file":
                    add(raw_url)
        except Exception:
            pass

    if infer_file_type(source_url) != "file":
        add(source_url, name=_text(item.get("title")))

    return results


__all__ = ["FILE_TYPES", "infer_file_type", "normalize_news_attachments"]
