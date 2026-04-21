"""
PyGen 错误案例库 - 结构化的 Few-shot 错误经验

这个模块维护了 LLM 生成爬虫代码时常见的错误模式，
以 Few-shot 的形式注入到 System Prompt 中，帮助模型避免重复犯错。

使用方式：
    from error_cases import get_error_cases_prompt
    system_prompt += get_error_cases_prompt()

扩展方式：
    在 ERROR_CASES 列表中添加新的错误案例字典
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from enum import Enum


class ErrorSeverity(Enum):
    """错误严重程度"""
    CRITICAL = "critical"     # 导致脚本完全失败
    HIGH = "high"             # 导致大量数据丢失/错误
    MEDIUM = "medium"         # 部分数据错误
    LOW = "low"               # 边缘情况问题


class ErrorCategory(Enum):
    """错误类别"""
    SELECTOR = "selector"           # 选择器相关
    DATE_EXTRACTION = "date"        # 日期提取相关
    PAGINATION = "pagination"       # 分页相关
    SCHEMA = "schema"               # 输出格式相关
    HTML_PARSING = "html_parsing"   # HTML 解析相关
    SPA = "spa"                     # SPA/动态页面相关
    ROBUSTNESS = "robustness"       # 健壮性相关


@dataclass
class ErrorCase:
    """错误案例数据结构"""
    id: str                              # 唯一标识
    title: str                           # 错误标题
    category: ErrorCategory              # 错误类别
    severity: ErrorSeverity              # 严重程度
    symptom: str                         # 症状描述
    root_cause: str                      # 根因分析
    bad_pattern: str                     # 错误代码模式
    good_pattern: str                    # 正确代码模式
    fix_instruction: str                 # 修复指令
    detection_hints: List[str] = field(default_factory=list)  # 检测关键词


# ============================================================================
# 错误案例库 - 在此添加新的错误案例
# ============================================================================

ERROR_CASES: List[ErrorCase] = [
    
    # -------------------------------------------------------------------------
    # Case 1: 硬编码列索引提取日期
    # -------------------------------------------------------------------------
    ErrorCase(
        id="ERR_001",
        title="硬编码列索引提取日期",
        category=ErrorCategory.DATE_EXTRACTION,
        severity=ErrorSeverity.CRITICAL,
        symptom="在某些网站正常，换站后 IndexError 或日期错位",
        root_cause="""LLM 看到表头 [项目名称, 主体等级, 债项等级, 评级展望, 公告时间, 下载]，
推断日期在第5列（索引4）。但不同网站列顺序不同，即使同一网站改版后也可能变。
这是"列顺序固定"的假设，属于泛化策略不够保守。""",
        bad_pattern="""# ❌ 错误写法：硬编码列索引
date_elem = tds[4].select_one('span')
date_text = tds[3].get_text()
date = row.query_selector_all('td')[4].inner_text()""",
        good_pattern="""# ✅ 正确写法：智能扫描整行
date = _pygen_smart_find_date_in_row_bs4(tds)  # BeautifulSoup
date = _pygen_smart_find_date_in_row_pw(tds)   # Playwright

# 或手动实现智能扫描
def find_date_in_row(tds):
    import re
    date_re = re.compile(r'(\\d{4}[-/.]\\d{1,2}[-/.]\\d{1,2})')
    for td in tds:
        for tag in ['span', 'time']:
            elem = td.select_one(tag)
            if elem:
                m = date_re.search(elem.get_text(strip=True))
                if m: return m.group(1)
        m = date_re.search(td.get_text(strip=True))
        if m: return m.group(1)
    return ""
""",
        fix_instruction="使用 _pygen_smart_find_date_in_row_* 函数扫描整行，不要假设日期在固定列",
        detection_hints=["tds[", ".get_text(", "date", "query_selector"]
    ),
    
    # -------------------------------------------------------------------------
    # Case 2: tbody tr 链式调用空指针
    # -------------------------------------------------------------------------
    ErrorCase(
        id="ERR_002",
        title="tbody tr 链式调用空指针",
        category=ErrorCategory.HTML_PARSING,
        severity=ErrorSeverity.HIGH,
        symptom="'NoneType' object has no attribute 'find_all'",
        root_cause="""LLM 看到的 HTML: <table><tr>...</tr></table>（无 tbody）
