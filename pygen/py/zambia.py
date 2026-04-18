import os
import json
import time
import random
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ==========================================
# 配置区域
# ==========================================
TARGET_URL = "https://www.bancaynegocios.com/news/"
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"
MAX_ITEMS = 6  # 任务目标：前6条

# ==========================================
# 辅助函数
# ==========================================

def clean_html_content(html_content, base_url):
    """
    清洗HTML内容：
    1. 将相对路径图片/链接转换为绝对路径
    2. 移除无用标签
    """
    if not html_content:
        return ""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 修复图片链接
        for img in soup.find_all('img'):
            if img.get('src'):
                img['src'] = urljoin(base_url, img['src'])
            # 移除 srcset 属性，避免干扰
            if img.get('srcset'):
                del img['srcset']
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
                
        # 移除脚本和样式
        for script in soup(["script", "style", "iframe", "noscript"]):
            script.decompose()

        return str(soup)
    except Exception as e:
        print(f"[Warn] 内容清洗出错: {e}")
        return html_content

def save_results(articles: list, output_dir: str):
    """保存结果为JSON"""
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"bancaynegocios_news_{timestamp}.json"
    output_path = os.path.join(output_dir, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "targetUrl": TARGET_URL,
        "articles": articles
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"✅ 已保存 {len(articles)} 条新闻到: {output_path}")

def extract_detail_content(page, url):
    """
    进入详情页提取正文
    """
    try:
        print(f"   -> 正在抓取详情页: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # 随机等待模拟人类
        time.sleep(random.uniform(1, 2))
        
        content_html = ""
        
        # 尝试定位正文区域，WordPress 常见结构
        # 优先尝试 article 标签下的内容区域
        selectors = [
            "div.entry-content", 
            "div.post-content", 
            "article .content",
            "div.td-post-content",
            "article" # 兜底
        ]
        
        for selector in selectors:
            if page.locator(selector).count() > 0:
                # 排除可能包含在 article 中的非正文元素（如相关推荐、广告）
                # 这里简单获取 innerHTML，后续由 clean_html_content 清洗
                content_html = page.locator(selector).first.inner_html()
                break
        
        # 提取作者（如果有）
        author = ""
        author_selectors = [".author-name", ".entry-author", "a[rel='author']", ".td-post-author-name"]
        for sel in author_selectors:
            if page.locator(sel).count() > 0:
                author = page.locator(sel).first.inner_text().strip()
                break
                
        return content_html, author
        
    except Exception as e:
        print(f"   [Error] 详情页抓取失败: {e}")
        return "", ""

# ==========================================
# 主爬虫逻辑
# ==========================================

def run_crawler():
    print(f"🚀 启动爬虫，目标: {TARGET_URL}")
    print(f"🎯 任务目标: 抓取列表红框区域前 {MAX_ITEMS} 条新闻")
    
    articles = []
    
    with sync_playwright() as p:
        # 启动浏览器，配置反爬参数
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
        
        try:
            # 1. 访问列表页
            page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
            
            # 2. 定位目标区域
            # 根据提供的 HTML，新闻列表在 div.grid > article
            # 截图红框区域对应页面主体列表
            list_selector = "div.grid > article"
            
            # 等待列表加载
            page.wait_for_selector(list_selector, timeout=15000)
            
            # 获取所有文章节点
            article_elements = page.locator(list_selector).all()
            print(f"📊 页面共发现 {len(article_elements)} 条新闻，将处理前 {MAX_ITEMS} 条")
            
            # 3. 遍历提取
            for i, article in enumerate(article_elements):
                if i >= MAX_ITEMS:
                    break
                
                print(f"\nProcessing item {i+1}/{MAX_ITEMS}...")
                
                # --- 提取列表页信息 ---
                try:
                    # 标题 & 链接
                    title_el = article.locator("h2.post-title a").first
                    if not title_el.count():
                        print("   [Skip] 未找到标题元素")
                        continue
                        
                    title = title_el.inner_text().strip()
                    link = title_el.get_attribute("href")
                    
                    if not link:
                        continue
                        
                    # 补全链接
                    full_link = urljoin(TARGET_URL, link)
                    
                    # 日期
                    # HTML中显示: <span id="post_date">2026-02-22 16:53:14</span>
                    date_str = ""
                    date_el = article.locator("#post_date").first
                    if date_el.count():
                        date_str = date_el.inner_text().strip()
                        # 尝试只保留 YYYY-MM-DD
                        if " " in date_str:
                            date_str = date_str.split(" ")[0]
                    
                    # 摘要 (列表页可能没有显式摘要，尝试获取图片alt作为备选或留空)
                    summary = ""
                    img_el = article.locator(".post-thumbnail img").first
                    if img_el.count():
                        summary = img_el.get_attribute("alt") or ""
                    
                    # --- 进入详情页提取正文 ---
                    # 创建新页面去抓取详情，避免破坏列表页状态
                    detail_page = context.new_page()
                    content_html, author = extract_detail_content(detail_page, full_link)
                    
                    # 清洗内容
                    clean_content = clean_html_content(content_html, full_link)
                    
                    # 如果列表页没找到摘要，尝试从正文提取纯文本前200字
                    if not summary and clean_content:
                        soup = BeautifulSoup(clean_content, 'html.parser')
                        text = soup.get_text(separator=" ", strip=True)
                        summary = text[:200] + "..." if len(text) > 200 else text
                    
                    detail_page.close()
                    
                    # 组装数据
                    item = {
                        "id": str(i + 1),
                        "title": title,
                        "date": date_str,
                        "author": author or "Banca y Negocios",
                        "source": "Banca y Negocios",
                        "sourceUrl": full_link,
                        "summary": summary,
                        "content": clean_content
                    }
                    
                    articles.append(item)
                    print(f"   [OK] {title} ({date_str})")
                    
                except Exception as e:
                    print(f"   [Error] 处理单条新闻时出错: {e}")
                    continue
                    
        except Exception as e:
            print(f"❌ 爬取过程发生严重错误: {e}")
        finally:
            browser.close()
            
    # 4. 保存结果
    if articles:
        save_results(articles, OUTPUT_DIR)
    else:
        print("⚠️ 未抓取到任何数据")

if __name__ == "__main__":
    run_crawler()