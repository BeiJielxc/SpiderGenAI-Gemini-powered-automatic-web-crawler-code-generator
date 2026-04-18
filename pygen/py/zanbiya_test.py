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
import re
import time
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# 配置参数
TARGET_URL = "https://www.seczambia.org.zm/resources/multimedia/news/"
MAX_ITEMS = 5
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
SOURCE_NAME = "Seczambia-News"

def clean_html_content(html_content, base_url):
    """将 HTML 中的相对路径转换为绝对路径，并确保所有链接在新标签页打开"""
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 修复图片链接
        for img in soup.find_all('img'):
            if img.get('src'):
                img['src'] = urljoin(base_url, img['src'])
                
        # 修复超链接并添加 target="_blank"
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
            a['target'] = '_blank'
                
        return str(soup)
    except Exception as e:
        print(f"内容清洗出错: {e}")
        return html_content

def parse_smart_date(text):
    """从文本中智能提取并格式化日期为 YYYY-MM-DD"""
    if not text:
        return ""
    
    # 尝试匹配 YYYY-MM-DD
    m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    if m: 
        return m.group(1)
    
    # 尝试匹配 Month DD, YYYY (例如: October 14, 2025)
    m = re.search(r'([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,\s+(\d{4})', text)
    if m:
        try:
            d = datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y")
            return d.strftime("%Y-%m-%d")
        except: 
            pass
        
    # 尝试匹配 DD Month YYYY (例如: 30th March 2023)
    m = re.search(r'(\d{1,2})(?:st|nd|rd|th)?\s+([A-Z][a-z]+)\s+(\d{4})', text)
    if m:
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)[:3]} {m.group(3)}", "%d %b %Y")
            return d.strftime("%Y-%m-%d")
        except: 
            pass
            
    return ""