但习惯性写出: table.find('tbody').find_all('tr')
很多教程和训练数据中都用 tbody tr，LLM 没仔细检查 HTML 就用了习惯写法。""",
        bad_pattern="""# ❌ 错误写法：链式调用可能空指针
rows = table.find('tbody').find_all('tr')
items = soup.find('div').find_all('li')
data = container.find('ul').find('li').get_text()""",
        good_pattern="""# ✅ 正确写法1：优先使用 CSS 选择器（返回空列表而非 None）
rows = soup.select('table tbody tr')
rows = soup.select('table tr')  # 若没有 tbody

# ✅ 正确写法2：如果必须用 find，做 None 检查
tbody = table.find('tbody')
rows = tbody.find_all('tr') if tbody else table.find_all('tr')

# ✅ 正确写法3：使用 walrus 操作符
if (tbody := table.find('tbody')):
    rows = tbody.find_all('tr')
else:
    rows = table.find_all('tr')""",
        fix_instruction="优先用 soup.select('table tbody tr')，或对每层 find 结果做 None 检查",
        detection_hints=[".find('tbody').find_all", ".find(", ").find_all("]
    ),
    
    # -------------------------------------------------------------------------
    # Case 3: 只处理第一页日期（分页日期丢失）
    # -------------------------------------------------------------------------
    ErrorCase(
        id="ERR_003",
        title="只处理第一页日期",
        category=ErrorCategory.PAGINATION,
        severity=ErrorSeverity.CRITICAL,
        symptom="第一页有日期，后续页日期全部为空",
        root_cause="""LLM 设计的流程:
1. fetch_page_data() 循环获取所有页面的数据
2. extract_dates_from_rendered_page() 只打开第一页提取日期
3. 合并 → 大部分记录没有日期

LLM 能理解分页，但在设计"日期提取"模块时没有考虑到分页场景，架构思考不完整。""",
        bad_pattern="""# ❌ 错误写法：日期提取与分页分离
def main():
    all_reports = []
    for page in range(1, total_pages + 1):
        reports = fetch_page_data(page)  # 只获取数据，没有日期
        all_reports.extend(reports)
    
    # 只从第一页提取日期！
    dates = extract_dates_from_page(page_url)
    for i, report in enumerate(all_reports):
        report['date'] = dates[i] if i < len(dates) else ''""",
        good_pattern="""# ✅ 正确写法：在同一个循环中提取日期
def fetch_page_data(page_num):
    # ... 获取 HTML/API 响应 ...
    for row in rows:
        tds = row.select('td')
        name = tds[0].get_text(strip=True)
        date = _pygen_smart_find_date_in_row_bs4(tds)  # 同步提取日期
        download_url = ...
        reports.append({
            "name": name,
            "date": date,  # 日期在这里就提取了
            "downloadUrl": download_url,
            "fileType": file_type
        })
    return reports

# ✅ 如果必须用 Playwright 提取日期，每页都要处理
def fetch_all_with_dates():
    all_reports = []
    for page in range(1, total_pages + 1):
        reports = fetch_page_data(page)
        dates = extract_dates_for_page(page)  # 每页都提取日期
        for r, d in zip(reports, dates):
            r['date'] = d
        all_reports.extend(reports)""",
        fix_instruction="在获取每页数据时同步提取日期，不要分成两个阶段处理",
        detection_hints=["extract_dates", "for page", "all_reports"]
    ),
    
    # -------------------------------------------------------------------------
    # Case 4: span vs 直接文本 - 样本偏差
    # -------------------------------------------------------------------------
    ErrorCase(
        id="ERR_004",
        title="假设日期总在 span 标签中",
        category=ErrorCategory.DATE_EXTRACTION,
        severity=ErrorSeverity.MEDIUM,
        symptom="部分网站日期提取为空",
        root_cause="""LLM 看到的样本: <td><span>2026-01-04</span></td>
