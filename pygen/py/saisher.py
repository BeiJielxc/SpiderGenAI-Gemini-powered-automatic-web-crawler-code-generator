import os
import json
import time
import re
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ================= 配置区域 =================
# 目标网站
BASE_URL = "https://www.nation.sc"
# 爬取时间范围
START_DATE = "2026-01-01"
END_DATE = "2026-02-12"
# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# ================= 工具函数 =================

def parse_date(date_str):
    """
    解析日期字符串，支持多种格式
    目标格式: YYYY-MM-DD
    """
    if not date_str:
        return None
    
    # 清理日期字符串 (例如 "|12.02.2026" -> "12.02.2026")
    date_str = date_str.replace('|', '').strip()
    
    # 尝试匹配 DD.MM.YYYY 格式 (页面特定格式)
    match = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', date_str)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"
    
    # 尝试其他通用格式
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
    except:
        pass
        
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
        for tag in soup.find_all(['script', 'style', 'iframe', 'button']):
            tag.decompose()
            
        # 修复图片链接
        for img in soup.find_all('img'):
            if img.get('src'):
                # 忽略 base64 图片
                if img['src'].startswith('data:'):
                    continue
                img['src'] = urljoin(base_url, img['src'])
                # 移除 srcset 属性，防止浏览器加载错误图片
                if img.get('srcset'):
                    del img['srcset']
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
                
        return str(soup)
    except Exception as e:
        print(f"[Warn] 内容清洗出错: {e}")
        return html_content

def save_results(articles, category_name):
    """保存结果到JSON文件"""
    if not articles:
        print("没有数据需要保存")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"NationSC_{category_name}_{timestamp}.json"
    # 清理文件名中的非法字符
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
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
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"✅ 成功保存 {len(articles)} 条新闻到: {output_path}")

# ================= 核心爬虫逻辑 =================

