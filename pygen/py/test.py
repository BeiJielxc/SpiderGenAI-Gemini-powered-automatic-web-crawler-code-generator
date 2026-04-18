#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
News Crawler Fallback Script
Target URL: https://www.cityam.com/news/

Note: This is a fallback template for news crawling.
Please modify the code according to actual page structure.
"""

import json
import os
import re
from datetime import datetime

# 使用 Playwright 处理动态页面
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    print("[WARN] Playwright not installed, trying requests...")
    import requests
    from bs4 import BeautifulSoup

# Configuration
BASE_URL = "https://www.cityam.com/news/"

def crawl_with_playwright():
    """使用 Playwright 爬取动态页面"""
    articles = []
    
    with sync_playwright() as p:
        # 使用内置反爬配置，不依赖 playwright-stealth 库
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        )
        page = context.new_page()

        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)  # 等待动态内容加载
            
            # 尝试绕过 WAF (简单滚动)
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)

            # 尝试多种常见的新闻列表选择器
            selectors = [
                'ul li a', '.news-list a', '.article-list a',
                '[class*="news"] a', '[class*="article"] a',
                '.list a', 'a[href*="article"]', 'a[href*="news"]'
            ]
            
            for selector in selectors:
                links = page.query_selector_all(selector)
                if len(links) > 3:  # 找到足够多的链接
                    for link in links[:50]:  # 最多取50条
                        try:
                            title = link.inner_text().strip()
                            href = link.get_attribute('href') or ''
                            
                            if title and len(title) > 5 and href:
                                # 补全相对链接
                                if href.startswith('/'):
                                    from urllib.parse import urljoin
                                    href = urljoin(BASE_URL, href)
                                
                                articles.append({
                                    "title": title,
                                    "sourceUrl": href,
                                    "date": datetime.now().strftime("%Y-%m-%d"),
                                    "source": "",
                                    "author": "",
                                    "summary": ""
                                })
                        except:
                            continue
                    
                    if articles:
                        break
        finally:
            browser.close()
    
    return articles

def crawl_with_requests():
    """使用 requests 爬取静态页面"""
    articles = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        resp = requests.get(BASE_URL, headers=headers, timeout=30)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 尝试多种选择器
        for link in soup.select('a')[:100]:
            title = link.get_text(strip=True)
            href = link.get('href', '')
            
            if title and len(title) > 10 and href:
                if href.startswith('/'):
                    from urllib.parse import urljoin
                    href = urljoin(BASE_URL, href)
                
                articles.append({
                    "title": title,
                    "sourceUrl": href,
                    "date": "",
                    "source": "",
                    "author": "",
                    "summary": ""
                })
    except Exception as e:
        print(f"[ERROR] {e}")
    
    return articles

def save_results(articles, output_dir):
    """保存结果为 JSON 格式"""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(output_dir, f"news_{timestamp}.json")
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"[OK] Saved {len(articles)} news to {filename}")
    return filename

def main():
    print(f"[INFO] Starting news crawl: {BASE_URL}")
    
    if HAS_PLAYWRIGHT:
        articles = crawl_with_playwright()
    else:
        articles = crawl_with_requests()
    
    if articles:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
        save_results(articles, output_dir)
        print(f"[SUCCESS] Crawled {len(articles)} news articles")
    else:
        print("[WARN] No news extracted, please check page structure")

if __name__ == "__main__":
    main()
