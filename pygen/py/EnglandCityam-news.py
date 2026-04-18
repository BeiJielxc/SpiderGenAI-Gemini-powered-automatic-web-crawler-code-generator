import os
import json
import time
import re
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==========================================
# 配置区域
# ==========================================
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
TARGET_URL = "https://www.cityam.com/news/"
MAX_ITEMS = 10  # 限制爬取前10条
REQUEST_DELAY = 2  # 请求间隔(秒)

# ==========================================
# 核心功能函数
# ==========================================

def setup_browser(p):
    """配置浏览器实例，包含反爬设置"""
    browser = p.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    )
    context = browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        locale="en-GB",
        extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"}
    )
    return browser, context

def clean_html_content(html_content, base_url):
    """
    清洗 HTML 内容：
    1. 移除不需要的干扰元素
    2. 将相对路径转换为绝对路径
    """
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除不需要的元素 (根据用户策略)
        selectors_to_remove = [
            '.article-header', 
            '.article-footer', 
            '.read-more', 
            '.newsletter-auto-inject', 
            '.notice-header', 
            'figure',
            'script',
            'style',
            'iframe',
            '.ad-container',
            '#leaderboard'
        ]
        for selector in selectors_to_remove:
            for tag in soup.select(selector):
                tag.decompose()

        # 修复图片链接 (相对路径 -> 绝对路径)
        for img in soup.find_all('img'):
            # 优先处理 data-src (懒加载)
            if img.get('data-src'):
                img['src'] = urljoin(base_url, img['data-src'])
            elif img.get('src'):
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
        print(f"内容清洗出错: {e}")
        return html_content

def extract_date_from_detail(soup):
    """
    从详情页提取发布日期
    优先级: JSON-LD -> meta标签 -> time标签
    """
    date_str = ""
    
    # 策略1: 查找 JSON-LD (结构化数据)
    try:
        ld_json_scripts = soup.find_all('script', type='application/ld+json')
        for script in ld_json_scripts:
            if script.string:
                try:
                    data = json.loads(script.string)
                    # 处理列表或字典
                    if isinstance(data, list):
                        data = data[0] if data else {}
                    
                    if 'datePublished' in data:
                        date_str = data['datePublished']
                        break
                    if '@graph' in data:
                        for item in data['@graph']:
                            if 'datePublished' in item:
                                date_str = item['datePublished']
                                break
                except:
                    continue
    except:
        pass

    # 策略2: 查找 meta 标签
    if not date_str:
        meta_date = soup.find('meta', property='article:published_time')
        if meta_date:
            date_str = meta_date.get('content')

    # 策略3: 查找 time 标签
    if not date_str:
        time_tag = soup.find('time')
        if time_tag:
            date_str = time_tag.get('datetime') or time_tag.get_text(strip=True)

    # 格式化日期 (提取 YYYY-MM-DD)
    if date_str:
        try:
            # 处理 ISO 格式 2024-05-20T10:00:00+00:00
            if 'T' in date_str:
                return date_str.split('T')[0]
            return date_str.strip()
        except:
            return date_str
            
    return datetime.now().strftime("%Y-%m-%d") # 兜底

