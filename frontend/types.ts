import React from 'react';

// 附件类型（支持 base64 编码用于传给 API）
export interface Attachment {
  file: File;
  base64?: string;  // 图片的 base64 编码
  mimeType?: string;
}

export interface CrawlerFormData {
  startDate: string;
  endDate: string;
  taskObjective: string;
  extraRequirements?: string; // deprecated: kept for backward compatibility
  siteName: string;
  listPageName: string;
  sourceCredibility: string; // 信息源可信度（T1/T2/T3）
  reportUrl: string;
  outputScriptName: string;
  runMode: string;
  crawlMode: string; // deprecated: 保留字段兼容性，新架构统一使用 agent 模式
  downloadReport: string;
  attachments: Attachment[];
}

// ============ Batch Types ============
export type BatchStatus = 'pending' | 'running' | 'success' | 'failed';

export interface BatchJob extends CrawlerFormData {
  id: string;
  status: BatchStatus;
  logs: string[];
  taskId?: string;
  // Hole-2.B: 当用户在批量页面里点"重新运行"，新克隆出的 job 会带上原 job 的
  // taskId 作为 prevTaskId，runSingleJob 把它一并提交给 /api/generate，
  // 后端再做 domain 校验（Hole-2.A）。仅在用户显式重跑时设置；新增 job 不带。
  prevTaskId?: string;
  resultFile?: string;
  error?: string;
  selectedPaths?: string[];
  rawStatus?: 'pending' | 'queued' | 'running' | 'completed' | 'failed';
  reports?: ReportFile[];
  newsArticles?: NewsArticle[];
  downloadedCount?: number;
  filesNotEnough?: boolean;
  pdfOutputDir?: string;
}

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label: string;
  icon?: React.ReactNode;
  fullWidth?: boolean;
}

export type StepStatus = 'pending' | 'running' | 'completed' | 'failed';

export interface ProcessStep {
  id: number;
  label: string;
  status: StepStatus;
}

export interface TreeNode {
  id: string;
  name: string;
  path: string;
  children?: TreeNode[];
  isLeaf?: boolean;
}

// ============ 报告文件类型 ============
export interface ReportFile {
  id: string;
  name: string;
  date: string;
  downloadUrl: string;
  fileType: string;
  localPath?: string;  // 本地文件路径（如果已下载到本地）
  isLocal?: boolean;   // 是否是本地文件
  category?: string;   // 来源板块（多页爬取时标识）
}

// ============ 新闻文章类型（新闻舆情场景）============
export interface NewsArticle {
  id: string;
  title: string;
  author: string;
  date: string;
  source: string;
  sourceUrl: string;
  summary?: string;
  content?: string;
  category?: string;   // 来源板块（多页爬取时标识）
}

// ============ API Types ============

export interface MenuTreeRequest {
  url: string;
}

export interface MenuTreeResponse {
  url: string;
  root: TreeNode | null;
  leaf_paths: string[];
}

// 附件数据（用于 API 传输）
export interface AttachmentData {
  filename: string;
  base64: string;
  mimeType: string;
}

export interface GenerateRequest {
  url: string;
  startDate: string;
  endDate: string;
  outputScriptName: string;
  taskObjective?: string;
  extraRequirements?: string;
  siteName?: string;
  listPageName?: string;
  sourceCredibility?: string; // 信息源可信度（T1/T2/T3）
  runMode: string;
  crawlMode?: string; // deprecated: 保留兼容，后端统一使用 agent 模式
  downloadReport?: string;
  selectedPaths?: string[];
  attachments?: AttachmentData[];  // 图片/文件附件（base64 编码）
  prevTaskId?: string;  // 重新运行场景：上一次任务 ID（用于 feedback_replay_hint）
}

export interface GenerateResponse {
  taskId: string;
  message: string;
}

