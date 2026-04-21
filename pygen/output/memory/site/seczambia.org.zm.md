# Site profile: `seczambia.org.zm`

- version: 1
- first seen: 2026-04-21T03:05:44.216165+00:00
- last updated: 2026-04-21T03:05:44.216188+00:00
- last success: 2026-04-21T03:05:44.216188+00:00
- last failure: -
- wins / losses: 1 / 0
- consecutive failures: 0
- quarantined: **False**
- has drift (HTML fingerprint): False
- confidence: 0.600

## Site traits (LLM-derived)

- **has_ld_json**: True
- **needs_playwright**: True
- **pagination_pattern**: a.page-numbers.next
- **platform**: WordPress + Elementor

## Known pitfalls

- 重复调用 get_page_html、open_page 等工具可省略，复用首次获取的 HTML 内容
- 验证过的 selector 无需重复 verify_selector，避免冗余请求
- 代码中存在两处完全重复的 playwright_fetch 逻辑，应合并为公共函数
