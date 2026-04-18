"""
LLM代理模块 - PyGen爬虫脚本生成核心

使用大语言模型分析页面结构，生成独立可运行的Python爬虫脚本。
增强版：支持SPA页面分类参数识别和处理。

架构增强 v2.0:
- 集成 Validator + Signals Collector + Failure Classifier
- 结构化错误案例 Few-shot 注入
- 自动修复循环

架构增强 v3.0:
- 支持多模态输入（图片附件）
- 支持 Gemini API
- 新闻舆情场景支持

架构增强 v3.1:
- 支持 Anthropic Claude API（Claude Sonnet/Opus）
"""
import json
import re
import base64
import codecs
import requests
from bs4 import BeautifulSoup, Comment
from typing import Dict, Any, List, Optional, Tuple
from openai import OpenAI
from pathlib import Path

# 导入增强模块
try:
    from error_cases import get_error_cases_prompt, ErrorSeverity
    from validator import StaticCodeValidator, validate_code
    from failure_classifier import FailureClassifier, FailureReport, FailureType
    from post_processor import apply_conditional_post_processing
    from prompts import load as load_prompt
except ImportError:
    # 作为包导入时使用相对导入
    from .error_cases import get_error_cases_prompt, ErrorSeverity
    from .validator import StaticCodeValidator, validate_code
    from .failure_classifier import FailureClassifier, FailureReport, FailureType
    from .post_processor import apply_conditional_post_processing
    from .prompts import load as load_prompt


# 附件数据类型
class AttachmentData:
    """图片/文件附件"""
    def __init__(self, filename: str, base64_data: str, mime_type: str):
        self.filename = filename
        self.base64 = base64_data
        self.mime_type = mime_type


