import os
import json
import time
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ================= 配置区域 =================
# 目标URL (根据HTML快照，实际数据在 moscow 子板块，但逻辑通用)
TARGET_URL = "https://www.interfax-russia.ru/moscow/news"

# 爬取时间范围 (用户指定)
START_DATE = "2026-02-20"
END_DATE = "2026-02-23"

# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# 俄语月份映射表
RU_MONTHS = {
    "января": "01", "февраля": "02", "марта": "03", "апреля": "04",
    "мая": "05", "июня": "06", "июля": "07", "августа": "08",
    "сентября": "09", "октября": "10", "ноября": "11", "декабря": "12"
}

# ================= 核心功能函数 =================

def ensure_dir(path):
    """确保目录存在"""
    if not os.path.exists(path):
        os.makedirs(path)

def parse_ru_date(date_text, year):
    """
    解析俄语日期字符串，如 "20 февраля" -> "2026-02-20"
    :param date_text: 包含日期的文本
    :param year: 指定年份
    :return: YYYY-MM-DD 格式字符串
    """
    try:
        parts = date_text.strip().lower().split()
        if len(parts) >= 2:
            day = parts[0].zfill(2)
            month_str = parts[1]
            month = RU_MONTHS.get(month_str)
            if month:
                return f"{year}-{month}-{day}"
    except Exception as e:
        print(f"日期解析错误: {date_text}, {e}")
    return None

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：修复相对路径图片和链接
    """
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除无用标签
        for tag in soup.select('script, style, iframe, .banner, .advertisement'):
            tag.decompose()

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
        print(f"内容清洗出错: {e}")
        return html_content

def extract_article_content(page, url):
    """
    进入详情页提取正文
    """
    try:
        print(f"正在抓取详情: {url}")
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        
        # 尝试定位正文容器
        # 策略：优先找 itemprop="articleBody"，其次找特定class，最后找 main
        content_html = ""
        
        # 尝试选择器列表
        selectors = [
            'div[itemprop="articleBody"]',
            '.article-body',
            '.news-body',
            'main article',
            'main'
        ]
        
        for selector in selectors:
            if page.locator(selector).count() > 0:
                content_html = page.inner_html(selector)
                break
        
        if not content_html:
            print(f"警告: 无法在 {url} 找到正文内容")
            return ""

        return clean_html_content(content_html, url)
        
    except Exception as e:
        print(f"详情页抓取失败 {url}: {e}")
        return ""

def crawl_news():
    """
    主爬虫逻辑
    """
    results = []
    
    # 解析目标年份 (从用户输入的 START_DATE 提取)
    target_year = int(START_DATE.split('-')[0])
    
    with sync_playwright() as p:
        # 启动浏览器
        browser = p.chromium.launch(
            headless=True,  # 设置为 False 可视化调试
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        page = context.new_page()
        
        print(f"开始访问列表页: {TARGET_URL}")
        page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
        
        # 状态变量
        current_date_str = None
        stop_crawling = False
        processed_urls = set()
        
        while not stop_crawling:
            # 获取列表项
            # 根据HTML结构: ul.lenta-all-news > li
            # li 可能是日期标题 (包含 h2)，也可能是新闻项 (包含 .news-datetime)
            list_items = page.locator('ul.lenta-all-news > li').all()
            
            print(f"当前页面加载了 {len(list_items)} 个列表项")
            
            new_items_found = False
            
            for item in list_items:
                try:
                    # 检查是否是日期标题
                    h2 = item.locator('h2')
                    if h2.count() > 0:
                        date_text = h2.inner_text().strip()
                        parsed_date = parse_ru_date(date_text, target_year)
                        if parsed_date:
                            current_date_str = parsed_date
                            # print(f"--- 扫描到日期: {current_date_str} ---")
                            
                            # 如果扫描到的日期早于开始日期，且列表是倒序的，说明后面的都不需要了
                            if current_date_str < START_DATE:
                                print(f"当前日期 {current_date_str} 早于开始日期 {START_DATE}，停止爬取")
                                stop_crawling = True
                                break
                        continue

                    # 如果没有当前日期，跳过（可能是页面顶部的其他元素）
                    if not current_date_str:
                        continue
                        
                    # 检查是否是新闻项
                    time_elem = item.locator('.news-datetime')
                    link_elem = item.locator('a')
                    
                    if time_elem.count() > 0 and link_elem.count() > 0:
                        news_time = time_elem.inner_text().strip()
                        title = link_elem.inner_text().strip()
                        href = link_elem.get_attribute('href')
                        
                        if not href:
                            continue
                            
                        full_url = urljoin(TARGET_URL, href)
                        
                        # 组合完整时间 YYYY-MM-DD HH:MM
                        full_datetime_str = f"{current_date_str} {news_time}"
                        full_date_only = current_date_str # 用于比较日期范围
                        
                        # 唯一性检查
                        if full_url in processed_urls:
                            continue
                        
                        # 日期范围过滤
                        if START_DATE <= full_date_only <= END_DATE:
                            print(f"发现目标新闻: [{full_date_only}] {title}")
                            
                            # 提取详情
                            content = extract_article_content(context.new_page(), full_url)
                            
                            article = {
                                "title": title,
                                "date": full_date_only,
                                "time": news_time,
                                "datetime": full_datetime_str,
                                "source": "Interfax Russia",
                                "sourceUrl": full_url,
                                "summary": title, # 简单使用标题作为摘要
                                "content": content
                            }
                            results.append(article)
                            processed_urls.add(full_url)
                            new_items_found = True
                        
                        elif full_date_only > END_DATE:
                            # 日期还没到范围（太新了），继续扫描
                            continue
                        elif full_date_only < START_DATE:
                            # 日期已过（太旧了），停止
                            stop_crawling = True
                            break
                            
                except Exception as e:
                    print(f"处理列表项时出错: {e}")
                    continue
            
            if stop_crawling:
                break
                
            # 尝试点击“加载更多”
            # 选择器: .btn-show-more
            show_more_btn = page.locator('.btn-show-more')
            if show_more_btn.count() > 0 and show_more_btn.is_visible():
                print("点击 'Показать еще' 加载更多...")
                try:
                    # 获取当前列表长度用于对比
                    prev_count = len(list_items)
                    show_more_btn.click()
                    # 等待列表长度增加
                    page.wait_for_function(f"document.querySelectorAll('ul.lenta-all-news > li').length > {prev_count}", timeout=10000)
                    time.sleep(2) # 额外缓冲
                except Exception as e:
                    print(f"点击加载更多失败或没有更多数据: {e}")
                    break
            else:
                print("未找到 '加载更多' 按钮或已到底部")
                break

        browser.close()
        
    return results

def save_results(articles, output_dir):
    """保存结果为JSON"""
    ensure_dir(output_dir)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"interfax_news_{timestamp}.json"
    output_path = os.path.join(output_dir, filename)
    
    result_data = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dateRange": {
            "start": START_DATE,
            "end": END_DATE
        },
        "articles": articles
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result_data, f, ensure_ascii=False, indent=2)
        
    print(f"\n爬取完成！共保存 {len(articles)} 条新闻")
    print(f"结果文件: {output_path}")

def main():
    print(f"启动爬虫任务...")
    print(f"目标网站: {TARGET_URL}")
    print(f"时间范围: {START_DATE} 至 {END_DATE}")
    
    try:
        articles = crawl_news()
        save_results(articles, OUTPUT_DIR)
    except Exception as e:
        print(f"爬虫运行发生严重错误: {e}")

if __name__ == "__main__":
    main()