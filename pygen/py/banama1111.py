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
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup

# ==========================================
# 配置区域
# ==========================================
TARGET_URL = "https://supervalores.gob.pa/comunicados-avisos-y-actualidad/"
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
MAX_ITEMS = 5  # 目标抓取数量

# 西班牙语月份映射
MONTHS_ES = {
    'enero': '01', 'febrero': '02', 'marzo': '03', 'abril': '04',
    'mayo': '05', 'junio': '06', 'julio': '07', 'agosto': '08',
    'septiembre': '09', 'octubre': '10', 'noviembre': '11', 'diciembre': '12',
    'january': '01', 'february': '02', 'march': '03', 'april': '04',
    'may': '05', 'june': '06', 'july': '07', 'august': '08',
    'september': '09', 'october': '10', 'november': '11', 'december': '12'
}

def parse_es_date(date_str):
    """解析西班牙语日期格式，如 '26 de enero de 2026'"""
    if not date_str:
        return ""
    try:
        # 清理多余空格和字符
        date_str = date_str.lower().strip()
        # 移除 'de' 或 ','
        date_str = date_str.replace(' de ', ' ').replace(',', '')
        
        parts = date_str.split()
        day = parts[0].zfill(2)
        year = parts[-1]
        month = '01'
        
        # 查找月份
        for part in parts:
            if part in MONTHS_ES:
                month = MONTHS_ES[part]
                break
        
        # 简单的数字检查
        if not day.isdigit() or not year.isdigit():
            return ""
            
        return f"{year}-{month}-{day}"
    except Exception as e:
        print(f"日期解析错误: {date_str}, {e}")
        return ""

def clean_html_content(html_content, base_url):
    """将 HTML 中的相对路径转换为绝对路径"""
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
                
        return str(soup)
    except Exception as e:
        print(f"内容清洗出错: {e}")
        return html_content

def save_results(articles, output_dir):
    """保存结果为 JSON"""
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
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

def run_crawler():
    print(f"启动爬虫，目标: {TARGET_URL}")
    
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
            locale="es-ES"
        )
        page = context.new_page()
        
        try:
            # 1. 访问列表页
            print(f"正在访问列表页: {TARGET_URL}")
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3) # 等待动态内容加载
            
            # 2. 提取列表项链接
            # 优先尝试用户提示的选择器 .wp-show-posts-single
            # 如果没有，尝试标准的 article 选择器
            news_links = []
            
            # 尝试策略 1: .wp-show-posts-single (WP Show Posts 插件)
            items = page.query_selector_all('.wp-show-posts-single')
            if not items:
                print("未找到 .wp-show-posts-single，尝试通用 article 选择器...")
                items = page.query_selector_all('article')
            
            print(f"找到 {len(items)} 个列表项")
            
            for item in items:
                if len(news_links) >= MAX_ITEMS:
                    break
                    
                # 提取链接
                link_el = item.query_selector('h2.wp-show-posts-entry-title a') or \
                          item.query_selector('h2.entry-title a') or \
                          item.query_selector('a.entry-title-link') or \
                          item.query_selector('a') # 最后的兜底
                
                if link_el:
                    url = link_el.get_attribute('href')
                    title = link_el.inner_text().strip()
                    if url and url not in [x['url'] for x in news_links]:
                        news_links.append({'url': url, 'title': title})
            
            # 如果第一页不够，尝试翻页 (简单实现，针对 page/2/ 模式)
            current_page = 1
            while len(news_links) < MAX_ITEMS:
                current_page += 1
                next_page_url = f"{TARGET_URL}page/{current_page}/"
                print(f"尝试加载下一页: {next_page_url}")
                
                try:
                    response = page.goto(next_page_url, wait_until="domcontentloaded", timeout=30000)
                    if response.status == 404:
                        print("分页结束 (404)")
                        break
                    
                    time.sleep(2)
                    items = page.query_selector_all('.wp-show-posts-single') or page.query_selector_all('article')
                    
                    if not items:
                        print("该页无内容")
                        break
                        
                    for item in items:
                        if len(news_links) >= MAX_ITEMS:
                            break
                        link_el = item.query_selector('h2.wp-show-posts-entry-title a') or item.query_selector('h2.entry-title a')
                        if link_el:
                            url = link_el.get_attribute('href')
                            title = link_el.inner_text().strip()
                            if url and url not in [x['url'] for x in news_links]:
                                news_links.append({'url': url, 'title': title})
                except Exception as e:
                    print(f"翻页失败: {e}")
                    break

            print(f"共收集到 {len(news_links)} 个目标链接，准备抓取详情...")
            
            # 3. 遍历详情页
            for i, link_info in enumerate(news_links):
                url = link_info['url']
                list_title = link_info['title']
                print(f"[{i+1}/{len(news_links)}] 处理: {url}")
                
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    time.sleep(1)
                    
                    # 提取详情
                    # 标题
                    title_el = page.query_selector('h1.entry-title')
                    title = title_el.inner_text().strip() if title_el else list_title
                    
                    # 日期
                    date_str = ""
                    date_el = page.query_selector('time.entry-date.published') or \
                              page.query_selector('.posted-on time') or \
                              page.query_selector('span.date')
                    
                    if date_el:
                        raw_date = date_el.inner_text().strip()
                        # 尝试从属性获取标准时间
                        datetime_attr = date_el.get_attribute('datetime')
                        if datetime_attr:
                            date_str = datetime_attr.split('T')[0]
                        else:
                            date_str = parse_es_date(raw_date)
                    
                    # 正文
                    content_html = ""
                    content_el = page.query_selector('.entry-content') or \
                                 page.query_selector('div.elementor-widget-theme-post-content')
                    
                    download_url = ""
                    
                    if content_el:
                        # 提取 PDF 链接
                        pdf_link = content_el.query_selector('a[href$=".pdf"]')
                        if pdf_link:
                            download_url = pdf_link.get_attribute('href')
                            # 确保是绝对路径
                            download_url = urljoin(url, download_url)
                        
                        # 获取 HTML 并清洗
                        raw_html = content_el.inner_html()
                        content_html = clean_html_content(raw_html, url)
                    
                    # 构建数据对象
                    article = {
                        "id": str(i + 1),
                        "title": title,
                        "date": date_str,
                        "source": "Superintendencia del Mercado de Valores",
                        "author": "SMV",
                        "sourceUrl": url,
                        "summary": title, # 简单使用标题作为摘要
                        "content": content_html,
                        "downloadUrl": download_url
                    }
                    
                    articles.append(article)
                    
                except Exception as e:
                    print(f"抓取详情页失败 {url}: {e}")
                    # 即使失败也保留基本信息
                    articles.append({
                        "id": str(i + 1),
                        "title": list_title,
                        "date": "",
                        "sourceUrl": url,
                        "content": "",
                        "error": str(e)
                    })
                
                # 礼貌性延迟
                time.sleep(1)
                
        except Exception as e:
            print(f"爬虫运行出错: {e}")
        finally:
            browser.close()
            
    # 保存结果
    save_results(articles, OUTPUT_DIR)

if __name__ == "__main__":
    run_crawler()
