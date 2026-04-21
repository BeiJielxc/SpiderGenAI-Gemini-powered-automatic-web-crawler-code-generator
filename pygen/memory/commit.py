"""Stage-2 commit: enrich a draft episode with LLM-generated lessons,
move it into ``episode/episodes.jsonl``, then update the site profile.

This module is the *only* place that calls a real LLM in the persistent
memory subsystem. The Stage-1 :mod:`pygen.agents.summarize_node` runs
zero tokens; everything LLM-shaped happens here, gated by user feedback.

Why the LLM call lives at commit time rather than at task end
-------------------------------------------------------------
Vague business-user feedback like "只爬到一堆图标" is the *primary signal*
we want the LLM to interpret. Translating that into a technical root
cause requires the user's plain English, the auto_findings, the tool
log, and a snippet of the generated code — all together. Splitting the
analysis (rules now, LLM later) lets us:

* Show the auto_findings to the user *before* they verdict the run, so
  their judgment is informed by what the model already noticed.
* Run the LLM exactly once, with all the right inputs in scope.

Triple fallback
---------------
The commit pipeline never raises. We aim for "lessons in episode" but
degrade gracefully:

1. **LLM success** → full ``lessons`` block (failure_analysis when
   verdict=wrong; optimization + site_traits always).
2. **LLM failure** → ``lessons`` carries auto_findings → optimization
   conversion + raw user_suggestion in failure_analysis.user_complaint_raw.
3. **I/O failure during commit** → episode is written without lessons,
   site profile update is attempted on best-effort, never throws.

Toggle ``memory.summary_agent.use_llm = false`` to short-circuit to (2)
and ``skip_llm_when_correct = true`` to avoid the LLM call on
verdict=correct (saves tokens at the cost of optimization insight).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from .episode import VALID_VERDICTS, Episode
from .site_profile import SiteProfile, update_profile_from_episode
from .store import MemoryStore


# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------


_DEFAULT_SYSTEM_PROMPT = """\
你就是刚刚执行这次爬虫任务的那个模型本人。现在是任务跑完后的复盘环节。

下面会给你完整的：你刚才收到的原始任务说明、你按时序调用过的工具、你最终
生成的代码、规则启发式扫描出的"嫌疑列表"（auto_findings，**仅供参考**），
以及业务方的简短反馈（可能很模糊或为空）。

你的任务：站在"我刚才是怎么决策的"这个第一人称视角，输出严格 JSON 形式的
lessons。

关键原则：
1. **auto_findings 是规则启发式扫描，可能误判**。请结合代码 + 工具序列验证；
   若证据不足以支持某条 finding，就在 lessons 中**忽略它**，不要照搬。
2. 用户反馈可能是非技术语言（例如"只爬到一堆图标"），你需要结合代码与日志
   推断真实根因。
3. 即便用户标 verdict=correct，也要从工具调用序列中找出冗余/可省步骤，写到
   optimization；如果代码确实毫无问题再写一句"无明显冗余"也可以。
4. **slot 级精确归因（slot_verdicts）**：任务级 verdict=wrong 不代表所有 slot 都
   错。"标题对、正文是图标"只意味着 detail.content 错了。请逐个 slot 判断，
   写到 slot_verdicts；没强证据一律给 "unknown"，**不要为了显得'分析得全面'
   瞎填 correct/wrong**——错杀代价高（直接给那条 selector 计 -1，可能进黑名单）。
   任务里没用到的 slot 整个 key 可省略。
5. **严格输出 JSON**，不要有任何 markdown 围栏或自然语言前后缀。
6. lessons.failure_analysis 仅当 verdict=wrong 时输出对象，verdict=correct 时
   **必须为 null**。
7. lessons.optimization 永远输出 list[str]，每条 ≤ 80 个汉字；至少 1 条。
8. lessons.site_traits 永远输出 dict[str, str|bool|number]，没有信息给 {}。
9. 全部内容用中文。

