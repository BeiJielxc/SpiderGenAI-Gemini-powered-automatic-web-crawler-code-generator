import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { 
  CheckCircle2, 
  Circle, 
  Loader2, 
  XCircle, 
  Terminal, 
  ExternalLink,
  FileText,
  ArrowLeft,
  PlayCircle,
  Download,
  Search,
  ChevronLeft,
  ChevronRight,
  File,
  DownloadCloud,
  AlertCircle,
  Square,
  CheckSquare,
  StopCircle,
  Eye,
  Info,
  Clock,
  Users
} from 'lucide-react';
import { ProcessStep, StepStatus, CrawlerFormData, API_BASE_URL, TaskStatusResponse, GenerateRequest, ReportFile, NewsArticle, NewsAttachment, QueueInfo, AutoFindings } from '../types';
import { explainError } from '../utils/errorExplain';
import TaskFeedbackModal from './TaskFeedbackModal';

interface ExecutionViewProps {
  mode: string;
  formData: CrawlerFormData;
  selectedPaths: string[];
  taskId: string;
  initialLogLines?: string[];
  initialReports?: ReportFile[];
  initialNewsArticles?: NewsArticle[];
  initialRawStatus?: TaskStatusResponse['status'];
  disableAutoStart?: boolean;
  // Hole-2.B: 用户从历史页/批量页"重新运行"进入时，把原任务 ID 透传到这里。
  // 第一次 /api/generate 会把它放进 prevTaskId 字段，让后端 planner 拿到
  // feedback_replay_hint。后端 (runner.py) 会按 domain 二次校验，跨域名时
  // 自动丢弃。仅在第一次自动启动时使用，后续 startGeneration 不会再带。
  prevTaskId?: string;
  onTaskIdChange: (taskId: string) => void;
  onBack: () => void;
}

const STEPS_TEMPLATE = [
  "正在启动Chrome浏览器",
  "正在连接浏览器",
  "正在打开目标页面",
  "正在滚动页面以加载更多内容",
  "正在分析页面结构",
  "正在执行增强页面分析",
  "正在调用LLM生成爬虫脚本",
  "🔍 正在验证生成的代码",
  "爬虫脚本已生成",
  "正在运行爬虫脚本",
  "📊 正在验证爬取结果",
  // ⚠️ 实际执行顺序 = critic / 跑爬虫 / 验证结果都跑完之后，summarize_node 才会
  // 跑（它是 planner_graph 的最后一个节点）。所以这一格放在"验证爬取结果"之后、
  // "任务完成"之前才与真实时序对应；之前画在 7 号位是因为代码 hard-code 错了 index。
  "🧠 Agent 正在自我复盘 (Stage-1)",
  "🎉 任务完成"
];

// 必须与 pygen/agents/summarize_node.py 里的 SUMMARIZE_STEP_INDEX 同步。
// 后端在用户提交反馈、Stage-2 LLM 复盘开始时会调用 _update_step(task_id,
// SUMMARIZE_STEP_INDEX, "🧠 Agent 正在根据反馈做 LLM 深度复盘…")，前端依靠
// 这个常量识别"应该把后端的临时 stepLabel 透传到哪一格"——即使任务整体
// status 已经是 completed 也要透传，否则那格永远显示模板里写死的 Stage-1。
const SUMMARIZE_STEP_INDEX = 12;

const MOCK_NEWS_CONTENT = `[2026-05-12] 行业动态：在最新发布的季度财报中，该公司展示了强劲的增长势头，营收同比增长25%。
[2026-05-10] 政策解读：新的环保法规出台，对制造业提出了更高的碳排放要求。
[2026-05-08] 竞争对手分析：主要竞争对手X公司宣布收购了一家AI初创企业。
[2026-05-05] 消费者洞察：最新的消费者调查显示，Z世代用户更倾向于购买具有可持续发展理念的产品。`;

