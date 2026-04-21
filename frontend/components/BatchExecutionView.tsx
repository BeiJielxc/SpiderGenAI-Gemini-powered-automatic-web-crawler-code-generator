import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft,
  CheckCircle2,
  Clock,
  Download,
  Eye,
  Loader2,
  Terminal,
  XCircle
} from 'lucide-react';
import {
  API_BASE_URL,
  Attachment,
  AttachmentData,
  BatchJob,
  GenerateRequest,
  TaskStatusResponse
} from '../types';

interface BatchExecutionViewProps {
  initialJobs: BatchJob[];
  onBack: () => void;
  onViewResult: (job: BatchJob) => void;
  onJobsChange?: (jobs: BatchJob[]) => void;
}

const RUN_MODE_LABELS: Record<string, string> = {
  enterprise_report: '企业报告下载',
  news_report_download: '新闻报告下载',
  news_sentiment: '新闻舆情爬取'
};

const DOWNLOAD_LABELS: Record<string, string> = {
  yes: '是',
  no: '否'
};

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const mapTaskStatusToBatchStatus = (
  status: TaskStatusResponse['status']
): BatchJob['status'] => {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'failed';
  if (status === 'running') return 'running';
  return 'pending';
};

const findLogOverlap = (localLogs: string[], serverLogs: string[]): number => {
  const max = Math.min(localLogs.length, serverLogs.length);
  for (let k = max; k > 0; k -= 1) {
    let matched = true;
    for (let i = 0; i < k; i += 1) {
      if (localLogs[localLogs.length - k + i] !== serverLogs[i]) {
        matched = false;
        break;
      }
    }
    if (matched) return k;
  }
  return 0;
};

const encodeFileBase64 = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const raw = String(reader.result || '');
      resolve(raw.includes(',') ? raw.split(',')[1] : raw);
    };
    reader.onerror = () => reject(reader.error || new Error('read file failed'));
    reader.readAsDataURL(file);
  });

const toAttachmentData = async (att: Attachment): Promise<AttachmentData> => {
  if (att.base64 && att.mimeType) {
    return {
      filename: att.file.name,
      base64: att.base64,
      mimeType: att.mimeType
    };
  }

  const base64 = await encodeFileBase64(att.file);
  return {
    filename: att.file.name,
    base64,
    mimeType: att.mimeType || att.file.type || 'application/octet-stream'
  };
};

