"""
Runtime critic with 3-round diagnosis/repair loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from failure_classifier import FailureClassifier, FailureType
    from signals_collector import ExecutionSignals, ExecutionStatus, SignalsCollector
    from validator import StaticCodeValidator
    from prompts import load as load_prompt
except ImportError:
    from .failure_classifier import FailureClassifier, FailureType  # type: ignore
    from .signals_collector import ExecutionSignals, ExecutionStatus, SignalsCollector  # type: ignore
    from .validator import StaticCodeValidator  # type: ignore
    from .prompts import load as load_prompt  # type: ignore


NETWORK_LIKE_CAUSES = {
    FailureType.BLOCKED_BY_WAF.value,
    FailureType.RATE_LIMITED.value,
    FailureType.NETWORK_ERROR.value,
    FailureType.TIMEOUT.value,
}

RUN_MODE_REQUIRED_FIELDS = {
    "enterprise_report": ("name", "downloadUrl"),
    "news_sentiment": ("title", "sourceUrl"),
}

CODE_ISSUE_TO_CAUSE = {
    "ERR_001": FailureType.HARDCODED_INDEX.value,
    "ERR_002": FailureType.NULL_POINTER.value,
    "ERR_003": FailureType.PAGINATION_DATE_LOST.value,
    "ERR_005": FailureType.SELECTOR_MISMATCH.value,
    "ERR_006": FailureType.SCHEMA_MISMATCH.value,
    "ERR_008": FailureType.DATE_EXTRACTION_FAILED.value,
    "ERR_009": FailureType.DATE_EXTRACTION_FAILED.value,
    "ERR_SYNTAX": FailureType.UNKNOWN.value,
}


def _trim_text(value: str, limit: int = 1200) -> str:
    if not value:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit] + "...(truncated)"


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 3:
            raw = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    depth = 0
    start = -1
    for idx, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    maybe = json.loads(raw[start : idx + 1])
                    if isinstance(maybe, dict):
                        return maybe
                except Exception:
                    start = -1
                    continue
    return None


def _unique_keep_order(values: Sequence[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _extract_records(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("reports", "articles", "news", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


@dataclass
class CriticIssue:
    severity: str
    code: str
    message: str


@dataclass
class CriticVerdict:
    passed: bool
    summary: str
    confidence: float
    issues: List[CriticIssue] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "summary": self.summary,
            "confidence": self.confidence,
            "issues": [issue.__dict__ for issue in self.issues],
            "recommendations": self.recommendations,
            "details": self.details,
        }


class Critic:
    def __init__(
        self,
        llm_agent=None,
        artifact_store=None,
        max_retries: int = 3,
        lightweight_timeout_sec: int = 120,
        lightweight_max_items: int = 5,
    ):
        self.llm_agent = llm_agent
        self.artifact_store = artifact_store
        self.max_retries = max(1, min(3, int(max_retries)))
        self.lightweight_timeout_sec = max(20, int(lightweight_timeout_sec))
        self.lightweight_max_items = max(3, min(5, int(lightweight_max_items)))
        self.static_validator = StaticCodeValidator()
        self.failure_classifier = FailureClassifier(llm_client=None)

    def evaluate_generated_code(
        self,
        code: str,
        run_mode: str,
        objective: str = "",
        min_items: int = 1,
        target_url: str = "",
        max_retries: Optional[int] = None,
        executor_session=None,
    ) -> CriticVerdict:
        coro = self.evaluate_generated_code_async(
            code=code,
            run_mode=run_mode,
            objective=objective,
            min_items=min_items,
            target_url=target_url,
            max_retries=max_retries,
            executor_session=executor_session,
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        raise RuntimeError("Use evaluate_generated_code_async() in async context.")

    async def evaluate_generated_code_async(
        self,
        code: str,
        run_mode: str,
        objective: str = "",
        min_items: int = 1,
        target_url: str = "",
        max_retries: Optional[int] = None,
        executor_session=None,
        enhanced_analysis: Optional[Dict[str, Any]] = None,
    ) -> CriticVerdict:
        retries = max(1, min(3, int(max_retries if max_retries is not None else self.max_retries)))
        if not (code or "").strip():
            return CriticVerdict(
                passed=False,
                summary="Generated code is empty.",
                confidence=1.0,
                issues=[CriticIssue("error", "empty_code", "Generated code is empty.")],
                recommendations=["Regenerate crawler code before validation."],
                details={"max_retries": retries},
            )

        recommendations: List[str] = []
        evidence: List[Dict[str, Any]] = []
        round_summaries: List[Dict[str, Any]] = []
        used_causes: List[str] = []
        working_code = code

        round1 = await self._run_rule_round(
            code=working_code,
            run_mode=run_mode,
            objective=objective,
            min_items=min_items,
            target_url=target_url,
            executor_session=executor_session,
            round_index=1,
            excluded_primary_causes=set(),
            run_minimal_experiment=True,
            enhanced_analysis=enhanced_analysis,
        )
        evidence.extend(round1["evidence"])
        round_summaries.append(round1["summary_payload"])
        recommendations.extend(round1["recommendations"])
        used_causes.append(round1["primary_cause"])

        if round1["passed"]:
            return self._build_pass_verdict(
                summary="Critic passed in round 1.",
                confidence=round1["confidence"],
                recommendations=_unique_keep_order(recommendations),
                details={
                    "final_round": 1,
                    "max_retries": retries,
                    "rounds": round_summaries,
                    "evidence": evidence,
                },
            )

        primary = round1["primary_cause"]
        backup = round1["backup_cause"]
        if round1["uncertain"] and self.llm_agent:
            adjudication = await self._llm_adjudicate_cause(
                code=working_code,
                run_mode=run_mode,
                objective=objective,
                primary_cause=primary,
                backup_cause=backup,
                evidence=evidence,
            )
            evidence.append({"round": 1, "step": "llm_adjudication", "result": adjudication or {"used": False}})
            if adjudication and adjudication.get("cause"):
                primary = str(adjudication["cause"])

        if retries <= 1:
            return self._build_fail_verdict(
                summary="Critic failed in round 1 and retries limit reached.",
                confidence=round1["confidence"],
                primary_cause=primary,
                backup_cause=backup,
                issues=round1["issues"],
                recommendations=_unique_keep_order(recommendations),
                details={
                    "final_round": 1,
                    "max_retries": retries,
                    "rounds": round_summaries,
                    "evidence": evidence,
                    "stopped_reason": "max_retries_exceeded",
                },
            )

        repaired2 = await self._llm_repair_once(
            code=working_code,
            run_mode=run_mode,
            objective=objective,
            primary_cause=primary,
            backup_cause=backup,
            evidence=evidence,
            round_index=2,
            strategy_hint="Apply one focused repair for primary cause.",
        )
        evidence.append(
            {
                "round": 2,
                "step": "llm_repair",
                "result": {"changed": bool(repaired2 and repaired2 != working_code)},
            }
        )
        if repaired2 and repaired2.strip():
            working_code = repaired2

        round2 = await self._run_rule_round(
            code=working_code,
            run_mode=run_mode,
            objective=objective,
            min_items=min_items,
            target_url=target_url,
            executor_session=executor_session,
            round_index=2,
            excluded_primary_causes=set(),
            run_minimal_experiment=True,
            enhanced_analysis=enhanced_analysis,
        )
        evidence.extend(round2["evidence"])
        round_summaries.append(round2["summary_payload"])
        recommendations.extend(round2["recommendations"])
        used_causes.append(round2["primary_cause"])

        if round2["passed"]:
            return self._build_pass_verdict(
                summary="Critic passed in round 2 after LLM repair.",
                confidence=round2["confidence"],
                recommendations=_unique_keep_order(recommendations),
                details={
                    "final_round": 2,
                    "max_retries": retries,
                    "repaired_code": working_code,
                    "rounds": round_summaries,
                    "evidence": evidence,
                },
            )

        if retries <= 2 or not self.llm_agent:
            return self._build_fail_verdict(
                summary="Critic failed after round 2 and stopped.",
                confidence=round2["confidence"],
                primary_cause=round2["primary_cause"],
                backup_cause=round2["backup_cause"],
                issues=round2["issues"],
                recommendations=_unique_keep_order(recommendations),
                details={
                    "final_round": 2,
                    "max_retries": retries,
                    "repaired_code": working_code,
                    "rounds": round_summaries,
                    "evidence": evidence,
                    "stopped_reason": "max_retries_exceeded_or_llm_unavailable",
                },
            )

        excluded = set(_unique_keep_order(used_causes))
        round3_diag = await self._run_rule_round(
            code=working_code,
            run_mode=run_mode,
            objective=objective,
            min_items=min_items,
            target_url=target_url,
            executor_session=executor_session,
            round_index=3,
            excluded_primary_causes=excluded,
            run_minimal_experiment=True,
            enhanced_analysis=enhanced_analysis,
        )
        evidence.extend(round3_diag["evidence"])
        round_summaries.append(round3_diag["summary_payload"])
        recommendations.extend(round3_diag["recommendations"])

        repaired3 = await self._llm_repair_once(
            code=working_code,
            run_mode=run_mode,
            objective=objective,
            primary_cause=round3_diag["primary_cause"],
            backup_cause=round3_diag["backup_cause"],
            evidence=evidence,
            round_index=3,
            strategy_hint="Final fallback repair, one targeted fix only.",
        )
        evidence.append(
            {
                "round": 3,
                "step": "final_targeted_repair",
                "result": {"changed": bool(repaired3 and repaired3 != working_code)},
            }
        )
        if repaired3 and repaired3.strip():
            working_code = repaired3

        final_verify = await self._run_rule_round(
            code=working_code,
            run_mode=run_mode,
            objective=objective,
            min_items=min_items,
            target_url=target_url,
            executor_session=executor_session,
            round_index=3,
            excluded_primary_causes=set(),
            run_minimal_experiment=False,
            enhanced_analysis=enhanced_analysis,
        )
        evidence.extend(final_verify["evidence"])
        round_summaries.append(final_verify["summary_payload"])
        recommendations.extend(final_verify["recommendations"])

        if final_verify["passed"]:
            return self._build_pass_verdict(
                summary="Critic passed in round 3 final fallback.",
                confidence=final_verify["confidence"],
                recommendations=_unique_keep_order(recommendations),
                details={
                    "final_round": 3,
                    "max_retries": retries,
                    "repaired_code": working_code,
                    "rounds": round_summaries,
                    "evidence": evidence,
                },
            )

        return self._build_fail_verdict(
            summary="Critic failed after 3 rounds. Auto-repair stopped.",
            confidence=final_verify["confidence"],
            primary_cause=final_verify["primary_cause"],
            backup_cause=final_verify["backup_cause"],
            issues=final_verify["issues"],
            recommendations=_unique_keep_order(recommendations),
            details={
                "final_round": 3,
                "max_retries": retries,
                "repaired_code": working_code,
                "rounds": round_summaries,
                "evidence": evidence,
                "stopped_reason": "max_retries_exceeded",
            },
        )

    async def _run_rule_round(
        self,
        code: str,
        run_mode: str,
        objective: str,
        min_items: int,
        target_url: str,
        executor_session,
        round_index: int,
        excluded_primary_causes: set[str],
        run_minimal_experiment: bool,
        enhanced_analysis: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        evidence_steps: List[Dict[str, Any]] = []
        detail_probe = (enhanced_analysis or {}).get("detail_probe") if isinstance(enhanced_analysis, dict) else None
        static_issues = self._collect_static_issues(
            code=code, run_mode=run_mode, objective=objective, detail_probe=detail_probe
        )
        static_errors = [item for item in static_issues if item.severity == "error"]
        static_warnings = [item for item in static_issues if item.severity == "warning"]

        evidence_steps.append(
            {
                "round": round_index,
                "step": "static_check",
                "result": {
                    "error_count": len(static_errors),
                    "warning_count": len(static_warnings),
                    "top_errors": [item.__dict__ for item in static_errors[:5]],
                    "top_warnings": [item.__dict__ for item in static_warnings[:5]],
                },
            }
        )

        runtime_result = await self._run_lightweight_execution(
            code=code,
            executor_session=executor_session,
            timeout_sec=self.lightweight_timeout_sec,
            max_items=self.lightweight_max_items,
        )

        evidence_steps.append(
            {
                "round": round_index,
                "step": "lightweight_runtime",
                "result": {
                    "execution_success": runtime_result["execution_success"],
                    "timed_out": runtime_result["timed_out"],
                    "exit_code": runtime_result["exit_code"],
                    "duration_seconds": runtime_result["duration_seconds"],
                    "record_count": runtime_result["record_count"],
                    "date_fill_rate": runtime_result["date_fill_rate"],
                    "status": runtime_result["signals"].status.value if runtime_result["signals"] else None,
                    "http_statuses": runtime_result["http_statuses"],
                    "stdout_tail": runtime_result["stdout_tail"],
                    "stderr_tail": runtime_result["stderr_tail"],
                },
            }
        )

        signals: ExecutionSignals = runtime_result["signals"]
        failure_report = self.failure_classifier.classify(signals, code)
        primary_cause, backup_cause = self._choose_top_two_causes(
            failure_report=failure_report,
            static_issues=static_issues,
            runtime_result=runtime_result,
            excluded_primary_causes=excluded_primary_causes,
        )
        uncertain = (failure_report.confidence < 0.65) or (primary_cause == FailureType.UNKNOWN.value)

        evidence_steps.append(
            {
                "round": round_index,
                "step": "cause_ranking",
                "result": {
                    "primary_cause": primary_cause,
                    "backup_cause": backup_cause,
                    "confidence": failure_report.confidence,
                    "classifier_summary": failure_report.summary,
                },
            }
        )

        if run_minimal_experiment:
            minimal_experiment = await self._run_minimal_experiment(
                primary_cause=primary_cause,
                run_mode=run_mode,
                target_url=target_url,
                runtime_result=runtime_result,
                code=code,
                executor_session=executor_session,
            )
            evidence_steps.append(
                {
                    "round": round_index,
                    "step": "minimal_experiment",
                    "result": minimal_experiment,
                }
            )

        quality = self._assess_quality(records=runtime_result["records"], run_mode=run_mode, min_items=min_items)
        evidence_steps.append(
            {
                "round": round_index,
                "step": "output_quality",
                "result": quality,
            }
        )

        passed = (
            len(static_errors) == 0
            and runtime_result["execution_success"]
            and quality["meets_min_items"]
            and quality["required_fields_ok"]
            and primary_cause not in NETWORK_LIKE_CAUSES
        )

        round_recommendations = _unique_keep_order(
            list(failure_report.fix_suggestions[:4])
            + [item.message for item in static_errors[:2]]
            + (quality.get("recommendations") or [])
        )
        issues = [CriticIssue("error", "round_failure", failure_report.summary)] + static_errors[:5]
        summary_payload = {
            "round": round_index,
            "passed": passed,
            "primary_cause": primary_cause,
            "backup_cause": backup_cause,
            "uncertain": uncertain,
            "classifier_confidence": failure_report.confidence,
            "record_count": runtime_result["record_count"],
            "static_error_count": len(static_errors),
        }

        return {
            "passed": passed,
            "confidence": max(0.2, min(0.95, float(failure_report.confidence if not passed else 0.9))),
            "primary_cause": primary_cause,
            "backup_cause": backup_cause,
            "uncertain": uncertain,
            "issues": issues,
            "recommendations": round_recommendations,
            "summary_payload": summary_payload,
            "evidence": evidence_steps,
            "runtime_result": runtime_result,
        }

    def _collect_static_issues(
        self,
        code: str,
        run_mode: str,
        objective: str,
        detail_probe: Optional[Dict[str, Any]] = None,
    ) -> List[CriticIssue]:
        issues: List[CriticIssue] = []
        validator_issues = self.static_validator.validate(code)
        for item in validator_issues:
            issues.append(CriticIssue(severity=item.severity.value, code=item.code, message=item.message))

        if "if __name__ == \"__main__\"" not in code and "if __name__ == '__main__'" not in code:
            issues.append(CriticIssue("warning", "missing_main_guard", "Missing main guard."))
        if ("json.dump(" not in code) and ("save_results(" not in code):
            issues.append(CriticIssue("error", "missing_output", "No JSON persistence detected."))
        if not any(token in code for token in ("requests", "httpx", "playwright", "BeautifulSoup")):
            issues.append(CriticIssue("error", "missing_backend", "No extraction backend detected in code."))

        for field in RUN_MODE_REQUIRED_FIELDS.get(run_mode, tuple()):
            if field not in code:
                issues.append(CriticIssue("warning", "field_missing", f"Expected field '{field}' not detected in code."))
        if objective and "TODO" in code:
            issues.append(CriticIssue("warning", "todo_left", "Code contains TODO placeholder."))

        # 详情页正文选择器：代码必须使用 probe 返回的候选中之一（大模型自己选），不能使用未在候选里的泛化选择器
        if detail_probe and isinstance(detail_probe, dict):
            candidates = detail_probe.get("contentCandidates") or []
            allowed_selectors = [str(c.get("selector", "")).strip() for c in candidates if c.get("selector")]
            if allowed_selectors:
                used_any = any(sel in code for sel in allowed_selectors)
                if not used_any:
                    msg = (
                        "详情页正文未使用 probe_detail_page 返回的任一候选选择器。"
                        "请从 probe 的 contentCandidates 中任选一个作为正文容器，不要使用泛化的 article/main。"
                    )
                    issues.append(
                        CriticIssue(
                            "error",
                            "detail_body_selector_mismatch",
                            msg,
                        )
                    )
        return issues

    async def _run_lightweight_execution(
        self,
        code: str,
        executor_session,
        timeout_sec: int,
        max_items: int,
    ) -> Dict[str, Any]:
        started = time.time()
        wrapper = self._build_lightweight_wrapper(code=code, max_items=max_items)
        stdout = ""
        stderr = ""
        timed_out = False
        exit_code = 0
        exec_error: Optional[Dict[str, Any]] = None
        captured_payload: Any = None
        artifact_ref = None

        if executor_session is not None:
            result = await executor_session.run_python(wrapper, timeout_sec=timeout_sec)
            payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
            stdout = payload.get("stdout") or ""
            stderr = payload.get("stderr") or ""
            timed_out = bool(payload.get("timed_out", False))
            exit_code = int(payload.get("exit_code", 0) or 0)
        else:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".py", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                tmp.write(wrapper)
            try:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable,
                        str(tmp_path),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
                        stdout = out_b.decode("utf-8", errors="replace") if out_b else ""
                        stderr = err_b.decode("utf-8", errors="replace") if err_b else ""
                        exit_code = int(proc.returncode or 0)
                    except asyncio.TimeoutError:
                        timed_out = True
                        proc.kill()
                        out_b, err_b = await proc.communicate()
                        stdout = out_b.decode("utf-8", errors="replace") if out_b else ""
                        stderr = err_b.decode("utf-8", errors="replace") if err_b else ""
                        exit_code = 124
                except Exception as exc:
                    exec_error = {"error": f"local_subprocess_unavailable: {exc}"}
                    stderr = str(exc)
                    exit_code = 1
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

        cleaned_lines: List[str] = []
        for line in stdout.splitlines():
            if line.startswith("__PYGEN_DIAG_CAPTURE__"):
                try:
                    parsed = json.loads(line.replace("__PYGEN_DIAG_CAPTURE__", "", 1))
                    captured_payload = parsed.get("payload")
                except Exception:
                    pass
                continue
            if line.startswith("__PYGEN_DIAG_ERROR__"):
                try:
                    exec_error = json.loads(line.replace("__PYGEN_DIAG_ERROR__", "", 1))
                except Exception:
                    exec_error = {"error": "failed_to_parse_exec_error_marker", "raw": _trim_text(line, 500)}
                continue
            cleaned_lines.append(line)
        cleaned_stdout = "\n".join(cleaned_lines)

        records = _extract_records(captured_payload)
        record_count = len(records)
        date_filled = sum(1 for rec in records if isinstance(rec.get("date"), str) and rec.get("date", "").strip())
        date_fill_rate = (date_filled / record_count) if record_count else 0.0

        signals = ExecutionSignals()
        signals.stdout = cleaned_stdout
        signals.stderr = stderr
        signals.exit_code = exit_code
        signals.output_record_count = record_count
        signals.date_fill_rate = date_fill_rate
        signals.duration_seconds = max(0.0, time.time() - started)
        if exec_error and exec_error.get("error"):
            signals.exceptions.append(str(exec_error.get("error")))
            if exec_error.get("traceback"):
                signals.exceptions.append(_trim_text(str(exec_error.get("traceback")), 1200))

        collector = SignalsCollector()
        collector._analyze_output(signals)
        signals.status = ExecutionStatus.TIMEOUT if timed_out else collector._determine_status(signals)

        if self.artifact_store and (len(cleaned_stdout) > 3000 or len(stderr) > 3000):
            try:
                ref = self.artifact_store.put_json(
                    {
                        "stdout": cleaned_stdout,
                        "stderr": stderr,
                        "exec_error": exec_error,
                        "captured_payload": captured_payload,
                    },
                    prefix="critic_runtime",
                )
                artifact_ref = ref.to_prompt_dict()
            except Exception:
                artifact_ref = None

        execution_success = (not timed_out) and (not exec_error) and exit_code == 0 and record_count > 0
        return {
            "execution_success": execution_success,
            "timed_out": timed_out,
            "exit_code": exit_code,
            "duration_seconds": round(max(0.0, time.time() - started), 3),
            "payload": captured_payload,
            "records": records,
            "record_count": record_count,
            "date_fill_rate": date_fill_rate,
            "stdout_tail": _trim_text(cleaned_stdout, 800),
            "stderr_tail": _trim_text(stderr, 800),
            "signals": signals,
            "http_statuses": [s.status_code for s in (signals.http_signals or []) if getattr(s, "status_code", 0)],
            "artifact_ref": artifact_ref,
        }

    def _build_lightweight_wrapper(self, code: str, max_items: int) -> str:
        encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
        return f"""
