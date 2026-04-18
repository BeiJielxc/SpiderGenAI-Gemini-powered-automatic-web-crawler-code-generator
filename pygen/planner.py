"""
PyGen Planner - autonomous decision loop (ReAct style).

This planner now supports:
- Dynamic tool discovery each iteration.
- Re-plan triggers on repeated failures.
- Artifact-aware observations for large payloads.
- Optional critic gate before finish.
"""

from __future__ import annotations

import asyncio
import codecs
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests as http_requests
from openai import OpenAI

try:
    from artifact_store import ArtifactStore
except ImportError:
    from .artifact_store import ArtifactStore  # type: ignore

try:
    from critic_runtime import Critic
except ImportError:
    try:
        from .critic_runtime import Critic  # type: ignore
    except ImportError:
        try:
            from critic import Critic
        except ImportError:
            from .critic import Critic  # type: ignore

try:
    from executor_session import ExecutorSession
except ImportError:
    from .executor_session import ExecutorSession  # type: ignore

try:
    from tool_registry import ToolRegistry, create_default_tool_registry
except ImportError:
    from .tool_registry import ToolRegistry, create_default_tool_registry  # type: ignore

try:
    from tools import ToolContext, ToolResult
except ImportError:
    from .tools import ToolContext, ToolResult  # type: ignore

try:
    from prompts import load as load_prompt
except ImportError:
    from .prompts import load as load_prompt  # type: ignore


class PlannerResult:
    """Planner execution result."""

    def __init__(self):
        self.success: bool = False
        self.script_code: Optional[str] = None
        self.enhanced_analysis: Dict[str, Any] = {}
        self.verified_mapping: Optional[Dict[str, Any]] = None
        self.strategy_summary: str = ""
        self.error: Optional[str] = None
        self.iterations: int = 0
        self.tool_calls: List[Dict[str, Any]] = []


# System prompt lives in prompts/planner/system.md and is loaded lazily via
# PromptLoader.  Kept here as a function so callers can still `from planner
# import PLANNER_SYSTEM_PROMPT` when they need the raw template string.
def _get_planner_system_template() -> str:
    return load_prompt("planner/system.md")


PLANNER_SYSTEM_PROMPT = _get_planner_system_template()

RUN_MODE_HINTS = {
    "enterprise_report": (
        "Target output fields should include: name, date, downloadUrl, fileType."
    ),
    "news_sentiment": (
        "Target output fields should include: title, date, source, sourceUrl, summary."
    ),
}