class LLMAgent:
    """LLM智能代理 - 爬虫脚本生成器（增强版 v3.0 - 多模态支持）"""

    def __init__(
        self,
        api_key: str,
        model: str = "qwen-max",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        max_repair_attempts: int = 2,
        enable_error_cases: bool = True,
        enable_auto_repair: bool = True,
        provider: str = "openai"  # 'openai' | 'gemini' | 'claude'
    ):
        """
        初始化LLM代理

        Args:
            api_key: API Key
            model: 模型名称
            base_url: API基础URL
            max_repair_attempts: 最大修复尝试次数
            enable_error_cases: 是否在 prompt 中注入错误案例
            provider: API 提供商 ('openai' 兼容模式、'gemini' 或 'claude')
        """
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.max_repair_attempts = max_repair_attempts
        self.enable_error_cases = enable_error_cases
        # 是否启用“验证+自动修复循环”。关闭时将直接返回模型原始生成代码。
        self.enable_auto_repair = enable_auto_repair
        self.provider = provider
        # 用于调试：标记当前调用所属任务（由上层传入）
        self._task_id: Optional[str] = None

        # 检测 API 提供商
        if 'gemini' in model.lower() or 'generativelanguage.googleapis.com' in base_url:
            self.provider = 'gemini'
            self.client = None  # Gemini 使用 REST API
        elif 'claude' in model.lower() or 'anthropic.com' in base_url:
            self.provider = 'claude'
            self.client = None  # Claude 使用 REST API
        else:
            self.provider = 'openai'
            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url
            )

        # Token统计
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        
        # 验证器和分类器
        self.code_validator = StaticCodeValidator()
        self.failure_classifier = FailureClassifier(llm_client=self.client)

    def _dbg_prefix(self) -> str:
        """统一的调试前缀，便于区分并发任务日志"""
        return f"[DEBUG][task={self._task_id}]" if self._task_id else "[DEBUG]"

    def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        attachments: Optional[List[AttachmentData]] = None,
        temperature: float = 0.2
    ) -> str:
        """
        统一的 LLM 调用接口，支持 OpenAI 兼容模式、Gemini API 和 Claude API
        
        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            attachments: 图片附件列表（多模态）
            temperature: 生成温度
            
        Returns:
            LLM 生成的文本
        """
        if self.provider == 'gemini':
            return self._call_gemini(system_prompt, user_prompt, attachments, temperature)
        elif self.provider == 'claude':
            return self._call_claude(system_prompt, user_prompt, attachments, temperature)
        else:
            return self._call_openai(system_prompt, user_prompt, attachments, temperature)
    
    def _call_openai(
        self,
        system_prompt: str,
        user_prompt: str,
        attachments: Optional[List[AttachmentData]] = None,
        temperature: float = 0.2
    ) -> str:
        """OpenAI 兼容模式调用"""
        print(f"{self._dbg_prefix()} 正在调用 OpenAI 兼容 API (model={self.model})...")
        
        # 显示附件接收状态
        if attachments:
            print(f"{self._dbg_prefix()} ✓ 收到 {len(attachments)} 个附件:")
            for i, att in enumerate(attachments):
                size_kb = len(att.base64) * 3 // 4 // 1024
                print(f"{self._dbg_prefix()}   [{i+1}] {att.filename} ({att.mime_type}, ~{size_kb}KB)")
        else:
            print(f"{self._dbg_prefix()} ⚠ 未收到任何附件/截图")
        
        messages = [
            {"role": "system", "content": system_prompt}
        ]
        
        # 构建用户消息（支持多模态）
        if attachments:
            user_content = [{"type": "text", "text": user_prompt}]
            for att in attachments:
                if att.mime_type.startswith('image/'):
                    user_content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{att.mime_type};base64,{att.base64}"
                        }
                    })
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": user_prompt})
        
        # kimi-k2.5 模型要求 temperature=1，其他模型使用传入的 temperature
        final_temperature = 1.0 if self.model == "kimi-k2.5" else temperature
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=final_temperature
        )
        
        # 统计 Token
        if hasattr(response, 'usage') and response.usage:
            self.total_prompt_tokens += response.usage.prompt_tokens
            self.total_completion_tokens += response.usage.completion_tokens
        
        return response.choices[0].message.content
    
    def _call_gemini(
        self,
        system_prompt: str,
        user_prompt: str,
        attachments: Optional[List[AttachmentData]] = None,
        temperature: float = 0.2
    ) -> str:
        """Gemini API 调用（REST API）- 支持流式响应以避免超时"""
        # 使用 streamGenerateContent 接口
        url = f"{self.base_url}models/{self.model}:streamGenerateContent?key={self.api_key}"
        
        print(f"{self._dbg_prefix()} Gemini API URL: {url[:80]}... (流式模式)")
        
        # 显示附件接收状态
        if attachments:
            print(f"{self._dbg_prefix()} ✓ 收到 {len(attachments)} 个附件:")
            for i, att in enumerate(attachments):
                size_kb = len(att.base64) * 3 // 4 // 1024
                print(f"{self._dbg_prefix()}   [{i+1}] {att.filename} ({att.mime_type}, ~{size_kb}KB)")
        else:
            print(f"{self._dbg_prefix()} ⚠ 未收到任何附件/截图")
        
        # 构建请求体 - 使用 Gemini 的标准格式
        payload = {
            "contents": [{
                "parts": [{"text": user_prompt}]
            }],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": 16384
            }
        }
        
        # 添加系统指令（Gemini 2.0+ 支持 systemInstruction）
        payload["systemInstruction"] = {
            "parts": [{"text": system_prompt}]
        }
        
        # 添加图片到内容中
        if attachments:
            for att in attachments:
                if att.mime_type.startswith('image/'):
                    payload["contents"][0]["parts"].append({
                        "inlineData": {
                            "mimeType": att.mime_type,
                            "data": att.base64
                        }
                    })
                    print(f"[DEBUG] 添加图片附件: {att.mime_type}")
        
        headers = {"Content-Type": "application/json"}
        
        try:
            print(f"[DEBUG] 正在调用 Gemini API (model={self.model}, stream=True)...")
            # 开启 stream=True 建立长连接
            response = requests.post(url, headers=headers, json=payload, timeout=180, stream=True)
            
            print(f"[DEBUG] Gemini 响应状态码: {response.status_code}")
            
            if response.status_code != 200:
                # 如果状态码错误，尝试读取部分内容作为错误信息
                try:
                    error_text = response.text[:500]
                except:
                    error_text = "Unknown error"
                raise Exception(f"Gemini API error: {response.status_code} - {error_text}")
            
            # 增量接收并拼装响应
            full_text = ""
            chunk_count = 0
            
            # Gemini 流式返回的是 JSON 数组结构: [{...}, {...}]
            # requests.iter_lines() 会按行读取，我们需要解析这些 JSON 对象
            # 格式通常是: "[" (第一行), "{" (对象开始)...
            # 但 REST API 可能返回紧凑的 JSON 数组。
            # 更稳妥的方式是：手动处理 JSON 对象流。
            
            # 简单处理：累积所有文本然后解析，或者尝试增量解析
            # 由于 iter_lines 处理 JSON 数组比较麻烦，我们先尝试直接解析 response.json() 
            # 但 response.json() 会等待整个响应结束，可能无法解决超时问题。
            # 正确做法是处理 stream。Gemini 的 REST stream 返回一系列 JSON 对象，以逗号分隔，包裹在 [] 中。
            
            buffer = ""
            # 使用增量解码器处理可能被分割的多字节 UTF-8 字符
            decoder = codecs.getincrementaldecoder('utf-8')('replace')
            for chunk in response.iter_content(chunk_size=None):
                if not chunk:
                    continue
                # 增量解码：自动处理跨 chunk 的多字节字符
                chunk_str = decoder.decode(chunk, final=False)
                buffer += chunk_str
                
                # 尝试从 buffer 中提取完整的 JSON 对象
                # 注意：Gemini 返回的是一个 JSON 列表，首尾有 [ ]，中间用 , 分隔
                # 这是一个简化的增量解析器
                while True:
                    # 查找可能的 JSON 对象结束位置
                    # 实际流中每个 chunk 往往是一个完整的 candidate 对象（但也可能被截断）
                    # 为了稳健，我们这里做一个简单的全量累积，因为 requests.stream=True 已经
                    # 保证了连接是活跃的 (Active)，不会因为 TTFB 过长被断开。
                    # 只要数据在传输，代理就不会断开。
                    # 所以我们可以只累积 buffer，最后一次性解析（或者分块解析以显示进度）。
                    break
            
            # 刷新解码器中可能残留的字节
            buffer += decoder.decode(b'', final=True)
            
            # 流传输完成后，buffer 中包含完整的 JSON 数组字符串
            try:
                # 清理可能的前后空白
                json_str = buffer.strip()
                # 尝试修正可能的截断（虽然 stream=True 应该接收完整的）
                results = json.loads(json_str)
            except json.JSONDecodeError as e:
                # 尝试处理常见的流式格式问题（如缺少闭合括号）
                print(f"[WARN] JSON 解析失败，尝试修复: {e}")
                if not json_str.endswith(']'):
                    json_str += ']'
                try:
                    results = json.loads(json_str)
                except:
                    raise Exception(f"Gemini 响应流解析失败 (长度: {len(buffer)})")

            # 遍历所有候选项拼装完整文本
            for result in results:
                # 检查是否有安全阻止
                if 'promptFeedback' in result:
                    feedback = result['promptFeedback']
                    if feedback.get('blockReason'):
                        print(f"[WARN] 部分内容被阻止: {feedback.get('blockReason')}")
                
                if 'candidates' in result and len(result['candidates']) > 0:
                    candidate = result['candidates'][0]
                    if 'content' in candidate and 'parts' in candidate['content']:
                        text_parts = [p.get('text', '') for p in candidate['content']['parts'] if 'text' in p]
                        full_text += ''.join(text_parts)
            
            # 统计大致 token 数（估算）
            estimated_tokens = len(full_text) // 4
            self.total_completion_tokens += estimated_tokens
            print(f"[DEBUG] Gemini 生成文本总长度: {len(full_text)} 字符")
            
            if not full_text:
                raise Exception("Gemini 响应为空")
                
            return full_text
            
        except requests.exceptions.Timeout:
            raise Exception("Gemini API 请求超时（180秒）")
        except requests.exceptions.ConnectionError as e:
            raise Exception(f"Gemini API 连接错误: {e}")

    def _call_claude(
        self,
        system_prompt: str,
        user_prompt: str,
        attachments: Optional[List[AttachmentData]] = None,
        temperature: float = 0.2
    ) -> str:
        """Anthropic Claude API 调用（REST API）"""
        # Claude API 端点
        url = "https://api.anthropic.com/v1/messages"
        
        print(f"{self._dbg_prefix()} Claude API URL: {url}")
        print(f"{self._dbg_prefix()} 正在调用 Claude API (model={self.model})...")
        
        # 显示附件接收状态
        if attachments:
            print(f"{self._dbg_prefix()} ✓ 收到 {len(attachments)} 个附件:")
            for i, att in enumerate(attachments):
                # 计算图片大小（base64 转实际大小约 3/4）
                size_kb = len(att.base64) * 3 // 4 // 1024
                print(f"{self._dbg_prefix()}   [{i+1}] {att.filename} ({att.mime_type}, ~{size_kb}KB)")
        else:
            print(f"{self._dbg_prefix()} ⚠ 未收到任何附件/截图")
        
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01"
        }
        
        # 构建消息内容
        user_content = []
        image_count = 0
        
        # 添加图片（如果有）
        if attachments:
            for att in attachments:
                if att.mime_type.startswith('image/'):
                    user_content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": att.mime_type,
                            "data": att.base64
                        }
                    })
                    image_count += 1
            print(f"{self._dbg_prefix()} ✓ 已将 {image_count} 张图片添加到 Claude 请求中")
        
        # 添加文本
        user_content.append({
            "type": "text",
            "text": user_prompt
        })
        
        payload = {
            "model": self.model,
            "max_tokens": 16384,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_content}
            ]
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=180)
            
            print(f"{self._dbg_prefix()} Claude 响应状态码: {response.status_code}")
            
            if response.status_code != 200:
                error_text = response.text[:500] if len(response.text) > 500 else response.text
                raise Exception(f"Claude API error: {response.status_code} - {error_text}")
            
            result = response.json()
            
            # 提取生成的文本
            content = result.get("content", [])
            if content and len(content) > 0:
                text_parts = [block.get("text", "") for block in content if block.get("type") == "text"]
                generated_text = ''.join(text_parts)
                
                # 统计 Token
                usage = result.get("usage", {})
                if usage:
                    self.total_prompt_tokens += usage.get("input_tokens", 0)
                    self.total_completion_tokens += usage.get("output_tokens", 0)
                
                print(f"{self._dbg_prefix()} Claude 生成文本长度: {len(generated_text)} 字符")
                return generated_text
            
            raise Exception(f"Claude API response format error: {str(result)[:500]}")
            
        except requests.exceptions.Timeout:
            raise Exception("Claude API 请求超时（180秒）")
        except requests.exceptions.ConnectionError as e:
            raise Exception(f"Claude API 连接错误: {e}")

    def generate_crawler_script(
        self,
        page_url: str,
        page_html: str,
        page_structure: Dict[str, Any],
        network_requests: Dict[str, List[Dict[str, Any]]],
        user_requirements: Optional[str] = None,
        start_date: str = "",
        end_date: str = "",
        enhanced_analysis: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[AttachmentData]] = None,
        run_mode: str = "enterprise_report",
        crawl_mode: str = "single_page",
        task_id: Optional[str] = None
    ) -> str:
        """
        分析页面并生成独立可运行的Python爬虫脚本

        Args:
            page_url: 目标页面URL
            page_html: 完整的页面HTML
            page_structure: 页面结构分析结果
            network_requests: 捕获的网络请求
            user_requirements: 用户任务目标（可选，最高优先级）
            start_date: 爬取开始时间（YYYY-MM-DD）
            end_date: 爬取结束时间（YYYY-MM-DD）
            enhanced_analysis: 增强分析结果（包含数据状态、交互API、参数分析）
            attachments: 图片附件（多模态输入，用户提供的截图）
            run_mode: 运行模式 ('enterprise_report' | 'news_sentiment')
            crawl_mode: 爬取模式 ('single_page' | 'multi_page' | 'auto_detect')

        Returns:
            生成的Python爬虫脚本代码
        """

        # 记录任务ID（便于区分并发任务日志）
        self._task_id = task_id
        print(f"{self._dbg_prefix()} >>> generate_crawler_script 入口")

        # 准备API请求信息（重点关注）
        print(f"{self._dbg_prefix()} 准备 API 信息...")
        api_info = self._extract_api_info(network_requests, enhanced_analysis)
        print(f"{self._dbg_prefix()} API 信息提取完成")

        # 准备页面结构摘要
        structure_summary = self._summarize_structure(page_structure)
        
        # 准备增强分析摘要
        enhanced_summary = self._summarize_enhanced_analysis(enhanced_analysis) if enhanced_analysis else ""

        system_prompt = self._build_system_prompt(run_mode=run_mode, crawl_mode=crawl_mode)
        user_prompt = self._build_user_prompt(
            page_url=page_url,
            page_html=page_html,
            structure_summary=structure_summary,
            api_info=api_info,
            user_requirements=user_requirements,
            start_date=start_date,
            end_date=end_date,
            enhanced_summary=enhanced_summary
        )

        def _needs_rendered_dom_dates() -> bool:
            """当 API 无有效日期且页面提示存在“日期-条目关联样本”/SPA线索时，强制要求用浏览器提取日期。"""
            try:
                has_samples = bool((page_structure or {}).get("dateItemSamples"))
                spa = (page_structure or {}).get("spaHints") or {}
                is_spa = bool(spa.get("hasHashRoute") or spa.get("hasAppRoot"))
                return has_samples or is_spa
            except Exception:
                return False

        def _script_looks_wrong_for_spa_dates(code: str) -> bool:
            """
            轻量质量闸门：如果需要渲染后DOM日期，却生成了“requests 抓站点主页/正则 span.list-time”等典型错误模式，则触发一次重试。
            只做非常保守的判断，避免误伤正常脚本。
            """
            if not code:
                return True
            c = code.lower()
            # 明显用 requests 去抓主页/根路径来抽日期（SPA常见失败）
            bad_requests_html = ("requests.get(" in c) and ("list-time" in c or "span.list-time" in c)
            # 没有任何 playwright 相关 import/使用
            no_playwright = ("playwright" not in c) and ("sync_playwright" not in c) and ("async_playwright" not in c)
            # 同时出现“从html提取日期”的语义
            mentions_html_dates = ("html" in c and "date" in c) or ("从html" in code)
            return bad_requests_html and no_playwright and mentions_html_dates

        def _script_looks_like_wrong_output_schema(code: str) -> bool:
            """
            轻量质量闸门：输出 schema 必须包含 name 字段。
            如果脚本明显在 reports 里写入 title 而不是 name，触发一次重试以提升泛化稳定性。
            """
            if not code:
                return True
            c = code.lower()
            # 常见错误：把记录写成 {"title": ...} 而不是 {"name": ...}
            uses_title_key = '"title"' in c or "'title'" in c
            uses_name_key = '"name"' in c or "'name'" in c
            # 兼容策略：title 可视为 name 的别名（后端解析会归一化为 name），不应触发重试/修复
            # 仍保留原意：如果既没有 name 也没有 title 才算“schema 可疑”
            if uses_name_key or uses_title_key:
                return False
            return True

        def _script_looks_like_keeps_undated_records(code: str) -> bool:
            """
            质量闸门：当要求按日期范围过滤时，脚本不应“保留无日期记录”。
            这里用启发式检测典型错误分支：elif not date_str: ... append(...)
            """
            if not code:
                return False
            c = code.lower()
            patterns = [
                "elif not date_str",
                "if not date_str",
                "date_str and start_date <= date_str <= end_date",
                "filtered_reports.append(report)",
                "all_reports.append(report)",
                "已保留",
                "无日期",
            ]
            hit = sum(1 for p in patterns if p in c)
            # 保守：同时出现“无日期/保留”+ append 更可疑
            return ("无日期" in code and "append(" in c) or hit >= 5

        def _script_looks_like_brittle_html_parsing(code: str) -> bool:
            """
            质量闸门：拦截典型的 BeautifulSoup 链式 find().find_all() 空指针写法，
            避免运行时报：'NoneType' object has no attribute 'find_all'。
            """
            if not code:
                return False
            c = code.lower()
            # 极常见坑：table.find('tbody').find_all('tr')
            if ".find('tbody').find_all(" in c or ".find(\"tbody\").find_all(" in c:
                return True
            # 更泛化：任意 .find(...).find_all(...) 链式
            if ".find(" in c and ").find_all(" in c:
                # 只要脚本里同时用到了 bs4/BeautifulSoup，就认为风险很高
                if "beautifulsoup" in c or "from bs4 import" in c:
                    return True
            return False

        def _script_looks_like_hardcoded_date_column(code: str) -> bool:
            """
            质量闸门：拦截硬编码列索引提取日期的脆弱写法。
            不同网站表格结构差异大，日期可能在任意列位置。
            """
            if not code:
                return False
            
            # 如果使用了注入的智能日期扫描函数，则认为是安全的
            if "_pygen_smart_find_date_in_row" in code:
                return False
            
            # 检测硬编码列索引提取日期的模式
            import re
            # 匹配 tds[数字] 后跟日期相关操作
            hardcoded_patterns = [
                r"tds\[\d+\]\.query_selector\(['\"]span['\"]\)",  # Playwright: tds[4].query_selector('span')
                r"tds\[\d+\]\.select_one\(['\"]span['\"]\)",      # BS4: tds[4].select_one('span')
                r"tds\[\d+\]\.get_text\(",                        # tds[3].get_text()
                r"tds\[\d+\]\.inner_text\(",                      # tds[4].inner_text()
            ]
            
            for pattern in hardcoded_patterns:
                if re.search(pattern, code):
                    # 进一步检查是否是日期提取上下文
                    # 查找附近是否有 date 相关变量名
                    if "date" in code.lower():
                        return True
            
            return False

        try:
            # =====================================================================
            # 第一次生成（支持多模态）
            # =====================================================================
            # 如果有图片附件，添加详细的提示说明
            if attachments:
                attachment_hint = load_prompt("codegen/shared/attachment_hint.md")
                # 将截图约束放到 user_prompt 最前面（优先级最高）
                user_prompt = attachment_hint.strip() + "\n\n" + user_prompt
                print(f"{self._dbg_prefix()} 已添加截图提示到 prompt，附件数量: {len(attachments)}")
            
            content = self._call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                attachments=attachments,
                temperature=0.2
            )
            
            script = self._extract_code_from_response(content)

            # =====================================================================
            # 可选：关闭自动修复/验证（直接返回 LLM 原始代码）
            # =====================================================================
            if not self.enable_auto_repair:
                print(f"{self._dbg_prefix()} ⚠ 已关闭自动修复/验证：直接返回 LLM 原始生成代码")
                print(f"{self._dbg_prefix()} <<< generate_crawler_script 即将返回 (auto_repair=false)，脚本长度: {len(script)}")
                return script

            # =====================================================================
            # 步骤1：预检查（获取问题列表，用于决定后处理）
            # =====================================================================
            pre_issues = self.code_validator.validate(script, page_structure=page_structure)
            
            # =====================================================================
            # 步骤2：条件性后处理（根据问题决定注入哪些增强代码）
            # =====================================================================
            # 后处理放在 LLM 修复之前，为后续修复提供工具函数
            script, injection_log = apply_conditional_post_processing(
                script_code=script,
                issues=pre_issues,
                page_structure=page_structure
            )
            
            if injection_log:
                print(f"{self._dbg_prefix()} 🔧 条件性后处理：")
                for log in injection_log:
                    print(f"{self._dbg_prefix()}   - {log}")
            
            # =====================================================================
            # 步骤3：LLM 修复循环（基于后处理后的代码）
            # =====================================================================
            # 检测是否捕获到 API 请求
            api_requests = network_requests.get("api_requests", [])
            has_api_requests = len(api_requests) > 0
            
            script, repair_log = self._validate_and_repair(
                script=script,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                page_structure=page_structure,
                context_checks={
                    "needs_rendered_dom_dates": _needs_rendered_dom_dates(),
                    "has_api_requests": has_api_requests,
                }
            )
            
            # 记录检查/修复日志（可用于后续分析）
            # 注意：repair_log 既可能包含“触发修复”的原因，也可能只是“存在警告但未修复”。
            if repair_log:
                print(f"{self._dbg_prefix()} 🔎 代码检查/修复日志：")
                for log in repair_log:
                    print(f"{self._dbg_prefix()}   - {log}")

            print(f"{self._dbg_prefix()} <<< generate_crawler_script 即将返回，脚本长度: {len(script)}")
            return script

        except Exception as e:
            print(f"{self._dbg_prefix()} !!! generate_crawler_script 抛出异常: {type(e).__name__}: {e}")
            print(f"❌ LLM调用失败: {e}")
            import traceback
            traceback.print_exc()
            return self._generate_fallback_script(page_url, run_mode=run_mode)
    
    def _validate_and_repair(
        self,
        script: str,
        system_prompt: str,
        user_prompt: str,
        page_structure: Dict[str, Any],
        context_checks: Dict[str, bool]
    ) -> Tuple[str, List[str]]:
        """
        验证代码并尝试修复
        
        Args:
            script: 生成的代码
            system_prompt: 系统提示
            user_prompt: 用户提示
            page_structure: 页面结构
            context_checks: 上下文相关检查结果
            
        Returns:
            (最终代码, 修复日志列表)
        """
        repair_log = []
        current_script = script
        
        for attempt in range(self.max_repair_attempts):
            # 使用验证器检查代码（传入 page_structure 进行上下文感知检查）
            issues = self.code_validator.validate(current_script, page_structure=page_structure)
            
            # 加入上下文相关的检查
            context_issues = self._check_context_issues(current_script, context_checks)
            
            # 合并问题
            all_issues = issues + context_issues
            
            # 如果没有错误级别的问题，返回当前代码
            has_errors = any(i.severity.value == "error" for i in all_issues)
            if not has_errors:
                if all_issues:
                    # 有警告但无错误：把警告也打印出来，避免用户觉得“莫名其妙修复/没原因”。
                    warnings = [i for i in all_issues if i.severity.value == "warning"]
                    infos = [i for i in all_issues if i.severity.value == "info"]
                    repair_log.append(
                        f"第{attempt+1}轮: 检查通过（{len(warnings)}个警告{'' if not infos else f'，{len(infos)}个提示'}）"
                    )
                    for w in warnings[:10]:
                        msg = f"- [{w.code}] {w.message}"
                        if w.suggestion:
                            msg += f"（建议: {w.suggestion}）"
                        repair_log.append(msg)
                    for inf in infos[:5]:
                        msg = f"- [{inf.code}] {inf.message}"
                        if getattr(inf, "suggestion", ""):
                            msg += f"（建议: {inf.suggestion}）"
                        repair_log.append(msg)
                break
            
            # 有错误，尝试修复
            error_issues = [i for i in all_issues if i.severity.value == "error"]
            repair_log.append(f"第{attempt+1}轮: 发现{len(error_issues)}个错误，尝试修复")
            # 记录触发修复的具体原因（便于定位）
            if error_issues:
                repair_log.append("触发修复的原因：")
                for i in error_issues:
                    msg = f"- [{i.code}] {i.message}"
                    if i.suggestion:
                        msg += f"（建议: {i.suggestion}）"
                    repair_log.append(msg)
            
            # 生成修复提示（传入 page_structure，让 LLM 知道正确的页面结构）
            repair_prompt = self._build_repair_prompt(all_issues, current_script, page_structure)
            
            # 调用 LLM 修复
            try:
                repair_response = self._call_llm(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt + "\n\n" + repair_prompt,
                    attachments=None,  # 修复时不需要图片
                    temperature=0.1  # 低温度提高稳定性
                )
                
                new_script = self._extract_code_from_response(repair_response)
                
                if new_script and new_script != current_script:
                    current_script = new_script
                else:
                    repair_log.append(f"第{attempt+1}轮: 修复未产生变化，停止")
                    break
                    
            except Exception as e:
                repair_log.append(f"第{attempt+1}轮: 修复调用失败 - {str(e)}")
                break
        
        return current_script, repair_log
    
    def _check_context_issues(
        self,
        code: str,
        context_checks: Dict[str, bool]
    ) -> List:
        """检查上下文相关的问题"""
        from validator import CodeIssue, IssueSeverity
        
        issues = []
        code_lower = code.lower()
        
        # 检查0: Windows 兼容性 (Emoji 检测)
        # 检测代码中是否包含可能导致 Windows GBK 终端崩溃的 Emoji 字符
        # 主要是超出基本多语言平面(BMP)的字符，即 ord > 65535 (如 🚀 \U0001f680)
        has_emoji = False
        for char in code:
            if ord(char) > 0xFFFF:
                has_emoji = True
                break
        
        if has_emoji and "print" in code_lower:
            issues.append(CodeIssue(
                code="WIN_COMPAT_001",
                severity=IssueSeverity.ERROR,
                message="代码中包含 Emoji 或特殊字符 (ord > 65535)，在 Windows 控制台输出会导致 UnicodeEncodeError 崩溃。",
                suggestion="请移除 print() 语句中的所有 Emoji 表情（如 🚀, ✅, ❌ 等），仅使用文本符号。"
            ))

        # 检查1: SPA 日期提取问题
        if context_checks.get("needs_rendered_dom_dates"):
            # 如果需要渲染后DOM日期，但代码用 requests 抓 HTML
            bad_requests_html = ("requests.get(" in code_lower) and ("list-time" in code_lower or "span.list-time" in code_lower)
            no_playwright = ("playwright" not in code_lower) and ("sync_playwright" not in code_lower)
            if bad_requests_html and no_playwright:
                issues.append(CodeIssue(
                    code="CTX_001",
                    severity=IssueSeverity.ERROR,
                    message="SPA 页面需要用 Playwright 提取日期，但代码使用了 requests",
                    suggestion="改用 Playwright 从渲染后 DOM 提取日期"
                ))
        
        # 检查2: 分页场景下日期可能只处理第一页
        if "for page" in code_lower or "while" in code_lower:
            if "extract_date" in code_lower:
                # 简单启发：日期提取函数是否在分页循环内
                lines = code.split('\n')
                in_loop = False
                date_in_loop = False
                
                for line in lines:
                    if 'for ' in line.lower() and 'page' in line.lower():
                        in_loop = True
                    if in_loop and ('extract_date' in line.lower() or '_pygen_smart_find_date' in line):
                        date_in_loop = True
                        break
                
                if not date_in_loop and 'extract_date' in code_lower:
                    issues.append(CodeIssue(
                        code="CTX_002",
                        severity=IssueSeverity.WARNING,
                        message="日期提取可能不在分页循环内，会导致只有第一页有日期",
                        suggestion="在每页数据获取时同步提取日期"
                    ))
        
        # 检查3: 有 API 请求可用但错误使用 HTML 解析
        if context_checks.get("has_api_requests"):
            # 检测是否使用 BeautifulSoup 解析 HTML 而不是调用 API
            uses_beautifulsoup = "beautifulsoup" in code_lower or "from bs4" in code_lower
            parses_table_html = "table" in code_lower and ("tbody" in code_lower or "tr" in code_lower)
            uses_requests_get_html = "requests.get(" in code_lower and ".text" in code_lower
            
            # 检查是否调用了 API（返回 JSON）
            uses_api = ".json()" in code_lower
            
            # 如果使用 BeautifulSoup 解析 HTML，但有 API 可用且没有调用 API
            if uses_beautifulsoup and parses_table_html and uses_requests_get_html and not uses_api:
                issues.append(CodeIssue(
                    code="ERR_011",
                    severity=IssueSeverity.ERROR,
                    message="检测到 API 请求可用，但代码使用 BeautifulSoup 解析 HTML 而非调用 API",
                    suggestion="页面数据通过 API 动态加载，必须使用 requests 调用 API 获取 JSON 数据，而不是解析 HTML"
                ))
        
        # 检查4: CATEGORIES 字典中分类参数重复（致命逻辑错误）
        # 检测模式：CATEGORIES = { "xxx": {...}, "yyy": {...} } 其中多个分类的参数值完全相同
        # 这会导致虽然遍历了多个分类，但实际只请求了同一个分类的数据
        if "categories" in code_lower and "for " in code_lower:
            import re
            # 提取 CATEGORIES 字典定义
            categories_match = re.search(
                r'CATEGORIES\s*=\s*\{([^}]+(?:\{[^}]*\}[^}]*)*)\}',
                code,
                re.DOTALL | re.IGNORECASE
            )
            if categories_match:
                categories_block = categories_match.group(1)
                # 提取所有子字典（分类参数）
                param_dicts = re.findall(r'\{([^}]+)\}', categories_block)
                if len(param_dicts) >= 2:
                    # 标准化参数字典（去除空格、引号差异）以检测重复
                    normalized_params = []
                    for pd in param_dicts:
                        # 移除空格和引号类型差异
                        normalized = re.sub(r'[\s\'"]', '', pd.lower())
                        normalized_params.append(normalized)
                    
                    # 检查是否有重复
                    unique_params = set(normalized_params)
                    if len(unique_params) == 1 and len(normalized_params) > 1:
                        # 所有分类的参数完全相同！这是致命错误
                        issues.append(CodeIssue(
                            code="CAT_DUP_001",
                            severity=IssueSeverity.ERROR,
                            message=f"CATEGORIES 字典中的 {len(normalized_params)} 个分类参数完全相同，实际只会请求同一个分类的数据",
                            suggestion="每个分类必须有不同的参数值（如不同的 levelthree/categoryId）。请检查 verified_category_mapping 并使用正确的分类 ID"
                        ))
                    elif len(unique_params) < len(normalized_params):
                        # 部分重复
                        dup_count = len(normalized_params) - len(unique_params)
                        issues.append(CodeIssue(
                            code="CAT_DUP_002",
                            severity=IssueSeverity.WARNING,
                            message=f"CATEGORIES 字典中有 {dup_count} 个分类的参数与其他分类重复",
                            suggestion="请检查 CATEGORIES 字典，确保每个分类都有唯一的区分参数"
                        ))
        
        return issues
    
    def _build_repair_prompt(self, issues: List, current_code: str, page_structure: Optional[Dict[str, Any]] = None) -> str:
        """
        构建修复提示（带页面结构上下文）
        
        Args:
            issues: 检查发现的问题列表
            current_code: 当前代码
            page_structure: 页面结构信息（帮助 LLM 正确修复）
        """
        lines = [
            "【代码检查发现以下问题，请修正】\n"
        ]
        
        for issue in issues:
            severity_icon = "🔴" if issue.severity.value == "error" else "🟡"
            lines.append(f"{severity_icon} [{issue.code}] {issue.message}")
            if hasattr(issue, 'suggestion') and issue.suggestion:
                lines.append(f"   修复建议: {issue.suggestion}")
            lines.append("")
        
        # 🔑 新增：添加页面结构信息，帮助 LLM 正确修复
        if page_structure:
            lines.append("")
            lines.append("## 页面结构参考（帮助你正确修复）\n")
            
            # 表格信息
            tables = page_structure.get('tables', [])
            if tables:
                table = tables[0]
                lines.append(f"- 表格列数: {table.get('columnCount', '未知')}")
                headers = table.get('headers', [])
                if headers:
                    lines.append(f"- 表头: {headers[:8]}")
                
                # 日期列信息（关键！）
                date_indices = table.get('dateColumnIndices', [])
                date_hints = table.get('dateColumnHints', [])
                if date_indices:
                    lines.append(f"- **日期列位置**: 索引 {date_indices} (从0开始)")
                    for hint in date_hints[:3]:
                        lines.append(f"  - 第{hint.get('columnIndex')}列「{hint.get('headerText', '?')}」检测到日期")
                
                # 下载链接列
                download_indices = table.get('downloadColumnIndices', [])
                if download_indices:
                    lines.append(f"- 下载链接列: 索引 {download_indices}")
                
                # 首行预览
                first_row = table.get('firstRowPreview', [])
                if first_row:
                    lines.append(f"- 首行数据预览: {first_row[:6]}")
            
            # SPA 线索
            spa_hints = page_structure.get('spaHints', {})
            if spa_hints and (spa_hints.get('hasHashRoute') or spa_hints.get('hasAppRoot')):
                lines.append(f"- **SPA 页面**: hasHashRoute={spa_hints.get('hasHashRoute')}, hasAppRoot={spa_hints.get('hasAppRoot')}")
                lines.append("  - 建议使用 Playwright 渲染后提取，或调用 API 获取 JSON")
            
            # 日期元素
            date_elements = page_structure.get('dateElements', [])
            if date_elements:
                lines.append(f"- 页面中检测到 {len(date_elements)} 个日期元素")
                for de in date_elements[:2]:
                    lines.append(f"  - 「{de.get('dateValue')}」位于 {de.get('selector')}")
            
            lines.append("")
        
        lines.append("")
        lines.append(load_prompt("codegen/repair_footer.md").rstrip("\n"))

        return "\n".join(lines)

    def _build_system_prompt(self, run_mode: str = "enterprise_report", crawl_mode: str = "single_page") -> str:
        """构建系统提示词（增强版 v3.0 - 含错误案例，支持多场景）
        
        Args:
            run_mode: 运行模式 ('enterprise_report' | 'news_sentiment')
            crawl_mode: 爬取模式 ('single_page' | 'multi_page' | 'auto_detect')
        """
        
        # 获取错误案例 Few-shot
        error_cases_section = ""
        if self.enable_error_cases:
            error_cases_section = get_error_cases_prompt(severity_threshold=ErrorSeverity.MEDIUM)
        
        # 根据爬取模式生成不同的分类策略提示（所有运行模式通用，短版，供 news_sentiment 复用）
        crawl_mode_instruction = ""
        if crawl_mode == "single_page":
            crawl_mode_instruction = load_prompt("codegen/shared/crawl_mode_single_short.md")
        elif crawl_mode == "multi_page":
            crawl_mode_instruction = load_prompt("codegen/shared/crawl_mode_multi_short.md")
        elif crawl_mode == "auto_detect":
            crawl_mode_instruction = load_prompt("codegen/shared/crawl_mode_auto_short.md")

        # 根据运行模式选择不同的 prompt
        if run_mode == "news_sentiment":
            return self._build_news_system_prompt() + crawl_mode_instruction + error_cases_section

        # 企业报告模式的爬取模式提示（更详细）
        detailed_crawl_mode_instruction = ""
        if crawl_mode == "single_page":
            detailed_crawl_mode_instruction = load_prompt("codegen/enterprise_report/crawl_mode_single.md")
        elif crawl_mode == "multi_page":
            detailed_crawl_mode_instruction = load_prompt("codegen/enterprise_report/crawl_mode_multi.md")
        elif crawl_mode == "auto_detect":
            detailed_crawl_mode_instruction = load_prompt("codegen/enterprise_report/crawl_mode_auto.md")
        
        base_prompt = load_prompt("codegen/enterprise_report/system_base.md")
        # 拼接爬取模式指令和错误案例（使用详细版）
        return base_prompt + detailed_crawl_mode_instruction + error_cases_section

    def _build_news_system_prompt(self) -> str:
        """构建新闻舆情场景的系统提示词"""
        return load_prompt("codegen/news_sentiment/system.md")

    def _compress_html(self, html_content: str) -> str:
        """
        结构化压缩 HTML (Token 优化核心策略):
        1. 移除 script, style, svg, path, link, meta, noscript
        2. 保留 DOM 树结构
        3. 仅保留关键属性 (id, class, href, name, type...)
        4. 文本内容截断 (超过 80 字符截断)
        """
        if not html_content:
            return ""
            
        # 如果内容本身不大，直接返回 (比如小于 15KB)
        if len(html_content) < 15000:
            return html_content

        try:
            from bs4 import BeautifulSoup, Comment
            
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # 1. 移除无关标签 (噪音)
            for tag in soup(['script', 'style', 'svg', 'link', 'meta', 'noscript', 'iframe']):
                tag.decompose()
                
            # 2. 移除注释
            for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
                comment.extract()
                
            # 3. 遍历所有标签进行属性清洗和文本截断
            # 关键属性白名单
            KEY_ATTRS = {
                'id', 'class', 'href', 'src', 'name', 'type', 
                'value', 'placeholder', 'action', 'method',
                'aria-label', 'role', 'title', 'alt'
            }
            
            for tag in soup.find_all(True):
                # 3.1 属性清洗
                current_attrs = list(tag.attrs.keys())
                for attr in current_attrs:
                    if attr not in KEY_ATTRS:
                        del tag.attrs[attr]
                
                # 3.2 文本截断 (仅针对叶子节点的文本)
                if tag.string and len(tag.string) > 80:
                    new_text = tag.string[:80] + "..."
                    tag.string.replace_with(new_text)
            
            cleaned_html = str(soup)
            
            # 简单压缩连续空行
            import re
            cleaned_html = re.sub(r'\n\s*\n', '\n', cleaned_html)
            
            return cleaned_html
            
        except ImportError:
            print("[WARN] bs4 not installed, falling back to raw truncation")
            return html_content[:30000] + "\n...(bs4 missing, truncated)..."
        except Exception as e:
            print(f"[WARN] HTML compression failed: {e}")
            return html_content[:30000] + "\n...(compression error, truncated)..."

    def _build_user_prompt(
        self,
        page_url: str,
        page_html: str,
        structure_summary: str,
        api_info: str,
        user_requirements: Optional[str] = None,
        start_date: str = "",
        end_date: str = "",
        enhanced_summary: str = ""
    ) -> str:
        """构建用户提示词 (模板位于 prompts/codegen/user_prompt/)。"""

        # 使用结构化压缩处理 HTML
        compressed_html = self._compress_html(page_html)

        html_section = load_prompt(
            "codegen/user_prompt/html_section.md",
            compressed_html=compressed_html,
        )

        date_section = ""
        if start_date and end_date:
            date_section = load_prompt(
                "codegen/user_prompt/date_section.md",
                start_date=start_date,
                end_date=end_date,
            )

        requirements_section = ""
        if user_requirements:
            requirements_section = load_prompt(
                "codegen/user_prompt/requirements_section.md",
                user_requirements=user_requirements,
            )

        enhanced_section = ""
        if enhanced_summary:
            enhanced_section = load_prompt(
                "codegen/user_prompt/enhanced_section.md",
                enhanced_summary=enhanced_summary,
            )

        # 获取输出目录的绝对路径
        output_dir = str(Path(__file__).parent / "output")

        # 将“任务目标/日期范围”等高优先级约束放在 user_prompt 最前面
        prefix = ""
        if requirements_section.strip():
            prefix += requirements_section.strip() + "\n\n"
        if date_section.strip():
            prefix += date_section.strip() + "\n\n"

        body = load_prompt(
            "codegen/user_prompt/main_template.md",
            page_url=page_url,
            structure_summary=structure_summary,
            api_info=api_info,
            enhanced_section=enhanced_section,
            html_section=html_section,
            output_dir=output_dir,
        )
        return prefix + body

    def _extract_api_info(
        self, 
        network_requests: Dict[str, List[Dict[str, Any]]],
        enhanced_analysis: Optional[Dict[str, Any]] = None
    ) -> str:
        """提取API请求信息（增强版）"""
        lines = []
        
        # 基础API请求
        api_requests = network_requests.get("api_requests", [])

        if not api_requests:
            lines.append("未捕获到明显的API请求，页面需使用 Playwright 解析 HTML。\n")
        else:
            lines.append("### 初始页面加载时的API请求\n")
            for i, req in enumerate(api_requests[:10], 1):
                lines.append(f"#### 请求 {i}")
                lines.append(f"- URL: {req.get('url', '')}")
                lines.append(f"- Method: {req.get('method', 'GET')}")

                if req.get('post_data'):
                    lines.append(f"- POST数据: {req.get('post_data')[:500]}")

                if req.get('response_status'):
                    lines.append(f"- 响应状态: {req.get('response_status')}")

                # 【关键】显示响应字段结构，帮助 LLM 识别日期字段
                if req.get('response_field_structure'):
                    field_structure = req.get('response_field_structure')
                    lines.append(f"\n- **【重要】API响应字段结构**:")
                    lines.append("  （请仔细检查哪个字段是日期字段，用于提取发布日期）")
                    
                    # 格式化字段结构，特别标记可能的日期字段
                    structure_str = self._format_field_structure(field_structure, indent=2)
                    lines.append(structure_str)
                    
                    # 额外提取并高亮日期字段
                    date_fields = self._find_date_fields(field_structure)
                    if date_fields:
                        lines.append(f"\n  **⚠️ 检测到的日期相关字段**: {', '.join(date_fields)}")
                        lines.append(f"  **请使用这些字段提取报告的发布日期，而不是从标题中提取年份！**")

                if req.get('response_preview'):
                    preview = req.get('response_preview', '')[:800]
                    lines.append(f"- 响应预览: {preview}")

                lines.append("")
        
        # 增强分析中的交互API
        if enhanced_analysis:
            interaction_apis = enhanced_analysis.get("interaction_apis", {})
            
            if interaction_apis.get("interaction_apis"):
                lines.append("\n### 通过交互捕获的API请求（点击菜单后）\n")
                
                for interaction in interaction_apis.get("interaction_apis", []):
                    menu_text = interaction.get("menu_text", "未知菜单")
                    lines.append(f"#### 点击菜单 [{menu_text}] 后的请求：")
                    
                    for api in interaction.get("apis", [])[:3]:
                        lines.append(f"- URL: {api.get('url', '')}")
                        if api.get('response_preview'):
                            preview = api.get('response_preview', '')[:500]
                            lines.append(f"- 响应预览: {preview}")
                    lines.append("")
            
            # 参数分析
            param_analysis = enhanced_analysis.get("param_analysis", {})
            
            if param_analysis.get("category_params"):
                lines.append("\n### 【关键】识别到的分类参数\n")
                lines.append("以下参数在不同菜单点击后值会变化，是必需的分类参数：\n")
                
                for cat_param in param_analysis.get("category_params", []):
                    param_name = cat_param.get("param_name", "")
                    sample_values = cat_param.get("sample_values", [])
                    menu_mapping = cat_param.get("menu_mapping", {})
                    
                    lines.append(f"- **参数名**: `{param_name}`")
                    lines.append(f"  - 示例值: {sample_values}")
                    if menu_mapping:
                        lines.append(f"  - 菜单映射: {json.dumps(menu_mapping, ensure_ascii=False)}")
                    lines.append("")
            
            if param_analysis.get("common_params"):
                lines.append("\n### 固定参数（所有请求都相同）\n")
                for key, value in param_analysis.get("common_params", {}).items():
                    # 截断过长的值
                    display_value = str(value)[:100] + "..." if len(str(value)) > 100 else value
                    lines.append(f"- `{key}`: {display_value}")

        return "\n".join(lines)

    def _format_field_structure(self, structure: Dict[str, Any], indent: int = 0) -> str:
        """格式化字段结构为可读字符串"""
        lines = []
        prefix = "  " * indent
        
        if not structure:
            return f"{prefix}(空)"
        
        # 处理列表类型的结构
        if "_list_of" in structure:
            lines.append(f"{prefix}[列表] 长度: {structure.get('_length', '?')}")
            if "_item_structure" in structure:
                lines.append(f"{prefix}列表元素结构:")
                lines.append(self._format_field_structure(structure["_item_structure"], indent + 1))
            return "\n".join(lines)
        
        for key, info in structure.items():
            if key.startswith("_"):
                continue
                
            field_type = info.get("type", "unknown")
            example = info.get("example", "")
            likely_date = info.get("likely_date", False)
            
            # 日期字段特殊标记
            date_marker = " 📅【日期字段】" if likely_date else ""
            
            if field_type in ("str", "int", "float", "bool", "NoneType"):
                example_str = f" = {repr(example)}" if example is not None else " = null"
                lines.append(f"{prefix}- `{key}` ({field_type}){example_str}{date_marker}")
            elif field_type == "list":
                length = info.get("length", "?")
                lines.append(f"{prefix}- `{key}` (list, 长度: {length}){date_marker}")
                if "item_structure" in info:
                    lines.append(f"{prefix}  元素结构:")
                    lines.append(self._format_field_structure(info["item_structure"], indent + 2))
            elif field_type == "object":
                lines.append(f"{prefix}- `{key}` (object):{date_marker}")
                if "fields" in info:
                    lines.append(self._format_field_structure(info["fields"], indent + 1))
            else:
                lines.append(f"{prefix}- `{key}` ({field_type}){date_marker}")
        
        return "\n".join(lines)

    def _find_date_fields(self, structure: Dict[str, Any], prefix: str = "") -> List[str]:
        """从字段结构中找出所有日期相关字段"""
        date_fields = []
        
        if not structure or not isinstance(structure, dict):
            return date_fields
        
        # 处理列表结构
        if "_item_structure" in structure:
            return self._find_date_fields(structure["_item_structure"], prefix)
        
        for key, info in structure.items():
            if key.startswith("_"):
                continue
                
            full_key = f"{prefix}.{key}" if prefix else key
            
            if isinstance(info, dict):
                if info.get("likely_date"):
                    date_fields.append(full_key)
                
                # 递归检查嵌套结构
                if "item_structure" in info:
                    date_fields.extend(self._find_date_fields(info["item_structure"], full_key))
                if "fields" in info:
                    date_fields.extend(self._find_date_fields(info["fields"], full_key))
        
        return date_fields

    def _summarize_structure(self, structure: Dict[str, Any]) -> str:
        """生成页面结构摘要"""
        lines = []

        # 表格信息
        tables = structure.get("tables", [])
        if tables:
            lines.append(f"### 表格 ({len(tables)} 个)")
            for t in tables[:5]:
                lines.append(f"- 选择器: `{t.get('selector')}`, 行数: {t.get('rows')}, 列数: {t.get('columnCount', '?')}")
                if t.get('headers'):
                    lines.append(f"  表头: {', '.join(t.get('headers', [])[:8])}")
                
                # 【新增】显示日期列位置提示（仅供参考，实际代码应使用智能扫描）
                date_hints = t.get('dateColumnHints', [])
                if date_hints:
                    hint_texts = [f"列{h.get('columnIndex')}({h.get('headerText', '?')})" for h in date_hints[:3]]
                    lines.append(f"  ⚠️ 日期可能在: {', '.join(hint_texts)} — **但不要硬编码列索引！使用 `_pygen_smart_find_date_in_row_*` 智能扫描**")
                
                # 下载列提示
                download_cols = t.get('downloadColumnIndices', [])
                if download_cols:
                    lines.append(f"  下载链接可能在: 列{', 列'.join(map(str, download_cols[:3]))}")

        # 列表信息
        lists = structure.get("lists", [])
        if lists:
            lines.append(f"\n### 列表 ({len(lists)} 个)")
            for l in lists[:5]:
                count = l.get('itemCount', 0)
                lines.append(f"- 选择器: `{l.get('selector')}`, 项数: {count}")
                if count < 5:
                    lines.append(f"  ⚠️ **注意**：当前页面检测到的列表项较少 ({count}项)。如果用户目标是爬取更多数据(如 Top 5/10)，**必须**编写翻页循环逻辑！")

        # 链接信息
        links = structure.get("links", {})
        if links:
            pdf_links = links.get("pdfLinks", [])
            report_links = links.get("reportLinks", [])
            lines.append(f"\n### 链接")
            lines.append(f"- 总链接数: {links.get('totalLinks', 0)}")
            lines.append(f"- PDF/下载链接: {len(pdf_links)} 个")
            lines.append(f"- 报告相关链接: {len(report_links)} 个")

            if pdf_links[:3]:
                lines.append("- PDF链接示例:")
                for pl in pdf_links[:3]:
                    lines.append(f"  - {pl.get('text', '')[:50]}: {pl.get('href', '')[:100]}")

        # 分页信息
        pagination = structure.get("pagination", [])
        if pagination:
            lines.append(f"\n### 分页元素 ({len(pagination)} 个)")
            for p in pagination[:5]:
                lines.append(f"- <{p.get('tag')}> {p.get('text')}")

        # 表单信息
        forms = structure.get("forms", [])
        if forms:
            lines.append(f"\n### 表单 ({len(forms)} 个)")
            for f in forms[:3]:
                lines.append(f"- 选择器: `{f.get('selector')}`, action: {f.get('action')}, method: {f.get('method')}")

        # 【新增】日期元素信息
        # 这对于 API 不返回日期但 HTML 中显示日期的情况非常重要
        date_elements = structure.get("dateElements", [])
        if date_elements:
            lines.append(f"\n### 📅 页面中检测到的日期元素 ({len(date_elements)} 个)")
            lines.append("**重要**：如果 API 响应中没有日期字段，可以从 HTML 中提取这些日期！")
            for de in date_elements[:5]:
                lines.append(f"- 日期值: `{de.get('dateValue')}`, 选择器: `{de.get('selector')}`, 标签: {de.get('tag')}")
            if len(date_elements) > 5:
                lines.append(f"  ... 还有 {len(date_elements) - 5} 个日期元素")

        # 【新增】“条目-日期”关联样本：比单个 dateElements 更可用（可做 join，而不是靠顺序猜）
        date_item_samples = structure.get("dateItemSamples", [])
        if date_item_samples:
            lines.append(f"\n### 📅📄 日期-条目关联样本 ({len(date_item_samples)} 个)")
            lines.append("**关键**：这些样本展示了“标题/条目容器”与“日期”的对应关系。若 API 无日期或日期字段为 null，应优先用浏览器渲染后的 DOM 按此方式提取并关联。")
            for s in date_item_samples[:6]:
                title = (s.get("title") or "")[:60]
                lines.append(
                    f"- 标题: `{title}` | 日期: `{s.get('dateValue')}` | 容器: `{s.get('containerSelector')}` | 日期节点: `{s.get('dateSelector')}`"
                )

        # SPA 线索（提醒模型不要用 requests 抓“渲染后HTML”）
        spa_hints = structure.get("spaHints", {})
        if isinstance(spa_hints, dict) and (spa_hints.get("hasHashRoute") or spa_hints.get("hasAppRoot")):
            lines.append("\n### 🧩 SPA 线索")
            lines.append(f"- hasHashRoute: {bool(spa_hints.get('hasHashRoute'))}")
            lines.append(f"- hasAppRoot: {bool(spa_hints.get('hasAppRoot'))}")

        return "\n".join(lines) if lines else "未检测到明显的结构化元素"

    def _summarize_enhanced_analysis(self, enhanced_analysis: Dict[str, Any]) -> str:
        """生成增强分析摘要"""
        lines = []

        # 0. Shadow DOM 检测结果
        list_extract = enhanced_analysis.get("list_extract", {})
        if list_extract.get("shadowDOM"):
            host_sel = list_extract.get("shadowHostSelector", "")
            code_tpl = list_extract.get("codeTemplate", "")
            lines.append("### 【关键】Shadow DOM 页面")
            lines.append(
                f"此页面的内容渲染在 Shadow DOM 内部（host: `{host_sel}`）。\n"
                "**必须**使用 Playwright 的 `page.evaluate()` 配合 `element.shadowRoot` 来提取数据。\n"
                "**不要**使用 `page.content()` + BeautifulSoup，因为它们无法穿透 Shadow DOM。\n"
            )
            if code_tpl:
                lines.append(
                    "以下是已验证可工作的提取代码模板，请直接在生成的爬虫脚本中使用：\n"
                    "```python\n" + code_tpl.strip() + "\n```\n"
                )
            else:
                hint = list_extract.get("structureHint", "")
                if hint:
                    lines.append(f"结构提示: {hint}\n")

        # 1. 【核心】已验证的分类映射表
        verified_mapping = enhanced_analysis.get("verified_category_mapping", {})
        if verified_mapping and isinstance(verified_mapping, dict):
            menu_to_filters = verified_mapping.get("menu_to_filters", {})
            if menu_to_filters:
                lines.append("### 【核心指令】必须遍历的分类映射表")
                lines.append("自动探测已确认以下板块及其对应的请求参数，生成的代码**必须遍历**这些分类：\n")
                lines.append("```json")
                lines.append(json.dumps(menu_to_filters, ensure_ascii=False, indent=2))
                lines.append("```")
                lines.append("\n**要求**：请直接将上述 JSON 复制到代码中的 `CATEGORIES` 字典，并使用循环遍历抓取。")
        
        # 数据状态
        data_status = enhanced_analysis.get("data_status", {})
        if data_status:
            lines.append("### 数据加载状态\n")
            lines.append(f"- **hasData（是否有数据）**: {data_status.get('hasData', False)}")
            lines.append(f"- 表格数据行数: {data_status.get('tableRowCount', 0)}")
            lines.append(f"- 列表项数量: {data_status.get('listItemCount', 0)}")
            
            empty_indicators = data_status.get('emptyIndicators', [])
            if empty_indicators:
                lines.append(f"- 空数据指示: {empty_indicators}")
            
            menus = data_status.get('potentialMenus', [])
            if menus:
                lines.append(f"\n- **检测到的菜单项** ({len(menus)} 个):")
                for menu in menus[:15]:
                    lines.append(f"  - {menu.get('text', '')}")
        
        # 建议
        recommendations = enhanced_analysis.get("recommendations", [])
        if recommendations:
            lines.append("\n### 系统建议\n")
            for rec in recommendations:
                lines.append(f"- ⚠️ {rec}")

        # 详情页探测结果（支持多次 probe，遍历 detail_probes 列表）
        detail_probes = enhanced_analysis.get("detail_probes") or []
        # 兼容旧格式：如果只有 detail_probe 没有 detail_probes
        if not detail_probes:
            dp = enhanced_analysis.get("detail_probe", {})
            if dp and isinstance(dp, dict):
                detail_probes = [dp]

        has_file_pages = any(p.get("isDirectFile") for p in detail_probes)
        has_html_pages = any(not p.get("isDirectFile") for p in detail_probes)

        if has_file_pages:
            file_probes = [p for p in detail_probes if p.get("isDirectFile")]
            file_type = file_probes[0].get("fileType", "file").upper()
            file_url = file_probes[0].get("url", "")
            lines.append(f"\n### 【关键】部分详情页是 {file_type} 文件，非 HTML 页面")
            mixed_note = "（该网站同时存在 HTML 新闻页和文件下载页，代码必须兼容两种情况）" if has_html_pages else ""
            lines.append(
                f"probe_detail_page 检测到某些详情页返回的是 **{file_type} 文件**（Content-Type 检测）。{mixed_note}\n"
                f"示例 URL: {file_url[:120]}\n\n"
                "**注意**：这类链接的 URL 路径可能**不包含文件后缀**（如 .pdf），不能仅靠 URL 后缀判断。\n"
                "**文件检测规则**（只有以下情况才视为文件链接）：\n"
                "1. 在访问每个详情页时，必须用 try-except 包裹 `page.goto()`。\n"
                "2. 如果捕获到 'Download is starting' 或 'net::ERR_ABORTED' 异常 → 文件链接。\n"
                "3. 如果 URL 以 .pdf/.doc/.docx/.xls/.xlsx 结尾 → 文件链接。\n"
                "4. 以上文件链接情况，content 字段放干净的 HTML 链接：`<a href=\"{url}\" target=\"_blank\">{url}</a>`\n"
                "5. **严禁**在 content 中添加任何额外文字（如\"抓取失败\"、\"Failed to load\"等）。\n\n"
                "**正文优先规则**（页面正常加载时）：\n"
                "6. 如果 `page.goto()` 成功（未抛出下载异常），则该页面是 HTML 页面，**必须尝试提取正文**。\n"
                "7. 提取前**必须**用 `page.wait_for_selector(selector, timeout=8000)` 等待内容容器出现，\n"
                "   因为某些页面在 domcontentloaded 后仍需 JS 渲染内容。\n"
                "8. 依次尝试 probe_detail_page 给出的多个候选 selector，取第一个 inner_html 长度 > 50 的。\n"
                "9. **只有在所有候选 selector 都等待超时或内容为空后**，才回退为 URL 链接。\n"
                "10. 即使该网站有些链接是 PDF，也**不影响** HTML 页面的正文提取——不要因为网站有 PDF 就放弃提取正文。\n"
            )

        # 收集所有 HTML 页面的正文候选（合并去重）
        all_candidates = []
        seen_selectors = set()
        for probe in detail_probes:
            if probe.get("isDirectFile"):
                continue
            for c in (probe.get("contentCandidates") or []):
                sel = c.get("selector", "")
                if sel and sel not in seen_selectors:
                    seen_selectors.add(sel)
                    all_candidates.append(c)

        if all_candidates:
            lines.append("\n### 【详情页正文容器】必须从以下候选中选择")
            lines.append(
                "probe_detail_page 已用 Playwright 实际访问详情页并分析 DOM 结构，得到以下**已验证的**正文容器候选。\n"
                "**强制约束**：\n"
                "- **必须**从以下候选列表中选择一个作为正文提取的 CSS 选择器。\n"
                "- **严禁**自行编造、猜测或使用不在此列表中的选择器（如 `div.interior-layout__main`、`#ContentPlaceholder_xxx` 等）。\n"
                "- 请根据正文长度（越长越好）、链接密度（越低越好）来选择最佳候选。\n"
            )
            for i, c in enumerate(all_candidates[:10], 1):
                sel = c.get("selector", "")
                tl = c.get("textLength", 0)
                ld = c.get("linkDensity", 0)
                preview = (c.get("textPreview") or "")[:60]
                is_file = c.get("isFileContainer", False)
                file_ext = c.get("fileExt", "")

                extra_info = ""
                if is_file:
                    extra_info = f" | 📄文件下载区域({file_ext})"

                lines.append(f"  {i}. selector=`{sel}` | 正文长度={tl} | 链接密度={ld}{extra_info} | 预览={preview!r}")
        elif detail_probes and not has_html_pages:
            lines.append("\n### 【详情页】所有探测到的详情页均为文件下载（无 HTML 正文）")
        else:
            lines.append("\n### 【详情页正文容器】未探测到正文容器候选，请根据页面结构自行选择正文区域。")
        
        return "\n".join(lines)

    def _extract_code_from_response(self, content: str) -> str:
        """从LLM响应中提取Python代码"""
        # 尝试提取 ```python ... ``` 代码块
        pattern = r'```python\s*(.*?)\s*```'
        matches = re.findall(pattern, content, re.DOTALL)

        if matches:
            # 返回最长的代码块（通常是主代码）
            return max(matches, key=len)

        # 如果没有代码块标记，尝试提取整个内容
        lines = content.split('\n')
        code_lines = []
        in_code = False

        for line in lines:
            if line.strip().startswith('import ') or line.strip().startswith('from ') or line.strip().startswith('#'):
                in_code = True
            if in_code:
                code_lines.append(line)

        if code_lines:
            return '\n'.join(code_lines)

        return content

    def _generate_fallback_script(self, page_url: str, run_mode: str = "enterprise_report") -> str:
        """生成备用脚本模板
        
        Args:
            page_url: 目标URL
            run_mode: 运行模式 ('enterprise_report' | 'news_sentiment')
        """
        if run_mode == "news_sentiment":
            return self._generate_news_fallback_script(page_url)
        
        return f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fallback crawler script
