import os
import json
import time
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# ==========================================
# 配置参数
# ==========================================
MAX_ITEMS = 5  # 任务目标：爬取前5条
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
BASE_URL = "https://www.centralbank.org.bz"
START_URL = "https://www.centralbank.org.bz/publications-search"

def clean_html_content(html_content, base_url):
    """
    将 HTML 中的相对路径转换为绝对路径
    使用 BeautifulSoup 进行清洗
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

def extract_date(text):
    """从文本中提取日期并格式化为 YYYY-MM-DD"""
    if not text:
        return ""
    text = text.strip()
    # 常见格式: 23 February 2026
    try:
        dt = datetime.strptime(text, "%d %B %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    
    # 常见格式: February 23, 2026
    try:
        dt = datetime.strptime(text, "%B %d, %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
        
    return text

def save_results(articles, output_path):
    """保存结果为 JSON"""
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[SUCCESS] Saved {len(articles)} items to {output_path}")

def run_crawler():
    # 确保输出目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    with sync_playwright() as p:
        # 启动浏览器，配置反爬参数
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
        )
        
        page = context.new_page()
        articles = []
        current_page_num = 1
        
        print(f"[INFO] Starting crawl on {START_URL}")
        
        # 翻页循环
        while len(articles) < MAX_ITEMS:
            # 构造分页 URL
            # 第一页 startRow=0, 第二页 startRow=20 (rowsPerPage=20)
            start_row = (current_page_num - 1) * 20
            url = f"{START_URL}?startRow={start_row}&rowsPerPage=20"
            
            print(f"[INFO] Processing page {current_page_num}: {url}")
            
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # 等待列表元素加载
                page.wait_for_selector(".sf-search-result", timeout=10000)
            except Exception as e:
                print(f"[ERROR] Failed to load page {current_page_num}: {e}")
                break
            
            # 获取当前页的所有列表项
            items = page.query_selector_all(".sf-search-result")
            if not items:
                print("[INFO] No items found on this page. Stopping.")
                break
                
            print(f"[INFO] Found {len(items)} items on page {current_page_num}")
            
            # 遍历列表项
            for item in items:
                if len(articles) >= MAX_ITEMS:
                    break
                
                try:
                    # 提取列表页信息
                    title_el = item.query_selector("h3 > a")
                    date_el = item.query_selector(".sf-search-result-date")
                    desc_el = item.query_selector(".sf-search-result-summary")
                    
                    if not title_el:
                        continue
                        
                    title = title_el.inner_text().strip()
                    link = title_el.get_attribute("href")
                    if link:
                        link = urljoin(BASE_URL, link)
                    
                    date_str = date_el.inner_text().strip() if date_el else ""
                    formatted_date = extract_date(date_str)
                    
                    summary = desc_el.inner_text().strip() if desc_el else ""
                    
                    # 初始化数据对象
                    article_data = {
                        "id": str(len(articles) + 1),
                        "title": title,
                        "name": title,  # 兼容字段
                        "date": formatted_date,
                        "source": "Central Bank of Belize",
                        "author": "",
                        "sourceUrl": link,
                        "summary": summary,
                        "content": "",
                        "fileType": "html",
                        "downloadUrl": ""
                    }
                    
                    # 检查链接类型 (HTML 详情页 vs 文件下载)
                    file_exts = ('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip')
                    is_file = False
                    if link:
                        path = urlparse(link).path.lower()
                        if path.endswith(file_exts):
                            is_file = True
                            article_data["fileType"] = os.path.splitext(path)[1][1:]
                            article_data["downloadUrl"] = link
                    
                    if is_file:
                        print(f"[INFO] File detected: {title}")
                        article_data["content"] = f'<a href="{link}" target="_blank">Download File: {title}</a>'
                    elif link:
                        # 进入详情页抓取正文
                        print(f"[INFO] Crawling detail: {title}")
                        detail_page = context.new_page()
                        try:
                            detail_page.goto(link, wait_until="domcontentloaded", timeout=30000)
                            
                            # 尝试提取正文
                            # 优先使用 Agent Strategy 推荐的选择器
                            content_sel = "div.group.margin-large"
                            if not detail_page.query_selector(content_sel):
                                # 备选选择器
                                content_sel = "#top .interior-layout__container"
                            
                            if detail_page.query_selector(content_sel):
                                content_html = detail_page.inner_html(content_sel)
                            else:
                                # 兜底：获取 body 内容
                                content_html = detail_page.inner_html("body")
                            
                            # 如果列表页没有日期，尝试从详情页提取
                            if not formatted_date:
                                detail_date_el = detail_page.query_selector(".item-list__date.detail-page")
                                if detail_date_el:
                                    formatted_date = extract_date(detail_date_el.inner_text())
                                    article_data["date"] = formatted_date
                            
                            # 清洗 HTML 内容
                            article_data["content"] = clean_html_content(content_html, link)
                            
                        except Exception as e:
                            print(f"[WARN] Detail page error for {link}: {e}")
                            article_data["content"] = f'<a href="{link}" target="_blank">Read Original Article</a>'
                        finally:
                            detail_page.close()
                    
                    articles.append(article_data)
                    time.sleep(1)  # 请求间隔
                    
                except Exception as e:
                    print(f"[ERROR] Error processing item: {e}")
                    continue
            
            # 检查是否还有下一页
            # 如果当前页获取的条目数少于 rowsPerPage (20)，说明是最后一页
            if len(items) < 20:
                print("[INFO] Reached last page.")
                break
                
            current_page_num += 1
            time.sleep(2)  # 翻页间隔

        browser.close()
        
        # 生成文件名并保存
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"centralbank_belize_news_{timestamp}.json"
        output_path = os.path.join(OUTPUT_DIR, filename)
        
        save_results(articles, output_path)

if __name__ == "__main__":
    run_crawler()