def extract_article_content(page, url):
    """进入详情页提取文章内容"""
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        # 稍作等待确保动态内容加载
        page.wait_for_timeout(1000)
        
        content = page.content()
        soup = BeautifulSoup(content, 'html.parser')
        
        article_data = {
            "sourceUrl": url,
            "source": "Seychelles Nation",
            "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # 1. 提取标题
        # 通常在 h1 中
        h1 = soup.find('h1')
        if h1:
            article_data['title'] = h1.get_text(strip=True)
        else:
            # 备选标题提取
            title_meta = soup.find('meta', property='og:title')
            article_data['title'] = title_meta['content'] if title_meta else "No Title"

        # 2. 提取日期
        # 详情页通常也有日期，格式类似 |12.02.2026
        date_elem = soup.select_one('.date, span.date, .article-date')
        if date_elem:
            raw_date = date_elem.get_text(strip=True)
            article_data['date'] = parse_date(raw_date)
        
        # 3. 提取正文
        # 尝试定位正文容器
        content_div = None
        # 常见的新闻正文选择器
        possible_selectors = [
            'div.article-content', 'div.news-body', 'div.content', 
            'article', 'div.col-md-8' # 根据首页结构推测
        ]
        
        for selector in possible_selectors:
            found = soup.select_one(selector)
            if found and len(found.get_text(strip=True)) > 50: # 确保有足够内容
                content_div = found
                break
        
        if content_div:
            # 移除正文中的标题和日期，避免重复
            if h1: h1.decompose()
            if date_elem: date_elem.decompose()
            
            article_data['content'] = clean_html_content(str(content_div), url)
            article_data['summary'] = content_div.get_text(strip=True)[:200] + "..."
        else:
            article_data['content'] = ""
            article_data['summary'] = ""
            
        # 4. 提取作者 (如果有)
        author_elem = soup.select_one('.author, .byline')
        if author_elem:
            article_data['author'] = author_elem.get_text(strip=True)
            
        return article_data

    except Exception as e:
        print(f"[Error] 解析详情页失败 {url}: {e}")
        return None

def crawl_category(page, category_url, category_name):
    """爬取指定分类列表页"""
    print(f"\n🚀 开始爬取板块: {category_name} ({category_url})")
    
    articles_collected = []
    page_num = 1
    has_next_page = True
    
    # 访问分类页
    try:
        page.goto(category_url, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        print(f"[Error] 无法访问分类页: {e}")
        return []

    while has_next_page:
        print(f"📄 正在处理第 {page_num} 页...")
        
        # 等待列表加载
        page.wait_for_timeout(2000)
        
        # 解析当前页面的文章链接
        # 根据首页结构，文章通常在 article 标签内，或者带有特定 class
        # 链接通常包含 /articles/
        content = page.content()
        soup = BeautifulSoup(content, 'html.parser')
        
        # 查找所有文章链接
        # 策略：查找 href 包含 /articles/ 的 a 标签
        article_links = []
        seen_urls = set()
        
        for a in soup.find_all('a', href=True):
            href = a['href']
            if '/articles/' in href and href not in seen_urls:
                # 排除评论链接等
                if '#' in href: continue
                
                full_url = urljoin(BASE_URL, href)
                
                # 尝试在列表页直接获取日期（减少请求次数）
                # 首页HTML显示日期在 span.date 中，格式 |12.02.2026
                # 我们需要找到这个链接对应的日期
                date_str = None
                
                # 情况1: 日期在 a 标签内部 (如首页某些部分)
                date_span = a.find('span', class_='date')
                
                # 情况2: 日期在 a 标签的父级或兄弟节点
                if not date_span:
                    # 向上找 article 容器
                    article_container = a.find_parent('article')
                    if article_container:
                        date_span = article_container.find('span', class_='date')
                
                if date_span:
                    date_str = parse_date(date_span.get_text(strip=True))
                
                article_links.append({
                    'url': full_url,
                    'date': date_str # 可能为 None
                })
                seen_urls.add(href)
        
        print(f"   -> 发现 {len(article_links)} 个文章链接")
        
        if not article_links:
            print("   -> 未发现文章，可能已到达末尾或页面结构改变")
            break

        # 遍历当前页的文章
        page_valid_count = 0
        for item in article_links:
            url = item['url']
            date = item['date']
            
            # 如果列表页没有日期，必须进入详情页获取日期进行判断
            # 如果列表页有日期，可以提前过滤
            
            if date:
                if date < START_DATE:
                    print(f"   -> [跳过] 日期 {date} 早于起始日期 {START_DATE}")
                    # 如果列表是按时间倒序的，这里可以考虑停止翻页
                    # 但为了保险（可能有置顶文章），我们只跳过当前，设置标志位
                    # 如果连续多条都早于日期，则停止
                    continue
                if date > END_DATE:
                    print(f"   -> [跳过] 日期 {date} 晚于结束日期 {END_DATE}")
                    continue
            
            # 进入详情页
            print(f"   -> 正在抓取: {url}")
            details = extract_article_content(page, url)
            
            if details:
                # 如果列表页没拿到日期，使用详情页的日期进行二次过滤
                final_date = details.get('date')
                if not final_date:
                    print("   -> [警告] 无法提取日期，默认保留")
                elif final_date < START_DATE:
                    print(f"   -> [丢弃] 详情页日期 {final_date} 早于范围")
                    # 返回列表页以便继续
                    page.go_back() 
                    # 如果发现日期已经很旧了，可以标记停止
                    if page_valid_count == 0 and len(articles_collected) > 0:
                         print("   -> 检测到旧新闻，停止翻页")
                         has_next_page = False
                         break
                    continue
                elif final_date > END_DATE:
                    print(f"   -> [丢弃] 详情页日期 {final_date} 晚于范围")
                    page.go_back()
                    continue
                
                articles_collected.append(details)
                page_valid_count += 1
                print(f"   -> [成功] {details.get('title', '')[:30]}... ({final_date})")
            
            # 每次抓取后返回列表页 (Playwright 单页模式)
            # 注意：extract_article_content 会跳转，所以需要 go_back 或者重新 goto 列表页
            # 为了稳定性，建议重新 goto 列表页并恢复分页状态（比较复杂），
            # 或者使用 browser.new_page() 打开详情页。这里使用 go_back 简单处理。
            page.go_back(wait_until="domcontentloaded")
            # 随机延时
            time.sleep(1)

        # 检查是否需要停止
        if not has_next_page:
            break

        # 翻页逻辑
        # 查找 "Next" 按钮或页码
        # 常见的分页选择器: .pagination .next a, a:has-text("Next"), a:has-text("»")
        try:
            next_btn = page.locator('ul.pagination li a[rel="next"], ul.pagination li:last-child a, a:has-text("Next"), a:has-text("»")').first
            if next_btn.is_visible():
                print("   -> 点击下一页...")
                next_btn.click()
                page_num += 1
                # 检查日期是否已经全部超期（简单的启发式规则）
                # 如果本页所有文章都早于 START_DATE，则停止
                # 这里简化处理：依赖上面的逐条检查逻辑来决定是否继续
            else:
                print("   -> 没有下一页了")
                has_next_page = False
        except Exception as e:
            print(f"   -> 翻页检测出错: {e}")
            has_next_page = False

    return articles_collected

def main():
    print(f"=== 启动爬虫: {BASE_URL} ===")
    print(f"目标时间范围: {START_DATE} 至 {END_DATE}")
    
    with sync_playwright() as p:
        # 启动浏览器
        browser = p.chromium.launch(
            headless=True, # 设置为 False 可视化观察
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        # 1. 访问首页
        print(f"正在访问首页...")
        page.goto(BASE_URL, wait_until="domcontentloaded")
        
        # 2. 自动发现一个主要板块进行爬取
        # 策略：从导航栏中提取第一个具体的分类链接 (例如 category/55/education)
        # 这样符合“单一板块”的要求，同时能进入列表页进行翻页
        content = page.content()
        soup = BeautifulSoup(content, 'html.parser')
        
        target_url = None
        target_name = "Home_Latest"
        
        # 查找导航栏中的分类链接
        # 根据HTML结构: ul.nav.navbar-nav.mainnav -> li -> ul.dropdown-menu -> li -> a
        nav_links = soup.select('ul.mainnav a[href*="category/"]')
        
        if nav_links:
            # 优先选择 "Politics" 或 "National" 这种更新频繁的板块，或者直接取第一个
            # 这里我们简单取第一个非空的
            first_link = nav_links[0]
            target_url = urljoin(BASE_URL, first_link['href'])
            target_name = first_link.get_text(strip=True)
            print(f"自动定位到板块: {target_name} -> {target_url}")
        else:
            print("未找到明确的分类导航，将尝试爬取首页可见内容...")
            # 如果找不到分类，就只爬首页
            target_url = BASE_URL
            
        # 3. 执行爬取
        if target_url:
            articles = crawl_category(page, target_url, target_name)
            save_results(articles, target_name)
        
        browser.close()

if __name__ == "__main__":
    main()