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
TARGET_URL = "https://www.centralbank.org.bz/publications-search"
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
MAX_ITEMS = 5  # 根据任务目标，限制为前5条，如果需要更多可修改此值
HEADLESS = True

# ==========================================
# 工具函数
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
                # 确保所有链接在新窗口打开
                a['target'] = '_blank'
                
        return str(soup)
    except Exception as e:
        print(f"[Warn] 内容清洗出错: {e}")
        return html_content

def parse_date(date_str):
    """
    解析日期字符串，格式如 "26 February 2026" -> "2026-02-26"
    """
    if not date_str:
        return ""
    try:
        # 移除多余空格
        date_str = date_str.strip()
        # 尝试解析 "26 February 2026" 格式
        dt = datetime.strptime(date_str, "%d %B %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        try:
            # 尝试其他常见格式
            dt = datetime.strptime(date_str, "%B %d, %Y")
            return dt.strftime("%Y-%m-%d")
        except:
            return date_str  # 解析失败返回原字符串

def is_file_url(url):
    """判断URL是否指向文件"""
    path = urlparse(url).path.lower()
    extensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.zip', '.rar']
    return any(path.endswith(ext) for ext in extensions)

# ==========================================
# 核心爬虫逻辑
# ==========================================

def crawl_news():
    articles = []
    
    with sync_playwright() as p:
        # 启动浏览器
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        print(f"[*] 开始访问: {TARGET_URL}")
        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
            # 等待列表加载
            page.wait_for_selector(".interior-layout-main ul.item-list", timeout=15000)
        except Exception as e:
            print(f"[Error] 页面加载失败: {e}")
            browser.close()
            return []

        page_num = 1
        
        while len(articles) < MAX_ITEMS:
            print(f"[*] 正在处理第 {page_num} 页...")
            
            # 定位列表项
            # 注意：页面可能有多个 ul.item-list，我们需要主内容区域的那个
            # 根据HTML结构，主内容在 .interior-layout-main 下
            list_items = page.locator(".interior-layout-main > ul.item-list > li.item-list__item").all()
            
            if not list_items:
                print("[Warn] 未找到列表项，停止翻页")
                break
                
            print(f"[*] 当前页发现 {len(list_items)} 条数据")
            
            for item in list_items:
                if len(articles) >= MAX_ITEMS:
                    break
                
                try:
                    # 提取基础信息
                    title_el = item.locator("a.item-list__title")
                    if not title_el.count():
                        continue
                        
                    title = title_el.inner_text().strip()
                    url = urljoin(TARGET_URL, title_el.get_attribute("href"))
                    
                    # 提取日期
                    date_el = item.locator("p.item-list__date")
                    raw_date = date_el.inner_text().strip() if date_el.count() else ""
                    date = parse_date(raw_date)
                    
                    # 提取摘要
                    summary_el = item.locator(".item-list__description")
                    summary = summary_el.inner_text().strip() if summary_el.count() else ""
                    
                    article_data = {
                        "title": title,
                        "date": date,
                        "source": "Central Bank of Belize",
                        "author": "",
                        "sourceUrl": url,
                        "summary": summary,
                        "content": ""
                    }
                    
                    print(f"[-] 正在抓取: {title[:30]}... ({date})")
                    
                    # 处理详情页/文件
                    if is_file_url(url):
                        print(f"    -> 检测到文件链接，跳过详情页抓取")
                        article_data["content"] = f'<a href="{url}" target="_blank">Download Document: {title}</a>'
                    else:
                        # 进入详情页抓取
                        try:
                            # 打开新页面以保持列表页状态
                            detail_page = context.new_page()
                            detail_page.goto(url, wait_until="domcontentloaded", timeout=30000)
                            
                            # 尝试提取正文
                            # 策略1: div.group.margin-large (Agent建议)
                            # 策略2: #top (Agent建议，但可能包含杂项)
                            # 策略3: .interior-layout-main (通用主内容区)
                            content_html = ""
                            
                            # 优先尝试更具体的选择器
                            selectors = [
                                "div.group.margin-large",
                                ".interior-layout-main",
                                "#ContentPlaceholder_T5E3E3FE2005_Col01", # 基于HTML ID推测
                                "#top"
                            ]
                            
                            for selector in selectors:
                                if detail_page.locator(selector).count() > 0:
                                    # 检查内容长度，避免提取到空容器
                                    html_candidate = detail_page.locator(selector).first.inner_html()
                                    if len(html_candidate.strip()) > 100:
                                        content_html = html_candidate
                                        break
                            
                            if not content_html or len(content_html.strip()) < 50:
                                print("    -> 正文提取为空，回退为链接")
                                article_data["content"] = f'<a href="{url}" target="_blank">View Original Article: {url}</a>'
                            else:
                                article_data["content"] = clean_html_content(content_html, url)
                                
                            detail_page.close()
                            
                        except Exception as e:
                            print(f"    -> 详情页抓取失败: {e}")
                            article_data["content"] = f'<a href="{url}" target="_blank">{url}</a>'
                            try:
                                detail_page.close()
                            except:
                                pass

                    articles.append(article_data)
                    # 礼貌性延时
                    time.sleep(1)
                    
                except Exception as e:
                    print(f"[Error] 处理单条数据出错: {e}")
                    continue

            # 检查是否达到目标数量
            if len(articles) >= MAX_ITEMS:
                break
                
            # 翻页逻辑
            # 查找 "»" 按钮或下一页链接
            # HTML: <a class="" href="...?startRow=20...">»</a>
            next_btn = page.locator(".pagination a:has-text('»')")
            
            if next_btn.count() > 0 and next_btn.is_visible():
                print("[*] 正在翻页...")
                try:
                    # 获取下一页链接并跳转，比直接click更稳定
                    next_href = next_btn.get_attribute("href")
                    if next_href:
                        next_url = urljoin(TARGET_URL, next_href)
                        page.goto(next_url, wait_until="domcontentloaded", timeout=30000)
                        page_num += 1
                        time.sleep(2) # 等待加载
                    else:
                        print("[Warn] 下一页链接为空，停止翻页")
                        break
                except Exception as e:
                    print(f"[Error] 翻页失败: {e}")
                    break
            else:
                print("[*] 没有下一页了")
                break
                
        browser.close()
        
    return articles

# ==========================================
# 保存逻辑
# ==========================================

def save_results(articles: list, output_dir: str):
    ensure_dir(output_dir)
    
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
        
    print(f"\n[Success] 已保存 {len(articles)} 条新闻到: {output_path}")

# ==========================================
# 主程序入口
# ==========================================

def main():
    print("=== 伯利兹中央银行新闻爬虫启动 ===")
    print(f"目标: {TARGET_URL}")
    print(f"计划抓取数量: {MAX_ITEMS}")
    
    try:
        articles = crawl_news()
        
        if articles:
            save_results(articles, OUTPUT_DIR)
        else:
            print("[Warn] 未抓取到任何数据")
            
    except Exception as e:
        print(f"[Fatal Error] 程序运行异常: {e}")

if __name__ == "__main__":
    main()