def crawl_news():
    """爬取新闻主逻辑"""
    articles = []
    
    with sync_playwright() as p:
        browser, context = setup_browser(p)
        page = context.new_page()
        
        try:
            print(f"正在访问列表页: {TARGET_URL}")
            page.goto(TARGET_URL, timeout=60000)
            
            # 等待列表元素加载
            try:
                page.wait_for_selector(".content-listing__content-item", timeout=15000)
            except:
                print("等待列表元素超时，尝试继续解析页面...")

            # 处理 Cookie 弹窗 (尝试点击接受，避免遮挡)
            try:
                accept_btn = page.locator("#onetrust-accept-btn-handler")
                if accept_btn.is_visible(timeout=2000):
                    accept_btn.click()
                    time.sleep(1)
            except:
                pass

            # 解析列表页
            list_html = page.content()
            list_soup = BeautifulSoup(list_html, 'html.parser')
            
            # 提取新闻项
            items = list_soup.select(".content-listing__content-item")
            print(f"页面共找到 {len(items)} 个新闻项")
            
            # 提取前 10 条链接
            links_to_crawl = []
            for item in items:
                if len(links_to_crawl) >= MAX_ITEMS:
                    break
                
                title_tag = item.select_one(".card__title a")
                if title_tag and title_tag.get('href'):
                    link = urljoin(TARGET_URL, title_tag.get('href'))
                    title = title_tag.get_text(strip=True)
                    links_to_crawl.append({"url": link, "title": title})
            
            print(f"准备爬取前 {len(links_to_crawl)} 条详情页...")
            
            # 遍历详情页
            for index, link_info in enumerate(links_to_crawl):
                url = link_info['url']
                list_title = link_info['title']
                
                print(f"[{index+1}/{len(links_to_crawl)}] 正在抓取: {list_title}")
                
                try:
                    # 导航到详情页
                    page.goto(url, timeout=45000)
                    
                    # 等待正文容器加载
                    try:
                        page.wait_for_selector("article.content-container", timeout=15000)
                    except:
                        print(f"  - 警告: 详情页元素加载超时: {url}")
                    
                    # 获取详情页 HTML
                    detail_html = page.content()
                    detail_soup = BeautifulSoup(detail_html, 'html.parser')
                    
                    # 1. 提取标题 (优先用详情页 h1)
                    h1 = detail_soup.select_one("h1.article-header__title")
                    title = h1.get_text(strip=True) if h1 else list_title
                    
                    # 2. 提取日期
                    pub_date = extract_date_from_detail(detail_soup)
                    
                    # 3. 提取作者
                    author = "City AM"
                    author_tag = detail_soup.select_one(".article-header__author a") or detail_soup.select_one(".article-header__author")
                    if author_tag:
                        author = author_tag.get_text(strip=True)
                    
                    # 4. 提取正文
                    content_container = detail_soup.select_one("article.content-container")
                    content_html = ""
                    summary = ""
                    
                    if content_container:
                        # 提取纯文本摘要 (前200字)
                        summary_text = content_container.get_text(strip=True)
                        summary = (summary_text[:200] + "...") if len(summary_text) > 200 else summary_text
                        
                        # 清洗 HTML
                        content_html = clean_html_content(str(content_container), url)
                    else:
                        print("  - 错误: 未找到正文内容容器")
                    
                    article = {
                        "id": str(index + 1),
                        "title": title,
                        "date": pub_date,
                        "author": author,
                        "source": "City AM",
                        "sourceUrl": url,
                        "summary": summary,
                        "content": content_html
                    }
                    
                    articles.append(article)
                    print(f"  - 成功提取: {pub_date} | {title[:30]}...")
                    
                    # 遵守延迟
                    time.sleep(REQUEST_DELAY)
                    
                except Exception as e:
                    print(f"  - 抓取失败 {url}: {e}")
                    
        except Exception as e:
            print(f"爬虫运行出错: {e}")
        finally:
            browser.close()
            
    return articles

def save_results(articles: list, output_dir: str):
    """保存结果为 JSON"""
    if not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"cityam_news_{timestamp}.json"
    output_path = os.path.join(output_dir, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"\n结果已保存到: {output_path}")

def main():
    print("=== 开始爬取 City AM 新闻 ===")
    print(f"目标 URL: {TARGET_URL}")
    print(f"最大条数: {MAX_ITEMS}")
    
    articles = crawl_news()
    
    if articles:
        save_results(articles, OUTPUT_DIR)
        print(f"爬取完成，共获取 {len(articles)} 条新闻。")
    else:
        print("未获取到任何新闻数据。")

if __name__ == "__main__":
    main()