JSON schema:
{
  "failure_analysis": null | {
      "user_complaint_interpreted": str,   // 把用户原话翻译成技术语言
      "root_cause_guess": str,              // 推断根因
      "fix_direction": str                  // 下次该怎么改
  },
  "optimization": [str, ...],
  "site_traits": {
      "platform": str,        // 例如 "WordPress + Elementor"
      "needs_playwright": bool,
      "pagination_pattern": str,
      ...                     // 任意键值对，但都要短
  },
  "slot_verdicts": {
      "list.container":      "correct" | "wrong" | "unknown",
      "list.title_link":     "correct" | "wrong" | "unknown",
      "list.title":          "correct" | "wrong" | "unknown",
      "list.date":           "correct" | "wrong" | "unknown",
      "list.next_page":      "correct" | "wrong" | "unknown",
      "detail.content":      "correct" | "wrong" | "unknown",
      "detail.title":        "correct" | "wrong" | "unknown",
      "detail.publish_date": "correct" | "wrong" | "unknown"
  }
}
"""


_USER_TEMPLATE = """\
verdict: {verdict}
domain: {domain}
url: {url}

## 我刚才收到的原始任务说明
{task_brief_block}

## 业务方反馈（用户原话，可能为空）
{suggestion_block}

## auto_findings（规则启发式扫描出的"嫌疑列表"，仅供参考，可以否决）
{findings_block}

## 任务事实包
{facts_block}

## 我按时序调用过的工具（最近 {tool_calls_count} 条）
{toolcalls_block}

## 我最终生成的代码
```python
{code_block}
```

