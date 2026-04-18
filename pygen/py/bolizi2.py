# === PyGen 注入：HTTP/SSL 韧性层（通用） ===
# 说明：
# 1) 优先保持 verify=True；如确需临时绕过（不推荐），可设置环境变量 PYGEN_INSECURE_SSL=1
# 2) 默认遇到 418/403/429 仅做一次"预热 cookie + 浏览器化 headers"的重试
# 3) 若仍被拦截且本机已安装 Playwright，会自动尝试一次 request-context 兜底
#    如需禁用：设置 PYGEN_DISABLE_PLAYWRIGHT_FALLBACK=1
import os as _pygen_os
import ssl as _pygen_ssl
import urllib.parse as _pygen_urlparse

try:
    # truststore 会让 Python/requests 使用系统证书库（Windows/macOS/Linux），更贴近浏览器行为
    import truststore as _pygen_truststore  # type: ignore
    _pygen_truststore.inject_into_ssl()
except Exception:
    _pygen_truststore = None  # noqa: F401

try:
    import requests as _pygen_requests
    from requests.structures import CaseInsensitiveDict as _pygen_CaseInsensitiveDict
except Exception:
    _pygen_requests = None  # noqa: F401

_PYGEN_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0 Safari/537.36"
)

def _pygen_origin(url: str) -> str:
    try:
        p = _pygen_urlparse.urlsplit(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return ""

def _pygen_merge_headers(h: dict | None) -> dict:
    base = {
        "User-Agent": _PYGEN_DEFAULT_UA,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }
    if h:
        for k, v in h.items():
            if v is not None:
                base[k] = v
    return base

def _pygen_make_response(status: int, url: str, headers: dict, body: bytes):
    r = _pygen_requests.Response()
    r.status_code = status
    r.url = url
    r.headers = _pygen_CaseInsensitiveDict(headers or {})
    r._content = body or b""
    return r

def _pygen_playwright_fetch(method: str, url: str, headers: dict, data=None, json=None, timeout: int = 30):
    """
    兜底：使用 Playwright 的 request context（更像浏览器/更容易过 WAF）。
    仅当已安装 playwright 且未设置 PYGEN_DISABLE_PLAYWRIGHT_FALLBACK=1 时启用。
    """
    if _pygen_os.getenv("PYGEN_DISABLE_PLAYWRIGHT_FALLBACK") == "1":
        return None
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return None

    with sync_playwright() as p:
        ctx = p.request.new_context(ignore_https_errors=_pygen_os.getenv("PYGEN_INSECURE_SSL") == "1",
                                    extra_http_headers=headers)
        try:
            resp = ctx.fetch(url, method=method.upper(), data=data, json=json, timeout=timeout * 1000)
            body = resp.body()
            return _pygen_make_response(resp.status, url, dict(resp.headers), body)
        finally:
            ctx.dispose()

def _pygen_install_requests_patch():
    if _pygen_requests is None:
        return
    _orig = _pygen_requests.sessions.Session.request

    def _patched(self, method, url, **kwargs):
        # 默认超时，避免脚本卡死
        kwargs.setdefault("timeout", 30)

        # 证书校验策略：默认开启；仅在用户明确设置时关闭
        if _pygen_os.getenv("PYGEN_INSECURE_SSL") == "1":
            kwargs["verify"] = False

        # 合并"更像浏览器"的 headers（很多站会对 UA/Accept 做拦截）
        headers = _pygen_merge_headers(kwargs.get("headers"))
        kwargs["headers"] = headers

        # 首次请求
        try:
            resp = _orig(self, method, url, **kwargs)
        except Exception as e:
            # 若是 SSL 校验错误：truststore 已注入则直接抛出；否则给出更友好的提示
            msg = str(e)
            if "CERTIFICATE_VERIFY_FAILED" in msg or "certificate verify failed" in msg:
                # 若 playwright 可用，尝试用 request-context 做一次兜底（有些环境下系统信任链更完整）
                try:
                    pw_resp = _pygen_playwright_fetch(method, url, headers=headers,
                                                      data=kwargs.get("data"),
                                                      json=kwargs.get("json"),
                                                      timeout=int(kwargs.get("timeout") or 30))
                    if pw_resp is not None:
                        return pw_resp
                except Exception:
                    pass
                raise
            raise

        # WAF/反爬：做一次轻量重试（预热 cookie + 再请求）
        if resp is not None and getattr(resp, "status_code", 0) in (418, 403, 429):
            try:
                origin = _pygen_origin(url)
                if origin:
                    # 预热：访问首页拿 cookie
                    _ = _orig(self, "GET", origin + "/", headers=headers, timeout=15, verify=kwargs.get("verify", True))
                resp2 = _orig(self, method, url, **kwargs)
                if resp2 is not None and getattr(resp2, "status_code", 0) not in (418, 403, 429):
                    return resp2
                # 若仍被拦截，且 playwright 可用，则再尝试一次（自动兜底）
                pw_resp = _pygen_playwright_fetch(method, url, headers=headers,
                                                  data=kwargs.get("data"),
                                                  json=kwargs.get("json"),
                                                  timeout=int(kwargs.get("timeout") or 30))
                if pw_resp is not None:
                    return pw_resp
            except Exception:
                # 兜底失败则返回原始响应，交由上层处理
                return resp

        return resp

    _pygen_requests.sessions.Session.request = _patched

_pygen_install_requests_patch()
# === PyGen 注入结束 ===
import os
import json
import time
import random
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup

# ==========================================
# 配置区域
# ==========================================
MAX_ITEMS = 5  # 限制爬取数量（用户要求前5条）
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
BASE_URL = "https://www.centralbank.org.bz"
START_URL = "https://www.centralbank.org.bz/publications-search"

# ==========================================
# 工具函数
# ==========================================

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：
    1. 将相对路径转换为绝对路径
    2. 移除无用标签
    """
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 修复图片链接
        for img in soup.find_all('img'):
            if img.get('src'):
                img['src'] = urljoin(base_url, img['src'])
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
        
        # 移除脚本和样式
        for script in soup(["script", "style"]):
            script.decompose()

        return str(soup)
    except Exception as e:
        print(f"内容清洗出错: {e}")
        return html_content

def parse_date(date_str):
    """
    解析日期字符串，尝试多种格式
    示例: "23 February 2026" -> "2026-02-23"
    """
    if not date_str:
        return ""
    
    date_str = date_str.strip()
    formats = [
        "%d %B %Y",      # 23 February 2026
        "%B %d, %Y",     # February 23, 2026
        "%Y-%m-%d",      # 2026-02-23
        "%d/%m/%Y",      # 23/02/2026
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
            
    # 如果无法解析，尝试提取年份和月份
    try:
        # 简单的正则提取 YYYY-MM-DD
        match = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', date_str)
        if match:
            return f"{match.group(1)}-{match.group(2).zfill(2)}-{match.group(3).zfill(2)}"
    except:
        pass
        
    return date_str  # 如果都失败，返回原字符串

def is_file_url(url):
    """判断是否为文件链接"""
    if not url:
        return False
    path = urlparse(url).path.lower()
    extensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.zip', '.rar']
    return any(path.endswith(ext) for ext in extensions)

def save_results(articles, output_path):
    """保存结果为JSON"""
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已保存 {len(articles)} 条新闻到 {output_path}")

# ==========================================
# 爬虫主逻辑
# ==========================================

def crawl_news():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    articles = []
    
    with sync_playwright() as p:
        # 启动浏览器，配置反爬参数
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US"
        )
        
        page = context.new_page()
        
        # 分页循环
        start_row = 0
        rows_per_page = 20
        
        while len(articles) < MAX_ITEMS:
            # 构造分页 URL
            current_url = f"{START_URL}?startRow={start_row}&rowsPerPage={rows_per_page}"
            print(f"正在访问列表页: {current_url}")
            
            try:
                page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_selector(".item-list__item", timeout=10000)
            except Exception as e:
                print(f"加载列表页失败: {e}")
                break
            
            # 提取列表项
            items = page.query_selector_all(".item-list__item")
            if not items:
                print("未找到列表项，停止翻页")
                break
                
            print(f"当前页找到 {len(items)} 条记录")
            
            for item in items:
                if len(articles) >= MAX_ITEMS:
                    break
                
                try:
                    # 提取基础信息
                    title_elem = item.query_selector("a.item-list__title")
                    if not title_elem:
                        continue
                        
                    title = title_elem.inner_text().strip()
                    link = title_elem.get_attribute("href")
                    if link:
                        link = urljoin(BASE_URL, link)
                    
                    # 尝试从列表页提取日期
                    date_elem = item.query_selector("p.item-list__date")
                    date_str = date_elem.inner_text().strip() if date_elem else ""
                    formatted_date = parse_date(date_str)
                    
                    # 提取摘要
                    summary_elem = item.query_selector(".item-list__description")
                    summary = summary_elem.inner_text().strip() if summary_elem else ""
                    
                    article = {
                        "id": str(len(articles) + 1),
                        "title": title,
                        "date": formatted_date,
                        "source": "Central Bank of Belize",
                        "author": "",
                        "sourceUrl": link,
                        "summary": summary,
                        "content": ""
                    }
                    
                    print(f"正在处理: {title}")
                    
                    # 处理详情页
                    if link:
                        if is_file_url(link):
                            print(f"  - 检测到文件链接，跳过详情页抓取: {link}")
                            article["content"] = f'<a href="{link}" target="_blank">Download Document</a>'
                        else:
                            # 打开新页面处理详情，避免破坏列表页状态
                            detail_page = context.new_page()
                            try:
                                detail_page.goto(link, wait_until="domcontentloaded", timeout=30000)
                                
                                # 尝试提取正文
                                # 策略1: div.group.margin-large (Agent Strategy 推荐)
                                content_html = ""
                                content_elem = detail_page.query_selector("div.group.margin-large")
                                
                                # 策略2: #top (Probe 推荐)
                                if not content_elem:
                                    content_elem = detail_page.query_selector("#top")
                                
                                # 策略3: div.group (通用)
                                if not content_elem:
                                    content_elem = detail_page.query_selector("div.group")
                                
                                if content_elem:
                                    content_html = content_elem.inner_html()
                                    # 清洗内容
                                    article["content"] = clean_html_content(content_html, link)
                                else:
                                    print("  - 未找到正文内容")
                                
                                # 如果列表页没有日期，尝试从详情页提取
                                if not formatted_date:
                                    # 策略: .item-list__date.detail-page p
                                    detail_date_elem = detail_page.query_selector(".item-list__date.detail-page p")
                                    if detail_date_elem:
                                        detail_date_str = detail_date_elem.inner_text().strip()
                                        article["date"] = parse_date(detail_date_str)
                                    
                                    # 备选策略: 查找包含日期的文本节点
                                    if not article["date"]:
                                        # 简单的文本搜索，寻找类似 "February 25, 2026" 的文本
                                        body_text = detail_page.inner_text("body")
                                        date_match = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}', body_text)
                                        if date_match:
                                            article["date"] = parse_date(date_match.group(0))

                            except Exception as e:
                                print(f"  - 详情页抓取失败: {e}")
                                # 如果详情页失败，保留摘要作为内容
                                if not article["content"]:
                                    article["content"] = f"<p>{summary}</p>"
                            finally:
                                detail_page.close()
                                
                    articles.append(article)
                    # 随机延迟
                    time.sleep(random.uniform(1, 2))
                    
                except Exception as e:
                    print(f"处理单条新闻出错: {e}")
                    continue
            
            # 翻页逻辑
            start_row += rows_per_page
            
            # 检查是否还有下一页
            # 如果当前页获取的数量少于 rows_per_page，说明是最后一页
            if len(items) < rows_per_page:
                print("已到达最后一页")
                break
                
            # 额外的安全检查：检查下一页按钮是否存在（虽然我们是用 URL 参数翻页，但检查 UI 状态是个好习惯）
            # 这里的下一页按钮选择器可能是 .pagination li:last-child a 或类似结构
            # 简单起见，依赖 len(items) 判断即可
            
        browser.close()
        
    return articles

def main():
    print("开始爬取 Central Bank of Belize 新闻...")
    try:
        articles = crawl_news()
        
        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"centralbank_belize_news_{timestamp}.json"
        output_path = os.path.join(OUTPUT_DIR, filename)
        
        save_results(articles, output_path)
        
    except Exception as e:
        print(f"爬虫运行出错: {e}")

if __name__ == "__main__":
    main()
