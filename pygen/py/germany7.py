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
from datetime import datetime
from urllib.parse import urljoin

# 配置常量
API_URL = "https://api.stage.bio/api/account/bundesbank/source/entry"
WIDGET_ID = "63aafa2172676c874ed99cce39464264"
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
MAX_ITEMS = 5  # 任务目标：前5条

def setup_output_dir():
    """确保输出目录存在"""
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        print(f"创建目录: {OUTPUT_DIR}")

def fetch_news_from_api():
    """调用API获取新闻数据"""
    params = {
        "amount": 10,  # 获取足够的数据以供筛选
        "widgetId": WIDGET_ID
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.news.bundesbank.de/",
        "Origin": "https://www.news.bundesbank.de"
    }

    print(f"正在请求API: {API_URL}")
    try:
        response = requests.get(API_URL, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"API请求失败: {e}")
        return []

def process_articles(api_data):
    """处理API返回的数据，转换为标准格式"""
    articles = []
    
    # 确保数据是列表
    if not isinstance(api_data, list):
        print("API返回数据格式不是列表")
        return articles

    for item in api_data[:MAX_ITEMS]:  # 只取前5条
        try:
            # 1. 提取基础字段
            content_text = item.get("content", "") or ""
            
            # 2. 生成标题 (API返回的title通常为null，使用内容截取)
            title = item.get("title")
            if not title and content_text:
                # 截取前80个字符作为标题，按单词截断
                title = content_text[:80].rsplit(' ', 1)[0] + "..." if len(content_text) > 80 else content_text
            elif not title:
                title = "无标题新闻"

            # 3. 处理日期 (Unix时间戳转YYYY-MM-DD)
            timestamp = item.get("original_created_at")
            if timestamp:
                pub_date = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
            else:
                pub_date = datetime.now().strftime("%Y-%m-%d")

            # 4. 提取来源信息
            source_info = item.get("source", {})
            source_name = source_info.get("name", "Bundesbank News")
            source_url = item.get("source_url", "")

            # 5. 构建正文内容 (HTML格式)
            # 将纯文本转换为HTML段落，并附加图片
            html_content = f"<p>{content_text.replace(chr(10), '<br>')}</p>"
            
            # 处理附件（图片）
            attachments = item.get("attachments", [])
            if attachments:
                html_content += '<div class="attachments">'
                for att in attachments:
                    if att.get("type") == "image" and att.get("url"):
                        img_url = att.get("url")
                        html_content += f'<img src="{img_url}" alt="News Image" style="max-width:100%;"><br>'
                html_content += '</div>'

            article = {
                "id": str(item.get("source_id", "")),
                "title": title.strip(),
                "date": pub_date,
                "author": source_info.get("handle", ""),
                "source": source_name,
                "sourceUrl": source_url,
                "summary": content_text[:200] + "..." if len(content_text) > 200 else content_text,
                "content": html_content
            }
            
            articles.append(article)
            print(f"[提取成功] {pub_date} - {title}")

        except Exception as e:
            print(f"处理单条数据出错: {e}")
            continue

    return articles

def save_results(articles):
    """保存结果到JSON文件"""
    if not articles:
        print("没有数据可保存")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"bundesbank_news_{timestamp}.json"
    filepath = os.path.join(OUTPUT_DIR, filename)

    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n成功保存 {len(articles)} 条新闻到: {filepath}")
    except Exception as e:
        print(f"保存文件失败: {e}")

def main():
    print("=== 开始爬取德国央行新闻 (API模式) ===")
    setup_output_dir()
    
    # 1. 获取数据
    raw_data = fetch_news_from_api()
    
    # 2. 处理数据
    if raw_data:
        articles = process_articles(raw_data)
        
        # 3. 保存结果
        save_results(articles)
    else:
        print("未获取到任何数据")

if __name__ == "__main__":
    main()
