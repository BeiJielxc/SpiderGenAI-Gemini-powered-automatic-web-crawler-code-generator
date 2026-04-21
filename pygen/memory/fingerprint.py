"""HTML fingerprinting for site-drift detection.

When a website redesigns its list page, all the cached selectors in the
site profile become traps: the planner would re-use them, fail in
hard-to-debug ways, and rack up wasted iterations. We need a cheap way
to tell "this is the same DOM shape we saw last time" vs "the structure
moved, treat the profile as stale".

Strategy
--------
We hash a *normalized* skeleton of the HTML rather than the raw bytes:

* Drop comments and ``<script>`` / ``<style>`` blocks.
* Reduce every text node to its character length (text changes daily;
  layout doesn't).
* Sort attribute names so reordered attrs don't poison the hash.
* Keep only attributes that influence selector matching: ``id``,
  ``class``, ``role``, ``data-*`` (whitelist).
* Cap recursion to the first ~30 KB of HTML; that's enough to capture
  the list-page repeating items + pagination.

The result is a SHA-256 hex digest. Two pages share the digest iff
their list-page DOM topology + class/id/data-attr fingerprint match,
regardless of news content or visit counts.

The function is pure and exception-safe: any parser error is swallowed
and a sentinel ``"sha256:parse_error"`` is returned so the caller can
still record *that* the fingerprint failed without crashing.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional


# Cap how much HTML we feed BeautifulSoup. List pages with thousands of
# items would blow up memory + hash time without changing the
# discriminating power: the first ~30 KB always covers the header, nav,
# repeating-item template and pagination block.
_MAX_HTML_CHARS = 30_000

# Whitelist of attributes that contribute to CSS selector matching. We
# keep ``data-*`` values too because Elementor / WP widgets often
# encode their template ID there (e.g. data-elementor-type=loop).
_KEEP_ATTRS = {"id", "class", "role", "name", "type"}
_DATA_ATTR_PREFIX = "data-"

# Tags whose children are noise (scripts/styles/comments).
_DROP_TAGS = {"script", "style", "noscript", "svg", "iframe"}


def _normalize_attrs(attrs: dict) -> str:
    """Render a stable, comparable attribute string for one element."""
    if not attrs:
        return ""
    parts: List[str] = []
    for k in sorted(attrs.keys()):
        if k in _KEEP_ATTRS or k.startswith(_DATA_ATTR_PREFIX):
            v = attrs[k]
            if isinstance(v, list):
                v = " ".join(sorted(str(x) for x in v))
            else:
                v = str(v)
            # Strip any per-request tokens (UUIDs, hashes) that would otherwise
            # shift the fingerprint without an actual structural change.
            v = _strip_volatile_tokens(v)
            parts.append(f"{k}={v}")
    return ",".join(parts)


_VOLATILE_RE_PATTERNS = [
    # 8-128 char hex blobs (commit hashes / uuid w/o dashes)
    (8, 128, "0123456789abcdef"),
]


def _strip_volatile_tokens(value: str) -> str:
    """Replace long hex-like tokens with a placeholder so per-request
    cache busters don't perturb the fingerprint."""
    if not value:
        return value
    out_parts: List[str] = []
    for token in value.split():
        if _looks_volatile(token):
            out_parts.append("<vol>")
        else:
            out_parts.append(token)
    return " ".join(out_parts)


def _looks_volatile(token: str) -> bool:
    if len(token) < 12:
        return False
    if all(c in "0123456789abcdefABCDEF-" for c in token):
        # uuid / sha hex
        return True
    return False


def _walk(node, depth: int, max_depth: int = 8) -> List[str]:
    """Emit a flat list of normalized lines for a parsed tree."""
    lines: List[str] = []
    if depth > max_depth:
        return lines
    name = getattr(node, "name", None)
    if not name:
        # Text node — record its length bucket only.
        text = getattr(node, "string", None)
        if isinstance(text, str):
            length = len(text.strip())
            if length:
                bucket = _length_bucket(length)
                lines.append(f"#text[{bucket}]")
        return lines
    if name in _DROP_TAGS:
        return lines
    attrs = _normalize_attrs(getattr(node, "attrs", {}) or {})
    lines.append(f"{name}({attrs})")
    for child in getattr(node, "children", []) or []:
        lines.extend(_walk(child, depth + 1, max_depth))
    return lines


def _length_bucket(length: int) -> str:
    """Bucket text lengths into log-ish bins to absorb minor edits."""
    if length < 16:
        return "xs"
    if length < 64:
        return "s"
    if length < 256:
        return "m"
    if length < 1024:
        return "l"
    return "xl"


def compute_list_page_fingerprint(html: Optional[str]) -> str:
    """Return ``"sha256:<64-hex>"`` for the structural skeleton of ``html``.

    Returns ``"sha256:empty"`` for empty input or
    ``"sha256:parse_error"`` if BeautifulSoup chokes — both are stable
    sentinels suitable for storage and comparison.
    """
    if not html or not isinstance(html, str):
        return "sha256:empty"
    snippet = html[:_MAX_HTML_CHARS]
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is a project-wide dep
        return "sha256:no_bs4"
    try:
        soup = BeautifulSoup(snippet, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(snippet, "html.parser")
        except Exception:
            return "sha256:parse_error"
    try:
        body = soup.body or soup
        lines = _walk(body, 0)
    except Exception:
        return "sha256:parse_error"
    blob = "\n".join(lines).encode("utf-8", errors="ignore")
    digest = hashlib.sha256(blob).hexdigest()
    return f"sha256:{digest}"


def fingerprints_match(a: Optional[str], b: Optional[str]) -> bool:
    """True iff both are non-sentinel and equal."""
    if not a or not b:
        return False
    if a in ("sha256:empty", "sha256:parse_error", "sha256:no_bs4"):
        return False
    if b in ("sha256:empty", "sha256:parse_error", "sha256:no_bs4"):
        return False
    return a == b


__all__ = ["compute_list_page_fingerprint", "fingerprints_match"]
