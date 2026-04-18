import os
import json
import time
import random
import requests
from datetime import datetime
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

# ================= 配置区域 =================
# 目标板块URL (Actualités)
BASE_URL = "https://www.business-magazine.mu"
START_URL = "https://www.business-magazine.mu/rubrique/actualites/"

# 爬取时间范围
START_DATE = "2026-01-01"
END_DATE = "2026-02-12"

# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    "Referer": "https://www.business-magazine.mu/"
}

# 月份映射（用于解析英文日期）
MONTH_MAP = {
    "January": "01", "February": "02", "March": "03", "April": "04",
    "May": "05", "June": "06", "July": "07", "August": "08",
    "September": "09", "October": "10", "November": "11", "December": "12",
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "Jun": "06", "Jul": "07", "Aug": "08", "Sep": "09",
    "Oct": "10", "Nov": "11", "Dec": "12",
    # 法语月份支持（以防万一）
    "Janvier": "01", "Février": "02", "Mars": "03", "Avril": "04",
    "Mai": "05", "Juin": "06", "Juillet": "07", "Août": "08",
    "Septembre": "09", "Octobre": "10", "Novembre": "11", "Décembre": "12"
}

# ================= 工具函数 =================

def ensure_dir(path):
    """确保目录存在"""
    if not os.path.exists(path):
        os.makedirs(path)

def parse_date(date_str):
    """
    解析日期字符串，格式示例: "February 11, 2026" 或 "11 February 2026"
    返回格式: YYYY-MM-DD
    """
    if not date_str:
        return ""
    
    clean_date = date_str.strip()
    try:
        # 尝试处理 "Month DD, YYYY" 格式 (如: February 11, 2026)
        parts = clean_date.replace(',', '').split()
        if len(parts) == 3:
            if parts[0] in MONTH_MAP: # Month DD YYYY
                month = MONTH_MAP[parts[0]]
                day = parts[1].zfill(2)
                year = parts[2]
                return f"{year}-{month}-{day}"
            elif parts[1] in MONTH_MAP: # DD Month YYYY
                day = parts[0].zfill(2)
                month = MONTH_MAP[parts[1]]
                year = parts[2]
                return f"{year}-{month}-{day}"
        
        # 尝试标准解析
        dt = datetime.strptime(clean_date, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""

def clean_html_content(html_content, base_url):
    """清洗HTML内容：修复图片链接，移除无用标签"""
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除 script, style, iframe 等干扰元素
        for tag in soup(['script', 'style', 'iframe', 'form', 'button', 'input']):
            tag.decompose()
            
        # 修复图片链接 (相对路径 -> 绝对路径)
        for img in soup.find_all('img'):
            src = img.get('src')
            if src:
                # 处理懒加载属性
                if img.get('data-src'):
                    src = img.get('data-src')
                elif img.get('data-lazy-src'):
                    src = img.get('data-lazy-src')
                
                if not src.startswith(('http://', 'https://', 'data:')):
                    img['src'] = urljoin(base_url, src)
                else:
                    img['src'] = src
                    
                # 移除 srcset 属性防止干扰
                if img.get('srcset'):
                    del img['srcset']

        # 修复超链接
        for a in soup.find_all('a'):
            href = a.get('href')
            if href and not href.startswith(('http://', 'https://', 'mailto:', 'tel:', '#')):
                a['href'] = urljoin(base_url, href)

        return str(soup)
    except Exception as e:
        print(f"[Warn] HTML清洗出错: {e}")
        return html_content

def save_results(articles, output_dir):
    """保存结果为JSON"""
    ensure_dir(output_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"business_magazine_actualites_{timestamp}.json"
    filepath = os.path.join(output_dir, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dateRange": {
            "start": START_DATE,
            "end": END_DATE
        },
        "source": "Business Magazine - Actualités",
        "articles": articles
    }
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"\n[Success] 已保存 {len(articles)} 条新闻到: {filepath}")

# ================= 核心爬虫逻辑 =================

