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
# 目标URL
TARGET_URL = "https://www.centralbank.org.bz/publications-search"
# 限制抓取数量（用户要求前5条）
MAX_ITEMS = 5
# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# ================= 工具函数 =================

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：
    1. 移除脚本、样式等无用标签
    2. 将相对路径转换为绝对路径
    """
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除无用标签
        for tag in soup(["script", "style", "iframe", "noscript", "header", "footer"]):
            tag.decompose()

        # 修复图片链接
        for img in soup.find_all('img'):
            if img.get('src'):
                img['src'] = urljoin(base_url, img['src'])
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
                a['target'] = '_blank'
                
        return str(soup)
    except Exception as e:
        print(f"内容清洗出错: {e}")
        return html_content

def parse_date(date_str):
    """
    解析日期字符串，支持多种格式
    示例: "26 February 2026" -> "2026-02-26"
    """
    if not date_str:
        return ""
    date_str = date_str.strip()
    formats = [
        "%d %B %Y",      # 26 February 2026
        "%B %d, %Y",     # February 26, 2026
        "%Y-%m-%d",      # 2026-02-26
        "%d/%m/%Y"       # 26/02/2026
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str

def is_file_url(url):
    """判断URL是否指向文件（PDF/DOC等）"""
    path = urlparse(url).path.lower()
    extensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip', '.rar']
    return any(path.endswith(ext) for ext in extensions)

def save_results(articles, output_dir):
    """保存结果为JSON"""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"centralbank_belize_news_{timestamp}.json"
    output_path = os.path.join(output_dir, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已保存 {len(articles)} 条新闻到 {output_path}")

# ================= 爬虫主逻辑 =================

def crawl_news():
    articles = []
    
    with sync_playwright() as p:
        # 启动浏览器（配置反爬参数）
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ignore_https_errors=True
        )
        
        page = context.new_page()
        
        try:
            print(f"正在访问列表页: {TARGET_URL}")
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
            
            # 等待列表元素加载
            # 注意：页面有多个 ul.item-list，主内容区域的在 .interior-layout-main 下
            list_selector = ".interior-layout-main ul.item-list > li.item-list__item"
            try:
                page.wait_for_selector(list_selector, timeout=15000)
            except:
                print("警告: 未检测到列表元素，页面可能加载缓慢或无数据")

            # 获取所有列表项
            items = page.locator(list_selector).all()
            print(f"当前页找到 {len(items)} 条数据")
            
            for i, item in enumerate(items):
                if len(articles) >= MAX_ITEMS:
                    print(f"已达到目标数量 {MAX_ITEMS}，停止抓取")
                    break
                
                print(f"--- 正在处理第 {len(articles) + 1} 条 ---")
                
                try:
                    # 1. 提取基础信息
                    title_el = item.locator("a.item-list__title")
                    if not title_el.count():
                        continue
                        
                    title = title_el.inner_text().strip()
                    link = title_el.get_attribute("href")
                    if link:
                        link = urljoin(TARGET_URL, link)
                    
                    # 提取日期
                    date_el = item.locator(".item-list__date")
                    date_text = date_el.inner_text().strip() if date_el.count() else ""
                    date = parse_date(date_text)
                    
                    # 提取摘要
                    summary_el = item.locator(".item-list__description")
                    summary = summary_el.inner_text().strip() if summary_el.count() else ""
                    
                    article_data = {
                        "id": str(len(articles) + 1),
                        "title": title,
                        "date": date,
                        "source": "Central Bank of Belize",
                        "author": "",
                        "sourceUrl": link,
                        "summary": summary,
                        "content": ""
                    }
                    
                    # 2. 处理详情页内容
                    if not link:
                        article_data["content"] = summary
                    elif is_file_url(link):
                        print(f"检测到文件链接: {link}")
                        article_data["content"] = f'<a href="{link}" target="_blank">{link}</a>'
                    else:
                        # 打开新页面抓取详情
                        detail_page = context.new_page()
                        try:
                            # 监听响应以检测是否为文件下载（Content-Type）
                            is_download = False
                            def handle_response(response):
                                nonlocal is_download
                                if response.url == link and response.status == 200:
                                    ct = response.headers.get("content-type", "").lower()
                                    if "application/pdf" in ct or "application/octet-stream" in ct:
                                        is_download = True

                            detail_page.on("response", handle_response)
                            
                            print(f"访问详情页: {link}")
                            # 使用 try-except 包裹 goto，防止下载触发的导航错误
                            try:
                                detail_page.goto(link, wait_until="domcontentloaded", timeout=30000)
                            except Exception as nav_err:
                                if "net::ERR_ABORTED" in str(nav_err) or "Download is starting" in str(nav_err):
                                    is_download = True
                                else:
                                    raise nav_err

                            if is_download:
                                print("页面响应为文件下载")
                                article_data["content"] = f'<a href="{link}" target="_blank">{link}</a>'
                            else:
                                # 提取正文
                                # 优先尝试 Agent Strategy 建议的 div.group.margin-large
                                content_html = ""
                                candidates = [
                                    "div.group.margin-large",
                                    "div.interior-layout__main",
                                    "#top"
                                ]
                                
                                for selector in candidates:
                                    if detail_page.locator(selector).count() > 0:
                                        content_html = detail_page.locator(selector).first.inner_html()
                                        if len(content_html.strip()) > 50:
                                            break
                                
                                if not content_html or len(content_html.strip()) < 50:
                                    print("正文为空或过短，回退为链接")
                                    article_data["content"] = f'<a href="{link}" target="_blank">{link}</a>'
                                else:
                                    article_data["content"] = clean_html_content(content_html, link)
                                    
                                    # 如果列表页没找到日期，尝试在详情页找
                                    if not date:
                                        detail_date = detail_page.locator(".item-list__date.detail-page p").first
                                        if detail_date.count():
                                            article_data["date"] = parse_date(detail_date.inner_text())

                        except Exception as e:
                            print(f"详情页抓取失败: {e}")
                            article_data["content"] = f'<a href="{link}" target="_blank">{link}</a>'
                        finally:
                            detail_page.close()
                    
                    articles.append(article_data)
                    print(f"成功抓取: {title}")
                    
                except Exception as e:
                    print(f"处理单条数据出错: {e}")
                    continue
                
                # 礼貌性间隔
                time.sleep(1)
                
        except Exception as e:
            print(f"爬虫运行发生错误: {e}")
        finally:
            browser.close()
            
    return articles

def main():
    print("开始执行爬虫任务...")
    start_time = time.time()
    
    articles = crawl_news()
    
    save_results(articles, OUTPUT_DIR)
    
    end_time = time.time()
    print(f"任务完成，耗时: {end_time - start_time:.2f} 秒")

if __name__ == "__main__":
    main()
