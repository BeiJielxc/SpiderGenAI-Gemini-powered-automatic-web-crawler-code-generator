import pytest

import api
from golden_crawlers import GoldenCrawlerStore, compute_task_signature


@pytest.mark.asyncio
async def test_feedback_promotes_then_invalidates_the_same_python_asset(tmp_path, monkeypatch):
    store = GoldenCrawlerStore(tmp_path / "golden")
    request = {
        "url": "https://example.com/news",
        "startDate": "2026-01-01",
        "endDate": "2026-01-31",
        "outputScriptName": "crawler.py",
        "runMode": "news_sentiment",
    }
    signature = compute_task_signature(request)
    pending = store.stage_pending(
        task_id="task-1",
        signature=signature,
        code="print('approved')\n",
    )

    monkeypatch.setattr(api, "_golden_store", store)
    monkeypatch.setattr(api, "config", None)
    history_updates = []
    monkeypatch.setattr(
        api,
        "get_history_detail",
        lambda _task_id: {"status": "completed", "result": {"resultFile": "crawler.py"}},
    )
    monkeypatch.setattr(
        api,
        "update_history_status",
        lambda task_id, status, result=None, *args, **kwargs: history_updates.append(
            (task_id, status, result)
        ),
    )
    api.tasks["task-1"] = {
        "request": request,
        "taskSignature": signature,
        "executionSource": "generated",
        "logs": [],
    }

    promoted = await api._transition_golden_from_feedback("task-1", "correct")
    assert promoted["goldenStatus"] == "active"
    assert not pending.exists()
    assert store.load_active(signature).code == "print('approved')\n"

    api.tasks["task-2"] = {
        "request": request,
        "taskSignature": signature,
        "executionSource": "golden_replay",
        "logs": [],
    }
    invalidated = await api._transition_golden_from_feedback("task-2", "wrong")
    assert invalidated["goldenStatus"] == "invalid"
    assert store.load_active(signature) is None
    assert "invalid" in invalidated["goldenCodePath"]
    assert history_updates[-1][1] == "completed"
    assert history_updates[-1][2]["goldenStatus"] == "invalid"
    assert history_updates[-1][2]["taskSignature"] == signature

    api.tasks.pop("task-1", None)
    api.tasks.pop("task-2", None)
