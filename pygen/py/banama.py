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
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup

# ==========================================
# 配置区域
# ==========================================
TARGET_URL = "https://supervalores.gob.pa/comunicados-avisos-y-actualidad/"
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
MAX_ITEMS = 5  # 目标爬取数量

# 西班牙语月份映射
MONTHS_ES = {
    "enero": "01", "febrero": "02", "marzo": "03", "abril": "04", "mayo": "05", "junio": "06",
    "julio": "07", "agosto": "08", "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12"
}

def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)

def parse_es_date(date_str):
    """解析西班牙语日期格式: 26 de enero de 2026"""
    if not date_str:
        return ""
    try:
        # 移除多余空格和非日期字符
        clean_str = date_str.lower().strip()
        # 匹配模式: DD de Month de YYYY
        match = re.search(r'(\d{1,2})\s+de\s+([a-z]+)\s+de\s+(\d{4})', clean_str)
        if match:
            day, month_name, year = match.groups()
            month = MONTHS_ES.get(month_name, "01")
            return f"{year}-{month}-{int(day):02d}"
        
        # 尝试其他格式 DD/MM/YYYY
        match_simple = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', clean_str)
        if match_simple:
            day, month, year = match_simple.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
            
        return date_str
    except Exception as e:
        print(f"日期解析错误: {e} -> {date_str}")
        return date_str

def clean_html_content(html_content, base_url):
    """将 HTML 中的相对路径转换为绝对路径"""
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除无用元素
        for tag in soup.select('script, style, .share-buttons, .post-navigation'):
            tag.decompose()

        # 修复图片链接
        for img in soup.find_all('img'):
            if img.get('src'):
                img['src'] = urljoin(base_url, img['src'])
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
                
        return str(soup)
    except Exception as e:
        print(f"内容清洗出错: {e}")
        return html_content

def save_results(articles: list, output_dir: str):
    """保存结果为 JSON"""
    ensure_dir(output_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"supervalores_news_{timestamp}.json"
    output_path = os.path.join(output_dir, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已保存 {len(articles)} 条新闻到 {output_path}")

def extract_detail_page(page, url):
    """提取详情页内容"""
    print(f"正在抓取详情页: {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("body", timeout=10000)
        
        # 提取标题
        title = ""
        title_selectors = ["h1.entry-title", "h1", ".elementor-heading-title"]
        for sel in title_selectors:
            if page.locator(sel).first.is_visible():
                title = page.locator(sel).first.inner_text().strip()
                break
        
        # 提取日期
        date_str = ""
        date_selectors = ["time.entry-date", ".elementor-widget-meta-data", "span.posted-on"]
        for sel in date_selectors:
            if page.locator(sel).first.is_visible():
                date_text = page.locator(sel).first.inner_text().strip()
                date_str = parse_es_date(date_text)
                if date_str: break
        
        # 提取正文
        content = ""
        content_selectors = [
            "div.entry-content", 
            "div.elementor-widget-theme-post-content", 
            "article .elementor-section-wrap",
            "#content"
        ]
        for sel in content_selectors:
            if page.locator(sel).first.is_visible():
                content_html = page.locator(sel).first.inner_html()
                content = clean_html_content(content_html, url)
                break
        
        # 提取附件/下载链接
        download_url = ""
        # 优先查找 PDF 链接
        try:
            pdf_link = page.locator("a[href$='.pdf']").first
            if pdf_link.is_visible():
                download_url = urljoin(url, pdf_link.get_attribute("href"))
        except:
            pass
            
        # 提取摘要
        summary = ""
        if content:
            soup = BeautifulSoup(content, 'html.parser')
            summary = soup.get_text()[:200].strip() + "..."

        return {
            "title": title,
            "date": date_str,
            "source": "Superintendencia del Mercado de Valores",
            "sourceUrl": url,
            "summary": summary,
            "content": content,
            "downloadUrl": download_url
        }

    except Exception as e:
        print(f"详情页抓取失败 {url}: {e}")
        return None

def crawl_news():
    articles = []
    
    with sync_playwright() as p:
        # 启动浏览器
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        print(f"正在访问列表页: {TARGET_URL}")
        try:
            page.goto(TARGET_URL, wait_until="networkidle", timeout=45000)
        except PlaywrightTimeoutError:
            print("页面加载超时，尝试继续解析...")
        
        # 尝试定位列表项
        # 策略1: WP Show Posts 结构 (Agent Strategy 建议)
        # 策略2: 标准 WordPress Article 结构
        # 策略3: Elementor Grid 结构
        
        item_selectors = [
            "div.wp-show-posts-inner", 
            "article.post",
            "div.elementor-post"
        ]
        
        found_selector = None
        for sel in item_selectors:
            if page.locator(sel).count() > 0:
                found_selector = sel
                print(f"找到列表项选择器: {sel}, 数量: {page.locator(sel).count()}")
                break
        
        if not found_selector:
            print("未找到已知的新闻列表结构，尝试通用链接提取...")
            # 兜底策略：提取主要区域的所有链接
            links = page.locator("main a, #content a").all()
        else:
            items = page.locator(found_selector).all()
            print(f"发现 {len(items)} 条潜在新闻")
            
            processed_urls = set()
            
            for i, item in enumerate(items):
                if len(articles) >= MAX_ITEMS:
                    break
                
                try:
                    # 提取链接
                    link_el = item.locator("a").first
                    if not link_el.is_visible():
                        # 尝试找标题内的链接
                        link_el = item.locator("h2 a, h3 a, .elementor-post__title a").first
                    
                    if not link_el.is_visible():
                        continue
                        
                    url = link_el.get_attribute("href")
                    if not url or url in processed_urls:
                        continue
                        
                    url = urljoin(TARGET_URL, url)
                    processed_urls.add(url)
                    
                    # 尝试在列表页提取日期（作为备选）
                    list_date = ""
                    date_el = item.locator("time, .wp-show-posts-entry-date, .elementor-post-date").first
                    if date_el.is_visible():
                        list_date = parse_es_date(date_el.inner_text())

                    # 进入详情页
                    detail_data = extract_detail_page(context.new_page(), url)
                    
                    if detail_data:
                        # 如果详情页没找到日期，使用列表页日期
                        if not detail_data['date'] and list_date:
                            detail_data['date'] = list_date
                        
                        # 只有标题有效才添加
                        if detail_data['title']:
                            articles.append(detail_data)
                            print(f"成功抓取 [{len(articles)}/{MAX_ITEMS}]: {detail_data['title']}")
                    
                    # 简单的防封禁延时
                    time.sleep(1)
                    
                except Exception as e:
                    print(f"处理列表项 {i} 时出错: {e}")
                    continue

        browser.close()
        
    return articles

def main():
    print("开始爬取任务...")
    start_time = time.time()
    
    try:
        articles = crawl_news()
        
        if articles:
            save_results(articles, OUTPUT_DIR)
        else:
            print("未爬取到任何新闻数据。")
            
    except Exception as e:
        print(f"爬虫运行出错: {e}")
    
    end_time = time.time()
    print(f"任务结束，耗时: {end_time - start_time:.2f} 秒")

if __name__ == "__main__":
    main()
