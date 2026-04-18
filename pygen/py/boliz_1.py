import os
import json
import time
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ================= 配置区域 =================
# 目标URL
BASE_URL = "https://www.centralbank.org.bz"
START_URL = "https://www.centralbank.org.bz/publications-search"

# 爬取数量限制 (用户要求前5条，但为了健壮性，这里设置为5，代码支持翻页)
MAX_ITEMS = 5

# 输出目录
OUTPUT_DIR = "/home/daranp591/SpiderGenAI-Gemini-powered-automatic-web-crawler-code-generator/pygen/output"

# 详情页正文候选选择器 (优先级从高到低)
CONTENT_SELECTORS = [
    "div.group.margin-large",  # Probe 推荐，看起来最精确
    "div.group",               # 稍宽泛
    "#top",                    # 兜底，包含整个内容区
    "div.interior-layout__main" # 另一个可能的容器
]

# ================= 工具函数 =================

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：
    1. 将相对路径转换为绝对路径
    2. 移除无用标签（可选）
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
                # 确保链接在新标签页打开
                a['target'] = '_blank'
                
        return str(soup)
    except Exception as e:
        print(f"内容清洗出错: {e}")
        return html_content

def parse_date(date_str):
    """
    解析日期字符串，格式如 "26 February 2026" -> "2026-02-26"
    """
    if not date_str:
        return ""
    try:
        # 清理多余空格
        date_str = date_str.strip()
        # 尝试解析 "26 February 2026"
        dt = datetime.strptime(date_str, "%d %B %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        try:
            # 尝试其他格式，如 "February 26, 2026"
            dt = datetime.strptime(date_str, "%B %d, %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return date_str # 无法解析则返回原字符串

def is_file_url(url):
    """判断URL是否指向文件"""
    path = urlparse(url).path.lower()
    extensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.zip', '.rar']
    return any(path.endswith(ext) for ext in extensions)

def save_results(articles, output_path):
    """保存结果为JSON"""
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已保存 {len(articles)} 条新闻到 {output_path}")

# ================= 核心爬虫逻辑 =================

def crawl_news():
    # 确保输出目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
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
            locale="en-US"
        )
        
        # 列表页页面对象
        page = context.new_page()
        
        try:
            print(f"正在访问列表页: {START_URL}")
            page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
            
            # 等待列表加载
            page.wait_for_selector("ul.item-list", timeout=15000)
            
            while len(articles) < MAX_ITEMS:
                # 获取当前页的所有列表项
                items = page.locator("ul.item-list > li.item-list__item").all()
                print(f"当前页发现 {len(items)} 条数据")
                
                if not items:
                    print("未找到列表项，停止翻页")
                    break
                
                for item in items:
                    if len(articles) >= MAX_ITEMS:
                        break
                        
                    try:
                        # 提取基础信息
                        title_el = item.locator("a.item-list__title").first
                        date_el = item.locator("p.item-list__date").first
                        summary_el = item.locator(".item-list__description").first
                        
                        title = title_el.inner_text().strip() if title_el.count() else "无标题"
                        link = title_el.get_attribute("href")
                        if link:
                            link = urljoin(BASE_URL, link)
                            
                        date_text = date_el.inner_text().strip() if date_el.count() else ""
                        date = parse_date(date_text)
                        
                        summary = summary_el.inner_text().strip() if summary_el.count() else ""
                        
                        article_data = {
                            "title": title,
                            "date": date,
                            "source": "Central Bank of Belize",
                            "author": "", # 列表页无作者信息
                            "sourceUrl": link,
                            "summary": summary,
                            "content": "" # 稍后填充
                        }
                        
                        print(f"正在处理: {title} ({date})")
                        
                        # --- 详情页处理逻辑 ---
                        if link:
                            # 检查是否为文件链接
                            if is_file_url(link):
                                print(f"  -> 检测到文件链接，跳过详情页抓取")
                                article_data["content"] = f'<a href="{link}" target="_blank">Download Document: {title}</a>'
                            else:
                                # 打开新页面抓取详情
                                detail_page = context.new_page()
                                try:
                                    # 捕获下载事件（防止点击链接直接触发下载导致超时）
                                    with detail_page.expect_download(timeout=2000) as download_info:
                                        # 尝试访问，如果触发下载会抛出异常或被 expect_download 捕获
                                        # 注意：这里我们主要想看是不是 HTML 页面
                                        try:
                                            response = detail_page.goto(link, wait_until="domcontentloaded", timeout=30000)
                                            
                                            # 检查 Content-Type
                                            content_type = response.headers.get("content-type", "")
                                            if "application/pdf" in content_type or "application/octet-stream" in content_type:
                                                print("  -> 响应头显示为文件，跳过正文提取")
                                                article_data["content"] = f'<a href="{link}" target="_blank">Download Document</a>'
                                            else:
                                                # 是 HTML 页面，尝试提取正文
                                                content_html = ""
                                                for sel in CONTENT_SELECTORS:
                                                    try:
                                                        # 等待内容加载
                                                        detail_page.wait_for_selector(sel, timeout=5000)
                                                        el = detail_page.locator(sel).first
                                                        if el.count():
                                                            html = el.inner_html()
                                                            if len(html.strip()) > 50:
                                                                content_html = html
                                                                print(f"  -> 使用选择器提取成功: {sel}")
                                                                break
                                                    except Exception:
                                                        continue
                                                
                                                if content_html:
                                                    article_data["content"] = clean_html_content(content_html, link)
                                                else:
                                                    print("  -> 未能提取到有效正文，回退为链接")
                                                    article_data["content"] = f'<a href="{link}" target="_blank">View Full Article</a>'
                                                    
                                        except Exception as e_nav:
                                            # 导航过程中出错，可能是触发了下载
                                            if "Download is starting" in str(e_nav) or "net::ERR_ABORTED" in str(e_nav):
                                                print("  -> 导航触发下载，视为文件链接")
                                                article_data["content"] = f'<a href="{link}" target="_blank">Download Document</a>'
                                            else:
                                                print(f"  -> 详情页访问出错: {e_nav}")
                                                article_data["content"] = f'<a href="{link}" target="_blank">View Full Article</a>'

                                except Exception as e:
                                    # expect_download 超时意味着没有触发下载，这是好事（说明是网页）
                                    # 但上面的逻辑已经处理了网页访问，这里主要是捕获外层异常
                                    if "Timeout" not in str(e): 
                                        print(f"  -> 详情页处理异常: {e}")
                                        article_data["content"] = f'<a href="{link}" target="_blank">View Full Article</a>'
                                finally:
                                    detail_page.close()
                        
                        articles.append(article_data)
                        
                    except Exception as e:
                        print(f"处理单条数据出错: {e}")
                        continue
                
                # 检查是否需要翻页
                if len(articles) >= MAX_ITEMS:
                    break
                
                # 翻页逻辑
                try:
                    # 查找 "»" 按钮或下一页链接
                    # 根据 HTML: <a href="...?startRow=20...">»</a>
                    next_btn = page.locator('ul.pagination a:has-text("»")').first
                    
                    if next_btn.count() and next_btn.is_visible():
                        print("正在翻页...")
                        # 获取当前第一条新闻的标题，用于判断翻页是否成功
                        first_item_title = page.locator("ul.item-list > li.item-list__item a.item-list__title").first.inner_text()
                        
                        next_btn.click()
                        
                        # 等待页面刷新：等待第一条新闻标题变化，或者等待 URL 变化
                        page.wait_for_function(
                            f"document.querySelector('ul.item-list > li.item-list__item a.item-list__title').innerText !== '{first_item_title}'",
                            timeout=15000
                        )
                        time.sleep(2) # 额外等待渲染
                    else:
                        print("没有下一页了")
                        break
                except Exception as e:
                    print(f"翻页失败: {e}")
                    break
                    
        except Exception as e:
            print(f"爬虫运行出错: {e}")
        finally:
            browser.close()
            
    return articles

def main():
    print("开始爬取 Central Bank of Belize 新闻...")
    start_time = time.time()
    
    articles = crawl_news()
    
    # 生成文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"centralbank_belize_news_{timestamp}.json"
    output_path = os.path.join(OUTPUT_DIR, filename)
    
    save_results(articles, output_path)
    
    end_time = time.time()
    print(f"爬取结束，耗时 {end_time - start_time:.2f} 秒")

if __name__ == "__main__":
    main()