请基于以上**全部**信息复盘自己的执行过程，按 system 中规定的 JSON schema 输出
lessons。**只输出 JSON**，不要有任何 markdown 围栏或自然语言前后缀。
"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def commit_episode(
    *,
    task_id: str,
    verdict: str,
    suggestion: str = "",
    store: MemoryStore,
    config: Any = None,
    log_callback: Optional[Callable[[str], None]] = None,
    model_factory: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """Promote a draft episode to committed status.

    Parameters
    ----------
    task_id : str
        ID of the draft sitting in ``episode/pending/<task_id>.json``.
    verdict : {"correct", "wrong"}
        Human verdict from the feedback Modal.
    suggestion : str
        Free-text user suggestion. Empty iff verdict == "correct".
    store : MemoryStore
        File-system facade; we read the draft, write to episode/episodes.jsonl,
        delete the draft, and update site/<domain>.json from here.
    config : Config, optional
        ``pygen.config.Config`` — used for memory.* / artifacts.* knobs.
    log_callback : callable, optional
        Used for diagnostic logging without raising.
    model_factory : callable, optional
        Returns a chat model (BaseChatModel-compatible). When omitted we
        try ``agents.llm.build_small_model`` with the configured alias.

    Returns
    -------
    A dict with shape::

        {
          "ok": bool,
          "stage": "no_draft" | "llm_success" | "llm_fallback" | "io_only",
          "episode": Episode | None,
          "profile": SiteProfile | None,
          "warnings": [str, ...],
        }

    Never raises — caller can rely on dict-shape inspection.
    """
    log = log_callback or (lambda _msg: None)
    warnings: List[str] = []
    out: Dict[str, Any] = {
        "ok": False,
        "stage": "no_draft",
        "episode": None,
        "profile": None,
        "warnings": warnings,
    }

    if verdict not in VALID_VERDICTS:
        warnings.append(f"invalid verdict: {verdict!r}")
        log(f"[COMMIT] 拒绝：非法 verdict={verdict!r}")
        return out

    log(
        f"[COMMIT] 开始处理用户评价 task_id={task_id} verdict={verdict} "
        f"suggestion_chars={len(suggestion or '')}"
    )

    draft = store.read_draft(task_id)
    if draft is None:
        warnings.append(f"no draft found for task_id={task_id!r}")
        log(
            f"[COMMIT] 未找到 draft（task_id={task_id}）— 可能 GC 已清理 / "
            f"任务从未跑成功 / 已经提交过。本次评价跳过 commit"
        )
        return out

    suggestion = (suggestion or "").strip()
    draft["user_verdict"] = verdict
    draft["user_suggestion"] = suggestion

    domain_for_log = draft.get("domain") or "?"
    auto_outcome_for_log = draft.get("auto_outcome") or "?"
    log(
        f"[COMMIT] 已加载 draft：domain={domain_for_log} "
        f"auto_outcome={auto_outcome_for_log} → 进入 Stage-2 LLM 复盘"
    )

    # ---- LLM enrichment (Stage 2 main act) ----
    use_llm = _get_bool(config, "memory_use_llm", default=True)
    skip_when_correct = _get_bool(config, "memory_skip_llm_when_correct", default=False)
    enrichment_stage = "llm_fallback"

    lessons: Optional[Dict[str, Any]] = None

    if use_llm and not (verdict == "correct" and skip_when_correct):
        log("[COMMIT] 调用 LLM 进行第一人称复盘（lessons / optimization / site_traits）...")
        try:
            lessons = _llm_enrich(
                draft,
                config=config,
                log=log,
                model_factory=model_factory,
            )
            if lessons is not None:
                enrichment_stage = "llm_success"
                log(
                    "[COMMIT] LLM 复盘完成："
                    f"lessons.keys={sorted(list(lessons.keys()))[:8]}"
                )
            else:
                warnings.append("LLM returned no parseable lessons; using fallback")
                log("[COMMIT] LLM 返回无法解析 — 回退到规则 lessons")
        except Exception as exc:  # pragma: no cover - defensive
            warnings.append(f"LLM enrichment raised: {exc.__class__.__name__}: {exc}")
            log(
                f"[COMMIT] LLM 复盘抛错（已降级到规则 fallback）："
                f"{exc.__class__.__name__}: {exc}"
            )
    else:
        log(
            f"[COMMIT] 跳过 LLM 复盘 "
            f"(use_llm={use_llm}, verdict={verdict}, skip_when_correct={skip_when_correct})"
        )

    if lessons is None:
        lessons = _fallback_lessons(draft)
        log("[COMMIT] 已生成规则 fallback lessons（无 LLM 增强）")

    draft["lessons"] = lessons
    out["episode"] = Episode.from_json(dict(draft))

    # ---- Write committed episode ----
    appended = store.append_committed_episode(draft)
    if not appended:
        warnings.append("append_committed_episode failed; site profile NOT updated")
        out["stage"] = "io_only"
        log("[COMMIT] episode 写盘失败 → 站点画像不更新（保持原状）")
        return out
    log(f"[COMMIT] episode 已写入 episode/episodes.jsonl (domain={domain_for_log})")

    # Best-effort: drop the draft after a successful append. If this fails
    # the GC will clean it up later (drafts are mtime-based).
    store.delete_draft(task_id)
    log(f"[COMMIT] draft 已删除：episode/pending/{task_id}.json")

    # ---- Site profile update (best-effort, never raises out) ----
    profile = _safe_update_profile(store, draft, config=config, log=log)
    if profile is not None:
        out["profile"] = profile
        confidence = profile.get("confidence", 0.0) if isinstance(profile, dict) else 0.0
        log(
            f"[COMMIT] 站点画像已更新 site/{domain_for_log}.json "
            f"(confidence={confidence:.2f})"
        )
    else:
        warnings.append("site profile update failed")
        log(f"[COMMIT] 站点画像更新失败（已忽略，episode 已落盘）")

    out["ok"] = True
    out["stage"] = enrichment_stage
    log(f"[COMMIT] 评价提交完成 ok=True stage={enrichment_stage}")
    return out


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def _llm_enrich(
    draft: Dict[str, Any],
    *,
    config: Any,
    log: Callable[[str], None],
    model_factory: Optional[Callable[[], Any]],
) -> Optional[Dict[str, Any]]:
    """Invoke the retrospective LLM and parse its JSON.

    Hole-1 fix — fallback chain at *invoke* time, not just *build* time:

    * If the caller passed an explicit ``model_factory``, we honour it as a
      single-shot (no fallback). Tests rely on this.
    * Otherwise we build a chain of ``(label, factory)`` candidates ordered
      by preference (default: task_model → small_model). We try each in
      sequence; the first one that *invokes successfully and returns
      parseable JSON* wins. A build failure, network error, timeout, or
      non-JSON response is treated as "this candidate failed; try the next".

    Why this matters: in production the task model (Gemini 3.1 Pro Preview)
    can return empty JSON or time out on long contexts, leaving Stage-2 with
    empty ``failure_analysis`` / ``site_traits``. Without a real fallback the
    next run inherits ZERO learnings — which is exactly the bug the user hit
    on Seczambia's second attempt.

    Returns ``None`` only if *every* candidate failed; never raises.
    """
    if model_factory is not None:
        candidates: List[tuple] = [("explicit", model_factory)]
    else:
        candidates = _build_factory_chain(draft, config=config, log=log)

    if not candidates:
        log("[MEMORY] commit LLM: no candidate model could be built")
        return None

    system_msg = _load_system_prompt()
    user_msg = _build_user_message(draft)
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]

    max_tokens = _get_int(config, "memory_summary_max_tokens", 0) or _get_int(
        config, "memory_small_model_max_tokens", 800,
    )
    timeout_sec = _get_int(config, "memory_summary_timeout_sec", 0) or _get_int(
        config, "memory_small_model_timeout_sec", 60,
    )

    last_failure: Optional[str] = None
    for idx, (label, factory) in enumerate(candidates):
        attempt_tag = f"[{idx + 1}/{len(candidates)} {label}]"
        try:
            model = factory()
        except Exception as exc:
            last_failure = f"build {exc.__class__.__name__}: {exc}"
            log(f"[MEMORY] commit LLM {attempt_tag} build failed: {exc}")
            continue

        bound = model
        try:
            bound = model.bind(max_tokens=max_tokens, timeout=timeout_sec)
        except Exception:
            pass

        try:
            resp = bound.invoke(messages)
            text = getattr(resp, "content", None) or str(resp)
            if isinstance(text, list):
                text = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in text
                )
        except Exception as exc:
            last_failure = f"invoke {exc.__class__.__name__}: {exc}"
            log(f"[MEMORY] commit LLM {attempt_tag} invoke failed: {exc}")
            continue

        parsed = _parse_lessons_json(text)
        if parsed is None:
            last_failure = "response not parseable as JSON"
            preview = (text or "")[:200].replace("\n", " ")
            log(
                f"[MEMORY] commit LLM {attempt_tag} returned non-JSON "
                f"(len={len(text or '')}, preview={preview!r})"
            )
            continue

        validated = _validate_lessons(parsed, draft.get("user_verdict"))
        log(f"[MEMORY] commit LLM {attempt_tag} succeeded")
        return validated

    log(
        f"[MEMORY] commit LLM: all {len(candidates)} candidates failed "
        f"(last_failure={last_failure!r}); falling back to rule-based lessons"
    )
    return None