class NewsCrawler:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.articles = []
        self.visited_urls = set()

    def fetch_page(self, url):
        """获取页面内容，带重试机制"""
        retries = 3
        for i in range(retries):
            try:
                time.sleep(random.uniform(1.5, 3.0)) # 礼貌爬取
                response = self.session.get(url, timeout=15)
                response.raise_for_status()
                return response.text
            except Exception as e:
                print(f"[Error] 请求失败 ({i+1}/{retries}): {url} - {e}")
                time.sleep(2)
        return None

    def parse_list_page(self, html, current_url):
        """解析列表页，提取文章链接和基本信息"""
        soup = BeautifulSoup(html, 'html.parser')
        items = []
        
        # 查找所有文章块
        # 根据页面分析，文章通常在 .tt-post 类中
        post_blocks = soup.select('.tt-post')
        
        print(f"  - 找到 {len(post_blocks)} 个文章块")
        
        for post in post_blocks:
            try:
                # 提取链接
                link_tag = post.select_one('a.tt-post-img, .tt-post-title a')
                if not link_tag:
                    continue
                
                url = urljoin(current_url, link_tag.get('href'))
                
                # 提取标题
                title_tag = post.select_one('.tt-post-title')
                title = title_tag.get_text(strip=True) if title_tag else ""
                
                # 提取日期
                date_tag = post.select_one('.tt-post-date')
                date_str = date_tag.get_text(strip=True) if date_tag else ""
                pub_date = parse_date(date_str)
                
                # 提取摘要
                summary_tag = post.select_one('.simple-text')
                summary = summary_tag.get_text(strip=True) if summary_tag else ""
                
                # 提取作者
                author_tag = post.select_one('.tt-post-author-name a')
                author = author_tag.get_text(strip=True) if author_tag else "Business Magazine"

                if url and title:
                    items.append({
                        "url": url,
                        "title": title,
                        "date": pub_date,
                        "summary": summary,
                        "author": author,
                        "source": "Business Magazine"
                    })
            except Exception as e:
                print(f"[Warn] 解析列表项出错: {e}")
                continue
                
        return items

    def parse_detail_page(self, url, basic_info):
        """解析详情页，提取正文"""
        html = self.fetch_page(url)
        if not html:
            return None
            
        soup = BeautifulSoup(html, 'html.parser')
        
        # 尝试定位正文区域
        # WordPress 常见正文类名
        content_selectors = [
            '.tt-post-content', 
            '.entry-content', 
            '.post-content',
            'article .content',
            '.tt-content'
        ]
        
        content_html = ""
        for selector in content_selectors:
            content_div = soup.select_one(selector)
            if content_div:
                # 移除分享按钮、广告等干扰项
                for noise in content_div.select('.tt-share-icons, .tt-post-bottom, .google-auto-placed'):
                    noise.decompose()
                content_html = str(content_div)
                break
        
        # 如果没找到正文，尝试提取所有 p 标签
        if not content_html:
            ps = soup.select('.tt-content p')
            if ps:
                content_html = "".join([str(p) for p in ps])

        # 清洗内容
        cleaned_content = clean_html_content(content_html, url)
        
        # 如果列表页没提取到日期，尝试在详情页提取
        if not basic_info.get('date'):
            date_tag = soup.select_one('.tt-post-date, .entry-date, time')
            if date_tag:
                basic_info['date'] = parse_date(date_tag.get_text(strip=True))

        article = {
            "id": url, # 使用URL作为ID
            "title": basic_info['title'],
            "date": basic_info['date'],
            "author": basic_info['author'],
            "source": basic_info['source'],
            "sourceUrl": url,
            "summary": basic_info['summary'],
            "content": cleaned_content
        }
        return article

    def run(self):
        print(f"=== 开始爬取 Business Magazine (Actualités) ===")
        print(f"目标时间范围: {START_DATE} 至 {END_DATE}")
        
        page = 1
        has_more = True
        
        while has_more:
            # 构建分页URL
            if page == 1:
                url = START_URL
            else:
                url = f"{START_URL}page/{page}/"
            
            print(f"\n正在抓取列表页: {url}")
            html = self.fetch_page(url)
            
            if not html:
                print("无法获取列表页，停止翻页")
                break
                
            # 检查是否是有效的列表页（有些WP站点页码超出返回404页面但状态码是200）
            if "error-404" in html or "Page not found" in html:
                print("页面不存在，停止翻页")
                break

            items = self.parse_list_page(html, url)
            if not items:
                print("当前页未找到文章，停止翻页")
                break
            
            # 处理当前页的文章
            page_valid_count = 0
            for item in items:
                # 日期过滤逻辑
                item_date = item.get('date')
                
                # 如果没有日期，默认需要抓取（或者进入详情页确认）
                # 这里假设列表页大部分都有日期
                if item_date:
                    if item_date < START_DATE:
                        print(f"  [Skip] 日期 {item_date} 早于开始时间，停止后续翻页")
                        has_more = False # 因为是按时间倒序，遇到旧新闻可以直接停止
                        break 
                    if item_date > END_DATE:
                        print(f"  [Skip] 日期 {item_date} 晚于结束时间，跳过")
                        continue
                
                if item['url'] in self.visited_urls:
                    continue
                
                print(f"  [Crawl] {item['title']} ({item_date})")
                
                # 抓取详情
                article = self.parse_detail_page(item['url'], item)
                if article:
                    # 二次检查日期（如果列表页没日期，详情页获取后再次检查）
                    final_date = article.get('date')
                    if final_date:
                        if START_DATE <= final_date <= END_DATE:
                            self.articles.append(article)
                            self.visited_urls.add(item['url'])
                            page_valid_count += 1
                        else:
                            print(f"    -> 详情页日期 {final_date} 不在范围内，丢弃")
                    else:
                        # 没日期的保留，或者根据策略丢弃
                        print(f"    -> 警告: 无法提取日期，保留数据")
                        self.articles.append(article)
                        self.visited_urls.add(item['url'])
                        page_valid_count += 1
            
            if not has_more:
                break
                
            page += 1
            # 安全限制，防止无限翻页
            if page > 50: 
                print("达到最大页数限制，停止")
                break

        # 保存结果
        save_results(self.articles, OUTPUT_DIR)

def main():
    crawler = NewsCrawler()
    crawler.run()

if __name__ == "__main__":
    main()