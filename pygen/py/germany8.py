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
import json
import os
import time
from datetime import datetime

# ==========================================
# 配置区域
# ==========================================
# API 配置
API_URL = "https://api.stage.bio/api/account/bundesbank/source/entry"
WIDGET_ID = "63aafa2172676c874ed99cce39464264"

# 输出目录配置
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# 爬取数量限制
LIMIT = 5

# ==========================================
# 核心爬虫逻辑
# ==========================================

def fetch_news_from_api():
    """
    调用 API 获取新闻数据
    """
    params = {
        "amount": "10",  # 获取足够的数据以便截取前5条
        "widgetId": WIDGET_ID
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.news.bundesbank.de",
        "Referer": "https://www.news.bundesbank.de/"
    }

    print(f"正在请求 API: {API_URL}")
    try:
        response = requests.get(API_URL, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if isinstance(data, list):
            return data
        else:
            print("API 返回格式异常，期望列表")
            return []
            
    except requests.RequestException as e:
        print(f"请求 API 失败: {e}")
        return []

def process_news_data(raw_data_list):
    """
    处理原始 JSON 数据，提取所需字段
    """
    articles = []
    
    # 只处理前 LIMIT 条
    for item in raw_data_list[:LIMIT]:
        try:
            # 1. 提取日期 (Unix 时间戳转换)
            timestamp = item.get('original_created_at')
            date_str = ""
            if timestamp:
                try:
                    dt = datetime.fromtimestamp(int(timestamp))
                    date_str = dt.strftime('%Y-%m-%d')
                except (ValueError, TypeError):
                    print(f"日期转换失败: {timestamp}")

            # 2. 提取内容和标题
            # 社交媒体帖子通常没有独立标题，使用内容的前部分作为标题
            content = item.get('content', '') or ""
            title = content.split('\n')[0][:100]  # 取第一行或前100字
            if len(content) > 100 and len(title) == 100:
                title += "..."
            if not title:
                title = "无标题新闻"

            # 3. 提取来源信息
            source_info = item.get('source', {})
            source_name = source_info.get('name', 'Bundesbank')
            author = source_info.get('handle', '')

            # 4. 提取链接
            source_url = item.get('source_url', '')

            # 5. 构建文章对象
            article = {
                "title": title,
                "date": date_str,
                "source": source_name,
                "author": author,
                "sourceUrl": source_url,
                "summary": title,  # 社交媒体短文摘要即标题
                "content": content  # 保留完整文本内容
            }
            
            articles.append(article)
            
        except Exception as e:
            print(f"处理单条数据时出错: {e}")
            continue
            
    return articles

def save_results(articles):
    """
    保存结果到 JSON 文件
    """
    # 确保目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"bundesbank_social_news_{timestamp}.json"
    filepath = os.path.join(OUTPUT_DIR, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"结果已保存到: {filepath}")
        
        # 同时生成一个简单的 Markdown 预览
        md_filename = f"bundesbank_social_news_{timestamp}.md"
        md_filepath = os.path.join(OUTPUT_DIR, md_filename)
        with open(md_filepath, 'w', encoding='utf-8') as f:
            f.write(f"# Bundesbank Social Media News\n\n")
            f.write(f"爬取时间: {result['crawlTime']}\n\n")
            for idx, art in enumerate(articles, 1):
                f.write(f"## {idx}. {art['title']}\n")
                f.write(f"- **日期**: {art['date']}\n")
                f.write(f"- **来源**: {art['source']} ({art['author']})\n")
                f.write(f"- **链接**: {art['sourceUrl']}\n\n")
                f.write(f"{art['content']}\n\n")
                f.write("---\n\n")
        print(f"Markdown 预览已保存到: {md_filepath}")
        
    except IOError as e:
        print(f"保存文件失败: {e}")

def main():
    print("开始爬取德国央行社交媒体新闻...")
    
    # 1. 获取数据
    raw_data = fetch_news_from_api()
    
    if not raw_data:
        print("未获取到数据，程序结束。")
        return

    print(f"API 返回了 {len(raw_data)} 条原始数据")

    # 2. 处理数据
    articles = process_news_data(raw_data)
    
    print(f"成功提取 {len(articles)} 条新闻")

    # 3. 保存结果
    if articles:
        save_results(articles)
    else:
        print("没有有效的新闻数据可保存。")

if __name__ == "__main__":
    main()