def _build_factory_chain(
    draft: Dict[str, Any],
    *,
    config: Any,
    log: Callable[[str], None],
) -> List[tuple]:
    """Return an ordered list of ``(label, no_arg_factory)`` candidates.

    Each entry is one (potentially callable, potentially failing) attempt
    at building the retrospective LLM. ``_llm_enrich`` walks them in order
    and stops at the first success.

    Strategy keys (``memory.summary_agent.model_strategy``):

    * ``"task_model"`` *(default)* → primary: the model that executed the
      task (``agents.llm.build_chat_model``); fallback: the configured
      small model (``memory.small_model.alias``, default ``qwen-next``).
    * ``"draft_alias"`` → primary: the alias snapshotted on the draft;
      fallback: task model, then small model.
    * ``"small_model"`` / ``"small"`` / ``"qwen-next"`` → primary: small
      model; fallback: task model. Use this to save tokens.
    * any other string → treat it as a literal alias for ``build_small_model``
      and still fall through to the task model.

    De-duplication: identical (label, factory-key) pairs are filtered so
    we never call the same model twice in the chain (e.g. when
    ``draft.model_alias`` happens to match the small_model alias).
    """
    strategy = _get_str(config, "memory_summary_model_strategy", "task_model").strip().lower()
    small_alias = _get_str(config, "memory_small_model_alias", "qwen-next")

    def _task_model_factory() -> Optional[tuple]:
        try:
            try:
                from agents.llm import build_chat_model  # type: ignore
            except ImportError:
                from ..agents.llm import build_chat_model  # type: ignore
        except Exception as exc:
            log(f"[MEMORY] task-model unavailable for commit: {exc}")
            return None
        if config is None:
            log("[MEMORY] task-model commit requires config; skipped")
            return None
        try:
            display = _get_str(config, "llm_display_name", "?")
        except Exception:
            display = "?"

        def _factory() -> Any:
            return build_chat_model(config, temperature=0.0)

        return (f"task_model({display})", _factory)

    def _small_model_factory(alias: str) -> Optional[tuple]:
        try:
            try:
                from agents.llm import build_small_model  # type: ignore
            except ImportError:
                from ..agents.llm import build_small_model  # type: ignore
        except Exception as exc:
            log(f"[MEMORY] small-model import failed: {exc}")
            return None
        max_tokens = _get_int(config, "memory_small_model_max_tokens", 800)
        timeout = _get_int(config, "memory_small_model_timeout_sec", 60)

        def _factory() -> Any:
            return build_small_model(
                config,
                alias=alias,
                temperature=0.0,
                max_tokens=max_tokens,
                timeout=timeout,
            )

        return (f"small_model({alias})", _factory)

    def _alias_from_draft() -> Optional[tuple]:
        alias = str(draft.get("model_alias") or "").strip()
        if not alias:
            return None
        return _small_model_factory(alias)

    chain: List[tuple] = []
    seen_labels: set = set()

    def _push(entry: Optional[tuple]) -> None:
        if entry is None:
            return
        label, factory = entry
        if label in seen_labels:
            return
        seen_labels.add(label)
        chain.append((label, factory))

    if strategy == "task_model":
        _push(_task_model_factory())
        _push(_small_model_factory(small_alias))
    elif strategy == "draft_alias":
        _push(_alias_from_draft())
        _push(_task_model_factory())
        _push(_small_model_factory(small_alias))
    elif strategy in ("small_model", "small", "qwen-next"):
        _push(_small_model_factory(small_alias))
        _push(_task_model_factory())
    elif strategy:
        # Treat the strategy string as a literal alias.
        _push(_small_model_factory(strategy))
        _push(_task_model_factory())
        _push(_small_model_factory(small_alias))
    else:
        _push(_task_model_factory())
        _push(_small_model_factory(small_alias))

    if chain:
        log(
            "[MEMORY] commit LLM candidate chain: "
            + " → ".join(label for label, _ in chain)
        )
    return chain