LLM 假设的: 日期都在 span 里
但实际情况: 有的是 <td>2026-01-04</td>（直接文本）
有的是 <td><time>2026-01-04</time></td>
LLM 可能只关注了部分样本，没有做防御性编程。""",
        bad_pattern="""# ❌ 错误写法：只检查 span
date_elem = td.select_one('span')
date = date_elem.get_text() if date_elem else ''""",
        good_pattern="""# ✅ 正确写法：多策略尝试
def extract_date_from_cell(td):
    import re
    date_re = re.compile(r'(\\d{4}[-/.]\\d{1,2}[-/.]\\d{1,2})')
    
    # 策略1：尝试常见的日期容器标签
    for tag in ['span', 'time', 'em', 'strong']:
        elem = td.select_one(tag)
        if elem:
            m = date_re.search(elem.get_text(strip=True))
            if m:
                return m.group(1)
    
    # 策略2：直接从 td 文本提取
    m = date_re.search(td.get_text(strip=True))
    if m:
        return m.group(1)
    
    return ''""",
        fix_instruction="依次尝试 span/time/直接文本 等多种模式，做防御性编程",
        detection_hints=["select_one('span')", "find('span')"]
    ),
    
    # -------------------------------------------------------------------------
    # Case 5: SPA 页面用 requests 抓 HTML
    # -------------------------------------------------------------------------
    ErrorCase(
        id="ERR_005",
        title="用 requests 抓取 SPA 页面内容",
        category=ErrorCategory.SPA,
        severity=ErrorSeverity.CRITICAL,
        symptom="date 全部为空，或抓取到的内容是空模板",
        root_cause="""SPA（单页应用）的数据是通过 JavaScript 在客户端渲染的。
requests.get() 只能拿到服务端返回的 HTML 骨架，看不到渲染后的内容。
LLM 没有识别出页面是 SPA，或者知道但没正确处理。""",
        bad_pattern="""# ❌ 错误写法：用 requests 抓 SPA 页面
