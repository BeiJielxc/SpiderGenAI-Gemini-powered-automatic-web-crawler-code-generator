#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyGen API Server - FastAPI 后端

提供 REST API 供前端调用：
- POST /api/menu-tree: 获取目标页面的目录树
- POST /api/generate: 启动爬虫脚本生成任务
- GET /api/status/{task_id}: 获取任务状态和日志
- GET /api/download/{filename}: 下载生成的脚本文件
"""
import asyncio
import uuid
import time
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from config import Config
from chrome_launcher import ChromeLauncher
from browser_controller import BrowserController
from llm_agent import LLMAgent
from golden_crawlers import GoldenCrawlerStore, compute_task_signature
from news_attachments import FILE_TYPES, infer_file_type, normalize_news_attachments
from database import (
    init_db,
    add_history,
    update_history_status,
    get_all_history,
    get_history_detail,
    delete_history as delete_history_record,
    reset_running_tasks,
)

# ============ Pydantic Models ============

class MenuTreeRequest(BaseModel):
    url: str
    
class MenuTreeResponse(BaseModel):
    url: str
    root: Optional[Dict[str, Any]]
    leaf_paths: List[str]
    
class AttachmentData(BaseModel):
    """附件数据（图片/文件的 base64 编码）"""
    filename: str
    base64: str
    mimeType: str

class GenerateRequest(BaseModel):
    url: str
    startDate: str
    endDate: str
    outputScriptName: str
    taskObjective: Optional[str] = ""
    extraRequirements: Optional[str] = ""
    siteName: Optional[str] = ""
    listPageName: Optional[str] = ""
    sourceCredibility: Optional[str] = ""  # 信息源可信度（T1/T2/T3）
    runMode: str  # 'enterprise_report' | 'news_sentiment'
    crawlMode: Optional[str] = "agent"  # deprecated: 保留兼容旧前端，新架构统一使用 agent 模式
    downloadReport: Optional[str] = "yes"  # 'yes' | 'no'
    selectedPaths: Optional[List[str]] = None  # 用户选中的目录路径（保留兼容）
    attachments: Optional[List[AttachmentData]] = None  # 图片/文件附件
    # 持久化记忆 — 重新运行场景：上一次任务 ID（用于注入 feedback_replay_hint）
    prevTaskId: Optional[str] = None

class ReportFile(BaseModel):
    id: str
    name: str
    date: str
    downloadUrl: str
    fileType: str
    localPath: Optional[str] = None  # 本地文件路径（如果已下载）
    isLocal: bool = False  # 是否是本地文件
    category: Optional[str] = None  # 来源板块（多页爬取时标识）

class NewsAttachment(BaseModel):
    id: str
    name: str
    url: str
    fileType: str = "file"
    localPath: Optional[str] = None
    isLocal: bool = False


class NewsArticle(BaseModel):
    """新闻文章（新闻舆情场景）"""
    id: str
    title: str
    author: str = ""
    date: str
    source: str
    sourceUrl: str
    summary: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None  # 来源板块（多页爬取时标识）
    attachments: List[NewsAttachment] = Field(default_factory=list)

class BatchDownloadRequest(BaseModel):
    filenames: List[str]

class TaskStatusResponse(BaseModel):
    taskId: str
    status: str  # 'pending' | 'queued' | 'running' | 'completed' | 'failed'
    currentStep: int
    totalSteps: int
    stepLabel: str
    logs: List[str]
    resultFile: Optional[str] = None
    error: Optional[str] = None
    # 企业报告场景
    reports: Optional[List[ReportFile]] = None
    downloadedCount: Optional[int] = None  # 已下载的文件数量
    filesNotEnough: Optional[bool] = None  # 日期范围内文件是否不足5份
    pdfOutputDir: Optional[str] = None  # PDF 下载目录
    # 新闻舆情场景
    newsArticles: Optional[List[NewsArticle]] = None
    markdownFile: Optional[str] = None
    totalCount: Optional[int] = None  # 总结果数（当结果被截断时使用）
    # 队列信息（仅 enable_queue=true 时有值）
    queuePosition: Optional[int] = None       # 排队位置（0=正在运行/已完成）
    queueWaitingCount: Optional[int] = None   # 当前队列等待数
    queueRunningCount: Optional[int] = None   # 当前正在运行数
    estimatedWaitSeconds: Optional[int] = None  # 预估等待秒数
    # 持久化记忆 / 任务评价
    pendingFeedback: Optional[bool] = None    # True 时前端应弹出"任务评价" Modal
    autoFindings: Optional[Dict[str, Any]] = None  # Stage-1 启发式扫描结果
    htmlFingerprint: Optional[str] = None     # 列表页结构指纹
    userVerdict: Optional[str] = None         # 用户评价后回填: correct | wrong
    userSuggestion: Optional[str] = None      # 用户建议（已提交时回填）
    # 专家证据与运行诊断（供审计/后续诊断 UI 使用）
    stageEvidence: Optional[Dict[str, Any]] = None
    validationReports: Optional[List[Dict[str, Any]]] = None
    attributionDecision: Optional[Dict[str, Any]] = None
    repairHistory: Optional[List[Dict[str, Any]]] = None
    runtimeReport: Optional[Dict[str, Any]] = None
    taskSignature: Optional[str] = None
    executionSource: Optional[str] = None
    goldenCodePath: Optional[str] = None
    goldenStatus: Optional[str] = None

# ============ 全局状态 ============

# 任务存储（生产环境应使用 Redis）
tasks: Dict[str, Dict[str, Any]] = {}

# 任务对应的浏览器资源（launcher, browser）
task_browsers: Dict[str, Dict[str, Any]] = {}

# 任务对应的 asyncio Task（用于取消）
task_asyncio_tasks: Dict[str, asyncio.Task] = {}

# 任务取消标志
task_cancelled: Dict[str, bool] = {}

# 配置
config: Optional[Config] = None
_golden_store = GoldenCrawlerStore(Path(__file__).parent / "output" / "golden_crawlers")

# ── 队列 & SSE（仅 config 开启时才实例化，见 lifespan） ──
_task_queue = None       # type: ignore  # Optional[TaskQueue]
_event_broadcaster = None  # type: ignore  # Optional[EventBroadcaster]
# FastAPI 主事件循环（lifespan 启动时捕获）。LangGraph 的同步节点会被
# run_in_executor 丢到线程池里执行；这些线程没有自己的事件循环，所以
# 像 ``asyncio.ensure_future`` 这种依赖 "current loop" 的 API 在 Py3.14+ 会抛
# RuntimeError。捕获主循环后我们用 ``run_coroutine_threadsafe`` 在工作
# 线程里安全地把协程派回主循环执行。
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def _is_cancelled(task_id: str) -> bool:
    """检查任务是否已被取消"""
    return task_cancelled.get(task_id, False)

# ============ 生命周期 ============

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global config, _task_queue, _event_broadcaster, _main_loop
    # 捕获 FastAPI / uvicorn 主事件循环；后续工作线程里的回调要用它做
    # 跨线程协程派发。
    try:
        _main_loop = asyncio.get_running_loop()
    except RuntimeError:
        _main_loop = None
    try:
        config = Config()
        print("✓ 配置加载成功")
    except Exception as e:
        print(f"✗ 配置加载失败: {e}")
        config = None
    
    # ── 按配置启用队列 / SSE ──
    if config and config.queue_enabled:
        from queue_manager import TaskQueue
        _task_queue = TaskQueue(max_concurrency=config.max_concurrency)
        await _task_queue.start()
        print(f"✓ 任务队列已启动 (max_concurrency={config.max_concurrency})")

    # 初始化数据库
    try:
        await asyncio.to_thread(init_db)
        await asyncio.to_thread(reset_running_tasks)
        print("✓ 数据库初始化成功")
    except Exception as e:
        print(f"✗ 数据库初始化失败: {e}")

    if config and config.sse_enabled:
        from realtime import EventBroadcaster
        _event_broadcaster = EventBroadcaster()
        print("✓ SSE 事件广播已启用")

    # Module A: bring up the URL-keyed PageCache (used by the specialist graph
    # tools' write-through path and by the rerun-validator). The cache
    # is a *singleton* shared across all task runs in this process, so
    # configure it once at startup. ``build_default_page_cache`` is a
    # no-op when ``page_cache.enabled = false``.
    if config:
        try:
            from page_cache import build_default_page_cache
            cache = build_default_page_cache(config)
            if cache is not None:
                print(
                    f"✓ PageCache 启用 (root={cache.root}, "
                    f"ttl={config.page_cache_ttl_sec}s, "
                    f"max={config.page_cache_max_total_mb}MB)"
                )
            else:
                print("ℹ PageCache 未启用 (page_cache.enabled=false)")
        except Exception as exc:
            print(f"⚠ PageCache 初始化失败 (非致命，将退化为无缓存): {exc}")

    yield

    # 清理
    if _task_queue is not None:
        await _task_queue.shutdown()
        print("✓ 任务队列已关闭")
    print("应用关闭")

# ============ FastAPI App ============

app = FastAPI(
    title="PyGen API",
    description="智能爬虫脚本生成器 API",
    version="2.0",
    lifespan=lifespan
)

# CORS 配置（线上部署通过 Nginx 反代同域，本地开发需要 localhost 跨域）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ 辅助函数 ============

def _schedule_publish(coro) -> None:
    """线程安全地把广播协程丢回主事件循环。

    LangGraph 的同步节点（例如确定性 Gate）会被
    ``run_in_executor`` 丢到 worker 线程里执行；那个线程没有自己的事件
    循环，因此 ``asyncio.ensure_future`` / ``get_event_loop`` 会在 Py3.14+
    抛 ``RuntimeError``。这里统一兜底：

    1. 当前线程有运行中的 loop → 直接 ``ensure_future``。
    2. 没有，但我们启动时捕获了主 loop → ``run_coroutine_threadsafe``。
    3. 都没有 → 关闭协程，静默放弃 SSE 推送（日志已经写进内存任务，
       前端走轮询仍能拿到）。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        try:
            asyncio.ensure_future(coro, loop=loop)
            return
        except Exception:
            pass

    if _main_loop is not None and not _main_loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(coro, _main_loop)
            return
        except Exception:
            pass

    # 兜底：避免协程被 GC 时抛 "coroutine was never awaited" 警告
    try:
        coro.close()
    except Exception:
        pass


def _add_log(task_id: str, message: str):
    """添加日志到任务"""
    if task_id in tasks:
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_line = f"[{timestamp}] {message}"
        tasks[task_id]["logs"].append(log_line)
        # SSE 推送日志（如果启用）
        if _event_broadcaster is not None:
            _schedule_publish(
                _event_broadcaster.publish(task_id, "log", {"message": log_line})
            )

def _update_step(task_id: str, step: int, label: str):
    """更新任务步骤"""
    if task_id in tasks:
        tasks[task_id]["currentStep"] = step
        tasks[task_id]["stepLabel"] = label
        # SSE 推送步骤变化
        if _event_broadcaster is not None:
            _schedule_publish(
                _event_broadcaster.publish(task_id, "step", {"currentStep": step, "stepLabel": label})
            )


async def _task_golden_context(task_id: str) -> Dict[str, Any]:
    """Resolve golden metadata from memory first, then persisted history."""
    task = tasks.get(task_id) or {}
    request_data = task.get("request")
    result_data: Dict[str, Any] = task

    if not request_data:
        try:
            history = await asyncio.to_thread(get_history_detail, task_id)
        except Exception:
            history = None
        if history:
            request_data = history.get("config")
            result_data = history.get("result") or {}

    if not request_data:
        raise HTTPException(status_code=404, detail="原任务不存在或无法恢复参数签名")

    signature = (
        task.get("taskSignature")
        or result_data.get("taskSignature")
        or compute_task_signature(request_data)
    )
    return {
        "signature": signature,
        "request": request_data,
        "execution_source": task.get("executionSource") or result_data.get("executionSource"),
    }