def _load_system_prompt() -> str:
    """Try the prompt file first; fall back to the inline default."""
    try:
        try:
            from prompts import load as load_prompt  # type: ignore
        except ImportError:
            from ..prompts import load as load_prompt  # type: ignore
        return load_prompt("summarize/system.md")
    except Exception:
        return _DEFAULT_SYSTEM_PROMPT


def _build_user_message(draft: Dict[str, Any]) -> str:
    """Render the ``USER_TEMPLATE`` from the draft episode.

    The draft is expected to carry the retrospective-context blocks added by
    ``extract_facts_from_state``: ``task_brief``, ``tool_calls_excerpt``,
    ``generated_code``. Any of them missing degrade gracefully to a
    placeholder so the LLM at least gets *something* to chew on.
    """
    suggestion = (draft.get("user_suggestion") or "").strip() or "(用户没有提供文字建议)"
    findings = draft.get("auto_findings") or {}
    findings_block = _render_findings_block(findings) or "(无发现)"
    facts_block = _render_facts_block(draft)

    excerpt = draft.get("tool_calls_excerpt") or []
    toolcalls_block = _render_tool_sequence_block(excerpt) if excerpt else _render_toolcalls_block(
        draft.get("tool_call_stats") or {}
    )
    tool_calls_count = len(excerpt) if excerpt else int(
        ((draft.get("tool_call_stats") or {}).get("total_calls") or 0)
    )

    task_brief_block = _render_task_brief_block(draft.get("task_brief") or {})

    code = draft.get("generated_code") or draft.get("_generated_code_excerpt") or ""
    if not code:
        code = "(本次未携带代码摘要)"

    fmt_kwargs = dict(
        verdict=draft.get("user_verdict", ""),
        domain=draft.get("domain", ""),
        url=draft.get("url", ""),
        task_brief_block=task_brief_block,
        suggestion_block=suggestion,
        findings_block=findings_block,
        facts_block=facts_block,
        toolcalls_block=toolcalls_block,
        tool_calls_count=tool_calls_count,
        code_block=code,
    )

    # Try the user prompt file; fall back to the inline template.
    try:
        try:
            from prompts import load as load_prompt  # type: ignore
        except ImportError:
            from ..prompts import load as load_prompt  # type: ignore
        return load_prompt("summarize/user.md", **fmt_kwargs)
    except Exception:
        return _USER_TEMPLATE.format(**fmt_kwargs)