const BatchExecutionView: React.FC<BatchExecutionViewProps> = ({
  initialJobs,
  onBack,
  onViewResult,
  onJobsChange
}) => {
  const [jobs, setJobs] = useState<BatchJob[]>(initialJobs);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [globalLogs, setGlobalLogs] = useState<string[]>([
    `[${new Date().toLocaleTimeString()}] [SYSTEM] Batch monitor initialized`
  ]);

  const jobsRef = useRef<BatchJob[]>(initialJobs);
  const runningRef = useRef(false);
  const disposedRef = useRef(false);
  const logPanelRef = useRef<HTMLDivElement>(null);

  const updateJobs = (updater: (prev: BatchJob[]) => BatchJob[]) => {
    setJobs((prev) => {
      const next = updater(prev);
      jobsRef.current = next;
      return next;
    });
  };

  const appendGlobalLog = (line: string) => {
    setGlobalLogs((prev) => [...prev, line]);
  };

  const appendJobLog = (jobId: string, line: string) => {
    updateJobs((prev) =>
      prev.map((j) => (j.id === jobId ? { ...j, logs: [...j.logs, line] } : j))
    );
  };

  const patchJob = (jobId: string, patch: Partial<BatchJob>) => {
    updateJobs((prev) => prev.map((j) => (j.id === jobId ? { ...j, ...patch } : j)));
  };

  const runSingleJob = async (job: BatchJob) => {
    const ts = () => new Date().toLocaleTimeString();
    setActiveJobId(job.id);
    const isResuming = !!job.taskId;
    let taskId = job.taskId;

    try {
      if (!taskId) {
        patchJob(job.id, { status: 'running', rawStatus: 'running' });
        appendGlobalLog(`[${ts()}] [${job.outputScriptName}] status -> RUNNING`);
        appendJobLog(job.id, `[${ts()}] job started`);

        const attachments = await Promise.all(job.attachments.map(toAttachmentData));
        const payload: GenerateRequest = {
          url: job.reportUrl,
          startDate: job.startDate,
          endDate: job.endDate,
          outputScriptName: job.outputScriptName,
          taskObjective: job.taskObjective || job.extraRequirements,
          extraRequirements: job.taskObjective || job.extraRequirements,
          siteName: job.siteName,
          listPageName: job.listPageName,
          sourceCredibility: job.sourceCredibility || undefined,
          runMode: job.runMode,
          crawlMode: job.crawlMode,
          downloadReport: job.downloadReport,
          selectedPaths: job.selectedPaths,
          attachments: attachments.length ? attachments : undefined,
          // Hole-2.B: 仅在用户显式重跑时透传 prevTaskId。后端会做 domain 校验
          // (Hole-2.A)，跨域名的 prev_task_id 会被自动丢弃，所以这里不需要
          // 在前端再次校验。
          prevTaskId: job.prevTaskId || undefined,
        };

        const generateResp = await fetch(`${API_BASE_URL}/api/generate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        if (!generateResp.ok) {
          const err = await generateResp.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${generateResp.status}`);
        }

        const generated = await generateResp.json();
        taskId = generated.taskId;
        if (!taskId) {
          throw new Error('task id missing in generate response');
        }
        patchJob(job.id, { taskId });
        appendGlobalLog(`[${ts()}] [${job.outputScriptName}] task created: ${taskId}`);
        appendJobLog(job.id, `[${ts()}] taskId=${taskId}`);
      } else {
        appendGlobalLog(`[${ts()}] [${job.outputScriptName}] resume task: ${taskId}`);
      }

      let lastLogLen = 0;
      let initializedLogCursor = false;
      while (!disposedRef.current) {
        const statusResp = await fetch(`${API_BASE_URL}/api/status/${taskId}`);
        if (!statusResp.ok) {
          if (statusResp.status === 404) {
            throw new Error('task not found');
          }
          throw new Error(`HTTP ${statusResp.status}`);
        }

        const statusData: TaskStatusResponse = await statusResp.json();
        const nextStatus = mapTaskStatusToBatchStatus(statusData.status);
        patchJob(job.id, {
          status: nextStatus,
          rawStatus: statusData.status,
          resultFile: statusData.resultFile,
          error: statusData.error,
          reports: statusData.reports,
          newsArticles: statusData.newsArticles,
          downloadedCount: statusData.downloadedCount,
          filesNotEnough: statusData.filesNotEnough,
          pdfOutputDir: statusData.pdfOutputDir
        });

        const logs = statusData.logs || [];
        if (!initializedLogCursor) {
          if (isResuming && job.logs.length > 0 && logs.length > 0) {
            const overlap = findLogOverlap(job.logs, logs);
            const delta = logs.slice(overlap);
            if (delta.length > 0) {
              delta.forEach((line) => appendJobLog(job.id, line));
              appendGlobalLog(`[${ts()}] [${job.outputScriptName}] +${delta.length} log(s)`);
            }
          } else if (logs.length > 0) {
            logs.forEach((line) => appendJobLog(job.id, line));
            appendGlobalLog(`[${ts()}] [${job.outputScriptName}] +${logs.length} log(s)`);
          }
          lastLogLen = logs.length;
          initializedLogCursor = true;
        } else if (logs.length > lastLogLen) {
          const delta = logs.slice(lastLogLen);
          delta.forEach((line) => appendJobLog(job.id, line));
          appendGlobalLog(`[${ts()}] [${job.outputScriptName}] +${delta.length} log(s)`);
          lastLogLen = logs.length;
        }

        if (statusData.status === 'completed') {
          patchJob(job.id, {
            status: 'success',
            rawStatus: 'completed',
            resultFile: statusData.resultFile,
            error: undefined,
            reports: statusData.reports,
            newsArticles: statusData.newsArticles,
            downloadedCount: statusData.downloadedCount,
            filesNotEnough: statusData.filesNotEnough,
            pdfOutputDir: statusData.pdfOutputDir
          });
          appendGlobalLog(`[${ts()}] [${job.outputScriptName}] status -> SUCCESS`);
          appendJobLog(job.id, `[${ts()}] completed`);
          return;
        }

        if (statusData.status === 'failed') {
          patchJob(job.id, {
            status: 'failed',
            rawStatus: 'failed',
            error: statusData.error || 'task failed'
          });
          appendGlobalLog(`[${ts()}] [${job.outputScriptName}] status -> FAILED`);
          appendJobLog(job.id, `[${ts()}] failed: ${statusData.error || 'unknown error'}`);
          return;
        }

        await sleep(1200);
      }
    } catch (err: any) {
      const msg = err?.message || 'unknown error';
      patchJob(job.id, { status: 'failed', rawStatus: 'failed', error: msg });
      appendGlobalLog(`[${ts()}] [${job.outputScriptName}] status -> FAILED (${msg})`);
      appendJobLog(job.id, `[${ts()}] failed: ${msg}`);
    }
  };

  const runQueue = async () => {
    if (runningRef.current) return;
    runningRef.current = true;

    while (!disposedRef.current) {
      const next =
        jobsRef.current.find((j) => j.status === 'running') ||
        jobsRef.current.find((j) => j.status === 'pending');
      if (!next) break;
      await runSingleJob(next);
    }

    if (!disposedRef.current) {
      appendGlobalLog(
        `[${new Date().toLocaleTimeString()}] [SYSTEM] Batch queue completed`
      );
      setActiveJobId(null);
    }
    runningRef.current = false;
  };

  useEffect(() => {
    onJobsChange?.(jobs);
  }, [jobs, onJobsChange]);

  useEffect(() => {
    disposedRef.current = false;
    if (jobsRef.current.some((j) => j.status === 'pending' || j.status === 'running')) {
      void runQueue();
    }
    return () => {
      disposedRef.current = true;
    };
  }, []);

  useEffect(() => {
    if (activeJobId && jobs.some((j) => j.id === activeJobId)) return;
    const preferred =
      jobs.find((j) => j.status === 'running') ||
      jobs.find((j) => j.logs.length > 0) ||
      null;
    setActiveJobId(preferred?.id ?? null);
  }, [jobs, activeJobId]);

  useEffect(() => {
    if (logPanelRef.current) {
      logPanelRef.current.scrollTop = logPanelRef.current.scrollHeight;
    }
  }, [globalLogs, activeJobId, jobs]);

  const activeLogText = useMemo(() => {
    if (!activeJobId) return globalLogs.join('\n');
    const job = jobs.find((j) => j.id === activeJobId);
    if (!job?.logs?.length) return globalLogs.join('\n');
    return job.logs.join('\n');
  }, [activeJobId, jobs, globalLogs]);

  const handleRerun = async (job: BatchJob) => {
    if (!job.taskId) return;

    try {
      // 创建新任务（克隆）
      const newJob: BatchJob = {
        ...job,
        id: `batch_${Date.now()}_rerun`,
        status: 'pending',
        rawStatus: 'pending',
        logs: [],
        taskId: undefined, // 清空 taskId，等待分配新的
        // Hole-2.B: 把原 job 的 taskId 当作 prevTaskId 传给新任务，让 planner
        // 通过 feedback_replay_hint 拿到上次的复盘 + 用户反馈。后端 (runner.py)
        // 会按 domain 二次校验，跨域名的 prev_task_id 会被丢弃。
        prevTaskId: job.taskId,
        error: undefined,
        resultFile: undefined
      };
      
      // 添加到列表末尾
      updateJobs(prev => [...prev, newJob]);
      appendGlobalLog(`[${new Date().toLocaleTimeString()}] [${job.outputScriptName}] Rerun queued as new job`);
      
      // 触发队列运行
      disposedRef.current = false;
      if (!runningRef.current) {
        void runQueue();
      }
    } catch (err) {
      console.error('Rerun failed', err);
      alert('重试失败，请查看控制台');
    }
  };

  const handleDownloadScript = (job: BatchJob) => {
    if (!job.resultFile) return;
    const filename = job.resultFile.split(/[/\\]/).pop();
    if (!filename) return;
    window.open(`${API_BASE_URL}/api/download/${encodeURIComponent(filename)}`, '_blank');
  };

  const statusBadge = (status: BatchJob['status']) => {
    if (status === 'running') {
      return (
        <span className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-blue-50 text-blue-600 border border-blue-100">
          <Loader2 size={12} className="animate-spin" />
          运行中
        </span>
      );
    }
    if (status === 'success') {
      return (
        <span className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-emerald-50 text-emerald-600 border border-emerald-100">
          <CheckCircle2 size={12} />
          成功
        </span>
      );
    }
    if (status === 'failed') {
      return (
        <span className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-red-50 text-red-600 border border-red-100">
          <XCircle size={12} />
          失败
        </span>
      );
    }
    return (
      <span className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs bg-gray-100 text-gray-500 border border-gray-200">
        <Clock size={12} />
        待运行
      </span>
    );
  };

  return (
    <div className="h-full w-full bg-white flex flex-col">
      <div className="px-6 py-4 border-b border-gray-100 flex items-center gap-3">
        <button onClick={onBack} className="p-2 hover:bg-gray-100 rounded-full text-gray-500">
          <ArrowLeft size={20} />
        </button>
        <div>
          <h1 className="text-lg font-bold text-gray-900">批量任务监控</h1>
          <p className="text-xs text-gray-500">
            总任务 {jobs.length} | 成功 {jobs.filter((j) => j.status === 'success').length} |
            运行中 {jobs.filter((j) => j.status === 'running').length}
          </p>
        </div>
      </div>

      <div className="h-1/3 min-h-[220px] bg-[#1e1e1e] border-b border-gray-700 flex flex-col">
        <div className="px-4 py-2 bg-[#2d2d2d] border-b border-[#3d3d3d] text-gray-300 text-sm flex items-center gap-2">
          <Terminal size={14} />
          <span>
            {activeJobId
              ? `Log: ${jobs.find((j) => j.id === activeJobId)?.outputScriptName || activeJobId}`
              : 'Log: Global'}
          </span>
        </div>
        <div
          ref={logPanelRef}
          className="flex-1 overflow-y-auto p-4 font-mono text-xs text-gray-300 whitespace-pre-wrap"
        >
          {activeLogText || '[empty]'}
        </div>
      </div>

      <div className="flex-1 overflow-auto bg-white">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-gray-50 text-gray-600 border-b border-gray-100">
            <tr>
              <th className="px-4 py-3 w-10">#</th>
              <th className="px-4 py-3">脚本名</th>
              <th className="px-4 py-3">目标URL</th>
              <th className="px-4 py-3">运行模式</th>
              <th className="px-4 py-3">下载</th>
              <th className="px-4 py-3">时间范围</th>
              <th className="px-4 py-3">状态</th>
              <th className="px-4 py-3 text-right">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {jobs.map((job, idx) => (
              <tr key={job.id} className={activeJobId === job.id ? 'bg-blue-50/30' : ''}>
                <td className="px-4 py-3 text-gray-400">{idx + 1}</td>
                <td className="px-4 py-3 text-gray-800 font-medium">{job.outputScriptName}</td>
                <td className="px-4 py-3 text-gray-600 max-w-[360px] truncate" title={job.reportUrl}>
                  {job.reportUrl}
                </td>
                <td className="px-4 py-3 text-gray-600">
                  {RUN_MODE_LABELS[job.runMode] || job.runMode}
                </td>
                <td className="px-4 py-3 text-gray-600">
                  {DOWNLOAD_LABELS[job.downloadReport] || job.downloadReport}
                </td>
                <td className="px-4 py-3 text-gray-500 font-mono">
                  {job.startDate} ~ {job.endDate}
                </td>
                <td className="px-4 py-3">{statusBadge(job.status)}</td>
                <td className="px-4 py-3">
                  <div className="flex items-center justify-end gap-2">
                    <button
                      onClick={() => setActiveJobId(job.id)}
                      className="inline-flex items-center gap-1 px-2 py-1 text-xs text-gray-700 border border-gray-200 rounded hover:bg-gray-50"
                    >
                      <Terminal size={12} />
                      查看日志
                    </button>
                    <button
                      onClick={() => handleRerun(job)}
                      disabled={job.status !== 'failed' && job.status !== 'success'}
                      className="inline-flex items-center gap-1 px-2 py-1 text-xs text-orange-600 border border-orange-200 rounded hover:bg-orange-50 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <Clock size={12} />
                      重新运行
                    </button>
                    <button
                      onClick={() => handleDownloadScript(job)}
                      disabled={!job.resultFile}
                      className="inline-flex items-center gap-1 px-2 py-1 text-xs text-gray-700 border border-gray-200 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <Download size={12} />
                      下载脚本
                    </button>
                    <button
                      onClick={() => onViewResult(job)}
                      disabled={job.status !== 'success'}
                      className="inline-flex items-center gap-1 px-2 py-1 text-xs text-white bg-emerald-600 rounded hover:bg-emerald-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
                    >
                      <Eye size={12} />
                      查看结果
                    </button>
                  </div>
                  {job.error ? <div className="text-xs text-red-500 mt-1 text-right">{job.error}</div> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

export default BatchExecutionView;
