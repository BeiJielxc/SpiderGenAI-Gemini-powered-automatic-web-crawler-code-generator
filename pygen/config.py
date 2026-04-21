"""
配置管理模块 - PyGen独立版
"""
import yaml
import os
from typing import Dict, Any, Optional
from pathlib import Path


class Config:
    """配置管理类"""
    
    def __init__(self, config_path: Optional[str] = None):
        """
        初始化配置
        
        Args:
            config_path: 配置文件路径，如果为None则自动查找
        """
        if config_path is None:
            # 优先查找 pygen 目录下的配置，否则使用项目根目录的配置
            pygen_config = Path(__file__).parent / "config.yaml"
            root_config = Path(__file__).parent.parent / "config.yaml"
            
            if pygen_config.exists():
                config_path = str(pygen_config)
            elif root_config.exists():
                config_path = str(root_config)
            else:
                raise FileNotFoundError(
                    "未找到配置文件！请确保以下位置之一存在 config.yaml:\n"
                    f"  - {pygen_config}\n"
                    f"  - {root_config}\n"
                    "或从 config.yaml.example 复制并配置。"
                )
        
        self.config_path = config_path
        self.config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件"""
        with open(self.config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    
    def _get_active_llm_config(self) -> Dict[str, Any]:
        """
        获取当前激活的 LLM 配置
        
        优先使用新的 llm 配置结构，如果不存在则回退到旧的 qwen 配置
        """
        llm_config = self.config.get('llm', {})
        
        if llm_config:
            # 使用新配置结构
            active_model = llm_config.get('active', 'qwen')
            model_config = llm_config.get(active_model, {})
            
            if model_config:
                return {
                    'name': active_model,
                    **model_config
                }
        
        # 回退到旧的 qwen 配置
        qwen_config = self.config.get('qwen', {})
        return {
            'name': 'qwen',
            **qwen_config
        }
    
    @property
    def active_model_name(self) -> str:
        """获取当前激活的模型名称"""
        return self._get_active_llm_config().get('name', 'qwen')
    
    @property
    def qwen_api_key(self) -> str:
        """获取当前激活模型的 API Key（保持向后兼容的属性名）"""
        llm_config = self._get_active_llm_config()
        api_key = llm_config.get('api_key', '')
        
        if not api_key or api_key.startswith('YOUR_'):
            raise ValueError(
                f"请在 config.yaml 中配置 {llm_config.get('name', 'LLM')} 的 API Key\n"
                f"当前激活模型: llm.active = {self.active_model_name}"
            )
        return api_key
    
    @property
    def qwen_model(self) -> str:
        """获取当前激活模型的模型名称（保持向后兼容的属性名）"""
        return self._get_active_llm_config().get('model', 'qwen-max')
    
    @property
    def qwen_base_url(self) -> str:
        """获取当前激活模型的 API 基础 URL（保持向后兼容的属性名）"""
        return self._get_active_llm_config().get('base_url', 
            'https://dashscope.aliyuncs.com/compatible-mode/v1')
    
    @property
    def llm_display_name(self) -> str:
        """获取用于显示的 LLM 名称（包含提供商和模型名）"""
        config = self._get_active_llm_config()
        provider = config.get('name', 'unknown')
        model = config.get('model', 'unknown')
        return f"{provider}/{model}"
    
    def list_available_models(self) -> list:
        """列出所有可用的模型配置"""
        llm_config = self.config.get('llm', {})
        models = []
        active = llm_config.get('active', 'qwen')
        
        for key, value in llm_config.items():
            if key == 'active':
                continue
            if isinstance(value, dict) and 'model' in value:
                is_active = key == active
                api_key = value.get('api_key', '')
                is_configured = api_key and not api_key.startswith('YOUR_')
                models.append({
                    'name': key,
                    'model': value.get('model', ''),
                    'active': is_active,
                    'configured': is_configured
                })
        
        return models

    @property
    def llm_auto_repair(self) -> bool:
        """
        是否启用“LLM 自动修复 + 代码静态检查”。

        - true（默认）：启用修复循环与后端静态检查
        - false：不进行任何修复/检查，直接运行 LLM 原始生成代码（风险更高）
        """
        llm_config = self.config.get("llm", {})
        if isinstance(llm_config, dict):
            v = llm_config.get("auto_repair", True)
            # yaml 里可能是字符串，做一次宽松转换
            if isinstance(v, str):
                return v.strip().lower() not in ("0", "false", "no", "off")
            return bool(v)
        return True
    
    @property
    def cdp_debug_port(self) -> int:
        """获取CDP调试端口"""
        return self.config.get('cdp', {}).get('debug_port', 9222)
    
    @property
    def cdp_auto_select_port(self) -> bool:
        """是否自动选择CDP端口"""
        return self.config.get('cdp', {}).get('auto_select_port', True)
    
    @property
    def cdp_user_data_dir(self) -> str:
        """获取Chrome Profile目录"""
        default_dir = str(Path(__file__).parent / "chrome-profile")
        return self.config.get('cdp', {}).get('user_data_dir', default_dir)
    
    @property
    def cdp_timeout(self) -> int:
        """获取CDP操作超时时间（毫秒）"""
        timeout_sec = self.config.get('cdp', {}).get('timeout', 60)
        return timeout_sec * 1000

    @property
    def browser_headless(self) -> bool:
        """
        浏览器无头模式开关（全局控制所有爬取模式下的浏览器是否显示窗口）。

        - False（默认）：显示浏览器窗口，方便本地开发调试
        - True：无头模式，不显示浏览器窗口（Linux 服务器部署必须设为 True）
        """
        v = self.config.get('cdp', {}).get('headless', False)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)
    
    # ============ Agent 配置 ============
    @property
    def agent_max_iterations(self) -> int:
        """Agent 模式最大迭代轮次"""
        v = self.config.get('agent', {}).get('max_iterations', 20)
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return 20

    # ============ Artifact 工作记忆配置 ============
    def _artifacts_cfg(self) -> Dict[str, Any]:
        return self.config.get('artifacts', {}) or {}

    @property
    def artifacts_ttl_days(self) -> int:
        """超过该天数的 artifact 文件在 runner 启动时清理（0 = 不清理）。"""
        v = self._artifacts_cfg().get('ttl_days', 7)
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 7

    @property
    def artifacts_per_task_subdir(self) -> bool:
        """按 task_id 分子目录存放 artifact。"""
        v = self._artifacts_cfg().get('per_task_subdir', True)
        if isinstance(v, str):
            return v.strip().lower() not in ("0", "false", "no", "off")
        return bool(v)

    @property
    def artifacts_summary_threshold(self) -> int:
        """大于该阈值（字符数）的 payload 才会落盘+生成 summary。"""
        v = self._artifacts_cfg().get('summary_threshold_chars', 3500)
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 3500

    @property
    def artifacts_enable_summary(self) -> bool:
        """关闭后退化为旧 put_json 行为（不生成结构化 summary）。"""
        v = self._artifacts_cfg().get('enable_summary', True)
        if isinstance(v, str):
            return v.strip().lower() not in ("0", "false", "no", "off")
        return bool(v)

    # ---- 小模型 fallback 摘要 ----
    def _small_model_cfg(self) -> Dict[str, Any]:
        sub = self._artifacts_cfg().get('small_model', {}) or {}
        return sub if isinstance(sub, dict) else {}

    @property
    def artifacts_small_model_enabled(self) -> bool:
        """规则摘要"弱"时是否调用小模型兜底。默认关闭。"""
        v = self._small_model_cfg().get('enabled', False)
        if isinstance(v, str):
            return v.strip().lower() not in ("0", "false", "no", "off")
        return bool(v)

    @property
    def artifacts_small_model_alias(self) -> str:
        """指向 ``llm:`` 段下的别名，独立于 ``llm.active``。"""
        v = self._small_model_cfg().get('alias', 'qwen-next')
        return str(v) if v else 'qwen-next'

    @property
    def artifacts_small_model_max_tokens(self) -> int:
        v = self._small_model_cfg().get('max_tokens', 800)
        try:
            return max(64, int(v))
        except (TypeError, ValueError):
            return 800

    @property
    def artifacts_small_model_timeout_sec(self) -> int:
        v = self._small_model_cfg().get('timeout_sec', 15)
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return 15

    @property
    def artifacts_small_model_raw_excerpt_chars(self) -> int:
        """喂给小模型的原始内容字符上限。"""
        v = self._small_model_cfg().get('raw_excerpt_chars', 6000)
        try:
            return max(500, int(v))
        except (TypeError, ValueError):
            return 6000

    # ============ 持久化记忆 (memory) ============
    def _memory_cfg(self) -> Dict[str, Any]:
        return self.config.get("memory", {}) or {}

    @property
    def memory_enabled(self) -> bool:
        return self._to_bool(self._memory_cfg().get("enabled", True), True)

    @property
    def memory_root(self) -> Path:
        """记忆根目录。

        config.yaml 里默认 ``pygen/output/memory``（相对仓库根）。我们做一次
        归一化：若以 ``pygen/`` 开头则改为 pygen 包目录下的对应子路径，否则
        当作相对仓库根（pygen 包父目录）解析。绝对路径原样返回。
        """
        raw = self._memory_cfg().get("root", "pygen/output/memory")
        p = Path(raw)
        if p.is_absolute():
            return p
        parts = p.parts
        if parts and parts[0] == "pygen":
            return Path(__file__).parent.joinpath(*parts[1:])
        return Path(__file__).parent.parent.joinpath(*parts)

    def _memory_episodes_cfg(self) -> Dict[str, Any]:
        sub = self._memory_cfg().get("episodes", {})
        return sub if isinstance(sub, dict) else {}

    @property
    def memory_max_keep(self) -> int:
        v = self._memory_episodes_cfg().get("max_keep", 1000)
        try:
            return max(10, int(v))
        except (TypeError, ValueError):
            return 1000

    @property
    def memory_pending_gc_days(self) -> int:
        v = self._memory_episodes_cfg().get("pending_gc_days", 30)
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 30

    def _memory_profile_cfg(self) -> Dict[str, Any]:
        sub = self._memory_cfg().get("site_profile", {})
        return sub if isinstance(sub, dict) else {}

    @property
    def memory_profile_enabled(self) -> bool:
        return self._to_bool(self._memory_profile_cfg().get("enabled", True), True)

    @property
    def memory_inject_into_planner(self) -> bool:
        return self._to_bool(self._memory_profile_cfg().get("inject_into_planner", True), True)

    @property
    def memory_require_user_verdict(self) -> bool:
        return self._to_bool(self._memory_profile_cfg().get("require_user_verdict", True), True)

    @property
    def memory_confidence_decay_per_30d(self) -> float:
        try:
            return float(self._memory_profile_cfg().get("confidence_decay_per_30d", 0.1))
        except (TypeError, ValueError):
            return 0.1

    @property
    def memory_confidence_penalty_on_fail(self) -> float:
        try:
            return float(self._memory_profile_cfg().get("confidence_penalty_on_fail", 0.3))
        except (TypeError, ValueError):
            return 0.3

    @property
    def memory_confidence_bonus_on_success(self) -> float:
        try:
            return float(self._memory_profile_cfg().get("confidence_bonus_on_success", 0.1))
        except (TypeError, ValueError):
            return 0.1

    @property
    def memory_min_inject_confidence(self) -> float:
        try:
            return float(self._memory_profile_cfg().get("min_inject_confidence", 0.3))
        except (TypeError, ValueError):
            return 0.3

    @property
    def memory_quarantine_after(self) -> int:
        try:
            return max(1, int(self._memory_profile_cfg().get("quarantine_after", 2)))
        except (TypeError, ValueError):
            return 2

    @property
    def memory_promote_min_wins(self) -> int:
        try:
            return max(1, int(self._memory_profile_cfg().get("promote_min_wins", 3)))
        except (TypeError, ValueError):
            return 3

    @property
    def memory_promote_min_winrate(self) -> float:
        try:
            return float(self._memory_profile_cfg().get("promote_min_winrate", 0.8))
        except (TypeError, ValueError):
            return 0.8

    @property
    def memory_drift_check(self) -> bool:
        return self._to_bool(self._memory_profile_cfg().get("drift_check", True), True)

    @property
    def memory_blacklist_min_losses(self) -> int:
        """How many user-verified failures before a selector goes on the
        "do NOT try" list (rendered into the planner site-memory hint).

        Default ``1`` — Module C lowered the threshold so a single
        confirmed loss is enough to blacklist the selector. The two
        guard rails that make this safe even with a 5–20% LLM
        misjudgement rate:

        1. Stage-2 LLM defaults to ``slot_verdicts="unknown"``, so we
           only bump losses when the model is confident.
        2. ``blacklist_require_consecutive=true`` *also* requires
           ``consecutive_losses >= min_losses``. A single subsequent
           ``correct`` verdict resets the streak and unblocks the
           selector.
        """
        try:
            return max(1, int(self._memory_profile_cfg().get("blacklist_min_losses", 1)))
        except (TypeError, ValueError):
            return 1

    @property
    def memory_blacklist_max_winrate(self) -> float:
        """Upper winrate bound for blacklist eligibility.

        A selector that wins occasionally (e.g. winrate 0.4) is
        *flaky* — better surface it as flaky on the positive side than
        as forbidden. Anything strictly above this floor is excluded
        from the blacklist even if losses ≥ ``min_losses``.
        """
        try:
            return float(self._memory_profile_cfg().get("blacklist_max_winrate", 0.2))
        except (TypeError, ValueError):
            return 0.2

    @property
    def memory_blacklist_require_consecutive(self) -> bool:
        """If True, the blacklist gate also requires
        ``consecutive_losses >= min_losses``.

        Intended to defend against Stage-2 LLM mis-attributing a slot
        verdict (false-positive ``wrong``): one stray failure in an
        otherwise healthy selector won't ship it to the blacklist if a
        subsequent verdict=correct resets the streak. Strongly recommended
        once ``slot_verdicts`` are in play; old profiles without the
        ``consecutive_losses`` field are treated conservatively (we
        assume ``consecutive_losses == losses``) to keep behaviour
        backward-compatible.
        """
        return self._to_bool(
            self._memory_profile_cfg().get("blacklist_require_consecutive", True),
            True,
        )

    def _memory_summary_cfg(self) -> Dict[str, Any]:
        sub = self._memory_cfg().get("summary_agent", {})
        return sub if isinstance(sub, dict) else {}

    @property
    def memory_use_llm(self) -> bool:
        return self._to_bool(self._memory_summary_cfg().get("use_llm", True), True)

    @property
    def memory_skip_llm_when_correct(self) -> bool:
        return self._to_bool(self._memory_summary_cfg().get("skip_llm_when_correct", False), False)

    @property
    def memory_small_model_alias(self) -> str:
        v = self._memory_summary_cfg().get("small_model_alias", "qwen-next")
        return str(v) if v else "qwen-next"

    @property
    def memory_small_model_max_tokens(self) -> int:
        try:
            return max(64, int(self._memory_summary_cfg().get("max_tokens", 800)))
        except (TypeError, ValueError):
            return 800

    @property
    def memory_small_model_timeout_sec(self) -> int:
        try:
            return max(1, int(self._memory_summary_cfg().get("timeout_sec", 25)))
        except (TypeError, ValueError):
            return 25

    @property
    def memory_summary_model_strategy(self) -> str:
        """Which LLM the commit-time retrospective should use.

        Values:

        * ``"task_model"`` (default) — use the same model as ``llm.active``,
          i.e. the model that actually executed the task. Highest fidelity
          self-review at the cost of using the more expensive planner model.
        * ``"draft_alias"`` — use the alias snapshot stored on the draft
          (``model_alias`` field). Useful when the active model changes
          between runs but you still want each task summarised by *its*
          model.
        * ``"small_model"`` — use ``memory.summary_agent.small_model_alias``
          (legacy behaviour, cheap & fast).
        * ``"<alias>"`` — any other string is treated as an llm alias.
        """
        v = self._memory_summary_cfg().get("model_strategy", "task_model")
        return str(v).strip() or "task_model"

    @property
    def memory_summary_max_tokens(self) -> int:
        """Override token cap for retrospective LLM (0 → use small-model cap)."""
        try:
            return max(0, int(self._memory_summary_cfg().get("summary_max_tokens", 0)))
        except (TypeError, ValueError):
            return 0

    @property
    def memory_summary_timeout_sec(self) -> int:
        """Override timeout for retrospective LLM (0 → use small-model timeout)."""
        try:
            return max(0, int(self._memory_summary_cfg().get("summary_timeout_sec", 0)))
        except (TypeError, ValueError):
            return 0

    @property
    def memory_enable_auto_findings(self) -> bool:
        return self._to_bool(self._memory_summary_cfg().get("enable_auto_findings", True), True)

    @property
    def memory_feedback_replay_priority(self) -> str:
        sub = self._memory_cfg().get("rerun", {}) or {}
        v = sub.get("feedback_replay_priority", "highest")
        return str(v).strip().lower() or "highest"

    @property
    def memory_feedback_replay_hops(self) -> int:
        """How many ``rerun_of`` hops back the replay block walks.

        ``1`` keeps the legacy single-jump behaviour (cheap, most common
        case). ``3`` is the default — captures the user's last 3 attempts
        on the same task so the planner sees the *trajectory* of failures,
        not just the most recent one. Increasing this beyond 5 starts
        bloating the prompt with diminishing returns.
        """
        sub = self._memory_cfg().get("rerun", {}) or {}
        try:
            return max(1, int(sub.get("feedback_replay_hops", 3)))
        except (TypeError, ValueError):
            return 3

    @property
    def memory_feedback_replay_domain_fallback(self) -> bool:
        """Hole-2.A: 没有显式 prev_task_id 时，是否按 domain 自动找最近一条。

        Default: True. Turn off only when you want to enforce the legacy
        "must pass prev_task_id explicitly" contract (rare).
        """
        sub = self._memory_cfg().get("rerun", {}) or {}
        return self._to_bool(sub.get("feedback_replay_domain_fallback", True), True)

    @property
    def memory_feedback_replay_fallback_age_days(self) -> float:
        """Hole-2.A: max age (days) of a draft considered for the
        domain-fallback. Anything older is ignored to avoid resurrecting
        stale grievances. ``0`` / negative disables the age check (test
        path)."""
        sub = self._memory_cfg().get("rerun", {}) or {}
        try:
            return float(sub.get("feedback_replay_fallback_age_days", 14.0))
        except (TypeError, ValueError):
            return 14.0

    # ============ Rerun pre-validation (Module B) ============

    def _rerun_pre_validate_cfg(self) -> Dict[str, Any]:
        """Live under ``memory.rerun.pre_validate`` so it shares the
        rerun namespace with feedback-replay knobs."""
        sub = self._memory_cfg().get("rerun", {}) or {}
        pv = sub.get("pre_validate", {})
        return pv if isinstance(pv, dict) else {}

    @property
    def rerun_pre_validate_enabled(self) -> bool:
        """Master switch for the offline pre-validation pass that runs
        between the chain walk and the planner LLM. Off = skip the
        BS4 check entirely; the planner just sees the plain
        feedback_replay_hint as before."""
        return self._to_bool(self._rerun_pre_validate_cfg().get("enabled", True), True)

    @property
    def rerun_pre_validate_max_selectors(self) -> int:
        """Hard cap on how many selector candidates we extract from
        the previous run's ``fix_direction`` text + ``verified_selectors``
        ledger. Each one is a BS4 ``soup.select`` call; the cap keeps
        the pre-validation budget bounded even if the LLM produced a
        chatty fix_direction."""
        try:
            return max(1, int(self._rerun_pre_validate_cfg().get("max_selectors", 5)))
        except (TypeError, ValueError):
            return 5

    # ============ Page cache (URL-keyed full-HTML store, Module A) ============

    def _page_cache_cfg(self) -> Dict[str, Any]:
        sub = self.config.get("page_cache", {})
        return sub if isinstance(sub, dict) else {}

    @property
    def page_cache_enabled(self) -> bool:
        """Master switch. Default ``True`` — turn off to make every read
        a miss and every write a no-op (the rerun-validator falls back
        to skipping pre-validation in that case)."""
        return self._to_bool(self._page_cache_cfg().get("enabled", True), True)

    @property
    def page_cache_root(self) -> Path:
        """Where the cache lives on disk. Same path-normalisation rules
        as ``memory_root`` (``pygen/...`` → pygen package dir, otherwise
        relative to repo root, abs paths untouched)."""
        raw = self._page_cache_cfg().get("root", "pygen/output/page_cache")
        p = Path(raw)
        if p.is_absolute():
            return p
        parts = p.parts
        if parts and parts[0] == "pygen":
            return Path(__file__).parent.joinpath(*parts[1:])
        return Path(__file__).parent.parent.joinpath(*parts)

    @property
    def page_cache_ttl_sec(self) -> int:
        """Past this many seconds a row is treated as a miss. Default
        24h — long enough to amortise across an evaluation session,
        short enough that yesterday's news doesn't poison a fresh
        scrape."""
        try:
            return max(0, int(self._page_cache_cfg().get("ttl_sec", 86400)))
        except (TypeError, ValueError):
            return 86400

    @property
    def page_cache_max_total_mb(self) -> int:
        """LRU bound on total HTML payload across the whole cache.
        Defaults to 500 MB; rows are evicted oldest-mtime-first when we
        cross this line."""
        try:
            return max(10, int(self._page_cache_cfg().get("max_total_mb", 500)))
        except (TypeError, ValueError):
            return 500

    @property
    def page_cache_invalidate_on_fingerprint_mismatch(self) -> bool:
        """When the freshly fetched HTML has a different
        ``compute_list_page_fingerprint`` than the prior cached row, mark
        the new row ``last_drift=True``. The rerun-validator refuses to
        rely on cache rows whose ``last_drift`` is set."""
        return self._to_bool(
            self._page_cache_cfg().get("invalidate_on_fingerprint_mismatch", True),
            True,
        )

    def get_llm_alias_config(self, alias: str) -> Dict[str, Any]:
        """按别名直接读取 ``llm.<alias>`` 配置，**不**触发 active 切换。

        返回 ``{'name': alias, 'api_key': ..., 'model': ..., 'base_url': ...}``。
        若别名不存在则返回 ``{}``。供小模型 fallback / 多模型路由使用。
        """
        llm_cfg = self.config.get('llm', {}) or {}
        sub = llm_cfg.get(alias)
        if not isinstance(sub, dict):
            return {}
        return {'name': alias, **sub}

    # ============ 服务端（多人在线）配置 ============

    def _server_cfg(self) -> Dict[str, Any]:
        return self.config.get("server", {}) or {}

    @property
    def queue_enabled(self) -> bool:
        """是否启用任务排队/并发控制"""
        v = self._server_cfg().get("enable_queue", False)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)

    @property
    def max_concurrency(self) -> int:
        """允许同时运行的最大任务数"""
        v = self._server_cfg().get("max_concurrency", 1)
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return 1

    @property
    def sse_enabled(self) -> bool:
        """是否启用 SSE 实时推送"""
        v = self._server_cfg().get("enable_sse", False)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)

    @property
    def output_dir(self) -> Path:
        """生成的爬虫脚本输出目录"""
        return Path(__file__).parent / "py"


    # ============ Sandbox / Executor ============

    def _sandbox_cfg(self) -> Dict[str, Any]:
        return self.config.get("sandbox", {}) or {}

    @staticmethod
    def _to_bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    @property
    def sandbox_enabled(self) -> bool:
        return self._to_bool(self._sandbox_cfg().get("enabled", True), True)

    @property
    def sandbox_backend(self) -> str:
        if not self.sandbox_enabled:
            return "local"
        backend = str(self._sandbox_cfg().get("backend", "auto")).strip().lower()
        if backend not in {"docker", "local", "auto"}:
            return "auto"
        return backend

    @property
    def sandbox_auto_start(self) -> bool:
        return self._to_bool(self._sandbox_cfg().get("auto_start", True), True)

    @property
    def sandbox_persistent_session(self) -> bool:
        return self._to_bool(self._sandbox_cfg().get("persistent_session", True), True)

    @property
    def sandbox_docker_image(self) -> str:
        return str(
            self._sandbox_cfg().get(
                "docker_image",
                "mcr.microsoft.com/playwright/python:v1.41.0-jammy",
            )
        ).strip()

    @property
    def sandbox_docker_auto_pull(self) -> bool:
        return self._to_bool(self._sandbox_cfg().get("docker_auto_pull", True), True)

    @property
    def sandbox_docker_disable_network(self) -> bool:
        return self._to_bool(self._sandbox_cfg().get("docker_disable_network", False), False)

    @property
    def sandbox_docker_mount_workdir(self) -> bool:
        return self._to_bool(self._sandbox_cfg().get("docker_mount_workdir", True), True)
