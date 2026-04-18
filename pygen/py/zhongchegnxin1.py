#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爬虫脚本 - 中诚信国际 (企业评级板块)
自动生成于 PyGen

功能：
1. 动态获取“企业评级”下的所有子板块分类
2. 遍历所有子板块，调用 API 获取评级报告数据
3. 按发布日期过滤 (2026-02-01 ~ 2026-02-28)
4. 提取报告名称、日期、PDF下载链接
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
from urllib.parse import urljoin

# ================= 配置区域 =================
# 基础 API 地址
BASE_API_URL = "https://website-api.ccxi.com.cn/admin/content/cspj/page"
CATEGORY_API_URL = "https://website-api.ccxi.com.cn/admin/content/pjjgfl/list"

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
    "Origin": "https://www.ccxi.com.cn",
    "Referer": "https://www.ccxi.com.cn/",
}

# 默认分类配置 (兜底用，如果动态获取失败)
DEFAULT_CATEGORIES = {
    "主体评级": {
        "filters": {
            "launchedstatus": "启用",
            "levelone": "73",
            "leveltwo": "74",
            "levelthree": "121"
        },
        "orderby": {"rankdate": "desc"}
    }
}

# ================= 工具函数 =================

def get_dynamic_categories():
    """
    尝试从 API 动态获取'企业评级'下的所有子分类
    返回格式符合 CATEGORIES 要求
    """
    print("[INFO] 正在动态获取分类列表...")
    try:
        # 这是一个推测的分类树接口，基于通常的 CMS 结构
        # 如果此接口不通或结构不同，将回退到默认配置
        resp = requests.get(CATEGORY_API_URL, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and "data" in data:
                tree = data["data"]
            else:
                tree = data # 假设直接返回列表
            
            categories = {}
            
            # 递归查找或遍历查找 "企业评级"
            # 根据抓包 levelone=73 (信用评级?), leveltwo=74 (企业评级)
            # 我们需要找到 id=74 的节点，获取其 children
            
            target_node = None
            
            # 辅助函数：在树中查找
            def find_node(nodes, target_id):
                for node in nodes:
                    # 检查 ID 是否匹配 (注意类型可能是 int 或 str)
                    if str(node.get("id")) == "74" or node.get("name") == "企业评级":
                        return node
                    if "children" in node and node["children"]:
                        res = find_node(node["children"], target_id)
                        if res:
                            return res
                return None

            target_node = find_node(tree, "74")
            
            if target_node and "children" in target_node:
                print(f"[INFO] 成功找到 '企业评级' 分类，包含 {len(target_node['children'])} 个子板块")
                for child in target_node["children"]:
                    name = child.get("name")
                    cid = str(child.get("id"))
                    if name and cid:
                        categories[name] = {
                            "filters": {
                                "launchedstatus": "启用",
                                "levelone": "73",   # 假设父级固定
                                "leveltwo": "74",   # 企业评级固定
                                "levelthree": cid
                            },
                            "orderby": {"rankdate": "desc"}
                        }
                return categories
            else:
                print("[WARN] 未找到 '企业评级' 节点或无子分类")
    except Exception as e:
        print(f"[WARN] 动态获取分类失败: {e}")
    
    print("[INFO] 使用默认分类配置")
    return DEFAULT_CATEGORIES

def fetch_data(page_num: int, category_config: dict) -> dict:
    """
    获取一页数据
    """
    # 构造查询参数
    filters = category_config["filters"]
    orderby = category_config["orderby"]
    
    params = {
        "codetranslate": "true",
        "pageNo": page_num,
        "pageSize": 20,
        "orderby": json.dumps(orderby),
        "filters": json.dumps(filters)
    }
    
    try:
        response = requests.get(BASE_API_URL, params=params, headers=HEADERS, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[ERROR] 请求第 {page_num} 页失败: {e}")
        return {}

def save_results(reports: list, output_path: str, target_url: str = ""):
    """保存结果到 JSON 文件"""
    # 确保目录存在
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    # 构建下载头信息
    download_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": target_url or "https://www.ccxi.com.cn/",
    }
    
    result = {
        "total": len(reports),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dateRange": f"{START_DATE} to {END_DATE}",
        "downloadHeaders": download_headers,
        "reports": reports
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[INFO] 已保存 {len(reports)} 条记录到 {output_path}")

# ================= 主程序 =================

def main():
    print("=== 开始爬取 中诚信国际-企业评级报告 ===")
    print(f"目标时间范围: {START_DATE} ~ {END_DATE}")
    
    # 1. 获取分类配置
    categories = get_dynamic_categories()
    
    all_reports = []
    
    # 2. 遍历分类
    for cat_name, cat_config in categories.items():
        print(f"\n>>> 正在处理分类: {cat_name}")
        
        page = 1
        cat_reports = []
        stop_paging = False
        
        while True:
            print(f"    正在抓取第 {page} 页...")
            
            # 添加随机延时，避免请求过快
            time.sleep(1)
            
            resp_json = fetch_data(page, cat_config)
            
            # 检查响应有效性
            if not resp_json or "data" not in resp_json or not resp_json["data"]:
                print("    [INFO] 响应为空或格式错误，停止翻页")
                break
                
            data_obj = resp_json["data"]
            records = data_obj.get("records", [])
            
            if not records:
                print("    [INFO] 当前页无数据，停止翻页")
                break
            
            # 处理当前页记录
            valid_count = 0
            for item in records:
                # 提取日期
                rank_date = item.get("rankdate")
                if not rank_date:
                    # 尝试其他日期字段
                    rank_date = item.get("createtime", "")[:10]
                
                # 日期过滤逻辑
                if not rank_date:
                    continue # 无日期记录跳过
                
                # 比较日期 (字符串比较 YYYY-MM-DD)
                if rank_date > END_DATE:
                    continue # 日期太新，跳过
                
                if rank_date < START_DATE:
                    # 日期太老，由于是按日期倒序，后续页只会更老，直接停止翻页
                    print(f"    [INFO] 遇到过期数据 ({rank_date} < {START_DATE})，停止该分类抓取")
                    stop_paging = True
                    break
                
                # 提取字段
                # 优先取主体报告，其次取债项报告
                download_url = item.get("subjectreport")
                if not download_url:
                    download_url = item.get("bondreport")
                
                # 如果没有下载链接，视情况处理，这里仅保留有链接或有名称的记录
                # 题目要求提取报告PDF，如果没有链接可能价值不大，但为了完整性还是保留
                
                name = item.get("bondname") or item.get("issuers") or "未知名称"
                
                # 确定文件类型
                file_type = "pdf" # 默认
                if download_url:
                    if download_url.lower().endswith(".doc") or download_url.lower().endswith(".docx"):
                        file_type = "doc"
                    elif download_url.lower().endswith(".xls") or download_url.lower().endswith(".xlsx"):
                        file_type = "xls"
                
                report = {
                    "category": cat_name,
                    "name": name,
                    "date": rank_date,
                    "downloadUrl": download_url,
                    "fileType": file_type,
                    "rating": item.get("subjectlevel", ""), # 额外信息：评级
                    "outlook": item.get("expectation", "")  # 额外信息：展望
                }
                
                cat_reports.append(report)
                valid_count += 1
            
            print(f"    本页提取有效数据: {valid_count} 条")
            
            if stop_paging:
                break
                
            # 翻页逻辑
            page += 1
            # 安全限制，防止意外无限翻页
            if page > 100: 
                print("    [WARN] 达到最大页数限制 (100)，强制停止")
                break
        
        print(f"    分类 [{cat_name}] 完成，共收集 {len(cat_reports)} 条")
        all_reports.extend(cat_reports)
    
    # 3. 保存结果
    output_file = os.path.join(OUTPUT_DIR, f"ccxi_reports_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    save_results(all_reports, output_file, target_url="https://www.ccxi.com.cn/creditrating/result")
    
    print("\n=== 爬取任务完成 ===")


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
  }
}
# === PyGen 注入结束 ===
if __name__ == "__main__":
    main()
