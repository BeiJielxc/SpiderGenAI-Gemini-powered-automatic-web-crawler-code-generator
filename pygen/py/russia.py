import os
import json
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime

class InterfaxNewsCrawler:
    def __init__(self):
        # 基础配置
        self.base_url = "https://www.interfax-russia.ru"
        self.list_url = "https://www.interfax-russia.ru/news"
        self.output_dir = r"d:\llm_mcp_genpy\pygen\output"
        
        # 反爬配置
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.interfax-russia.ru/"
        }
        
        # 确保输出目录存在
        os.makedirs(self.output_dir, exist_ok=True)

    def parse_russian_date(self, date_str):
        """
        解析俄语日期字符串，例如: "21 февраля 2026 г. 18:48"
        转换为: "2026-02-21"
        """
        if not date_str:
            return ""
            
        months = {
            'января': '01', 'февраля': '02', 'марта': '03', 'апреля': '04',
            'мая': '05', 'июня': '06', 'июля': '07', 'августа': '08',
            'сентября': '09', 'октября': '10', 'ноября': '11', 'декабря': '12'
        }
        
        try:
            # 移除 'г.' 和多余空格
            clean_str = date_str.replace('г.', '').replace(',', '').strip()
            parts = clean_str.split()
            
            # 尝试匹配 "DD month YYYY" 格式
            if len(parts) >= 3:
                day = parts[0].zfill(2)
                month_name = parts[1].lower()
                year = parts[2]
                
                # 如果月份是俄语单词
                if month_name in months:
                    month = months[month_name]
                    return f"{year}-{month}-{day}"
            
            # 如果无法解析，尝试返回原始字符串中的数字部分作为备选（或留空）
            return ""
        except Exception as e:
            print(f"日期解析警告: {e} -> {date_str}")
            return ""

    def clean_html_content(self, html_content, page_url):
        """
        清洗HTML内容：移除广告，修复相对路径
        """
        if not html_content:
            return ""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # 1. 移除广告和无关元素
            for tag in soup.find_all(['script', 'style', 'iframe']):
                tag.decompose()
            
            # 移除特定的广告 div (根据提供的HTML分析)
            for div in soup.find_all('div', class_=lambda x: x and ('banner' in x or 'adfox' in x)):
                div.decompose()
                
            # 2. 修复图片链接 (相对 -> 绝对)
            for img in soup.find_all('img'):
                if img.get('src'):
                    img['src'] = urljoin(page_url, img['src'])
                    # 移除懒加载占位符等无用属性
                    if img.get('data-src'):
                        img['src'] = urljoin(page_url, img['data-src'])
            
            # 3. 修复超链接 (相对 -> 绝对)
            for a in soup.find_all('a'):
                if a.get('href'):
                    a['href'] = urljoin(page_url, a['href'])
            
            return str(soup)
        except Exception as e:
            print(f"内容清洗出错: {e}")
            return html_content

    def get_news_links(self):
        """
        获取新闻列表页的前10条新闻链接
        """
        print(f"正在访问列表页: {self.list_url}")
        links = []
        try:
            response = requests.get(self.list_url, headers=self.headers, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 策略 1: 尝试用户建议的选择器 (针对标准列表页)
            items = soup.select('.list-unstyled.lenta-all-news > li')
            
            # 策略 2: 如果策略1未找到，尝试侧边栏或通用新闻列表结构 (根据提供的HTML推断)
            if not items:
                items = soup.select('.lenta-news > li')
            
            # 策略 3: 兜底策略，查找所有看起来像新闻详情的链接
            if not items:
                print("未找到标准列表结构，使用通用链接提取模式...")
                all_links = soup.find_all('a', href=True)
                seen = set()
                for a in all_links:
                    href = a['href']
                    # 过滤逻辑：包含 /news/ 且路径深度足够，排除纯分类链接
                    if '/news/' in href and len(href.split('/')) > 3:
                        full_url = urljoin(self.base_url, href)
                        if full_url not in seen:
                            links.append(full_url)
                            seen.add(full_url)
            else:
                # 从找到的 li 标签中提取链接
                for item in items:
                    a = item.find('a')
                    if a and a.get('href'):
                        full_url = urljoin(self.base_url, a['href'])
                        if full_url not in links:
                            links.append(full_url)

            # 截取前10条
            target_links = links[:10]
            print(f"成功获取 {len(target_links)} 条新闻链接")
            return target_links
            
        except Exception as e:
            print(f"获取新闻列表失败: {e}")
            return []

    def parse_detail_page(self, url):
        """
        解析新闻详情页
        """
        print(f"正在抓取详情: {url}")
        try:
            time.sleep(1.5) # 礼貌延时
            response = requests.get(url, headers=self.headers, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 1. 提取标题 (h1)
            title = ""
            h1 = soup.find('h1')
            if h1:
                title = h1.get_text(strip=True)
            
            # 2. 提取日期
            # 优先尝试 meta 标签 (ISO 格式)
            date_str = ""
            meta_date = soup.find('meta', property="article:published_time")
            if meta_date:
                raw_date = meta_date.get('content', '')
                if 'T' in raw_date:
                    date_str = raw_date.split('T')[0]
                else:
                    date_str = raw_date
            
            # 如果 meta 失败，尝试从页面文本提取 (俄语格式)
            if not date_str:
                # 根据提供的HTML，日期在 span.news-datetime
                date_tag = soup.select_one('.news-datetime')
                if date_tag:
                    date_str = self.parse_russian_date(date_tag.get_text(strip=True))
            
            # 3. 提取正文
            # 根据提供的HTML，正文在 .editor-content
            content_html = ""
            content_div = soup.select_one('.editor-content')
            
            # 备选选择器
            if not content_div:
                content_div = soup.find('div', itemprop='articleBody')
            if not content_div:
                content_div = soup.select_one('.article-body')
                
            if content_div:
                content_html = self.clean_html_content(str(content_div), url)
            
            # 4. 提取摘要
            summary = ""
            meta_desc = soup.find('meta', attrs={"name": "description"})
            if meta_desc:
                summary = meta_desc.get('content', '')
            
            # 如果没有摘要，从正文截取
            if not summary and content_div:
                summary = content_div.get_text(strip=True)[:200] + "..."

            # 5. 组装数据
            article = {
                "title": title,
                "date": date_str,
                "source": "Interfax-Russia",
                "author": "Interfax", # 默认作者
                "sourceUrl": url,
                "summary": summary,
                "content": content_html
            }
            
            # 简单验证
            if not title:
                print(f"警告: 未找到标题 - {url}")
            
            return article

        except Exception as e:
            print(f"解析详情页出错 {url}: {e}")
            return None

    def save_results(self, articles):
        """
        保存结果为 JSON
        """
        if not articles:
            print("没有数据需要保存")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"interfax_news_{timestamp}.json"
        output_path = os.path.join(self.output_dir, filename)
        
        result = {
            "total": len(articles),
            "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "articles": articles
        }
        
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"成功保存 {len(articles)} 条新闻到: {output_path}")
        except Exception as e:
            print(f"保存文件失败: {e}")

    def run(self):
        """
        主运行逻辑
        """
        print("=== 开始爬取 Interfax-Russia ===")
        
        # 1. 获取列表
        links = self.get_news_links()
        if not links:
            print("未获取到任何链接，程序终止")
            return

        # 2. 遍历详情页
        articles = []
        for i, link in enumerate(links):
            print(f"[{i+1}/{len(links)}] 处理中...")
            article = self.parse_detail_page(link)
            if article:
                article['id'] = str(i + 1)
                articles.append(article)
        
        # 3. 保存结果
        self.save_results(articles)
        print("=== 爬取完成 ===")

if __name__ == "__main__":
    crawler = InterfaxNewsCrawler()
    crawler.run()