"""
浏览器控制器 - PyGen独立版

基于 Playwright CDP 的增强版浏览器控制器，支持：
- 页面内容获取和网络请求捕获
- 空数据检测和交互探测
- API参数差异分析
"""
import json
import asyncio
import time
import re
from typing import Optional, Dict, Any, List
import hashlib
from urllib import parse
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

# 尝试导入 playwright-stealth (反爬兜底)
try:
    from playwright_stealth import stealth_async
except ImportError:
    stealth_async = None


class BrowserController:
    """增强版浏览器控制器，支持空数据检测和交互探测"""
    
    # 类级别的域名缓存（避免重复检测同一网站）
    _menu_tree_cache: Dict[str, Dict[str, Any]] = {}

    def __init__(self, cdp_url: str, timeout: int = 60000):
        """
        初始化控制器

        Args:
            cdp_url: Chrome CDP WebSocket URL
            timeout: 默认超时时间（毫秒）
        """
        self.cdp_url = cdp_url
        self.timeout = timeout
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        # 网络请求记录
        self.network_requests: List[Dict[str, Any]] = []
        self.api_requests: List[Dict[str, Any]] = []
        
        # 交互探测记录
        self.interaction_api_records: List[Dict[str, Any]] = []
        
        # 目录树元信息（用于“找到就能点”的泛化点击）
        # - key: 节点 path (A/B/C)
        # - value: {"href": "...", "selector": "...", "xpath": "..."}
        self._menu_path_meta: Dict[str, Dict[str, Any]] = {}
        self._last_menu_tree: Optional[Dict[str, Any]] = None

        # CDP 增强监听（P0/P1 技术）
        self.cdp_session = None
        self._cdp_request_map: Dict[str, Dict[str, Any]] = {}   # requestId -> request info
        self._pending_request_ids: set = set()                   # 飞行中请求
        self._last_network_activity: float = 0.0                 # 最后网络活动时间
        self._cdp_enhanced: bool = False                         # CDP 增强是否成功启用

    async def connect(self) -> bool:
        """连接到 Chrome 浏览器"""
        try:
            print(f"→ 正在连接到 CDP: {self.cdp_url}")

            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)

            contexts = self.browser.contexts
            if contexts:
                self.context = contexts[0]
            else:
                self.context = await self.browser.new_context()
            
            # 【反爬增强】注入 JS 规避检测（覆盖 navigator.webdriver 等特征）
            # 虽然启动参数已经禁用 AutomationControlled，但双重保险更稳妥
            await self.context.add_init_script("""
                // 覆盖 navigator.webdriver
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                
                // 模拟 window.chrome (非自动化浏览器通常有这个对象)
                if (!window.chrome) {
                    window.chrome = {
                        runtime: {}
                    };
                }
                
                // 模拟 navigator.plugins (无头模式通常为空)
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
                
                // 模拟 navigator.languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['zh-CN', 'zh', 'en']
                });
            """)

            pages = self.context.pages
            if pages:
                self.page = pages[0]
            else:
                self.page = await self.context.new_page()

            # 【反爬兜底】应用 playwright-stealth
            if stealth_async and self.page:
                try:
                    await stealth_async(self.page)
                    print("✓ 已启用 playwright-stealth 反爬增强")
                except Exception as e:
                    # 不阻断主流程
                    print(f"⚠ playwright-stealth 启用失败: {e}")

            self.page.set_default_timeout(self.timeout)
            self.page.set_default_navigation_timeout(self.timeout)

            # 启用 CDP 增强网络监听（P0/P1）
            await self._init_cdp_enhanced()
            # 回退：如果 CDP 增强失败，使用 Playwright 事件监听
            self._setup_network_listener()

            print("✓ CDP 连接成功")
            return True

        except Exception as e:
            print(f"✗ CDP 连接失败: {e}")
            return False

    async def _ensure_live_page(self) -> bool:
        """
        确保 self.page/self.context/self.browser 可用。
        用于处理 Playwright 报错：Target page, context or browser has been closed。
        """
        try:
            # page 可用
            if self.page and hasattr(self.page, "is_closed") and not self.page.is_closed():
                return True
        except Exception:
            pass

        # 尝试从已有 context 复活/新建 page
        try:
            if self.context:
                pages = self.context.pages
                if pages:
                    self.page = pages[0]
                else:
                    self.page = await self.context.new_page()
                
                if stealth_async and self.page:
                    try:
                        await stealth_async(self.page)
                    except: pass

                self.page.set_default_timeout(self.timeout)
                self.page.set_default_navigation_timeout(self.timeout)
                self._cdp_enhanced = False
                await self._init_cdp_enhanced()
                self._setup_network_listener()
                return True
        except Exception:
            pass

        # 尝试从已有 browser 新建 context/page
        try:
            if self.browser:
                self.context = await self.browser.new_context()
                self.page = await self.context.new_page()
                
                if stealth_async and self.page:
                    try:
                        await stealth_async(self.page)
                    except: pass

                self.page.set_default_timeout(self.timeout)
                self.page.set_default_navigation_timeout(self.timeout)
                self._cdp_enhanced = False
                await self._init_cdp_enhanced()
                self._setup_network_listener()
                return True
        except Exception:
            pass

        # 最后：重新 connect（CDP 可能还活着）
        try:
            return await self.connect()
        except Exception:
            return False

    def _setup_network_listener(self):
        """设置网络请求监听器（CDP 增强可用时为空操作，回退到 Playwright 事件）"""
        if not self.page:
            return
        if self._cdp_enhanced:
            return  # CDP 增强已接管网络监听，跳过 Playwright 监听避免重复

        async def on_request(request):
            """记录所有请求"""
            url = request.url
            method = request.method

            # 过滤掉静态资源
            skip_extensions = ['.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg', '.woff', '.woff2', '.ttf']
            if any(url.lower().endswith(ext) for ext in skip_extensions):
                return

            # 记录请求
            post_data = None
            if method == "POST":
                try:
                    # 尝试获取文本形式的 post_data
                    post_data = request.post_data
                except Exception:
                    # 如果解码失败（如 gzip/二进制），记录占位符或尝试读取 buffer
                    post_data = "[Binary/Gzip Data]"

            req_info = {
                "url": url,
                "method": method,
                "headers": dict(request.headers) if request.headers else {},
                "post_data": post_data,
            }
            self.network_requests.append(req_info)

            # 识别可能的数据API请求
            api_keywords = ['api', 'ajax', 'json', 'data', 'list', 'search', 'query', 'page', 'get', 'post']
            url_lower = url.lower()
            if any(kw in url_lower for kw in api_keywords) or method == "POST":
                self.api_requests.append(req_info)

        async def on_response(response):
            """记录响应信息"""
            url = response.url

            # 找到对应的请求并更新
            for req in reversed(self.network_requests):
                if req["url"] == url and "response_status" not in req:
                    req["response_status"] = response.status
                    req["response_headers"] = dict(response.headers) if response.headers else {}

                    # 尝试获取响应体（仅对API请求）
                    content_type = response.headers.get("content-type", "")
                    if "json" in content_type or "text" in content_type:
                        try:
                            body = await response.text()
                            req["response_preview"] = body[:2000] if body else ""
                            
                            # 【新增】解析 JSON 响应，提取字段结构
                            if body and "json" in content_type:
                                try:
                                    json_data = json.loads(body)
                                    field_structure = self._extract_json_field_structure(json_data)
                                    if field_structure:
                                        req["response_field_structure"] = field_structure
                                except:
                                    pass
                        except:
                            pass
                    break

        self.page.on("request", on_request)
        self.page.on("response", on_response)

    # ============ CDP 增强网络监听 (P0/P1) ============

    async def _init_cdp_enhanced(self):
        """
        初始化 CDP 增强监听，集成 6 项 CDP 技术：
        P0-1: Network.setCacheDisabled      — 禁用缓存
        P0-2: resourceType + postData 补全  — 从 CDP 事件获取完整字段
        P0-3: addScriptToEvaluateOnNewDocument — 注入 XHR/fetch 钩子
        P1-4: Network.getResponseBody       — 按 requestId 取完整响应体
        P1-5: Network.loadingFinished       — 网络空闲检测
        P1-6: initiator 归因               — 记录请求触发源
        """
        if not self.page or not self.context:
            return
        try:
            # 创建 CDP 会话
            self.cdp_session = await self.context.new_cdp_session(self.page)

            # P0-1: 禁用缓存，确保每次日期操作都触发新请求
            await self.cdp_session.send('Network.enable')
            await self.cdp_session.send('Network.setCacheDisabled', {'cacheDisabled': True})

            # P0-3: 注入 XHR/fetch 拦截钩子（在任何页面脚本执行前生效）
            await self.cdp_session.send('Page.enable')
            await self.cdp_session.send('Page.addScriptToEvaluateOnNewDocument', {
                'source': self._get_intercept_script()
            })

            # 注册 CDP 网络事件监听
            self.cdp_session.on('Network.requestWillBeSent', self._on_cdp_request_will_be_sent)
            self.cdp_session.on('Network.responseReceived', self._on_cdp_response_received)
            self.cdp_session.on('Network.loadingFinished', self._on_cdp_loading_finished)
            self.cdp_session.on('Network.loadingFailed', self._on_cdp_loading_failed)

            self._cdp_enhanced = True
            print("  ✓ CDP 增强网络监听已启用（缓存禁用 + XHR/fetch 钩子 + 完整响应体 + 空闲检测 + 归因）")
        except Exception as e:
            self._cdp_enhanced = False
            print(f"  ⚠ CDP 增强初始化失败，将回退到 Playwright 事件: {e}")

    @staticmethod
    def _get_intercept_script() -> str:
        """
        P0-3: 返回注入到每个页面的 XHR/fetch 拦截脚本。
        
        功能：
        - Monkey-patch XMLHttpRequest.prototype.open/send 和 window.fetch
        - 记录每次 API 调用的 URL、方法、POST body、JS 调用栈
        - 数据存储在 window.__interceptedAPIs 数组中，供 Layer 0 读取
        """
        return r'''
(() => {
    if (window.__cdp_api_interceptor_installed) return;
    window.__cdp_api_interceptor_installed = true;
    window.__interceptedAPIs = [];
    const MAX_RECORDS = 300;

    const _origOpen = XMLHttpRequest.prototype.open;
    const _origSend = XMLHttpRequest.prototype.send;
    const _origFetch = window.fetch;

    XMLHttpRequest.prototype.open = function(method, url) {
        this.__cdp_method = method;
        this.__cdp_url = typeof url === 'string' ? url : String(url);
        this.__cdp_stack = new Error().stack;
        return _origOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function(body) {
        try {
            var record = {
                type: 'xhr',
                method: this.__cdp_method || 'GET',
                url: this.__cdp_url || '',
                stack: (this.__cdp_stack || '').slice(0, 1000),
                postData: typeof body === 'string' ? body.slice(0, 2000) : null,
                timestamp: Date.now()
            };
            if (window.__interceptedAPIs.length >= MAX_RECORDS) {
                window.__interceptedAPIs.shift();
            }
            window.__interceptedAPIs.push(record);
        } catch(e) {}
        return _origSend.apply(this, arguments);
    };

    window.fetch = function(input, init) {
        try {
            var url = typeof input === 'string' ? input :
                      (input instanceof Request ? input.url : String(input));
            var method = (init && init.method) ||
                         (input instanceof Request ? input.method : 'GET');
            var postData = null;
            if (init && init.body) {
                if (typeof init.body === 'string') {
                    postData = init.body.slice(0, 2000);
                }
            }
            var record = {
                type: 'fetch',
                method: (method || 'GET').toUpperCase(),
                url: url,
                stack: new Error().stack.slice(0, 1000),
                postData: postData,
                timestamp: Date.now()
            };
            if (window.__interceptedAPIs.length >= MAX_RECORDS) {
                window.__interceptedAPIs.shift();
            }
            window.__interceptedAPIs.push(record);
        } catch(e) {}
        return _origFetch.apply(this, arguments);
    };
})();
'''

    def _on_cdp_request_will_be_sent(self, params):
        """
        CDP Network.requestWillBeSent 回调
        P0-2: 记录完整的 resourceType、postData
        P1-6: 记录 initiator（触发源 + 调用栈）
        """
        request = params.get('request', {})
        url = request.get('url', '')
        method = request.get('method', 'GET')
        request_id = params.get('requestId', '')
        resource_type = params.get('type', '')  # XHR, Fetch, Script, Document, ...
        initiator = params.get('initiator', {})

        # 过滤纯静态资源（但保留可能是 JSONP 的 .js）
        skip_extensions = ['.css', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg',
                           '.woff', '.woff2', '.ttf', '.eot', '.mp4', '.mp3']
        url_lower = url.lower()
        parsed_path = url_lower.split('?')[0]
        if any(parsed_path.endswith(ext) for ext in skip_extensions):
            return

        # .js 文件：仅保留疑似 JSONP 的（含 callback/jsonp 参数）
        if parsed_path.endswith('.js'):
            if not any(kw in url_lower for kw in ['callback', 'jsonp', 'jsoncallback']):
                return

        post_data = request.get('postData')

        req_info = {
            "url": url,
            "method": method,
            "headers": request.get('headers', {}),
            # 同时提供两种命名，兼容上下游
            "post_data": post_data,
            "postData": post_data,
            # P0-2: 新增字段
            "resourceType": resource_type,
            # P1-6: 新增字段
            "initiator": initiator,
            # 内部字段
            "_requestId": request_id,
            "_is_api": False,
        }

        self._cdp_request_map[request_id] = req_info
        self.network_requests.append(req_info)
        self._pending_request_ids.add(request_id)
        self._last_network_activity = time.time()

        # 识别 API 请求（比旧版更精准：使用 resourceType）
        is_api = False
        if resource_type in ('XHR', 'Fetch'):
            is_api = True
        elif method == 'POST':
            is_api = True
        else:
            api_keywords = ['api', 'ajax', 'json', 'data', 'list', 'search',
                            'query', 'page', 'get', 'post', '.do', '.action']
            if any(kw in url_lower for kw in api_keywords):
                is_api = True

        if is_api:
            req_info['_is_api'] = True
            self.api_requests.append(req_info)

    def _on_cdp_response_received(self, params):
        """CDP Network.responseReceived 回调 — 记录响应头"""
        request_id = params.get('requestId', '')
        response = params.get('response', {})

        req_info = self._cdp_request_map.get(request_id)
        if not req_info:
            return

        req_info['response_status'] = response.get('status', 0)
        req_info['response_headers'] = response.get('headers', {})
        req_info['_response_mime_type'] = response.get('mimeType', '')

    def _on_cdp_loading_finished(self, params):
        """
        CDP Network.loadingFinished 回调
        P1-4: 触发完整响应体获取
        P1-5: 更新网络空闲状态
        """
        request_id = params.get('requestId', '')
        self._pending_request_ids.discard(request_id)
        self._last_network_activity = time.time()

        req_info = self._cdp_request_map.get(request_id)
        if not req_info:
            return

        # P1-4: 对 API 请求自动获取完整响应体
        if req_info.get('_is_api'):
            mime = req_info.get('_response_mime_type', '')
            # 对 JSON/文本/JavaScript(JSONP) 获取响应体
            if any(kw in mime for kw in ['json', 'text', 'javascript', 'html']):
                try:
                    asyncio.get_event_loop().create_task(
                        self._fetch_and_store_response_body(request_id)
                    )
                except Exception:
                    pass

    def _on_cdp_loading_failed(self, params):
        """CDP Network.loadingFailed 回调 — 更新空闲状态"""
        request_id = params.get('requestId', '')
        self._pending_request_ids.discard(request_id)
        self._last_network_activity = time.time()

    async def _fetch_and_store_response_body(self, request_id: str):
        """
        P1-4: 使用 CDP Network.getResponseBody 获取完整响应体。
        
        优势（对比旧版 response.text()[:2000]）：
        - 不截断：获取完整的响应数据
        - 不受 CORS 限制：CDP 直接读取浏览器内存
        - 支持 base64 解码：自动处理二进制/gzip 响应
        - 覆盖 JSONP：即使 content-type 是 application/javascript 也能获取
        """
        if not self.cdp_session:
            return

        req_info = self._cdp_request_map.get(request_id)
        if not req_info:
            return

        try:
            result = await self.cdp_session.send('Network.getResponseBody', {
                'requestId': request_id
            })
            body = result.get('body', '')
            is_base64 = result.get('base64Encoded', False)

            if is_base64 and body:
                import base64
                body = base64.b64decode(body).decode('utf-8', errors='replace')

            # 存储完整响应体（不截断！）
            req_info['response_body'] = body
            req_info['response_preview'] = body[:2000] if body else ""

            # 解析 JSON 响应，提取字段结构
            if body:
                try:
                    text = body.strip()
                    # 处理 JSONP 包裹
                    if text.startswith('/**/'):
                        text = text[4:].strip()
                    if text.startswith('?(') and text.endswith(')'):
                        text = text[2:-1]
                    jsonp_m = re.match(r'^[a-zA-Z_]\w*\s*\((.*)\)\s*;?\s*$', text, re.S)
                    if jsonp_m:
                        text = jsonp_m.group(1)

                    if text.startswith('{') or text.startswith('['):
                        json_data = json.loads(text)
                        field_structure = self._extract_json_field_structure(json_data)
                        if field_structure:
                            req_info['response_field_structure'] = field_structure
                except Exception:
                    pass

        except Exception:
            # 请求可能已从浏览器内存中逐出
            pass

    async def wait_for_network_idle(self, timeout: float = 5.0, idle_time: float = 0.5) -> bool:
        """
        P1-5: 等待网络空闲。
        
        替代硬编码的 asyncio.sleep(2)：
        - 快站 0.3s 就走（响应快则不等）
        - 慢站自动多等（直到 timeout）
        - 精确知道所有请求何时返回
        
        Args:
            timeout: 最大等待时间（秒）
            idle_time: 无网络活动持续多久算"空闲"（秒）
            
        Returns:
            True 表示网络已空闲，False 表示超时
        """
        if not self._cdp_enhanced:
            # 回退到 sleep
            await asyncio.sleep(min(timeout, 2.0))
            return True

        start = time.time()
        while time.time() - start < timeout:
            # 检查是否有飞行中的请求
            if len(self._pending_request_ids) == 0:
                # 没有飞行中的请求，检查是否持续空闲
                idle_since = time.time() - self._last_network_activity
                if idle_since >= idle_time:
                    return True
            await asyncio.sleep(0.05)

        # 超时但可能请求已经完成
        return len(self._pending_request_ids) == 0

    async def get_intercepted_apis(self) -> list:
        """
        P0-3: 读取注入钩子捕获的 API 调用记录。
        
        返回 window.__interceptedAPIs 数组，每个元素包含：
        - type: 'xhr' | 'fetch'
        - method: 'GET' | 'POST' | ...
        - url: 请求 URL
        - stack: JS 调用栈（可追溯到触发源）
        - postData: POST body（如有）
        - timestamp: 时间戳
        """
        if not self.page:
            return []
        try:
            result = await self.page.evaluate('() => window.__interceptedAPIs || []')
            return result if isinstance(result, list) else []
        except Exception:
            return []

    async def _get_rating_menubar(self):
        """尽量定位“评级结果发布”页面的顶部菜单（role=menubar）。"""
        if not self.page:
            return None
        try:
            menubars = self.page.get_by_role("menubar")
            count = await menubars.count()
            if count == 0:
                return None
            # 通常第一个就是评级菜单
            return menubars.first
        except Exception:
            return None

    async def enumerate_menu_tree(self, max_depth: int = 4, use_llm_fallback: bool = True) -> Dict[str, Any]:
        """
        【混合方案】枚举页面的多级目录树（多策略启发式 + LLM 兜底）。
        
        检测流程：
        1. 快速启发式探测（多种策略并行尝试）
        2. 如果全部失败且 use_llm_fallback=True，调用 LLM 智能识别
        3. 缓存结果（同域名复用）

        返回结构：
        {
          "root": {...},
          "leaf_paths": ["新闻公告/公告通知/重要通知", ...],
          "detection_method": "aria" | "nav" | "sidebar" | "nested_list" | "llm" | "none"
        }
        """
        if not self.page:
            return {"root": None, "leaf_paths": [], "detection_method": "none"}

        # 预展开树形菜单（如 Element UI 的 el-tree），避免子节点未渲染导致只能识别第一层
        try:
            await self.page.evaluate(
                """() => {
                const tree = document.querySelector('.el-tree');
                if (!tree) return;
                const icons = tree.querySelectorAll('.el-tree-node__expand-icon:not(.is-leaf)');
                icons.forEach(ic => {
                    try {
                        if (!ic.classList.contains('expanded')) ic.click();
                    } catch (e) {}
                });
            }"""
            )
            await asyncio.sleep(0.2)
        except Exception:
            pass

        # 检查缓存（同域名复用）
        try:
            current_url = self.page.url
            from urllib.parse import urlparse
            domain = urlparse(current_url).netloc
            if domain in self._menu_tree_cache:
                cached = self._menu_tree_cache[domain]
                print(f"[MenuTree] 使用缓存结果: {domain} (method={cached.get('detection_method')})")
                # 让后续点击逻辑也能使用缓存的 selector/href
                self._menu_path_meta = cached.get("path_meta") or {}
                self._last_menu_tree = cached
                return cached
        except Exception:
            domain = ""

        # Step 1: 多策略启发式探测
        result = await self._heuristic_menu_detection(max_depth)
        
        if result and result.get("leaf_paths"):
            print(f"[MenuTree] 启发式探测成功: method={result.get('detection_method')}, leaves={len(result.get('leaf_paths', []))}")
            # 索引 path->meta，供后续“按 selector/href 点击/跳转”
            try:
                path_meta: Dict[str, Dict[str, Any]] = {}
                def _walk(n: Dict[str, Any]):
                    if not isinstance(n, dict):
                        return
                    p = n.get("path")
                    if p:
                        path_meta[p] = {
                            "href": n.get("href"),
                            "selector": n.get("selector"),
                            "xpath": n.get("xpath"),
                            "name": n.get("name"),
                        }
                    for c in (n.get("children") or []):
                        _walk(c)
                root = result.get("root") or {}
                for c in (root.get("children") or []):
                    _walk(c)
                result["path_meta"] = path_meta
                self._menu_path_meta = path_meta
                self._last_menu_tree = result
            except Exception:
                pass
            if domain:
                self._menu_tree_cache[domain] = result
            return result

        # Step 2: LLM 智能识别（兜底）
        if use_llm_fallback:
            print("[MenuTree] 启发式探测失败，尝试 LLM 智能识别...")
            result = await self._llm_menu_detection(max_depth)
            if result and result.get("leaf_paths"):
                print(f"[MenuTree] LLM 识别成功: leaves={len(result.get('leaf_paths', []))}")
                # LLM 兜底可能拿不到 selector/href，这里仍然初始化为空，避免 None
                self._menu_path_meta = result.get("path_meta") or {}
                self._last_menu_tree = result
                if domain:
                    self._menu_tree_cache[domain] = result
                return result

        print("[MenuTree] 所有检测方法均失败")
        return {"root": None, "leaf_paths": [], "detection_method": "none"}

    async def _heuristic_menu_detection(self, max_depth: int = 4) -> Dict[str, Any]:
        """
        多策略启发式探测导航菜单。
        按优先级尝试多种常见的导航结构模式。
        """
        if not self.page:
            return {"root": None, "leaf_paths": [], "detection_method": "none"}

        try:
            # 多策略探测的 JavaScript 代码
            result = await self.page.evaluate(
                """(maxDepth) => {
                // ========== 工具函数 ==========
                const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                
                const nodeId = (path) => {
                    let h = 0;
                    for (let i = 0; i < path.length; i++) {
                        h = ((h << 5) - h) + path.charCodeAt(i);
                        h |= 0;
                    }
                    return Math.abs(h).toString(16).slice(0, 12);
                };

                // 生成“更稳定”的 selector：优先 id / data-* / href(唯一)；否则构造 css path
                const _cssEscape = (s) => {
                    if (!s) return '';
                    return String(s).replace(/\\\\/g, '\\\\\\\\').replace(/\"/g, '\\\\\"');
                };

                const _uniqueSelectorFor = (el) => {
                    if (!el || el.nodeType !== 1) return null;
                    const tag = el.tagName ? el.tagName.toLowerCase() : 'div';
                    // 1) id
                    try {
                        if (el.id) {
                            const idSel = `#${CSS.escape(el.id)}`;
                            if (document.querySelectorAll(idSel).length === 1) return idSel;
                        }
                    } catch (e) {}
                    // 2) 常见 data-* / aria / name
                    const attrs = [
                        'data-testid', 'data-test', 'data-id', 'data-key', 'data-value',
                        'aria-label', 'aria-controls', 'name'
                    ];
                    for (const a of attrs) {
                        try {
                            const v = el.getAttribute && el.getAttribute(a);
                            if (!v) continue;
                            const sel = `${tag}[${a}=\"${_cssEscape(v)}\"]`;
                            if (document.querySelectorAll(sel).length === 1) return sel;
                        } catch (e) {}
                    }
                    // 3) href (仅当唯一)
                    try {
                        if (tag === 'a' && el.getAttribute && el.getAttribute('href')) {
                            const hrefAttr = el.getAttribute('href');
                            const sel1 = `a[href=\"${_cssEscape(hrefAttr)}\"]`;
                            if (document.querySelectorAll(sel1).length === 1) return sel1;
                            const abs = el.href;
                            if (abs) {
                                const sel2 = `a[href=\"${_cssEscape(abs)}\"]`;
                                if (document.querySelectorAll(sel2).length === 1) return sel2;
                            }
                        }
                    } catch (e) {}
                    return null;
                };

                const _cssPath = (el) => {
                    if (!el || el.nodeType !== 1) return '';
                    const maxHops = 8;
                    let cur = el;
                    const parts = [];
                    for (let hop = 0; hop < maxHops && cur && cur.nodeType === 1 && cur !== document.documentElement; hop++) {
                        const unique = _uniqueSelectorFor(cur);
                        if (unique) {
                            parts.unshift(unique);
                            break;
                        }
                        const tag = cur.tagName.toLowerCase();
                        // nth-of-type
                        let idx = 1;
                        let sib = cur;
                        while ((sib = sib.previousElementSibling)) {
                            if (sib.tagName.toLowerCase() === tag) idx++;
                        }
                        parts.unshift(`${tag}:nth-of-type(${idx})`);
                        cur = cur.parentElement;
                    }
                    if (!parts.length) return '';
                    return parts.join(' > ');
                };

                const _xpath = (el) => {
                    if (!el || el.nodeType !== 1) return '';
                    const segs = [];
                    let cur = el;
                    const maxHops = 10;
                    for (let hop = 0; hop < maxHops && cur && cur.nodeType === 1; hop++) {
                        const tag = cur.tagName.toLowerCase();
                        let idx = 1;
                        let sib = cur.previousElementSibling;
                        while (sib) {
                            if (sib.tagName.toLowerCase() === tag) idx++;
                            sib = sib.previousElementSibling;
                        }
                        segs.unshift(`${tag}[${idx}]`);
                        cur = cur.parentElement;
                        if (tag === 'body') break;
                    }
                    return '/' + segs.join('/');
                };

                const _stableSelector = (el) => {
                    return _uniqueSelectorFor(el) || _cssPath(el);
                };

                const getOwnText = (el) => {
                    if (!el) return '';
                    const clone = el.cloneNode(true);
                    clone.querySelectorAll('ul, ol, [role="menu"], .submenu, .sub-menu, .child-menu, .el-tree-node__children').forEach(m => m.remove());
                    let text = normalize(clone.innerText || clone.textContent || '');
                    if (text.length > 50) text = text.slice(0, 50);
                    return text;
                };

                const getLinkText = (el) => {
                    const link = el.tagName === 'A' ? el : el.querySelector('a');
                    if (link) {
                        return normalize(link.innerText || link.textContent || '');
                    }
                    return getOwnText(el);
                };

                const isValidMenuItem = (el) => {
                    const text = getLinkText(el);
                    if (!text || text.length < 2 || text.length > 30) return false;
                    const excludePatterns = /^(copyright|©|版权|备案|icp|联系我们|关于我们|登录|注册|首页|home|more|更多|>>|<<|\\d{4}-\\d{2}|\\d{4}年)/i;
                    if (excludePatterns.test(text)) return false;
                    return true;
                };

                const buildTree = (container, pathParts, depth, getChildren) => {
                    const children = getChildren(container);
                    const nodes = [];
                    for (const child of children) {
                        const name = getLinkText(child);
                        if (!name || !isValidMenuItem(child)) continue;
                        const parts = pathParts.concat([name]);
                        const path = parts.join('/');
                        const node = { id: nodeId(path), name, path, children: [], href: '', selector: '', xpath: '' };
                        const link = child.tagName === 'A' ? child : child.querySelector('a');
                        if (link) node.href = link.href || '';
                        const clickEl = link || child;
                        node.selector = _stableSelector(clickEl) || '';
                        node.xpath = _xpath(clickEl) || '';
                        if (depth < maxDepth) {
                            const subMenu = child.querySelector('ul, ol, [role="menu"], .submenu, .sub-menu, .child-menu, .el-tree-node__children');
                            if (subMenu) {
                                node.children = buildTree(subMenu, parts, depth + 1, getChildren);
                            }
                        }
                        node.isLeaf = node.children.length === 0;
                        nodes.push(node);
                    }
                    return nodes;
                };

                const extractLeafPaths = (nodes) => {
                    const paths = [];
                    const walk = (n) => {
                        if (n.isLeaf && n.path) paths.push(n.path);
                        (n.children || []).forEach(walk);
                    };
                    nodes.forEach(walk);
                    return paths;
                };

                // 策略 0: Element UI Tree（中诚信常见）
                const tryElementTree = () => {
                    const tree = document.querySelector('.el-tree');
                    if (!tree) return null;
                    const getChildren = (container) => {
                        if (container.classList && container.classList.contains('el-tree')) {
                            return Array.from(container.querySelectorAll(':scope > .el-tree-node'));
                        }
                        const childWrap = container.querySelector(':scope > .el-tree-node__children') || container.querySelector('.el-tree-node__children');
                        if (childWrap) return Array.from(childWrap.querySelectorAll(':scope > .el-tree-node'));
                        return Array.from(container.querySelectorAll(':scope > .el-tree-node'));
                    };
                    const children = buildTree(tree, [], 1, getChildren);
                    if (!children.length) return null;
                    const root = { id: 'root', name: 'root', path: '', children };
                    return { root, leaf_paths: extractLeafPaths(children), detection_method: 'el_tree' };
                };

                // 策略 1: ARIA
                const tryAriaRoles = () => {
                    const menubar = document.querySelector('[role="menubar"]');
                    if (!menubar) return null;
                    const getChildren = (c) => {
                        if (c.matches('[role="menubar"]')) return Array.from(c.querySelectorAll(':scope > [role="menuitem"]'));
                        // 递归到 role="menu" 容器时，子项通常就是其直接子 menuitem
                        if (c.matches('[role="menu"]')) return Array.from(c.querySelectorAll(':scope > [role="menuitem"]'));
                        const menu = c.querySelector(':scope > [role="menu"]') || c.querySelector('[role="menu"]');
                        return menu ? Array.from(menu.querySelectorAll(':scope > [role="menuitem"]')) : [];
                    };
                    const children = buildTree(menubar, [], 1, getChildren);
                    if (!children.length) return null;
                    const root = { id: 'root', name: 'root', path: '', children };
                    return { root, leaf_paths: extractLeafPaths(children), detection_method: 'aria' };
                };

                // 策略 2: nav
                const tryNavElement = () => {
                    for (const nav of document.querySelectorAll('nav')) {
                        const list = nav.querySelector('ul, ol');
                        if (!list) continue;
                        const getChildren = (c) => Array.from(c.querySelectorAll(':scope > li'));
                        const children = buildTree(list, [], 1, getChildren);
                        if (children.length >= 2) {
                            const root = { id: 'root', name: 'root', path: '', children };
                            return { root, leaf_paths: extractLeafPaths(children), detection_method: 'nav' };
                        }
                    }
                    return null;
                };

                // 策略 3: sidebar
                const trySidebar = () => {
                    const selectors = ['.sidebar', '.side-bar', '.left-nav', '.left-menu', '.leftNav', '.leftMenu',
                        '[class*="sidebar"]', '[class*="sidenav"]', '[class*="leftNav"]', '[class*="leftMenu"]',
                        '[class*="left-nav"]', '[class*="left-menu"]', '.menu-tree', '.nav-tree'];
                    for (const sel of selectors) {
                        try {
                            const sidebar = document.querySelector(sel);
                            if (!sidebar) continue;
                            const list = sidebar.querySelector('ul, ol') || sidebar;
                            const getChildren = (c) => {
                                const lis = c.querySelectorAll(':scope > li');
                                if (lis.length > 0) return Array.from(lis);
                                return Array.from(c.querySelectorAll(':scope > a, :scope > div > a'));
                            };
                            const children = buildTree(list, [], 1, getChildren);
                            if (children.length >= 2) {
                                const root = { id: 'root', name: 'root', path: '', children };
                                return { root, leaf_paths: extractLeafPaths(children), detection_method: 'sidebar' };
                            }
                        } catch (e) { continue; }
                    }
                    return null;
                };

                // 策略 4: nested list
                const tryNestedList = () => {
                    let bestList = null, bestScore = 0;
                    for (const list of document.querySelectorAll('ul, ol')) {
                        if (list.closest('footer, header, .footer, .header')) continue;
                        const nested = list.querySelectorAll('ul, ol');
                        const links = list.querySelectorAll('a');
                        const rect = list.getBoundingClientRect();
                        let score = (nested.length > 0 ? 2 : 1) * 10 + links.length;
                        if (rect.left < window.innerWidth * 0.35) score += 20;
                        if (rect.top < window.innerHeight * 0.5) score += 10;
                        if (nested.length >= 2) score += 15;
                        if (score > bestScore && links.length >= 3) { bestScore = score; bestList = list; }
                    }
                    if (!bestList) return null;
                    const getChildren = (c) => Array.from(c.querySelectorAll(':scope > li'));
                    const children = buildTree(bestList, [], 1, getChildren);
                    if (children.length >= 2) {
                        const root = { id: 'root', name: 'root', path: '', children };
                        return { root, leaf_paths: extractLeafPaths(children), detection_method: 'nested_list' };
                    }
                    return null;
                };

                // 策略 5: link hierarchy
                const tryLinkHierarchy = () => {
                    const leftLinks = [];
                    for (const link of document.querySelectorAll('a')) {
                        const rect = link.getBoundingClientRect();
                        if (rect.left < window.innerWidth * 0.35 && rect.top > 0 && rect.top < window.innerHeight * 1.5) {
                            const text = normalize(link.innerText || link.textContent || '');
                            if (text.length >= 2 && text.length <= 20 && !text.match(/^(首页|home|more|>>)/i)) {
                                leftLinks.push({ text, href: link.href, top: rect.top });
                            }
                        }
                    }
                    if (leftLinks.length < 3) return null;
                    leftLinks.sort((a, b) => a.top - b.top);
                    const root = { id: 'root', name: 'root', path: '', children: [] };
                    const seen = new Set();
                    for (const item of leftLinks) {
                        if (seen.has(item.text)) continue;
                        seen.add(item.text);
                        root.children.push({ id: nodeId(item.text), name: item.text, path: item.text, href: item.href, children: [], isLeaf: true });
                    }
                    if (root.children.length >= 3) {
                        return { root, leaf_paths: root.children.map(c => c.path), detection_method: 'link_hierarchy' };
                    }
                    return null;
                };

                // 按优先级尝试
                for (const fn of [tryElementTree, tryAriaRoles, tryNavElement, trySidebar, tryNestedList, tryLinkHierarchy]) {
                    try {
                        const r = fn();
                        if (r && r.leaf_paths && r.leaf_paths.length > 0) return r;
                    } catch (e) {}
                }
                return null;
            }""",
                max_depth
            )
            
            if result:
                leaf_paths = list(dict.fromkeys([p for p in (result.get("leaf_paths") or []) if p]))
                return {
                    "root": result.get("root"),
                    "leaf_paths": leaf_paths,
                    "detection_method": result.get("detection_method", "unknown")
                }
            return {"root": None, "leaf_paths": [], "detection_method": "none"}
        except Exception as e:
            print(f"[MenuTree] 启发式探测异常: {e}")
            return {"root": None, "leaf_paths": [], "detection_method": "none"}

    async def _llm_menu_detection(self, max_depth: int = 4) -> Dict[str, Any]:
        """LLM 智能识别导航菜单（兜底）。"""
        if not self.page:
            return {"root": None, "leaf_paths": [], "detection_method": "none"}

        try:
            html_content = await self.page.evaluate("""() => {
                const leftArea = document.querySelector('.left, .sidebar, [class*="left"], [class*="side"], nav');
                if (leftArea) return leftArea.outerHTML.slice(0, 10000);
                const body = document.body.cloneNode(true);
                body.querySelectorAll('script, style, iframe, svg, img').forEach(el => el.remove());
                return body.innerHTML.slice(0, 15000);
            }""")
            
            if not html_content or len(html_content) < 100:
                return {"root": None, "leaf_paths": [], "detection_method": "none"}

            from config import Config
            config = Config()
            from openai import OpenAI
            
            client = OpenAI(api_key=config.qwen_api_key, base_url=config.qwen_base_url)
            
            try:
                from prompts import load as load_prompt
            except ImportError:
                from .prompts import load as load_prompt

            prompt = load_prompt(
                "browser/menu_analyze.md",
                html_content=html_content[:8000],
            )
            menu_system_prompt = load_prompt("browser/menu_analyze_system.md").strip()

            response = client.chat.completions.create(
                model=config.qwen_model,
                messages=[
                    {"role": "system", "content": menu_system_prompt},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=2000
            )
            
            result_text = response.choices[0].message.content.strip()
            import re
            json_match = re.search(r'```json\s*(.*?)\s*```', result_text, re.DOTALL)
            if json_match:
                result_text = json_match.group(1)
            
            llm_result = json.loads(result_text)
            
            if not llm_result.get("success") or not llm_result.get("menu_items"):
                return {"root": None, "leaf_paths": [], "detection_method": "none"}
            
            def convert_node(item, path_parts):
                name = item.get("name", "")
                parts = path_parts + [name] if name else path_parts
                path = "/".join(parts)
                children_data = item.get("children", [])
                children = [convert_node(c, parts) for c in children_data if c.get("name")]
                node_id = hashlib.md5(path.encode()).hexdigest()[:12]
                return {"id": node_id, "name": name, "path": path, "children": children, "isLeaf": len(children) == 0}
            
            root_children = [convert_node(item, []) for item in llm_result["menu_items"] if item.get("name")]
            root = {"id": "root", "name": "root", "path": "", "children": root_children}
            
            leaf_paths = []
            def walk(n):
                if n.get("isLeaf") and n.get("path"):
                    leaf_paths.append(n["path"])
                for c in n.get("children", []):
                    walk(c)
            for c in root_children:
                walk(c)
            
            return {"root": root, "leaf_paths": leaf_paths, "detection_method": "llm"}
            
        except Exception as e:
            print(f"[MenuTree] LLM 识别异常: {e}")
            return {"root": None, "leaf_paths": [], "detection_method": "none"}

    def clear_menu_tree_cache(self, domain: str = None):
        """清除目录树缓存。"""
        if domain:
            self._menu_tree_cache.pop(domain, None)
        else:
            self._menu_tree_cache.clear()

    async def capture_mapping_for_leaf_paths(self, leaf_paths: List[str], max_wait: float = 1.5) -> Dict[str, Any]:
        """
        【新】只对给定叶子路径抓包并还原 filters 内分类参数，返回可信映射。
        结果形态与 build_verified_category_mapping 一致，但 key 使用 leaf path。
        """
        if not self.page:
            return {"menu_to_filters": {}, "confidence": "low"}

        menu_to_filters: Dict[str, Dict[str, Any]] = {}
        # 对于“服务端渲染/跳转型”的目录树：点击后只发生 URL 跳转，没有可抓包的 filters API
        # 这种场景用 URL 映射更可信：LLM 可直接遍历多个列表页 URL 抓取
        menu_to_urls: Dict[str, str] = {}
        source_endpoint = ""
        # 额外：收集“每个叶子路径”触发的真实 API 请求样本（提供给 LLM，避免猜测）
        interaction_samples: List[Dict[str, Any]] = []

        def _redact_headers(h: Dict[str, Any] | None) -> Dict[str, Any]:
            """移除可能包含敏感信息的请求头（Cookie/Token 等），避免传给 LLM。"""
            if not isinstance(h, dict):
                return {}
            redacted: Dict[str, Any] = {}
            sensitive = {
                "cookie",
                "authorization",
                "proxy-authorization",
                "x-csrf-token",
                "x-xsrf-token",
                "x-auth-token",
            }
            for k, v in h.items():
                if not k:
                    continue
                lk = str(k).lower()
                if lk in sensitive:
                    continue
                redacted[k] = v
            return redacted

        def score_api(url: str) -> int:
            u = url.lower()
            score = 0
            if "filters=" in u:
                score += 5
            if "pageno=" in u or "pagesize=" in u:
                score += 2
            if "/page" in u or "page?" in u:
                score += 2
            if "/list" in u:
                score += 1
            return score

        def _is_valid_http_url(u: str) -> bool:
            try:
                if not u or not isinstance(u, str):
                    return False
                lu = u.strip().lower()
                if lu.startswith("javascript:") or lu == "#" or lu.startswith("#"):
                    return False
                p = parse.urlparse(u)
                return p.scheme in ("http", "https") and bool(p.netloc)
            except Exception:
                return False

        def _looks_like_data_api(req: Dict[str, Any]) -> bool:
            """
            更严格地挑选“像数据接口”的请求，避免宽松收集把静态/埋点也算进来。
            不改变 self.api_requests 的收集规则，只在这里筛选 best。
            """
            if not isinstance(req, dict):
                return False
            url = str(req.get("url") or "")
            if not url:
                return False
            method = str(req.get("method") or "").upper()
            # 响应状态优先（如果有）
            status = req.get("response_status")
            if isinstance(status, int) and status >= 400:
                return False

            headers = req.get("response_headers") or {}
            ctype = ""
            if isinstance(headers, dict):
                ctype = str(headers.get("content-type") or "").lower()

            u = url.lower()
            # 明显的列表参数/filters：强信号
            if "filters=" in u or "pageno=" in u or "pagesize=" in u:
                return True
            # JSON 响应：强信号
            if "application/json" in ctype or "json" in ctype:
                return True
            # POST 往往是数据接口（但可能是埋点），给中等权重
            if method == "POST":
                return True
            # 如果有字段结构，说明解析出 JSON
            if req.get("response_field_structure"):
                return True
            # preview 看起来像 JSON
            preview = str(req.get("response_preview") or "").lstrip()
            if preview.startswith("{") or preview.startswith("["):
                return True
            return False

        def _score_strict_api(req: Dict[str, Any]) -> int:
            url = str(req.get("url") or "")
            base = score_api(url)
            headers = req.get("response_headers") or {}
            ctype = ""
            if isinstance(headers, dict):
                ctype = str(headers.get("content-type") or "").lower()
            if "application/json" in ctype or "json" in ctype:
                base += 8
            status = req.get("response_status")
            if isinstance(status, int) and 200 <= status < 300:
                base += 3
            if req.get("response_field_structure"):
                base += 3
            if str(req.get("method") or "").upper() == "POST":
                base += 2
            return base

        async def _safe_hover_menuitem(name: str, timeout_ms: int = 1500) -> None:
            """尽量 hover 顶层菜单，但要快速失败，避免卡住。"""
            if not self.page:
                return
            try:
                await self.page.get_by_role("menuitem", name=name).hover(timeout=timeout_ms)
            except Exception:
                # role 不存在或不可用时直接忽略
                return

        async def _safe_click_selector(selector: str, timeout_ms: int = 1500) -> bool:
            """优先用枚举阶段产出的 selector 点击（比文本更稳定）。"""
            if not self.page or not selector:
                return False
            try:
                await self.page.locator(selector).first.click(timeout=timeout_ms, no_wait_after=True)
                return True
            except Exception:
                return False

        async def _safe_goto(url: str, timeout_ms: int = 8000) -> bool:
            """优先用 href 直接导航（SSR/跳转型菜单最稳）。"""
            if not self.page or not url:
                return False
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                return True
            except Exception:
                return False

        async def _safe_click_label(label: str, timeout_ms: int = 1500) -> None:
            """
            点击一个菜单标签（两种路径）：
            1) 优先 ARIA role=menuitem
            2) 回退到文本/链接点击（适配 chinabond 这种普通 <a> 菜单）
            必须快速失败，避免 Playwright 默认等待导致卡住。
            """
            if not self.page:
                return
            # 1) ARIA
            try:
                await self.page.get_by_role("menuitem", name=label).click(timeout=timeout_ms, no_wait_after=True)
                return
            except Exception:
                pass

            # 2) 文本/链接回退：先点 <a>，再点任意包含该文本的元素
            try:
                await self.page.locator(f"a:has-text(\"{label}\")").first.click(timeout=timeout_ms, no_wait_after=True)
                return
            except Exception:
                pass
            try:
                await self.page.get_by_text(label, exact=True).first.click(timeout=timeout_ms, no_wait_after=True)
                return
            except Exception:
                return

        async def click_path(path: str, clear_before_leaf: bool = True):
            """
            点击路径的各层级，触发对应 API 请求。
            改进：在点击叶子级之前清空已捕获的请求，确保只捕获叶子级触发的 API。
            """
            parts = [p for p in path.split("/") if p]
            if not parts:
                return
            # 1) SSR/跳转型：如果叶子节点本身有 href，直接 goto（不需要硬点 DOM）
            meta_leaf = (self._menu_path_meta or {}).get(path) if isinstance(getattr(self, "_menu_path_meta", None), dict) else None
            href = meta_leaf.get("href") if isinstance(meta_leaf, dict) else None
            if href:
                # 对于 SSR 跳转，在 goto 前清空请求
                if clear_before_leaf:
                    self._clear_captured_requests()
                await _safe_goto(href, timeout_ms=8000)
                return

            # 2) SPA/无 href：逐层点击 prefix（优先 selector，其次文本兜底）
            # 顶层 hover（仅提升 ARIA 菜单命中率；普通菜单无影响）
            await _safe_hover_menuitem(parts[0], timeout_ms=1200)
            await asyncio.sleep(0.1)

            prefix_parts: List[str] = []
            for idx, p in enumerate(parts):
                prefix_parts.append(p)
                prefix_path = "/".join(prefix_parts)
                
                # 关键改进：在点击叶子级之前清空请求，确保只捕获叶子触发的 API
                is_leaf = (idx == len(parts) - 1)
                if is_leaf and clear_before_leaf:
                    self._clear_captured_requests()
                    await asyncio.sleep(0.2)  # 等待父级请求完成后再清空
                
                meta = (self._menu_path_meta or {}).get(prefix_path) if isinstance(getattr(self, "_menu_path_meta", None), dict) else None
                sel = meta.get("selector") if isinstance(meta, dict) else None
                clicked = False
                if sel:
                    clicked = await _safe_click_selector(sel, timeout_ms=1800)
                if not clicked:
                    await _safe_click_label(p, timeout_ms=1800)
                await asyncio.sleep(0.15)

        for path in leaf_paths:
            # 注意：请求清空已移到 click_path 内部（在点击叶子级之前），确保只捕获叶子触发的 API

            # 如果目录树枚举阶段已经拿到了叶子 href，优先把它当成 URL 型映射（最稳）
            try:
                meta_leaf = (self._menu_path_meta or {}).get(path) if isinstance(getattr(self, "_menu_path_meta", None), dict) else None
                href = meta_leaf.get("href") if isinstance(meta_leaf, dict) else None
                if href and _is_valid_http_url(href):
                    menu_to_urls[path] = href
            except Exception:
                pass

            before_url = ""
            try:
                before_url = self.page.url if self.page else ""
            except Exception:
                before_url = ""

            await click_path(path)
            # 兼容“点击触发导航”的页面：不要让 click 阻塞等待导航（上面 no_wait_after=True）
            # 这里统一小等待 + 尝试等待 domcontentloaded（短超时）
            await asyncio.sleep(max_wait)
            try:
                if self.page:
                    await self.page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass

            after_url = ""
            try:
                after_url = self.page.url if self.page else ""
            except Exception:
                after_url = ""

            # 只要 URL 发生了变化，就记录为 URL 型映射（不依赖 api_requests 是否为空）
            # 这样 chinabond 这类“板块=不同 URL”即便加载过程中有若干请求，也能稳定产出 menu_to_urls
            if after_url and before_url and after_url != before_url:
                if _is_valid_http_url(after_url):
                    menu_to_urls[path] = after_url
                # 记录一个“交互样本”给 LLM：这是跳转行为（可能同时也触发 API）
                interaction_samples.append({
                    "menu_text": path,
                    "menu_selector": None,
                    "apis": [],
                    "navigation": {"from": before_url, "to": after_url}
                })

            # 取最像列表接口的 API
            best = None
            best_score = -1

            strict_candidates = [r for r in (self.api_requests or []) if _looks_like_data_api(r)]
            candidate_pool = strict_candidates if strict_candidates else list(self.api_requests or [])

            for api in candidate_pool:
                url = api.get("url", "")
                s = _score_strict_api(api) if candidate_pool is strict_candidates else score_api(url)
                if s > best_score:
                    best_score = s
                    best = api
            if not best:
                continue

            url = best.get("url", "")
            params = self._extract_url_params(url)
            filters_raw = params.get("filters")
            filters_obj = self._try_parse_json(filters_raw) if filters_raw else None
            if isinstance(filters_obj, dict):
                candidate = {}
                for k, v in filters_obj.items():
                    # 只保留“像分类参数”的 key
                    if self._is_likely_category_param(k, [str(v)]):
                        candidate[k] = v
                if candidate:
                    menu_to_filters[path] = candidate
                    if not source_endpoint:
                        source_endpoint = url.split("?", 1)[0]

            # 无论是否抽取到 candidate，都记录该路径触发的“最佳 API 请求样本”给 LLM 参考
            try:
                best_slim = dict(best)
                # 脱敏请求头
                best_slim["headers"] = _redact_headers(best_slim.get("headers"))
                # 限制 preview 体积，避免 prompt 过大
                if best_slim.get("response_preview"):
                    best_slim["response_preview"] = str(best_slim.get("response_preview", ""))[:1200]
                interaction_samples.append({
                    "menu_text": path,
                    "menu_selector": None,
                    "apis": [best_slim],
                })
            except Exception:
                pass

        # 只要我们拿到了 URL 映射，也算“中等以上”可信（至少不靠猜）
        total_hits = len(menu_to_filters) + len(menu_to_urls)
        confidence = "high" if total_hits >= 3 else ("medium" if total_hits >= 1 else "low")
        return {
            "menu_to_filters": menu_to_filters,
            "menu_to_urls": menu_to_urls,
            "source_endpoint": source_endpoint,
            "confidence": confidence,
            # 供 LLM 使用：点击每个选中叶子后触发的真实 API 请求样本
            "interaction_apis": {
                "initial_apis": [],
                "interaction_apis": interaction_samples,
                "all_unique_apis": [it["apis"][0] for it in interaction_samples if it.get("apis")]
            }
        }

    def _clear_captured_requests(self):
        """清空捕获的请求记录"""
        self.network_requests.clear()
        self.api_requests.clear()
        self._cdp_request_map.clear()
        self._pending_request_ids.clear()

    async def disconnect(self):
        """断开连接"""
        try:
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("✓ CDP 连接已关闭")
        except Exception as e:
            print(f"⚠️  关闭连接时出错: {e}")

    async def open(self, url: str, wait_until: str = "domcontentloaded") -> tuple:
        """
        打开 URL
        
        Returns:
            tuple: (success: bool, error_message: str | None)
        """
        if not self.page:
            ok = await self._ensure_live_page()
            if not ok or not self.page:
                return False, "未连接到浏览器"

        # 清空之前的请求记录
        self._clear_captured_requests()
        self.interaction_api_records.clear()

        # 对于 hash 路由的 URL，使用更宽松的等待策略
        has_hash = "#" in url
        
        # 优化等待策略：默认不再使用 networkidle，因为太容易超时
        # 优先使用 domcontentloaded，这通常足够开始分析 DOM
        strategies = ["domcontentloaded", "load", "commit"]
        
        last_error = None
        
        for strategy in strategies:
            try:
                print(f"→ 正在打开: {url} (等待策略: {strategy})")
                # 增加超时时间到 60s
                await self.page.goto(url, wait_until=strategy, timeout=self.timeout)

                # 额外等待一会，确保动态内容加载完成
                # hash 路由的 SPA 需要更长时间等待 JS 渲染
                wait_time = 4 if has_hash else 3
                await asyncio.sleep(wait_time)

                print(f"✓ 页面已加载")
                return True, None
                
            except Exception as e:
                error_msg = str(e)
                last_error = error_msg

                # 如果是超时错误，但页面 URL 已经匹配，或者页面已有内容，视为成功
                if "Timeout" in error_msg:
                    try:
                        current_url = self.page.url
                        content_len = len(await self.page.content())
                        if content_len > 2000:
                            print(f"⚠ 导航超时 ({strategy})，但页面已有内容 ({content_len} 字符)，视为成功")
                            return True, None
                    except:
                        pass

                # 如果 page/context/browser 被关闭：尝试复活并继续尝试下一个策略
                if "Target page, context or browser has been closed" in error_msg or "has been closed" in error_msg:
                    print(f"⚠ 检测到浏览器/页面被关闭，尝试重建并重试... ({strategy})")
                    try:
                        await asyncio.sleep(0.5)
                        await self._ensure_live_page()
                    except Exception:
                        pass
                    continue
                
                # 如果是 ERR_ABORTED 且有 hash，这通常是正常的 SPA 行为
                # 尝试下一个策略
                if "ERR_ABORTED" in error_msg and has_hash:
                    print(f"⚠ 策略 {strategy} 失败（SPA hash 路由），尝试下一个...")
                    continue
                
                # 如果是 "interrupted by another navigation"，这是 SPA 路由器的正常行为
                # 页面实际上已经加载成功了，检查页面内容
                if "interrupted by another navigation" in error_msg:
                    print(f"⚠ 导航被 SPA 路由器中断，检查页面是否已加载...")
                    await asyncio.sleep(2)  # 等待 SPA 完成路由
                    try:
                        content = await self.page.content()
                        if len(content) > 1000:
                            print(f"✓ 页面已加载（HTML: {len(content)} 字符）")
                            return True, None
                    except:
                        pass
                    # 如果页面没有加载成功，尝试下一个策略
                    continue
                    
                # 其他错误直接返回
                print(f"✗ 打开页面失败: {error_msg}")
                return False, error_msg
        
        # 所有策略都失败了，但对于 hash URL，页面可能已经加载
        # 检查页面是否可用
        if has_hash:
            try:
                # 检查页面是否有内容
                content = await self.page.content()
                if len(content) > 1000:  # 有实质内容
                    print(f"⚠ 导航有警告，但页面已加载（HTML: {len(content)} 字符）")
                    await asyncio.sleep(2)
                    return True, None
            except:
                pass
        
        print(f"✗ 打开页面失败: {last_error}")
        return False, last_error

    async def take_screenshot_base64(self) -> str:
        """获取当前页面截图（Base64编码）"""
        if not self.page:
            return ""
        
        try:
            # 截取完整页面或视口
            # full_page=True 可能导致截图过大，对于 LLM 分析，通常首屏/视口更重要且节省 token
            # 但为了看到底部菜单，我们还是尝试滚动一下再截，或者截取 full_page
            # 这里选择 full_page=False (仅视口) 但先滚动到底部再回滚，或者直接截取 full_page=True 但限制质量
            # 权衡：full_page 对长页面消耗极大。对于“目录树决策”，通常菜单在顶部或侧边栏。
            # 策略：直接截取当前视口即可，大多数导航都在首屏。
            
            # 稍微滚动一下以确保懒加载元素出现（如果菜单是动态加载的）
            # await self.page.evaluate("window.scrollBy(0, 100)")
            # await asyncio.sleep(0.5)
            # await self.page.evaluate("window.scrollTo(0, 0)")
            
            buffer = await self.page.screenshot(full_page=False, type='jpeg', quality=60)
            import base64
            return base64.b64encode(buffer).decode('utf-8')
        except Exception as e:
            print(f"截图失败: {e}")
            return ""

    async def get_full_html(self) -> str:
        """获取完整的页面HTML（无长度限制）"""
        if not self.page:
            return ""

        try:
            return await self.page.content()
        except Exception as e:
            print(f"获取HTML失败: {e}")
            return ""

    async def get_page_info(self) -> Dict[str, Any]:
        """获取页面基本信息"""
        if not self.page:
            return {}

        try:
            return {
                "url": self.page.url,
                "title": await self.page.title(),
            }
        except Exception as e:
            print(f"获取页面信息失败: {e}")
            return {}

    async def detect_data_status(self) -> Dict[str, Any]:
        """
        【增强功能1】检测页面数据加载状态
        
        Returns:
            包含数据状态信息的字典：
            - hasData: 是否有实际数据（更严格的判断）
            - tableRowCount: 表格数据行数
            - listItemCount: 真正的数据列表项数量（排除导航）
            - potentialMenus: 可能的分类菜单
            - emptyIndicators: 空数据指示信息
            - needsInteraction: 是否需要交互才能加载数据
        """
        if not self.page:
            return {"hasData": False, "error": "未连接到浏览器"}
        
        try:
            data_status = await self.page.evaluate("""
            () => {
                const result = {
                    hasData: false,
                    tableRowCount: 0,
                    listItemCount: 0,
                    potentialMenus: [],
                    emptyIndicators: [],
                    needsInteraction: false
                };
                
                // 1. 检查表格数据（更严格：必须有多行数据）
                const tableRows = document.querySelectorAll('table tbody tr, table tr');
                let dataRows = 0;
                tableRows.forEach(tr => {
                    const tds = tr.querySelectorAll('td');
                    // 必须有多个单元格，且内容有意义
                    if (tds.length >= 2) {
                        const text = tr.textContent.trim();
                        // 排除空行和无数据提示行
                        if (text.length > 20 && !text.includes('暂无') && !text.includes('没有数据') && !text.includes('无记录')) {
                            dataRows++;
                        }
                    }
                });
                result.tableRowCount = dataRows;
                
                // 2. 检查真正的数据列表（排除导航菜单）
                // 更严格的选择器：只匹配看起来像数据列表的元素
                const dataListSelectors = [
                    '[class*="data"] > div',
                    '[class*="result"] > div', 
                    '[class*="content"] > div[class*="item"]',
                    '[class*="list-item"]',
                    '[class*="record"]',
                    'main [class*="item"]'
                ];
                
                let validDataItems = 0;
                const seenContents = new Set();
                
                dataListSelectors.forEach(selector => {
                    document.querySelectorAll(selector).forEach(item => {
                        const text = item.textContent.trim();
                        // 数据项通常较长且不重复
                        if (text.length > 30 && !seenContents.has(text.slice(0, 50))) {
                            seenContents.add(text.slice(0, 50));
                            // 排除明显的导航/菜单项
                            const isNav = item.closest('nav, header, footer, [class*="menu"], [class*="nav"]');
                            if (!isNav) {
                                validDataItems++;
                            }
                        }
                    });
                });
                result.listItemCount = validDataItems;
                
                // 3. 判断是否有真正的数据（更严格）
                // 必须有至少3行表格数据，或至少5个有效的数据列表项
                result.hasData = (dataRows >= 3 || validDataItems >= 5);
                
                // 4. 检测"无数据"指示器
                const emptyPatterns = ['暂无数据', '没有数据', '无记录', 'no data', 'no results', '0条', '共0', '0 条'];
                document.querySelectorAll('div, span, p, td').forEach(el => {
                    const text = el.textContent.trim().toLowerCase();
                    if (text.length < 50) {
                        emptyPatterns.forEach(pattern => {
                            if (text.includes(pattern.toLowerCase())) {
                                result.emptyIndicators.push(text);
                            }
                        });
                    }
                });
                result.emptyIndicators = [...new Set(result.emptyIndicators)].slice(0, 5);
                
                // 5. 检测可点击的分类/筛选菜单
                const menuSelectors = [
                    '[role="menuitem"]',
                    '[role="tab"]',
                    '[class*="menu-item"]',
                    '[class*="el-menu"] li',
                    '[class*="ant-menu"] li',
                    '[class*="tab-item"]',
                    '[class*="category"] a',
                    '[class*="category"] span',
                    '[class*="filter"] a',
                    '[class*="filter"] li'
                ];
                
                const seenTexts = new Set();
                menuSelectors.forEach(selector => {
                    document.querySelectorAll(selector).forEach(el => {
                        const text = el.textContent.trim();
                        // 过滤有效的菜单项（排除通用导航）
                        const skipKeywords = ['首页', '关于', '联系', '登录', '注册', 'home', 'about', 'contact', 'login'];
                        const isSkip = skipKeywords.some(kw => text.toLowerCase().includes(kw));
                        
                        if (text.length > 0 && text.length < 30 && 
                            el.offsetParent !== null && !seenTexts.has(text) && !isSkip) {
                            seenTexts.add(text);
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                result.potentialMenus.push({
                                    text: text,
                                    tag: el.tagName.toLowerCase(),
                                    selector: el.id ? `#${el.id}` : 
                                             (el.className && typeof el.className === 'string') ? 
                                             `${el.tagName.toLowerCase()}.${el.className.split(' ')[0]}` : null,
                                    role: el.getAttribute('role') || ''
                                });
                            }
                        }
                    });
                });
                
                result.potentialMenus = result.potentialMenus.slice(0, 50);
                
                // 6. 判断是否需要交互
                // 条件：无数据 且 有菜单项，或者 有很多菜单项（说明是SPA分类页面）
                result.needsInteraction = (!result.hasData && result.potentialMenus.length > 0) || 
                                          result.potentialMenus.length >= 5;
                
                return result;
            }
            """)
            
            return data_status
            
        except Exception as e:
            print(f"检测数据状态失败: {e}")
            return {"hasData": False, "error": str(e), "needsInteraction": True}

    async def capture_api_with_interactions(self, max_interactions: int = 5, force: bool = False) -> Dict[str, Any]:
        """
        【增强功能2】通过交互捕获不同状态的API请求
        
        Args:
            max_interactions: 最大交互次数
            force: 是否强制执行交互探测（即使检测到有数据）
            
        Returns:
            包含不同交互状态下API请求的字典
        """
        if not self.page:
            return {"error": "未连接到浏览器"}
        
        result = {
            "initial_apis": list(self.api_requests),
            "interaction_apis": [],
            "all_unique_apis": []
        }
        
        # 获取可交互的菜单元素
        data_status = await self.detect_data_status()
        menus = data_status.get("potentialMenus", [])
        needs_interaction = data_status.get("needsInteraction", False)
        
        if not menus:
            print("  未检测到可交互的菜单元素")
            return result
        
        # 如果不需要交互且没有强制执行，跳过
        if not force and not needs_interaction and data_status.get("hasData") and len(self.api_requests) > 0:
            print("  页面已有完整数据，跳过交互探测")
            return result
        
        print(f"  检测到 {len(menus)} 个潜在菜单项，开始交互探测...")
        
        # 尝试点击菜单项
        interactions_done = 0
        for menu in menus[:max_interactions]:
            try:
                menu_text = menu.get("text", "")
                
                # 跳过一些通用导航菜单
                skip_keywords = ['首页', '关于', '联系', '搜索', '登录', '注册', 'home', 'about', 'contact']
                if any(kw in menu_text.lower() for kw in skip_keywords):
                    continue
                
                # 清空请求记录
                before_count = len(self.api_requests)
                self._clear_captured_requests()
                
                # 尝试点击
                selector = menu.get("selector")
                clicked = False
                
                if selector:
                    try:
                        element = await self.page.query_selector(selector)
                        if element:
                            await element.click()
                            clicked = True
                    except:
                        pass
                
                if not clicked:
                    # 通过文本查找
                    try:
                        element = await self.page.query_selector(f'text="{menu_text}"')
                        if element:
                            await element.click()
                            clicked = True
                    except:
                        pass
                
                if clicked:
                    await asyncio.sleep(1.5)  # 等待API响应
                    
                    if self.api_requests:
                        result["interaction_apis"].append({
                            "menu_text": menu_text,
                            "menu_selector": selector,
                            "apis": list(self.api_requests)
                        })
                        print(f"    ✓ 点击 [{menu_text}] 捕获到 {len(self.api_requests)} 个API请求")
                        interactions_done += 1
                        
            except Exception as e:
                continue
        
        # 合并所有唯一的API
        all_apis = result["initial_apis"].copy()
        for interaction in result["interaction_apis"]:
            for api in interaction.get("apis", []):
                # 检查是否已存在（通过URL基础部分判断）
                base_url = api.get("url", "").split("?")[0]
                existing_bases = [a.get("url", "").split("?")[0] for a in all_apis]
                if base_url not in existing_bases:
                    all_apis.append(api)
        
        result["all_unique_apis"] = all_apis
        result["interactions_count"] = interactions_done
        
        return result

    def analyze_api_parameters(self, captured_apis: Dict[str, Any]) -> Dict[str, Any]:
        """
        【增强功能3】分析不同交互下API参数的差异，识别必需参数
        
        Args:
            captured_apis: capture_api_with_interactions 的返回结果
            
        Returns:
            API参数分析结果
        """
        analysis = {
            "common_params": {},       # 所有请求都有的参数
            "variable_params": {},     # 随交互变化的参数
            "category_params": [],     # 识别为分类ID的参数
            "api_endpoints": [],       # API端点列表
            "param_patterns": {},      # 参数模式
            "filters_diff": {},        # filters(JSON) 内层差异（关键）
        }
        
        # 收集所有API的参数
        all_params_list = []
        
        # 处理初始API
        for api in captured_apis.get("initial_apis", []):
            params = self._extract_url_params(api.get("url", ""))
            if params:
                all_params_list.append({
                    "source": "initial",
                    "url": api.get("url", ""),
                    "params": params
                })
        
        # 处理交互后的API
        for interaction in captured_apis.get("interaction_apis", []):
            menu_text = interaction.get("menu_text", "")
            for api in interaction.get("apis", []):
                params = self._extract_url_params(api.get("url", ""))
                if params:
                    all_params_list.append({
                        "source": menu_text,
                        "url": api.get("url", ""),
                        "params": params
                    })
        
        if not all_params_list:
            return analysis
        
        # 提取所有唯一的API端点
        endpoints = list(set(p.get("url", "").split("?")[0] for p in all_params_list))
        analysis["api_endpoints"] = endpoints
        
        # 分析每个端点的参数模式
        for endpoint in endpoints:
            endpoint_params = [p for p in all_params_list if p["url"].split("?")[0] == endpoint]
            
            if len(endpoint_params) < 1:
                continue
                
            # 找出所有参数键
            all_keys = set()
            for ep in endpoint_params:
                all_keys.update(ep["params"].keys())
            
            # 分析每个参数
            for key in all_keys:
                values = []
                sources = []
                for ep in endpoint_params:
                    if key in ep["params"]:
                        values.append(ep["params"][key])
                        sources.append(ep["source"])
                
                unique_values = list(set(values))
                
                # 判断参数类型
                if len(unique_values) == 1:
                    # 固定值参数
                    if key not in analysis["common_params"]:
                        analysis["common_params"][key] = unique_values[0]
                else:
                    # 变化的参数 - 可能是分类ID
                    analysis["variable_params"][key] = {
                        "values": unique_values[:10],
                        "sources": sources[:10],
                        "likely_category": self._is_likely_category_param(key, unique_values)
                    }
                    
                    if self._is_likely_category_param(key, unique_values):
                        analysis["category_params"].append({
                            "param_name": key,
                            "sample_values": unique_values[:5],
                            "menu_mapping": dict(zip(sources[:5], values[:5]))
                        })

            # 特殊处理：filters 参数通常是 JSON，分类 ID 往往藏在内层
            if "filters" in all_keys:
                filters_rows = []
                for ep in endpoint_params:
                    if "filters" in ep["params"]:
                        parsed = self._try_parse_json(ep["params"]["filters"])
                        if isinstance(parsed, dict):
                            filters_rows.append({
                                "source": ep["source"],
                                "filters": parsed
                            })
                if filters_rows:
                    diff = self._diff_filters(filters_rows)
                    if diff:
                        analysis["filters_diff"][endpoint] = diff
                        # 将内层差异也作为 category_params 输出，供上层直接生成映射
                        for k, info in diff.items():
                            if info.get("likely_category"):
                                analysis["category_params"].append({
                                    "param_name": f"filters.{k}",
                                    "sample_values": info.get("values", [])[:5],
                                    "menu_mapping": info.get("menu_mapping", {})
                                })
        
        return analysis
    
    def _extract_url_params(self, url: str) -> Dict[str, str]:
        """从URL中提取查询参数"""
        if "?" not in url:
            return {}
        
        query_string = url.split("?", 1)[1]
        params = {}
        
        for item in query_string.split("&"):
            if "=" in item:
                key, value = item.split("=", 1)
                # URL解码
                key = parse.unquote(key)
                value = parse.unquote(value)
                params[key] = value
        
        return params

    def _try_parse_json(self, s: str) -> Any:
        """尽力解析 JSON 字符串（可能是 URL 编码后的 JSON）"""
        if not isinstance(s, str):
            return None
        raw = s.strip()
        if not raw:
            return None
        # 有些 filters 可能被多次编码
        for _ in range(2):
            try:
                if (raw.startswith("{") and raw.endswith("}")) or (raw.startswith("[") and raw.endswith("]")):
                    return json.loads(raw)
            except Exception:
                pass
            raw = parse.unquote(raw)
        return None

    def _diff_filters(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        对 filters 的内层 key 做差异分析：
        - 哪些 key 随菜单变化（大概率是分类/筛选 ID）
        - 给出 menu_mapping（菜单→该 key 的值）
        """
        # 收集所有 key
        keys = set()
        for r in rows:
            keys.update(r.get("filters", {}).keys())

        diff: Dict[str, Any] = {}
        for k in keys:
            values = []
            menu_mapping: Dict[str, Any] = {}
            for r in rows:
                src = r.get("source", "")
                v = r.get("filters", {}).get(k)
                if v is None:
                    continue
                values.append(v)
                # 只保留可序列化的简单值作为映射
                if isinstance(v, (str, int, float, bool)) or v is None:
                    menu_mapping[src] = v
                else:
                    # dict/list 的话先转短字符串，避免爆长
                    try:
                        menu_mapping[src] = json.dumps(v, ensure_ascii=False)[:200]
                    except Exception:
                        menu_mapping[src] = str(v)[:200]

            uniq = []
            for v in values:
                if v not in uniq:
                    uniq.append(v)

            if len(uniq) <= 1:
                continue

            likely = self._is_likely_category_param(k, [str(x) for x in uniq if x is not None])
            diff[k] = {
                "values": [str(x) if x is not None else "" for x in uniq][:10],
                "menu_mapping": menu_mapping,
                "likely_category": likely
            }
        return diff

    def build_verified_category_mapping(self, captured_apis: Dict[str, Any]) -> Dict[str, Any]:
        """
        从“交互抓包”中构建可信分类映射（只来源于真实请求，不允许模型猜）
        返回：
        {
          "menu_to_filters": { "主体评级": {"levelone":"73",...}, ... },
          "source_endpoint": ".../page",
          "confidence": "high|medium|low"
        }
        """
        interaction_apis = captured_apis.get("interaction_apis", [])
        if not interaction_apis:
            return {"menu_to_filters": {}, "confidence": "low"}

        menu_to_filters: Dict[str, Dict[str, Any]] = {}
        source_endpoint = ""

        # 选择最像“列表分页接口”的请求：包含 filters 且 url 含 page / list
        def score_api(url: str) -> int:
            u = url.lower()
            score = 0
            if "filters=" in u:
                score += 5
            if "pageno=" in u or "pagesize=" in u:
                score += 2
            if "/page" in u or "page?" in u:
                score += 2
            if "/list" in u:
                score += 1
            return score

        for inter in interaction_apis:
            menu_text = inter.get("menu_text", "") or "未知菜单"
            best = None
            best_score = -1
            for api in inter.get("apis", []):
                url = api.get("url", "")
                s = score_api(url)
                if s > best_score:
                    best_score = s
                    best = api
            if not best:
                continue

            url = best.get("url", "")
            params = self._extract_url_params(url)
            filters_raw = params.get("filters")
            filters_obj = self._try_parse_json(filters_raw) if filters_raw else None
            if isinstance(filters_obj, dict):
                # 只保留“像分类参数”的 key，避免把 launchedstatus 等固定项也塞进去
                candidate = {}
                for k, v in filters_obj.items():
                    if self._is_likely_category_param(k, [str(v)]):
                        candidate[k] = v
                if candidate:
                    menu_to_filters[menu_text] = candidate
                    if not source_endpoint:
                        source_endpoint = url.split("?", 1)[0]

        confidence = "high" if len(menu_to_filters) >= 3 else ("medium" if len(menu_to_filters) >= 1 else "low")
        return {
            "menu_to_filters": menu_to_filters,
            "source_endpoint": source_endpoint,
            "confidence": confidence
        }
    
    def _is_likely_category_param(self, key: str, values: List[str]) -> bool:
        """判断参数是否可能是分类ID"""
        # 参数名称特征
        category_keywords = ['level', 'type', 'category', 'class', 'id', 'filter', 'kind', 'group', 'tab']
        key_lower = key.lower()
        
        if any(kw in key_lower for kw in category_keywords):
            return True
        
        # 值特征：数字ID或JSON格式
        for value in values:
            # 纯数字ID
            if value.isdigit():
                return True
            # JSON格式
            if value.startswith("{") or value.startswith("["):
                return True
        
        return False

    def _extract_json_field_structure(self, json_data: Any, max_depth: int = 2, current_depth: int = 0, max_fields: int = 30) -> Dict[str, Any]:
        """
        从 JSON 数据中提取字段结构，包括字段名、类型和示例值
        特别关注日期相关字段
        
        Args:
            json_data: JSON 数据（dict 或 list）
            max_depth: 最大递归深度（降低到 2 以提高性能）
            current_depth: 当前深度
            max_fields: 最大字段数量限制
            
        Returns:
            字段结构描述
        """
        if current_depth >= max_depth:
            return {"_truncated": True}
        
        result = {}
        field_count = 0
        
        # 日期相关关键词（预编译正则以提高性能）
        date_keywords = {'date', 'time', 'day', 'created', 'updated', 'publish', 'release', 
                         'input', 'add', 'modify', 'rankdate', 'createtime',
                         'updatetime', 'publishtime', 'addtime', 'inputtime', 'releasetime'}
        
        # 预编译日期正则模式
        import re
        date_pattern = re.compile(r'(\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2})|(\d{4}年\d{1,2}月\d{1,2}日)|(\d{13,})')
        
        if isinstance(json_data, dict):
            for key, value in json_data.items():
                # 限制字段数量
                if field_count >= max_fields:
                    result["_more_fields"] = f"... 还有 {len(json_data) - field_count} 个字段被截断"
                    break
                
                field_count += 1
                key_lower = key.lower()
                field_info = {"type": type(value).__name__}
                
                # 检测是否是日期字段（通过关键词）
                is_date_field = any(kw in key_lower for kw in date_keywords)
                if is_date_field:
                    field_info["likely_date"] = True
                
                if value is None:
                    field_info["example"] = None
                elif isinstance(value, (str, int, float, bool)):
                    # 基本类型：提供示例值（限制长度）
                    example = str(value)[:80] if isinstance(value, str) else value
                    field_info["example"] = example
                    
                    # 通过值的格式检测日期
                    if isinstance(value, str) and len(value) <= 30:
                        if date_pattern.search(value):
                            field_info["likely_date"] = True
                                
                elif isinstance(value, list):
                    field_info["type"] = "list"
                    field_info["length"] = len(value)
                    # 只分析第一个元素，且只递归一层
                    if value and len(value) > 0 and current_depth < max_depth - 1:
                        first_item = value[0]
                        if isinstance(first_item, dict):
                            field_info["item_structure"] = self._extract_json_field_structure(
                                first_item, max_depth, current_depth + 1, max_fields=15
                            )
                        else:
                            field_info["item_type"] = type(first_item).__name__
                            if isinstance(first_item, str):
                                field_info["item_example"] = first_item[:50]
                            
                elif isinstance(value, dict):
                    field_info["type"] = "object"
                    # 只递归一层，限制嵌套字段数
                    if current_depth < max_depth - 1:
                        field_info["fields"] = self._extract_json_field_structure(
                            value, max_depth, current_depth + 1, max_fields=15
                        )
                
                result[key] = field_info
                
        elif isinstance(json_data, list) and json_data:
            # 分析列表的第一个元素
            first_item = json_data[0]
            if isinstance(first_item, dict):
                result = {
                    "_list_of": "objects",
                    "_length": len(json_data),
                    "_item_structure": self._extract_json_field_structure(
                        first_item, max_depth, current_depth + 1, max_fields=20
                    )
                }
            else:
                result = {
                    "_list_of": type(first_item).__name__,
                    "_length": len(json_data),
                    "_example": str(first_item)[:50] if isinstance(first_item, str) else first_item
                }
        
        return result

    async def analyze_page_structure(self) -> Dict[str, Any]:
        """
        分析页面结构，提取关键信息供LLM使用

        Returns:
            包含页面结构信息的字典
        """
        if not self.page:
            return {}

        try:
            # 执行JS分析页面结构
            structure = await self.page.evaluate("""
            () => {
                const result = {
                    tables: [],
                    lists: [],
                    links: [],
                    forms: [],
                    pagination: [],
                };

                // 分析表格
                const tableDatePattern = /\d{4}[-\/\.]\d{1,2}[-\/\.]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日/;
                
                const tables = document.querySelectorAll('table');
                tables.forEach((table, idx) => {
                    const rows = table.querySelectorAll('tr');
                    const headers = Array.from(table.querySelectorAll('th')).map(th => th.textContent.trim());
                    const firstRow = rows.length > 1 ?
                        Array.from(rows[1].querySelectorAll('td')).map(td => td.textContent.trim().slice(0, 50)) : [];

                    // 【新增】分析表格结构，检测日期可能出现的列
                    const tableInfo = {
                        index: idx,
                        rows: rows.length,
                        headers: headers.slice(0, 10),
                        firstRowPreview: firstRow.slice(0, 10),
                        selector: table.id ? `#${table.id}` :
                                 table.className ? `table.${table.className.split(' ')[0]}` :
                                 `table:nth-of-type(${idx + 1})`,
                        columnCount: 0,
                        dateColumnIndices: [],
                        dateColumnHints: [],
                        downloadColumnIndices: [],
                    };
                    
                    // 分析多行数据，找出日期和下载链接可能出现的列
                    const dataRows = Array.from(rows).slice(1, 6);  // 取前5行数据
                    const dateColCounts = {};
                    const downloadColCounts = {};
                    
                    dataRows.forEach(row => {
                        const tds = row.querySelectorAll('td');
                        tableInfo.columnCount = Math.max(tableInfo.columnCount, tds.length);
                        
                        tds.forEach((td, colIdx) => {
                            // 检测日期
                            const text = td.textContent.trim();
                            if (tableDatePattern.test(text)) {
                                dateColCounts[colIdx] = (dateColCounts[colIdx] || 0) + 1;
                            }
                            
                            // 检测下载链接
                            const links = td.querySelectorAll('a[href]');
                            links.forEach(link => {
                                const href = (link.getAttribute('href') || '').toLowerCase();
                                if (href.includes('.pdf') || href.includes('.doc') || 
                                    href.includes('download') || href.includes('/uploads/') ||
                                    href.includes('/files/')) {
                                    downloadColCounts[colIdx] = (downloadColCounts[colIdx] || 0) + 1;
                                }
                            });
                        });
                    });
                    
                    // 找出最可能包含日期的列（出现次数 >= 2 的列）
                    for (const [colIdx, count] of Object.entries(dateColCounts)) {
                        if (count >= 2) {
                            tableInfo.dateColumnIndices.push(parseInt(colIdx));
                            // 同时检查这一列的表头
                            if (parseInt(colIdx) < headers.length) {
                                tableInfo.dateColumnHints.push({
                                    columnIndex: parseInt(colIdx),
                                    headerText: headers[parseInt(colIdx)],
                                    occurrences: count
                                });
                            }
                        }
                    }
                    
                    // 找出最可能包含下载链接的列
                    for (const [colIdx, count] of Object.entries(downloadColCounts)) {
                        if (count >= 2) {
                            tableInfo.downloadColumnIndices.push(parseInt(colIdx));
                        }
                    }

                    if (rows.length > 0) {
                        result.tables.push(tableInfo);
                    }
                });

                // 分析列表（ul/ol）
                const listContainers = document.querySelectorAll('ul, ol');
                listContainers.forEach((list, idx) => {
                    const items = list.querySelectorAll('li');
                    if (items.length >= 3) {
                        const firstItemText = items[0] ? items[0].textContent.trim().slice(0, 100) : '';
                        result.lists.push({
                            index: idx,
                            tag: list.tagName.toLowerCase(),
                            itemCount: items.length,
                            firstItemPreview: firstItemText,
                            selector: list.id ? `#${list.id}` :
                                     list.className ? `${list.tagName.toLowerCase()}.${list.className.split(' ')[0]}` :
                                     `${list.tagName.toLowerCase()}:nth-of-type(${idx + 1})`,
                        });
                    }
                });

                // 分析可能是列表的div容器
                const divLists = document.querySelectorAll('div[class*="list"], div[class*="item"], div[class*="row"]');
                const seenParents = new Set();
                divLists.forEach(div => {
                    const parent = div.parentElement;
                    if (parent && !seenParents.has(parent)) {
                        const siblings = parent.querySelectorAll(':scope > div');
                        if (siblings.length >= 3) {
                            seenParents.add(parent);
                            result.lists.push({
                                tag: 'div-container',
                                itemCount: siblings.length,
                                firstItemPreview: siblings[0].textContent.trim().slice(0, 100),
                                selector: parent.id ? `#${parent.id}` :
                                         parent.className ? `div.${parent.className.split(' ')[0]}` : 'div',
                            });
                        }
                    }
                });

                // 分析链接
                const links = document.querySelectorAll('a[href]');
                const pdfLinks = [];
                const reportLinks = [];

                links.forEach(link => {
                    const href = link.getAttribute('href') || '';
                    const text = link.textContent.trim().slice(0, 80);

                    if (href.includes('.pdf') || href.includes('download') || href.includes('file')) {
                        pdfLinks.push({ href, text });
                    }
                    if (text.includes('报告') || text.includes('评级') || text.includes('公告') || text.includes('说明书')) {
                        reportLinks.push({ href: href.slice(0, 200), text });
                    }
                });

                result.links = {
                    pdfLinks: pdfLinks.slice(0, 20),
                    reportLinks: reportLinks.slice(0, 20),
                    totalLinks: links.length,
                };

                // 分析分页
                const paginationKeywords = ['page', '页', '下一页', 'next', 'prev', '上一页', '首页', '末页'];
                const paginationElements = [];

                document.querySelectorAll('a, button, span, div').forEach(el => {
                    const text = el.textContent.trim().toLowerCase();
                    const cls = (el.className || '').toLowerCase();

                    if (paginationKeywords.some(k => text.includes(k) || cls.includes(k))) {
                        paginationElements.push({
                            tag: el.tagName.toLowerCase(),
                            text: el.textContent.trim().slice(0, 30),
                            class: el.className || '',
                        });
                    }
                });

                result.pagination = paginationElements.slice(0, 10);

                // 分析表单
                const forms = document.querySelectorAll('form');
                forms.forEach((form, idx) => {
                    const inputs = form.querySelectorAll('input, select');
                    result.forms.push({
                        index: idx,
                        action: form.action || '',
                        method: form.method || 'get',
                        inputCount: inputs.length,
                        selector: form.id ? `#${form.id}` :
                                 form.className ? `form.${form.className.split(' ')[0]}` :
                                 `form:nth-of-type(${idx + 1})`,
                    });
                });

                // 【新增】分析页面中的日期元素
                // 这对于 API 不返回日期但 HTML 中显示日期的情况很重要
                const dateElements = [];
                const datePattern = /\\d{4}[-\\/\\.]\\d{1,2}[-\\/\\.]\\d{1,2}|\\d{4}年\\d{1,2}月\\d{1,2}日/;
                const dateSelectors = [
                    '[class*="date"]', '[class*="time"]', '[class*="day"]',
                    'span[class*="list"]', 'div[class*="list"]',
                    'td', 'span', 'time', '[datetime]'
                ];
                
                const seenDates = new Set();
                dateSelectors.forEach(selector => {
                    document.querySelectorAll(selector).forEach(el => {
                        const text = el.textContent.trim();
                        if (text.length <= 20 && datePattern.test(text)) {
                            const dateMatch = text.match(datePattern);
                            if (dateMatch && !seenDates.has(dateMatch[0])) {
                                seenDates.add(dateMatch[0]);
                                // 获取选择器
                                let elemSelector = '';
                                if (el.id) {
                                    elemSelector = `#${el.id}`;
                                } else if (el.className && typeof el.className === 'string') {
                                    elemSelector = `${el.tagName.toLowerCase()}.${el.className.split(' ')[0]}`;
                                } else {
                                    elemSelector = el.tagName.toLowerCase();
                                }
                                
                                dateElements.push({
                                    text: text,
                                    dateValue: dateMatch[0],
                                    selector: elemSelector,
                                    tag: el.tagName.toLowerCase(),
                                    className: el.className || ''
                                });
                            }
                        }
                    });
                });
                
                result.dateElements = dateElements.slice(0, 10);

                // 【新增】尝试提取“列表项-日期”关联样本（对 SPA/动态渲染网站非常关键）
                // 目标：给 LLM 一个可对齐的信号（title + date + selector），避免“按顺序硬配对/用 requests 抓不到渲染后HTML”
                const dateItemSamples = [];
                const seenPairs = new Set();

                const makeSelector = (el) => {
                    if (!el) return '';
                    if (el.id) return `#${el.id}`;
                    if (el.className && typeof el.className === 'string') {
                        const cls = el.className.split(' ').filter(Boolean)[0];
                        if (cls) return `${el.tagName.toLowerCase()}.${cls}`;
                    }
                    return el.tagName.toLowerCase();
                };

                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const pickTitleFromContainer = (container) => {
                    if (!container) return '';
                    // 常见标题节点
                    const cand = container.querySelector('[class*="title"], [class*="name"], a, span, div');
                    if (cand) {
                        const t = norm(cand.textContent || '');
                        if (t.length >= 4 && t.length <= 120) return t;
                    }
                    const t2 = norm(container.textContent || '');
                    return t2.slice(0, 120);
                };

                // 优先从已发现的 dateElements 反推容器，避免全量遍历 DOM
                for (const de of dateElements) {
                    if (dateItemSamples.length >= 12) break;
                    const selector = de.selector;
                    if (!selector) continue;
                    const el = document.querySelector(selector);
                    if (!el) continue;
                    const text = norm(el.textContent || '');
                    const m = text.match(datePattern);
                    if (!m) continue;
                    const dateValue = m[0].replace(/\\//g, '-').replace(/\\./g, '-');

                    // 向上找“条目容器”
                    let container = el.closest('li, tr, [class*="item"], [class*="row"], [class*="list"], [class*="card"]');
                    if (!container) container = el.parentElement;
                    // 再兜底向上爬几层
                    let hops = 0;
                    while (container && hops < 4) {
                        const title = pickTitleFromContainer(container);
                        if (title && title.length >= 4) break;
                        container = container.parentElement;
                        hops += 1;
                    }
                    if (!container) continue;

                    const title = pickTitleFromContainer(container);
                    const a = container.querySelector('a[href]');
                    const href = a ? (a.getAttribute('href') || '') : '';
                    const key = `${dateValue}__${title}__${href}`.slice(0, 240);
                    if (seenPairs.has(key)) continue;
                    seenPairs.add(key);

                    dateItemSamples.push({
                        title: title,
                        dateValue: dateValue,
                        dateSelector: makeSelector(el),
                        containerSelector: makeSelector(container),
                        linkHref: href.slice(0, 200),
                    });
                }

                // 补充：若 dateElements 很少/选择器不可定位，做一次小规模扫描
                if (dateItemSamples.length < 5) {
                    const candidates = Array.from(document.querySelectorAll('span, time, td')).slice(0, 800);
                    for (const el of candidates) {
                        if (dateItemSamples.length >= 12) break;
                        const text = norm(el.textContent || '');
                        if (text.length > 24) continue;
                        const m = text.match(datePattern);
                        if (!m) continue;
                        const dateValue = m[0].replace(/\\//g, '-').replace(/\\./g, '-');
                        let container = el.closest('li, tr, [class*="item"], [class*="row"], [class*="list"], [class*="card"]') || el.parentElement;
                        if (!container) continue;
                        const title = pickTitleFromContainer(container);
                        if (!title || title.length < 4) continue;
                        const a = container.querySelector('a[href]');
                        const href = a ? (a.getAttribute('href') || '') : '';
                        const key = `${dateValue}__${title}__${href}`.slice(0, 240);
                        if (seenPairs.has(key)) continue;
                        seenPairs.add(key);
                        dateItemSamples.push({
                            title: title,
                            dateValue: dateValue,
                            dateSelector: makeSelector(el),
                            containerSelector: makeSelector(container),
                            linkHref: href.slice(0, 200),
                        });
                    }
                }

                result.dateItemSamples = dateItemSamples.slice(0, 12);

                // 轻量 SPA 线索（用于提示模型不要用 requests 抓“渲染后HTML”）
                result.spaHints = {
                    hasHashRoute: !!(location && location.hash && location.hash.length > 1),
                    hasAppRoot: !!document.querySelector('#app, #root'),
                };

                return result;
            }
            """)

            return structure

        except Exception as e:
            print(f"分析页面结构失败: {e}")
            return {}

    def get_captured_requests(self) -> Dict[str, List[Dict[str, Any]]]:
        """获取捕获的网络请求"""
        return {
            "all_requests": self.network_requests[-50:],
            "api_requests": self.api_requests[-20:],
        }

    async def scroll_page(self, times: int = 3):
        """滚动页面以加载更多内容"""
        if not self.page:
            return

        for i in range(times):
            await self.page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(1)

        # 滚回顶部
        await self.page.evaluate("window.scrollTo(0, 0)")

    async def enhanced_page_analysis(self) -> Dict[str, Any]:
        """
        【综合增强功能】执行完整的增强页面分析
        
        包含：
        1. 数据状态检测
        2. 交互式API捕获
        3. API参数差异分析
        
        Returns:
            完整的增强分析结果
        """
        print("\n[增强分析] 开始执行增强页面分析...")
        
        result = {
            "data_status": {},
            "interaction_apis": {},
            "param_analysis": {},
            "verified_category_mapping": {},
            "recommendations": []
        }
        
        # 1. 检测数据状态
        print("  [1/3] 检测数据加载状态...")
        data_status = await self.detect_data_status()
        result["data_status"] = data_status
        
        has_data = data_status.get("hasData", False)
        needs_interaction = data_status.get("needsInteraction", False)
        menus = data_status.get("potentialMenus", [])
        
        if not has_data:
            print(f"    ⚠ 页面当前无数据（表格行数: {data_status.get('tableRowCount', 0)}, 数据列表项: {data_status.get('listItemCount', 0)}）")
            result["recommendations"].append("页面初始状态无数据，需要选择分类才能加载")
        else:
            print(f"    ✓ 页面有数据（表格行数: {data_status.get('tableRowCount', 0)}, 数据列表项: {data_status.get('listItemCount', 0)}）")
        
        if needs_interaction:
            print(f"    ⚠ 检测到需要交互（{len(menus)} 个菜单项）")
        
        # 2. 执行交互探测（如果需要或强制）
        # 条件：needsInteraction 为 True，或者有很多菜单项
        if needs_interaction or len(menus) >= 5:
            print(f"  [2/3] 执行交互式API捕获（{len(menus)} 个潜在菜单项）...")
            # 强制执行交互探测以获取正确的分类参数
            interaction_result = await self.capture_api_with_interactions(max_interactions=8, force=True)
            result["interaction_apis"] = interaction_result
            
            # 3. 分析API参数
            if interaction_result.get("interaction_apis"):
                print("  [3/3] 分析API参数差异...")
                param_analysis = self.analyze_api_parameters(interaction_result)
                result["param_analysis"] = param_analysis

                verified = self.build_verified_category_mapping(interaction_result)
                result["verified_category_mapping"] = verified
                if verified.get("menu_to_filters"):
                    result["recommendations"].append(
                        f"已从真实抓包构建分类映射（{verified.get('confidence')}）：{len(verified.get('menu_to_filters', {}))} 个菜单"
                    )
                else:
                    result["recommendations"].append("未能从抓包中抽取到 filters 内的分类ID，避免让模型猜测")
                
                # 生成建议
                if param_analysis.get("category_params"):
                    cat_params = param_analysis["category_params"]
                    result["recommendations"].append(
                        f"检测到 {len(cat_params)} 个分类参数: {', '.join([p['param_name'] for p in cat_params])}"
                    )
                    result["recommendations"].append("生成的爬虫应该遍历所有分类值来获取完整数据")
                
                if param_analysis.get("variable_params"):
                    var_params = list(param_analysis["variable_params"].keys())
                    result["recommendations"].append(
                        f"以下参数随菜单变化: {', '.join(var_params)}"
                    )
            else:
                print("  [3/3] 跳过参数分析（未捕获到交互API）")
                result["recommendations"].append("未能捕获到交互后的API请求，可能需要手动分析")
        else:
            print("  [2/3] 跳过交互探测（页面已有完整数据且不需要分类）")
            print("  [3/3] 跳过参数分析")
        
        print("[增强分析] 分析完成\n")
        return result

    async def click_next_page(self) -> bool:
        """尝试点击下一页"""
        if not self.page:
            return False

        try:
            next_selectors = [
                # Common pagination patterns (including ChinaBond)
                ".pageNext",
                "a.pageNext",
                'a:has-text("下一页")',
                'a:has-text(">")',
                'a:has-text("Next")',
                'button:has-text("下一页")',
                '.next',
                '.pagination a:last-child',
                'a[class*="next"]',
            ]

            for selector in next_selectors:
                try:
                    element = await self.page.query_selector(selector)
                    if element:
                        await element.click()
                        try:
                            await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                        except Exception:
                            await asyncio.sleep(2)
                        return True
                except:
                    continue

            return False
        except Exception as e:
            print(f"点击下一页失败: {e}")
            return False
