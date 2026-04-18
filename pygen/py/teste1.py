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
import random
from datetime import datetime
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# ================= 配置区域 =================
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
TARGET_URL = "https://www.cityam.com/news/"
MAX_ITEMS = 5  # 任务目标：只爬取前5条

# ================= 工具函数 =================

def clean_html_content(html_content, base_url):
    """
    清洗 HTML 内容：
    1. 将相对路径图片/链接转换为绝对路径
    2. 移除无用标签
    """
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 修复图片链接
        for img in soup.find_all('img'):
            # 处理 src
            if img.get('src'):
                img['src'] = urljoin(base_url, img['src'])
            # 处理懒加载 data-src
            if img.get('data-src'):
                img['src'] = urljoin(base_url, img['data-src'])
            # 处理 srcset (可选，简单起见通常只处理 src)
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
        
        # 移除脚本和样式
        for script in soup(["script", "style"]):
            script.decompose()
            
        return str(soup)
    except Exception as e:
        print(f"内容清洗出错: {e}")
        return html_content

def save_results(articles: list, output_dir: str):
    """保存结果为 JSON 文件"""
    os.makedirs(output_dir, exist_ok=True)
    
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
    
    print(f"已保存 {len(articles)} 条新闻到 {output_path}")

# ================= 爬虫逻辑 =================

def extract_detail_page(page, url):
    """
    进入详情页提取完整内容
    """
    print(f"正在抓取详情页: {url}")
    article_data = {
        "sourceUrl": url,
        "source": "City A.M.",
        "crawl_status": "failed"
    }
    
    try:
        # 导航到详情页
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000) # 等待动态加载
        
        # 尝试关闭 Cookie 弹窗（如果存在），以免遮挡
        try:
            if page.is_visible("#onetrust-accept-btn-handler"):
                page.click("#onetrust-accept-btn-handler", timeout=2000)
                page.wait_for_timeout(1000)
        except:
            pass

        # 获取 HTML 供 BS4 解析
        html = page.content()
        soup = BeautifulSoup(html, 'html.parser')
        
        # 1. 提取标题
        # 优先使用 h1，备用 og:title
        title_tag = soup.select_one('h1')
        if title_tag:
            article_data['title'] = title_tag.get_text(strip=True)
        else:
            meta_title = soup.select_one('meta[property="og:title"]')
            article_data['title'] = meta_title['content'] if meta_title else ""

        # 2. 提取日期
        # 策略：优先查找 time.date-time__time，其次查找 meta 标签
        date_str = ""
        time_tag = soup.select_one('time.date-time__time')
        
        if time_tag and time_tag.get('datetime'):
            date_str = time_tag.get('datetime')
        elif time_tag:
            date_str = time_tag.get_text(strip=True)
        
        if not date_str:
            # 尝试 JSON-LD 或 meta
            meta_date = soup.select_one('meta[property="article:published_time"]')
            if meta_date:
                date_str = meta_date.get('content')
        
        # 简单格式化日期 (保留 YYYY-MM-DD)
        if date_str and 'T' in date_str:
            date_str = date_str.split('T')[0]
        article_data['date'] = date_str

        # 3. 提取作者
        author_tag = soup.select_one('.author-name a, .author__name')
        article_data['author'] = author_tag.get_text(strip=True) if author_tag else "City A.M."

        # 4. 提取正文
        # 使用分析得出的最佳选择器
        content_selector = "article.content-container.content-container__single"
        content_tag = soup.select_one(content_selector)
        
        # 备选选择器
        if not content_tag:
            content_tag = soup.select_one("#main article")
            
        if content_tag:
            # 移除广告和推荐模块
            for trash in content_tag.select('.ad-container, .related-articles, .outbrain-container'):
                trash.decompose()
                
            raw_html = str(content_tag)
            article_data['content'] = clean_html_content(raw_html, url)
            
            # 生成摘要
            text_content = content_tag.get_text(strip=True)
            article_data['summary'] = text_content[:200] + "..." if len(text_content) > 200 else text_content
        else:
            article_data['content'] = ""
            article_data['summary'] = ""

        article_data['crawl_status'] = "success"
        return article_data

    except Exception as e:
        print(f"详情页解析错误 {url}: {e}")
        return None

def crawl_news():
    """主爬虫函数"""
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
            locale="en-GB"
        )
        page = context.new_page()
        
        try:
            print(f"正在访问列表页: {TARGET_URL}")
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000) # 等待列表渲染
            
            # 处理 Cookie 弹窗
            try:
                if page.is_visible("#onetrust-accept-btn-handler"):
                    print("点击 Cookie 同意按钮...")
                    page.click("#onetrust-accept-btn-handler")
                    page.wait_for_timeout(1000)
            except:
                pass

            # 解析列表页
            list_html = page.content()
            soup = BeautifulSoup(list_html, 'html.parser')
            
            # 提取新闻列表项
            # 选择器: .content-listing__content-item
            items = soup.select('.content-listing__content-item')
            print(f"列表页找到 {len(items)} 个新闻项")
            
            # 收集前5个链接
            links_to_crawl = []
            for item in items:
                # 提取链接和标题
                link_tag = item.select_one('.card__title a')
                if link_tag and link_tag.get('href'):
                    url = urljoin(TARGET_URL, link_tag.get('href'))
                    title = link_tag.get_text(strip=True)
                    
                    # 简单去重
                    if url not in [x['url'] for x in links_to_crawl]:
                        links_to_crawl.append({'url': url, 'title': title})
                    
                    if len(links_to_crawl) >= MAX_ITEMS:
                        break
            
            print(f"目标锁定: 准备抓取前 {len(links_to_crawl)} 条新闻详情")
            
            # 遍历抓取详情
            for i, link_info in enumerate(links_to_crawl):
                print(f"--- [{i+1}/{len(links_to_crawl)}] ---")
                
                detail_data = extract_detail_page(page, link_info['url'])
                
                if detail_data and detail_data['crawl_status'] == 'success':
                    # 如果详情页没提取到标题，回退使用列表页标题
                    if not detail_data.get('title'):
                        detail_data['title'] = link_info['title']
                    
                    detail_data['id'] = str(i + 1)
                    # 移除临时状态字段
                    del detail_data['crawl_status']
                    
                    articles.append(detail_data)
                    print(f"成功抓取: {detail_data['title']}")
                else:
                    print(f"抓取失败: {link_info['url']}")
                
                # 随机延时，避免请求过快
                time.sleep(random.uniform(1.5, 3.0))
                
        except Exception as e:
            print(f"爬虫运行异常: {e}")
        finally:
            browser.close()
            
    return articles

def main():
    print("=== City A.M. 新闻爬虫启动 ===")
    start_time = time.time()
    
    articles = crawl_news()
    
    if articles:
        save_results(articles, OUTPUT_DIR)
    else:
        print("未获取到有效数据")
        
    print(f"=== 任务结束，耗时: {time.time() - start_time:.2f}秒 ===")

if __name__ == "__main__":
    main()
