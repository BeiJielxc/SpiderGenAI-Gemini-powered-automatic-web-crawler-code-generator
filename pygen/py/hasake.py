import os
import json
import time
import random
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ================= 配置区域 =================
# 目标URL
START_URL = "https://kz.kursiv.media/en/category/news/"

# 爬取时间范围
START_DATE = datetime(2026, 1, 1)
END_DATE = datetime(2026, 2, 12, 23, 59, 59)

# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# 浏览器配置
VIEWPORT = {'width': 1920, 'height': 1080}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ================= 工具函数 =================

def ensure_dir(path):
    """确保目录存在"""
    if not os.path.exists(path):
        os.makedirs(path)

def parse_news_date(date_str):
    """
    解析新闻日期
    格式示例: "February 12, 2026 14:30"
    """
    if not date_str:
        return None
    
    clean_str = date_str.strip()
    try:
        # 尝试匹配格式: February 12, 2026 14:30
        return datetime.strptime(clean_str, "%B %d, %Y %H:%M")
    except ValueError:
        try:
            # 备用格式尝试
            return datetime.strptime(clean_str, "%B %d, %Y")
        except ValueError:
            return None

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：
    1. 移除无用标签
    2. 将相对路径转换为绝对路径
    """
    if not html_content:
        return ""
    
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除脚本和样式
        for script in soup(["script", "style", "iframe", "noscript"]):
            script.decompose()
            
        # 移除广告和推荐块 (根据常见类名)
        for div in soup.select('.banner, .share-bar, .single-read-more, .single-tags, .post-info'):
            div.decompose()

        # 修复图片链接
        for img in soup.find_all('img'):
            src = img.get('src')
            if src:
                # 处理懒加载
                if 'data-src' in img.attrs:
                    src = img['data-src']
                img['src'] = urljoin(base_url, src)
                # 移除 srcset 避免干扰
                if 'srcset' in img.attrs:
                    del img['srcset']
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
                
        return str(soup)
    except Exception as e:
        print(f"[Warn] 内容清洗出错: {e}")
        return html_content

def save_results(articles, output_dir):
    """保存结果为JSON"""
    ensure_dir(output_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"kursiv_news_{timestamp}.json"
    filepath = os.path.join(output_dir, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dateRange": {
            "start": START_DATE.strftime("%Y-%m-%d"),
            "end": END_DATE.strftime("%Y-%m-%d")
        },
        "articles": articles
    }
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"\n[Success] 已保存 {len(articles)} 条新闻到: {filepath}")

# ================= 核心爬虫逻辑 =================

class KursivNewsCrawler:
    def __init__(self):
        self.articles_to_scrape = [] # 存储待抓取的文章元数据
        self.scraped_data = []       # 存储最终抓取的数据
        self.stop_crawling = False   # 停止标志

    def run(self):
        print(f"=== 开始爬取任务: {START_URL} ===")
        print(f"=== 时间范围: {START_DATE.date()} 至 {END_DATE.date()} ===")
        
        with sync_playwright() as p:
            # 启动浏览器 (配置反爬参数)
            browser = p.chromium.launch(
                headless=True, # 设置为 False 可观察浏览器行为
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = browser.new_context(
                viewport=VIEWPORT,
                user_agent=USER_AGENT,
                locale="en-US"
            )
            page = context.new_page()
            
            # 1. 遍历列表页获取符合日期的文章链接
            self.crawl_listing_pages(page)
            
            print(f"\n[Info] 列表扫描完成，共找到 {len(self.articles_to_scrape)} 篇符合日期的文章。")
            print("[Info] 开始抓取详情页内容...\n")
            
            # 2. 遍历详情页抓取正文
            self.crawl_detail_pages(page)
            
            browser.close()
            
        # 3. 保存结果
        if self.scraped_data:
            save_results(self.scraped_data, OUTPUT_DIR)
        else:
            print("[Warn] 未抓取到任何数据。")

    def crawl_listing_pages(self, page):
        """遍历列表页，提取符合条件的文章链接"""
        current_url = START_URL
        page_num = 1
        
        while not self.stop_crawling and current_url:
            print(f"[List] 正在扫描第 {page_num} 页: {current_url}")
            try:
                page.goto(current_url, timeout=60000, wait_until="domcontentloaded")
                # 等待文章列表加载
                page.wait_for_selector("article.news-post", timeout=10000)
                
                # 获取页面内容进行解析
                content = page.content()
                soup = BeautifulSoup(content, 'html.parser')
                
                articles = soup.select("article.news-post")
                if not articles:
                    print("[Warn] 当前页面未找到文章列表，停止翻页。")
                    break
                
                page_has_valid_date = False
                
                for article in articles:
                    # 提取日期
                    time_tag = article.select_one("time.post-date")
                    if not time_tag:
                        continue
                        
                    date_str = time_tag.get_text(strip=True)
                    pub_date = parse_news_date(date_str)
                    
                    if not pub_date:
                        continue
                    
                    # 日期过滤逻辑
                    if pub_date > END_DATE:
                        continue # 日期太新，跳过
                    
                    if pub_date < START_DATE:
                        print(f"[Info] 发现早于开始日期的文章 ({pub_date}), 停止翻页。")
                        self.stop_crawling = True
                        break # 日期太旧，停止整个爬虫
                    
                    # 日期符合要求
                    page_has_valid_date = True
                    
                    # 提取基础信息
                    title_tag = article.select_one("h2.single-header__title a")
                    if not title_tag:
                        continue
                        
                    link = title_tag.get('href')
                    title = title_tag.get_text(strip=True)
                    
                    # 提取作者
                    author_tag = article.select_one(".author-card__name a")
                    author = author_tag.get_text(strip=True) if author_tag else "Unknown"
                    
                    # 提取摘要
                    summary = ""
                    summary_tag = article.select_one(".single-body p") # 列表页通常显示第一段
                    if summary_tag:
                        summary = summary_tag.get_text(strip=True)
                    
                    self.articles_to_scrape.append({
                        "title": title,
                        "url": link,
                        "date": pub_date.strftime("%Y-%m-%d"),
                        "author": author,
                        "summary": summary
                    })
                
                # 翻页逻辑
                if not self.stop_crawling:
                    # 查找下一页链接
                    next_page = soup.select_one("a.next.page-numbers")
                    if next_page and next_page.get('href'):
                        current_url = next_page['href']
                        page_num += 1
                        # 随机延时
                        time.sleep(random.uniform(1, 3))
                    else:
                        print("[Info] 没有下一页了。")
                        break
                        
            except Exception as e:
                print(f"[Error] 处理列表页出错: {e}")
                break

    def crawl_detail_pages(self, page):
        """进入详情页抓取正文"""
        total = len(self.articles_to_scrape)
        for i, item in enumerate(self.articles_to_scrape, 1):
            url = item['url']
            print(f"[{i}/{total}] 正在抓取: {item['title']}")
            
            try:
                # 访问详情页
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                
                # 获取HTML
                content_html = page.content()
                soup = BeautifulSoup(content_html, 'html.parser')
                
                # 提取正文
                # 根据页面结构，正文通常在 .single-body 中
                # 注意：列表页也有 .single-body，但详情页内容更全
                article_body = soup.select_one("div.single-body")
                
                if article_body:
                    # 清洗内容
                    cleaned_content = clean_html_content(str(article_body), url)
                    
                    # 如果列表页没提取到摘要，尝试从正文提取
                    if not item['summary']:
                        first_p = article_body.find('p')
                        if first_p:
                            item['summary'] = first_p.get_text(strip=True)[:200] + "..."
                else:
                    print(f"[Warn] 未找到正文内容: {url}")
                    cleaned_content = ""

                # 组装最终数据
                news_item = {
                    "id": str(i),
                    "title": item['title'],
                    "date": item['date'],
                    "author": item['author'],
                    "source": "Kursiv Media",
                    "sourceUrl": url,
                    "summary": item['summary'],
                    "content": cleaned_content
                }
                
                self.scraped_data.append(news_item)
                
                # 礼貌性延时
                time.sleep(random.uniform(1.5, 3.5))
                
            except Exception as e:
                print(f"[Error] 抓取详情页失败 {url}: {e}")
                # 即使失败也保存已有信息
                news_item = {
                    "id": str(i),
                    "title": item['title'],
                    "date": item['date'],
                    "author": item['author'],
                    "source": "Kursiv Media",
                    "sourceUrl": url,
                    "summary": item['summary'],
                    "content": "<p>Error: Failed to retrieve content.</p>"
                }
                self.scraped_data.append(news_item)

if __name__ == "__main__":
    crawler = KursivNewsCrawler()
    crawler.run()