def save_results_json(articles: list, output_path: str):
    """保存爬取结果为JSON格式"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[OK] 已保存 {len(articles)} 条新闻到 {output_path}")

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
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        )
        
        page = context.new_page()
        print(f"-> 正在访问列表页: {TARGET_URL}")
        
        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000) # 等待动态内容加载
        except Exception as e:
            print(f"[FAIL] 访问列表页失败: {e}")
            browser.close()
            return articles

        # 翻页循环收集基础信息
        collected_items = []
        page_num = 1
        
        while len(collected_items) < MAX_ITEMS:
            print(f"-> 正在解析第 {page_num} 页...")
            
            # 提取当前页的列表项
            # 优先使用 e-loop-item，如果找不到则尝试 Agent 提供的选择器
            list_items = page.locator('div.e-loop-item')
            if list_items.count() == 0:
                list_items = page.locator('.elementor-element.elementor-element-35ac47c.e-con-full.e-flex.e-con.e-child')
            
            item_count = list_items.count()
            print(f"   当前页找到 {item_count} 个新闻项")
            
            if item_count == 0:
                print("   [!] 未找到更多新闻项，停止翻页。")
                break
                
            for i in range(item_count):
                if len(collected_items) >= MAX_ITEMS:
                    break
                    
                item = list_items.nth(i)
                
                try:
                    # 提取标题
                    title_loc = item.locator('h1.elementor-heading-title')
                    title = title_loc.inner_text().strip() if title_loc.count() > 0 else ""
                    
                    # 提取链接 (真正的详情页链接在 READ MORE 按钮中)
                    link_loc = item.locator('a.elementor-button-link')
                    url = link_loc.get_attribute('href') if link_loc.count() > 0 else ""
                    if url:
                        url = urljoin(TARGET_URL, url)
                        
                    # 提取摘要
                    summary_loc = item.locator('div.elementor-widget-theme-post-excerpt')
                    summary = summary_loc.inner_text().strip() if summary_loc.count() > 0 else ""
                    
                    if title and url:
                        collected_items.append({
                            "title": title,
                            "sourceUrl": url,
                            "summary": summary,
                            "source": SOURCE_NAME,
                            "author": SOURCE_NAME
                        })
                        print(f"   已收集: {title[:30]}...")
                except Exception as e:
                    print(f"   [!] 解析列表项出错: {e}")
                    continue
            
            # 检查退出条件
            if len(collected_items) >= MAX_ITEMS:
                break
                
            # 尝试点击下一页
            next_btn = page.locator('a.page-numbers.next')
            if next_btn.count() > 0 and next_btn.is_visible():
                try:
                    print("-> 点击下一页...")
                    next_btn.click()
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(2000)
                    page_num += 1
                except Exception as e:
                    print(f"   [!] 翻页失败: {e}")
                    break
            else:
                print("   [!] 没有下一页按钮，停止翻页。")
                break

        # 遍历收集到的链接，获取详情页正文和日期
        detail_page = context.new_page()
        
        # 详情页正文候选选择器
        content_selectors = [
            'div.elementor-widget-theme-post-content', # Agent 推荐
            'div.elementor-location-single',           # 泛化自候选8
            'div[data-elementor-type="single-post"]',
            'div.elementor-widget-container',          # 候选5
            'article',
            'main'
        ]
        
        for idx, item in enumerate(collected_items):
            print(f"\n-> [{idx+1}/{len(collected_items)}] 正在抓取详情: {item['title']}")
            url = item['sourceUrl']
            
            # 检查是否为直接的文件链接
            if url.lower().endswith(('.pdf', '.doc', '.docx', '.xls', '.xlsx')):
                print("   检测到文件链接，跳过页面渲染")
                item['content'] = f'<a href="{url}" target="_blank">{url}</a>'
                item['date'] = parse_smart_date(item['summary']) # 尝试从摘要提取日期
                articles.append(item)
                continue
                
            try:
                detail_page.goto(url, wait_until="domcontentloaded", timeout=30000)
                
                # 尝试提取日期 (从页面全文或特定标签)
                page_text = detail_page.locator('body').inner_text()
                date_str = parse_smart_date(page_text)
                if not date_str:
                    # 如果详情页没找到，尝试从摘要找
                    date_str = parse_smart_date(item['summary'])
                item['date'] = date_str if date_str else ""
                
                # 提取正文
                content_html = ""
                for sel in content_selectors:
                    try:
                        detail_page.wait_for_selector(sel, timeout=5000)
                        el = detail_page.locator(sel).first
                        if el.count():
                            html = el.inner_html()
                            if len(html.strip()) > 50:
                                content_html = html
                                break
                    except Exception:
                        continue
                        
                if content_html:
                    item["content"] = clean_html_content(content_html, url)
                    print("   [OK] 正文提取成功")
                else:
                    # 所有候选 selector 都未匹配到足够内容 → 回退为 URL 链接
                    print("   [!] 未匹配到正文容器，回退为链接")
                    item["content"] = f'<a href="{url}" target="_blank">{url}</a>'
                    
            except Exception as e:
                if "ERR_ABORTED" in str(e) or "Download is starting" in str(e):
                    print("   [!] 下载中断，识别为文件链接")
                    item["content"] = f'<a href="{url}" target="_blank">{url}</a>'
                else:
                    print(f"   [!] 详情页访问异常: {e}，回退为链接")
                    item["content"] = f'<a href="{url}" target="_blank">{url}</a>'
            
            # 确保包含必需字段
            item['id'] = str(idx + 1)
            if 'date' not in item:
                item['date'] = ""
                
            articles.append(item)
            time.sleep(1) # 礼貌性延迟
            
        browser.close()
        
    return articles

def main():
    print("=== 开始执行爬虫任务 ===")
    
    # 确保输出目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # 爬取数据
    articles = crawl_news()
    
    if articles:
        # 保存结果
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = os.path.join(OUTPUT_DIR, f"Seczambia_News_{timestamp}.json")
        save_results_json(articles, json_path)
    else:
        print("[!] 未抓取到任何数据")
        
    print("=== 爬虫任务执行完毕 ===")

if __name__ == "__main__":
    main()
