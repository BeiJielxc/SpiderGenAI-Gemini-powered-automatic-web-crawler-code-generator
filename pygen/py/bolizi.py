import os
import json
import time
import re
import requests
from datetime import datetime
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

# ==========================================
# 配置区域
# ==========================================

# 目标网站配置
BASE_URL = "https://www.centralbank.org.bz"
LIST_URL = "https://www.centralbank.org.bz/publications-search"

# 爬取时间范围 (用户指定)
START_DATE_STR = "2026-02-01"
END_DATE_STR = "2026-02-28"

# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ==========================================
# 辅助函数
# ==========================================

def parse_date(date_str):
    """
    解析日期字符串，支持多种格式
    目标格式: YYYY-MM-DD
    """
    if not date_str:
        return None
    
    date_str = date_str.strip()
    
    # 格式: 11 February 2026
    try:
        dt = datetime.strptime(date_str, "%d %B %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    
    # 尝试从其他常见格式解析
    formats = [
        "%Y-%m-%d", "%B %d, %Y", "%d %b %Y", "%Y/%m/%d"
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
            
    return None

def extract_date_from_url(url):
    """从URL中提取日期 (例如: .../2026/01/23/...)"""
    match = re.search(r'/(\d{4})/(\d{2})/(\d{2})/', url)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
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
        
        # 移除 script, style, iframe 等干扰元素
        for tag in soup(['script', 'style', 'iframe', 'noscript', 'header', 'footer']):
            tag.decompose()
            
        # 修复图片链接
        for img in soup.find_all('img'):
            if img.get('src'):
                img['src'] = urljoin(base_url, img['src'])
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
                
        # 返回 body 内容或 soup 字符串
        if soup.body:
            return str(soup.body).strip()
        return str(soup).strip()
    except Exception as e:
        print(f"内容清洗出错: {e}")
        return html_content

def save_results(articles, output_dir):
    """保存结果为JSON文件"""
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"centralbank_bz_news_{timestamp}.json"
    output_path = os.path.join(output_dir, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dateRange": {
            "start": START_DATE_STR,
            "end": END_DATE_STR
        },
        "articles": articles
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        
    print(f"✅ 结果已保存至: {output_path}")

# ==========================================
# 爬虫主逻辑
# ==========================================

def get_article_content(url):
    """获取文章详情页内容"""
    try:
        time.sleep(1) # 礼貌请求
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"❌ 无法访问详情页: {url} (Status: {resp.status_code})")
            return "", ""
            
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 尝试定位正文区域
        # 根据页面结构分析，内容通常在 .interior-layout__main 或 .content 中
        content_div = soup.find('div', class_='interior-layout__main')
        if not content_div:
            content_div = soup.find('div', class_='content')
        if not content_div:
            content_div = soup.find('div', id='ContentPlaceholder_T5E3E3FE2005_Col01')
            
        # 如果还是找不到，尝试找 article 标签
        if not content_div:
            content_div = soup.find('article')

        content = ""
        if content_div:
            # 移除面包屑、标题等可能重复的内容
            for ignore in content_div.find_all(['div', 'ul'], class_=['interior-layout__title', 'pagination', 'item-list']):
                ignore.decompose()
            content = clean_html_content(str(content_div), url)
            
        # 尝试提取作者或来源 (页面上可能没有明确的作者字段，尝试查找常见元数据)
        author = "Central Bank of Belize" # 默认来源
        
        return content, author
        
    except Exception as e:
        print(f"❌ 解析详情页出错 {url}: {e}")
        return "", ""

def crawl_news():
    """主爬取函数"""
    print(f"🚀 开始爬取 Central Bank of Belize 新闻")
    print(f"📅 目标时间范围: {START_DATE_STR} 至 {END_DATE_STR}")
    
    articles = []
    start_row = 0
    rows_per_page = 20
    has_more = True
    page_num = 1
    
    # 转换时间范围用于比较
    start_date_obj = datetime.strptime(START_DATE_STR, "%Y-%m-%d")
    end_date_obj = datetime.strptime(END_DATE_STR, "%Y-%m-%d")
    
    with requests.Session() as session:
        session.headers.update(HEADERS)
        
        while has_more:
            print(f"\n📄 正在处理第 {page_num} 页 (StartRow: {start_row})...")
            
            # 构造分页URL
            params = {
                "startRow": start_row,
                "rowsPerPage": rows_per_page
            }
            
            try:
                resp = session.get(LIST_URL, params=params, timeout=15)
                if resp.status_code != 200:
                    print(f"❌ 请求列表页失败: {resp.status_code}")
                    break
                
                soup = BeautifulSoup(resp.text, 'html.parser')
                
                # 定位列表项
                items = soup.select('ul.item-list > li.item-list__item')
                
                if not items:
                    print("⚠️ 当前页未找到数据，停止翻页")
                    break
                
                page_articles_count = 0
                stop_crawling = False
                
                for item in items:
                    try:
                        # 提取标题和链接
                        title_tag = item.select_one('a.item-list__title')
                        if not title_tag:
                            continue
                            
                        title = title_tag.get_text(strip=True)
                        link = title_tag.get('href')
                        if link:
                            link = urljoin(BASE_URL, link)
                        
                        # 提取摘要
                        summary_div = item.select_one('.item-list__description')
                        summary = summary_div.get_text(strip=True) if summary_div else ""
                        
                        # 提取日期
                        # 策略1: 从列表页 <p class="item-list__date"> 提取
                        date_tag = item.select_one('p.item-list__date')
                        date_str = None
                        
                        if date_tag:
                            raw_date = date_tag.get_text(strip=True)
                            date_str = parse_date(raw_date)
                        
                        # 策略2: 如果列表页没有日期，尝试从URL提取
                        if not date_str and link:
                            date_str = extract_date_from_url(link)
                            
                        # 策略3: 如果还是没有日期，且链接存在，可能需要进详情页(暂略，为效率先跳过，除非必要)
                        # 这里假设如果没有日期，无法判断范围，暂时标记为 None
                        
                        # 日期过滤逻辑
                        is_in_range = False
                        if date_str:
                            current_date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                            
                            if current_date_obj > end_date_obj:
                                # 日期比结束时间晚，跳过，继续找后面的
                                continue
                            elif current_date_obj < start_date_obj:
                                # 日期比开始时间早，说明已经超出了范围
                                # 因为列表通常是倒序的，所以可以停止爬取了
                                print(f"⏹️ 发现早于开始时间的记录 ({date_str})，停止爬取")
                                stop_crawling = True
                                break
                            else:
                                is_in_range = True
                        else:
                            # 如果没有日期，无法判断。
                            # 严格模式下丢弃，或者尝试进入详情页获取。
                            # 鉴于用户要求严格的时间范围，这里如果无法获取日期则丢弃。
                            print(f"⚠️ 跳过无日期记录: {title}")
                            continue
                            
                        if is_in_range:
                            print(f"✅ 捕获目标: {date_str} | {title}")
                            
                            # 获取详情页内容
                            content, author = get_article_content(link)
                            
                            article = {
                                "title": title,
                                "date": date_str,
                                "source": "Central Bank of Belize",
                                "author": author,
                                "sourceUrl": link,
                                "summary": summary,
                                "content": content
                            }
                            articles.append(article)
                            page_articles_count += 1
                            
                    except Exception as e:
                        print(f"❌ 处理单条记录出错: {e}")
                        continue
                
                # 翻页逻辑
                if stop_crawling:
                    has_more = False
                else:
                    # 检查是否有下一页按钮或者数据是否满页
                    next_btn = soup.find('a', string='»')
                    if not next_btn and len(items) < rows_per_page:
                        print("🏁 已到达最后一页")
                        has_more = False
                    else:
                        start_row += rows_per_page
                        page_num += 1
                        time.sleep(2) # 翻页间隔
                        
            except Exception as e:
                print(f"❌ 爬取过程发生异常: {e}")
                break
                
    return articles

def main():
    # 确保输出目录存在
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        
    # 执行爬取
    articles = crawl_news()
    
    # 保存结果
    if articles:
        save_results(articles, OUTPUT_DIR)
        print(f"\n🎉 爬取完成! 共获取 {len(articles)} 条符合条件的新闻。")
    else:
        print("\n⚠️ 未找到符合指定时间范围的新闻。")

if __name__ == "__main__":
    main()