class AgentPlanner:
    """Autonomous planner with dynamic tool ecosystem."""

    def __init__(
        self,
        browser,
        config,
        llm_agent,
        url: str,
        run_mode: str,
        start_date: str,
        end_date: str,
        extra_requirements: str = "",
        task_id: str = "",
        log_callback=None,
        attachments=None,
        max_iterations: int = 20,
        tool_registry: Optional[ToolRegistry] = None,
        artifact_store: Optional[ArtifactStore] = None,
        critic: Optional[Critic] = None,
        executor_session=None,
        replan_failure_threshold: int = 2,
        replan_repeat_threshold: int = 3,
        cancel_check: Optional[Callable[[], bool]] = None,
    ):
        self.config = config
        self.max_iterations = max_iterations
        self.log = log_callback or (lambda msg: None)
        self._cancel_check = cancel_check or (lambda: False)

        self._critic_rejected_current_code = False

        self._api_key = config.qwen_api_key
        self._model = config.qwen_model
        self._base_url = config.qwen_base_url
        self._provider = self._detect_provider()

        self._openai_client = None
        if self._provider == "openai":
            self._openai_client = OpenAI(api_key=self._api_key, base_url=self._base_url)

        if artifact_store is None:
            artifact_root = Path(__file__).parent / "output" / "artifacts"
            artifact_store = ArtifactStore(artifact_root)

        if critic is None:
            critic = Critic(llm_agent=llm_agent, artifact_store=artifact_store, max_retries=3)

        self._owns_executor_session = False
        if executor_session is None:
            sandbox_root = Path(__file__).parent / "sandbox_sessions" / (task_id or "default")
            backend = config.sandbox_backend if config else "docker"
            auto_start = config.sandbox_auto_start if config else True
            persistent = config.sandbox_persistent_session if config else True
            docker_image = config.sandbox_docker_image if config else None
            docker_auto_pull = config.sandbox_docker_auto_pull if config else True
            docker_disable_network = config.sandbox_docker_disable_network if config else False
            docker_mount_workdir = config.sandbox_docker_mount_workdir if config else True
            executor_session = ExecutorSession(
                workdir=sandbox_root,
                auto_start=auto_start,
                persistent=persistent,
                backend=backend,
                docker_image=docker_image,
                docker_auto_pull=docker_auto_pull,
                docker_disable_network=docker_disable_network,
                docker_mount_workdir=docker_mount_workdir,
            )
            self._owns_executor_session = True

        self.ctx = ToolContext(
            browser=browser,
            config=config,
            llm_agent=llm_agent,
            url=url,
            run_mode=run_mode,
            start_date=start_date,
            end_date=end_date,
            extra_requirements=extra_requirements,
            task_id=task_id,
            log_callback=log_callback,
            attachments=attachments,
            artifact_store=artifact_store,
            executor_session=executor_session,
            critic=critic,
        )

        self.tool_registry = tool_registry or create_default_tool_registry()

        self._messages: List[Dict[str, str]] = []
        self._consecutive_failures = 0
        self._repeat_action_failures = 0
        self._last_action: Optional[str] = None
        self._replan_failure_threshold = max(1, replan_failure_threshold)
        self._replan_repeat_threshold = max(2, replan_repeat_threshold)

    def _detect_provider(self) -> str:
        model_lower = self._model.lower()
        base_lower = self._base_url.lower()
        if "gemini" in model_lower or "generativelanguage.googleapis.com" in base_lower:
            return "gemini"
        if "claude" in model_lower or "anthropic.com" in base_lower:
            return "claude"
        return "openai"

    def _call_llm_multi_turn(self, system_prompt: str, messages: List[Dict[str, str]], temperature: float = 0.3) -> str:
        if self._provider == "gemini":
            return self._call_gemini_multi(system_prompt, messages, temperature)
        if self._provider == "claude":
            return self._call_claude_multi(system_prompt, messages, temperature)
        return self._call_openai_multi(system_prompt, messages, temperature)

    def _call_openai_multi(self, system_prompt: str, messages: List[Dict[str, str]], temperature: float = 0.3) -> str:
        api_messages = [{"role": "system", "content": system_prompt}]
        for msg in messages:
            api_messages.append({"role": msg["role"], "content": msg["content"]})

        final_temperature = 1.0 if self._model == "kimi-k2.5" else temperature

        # Prefer strict JSON mode when supported.
        try:
            response = self._openai_client.chat.completions.create(
                model=self._model,
                messages=api_messages,
                temperature=final_temperature,
                response_format={"type": "json_object"},
            )
        except Exception:
            response = self._openai_client.chat.completions.create(
                model=self._model,
                messages=api_messages,
                temperature=final_temperature,
            )

        return response.choices[0].message.content

    def _call_gemini_multi(self, system_prompt: str, messages: List[Dict[str, str]], temperature: float = 0.3) -> str:
        url = f"{self._base_url}models/{self._model}:streamGenerateContent?key={self._api_key}"

        contents = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})

        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": 4096,
                "responseMimeType": "application/json",
            },
        }

        resp = http_requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=120, stream=True)
        if resp.status_code != 200:
            raise Exception(f"Gemini API error {resp.status_code}: {resp.text[:300]}")

        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        for chunk in resp.iter_content(chunk_size=None):
            if chunk:
                buffer += decoder.decode(chunk, final=False)
        buffer += decoder.decode(b"", final=True)

        try:
            results = json.loads(buffer.strip())
        except json.JSONDecodeError:
            fixed = buffer.strip()
            if not fixed.endswith("]"):
                fixed += "]"
            results = json.loads(fixed)

        full_text = ""
        for item in results:
            for cand in item.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    full_text += part.get("text", "")

        if not full_text:
            raise Exception("Gemini returned empty response")
        return full_text

    def _call_claude_multi(self, system_prompt: str, messages: List[Dict[str, str]], temperature: float = 0.3) -> str:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
        }

        payload = {
            "model": self._model,
            "max_tokens": 4096,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        }

        resp = http_requests.post(url, headers=headers, json=payload, timeout=120)
        if resp.status_code != 200:
            raise Exception(f"Claude API error {resp.status_code}: {resp.text[:300]}")

        content = resp.json().get("content", [])
        return "".join([block.get("text", "") for block in content if block.get("type") == "text"])

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        text = text.strip()

        if text.startswith("```"):
            lines = text.split("\n")
            start = 1
            end = len(lines)
            for idx in range(len(lines) - 1, 0, -1):
                if lines[idx].strip().startswith("```"):
                    end = idx
                    break
            text = "\n".join(lines[start:end]).strip()

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        depth = 0
        start_idx = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start_idx = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start_idx >= 0:
                    try:
                        maybe = json.loads(text[start_idx : i + 1])
                        if isinstance(maybe, dict):
                            return maybe
                    except json.JSONDecodeError:
                        start_idx = -1
                        continue

        return None

    def _build_system_prompt(self) -> str:
        tools_desc = self.tool_registry.get_tools_prompt(self.ctx)
        return load_prompt(
            "planner/system.md",
            tools_description=tools_desc,
            max_iterations=self.max_iterations,
        )

    def _build_initial_message(self) -> str:
        run_mode_hint = RUN_MODE_HINTS.get(self.ctx.run_mode, "")
        msg = (
            f"## Task\n"
            f"Generate a crawler script for: {self.ctx.url}\n\n"
            f"## Parameters\n"
            f"- Run mode: {self.ctx.run_mode}\n"
            f"- Date range: {self.ctx.start_date} ~ {self.ctx.end_date}\n"
        )
        if run_mode_hint:
            msg += f"- Mode hint: {run_mode_hint}\n"
        if self.ctx.extra_requirements:
            msg += f"- Task objective (highest priority): {self.ctx.extra_requirements}\n"
        msg += "\nStart by opening and analyzing the page. Respond with JSON only."
        return msg

    def _format_observation(self, action: str, tool_result: ToolResult, iteration: int) -> str:
        payload: Dict[str, Any] = {
            "kind": "tool_observation",
            "iteration": iteration,
            "action": action,
            "success": tool_result.success,
            "summary": tool_result.summary,
            "error": tool_result.error,
            "error_code": tool_result.error_code,
            "retryable": tool_result.retryable,
            "recoverable": tool_result.recoverable,
            "suggested_next_tools": tool_result.suggested_next_tools,
            "confidence": tool_result.confidence,
        }

        if tool_result.artifacts:
            payload["artifacts"] = tool_result.artifacts

        if tool_result.data is not None:
            try:
                data_text = json.dumps(tool_result.data, ensure_ascii=False, default=str)
            except Exception:
                data_text = str(tool_result.data)

            if len(data_text) > 3000:
                payload["data_preview"] = data_text[:3000] + "... (truncated)"
                if self.ctx.artifact_store:
                    try:
                        ref = self.ctx.artifact_store.put_json(tool_result.data, prefix=f"{action}_iter{iteration}")
                        payload["data_artifact_ref"] = ref.to_prompt_dict()
                    except Exception as exc:
                        payload["artifact_store_error"] = str(exc)
            else:
                payload["data"] = tool_result.data

        return json.dumps(payload, ensure_ascii=False)

    def _build_replan_message(self, action: str, tool_result: ToolResult) -> Optional[str]:
        if tool_result.success:
            self._consecutive_failures = 0
            self._repeat_action_failures = 0
            self._last_action = action
            return None

        self._consecutive_failures += 1
        if action == self._last_action:
            self._repeat_action_failures += 1
        else:
            self._repeat_action_failures = 1
        self._last_action = action

        need_replan = (
            self._consecutive_failures >= self._replan_failure_threshold
            or self._repeat_action_failures >= self._replan_repeat_threshold
            or tool_result.error_code in {"unknown_tool", "invalid_params", "tool_unavailable"}
        )

        if not need_replan:
            return None

        fallback = self.tool_registry.get_fallback_tools(action, tool_result)
        if not fallback:
            fallback = self.tool_registry.list_tool_names(self.ctx)[:5]

        return (
            "[REPLAN_REQUIRED] "
            f"Recent tool execution failed (action={action}, error_code={tool_result.error_code}). "
            "Do not repeat the same failed action immediately. "
            f"Choose an alternative tool path. Suggested tools: {fallback}."
        )

    async def _critic_gate(self) -> Optional[str]:
        if not self.ctx.critic or not self.ctx.generated_code:
            return None

        if hasattr(self.ctx.critic, "evaluate_generated_code_async"):
            verdict = await self.ctx.critic.evaluate_generated_code_async(
                code=self.ctx.generated_code,
                run_mode=self.ctx.run_mode,
                objective=self.ctx.extra_requirements,
                min_items=1,
                target_url=self.ctx.url,
                max_retries=3,
                executor_session=self.ctx.executor_session,
                enhanced_analysis=getattr(self.ctx, "enhanced_analysis", None),
            )
        else:
            verdict = self.ctx.critic.evaluate_generated_code(
                code=self.ctx.generated_code,
                run_mode=self.ctx.run_mode,
                objective=self.ctx.extra_requirements,
                min_items=1,
            )

        repaired_code = None
        details = verdict.details if hasattr(verdict, "details") and isinstance(verdict.details, dict) else {}

        if details:
            maybe_repaired = details.get("repaired_code")
            if isinstance(maybe_repaired, str) and maybe_repaired.strip():
                repaired_code = maybe_repaired

            rounds = details.get("rounds") or []
            for rd in rounds:
                ri = rd.get("round", "?")
                passed = rd.get("passed", False)
                rc = rd.get("record_count", "?")
                cause = rd.get("primary_cause", "")
                conf = rd.get("classifier_confidence", "")
                status = "PASS" if passed else "FAIL"
                self.log(f"[CRITIC] Round {ri}: {status} | records={rc} | cause={cause} | confidence={conf}")

            evidence = details.get("evidence") or []
            for ev in evidence:
                step = ev.get("step", "")
                result = ev.get("result") or {}
                if step == "lightweight_runtime":
                    self.log(
                        f"[CRITIC] Runtime: success={result.get('execution_success')} "
                        f"timed_out={result.get('timed_out')} "
                        f"duration={result.get('duration_seconds', '?')}s "
                        f"records={result.get('record_count', '?')} "
                        f"exit_code={result.get('exit_code', '?')}"
                    )
                    stderr_tail = result.get("stderr_tail", "")
                    if stderr_tail:
                        self.log(f"[CRITIC] stderr: {str(stderr_tail)[:300]}")

        if repaired_code and repaired_code != self.ctx.generated_code:
            self.ctx.generated_code = repaired_code
            self.log("[PLANNER] Critic applied repaired code to context")

        if verdict.passed:
            self.log(f"[PLANNER] Critic passed: {verdict.summary}")
            return None

        self._critic_rejected_current_code = True
        self.log(f"[PLANNER] Critic rejected finish: {verdict.summary}")
        self.log(f"[CRITIC] Issues: {[i.code for i in verdict.issues]}")
        self.log(f"[CRITIC] Recommendations: {verdict.recommendations[:3]}")
        return (
            "[REPLAN_REQUIRED] Critic rejected finish. "
            f"Summary: {verdict.summary}. "
            f"Issues: {[i.code for i in verdict.issues]}. "
            f"Recommendations: {verdict.recommendations}."
        )

    async def run(self) -> PlannerResult:
        result = PlannerResult()
        try:
            self._messages = [{"role": "user", "content": self._build_initial_message()}]
            self.log(f"[PLANNER] Start Agent Loop (model={self._model}, max_iter={self.max_iterations})")

            for iteration in range(1, self.max_iterations + 1):
                if self._cancel_check():
                    self.log("[PLANNER] Task cancelled by user")
                    result.error = "任务已被用户取消"
                    break

                result.iterations = iteration
                self.log(f"[PLANNER] --- Iteration {iteration}/{self.max_iterations} ---")

                system_prompt = self._build_system_prompt()

                try:
                    llm_output = await asyncio.to_thread(
                        self._call_llm_multi_turn, system_prompt, self._messages, 0.3
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.log(f"[PLANNER] LLM call failed: {exc}")
                    result.error = f"LLM call failed at iteration {iteration}: {exc}"
                    break

                parsed = self._extract_json(llm_output)
                if not parsed:
                    self.log("[PLANNER] Invalid JSON from model; asking for retry")
                    self._messages.append({"role": "assistant", "content": llm_output})
                    self._messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your response was not valid JSON. "
                                "Respond with one JSON object: "
                                '{"thought":"...","action":"tool_name","action_input":{}}'
                            ),
                        }
                    )
                    continue

                thought = parsed.get("thought", "")
                action = parsed.get("action", "")
                action_input = parsed.get("action_input", {})
                if not isinstance(action_input, dict):
                    action_input = {}

                self.log(f"[PLANNER] Thought: {str(thought)[:200]}")
                if action == "finish":
                    self.log(f"[PLANNER] Action: finish and submit code to critic")
                else:
                    self.log(f"[PLANNER] Action: {action}")

                self._messages.append({"role": "assistant", "content": llm_output})

                if action == "finish":
                    if not self.ctx.generated_code:
                        self._messages.append(
                            {
                                "role": "user",
                                "content": "Finish rejected: no generated code exists yet. Generate code first.",
                            }
                        )
                        continue

                    critic_feedback = await self._critic_gate()
                    if critic_feedback:
                        self._messages.append({"role": "user", "content": critic_feedback})
                        continue

                    self.log("[PLANNER] Finish accepted")
                    result.success = True
                    result.script_code = self.ctx.generated_code
                    result.enhanced_analysis = self.ctx.enhanced_analysis
                    result.verified_mapping = self.ctx.verified_mapping
                    result.strategy_summary = str(thought)
                    break

                if not action:
                    self._messages.append(
                        {
                            "role": "user",
                            "content": (
                                "No action specified. Respond with: "
                                '{"thought":"...","action":"tool_name","action_input":{}}'
                            ),
                        }
                    )
                    continue

                self.log(f"[PLANNER] Execute tool: {action}({json.dumps(action_input, ensure_ascii=False)[:200]})")
                tool_result = await self.tool_registry.execute_tool(self.ctx, action, action_input)

                if action == "generate_crawler_code" and tool_result.success:
                    self._critic_rejected_current_code = False

                result.tool_calls.append(
                    {
                        "iteration": iteration,
                        "action": action,
                        "action_input": action_input,
                        "success": tool_result.success,
                        "summary": tool_result.summary,
                        "error_code": tool_result.error_code,
                        "suggested_next_tools": tool_result.suggested_next_tools,
                    }
                )

                observation = self._format_observation(action, tool_result, iteration)
                self._messages.append({"role": "user", "content": observation})

                replan_msg = self._build_replan_message(action, tool_result)
                if replan_msg:
                    self.log(f"[PLANNER] Trigger re-plan: {replan_msg}")
                    self._messages.append({"role": "user", "content": replan_msg})

            else:
                self.log(f"[PLANNER] Max iterations reached ({self.max_iterations})")
                if self.ctx.generated_code and not self._critic_rejected_current_code:
                    result.success = True
                    result.script_code = self.ctx.generated_code
                    result.enhanced_analysis = self.ctx.enhanced_analysis
                    result.verified_mapping = self.ctx.verified_mapping
                    result.strategy_summary = "Max iterations reached; using last generated code"
                elif self._critic_rejected_current_code:
                    result.error = (
                        "Max iterations reached; generated code was rejected by Critic "
                        "(records=0 or quality check failed)"
                    )
                else:
                    result.error = "Max iterations reached without generating code"

            if result.success:
                self.log(
                    f"[PLANNER] Task completed: {result.iterations} iterations, "
                    f"{len(result.tool_calls)} tool calls"
                )
            else:
                self.log(f"[PLANNER] Task failed: {result.error}")

            return result
        finally:
            if self._owns_executor_session and self.ctx.executor_session is not None:
                try:
                    await self.ctx.executor_session.close(force=True)
                except BaseException as exc:
                    self.log(f"[PLANNER] Failed to close owned executor session: {exc}")
