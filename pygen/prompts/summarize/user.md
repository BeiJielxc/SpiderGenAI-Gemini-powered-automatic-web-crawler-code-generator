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

请基于以上**全部**信息复盘自己的执行过程，按 system 中规定的 JSON schema 输出 lessons。再次强调：**只输出 JSON，无任何 markdown 围栏或自然语言前后缀**。
