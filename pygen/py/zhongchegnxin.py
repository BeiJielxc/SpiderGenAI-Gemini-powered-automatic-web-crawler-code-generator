#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爬虫脚本 - 中诚信国际 (企业评级板块)
自动生成于 PyGen

功能：爬取中诚信国际网站企业评级板块各类债券的评级报告
时间范围：2026-02-01 至 2026-02-28
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

# 配置
BASE_API_URL = "https://website-api.ccxi.com.cn/admin/content/cspj/page"
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
# START_DATE = "2026-02-01"  # (disabled by PyGen: injected dates take precedence)
# END_DATE = "2026-02-28"  # (disabled by PyGen: injected dates take precedence)

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.ccxi.com.cn/",
    "Origin": "https://www.ccxi.com.cn",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json;charset=UTF-8"
}

# 分类配置 (基于 verified_category_mapping)
CATEGORIES = {
    "主体评级": {
        "filters": {"launchedstatus": "启用", "levelone": "73", "leveltwo": "74", "levelthree": "121"},
        "orderby": {"rankdate": "desc"}
    },
    "短期融资券": {
        "filters": {"launchedstatus": "启用", "levelone": "73", "leveltwo": "74", "levelthree": "81"},
        "orderby": {"rankdate": "desc"}
    },
    "中期票据": {
        "filters": {"launchedstatus": "启用", "levelone": "73", "leveltwo": "74", "levelthree": "82"},
        "orderby": {"rankdate": "desc"}
    },
    "企业债券": {
        "filters": {"launchedstatus": "启用", "levelone": "73", "leveltwo": "74", "levelthree": "83"},
        "orderby": {"rankdate": "desc"}
    },
    "超短期融资券": {
        "filters": {"launchedstatus": "启用", "levelone": "73", "leveltwo": "74", "levelthree": "84"},
        "orderby": {"rankdate": "desc"}
    },
    "可转及可交换债券": {
        "filters": {"launchedstatus": "启用", "levelone": "73", "leveltwo": "74", "levelthree": "126"},
        "orderby": {"rankdate": "desc"}
    },
    "公司债券": {
        "filters": {"launchedstatus": "启用", "levelone": "73", "leveltwo": "74", "levelthree": "122"},
        "orderby": {"rankdate": "desc"}
    },
    "其他": {
        "filters": {"launchedstatus": "启用", "levelone": "130"},
        "orderby": {"rankdate": "desc"}
    },
    "地方政府债": {
        "filters": {"launchedstatus": "启用", "levelone": "73", "leveltwo": "78"},
        "orderby": {"rankdate": "desc"}
    },
    "熊猫债": {
        "filters": {"launchedstatus": "启用", "levelone": "73", "leveltwo": "79"},
        "orderby": {"rankdate": "desc"}
    },
    "主动评级": {
        "filters": {"launchedstatus": "启用", "status": "1"},
        "orderby": {"rankdate": "desc"}
    }
}

def fetch_data_by_category(category_name: str, config: dict, page: int = 1) -> dict:
    """获取特定分类的一页数据"""
    try:
        # 构造查询参数
        params = {
            "codetranslate": "true",
            "pageNo": page,
            "pageSize": 20,
            "orderby": json.dumps(config["orderby"]),
            "filters": json.dumps(config["filters"])
        }
        
        response = requests.get(BASE_API_URL, params=params, headers=HEADERS, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[ERROR] 获取分类 {category_name} 第 {page} 页失败: {e}")
        return {}

def save_results(reports: list, output_path: str):
    """保存结果到文件"""
    # 构建下载头信息
    download_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": "https://www.ccxi.com.cn/",
    }
    
    result = {
        "total": len(reports),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "downloadHeaders": download_headers,
        "reports": reports
    }
    
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[INFO] 已保存 {len(reports)} 条记录到 {output_path}")
    except Exception as e:
        print(f"[ERROR] 保存文件失败: {e}")