def _render_task_brief_block(brief: Dict[str, Any]) -> str:
    """Render the original task block (URL, dates, run_mode, hints) for the LLM."""
    if not isinstance(brief, dict) or not brief:
        return "(无任务说明快照)"
    parts: List[str] = []
    for k, label in (
        ("url", "URL"),
        ("run_mode", "Run mode"),
        ("start_date", "Date range start"),
        ("end_date", "Date range end"),
    ):
        v = brief.get(k)
        if v:
            parts.append(f"- {label}: {v}")
    extra = (brief.get("extra_requirements") or "").strip()
    if extra:
        parts.append(f"- Task objective (业务方主诉): {extra}")
    if brief.get("had_site_memory_hint"):
        site = (brief.get("site_memory_hint") or "").strip()
        if site:
            parts.append("- 网站记忆 hint（任务开始时注入到我 prompt 顶部）:")
            parts.append(_indent_block(site, "    "))
    if brief.get("had_feedback_replay_hint"):
        fb = (brief.get("feedback_replay_hint") or "").strip()
        if fb:
            parts.append("- 上一次任务反馈 hint（rerun 场景，最高优先级）:")
            parts.append(_indent_block(fb, "    "))
    return "\n".join(parts) or "(无任务说明快照)"


def _indent_block(text: str, indent: str) -> str:
    return "\n".join(indent + line for line in text.splitlines())


def _render_tool_sequence_block(excerpt: List[Dict[str, Any]]) -> str:
    """Render the chronological tool-call excerpt as a compact list.

    Each item: ``[i] action(input...) -> ok|FAIL: summary``. Inputs are
    already shrunk by :func:`episode._shrink_action_input`.
    """
    if not excerpt:
        return "(无)"
    parts: List[str] = []
    for entry in excerpt:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("i", "?")
        action = entry.get("action") or "unknown"
        success = bool(entry.get("success", True))
        status = "ok" if success else f"FAIL[{entry.get('error_code') or '?'}]"
        try:
            args = json.dumps(entry.get("input"), ensure_ascii=False, default=str)
        except Exception:
            args = str(entry.get("input"))
        if len(args) > 280:
            args = args[:280] + "…"
        summary = (entry.get("summary") or "").strip()
        if len(summary) > 240:
            summary = summary[:240] + "…"
        parts.append(f"[{idx}] {action}({args}) -> {status}: {summary}")
    return "\n".join(parts)


def _render_findings_block(findings: Dict[str, Any]) -> str:
    if not isinstance(findings, dict):
        return ""
    parts: List[str] = []
    for bucket, label in (
        ("suspected_failures", "疑似失败"),
        ("redundant_tool_calls", "工具冗余"),
        ("redundant_code_blocks", "代码重复"),
    ):
        items = findings.get(bucket) or []
        if not items:
            continue
        parts.append(f"- {label}:")
        for item in items[:8]:
            parts.append(f"  * {item}")
    return "\n".join(parts)


def _render_facts_block(draft: Dict[str, Any]) -> str:
    keys = (
        "iterations",
        "critic_rounds",
        "duration_sec",
        "code_size_lines",
        "html_fingerprint",
        "auto_outcome",
    )
    parts = []
    for k in keys:
        v = draft.get(k)
        if v in (None, "", []):
            continue
        parts.append(f"- {k}: {v}")
    verified = draft.get("verified_selectors") or {}
    if isinstance(verified, dict):
        slots = []
        for section in ("list", "detail"):
            sub = verified.get(section) or {}
            if isinstance(sub, dict):
                for slot, sel in sub.items():
                    if isinstance(sel, str) and sel.strip():
                        slots.append(f"  * {section}.{slot}: `{sel}`")
                    if len(slots) >= 8:
                        break
        if slots:
            parts.append("- verified_selectors:")
            parts.extend(slots)
    return "\n".join(parts) or "(无)"


def _render_toolcalls_block(stats: Dict[str, Any]) -> str:
    if not isinstance(stats, dict):
        return "(无)"
    by_tool = stats.get("by_tool") or {}
    failures = stats.get("failures_by_tool") or {}
    if not by_tool:
        return "(无)"
    parts = [f"- 总调用数: {stats.get('total_calls', 0)}"]
    for name, count in sorted(by_tool.items(), key=lambda kv: -kv[1])[:10]:
        line = f"  * {name}: {count} 次"
        if name in failures:
            line += f" (其中失败 {failures[name]} 次)"
        parts.append(line)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# JSON parsing & validation