resp = requests.get("https://example.com/#/rating/list")
soup = BeautifulSoup(resp.text, 'html.parser')
dates = soup.select('span.list-time')  # 通常为空！""",
        good_pattern="""# ✅ 正确写法：用 Playwright 渲染后提取
from playwright.sync_api import sync_playwright

def extract_from_spa(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)  # 等待 JS 渲染
        
        # 现在可以拿到渲染后的内容
        items = page.query_selector_all('.list-item')
        results = []
        for item in items:
            date_el = item.query_selector('span.list-time')
            date = date_el.inner_text() if date_el else ''
            results.append(date)
        
        browser.close()
    return results

# ✅ 混合模式：API 获取主数据，Playwright 只用于日期
# 这样可以平衡速度和正确性""",
        fix_instruction="SPA 页面必须用 Playwright 渲染后提取，或者直接调用其 API 接口",
        detection_hints=["requests.get", "/#/", "hash", "spa"]
    ),
    
    # -------------------------------------------------------------------------
    # Case 6: 输出字段名用 title 而不是 name
    # -------------------------------------------------------------------------
    ErrorCase(
        id="ERR_006",
        title="输出字段名使用 title 而不是 name",
        category=ErrorCategory.SCHEMA,
        severity=ErrorSeverity.HIGH,
        symptom="前端无法显示报告名称",
        root_cause="""系统要求输出字段名必须是 name/date/downloadUrl/fileType。
但 LLM 习惯性使用了 title 作为字段名。
这是对输出规范理解不准确导致的。""",
        bad_pattern="""# ❌ 错误写法：使用 title 作为字段名
reports.append({
    "title": item_title,  # 应该是 "name"
    "date": date,
    "url": download_url,   # 应该是 "downloadUrl"
    "type": "pdf"          # 应该是 "fileType"
})""",
        good_pattern="""# ✅ 正确写法：使用规定的字段名
reports.append({
    "name": item_title,       # 必须用 name
    "date": date,
    "downloadUrl": download_url,  # 必须用 downloadUrl
    "fileType": "pdf"             # 必须用 fileType
})""",
        fix_instruction="输出 JSON 必须使用字段名: name, date, downloadUrl, fileType",
        detection_hints=['"title":', "'title':"]
    ),
    
    # -------------------------------------------------------------------------
    # Case 7: 日期范围过滤保留了无日期记录
    # -------------------------------------------------------------------------
    ErrorCase(
        id="ERR_007",
        title="日期范围过滤时保留无日期记录",
        category=ErrorCategory.DATE_EXTRACTION,
        severity=ErrorSeverity.MEDIUM,
        symptom="输出中包含大量无日期的记录",
        root_cause="""用户要求按日期范围过滤，但脚本对无日期记录做了"保留"处理。
这通常是 LLM 想"保证数据完整性"的好意，但违反了用户的过滤要求。""",
        bad_pattern="""# ❌ 错误写法：保留无日期记录
if date_str and start_date <= date_str <= end_date:
    filtered.append(report)
elif not date_str:
    # 无日期也保留
    filtered.append(report)""",
        good_pattern="""# ✅ 正确写法：严格按日期范围过滤
for report in reports:
    date_str = report.get('date', '')
    if not date_str:
        continue  # 无日期直接跳过
    if start_date <= date_str <= end_date:
        filtered.append(report)""",
        fix_instruction="当用户指定日期范围时，无日期记录必须丢弃，不要保留",
        detection_hints=["elif not date", "if not date", "无日期"]
    ),
    
    # -------------------------------------------------------------------------
    # Case 8: 从标题猜测日期
    # -------------------------------------------------------------------------
    ErrorCase(
        id="ERR_008",
        title="从报告标题猜测日期",
        category=ErrorCategory.DATE_EXTRACTION,
        severity=ErrorSeverity.CRITICAL,
        symptom="日期全是年末（12-31）或格式错误",
        root_cause="""LLM 看到标题 "2025年度主动评级报告"，从中提取 2025，
然后拼成 2025-12-31 作为日期。这是完全错误的做法。
报告的发布日期和标题中的年份是不同的概念。""",
        bad_pattern="""# ❌ 错误写法：从标题提取年份作为日期
import re
title = "2025年度主动评级报告"
year = re.search(r'(\\d{4})年', title)
if year:
    date = f"{year.group(1)}-12-31"  # 完全错误！""",
        good_pattern="""# ✅ 正确写法：只从正规日期源获取
# 1. 优先从 API 响应的日期字段获取
date = item.get('rankdate') or item.get('publishtime') or ''

# 2. 从 HTML 的日期元素获取
date = _pygen_smart_find_date_in_row_bs4(tds)

# 3. 如果无法获取，留空而不是猜测
if not date:
    date = ''  # 留空，不要猜""",
        fix_instruction="绝对禁止从标题猜测日期，无法获取时留空",
        detection_hints=["年度", "年报", "12-31"]
    ),
    
    # -------------------------------------------------------------------------
    # Case 9: 静态 HTML 页面未提取日期
    # -------------------------------------------------------------------------
    ErrorCase(
        id="ERR_009",
        title="静态 HTML 页面未提取日期",
        category=ErrorCategory.DATE_EXTRACTION,
        severity=ErrorSeverity.CRITICAL,
        symptom="所有记录的 date 字段为空，即使页面 HTML 中明确有日期显示",
        root_cause="""LLM 生成的代码解析了 HTML 表格提取了标题和下载链接，
但完全遗漏了日期提取逻辑。这通常发生在：
1. LLM 专注于提取主要字段（标题、链接），忘记日期
2. 日期显示在表格中但 LLM 没有识别到对应列
3. 代码只提取了部分字段，没有覆盖完整的输出 schema

这导致最终结果中所有记录的日期为空，被日期范围过滤器全部丢弃。""",
        bad_pattern="""# ❌ 错误写法：遍历表格但没有提取日期
def parse_list(html):
    soup = BeautifulSoup(html, 'html.parser')
    rows = soup.select('table tr')
    results = []
    for row in rows[1:]:
        cols = row.select('td')
        if cols:
            results.append({
                "name": cols[0].get_text(strip=True),
                "downloadUrl": cols[-1].select_one('a')['href'],
                # 缺少 date 字段的提取！
            })
    return results""",
        good_pattern="""# ✅ 正确写法：在同一个循环中同时提取日期
def parse_list(html):
    soup = BeautifulSoup(html, 'html.parser')
    rows = soup.select('table tr')
    results = []
    for row in rows[1:]:
        cols = row.select('td')
        if cols:
            # 使用智能日期扫描函数提取日期
            date = _pygen_smart_find_date_in_row_bs4(cols)
            results.append({
                "name": cols[0].get_text(strip=True),
                "date": date,  # 日期在这里提取
                "downloadUrl": cols[-1].select_one('a')['href'],
                "fileType": "pdf"
            })
    return results""",
        fix_instruction="在遍历表格行时，同步使用 _pygen_smart_find_date_in_row_bs4(tds) 提取日期",
        detection_hints=["soup.select", "table tr", "BeautifulSoup"]
    ),
    
    # -------------------------------------------------------------------------
    # Case 10: print 输出包含非 ASCII 字符导致编码错误
    # -------------------------------------------------------------------------
    ErrorCase(
        id="ERR_010",
        title="print 输出包含非 ASCII 字符导致 GBK 编码错误",
        category=ErrorCategory.ROBUSTNESS,
        severity=ErrorSeverity.HIGH,
        symptom="'gbk' codec can't encode character '\\u2713' (或其他 Unicode 字符)",
        root_cause="""Windows 命令行默认使用 GBK 编码，无法显示某些 Unicode 字符（如 ✓、✗、→ 等）。
当脚本使用 print() 输出这些字符时会报编码错误并终止运行。
这是跨平台兼容性问题，在 Linux/macOS 上通常不会出现。""",
        bad_pattern="""# ❌ 错误写法：使用 Unicode 特殊字符
print(f"✓ 已保存 {len(data)} 条记录")
print("✗ 下载失败")
print("→ 正在处理...")""",
        good_pattern="""# ✅ 正确写法：使用 ASCII 字符或安全输出
print(f"[OK] 已保存 {len(data)} 条记录")
print("[FAIL] 下载失败")
print("-> 正在处理...")

# 或使用安全输出函数
def safe_print(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode('ascii', 'replace').decode())

safe_print("✓ 任务完成")""",
        fix_instruction="避免在 print 中使用 Unicode 特殊字符（✓✗→等），改用 ASCII 字符如 [OK]、[FAIL]、->",
        detection_hints=["print", "✓", "✗", "→", "✔", "✘"]
    ),
    
    # -------------------------------------------------------------------------
    # Case 11: 动态页面使用静态 HTML 解析
    # -------------------------------------------------------------------------
    ErrorCase(
        id="ERR_011",
        title="动态加载页面错误使用静态 HTML 解析",
        category=ErrorCategory.SPA,
        severity=ErrorSeverity.CRITICAL,
        symptom="表格/列表为空（未找到数据行），即使页面在浏览器中显示正常",
        root_cause="""页面数据通过 JavaScript 动态加载（SPA/AJAX），但脚本使用 requests.get() + BeautifulSoup 解析 HTML。
requests 只能获取初始 HTML 骨架，看不到 JavaScript 渲染后填充的数据。
常见表现：
1. "未找到数据行" 或 "未找到表格"
2. 表格存在但 tbody 为空
3. API 请求被捕获但代码没有使用

正确做法是直接调用捕获到的 API 接口获取 JSON 数据。""",
        bad_pattern="""# ❌ 错误写法：用 requests 解析动态加载的页面
import requests
from bs4 import BeautifulSoup

def fetch_data():
    response = requests.get("https://example.com/list.html")
    soup = BeautifulSoup(response.text, 'html.parser')
    # 表格存在但 tbody 是空的！因为数据是 JS 填充的
    rows = table.select('tbody tr')  # 返回空列表
    # ...

# 问题：API 请求信息已经提供，但代码没有使用""",
        good_pattern="""# ✅ 正确写法：直接调用 API 获取 JSON 数据
import requests

# 使用捕获到的 API 端点
API_URL = "https://example.com/api/list"

def fetch_data(page=1):
    params = {
        "pageNo": page,
        "pageSize": 20,
    }
    response = requests.get(API_URL, params=params, headers=HEADERS)
    data = response.json()
    
    reports = []
    for item in data.get("data", {}).get("rows", []):
        reports.append({
            "name": item.get("title", ""),
            "date": item.get("rankdate", ""),
            "downloadUrl": item.get("fileUrl", ""),
            "fileType": "pdf"
        })
    return reports

# 关键：从 API 响应中提取数据，不要解析 HTML""",
        fix_instruction="当捕获到 API 请求时，必须使用 requests 调用 API 获取 JSON 数据，而不是解析 HTML",
        detection_hints=["BeautifulSoup", "tbody tr", "未找到", "table.select"]
    ),
    
]


