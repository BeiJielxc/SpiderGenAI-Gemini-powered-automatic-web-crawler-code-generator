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
MAX_ITEMS = 10  # 任务目标：前10条

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
            if img.get('src'):
                # 处理懒加载属性 data-src 
                if img.get('data-src'):
                    img['src'] = img['data-src']
                img['src'] = urljoin(base_url, img['src'])
                
        # 修复超链接
        for a in soup.find_all('a'):
            if a.get('href'):
                a['href'] = urljoin(base_url, a['href'])
                
        # 移除可能的广告或无关元素 (根据通用经验)
        for useless in soup.select('.ads, .share-buttons, script, style'):
            useless.decompose()
            
        return str(soup)
    except Exception as e:
        print(f"[Warn] 内容清洗出错: {e}")
        return html_content

def extract_detail_page(page, url):
    """
    进入详情页提取正文和作者信息
    """
    try:
        print(f"-> 正在抓取详情页: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # 随机等待模拟人类阅读
        time.sleep(random.uniform(1.5, 3.0))
        
        # 尝试定位正文容器
        # 常见 WordPress 正文类名
        content_selectors = [
            "div.entry-content",
            "article.post .content",
            "div.post-content",
            "article"
        ]
        
        content_html = ""
        for selector in content_selectors:
            if page.locator(selector).count() > 0:
                # 排除正文后的分享区、广告等
                content_html = page.locator(selector).first.inner_html()
                break
        
        # 提取作者 (如果有)
        author = ""
        author_selectors = [".author-name", ".post-author", "a[rel='author']", ".meta-author"]
        for sel in author_selectors:
            if page.locator(sel).count() > 0:
                author = page.locator(sel).first.inner_text().strip()
                break
        
        # 如果没找到作者，默认为 Banca y Negocios
        if not author:
            author = "Banca y Negocios"

        return content_html, author
        
    except Exception as e:
        print(f"[Error] 详情页抓取失败 {url}: {e}")
        return "", ""

def crawl_news():
    """
    主爬虫逻辑
    """
    articles_data = []
    
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
            print(f"正在访问列表页: {TARGET_URL}")
            page.goto(TARGET_URL, wait_until="networkidle", timeout=45000)
            
            # 根据截图红框区域定位：Últimas Noticias 下的 grid
            # 选择器策略：找到包含 'Últimas Noticias' 的标题，然后找其后的 .grid
            # 或者直接定位 .grid > article (根据提供的HTML结构)
            
            # 等待列表加载
            page.wait_for_selector(".grid > article", timeout=15000)
            
            # 获取所有文章卡片
            cards = page.locator(".grid > article").all()
            print(f"发现 {len(cards)} 篇文章，准备抓取前 {MAX_ITEMS} 条...")
            
            # 限制抓取数量
            target_cards = cards[:MAX_ITEMS]
            
            # 第一阶段：从列表页提取基础信息
            basic_info_list = []
            for i, card in enumerate(target_cards):
                try:
                    # 提取标题
                    title_el = card.locator("h2.post-title a")
                    if title_el.count() == 0:
                        continue
                    title = title_el.inner_text().strip()
                    link = title_el.get_attribute("href")
                    
                    # 提取日期
                    # HTML中显示有 <span id="post_date">...</span>
                    # 注意：ID应该是唯一的，但这里每个article都有，Playwright locator会定位到当前card下的元素
                    date_str = ""
                    date_el = card.locator("#post_date")
                    if date_el.count() > 0:
                        raw_date = date_el.inner_text().strip()
                        # 格式示例: 2026-02-22 16:53:14
                        # 只需要 YYYY-MM-DD
                        if " " in raw_date:
                            date_str = raw_date.split(" ")[0]
                        else:
                            date_str = raw_date
                    else:
                        # 备选：从 modified date 提取
                        mod_el = card.locator("#post_modified")
                        if mod_el.count() > 0:
                            raw_date = mod_el.inner_text().strip()
                            date_str = raw_date.split(" ")[0] if " " in raw_date else raw_date
                    
                    # 提取封面图作为摘要图片（可选）
                    img_src = ""
                    img_el = card.locator("a.post-thumbnail img")
                    if img_el.count() > 0:
                        img_src = img_el.get_attribute("src")

                    # 完整链接
                    full_link = urljoin(TARGET_URL, link)
                    
                    basic_info_list.append({
                        "id": str(i + 1),
                        "title": title,
                        "date": date_str,
                        "sourceUrl": full_link,
                        "source": "Banca y Negocios",
                        "summary": title, # 暂时用标题做摘要，详情页可优化
                        "cover_image": img_src
                    })
                    
                except Exception as e:
                    print(f"[Error] 解析列表项 {i} 失败: {e}")
                    continue
            
            # 第二阶段：进入详情页抓取正文
            for item in basic_info_list:
                try:
                    content_html, author = extract_detail_page(page, item['sourceUrl'])
                    
                    # 清洗内容
                    cleaned_content = clean_html_content(content_html, item['sourceUrl'])
                    
                    # 更新字段
                    item['content'] = cleaned_content
                    item['author'] = author if author else "Banca y Negocios"
                    
                    # 如果没有摘要，尝试从正文提取纯文本前200字
                    if not item['summary'] or item['summary'] == item['title']:
                        soup = BeautifulSoup(cleaned_content, 'html.parser')
                        text = soup.get_text(strip=True)
                        item['summary'] = text[:200] + "..." if len(text) > 200 else text
                    
                    articles_data.append(item)
                    
                except Exception as e:
                    print(f"[Error] 处理详情页 {item['sourceUrl']} 失败: {e}")
            
        except Exception as e:
            print(f"[Fatal] 爬虫运行出错: {e}")
        finally:
            context.close()
            browser.close()
            
    return articles_data

def save_results(articles: list, output_dir: str):
    """
    保存结果为JSON
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"bancaynegocios_news_{timestamp}.json"
    output_path = os.path.join(output_dir, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[Success] 已保存 {len(articles)} 条新闻到: {output_path}")
    except Exception as e:
        print(f"[Error] 保存文件失败: {e}")

def main():
    print("=== 开始爬取 Banca y Negocios 新闻 ===")
    print(f"目标区域: Últimas Noticias (前 {MAX_ITEMS} 条)")
    
    articles = crawl_news()
    
    if articles:
        save_results(articles, OUTPUT_DIR)
    else:
        print("[Warn] 未抓取到任何数据")

if __name__ == "__main__":
    main()