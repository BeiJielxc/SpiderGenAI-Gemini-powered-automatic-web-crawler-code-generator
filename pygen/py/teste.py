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
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime

# 配置常量
BASE_URL = "https://www.cityam.com"
LIST_URL = "https://www.cityam.com/news/"
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
MAX_ITEMS = 5  # 限制爬取前5条

# 请求头配置
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def get_page_content(url):
    """获取页面内容，包含重试机制"""
    retries = 3
    for i in range(retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            print(f"请求失败 ({i+1}/{retries}): {url} - {e}")
            time.sleep(2)
    return None

def clean_html_content(html_content, base_url):
    """清洗HTML内容：修复图片链接，移除无用标签"""
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除不需要的标签
        for tag in soup.select('header, footer, aside, .notice-header, .newsletter-auto-inject, script, style'):
            tag.decompose()

        # 修复图片链接为绝对路径
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

def parse_detail_page(url):
    """解析详情页获取日期和正文"""
    print(f"正在抓取详情页: {url}")
    html = get_page_content(url)
    if not html:
        return None
    
    soup = BeautifulSoup(html, 'html.parser')
    
    # 提取日期
    date_str = ""
    # 策略: time.date-time__time (优先 datetime 属性)
    time_tag = soup.select_one('time.date-time__time')
    if time_tag:
        date_str = time_tag.get('datetime') or time_tag.get_text(strip=True)
        # 尝试格式化日期，如果格式是 ISO 格式
        if date_str:
            try:
                # 尝试解析 ISO 格式 (e.g., 2026-02-19T12:00:00)
                dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                date_str = dt.strftime("%Y-%m-%d")
            except ValueError:
                pass # 保持原样或尝试其他格式
    
    # 如果没找到，尝试从 meta 标签找
    if not date_str:
        meta_date = soup.find('meta', property='article:published_time')
        if meta_date:
            date_str = meta_date.get('content', '')[:10]

    # 提取正文
    # 策略: article.content-container.content-container__single
    content_selector = "article.content-container.content-container__single"
    article_body = soup.select_one(content_selector)
    
    content_html = ""
    summary = ""
    
    if article_body:
        # 提取纯文本用于摘要
        paragraphs = article_body.find_all('p')
        text_content = " ".join([p.get_text(strip=True) for p in paragraphs])
        summary = text_content[:200] + "..." if len(text_content) > 200 else text_content
        
        # 获取清洗后的HTML
        # 注意：这里传入的是 article_body 的 inner HTML
        content_html = clean_html_content(str(article_body), url)
    else:
        print(f"警告: 未找到正文内容 (选择器: {content_selector})")

    return {
        "date": date_str,
        "content": content_html,
        "summary": summary
    }

def crawl_news():
    """主爬取逻辑"""
    print(f"开始爬取列表页: {LIST_URL}")
    list_html = get_page_content(LIST_URL)
    if not list_html:
        print("无法获取列表页，程序结束")
        return []

    soup = BeautifulSoup(list_html, 'html.parser')
    
    # 列表项选择器: .content-listing__content-item
    items = soup.select('.content-listing__content-item')
    print(f"找到 {len(items)} 个列表项，将处理前 {MAX_ITEMS} 个")
    
    articles = []
    count = 0
    
    for item in items:
        if count >= MAX_ITEMS:
            break
            
        # 提取标题和链接
        # Title selector: .card__title a
        title_tag = item.select_one('.card__title a')
        if not title_tag:
            continue
            
        title = title_tag.get_text(strip=True)
        link = title_tag.get('href')
        
        if not link:
            continue
            
        full_link = urljoin(BASE_URL, link)
        
        # 访问详情页
        detail_data = parse_detail_page(full_link)
        
        if detail_data:
            article = {
                "id": str(count + 1),
                "title": title,
                "date": detail_data['date'],
                "author": "", # 页面分析未指定作者选择器，留空
                "source": "City AM",
                "sourceUrl": full_link,
                "summary": detail_data['summary'],
                "content": detail_data['content']
            }
            articles.append(article)
            count += 1
            # 礼貌性延迟
            time.sleep(1)
            
    return articles

def save_results(articles, output_dir):
    """保存结果为JSON"""
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"cityam_news_{timestamp}.json"
    output_path = os.path.join(output_dir, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        
    print(f"爬取完成，结果已保存至: {output_path}")

def main():
    print("启动 City AM 新闻爬虫...")
    articles = crawl_news()
    
    if articles:
        save_results(articles, OUTPUT_DIR)
    else:
        print("未爬取到任何新闻。")

if __name__ == "__main__":
    main()
