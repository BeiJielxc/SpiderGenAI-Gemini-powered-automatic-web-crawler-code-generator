# Prompts 维护手册

所有 LLM 交互用的长文本模板都集中在这里。Python 代码只负责「动态拼装」（选 crawl_mode、插入 HTML、注入错误案例等），「静态文本」一律落盘成 `.md`，通过 `PromptLoader` 按需加载。

## 目录速览

```text
pygen/prompts/
├── loader.py                 # 统一加载器（lru_cache + 可选 debug dump）
├── __init__.py               # 暴露 load / reload / render
├── planner/
│   └── system.md             # Planner 的 ReAct 系统提示
├── codegen/
│   ├── news_sentiment/system.md
│   ├── enterprise_report/
│   │   ├── system_base.md    # 企业报告主系统提示（最长）
│   │   ├── crawl_mode_single.md
│   │   ├── crawl_mode_multi.md
│   │   └── crawl_mode_auto.md
│   ├── shared/
│   │   ├── attachment_hint.md
│   │   ├── crawl_mode_single_short.md
│   │   ├── crawl_mode_multi_short.md
│   │   └── crawl_mode_auto_short.md
│   ├── user_prompt/
│   │   ├── main_template.md
│   │   ├── html_section.md
│   │   ├── date_section.md
│   │   ├── requirements_section.md
│   │   └── enhanced_section.md
│   └── repair_footer.md      # _build_repair_prompt 的收尾段
├── critic/
│   ├── fault_adjudicate.md
│   ├── fault_adjudicate_system.md
│   ├── repair_round.md
│   └── repair_fallback_system.md
├── browser/
│   ├── menu_analyze.md
│   ├── menu_analyze_system.md
│   ├── menu_exploration_system.md
│   └── menu_exploration_user.md
└── errors/
    └── cases_header.md
```

## 占位符一览

| 模板 | 占位符 |
|---|---|
| `planner/system.md` | `{tools_description}`, `{max_iterations}` |
| `codegen/user_prompt/main_template.md` | `{page_url}`, `{structure_summary}`, `{api_info}`, `{enhanced_section}`, `{html_section}`, `{output_dir}` |
| `codegen/user_prompt/html_section.md` | `{compressed_html}` |
| `codegen/user_prompt/date_section.md` | `{start_date}`, `{end_date}` |
| `codegen/user_prompt/requirements_section.md` | `{user_requirements}` |
| `codegen/user_prompt/enhanced_section.md` | `{enhanced_summary}` |
| `critic/fault_adjudicate.md` | `{allowed_causes}`, `{primary_cause}`, `{backup_cause}`, `{run_mode}`, `{objective}`, `{evidence_preview}` |
| `critic/repair_round.md` | `{round_index}`, `{primary_cause}`, `{backup_cause}`, `{run_mode}`, `{objective}`, `{strategy_hint}`, `{evidence_preview}`, `{code}` |
| `browser/menu_analyze.md` | `{html_content}` |
| `browser/menu_exploration_user.md` | `{leaf_paths_json}`, `{truncated_msg}` |

其余模板无占位符，`load()` 会直接原样返回内容。

## 花括号转义规则（踩坑重点）

`load(rel_path, **vars)` 在**传入任意 `vars`**时会对文件内容执行 `str.format(**vars)`。因此：

- **没有占位符**的模板：`load("foo.md")` 不会触发 `format`，文件里的 `{` `}` 原样保留，可以直接写 JSON、Python dict 字面量。
- **有占位符**的模板：文件中所有**不是占位符**的 `{` 必须写成 `{{`，`}` 写成 `}}`。
  - 例：想输出 `{"success": true}` 应写成 `{{"success": true}}`。
  - 例：想输出 `f'<a href="{url}">'` 应写成 `{{url}}` 才能渲染成 `{url}`。
- 占位符严禁使用下标或切片（如 `{html[:8000]}`），请在 Python 侧先切片再 `load(..., html=snippet)`。

## 使用姿势

```python
from prompts import load           # pygen/ 已是 sys.path 根
from prompts import load as load_prompt   # 在 pygen 包内的典型写法

system_prompt = load(
    "planner/system.md",
    tools_description=tools_desc,
    max_iterations=20,
)
```

无变量时同样调用，省掉 `format` 开销：

```python
attachment_hint = load("codegen/shared/attachment_hint.md")
```

## 热重载（开发期）

`_read()` 带 `lru_cache`，**第一次 load 以后改 md 文件不会立即生效**。两种刷新方式：

1. 重启进程（最省心）。
2. 在 REPL / Notebook 里：
   ```python
   from prompts import reload
   reload()   # 清缓存，下次 load() 重新读盘
   ```

## Debug Dump（做 A/B 或 diff 回归用）

设置环境变量 `PYGEN_DEBUG_DUMP_PROMPT=1` 后，每次 `load()` 的**最终渲染结果**都会写入 `pygen/prompts/_debug_dump/`，文件名是把相对路径里的 `/` 替换成 `__`（例：`planner__system.md`）。

建议用法：

```powershell
$env:PYGEN_DEBUG_DUMP_PROMPT = '1'
python pygen/main.py --task ...
# 用 git diff 或 VSCode 比较新老版本
Remove-Item -Recurse -Force pygen/prompts/_debug_dump
Remove-Item Env:\PYGEN_DEBUG_DUMP_PROMPT
```

`_debug_dump/` 已加进 `.gitignore`（如未加请自行补上），避免把大段临时产物提交到仓库。

## 回归脚本

`scripts/verify_prompts.py` 覆盖所有模板的 `load()` 调用并喂入哑值，验证占位符拼写是否正确。**改任何 md 之前和之后都跑一遍**：

```bash
python scripts/verify_prompts.py
```

返回非零退出码说明至少有一个模板渲染失败（通常是漏写 `{{}}` 转义）。

## A/B 切换小技巧

想同时保留两套 prompt 并在运行时切换，可以：

1. 新增 `planner/system_experiment.md`，不要删掉旧版。
2. 在调用处根据 env 变量挑一个：
   ```python
   import os
   variant = "planner/system.md"
   if os.environ.get("PYGEN_PROMPT_VARIANT") == "experiment":
       variant = "planner/system_experiment.md"
   load(variant, ...)
   ```
3. 回归结束后合并优胜版本。

这样可以在不改代码的情况下对比 prompt 改动对 LLM 行为的影响。

## 新增模板的工作流

1. 写 `foo/bar.md`，只放静态文本 + 占位符（如需要）。
2. 在 `scripts/verify_prompts.py` 的 `CASES` 里补一行，填哑变量。
3. `python scripts/verify_prompts.py` 通过后，再把旧的内联字符串替换为 `load("foo/bar.md", ...)`。
4. 跑一次已知能成功的任务做回归；若改动较大，开启 `PYGEN_DEBUG_DUMP_PROMPT=1` 与线上版本 diff。

## 不建议迁移

- `pygen/llm_agent.py` 的 `_generate_fallback_template` 类脚本骨架——那是 Python 代码模板，不是 LLM prompt。
- `pygen/critic_runtime.py` 的 `probe_script`——运行时生成的 Python 代码片段。
- 小于 8 行、且强依赖上下文变量的临时 prompt（如某些 one-shot diag 信息），直接内联反而更清晰。

---

> 维护原则：**动静分离、零接口改动、每次 md 改动都跑一遍 `verify_prompts.py`**。
