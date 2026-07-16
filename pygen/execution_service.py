"""Task-isolated execution and explicit result manifests."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

try:
    from agents.evidence import RuntimeReport
    from executor_session import ExecutorSession
except ImportError:  # pragma: no cover
    from .agents.evidence import RuntimeReport  # type: ignore
    from .executor_session import ExecutorSession  # type: ignore


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_OUTPUT_DIR_NAMES = {
    "output_dir", "output_directory", "result_dir", "results_dir",
    "save_dir", "export_dir",
}


def _absolute_path_literal(node: ast.AST) -> Optional[str]:
    candidate = node
    if isinstance(candidate, ast.Call) and candidate.args:
        func = candidate.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name in {"Path", "PurePath", "PureWindowsPath", "PurePosixPath"}:
            candidate = candidate.args[0]
    if not isinstance(candidate, ast.Constant) or not isinstance(candidate.value, str):
        return None
    value = candidate.value
    if PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute():
        return value
    return None


class _OutputDirectoryNormalizer(ast.NodeTransformer):
    def __init__(self) -> None:
        self.rewrites = 0

    def _normalize(self, targets: List[ast.expr], value: ast.expr) -> ast.expr:
        names = [target.id.lower() for target in targets if isinstance(target, ast.Name)]
        if not any(name in _OUTPUT_DIR_NAMES for name in names):
            return value
        if _absolute_path_literal(value) is None:
            return value
        self.rewrites += 1
        env_expr = ast.parse(
            '__import__("os").environ.get("PYGEN_OUTPUT_DIR", ".")',
            mode="eval",
        ).body
        if isinstance(value, ast.Call):
            value.args[0] = env_expr
            return value
        return env_expr

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        self.generic_visit(node)
        node.value = self._normalize(node.targets, node.value)
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        self.generic_visit(node)
        if node.value is not None:
            node.value = self._normalize([node.target], node.value)
        return node


def normalize_output_directories(script_code: str) -> Tuple[str, int]:
    """Redirect absolute output-directory assignments to the task runtime.

    The canonical generated script is left untouched; only the isolated runtime
    copy is normalized. Relative paths remain valid because its cwd is already
    the task-owned directory.
    """
    try:
        tree = ast.parse(script_code)
    except SyntaxError:
        return script_code, 0
    normalizer = _OutputDirectoryNormalizer()
    tree = normalizer.visit(tree)
    if not normalizer.rewrites:
        return script_code, 0
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n", normalizer.rewrites


def _records_from_payload(payload: Any, run_mode: str) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    keys = ("articles", "news", "items", "data") if run_mode == "news_sentiment" else ("reports", "items", "data")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for nested_key in ("records", "rows", "list", "items"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
    return []


def _quality(records: List[Dict[str, Any]], run_mode: str) -> Dict[str, float]:
    if not records:
        return {"date_fill_rate": 0.0, "url_valid_rate": 0.0}
    date_count = sum(1 for item in records if item.get("date") or item.get("publishDate"))
    url_keys = ("sourceUrl", "url", "link") if run_mode == "news_sentiment" else ("downloadUrl", "url", "link")
    valid_urls = 0
    for item in records:
        value = next((item.get(key) for key in url_keys if item.get(key)), "")
        if str(value).startswith(("http://", "https://", "//")):
            valid_urls += 1
    return {
        "date_fill_rate": date_count / len(records),
        "url_valid_rate": valid_urls / len(records),
    }


class TaskExecutionService:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _workdir(self, task_id: str) -> Path:
        if not _SAFE_TASK_ID.match(task_id or ""):
            raise ValueError(f"Unsafe task_id: {task_id!r}")
        workdir = self.root / task_id
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir

    async def execute(
        self,
        *,
        task_id: str,
        script_code: str,
        run_mode: str,
        config: Any,
        timeout_sec: int = 300,
    ) -> Tuple[RuntimeReport, Optional[Dict[str, Any]]]:
        workdir = self._workdir(task_id)
        script_path = workdir / "crawler.py"
        runtime_code, output_path_rewrites = normalize_output_directories(script_code)
        script_path.write_text(runtime_code, encoding="utf-8")

        session = ExecutorSession(
            session_id=f"runtime-{task_id[:18]}",
            workdir=workdir,
            auto_start=getattr(config, "sandbox_auto_start", True),
            persistent=getattr(config, "sandbox_persistent_session", True),
            backend=getattr(config, "sandbox_backend", "docker"),
            docker_image=getattr(config, "sandbox_docker_image", None),
            docker_auto_pull=getattr(config, "sandbox_docker_auto_pull", True),
            docker_disable_network=getattr(config, "sandbox_docker_disable_network", False),
            docker_mount_workdir=getattr(config, "sandbox_docker_mount_workdir", True),
        )
        result = None
        try:
            result = await session.run_shell(
                "python -u crawler.py",
                timeout_sec=timeout_sec,
                env={"PYTHONIOENCODING": "utf-8", "PYGEN_OUTPUT_DIR": "."},
            )
        finally:
            await session.close(force=True)

        output_files = sorted(
            [p for p in workdir.rglob("*.json") if p.name != "result_manifest.json"],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        selected_payload: Optional[Dict[str, Any]] = None
        selected_records: List[Dict[str, Any]] = []
        selected_path: Optional[Path] = None
        for path in output_files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            records = _records_from_payload(payload, run_mode)
            if records:
                selected_payload = payload if isinstance(payload, dict) else {"data": payload}
                selected_records = records
                selected_path = path
                break

        quality = _quality(selected_records, run_mode)
        execution_ok = bool(result and result.success)
        success = execution_ok and bool(selected_records)
        error = None
        if not execution_ok:
            error = (result.error if result else None) or "Crawler execution failed."
        elif not output_files:
            error = (
                "Crawler produced no task-owned JSON output. The script may have "
                "written to an absolute/shared path; use PYGEN_OUTPUT_DIR."
            )
        elif not selected_records:
            error = "Crawler output contained zero usable records."

        report = RuntimeReport(
            success=success,
            task_id=task_id,
            workdir=str(workdir),
            script_path=str(script_path),
            output_files=[str(path) for path in output_files],
            record_count=len(selected_records),
            stage_counts={
                "runtime_records": len(selected_records),
                "final_records": len(selected_records),
                "output_path_rewrites": output_path_rewrites,
            },
            schema_valid=bool(selected_payload and selected_records),
            date_fill_rate=quality["date_fill_rate"],
            url_valid_rate=quality["url_valid_rate"],
            exit_code=int(result.exit_code if result else 1),
            timed_out=bool(result.timed_out if result else False),
            error=error,
            stdout_tail=(result.stdout[-2000:] if result else ""),
            stderr_tail=(result.stderr[-2000:] if result else ""),
        )
        manifest = report.to_dict()
        manifest["selected_output"] = str(selected_path) if selected_path else None
        manifest["output_path_rewrites"] = output_path_rewrites
        (workdir / "result_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return report, selected_payload


__all__ = ["TaskExecutionService", "normalize_output_directories"]
