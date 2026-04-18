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
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ================= 配置区域 =================
# 目标 URL
START_URL = "https://www.centralbank.org.bz/publications-search"
# 基础 URL (用于拼接相对路径)
BASE_URL = "https://www.centralbank.org.bz"
# 最大抓取数量 (根据任务目标设为 5)
MAX_ITEMS = 5
# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
# 请求间隔 (秒)
DELAY = 2

# ================= 工具函数 =================

def setup_output_dir():
    """创建输出目录"""
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        print(f"创建目录: {OUTPUT_DIR}")

def clean_html_content(html_content, base_url):
    """
    清洗 HTML 内容：
    1. 将相对路径图片/链接转换为绝对路径
    2. 移除无用标签
    """
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 修复图片链接
        for img in soup.find_all('img'):
            if img.get('src'):
                # 忽略 base64 图片
                if not img['src'].startswith('data:'):
                    img['src'] = urljoin(base_url, img['src'])
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                href = a['href']
                if not href.startswith(('http', 'https', 'mailto', 'tel', '#')):
                    a['href'] = urljoin(base_url, href)
                
        # 移除脚本和样式
        for script in soup(["script", "style", "iframe", "noscript"]):
            script.decompose()

        return str(soup)
    except Exception as e:
        print(f"内容清洗出错: {e}")
        return html_content

