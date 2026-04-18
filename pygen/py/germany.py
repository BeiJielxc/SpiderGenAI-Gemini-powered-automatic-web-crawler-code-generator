import os
import json
import time
import requests
from datetime import datetime
from urllib.parse import urljoin

# ================= 配置区域 =================
# 目标 API URL (从用户提供的网络请求中提取)
API_URL = "https://api.stage.bio/api/account/bundesbank/source/entry"
# Widget ID (从用户提供的网络请求中提取)
WIDGET_ID = "63aafa2172676c874ed99cce39464264"
# 请求数量 (用户要求前10条)
AMOUNT = 10

# 输出目录
OUTPUT_DIR = r"d:\llm_mcp_genpy\pygen\output"

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.news.bundesbank.de/",
    "Origin": "https://www.news.bundesbank.de"
}

def ensure_dir(path):
    """确保目录存在"""
    if not os.path.exists(path):
        os.makedirs(path)

def clean_html_content(html_content, base_url=""):
    """
    虽然API返回的是纯文本，但为了统一接口规范，保留此函数。
    此处主要用于处理我们自己构建的HTML内容。
    """
    return html_content

def format_timestamp(timestamp):
    """将Unix时间戳转换为YYYY-MM-DD格式"""
    if not timestamp:
        return ""
    try:
        dt = datetime.fromtimestamp(int(timestamp))
        return dt.strftime("%Y-%m-%d")
    except Exception as e:
        print(f"时间戳转换错误: {e}")
        return ""

def fetch_news_from_api():
    """从 API 获取新闻数据"""
    params = {
        "amount": AMOUNT,
        "widgetId": WIDGET_ID
    }
    
    print(f"正在请求 API: {API_URL}")
    try:
        response = requests.get(API_URL, headers=HEADERS, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        # API 返回的是一个列表
        if isinstance(data, list):
            return data
        else:
            print("API 返回格式非列表，可能结构已变")
            return []
            
    except requests.RequestException as e:
        print(f"API 请求失败: {e}")
        return []

def parse_news_item(item):
    """解析单条新闻数据"""
    try:
        # 1. 提取基础字段
        # 社交媒体帖子通常没有标题，使用内容的前50个字符作为标题
        content_text = item.get("content", "") or ""
        title = item.get("title")
        if not title:
            title = content_text[:80] + "..." if len(content_text) > 80 else content_text
            # 移除换行符以便作为标题显示
            title = title.replace("\n", " ")
        
        # 2. 提取日期
        # API 使用 original_created_at 作为发布时间戳
        timestamp = item.get("original_created_at")
        date_str = format_timestamp(timestamp)
        
        # 3. 提取来源和链接
        source_info = item.get("source", {})
        source_name = source_info.get("name", "Bundesbank Social Media")
        source_url = item.get("source_url", "")
        
        # 4. 构建正文内容 (HTML格式)
        # 将纯文本内容转换为 HTML 段落
        html_content = f"<p>{content_text.replace(chr(10), '<br>')}</p>"
        
        # 处理附件（图片）
        attachments = item.get("attachments", [])
        if attachments:
            html_content += '<div class="attachments">'
            for attachment in attachments:
                # 检查附件类型，通常是图片
                if attachment.get("type") == "image" or "url" in attachment:
                    img_url = attachment.get("url")
                    if img_url:
                        html_content += f'<img src="{img_url}" alt="Attachment" style="max-width:100%; margin-top:10px;">'
            html_content += '</div>'
            
        # 5. 提取摘要
        summary = content_text[:200] + "..." if len(content_text) > 200 else content_text
        
        return {
            "title": title.strip(),
            "date": date_str,
            "source": source_name,
            "author": source_info.get("handle", ""),
            "sourceUrl": source_url,
            "summary": summary.strip(),
            "content": html_content
        }
        
    except Exception as e:
        print(f"解析新闻条目出错: {e}")
        return None

def save_results(articles):
    """保存结果到 JSON 文件"""
    ensure_dir(OUTPUT_DIR)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"bundesbank_social_news_{timestamp}.json"
    output_path = os.path.join(OUTPUT_DIR, filename)
    
    result = {
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }
    
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"✅ 成功保存 {len(articles)} 条新闻到: {output_path}")
    except Exception as e:
        print(f"❌ 保存文件失败: {e}")

def main():
    print("🚀 开始爬取德国央行社交媒体新闻墙...")
    
    # 1. 获取数据
    raw_data = fetch_news_from_api()
    print(f"📦 API 返回了 {len(raw_data)} 条原始数据")
    
    # 2. 解析数据
    articles = []
    for item in raw_data:
        article = parse_news_item(item)
        if article:
            articles.append(article)
            
    # 3. 过滤和截取 (确保只取前10条，虽然API参数已限制，但双重保险)
    articles = articles[:AMOUNT]
    
    # 4. 保存结果
    if articles:
        save_results(articles)
    else:
        print("⚠️ 未提取到有效新闻数据")

if __name__ == "__main__":
    main()