const ExecutionView: React.FC<ExecutionViewProps> = ({ 
  mode, 
  formData, 
  selectedPaths, 
  taskId: initialTaskId,
  initialLogLines,
  initialReports,
  initialNewsArticles,
  initialRawStatus,
  disableAutoStart = false,
  prevTaskId,
  onTaskIdChange,
  onBack 
}) => {
  const [taskId, setTaskId] = useState(initialTaskId);
  const [steps, setSteps] = useState<ProcessStep[]>(
    STEPS_TEMPLATE.map((label, index) => ({
      id: index,
      label,
      status: 'pending'
    }))
  );
  
  const [logContent, setLogContent] = useState<string>(
    initialLogLines && initialLogLines.length > 0 ? initialLogLines.join('\n') : ""
  );
  const [taskStatus, setTaskStatus] = useState<'pending' | 'queued' | 'running' | 'completed' | 'failed'>(
    initialRawStatus || 'pending'
  );
  const [resultFile, setResultFile] = useState<string>("");
  const [errorMsg, setErrorMsg] = useState<string>("");
  const explainedError = useMemo(() => explainError(errorMsg), [errorMsg]);
  const errorTitle = useMemo(() => {
    const normalized = (errorMsg || '').toLowerCase();
    if (normalized.includes('max iterations reached without generating code')) {
      return '爬取失败';
    }
    return 'Agent执行出错（请根据下方给出的错误原因说明排查内部问题）';
  }, [errorMsg]);
  const [newsArticles, setNewsArticles] = useState<NewsArticle[]>(initialNewsArticles || []);
  
  // Report List State
  const [reports, setReports] = useState<ReportFile[]>(initialReports || []);
  const [loadingReports, setLoadingReports] = useState(
    Boolean(initialTaskId) &&
      (mode === 'enterprise_report' || mode === 'news_report_download') &&
      !((initialReports || []).length > 0)
  );
  const [reportSearch, setReportSearch] = useState("");
  const [currentPage, setCurrentPage] = useState(1);
  const PAGE_SIZE = 20;
  
  // PDF 下载状态
  const [downloadedCount, setDownloadedCount] = useState(0);
  const [filesNotEnough, setFilesNotEnough] = useState(false);
  const [pdfOutputDir, setPdfOutputDir] = useState<string>("");
  
  // 批量选择状态
  const [selectedReportIds, setSelectedReportIds] = useState<Set<string>>(new Set());
  
  // 停止任务状态
  const [isStopping, setIsStopping] = useState(false);
  
  // 大量数据截断提示
  const [hasMoreReports, setHasMoreReports] = useState(false);
  const [totalReportsCount, setTotalReportsCount] = useState(0);
  
  // 队列信息
  const [queuePosition, setQueuePosition] = useState(0);
  const [queueWaiting, setQueueWaiting] = useState(0);
  const [queueRunning, setQueueRunning] = useState(0);
  const [estimatedWait, setEstimatedWait] = useState(0);

  // 任务评价 / 持久化记忆
  const [pendingFeedback, setPendingFeedback] = useState(false);
  const [autoFindings, setAutoFindings] = useState<AutoFindings | undefined>(undefined);
  const [userVerdict, setUserVerdict] = useState<'correct' | 'wrong' | undefined>(undefined);
  const [feedbackModalOpen, setFeedbackModalOpen] = useState(false);
  // 用户对每个 task_id 已经手动关闭过的状态（防止反复弹出）
  const dismissedFeedbackRef = useRef<Set<string>>(new Set());
  
  const logScrollRef = useRef<HTMLDivElement>(null);
  const rightPanelRef = useRef<HTMLDivElement>(null);
  const pollingRef = useRef<NodeJS.Timeout | null>(null);
  const prevInitialTaskIdRef = useRef(initialTaskId);

  const isEnterprise = mode === 'enterprise_report';
  const isNewsReport = mode === 'news_report_download';
  // 企业报告 和 新闻报告 模式都使用表格视图展示文件列表
  const isReportMode = isEnterprise || isNewsReport;
  
  const title = isEnterprise ? '企业报告下载' : (isNewsReport ? '新闻报告下载' : '新闻舆情爬取');

  useEffect(() => {
    if (prevInitialTaskIdRef.current === initialTaskId) return;
    prevInitialTaskIdRef.current = initialTaskId;

    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }

    setTaskId(initialTaskId);
    setTaskStatus(initialRawStatus || 'pending');
    setLogContent(initialLogLines && initialLogLines.length > 0 ? initialLogLines.join('\n') : '');
    setErrorMsg('');
    setResultFile('');
    setNewsArticles(initialNewsArticles || []);
    setReports(initialReports || []);
    setLoadingReports(Boolean(initialTaskId) && isReportMode && !((initialReports || []).length > 0));
    setReportSearch('');
    setCurrentPage(1);
    setDownloadedCount(0);
    setFilesNotEnough(false);
    setPdfOutputDir('');
    setSelectedReportIds(new Set());
    setIsStopping(false);
    setHasMoreReports(false);
    setTotalReportsCount(0);
    setQueuePosition(0);
    setQueueWaiting(0);
    setQueueRunning(0);
    setEstimatedWait(0);
    setPendingFeedback(false);
    setAutoFindings(undefined);
    setUserVerdict(undefined);
    setFeedbackModalOpen(false);
    setSteps(
      STEPS_TEMPLATE.map((label, index) => ({
        id: index,
        label,
        status: 'pending'
      }))
    );
  }, [initialTaskId, initialRawStatus, initialLogLines, initialNewsArticles, initialReports, isReportMode]);

  // 启动任务（使用 ref 防止 React Strict Mode 导致的重复请求）
  const isStartingRef = useRef(false);
  const startTask = useCallback(async () => {
    // 1. 如果已有 taskId，说明任务已创建，不应再创建
    if (taskId) return;
    
    // 2. 如果正在启动中，也不要重复创建
    if (isStartingRef.current) return;
    isStartingRef.current = true;
    
    try {
      // 准备附件数据（转换为 API 可用的格式）
      const attachmentData = formData.attachments
        .filter(att => att.base64 && att.mimeType)
        .map(att => ({
          filename: att.file.name,
          base64: att.base64!,
          mimeType: att.mimeType!
        }));

      const request: GenerateRequest = {
        url: formData.reportUrl,
        startDate: formData.startDate,
        endDate: formData.endDate,
        outputScriptName: formData.outputScriptName,
        taskObjective: formData.taskObjective,
        extraRequirements: formData.taskObjective,
        siteName: formData.siteName,
        listPageName: formData.listPageName,
        sourceCredibility: formData.sourceCredibility || undefined,
        runMode: formData.runMode,
        crawlMode: formData.crawlMode,
        downloadReport: formData.downloadReport,
        selectedPaths: selectedPaths.length > 0 ? selectedPaths : undefined,
        attachments: attachmentData.length > 0 ? attachmentData : undefined,
        // Hole-2.B: 历史"重新运行"路径会通过 prevTaskId prop 传入原任务 ID。
        // 后端 (runner.py) 会按 domain 二次校验，跨域名时丢弃，所以这里
        // 不需要在前端再做 URL 比对。
        prevTaskId: prevTaskId || undefined,
      };
      
      const response = await fetch(`${API_BASE_URL}/api/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request)
      });
      
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || `HTTP ${response.status}`);
      }
      
      const data = await response.json();
      setTaskId(data.taskId);
      onTaskIdChange(data.taskId);
      
    } catch (err: any) {
      setErrorMsg(err.message || '启动任务失败');
      setTaskStatus('failed');
    }
  }, [taskId, formData, selectedPaths, onTaskIdChange, prevTaskId]);

  // 轮询任务状态
  const pollStatus = useCallback(async () => {
    if (!taskId) return;
    
    try {
      const response = await fetch(`${API_BASE_URL}/api/status/${taskId}`);
      
      if (!response.ok) {
        if (response.status === 404) {
          setErrorMsg('任务不存在');
          setTaskStatus('failed');
          return;
        }
        throw new Error(`HTTP ${response.status}`);
      }
      
      const data: TaskStatusResponse = await response.json();
      
      setTaskStatus(data.status);
      setLogContent(data.logs.join('\n'));
      
      // 更新队列信息
      if (data.queuePosition !== undefined) setQueuePosition(data.queuePosition);
      if (data.queueWaitingCount !== undefined) setQueueWaiting(data.queueWaitingCount);
      if (data.queueRunningCount !== undefined) setQueueRunning(data.queueRunningCount);
      if (data.estimatedWaitSeconds !== undefined) setEstimatedWait(data.estimatedWaitSeconds);

      // 任务评价 / 持久化记忆字段
      if (data.autoFindings !== undefined) {
        setAutoFindings(data.autoFindings || undefined);
      }
      if (data.userVerdict) {
        setUserVerdict(data.userVerdict as 'correct' | 'wrong');
      }
      if (data.pendingFeedback !== undefined) {
        setPendingFeedback(Boolean(data.pendingFeedback));
      }
      
      if (data.resultFile) {
        setResultFile(data.resultFile);
      }
      
      if (data.error) {
        setErrorMsg(data.error);
      }
      
      // 更新报告列表（企业报告场景 / 新闻报告场景）
      if (data.reports !== undefined) {
        const nextReports = data.reports || [];
        setReports(nextReports);
        setLoadingReports(false);
        
        // 检查是否有截断（后端截断后会返回 totalCount）
        if (data.totalCount && data.totalCount > nextReports.length) {
          setHasMoreReports(true);
          setTotalReportsCount(data.totalCount);
        } else {
          setHasMoreReports(false);
          setTotalReportsCount(nextReports.length);
        }
      } else if (data.status === 'completed' && isReportMode) {
        setLoadingReports(false);
      }
      
      // 更新 PDF 下载状态
      if (data.downloadedCount !== undefined) {
        setDownloadedCount(data.downloadedCount);
      }
      if (data.filesNotEnough !== undefined) {
        setFilesNotEnough(data.filesNotEnough);
      }
      if (data.pdfOutputDir) {
        setPdfOutputDir(data.pdfOutputDir);
      }
      
      // 更新新闻列表（新闻舆情场景）
      if (data.newsArticles && data.newsArticles.length > 0) {
        setNewsArticles(data.newsArticles);
      }
      
      // 更新步骤状态（确保与后端同步）
      setSteps(prev => prev.map((step, idx) => {
        // 排队中：所有步骤保持 pending
        if (data.status === 'queued') {
          return { ...step, status: 'pending' };
        }
        // 如果任务已完成，所有步骤都应该是 completed
        if (data.status === 'completed' && idx < prev.length) {
          // 例外：Stage-2 LLM 复盘只在「任务已完成 + 用户提交反馈」之后才发生，
          // 此时后端会把 stepLabel 改成「🧠 Agent 正在根据反馈做 LLM 深度复盘…」
          // 或「✅ Stage-2 复盘完成 …」之类。这里把后端的 stepLabel 透传到
          // 复盘那一格，让用户看见 Stage-1 → Stage-2 的真实切换；其他格保持
          // STEPS_TEMPLATE 里写死的原文不动。
          if (idx === SUMMARIZE_STEP_INDEX && data.stepLabel) {
            return { ...step, status: 'completed', label: data.stepLabel };
          }
          return { ...step, status: 'completed' };
        }
        // 如果任务失败，当前步骤及之前的应该是 completed，失败步骤标记为 failed
        if (data.status === 'failed') {
          if (idx < data.currentStep) {
            return { ...step, status: 'completed' };
          } else if (idx === data.currentStep) {
            return { ...step, status: 'failed', label: data.stepLabel || step.label };
          } else {
            return { ...step, status: 'pending' };
          }
        }
        // 正常运行中
        if (idx < data.currentStep) {
          return { ...step, status: 'completed' };
        } else if (idx === data.currentStep) {
          return { ...step, status: 'running', label: data.stepLabel || step.label };
        } else {
          return { ...step, status: 'pending' };
        }
      }));
      
      // 如果完成或失败，停止轮询
      if (data.status === 'completed' || data.status === 'failed') {
        if (pollingRef.current) {
          clearInterval(pollingRef.current);
          pollingRef.current = null;
        }
        
        if (data.status === 'completed') {
          setSteps(prev => prev.map((step, idx) => ({
            ...step,
            status: idx === prev.length - 1 ? 'completed' : step.status
          })));
          
          // 新闻模式的内容已通过 newsArticles 更新
          // 不再使用模拟内容
        }
      }
      
    } catch (err: any) {
      console.error('Poll error:', err);
    }
  }, [taskId, isEnterprise, isReportMode]);

  // 组件挂载时启动任务
  useEffect(() => {
    if (disableAutoStart) return;
    startTask();
  }, [disableAutoStart, startTask]);

  useEffect(() => {
    if (!disableAutoStart) return;
    if (taskId) return;
    setLoadingReports(false);
    setErrorMsg('任务ID缺失，无法加载历史执行结果，请返回批量监控后重试。');
  }, [disableAutoStart, taskId]);

  // 有 taskId 后开始轮询
  useEffect(() => {
    if (!taskId) return;
    
    if (isReportMode && reports.length === 0) {
      setLoadingReports(true);
    }
    
    pollStatus();
    // 提高轮询频率到 500ms，确保步骤更新更及时
    pollingRef.current = setInterval(pollStatus, 500);
    
    return () => {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    };
  }, [taskId, pollStatus, isReportMode, reports.length]);

  // Auto-scroll logs
  useEffect(() => {
    if (logScrollRef.current) {
      logScrollRef.current.scrollTop = logScrollRef.current.scrollHeight;
    }
  }, [logContent]);

  // 自动弹出 "任务评价" Modal —
  //   触发条件：任务终态(completed|failed) + 后端标记 pendingFeedback + 用户尚未提交过 + 用户未在本次会话主动关闭
  useEffect(() => {
    if (!taskId) return;
    const isTerminal = taskStatus === 'completed' || taskStatus === 'failed';
    if (!isTerminal) return;
    if (userVerdict) return; // 已评价过
    if (!pendingFeedback) return; // 后端未生成草稿（如非 agent 模式）
    if (dismissedFeedbackRef.current.has(taskId)) return;
    setFeedbackModalOpen(true);
  }, [taskId, taskStatus, pendingFeedback, userVerdict]);

  const handleFeedbackClose = useCallback(() => {
    setFeedbackModalOpen(false);
    if (taskId) dismissedFeedbackRef.current.add(taskId);
  }, [taskId]);

  const handleFeedbackSubmitted = useCallback(
    (verdict: 'correct' | 'wrong') => {
      setUserVerdict(verdict);
      setPendingFeedback(false);

      // 反馈触发的是 Stage-2 LLM 复盘，commit_episode 在后端线程池里跑期间
      // 会把 [SUMMARY/Stage-2] / [COMMIT] / [MEMORY] 日志 push 到
      // tasks[task_id]["logs"]。但前端在任务 status=completed 后已经停掉了
      // 轮询（见上面 pollStatus 里的 clearInterval 分支），所以这里需要
      // 重新拉一次状态，把这些刚 append 的日志补回来，让用户能看到
      // 「Agent 正在根据反馈做深度复盘」的全过程。
      pollStatus();
      // 兜底：再开一个短窗口轮询，覆盖「响应已经回来但 SSE/落盘日志稍微
      // 滞后」的窗口（一般不会发生，但保险起见）。
      const burstTimer = setInterval(pollStatus, 1000);
      setTimeout(() => clearInterval(burstTimer), 8000);
    },
    [pollStatus]
  );

  // 重新运行（带反馈）：调用 /api/rerun/{taskId}，使用同样表单参数 + prevTaskId
  const handleRerunWithFeedback = useCallback(
    async (verdict: 'correct' | 'wrong', suggestion: string) => {
      if (!taskId) return;
      try {
        const resp = await fetch(`${API_BASE_URL}/api/rerun/${taskId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          // verdict 已通过 /feedback 提交并 commit 过；这里再传一次让后端
          // 走 commit-or-skip 的幂等逻辑（draft 已删除时会自然跳过）。
          body: JSON.stringify({ verdict, suggestion, skipFeedback: true }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data?.taskId) {
          throw new Error(data?.detail || `HTTP ${resp.status}`);
        }
        // 切换到新任务：重置 UI 状态，让父组件持有新 task_id
        setLogContent('');
        setErrorMsg('');
        setResultFile('');
        setNewsArticles([]);
        setReports([]);
        setSelectedReportIds(new Set());
        setUserVerdict(undefined);
        setAutoFindings(undefined);
        setPendingFeedback(false);
        setSteps(
          STEPS_TEMPLATE.map((label, index) => ({
            id: index,
            label,
            status: 'pending',
          }))
        );
        setTaskId(data.taskId);
        onTaskIdChange(data.taskId);
      } catch (err: any) {
        setErrorMsg(`重新运行失败：${err?.message || err}`);
      }
    },
    [taskId, onTaskIdChange]
  );

  // Filter and Pagination Logic
  const filteredReports = useMemo(() => {
    return reports.filter(r => r.name.toLowerCase().includes(reportSearch.toLowerCase()));
  }, [reports, reportSearch]);

  const paginatedReports = useMemo(() => {
    const start = (currentPage - 1) * PAGE_SIZE;
    return filteredReports.slice(start, start + PAGE_SIZE);
  }, [filteredReports, currentPage]);

  const totalPages = Math.ceil(filteredReports.length / PAGE_SIZE);

  // 检查是否有报告包含 category 字段（多页爬取时显示来源板块列）
  const hasCategory = useMemo(() => {
    return reports.some(r => r.category && r.category.trim() !== '');
  }, [reports]);

  // 下载脚本
  const handleDownloadScript = () => {
    if (!resultFile) return;
    const filename = resultFile.split(/[/\\]/).pop() || 'crawler.py';
    window.open(`${API_BASE_URL}/api/download/${filename}`, '_blank');
  };

  // 强制下载文件（使用 fetch + blob 方式，避免浏览器直接打开 PDF）
  const forceDownloadFile = async (url: string, filename: string) => {
    try {
      // 尝试通过 fetch 获取文件
      const response = await fetch(url, {
        mode: 'cors',
        credentials: 'omit'
      });
      
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      
      const blob = await response.blob();
      const blobUrl = URL.createObjectURL(blob);
      
      // 创建隐藏的 a 标签触发下载
      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = filename;
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      
      // 清理
      setTimeout(() => {
        document.body.removeChild(a);
        URL.revokeObjectURL(blobUrl);
      }, 100);
      
    } catch (err) {
      // 如果 fetch 失败（跨域等问题），回退到使用 download 属性的方式
      console.warn('Fetch 下载失败，使用备用方式:', err);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }
  };

  // 下载/查看单个报告
  const handleDownloadReport = (report: ReportFile) => {
    if (report.isLocal && report.localPath) {
      // 本地文件：通过 API 打开查看
      // localPath 格式: "子文件夹名/文件名.pdf"
      // 对路径中的每个部分分别编码，保持斜杠不变
      const encodedPath = report.localPath
        .split('/')
        .map(part => encodeURIComponent(part))
        .join('/');
      const viewUrl = `${API_BASE_URL}/api/pdf/${encodedPath}`;
      window.open(viewUrl, '_blank');
    } else if (report.downloadUrl && report.downloadUrl !== '#') {
      // 远程文件：下载
      const filename = `${report.name}.${report.fileType || 'pdf'}`;
      forceDownloadFile(report.downloadUrl, filename);
    }
  };

  const handleOpenNewsAttachment = (attachment: NewsAttachment) => {
    if (attachment.isLocal && attachment.localPath) {
      const encodedPath = attachment.localPath
        .split('/')
        .map(part => encodeURIComponent(part))
        .join('/');
      window.open(`${API_BASE_URL}/api/pdf/${encodedPath}`, '_blank', 'noopener,noreferrer');
      return;
    }
    if (attachment.url) {
      window.open(attachment.url, '_blank', 'noopener,noreferrer');
    }
  };

  // 批量下载（下载选中的报告）
  const handleBatchDownload = async () => {
    const reportsToDownload = selectedReportIds.size > 0 
      ? filteredReports.filter(r => selectedReportIds.has(r.id))
      : filteredReports;
    
    // 逐个下载，每个之间间隔 500ms 避免浏览器阻止
    for (let i = 0; i < reportsToDownload.length; i++) {
      const report = reportsToDownload[i];
      if (report.downloadUrl && report.downloadUrl !== '#') {
        const filename = `${report.name}.${report.fileType || 'pdf'}`;
        forceDownloadFile(report.downloadUrl, filename);
        
        // 等待一小段时间再下载下一个
        if (i < reportsToDownload.length - 1) {
          await new Promise(resolve => setTimeout(resolve, 500));
        }
      }
    }
  };
  
  // 选择/取消选择单个报告
  const handleToggleSelect = (reportId: string) => {
    setSelectedReportIds(prev => {
      const newSet = new Set(prev);
      if (newSet.has(reportId)) {
        newSet.delete(reportId);
      } else {
        newSet.add(reportId);
      }
      return newSet;
    });
  };
  
  // 全选/取消全选（当前过滤后的报告）
  const handleToggleSelectAll = () => {
    const allFilteredIds = filteredReports.map(r => r.id);
    const allSelected = allFilteredIds.every(id => selectedReportIds.has(id));
    
    if (allSelected) {
      // 取消全选
      setSelectedReportIds(prev => {
        const newSet = new Set(prev);
        allFilteredIds.forEach(id => newSet.delete(id));
        return newSet;
      });
    } else {
      // 全选
      setSelectedReportIds(prev => {
        const newSet = new Set(prev);
        allFilteredIds.forEach(id => newSet.add(id));
        return newSet;
      });
    }
  };
  
  // 判断是否全选
  const isAllSelected = useMemo(() => {
    if (filteredReports.length === 0) return false;
    return filteredReports.every(r => selectedReportIds.has(r.id));
  }, [filteredReports, selectedReportIds]);
  
  // 判断是否部分选择
  const isPartiallySelected = useMemo(() => {
    if (filteredReports.length === 0) return false;
    const selectedCount = filteredReports.filter(r => selectedReportIds.has(r.id)).length;
    return selectedCount > 0 && selectedCount < filteredReports.length;
  }, [filteredReports, selectedReportIds]);
  
  // 停止任务
  const handleStopTask = async () => {
    if (!taskId || isStopping) return;
    
    const confirmed = window.confirm('确定要停止当前任务吗？这将终止所有正在运行的相关程序。');
    if (!confirmed) return;
    
    setIsStopping(true);
    
    try {
      const response = await fetch(`${API_BASE_URL}/api/stop/${taskId}`, {
        method: 'POST'
      });
      
      if (response.ok) {
        setTaskStatus('failed');
        if (pollingRef.current) {
          clearInterval(pollingRef.current);
          pollingRef.current = null;
        }
      }
    } catch (err) {
      console.error('停止任务失败:', err);
    } finally {
      setIsStopping(false);
    }
  };

  return (
    <div className="flex flex-col h-full w-full bg-white">
      {/* Header */}
      <div className="flex items-center gap-4 px-6 py-4 border-b border-gray-100 bg-white shrink-0">
        <button 
          onClick={onBack}
          className="p-2 hover:bg-gray-100 rounded-full text-gray-500 transition-colors"
        >
          <ArrowLeft size={20} />
        </button>
        <div className="flex items-center gap-3">
          <div className={`p-2 rounded-lg ${isEnterprise ? 'bg-blue-50 text-blue-600' : 'bg-orange-50 text-orange-600'}`}>
            {isEnterprise ? <FileText size={20} /> : <ExternalLink size={20} />}
          </div>
          <div>
            <h1 className="text-lg font-bold text-gray-800">{title} - 执行监控</h1>
            {taskId && <p className="text-xs text-gray-400">任务 ID: {taskId}</p>}
          </div>
        </div>
        
        {/* 状态指示 */}
        <div className="ml-auto flex items-center gap-2">
          {taskStatus === 'queued' && (
            <span className="flex items-center gap-1 px-3 py-1 bg-amber-50 text-amber-600 rounded-full text-sm">
              <Clock size={14} />
              排队中（第 {queuePosition} 位）
            </span>
          )}
          {taskStatus === 'running' && (
            <span className="flex items-center gap-1 px-3 py-1 bg-blue-50 text-blue-600 rounded-full text-sm">
              <Loader2 size={14} className="animate-spin" />
              运行中
            </span>
          )}
          {taskStatus === 'completed' && (
            <span className="flex items-center gap-1 px-3 py-1 bg-emerald-50 text-emerald-600 rounded-full text-sm">
              <CheckCircle2 size={14} />
              已完成
            </span>
          )}
          {taskStatus === 'failed' && (
            <span className="flex items-center gap-1 px-3 py-1 bg-red-50 text-red-600 rounded-full text-sm">
              <XCircle size={14} />
              失败
            </span>
          )}
          
          {resultFile && (
            <button 
              onClick={handleDownloadScript}
              className="flex items-center gap-1 px-3 py-1 bg-gray-100 text-gray-600 rounded-full text-sm hover:bg-gray-200"
            >
              <Download size={14} />
              下载脚本
            </button>
          )}

          {/* 任务评价状态徽章 */}
          {(taskStatus === 'completed' || taskStatus === 'failed') && (
            userVerdict ? (
              <span
                className={`flex items-center gap-1 px-3 py-1 rounded-full text-sm ${
                  userVerdict === 'correct'
                    ? 'bg-emerald-50 text-emerald-600'
                    : 'bg-red-50 text-red-600'
                }`}
                title="本次任务已评价"
              >
                {userVerdict === 'correct' ? (
                  <CheckCircle2 size={14} />
                ) : (
                  <XCircle size={14} />
                )}
                已{userVerdict === 'correct' ? '通过' : '反馈'}
              </span>
            ) : (
              pendingFeedback && (
                <button
                  onClick={() => setFeedbackModalOpen(true)}
                  className="flex items-center gap-1 px-3 py-1 bg-amber-50 text-amber-600 rounded-full text-sm hover:bg-amber-100"
                  title="点击填写任务评价（写入持久化记忆）"
                >
                  <AlertCircle size={14} />
                  待评价
                </button>
              )
            )
          )}
        </div>
      </div>

      {/* Main Content */}
      <div className="flex-1 flex flex-col md:flex-row overflow-hidden">
        
        {/* Left Sidebar: Progress Steps */}
        <div className="w-full md:w-80 bg-slate-50 border-b md:border-b-0 md:border-r border-gray-100 flex flex-col overflow-hidden shrink-0">
          <div className="p-4 border-b border-gray-100 bg-slate-50 font-medium text-gray-700 flex items-center gap-2">
            <PlayCircle size={18} className="text-emerald-500" />
            执行进度
          </div>
          {/* 排队等待横幅 */}
          {taskStatus === 'queued' && queuePosition > 0 && (
            <div className="mx-4 mt-4 px-4 py-4 bg-amber-50 border border-amber-200 rounded-xl">
              <div className="flex items-center gap-3 mb-2">
                <div className="p-2 bg-amber-100 rounded-lg">
                  <Users size={18} className="text-amber-600" />
                </div>
                <div>
                  <p className="font-bold text-amber-800 text-sm">
                    正在排队等待 - 第 {queuePosition} 位
                  </p>
                  <p className="text-xs text-amber-600 mt-0.5">
                    {estimatedWait > 0 
                      ? `预计等待约 ${Math.ceil(estimatedWait / 60)} 分钟`
                      : '即将开始...'
                    }
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-4 text-xs text-amber-700 mt-2 pt-2 border-t border-amber-200">
                <span>等待中: {queueWaiting}</span>
                <span>运行中: {queueRunning}</span>
              </div>
              {/* 排队进度条 */}
              <div className="mt-2 h-1.5 bg-amber-100 rounded-full overflow-hidden">
                <div 
                  className="h-full bg-amber-400 rounded-full transition-all duration-1000"
                  style={{ width: `${Math.max(5, 100 - queuePosition * 20)}%` }}
                />
              </div>
            </div>
          )}

          <div className="flex-1 overflow-y-auto p-4 space-y-4">
            {steps.map((step) => (
              <div key={step.id} className="flex items-start gap-3">
                <div className="mt-0.5">
                  {step.status === 'completed' && <CheckCircle2 size={18} className="text-emerald-500" />}
                  {step.status === 'running' && <Loader2 size={18} className="text-blue-500 animate-spin" />}
                  {step.status === 'pending' && <Circle size={18} className="text-gray-300" />}
                  {step.status === 'failed' && <XCircle size={18} className="text-red-500" />}
                </div>
                <div className={`text-sm ${
                  step.status === 'running' ? 'text-blue-600 font-medium' :
                  step.status === 'completed' ? 'text-gray-600' :
                  step.status === 'failed' ? 'text-red-600' :
                  'text-gray-400'
                }`}>
                  {step.label}
                </div>
              </div>
            ))}
          </div>
          
          {/* 任务信息 */}
          <div className="p-4 border-t border-gray-200 bg-white text-xs text-gray-500 space-y-1">
            <div><span className="text-gray-400">URL:</span> {formData.reportUrl?.slice(0, 40)}...</div>
            <div><span className="text-gray-400">日期:</span> {formData.startDate} ~ {formData.endDate}</div>
            <div><span className="text-gray-400">输出:</span> {formData.outputScriptName}</div>
          </div>
        </div>

        {/* Right Content */}
        <div 
          ref={rightPanelRef}
          className="flex-1 flex flex-col min-w-0 bg-slate-50 h-full overflow-y-auto"
        >
          
          {/* Console Log */}
          <div className="bg-white border-b border-gray-200 shrink-0">
             <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between bg-gray-50">
               <div className="flex items-center gap-2">
                 <Terminal size={20} className="text-gray-600" />
                 <span className="text-lg font-bold text-gray-800">运行日志</span>
               </div>
               {/* 停止运行按钮 */}
               {(taskStatus === 'running' || taskStatus === 'pending' || taskStatus === 'queued') && (
                 <button
                   onClick={handleStopTask}
                   disabled={isStopping}
                   className={`
                     flex items-center gap-2 px-4 py-2 rounded-lg font-medium text-sm
                     transition-all duration-200 shadow-sm
                     ${isStopping 
                       ? 'bg-red-100 text-red-400 cursor-not-allowed' 
                       : 'bg-red-600 text-white hover:bg-red-700 hover:shadow-md active:scale-95'
                     }
                   `}
                   title="停止运行"
                 >
                   <StopCircle size={16} className={isStopping ? 'animate-pulse' : ''} />
                   {isStopping ? '正在停止...' : '停止运行'}
                 </button>
               )}
             </div>
             <div 
               ref={logScrollRef}
               className="h-64 overflow-y-auto p-4 bg-[#1e1e1e] text-green-400 font-mono text-sm leading-relaxed whitespace-pre-wrap"
             >
               {logContent || <span className="text-gray-500 opacity-50">等待任务启动...</span>}
               {taskStatus === 'running' && (
                 <span className="inline-block w-2 h-4 ml-1 bg-green-400 animate-pulse align-middle" />
               )}
             </div>
          </div>

          {/* Error Message */}
          {errorMsg && (
            <div className="mx-4 mt-4 p-4 bg-red-50 border border-red-200 rounded-lg flex items-start gap-3">
              <AlertCircle size={20} className="text-red-500 shrink-0 mt-0.5" />
              <div>
                <p className="font-medium text-red-700">{errorTitle}</p>
                <p className="text-sm text-red-600 mt-1">错误原因：{explainedError.reason}</p>
                {explainedError.explanation && (
                  <p className="text-sm text-red-600 mt-1">原因说明：{explainedError.explanation}</p>
                )}
              </div>
            </div>
          )}

          {/* Result Output */}
          <div className="flex-1 flex flex-col min-h-0">
            <div className="px-5 py-4 bg-white border-b border-gray-100 flex items-center gap-2 border-t border-gray-200">
              <FileText size={20} className="text-gray-600" />
              <span className="text-lg font-bold text-gray-800">执行结果</span>
            </div>
            
            <div className="flex-1 relative">
              {isReportMode ? (
                // Table View for Report Files
                <div className="bg-white flex flex-col h-full min-h-[500px]">
                  
                  {/* Toolbar */}
                  <div className="px-6 py-4 flex flex-col sm:flex-row gap-4 justify-between items-center border-b border-gray-100">
                    <div className="flex items-center gap-2 flex-wrap">
                      <div className={`p-2 rounded-lg ${isEnterprise ? 'bg-emerald-50 text-emerald-600' : 'bg-blue-50 text-blue-600'}`}>
                        <FileText size={18} />
                      </div>
                      <h2 className="font-bold text-gray-800">{isEnterprise ? '评级报告列表' : '新闻报告列表'}</h2>
                      <span className="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded-full">
                        {filteredReports.length} 条
                      </span>
                      {downloadedCount > 0 && (
                        <span className="text-xs bg-blue-100 text-blue-600 px-2 py-0.5 rounded-full font-medium">
                          已下载 {downloadedCount} 个文件
                        </span>
                      )}
                      {selectedReportIds.size > 0 && (
                        <span className="text-xs bg-emerald-100 text-emerald-600 px-2 py-0.5 rounded-full font-medium">
                          已选 {filteredReports.filter(r => selectedReportIds.has(r.id)).length} 条
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-3 w-full sm:w-auto">
                      <div className="relative flex-1 sm:w-64">
                        <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" size={16} />
                        <input 
                           type="text" 
                           placeholder="搜索报告名称..." 
                           value={reportSearch}
                           onChange={(e) => { setReportSearch(e.target.value); setCurrentPage(1); }}
                           className="w-full pl-9 pr-4 py-2 bg-gray-50 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-emerald-500 transition-colors"
                        />
                      </div>
                      <button 
                        onClick={handleBatchDownload}
                        disabled={filteredReports.length === 0}
                        className="flex items-center gap-2 px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 transition-colors text-sm font-medium shadow-sm hover:shadow-md disabled:opacity-50"
                      >
                        <DownloadCloud size={16} />
                        <span className="hidden sm:inline">
                          {selectedReportIds.size > 0 
                            ? `下载选中 (${filteredReports.filter(r => selectedReportIds.has(r.id)).length})` 
                            : '批量下载全部'
                          }
                        </span>
                      </button>
                    </div>
                  </div>
                  
                  {/* 截断提示 */}
                  {hasMoreReports && (
                    <div className="mx-6 mt-3 px-4 py-3 bg-amber-50 border border-amber-200 rounded-lg flex items-center gap-3">
                      <AlertCircle size={18} className="text-amber-600 shrink-0" />
                      <p className="text-sm text-amber-700 font-medium">
                        结果过多（共 {totalReportsCount} 条），为优化性能，当前仅展示前 100 条。完整数据请下载结果文件查看。
                      </p>
                    </div>
                  )}
                  
                  {/* 文件不足提示 */}
                  {filesNotEnough && taskStatus === 'completed' && (
                    <div className="mx-6 mt-2 px-4 py-3 bg-amber-50 border border-amber-200 rounded-lg flex items-center gap-3">
                      <Info size={18} className="text-amber-600 shrink-0" />
                      <p className="text-sm text-amber-700">
                        该日期范围内文件不足5份，现仅展示满足条件的 {reports.length} 个文件。
                      </p>
                    </div>
                  )}

                  {/* Table Content */}
                  <div className="flex-1 overflow-x-auto">
                    {loadingReports ? (
                      // Skeleton Loading
                      <div className="p-6 space-y-4">
                        {[1, 2, 3, 4, 5].map((i) => (
                          <div key={i} className="flex items-center gap-4 animate-pulse">
                            <div className="w-8 h-8 bg-gray-100 rounded-md"></div>
                            <div className="flex-1 h-8 bg-gray-100 rounded-md"></div>
                            <div className="w-32 h-8 bg-gray-100 rounded-md"></div>
                          </div>
                        ))}
                      </div>
                    ) : filteredReports.length === 0 && reports.length > 0 ? (
                      // Empty Search
                      <div className="flex flex-col items-center justify-center h-64 text-gray-400">
                        <div className="w-12 h-12 bg-gray-50 rounded-full flex items-center justify-center mb-3">
                           <Search size={24} className="opacity-30" />
                        </div>
                        <p>未找到相关报告</p>
                      </div>
                    ) : reports.length === 0 ? (
                      // Empty Initial
                      <div className="flex flex-col items-center justify-center h-64 text-gray-400">
                        <div className="w-12 h-12 bg-gray-50 rounded-full flex items-center justify-center mb-3">
                           <Loader2 size={24} className="animate-spin text-emerald-500 opacity-50" />
                        </div>
                        <p>等待数据生成...</p>
                        <p className="text-xs text-gray-400 mt-1">爬虫脚本运行后将显示报告列表</p>
                      </div>
                    ) : (
                      // Real Data Table
                      <table className="w-full text-left border-collapse">
                        <thead>
                          <tr className="bg-gray-50 text-gray-500 text-xs font-semibold uppercase tracking-wider border-b border-gray-100">
                            <th className="px-4 py-4 w-12">
                              <button
                                onClick={handleToggleSelectAll}
                                className="p-1 hover:bg-gray-200 rounded transition-colors"
                                title={isAllSelected ? "取消全选" : "全选"}
                              >
                                {isAllSelected ? (
                                  <CheckSquare size={18} className="text-emerald-600" />
                                ) : isPartiallySelected ? (
                                  <div className="relative">
                                    <Square size={18} className="text-gray-400" />
                                    <div className="absolute inset-0 flex items-center justify-center">
                                      <div className="w-2 h-0.5 bg-emerald-500 rounded"></div>
                                    </div>
                                  </div>
                                ) : (
                                  <Square size={18} className="text-gray-400" />
                                )}
                              </button>
                            </th>
                            <th className="px-4 py-4 w-16">序号</th>
                            <th className="px-6 py-4">报告名称</th>
                            {hasCategory && <th className="px-6 py-4 w-48">来源板块</th>}
                            <th className="px-6 py-4 w-40">发布日期</th>
                            <th className="px-6 py-4 w-32 text-center">操作</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-50">
                          {paginatedReports.map((report, index) => {
                            const isSelected = selectedReportIds.has(report.id);
                            return (
                              <tr 
                                key={report.id} 
                                className={`transition-colors group cursor-pointer ${
                                  isSelected 
                                    ? 'bg-emerald-50/60 hover:bg-emerald-50' 
                                    : 'hover:bg-emerald-50/40'
                                }`}
                                onClick={() => handleToggleSelect(report.id)}
                              >
                                <td className="px-4 py-4" onClick={e => e.stopPropagation()}>
                                  <button
                                    onClick={() => handleToggleSelect(report.id)}
                                    className="p-1 hover:bg-gray-200 rounded transition-colors"
                                  >
                                    {isSelected ? (
                                      <CheckSquare size={18} className="text-emerald-600" />
                                    ) : (
                                      <Square size={18} className="text-gray-400 group-hover:text-gray-500" />
                                    )}
                                  </button>
                                </td>
                                <td className="px-4 py-4 text-gray-400 text-sm font-mono">
                                  {(currentPage - 1) * PAGE_SIZE + index + 1}
                                </td>
                                <td className="px-6 py-4">
                                  <div className="flex items-center gap-3">
                                    <div className={`p-2 rounded-lg transition-colors ${
                                      isSelected 
                                        ? 'bg-emerald-100 text-emerald-600' 
                                        : 'bg-red-50 text-red-500 group-hover:bg-red-100'
                                    }`}>
                                      <File size={16} />
                                    </div>
                                    <span className={`font-medium transition-colors ${
                                      isSelected 
                                        ? 'text-emerald-700' 
                                        : 'text-gray-700 group-hover:text-emerald-700'
                                    }`}>
                                      {report.name}
                                    </span>
                                  </div>
                                </td>
                                {hasCategory && (
                                  <td className="px-6 py-4">
                                    {report.category ? (
                                      <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium bg-blue-50 text-blue-700 border border-blue-100">
                                        {report.category.split('/').pop()}
                                      </span>
                                    ) : (
                                      <span className="text-gray-400 text-sm">-</span>
                                    )}
                                  </td>
                                )}
                                <td className="px-6 py-4 text-gray-500 text-sm">
                                  {report.date}
                                </td>
                                <td className="px-6 py-4 text-center" onClick={e => e.stopPropagation()}>
                                  <button 
                                    onClick={() => handleDownloadReport(report)}
                                    className={`p-2 rounded-lg transition-colors ${
                                      report.isLocal 
                                        ? 'text-blue-600 hover:bg-blue-100' 
                                        : 'text-emerald-600 hover:bg-emerald-100'
                                    }`}
                                    title={report.isLocal ? "查看文件" : "下载 PDF"}
                                  >
                                    {report.isLocal ? <Eye size={18} /> : <Download size={18} />}
                                  </button>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    )}
                  </div>

                  {/* Pagination Footer */}
                  {reports.length > 0 && (
                    <div className="px-6 py-4 border-t border-gray-100 flex items-center justify-between bg-white">
                       <span className="text-sm text-gray-500">
                         显示 {(currentPage - 1) * PAGE_SIZE + 1} 到 {Math.min(currentPage * PAGE_SIZE, filteredReports.length)} 条，共 {filteredReports.length} 条
                       </span>
                       
                       <div className="flex items-center gap-2">
                         <button 
                            onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
                            disabled={currentPage === 1}
                            className="p-2 rounded-lg border border-gray-200 text-gray-500 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                         >
                           <ChevronLeft size={16} />
                         </button>
                         <span className="text-sm font-medium text-gray-700 px-2">
                            {currentPage} / {totalPages || 1}
                         </span>
                         <button 
                            onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
                            disabled={currentPage === totalPages || totalPages === 0}
                            className="p-2 rounded-lg border border-gray-200 text-gray-500 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                         >
                           <ChevronRight size={16} />
                         </button>
                       </div>
                    </div>
                  )}
                </div>
              ) : (
                // News Sentiment View - 新闻舆情列表
                <div className="bg-white flex flex-col h-full min-h-[500px]">
                  
                  {/* Toolbar */}
                  <div className="px-6 py-4 flex flex-col sm:flex-row gap-4 justify-between items-center border-b border-gray-100">
                    <div className="flex items-center gap-2">
                      <div className="bg-orange-50 text-orange-600 p-2 rounded-lg">
                        <ExternalLink size={18} />
                      </div>
                      <h2 className="font-bold text-gray-800">舆情新闻列表</h2>
                      <span className="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded-full">
                        JSON 视图
                      </span>
                    </div>
                  </div>

                  {/* News Content */}
                  <div className="flex-1 overflow-y-auto">
                    {newsArticles.length > 0 ? (
                      <div className="p-6 space-y-6">
                        {newsArticles.map((article, index) => (
                          <div key={index} className="bg-white border border-gray-100 rounded-lg p-5 shadow-sm hover:shadow-md transition-shadow">
                            <h3 className="text-lg font-bold text-gray-800 mb-2 flex items-start gap-2">
                              <span className="text-orange-500 font-mono mt-0.5">{index + 1}.</span>
                              {article.title}
                            </h3>
                            
                            <div className="flex flex-wrap gap-3 text-xs text-gray-500 mb-4 bg-gray-50 p-2 rounded-lg">
                              <span className="flex items-center gap-1">
                                <span className="font-medium text-gray-400">日期:</span> 
                                {article.date}
                              </span>
                              <span className="w-px h-3 bg-gray-300 self-center"></span>
                              <span className="flex items-center gap-1">
                                <span className="font-medium text-gray-400">来源:</span> 
                                {article.source}
                              </span>
                              {article.author && (
                                <>
                                  <span className="w-px h-3 bg-gray-300 self-center"></span>
                                  <span className="flex items-center gap-1">
                                    <span className="font-medium text-gray-400">作者:</span> 
                                    {article.author}
                                  </span>
                                </>
                              )}
                              {/* 来源板块标识（多页爬取时显示） */}
                              {article.category && (
                                <>
                                  <span className="w-px h-3 bg-gray-300 self-center"></span>
                                  <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700 border border-blue-100">
                                    {article.category.split('/').pop()}
                                  </span>
                                </>
                              )}
                              {article.sourceUrl && (
                                <>
                                  <span className="w-px h-3 bg-gray-300 self-center"></span>
                                  <a 
                                    href={article.sourceUrl} 
                                    target="_blank" 
                                    rel="noreferrer" 
                                    className="text-blue-500 hover:text-blue-600 hover:underline flex items-center gap-1 ml-auto"
                                  >
                                    原文链接 <ExternalLink size={10} />
                                  </a>
                                </>
                              )}
                            </div>
                            
                            {article.summary && (
                              <div className="bg-orange-50/50 p-3 rounded-md mb-4 text-sm text-gray-700 border-l-4 border-orange-200">
                                <span className="font-bold text-orange-400 text-xs uppercase tracking-wider block mb-1">摘要</span>
                                {article.summary}
                              </div>
                            )}
                            
                            {/* 正文内容 */}
                            {article.content && (
                              <div className="mt-4 pt-4 border-t border-gray-100">
                                <div className="flex items-center justify-between mb-2">
                                  <h4 className="text-sm font-bold text-gray-700">正文内容</h4>
                                  <span className="text-xs text-gray-400">HTML/Markdown 预览</span>
                                </div>
                                <div 
                                  className="prose prose-sm max-w-none text-gray-600 overflow-hidden bg-gray-50/30 p-4 rounded-lg border border-gray-100"
                                  style={{ maxHeight: '300px', overflowY: 'auto' }}
                                  dangerouslySetInnerHTML={{ __html: article.content.replace(/\n/g, '<br/>') }} 
                                />
                              </div>
                            )}

                            {article.attachments && article.attachments.length > 0 && (
                              <div className="mt-4 pt-4 border-t border-gray-100">
                                <div className="flex items-center gap-2 mb-2">
                                  <FileText size={15} className="text-rose-500" />
                                  <h4 className="text-sm font-bold text-gray-700">
                                    附件 ({article.attachments.length})
                                  </h4>
                                </div>
                                <div className="divide-y divide-gray-100 border-y border-gray-100">
                                  {article.attachments.map((attachment, attachmentIndex) => (
                                    <div
                                      key={attachment.id || `${attachment.url}-${attachmentIndex}`}
                                      className="flex items-center gap-3 py-2.5"
                                    >
                                      <File size={16} className="text-gray-400 shrink-0" />
                                      <div className="min-w-0 flex-1">
                                        <div className="text-sm text-gray-700 truncate" title={attachment.name}>
                                          {attachment.name || `附件 ${attachmentIndex + 1}`}
                                        </div>
                                        <div className="text-xs text-gray-400 uppercase">
                                          {attachment.fileType || 'file'}{attachment.isLocal ? ' · 已下载' : ' · 远程文件'}
                                        </div>
                                      </div>
                                      <button
                                        type="button"
                                        onClick={() => handleOpenNewsAttachment(attachment)}
                                        className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium text-rose-600 hover:text-rose-700 hover:bg-rose-50 border border-rose-200 rounded-md transition-colors shrink-0"
                                        title={attachment.isLocal ? '查看已下载文件' : '打开远程文件'}
                                      >
                                        {attachment.isLocal ? <Eye size={14} /> : <ExternalLink size={14} />}
                                        查看
                                      </button>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="h-[400px] flex flex-col items-center justify-center text-gray-400">
                        <div className="w-12 h-12 bg-gray-50 rounded-full flex items-center justify-center mb-3">
                          <Loader2 size={24} className="animate-spin text-orange-500 opacity-50" />
                        </div>
                        <p>等待抓取新闻数据...</p>
                        <p className="text-xs text-gray-400 mt-1">爬虫脚本运行后将显示新闻列表（JSON 渲染）</p>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* 任务评价 Modal — 任务结束后必填，提交后才能解锁 "已评价" 徽章 */}
      {taskId && (
        <TaskFeedbackModal
          taskId={taskId}
          open={feedbackModalOpen}
          initialAutoFindings={autoFindings}
          initialUrl={formData.reportUrl}
          onClose={handleFeedbackClose}
          onFeedbackSubmitted={handleFeedbackSubmitted}
          onRerun={handleRerunWithFeedback}
        />
      )}
    </div>
  );
};

export default ExecutionView;