export interface TaskStatusResponse {
  taskId: string;
  status: 'pending' | 'queued' | 'running' | 'completed' | 'failed';
  currentStep: number;
  totalSteps: number;
  stepLabel: string;
  logs: string[];
  resultFile?: string;
  error?: string;
  // 爬取结果（报告下载场景）
  reports?: ReportFile[];
  // 下载的文件数量（前5个）
  downloadedCount?: number;
  // 日期范围内文件是否不足5份
  filesNotEnough?: boolean;
  // 文件下载目录
  pdfOutputDir?: string;
  // 爬取结果（新闻舆情场景）
  newsArticles?: NewsArticle[];
  // Markdown 文件路径（新闻舆情场景）
  markdownFile?: string;
  // 总结果数（当结果被截断时使用）
  totalCount?: number;
  // 队列信息（仅 enable_queue=true 时有值）
  queuePosition?: number;         // 排队位置（0=正在运行/已完成）
  queueWaitingCount?: number;     // 当前队列等待数
  queueRunningCount?: number;     // 当前正在运行数
  estimatedWaitSeconds?: number;  // 预估等待秒数
  // 持久化记忆 / 任务评价
  pendingFeedback?: boolean;      // True 时前端应弹出 "任务评价" Modal
  autoFindings?: AutoFindings;    // Stage-1 启发式扫描结果
  htmlFingerprint?: string;       // 列表页结构指纹
  userVerdict?: 'correct' | 'wrong' | string;  // 已提交的评价
  userSuggestion?: string;        // 已提交的用户建议
}

// ============ 持久化记忆 / 任务评价 ============

export interface AutoFindings {
  // 后端 (pygen/memory/auto_findings.py) 返回每个桶都是 string[]：
  //   - redundant_tool_calls : 重复或重叠的工具调用描述
  //   - suspected_failures   : 隐蔽失败信号（如 sourceUrl 全部回退到 baseUrl）
  //   - redundant_code_blocks: 重复出现的代码块描述
  // 仍然保留 any 兼容未来字段升级（例如对象形式的结构化项）
  redundant_tool_calls?: string[] | Array<Record<string, any>>;
  suspected_failures?: string[] | Array<Record<string, any>>;
  redundant_code_blocks?: string[] | Array<Record<string, any>>;
  [k: string]: any;
}

export interface FeedbackRequest {
  verdict: 'correct' | 'wrong';
  suggestion?: string;
}

export interface FeedbackResponse {
  ok: boolean;
  stage?: string;
  warnings?: string[];
  lessons?: any;
  domain?: string;
  profile_confidence?: number | null;
  profile_quarantined?: boolean | null;
}

export interface DraftEpisodeResponse {
  exists: boolean;
  task_id: string;
  auto_findings?: AutoFindings | null;
  facts?: Record<string, any> | null;
  url?: string | null;
  domain?: string | null;
  html_fingerprint?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  verified_selectors_count?: number | null;
}

// ============ 队列全局信息 ============
export interface QueueInfo {
  queueEnabled: boolean;
  waitingCount?: number;
  runningCount?: number;
  maxConcurrency?: number;
  position?: number;
  estimatedWaitSeconds?: number;
  averageTaskSeconds?: number;
}

// ============ SSE 事件类型 ============
export interface SSEEvent {
  type: 'queue_position' | 'log' | 'step' | 'status' | 'complete' | 'failed' | 'cancelled';
  taskId: string;
  timestamp: number;
  [key: string]: any;
}

// ============ History Types ============
export interface HistoryItem {
  id: string;
  taskType: 'single' | 'batch';
  status: 'pending' | 'running' | 'completed' | 'failed';
  createdAt: string;
  endAt?: string;
  owner?: string;
  recordCount?: number;
  config: CrawlerFormData | BatchJob[];
  result?: any;
  logs?: string[];
}

// ============ API Base URL ============
// 自动判断：本地开发 → 直连后端 8000；线上部署 → 空字符串走 Nginx 反代
export const API_BASE_URL =
  window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
    ? 'http://localhost:8000'
    : '';
