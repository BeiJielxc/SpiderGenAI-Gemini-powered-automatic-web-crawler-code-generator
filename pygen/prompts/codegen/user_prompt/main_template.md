请为以下页面生成爬虫脚本：

## 目标URL

{page_url}

## 页面结构分析

{structure_summary}

## 捕获的网络请求（重点关注API请求）

{api_info}

{enhanced_section}

{html_section}

## 任务要求

1. 分析页面数据来源（API 接口 or HTML 页面；若为 HTML 必须使用 Playwright 解析）
2. 生成能爬取该页面所有数据的Python脚本
3. 【无条件强制】必须实现翻页循环，且**严禁无限循环**
   - 当 MAX_ITEMS > 1 时，必须用 `while len(collected) < MAX_ITEMS:` 的循环；循环内**必须同时满足以下两种退出条件之一**才停止，否则会无限翻页：
     - **退出条件①**：已收集数量 ≥ MAX_ITEMS，则 `break`。
     - **退出条件②**：没有更多页——当前页解析到的条目数为 0，或（若为 HTML）找不到“下一页”按钮/链接，或（若为 API）当前页返回空列表，则 `break`，不要继续请求下一页。
   - **如何知道“第几页”**：若为 API，用请求参数 `page`/`pageNo` 等递增（如 `page=1,2,3...`）；若为 HTML，要么用 URL 中的 `?page=N` 或 `?paged=N` 递增，要么用 Playwright 点击“下一页”按钮（如 `a.page-numbers.next`、`a.next`），点击后无需自己维护页码，但**必须**在每次处理完当前页后检查“是否还有下一页”（例如按钮不可见或 `disabled` 则 break）。
   - 严禁只写单页抓取的 for 循环；也严禁在“无新数据/无下一页”时仍不 break 导致死循环。
4. 提取每条记录的关键字段（标题、日期、链接等）
5. 如果有下载链接（PDF等），提取下载URL
   - 【重要】混合内容处理：某些列表项的链接可能直接指向 PDF/DOC 文件（而不是 HTML 详情页）。
   - **文件检测不能仅看 URL 后缀**：有些网站的 PDF 链接路径中不含 .pdf 后缀（如 `/detail-pages/publication/xxx`），必须通过运行时行为判断。
   - 先检查 URL 后缀（.pdf, .doc, .docx, .xls, .xlsx）：如果匹配，直接保存链接，跳过 page.goto()。
   - **如果 URL 无文件后缀**：正常导航并提取正文。
   - 【强制】详情页内容提取的 try-except 代码结构：
     ```python
     try:
         detail_page.goto(url, wait_until="domcontentloaded", timeout=30000)
         content_html = ""
         # 依次尝试 probe_detail_page 给出的候选 selector
         for sel in content_selectors:
             try:
                 detail_page.wait_for_selector(sel, timeout=8000)
                 el = detail_page.locator(sel).first
                 if el.count():
                     html = el.inner_html()
                     if len(html.strip()) > 50:
                         content_html = html
                         break
             except Exception:
                 continue
         if content_html:
             article_data["content"] = clean_html_content(content_html, url)
         else:
             # 所有候选 selector 都未匹配到足够内容 → 回退为 URL 链接
             article_data["content"] = f'<a href="{{url}}" target="_blank">{{url}}</a>'
     except Exception as e:
         if "ERR_ABORTED" in str(e) or "Download is starting" in str(e):
             # 下载中断 → 文件链接
             article_data["content"] = f'<a href="{{url}}" target="_blank">{{url}}</a>'
         else:
             # 其他异常 → 也回退为 URL 链接
             article_data["content"] = f'<a href="{{url}}" target="_blank">{{url}}</a>'
     ```
   - 【关键】必须对每个 selector 用 `wait_for_selector(sel, timeout=8000)` 等待，因为某些页面内容在 domcontentloaded 后由 JS 动态渲染。
   - 【严禁】在 content 字段中添加任何额外文字，如 "抓取失败"、"Failed to load"、"Download Document"、"请访问原文" 等。
   - 【严禁】生成不含 target="_blank" 的链接。所有 <a> 标签必须包含 `target="_blank"`。
   - content 字段要么是提取到的完整 HTML 正文，要么是纯链接 `<a href="{{url}}" target="_blank">{{url}}</a>`，没有第三种格式。
6. 【重要】如果检测到分类参数，必须：
   - 定义分类配置字典
   - 遍历所有分类获取完整数据
   - 在输出中标记每条数据的分类来源
7. 将结果JSON文件保存到固定目录：`{output_dir}`
   - 使用 os.makedirs 确保目录存在
   - 文件名使用有意义的名称（如：网站名_数据类型_时间.json）

请直接输出完整的Python代码：