def main():
    """主函数"""
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        
    all_reports = []
    
    print(f"[INFO] 开始爬取，目标日期范围: {START_DATE} 至 {END_DATE}")
    
    for category_name, config in CATEGORIES.items():
        print(f"\n[INFO] 正在处理分类: {category_name}")
        page = 1
        category_active = True
        
        while category_active:
            print(f"  -> 正在请求第 {page} 页...")
            data = fetch_data_by_category(category_name, config, page)
            
            # 检查响应结构
            if not data or "data" not in data or "records" not in data["data"]:
                print(f"  [WARN] 分类 {category_name} 第 {page} 页无有效数据，停止翻页")
                break
                
            records = data["data"]["records"]
            if not records:
                print(f"  [INFO] 分类 {category_name} 第 {page} 页记录为空，停止翻页")
                break
                
            items_added = 0
            for item in records:
                # 提取日期
                rank_date = item.get("rankdate")
                if not rank_date:
                    continue
                
                # 截取日期部分 (YYYY-MM-DD)
                rank_date = rank_date.split(" ")[0]
                
                # 日期范围检查 (假设列表按日期倒序排列)
                if rank_date < START_DATE:
                    print(f"  [INFO] 发现日期 {rank_date} 早于开始日期 {START_DATE}，停止该分类爬取")
                    category_active = False
                    break
                
                if rank_date > END_DATE:
                    continue
                
                # 提取字段
                name = item.get("bondname") or item.get("issuers") or "未命名报告"
                # 优先取主体报告，其次取债项报告
                download_url = item.get("subjectreport") or item.get("bondreport") or ""
                
                # 只有在有下载链接或明确需要记录时才添加
                # 这里我们保留记录，即使没有下载链接，以便完整性
                
                report = {
                    "id": str(item.get("id", "")),
                    "category": category_name,
                    "name": name,
                    "date": rank_date,
                    "downloadUrl": download_url,
                    "fileType": "pdf" if download_url else "",
                    "details": {
                        "subjectLevel": item.get("subjectlevel", ""),
                        "expectation": item.get("expectation", "")
                    }
                }
                
                all_reports.append(report)
                items_added += 1
            
            print(f"  -> 第 {page} 页处理完毕，新增 {items_added} 条记录")
            
            # 如果当前页没有添加任何数据，且是因为日期太早导致的退出，循环会在上面 break
            # 如果是因为所有日期都比 END_DATE 大（虽然不太可能，因为是倒序），则继续翻页
            
            page += 1
            time.sleep(1) # 避免请求过快
            
    # 保存最终结果
    output_file = os.path.join(OUTPUT_DIR, f"ccxi_reports_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    save_results(all_reports, output_file)


# === PyGen 注入：可信分类映射（来源：真实交互抓包 filters） ===
CATEGORIES = {
  "主体评级": {
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
  "短期融资券": {
    "filters": {
      "launchedstatus": "启用",
      "levelone": "73",
      "leveltwo": "74",
      "levelthree": "81"
    },
    "orderby": {
      "rankdate": "desc"
    }
  },
  "中期票据": {
    "filters": {
      "launchedstatus": "启用",
      "levelone": "73",
      "leveltwo": "74",
      "levelthree": "82"
    },
    "orderby": {
      "rankdate": "desc"
    }
  },
  "企业债券": {
    "filters": {
      "launchedstatus": "启用",
      "levelone": "73",
      "leveltwo": "74",
      "levelthree": "83"
    },
    "orderby": {
      "rankdate": "desc"
    }
  },
  "超短期融资券": {
    "filters": {
      "launchedstatus": "启用",
      "levelone": "73",
      "leveltwo": "74",
      "levelthree": "84"
    },
    "orderby": {
      "rankdate": "desc"
    }
  },
  "可转及可交换债券": {
    "filters": {
      "launchedstatus": "启用",
      "levelone": "73",
      "leveltwo": "74",
      "levelthree": "126"
    },
    "orderby": {
      "rankdate": "desc"
    }
  },
  "公司债券": {
    "filters": {
      "launchedstatus": "启用",
      "levelone": "73",
      "leveltwo": "74",
      "levelthree": "122"
    },
    "orderby": {
      "rankdate": "desc"
    }
  },
  "其他": {
    "filters": {
      "launchedstatus": "启用",
      "levelone": "130"
    },
    "orderby": {
      "rankdate": "desc"
    }
  },
  "地方政府债": {
    "filters": {
      "launchedstatus": "启用",
      "levelone": "73",
      "leveltwo": "78"
    },
    "orderby": {
      "rankdate": "desc"
    }
  },
  "熊猫债": {
    "filters": {
      "launchedstatus": "启用",
      "levelone": "73",
      "leveltwo": "79"
    },
    "orderby": {
      "rankdate": "desc"
    }
  },
  "主动评级": {
    "filters": {
      "launchedstatus": "启用",
      "status": "1"
    },
    "orderby": {
      "rankdate": "desc"
    }
  }
}
# === PyGen 注入结束 ===
if __name__ == "__main__":
    main()