def parse_custom_date(date_str):
    """解析日期字符串，支持多种格式"""
    if not date_str:
        return ""
    
    date_str = date_str.strip()
    # 常见格式尝试
    formats = [
        "%d %B %Y",       # 23 February 2026
        "%B %d, %Y",      # February 23, 2026
        "%Y-%m-%d",       # 2026-02-23
        "%d/%m/%Y",       # 23/02/2026
        "%m/%d/%Y"        # 02/23/2026
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
            
    # 如果都失败，尝试用正则提取 YYYY-MM-DD
    match = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', date_str)
    if match:
        return f"{match.group(1)}-{match.group(2).zfill(2)}-{match.group(3).zfill(2)}"
    
    return date_str  # 如果无法解析，返回原字符串

def is_file_url(url):
    """判断是否为文件下载链接"""
    if not url:
        return False
    path = urlparse(url).path.lower()
    extensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.zip', '.rar', '.csv']
    return any(path.endswith(ext) for ext in extensions)

# ================= 爬虫主逻辑 =================

def crawl_news():
    articles = []
    
    # 启动 Playwright
    with sync_playwright() as p:
        # 配置浏览器 (反爬设置)
        browser = p.chromium.launch(
            headless=True,  # 设置为 False 可视化调试
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US"
        )
        
        page = context.new_page()
        
        try:
            print(f"正在访问列表页: {START_URL}")
            page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
            
            # 等待列表元素加载
            # 注意：页面可能有多个 .item-list，我们需要主内容区域的那个
            # 根据 HTML 结构，主内容在 .interior-layout-main 下
            list_selector = ".interior-layout-main .item-list__item"
            try:
                page.wait_for_selector(list_selector, timeout=15000)
            except:
                print("未找到列表元素，尝试备用选择器...")
                # 备用：直接找所有 item-list__item，但在处理时过滤
                list_selector = ".item-list__item"
                page.wait_for_selector(list_selector, timeout=10000)

            # 开始循环翻页抓取 (虽然只抓5条，但保留翻页逻辑结构)
            while len(articles) < MAX_ITEMS:
                # 获取当前页所有列表项
                items = page.locator(list_selector).all()
                print(f"当前页发现 {len(items)} 条数据")
                
                if not items:
                    print("当前页无数据，停止抓取")
                    break
                
                for item in items:
                    if len(articles) >= MAX_ITEMS:
                        break
                        
                    try:
                        # 1. 提取基础信息
                        title_el = item.locator("a.item-list__title")
                        if not title_el.count():
                            continue
                            
                        title = title_el.inner_text().strip()
                        link = title_el.get_attribute("href")
                        if link:
                            link = urljoin(BASE_URL, link)
                        
                        # 提取日期 (列表页通常有日期)
                        date_str = ""
                        date_el = item.locator("p.item-list__date")
                        if date_el.count():
                            date_text = date_el.inner_text().strip()
                            date_str = parse_custom_date(date_text)
                        
                        # 提取摘要
                        summary = ""
                        desc_el = item.locator(".item-list__description")
                        if desc_el.count():
                            summary = desc_el.inner_text().strip()

                        article = {
                            "title": title,
                            "date": date_str,
                            "source": "Central Bank of Belize",
                            "author": "",  # 网站未明确标注作者
                            "sourceUrl": link,
                            "summary": summary,
                            "content": ""
                        }
                        
                        print(f"发现文章: {title} ({date_str})")
                        articles.append(article)
                        
                    except Exception as e:
                        print(f"提取列表项失败: {e}")
                        continue
                
                # 检查是否达到数量限制
                if len(articles) >= MAX_ITEMS:
                    print(f"已达到目标数量 {MAX_ITEMS}，停止翻页")
                    break
                
                # 翻页逻辑 (如果需要更多数据)
                # 寻找下一页按钮
                # 根据 HTML，分页是 ul.pagination 下的链接
                # 这里简单处理：如果还需要数据，尝试点击下一页
                # 由于任务只要求前5条，通常第一页就够了，这里仅作为框架保留
                break 

            # ================= 详情页抓取 =================
            print(f"\n开始抓取 {len(articles)} 条详情内容...")
            
            for i, article in enumerate(articles):
                url = article['sourceUrl']
                if not url:
                    continue
                    
                print(f"[{i+1}/{len(articles)}] 处理: {article['title']}")
                
                # 检查是否为文件链接
                if is_file_url(url):
                    print(f"  -> 检测到文件链接，跳过正文提取: {url}")
                    article['content'] = f'<a href="{url}" target="_blank">Download File: {os.path.basename(url)}</a>'
                    continue
                
                try:
                    # 访问详情页
                    # 使用 try-except 捕获可能的下载行为或导航错误
                    try:
                        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        if response and response.status >= 400:
                            print(f"  -> 页面访问失败，状态码: {response.status}")
                            continue
                    except Exception as nav_err:
                        # 如果是下载导致的中断，视为文件
                        if "download" in str(nav_err).lower() or "net::ERR_ABORTED" in str(nav_err):
                            print(f"  -> 触发了下载，保存为文件链接")
                            article['content'] = f'<a href="{url}" target="_blank">Download File</a>'
                            continue
                        else:
                            raise nav_err

                    # 等待内容加载
                    time.sleep(1) # 稍作等待确保动态内容加载
                    
                    # 提取正文
                    # 策略：优先使用 .interior-layout-main，其次 #top
                    content_html = ""
                    
                    # 尝试定位主要内容区域
                    # 排除侧边栏 (.interior-layout__aside) 和导航 (.interior-layout__navigation)
                    main_content = page.locator(".interior-layout-main")
                    if main_content.count():
                        # 移除分页和列表（如果是列表页误判）
                        content_html = main_content.inner_html()
                    else:
                        # 备用：尝试 #top 但需要清洗
                        top_content = page.locator("#top")
                        if top_content.count():
                            content_html = top_content.inner_html()
                    
                    # 如果正文为空，可能页面结构不同
                    if not content_html or len(content_html) < 100:
                        # 尝试找 div.group
                        group_div = page.locator("div.group.margin-large")
                        if group_div.count():
                            content_html = group_div.inner_html()

                    # 清洗内容
                    if content_html:
                        # 使用 BeautifulSoup 进一步清洗（移除侧边栏等噪音）
                        soup = BeautifulSoup(content_html, 'html.parser')
                        # 移除面包屑
                        for breadcrumb in soup.select(".interior-layout__title"):
                            breadcrumb.decompose()
                        # 移除分页
                        for pagination in soup.select(".pagination"):
                            pagination.decompose()
                        
                        article['content'] = clean_html_content(str(soup), url)
                    
                    # 如果列表页没有日期，尝试在详情页找
                    if not article['date']:
                        # 尝试找日期文本
                        page_text = page.locator("body").inner_text()
                        # 简单正则匹配日期
                        date_match = re.search(r'(\d{1,2}\s+[A-Za-z]+\s+\d{4})', page_text[:1000]) # 只在前1000字找
                        if date_match:
                            article['date'] = parse_custom_date(date_match.group(1))
                            print(f"  -> 详情页补全日期: {article['date']}")

                    # 随机延时
                    time.sleep(DELAY)
                    
                except Exception as e:
                    print(f"  -> 详情页抓取失败: {e}")
                    # 保留已有信息
        
        except Exception as e:
            print(f"爬虫运行出错: {e}")
        finally:
            browser.close()
            
    return articles

def save_results(articles):
    """保存结果为 JSON"""
    if not articles:
        print("没有抓取到数据，跳过保存")
        return

    setup_output_dir()
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"centralbank_belize_news_{timestamp}.json"
    filepath = os.path.join(OUTPUT_DIR, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n成功保存 {len(articles)} 条数据到: {filepath}")
    except Exception as e:
        print(f"保存文件失败: {e}")

def main():
    print("=== 开始爬取伯利兹中央银行新闻 ===")
    start_time = time.time()
    
    articles = crawl_news()
    save_results(articles)
    
    duration = time.time() - start_time
    print(f"=== 任务完成，耗时 {duration:.2f} 秒 ===")

if __name__ == "__main__":
    main()