async def _transition_golden_from_feedback(task_id: str, verdict: str) -> Dict[str, Any]:
    """Move the sole Python asset according to explicit user feedback."""
    context = await _task_golden_context(task_id)
    signature = context["signature"]
    source = context.get("execution_source")
    path: Optional[Path] = None
    status: Optional[str] = None

    if verdict == "correct":
        path = _golden_store.activate(task_id=task_id, signature=signature)
        if path is not None:
            status = "active"
    elif source == "golden_replay":
        path = _golden_store.invalidate_active(signature=signature, task_id=task_id)
        status = "invalid" if path is not None else None
    else:
        path = _golden_store.reject_pending(task_id=task_id, signature=signature)
        status = "invalid" if path is not None else None

    existing_task = tasks.get(task_id) or {}
    if path is None and existing_task.get("goldenStatus") == "invalid":
        existing_path = existing_task.get("goldenCodePath")
        if existing_path:
            path = Path(existing_path)
            status = "invalid"

    update = {
        "taskSignature": signature,
        "goldenStatus": status,
        "goldenCodePath": str(path) if path is not None else None,
    }
    if task_id in tasks:
        tasks[task_id].update(update)

    # Keep the pending Episode relationship aligned before commit_episode
    # appends it to JSONL. The draft stores only path/signature metadata.
    if config is not None:
        try:
            from memory import MemoryStore  # type: ignore
            store = MemoryStore(config.memory_root)
            draft = await asyncio.to_thread(store.read_draft, task_id)
            if draft is not None:
                draft.update({
                    "task_signature": signature,
                    "golden_status": status,
                    "golden_code_path": str(path) if path is not None else None,
                })
                await asyncio.to_thread(store.write_draft, draft)
        except Exception as exc:
            _add_log(task_id, f"[WARNING] Episode 黄金关联同步失败: {exc}")

    # Persist the relationship without changing the task execution status.
    try:
        history = await asyncio.to_thread(get_history_detail, task_id)
        if history:
            persisted_result = dict(history.get("result") or {})
            persisted_result.update(update)
            await asyncio.to_thread(
                update_history_status,
                task_id,
                history.get("status") or "completed",
                persisted_result,
            )
    except Exception as exc:
        _add_log(task_id, f"[WARNING] 历史记录黄金关联同步失败: {exc}")
    return update


def _golden_failure_is_definitive(error: str) -> bool:
    """Avoid invalidating good code for infrastructure/network failures."""
    value = str(error or "").lower()
    transient_markers = (
        "timeout", "timed out", "429", "rate limit", "temporarily unavailable",
        "connection reset", "connection refused", "dns", "name resolution",
        "chrome", "browser", "cdp", "任务已被用户取消", "任务被用户停止",
    )
    return not any(marker in value for marker in transient_markers)


async def _download_news_attachments(
    *,
    task_id: str,
    articles: List[Dict[str, Any]],
    request: GenerateRequest,
) -> tuple[int, Optional[Path]]:
    """Best-effort local download while preserving every remote URL."""
    if request.downloadReport != "yes" or not articles or config is None:
        return 0, None

    attachment_count = sum(len(article.get("attachments") or []) for article in articles)
    if attachment_count <= 0:
        return 0, None

    import httpx

    folder_name = f"{task_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_news"
    base_dir = config.output_dir.parent / "output" / "output_pdf"
    output_dir = base_dir / folder_name
    output_dir.mkdir(parents=True, exist_ok=True)
    _add_log(task_id, f"[INFO] 正在下载新闻附件，共 {attachment_count} 个候选")

    downloaded = 0
    sequence = 0
    downloaded_by_url: Dict[str, Dict[str, Any]] = {}
    max_bytes = 50 * 1024 * 1024
    default_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/pdf, application/octet-stream, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True, verify=False) as client:
        for article in articles:
            referer = str(article.get("sourceUrl") or request.url)
            for attachment in article.get("attachments") or []:
                url = str(attachment.get("url") or "").strip()
                if not url:
                    continue
                if url in downloaded_by_url:
                    attachment.update(downloaded_by_url[url])
                    continue

                sequence += 1
                try:
                    response = await client.get(url, headers={**default_headers, "Referer": referer})
                    if response.status_code != 200:
                        _add_log(task_id, f"[WARNING] 新闻附件下载失败 ({response.status_code}): {url[:120]}")
                        continue
                    declared_size = int(response.headers.get("content-length") or 0)
                    if declared_size > max_bytes or len(response.content) > max_bytes:
                        _add_log(task_id, f"[WARNING] 新闻附件超过 50MB，保留远程链接: {url[:120]}")
                        continue

                    content_type = (response.headers.get("content-type") or "").lower()
                    detected_type = "pdf" if (
                        "application/pdf" in content_type or response.content.startswith(b"%PDF")
                    ) else infer_file_type(url, mime_type=content_type, label=attachment.get("name", ""))
                    requested_type = str(attachment.get("fileType") or "file").lower().lstrip(".")
                    if requested_type not in (set(FILE_TYPES.values()) | {"file"}):
                        requested_type = "file"
                    if requested_type == "pdf" and detected_type != "pdf":
                        _add_log(task_id, f"[WARNING] PDF 候选返回的不是 PDF，保留远程链接: {url[:120]}")
                        continue

                    file_type = detected_type if detected_type != "file" else requested_type
                    if file_type == "file":
                        file_type = "bin"
                    raw_name = str(attachment.get("name") or f"attachment_{sequence}")[:80]
                    safe_name = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff ]+", "_", raw_name).strip(" ._")
                    safe_name = safe_name or f"attachment_{sequence}"
                    known_suffix = f".{file_type}"
                    if safe_name.lower().endswith(known_suffix):
                        safe_name = safe_name[: -len(known_suffix)].rstrip(" ._") or f"attachment_{sequence}"
                    filename = f"{sequence}_{safe_name}.{file_type}"
                    local_path = output_dir / filename
                    local_path.write_bytes(response.content)
                    relationship = {
                        "fileType": file_type,
                        "localPath": f"{folder_name}/{filename}",
                        "isLocal": True,
                    }
                    attachment.update(relationship)
                    downloaded_by_url[url] = relationship
                    downloaded += 1
                    _add_log(task_id, f"[SUCCESS] 新闻附件已下载: {filename}")
                except Exception as exc:
                    _add_log(task_id, f"[WARNING] 新闻附件下载异常，保留远程链接: {str(exc)[:100]}")

    if downloaded <= 0:
        try:
            output_dir.rmdir()
        except OSError:
            pass
        return 0, None
    _add_log(task_id, f"[SUCCESS] 共下载 {downloaded} 个新闻附件到 {output_dir}")
    return downloaded, output_dir

# ============ 核心逻辑：获取目录树 ============

async def _fetch_menu_tree(url: str) -> Dict[str, Any]:
    """启动浏览器并获取目录树"""
    launcher = None
    browser = None
    
    try:
        # 使用随机端口以避免多请求间的资源冲突（防止请求A关闭了请求B正在复用的浏览器）
        import random
        random_port = random.randint(10000, 20000)
        
        # 启动 Chrome（headless 模式由 config.yaml 统一控制）
        _headless = config.browser_headless if config else False
        launcher = ChromeLauncher(
            debug_port=random_port,
            user_data_dir=None,  # 让 Launcher 自动创建临时目录，避免 Profile 锁冲突
            headless=_headless,
            auto_select_port=True
        )
        launcher.launch()
        
        # 连接浏览器
        browser = BrowserController(
            cdp_url=launcher.get_ws_endpoint(),
            timeout=config.cdp_timeout if config else 60000
        )
        
        if not await browser.connect():
            # 简单的重试一次
            await asyncio.sleep(1)
            if not await browser.connect():
                raise RuntimeError("无法连接到 Chrome 浏览器")
        
        # 打开页面
        success, error_msg = await browser.open(url)
        if not success:
            raise RuntimeError(f"无法打开目标页面: {error_msg}")
        
        # 滚动页面
        await browser.scroll_page(times=2)
        
        # 获取目录树
        menu_tree = await browser.enumerate_menu_tree(max_depth=3)
        
        return {
            "url": url,
            "root": menu_tree.get("root"),
            "leaf_paths": menu_tree.get("leaf_paths", [])
        }
        
    finally:
        if browser:
            await browser.disconnect()
        if launcher:
            launcher.terminate()

# ============ 核心逻辑：生成爬虫脚本 ============