import base64
import json
import traceback

MAX_ITEMS = {max_items}
_captured_payload = None
_orig_json_dump = json.dump

def _shrink_value(value):
    if isinstance(value, str):
        return value[:400]
    if isinstance(value, list):
        return [_shrink_value(v) for v in value[:MAX_ITEMS]]
    if isinstance(value, dict):
        out = {{}}
        for idx, (k, v) in enumerate(value.items()):
            if idx >= 80:
                break
            out[k] = _shrink_value(v)
        return out
    return value

def _limit_payload(obj):
    if isinstance(obj, list):
        return [_shrink_value(item) for item in obj[:MAX_ITEMS]]
    if isinstance(obj, dict):
        clone = {{}}
        for k, v in obj.items():
            if isinstance(v, list) and k in ("reports", "articles", "news", "items", "data"):
                clone[k] = [_shrink_value(item) for item in v[:MAX_ITEMS]]
            else:
                clone[k] = _shrink_value(v)
        return clone
    return _shrink_value(obj)

def _patched_dump(obj, fp, *args, **kwargs):
    global _captured_payload
    limited = _limit_payload(obj)
    _captured_payload = limited
    return _orig_json_dump(limited, fp, *args, **kwargs)

json.dump = _patched_dump
exec_err = None
try:
    source = base64.b64decode("{encoded}").decode("utf-8")
    ns = {{"__name__": "__main__", "__file__": "generated_script.py"}}
    exec(compile(source, "generated_script.py", "exec"), ns, ns)
