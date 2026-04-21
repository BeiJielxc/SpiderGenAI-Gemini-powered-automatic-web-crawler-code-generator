"""Tests for the v2 ArtifactStore (per-task subdirs, summary, scoped read, TTL)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

PYGEN_DIR = Path(__file__).resolve().parent.parent
if str(PYGEN_DIR) not in sys.path:
    sys.path.insert(0, str(PYGEN_DIR))

from artifact_store import ArtifactStore  # noqa: E402


# ---------------------------------------------------------------------------
# Layout & isolation
# ---------------------------------------------------------------------------


def test_per_task_subdir_isolates_artifacts(tmp_path):
    store = ArtifactStore(tmp_path, per_task_subdir=True)

    ref_a = store.put_text("hello A", prefix="page_html", task_id="task-A")
    ref_b = store.put_text("hello B", prefix="page_html", task_id="task-B")

    # Files land in their respective buckets
    assert (tmp_path / "task-A").is_dir()
    assert (tmp_path / "task-B").is_dir()
    assert Path(ref_a.path).parent.name == "task-A"
    assert Path(ref_b.path).parent.name == "task-B"

    # Cross-task lookup still works via _locate
    assert store.read_text(ref_a.artifact_id) == "hello A"
    assert store.read_text(ref_b.artifact_id) == "hello B"


def test_missing_task_id_falls_back_to_global_bucket(tmp_path):
    store = ArtifactStore(tmp_path, per_task_subdir=True)
    ref = store.put_text("orphan", prefix="page_html")
    assert (tmp_path / "_global").is_dir()
    assert Path(ref.path).parent.name == "_global"


def test_disable_per_task_subdir_keeps_flat_layout(tmp_path):
    store = ArtifactStore(tmp_path, per_task_subdir=False)
    ref = store.put_text("flat", prefix="page_html", task_id="task-A")
    assert Path(ref.path).parent == tmp_path


# ---------------------------------------------------------------------------
# put_with_summary
# ---------------------------------------------------------------------------


def test_put_with_summary_attaches_summary_to_ref(tmp_path):
    store = ArtifactStore(tmp_path)
    summarizer = lambda payload: {"len": len(payload)}

    ref = store.put_with_summary(
        "<html>hi</html>",
        prefix="page_html",
        summarizer=summarizer,
        task_id="t1",
    )
    assert ref.summary == {"len": len("<html>hi</html>")}
    prompt_dict = ref.to_prompt_dict()
    assert prompt_dict["summary"] == {"len": len("<html>hi</html>")}
    assert "path" not in prompt_dict, "absolute path must not leak to prompt"


def test_put_with_summary_handles_summarizer_exceptions(tmp_path):
    store = ArtifactStore(tmp_path)

    def boom(_payload):
        raise ValueError("can't summarize")

    ref = store.put_with_summary(
        "<html>hi</html>", prefix="page_html", summarizer=boom, task_id="t1"
    )
    assert ref.summary is not None
    assert "_summary_error" in ref.summary
    # Underlying file must still exist
    assert Path(ref.path).read_text(encoding="utf-8") == "<html>hi</html>"


def test_put_with_summary_no_summarizer_acts_like_put(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.put_with_summary({"a": 1}, prefix="json", summarizer=None, task_id="t1")
    assert ref.summary is None
    assert json.loads(Path(ref.path).read_text(encoding="utf-8")) == {"a": 1}


def test_fallback_hints_propagate_to_ref(tmp_path):
    store = ArtifactStore(tmp_path)
    summarizer = lambda payload: {
        "list_candidates": [{"selector": "ul.x"}],
        "fallback_hints": ["read_artifact(id, scope='css:ul.x')"],
    }
    ref = store.put_with_summary(
        "<html><ul class='x'><li>1</li></ul></html>",
        prefix="page_html",
        summarizer=summarizer,
        task_id="t1",
    )
    assert ref.fallback_hints == ["read_artifact(id, scope='css:ul.x')"]
    assert ref.to_prompt_dict()["fallback_hints"]


# ---------------------------------------------------------------------------
# Scoped reads
# ---------------------------------------------------------------------------


def test_read_full_returns_content(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.put_text("abcdefghij", prefix="t", task_id="t1")
    assert store.read(ref.artifact_id) == "abcdefghij"


def test_read_head_and_tail(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.put_text("abcdefghij", prefix="t", task_id="t1")
    assert store.read(ref.artifact_id, scope="head:3") == "abc"
    assert store.read(ref.artifact_id, scope="tail:3") == "hij"


def test_read_css_scope_on_html(tmp_path):
    store = ArtifactStore(tmp_path)
    html = "<html><body><ul class='news'><li>a</li><li>b</li></ul><p>x</p></body></html>"
    ref = store.put_text(html, prefix="page_html", task_id="t1")
    out = store.read(ref.artifact_id, scope="css:ul.news li")
    assert "<li>a</li>" in out
    assert "<li>b</li>" in out


def test_read_css_unwraps_json_html_payload(tmp_path):
    store = ArtifactStore(tmp_path)
    payload = {"html": "<html><body><div id='x'>hi</div></body></html>"}
    ref = store.put_json(payload, prefix="page_html", task_id="t1")
    out = store.read(ref.artifact_id, scope="css:#x")
    assert "<div id=\"x\">hi</div>" in out


def test_read_jsonpath_traversal(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.put_json(
        {"api_requests": [{"url": "https://a/b", "method": "GET"}]},
        prefix="net",
        task_id="t1",
    )
    out = store.read(ref.artifact_id, scope="jsonpath:api_requests[0].url")
    assert out == "https://a/b"


def test_read_unknown_scope_returns_helpful_error(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.put_text("abc", prefix="t", task_id="t1")
    out = store.read(ref.artifact_id, scope="xpath://div")
    assert "unknown scope" in out


def test_read_missing_returns_none(tmp_path):
    store = ArtifactStore(tmp_path)
    assert store.read("does_not_exist", scope=None) is None


# ---------------------------------------------------------------------------
# TTL cleanup
# ---------------------------------------------------------------------------


def test_cleanup_expired_removes_old_files_and_empty_buckets(tmp_path):
    store = ArtifactStore(tmp_path, per_task_subdir=True)
    fresh = store.put_text("new", prefix="t", task_id="task-fresh")
    old = store.put_text("old", prefix="t", task_id="task-old")

    # Backdate the old file's mtime to 10 days ago.
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old.path, (ten_days_ago, ten_days_ago))

    removed = store.cleanup_expired(7 * 86400)
    assert removed == 1

    # Old bucket is now empty -> directory cleared.
    assert not Path(old.path).exists()
    assert not (tmp_path / "task-old").exists()

    # Fresh file untouched.
    assert Path(fresh.path).exists()


def test_cleanup_with_zero_ttl_is_noop(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.put_text("x", prefix="t", task_id="t1")
    assert store.cleanup_expired(0) == 0
    assert Path(ref.path).exists()
