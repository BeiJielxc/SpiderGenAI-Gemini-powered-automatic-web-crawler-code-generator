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
import requests
from bs4 import BeautifulSoup
import json
import os
import time
from datetime import datetime
from urllib.parse import urljoin
import re

# ==========================================
# 配置区域
# ==========================================

# 目标URL
BASE_URL = "https://supervalores.gob.pa/comunicados-avisos-y-actualidad/"

# 爬取数量限制（任务要求前5条）
MAX_ITEMS = 5

# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

# 西班牙语月份映射
SPANISH_MONTHS = {
    "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
    "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
    "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12"
}

# ==========================================
# 核心功能函数
# ==========================================

def parse_spanish_date(date_str):
    """解析西班牙语日期字符串，例如 '26 de enero de 2026'"""
    if not date_str:
        return ""
    try:
        # 移除多余空格并转小写
        date_str = date_str.strip().lower()
        # 替换月份
        for es_month, num_month in SPANISH_MONTHS.items():
            if es_month in date_str:
                date_str = date_str.replace(es_month, num_month)
                break
        
        # 移除 'de'
        date_str = date_str.replace(" de ", " ").replace(" del ", " ")
        
        # 尝试解析多种格式
        # 格式: 26 01 2026
        parts = date_str.split()
        if len(parts) >= 3:
            day = parts[0].zfill(2)
            month = parts[1].zfill(2)
            year = parts[2]
            # 简单的验证
            if day.isdigit() and month.isdigit() and year.isdigit() and len(year) == 4:
                return f"{year}-{month}-{day}"
                
        return date_str
    except Exception as e:
        print(f"日期解析错误: {e} (原始字符串: {date_str})")
        return date_str

def clean_html_content(html_content, base_url):
    """清洗HTML内容：修复相对路径图片和链接"""
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除无用的脚本和样式
        for script in soup(["script", "style", "iframe"]):
            script.decompose()

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

def fetch_detail_page(url):
    """获取详情页内容"""
    try:
        # 检查是否是 PDF 文件
        if url.lower().endswith('.pdf'):
            return {
                "content": f'<p>这是一个PDF文档，请点击下载：<a href="{url}">下载PDF</a></p>',
                "downloadUrl": url
            }

        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        
        # 检查 Content-Type
        content_type = response.headers.get('Content-Type', '').lower()
        if 'application/pdf' in content_type:
             return {
                "content": f'<p>这是一个PDF文档，请点击下载：<a href="{url}">下载PDF</a></p>',
                "downloadUrl": url
            }

        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 提取正文
        # 优先尝试 WordPress 标准正文容器
        content_div = soup.select_one('div.entry-content')
        
        # 备选容器：Elementor 容器
        if not content_div:
            content_div = soup.select_one('div.elementor-widget-container')
            
        # 如果还是没找到，尝试 main 标签
        if not content_div:
            content_div = soup.select_one('main#main')

        content_html = ""
        download_url = ""

        if content_div:
            # 提取正文中的下载链接（如果有）
            pdf_link = content_div.select_one('a[href$=".pdf"]')
            if pdf_link:
                download_url = urljoin(url, pdf_link['href'])
            
            content_html = clean_html_content(str(content_div), url)
        
        return {
            "content": content_html,
            "downloadUrl": download_url
        }

    except Exception as e:
        print(f"获取详情页失败 {url}: {e}")
        return {"content": "", "downloadUrl": ""}

def save_results(articles, output_path):
    """保存结果为JSON"""
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"已保存 {len(articles)} 条新闻到 {output_path}")
    except Exception as e:
        print(f"保存文件失败: {e}")

# ==========================================
# 主爬虫逻辑
# ==========================================

def crawl_news():
    articles = []
    page = 1
    
    print(f"开始爬取，目标数量: {MAX_ITEMS}")
    
    while len(articles) < MAX_ITEMS:
        # 构建分页 URL
        if page == 1:
            url = BASE_URL
        else:
            url = f"{BASE_URL}page/{page}/"
            
        print(f"正在抓取第 {page} 页: {url}")
        
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code == 404:
                print("页面不存在，停止翻页")
                break
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 定位新闻列表容器
            # 根据提供的 HTML，文章在 article.wp-show-posts-single 中
            news_items = soup.select('article.wp-show-posts-single')
            
            if not news_items:
                print("未找到新闻列表项，停止翻页")
                break
                
            print(f"本页发现 {len(news_items)} 条新闻")
            
            for item in news_items:
                if len(articles) >= MAX_ITEMS:
                    break
                
                try:
                    # 提取标题和链接
                    title_tag = item.select_one('h2.wp-show-posts-entry-title a')
                    if not title_tag:
                        continue
                        
                    title = title_tag.get_text(strip=True)
                    link = urljoin(url, title_tag['href'])
                    
                    # 提取日期
                    date_tag = item.select_one('time.wp-show-posts-entry-date')
                    date_str = date_tag.get_text(strip=True) if date_tag else ""
                    formatted_date = parse_spanish_date(date_str)
                    
                    # 提取摘要
                    summary_div = item.select_one('div.wp-show-posts-entry-content')
                    summary = summary_div.get_text(strip=True)[:200] + "..." if summary_div else ""
                    
                    print(f"处理新闻: {title} ({formatted_date})")
                    
                    # 获取详情页内容
                    detail_data = fetch_detail_page(link)
                    
                    # 如果详情页没有提取到下载链接，尝试从列表页提取
                    download_url = detail_data.get('downloadUrl', '')
                    if not download_url and summary_div:
                        pdf_link = summary_div.select_one('a[href$=".pdf"]')
                        if pdf_link:
                            download_url = urljoin(url, pdf_link['href'])

                    article = {
                        "id": str(len(articles) + 1),
                        "title": title,
                        "date": formatted_date,
                        "source": "Superintendencia del Mercado de Valores",
                        "author": "SMV",
                        "sourceUrl": link,
                        "summary": summary,
                        "content": detail_data.get('content', ''),
                        "downloadUrl": download_url
                    }
                    
                    articles.append(article)
                    
                    # 礼貌性延时
                    time.sleep(1)
                    
                except Exception as e:
                    print(f"处理单条新闻出错: {e}")
                    continue
            
            # 检查是否有下一页
            # 根据 HTML: <a class="next page-numbers" href="...">Siguiente →</a>
            next_page = soup.select_one('a.next.page-numbers')
            if not next_page:
                print("没有下一页了")
                break
                
            page += 1
            time.sleep(2)
            
        except Exception as e:
            print(f"请求页面失败: {e}")
            break
            
    return articles

def main():
    # 确保输出目录存在
    if not os.path.exists(OUTPUT_DIR):
        try:
            os.makedirs(OUTPUT_DIR)
        except Exception as e:
            print(f"创建目录失败: {e}")
            return

    # 执行爬取
    articles = crawl_news()
    
    # 生成文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"supervalores_news_{timestamp}.json"
    output_path = os.path.join(OUTPUT_DIR, filename)
    
    # 保存结果
    save_results(articles, output_path)

if __name__ == "__main__":
    main()