# ---------------------------------------------------------------------------


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def _parse_lessons_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _validate_lessons(parsed: Dict[str, Any], verdict: Optional[str]) -> Dict[str, Any]:
    """Coerce LLM output into the contract; never raises."""
    out: Dict[str, Any] = {
        "failure_analysis": None,
        "optimization": [],
        "site_traits": {},
        # slot_verdicts is OMITTED when LLM didn't give us one (or gave us
        # an unusable one). Downstream site_profile.update_profile_from_episode
        # falls back to the task-level verdict when this key is missing.
        # Keys present here override the task-level verdict for that one slot.
    }

    fa = parsed.get("failure_analysis")
    if verdict == "wrong" and isinstance(fa, dict):
        out["failure_analysis"] = {
            "user_complaint_interpreted": _short_str(fa.get("user_complaint_interpreted")),
            "root_cause_guess": _short_str(fa.get("root_cause_guess")),
            "fix_direction": _short_str(fa.get("fix_direction")),
        }
    # verdict == "correct" → keep failure_analysis = None even if LLM returned one

    opt = parsed.get("optimization")
    if isinstance(opt, list):
        cleaned = []
        for item in opt:
            if isinstance(item, str) and item.strip():
                cleaned.append(_short_str(item, cap=200))
            if len(cleaned) >= 8:
                break
        out["optimization"] = cleaned
    if not out["optimization"]:
        out["optimization"] = ["（LLM 未返回有效优化建议）"]

    traits = parsed.get("site_traits")
    if isinstance(traits, dict):
        cleaned_traits: Dict[str, Any] = {}
        for k, v in traits.items():
            if not isinstance(k, str) or not k.strip():
                continue
            if isinstance(v, bool):
                cleaned_traits[k.strip()[:40]] = v
            elif isinstance(v, (int, float)):
                cleaned_traits[k.strip()[:40]] = v
            elif isinstance(v, str) and v.strip():
                cleaned_traits[k.strip()[:40]] = _short_str(v, cap=200)
            if len(cleaned_traits) >= 12:
                break
        out["site_traits"] = cleaned_traits

    # ---- slot_verdicts (Stage-2 slot-level error attribution) ----
    # We accept only the closed enum {correct, wrong, unknown}. "unknown" is
    # stored explicitly so that site_profile can tell "LLM intentionally
    # passed on this slot" apart from "LLM forgot the key entirely" (the
    # second case still falls back to the task-level verdict). Keys not in
    # the canonical _TRACKABLE_SLOTS list are silently dropped — protects
    # us from typos / hallucinated slots polluting the profile.
    sv_raw = parsed.get("slot_verdicts")
    if isinstance(sv_raw, dict) and sv_raw:
        cleaned_sv = _sanitize_slot_verdicts(sv_raw)
        if cleaned_sv:
            out["slot_verdicts"] = cleaned_sv

    return out


# Canonical set the planner / site_profile cares about. We deliberately
# duplicate the value rather than import from site_profile to keep memory
# / commit decoupled (commit doesn't otherwise import that module's tuple).
_VALID_SLOT_VERDICTS: tuple = ("correct", "wrong", "unknown")
_VALID_TRACKABLE_SLOTS: frozenset = frozenset({
    "list.container",
    "list.title_link",
    "list.title",
    "list.date",
    "list.next_page",
    "detail.content",
    "detail.title",
    "detail.publish_date",
})


def _sanitize_slot_verdicts(raw: Dict[str, Any]) -> Dict[str, str]:
    """Drop unknown keys, normalise values to {correct,wrong,unknown}, lowercase."""
    cleaned: Dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        slot = k.strip()
        if slot not in _VALID_TRACKABLE_SLOTS:
            continue
        if not isinstance(v, str):
            continue
        val = v.strip().lower()
        if val not in _VALID_SLOT_VERDICTS:
            continue
        cleaned[slot] = val
    return cleaned


def _short_str(value: Any, *, cap: int = 280) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if len(s) <= cap:
        return s
    return s[:cap] + "…"


