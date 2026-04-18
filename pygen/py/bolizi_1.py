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
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ================= 配置区域 =================
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
TARGET_URL = "https://www.centralbank.org.bz/publications-search"
MAX_ITEMS = 5  # 限制抓取前5条
TIMEOUT = 30000  # 请求超时时间 (ms)

# ================= 工具函数 =================

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：
    1. 修复相对路径的图片和链接
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
        for script in soup(["script", "style", "iframe"]):
            script.decompose()

        return str(soup)
    except Exception as e:
        print(f"[Warn] 内容清洗出错: {e}")
        return html_content

def parse_date(date_str):
    """
    解析日期字符串，支持多种格式
    例如: "23 February 2026" -> "2026-02-23"
    """
    if not date_str:
        return ""
    
    date_str = date_str.strip()
    
    # 常见格式尝试
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
            
    # 尝试提取年份和月份进行模糊匹配（如果需要更复杂的逻辑可以在此添加）
    return date_str

def save_results(articles, output_path):
    """保存结果为JSON"""
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[Success] 已保存 {len(articles)} 条新闻到 {output_path}")

# ================= 爬虫主逻辑 =================

def crawl_news():
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
        
        try:
            print(f"[Info] 正在访问列表页: {TARGET_URL}")
            page.goto(TARGET_URL, timeout=TIMEOUT, wait_until="domcontentloaded")
            
            # 等待列表元素加载
            # 使用 .interior-layout-main .item-list__item 避免抓取到侧边栏的内容
            list_selector = ".interior-layout-main .item-list .item-list__item"
            try:
                page.wait_for_selector(list_selector, timeout=10000)
            except:
                print("[Warn] 未找到新闻列表，可能是页面结构变化或加载失败")
                return []

            # 获取所有列表项句柄
            items = page.query_selector_all(list_selector)
            print(f"[Info] 发现 {len(items)} 条记录，将抓取前 {MAX_ITEMS} 条")
            
            # 提取列表页基本信息
            links_to_crawl = []
            
            for i, item in enumerate(items):
                if i >= MAX_ITEMS:
                    break
                
                try:
                    # 提取标题和链接
                    title_el = item.query_selector("a.item-list__title")
                    if not title_el:
                        continue
                        
                    title = title_el.inner_text().strip()
                    url = title_el.get_attribute("href")
                    if url:
                        url = urljoin(TARGET_URL, url)
                    
                    # 提取日期
                    date_el = item.query_selector(".item-list__date")
                    date_str = date_el.inner_text().strip() if date_el else ""
                    formatted_date = parse_date(date_str)
                    
                    # 提取摘要
                    summary_el = item.query_selector(".item-list__description")
                    summary = summary_el.inner_text().strip() if summary_el else ""
                    
                    article_info = {
                        "id": str(i + 1),
                        "title": title,
                        "date": formatted_date,
                        "source": "Central Bank of Belize",
                        "sourceUrl": url,
                        "summary": summary,
                        "content": "" # 稍后填充
                    }
                    
                    links_to_crawl.append(article_info)
                    print(f"[List] 提取成功: {title} ({formatted_date})")
                    
                except Exception as e:
                    print(f"[Error] 提取列表项 {i+1} 失败: {e}")
                    continue
            
            # 逐个进入详情页抓取
            for article in links_to_crawl:
                if not article['sourceUrl']:
                    continue
                    
                print(f"[Detail] 正在抓取: {article['title']}")
                try:
                    # 访问详情页
                    detail_page = context.new_page()
                    detail_page.goto(article['sourceUrl'], timeout=TIMEOUT, wait_until="domcontentloaded")
                    
                    # 等待内容加载
                    # 优先使用 #top，备选 div.group
                    content_selector = "#top"
                    try:
                        detail_page.wait_for_selector(content_selector, timeout=10000)
                    except:
                        print(f"[Warn] 未找到主要内容区域 {content_selector}，尝试备选方案")
                        content_selector = "div.group"
                        if not detail_page.query_selector(content_selector):
                            # 如果都找不到，尝试 body
                            content_selector = "body"

                    # 提取正文 HTML
                    content_html = detail_page.inner_html(content_selector)
                    
                    # 如果列表页没有日期，尝试在详情页找
                    if not article['date']:
                        # 尝试常见的日期选择器
                        detail_date_el = detail_page.query_selector(".date, .timestamp, time")
                        if detail_date_el:
                            d_str = detail_date_el.inner_text().strip()
                            article['date'] = parse_date(d_str)
                    
                    # 清洗内容
                    article['content'] = clean_html_content(content_html, article['sourceUrl'])
                    
                    detail_page.close()
                    
                    # 礼貌性延迟
                    time.sleep(1)
                    
                except Exception as e:
                    print(f"[Error] 抓取详情页失败 {article['sourceUrl']}: {e}")
                    # 即使详情页失败，也保留列表页信息
                
                articles.append(article)

        except Exception as e:
            print(f"[Fatal] 爬虫运行出错: {e}")
        finally:
            browser.close()
            
    return articles

def main():
    print("=== 开始爬取 Belize Central Bank 新闻 ===")
    
    # 确保输出目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # 执行爬取
    articles = crawl_news()
    
    # 生成文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"centralbank_bz_news_{timestamp}.json"
    output_path = os.path.join(OUTPUT_DIR, filename)
    
    # 保存结果
    if articles:
        save_results(articles, output_path)
    else:
        print("[Warn] 未抓取到任何数据")

if __name__ == "__main__":
    main()
