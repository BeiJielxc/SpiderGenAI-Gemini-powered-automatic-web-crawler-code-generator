import os
import json
import time
import re
from datetime import datetime
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# ==========================================
# 配置区域
# ==========================================
MAX_ITEMS = 5  # 任务目标：爬取前5条
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
BASE_URL = "https://www.centralbank.org.bz"
START_URL = "https://www.centralbank.org.bz/publications-search"

# ==========================================
# 工具函数
# ==========================================

def clean_html_content(html_content, base_url):
    """
    将 HTML 中的相对路径转换为绝对路径
    修复图片 src 和链接 href
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
                
        return str(soup)
    except Exception as e:
        print(f"[WARN] Content cleaning failed: {e}")
        return html_content

def parse_date(date_str):
    """
    解析日期字符串
    示例输入: "26 February 2026"
    输出: "2026-02-26"
    """
    if not date_str:
        return ""
    try:
        date_str = date_str.strip()
        # 尝试匹配 "26 February 2026" 格式
        dt = datetime.strptime(date_str, "%d %B %Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        # 如果解析失败，返回原始字符串或空
        return date_str

def save_results(articles, output_path):
    """保存爬取结果为JSON"""
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[INFO] Saved {len(articles)} articles to {output_path}")

# ==========================================
# 爬虫主逻辑
# ==========================================

def run_crawler():
    # 确保输出目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    print("[INFO] Starting crawler...")
    
    with sync_playwright() as p:
        # 启动浏览器，配置反爬参数
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        # 创建主页面
        page = context.new_page()
        articles = []
        
        try:
            print(f"[INFO] Navigating to {START_URL}")
            page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
            
            # 循环翻页抓取
            while len(articles) < MAX_ITEMS:
                # 等待列表元素加载
                try:
                    page.wait_for_selector("ul.item-list li.item-list__item", timeout=10000)
                except Exception:
                    print("[WARN] List items not found or timeout.")
                    break
                
                # 获取当前页的所有列表项
                items = page.query_selector_all("ul.item-list li.item-list__item")
                print(f"[INFO] Found {len(items)} items on current page.")
                
                if not items:
                    print("[INFO] No items found, stopping.")
                    break
                
                # 遍历当前页条目
                for item in items:
                    if len(articles) >= MAX_ITEMS:
                        break
                    
                    try:
                        # 1. 提取列表页基础信息
                        title_el = item.query_selector("a.item-list__title")
                        date_el = item.query_selector(".item-list__date")
                        summary_el = item.query_selector(".item-list__description")
                        
                        if not title_el:
                            continue
                            
                        title = title_el.inner_text().strip()
                        link = title_el.get_attribute("href")
                        if link:
                            link = urljoin(BASE_URL, link)
                        
                        date_text = date_el.inner_text().strip() if date_el else ""
                        date_val = parse_date(date_text)
                        
                        summary = summary_el.inner_text().strip() if summary_el else ""
                        
                        article_data = {
                            "id": str(len(articles) + 1),
                            "title": title,
                            "date": date_val,
                            "author": "Central Bank of Belize",
                            "source": "Central Bank of Belize",
                            "sourceUrl": link,
                            "summary": summary,
                            "content": ""
                        }
                        
                        # 2. 处理详情页
                        if link:
                            # 检查是否为文件链接 (PDF/DOC等)
                            lower_link = link.lower()
                            is_file = any(lower_link.endswith(ext) for ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx'])
                            
                            if is_file:
                                print(f"[INFO] File detected, skipping detail page: {title[:30]}...")
                                article_data["content"] = f'<a href="{link}" target="_blank">Download Document</a>'
                            else:
                                print(f"[INFO] Processing detail page: {title[:30]}...")
                                # 使用新页面打开详情页，保持列表页状态
                                detail_page = context.new_page()
                                try:
                                    detail_page.goto(link, wait_until="domcontentloaded", timeout=30000)
                                    
                                    # 尝试提取正文
                                    # 优先尝试 div.group.margin-large，其次 #top
                                    content_html = ""
                                    selectors = [
                                        "div.group.margin-large",
                                        "#top",
                                        "div[role='main']"
                                    ]
                                    
                                    for sel in selectors:
                                        if detail_page.query_selector(sel):
                                            content_html = detail_page.inner_html(sel)
                                            # 简单的有效性检查
                                            if len(content_html) > 100:
                                                break
                                    
                                    if not content_html:
                                        print("[WARN] No content found with selectors, using summary.")
                                        content_html = summary
                                    
                                    # 清洗内容（修复图片路径）
                                    article_data["content"] = clean_html_content(content_html, link)
                                    
                                    # 尝试在详情页提取更准确的日期
                                    # 某些页面可能有 .item-list__date.detail-page
                                    detail_date_el = detail_page.query_selector(".item-list__date.detail-page")
                                    if detail_date_el:
                                        d_text = detail_date_el.inner_text().strip()
                                        d_val = parse_date(d_text)
                                        if d_val:
                                            article_data["date"] = d_val
                                            
                                except Exception as e:
                                    print(f"[WARN] Failed to load detail page: {e}")
                                    article_data["content"] = f"<p>Failed to load content. <a href='{link}'>Original Link</a></p>"
                                finally:
                                    detail_page.close()
                                    time.sleep(1) # 礼貌性延迟
                        
                        articles.append(article_data)
                        
                    except Exception as e:
                        print(f"[ERROR] Error processing item: {e}")
                        continue
                
                # 3. 翻页逻辑
                if len(articles) < MAX_ITEMS:
                    # 寻找下一页按钮 "»"
                    next_btn = page.query_selector("ul.pagination li a:has-text('»')")
                    
                    if next_btn:
                        next_url = next_btn.get_attribute("href")
                        if next_url:
                            next_url = urljoin(BASE_URL, next_url)
                            print(f"[INFO] Navigating to next page: {next_url}")
                            page.goto(next_url, wait_until="domcontentloaded", timeout=30000)
                        else:
                            print("[INFO] Next button has no href, stopping.")
                            break
                    else:
                        print("[INFO] No next page button found, stopping.")
                        break
                else:
                    print(f"[INFO] Reached target count ({MAX_ITEMS}), stopping.")
                    break
                    
        except Exception as e:
            print(f"[ERROR] Main crawler loop failed: {e}")
        finally:
            browser.close()
            
        # 保存结果
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"centralbank_belize_news_{timestamp}.json"
        output_path = os.path.join(OUTPUT_DIR, filename)
        save_results(articles, output_path)

if __name__ == "__main__":
    run_crawler()