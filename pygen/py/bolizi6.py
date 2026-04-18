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

# ==========================================
# 配置区域
# ==========================================
MAX_ITEMS = 5  # 任务目标：前5条
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
BASE_URL = "https://www.centralbank.org.bz"
START_URL = "https://www.centralbank.org.bz/publications-search"

# ==========================================
# 工具函数
# ==========================================

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：
    1. 将相对路径转换为绝对路径 (img src, a href)
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

def save_results(articles, output_dir):
    """保存结果为JSON"""
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"centralbank_belize_news_{timestamp}.json"
    filepath = os.path.join(output_dir, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"\n[Success] 已保存 {len(articles)} 条数据到: {filepath}")

def is_file_link(url):
    """检查链接是否指向文件"""
    if not url:
        return False
    path = urlparse(url).path.lower()
    extensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.zip', '.rar']
    return any(path.endswith(ext) for ext in extensions)

# ==========================================
# 爬虫主逻辑
# ==========================================

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
            locale="en-US"
        )
        
        page = context.new_page()
        
        print(f"[Info] 开始访问: {START_URL}")
        try:
            page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(".interior-layout-main", timeout=15000)
        except Exception as e:
            print(f"[Error] 页面加载失败: {e}")
            return []

        while len(articles) < MAX_ITEMS:
            # 定位列表项
            # 页面结构分析：主内容区域在 .interior-layout-main 下的 ul.item-list
            # 侧边栏也有 ul.item-list，需要区分
            list_items = page.locator(".interior-layout-main > ul.item-list > li.item-list__item").all()
            
            print(f"[Info] 当前页发现 {len(list_items)} 条数据")
            
            if not list_items:
                print("[Warn] 未找到列表项，停止抓取")
                break

            # 遍历当前页列表
            for item in list_items:
                if len(articles) >= MAX_ITEMS:
                    break
                
                try:
                    # 1. 提取列表页基础信息
                    title_el = item.locator("a.item-list__title")
                    date_el = item.locator("p.item-list__date")
                    summary_el = item.locator(".item-list__description")
                    
                    title = title_el.inner_text().strip() if title_el.count() > 0 else "无标题"
                    url_suffix = title_el.get_attribute("href") if title_el.count() > 0 else ""
                    full_url = urljoin(BASE_URL, url_suffix) if url_suffix else ""
                    
                    date_str = date_el.inner_text().strip() if date_el.count() > 0 else ""
                    # 尝试格式化日期
                    try:
                        # 示例格式: 26 February 2026
                        dt = datetime.strptime(date_str, "%d %B %Y")
                        date_std = dt.strftime("%Y-%m-%d")
                    except:
                        date_std = date_str

                    summary = summary_el.inner_text().strip() if summary_el.count() > 0 else ""
                    
                    print(f"[-] 处理: {title[:30]}... | {date_std}")

                    article_data = {
                        "title": title,
                        "date": date_std,
                        "source": "Central Bank of Belize",
                        "author": "",
                        "sourceUrl": full_url,
                        "summary": summary,
                        "content": ""
                    }

                    # 2. 获取详情页内容
                    if not full_url:
                        print("    [Skip] 无链接")
                        continue

                    # 检查是否为文件链接
                    if is_file_link(full_url):
                        print("    [File] 检测到文件链接，跳过详情页抓取")
                        article_data["content"] = f'<p>文件下载: <a href="{full_url}" target="_blank">{title}</a></p><p>摘要: {summary}</p>'
                    else:
                        # 打开新页面抓取详情，避免破坏列表页状态
                        detail_page = context.new_page()
                        try:
                            detail_page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
                            
                            # 尝试提取正文
                            # 策略：优先 div.group.margin-large (Agent建议)，其次 .interior-layout-main (通用容器)，最后 #top
                            content_html = ""
                            
                            # 尝试选择器列表
                            selectors = [
                                "div.group.margin-large", 
                                ".interior-layout-main", 
                                "#top"
                            ]
                            
                            for sel in selectors:
                                if detail_page.locator(sel).count() > 0:
                                    # 排除面包屑、标题等非正文元素（如果包含在容器内）
                                    # 这里简单提取整个容器
                                    content_html = detail_page.locator(sel).first.inner_html()
                                    if len(content_html) > 200: # 简单的有效性检查
                                        break
                            
                            if not content_html:
                                content_html = f"<p>{summary}</p><p><a href='{full_url}'>查看原文</a></p>"
                            
                            # 清洗内容
                            article_data["content"] = clean_html_content(content_html, full_url)
                            
                            # 尝试在详情页提取更准确的日期（如果列表页没有）
                            if not date_std:
                                detail_date = detail_page.locator(".item-list__date.detail-page p").first
                                if detail_date.count() > 0:
                                    d_text = detail_date.inner_text().strip()
                                    try:
                                        article_data["date"] = datetime.strptime(d_text, "%d %B %Y").strftime("%Y-%m-%d")
                                    except:
                                        article_data["date"] = d_text

                        except Exception as e:
                            print(f"    [Error] 详情页抓取失败: {e}")
                            # 失败回退
                            article_data["content"] = f"<p>抓取失败，请访问原文: <a href='{full_url}'>{full_url}</a></p>"
                        finally:
                            detail_page.close()

                    articles.append(article_data)

                except Exception as e:
                    print(f"    [Error] 处理单条数据出错: {e}")
                    continue

            # 翻页逻辑
            if len(articles) < MAX_ITEMS:
                # 查找下一页按钮 "»"
                # 根据HTML: <li><a class="" href="...?startRow=20...">»</a></li>
                next_btn = page.locator("ul.pagination li a:has-text('»')")
                
                if next_btn.count() > 0 and next_btn.is_visible():
                    print("[Info] 正在翻页...")
                    try:
                        # 记录当前第一条标题，用于判断翻页是否成功
                        first_title_before = list_items[0].locator("a.item-list__title").inner_text()
                        
                        next_btn.click()
                        page.wait_for_timeout(2000) # 等待点击反应
                        page.wait_for_load_state("networkidle")
                        
                        # 简单的翻页成功检查
                        new_items = page.locator(".interior-layout-main > ul.item-list > li.item-list__item").all()
                        if not new_items:
                            print("[Warn] 翻页后未找到数据，停止")
                            break
                        
                        first_title_after = new_items[0].locator("a.item-list__title").inner_text()
                        if first_title_before == first_title_after:
                            print("[Warn] 翻页后内容未变，可能已达末尾")
                            break
                            
                    except Exception as e:
                        print(f"[Error] 翻页失败: {e}")
                        break
                else:
                    print("[Info] 没有下一页了")
                    break
            else:
                print(f"[Info] 已达到目标数量 {MAX_ITEMS}，停止抓取")
                break

        browser.close()
    
    return articles

def main():
    print("=== 伯利兹中央银行新闻爬虫启动 ===")
    start_time = time.time()
    
    try:
        articles = crawl_news()
        if articles:
            save_results(articles, OUTPUT_DIR)
        else:
            print("[Warn] 未抓取到任何数据")
    except Exception as e:
        print(f"[Fatal] 程序运行异常: {e}")
    
    print(f"=== 任务结束，耗时: {time.time() - start_time:.2f}秒 ===")

if __name__ == "__main__":
    main()
