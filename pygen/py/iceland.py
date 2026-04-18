import os
import json
import time
import re
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ================= 配置区域 =================
# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
# 目标URL (新闻列表页)
TARGET_URL = "https://cb.is/news-and-publications/news/"
# 爬取时间范围
START_DATE = "2026-01-01"
END_DATE = "2026-02-12"

# 月份映射表 (包含英文和可能出现的冰岛语)
MONTH_MAP = {
    # English
    "january": "01", "february": "02", "march": "03", "april": "04", "may": "05", "june": "06",
    "july": "07", "august": "08", "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "jun": "06", "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    # Icelandic
    "janúar": "01", "febrúar": "02", "mars": "03", "apríl": "04", "maí": "05", "júní": "06",
    "júlí": "07", "ágúst": "08", "september": "09", "október": "10", "nóvember": "11", "desember": "12"
}

def parse_date(date_str):
    """
    解析日期字符串，支持格式如 '4 February 2026'
    """
    if not date_str:
        return None
    try:
        # 移除多余空格，转小写
        clean_str = date_str.strip().lower()
        # 移除可能的逗号
        clean_str = clean_str.replace(',', '')
        parts = clean_str.split()
        
        if len(parts) >= 3:
            # 假设格式为: Day Month Year (e.g., 4 February 2026)
            day = parts[0].zfill(2)
            month_str = parts[1]
            year = parts[2]
            
            # 尝试从映射表中获取月份
            month = MONTH_MAP.get(month_str)
            
            if month and day.isdigit() and year.isdigit():
                return f"{year}-{month}-{day}"
                
        return None
    except Exception as e:
        print(f"日期解析警告: '{date_str}' - {e}")
        return None

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：
    1. 将相对路径图片/链接转换为绝对路径
    2. 移除无用标签
    """
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除脚本和样式
        for tag in soup(['script', 'style', 'iframe', 'noscript']):
            tag.decompose()

        # 修复图片链接 (src)
        for img in soup.find_all('img'):
            if img.get('src'):
                # 如果是相对路径，转为绝对路径
                if not img['src'].startswith(('http:', 'https:', 'data:')):
                    img['src'] = urljoin(base_url, img['src'])
                
        # 修复超链接 (href)
        for a in soup.find_all('a'):
            if a.get('href'):
                if not a['href'].startswith(('http:', 'https:', 'mailto:', 'tel:', 'javascript:')):
                    a['href'] = urljoin(base_url, a['href'])
                
        return str(soup)
    except Exception as e:
        print(f"HTML清洗出错: {e}")
        return html_content

def extract_article_content(page, url):
    """
    进入详情页提取正文内容
    """
    print(f"正在抓取详情: {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # 等待可能的动态内容加载
        page.wait_for_timeout(1500)
        
        content_html = ""
        
        # 尝试定位正文区域，优先级从高到低
        selectors = [
            ".article-body",       # 常见文章体
            ".news-content",       # 常见新闻体
            ".page-content",       # 通用内容
            "main .veva-grid",     # 网站特定结构 (根据首页推断)
            "article",             # 语义化标签
            "#main"                # 主区域
        ]
        
        for selector in selectors:
            if page.locator(selector).count() > 0:
                # 获取HTML并检查长度，避免获取到空容器
                html = page.locator(selector).first.inner_html()
                if len(html.strip()) > 100:
                    content_html = html
                    break
        
        # 如果上述选择器都失效，尝试提取所有段落
        if not content_html:
            print("  -> 未找到明确内容容器，尝试提取所有段落...")
            paragraphs = page.locator("main p").all()
            if paragraphs:
                content_html = "".join([p.inner_html() for p in paragraphs])
        
        if not content_html:
            print("  -> [警告] 无法提取正文内容")
            
        return clean_html_content(content_html, url)
        
    except Exception as e:
        print(f"  -> 详情页抓取失败: {e}")
        return ""

def crawl_news():
    """
    主爬虫逻辑
    """
    # 确保输出目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    collected_items = []
    
    with sync_playwright() as p:
        # 启动浏览器 (配置反爬参数)
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        # === 第一阶段：遍历列表页收集元数据 ===
        page = context.new_page()
        print(f"开始访问列表页: {TARGET_URL}")
        
        try:
            page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            print(f"页面加载超时 (非致命): {e}")

        page_num = 1
        has_next_page = True
        stop_crawling = False
        
        while has_next_page and not stop_crawling:
            print(f"\n--- 正在分析第 {page_num} 页 ---")
            
            # 等待列表元素加载
            try:
                page.wait_for_selector(".article-card", timeout=10000)
            except:
                print("未找到新闻列表项，可能已到达末尾或页面结构改变。")
                break
            
            # 获取所有新闻卡片
            cards = page.locator(".article-card").all()
            print(f"当前页发现 {len(cards)} 条记录")
            
            if len(cards) == 0:
                break
                
            for card in cards:
                try:
                    # 提取标题和链接
                    title_el = card.locator(".article-card-title a").first
                    if not title_el.count():
                        continue
                        
                    title = title_el.inner_text().strip()
                    link = title_el.get_attribute("href")
                    full_link = urljoin(TARGET_URL, link)
                    
                    # 提取日期
                    date_el = card.locator(".article-card-date").first
                    date_text = date_el.inner_text().strip() if date_el.count() else ""
                    pub_date = parse_date(date_text)
                    
                    # 调试输出
                    # print(f"  扫描: {pub_date} | {title}")
                    
                    if not pub_date:
                        continue
                        
                    # === 日期过滤逻辑 ===
                    if pub_date > END_DATE:
                        # print("    -> 跳过: 日期太新")
                        continue
                        
                    if pub_date < START_DATE:
                        print(f"  -> 发现旧新闻 ({pub_date})，停止翻页。")
                        stop_crawling = True
                        break
                    
                    # 符合条件，加入待抓取列表
                    print(f"  [+] 添加任务: {pub_date} | {title}")
                    collected_items.append({
                        "title": title,
                        "date": pub_date,
                        "sourceUrl": full_link,
                        "source": "Central Bank of Iceland",
                        "author": "Central Bank of Iceland",
                        "summary": "" # 列表页无摘要，留空
                    })
                    
                except Exception as e:
                    print(f"解析卡片出错: {e}")
            
            if stop_crawling:
                break
                
            # === 翻页逻辑 ===
            # 尝试查找下一页按钮
            # 常见的 Next 按钮选择器
            next_selectors = [
                "a[aria-label='Next']",
                "a[aria-label='Next page']",
                ".pagination .next",
                "text='Next'",
                "text='>'"
            ]
            
            next_btn = None
            for sel in next_selectors:
                if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                    next_btn = page.locator(sel).first
                    break
            
            if next_btn:
                print("点击下一页...")
                try:
                    with page.expect_navigation(timeout=15000):
                        next_btn.click()
                    page_num += 1
                    time.sleep(2) # 缓冲
                except Exception as e:
                    print(f"翻页点击失败: {e}")
                    has_next_page = False
            else:
                print("未找到下一页按钮，列表遍历结束。")
                has_next_page = False
        
        page.close()
        
        # === 第二阶段：抓取详情页内容 ===
        print(f"\n开始抓取 {len(collected_items)} 条新闻的详情内容...")
        
        # 使用新页面抓取详情，避免列表页状态干扰
        detail_page = context.new_page()
        
        for i, item in enumerate(collected_items):
            print(f"[{i+1}/{len(collected_items)}] 处理中...")
            item['content'] = extract_article_content(detail_page, item['sourceUrl'])
            # 简单的防封禁延时
            time.sleep(1)
            
        detail_page.close()
        browser.close()
        
    return collected_items

def save_results(articles):
    """
    保存结果为JSON文件
    """
    if not articles:
        print("没有抓取到符合条件的数据。")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 文件名包含时间戳
    filename = f"iceland_central_bank_news_{timestamp}.json"
    output_path = os.path.join(OUTPUT_DIR, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dateRange": {
            "start": START_DATE,
            "end": END_DATE
        },
        "articles": articles
    }
    
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n成功保存 {len(articles)} 条数据到: {output_path}")
    except Exception as e:
        print(f"保存文件失败: {e}")

def main():
    print(f"启动爬虫 - 目标: 冰岛央行新闻")
    print(f"时间范围: {START_DATE} 至 {END_DATE}")
    print("-" * 50)
    
    try:
        data = crawl_news()
        save_results(data)
    except KeyboardInterrupt:
        print("\n用户中断爬虫。")
    except Exception as e:
        print(f"\n程序运行出错: {e}")

if __name__ == "__main__":
    main()