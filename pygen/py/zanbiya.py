import os
import json
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==========================================
# 配置区域
# ==========================================
TARGET_URL = "https://www.seczambia.org.zm/resources/multimedia/news/"
MAX_ITEMS = 5  # 限制抓取条数
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# 详情页正文提取选择器优先级列表
CONTENT_SELECTORS = [
    "div.elementor-widget-theme-post-content",  # 标准 Elementor 内容容器
    "div.elementor.elementor-116",              # 页面分析推荐
    "div.elementor-location-single",            # 通用单页容器
    "article",                                  # 语义化标签
    "div.elementor-section-wrap",               # 兜底容器
]

# ==========================================
# 工具函数
# ==========================================

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：
    1. 将相对路径图片/链接转换为绝对路径
    2. 移除无用标签（可选）
    """
    if not html_content:
        return ""
    
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 修复图片链接
        for img in soup.find_all('img'):
            src = img.get('src')
            if src:
                img['src'] = urljoin(base_url, src)
                
        # 修复超链接
        for a in soup.find_all('a'):
            href = a.get('href')
            if href:
                a['href'] = urljoin(base_url, href)
                a['target'] = '_blank'  # 强制新标签页打开
                
        return str(soup)
    except Exception as e:
        print(f"[WARN] HTML cleaning failed: {e}")
        return html_content

def extract_date_from_text(text):
    """从文本中提取日期"""
    if not text:
        return ""
    
    # 常见日期格式正则
    patterns = [
        r'(\w+ \d{1,2}, \d{4})',          # October 14, 2025
        r'(\d{4}-\d{2}-\d{2})',           # 2025-10-14
        r'(\d{2}/\d{2}/\d{4})',           # 14/10/2025
        r'(\d{1,2} \w+ \d{4})'            # 14 October 2025
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            date_str = match.group(1)
            # 尝试标准化日期格式
            try:
                # 针对 Month DD, YYYY 格式
                if ',' in date_str:
                    dt = datetime.strptime(date_str, "%B %d, %Y")
                    return dt.strftime("%Y-%m-%d")
            except:
                pass
            return date_str
            
    return ""

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
        print(f"[INFO] Saved {len(articles)} articles to {output_path}")
    except Exception as e:
        print(f"[ERROR] Failed to save results: {e}")

# ==========================================
# 核心爬虫逻辑
# ==========================================

def crawl_news():
    articles = []
    
    with sync_playwright() as p:
        # 启动浏览器（配置反爬参数）
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
        
        try:
            print(f"[INFO] Navigating to {TARGET_URL}")
            page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
            
            # 循环翻页抓取
            while len(articles) < MAX_ITEMS:
                # 等待列表加载
                try:
                    page.wait_for_selector(".e-loop-item", timeout=10000)
                except:
                    print("[WARN] No news items found on this page.")
                    break
                
                # 获取当前页的所有新闻项
                news_items = page.locator(".e-loop-item").all()
                print(f"[INFO] Found {len(news_items)} items on current page")
                
                if not news_items:
                    break
                
                for item in news_items:
                    if len(articles) >= MAX_ITEMS:
                        break
                        
                    try:
                        # 1. 提取基础信息
                        title_el = item.locator(".elementor-heading-title a").first
                        if not title_el.count():
                            title_el = item.locator("h1 a, h2 a, h3 a").first
                        
                        title = title_el.inner_text().strip() if title_el.count() else "No Title"
                        
                        # 链接提取：优先使用 READ MORE 按钮，否则使用标题链接
                        link_el = item.locator("a.elementor-button").first
                        if not link_el.count():
                            link_el = title_el
                        
                        url = link_el.get_attribute("href")
                        if url:
                            url = urljoin(TARGET_URL, url)
                        else:
                            continue # 没有链接无法抓取详情
                            
                        # 摘要提取
                        summary_el = item.locator(".elementor-widget-theme-post-excerpt").first
                        summary = summary_el.inner_text().strip() if summary_el.count() else ""
                        
                        # 日期提取：优先从摘要中提取
                        date = extract_date_from_text(summary)
                        
                        print(f"[INFO] Processing: {title}")
                        
                        # 2. 进入详情页抓取
                        content = ""
                        
                        # 检查是否为文件链接
                        file_exts = ('.pdf', '.doc', '.docx', '.xls', '.xlsx')
                        parsed_url = urlparse(url)
                        if parsed_url.path.lower().endswith(file_exts):
                            content = f'<a href="{url}" target="_blank">Download Document</a>'
                            if not date: date = datetime.now().strftime("%Y-%m-%d") # 文件链接通常无日期，给个默认或留空
                        else:
                            # 打开新页面处理详情，避免破坏列表页状态
                            detail_page = context.new_page()
                            try:
                                detail_page.goto(url, wait_until="domcontentloaded", timeout=30000)
                                
                                # 如果列表页没找到日期，尝试在详情页找
                                if not date:
                                    # 尝试查找日期元素
                                    date_selectors = [
                                        ".elementor-widget-theme-post-info", 
                                        ".elementor-post-info__terms-list",
                                        "time",
                                        ".date"
                                    ]
                                    for ds in date_selectors:
                                        if detail_page.locator(ds).count():
                                            d_text = detail_page.locator(ds).first.inner_text()
                                            date = extract_date_from_text(d_text)
                                            if date: break
                                    
                                    # 如果还没找到，尝试从正文前几行找
                                    if not date:
                                        body_text = detail_page.locator("body").inner_text()[:500]
                                        date = extract_date_from_text(body_text)

                                # 提取正文
                                content_html = ""
                                for sel in CONTENT_SELECTORS:
                                    try:
                                        # 快速检查元素是否存在
                                        if detail_page.locator(sel).count() > 0:
                                            # 获取HTML
                                            html = detail_page.locator(sel).first.inner_html()
                                            if len(html.strip()) > 50:
                                                content_html = html
                                                break
                                    except Exception:
                                        continue
                                
                                if content_html:
                                    content = clean_html_content(content_html, url)
                                else:
                                    content = f'<a href="{url}" target="_blank">Read Full Article</a>'
                                    
                            except Exception as e:
                                print(f"[WARN] Failed to load detail page {url}: {e}")
                                content = f'<a href="{url}" target="_blank">{url}</a>'
                            finally:
                                detail_page.close()
                        
                        # 3. 组装数据
                        article_data = {
                            "id": str(len(articles) + 1),
                            "title": title, # 兼容性保留
                            "name": title,  # 规范字段名
                            "date": date if date else "",
                            "source": "Securities and Exchange Commission (SEC)",
                            "author": "SEC Zambia",
                            "sourceUrl": url,
                            "summary": summary,
                            "content": content
                        }
                        
                        articles.append(article_data)
                        print(f"[INFO] Collected item {len(articles)}: {date} - {title}")
                        
                    except Exception as e:
                        print(f"[ERROR] Error processing item: {e}")
                        continue
                
                # 检查是否需要翻页
                if len(articles) >= MAX_ITEMS:
                    break
                
                # 翻页逻辑
                next_btn = page.locator("a.page-numbers.next, a.next").first
                if next_btn.is_visible() and next_btn.is_enabled():
                    print("[INFO] Clicking Next Page...")
                    next_btn.click()
                    # 等待页面加载完成，可以通过检测当前页码变化或列表刷新
                    time.sleep(3) # 简单等待，配合 wait_for_selector
                else:
                    print("[INFO] No more pages available.")
                    break
                    
        except Exception as e:
            print(f"[ERROR] Main crawler loop error: {e}")
        finally:
            browser.close()
            
    return articles

def main():
    # 确保输出目录存在
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        
    print("[INFO] Starting crawler...")
    start_time = time.time()
    
    articles = crawl_news()
    
    # 生成文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"seczambia_news_{timestamp}.json"
    output_path = os.path.join(OUTPUT_DIR, filename)
    
    save_results(articles, output_path)
    
    end_time = time.time()
    print(f"[INFO] Crawl finished in {end_time - start_time:.2f} seconds.")

if __name__ == "__main__":
    main()