Target URL: {page_url}

Note: This is a fallback template, generated when LLM fails.
Please modify the code according to actual page structure.
"""

import requests
from bs4 import BeautifulSoup
import json
import time
import re
import os
from datetime import datetime

# Configuration
BASE_URL = "{page_url}"
HEADERS = {{
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}}

# Date extraction patterns
_DATE_PATTERNS = [
    re.compile(r'(\\d{{4}})[-/\\.](\\d{{1,2}})[-/\\.](\\d{{1,2}})'),
    re.compile(r'(\\d{{4}})年(\\d{{1,2}})月(\\d{{1,2}})日'),
]

def _normalize_date(date_str: str) -> str:
    """Normalize date string to YYYY-MM-DD format"""
    if not date_str:
        return ""
    for pattern in _DATE_PATTERNS:
        match = pattern.search(date_str)
        if match:
            groups = match.groups()
            if len(groups[0]) == 4:
                year, month, day = groups[0], groups[1], groups[2]
                try:
                    if 1900 <= int(year) <= 2100 and 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
                        return f"{{year}}-{{str(month).zfill(2)}}-{{str(day).zfill(2)}}"
                except:
                    pass
    return ""

def _smart_find_date_in_row(tds) -> str:
    """Smart date extraction: scan all columns in a table row"""
    for td in tds:
        try:
            # Try span, time elements first
            for tag in ['span', 'time']:
                elem = td.select_one(tag) if hasattr(td, 'select_one') else None
                if elem:
                    text = elem.get_text(strip=True)
                    date = _normalize_date(text)
                    if date:
                        return date
            # Try date/time class elements
            for sel in ['.date', '.time', '[class*="date"]', '[class*="time"]']:
                try:
                    elem = td.select_one(sel) if hasattr(td, 'select_one') else None
                    if elem:
                        text = elem.get_text(strip=True)
                        date = _normalize_date(text)
                        if date:
                            return date
                except:
                    pass
            # Try direct td text
            text = td.get_text(strip=True) if hasattr(td, 'get_text') else str(td)
            date = _normalize_date(text)
            if date:
                return date
        except:
            continue
    return ""

def fetch_page(url: str) -> str:
    """Fetch page content"""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text

def parse_list(html: str) -> list:
    """Parse list page"""
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # Find table rows (with fallback for tables without tbody)
    rows = soup.select("table tbody tr") or soup.select("table tr")

    for row in rows[1:]:  # Skip header row
        cols = row.select("td")
        if cols and len(cols) >= 2:
            # Extract name from first column
            name_elem = cols[0].select_one("a") or cols[0]
            name = name_elem.get_text(strip=True) if name_elem else ""
            
            # Smart date extraction - scan all columns
            date = _smart_find_date_in_row(cols)
            
            # Extract download URL from last column or any column with PDF link
            download_url = ""
            for col in reversed(cols):
                link = col.select_one("a")
                if link and link.get("href"):
                    href = link.get("href", "")
                    if href and ('.pdf' in href.lower() or '/download' in href.lower() or 
                                href.startswith('http') or href.startswith('/')):
                        download_url = href
                        break
            
            if name and download_url:
                results.append({{
                    "name": name,
                    "date": date,
                    "downloadUrl": download_url,
                    "fileType": "pdf"
                }})

    return results

def save_results(data: list, output_dir: str = "."):
    """Save results to JSON file"""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(output_dir, f"crawl_result_{{timestamp}}.json")
    
    # 构建下载头信息（供后续下载 PDF/附件时使用，绕过防盗链 403）
    from urllib.parse import urlsplit
    _p = urlsplit(BASE_URL)
    _origin = "{{}}://{{}}".format(_p.scheme or "https", _p.netloc)
    download_headers = {{
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": BASE_URL or _origin + "/",
    }}
    
    result = {{
        "total": len(data),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "downloadHeaders": download_headers,
        "reports": data
    }}
    
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[OK] Saved {{len(data)}} records to {{filename}}")
    return filename

def main():
    """Main function"""
    print(f"Starting crawl: {{BASE_URL}}")

    try:
        html = fetch_page(BASE_URL)
        results = parse_list(html)

        if results:
            output_dir = os.path.dirname(os.path.abspath(__file__))
            output_dir = os.path.join(output_dir, "output")
            save_results(results, output_dir)
        else:
            print("[WARN] No data extracted, please check parsing logic")

    except Exception as e:
        print(f"[ERROR] Crawl failed: {{e}}")

if __name__ == "__main__":
    main()
'''

    def _generate_news_fallback_script(self, page_url: str) -> str:
        """生成新闻舆情模式的备用脚本模板"""
        return f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
News Crawler Fallback Script
Target URL: {page_url}

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
BASE_URL = "{page_url}"

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
            viewport={{'width': 1920, 'height': 1080}},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            extra_http_headers={{"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}}
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
                                
                                articles.append({{
                                    "title": title,
                                    "sourceUrl": href,
                                    "date": datetime.now().strftime("%Y-%m-%d"),
                                    "source": "",
                                    "author": "",
                                    "summary": ""
                                }})
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
    headers = {{
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }}
    
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
                
                articles.append({{
                    "title": title,
                    "sourceUrl": href,
                    "date": "",
                    "source": "",
                    "author": "",
                    "summary": ""
                }})
    except Exception as e:
        print(f"[ERROR] {{e}}")
    
    return articles

def save_results(articles, output_dir):
    """保存结果为 JSON 格式"""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(output_dir, f"news_{{timestamp}}.json")
    
    result = {{
        "total": len(articles),
        "crawlTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "articles": articles
    }}
    
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"[OK] Saved {{len(articles)}} news to {{filename}}")
    return filename

def main():
    print(f"[INFO] Starting news crawl: {{BASE_URL}}")
    
    if HAS_PLAYWRIGHT:
        articles = crawl_with_playwright()
    else:
        articles = crawl_with_requests()
    
    if articles:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
        save_results(articles, output_dir)
        print(f"[SUCCESS] Crawled {{len(articles)}} news articles")
    else:
        print("[WARN] No news extracted, please check page structure")

if __name__ == "__main__":
    main()
'''

    def analyze_menu_for_probing(
        self,
        menu_tree: Dict[str, Any],
        screenshot_base64: Optional[str] = None
    ) -> List[str]:
        """
        分析目录树和截图，决定需要探测哪些板块
        
        Args:
            menu_tree: 页面目录树结构
            screenshot_base64: 页面截图（Base64）
            
        Returns:
            List[str]: 需要探测的菜单路径列表
        """
        import json
        
        # 提取叶子路径供选择
        leaf_paths = menu_tree.get("leaf_paths", [])
        if not leaf_paths:
            return []
            
        # 如果叶子太多，截断展示以防 Prompt 过大
        leaf_paths_display = leaf_paths[:200]
        truncated_msg = f"\n(还有 {len(leaf_paths) - 200} 个路径未显示)" if len(leaf_paths) > 200 else ""
        
        system_prompt = load_prompt("browser/menu_exploration_system.md")

        user_prompt = load_prompt(
            "browser/menu_exploration_user.md",
            leaf_paths_json=json.dumps(leaf_paths_display, ensure_ascii=False, indent=2),
            truncated_msg=truncated_msg,
        )

        attachments = []
        if screenshot_base64:
            attachments.append(AttachmentData(
                filename="page_screenshot.jpg",
                base64_data=screenshot_base64,
                mime_type="image/jpeg"
            ))
            
        try:
            print(f"{self._dbg_prefix()} 正在调用 LLM 进行菜单探测决策...")
            response = self._call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                attachments=attachments,
                temperature=0.1
            )
            
            # 清理可能的 markdown 标记
            cleaned = response.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
            
            selected_paths = json.loads(cleaned)
            if isinstance(selected_paths, list):
                # 过滤掉不在原始列表中的幻觉路径
                valid_paths = [p for p in selected_paths if p in leaf_paths]
                print(f"{self._dbg_prefix()} LLM 选中了 {len(valid_paths)} 个有效路径: {valid_paths}")
                return valid_paths
            return []
            
        except Exception as e:
            print(f"❌ 菜单分析失败: {e}")
            # 兜底：如果 LLM 失败，默认选前 5 个非首页路径
            return [p for p in leaf_paths if "首页" not in p and "关于" not in p][:5]

    def get_token_usage(self) -> Dict[str, int]:
        """获取Token使用统计"""
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }
    
    def execute_and_diagnose(
        self,
        script_path: str,
        timeout: int = 120
    ) -> Tuple[bool, Optional[FailureReport]]:
        """
        执行脚本并诊断故障
        
        Args:
            script_path: 脚本路径
            timeout: 超时时间
            
        Returns:
            (是否成功, 故障报告)
        """
        from signals_collector import SignalsCollector, ExecutionStatus
        
        collector = SignalsCollector()
        signals = collector.execute_and_collect(script_path, timeout)
        
        # 如果成功，无需诊断
        if signals.status == ExecutionStatus.SUCCESS:
            return True, None
        
        # 读取脚本内容用于分析
        try:
            with open(script_path, 'r', encoding='utf-8') as f:
                code = f.read()
        except Exception:
            code = None
        
        # 分类故障
        report = self.failure_classifier.classify(signals, code)
        
        return False, report
    
    def generate_with_auto_repair(
        self,
        page_url: str,
        page_html: str,
        page_structure: Dict[str, Any],
        network_requests: Dict[str, List[Dict[str, Any]]],
        user_requirements: Optional[str] = None,
        start_date: str = "",
        end_date: str = "",
        enhanced_analysis: Optional[Dict[str, Any]] = None,
        test_after_generation: bool = False,
        script_save_path: Optional[str] = None
    ) -> Tuple[str, List[str]]:
        """
        生成爬虫脚本并可选执行测试+自动修复
        
        Args:
            page_url: 目标页面URL
            page_html: 完整的页面HTML
            page_structure: 页面结构分析结果
            network_requests: 捕获的网络请求
            user_requirements: 用户任务目标
            start_date: 爬取开始时间
            end_date: 爬取结束时间
            enhanced_analysis: 增强分析结果
            test_after_generation: 是否在生成后执行测试
            script_save_path: 脚本保存路径（用于测试）
            
        Returns:
            (最终脚本, 修复日志)
        """
        repair_history = []
        
        # 第一步：生成脚本
        script = self.generate_crawler_script(
            page_url=page_url,
            page_html=page_html,
            page_structure=page_structure,
            network_requests=network_requests,
            user_requirements=user_requirements,
            start_date=start_date,
            end_date=end_date,
            enhanced_analysis=enhanced_analysis
        )
        
        if not test_after_generation or not script_save_path:
            return script, repair_history
        
        # 第二步：保存并测试
        from signals_collector import SignalsCollector, ExecutionStatus
        
        for attempt in range(self.max_repair_attempts):
            # 保存脚本
            with open(script_save_path, 'w', encoding='utf-8') as f:
                f.write(script)
            
            # 执行测试
            collector = SignalsCollector()
            signals = collector.execute_and_collect(script_save_path, timeout=120)
            
            # 成功则返回
            if signals.status == ExecutionStatus.SUCCESS:
                repair_history.append(f"✅ 第{attempt+1}次执行成功")
                return script, repair_history
            
            # 失败则诊断并修复
            report = self.failure_classifier.classify(signals, script)
            repair_history.append(f"❌ 第{attempt+1}次执行失败: {report.summary}")
            
            # 生成修复提示
            repair_prompt = report.to_repair_prompt()
            
            # 调用 LLM 修复
            try:
                system_prompt = self._build_system_prompt()
                repair_response = self._call_llm(
                    system_prompt=system_prompt,
                    user_prompt=f"修复以下爬虫脚本:\n\n```python\n{script}\n```\n\n{repair_prompt}",
                    attachments=None,
                    temperature=0.1
                )
                
                new_script = self._extract_code_from_response(repair_response)
                
                if new_script and new_script != script:
                    script = new_script
                    repair_history.append(f"🔧 第{attempt+1}次修复完成")
                else:
                    repair_history.append(f"⚠️ 第{attempt+1}次修复未产生变化")
                    break
                    
            except Exception as e:
                repair_history.append(f"❌ 修复调用失败: {str(e)}")
                break
        
        return script, repair_history
