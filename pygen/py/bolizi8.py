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
MAX_ITEMS = 5  # 限制爬取数量（用户要求前5条）
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
BASE_URL = "https://www.centralbank.org.bz"
START_URL = "https://www.centralbank.org.bz/publications-search"

# ==========================================
# 辅助函数
# ==========================================

def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)

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
        for script in soup(["script", "style", "iframe"]):
            script.decompose()

        return str(soup)
    except Exception as e:
        print(f"[Warn] 内容清洗出错: {e}")
        return html_content

def parse_date(date_str):
    """
    解析日期字符串，格式示例: "26 February 2026"
    """
    if not date_str:
        return ""
    try:
        # 清理多余空格
        date_str = date_str.strip()
        # 尝试解析 "26 February 2026"
        dt = datetime.strptime(date_str, "%d %B %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        try:
            # 备用尝试其他格式
            dt = datetime.strptime(date_str, "%B %d, %Y")
            return dt.strftime("%Y-%m-%d")
        except:
            return ""

def is_file_url(url):
    """判断是否为文件链接"""
    url_lower = url.lower()
    extensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip', '.rar']
    return any(url_lower.endswith(ext) for ext in extensions)

# ==========================================
# 爬虫主逻辑
# ==========================================

def crawl():
    ensure_dir(OUTPUT_DIR)
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
        
        # 屏蔽不必要的资源加载
        # context.route("**/*.{png,jpg,jpeg,gif,svg}", lambda route: route.abort())
        
        page = context.new_page()
        
        try:
            print(f"正在访问列表页: {START_URL}")
            page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
            
            # 等待列表加载
            try:
                page.wait_for_selector("ul.item-list", timeout=15000)
            except:
                print("未找到新闻列表容器 ul.item-list")
                return []

            # 翻页循环
            page_num = 1
            while len(articles) < MAX_ITEMS:
                print(f"正在处理第 {page_num} 页...")
                
                # 获取当前页的所有列表项
                items = page.locator("ul.item-list > li.item-list__item").all()
                print(f"当前页发现 {len(items)} 条数据")
                
                if not items:
                    print("当前页无数据，停止翻页")
                    break
                
                for item in items:
                    if len(articles) >= MAX_ITEMS:
                        break
                        
                    try:
                        # 提取基础信息
                        title_el = item.locator("a.item-list__title")
                        if not title_el.count():
                            continue
                            
                        title = title_el.inner_text().strip()
                        link = title_el.get_attribute("href")
                        if link:
                            link = urljoin(BASE_URL, link)
                        
                        date_el = item.locator(".item-list__date")
                        date_str = date_el.inner_text().strip() if date_el.count() else ""
                        date = parse_date(date_str)
                        
                        summary_el = item.locator(".item-list__description")
                        summary = summary_el.inner_text().strip() if summary_el.count() else ""
                        
                        print(f"正在抓取: {title} ({date})")
                        
                        article_data = {
                            "title": title,
                            "date": date,
                            "source": "Central Bank of Belize",
                            "author": "",
                            "sourceUrl": link,
                            "summary": summary,
                            "content": ""
                        }
                        
                        # 详情页处理
                        if link:
                            # 1. 检查是否为文件后缀
                            if is_file_url(link):
                                print(f"  -> 检测到文件链接，跳过详情页抓取: {link}")
                                article_data["content"] = f'<a href="{link}" target="_blank">{link}</a>'
                            else:
                                # 2. 访问详情页提取正文
                                detail_page = context.new_page()
                                try:
                                    # 监听下载事件，如果触发下载则说明是文件
                                    is_download = False
                                    def handle_download(download):
                                        nonlocal is_download
                                        is_download = True
                                        print(f"  -> 触发下载，视为文件: {download.suggested_filename}")
                                        download.cancel() # 取消下载
                                    
                                    detail_page.on("download", handle_download)
                                    
                                    # 访问页面
                                    detail_page.goto(link, wait_until="domcontentloaded", timeout=30000)
                                    
                                    if is_download:
                                        article_data["content"] = f'<a href="{link}" target="_blank">{link}</a>'
                                    else:
                                        # 尝试提取正文
                                        # 优先尝试 div.group.margin-large，其次 div.group
                                        content_html = ""
                                        if detail_page.locator("div.group.margin-large").count() > 0:
                                            content_html = detail_page.locator("div.group.margin-large").first.inner_html()
                                        elif detail_page.locator("div.group").count() > 0:
                                            content_html = detail_page.locator("div.group").first.inner_html()
                                        
                                        # 检查内容有效性
                                        if not content_html or len(content_html.strip()) < 50:
                                            print("  -> 正文为空或过短，回退为链接")
                                            article_data["content"] = f'<a href="{link}" target="_blank">{link}</a>'
                                        else:
                                            article_data["content"] = clean_html_content(content_html, link)
                                            print("  -> 正文提取成功")
                                            
                                except Exception as e:
                                    print(f"  -> 详情页访问失败: {e}")
                                    article_data["content"] = f'<a href="{link}" target="_blank">{link}</a>'
                                finally:
                                    detail_page.close()
                        
                        articles.append(article_data)
                        
                    except Exception as e:
                        print(f"处理单条数据出错: {e}")
                        continue
                
                # 检查是否达到数量限制
                if len(articles) >= MAX_ITEMS:
                    print(f"已达到目标数量 {MAX_ITEMS}，停止抓取")
                    break
                
                # 翻页逻辑
                # 查找下一页按钮：通常是 pagination 中的链接
                # 页面结构显示分页链接类似: ?startRow=20&rowsPerPage=20
                # 我们可以尝试点击包含 ">" 或 "»" 的链接，或者根据当前页码推断
                
                # 尝试找到 "Next" 按钮或下一页页码
                # 这里的 HTML 显示下一页可能是 "»"
                next_btn = page.locator("ul.pagination li a:has-text('»')")
                if next_btn.count() > 0 and next_btn.is_visible():
                    print("正在翻页...")
                    next_btn.click()
                    page.wait_for_timeout(2000) # 等待页面刷新
                    page_num += 1
                else:
                    print("未找到下一页按钮，停止翻页")
                    break
                    
        except Exception as e:
            print(f"爬虫运行出错: {e}")
        finally:
            browser.close()
            
    return articles

def save_results(articles, output_path):
    """保存结果为JSON"""
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"结果已保存至: {output_path}")

def main():
    print("开始爬取伯利兹中央银行新闻...")
    articles = crawl()
    
    # 生成文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"centralbank_belize_news_{timestamp}.json"
    output_path = os.path.join(OUTPUT_DIR, filename)
    
    save_results(articles, output_path)
    print("任务完成")

if __name__ == "__main__":
    main()
