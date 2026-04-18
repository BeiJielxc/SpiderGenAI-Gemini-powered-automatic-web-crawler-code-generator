import os
import json
import time
import random
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime

# =================配置区域=================
# 目标URL
LIST_URL = "https://press.vatican.va/content/salastampa/it/bollettino/pubblico/2026/02.html"
BASE_URL = "https://press.vatican.va"

# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
}

# 限制抓取数量
MAX_ITEMS = 10
# =========================================

def ensure_dir(path):
    """确保目录存在"""
    if not os.path.exists(path):
        os.makedirs(path)

def get_request(url, retries=3):
    """发送GET请求，带重试机制"""
    for i in range(retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            response.raise_for_status()
            # 梵蒂冈网站通常是 UTF-8 或 Latin-1，根据内容自动推断或指定
            response.encoding = response.apparent_encoding
            return response.text
        except Exception as e:
            print(f"[Attempt {i+1}/{retries}] Request failed for {url}: {e}")
            time.sleep(2)
    return None

def parse_date(date_str):
    """解析日期字符串 DD.MM.YYYY -> YYYY-MM-DD"""
    try:
        # 移除可能存在的空白字符
        date_str = date_str.strip()
        dt = datetime.strptime(date_str, "%d.%m.%Y")
        return dt.strftime("%Y-%m-%d")
    except Exception as e:
        print(f"日期解析错误 '{date_str}': {e}")
        return datetime.now().strftime("%Y-%m-%d")

def clean_html_content(html_content, base_url):
    """清洗HTML内容：修复相对路径，移除无用标签"""
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 1. 移除已知的非正文元素 (根据列表页结构推断的导航元素)
        for selector in ['.header-nav', '.breadcrumb', '.languagesnav', '.mobile-gone', 'script', 'style', 'iframe']:
            for tag in soup.select(selector):
                tag.decompose()
        
        # 2. 修复图片链接 (相对 -> 绝对)
        for img in soup.find_all('img'):
            if img.get('src'):
                img['src'] = urljoin(base_url, img['src'])
                
        # 3. 修复超链接 (相对 -> 绝对)
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
                
        return str(soup)
    except Exception as e:
        print(f"内容清洗出错: {e}")
        return html_content

def crawl_list_page():
    """爬取列表页，提取前10条新闻的基础信息"""
    print(f"正在抓取列表页: {LIST_URL}")
    html = get_request(LIST_URL)
    if not html:
        print("列表页获取失败")
        return []

    soup = BeautifulSoup(html, 'html.parser')
    items = []
    
    # 定位到包含所有天数的 ul
    # 选择器: .day
    day_list = soup.select('ul.day')
    if not day_list:
        print("未找到新闻列表容器 ul.day")
        return []

    # 遍历每一天 (ul.day > li)
    # 结构:
    # <li>
    #   <a ...>01.02.2026</a>  <-- 日期
    #   <ul>
    #     <li><a ...>News Title</a></li> <-- 新闻项
    #   </ul>
    # </li>
    
    # 注意：页面可能有多个 ul.day 或者嵌套，这里假设主要新闻在第一个或遍历所有
    for ul in day_list:
        days = ul.find_all('li', recursive=False) # 只找直接子节点
        for day_li in days:
            # 提取日期
            date_link = day_li.find('a', recursive=False)
            if not date_link:
                continue
            
            date_text = date_link.get_text(strip=True)
            formatted_date = parse_date(date_text)
            
            # 提取该天下的新闻列表
            news_ul = day_li.find('ul', recursive=False)
            if not news_ul:
                continue
                
            news_items = news_ul.find_all('li', recursive=False)
            for news_li in news_items:
                link_tag = news_li.find('a')
                if not link_tag:
                    continue
                
                url = urljoin(BASE_URL, link_tag.get('href'))
                title = link_tag.get_text(strip=True)
                
                items.append({
                    "title": title,
                    "date": formatted_date,
                    "sourceUrl": url,
                    "source": "Vatican Press",
                    "author": "Vatican Press"
                })
                
                # 如果已经达到最大数量，提前返回（优化）
                # 但为了保证逻辑清晰，我们先收集所有再截取，或者在这里判断
                # 考虑到可能需要跨天收集，这里暂不break，最后统一截取
    
    print(f"共发现 {len(items)} 条新闻，将截取前 {MAX_ITEMS} 条")
    return items[:MAX_ITEMS]

def crawl_detail_page(item):
    """爬取详情页内容"""
    url = item['sourceUrl']
    print(f"正在抓取详情: {item['title'][:30]}...")
    
    html = get_request(url)
    if not html:
        print(f"详情页获取失败: {url}")
        item['content'] = ""
        return item

    soup = BeautifulSoup(html, 'html.parser')
    
    # 提取正文
    # 用户策略: Extract the full content from .rounded
    content_div = soup.select_one('.rounded')
    
    if content_div:
        # 清洗内容
        # 注意：.rounded 包含整个页面框架，我们需要移除 header 等部分
        # 在 clean_html_content 中已经处理了 .header-nav 等
        cleaned_html = clean_html_content(str(content_div), url)
        item['content'] = cleaned_html
        
        # 尝试从详情页获取更完整的标题（有时列表页标题被截断）
        page_title = soup.find('title')
        if page_title:
            item['title'] = page_title.get_text(strip=True)
    else:
        print(f"未找到正文内容 (.rounded): {url}")
        item['content'] = "" # 即使没找到，也保留条目
        
    return item

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

def main():
    # 1. 准备环境
    ensure_dir(OUTPUT_DIR)
    
    # 2. 获取列表
    items = crawl_list_page()
    if not items:
        print("未获取到任何新闻，程序结束")
        return

    # 3. 遍历抓取详情
    full_articles = []
    for item in items:
        article = crawl_detail_page(item)
        full_articles.append(article)
        # 礼貌性延时
        time.sleep(random.uniform(1, 2))
        
    # 4. 保存结果
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"vatican_press_news_{timestamp}.json"
    output_path = os.path.join(OUTPUT_DIR, filename)
    
    save_results(full_articles, output_path)

if __name__ == "__main__":
    main()