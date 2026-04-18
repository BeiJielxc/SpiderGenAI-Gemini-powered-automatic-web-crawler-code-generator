#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爬虫脚本 - 中诚信国际 (企业评级板块)
自动生成于 PyGen

功能：
1. 自动获取或使用默认配置遍历“企业评级”下的所有子板块（如主体评级、短期融资券等）
2. 按日期范围 (2026-02-01 ~ 2026-02-28) 筛选报告
3. 提取报告名称、发布日期、PDF下载链接
4. 结果保存为 JSON 文件
"""

# === PyGen 注入：HTTP/SSL 韧性层（通用） ===
# 说明：
# 1) 优先保持 verify=True；如确需临时绕过（不推荐），可设置环境变量 PYGEN_INSECURE_SSL=1
# 2) 默认遇到 418/403/429 仅做一次"预热 cookie + 浏览器化 headers"的重试
# 3) 若仍被拦截且本机已安装 Playwright，会自动尝试一次 request-context 兜底
#    如需禁用：设置 PYGEN_DISABLE_PLAYWRIGHT_FALLBACK=1
# === PyGen 注入：日期范围（权威来源：本次输入） ===
# 允许通过环境变量覆盖：PYGEN_START_DATE / PYGEN_END_DATE
# 允许通过命令行覆盖：--start-date / --end-date
import argparse as _argparse
import os as _os

def _pygen_resolve_dates():
    parser = _argparse.ArgumentParser(add_help=False)
    parser.add_argument("--start-date", dest="start_date")
    parser.add_argument("--end-date", dest="end_date")
    args, _ = parser.parse_known_args()
    sd = args.start_date or _os.getenv("PYGEN_START_DATE") or "2026-02-01"
    ed = args.end_date or _os.getenv("PYGEN_END_DATE") or "2026-02-28"
    return sd, ed

START_DATE, END_DATE = _pygen_resolve_dates()
# === PyGen 注入结束 ===
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
from urllib.parse import urlsplit

# ================= 配置区域 =================
# API 基础地址
API_BASE_URL = "https://website-api.ccxi.com.cn/admin/content/cspj/page"
API_CATEGORY_URL = "https://website-api.ccxi.com.cn/admin/content/pjjgfl/list"

# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# 爬取时间范围 (严格过滤)
# START_DATE = "2026-02-01"  # (disabled by PyGen: injected dates take precedence)
# END_DATE = "2026-02-28"  # (disabled by PyGen: injected dates take precedence)

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.ccxi.com.cn/",
    "Origin": "https://www.ccxi.com.cn",
    "Content-Type": "application/json;charset=UTF-8"
}

# 默认分类配置 (作为兜底，如果动态获取失败)
# levelone=73 (信用评级), leveltwo=74 (企业评级)
DEFAULT_CATEGORIES = [
    {"name": "主体评级", "id": "121"},
    {"name": "短期融资券", "id": "81"},
    {"name": "中期票据", "id": "82"},
    {"name": "企业债券", "id": "83"},
    {"name": "超短期融资券", "id": "84"},
    {"name": "公司债券", "id": "86"},
    {"name": "其他", "id": "87"}
]

# ================= 工具函数 =================

def get_categories():
    """
    尝试从 API 动态获取'企业评级'(id=74)下的子分类列表。
    如果失败，返回默认列表。
    """
    print("[INFO] 正在尝试动态获取分类列表...")
    try:
        response = requests.get(API_CATEGORY_URL, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            data = response.json()
            # API 返回结构通常是树形，需要找到 id=74 的节点
            # 这里做一个简单的递归查找
            def find_node(nodes, target_id):
                for node in nodes:
                    if str(node.get('id')) == str(target_id):
                        return node
                    if node.get('children'):
                        result = find_node(node['children'], target_id)
                        if result:
                            return result
                return None

            # 处理可能的不同返回结构 (data.data 或 data)
            root_data = data.get('data', data) if isinstance(data, dict) else data
            if isinstance(root_data, list):
                target_node = find_node(root_data, 74) # 74 是企业评级
                if target_node and target_node.get('children'):
                    categories = []
                    for child in target_node['children']:
                        categories.append({
                            "name": child.get('name', '未知分类'),
                            "id": str(child.get('id'))
                        })
                    print(f"[INFO] 成功获取 {len(categories)} 个子分类")
                    return categories
    except Exception as e:
        print(f"[WARN] 动态获取分类失败: {e}")
    
    print("[INFO] 使用默认分类配置")
    return DEFAULT_CATEGORIES

def fetch_data(category_id: str, page: int = 1) -> list:
    """
    调用 API 获取指定分类的一页数据
    """
    # 构造筛选参数
    filters = {
        "launchedstatus": "启用",
        "levelone": "73",   # 信用评级
        "leveltwo": "74",   # 企业评级
        "levelthree": category_id
    }
    
    # 构造请求参数
    params = {
        "pageNo": page,
        "pageSize": 20,
        # 按评级日期倒序
        "orderby": json.dumps({"rankdate": "desc"}), 
        "filters": json.dumps(filters)
    }
    
    try:
        response = requests.get(API_BASE_URL, params=params, headers=HEADERS, timeout=15)
        if response.status_code == 200:
            res_json = response.json()
            if res_json.get("code") == 0:
                return res_json.get("data", {}).get("records", [])
            else:
                print(f"[ERROR] API 返回错误代码: {res_json.get('msg')}")
        else:
            print(f"[ERROR] HTTP 状态码: {response.status_code}")
    except Exception as e:
        print(f"[ERROR] 请求异常: {e}")
        time.sleep(2) # 出错后稍作等待
    
    return []

def save_results(reports: list, output_path: str):
    """保存结果到 JSON 文件"""
    # 构建下载头信息（供后续下载 PDF/附件时使用，绕过防盗链 403）
    download_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": "https://www.ccxi.com.cn/",
    }
    
    result = {
        "total": len(reports),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dateRange": f"{START_DATE} to {END_DATE}",
        "downloadHeaders": download_headers,
        "reports": reports
    }
    
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[INFO] 已保存 {len(reports)} 条记录到 {output_path}")
    except Exception as e:
        print(f"[ERROR] 保存文件失败: {e}")

# ================= 主程序 =================

def main():
    # 确保输出目录存在
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
    
    print(f"[INFO] 启动爬虫 - 中诚信国际 (企业评级)")
    print(f"[INFO] 目标日期范围: {START_DATE} 至 {END_DATE}")
    
    # 1. 获取分类列表
    categories = get_categories()
    all_reports = []
    
    # 2. 遍历每个分类
    for cat in categories:
        cat_name = cat['name']
        cat_id = cat['id']
        print(f"\n[INFO] >>> 开始爬取分类: {cat_name} (ID: {cat_id})")
        
        page = 1
        while True:
            print(f"  -> 正在抓取第 {page} 页...")
            items = fetch_data(cat_id, page)
            
            # 退出条件②：当前页无数据
            if not items:
                print("  -> 当前页无数据，停止本分类翻页")
                break
            
            page_valid_count = 0
            stop_paging = False
            
            for item in items:
                # 提取日期: 优先使用 rankdate (评级日期), 其次 createtime
                date_str = item.get("rankdate")
                if not date_str and item.get("createtime"):
                    date_str = item.get("createtime")[:10]
                
                # 如果完全没有日期，跳过（无法判断范围）
                if not date_str:
                    continue
                
                # 日期范围判断
                if date_str > END_DATE:
                    # 日期太新，继续往后找
                    continue
                
                if date_str < START_DATE:
                    # 日期太老，因为是倒序排列，后续页只会更老，可以停止
                    # 为了保险，只有当确实小于开始日期时才标记停止
                    stop_paging = True
                    # 注意：这里不能直接 break item 循环，因为同一页可能存在乱序（虽然概率小）
                    # 但通常 API 排序是准确的。为了效率，这里选择 break
                    break
                
                # 提取字段
                # 报告名称：优先 bondname (债券名)，其次 issuers (发行人)
                name = item.get("bondname") or item.get("issuers") or "无标题报告"
                
                # 下载链接：优先 bondreport，其次 subjectreport
                download_url = item.get("bondreport") or item.get("subjectreport")
                
                if download_url:
                    page_valid_count += 1
                    all_reports.append({
                        "name": name,
                        "date": date_str,
                        "downloadUrl": download_url,
                        "fileType": "pdf",
                        "category": cat_name,
                        "id": str(item.get("id", ""))
                    })
            
            print(f"  -> 第 {page} 页提取到 {page_valid_count} 条符合日期范围的数据")
            
            # 退出条件：遇到早于开始日期的记录
            if stop_paging:
                print(f"  -> 遇到早于 {START_DATE} 的记录，停止本分类翻页")
                break
            
            # 翻页
            page += 1
            time.sleep(1) # 避免请求过快
            
    # 3. 保存结果
    output_file = os.path.join(OUTPUT_DIR, f"ccxi_reports_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    save_results(all_reports, output_file)
    print(f"\n[INFO] 爬取完成，共收集 {len(all_reports)} 条报告")


# === PyGen 注入：可信分类映射（来源：真实交互抓包 filters） ===
CATEGORIES = {
  "企业评级/主体评级": {
    "filters": {
      "launchedstatus": "启用",
      "levelone": "73",
      "leveltwo": "74",
      "levelthree": "121"
    },
    "orderby": {
      "rankdate": "desc"
    }
  },
  "企业评级/短期融资券": {
    "filters": {
      "launchedstatus": "启用",
      "levelone": "73",
      "leveltwo": "74",
      "levelthree": "81"
    },
    "orderby": {
      "rankdate": "desc"
    }
  }
}
# === PyGen 注入结束 ===
if __name__ == "__main__":
    main()
