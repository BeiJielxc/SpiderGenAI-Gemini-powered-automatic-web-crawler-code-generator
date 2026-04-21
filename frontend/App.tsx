import React, { useCallback, useState } from 'react';
import HistoryView from './components/HistoryView';
import { 
  FileJson, 
  Globe, 
  LayoutList, 
  Link as LinkIcon, 
  Terminal, 
  Bot,
  Settings2,
  FileDown,
  ShieldCheck,
  FileSpreadsheet,
  Clock // Added Clock icon
} from 'lucide-react';
import FormInput from './components/FormInput';
import RichInput from './components/RichInput';
import DateInput from './components/DateInput';
import SelectInput from './components/SelectInput';
import ExecutionView from './components/ExecutionView';
import TreeSelectionView from './components/TreeSelectionView';
import BatchConfigView, { BatchConfigData } from './components/BatchConfigView';
import BatchExecutionView from './components/BatchExecutionView';
import {
  API_BASE_URL,
  Attachment,
  BatchJob,
  CrawlerFormData,
  HistoryItem, // Added HistoryItem
  NewsArticle,
  ReportFile,
  TaskStatusResponse
} from './types';

type BatchExecutionPrefill = {
  logs: string[];
  reports: ReportFile[];
  newsArticles: NewsArticle[];
  rawStatus?: TaskStatusResponse['status'];
};

const mapTaskStatusToBatchStatus = (
  status: TaskStatusResponse['status'] | undefined
): BatchJob['status'] => {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'failed';
  if (status === 'running') return 'running';
  return 'pending';
};

const mapBatchStatusToRawStatus = (
  status: BatchJob['status'] | undefined
): TaskStatusResponse['status'] => {
  if (status === 'success') return 'completed';
  if (status === 'failed') return 'failed';
  if (status === 'running') return 'running';
  return 'pending';
};

const mergeBatchJobPreferDefined = (base: BatchJob, patch: Partial<BatchJob>): BatchJob => {
  const merged: BatchJob = { ...base };
  (Object.keys(patch) as Array<keyof BatchJob>).forEach((key) => {
    const value = patch[key];
    if (value !== undefined) {
      (merged as any)[key] = value;
    }
  });
  return merged;
};

const DEFAULT_FORM_DATA: CrawlerFormData = {
  startDate: '',
  endDate: '',
  taskObjective: '',
  siteName: '',
  listPageName: '',
  sourceCredibility: '',
  reportUrl: '',
  outputScriptName: '',
  runMode: '',
  crawlMode: 'agent',
  downloadReport: '',
  attachments: []
};

const normalizeAttachments = (attachments: unknown): Attachment[] => {
  if (!Array.isArray(attachments)) return [];
  return attachments.filter((att): att is Attachment => {
    return Boolean(att && (att as Attachment).file && typeof (att as Attachment).file.name === 'string');
  });
};

const normalizeFormData = (input: Partial<CrawlerFormData> | null | undefined): CrawlerFormData => {
  const data: any = input || {};
  return {
    startDate: typeof data.startDate === 'string' ? data.startDate : DEFAULT_FORM_DATA.startDate,
    endDate: typeof data.endDate === 'string' ? data.endDate : DEFAULT_FORM_DATA.endDate,
    taskObjective:
      (typeof data.taskObjective === 'string' && data.taskObjective) ||
      (typeof data.extraRequirements === 'string' && data.extraRequirements) ||
      '',
    siteName: typeof data.siteName === 'string' ? data.siteName : '',
    listPageName: typeof data.listPageName === 'string' ? data.listPageName : '',
    sourceCredibility: typeof data.sourceCredibility === 'string' ? data.sourceCredibility : '',
    reportUrl: typeof data.reportUrl === 'string' ? data.reportUrl
      : (typeof data.url === 'string' ? data.url : ''),
    outputScriptName: typeof data.outputScriptName === 'string' ? data.outputScriptName : '',
    runMode: typeof data.runMode === 'string' ? data.runMode : '',
    crawlMode: typeof data.crawlMode === 'string' && data.crawlMode ? data.crawlMode : 'agent',
    downloadReport: typeof data.downloadReport === 'string' ? data.downloadReport : '',
    attachments: normalizeAttachments(data.attachments)
  };
};

