import os
import json
import time
import random
import requests
from datetime import datetime
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

# ================= 配置区域 =================
# 目标URL
BASE_URL = "https://pfts.ua/"
# 爬取时间范围
START_DATE = "2026-01-01"
END_DATE = "2026-02-12"
# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
}

# ================= 工具函数 =================

def ensure_dir(path):
    """确保目录存在"""
    if not os.path.exists(path):
        os.makedirs(path)

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：
    1. 将相对路径图片/链接转换为绝对路径
    2. 移除无用标签（可选）
    """
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 修复图片链接
        for img in soup.find_all('img'):
            src = img.get('src')
            if src:
                # 处理可能的懒加载属性
                if not src.startswith(('http://', 'https://', 'data:')):
                    img['src'] = urljoin(base_url, src)
                
        # 修复超链接
        for a in soup.find_all('a'):
            href = a.get('href')
            if href:
                if not href.startswith(('http://', 'https://', 'mailto:', 'tel:', '#')):
                    a['href'] = urljoin(base_url, href)
                
        return str(soup)
    except Exception as e:
        print(f"内容清洗出错: {e}")
        return html_content

def parse_date(date_str):
    """
    解析日期字符串
    支持格式: DD-MM-YYYY, DD.MM.YYYY
    """
    if not date_str:
        return None
    
    date_str = date_str.strip()
    formats = [
        "%d-%m-%Y", 
        "%d.%m.%Y", 
        "%Y-%m-%d",
        "%d %B %Y" # 尝试处理月份名称（如果locale支持）
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None

def is_date_in_range(date_str):
    """判断日期是否在指定范围内"""
    if not date_str:
        return False
    return START_DATE <= date_str <= END_DATE

def save_results(articles, output_path):
    """保存结果为JSON"""
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dateRange": {
            "start": START_DATE,
            "end": END_DATE
        },
        "articles": articles
    }
    
    ensure_dir(os.path.dirname(output_path))
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已保存 {len(articles)} 条新闻到 {output_path}")

# ================= 爬虫逻辑 =================

def fetch_page(url):
    """获取页面内容，带重试机制"""
    max_retries = 3
    for i in range(max_retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            response.raise_for_status()
            response.encoding = response.apparent_encoding # 自动检测编码
            return response.text
        except Exception as e:
            print(f"请求失败 {url} (重试 {i+1}/{max_retries}): {e}")
            time.sleep(random.uniform(1, 3))
    return None

def parse_detail_page(url):
    """解析新闻详情页"""
    html = fetch_page(url)
    if not html:
        return None
    
    soup = BeautifulSoup(html, 'html.parser')
    
    # 尝试定位正文内容
    # Joomla 常见的文章容器
    content_div = soup.find('div', itemprop='articleBody')
    if not content_div:
        content_div = soup.find('div', class_='item-page')
    if not content_div:
        content_div = soup.find('section', class_='article-content')
    
    # 如果还没找到，尝试找 id="sp-component" 下的内容
    if not content_div:
        main_comp = soup.find('main', id='sp-component')
        if main_comp:
            content_div = main_comp
            
    if content_div:
        # 移除不需要的元素（如打印按钮、分享按钮等）
        for trash in content_div.select('.print-icon, .email-icon, .article-info'):
            trash.decompose()
            
        content = clean_html_content(str(content_div), url)
        
        # 尝试提取作者或来源（如果有）
        author = ""
        meta_author = soup.find('meta', attrs={'name': 'author'})
        if meta_author:
            author = meta_author.get('content', '')
            
        return {
            "content": content,
            "author": author
        }
    
    return {"content": "", "author": ""}

def crawl_pfts_news():
    """主爬虫函数"""
    print(f"开始爬取 {BASE_URL}，时间范围: {START_DATE} 至 {END_DATE}")
    
    html = fetch_page(BASE_URL)
    if not html:
        print("无法获取首页内容，终止爬取。")
        return []
    
    soup = BeautifulSoup(html, 'html.parser')
    articles_list = []
    seen_urls = set()
    
    # 根据页面结构分析，新闻项在 div.nspArt 中
    # 首页有多个板块（新闻、分析、统计），它们共享相同的类名结构
    # 我们遍历所有 .nspArt 元素
    news_items = soup.select('div.nspArt')
    
    print(f"首页共发现 {len(news_items)} 个潜在新闻项")
    
    for item in news_items:
        try:
            # 提取标题和链接
            header_tag = item.select_one('h4.nspHeader a')
            if not header_tag:
                continue
                
            title = header_tag.get_text(strip=True)
            link = header_tag.get('href')
            
            if not link:
                continue
                
            full_url = urljoin(BASE_URL, link)
            
            # 去重
            if full_url in seen_urls:
                continue
            
            # 提取日期
            # 日期在 p.nspInfo 中，格式如 "03-02-2026 "
            date_tag = item.select_one('p.nspInfo')
            date_str = ""
            pub_date = None
            
            if date_tag:
                raw_date = date_tag.get_text(strip=True)
                # 尝试解析日期
                pub_date = parse_date(raw_date)
                if pub_date:
                    date_str = pub_date
            
            # 过滤日期
            if not pub_date:
                print(f"跳过无日期文章: {title}")
                continue
                
            if not is_date_in_range(pub_date):
                # print(f"跳过日期不符文章: {pub_date} - {title}")
                continue
            
            seen_urls.add(full_url)
            
            print(f"发现目标文章: [{pub_date}] {title}")
            
            # 抓取详情
            # 礼貌性延迟
            time.sleep(random.uniform(0.5, 1.5))
            
            detail_data = parse_detail_page(full_url)
            
            article = {
                "id": str(len(articles_list) + 1),
                "title": title,
                "date": pub_date,
                "source": "PFTS Stock Exchange",
                "sourceUrl": full_url,
                "summary": title, # 简单使用标题作为摘要
                "content": detail_data.get("content", "") if detail_data else "",
                "author": detail_data.get("author", "") if detail_data else ""
            }
            
            articles_list.append(article)
            
        except Exception as e:
            print(f"处理列表项时出错: {e}")
            continue
            
    return articles_list

def main():
    # 确保输出目录存在
    ensure_dir(OUTPUT_DIR)
    
    # 执行爬取
    articles = crawl_pfts_news()
    
    # 生成文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"pfts_news_{timestamp}.json"
    output_path = os.path.join(OUTPUT_DIR, filename)
    
    # 保存结果
    save_results(articles, output_path)

if __name__ == "__main__":
    main()