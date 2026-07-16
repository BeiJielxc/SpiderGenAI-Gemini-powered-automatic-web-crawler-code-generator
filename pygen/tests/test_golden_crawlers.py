from pathlib import Path

import pytest

from golden_crawlers import GoldenCrawlerStore, canonical_task_config, compute_task_signature


def _request(**overrides):
    data = {
        "url": "HTTPS://Example.COM/news/?b=2&a=1#latest",
        "startDate": "2026-01-01",
        "endDate": "2026-01-31",
        "outputScriptName": "first_name.py",
        "taskObjective": "Collect news",
        "extraRequirements": "Keep source URLs",
        "siteName": "Example",
        "listPageName": "News",
        "sourceCredibility": "T1",
        "runMode": "news_sentiment",
        "crawlMode": "agent",
        "downloadReport": "yes",
        "selectedPaths": ["Markets", "News"],
        "attachments": [],
        "prevTaskId": "old-task",
    }
    data.update(overrides)
    return data


def test_signature_is_canonical_and_ignores_output_identity():
    first = _request()
    second = _request(
        url="https://example.com/news?a=1&b=2",
        outputScriptName="renamed.py",
        selectedPaths=["News", "Markets", "News"],
        prevTaskId="another-task",
    )

    assert canonical_task_config(first) == canonical_task_config(second)
    assert compute_task_signature(first) == compute_task_signature(second)


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "https://example.com/reports"),
        ("startDate", "2026-02-01"),
        ("endDate", "2026-02-28"),
        ("taskObjective", "Collect reports"),
        ("extraRequirements", "Only PDFs"),
        ("runMode", "enterprise_report"),
        ("downloadReport", "no"),
        ("selectedPaths", ["Reports"]),
    ],
)
def test_signature_changes_when_crawler_behaviour_changes(field, value):
    assert compute_task_signature(_request()) != compute_task_signature(_request(**{field: value}))


def test_attachment_content_is_part_of_signature():
    first = _request(attachments=[{"filename": "hint.txt", "mimeType": "text/plain", "base64": "YQ=="}])
    second = _request(attachments=[{"filename": "hint.txt", "mimeType": "text/plain", "base64": "Yg=="}])
    assert compute_task_signature(first) != compute_task_signature(second)


def test_file_lifecycle_is_expressed_only_by_directories(tmp_path: Path):
    store = GoldenCrawlerStore(tmp_path / "golden")
    signature = compute_task_signature(_request())

    pending = store.stage_pending(task_id="task-1", signature=signature, code="print('v1')\n")
    assert pending.parent.name == "pending"
    assert store.load_active(signature) is None

    active = store.activate(task_id="task-1", signature=signature)
    assert active == store.active_path(signature)
    assert active is not None and active.parent.name == "active"
    assert not pending.exists()
    assert store.load_active(signature).code == "print('v1')\n"

    invalid = store.invalidate_active(signature=signature, task_id="task-2")
    assert invalid is not None and invalid.parent.parent.name == "invalid"
    assert invalid.read_text(encoding="utf-8") == "print('v1')\n"
    assert store.load_active(signature) is None
    assert list((tmp_path / "golden").rglob("*.json")) == []


def test_rejected_pending_is_retained_but_never_active(tmp_path: Path):
    store = GoldenCrawlerStore(tmp_path)
    signature = compute_task_signature(_request())
    store.stage_pending(task_id="task-1", signature=signature, code="print('candidate')")

    invalid = store.reject_pending(task_id="task-1", signature=signature)
    assert invalid is not None and invalid.is_file()
    assert store.load_active(signature) is None


def test_store_rejects_unsafe_identifiers(tmp_path: Path):
    store = GoldenCrawlerStore(tmp_path)
    signature = compute_task_signature(_request())
    with pytest.raises(ValueError):
        store.stage_pending(task_id="../escape", signature=signature, code="print(1)")
    with pytest.raises(ValueError):
        store.load_active("not-a-signature")