except Exception as e:
    exec_err = {{
        "error": str(e),
        "traceback": traceback.format_exc(),
    }}
finally:
    json.dump = _orig_json_dump
    if exec_err:
        print("__PYGEN_DIAG_ERROR__" + json.dumps(exec_err, ensure_ascii=False))
    print("__PYGEN_DIAG_CAPTURE__" + json.dumps({{"payload": _captured_payload}}, ensure_ascii=False))
"""

    def _choose_top_two_causes(
        self,
        failure_report,
        static_issues: List[CriticIssue],
        runtime_result: Dict[str, Any],
        excluded_primary_causes: set[str],
    ) -> Tuple[str, str]:
        primary = str(getattr(failure_report.failure_type, "value", FailureType.UNKNOWN.value) or FailureType.UNKNOWN.value)
        backup = FailureType.UNKNOWN.value

        mapped_candidates: List[str] = []
        for issue in static_issues:
            mapped = CODE_ISSUE_TO_CAUSE.get(issue.code)
            if mapped:
                mapped_candidates.append(mapped)

        if primary in excluded_primary_causes:
            for candidate in mapped_candidates:
                if candidate and candidate not in excluded_primary_causes:
                    primary = candidate
                    break
            else:
                primary = FailureType.UNKNOWN.value

        for candidate in mapped_candidates:
            if candidate != primary:
                backup = candidate
                break

        if backup == FailureType.UNKNOWN.value:
            if runtime_result.get("timed_out"):
                backup = FailureType.TIMEOUT.value
            elif runtime_result.get("record_count", 0) == 0:
                backup = FailureType.EMPTY_OUTPUT.value
            elif runtime_result.get("http_statuses"):
                backup = FailureType.NETWORK_ERROR.value

        if backup == primary:
            backup = FailureType.UNKNOWN.value
        return primary, backup

    def _assess_quality(self, records: List[Dict[str, Any]], run_mode: str, min_items: int) -> Dict[str, Any]:
        required = RUN_MODE_REQUIRED_FIELDS.get(run_mode, tuple())
        count = len(records)
        field_fill: Dict[str, float] = {}
        for field in required:
            if count == 0:
                field_fill[field] = 0.0
            else:
                non_empty = sum(
                    1
                    for rec in records
                    if isinstance(rec.get(field), str) and rec.get(field, "").strip()
                )
                field_fill[field] = non_empty / count

        required_ok = all(rate > 0 for rate in field_fill.values()) if required else True
        meets_min_items = count >= max(1, int(min_items))
        recommendations: List[str] = []
        if not meets_min_items:
            recommendations.append(f"Extract at least {max(1, int(min_items))} items in lightweight validation.")
        if not required_ok:
            missing = [k for k, v in field_fill.items() if v <= 0]
            recommendations.append(f"Ensure required fields are populated: {missing}")

        return {
            "record_count": count,
            "required_fields": list(required),
            "field_fill_rate": field_fill,
            "required_fields_ok": required_ok,
            "meets_min_items": meets_min_items,
            "recommendations": recommendations,
            "sample_records": records[:3],
        }

    async def _run_minimal_experiment(
        self,
        primary_cause: str,
        run_mode: str,
        target_url: str,
        runtime_result: Dict[str, Any],
        code: str,
        executor_session,
    ) -> Dict[str, Any]:
        if primary_cause in NETWORK_LIKE_CAUSES and target_url:
            probe_script = f"""
