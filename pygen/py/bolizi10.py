import os
import json
import time
import re
from datetime import datetime
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==========================================
# 配置区域
# ==========================================
MAX_ITEMS = 5  # 限制爬取数量（用户目标：前5条）
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
BASE_URL = "https://www.centralbank.org.bz"
START_URL = "https://www.centralbank.org.bz/publications-search"

# 详情页正文候选选择器（按优先级排序）
CONTENT_SELECTORS = [
    "div.group.margin-large",
    "div.group",
    "#top",
    "div.interior-layout__main"
]

# ==========================================
# 辅助函数
# ==========================================

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：修复相对路径，移除无用标签
    """
    if not html_content:
        return ""
    
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 修复图片链接
        for img in soup.find_all('img'):
            if img.get('src'):
                img['src'] = urljoin(base_url, img['src'])
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
                a['target'] = '_blank'  # 强制新标签页打开

        # 移除脚本和样式
        for script in soup(["script", "style", "iframe"]):
            script.decompose()

        return str(soup)
    except Exception as e:
        print(f"[WARN] HTML cleaning failed: {e}")
        return html_content

def normalize_date(date_str):
    """
    将日期字符串标准化为 YYYY-MM-DD
    示例输入: "26 February 2026"
    """
    if not date_str:
        return ""
    try:
        # 尝试解析 "26 February 2026" 格式
        dt = datetime.strptime(date_str.strip(), "%d %B %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        try:
            # 尝试其他常见格式
            dt = datetime.strptime(date_str.strip(), "%B %d, %Y")
            return dt.strftime("%Y-%m-%d")
        except:
            return date_str

def save_results(articles, output_path):
    """保存结果为JSON"""
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[OK] Saved {len(articles)} items to {output_path}")

# ==========================================
# 爬虫主逻辑
# ==========================================

def crawl_news():
    # 确保输出目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
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
            locale="en-US"
        )
        
        page = context.new_page()
        
        try:
            print(f"[INFO] Navigating to {START_URL}")
            page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
            
            # 等待列表加载
            # 注意：页面有两个 .item-list，我们需要主内容区域的那个
            list_selector = ".interior-layout-main .item-list > li.item-list__item"
            try:
                page.wait_for_selector(list_selector, timeout=15000)
            except PlaywrightTimeoutError:
                print("[WARN] List items not found or timeout.")
                return []

            while len(articles) < MAX_ITEMS:
                # 获取当前页的所有条目
                items = page.locator(list_selector).all()
                print(f"[INFO] Found {len(items)} items on current page.")
                
                if not items:
                    break
                
                for item in items:
                    if len(articles) >= MAX_ITEMS:
                        break
                        
                    try:
                        # 提取列表页信息
                        title_el = item.locator("a.item-list__title").first
                        date_el = item.locator("p.item-list__date").first
                        summary_el = item.locator("div.item-list__description").first
                        
                        title = title_el.inner_text().strip() if title_el.count() else "No Title"
                        link = title_el.get_attribute("href")
                        if link:
                            link = urljoin(BASE_URL, link)
                        
                        date_text = date_el.inner_text().strip() if date_el.count() else ""
                        date = normalize_date(date_text)
                        
                        summary = summary_el.inner_text().strip() if summary_el.count() else ""
                        
                        print(f"[INFO] Processing: {title[:30]}... ({date})")
                        
                        article_data = {
                            "id": str(len(articles) + 1),
                            "title": title,
                            "name": title,  # 兼容字段
                            "date": date,
                            "source": "Central Bank of Belize",
                            "author": "",
                            "sourceUrl": link,
                            "summary": summary,
                            "content": ""
                        }
                        
                        # 处理详情页内容
                        if link:
                            # 检查是否为文件链接
                            lower_link = link.lower()
                            is_file = any(lower_link.endswith(ext) for ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx'])
                            
                            if is_file:
                                article_data["content"] = f'<a href="{link}" target="_blank">{link}</a>'
                                article_data["fileType"] = link.split('.')[-1]
                                article_data["downloadUrl"] = link
                            else:
                                # 尝试打开详情页提取正文
                                detail_page = context.new_page()
                                try:
                                    # 捕获下载异常（有些链接虽然没有后缀，但实际上是文件下载）
                                    try:
                                        detail_page.goto(link, wait_until="domcontentloaded", timeout=30000)
                                    except Exception as e:
                                        if "ERR_ABORTED" in str(e) or "Download is starting" in str(e):
                                            print(f"[INFO] Detected file download: {link}")
                                            article_data["content"] = f'<a href="{link}" target="_blank">{link}</a>'
                                            detail_page.close()
                                            articles.append(article_data)
                                            continue
                                        else:
                                            raise e

                                    content_html = ""
                                    # 依次尝试候选选择器
                                    for sel in CONTENT_SELECTORS:
                                        try:
                                            # 短暂等待元素出现
                                            detail_page.wait_for_selector(sel, timeout=5000)
                                            el = detail_page.locator(sel).first
                                            if el.count():
                                                html = el.inner_html()
                                                if len(html.strip()) > 50:
                                                    content_html = html
                                                    break
                                        except:
                                            continue
                                    
                                    if content_html:
                                        article_data["content"] = clean_html_content(content_html, link)
                                    else:
                                        # 回退
                                        article_data["content"] = f'<a href="{link}" target="_blank">{link}</a>'
                                        
                                except Exception as e:
                                    print(f"[WARN] Failed to extract content from {link}: {e}")
                                    article_data["content"] = f'<a href="{link}" target="_blank">{link}</a>'
                                finally:
                                    detail_page.close()
                        
                        articles.append(article_data)
                        
                    except Exception as e:
                        print(f"[ERROR] Error processing item: {e}")
                        continue
                
                # 翻页逻辑 (虽然只取5条可能用不到，但为了完整性保留)
                if len(articles) < MAX_ITEMS:
                    # 寻找下一页按钮
                    # 假设分页结构: <ul class="pagination"> ... <li><a href="...">»</a></li> </ul>
                    # 或者检查是否有下一页的链接
                    next_page_btn = page.locator("ul.pagination li a:has-text('»')").first
                    if next_page_btn.count() and next_page_btn.is_visible():
                        print("[INFO] Clicking next page...")
                        next_page_btn.click()
                        # 等待新内容加载（简单判断：等待URL变化或列表元素刷新）
                        page.wait_for_timeout(3000) 
                        page.wait_for_selector(list_selector, timeout=15000)
                    else:
                        print("[INFO] No more pages.")
                        break
                else:
                    break

        except Exception as e:
            print(f"[ERROR] Main crawler loop error: {e}")
        finally:
            browser.close()
            
    return articles

def main():
    print("[INFO] Starting crawler...")
    start_time = time.time()
    
    data = crawl_news()
    
    # 生成文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"centralbank_belize_news_{timestamp}.json"
    output_path = os.path.join(OUTPUT_DIR, filename)
    
    save_results(data, output_path)
    
    end_time = time.time()
    print(f"[INFO] Crawl finished in {end_time - start_time:.2f} seconds.")

if __name__ == "__main__":
    main()