def get_error_cases_prompt(
    categories: List[ErrorCategory] = None,
    severity_threshold: ErrorSeverity = ErrorSeverity.LOW
) -> str:
    """
    生成错误案例的 Prompt 文本
    
    Args:
        categories: 要包含的错误类别，None 表示全部
        severity_threshold: 严重程度阈值，只包含大于等于此级别的错误
    
    Returns:
        格式化的 Prompt 文本
    """
    severity_order = {
        ErrorSeverity.CRITICAL: 4,
        ErrorSeverity.HIGH: 3,
        ErrorSeverity.MEDIUM: 2,
        ErrorSeverity.LOW: 1
    }
    
    threshold = severity_order[severity_threshold]
    
    filtered_cases = []
    for case in ERROR_CASES:
        # 过滤类别
        if categories and case.category not in categories:
            continue
        # 过滤严重程度
        if severity_order[case.severity] < threshold:
            continue
        filtered_cases.append(case)
    
    if not filtered_cases:
        return ""

    try:
        from prompts import load as load_prompt
    except ImportError:
        from .prompts import load as load_prompt  # type: ignore

    lines = [load_prompt("errors/cases_header.md").rstrip("\n")]
    
    for i, case in enumerate(filtered_cases, 1):
        severity_emoji = {
            ErrorSeverity.CRITICAL: "🔴",
            ErrorSeverity.HIGH: "🟠",
            ErrorSeverity.MEDIUM: "🟡",
            ErrorSeverity.LOW: "🟢"
        }[case.severity]
        
        # 精简版：只提供“错误 + 应该怎么写”，不提供任何示例代码块
        lines.extend([
            f"### Case {i}: {case.title} {severity_emoji}",
            f"- 错误：{case.symptom}",
            f"- 应该：{case.fix_instruction}",
            ""
        ])
    
    return "\n".join(lines)


def get_detection_patterns() -> Dict[str, ErrorCase]:
    """
    获取用于检测错误的模式
    
    Returns:
        {检测关键词: 对应的错误案例} 的映射
    """
    patterns = {}
    for case in ERROR_CASES:
        for hint in case.detection_hints:
            patterns[hint] = case
    return patterns


def get_error_case_by_id(error_id: str) -> Optional[ErrorCase]:
    """根据 ID 获取错误案例"""
    for case in ERROR_CASES:
        if case.id == error_id:
            return case
    return None


def add_error_case(case: ErrorCase) -> None:
    """动态添加错误案例（运行时扩展）"""
    # 检查 ID 是否重复
    existing_ids = {c.id for c in ERROR_CASES}
    if case.id in existing_ids:
        raise ValueError(f"错误案例 ID '{case.id}' 已存在")
    ERROR_CASES.append(case)


# 导出类型注解用
from typing import Optional


if __name__ == "__main__":
    # 测试：打印所有错误案例
    print(get_error_cases_prompt())
    print("\n" + "=" * 60 + "\n")
    print("检测模式:", list(get_detection_patterns().keys()))

