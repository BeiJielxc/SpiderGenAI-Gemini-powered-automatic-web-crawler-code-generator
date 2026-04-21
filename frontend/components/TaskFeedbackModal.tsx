import React, { useState, useEffect, useMemo, useCallback } from 'react';
import {
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Loader2,
  RefreshCw,
  Send,
  Info,
  X,
  ChevronDown,
  ChevronUp,
  Clock,
} from 'lucide-react';
import {
  API_BASE_URL,
  AutoFindings,
  DraftEpisodeResponse,
  FeedbackResponse,
} from '../types';

interface TaskFeedbackModalProps {
  taskId: string;
  open: boolean;
  /** 任务跑出来的 autoFindings，可由父组件直接传入，避免再调一次 GET /draft */
  initialAutoFindings?: AutoFindings;
  initialUrl?: string;
  /**
   * 关闭弹窗的回调。注意：弹窗本身不可点击外部关闭，
   * 仅在用户提交评价或主动按"重新运行/暂不评价"后调用。
   */
  onClose: () => void;
  /**
   * 评价提交完成后的回调（用户已选择 correct/wrong 并提交成功）。
   * 父组件可借此切换 UI、隐藏"待评价"提示等。
   */
  onFeedbackSubmitted?: (verdict: 'correct' | 'wrong', resp: FeedbackResponse) => void;
  /**
   * 用户点击"提交并重新运行"时触发。父组件应调用 /api/rerun/{taskId}
   * 并接管后续轮询（切到新 task）。
   */
  onRerun?: (verdict: 'correct' | 'wrong', suggestion: string) => Promise<void> | void;
}

/**
 * 任务评价 Modal
 *
 * - 必选：correct / wrong
 * - wrong 时：suggestion 必填
 * - 提交后调用 POST /api/tasks/{task_id}/feedback
 * - 可选 "提交 + 重新运行" → /api/rerun/{task_id}（带 verdict + suggestion）
 *
 * 交互原则（v2 调整后）：
 * 1. **可关闭**：用户可点右上角 ✕、按 Esc 或 backdrop 关闭。关闭后不污染
 *    记忆 — 后台 draft 仍保留，顶栏"待评价"徽章作为回填入口。只有
 *    点击"提交"按钮才会真正写入持久化记忆。
 * 2. findings 列表默认折叠，可逐组展开查看全部条目。
 * 3. "稍后评价" 按钮显式提示用户：可以先去看结果，回头再评价。
 */
