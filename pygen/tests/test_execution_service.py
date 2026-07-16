"""Task-isolated runtime and zero-record hard-failure tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _local_config():
    return SimpleNamespace(
        sandbox_auto_start=True,
        sandbox_persistent_session=False,
        sandbox_backend="local",
        sandbox_docker_image=None,
        sandbox_docker_auto_pull=False,
        sandbox_docker_disable_network=False,
        sandbox_docker_mount_workdir=True,
    )


@pytest.mark.asyncio
async def test_execution_service_accepts_task_owned_records(tmp_path):
    from execution_service import TaskExecutionService

    script = (
        "import json\n"
        "json.dump({'reports': [{'name': 'A', 'date': '2026-07-01', "
        "'downloadUrl': 'https://example.test/a.pdf'}]}, "
        "open('records.json', 'w', encoding='utf-8'))\n"
    )
    report, payload = await TaskExecutionService(tmp_path).execute(
        task_id="task-ok", script_code=script, run_mode="enterprise_report",
        config=_local_config(), timeout_sec=20,
    )
    assert report.success is True
    assert report.record_count == 1
    assert payload and payload["reports"][0]["name"] == "A"
    assert all("task-ok" in path for path in report.output_files)


@pytest.mark.asyncio
async def test_execution_service_rejects_clean_exit_with_zero_records(tmp_path):
    from execution_service import TaskExecutionService

    script = "import json\njson.dump({'reports': []}, open('empty.json', 'w'))\n"
    report, payload = await TaskExecutionService(tmp_path).execute(
        task_id="task-empty", script_code=script, run_mode="enterprise_report",
        config=_local_config(), timeout_sec=20,
    )
    assert report.success is False
    assert report.exit_code == 0
    assert report.record_count == 0
    assert payload is None
    assert "zero usable records" in (report.error or "")


@pytest.mark.asyncio
async def test_execution_service_redirects_absolute_output_directory(tmp_path):
    from execution_service import TaskExecutionService

    shared_dir = tmp_path / "shared-output"
    absolute_literal = repr(str(shared_dir))
    script = (
        "import json, os\n"
        f"OUTPUT_DIR = {absolute_literal}\n"
        "os.makedirs(OUTPUT_DIR, exist_ok=True)\n"
        "with open(os.path.join(OUTPUT_DIR, 'records.json'), 'w', encoding='utf-8') as f:\n"
        "    json.dump({'reports': [{'name': 'owned'}]}, f)\n"
    )
    report, payload = await TaskExecutionService(tmp_path / "tasks").execute(
        task_id="task-redirect", script_code=script,
        run_mode="enterprise_report", config=_local_config(), timeout_sec=20,
    )
    assert report.success is True
    assert payload and payload["reports"][0]["name"] == "owned"
    assert not (shared_dir / "records.json").exists()
    assert Path(report.workdir, "records.json").exists()