const App: React.FC = () => {
  const [view, setView] = useState<'form' | 'tree-selection' | 'execution' | 'batch-config' | 'batch-execution' | 'history'>('form');
  const [executionBackTarget, setExecutionBackTarget] = useState<'form' | 'history' | 'batch-execution'>('form');
  const [executionViewSeed, setExecutionViewSeed] = useState(0);
  const [currentBatchSessionId, setCurrentBatchSessionId] = useState<string>('');
  
  // Helper to log batch history
  const logBatchHistory = async (id: string, jobs: BatchJob[]) => {
    try {
      // Determine overall status
      const allCompleted = jobs.every(j => j.status === 'success' || j.status === 'failed');
      const anyFailed = jobs.some(j => j.status === 'failed');
      const status = allCompleted ? (anyFailed ? 'failed' : 'completed') : 'running';
      
      await fetch(`${API_BASE_URL}/api/history/log`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id,
          taskType: 'batch',
          status,
          config: jobs, // Use jobs as config/result for batch
          result: jobs,
          logs: []
        })
      });
    } catch (err) {
      console.warn('Failed to log batch history:', err);
    }
  };

  const [formData, setFormData] = useState<CrawlerFormData>(DEFAULT_FORM_DATA);

  // 用户在 TreeSelectionView 选中的目录路径
  const [selectedPaths, setSelectedPaths] = useState<string[]>([]);
  
  // 当前任务 ID（用于 ExecutionView 轮询状态）
  const [taskId, setTaskId] = useState<string>('');
  const [batchJobs, setBatchJobs] = useState<BatchJob[]>([]);
  const [isFromBatchExecution, setIsFromBatchExecution] = useState(false);
  const [batchExecutionPrefill, setBatchExecutionPrefill] = useState<BatchExecutionPrefill | null>(null);
  // Hole-2.B: 当用户从历史页点"重新运行"时，把原任务 ID 暂存到这里。
  // ExecutionView 在调用 /api/generate 时会读这个值并传给后端，让 planner
  // 拿到 feedback_replay_hint。一次性使用：任务真正启动后由 ExecutionView
  // 通过 onTaskIdChange / 父级 reset 清空，避免脏数据带到下一次新建任务。
  const [prevTaskIdForRerun, setPrevTaskIdForRerun] = useState<string | undefined>(undefined);

  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
    const { name, value } = e.target;
    setFormData(prev => {
      const updated = { ...prev, [name]: value };
      // 当运行模式为"新闻舆情爬取"时，强制设置 downloadReport 为 "no"
      if (name === 'runMode' && value === 'news_sentiment') {
        updated.downloadReport = 'no';
      }
      return updated;
    });
  };

  const handleFileSelect = (files: Attachment[]) => {
    setFormData(prev => ({
      ...prev,
      attachments: [...prev.attachments, ...files]
    }));
  };

  const handleRemoveFile = (index: number) => {
    setFormData(prev => ({
      ...prev,
      attachments: prev.attachments.filter((_, i) => i !== index)
    }));
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    
    // 如果正在提交，直接返回，防止多次触发
    if (isSubmitting) {
      return;
    }

    if (!formData.runMode) {
      alert('请选择运行模式');
      return;
    }

    if (!formData.taskObjective || !formData.taskObjective.trim()) {
      alert('请输入任务目标');
      return;
    }

    // crawlMode 已废弃，统一使用 agent 模式

    if (!formData.downloadReport) {
      alert('请选择是否下载文件');
      return;
    }

    if (!formData.reportUrl) {
      alert('请输入报告页面链接');
      return;
    }

    if (!formData.outputScriptName || !formData.outputScriptName.trim()) {
      alert('请输入输出脚本名称');
      return;
    }

    setIsSubmitting(true);
    setTaskId('');
    setSelectedPaths([]);
    setIsFromBatchExecution(false);
    setBatchExecutionPrefill(null);
    setExecutionBackTarget('form');
    
    // Agent 模式：直接进入执行页（Planner 自主决策，不再需要手动选目录）
    setTimeout(() => {
      setTimeout(() => {
        setIsSubmitting(false);
      }, 2000);
      
      setView('execution');
    }, 300);
  };

  const handleSetToday = () => {
    const today = new Date();
    const yyyy = today.getFullYear();
    const mm = String(today.getMonth() + 1).padStart(2, '0');
    const dd = String(today.getDate()).padStart(2, '0');
    const s = `${yyyy}-${mm}-${dd}`;
    setFormData(prev => ({ ...prev, startDate: s, endDate: s }));
  };

  const handleBackToForm = () => {
    setFormData((prev) => normalizeFormData(prev));
    setView('form');
    setExecutionBackTarget('form');
    setTaskId('');
    setSelectedPaths([]);
    setIsFromBatchExecution(false);
    setBatchExecutionPrefill(null);
    // 用户主动回到表单 = 放弃当前的 rerun 上下文。下次 /api/generate 必须
    // 是真正的"新任务"，不应继续夹带旧 prev_task_id。
    setPrevTaskIdForRerun(undefined);
  };

  // TreeSelectionView 完成选择后回调
  const handleTreeSelectionComplete = (paths: string[], newTaskId: string) => {
    setSelectedPaths(paths);
    setTaskId(newTaskId);
    setIsFromBatchExecution(false);
    setBatchExecutionPrefill(null);
    setExecutionBackTarget('form');
    setExecutionViewSeed((s) => s + 1);
    setView('execution');
  };

  // 单页模式直接生成时的 taskId 回调
  const handleExecutionStart = (newTaskId: string) => {
    setTaskId(newTaskId);
    setIsFromBatchExecution(false);
    setBatchExecutionPrefill(null);
    // Hole-2.B: 一次性消费 prevTaskIdForRerun。任务真正启动后立刻清掉，
    // 防止接下来用户在同一个 ExecutionView 里点 "新建任务" 时把上一次的
    // prev_task_id 又夹带进去。
    setPrevTaskIdForRerun(undefined);
  };

  const handleBatchSubmit = (data: BatchConfigData) => {
    const sessionId = `batch_${Date.now()}`;
    const jobs: BatchJob[] = data.rows.map((row, index) => ({
      ...row,
      id: `${sessionId}_${index}`,
      status: 'pending',
      rawStatus: 'pending',
      logs: []
    }));

    setBatchJobs(jobs);
    setCurrentBatchSessionId(sessionId);
    setIsFromBatchExecution(false);
    setBatchExecutionPrefill(null);
    setTaskId('');
    setSelectedPaths([]);
    setView('batch-execution');
    
    // Log initial batch history
    logBatchHistory(sessionId, jobs);
  };

  const handleBatchJobsChange = useCallback((jobs: BatchJob[]) => {
    setBatchJobs(jobs);
    if (currentBatchSessionId) {
      logBatchHistory(currentBatchSessionId, jobs);
    }
  }, [currentBatchSessionId]);

  const handleViewBatchResult = async (job: BatchJob) => {
    const latestFromStore = batchJobs.find((item) => item.id === job.id);
    let mergedJob = latestFromStore
      ? mergeBatchJobPreferDefined(latestFromStore, job)
      : job;
    if (!mergedJob.taskId && latestFromStore?.taskId) {
      mergedJob = { ...mergedJob, taskId: latestFromStore.taskId };
    }
    const resolvedTaskId = mergedJob.taskId || '';

    if (!resolvedTaskId) {
      alert('该任务暂无 taskId，无法查看执行结果。请回到批量监控页稍后重试。');
      return;
    }

    try {
      const response = await fetch(`${API_BASE_URL}/api/status/${resolvedTaskId}`);
      if (response.ok) {
        const statusData: TaskStatusResponse = await response.json();
        mergedJob = mergeBatchJobPreferDefined(mergedJob, {
          rawStatus: statusData.status,
          status: mapTaskStatusToBatchStatus(statusData.status),
          logs: statusData.logs?.length ? statusData.logs : mergedJob.logs,
          resultFile: statusData.resultFile,
          error: statusData.error,
          reports: statusData.reports,
          newsArticles: statusData.newsArticles,
          downloadedCount: statusData.downloadedCount,
          filesNotEnough: statusData.filesNotEnough,
          pdfOutputDir: statusData.pdfOutputDir
        });
      }
    } catch (error) {
      console.warn('hydrate batch result before navigation failed', error);
    }

    setBatchJobs((prev) =>
      prev.map((item) => (item.id === mergedJob.id ? { ...item, ...mergedJob } : item))
    );
    setBatchExecutionPrefill({
      logs: mergedJob.logs || [],
      reports: mergedJob.reports || [],
      newsArticles: mergedJob.newsArticles || [],
      rawStatus: mergedJob.rawStatus || mapBatchStatusToRawStatus(mergedJob.status)
    });
    setFormData(normalizeFormData({
      startDate: mergedJob.startDate,
      endDate: mergedJob.endDate,
      taskObjective: mergedJob.taskObjective || mergedJob.extraRequirements || '',
      siteName: mergedJob.siteName,
      listPageName: mergedJob.listPageName,
      sourceCredibility: mergedJob.sourceCredibility,
      reportUrl: mergedJob.reportUrl,
      outputScriptName: mergedJob.outputScriptName,
      runMode: mergedJob.runMode,
      crawlMode: mergedJob.crawlMode,
      downloadReport: mergedJob.downloadReport,
      attachments: mergedJob.attachments
    }));
    setSelectedPaths(mergedJob.selectedPaths || []);
    setTaskId(resolvedTaskId);
    setIsFromBatchExecution(true);
    setExecutionBackTarget('batch-execution');
    setExecutionViewSeed((s) => s + 1);
    setView('execution');
  };

  const handleRerunFromHistory = (config: CrawlerFormData, sourceTaskId?: string) => {
    setFormData(normalizeFormData(config));
    setTaskId('');
    setSelectedPaths([]);
    setIsFromBatchExecution(false);
    setBatchExecutionPrefill(null);
    // Hole-2.B: 历史页"重新运行"路径也带 prev_task_id。后端 (runner.py)
    // 会按 domain 二次校验，确保 LLM 不被跨域名 prev_task_id 误导。
    setPrevTaskIdForRerun(sourceTaskId || undefined);
    setView('form');
  };

  const handleViewResultFromHistory = (item: HistoryItem) => {
    if (item.taskType === 'batch') {
      const jobs = item.result as BatchJob[];
      setBatchJobs(jobs);
      setCurrentBatchSessionId(item.id);
      setView('batch-execution');
    } else {
      const config = item.config as CrawlerFormData;
      setFormData(normalizeFormData(config));
      setTaskId(item.id);
      setIsFromBatchExecution(false);
      setBatchExecutionPrefill(null);
      setExecutionBackTarget('history');
      setExecutionViewSeed((s) => s + 1);
      // Determine if we have logs/results to prefill (optional, execution view fetches status anyway)
      // But if task is old, maybe we want to show stored result if API 404s?
      // For now rely on API status endpoint.
      setView('execution');
    }
  };

  return (
    // Main container forces full viewport size
    <div className="h-screen w-screen overflow-hidden bg-white text-gray-800 font-sans">
      
      {view === 'history' ? (
        <HistoryView 
          onBack={handleBackToForm}
          onRerun={handleRerunFromHistory}
          onViewResult={handleViewResultFromHistory}
        />
      ) : view === 'execution' ? (
        <ExecutionView 
          key={`execution-${executionViewSeed}-${isFromBatchExecution ? 'batch' : 'single'}-${taskId || 'new'}`}
          mode={formData.runMode} 
          formData={formData}
          selectedPaths={selectedPaths}
          taskId={taskId}
          initialLogLines={isFromBatchExecution ? batchExecutionPrefill?.logs : undefined}
          initialReports={isFromBatchExecution ? batchExecutionPrefill?.reports : undefined}
          initialNewsArticles={isFromBatchExecution ? batchExecutionPrefill?.newsArticles : undefined}
          initialRawStatus={isFromBatchExecution ? batchExecutionPrefill?.rawStatus : undefined}
          disableAutoStart={isFromBatchExecution}
          // Hole-2.B: 当用户从历史页"重新运行"进入时，把原任务 ID 传下去；
          // ExecutionView 在 /api/generate 时把它放进 prevTaskId 字段。
          prevTaskId={prevTaskIdForRerun}
          onTaskIdChange={handleExecutionStart}
          onBack={() => {
            if (executionBackTarget === 'history') {
              setView('history');
              return;
            }
            if (executionBackTarget === 'batch-execution') {
              setView('batch-execution');
              return;
            }
            handleBackToForm();
          }} 
        />
      ) : view === 'tree-selection' ? (
        <TreeSelectionView 
          url={formData.reportUrl || "https://www.ccxi.com.cn/creditrating/result"}
          formData={formData}
          onGenerate={handleTreeSelectionComplete}
          onBack={handleBackToForm}
        />
      ) : view === 'batch-config' ? (
        <BatchConfigView
          onBack={handleBackToForm}
          onSubmit={handleBatchSubmit}
        />
      ) : view === 'batch-execution' ? (
        <BatchExecutionView
          initialJobs={batchJobs}
          onBack={() => setView('batch-config')}
          onViewResult={handleViewBatchResult}
          onJobsChange={handleBatchJobsChange}
        />
      ) : (
        // Form view acts as a scrollable full page without floating card margins
        <div className="h-full w-full overflow-y-auto bg-white">
          <div className="min-h-full w-full px-4 md:px-12 py-8 md:py-12">
            
            {/* Constrain content width for readability, but background is full */}
            <div className="max-w-6xl mx-auto space-y-10">
              
              <div className="flex items-center gap-5 pb-8 border-b border-gray-100">
                <div className="p-4 bg-emerald-50 rounded-2xl text-emerald-600 shrink-0">
                  <Bot size={32} />
                </div>
                <div>
                  <h1 className="text-3xl font-bold text-gray-900 tracking-tight mb-2">
                    SpiderGenAI-基于gemini的网页爬虫代码生成agent
                  </h1>
                  <p className="text-gray-500 text-base">
                    配置您的爬虫参数以生成自动化脚本
                  </p>
                </div>
                
                <div className="ml-auto flex items-center gap-2">
                  <button 
                    onClick={() => setView('history')}
                    className="flex items-center gap-2 px-4 py-2 text-sm text-gray-600 bg-gray-50 rounded-lg hover:bg-gray-100 transition-colors font-medium border border-gray-100"
                  >
                    <Clock size={18} />
                    历史记录
                  </button>
                  <button 
                     onClick={() => {
                       setBatchJobs([]); // Clear previous batch context if starting new
                       setView('batch-config');
                     }}
                     className="flex items-center gap-2 px-4 py-2 text-sm text-indigo-600 bg-indigo-50 rounded-lg hover:bg-indigo-100 transition-colors font-medium border border-indigo-100"
                   >
                     <FileSpreadsheet size={18} />
                     批量报告爬取
                   </button>
                </div>
              </div>

              {/* Form Section */}
              <form onSubmit={handleSubmit} className="space-y-10">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-x-10 gap-y-8">
                  
                  {/* Row 1: 日期 */}
                  <DateInput
                    label="开始时间:"
                    name="startDate"
                    placeholder="YYYY-MM-DD"
                    value={formData.startDate}
                    onChange={handleChange}
                  />
                  
                  <DateInput
                    label="结束时间:"
                    name="endDate"
                    placeholder="YYYY-MM-DD"
                    value={formData.endDate}
                    onChange={handleChange}
                  />

                  {/* Quick Action: 仅今日数据 */}
                  <div className="md:col-span-2 flex justify-start -mt-4">
                    <button
                      type="button"
                      onClick={handleSetToday}
                      className="
                        inline-flex items-center justify-center
                        px-4 py-2 rounded-lg
                        border border-gray-200 bg-white
                        text-sm font-medium text-gray-700
                        shadow-sm hover:border-gray-300
                        hover:bg-gray-50
                        transition-colors duration-200
                      "
                    >
                      仅今日数据
                    </button>
                  </div>

                  {/* Row 2: 任务目标 */}
                  <RichInput
                    label="任务目标 *（可直接粘贴图片或从本机上传图片/文件）"
                    name="taskObjective"
                    placeholder="请明确写出你要爬取什么、筛选规则、输出字段和结果要求..."
                    value={formData.taskObjective}
                    onChange={handleChange}
                    onFileSelect={handleFileSelect}
                    onRemoveFile={handleRemoveFile}
                    attachedFiles={formData.attachments}
                    className="md:col-span-2"
                    clearable
                    onClear={() => setFormData(prev => ({ ...prev, taskObjective: '', attachments: [] }))}
                  />

                  {/* Row 3: 站点信息（3列） */}
                  <div className="md:col-span-2 grid grid-cols-1 md:grid-cols-3 gap-x-6 gap-y-8">
                    <FormInput
                      label="官网名称"
                      name="siteName"
                      type="text"
                      placeholder="例如：中诚信国际"
                      value={formData.siteName}
                      onChange={handleChange}
                      icon={<Globe size={18} />}
                      clearable
                      onClear={() => setFormData(prev => ({ ...prev, siteName: '' }))}
                    />
                    
                    <FormInput
                      label="列表页面名称"
                      name="listPageName"
                      type="text"
                      placeholder="例如：中诚信国际-评级结果发布"
                      value={formData.listPageName}
                      onChange={handleChange}
                      icon={<LayoutList size={18} />}
                      clearable
                      onClear={() => setFormData(prev => ({ ...prev, listPageName: '' }))}
                    />

                    <SelectInput
                      label="信息源可信度"
                      name="sourceCredibility"
                      value={formData.sourceCredibility}
                      onChange={handleChange}
                      icon={<ShieldCheck size={18} />}
                      options={[
                        { value: 'T1', label: 'T1' },
                        { value: 'T2', label: 'T2' },
                        { value: 'T3', label: 'T3' }
                      ]}
                    />
                  </div>

                  {/* Row 4: URL（必填） */}
                  <FormInput
                    label="报告页面链接 *"
                    name="reportUrl"
                    type="url"
                    placeholder="https://example.com/reports"
                    fullWidth
                    value={formData.reportUrl}
                    onChange={handleChange}
                    icon={<LinkIcon size={18} />}
                    clearable
                    onClear={() => setFormData(prev => ({ ...prev, reportUrl: '' }))}
                  />

                  {/* Row 5: 输出脚本名称 */}
                  <FormInput
                    label="输出脚本名称 *"
                    name="outputScriptName"
                    type="text"
                    placeholder="例如：test.py"
                    fullWidth
                    value={formData.outputScriptName}
                    onChange={handleChange}
                    icon={<Terminal size={18} />}
                    clearable
                    onClear={() => setFormData(prev => ({ ...prev, outputScriptName: '' }))}
                  />

                  {/* Row 6: Configuration Group (3 Columns) */}
                  <div className="md:col-span-2 grid grid-cols-1 md:grid-cols-3 gap-x-6 gap-y-8">
                    <SelectInput
                      label="运行模式 *"
                      name="runMode"
                      value={formData.runMode}
                      onChange={handleChange}
                      icon={<Settings2 size={18} />}
                      options={[
                        { value: 'enterprise_report', label: '企业报告下载' },
                        { value: 'news_report_download', label: '新闻报告下载' },
                        { value: 'news_sentiment', label: '新闻舆情爬取' }
                      ]}
                    />
                    {/* 爬取模式已移除：由 Agent Planner 自主决策 */}
                    <SelectInput
                      label="是否下载文件"
                      name="downloadReport"
                      value={formData.downloadReport}
                      onChange={handleChange}
                      icon={<FileDown size={18} />}
                      options={[
                        { 
                          value: 'yes', 
                          label: '是 (下载前5个PDF/文件并展示)', 
                          disabled: formData.runMode === 'news_sentiment' 
                        },
                        { value: 'no', label: '否 (仅展示结果列表)' }
                      ]}
                    />
                  </div>
                </div>

                <div className="pt-6">
                  <button
                    type="submit"
                    disabled={isSubmitting}
                    className={`
                      w-full py-4 rounded-xl font-semibold text-lg text-white shadow-lg shadow-emerald-200
                      transition-all duration-300 transform active:scale-[0.99]
                      ${isSubmitting 
                        ? 'bg-emerald-400 cursor-not-allowed' 
                        : 'bg-emerald-500 hover:bg-emerald-600 hover:shadow-emerald-300'
                      }
                    `}
                  >
                    {isSubmitting ? '正在初始化...' : '提交配置'}
                  </button>
                </div>
              </form>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default App;