import json
import urllib.request
import urllib.error

url = {json.dumps(target_url)}
req = urllib.request.Request(url, headers={{"User-Agent": "Mozilla/5.0 (PyGenCriticProbe)"}})
out = {{"ok": False, "status": None, "error": None, "challenge_detected": False}}
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read(2048).decode("utf-8", errors="ignore").lower()
        out["status"] = getattr(resp, "status", None)
        out["ok"] = True
        out["challenge_detected"] = any(k in body for k in ["captcha", "forbidden", "access denied", "challenge", "waf"])
except Exception as e:
    out["error"] = str(e)
print(json.dumps(out, ensure_ascii=False))
"""
            probe = await self._run_probe_script(probe_script, executor_session=executor_session, timeout_sec=25)
            return {
                "type": "network_probe",
                "cause": primary_cause,
                "result": probe,
            }

        records = runtime_result.get("records") or []
        if records:
            required = RUN_MODE_REQUIRED_FIELDS.get(run_mode, tuple())
            fill = {}
            for field in required:
                fill[field] = sum(
                    1
                    for rec in records[:5]
                    if isinstance(rec.get(field), str) and rec.get(field, "").strip()
                )
            return {
                "type": "sample_output_probe",
                "cause": primary_cause,
                "result": {
                    "sample_size": min(len(records), 5),
                    "required_non_empty_count": fill,
                    "sample": records[:3],
                },
            }

        return {
            "type": "selector_brittleness_probe",
            "cause": primary_cause,
            "result": {
                "select_calls": len(re.findall(r"\.select\(", code)),
                "find_calls": len(re.findall(r"\.find\(", code)),
                "has_chained_find": ".find(" in code and ").find(" in code,
            },
        }

    async def _run_probe_script(self, code: str, executor_session, timeout_sec: int) -> Dict[str, Any]:
        if executor_session is not None:
            result = await executor_session.run_python(code, timeout_sec=timeout_sec)
            payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
            if payload.get("success"):
                parsed = _extract_json_object(payload.get("stdout", ""))
                return parsed or {
                    "ok": False,
                    "error": "probe_parse_failed",
                    "stdout": _trim_text(payload.get("stdout", ""), 600),
                }
            return {
                "ok": False,
                "error": payload.get("error") or "probe_execution_failed",
                "stderr": _trim_text(payload.get("stderr", ""), 600),
            }

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".py", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(code)
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(tmp_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as exc:
                return {"ok": False, "error": f"probe_subprocess_unavailable: {exc}"}
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return {"ok": False, "error": "probe_timeout"}
            stdout = out_b.decode("utf-8", errors="replace") if out_b else ""
            stderr = err_b.decode("utf-8", errors="replace") if err_b else ""
            if proc.returncode == 0:
                parsed = _extract_json_object(stdout)
                return parsed or {"ok": False, "error": "probe_parse_failed", "stdout": _trim_text(stdout, 600)}
            return {"ok": False, "error": "probe_nonzero_exit", "stderr": _trim_text(stderr, 600)}
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    async def _llm_adjudicate_cause(
        self,
        code: str,
        run_mode: str,
        objective: str,
        primary_cause: str,
        backup_cause: str,
        evidence: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not self.llm_agent:
            return None
        allowed_causes = [item.value for item in FailureType]
        evidence_preview = json.dumps(evidence[-6:], ensure_ascii=False, default=str)
        prompt = load_prompt(
            "critic/fault_adjudicate.md",
            allowed_causes=allowed_causes,
            primary_cause=primary_cause,
            backup_cause=backup_cause,
            run_mode=run_mode,
            objective=objective,
            evidence_preview=evidence_preview,
        )
        adjudicate_system_prompt = load_prompt("critic/fault_adjudicate_system.md").strip()
        try:
            response = await asyncio.to_thread(
                self.llm_agent._call_llm,  # type: ignore[attr-defined]
                adjudicate_system_prompt,
                prompt,
                None,
                0.1,
            )
            parsed = _extract_json_object(response)
            if not parsed:
                return None
            cause = str(parsed.get("cause", "")).strip()
            if cause not in allowed_causes:
                return None
            return {
                "used": True,
                "cause": cause,
                "confidence": float(parsed.get("confidence", 0.5)),
                "reason": str(parsed.get("reason", "")),
            }
        except Exception as exc:
            return {"used": False, "error": str(exc)}

    async def _llm_repair_once(
        self,
        code: str,
        run_mode: str,
        objective: str,
        primary_cause: str,
        backup_cause: str,
        evidence: List[Dict[str, Any]],
        round_index: int,
        strategy_hint: str,
    ) -> Optional[str]:
        if not self.llm_agent:
            return None
        evidence_preview = json.dumps(evidence[-8:], ensure_ascii=False, default=str)
        repair_prompt = load_prompt(
            "critic/repair_round.md",
            round_index=round_index,
            primary_cause=primary_cause,
            backup_cause=backup_cause,
            run_mode=run_mode,
            objective=objective,
            strategy_hint=strategy_hint,
            evidence_preview=evidence_preview,
            code=code,
        )
        try:
            system_prompt = self.llm_agent._build_system_prompt(run_mode=run_mode)  # type: ignore[attr-defined]
        except Exception:
            system_prompt = load_prompt("critic/repair_fallback_system.md").strip()
        try:
            response = await asyncio.to_thread(
                self.llm_agent._call_llm,  # type: ignore[attr-defined]
                system_prompt,
                repair_prompt,
                None,
                0.1,
            )
            if hasattr(self.llm_agent, "_extract_code_from_response"):
                new_code = await asyncio.to_thread(self.llm_agent._extract_code_from_response, response)  # type: ignore[attr-defined]
            else:
                new_code = response
            new_code = (new_code or "").strip()
            return new_code if new_code else None
        except Exception:
            return None

    def _build_pass_verdict(
        self,
        summary: str,
        confidence: float,
        recommendations: List[str],
        details: Dict[str, Any],
    ) -> CriticVerdict:
        return CriticVerdict(
            passed=True,
            summary=summary,
            confidence=max(0.0, min(1.0, confidence)),
            issues=[],
            recommendations=recommendations[:8],
            details=self._maybe_attach_artifact(details, prefix="critic_pass"),
        )

    def _build_fail_verdict(
        self,
        summary: str,
        confidence: float,
        primary_cause: str,
        backup_cause: str,
        issues: List[CriticIssue],
        recommendations: List[str],
        details: Dict[str, Any],
    ) -> CriticVerdict:
        normalized_issues = issues[:] if issues else [CriticIssue("error", "critic_failed", summary)]
        normalized_issues.insert(0, CriticIssue("error", "primary_cause", f"Primary cause: {primary_cause}"))
        normalized_issues.insert(1, CriticIssue("warning", "backup_cause", f"Backup cause: {backup_cause}"))
        details = dict(details)
        details["primary_cause"] = primary_cause
        details["backup_cause"] = backup_cause
        return CriticVerdict(
            passed=False,
            summary=summary,
            confidence=max(0.0, min(1.0, confidence)),
            issues=normalized_issues[:12],
            recommendations=recommendations[:10],
            details=self._maybe_attach_artifact(details, prefix="critic_fail"),
        )

    def _maybe_attach_artifact(self, details: Dict[str, Any], prefix: str) -> Dict[str, Any]:
        if not self.artifact_store:
            return details
        text = json.dumps(details, ensure_ascii=False, default=str)
        if len(text) <= 9000:
            return details
        try:
            ref = self.artifact_store.put_json(details, prefix=prefix)
            return {
                "details_truncated": True,
                "artifact_ref": ref.to_prompt_dict(),
                "preview": _trim_text(text, 1200),
            }
        except Exception:
            return details
