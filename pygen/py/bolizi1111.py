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
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# ==========================================
# 配置区域
# ==========================================
TARGET_URL = "https://www.centralbank.org.bz/publications-search"
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
MAX_ITEMS = 5  # 限制爬取前5条

# ==========================================
# 辅助函数
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
    尝试解析多种格式的日期字符串，返回 YYYY-MM-DD 格式
    """
    if not date_str:
        return ""
    
    # 清理多余空格
    date_str = date_str.strip()
    
    # 常见格式尝试
    formats = [
        "%d %B %Y",      # 25 February 2026
        "%B %d, %Y",     # February 25, 2026
        "%Y-%m-%d",      # 2026-02-25
        "%d/%m/%Y",      # 25/02/2026
        "%m/%d/%Y"       # 02/25/2026
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
            
    # 尝试正则提取 (例如从 "Published on: 25 February 2026" 中提取)
    match = re.search(r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})', date_str)
    if match:
        try:
            day, month, year = match.groups()
            dt = datetime.strptime(f"{day} {month} {year}", "%d %B %Y")
            return dt.strftime("%Y-%m-%d")
        except:
            pass
            
    match = re.search(r'([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})', date_str)
    if match:
        try:
            month, day, year = match.groups()
            dt = datetime.strptime(f"{month} {day}, {year}", "%B %d, %Y")
            return dt.strftime("%Y-%m-%d")
        except:
            pass

    return date_str  # 如果无法解析，返回原字符串

def save_results(articles: list, output_path: str):
    """保存结果为JSON"""
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    # 确保目录存在
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已保存 {len(articles)} 条新闻到 {output_path}")

# ==========================================
# 爬虫主逻辑
# ==========================================

def crawl_news():
    print(f"开始爬取: {TARGET_URL}")
    
    # 启动 Playwright
    with sync_playwright() as p:
        # 配置浏览器（反爬设置）
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        )
        page = context.new_page()
        
        try:
            # 1. 访问列表页
            page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
            
            # 等待列表元素加载
            # 注意：页面可能有多个 .item-list，我们需要定位到主内容区域的列表
            # 根据HTML结构，主内容在 .interior-layout-main 下
            list_selector = ".interior-layout-main .item-list__item"
            try:
                page.wait_for_selector(list_selector, timeout=15000)
            except:
                print("未找到列表元素，可能页面加载失败或无数据")
                return []

            articles = []
            
            # 2. 提取列表项
            items = page.query_selector_all(list_selector)
            print(f"发现 {len(items)} 条列表数据，准备抓取前 {MAX_ITEMS} 条...")
            
            for i, item in enumerate(items):
                if i >= MAX_ITEMS:
                    break
                
                article = {}
                
                # 提取标题和链接
                title_el = item.query_selector("a.item-list__title")
                if title_el:
                    article['title'] = title_el.inner_text().strip()
                    href = title_el.get_attribute("href")
                    article['sourceUrl'] = urljoin(TARGET_URL, href)
                else:
                    continue # 没有标题跳过
                
                # 提取列表页显示的日期（如果有）
                date_el = item.query_selector("p.item-list__date")
                if date_el:
                    raw_date = date_el.inner_text().strip()
                    article['date'] = parse_date(raw_date)
                
                # 提取摘要
                desc_el = item.query_selector(".item-list__description")
                if desc_el:
                    article['summary'] = desc_el.inner_text().strip()
                
                article['source'] = "Central Bank of Belize"
                article['id'] = str(i + 1)
                
                articles.append(article)
            
            # 3. 进入详情页抓取正文和补充日期
            print(f"开始处理 {len(articles)} 个详情页...")
            
            for article in articles:
                if not article.get('sourceUrl'):
                    continue
                    
                print(f"正在抓取详情: {article['title']}")
                try:
                    # 访问详情页
                    page.goto(article['sourceUrl'], wait_until="domcontentloaded", timeout=30000)
                    
                    # 尝试定位正文
                    # 优先使用 div.group.margin-large，其次 div.group，再次 #top
                    content_selectors = [
                        "div.group.margin-large",
                        "div.group",
                        "#top"
                    ]
                    
                    content_html = ""
                    for selector in content_selectors:
                        if page.locator(selector).count() > 0:
                            # 排除一些非正文元素，如面包屑、标题等（如果包含在容器内）
                            # 这里直接获取innerHTML
                            content_html = page.locator(selector).first.inner_html()
                            # 如果内容太短，可能选错了，继续尝试下一个
                            if len(content_html) > 200: 
                                break
                    
                    # 清洗内容（修复图片链接）
                    article['content'] = clean_html_content(content_html, article['sourceUrl'])
                    
                    # 如果列表页没有日期，尝试在详情页查找
                    if not article.get('date'):
                        # 尝试查找详情页的日期元素
                        # 常见选择器猜测：.item-list__date, .date, time, 或者在正文中正则匹配
                        detail_date_el = page.query_selector(".item-list__date")
                        if detail_date_el:
                            article['date'] = parse_date(detail_date_el.inner_text())
                        else:
                            # 尝试从正文文本前部查找日期
                            text_content = page.locator("body").inner_text()
                            # 截取前1000个字符查找日期
                            found_date = parse_date(text_content[:1000])
                            if found_date != text_content[:1000]: # 如果解析成功（parse_date返回了不同于输入的字符串）
                                article['date'] = found_date
                    
                    # 随机等待，避免请求过快
                    time.sleep(1)
                    
                except Exception as e:
                    print(f"抓取详情页失败 {article['sourceUrl']}: {e}")
            
            return articles

        except Exception as e:
            print(f"爬虫运行出错: {e}")
            return []
        finally:
            browser.close()

def main():
    # 确保输出目录存在
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        
    # 执行爬取
    articles = crawl_news()
    
    # 生成文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"centralbank_belize_news_{timestamp}.json"
    output_path = os.path.join(OUTPUT_DIR, filename)
    
    # 保存结果
    if articles:
        save_results(articles, output_path)
    else:
        print("未抓取到任何数据")

if __name__ == "__main__":
    main()