const TaskFeedbackModal: React.FC<TaskFeedbackModalProps> = ({
  taskId,
  open,
  initialAutoFindings,
  initialUrl,
  onClose,
  onFeedbackSubmitted,
  onRerun,
}) => {
  const [verdict, setVerdict] = useState<'correct' | 'wrong' | null>(null);
  const [suggestion, setSuggestion] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string>('');
  const [draft, setDraft] = useState<DraftEpisodeResponse | null>(null);
  const [draftLoading, setDraftLoading] = useState(false);
  // 每个 finding 分组的展开状态：key = label
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({});

  // 弹窗每次打开都重置一次状态
  useEffect(() => {
    if (open) {
      setVerdict(null);
      setSuggestion('');
      setErrorMsg('');
      setSubmitting(false);
      setExpandedGroups({});
    }
  }, [open, taskId]);

  // Esc 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) {
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, submitting, onClose]);

  // 拉取 draft / auto_findings（即使父组件传了 initialAutoFindings，仍后台再请求一次以拿到 facts/domain）
  useEffect(() => {
    if (!open || !taskId) return;
    let cancelled = false;
    setDraftLoading(true);
    fetch(`${API_BASE_URL}/api/tasks/${taskId}/draft`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data: DraftEpisodeResponse | null) => {
        if (!cancelled && data) setDraft(data);
      })
      .catch(() => {
        // 静默失败：父组件传入的 initialAutoFindings 仍可用
      })
      .finally(() => {
        if (!cancelled) setDraftLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, taskId]);

  const findings: AutoFindings | undefined = useMemo(() => {
    return draft?.auto_findings || initialAutoFindings;
  }, [draft, initialAutoFindings]);

  const url = draft?.url || initialUrl;
  const domain = draft?.domain;

  const canSubmit = useMemo(() => {
    if (!verdict) return false;
    if (verdict === 'wrong' && !suggestion.trim()) return false;
    return !submitting;
  }, [verdict, suggestion, submitting]);

  const submitFeedback = async (): Promise<FeedbackResponse | null> => {
    if (!verdict) return null;
    setSubmitting(true);
    setErrorMsg('');
    try {
      const resp = await fetch(`${API_BASE_URL}/api/tasks/${taskId}/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          verdict,
          suggestion: suggestion.trim(),
        }),
      });
      const data: FeedbackResponse = await resp.json().catch(() => ({} as any));
      if (!resp.ok) {
        const reason =
          (data as any)?.detail ||
          (data as any)?.warnings?.join('; ') ||
          `HTTP ${resp.status}`;
        throw new Error(String(reason));
      }
      return data;
    } catch (err: any) {
      setErrorMsg(err?.message || '提交失败，请稍后重试');
      return null;
    } finally {
      setSubmitting(false);
    }
  };

  const handleSubmitOnly = async () => {
    const resp = await submitFeedback();
    if (resp && verdict) {
      onFeedbackSubmitted?.(verdict, resp);
      onClose();
    }
  };

  const handleSubmitAndRerun = async () => {
    if (!verdict) return;
    const resp = await submitFeedback();
    if (!resp) return;
    onFeedbackSubmitted?.(verdict, resp);
    try {
      await onRerun?.(verdict, suggestion.trim());
    } catch (err) {
      // onRerun 内部应自行处理错误展示；这里不阻塞关闭
      console.error('rerun failed', err);
    }
    onClose();
  };

  const handleBackdropClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (submitting) return;
      // 仅当点击的是 backdrop 本身（不是子元素）时才关闭
      if (e.target === e.currentTarget) onClose();
    },
    [submitting, onClose]
  );

  const toggleGroup = (label: string) => {
    setExpandedGroups((prev) => ({ ...prev, [label]: !prev[label] }));
  };

  if (!open) return null;

  const findingItems: Array<{ label: string; items: any[]; tone: 'amber' | 'red' | 'gray' }> = [];
  if (findings?.suspected_failures?.length) {
    findingItems.push({
      label: '疑似失败信号',
      items: findings.suspected_failures,
      tone: 'red',
    });
  }
  if (findings?.redundant_tool_calls?.length) {
    findingItems.push({
      label: '冗余工具调用',
      items: findings.redundant_tool_calls,
      tone: 'amber',
    });
  }
  if (findings?.redundant_code_blocks?.length) {
    findingItems.push({
      label: '可优化的代码片段',
      items: findings.redundant_code_blocks,
      tone: 'amber',
    });
  }

  return (
    // 可点击 backdrop 关闭（提交中除外）
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={handleBackdropClick}
    >
      <div
        className="w-full max-w-2xl bg-white rounded-2xl shadow-2xl flex flex-col max-h-[90vh]"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-6 py-5 border-b border-gray-100 flex items-start gap-3 shrink-0">
          <div className="p-2 rounded-lg bg-emerald-50 text-emerald-600 mt-0.5">
            <CheckCircle2 size={22} />
          </div>
          <div className="flex-1 min-w-0">
            <h2 className="text-lg font-bold text-gray-800">任务评价</h2>
            <p className="text-xs text-gray-500 mt-1">
              评价是可选的，你随时可以关闭后回头再填——顶栏「待评价」徽章会一直保留入口。
              {domain && (
                <span className="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs bg-blue-50 text-blue-600">
                  {domain}
                </span>
              )}
            </p>
            {url && (
              <p className="text-[11px] text-gray-400 truncate mt-1" title={url}>
                {url}
              </p>
            )}
          </div>
          {/* 关闭按钮 */}
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="p-1.5 rounded-md text-gray-400 hover:bg-gray-100 hover:text-gray-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
            title="稍后评价（Esc）"
            aria-label="关闭"
          >
            <X size={18} />
          </button>
        </div>

        {/* Body — 滚动区 */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
          {/* Auto-findings 自检报告 */}
          <section>
            <div className="flex items-center gap-2 mb-2">
              <Info size={16} className="text-gray-500" />
              <h3 className="text-sm font-semibold text-gray-700">模型自检报告</h3>
              {draftLoading && (
                <Loader2 size={14} className="text-gray-400 animate-spin ml-1" />
              )}
            </div>
            <p className="text-[11px] text-gray-400 mb-2 leading-relaxed">
              这是规则启发式扫描的"嫌疑列表"，<b>仅供参考</b>，可能误判。提交评价后，
              做任务的同款大模型会基于完整上下文复盘代码与日志，自行判断是否采纳。
            </p>
            {findingItems.length === 0 ? (
              <p className="text-xs text-gray-500 bg-gray-50 px-3 py-2 rounded-lg">
                未发现明显冗余或失败信号。如有问题请在下方说明。
              </p>
            ) : (
              <div className="space-y-2">
                {findingItems.map((group) => {
                  const expanded = expandedGroups[group.label] ?? false;
                  const initialCount = 5;
                  const showAll = expanded || group.items.length <= initialCount;
                  const visible = showAll ? group.items : group.items.slice(0, initialCount);
                  return (
                    <div
                      key={group.label}
                      className={`px-3 py-2 rounded-lg border text-xs ${
                        group.tone === 'red'
                          ? 'bg-red-50 border-red-100 text-red-700'
                          : group.tone === 'amber'
                          ? 'bg-amber-50 border-amber-100 text-amber-700'
                          : 'bg-gray-50 border-gray-100 text-gray-600'
                      }`}
                    >
                      <div className="font-semibold mb-1 flex items-center gap-1">
                        {group.tone === 'red' ? (
                          <XCircle size={13} />
                        ) : (
                          <AlertTriangle size={13} />
                        )}
                        {group.label}
                        <span className="ml-1 text-[11px] opacity-70">
                          ({group.items.length})
                        </span>
                        {group.items.length > initialCount && (
                          <button
                            type="button"
                            onClick={() => toggleGroup(group.label)}
                            className="ml-auto inline-flex items-center gap-0.5 text-[11px] font-normal opacity-80 hover:opacity-100 underline-offset-2 hover:underline"
                          >
                            {expanded ? (
                              <>
                                收起 <ChevronUp size={12} />
                              </>
                            ) : (
                              <>
                                展开全部 <ChevronDown size={12} />
                              </>
                            )}
                          </button>
                        )}
                      </div>
                      <ul
                        className={`list-disc pl-5 space-y-0.5 ${
                          expanded && group.items.length > 12
                            ? 'max-h-60 overflow-y-auto pr-1'
                            : ''
                        }`}
                      >
                        {visible.map((it: any, i: number) => (
                          <li key={i} className="break-all">
                            {(() => {
                              if (typeof it === 'string') return it;
                              if (it.tool) {
                                return (
                                  <>
                                    <code className="font-mono">{it.tool}</code>
                                    {typeof it.count === 'number' && (
                                      <span> × {it.count}</span>
                                    )}
                                    {it.note && (
                                      <span className="opacity-80">  {it.note}</span>
                                    )}
                                  </>
                                );
                              }
                              if (it.kind) {
                                return (
                                  <>
                                    <span className="font-mono">{it.kind}</span>
                                    {it.detail && <span> — {it.detail}</span>}
                                  </>
                                );
                              }
                              return JSON.stringify(it);
                            })()}
                          </li>
                        ))}
                      </ul>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* Verdict 选择 */}
          <section>
            <h3 className="text-sm font-semibold text-gray-700 mb-2">
              执行结果是否符合预期？ <span className="text-red-500">*</span>
            </h3>
            <div className="grid grid-cols-2 gap-3">
              <button
                type="button"
                onClick={() => setVerdict('correct')}
                className={`flex items-center gap-2 px-4 py-3 rounded-xl border-2 text-sm font-medium transition-all ${
                  verdict === 'correct'
                    ? 'border-emerald-500 bg-emerald-50 text-emerald-700 shadow-sm'
                    : 'border-gray-200 hover:border-emerald-300 text-gray-600'
                }`}
              >
                <CheckCircle2 size={18} />
                运行正确
              </button>
              <button
                type="button"
                onClick={() => setVerdict('wrong')}
                className={`flex items-center gap-2 px-4 py-3 rounded-xl border-2 text-sm font-medium transition-all ${
                  verdict === 'wrong'
                    ? 'border-red-500 bg-red-50 text-red-700 shadow-sm'
                    : 'border-gray-200 hover:border-red-300 text-gray-600'
                }`}
              >
                <XCircle size={18} />
                运行错误
              </button>
            </div>
          </section>

          {/* Suggestion */}
          <section>
            <label className="block text-sm font-semibold text-gray-700 mb-2">
              人工建议或问题描述
              {verdict === 'wrong' ? (
                <span className="text-red-500 ml-1">*（必填）</span>
              ) : (
                <span className="text-gray-400 ml-1 text-xs">（选填）</span>
              )}
            </label>
            <textarea
              value={suggestion}
              onChange={(e) => setSuggestion(e.target.value)}
              rows={4}
              maxLength={2000}
              placeholder={
                verdict === 'wrong'
                  ? '例：只爬到了图标，没拿到正文 / 日期都是今天的 / 翻页失败...'
                  : '例：标题字段偶尔会缺失（可选填，作为优化提示）'
              }
              className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:border-emerald-500 resize-none"
            />
            <p className="text-[11px] text-gray-400 mt-1">
              说人话即可，不需要写代码。Summary Agent 会结合代码与日志自动定位技术原因。
            </p>
          </section>

          {/* Error display */}
          {errorMsg && (
            <div className="px-3 py-2 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700 flex items-start gap-2">
              <XCircle size={16} className="mt-0.5 shrink-0" />
              {errorMsg}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-gray-100 bg-gray-50/60 flex flex-wrap items-center justify-end gap-2 shrink-0 rounded-b-2xl">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium text-gray-600 hover:bg-gray-200/70 transition-colors disabled:opacity-40 disabled:cursor-not-allowed mr-auto"
            title="关闭弹窗，先去查看结果。任意时候可点顶栏「待评价」徽章回填评价。"
          >
            <Clock size={16} />
            稍后评价
          </button>
          <button
            type="button"
            onClick={handleSubmitOnly}
            disabled={!canSubmit}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              canSubmit
                ? 'bg-emerald-600 text-white hover:bg-emerald-700 shadow-sm'
                : 'bg-gray-200 text-gray-400 cursor-not-allowed'
            }`}
          >
            {submitting ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Send size={16} />
            )}
            提交评价
          </button>
          {onRerun && (
            <button
              type="button"
              onClick={handleSubmitAndRerun}
              disabled={!canSubmit}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                canSubmit
                  ? 'bg-blue-600 text-white hover:bg-blue-700 shadow-sm'
                  : 'bg-gray-200 text-gray-400 cursor-not-allowed'
              }`}
              title="提交评价后立即用同样参数重新跑，反馈会注入新任务的 prompt 顶部"
            >
              {submitting ? (
                <Loader2 size={16} className="animate-spin" />
              ) : (
                <RefreshCw size={16} />
              )}
              提交并重新运行
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

export default TaskFeedbackModal;
