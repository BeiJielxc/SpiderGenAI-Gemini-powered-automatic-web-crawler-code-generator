import requests
from bs4 import BeautifulSoup
import json
import time
import os
from datetime import datetime
from urllib.parse import urljoin, urlparse
import re
import random

# ================= 配置区域 =================
# 目标URL
BASE_URL = "https://www.cityam.com/news/"
# 爬取时间范围
START_DATE = "2026-01-01"
END_DATE = "2026-02-12"
# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.cityam.com/"
}
# 最大翻页数（防止死循环）
MAX_PAGES = 50
# 连续遇到旧新闻的停止阈值
MAX_OLD_NEWS_COUNT = 10

def setup_output_dir():
    """创建输出目录"""
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        print(f"创建目录: {OUTPUT_DIR}")

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：
    1. 将相对路径转换为绝对路径
    2. 移除无用的标签（如script, style）
    """
    if not html_content:
        return ""
    
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除无用标签
        for tag in soup(['script', 'style', 'iframe', 'noscript']):
            tag.decompose()
            
        # 修复图片链接
        for img in soup.find_all('img'):
            if img.get('src'):
                img['src'] = urljoin(base_url, img['src'])
            if img.get('data-src'): # 处理懒加载
                img['src'] = urljoin(base_url, img['data-src'])
            # 移除srcset属性，防止显示问题
            if img.get('srcset'):
                del img['srcset']
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
                
        return str(soup)
    except Exception as e:
        print(f"内容清洗出错: {e}")
        return html_content

def parse_date(date_str):
    """
    解析日期字符串，支持多种格式
    返回格式: YYYY-MM-DD
    """
    if not date_str:
        return None
        
    # 常见格式尝试
    formats = [
        "%Y-%m-%dT%H:%M:%S%z", # ISO 8601
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%B %d, %Y", # February 12, 2026
        "%d %B %Y",  # 12 February 2026
        "%Y-%m-%d"
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
            
    # 尝试从字符串中提取日期 (YYYY-MM-DD)
    match = re.search(r'(\d{4})-(\d{2})-(\d{2})', date_str)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        
    return None

def get_article_details(url):
    """
    获取文章详情
    """
    try:
        time.sleep(random.uniform(1, 2)) # 礼貌延时
        print(f"正在抓取详情: {url}")
        
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            print(f"详情页请求失败: {response.status_code}")
            return None
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 1. 提取标题
        title = ""
        h1 = soup.find('h1')
        if h1:
            title = h1.get_text(strip=True)
        
        # 2. 提取日期 (优先使用 meta 标签)
        date_str = None
        
        # 尝试 meta article:published_time
        meta_date = soup.find('meta', property='article:published_time')
        if meta_date:
            date_str = parse_date(meta_date.get('content'))
            
        # 尝试 meta date
        if not date_str:
            meta_date = soup.find('meta', attrs={'name': 'date'})
            if meta_date:
                date_str = parse_date(meta_date.get('content'))
        
        # 尝试 time 标签
        if not date_str:
            time_tag = soup.find('time')
            if time_tag:
                if time_tag.get('datetime'):
                    date_str = parse_date(time_tag.get('datetime'))
                else:
                    date_str = parse_date(time_tag.get_text(strip=True))
        
        # 3. 提取作者
        author = "City AM"
        meta_author = soup.find('meta', attrs={'name': 'author'})
        if meta_author:
            author = meta_author.get('content')
        else:
            author_tag = soup.find(class_=re.compile(r'author|byline'))
            if author_tag:
                author = author_tag.get_text(strip=True)
                
        # 4. 提取正文
        content = ""
        # 常见的正文容器类名
        content_selectors = [
            'div.entry-content', 
            'div.post-content', 
            'article', 
            'div.article-body',
            'main#main'
        ]
        
        for selector in content_selectors:
            content_div = soup.select_one(selector)
            if content_div:
                # 移除可能的广告或推荐阅读
                for remove_sel in ['.ad-container', '.related-posts', '.share-buttons']:
                    for tag in content_div.select(remove_sel):
                        tag.decompose()
                content = clean_html_content(str(content_div), url)
                break
        
        # 5. 提取摘要
        summary = ""
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        if meta_desc:
            summary = meta_desc.get('content')
        elif content:
            # 简单的去除HTML标签取前200字
            text_content = BeautifulSoup(content, 'html.parser').get_text(strip=True)
            summary = text_content[:200] + "..." if len(text_content) > 200 else text_content

        return {
            "title": title,
            "date": date_str,
            "author": author,
            "source": "City AM",
            "sourceUrl": url,
            "summary": summary,
            "content": content
        }
        
    except Exception as e:
        print(f"解析详情页出错 {url}: {e}")
        return None

def crawl_news():
    """
    主爬虫逻辑
    """
    all_articles = []
    old_news_counter = 0
    
    for page in range(1, MAX_PAGES + 1):
        # 构造分页URL
        if page == 1:
            url = BASE_URL
        else:
            url = f"{BASE_URL}page/{page}/"
            
        print(f"\n正在爬取列表页 第 {page} 页: {url}")
        
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code != 200:
                print(f"请求列表页失败: {response.status_code}")
                break
                
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 定位文章列表
            # 根据提供的HTML结构，文章在 ul.content-listing__content > li
            article_items = soup.select('ul.content-listing__content > li.content-listing__content-item')
            
            if not article_items:
                print("未找到文章列表，可能已到达最后一页或结构变更")
                break
                
            print(f"本页发现 {len(article_items)} 篇文章")
            
            page_processed_count = 0
            
            for item in article_items:
                # 提取链接
                link_tag = item.select_one('h3.card__title a')
                if not link_tag:
                    continue
                    
                article_url = link_tag.get('href')
                if not article_url:
                    continue
                    
                # 详情页抓取
                article = get_article_details(article_url)
                
                if not article or not article.get('date'):
                    print(f"  [跳过] 无法获取详情或日期: {article_url}")
                    continue
                
                pub_date = article['date']
                
                # 日期过滤逻辑
                if pub_date > END_DATE:
                    print(f"  [跳过] 日期 {pub_date} 超出范围 (晚于 {END_DATE})")
                    continue
                    
                if pub_date < START_DATE:
                    print(f"  [跳过] 日期 {pub_date} 超出范围 (早于 {START_DATE})")
                    old_news_counter += 1
                    if old_news_counter >= MAX_OLD_NEWS_COUNT:
                        print(f"连续 {MAX_OLD_NEWS_COUNT} 篇文章早于开始日期，停止爬取")
                        return all_articles
                    continue
                
                # 日期符合要求
                old_news_counter = 0 # 重置计数器
                print(f"  [成功] 抓取: {article['title']} ({pub_date})")
                
                # 添加ID
                article['id'] = str(len(all_articles) + 1)
                all_articles.append(article)
                page_processed_count += 1
                
            if page_processed_count == 0 and old_news_counter > 0:
                print("本页无符合日期的数据，且已遇到旧数据，可能需要停止")
                
            # 简单的翻页间隔
            time.sleep(2)
            
        except Exception as e:
            print(f"爬取列表页出错: {e}")
            break
            
    return all_articles

def save_results(articles):
    """保存结果为JSON"""
    if not articles:
        print("没有抓取到任何数据")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"CityAM_News_{timestamp}.json"
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
    print("=== City AM 新闻爬虫启动 ===")
    print(f"目标URL: {BASE_URL}")
    print(f"时间范围: {START_DATE} 至 {END_DATE}")
    
    setup_output_dir()
    
    articles = crawl_news()
    
    save_results(articles)
    print("=== 爬取任务结束 ===")

if __name__ == "__main__":
    main()