async def _run_generation_task(task_id: str, request: GenerateRequest):
    """后台执行爬虫脚本生成"""
    launcher = None
    browser = None
    planner_result = None
    runtime_report = None
    memory_finalized = False
    golden_runtime_started = False
    task_signature = tasks.get(task_id, {}).get("taskSignature") or compute_task_signature(request)
    golden_asset = _golden_store.load_active(task_signature)
    if task_id in tasks:
        tasks[task_id].update({
            "taskSignature": task_signature,
            "executionSource": "golden_replay" if golden_asset else "generated",
            "goldenCodePath": str(golden_asset.path) if golden_asset else None,
            "goldenStatus": "active" if golden_asset else None,
        })
    
    try:
        tasks[task_id]["status"] = "running"
        task_cancelled[task_id] = False  # 初始化取消标志

        # 更新历史记录状态
        try:
            await asyncio.to_thread(update_history_status, task_id, "running")
        except Exception as e:
            print(f"更新历史记录状态失败: {e}")

        # SSE 推送状态变更（从 queued → running）
        if _event_broadcaster is not None:
            await _event_broadcaster.publish(task_id, "status", {"status": "running"})

        # 读取配置：是否启用自动修复/检查/后处理
        auto_repair_enabled = config.llm_auto_repair if config else True
        
        # Step 1: 启动 Chrome
        if _is_cancelled(task_id):
            raise RuntimeError("任务已被用户取消")
            
        _update_step(task_id, 0, "正在启动Chrome浏览器")
        _add_log(task_id, "[INFO] 正在启动 Chrome 浏览器...")
        
        # 使用随机端口以避免多任务间的资源冲突（防止任务A关闭了任务B正在复用的浏览器）
        import random
        # 随机选择一个端口范围，避免与常用端口冲突
        random_port = random.randint(10000, 20000)
        
        # headless 模式由 config.yaml 统一控制
        _headless = config.browser_headless if config else False
        launcher = ChromeLauncher(
            debug_port=random_port,
            user_data_dir=None, # 让 Launcher 自动创建临时目录，避免 Profile 锁冲突
            headless=_headless,
            auto_select_port=True
        )
        launcher.launch()
        _add_log(task_id, f"[SUCCESS] Chrome 启动成功 (端口 {launcher.actual_port}, headless={_headless})")
        
        # 存储浏览器资源引用，以便停止时可以关闭
        task_browsers[task_id] = {"launcher": launcher, "browser": None}
        
        # Step 2: 连接浏览器
        if _is_cancelled(task_id):
            raise RuntimeError("任务已被用户取消")
            
        _update_step(task_id, 1, "正在连接浏览器")
        ws_url = launcher.get_ws_endpoint()
        _add_log(task_id, f"[INFO] 正在连接到 CDP: {ws_url}")
        
        browser = BrowserController(
            cdp_url=ws_url,
            timeout=config.cdp_timeout if config else 60000
        )
        
        # 增加连接重试与自动恢复机制
        connected = False
        for i in range(3):
            if await browser.connect():
                connected = True
                break
            
            _add_log(task_id, f"[WARNING] 浏览器连接尝试 {i+1}/3 失败，等待重试...")
            await asyncio.sleep(2)
            
            # 如果是最后一次尝试前，且看起来连接完全不通，尝试重启 Chrome
            if i == 1:
                _add_log(task_id, "[WARNING] 连接持续失败，尝试重启 Chrome...")
                try:
                    launcher.terminate()
                    await asyncio.sleep(1)
                    launcher.launch()
                    # 更新 WS URL
                    browser.cdp_url = launcher.get_ws_endpoint()
                    _add_log(task_id, f"[INFO] Chrome 已重启，新地址: {browser.cdp_url}")
                except Exception as e:
                    _add_log(task_id, f"[ERROR] 重启 Chrome 失败: {e}")

        if not connected:
            raise RuntimeError("无法连接到 Chrome 浏览器，请检查 Chrome 是否正确安装或端口是否被占用")

        _add_log(task_id, "[SUCCESS] 浏览器连接成功")
        
        # 更新浏览器引用
        task_browsers[task_id]["browser"] = browser
        
        # ================================================================
        # Agent 模式：使用证据驱动专家状态机（替代原有 crawlMode 分支）
        # ================================================================
        if _is_cancelled(task_id):
            raise RuntimeError("任务已被用户取消")

        if golden_asset:
            _update_step(task_id, 2, "已命中黄金爬虫，跳过大模型")
            _add_log(
                task_id,
                f"[GOLDEN] 参数签名命中 active/{task_signature}.py，将直接复用人工确认代码",
            )
        else:
            _update_step(task_id, 2, "正在启动 Agent 智能决策")
            _add_log(task_id, "[INFO] Agent 模式 (LangGraph)：Supervisor 将按阶段调度专家、Gate 与失败回退")

        task_objective = (request.taskObjective or request.extraRequirements or "").strip()
        supplemental_lines: List[str] = []
        if request.siteName:
            supplemental_lines.append(f"官网名称: {request.siteName}")
        if request.listPageName:
            supplemental_lines.append(f"列表页面名称: {request.listPageName}")
        if request.sourceCredibility:
            supplemental_lines.append(f"信息源可信度: {request.sourceCredibility}")

        if task_objective:
            user_requirements = f"任务目标（最高优先级）:\n{task_objective}"
        else:
            _add_log(task_id, "[WARNING] 未提供任务目标，模型将仅根据页面结构自动推断任务")
            user_requirements = ""

        if supplemental_lines:
            if user_requirements:
                user_requirements += "\n\n"
            user_requirements += "\n".join(supplemental_lines)

        llm_attachments = None
        if request.attachments:
            from llm_agent import AttachmentData as LLMAttachmentData
            llm_attachments = [
                LLMAttachmentData(filename=att.filename, base64_data=att.base64, mime_type=att.mimeType)
                for att in request.attachments
            ]
            _add_log(task_id, f"[INFO] 已附加 {len(llm_attachments)} 个图片/文件")

        llm = LLMAgent(
            api_key=config.qwen_api_key if config else "",
            model=config.qwen_model if config else "qwen-max",
            base_url=config.qwen_base_url if config else None,
            enable_auto_repair=auto_repair_enabled,
        )

        _update_step(task_id, 5, "Agent 正在自主探测和分析网站")

        from agents.runner import run_agent as langgraph_run_agent

        planner_result = await langgraph_run_agent(
            browser=browser, config=config, llm_agent=llm,
            url=request.url, run_mode=request.runMode,
            start_date=request.startDate, end_date=request.endDate,
            extra_requirements=user_requirements, task_id=task_id,
            log_callback=lambda msg: _add_log(task_id, msg),
            attachments=llm_attachments,
            max_iterations=config.agent_max_iterations if config else 20,
            cancel_check=lambda: _is_cancelled(task_id),
            prev_task_id=request.prevTaskId or None,
            # Specialist and runtime-finalization phases can update the sidebar
            # through one thread-safe callback.
            step_callback=lambda step, label: _update_step(task_id, step, label),
            reusable_script_code=golden_asset.code if golden_asset else None,
            task_signature=task_signature,
            golden_code_path=str(golden_asset.path) if golden_asset else None,
        )

        tasks[task_id]["stageEvidence"] = planner_result.stage_evidence
        tasks[task_id]["validationReports"] = planner_result.validation_reports
        tasks[task_id]["attributionDecision"] = planner_result.attribution_decision
        tasks[task_id]["repairHistory"] = planner_result.repair_history

        if not planner_result.success or not planner_result.script_code:
            error_msg = planner_result.error or "Agent 未能生成有效代码"
            _add_log(task_id, f"[ERROR] Agent 失败: {error_msg}")
            raise RuntimeError(error_msg)

        script_code = planner_result.script_code
        enhanced_analysis = planner_result.enhanced_analysis
        verified_mapping = planner_result.verified_mapping

        if golden_asset:
            _add_log(task_id, "[SUCCESS] 黄金爬虫已加载：0 次 LLM 调用，0 次专家工具调用")
        else:
            _add_log(task_id, f"[SUCCESS] Agent 完成: {planner_result.iterations} 次迭代, "
                              f"{len(planner_result.tool_calls)} 次工具调用")
        _add_log(task_id, f"[INFO] 策略: {planner_result.strategy_summary[:200]}")

        # 兜底：确保 script_code 是字符串
        if not isinstance(script_code, str) or not script_code.strip():
            _add_log(task_id, "[ERROR] 脚本生成失败：script_code 为空或非字符串")
            raise RuntimeError("脚本生成失败：script_code 为空或非字符串")

        # Static validation and repair already ran inside Codegen + Critic gates.
        _update_step(task_id, 8, "代码证据与静态 Gate 已通过")

        # Step 9: 保存脚本
        print(f"[DEBUG][task={task_id}] === 进入 Step 9: 保存脚本 ===")
        _update_step(task_id, 9, "爬虫脚本已生成")
        
        output_dir = config.output_dir if config else Path("./py")
        print(f"[DEBUG][task={task_id}] output_dir = {output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        output_name = request.outputScriptName
        if not output_name.endswith(".py"):
            output_name += ".py"
        
        output_path = output_dir / output_name
        print(f"[DEBUG][task={task_id}] 准备写入脚本到: {output_path}")
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(script_code)
        print(f"[DEBUG][task={task_id}] 脚本写入完成")
        
        _add_log(task_id, f"[SUCCESS] 脚本已保存: {output_path}")
        _add_log(task_id, f"[SUCCESS] 文件大小: {len(script_code):,} 字符")
        
        # Step 10: 在任务独立目录中运行生成脚本。ExecutorSession 优先使用
        # Docker；没有 Docker 时才按配置回退到 local backend。
        print(f"[DEBUG][task={task_id}] 检查任务是否被取消 (Step 10 前)...")
        if _is_cancelled(task_id):
            raise RuntimeError("任务已被用户取消")
            
        print(f"[DEBUG][task={task_id}] === 进入 Step 10: 运行爬虫脚本 ===")
        _update_step(task_id, 10, "正在运行爬虫脚本")
        _add_log(task_id, "[INFO] 正在任务隔离沙箱中运行 crawler.py")
        from execution_service import TaskExecutionService
        execution_root = Path(__file__).parent / "output" / "tasks"
        runtime_service = TaskExecutionService(execution_root)
        golden_runtime_started = bool(golden_asset)
        runtime_report_obj, runtime_payload = await runtime_service.execute(
            task_id=task_id,
            script_code=script_code,
            run_mode=request.runMode,
            config=config,
            timeout_sec=300,
        )
        runtime_report = runtime_report_obj.to_dict()
        tasks[task_id]["runtimeReport"] = runtime_report
        rewrite_count = int((runtime_report.get("stage_counts") or {}).get("output_path_rewrites") or 0)
        if rewrite_count:
            _add_log(
                task_id,
                f"[INFO] Runtime 已将 {rewrite_count} 个绝对输出目录重定向到当前任务目录",
            )
        if runtime_report.get("stdout_tail"):
            _add_log(task_id, runtime_report["stdout_tail"][-1000:])
        if not runtime_report_obj.success:
            _add_log(task_id, f"[ERROR] Runtime Gate 失败: {runtime_report_obj.error}")
            raise RuntimeError(runtime_report_obj.error or "Runtime Gate failed")
        _add_log(task_id, f"[SUCCESS] Runtime Gate 通过，records={runtime_report_obj.record_count}")
        
        # Step 11: 验证爬取结果
        _update_step(task_id, 11, "📊 正在验证爬取结果")
        
        # Runtime service returns only files owned by this task. Never scan
        # process-wide output directories or use "latest file" heuristics.
        possible_dirs = [Path(runtime_report["workdir"])]
            
        # 确定主输出目录（用于后续下载文件存放等），优先选择存在的目录
        output_json_dir = possible_dirs[0]
        for d in possible_dirs:
            if d.exists():
                output_json_dir = d
                break
            
        reports = []
        news_articles = []
        markdown_file = None
        
        all_json_files = [
            Path(path) for path in (runtime_report.get("output_files") or [])
            if Path(path).is_file() and Path(path).suffix.lower() == ".json"
        ]
        
        if all_json_files:
            # 根据运行模式处理不同的结果格式
            if request.runMode == "news_sentiment":
                # 新闻舆情模式：查找 JSON 文件（优先 news_ 开头的文件）
                all_json_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
                
                # 只考虑最近 5 分钟内创建的文件
                import time as time_module
                recent_cutoff = time_module.time() - 300  # 5 分钟
                recent_json_files = [f for f in all_json_files if f.stat().st_mtime > recent_cutoff]
                
                # 优先查找 news_ 开头的文件
                news_json_files = [f for f in recent_json_files if f.name.startswith("news_")]
                json_files = news_json_files if news_json_files else recent_json_files
                
                _add_log(task_id, f"[DEBUG] 找到 {len(recent_json_files)} 个最近的 JSON 文件，其中 {len(news_json_files)} 个是 news_ 开头的")
                
                latest_json = None
                for jf in json_files:
                    try:
                        with open(jf, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        # 检查是否包含 articles 或 news 字段
                        if "articles" in data or "news" in data:
                            latest_json = jf
                            _add_log(task_id, f"[DEBUG] 使用文件: {jf.name}，包含 {len(data.get('articles', data.get('news', [])))} 条数据")
                            break
                    except Exception as e:
                        _add_log(task_id, f"[DEBUG] 跳过文件 {jf.name}: {e}")
                        continue
                
                if latest_json:
                    _add_log(task_id, f"[INFO] 找到新闻 JSON 文件: {latest_json}")
                    
                    try:
                        with open(latest_json, 'r', encoding='utf-8') as f:
                            data = json.load(f)

                        def _safe_str(v: Any, default: str = "") -> str:
                            """将任意值安全转换为字符串，避免 Pydantic 因 None/非字符串类型报错"""
                            if v is None:
                                return default
                            if isinstance(v, str):
                                return v
                            try:
                                return str(v)
                            except Exception:
                                return default
                        
                        # 解析 articles 数组
                        articles_key = "articles" if "articles" in data else "news" if "news" in data else None
                        if articles_key and isinstance(data.get(articles_key), list):
                            for i, item in enumerate(data[articles_key]):
                                if not isinstance(item, dict):
                                    continue
                                
                                content = _safe_str(item.get("content"), "")
                                normalized_item = dict(item)
                                normalized_item.update({
                                    "sourceUrl": _safe_str(item.get("sourceUrl") or item.get("url") or item.get("link"), ""),
                                    "content": content,
                                })
                                attachments = normalize_news_attachments(normalized_item)
                                news_articles.append({
                                    "id": str(i + 1),
                                    "title": _safe_str(item.get("title"), "未知") or "未知",
                                    "author": _safe_str(item.get("author"), ""),
                                    "date": _safe_str(item.get("date"), ""),
                                    "source": _safe_str(item.get("source"), ""),
                                    "sourceUrl": normalized_item["sourceUrl"],
                                    "summary": _safe_str(item.get("summary"), ""),
                                    "content": content,
                                    "category": _safe_str(item.get("category"), ""),  # 来源板块
                                    "attachments": attachments,
                                })
                            
                            articles_with_content = sum(1 for item in news_articles if item.get("content"))
                            articles_with_attachments = sum(1 for item in news_articles if item.get("attachments"))
                            attachment_total = sum(len(item.get("attachments") or []) for item in news_articles)
                            _add_log(
                                task_id,
                                f"[SUCCESS] 解析到 {len(news_articles)} 条新闻，正文 {articles_with_content} 条，附件 {attachment_total} 个",
                            )
                    except Exception as parse_err:
                        _add_log(task_id, f"[WARNING] 解析新闻 JSON 失败: {parse_err}")
                else:
                    _add_log(task_id, f"[WARNING] 未找到包含新闻数据的 JSON 文件")
            else:
                # 企业报告模式：原有逻辑
                # 使用已经收集好的 all_json_files
                json_files = all_json_files
                json_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
                
                if json_files:
                    latest_json = json_files[0]
                    _add_log(task_id, f"[INFO] 找到结果文件: {latest_json}")
                    
                    try:
                        with open(latest_json, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        
                        # 提取生成脚本输出的下载头（如有），供后续下载 PDF 时使用
                        _script_download_headers = data.get("downloadHeaders") if isinstance(data, dict) else None
                        
                        # 解析 reports 数组（兼容不同脚本字段命名：name/title/reportName/text）
                        if isinstance(data, dict) and "reports" in data:
                            for i, item in enumerate(data["reports"]):
                                if not isinstance(item, dict):
                                    continue

                                name = (
                                    item.get("name")
                                    or item.get("title")
                                    or item.get("reportName")
                                    or item.get("report_title")
                                    or item.get("text")
                                    or "未知"
                                )
                                download_url = (
                                    item.get("downloadUrl")
                                    or item.get("download_url")
                                    or item.get("url")
                                    or item.get("pdfUrl")
                                    or "#"
                                )
                                file_type = item.get("fileType") or item.get("file_type") or "pdf"

                                reports.append({
                                    "id": str(i + 1),
                                    "name": name,
                                    "date": item.get("date", ""),
                                    "downloadUrl": download_url,
                                    "fileType": file_type,
                                    "category": item.get("category", "")  # 来源板块
                                })
                        # 若用户提供了日期范围，则在服务端做一次"硬过滤"，避免脚本把无日期/越界数据也塞进来
                        # 规则：只保留 date 为 YYYY-MM-DD 且在 [startDate, endDate] 范围内的数据；无日期直接丢弃
                        try:
                            start_date = (request.startDate or "").strip()
                            end_date = (request.endDate or "").strip()
                            if start_date and end_date:
                                before = len(reports)
                                filtered = []
                                for r in reports:
                                    d = (r.get("date") or "").strip()
                                    if len(d) == 10 and d[4] == "-" and d[7] == "-" and start_date <= d <= end_date:
                                        filtered.append(r)
                                reports = filtered
                                dropped = before - len(reports)
                                if dropped:
                                    _add_log(task_id, f"[INFO] 已按日期范围过滤：丢弃 {dropped} 条（无日期或不在 {start_date}~{end_date}）")
                        except Exception as _filter_err:
                            _add_log(task_id, f"[WARNING] 服务端日期过滤失败（已忽略）：{_filter_err}")

                        # 【新增】结果截断：如果报告数量超过 100 且用户选了“不下载”或“新闻报告下载”等场景，为优化前端渲染，截断列表
                        # 记录总数以便前端展示提示
                        tasks[task_id]["totalCount"] = len(reports)
                        if len(reports) > 100:
                            _add_log(task_id, f"[INFO] 结果过多（{len(reports)} 条），已截断为前 100 条以优化展示")
                            reports = reports[:100]

                        _add_log(task_id, f"[SUCCESS] 解析到 {len(reports)} 条报告记录（总匹配: {tasks[task_id].get('totalCount', len(reports))}）")
                        
                        # 验证输出数据质量
                        try:
                            from validator import OutputValidator
                            output_validator = OutputValidator()
                            quality = output_validator.get_quality_report({"reports": reports})
                            
                            if quality["date_fill_rate"] < 0.3:
                                _add_log(task_id, f"[WARNING] 日期填充率较低: {quality['date_fill_rate']:.1%}")
                            else:
                                _add_log(task_id, f"[INFO] 数据质量: 日期填充率 {quality['date_fill_rate']:.1%}")
                        except ImportError:
                            pass
                        except Exception as qual_err:
                            _add_log(task_id, f"[WARNING] 质量检查失败: {qual_err}")
                            
                    except Exception as parse_err:
                        _add_log(task_id, f"[WARNING] 解析结果文件失败: {parse_err}")
        else:
            _add_log(task_id, f"[INFO] 未找到 output 目录: {output_json_dir}")
        
        # Final Output Gate runs after API-side normalization and date filtering.
        # A crawler that exits cleanly but yields no usable records is a failure.
        final_record_count = (
            len(news_articles) if request.runMode == "news_sentiment" else len(reports)
        )
        runtime_report.setdefault("stage_counts", {})["after_date_filter"] = final_record_count
        runtime_report["stage_counts"]["final_records"] = final_record_count
        runtime_report["record_count"] = final_record_count
        if request.runMode == "news_sentiment":
            runtime_report["stage_counts"].update({
                "articles_with_content": sum(1 for item in news_articles if item.get("content")),
                "articles_with_attachments": sum(1 for item in news_articles if item.get("attachments")),
                "articles_with_content_and_attachments": sum(
                    1 for item in news_articles if item.get("content") and item.get("attachments")
                ),
                "attachment_count": sum(len(item.get("attachments") or []) for item in news_articles),
            })
        if final_record_count <= 0:
            runtime_report["success"] = False
            runtime_report["error"] = (
                "Final Output Gate failed: crawler produced no usable records "
                "after normalization and date filtering"
            )
            tasks[task_id]["runtimeReport"] = runtime_report
            _add_log(task_id, f"[ERROR] {runtime_report['error']}")
            raise RuntimeError(runtime_report["error"])

        manifest_path = Path(runtime_report["workdir"]) / "result_manifest.json"
        try:
            with open(manifest_path, "w", encoding="utf-8") as manifest_file:
                json.dump(runtime_report, manifest_file, ensure_ascii=False, indent=2)
        except Exception as manifest_err:
            _add_log(task_id, f"[WARNING] 更新运行清单失败: {manifest_err}")
        tasks[task_id]["runtimeReport"] = runtime_report
        _add_log(task_id, f"[SUCCESS] Final Output Gate 通过，records={final_record_count}")

        # ============ PDF 下载逻辑（企业报告/新闻报告场景 + 选择下载文件）============
        downloaded_count = 0
        files_not_enough = False
        pdf_output_dir = None
        # 确保 _script_download_headers 已定义（LLM 生成的脚本可能没有此字段）
        if '_script_download_headers' not in dir():
            _script_download_headers = None
        
        if request.runMode in ["enterprise_report", "news_report_download"] and request.downloadReport == "yes" and len(reports) > 0:
            _add_log(task_id, "[INFO] 正在下载前5个PDF文件...")
            
            # 创建唯一的子文件夹：task_id + 时间戳
            import hashlib
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # 使用 URL 的哈希值作为标识（取前8位）
            url_hash = hashlib.md5(request.url.encode()).hexdigest()[:8]
            folder_name = f"{task_id}_{timestamp}_{url_hash}"
            
            pdf_output_dir = output_json_dir / "output_pdf" / folder_name
            pdf_output_dir.mkdir(parents=True, exist_ok=True)
            _add_log(task_id, f"[INFO] 下载目录: output_pdf/{folder_name}")
            
            # 最多下载5个
            max_download = 5
            to_download = reports[:max_download]
            
            # 检查是否不足5个
            if len(reports) < max_download:
                files_not_enough = True
                _add_log(task_id, f"[INFO] 日期范围内仅有 {len(reports)} 个文件，不足5份")
            
            import httpx
            import urllib.parse
            from urllib.parse import urlparse as _dl_urlparse
            
            # ── 构建防盗链请求头（泛化：从目标页面 URL 自动派生 Referer）──
            # 优先使用生成脚本输出的 downloadHeaders（更精准），否则从 request.url 派生
            _target_parsed = _dl_urlparse(request.url)
            _target_origin = f"{_target_parsed.scheme}://{_target_parsed.netloc}"
            _download_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/pdf, application/octet-stream, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": request.url,  # 用爬取的目标页面作为 Referer（最自然）
            }
            # 合并脚本输出的下载头（如果有），脚本头优先
            if _script_download_headers and isinstance(_script_download_headers, dict):
                _download_headers.update(_script_download_headers)
            
            downloaded_reports = []
            
            for i, report in enumerate(to_download):
                download_url = report.get("downloadUrl", "")
                if not download_url or download_url == "#":
                    _add_log(task_id, f"[WARNING] 报告 '{report.get('name', '未知')[:30]}...' 无有效下载链接，跳过")
                    downloaded_reports.append(report)  # 保留但不标记为本地
                    continue
                
                try:
                    # 生成安全的文件名
                    safe_name = report.get("name", f"report_{i+1}")[:50]
                    # 移除文件名中的非法字符
                    safe_name = "".join(c for c in safe_name if c.isalnum() or c in (' ', '-', '_', '.', '（', '）')).strip()
                    if not safe_name:
                        safe_name = f"report_{i+1}"
                    
                    file_ext = report.get("fileType", "pdf")
                    filename = f"{i+1}_{safe_name}.{file_ext}"
                    local_path = pdf_output_dir / filename
                    
                    # 下载文件（带防盗链头 + 403 自动重试）
                    dl_parsed = _dl_urlparse(download_url)
                    dl_headers = dict(_download_headers)
                    if dl_parsed.netloc and dl_parsed.netloc != _target_parsed.netloc:
                        dl_headers["Referer"] = f"{dl_parsed.scheme or 'https'}://{dl_parsed.netloc}/"
                    
                    # 构建候选 URL 列表（原始 URL + 子域名变体），用于 403 重试
                    candidate_urls = [download_url]
                    if dl_parsed.netloc:
                        # 常见的文件托管子域名前缀
                        # 如深交所: www.szse.cn/disc/... → disc.szse.cn/disc/...
                        path = dl_parsed.path or ""
                        base_domain = dl_parsed.netloc.replace("www.", "")
                        file_subdomains = ["disc", "download", "file", "static", "cdn", "docs"]
                        for prefix in file_subdomains:
                            if path.startswith(f"/{prefix}/"):
                                alt_host = f"{prefix}.{base_domain}"
                                if alt_host != dl_parsed.netloc:
                                    alt_url = f"{dl_parsed.scheme or 'https'}://{alt_host}{path}"
                                    candidate_urls.append(alt_url)
                                break
                    
                    dl_success = False
                    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True, verify=False) as client:
                        for try_url in candidate_urls:
                            response = await client.get(try_url, headers=dl_headers)
                            if response.status_code == 200:
                                with open(local_path, 'wb') as f:
                                    f.write(response.content)
                                _add_log(task_id, f"[SUCCESS] 已下载: {filename}")
                                downloaded_count += 1
                                relative_path = f"{folder_name}/{filename}"
                                report["localPath"] = relative_path
                                report["isLocal"] = True
                                dl_success = True
                                break
                            elif response.status_code == 403 and try_url != candidate_urls[-1]:
                                # 403 且还有备选 URL，继续尝试
                                continue
                            else:
                                _add_log(task_id, f"[WARNING] 下载失败 ({response.status_code}): {report.get('name', '未知')[:30]}...")
                            
                except Exception as dl_err:
                    _add_log(task_id, f"[WARNING] 下载出错: {str(dl_err)[:50]}...")
                
                downloaded_reports.append(report)
            
            # 只保留已下载的报告（前5个）
            reports = downloaded_reports
            _add_log(task_id, f"[SUCCESS] 共下载 {downloaded_count} 个文件到 {pdf_output_dir}")

        if request.runMode == "news_sentiment":
            downloaded_count, pdf_output_dir = await _download_news_attachments(
                task_id=task_id,
                articles=news_articles,
                request=request,
            )
            tasks[task_id]["downloadedCount"] = downloaded_count
            tasks[task_id]["pdfOutputDir"] = str(pdf_output_dir) if pdf_output_dir else None
        
        # 新闻舆情模式：如果选择下载，则保存 Markdown 文件
        if request.runMode == "news_sentiment" and request.downloadReport == "yes" and len(news_articles) > 0:
            _add_log(task_id, "[INFO] 正在生成 Markdown 文件...")
            
            # 创建 output_markdown 目录
            markdown_output_dir = config.output_dir.parent / "output" / "output_markdown"
            markdown_output_dir.mkdir(parents=True, exist_ok=True)
            
            # 生成 Markdown 内容
            md_lines = [
                f"# 新闻舆情爬取结果",
                f"",
                f"- **爬取时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"- **目标网址**: {request.url}",
                f"- **新闻数量**: {len(news_articles)}",
                f"",
                f"---",
                f""
            ]
            
            for article in news_articles:
                md_lines.append(f"## {article.get('id', '')}. {article.get('title', '未知标题')}")
                md_lines.append(f"")
                if article.get('date'):
                    md_lines.append(f"- **日期**: {article['date']}")
                if article.get('source'):
                    md_lines.append(f"- **来源**: {article['source']}")
                if article.get('author'):
                    md_lines.append(f"- **作者**: {article['author']}")
                if article.get('sourceUrl'):
                    md_lines.append(f"- **链接**: [{article['sourceUrl']}]({article['sourceUrl']})")
                for attachment in article.get('attachments') or []:
                    attachment_url = attachment.get('url')
                    if attachment_url:
                        md_lines.append(
                            f"- **附件 ({attachment.get('fileType', 'file').upper()})**: "
                            f"[{attachment.get('name') or 'Download'}]({attachment_url})"
                        )
                if article.get('summary'):
                    md_lines.append(f"")
                    md_lines.append(f"> {article['summary']}")
                md_lines.append(f"")
                md_lines.append(f"---")
                md_lines.append(f"")
            
            # 保存 Markdown 文件
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            md_filename = f"news_{request.siteName or 'crawl'}_{timestamp}.md"
            md_filepath = markdown_output_dir / md_filename
            
            with open(md_filepath, 'w', encoding='utf-8') as f:
                f.write('\n'.join(md_lines))
            
            markdown_file = str(md_filepath)
            _add_log(task_id, f"[SUCCESS] Markdown 已保存: {md_filepath}")
        
        # A generated script becomes a candidate only after both runtime and
        # final-output validation have passed.  The full Python file is the
        # only golden asset; no manifest or code copy is written elsewhere.
        if golden_asset is None:
            pending_path = _golden_store.stage_pending(
                task_id=task_id,
                signature=task_signature,
                code=script_code,
            )
            tasks[task_id]["goldenStatus"] = "pending"
            tasks[task_id]["goldenCodePath"] = str(pending_path)
            _add_log(task_id, f"[GOLDEN] 候选爬虫已进入 pending，等待人工评价: {pending_path}")

        planner_result.final_state.update({
            "task_signature": task_signature,
            "execution_source": tasks[task_id].get("executionSource"),
            "golden_code_path": tasks[task_id].get("goldenCodePath"),
            "golden_status": tasks[task_id].get("goldenStatus"),
        })

        # Stage-1 memory is grounded in the real isolated runtime result, not
        # in a specialist's self-reported success.
        try:
            from memory import finalize_runtime_episode
            memory_summary = await asyncio.to_thread(
                finalize_runtime_episode,
                planner_result=planner_result,
                runtime_report=runtime_report,
                final_output={"reports": reports, "newsArticles": news_articles},
                config=config,
                log_callback=lambda msg: _add_log(task_id, msg),
            )
            tasks[task_id]["autoFindings"] = memory_summary.get("auto_findings")
            tasks[task_id]["summaryDraftPath"] = memory_summary.get("summary_draft_path")
            tasks[task_id]["htmlFingerprint"] = memory_summary.get("html_fingerprint")
            tasks[task_id]["pendingFeedback"] = bool(memory_summary.get("summary_draft_path"))
            tasks[task_id]["validationReports"] = memory_summary.get("validation_reports")
            tasks[task_id]["attributionDecision"] = memory_summary.get("attribution_decision")
            tasks[task_id]["repairHistory"] = memory_summary.get("repair_history")
            memory_finalized = True
        except Exception as memory_err:
            _add_log(task_id, f"[WARNING] Runtime-grounded memory 写入失败: {memory_err}")

        # Step 12: 任务完成
        _update_step(task_id, 13, "🎉 任务完成")
        
        # 根据运行模式保存不同的结果
        if request.runMode == "news_sentiment":
            tasks[task_id]["newsArticles"] = news_articles
            tasks[task_id]["markdownFile"] = markdown_file
            tasks[task_id]["downloadedCount"] = downloaded_count
            tasks[task_id]["pdfOutputDir"] = str(pdf_output_dir) if pdf_output_dir else None
        else:
            tasks[task_id]["reports"] = reports
            tasks[task_id]["downloadedCount"] = downloaded_count
            tasks[task_id]["filesNotEnough"] = files_not_enough
            tasks[task_id]["pdfOutputDir"] = str(pdf_output_dir) if pdf_output_dir else None
        _add_log(task_id, "[SUCCESS] ✅ 任务完成！")
        
        tasks[task_id]["status"] = "completed"
        tasks[task_id]["resultFile"] = str(output_path)

        # 更新历史记录为完成
        try:
            # 过滤不需要存储的大对象（如果有）
            result_to_save = tasks[task_id].copy()
            # 移除 logs 以避免重复存储（logs 单独存储）
            if "logs" in result_to_save:
                del result_to_save["logs"]
            
            # 计算 record_count
            record_count = 0
            if request.runMode == "news_sentiment":
                record_count = len(news_articles)
            else:
                record_count = len(reports)
                
            await asyncio.to_thread(
                update_history_status, 
                task_id, 
                "completed", 
                result_to_save, 
                tasks[task_id]["logs"],
                end_at=datetime.now(),
                record_count=record_count
            )
        except Exception as e:
            print(f"保存历史记录失败: {e}")

        # SSE 推送完成
        if _event_broadcaster is not None:
            await _event_broadcaster.publish(task_id, "complete", {"status": "completed"})
        
    except asyncio.CancelledError:
        # asyncio 任务被取消（用户点击停止）
        _add_log(task_id, "[INFO] 任务已被强制停止")
        tasks[task_id]["status"] = "failed"
        tasks[task_id]["error"] = "任务被用户强制停止"

        try:
            result_to_save = tasks[task_id].copy()
            if "logs" in result_to_save:
                del result_to_save["logs"]
            
            # 计算 record_count (safe)
            record_count = 0
            # 尝试从 request 中获取 runMode，若 request 是对象则直接访问，若是字典则用 get
            _run_mode = request.runMode if hasattr(request, "runMode") else (request.get("runMode") if isinstance(request, dict) else "")
            
            if _run_mode == "news_sentiment":
                record_count = len(tasks[task_id].get("newsArticles") or [])
            else:
                record_count = len(tasks[task_id].get("reports") or [])

            await asyncio.to_thread(
                update_history_status, 
                task_id, 
                "failed", 
                result_to_save, 
                tasks[task_id]["logs"],
                end_at=datetime.now(),
                record_count=record_count
            )
        except Exception as e:
            print(f"保存历史记录失败: {e}")

        # SSE 推送取消
        if _event_broadcaster is not None:
            await _event_broadcaster.publish(task_id, "cancelled", {"status": "failed"})
        # 不要重新抛出，让 finally 执行清理
        
    except Exception as e:
        error_str = str(e)
        if golden_asset is not None and golden_runtime_started and _golden_failure_is_definitive(error_str):
            invalid_path = _golden_store.invalidate_active(
                signature=task_signature,
                task_id=task_id,
            )
            if invalid_path is not None:
                tasks[task_id]["goldenStatus"] = "invalid"
                tasks[task_id]["goldenCodePath"] = str(invalid_path)
                _add_log(
                    task_id,
                    "[GOLDEN] 已确认黄金爬虫无法通过结果 Gate，已移入 invalid；下次相同参数将重新走 Agent",
                )
        elif golden_asset is None:
            rejected_path = _golden_store.reject_pending(
                task_id=task_id,
                signature=task_signature,
            )
            if rejected_path is not None:
                tasks[task_id]["goldenStatus"] = "invalid"
                tasks[task_id]["goldenCodePath"] = str(rejected_path)

        if planner_result is not None:
            planner_result.final_state.update({
                "task_signature": task_signature,
                "execution_source": tasks[task_id].get("executionSource"),
                "golden_code_path": tasks[task_id].get("goldenCodePath"),
                "golden_status": tasks[task_id].get("goldenStatus"),
            })
        # 如果是用户取消，使用更友好的消息
        if "任务已被用户取消" in error_str:
            _add_log(task_id, "[INFO] 任务已被用户停止")
            tasks[task_id]["error"] = "任务被用户停止"
        else:
            _add_log(task_id, f"[ERROR] 生成失败: {error_str}")
            tasks[task_id]["error"] = error_str
            import traceback
            _add_log(task_id, f"[ERROR] {traceback.format_exc()}")
        
        tasks[task_id]["status"] = "failed"

        # Preserve failed runtime evidence as a pending memory draft. This is
        # where hidden selector/API mistakes become learnable across reruns.
        if planner_result is not None and not memory_finalized:
            try:
                from memory import finalize_runtime_episode
                failure_report = dict(runtime_report or {})
                failure_report.update({
                    "success": False,
                    "error": error_str,
                    "record_count": int(failure_report.get("record_count") or 0),
                })
                memory_summary = await asyncio.to_thread(
                    finalize_runtime_episode,
                    planner_result=planner_result,
                    runtime_report=failure_report,
                    final_output={
                        "reports": tasks[task_id].get("reports") or [],
                        "newsArticles": tasks[task_id].get("newsArticles") or [],
                    },
                    config=config,
                    log_callback=lambda msg: _add_log(task_id, msg),
                )
                tasks[task_id]["autoFindings"] = memory_summary.get("auto_findings")
                tasks[task_id]["summaryDraftPath"] = memory_summary.get("summary_draft_path")
                tasks[task_id]["htmlFingerprint"] = memory_summary.get("html_fingerprint")
                tasks[task_id]["pendingFeedback"] = bool(memory_summary.get("summary_draft_path"))
                tasks[task_id]["validationReports"] = memory_summary.get("validation_reports")
                tasks[task_id]["attributionDecision"] = memory_summary.get("attribution_decision")
                tasks[task_id]["repairHistory"] = memory_summary.get("repair_history")
                memory_finalized = True
            except Exception as memory_err:
                _add_log(task_id, f"[WARNING] 失败任务记忆写入失败: {memory_err}")

        try:
            result_to_save = tasks[task_id].copy()
            if "logs" in result_to_save:
                del result_to_save["logs"]
            
            # 计算 record_count (safe)
            record_count = 0
            # 尝试从 request 中获取 runMode，若 request 是对象则直接访问，若是字典则用 get
            _run_mode = request.runMode if hasattr(request, "runMode") else (request.get("runMode") if isinstance(request, dict) else "")
            
            if _run_mode == "news_sentiment":
                record_count = len(tasks[task_id].get("newsArticles") or [])
            else:
                record_count = len(tasks[task_id].get("reports") or [])

            await asyncio.to_thread(
                update_history_status, 
                task_id, 
                "failed", 
                result_to_save, 
                tasks[task_id]["logs"],
                end_at=datetime.now(),
                record_count=record_count
            )
        except Exception as e:
            print(f"保存历史记录失败: {e}")

        # SSE 推送失败
        if _event_broadcaster is not None:
            await _event_broadcaster.publish(task_id, "failed", {
                "status": "failed",
                "error": tasks[task_id].get("error", "")
            })
        
    finally:
        # 清理浏览器资源
        try:
            if browser:
                await browser.disconnect()
        except:
            pass
        try:
            if launcher:
                launcher.terminate()
        except:
            pass
        
        # 清理全局引用
        if task_id in task_browsers:
            del task_browsers[task_id]
        if task_id in task_cancelled:
            del task_cancelled[task_id]
        if task_id in task_asyncio_tasks:
            del task_asyncio_tasks[task_id]
        # 延迟清理 SSE 订阅（给客户端一点时间收到终止事件）
        if _event_broadcaster is not None:
            async def _delayed_cleanup():
                await asyncio.sleep(5)
                _event_broadcaster.cleanup(task_id)
            asyncio.ensure_future(_delayed_cleanup())

# ============ API 路由 ============

@app.get("/")
async def root():
    """健康检查"""
    return {"status": "ok", "service": "PyGen API", "version": "2.0"}

@app.post("/api/menu-tree", response_model=MenuTreeResponse)
async def get_menu_tree(request: MenuTreeRequest):
    """
    获取目标页面的目录树
    
    用于"多页爬取"模式，在目录选择页展示可选目录
    """
    if not request.url:
        raise HTTPException(status_code=400, detail="URL 不能为空")
    
    url = request.url
    if not url.startswith("http"):
        url = "https://" + url
    
    try:
        result = await _fetch_menu_tree(url)
        return MenuTreeResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取目录树失败: {str(e)}")

@app.post("/api/rerun/{task_id}")
async def rerun_task(task_id: str, body: Optional[Dict[str, Any]] = None):
    """重新运行指定任务。

    可选 body 字段（用于持久化记忆 + 反馈回放）::

        {
          "verdict":    "correct" | "wrong",  # 用户对原任务的评价
          "suggestion": "...",                # 用户建议（verdict=wrong 时必填）
          "skipFeedback": false,              # true 表示纯重跑、不写记忆
        }

    传入 ``verdict`` 时，先调用 :func:`commit_episode` 把上一次的草稿
    晋升为正式记录并更新站点画像；接着把 ``prev_task_id = task_id``
    透传给新任务，使 ``feedback_replay_hint`` 能注入专家图初始状态
    最顶端。
    """
    body = body or {}
    verdict = (body.get("verdict") or "").strip().lower() or None
    suggestion = (body.get("suggestion") or "").strip()
    skip_feedback = bool(body.get("skipFeedback") or False)

    req_data = None

    if task_id in tasks:
        task = tasks[task_id]
        if "request" in task:
            req_data = task["request"]

    if not req_data:
        try:
            history_item = await asyncio.to_thread(get_history_detail, task_id)
            if history_item and history_item.get("config"):
                req_data = history_item["config"]
        except Exception as e:
            print(f"从历史记录获取任务配置失败: {e}")

    if not req_data:
        raise HTTPException(status_code=404, detail="原任务不存在或已失效")

    if verdict and not skip_feedback:
        try:
            golden_update = await _transition_golden_from_feedback(task_id, verdict)
            print(
                f"[GOLDEN] rerun pre-transition: status={golden_update.get('goldenStatus')} "
                f"path={golden_update.get('goldenCodePath')}"
            )
        except Exception as golden_err:
            print(f"[GOLDEN] transition before rerun failed (continuing): {golden_err}")
        try:
            commit_result = await _commit_feedback_async(
                task_id=task_id,
                verdict=verdict,
                suggestion=suggestion,
            )
            print(f"[MEMORY] rerun pre-commit: stage={commit_result.get('stage')} "
                  f"warnings={commit_result.get('warnings')}")
        except HTTPException:
            raise
        except Exception as commit_err:
            print(f"[MEMORY] commit before rerun failed (continuing): {commit_err}")

    try:
        request = GenerateRequest(**req_data)
        if not skip_feedback:
            request.prevTaskId = task_id
        return await start_generation(request)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重试失败: {str(e)}")


# ============ 持久化记忆 / 任务评价 ============

class FeedbackRequest(BaseModel):
    """前端"任务评价"提交体。"""

    verdict: str  # "correct" | "wrong"
    suggestion: Optional[str] = ""


def _build_memory_store():
    """Lazy import + 实例化 MemoryStore（与 runner.py 行为一致）。"""
    if config is None:
        raise HTTPException(status_code=503, detail="config not initialized")
    try:
        from memory import MemoryStore  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"memory module unavailable: {e}")
    try:
        return MemoryStore(config.memory_root)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to init MemoryStore: {e}")


