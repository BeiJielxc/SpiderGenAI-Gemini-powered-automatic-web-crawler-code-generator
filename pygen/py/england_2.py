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
import random
from datetime import datetime
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup

# ==========================================
# 配置区域
# ==========================================
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
TARGET_URL = "https://www.businesswire.com/newsroom"
MAX_ITEMS = 5  # 限制爬取数量
TIMEOUT = 60000  # 60秒超时

# ==========================================
# 工具函数
# ==========================================

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：
    1. 修复相对路径图片和链接
    2. 移除无用标签（可选）
    """
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 修复图片链接
        for img in soup.find_all('img'):
            if img.get('src'):
                # 处理 data-src 懒加载情况
                src = img.get('src')
                if src.startswith('data:') and img.get('data-src'):
                    src = img.get('data-src')
                img['src'] = urljoin(base_url, src)
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
                
        # 移除脚本和样式
        for script in soup(["script", "style", "iframe", "noscript"]):
            script.decompose()

        return str(soup)
    except Exception as e:
        print(f"[Warn] 内容清洗出错: {e}")
        return html_content

def parse_bw_date(date_str):
    """
    解析 Business Wire 的日期格式
    例如: "Feb 24, 2026 at 2:16 AM ET"
    """
    if not date_str:
        return ""
    
    try:
        # 移除时区标识，简化解析
        clean_str = re.sub(r'\s+[A-Z]{1,3}T?$', '', date_str.strip())
        # 尝试解析
        dt = datetime.strptime(clean_str, "%b %d, %Y at %I:%M %p")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        pass

    try:
        # 备用尝试：只提取年月日
        match = re.search(r'([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})', date_str)
        if match:
            month_str, day, year = match.groups()
            dt = datetime.strptime(f"{month_str} {day} {year}", "%b %d %Y")
            return dt.strftime("%Y-%m-%d")
    except Exception as e:
        print(f"[Warn] 日期解析失败 '{date_str}': {e}")
    
    # 如果都失败，返回当前日期或空
    return datetime.now().strftime("%Y-%m-%d")

def save_results(articles: list, output_path: str):
    """保存结果为JSON"""
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[Success] 已保存 {len(articles)} 条新闻到 {output_path}")
    except Exception as e:
        print(f"[Error] 保存文件失败: {e}")

# ==========================================
# 爬虫逻辑
# ==========================================

def crawl_detail_page(page, url):
    """爬取详情页内容"""
    print(f"-> 正在抓取详情页: {url}")
    try:
        page.goto(url, timeout=TIMEOUT, wait_until="domcontentloaded")
        # 随机等待，模拟人类行为
        time.sleep(random.uniform(1.5, 3.0))
        
        # 尝试定位正文区域
        # 策略：优先找 main，其次找特定 class，最后找包含大量文本的 div
        content_html = ""
        
        # 尝试选择器列表
        selectors = [
            ".ui-kit-press-release-content", # 常见 BW 结构
            "div[itemprop='articleBody']",
            ".bw-release-story",
            "main",
            "article"
        ]
        
        for selector in selectors:
            if page.locator(selector).count() > 0:
                # 排除一些非正文元素
                try:
                    # 获取该元素的 HTML
                    content_html = page.locator(selector).first.inner_html()
                    if len(content_html) > 200: # 确保内容足够长
                        break
                except:
                    continue
        
        # 如果还是没找到，尝试提取 body
        if not content_html:
            content_html = page.locator("body").inner_html()

        # 提取标题 (h1)
        title = ""
        if page.locator("h1").count() > 0:
            title = page.locator("h1").first.inner_text().strip()

        return title, content_html

    except Exception as e:
        print(f"[Error] 详情页抓取失败 {url}: {e}")
        return "", ""

def crawl_news():
    """主爬取函数"""
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
            print(f"正在访问列表页: {TARGET_URL}")
            page.goto(TARGET_URL, timeout=TIMEOUT, wait_until="networkidle")
            
            # 等待列表元素加载
            # 使用较为宽松的选择器以防页面微调
            # 目标是找到包含新闻链接的容器
            try:
                page.wait_for_selector("div.relative.py-6", timeout=15000)
            except:
                print("[Info] 等待特定选择器超时，尝试直接查找链接...")

            # 获取所有潜在的新闻项容器
            # 根据HTML结构：div class="relative py-6 lg:py-[34px] border-b-[1px] border-gray300"
            # 我们使用部分匹配 class
            items = page.locator("div.relative.py-6").all()
            
            print(f"找到 {len(items)} 个潜在新闻项")
            
            count = 0
            for item in items:
                if count >= MAX_ITEMS:
                    break
                
                try:
                    # 1. 提取链接和标题
                    # 查找内部的 a 标签，且 href 包含 /news/home/
                    link_el = item.locator("a[href*='/news/home/']").first
                    if not link_el.count():
                        continue
                        
                    relative_url = link_el.get_attribute("href")
                    full_url = urljoin(TARGET_URL, relative_url)
                    
                    # 列表页标题
                    list_title = link_el.inner_text().strip()
                    
                    # 2. 提取日期
                    # 日期通常在上面的一个 div > span 中，或者就在 item 文本中
                    # 格式: Feb 24, 2026 at 2:16 AM ET
                    date_text = ""
                    
                    # 尝试在 item 内部查找日期格式的文本
                    item_text = item.inner_text()
                    date_match = re.search(r'[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}.*?(?:AM|PM)?(?:\s+[A-Z]{1,3})?', item_text)
                    if date_match:
                        date_text = date_match.group(0)
                    
                    formatted_date = parse_bw_date(date_text)
                    
                    print(f"[{count+1}] 发现新闻: {list_title[:30]}... ({formatted_date})")
                    
                    # 暂存基本信息
                    article = {
                        "id": str(count + 1),
                        "title": list_title,
                        "date": formatted_date,
                        "source": "Business Wire",
                        "author": "Business Wire", # 默认
                        "sourceUrl": full_url,
                        "summary": "",
                        "content": ""
                    }
                    articles.append(article)
                    count += 1
                    
                except Exception as e:
                    print(f"[Error] 解析列表项失败: {e}")
                    continue

            # 3. 逐个进入详情页抓取内容
            for article in articles:
                try:
                    detail_title, raw_content = crawl_detail_page(page, article['sourceUrl'])
                    
                    # 如果详情页标题更完整，使用详情页标题
                    if detail_title and len(detail_title) > len(article['title']):
                        article['title'] = detail_title
                    
                    # 清洗内容
                    cleaned_content = clean_html_content(raw_content, article['sourceUrl'])
                    article['content'] = cleaned_content
                    
                    # 生成摘要 (去除HTML标签后截取)
                    text_content = BeautifulSoup(cleaned_content, 'html.parser').get_text(separator=' ', strip=True)
                    article['summary'] = text_content[:200] + "..." if len(text_content) > 200 else text_content
                    
                except Exception as e:
                    print(f"[Error] 处理详情页数据失败 {article['sourceUrl']}: {e}")

        except Exception as e:
            print(f"[Fatal] 爬虫运行出错: {e}")
        finally:
            browser.close()
            
    return articles

def main():
    # 确保输出目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    print("=== 开始爬取 Business Wire 新闻 ===")
    start_time = time.time()
    
    articles = crawl_news()
    
    # 生成文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"businesswire_news_{timestamp}.json"
    output_path = os.path.join(OUTPUT_DIR, filename)
    
    # 保存
    if articles:
        save_results(articles, output_path)
    else:
        print("[Warn] 未抓取到任何数据")
        
    print(f"=== 任务完成，耗时 {time.time() - start_time:.2f} 秒 ===")

if __name__ == "__main__":
    main()
