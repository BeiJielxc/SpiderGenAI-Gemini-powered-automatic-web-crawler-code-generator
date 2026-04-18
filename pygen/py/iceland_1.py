import os
import json
import time
import re
from datetime import datetime
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup

# ==========================================
# 配置区域
# ==========================================
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
TARGET_URL = "https://cb.is/news-and-publications/news/"
MAX_ITEMS = 5  # 任务目标：前5条

# ==========================================
# 辅助函数
# ==========================================

def clean_html_content(html_content, base_url):
    """
    将 HTML 中的相对路径转换为绝对路径，确保本地预览正常
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
        print(f"[ERROR] 内容清洗出错: {e}")
        return html_content

def parse_date(date_str):
    """
    解析日期字符串，目标格式如 '5 December 2025'
    """
    if not date_str:
        return ""
    try:
        # 清理多余空格
        clean_str = date_str.strip()
        # 尝试解析 "5 December 2025"
        dt = datetime.strptime(clean_str, "%d %B %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        try:
            # 备用尝试
            return date_str.strip()
        except:
            return ""

def extract_article_content(page, url):
    """
    进入详情页提取正文内容
    """
    try:
        print(f"[INFO] 正在抓取详情页: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        # 等待内容加载，Blazor 应用可能需要一点时间渲染
        page.wait_for_timeout(2000)

        # 策略：优先寻找语义化的正文容器
        # 根据列表页结构推断，详情页通常在 main 标签或特定的 class 中
        selectors = [
            ".article-body",           # 常见新闻正文类名
            ".news-content",           # 常见新闻正文类名
            ".page-section-content",   # 列表页中出现的结构
            "main#main",               # 页面主体
            ".rs-content"              # 列表页中出现的结构
        ]
        
        content_html = ""
        for selector in selectors:
            if page.locator(selector).count() > 0:
                # 获取 HTML 内容
                html = page.locator(selector).first.inner_html()
                # 简单验证内容长度，避免获取到空容器
                if len(html) > 100:
                    content_html = html
                    break
        
        # 如果上述选择器都失效，尝试获取 body 内容作为兜底
        if not content_html:
            content_html = page.locator("body").inner_html()

        return content_html
    except Exception as e:
        print(f"[ERROR] 详情页抓取失败 {url}: {e}")
        return ""

# ==========================================
# 核心爬虫逻辑
# ==========================================

def crawl_news():
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
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
        )
        
        try:
            page = context.new_page()
            print(f"[INFO] 正在访问列表页: {TARGET_URL}")
            
            # 访问页面，等待网络空闲（Blazor 应用通常需要加载 WASM）
            page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
            
            # 显式等待新闻卡片元素出现
            # 根据 HTML 分析，新闻卡片 class 为 "article-card"
            try:
                page.wait_for_selector(".article-card", timeout=15000)
            except PlaywrightTimeoutError:
                print("[WARN] 等待新闻卡片超时，尝试滚动页面...")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2000)
            
            # 获取所有新闻卡片
            cards = page.locator(".article-card").all()
            total_found = len(cards)
            print(f"[INFO] 页面上找到 {total_found} 条新闻，准备抓取前 {MAX_ITEMS} 条")
            
            for i, card in enumerate(cards):
                if i >= MAX_ITEMS:
                    break
                
                try:
                    # 提取列表页基本信息
                    # 标题和链接在 .article-card-title a 中
                    title_el = card.locator(".article-card-title a").first
                    # 日期在 .article-card-date 中
                    date_el = card.locator(".article-card-date").first
                    
                    if title_el.count() == 0:
                        continue

                    title = title_el.inner_text().strip()
                    link = title_el.get_attribute("href")
                    
                    # 提取日期文本
                    date_text = date_el.inner_text().strip() if date_el.count() > 0 else ""
                    formatted_date = parse_date(date_text)
                    
                    if not link:
                        continue
                        
                    full_link = urljoin(TARGET_URL, link)
                    
                    print(f"[INFO] 处理第 {i+1} 条: {title} ({formatted_date})")
                    
                    # 使用新页面抓取详情，避免干扰列表页状态
                    detail_page = context.new_page()
                    content_html = extract_article_content(detail_page, full_link)
                    detail_page.close()
                    
                    # 清洗 HTML 内容
                    cleaned_content = clean_html_content(content_html, full_link)
                    
                    # 构建数据对象
                    article = {
                        "id": str(i + 1),
                        "title": title,
                        "name": title,  # 兼容字段
                        "date": formatted_date,
                        "source": "Central Bank of Iceland",
                        "sourceUrl": full_link,
                        "summary": "",  # 列表页无明显摘要，留空
                        "content": cleaned_content
                    }
                    articles.append(article)
                    
                    # 礼貌性延时
                    time.sleep(1)
                    
                except Exception as e:
                    print(f"[ERROR] 处理单条新闻出错: {e}")
                    continue
                    
        except Exception as e:
            print(f"[ERROR] 爬虫运行出错: {e}")
        finally:
            browser.close()
            
    return articles

def save_results(articles, output_dir):
    """保存结果为 JSON 文件"""
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 文件名包含网站标识
    filename = f"cb_is_news_{timestamp}.json"
    filepath = os.path.join(output_dir, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        
    print(f"[SUCCESS] 结果已保存到: {filepath}")

def main():
    print("[START] 开始爬取任务...")
    
    # 执行爬取
    articles = crawl_news()
    
    # 保存结果
    if articles:
        save_results(articles, OUTPUT_DIR)
    else:
        print("[WARN] 未抓取到任何数据，请检查网络或页面结构是否变更")
    
    print("[END] 任务结束")

if __name__ == "__main__":
    main()