# ---------------------------------------------------------------------------
# Fallback (no LLM available / LLM failed)
# ---------------------------------------------------------------------------


def _fallback_lessons(draft: Dict[str, Any]) -> Dict[str, Any]:
    """Pure-rule lessons used when the LLM call is skipped or failed.

    Hole-1.b fix — even with zero LLM, ensure the user's plain-English
    complaint reaches ``site_profile.known_pitfalls`` so the *next* run on
    the same domain has at least *something* to learn from. We do this by
    populating ``failure_analysis.fix_direction`` and ``root_cause_guess``
    with the raw suggestion (clearly tagged), because
    :func:`update_profile_from_episode` harvests those two specific keys
    into ``known_pitfalls``. Tipping the user's words into a pitfall is
    strictly better than silently swallowing them.
    """
    findings = draft.get("auto_findings") or {}
    suggestion = (draft.get("user_suggestion") or "").strip()
    verdict = draft.get("user_verdict")

    optimization: List[str] = []
    for bucket in ("redundant_tool_calls", "redundant_code_blocks", "suspected_failures"):
        for item in (findings.get(bucket) or [])[:3]:
            if isinstance(item, str) and item.strip():
                optimization.append(item.strip())
    if not optimization:
        optimization = ["（无 LLM 加持，未生成具体优化建议）"]

    failure_analysis = None
    if verdict == "wrong":
        failure_analysis = {
            "user_complaint_interpreted": "",
            "root_cause_guess": "",
            "fix_direction": "",
        }
        if suggestion:
            # Truncate to keep pitfall lines bounded (the rendered hint
            # block has its own trimming, but profile JSON shouldn't
            # carry a 5KB user rant either).
            short = suggestion if len(suggestion) <= 240 else suggestion[:240] + "..."
            failure_analysis["user_complaint_interpreted"] = (
                f"用户原话: {short}（LLM 复盘失败，未做技术解读）"
            )
            # These two keys are the ones harvested into known_pitfalls,
            # so plant the raw complaint there too — it's the single most
            # important signal we have when the LLM falls over.
            failure_analysis["root_cause_guess"] = f"[未经 LLM 解读] 用户反馈：{short}"
            failure_analysis["fix_direction"] = (
                f"[未经 LLM 解读] 下次按用户反馈核对：{short}"
            )

    return {
        "failure_analysis": failure_analysis,
        "optimization": optimization,
        "site_traits": {},
    }


# ---------------------------------------------------------------------------
# Site profile update (best-effort)
# ---------------------------------------------------------------------------


def _safe_update_profile(
    store: MemoryStore,
    episode: Dict[str, Any],
    *,
    config: Any,
    log: Callable[[str], None],
) -> Optional[SiteProfile]:
    domain = str(episode.get("domain") or "").strip()
    if not domain:
        return None
    try:
        existing = store.lookup_site(domain)
        new_profile = update_profile_from_episode(
            existing,
            episode,
            promote_min_wins=_get_int(config, "memory_promote_min_wins", 3),
            promote_min_winrate=_get_float(config, "memory_promote_min_winrate", 0.8),
            confidence_penalty_on_fail=_get_float(config, "memory_confidence_penalty_on_fail", 0.3),
            confidence_bonus_on_success=_get_float(config, "memory_confidence_bonus_on_success", 0.1),
            quarantine_after=_get_int(config, "memory_quarantine_after", 2),
            drift_check=_get_bool(config, "memory_drift_check", True),
        )
        ok = store.write_site(new_profile)
        return new_profile if ok else None
    except Exception as exc:
        log(f"[MEMORY] _safe_update_profile failed for {domain}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Config accessors (defensive — config may be None / partial)
# ---------------------------------------------------------------------------


def _get_bool(config: Any, attr: str, default: bool) -> bool:
    if config is None:
        return default
    v = getattr(config, attr, None)
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off")
    return bool(v)


def _get_int(config: Any, attr: str, default: int) -> int:
    if config is None:
        return default
    v = getattr(config, attr, None)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _get_float(config: Any, attr: str, default: float) -> float:
    if config is None:
        return default
    v = getattr(config, attr, None)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _get_str(config: Any, attr: str, default: str) -> str:
    if config is None:
        return default
    v = getattr(config, attr, None)
    if not v:
        return default
    return str(v)


__all__ = ["commit_episode"]
