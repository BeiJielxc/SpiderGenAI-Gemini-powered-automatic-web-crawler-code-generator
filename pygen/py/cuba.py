import os
import re
import json
import time
import random
from datetime import datetime
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ================= 配置区域 =================
# 目标URL
TARGET_URL = "https://www.granma.cu/"

# 爬取时间范围
START_DATE = "2026-02-01"
END_DATE = "2026-02-12"

# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# ================= 工具函数 =================

def ensure_dir(path):
    """确保目录存在"""
    if not os.path.exists(path):
        os.makedirs(path)

def clean_html_content(html_content, base_url):
    """
    【强制要求】清洗HTML内容：
    1. 将相对路径转换为绝对路径 (img src, a href)
    2. 保留原始标签结构
    """
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 修复图片链接
        for img in soup.find_all('img'):
            if img.get('src'):
                # 处理可能的懒加载属性
                src = img.get('data-src') or img.get('src')
                img['src'] = urljoin(base_url, src)
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
                
        # 移除无用的脚本和样式标签
        for script in soup(["script", "style", "iframe"]):
            script.decompose()

        return str(soup)
    except Exception as e:
        print(f"[Warn] 内容清洗出错: {e}")
        return html_content

def extract_date_from_url(url):
    """
    从URL中提取日期
    例如: https://www.granma.cu/mundo/2026-02-11/titulo... -> 2026-02-11
    """
    match = re.search(r'/(\d{4}-\d{2}-\d{2})/', url)
    if match:
        return match.group(1)
    return None

def is_date_in_range(date_str):
    """判断日期是否在指定范围内"""
    if not date_str:
        return False
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
        start = datetime.strptime(START_DATE, "%Y-%m-%d")
        end = datetime.strptime(END_DATE, "%Y-%m-%d")
        return start <= target_date <= end
    except ValueError:
        return False

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
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"✅ 已保存 {len(articles)} 条新闻到 {output_path}")

# ================= 核心爬虫逻辑 =================

def crawl_detail_page(page, url):
    """爬取新闻详情页"""
    print(f"   -> 正在抓取详情: {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # 稍作等待以确保动态内容加载
        page.wait_for_timeout(1500)
        
        content_html = page.content()
        soup = BeautifulSoup(content_html, 'html.parser')
        
        # 提取正文：尝试多个可能的容器
        # Granma 通常结构: .story-content, 或者直接在 article 内
        content_div = soup.select_one('.story-content')
        if not content_div:
            content_div = soup.select_one('article .body')
        if not content_div:
            # 兜底：查找包含最多 p 标签的 div
            candidates = soup.find_all('div')
            max_p = 0
            for div in candidates:
                p_count = len(div.find_all('p'))
                if p_count > max_p:
                    max_p = p_count
                    content_div = div
        
        # 提取作者
        author = ""
        author_elem = soup.select_one('.author-name, .g-story-author, .byline')
        if author_elem:
            author = author_elem.get_text(strip=True)
            
        # 提取来源
        source = "Granma" # 默认为 Granma
        
        # 清洗正文
        clean_content = ""
        if content_div:
            clean_content = clean_html_content(str(content_div), url)
        else:
            print("   [Warn] 未找到正文内容")

        return {
            "content": clean_content,
            "author": author,
            "source": source
        }

    except Exception as e:
        print(f"   [Error] 详情页抓取失败: {e}")
        return {"content": "", "author": "", "source": "Granma"}

def run_crawler():
    ensure_dir(OUTPUT_DIR)
    
    # 启动 Playwright
    with sync_playwright() as p:
        # 配置浏览器（反爬设置）
        browser = p.chromium.launch(
            headless=True, # 设为 False 可观察浏览器行为
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="es-ES"
        )
        page = context.new_page()
        
        print(f"🚀 开始爬取: {TARGET_URL}")
        print(f"📅 目标日期范围: {START_DATE} 至 {END_DATE}")
        
        try:
            page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
        except PlaywrightTimeoutError:
            print("⚠️ 页面加载超时，尝试继续解析已加载内容...")
        
        # 获取列表页 HTML
        html = page.content()
        soup = BeautifulSoup(html, 'html.parser')
        
        articles_data = []
        seen_urls = set()
        
        # 定位所有文章链接
        # 根据页面分析，文章主要在 <article> 标签内，或者带有 g-regular-story 等类
        # 我们查找所有 article 标签内的 h2 > a
        article_nodes = soup.find_all('article')
        
        print(f"🔍 首页发现 {len(article_nodes)} 个文章块，开始筛选...")
        
        for node in article_nodes:
            # 提取标题和链接
            title_tag = node.find('h2')
            if not title_tag:
                continue
            
            link_tag = title_tag.find('a')
            if not link_tag:
                continue
                
            relative_url = link_tag.get('href')
            full_url = urljoin(TARGET_URL, relative_url)
            
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            
            title = link_tag.get_text(strip=True)
            
            # 提取摘要
            summary = ""
            summary_div = node.select_one('.sumario p') or node.find('p')
            if summary_div:
                summary = summary_div.get_text(strip=True)
            
            # 1. 尝试从 URL 提取日期 (最准确)
            date_str = extract_date_from_url(full_url)
            
            # 2. 如果 URL 没日期，尝试从页面文本提取 (备选)
            if not date_str:
                # 尝试查找类似 "11 de febrero de 2026" 的文本，这里简化处理
                # Granma 的 URL 通常都包含日期，如果找不到，可能是非新闻链接
                pass

            # 日期过滤
            if date_str:
                if not is_date_in_range(date_str):
                    # print(f"   [Skip] 日期不符: {date_str} - {title}")
                    continue
            else:
                print(f"   [Skip] 无法提取日期: {full_url}")
                continue
            
            print(f"✅ 命中目标: [{date_str}] {title}")
            
            # 进入详情页抓取
            detail_data = crawl_detail_page(page, full_url)
            
            article_item = {
                "id": str(len(articles_data) + 1),
                "title": title,
                "date": date_str,
                "source": detail_data['source'],
                "author": detail_data['author'],
                "sourceUrl": full_url,
                "summary": summary,
                "content": detail_data['content']
            }
            
            articles_data.append(article_item)
            
            # 礼貌性延时
            time.sleep(random.uniform(1, 2))
            
        # 保存结果
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"Granma_PORTADA_{timestamp}.json"
        output_path = os.path.join(OUTPUT_DIR, filename)
        
        save_results(articles_data, output_path)
        
        browser.close()

if __name__ == "__main__":
    run_crawler()