async def _commit_feedback_async(*, task_id: str, verdict: str, suggestion: str) -> Dict[str, Any]:
    """把 commit_episode 放到线程池，避免 LLM 调用阻塞事件循环。"""
    from memory import commit_episode  # type: ignore

    store = _build_memory_store()

    def _logger(msg: str):
        try:
            _add_log(task_id, msg)
        except Exception:
            print(msg)

    def _do_commit():
        return commit_episode(
            task_id=task_id,
            verdict=verdict,
            suggestion=suggestion,
            store=store,
            config=config,
            log_callback=_logger,
        )

    return await asyncio.to_thread(_do_commit)


@app.get("/api/tasks/{task_id}/draft")
async def get_task_draft(task_id: str):
    """读取草稿 episode，供前端"任务评价" Modal 展示 auto_findings。

    **重要**：此接口直接基于磁盘上的 ``episode/pending/<task_id>.json``，
    与内存里的 ``tasks`` 字典解耦——后端重启后、任务从内存里失效后，
    甚至业务方第二天回头评价，都能正常返回 draft。

    返回字段：
        - ``exists``           : 是否存在
        - ``auto_findings``    : 启发式扫描结果
        - ``facts``            : 任务事实摘要（成功率、工具调用次数等）
        - ``url`` / ``domain`` : 任务对应站点
        - ``html_fingerprint`` : 用于站点画像漂移检测
        - ``user_verdict``     : 已存在的评价（如有），用来判断 pending 状态
    """
    store = _build_memory_store()
    try:
        draft = await asyncio.to_thread(store.read_draft, task_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to read draft: {e}")

    if draft is None:
        return {
            "exists": False,
            "task_id": task_id,
            "auto_findings": None,
            "facts": None,
            "url": None,
            "domain": None,
            "html_fingerprint": None,
            "user_verdict": None,
        }

    # `pending` 反映"是否仍在等待用户评价"。draft 上有 user_verdict 时
    # 说明用户已提交（理论上提交后 commit_episode 会删 draft，但失败时
    # 可能残留），前端据此可决定是否再弹窗。
    pending = not bool(draft.get("user_verdict"))

    return {
        "exists": True,
        "task_id": task_id,
        "pending": pending,
        "auto_findings": draft.get("auto_findings"),
        "facts": draft.get("facts"),
        "url": draft.get("url"),
        "domain": draft.get("domain"),
        "html_fingerprint": draft.get("html_fingerprint"),
        "user_verdict": draft.get("user_verdict"),
        "user_suggestion": draft.get("user_suggestion"),
        "started_at": draft.get("started_at"),
        "ended_at": draft.get("ended_at"),
        "verified_selectors_count": draft.get("verified_selectors_count"),
    }


@app.post("/api/tasks/{task_id}/feedback")
async def submit_task_feedback(task_id: str, body: FeedbackRequest):
    """提交"任务评价"，触发 Stage-2 LLM 总结 + 站点画像更新。

    前端流程：
        1. 任务跑完后弹出"任务评价" Modal（必填）。
        2. 用户选择 ``correct`` / ``wrong``，``wrong`` 时必填 ``suggestion``。
        3. 调用本接口 → commit_episode → episode/episodes.jsonl + site/<domain>.json。
        4. 如需重跑，前端再调用 ``POST /api/rerun/{task_id}``，已写入的
           draft 不会重复 commit（draft 已被删除）。
    """
    verdict = (body.verdict or "").strip().lower()
    suggestion = (body.suggestion or "").strip()

    if verdict not in {"correct", "wrong"}:
        raise HTTPException(status_code=400, detail="verdict must be 'correct' or 'wrong'")
    if verdict == "wrong" and not suggestion:
        raise HTTPException(
            status_code=400,
            detail="suggestion is required when verdict='wrong'",
        )

    # ---- Stage-2 复盘开场白 ----
    # 让用户在前端日志面板里能看到「Agent 正在根据反馈做深度复盘」这件事，
    # 而不是一个静默的 spinner。下面 commit_episode 在线程池里跑期间，
    # 它内部的 [COMMIT] / [MEMORY] 日志会通过 _logger -> _add_log 持续 push
    # 进 tasks[task_id]["logs"]，前端在收到本接口响应后会再 poll 一次
    # /api/status 把这批日志拉回来。
    _add_log(task_id, "🧠 [SUMMARY/Stage-2] 收到用户反馈，开始 LLM 深度复盘…")
    _add_log(
        task_id,
        f"🧠 [SUMMARY/Stage-2] verdict={verdict}"
        + (
            f" | 用户原话: {suggestion if len(suggestion) <= 120 else suggestion[:120] + '…'}"
            if suggestion
            else ""
        ),
    )
    # 同步更新侧边步骤标签为「正在根据反馈复盘」（不推进 step 索引，避免
    # 把进度条往前拽；只是把当前格的 label 换成更贴当前动作的话术）。
    if task_id in tasks:
        try:
            current_step = int(tasks[task_id].get("currentStep") or 0)
        except Exception:
            current_step = 12
        _update_step(task_id, current_step, "🧠 Agent 正在根据反馈做 LLM 深度复盘…")

    try:
        golden_update = await _transition_golden_from_feedback(task_id, verdict)
    except Exception as e:
        _add_log(task_id, f"[ERROR] 黄金爬虫状态迁移失败: {e}")
        raise HTTPException(status_code=500, detail=f"golden crawler transition failed: {e}")

    if golden_update.get("goldenStatus") == "active":
        _add_log(task_id, f"[GOLDEN] 用户确认成功，黄金爬虫已激活: {golden_update.get('goldenCodePath')}")
    elif golden_update.get("goldenStatus") == "invalid":
        _add_log(task_id, f"[GOLDEN] 用户判定失败，脚本已移入 invalid: {golden_update.get('goldenCodePath')}")

    try:
        result = await _commit_feedback_async(
            task_id=task_id,
            verdict=verdict,
            suggestion=suggestion,
        )
    except HTTPException:
        raise
    except Exception as e:
        _add_log(task_id, f"❌ [SUMMARY/Stage-2] 复盘失败: {e}（黄金脚本状态已按用户评价更新）")
        raise HTTPException(status_code=500, detail=f"commit_episode failed: {e}")

    # ---- Stage-2 收尾 ----
    # commit_episode 返回里同时挂了 ``stage``（旧 key）和我们后面加的 ``enrichment_stage``，
    # 谁有就用谁，避免 commit.py 内部字段重命名时这里失声。
    enrichment_stage = result.get("enrichment_stage") or result.get("stage") or "unknown"
    if result.get("ok"):
        _add_log(
            task_id,
            f"✅ [SUMMARY/Stage-2] 复盘完成 (enrichment={enrichment_stage}) → 经验已写入 episode/episodes.jsonl + site/<domain>.json",
        )
        # 把"复盘那一格"的 label 切到 Stage-2 完成态。前端 pollStatus 在
        # status=completed 分支下也会把这个 stepLabel 透传到 SUMMARIZE_STEP_INDEX
        # （见 ExecutionView.tsx），所以用户能看见 Stage-1 → Stage-2 的真实切换。
        if task_id in tasks:
            try:
                final_step = int(tasks[task_id].get("currentStep") or 12)
            except Exception:
                final_step = 12
            _update_step(
                task_id,
                final_step,
                f"✅ Stage-2 LLM 复盘完成 ({enrichment_stage})",
            )
    else:
        warns = result.get("warnings") or []
        _add_log(
            task_id,
            f"⚠️ [SUMMARY/Stage-2] 复盘部分失败 (enrichment={enrichment_stage}) warnings={warns}",
        )
        if task_id in tasks:
            try:
                final_step = int(tasks[task_id].get("currentStep") or 12)
            except Exception:
                final_step = 12
            _update_step(
                task_id,
                final_step,
                f"⚠️ Stage-2 复盘部分失败 ({enrichment_stage})",
            )

    # 同步把 verdict 写到内存任务字段，便于 /api/status 即时反映
    if task_id in tasks:
        tasks[task_id]["userVerdict"] = verdict
        tasks[task_id]["userSuggestion"] = suggestion
        tasks[task_id]["pendingFeedback"] = False

    episode = result.get("episode") or {}
    profile = result.get("profile") or {}
    return {
        "ok": bool(result.get("ok")),
        "stage": result.get("stage"),
        "warnings": result.get("warnings", []),
        "lessons": (episode.get("lessons") if isinstance(episode, dict) else None),
        "domain": (episode.get("domain") if isinstance(episode, dict) else None),
        "profile_confidence": (
            profile.get("confidence") if isinstance(profile, dict) else None
        ),
        "profile_quarantined": (
            profile.get("quarantined") if isinstance(profile, dict) else None
        ),
        "task_signature": golden_update.get("taskSignature"),
        "golden_status": golden_update.get("goldenStatus"),
        "golden_code_path": golden_update.get("goldenCodePath"),
    }

@app.post("/api/generate")
async def start_generation(request: GenerateRequest):
    """
    启动爬虫脚本生成任务
    
    返回任务 ID，前端可通过 /api/status/{task_id} 轮询状态
    queue 模式下任务先排队，非 queue 模式（本地开发默认）直接运行
    """
    if not request.url:
        raise HTTPException(status_code=400, detail="URL 不能为空")
    
    # 创建任务
    task_id = str(uuid.uuid4())[:8]
    task_signature = compute_task_signature(request)

    # 判断是否进入排队
    use_queue = _task_queue is not None
    initial_status = "queued" if use_queue else "pending"

    tasks[task_id] = {
        "taskId": task_id,
        "request": request.model_dump(),
        "taskSignature": task_signature,
        "executionSource": None,
        "goldenCodePath": None,
        "goldenStatus": None,
        "status": initial_status,
        "currentStep": 0,
        "totalSteps": 14,  # 0..13: 启动 → 连接 → 打开 → 滚动 → 分析 → 增强分析 → LLM 生成 → 验证代码 → 已生成 → 运行 → 验证结果 → 🧠 自我复盘 → 完成
        "stepLabel": "排队等待中..." if use_queue else "准备中...",
        "logs": [],
        "resultFile": None,
        "error": None,
        "reports": [],  # 企业报告场景
        "downloadedCount": 0,  # 已下载文件数量
        "filesNotEnough": False,  # 文件是否不足5份
        "pdfOutputDir": None,  # PDF 下载目录
        "newsArticles": [],  # 新闻舆情场景
        "markdownFile": None,  # 新闻舆情 Markdown 文件
        "createdAt": time.time()
    }
    
    # 记录到历史记录数据库
    try:
        await asyncio.to_thread(add_history, task_id, "single", request.model_dump(), "admin")
    except Exception as e:
        print(f"写入历史记录失败: {e}")
    
    if use_queue:
        # 排队模式：放入队列，worker 会在有空位时启动任务
        position = await _task_queue.enqueue(
            task_id,
            lambda _tid=task_id, _req=request: _run_generation_task(_tid, _req)
        )
        _add_log(task_id, f"[INFO] 任务已加入队列，当前排在第 {position} 位")

        # SSE 推送排队位置
        if _event_broadcaster is not None:
            await _event_broadcaster.publish(task_id, "queue_position", {
                "position": position,
                **_task_queue.get_queue_info(task_id)
            })

        return {"taskId": task_id, "message": f"任务已加入队列（第 {position} 位）"}
    else:
        # 直接运行模式（兼容原有行为）
        asyncio_task = asyncio.create_task(_run_generation_task(task_id, request))
        task_asyncio_tasks[task_id] = asyncio_task
        return {"taskId": task_id, "message": "任务已创建"}

def _read_draft_for_status(task_id: str) -> Optional[Dict[str, Any]]:
    """同步读取磁盘 draft（如有）；失败时静默返回 None，不影响 /api/status 主流程。

    用于 ``/api/status/{task_id}`` 兜底：当任务已不在内存（重启后 / 走历史）
    时，仍能从 ``episode/pending/<task_id>.json`` 推断出 ``pendingFeedback``
    与 ``autoFindings``，让前端"待评价"徽章在任意时间都能回填。
    """
    if config is None:
        return None
    try:
        from memory import MemoryStore  # type: ignore
    except Exception:
        return None
    try:
        store = MemoryStore(config.memory_root)
        d = store.read_draft(task_id)
        return dict(d) if d else None
    except Exception:
        return None


@app.get("/api/status/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    """
    获取任务状态和日志
    
    前端轮询此接口以更新进度和日志
    """
    # 0. 不论走哪条路径都先尝试拉一次磁盘 draft，作为 pendingFeedback 的兜底数据源
    draft_disk = await asyncio.to_thread(_read_draft_for_status, task_id)
    draft_pending_feedback: Optional[bool] = None
    draft_auto_findings: Optional[Dict[str, Any]] = None
    draft_html_fingerprint: Optional[str] = None
    draft_user_verdict: Optional[str] = None
    draft_user_suggestion: Optional[str] = None
    if draft_disk is not None:
        draft_user_verdict = draft_disk.get("user_verdict")
        draft_user_suggestion = draft_disk.get("user_suggestion") or None
        draft_pending_feedback = not bool(draft_user_verdict)
        draft_auto_findings = draft_disk.get("auto_findings")
        draft_html_fingerprint = draft_disk.get("html_fingerprint")

    # 1. 优先从内存中获取（活跃任务）
    if task_id in tasks:
        task = tasks[task_id]

        # 队列信息（仅队列开启时返回）
        queue_position = None
        queue_waiting = None
        queue_running = None
        estimated_wait = None
        if _task_queue is not None:
            qi = _task_queue.get_queue_info(task_id)
            queue_position = qi["position"]
            queue_waiting = qi["waitingCount"]
            queue_running = qi["runningCount"]
            estimated_wait = qi["estimatedWaitSeconds"]
            # 更新排队中任务的 stepLabel（实时位置）
            if task["status"] == "queued" and queue_position > 0:
                task["stepLabel"] = f"排队等待中...（第 {queue_position} 位，预计 {estimated_wait}s）"

        # pendingFeedback / autoFindings：内存值优先，磁盘 draft 兜底
        pending_feedback = task.get("pendingFeedback")
        if pending_feedback is None:
            pending_feedback = draft_pending_feedback
        auto_findings = task.get("autoFindings") or draft_auto_findings
        html_fingerprint = task.get("htmlFingerprint") or draft_html_fingerprint
        user_verdict = task.get("userVerdict") or draft_user_verdict
        user_suggestion = task.get("userSuggestion") or draft_user_suggestion

        return TaskStatusResponse(
            taskId=task["taskId"],
            status=task["status"],
            currentStep=task["currentStep"],
            totalSteps=task["totalSteps"],
            stepLabel=task["stepLabel"],
            logs=task["logs"],
            resultFile=task.get("resultFile"),
            error=task.get("error"),
            reports=task.get("reports"),
            downloadedCount=task.get("downloadedCount"),
            filesNotEnough=task.get("filesNotEnough"),
            pdfOutputDir=task.get("pdfOutputDir"),
            newsArticles=task.get("newsArticles"),
            markdownFile=task.get("markdownFile"),
            totalCount=task.get("totalCount"),
            queuePosition=queue_position,
            queueWaitingCount=queue_waiting,
            queueRunningCount=queue_running,
            estimatedWaitSeconds=estimated_wait,
            pendingFeedback=pending_feedback,
            autoFindings=auto_findings,
            htmlFingerprint=html_fingerprint,
            userVerdict=user_verdict,
            userSuggestion=user_suggestion,
            stageEvidence=task.get("stageEvidence"),
            validationReports=task.get("validationReports"),
            attributionDecision=task.get("attributionDecision"),
            repairHistory=task.get("repairHistory"),
            runtimeReport=task.get("runtimeReport"),
            taskSignature=task.get("taskSignature"),
            executionSource=task.get("executionSource"),
            goldenCodePath=task.get("goldenCodePath"),
            goldenStatus=task.get("goldenStatus"),
        )
    
    # 2. 如果内存中没有，尝试从数据库获取（历史任务）
    try:
        history_item = await asyncio.to_thread(get_history_detail, task_id)
        if history_item:
            # 构造响应
            result = history_item.get("result", {})
            return TaskStatusResponse(
                taskId=history_item["id"],
                status=history_item["status"],
                currentStep=13 if history_item["status"] == "completed" else 0, # 假定完成
                totalSteps=14,
                stepLabel="任务已归档",
                logs=history_item.get("logs", []),
                resultFile=result.get("resultFile"),
                error=result.get("error"),
                reports=result.get("reports"),
                downloadedCount=result.get("downloadedCount"),
                filesNotEnough=result.get("filesNotEnough"),
                pdfOutputDir=result.get("pdfOutputDir"),
                newsArticles=result.get("newsArticles"),
                markdownFile=result.get("markdownFile"),
                totalCount=result.get("totalCount"),
                # 即使任务已归档，只要磁盘 draft 还在 → 业务方依旧可以补评价
                pendingFeedback=draft_pending_feedback,
                autoFindings=draft_auto_findings,
                htmlFingerprint=draft_html_fingerprint,
                userVerdict=draft_user_verdict,
                userSuggestion=draft_user_suggestion,
                stageEvidence=result.get("stageEvidence") or (draft_disk or {}).get("stage_evidence"),
                validationReports=result.get("validationReports") or (draft_disk or {}).get("validation_reports"),
                attributionDecision=result.get("attributionDecision") or (draft_disk or {}).get("attribution_decision"),
                repairHistory=result.get("repairHistory") or (draft_disk or {}).get("repair_history"),
                runtimeReport=result.get("runtimeReport") or (draft_disk or {}).get("runtime_report"),
                taskSignature=result.get("taskSignature") or (draft_disk or {}).get("task_signature"),
                executionSource=result.get("executionSource") or (draft_disk or {}).get("execution_source"),
                goldenCodePath=result.get("goldenCodePath") or (draft_disk or {}).get("golden_code_path"),
                goldenStatus=result.get("goldenStatus") or (draft_disk or {}).get("golden_status"),
            )
    except Exception as e:
        print(f"从数据库恢复任务状态失败: {e}")

    # 3. 内存 + 历史都没有，但磁盘 draft 还在：返回最简响应让前端能补评价
    if draft_disk is not None:
        url_in_draft = draft_disk.get("url") or ""
        return TaskStatusResponse(
            taskId=task_id,
            status="completed" if not draft_disk.get("user_verdict") else "completed",
            currentStep=13,
            totalSteps=14,
            stepLabel="任务已归档（仅 draft 仍可用）",
            logs=[],
            resultFile=None,
            error=None,
            pendingFeedback=draft_pending_feedback,
            autoFindings=draft_auto_findings,
            htmlFingerprint=draft_html_fingerprint,
            userVerdict=draft_user_verdict,
            userSuggestion=draft_user_suggestion,
            stageEvidence=draft_disk.get("stage_evidence"),
            validationReports=draft_disk.get("validation_reports"),
            attributionDecision=draft_disk.get("attribution_decision"),
            repairHistory=draft_disk.get("repair_history"),
            runtimeReport=draft_disk.get("runtime_report"),
            taskSignature=draft_disk.get("task_signature"),
            executionSource=draft_disk.get("execution_source"),
            goldenCodePath=draft_disk.get("golden_code_path"),
            goldenStatus=draft_disk.get("golden_status"),
        )

    raise HTTPException(status_code=404, detail="任务不存在")

@app.get("/api/download/{filename}")
async def download_file(filename: str):
    """
    下载生成的脚本文件
    """
    output_dir = config.output_dir if config else Path("./py")
    file_path = output_dir / filename
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type="text/x-python"
    )

@app.post("/api/download/batch")
async def download_batch_files(request: BatchDownloadRequest):
    """
    批量下载脚本文件（打包为 ZIP）
    """
    import io
    import zipfile
    
    output_dir = config.output_dir if config else Path("./py")
    
    # 在内存中创建 ZIP
    zip_buffer = io.BytesIO()
    
    # 检查是否有有效文件
    valid_files_count = 0
    
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename in request.filenames:
            # 安全检查：防止路径遍历
            safe_name = Path(filename).name
            file_path = output_dir / safe_name
            
            if file_path.exists() and file_path.is_file():
                try:
                    # 将文件写入 ZIP，arcname 为 ZIP 内的文件名
                    zf.write(file_path, arcname=safe_name)
                    valid_files_count += 1
                except Exception as e:
                    print(f"[WARNING] 添加文件到 ZIP 失败: {filename} - {e}")
            else:
                print(f"[WARNING] 批量下载跳过不存在的文件: {filename}")
    
    if valid_files_count == 0:
        raise HTTPException(status_code=404, detail="选中的文件均不存在")

    # 指针回到开头
    zip_buffer.seek(0)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = f"scripts_batch_{timestamp}.zip"
    
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename={zip_filename}"
        }
    )


@app.get("/api/pdf/{filepath:path}")
async def view_pdf(filepath: str):
    """
    查看本地下载的 PDF 文件
    
    filepath 格式: "子文件夹名/文件名.pdf" (相对于 output/output_pdf 目录)
    """
    import urllib.parse
    from starlette.responses import Response
    
    try:
        # 解码 URL 编码的路径
        decoded_path = urllib.parse.unquote(filepath)
        
        # 与保存文件时使用相同的路径逻辑
        # output_dir 是 py 目录，output_dir.parent / "output" 是 output 目录
        output_dir = config.output_dir if config else Path("./py")
        pdf_base_dir = output_dir.parent / "output" / "output_pdf"
        
        # 调试日志
        print(f"[DEBUG] 请求路径: {filepath}")
        print(f"[DEBUG] 解码路径: {decoded_path}")
        print(f"[DEBUG] output_dir: {output_dir}")
        print(f"[DEBUG] pdf_base_dir: {pdf_base_dir}")
        print(f"[DEBUG] pdf_base_dir 绝对路径: {pdf_base_dir.absolute()}")
        print(f"[DEBUG] pdf_base_dir 存在: {pdf_base_dir.exists()}")
        
        # 首先尝试直接拼接路径，并阻止 ../ 穿越 output_pdf 根目录。
        pdf_base_resolved = pdf_base_dir.resolve()
        file_path = (pdf_base_dir / decoded_path).resolve()
        try:
            file_path.relative_to(pdf_base_resolved)
        except ValueError:
            raise HTTPException(status_code=400, detail="非法文件路径")
        print(f"[DEBUG] 尝试路径: {file_path}")
        print(f"[DEBUG] 文件存在: {file_path.exists()}")
        
        if not file_path.exists() and pdf_base_dir.exists():
            # 如果找不到，尝试遍历子文件夹
            filename_only = Path(decoded_path).name
            for subdir in pdf_base_dir.iterdir():
                if subdir.is_dir():
                    potential_path = subdir / filename_only
                    if potential_path.exists():
                        file_path = potential_path
                        break
        
        if not file_path.exists():
            raise HTTPException(
                status_code=404, 
                detail=f"PDF 文件不存在: {filepath}, 查找目录: {pdf_base_dir}, 完整路径: {file_path}"
            )
        
        # 根据文件扩展名确定 MIME 类型
        ext = file_path.suffix.lower()
        mime_types = {
            ".pdf": "application/pdf",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xls": "application/vnd.ms-excel",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        media_type = mime_types.get(ext, "application/octet-stream")
        
        # 读取文件内容
        with open(file_path, 'rb') as f:
            content = f.read()
        
        # 对文件名进行 URL 编码，处理中文字符
        # 使用 RFC 5987 格式: filename*=UTF-8''encoded_filename
        encoded_filename = urllib.parse.quote(file_path.name)
        
        # 使用 inline 模式让浏览器直接显示而不是下载
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f"inline; filename*=UTF-8''{encoded_filename}"
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取文件失败: {str(e)}")


@app.post("/api/stop/{task_id}")
async def stop_task(task_id: str):
    """
    停止正在运行的任务
    
    终止任务相关的所有进程（浏览器、爬虫脚本等）
    """
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    task = tasks[task_id]
    
    # 如果任务已经完成或失败，无需停止
    if task["status"] in ["completed", "failed"]:
        return {"message": "任务已结束", "status": task["status"]}
    
    _add_log(task_id, "[INFO] 收到停止请求，正在强制终止任务...")

    # 如果任务还在排队中，直接从队列移除
    if _task_queue is not None and task["status"] == "queued":
        _task_queue.cancel(task_id)
        task["status"] = "failed"
        task["error"] = "任务在排队中被取消"
        
        # 更新历史记录
        try:
            result_to_save = task.copy()
            if "logs" in result_to_save:
                del result_to_save["logs"]
            await asyncio.to_thread(update_history_status, task_id, "failed", result_to_save, task["logs"])
        except Exception as e:
            print(f"更新历史记录失败: {e}")

        _add_log(task_id, "[INFO] 任务已从排队队列中移除")
        if _event_broadcaster is not None:
            await _event_broadcaster.publish(task_id, "cancelled", {"status": "failed"})
        return {"message": "任务已取消（排队中）", "taskId": task_id}

    # 设置取消标志
    task_cancelled[task_id] = True
    
    # 【关键】取消 asyncio 任务（立即中断正在等待的异步操作）
    if task_id in task_asyncio_tasks:
        asyncio_task = task_asyncio_tasks[task_id]
        if not asyncio_task.done():
            asyncio_task.cancel()
            _add_log(task_id, "[INFO] 异步任务已取消")
        if task_id in task_asyncio_tasks:
            del task_asyncio_tasks[task_id]
    
    # 尝试关闭浏览器
    if task_id in task_browsers:
        browser_info = task_browsers[task_id]
        try:
            browser = browser_info.get("browser")
            launcher = browser_info.get("launcher")
            
            if browser:
                try:
                    await browser.disconnect()
                    _add_log(task_id, "[INFO] 浏览器连接已断开")
                except Exception as e:
                    _add_log(task_id, f"[WARNING] 断开浏览器时出错: {e}")
            
            if launcher:
                try:
                    launcher.terminate()
                    _add_log(task_id, "[INFO] Chrome 进程已终止")
                except Exception as e:
                    _add_log(task_id, f"[WARNING] 终止 Chrome 时出错: {e}")
                    
        except Exception as e:
            _add_log(task_id, f"[WARNING] 清理浏览器资源时出错: {e}")
        finally:
            if task_id in task_browsers:
                del task_browsers[task_id]
    
    # 更新任务状态
    task["status"] = "failed"
    task["error"] = "任务被用户停止"
    
    # 更新历史记录
    try:
        result_to_save = task.copy()
        if "logs" in result_to_save:
            del result_to_save["logs"]
        
        # 计算 record_count
        record_count = 0
        _req = task.get("request", {})
        if _req.get("runMode") == "news_sentiment":
            record_count = len(task.get("newsArticles") or [])
        else:
            record_count = len(task.get("reports") or [])
            
        await asyncio.to_thread(
            update_history_status, 
            task_id, 
            "failed", 
            result_to_save, 
            task["logs"],
            end_at=datetime.now(),
            record_count=record_count
        )
    except Exception as e:
        print(f"更新历史记录失败: {e}")

    _add_log(task_id, "[INFO] ✅ 任务已停止")
    
    return {"message": "任务已停止", "taskId": task_id}

# ============ 队列 & SSE 路由 ============

@app.get("/api/queue/info")
async def get_queue_info():
    """
    获取全局队列状态

    仅在 enable_queue=true 时返回有意义的数据；
    关闭时返回 queueEnabled=false，前端据此隐藏排队 UI。
    """
    if _task_queue is None:
        return {"queueEnabled": False}
    return _task_queue.get_queue_info()


@app.get("/api/events/{task_id}")
async def sse_events(task_id: str):
    """
    SSE 实时事件流

    前端通过 EventSource('/api/events/{taskId}') 订阅。
    仅在 enable_sse=true 时可用，否则返回 404。
    """
    if _event_broadcaster is None:
        raise HTTPException(status_code=404, detail="SSE 未启用")
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")

    return StreamingResponse(
        _event_broadcaster.subscribe(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx 不缓冲
        }
    )


@app.get("/api/history")
async def get_history_list():
    """
    获取历史任务列表
    """
    try:
        return await asyncio.to_thread(get_all_history)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取历史记录失败: {str(e)}")

@app.get("/api/history/{task_id}")
async def get_history_item(task_id: str):
    """
    获取单个历史任务详情
    """
    try:
        item = await asyncio.to_thread(get_history_detail, task_id)
        if not item:
            raise HTTPException(status_code=404, detail="任务不存在")
        return item
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取任务详情失败: {str(e)}")

@app.delete("/api/history/{task_id}")
async def delete_history_item(task_id: str):
    """
    删除单个历史任务
    """
    try:
        deleted = await asyncio.to_thread(delete_history_record, task_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"message": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除历史记录失败: {str(e)}")

class HistoryLogRequest(BaseModel):
    id: str
    taskType: str
    status: str
    config: Any
    result: Optional[Any] = None
    logs: Optional[List[str]] = None
    endAt: Optional[datetime] = None
    recordCount: Optional[int] = None
    owner: Optional[str] = "admin"

@app.post("/api/history/log")
async def log_history(request: HistoryLogRequest):
    """
    前端主动记录历史（用于 Batch 任务等客户端管理的任务）
    """
    try:
        # 检查是否存在
        existing = await asyncio.to_thread(get_history_detail, request.id)
        if existing:
            await asyncio.to_thread(
                update_history_status, 
                request.id, 
                request.status, 
                request.result or request.config, # Batch 任务 result 往往就是更新后的 config (jobs)
                request.logs,
                request.endAt,
                request.recordCount
            )
        else:
            await asyncio.to_thread(
                add_history, 
                request.id, 
                request.taskType, 
                request.config,
                request.owner or "admin"
            )
        return {"message": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"记录历史失败: {str(e)}")

# ============ 主入口 ============

if __name__ == "__main__":
    # Windows + Python 3.12+ 默认 ProactorEventLoop 在进程退出/CTRL+C 时
    # 偶发出现 "unclosed transport" / "I/O operation on closed pipe" 的清理噪音。
    # 切换为 SelectorEventLoop 可显著减少该类报错（不影响功能）。
    import os as _os
    import asyncio as _asyncio
    if _os.name == "nt":
        try:
            _asyncio.set_event_loop_policy(_asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
        except Exception:
            pass

    import uvicorn
    print("🚀 启动 PyGen API Server...")
    print("   访问 http://localhost:8000/docs 查看 API 文档")
    uvicorn.run(app, host="0.0.0.0", port=8000)
