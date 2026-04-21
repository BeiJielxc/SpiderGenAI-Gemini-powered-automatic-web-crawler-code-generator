import React, { useState, useEffect } from 'react';
import { 
  ArrowLeft, 
  Clock, 
  CheckCircle2, 
  XCircle, 
  Loader2, 
  Calendar, 
  Search, 
  ExternalLink,
  FileText,
  PlayCircle,
  Eye,
  Trash2,
  RefreshCw,
  Download,
  CheckSquare,
  Square,
  User,
  FileCode
} from 'lucide-react';
import { API_BASE_URL, HistoryItem, CrawlerFormData, BatchJob } from '../types';

interface HistoryViewProps {
  onBack: () => void;
  // Hole-2.B: 第二参数 sourceTaskId 让父级 App 把它存为 prevTaskIdForRerun，
  // 透传到 ExecutionView 的 /api/generate 调用，让 planner 拿到上次的复盘 +
  // 用户反馈。可选——后端会按 domain 二次校验，传错也只会被静默丢弃。
  onRerun: (config: CrawlerFormData, sourceTaskId?: string) => void;
  onViewResult: (item: HistoryItem) => void;
}

const HistoryView: React.FC<HistoryViewProps> = ({ onBack, onRerun, onViewResult }) => {
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [filterStatus, setFilterStatus] = useState<string>('all');
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    fetchHistory();
  }, []);

  const fetchHistory = async () => {
    try {
      setLoading(true);
      const res = await fetch(`${API_BASE_URL}/api/history`);
      if (res.ok) {
        const data = await res.json();
        setHistory(data);
        setSelectedIds(new Set()); // Clear selection on refresh
      }
    } catch (err) {
      console.error('Failed to fetch history:', err);
    } finally {
      setLoading(false);
    }
  };

  const deleteHistoryItem = async (id: string) => {
    try {
      setDeletingIds((prev) => new Set(prev).add(id));
      const res = await fetch(`${API_BASE_URL}/api/history/${encodeURIComponent(id)}`, {
        method: 'DELETE'
      });

      if (!res.ok) {
        let errMsg = `HTTP ${res.status}`;
        try {
          const errData = await res.json();
          if (errData?.detail) errMsg = errData.detail;
        } catch {
          // ignore parse error
        }
        throw new Error(errMsg);
      }

      setHistory((prev) => prev.filter((item) => item.id !== id));
      setSelectedIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    } catch (err) {
      console.error(`Failed to delete item ${id}:`, err);
    } finally {
      setDeletingIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  };

  const handleDeleteSingle = async (item: HistoryItem) => {
    const confirmed = window.confirm('确定删除该历史记录吗？删除后不可恢复。');
    if (!confirmed) return;
    await deleteHistoryItem(item.id);
  };

  const handleBatchDelete = async () => {
    if (selectedIds.size === 0) return;
    const confirmed = window.confirm(`确定删除选中的 ${selectedIds.size} 条记录吗？删除后不可恢复。`);
    if (!confirmed) return;

    const ids = Array.from(selectedIds);
    await Promise.all(ids.map(id => deleteHistoryItem(id)));
  };

  const handleExportCSV = () => {
    if (selectedIds.size === 0) return;
    
    const itemsToExport = history.filter(item => selectedIds.has(item.id));
    
    const columns = [
      'owner',
      '任务id',
      '任务创建时间',
      '任务完成时间',
      '爬取数量',
      '数据开始时间',
      '数据结束时间',
      '任务目标',
      '官网名称',
      '报告页面链接',
      '列表页面名称',
      '信息源可信度',
      '运行模式',
      '是否下载文件',
      '任务状态'
    ];

    const csvRows = [columns.join(',')];
    
    itemsToExport.forEach(item => {
      const config = item.config as CrawlerFormData; 
      const isBatch = item.taskType === 'batch';
      const conf = isBatch ? (item.config as BatchJob[])[0] : (item.config as CrawlerFormData);
      
      const row = [
        item.owner || 'admin',
        item.id,
        item.createdAt ? new Date(item.createdAt).toLocaleString() : '',
        item.endAt ? new Date(item.endAt).toLocaleString() : '',
        item.recordCount || 0,
        conf?.startDate || '',
        conf?.endDate || '',
        (conf?.taskObjective || '').replace(/"/g, '""').replace(/\n/g, ' '),
        (conf?.siteName || '').replace(/"/g, '""'),
        (conf?.url || conf?.reportUrl || '').replace(/"/g, '""'),
        (conf?.listPageName || '').replace(/"/g, '""'),
        conf?.sourceCredibility || '',
        conf?.runMode || '',
        conf?.downloadReport === 'yes' ? '是' : '否',
        getStatusText(item.status)
      ];
      
      const escapedRow = row.map(field => {
        const str = String(field);
        if (str.includes(',')) return `"${str}"`;
        return str;
      });
      
      csvRows.push(escapedRow.join(','));
    });

    const blob = new Blob(["\uFEFF" + csvRows.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `history_export_${new Date().toISOString().slice(0,10)}.csv`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const handleDownloadScript = (item: HistoryItem) => {
    // 仅支持单任务或简单结构的配置获取脚本名
    const config = item.taskType === 'single' ? item.config as CrawlerFormData : (item.config as BatchJob[])[0];
    let filename = config.outputScriptName;
    
    if (!filename) {
      alert('未找到脚本文件名');
      return;
    }
    if (!filename.endsWith('.py')) {
      filename += '.py';
    }
    
    // 打开下载链接
    window.open(`${API_BASE_URL}/api/download/${encodeURIComponent(filename)}`, '_blank');
  };

  const handleBatchDownloadScripts = async () => {
    if (selectedIds.size === 0) return;
    
    const items = history.filter(item => selectedIds.has(item.id));
    // 收集所有有效的文件名
    const filenames = items.map(item => {
      const config = item.taskType === 'single' ? item.config as CrawlerFormData : (item.config as BatchJob[])[0];
      let name = config.outputScriptName;
      if (name && !name.endsWith('.py')) name += '.py';
      return name;
    }).filter(Boolean) as string[];

    if (filenames.length === 0) {
      alert('选中的记录中没有有效的脚本文件信息');
      return;
    }

    try {
      // 调用后端打包接口
      const res = await fetch(`${API_BASE_URL}/api/download/batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filenames })
      });

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }

      // 接收 blob 并触发下载
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `scripts_batch_${new Date().toISOString().slice(0,10)}.zip`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      console.error('Batch download failed:', err);
      alert('批量下载失败，可能部分文件不存在');
    }
  };

  const toggleSelection = (id: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selectedIds.size === filteredHistory.length && filteredHistory.length > 0) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(filteredHistory.map(item => item.id)));
    }
  };

  const filteredHistory = history.filter(item => {
    const config = item.taskType === 'single' ? (item.config as CrawlerFormData) : null;
    const matchesSearch = 
      item.id.toLowerCase().includes(search.toLowerCase()) ||
      (config && config.siteName?.toLowerCase().includes(search.toLowerCase())) ||
      (config && (config.url || config.reportUrl)?.toLowerCase().includes(search.toLowerCase()));
    
    const matchesStatus = filterStatus === 'all' || item.status === filterStatus;
    
    return matchesSearch && matchesStatus;
  });

  const formatDate = (dateStr?: string) => {
    if (!dateStr) return '-';
    try {
      return new Date(dateStr).toLocaleString();
    } catch {
      return dateStr;
    }
  };

  const getStatusText = (status: string) => {
    switch (status) {
      case 'completed': return '已完成';
      case 'failed': return '失败';
      case 'running': return '运行中';
      case 'queued': return '排队中';
      default: return '等待中';
    }
  };

  const getStatusClass = (status: string) => {
    switch (status) {
      case 'completed': return 'bg-emerald-50 text-emerald-700 border-emerald-100';
      case 'failed': return 'bg-red-50 text-red-700 border-red-100';
      case 'running': return 'bg-blue-50 text-blue-700 border-blue-100';
      default: return 'bg-amber-50 text-amber-700 border-amber-100';
    }
  };

  return (
    <div className="flex flex-col h-full w-full bg-slate-50">
      {/* Header */}
      <div className="bg-white border-b border-gray-200 px-6 py-4 flex items-center justify-between shadow-sm z-10">
        <div className="flex items-center gap-4">
          <button 
            onClick={onBack}
            className="p-2 hover:bg-gray-100 rounded-full text-gray-500 transition-colors"
          >
            <ArrowLeft size={20} />
          </button>
          <div>
            <h1 className="text-xl font-bold text-gray-800 flex items-center gap-2">
              <Clock className="text-indigo-500" />
              历史记录
            </h1>
            <p className="text-xs text-gray-500 mt-1">查看过往的任务执行记录</p>
          </div>
        </div>
        
        <div className="flex items-center gap-3">
          {selectedIds.size > 0 && (
            <>
              <span className="text-sm text-gray-500 mr-2">已选 {selectedIds.size} 项</span>
              
              <button
                onClick={handleBatchDownloadScripts}
                className="flex items-center gap-2 px-3 py-1.5 bg-white border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 text-sm"
              >
                <FileCode size={16} />
                批量下载脚本
              </button>

              <button
                onClick={handleExportCSV}
                className="flex items-center gap-2 px-3 py-1.5 bg-white border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 text-sm"
              >
                <Download size={16} />
                导出 CSV
              </button>
              
              <button
                onClick={handleBatchDelete}
                className="flex items-center gap-2 px-3 py-1.5 bg-red-50 border border-red-200 text-red-600 rounded-lg hover:bg-red-100 text-sm"
              >
                <Trash2 size={16} />
                批量删除
              </button>
              <div className="w-px h-6 bg-gray-300 mx-2"></div>
            </>
          )}
          <button 
            onClick={fetchHistory}
            className="p-2 hover:bg-gray-100 rounded-lg text-gray-500 transition-colors"
            title="刷新"
          >
            <RefreshCw size={20} />
          </button>
        </div>
      </div>

      {/* Toolbar */}
      <div className="px-6 py-4 bg-white border-b border-gray-100 flex flex-col md:flex-row gap-4 justify-between items-center">
        <div className="flex items-center gap-4 w-full md:w-auto">
          <button
            onClick={toggleSelectAll}
            className="flex items-center gap-2 text-sm text-gray-600 hover:text-gray-900"
          >
            {selectedIds.size > 0 && selectedIds.size === filteredHistory.length ? (
              <CheckSquare size={18} className="text-indigo-600" />
            ) : (
              <Square size={18} className="text-gray-400" />
            )}
            全选
          </button>
          <div className="relative w-full md:w-96">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" size={18} />
            <input 
              type="text" 
              placeholder="搜索任务 ID、网站名称、URL..." 
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full pl-10 pr-4 py-2 bg-gray-50 border border-gray-200 rounded-lg focus:outline-none focus:border-indigo-500 transition-colors"
            />
          </div>
        </div>
        
        <div className="flex gap-2 w-full md:w-auto overflow-x-auto pb-1 md:pb-0">
          {['all', 'completed', 'running', 'failed', 'pending'].map(status => (
            <button
              key={status}
              onClick={() => setFilterStatus(status)}
              className={`px-4 py-1.5 rounded-full text-sm font-medium whitespace-nowrap transition-colors ${
                filterStatus === status 
                  ? 'bg-indigo-600 text-white shadow-md' 
                  : 'bg-white border border-gray-200 text-gray-600 hover:bg-gray-50'
              }`}
            >
              {status === 'all' ? '全部' : getStatusText(status)}
            </button>
          ))}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4 md:p-6">
        {loading ? (
          <div className="flex flex-col items-center justify-center h-64 text-gray-400">
            <Loader2 size={32} className="animate-spin text-indigo-500 mb-4" />
            <p>加载历史记录中...</p>
          </div>
        ) : filteredHistory.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-64 text-gray-400 bg-white rounded-xl border border-dashed border-gray-200">
            <div className="w-16 h-16 bg-gray-50 rounded-full flex items-center justify-center mb-4">
              <Search size={32} className="opacity-30" />
            </div>
            <p className="text-lg font-medium text-gray-500">未找到相关记录</p>
            <p className="text-sm mt-1">尝试调整搜索词或筛选条件</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4">
            {filteredHistory.map((item) => {
              const config = item.config as CrawlerFormData; 
              const isBatch = item.taskType === 'batch';
              const title = isBatch 
                ? `批量任务 (${(item.config as BatchJob[]).length} 个子任务)`
                : (config.siteName || config.listPageName || '未命名网站');
              const url = isBatch ? '' : (config.reportUrl || (config as any).url || '');
              
              return (
                <div 
                  key={item.id}
                  className={`bg-white rounded-xl border shadow-sm transition-all p-5 flex gap-4 group ${
                    selectedIds.has(item.id) ? 'border-indigo-300 ring-1 ring-indigo-100 bg-indigo-50/10' : 'border-gray-200 hover:shadow-md'
                  }`}
                >
                  {/* Selection Checkbox */}
                  <div className="pt-1">
                    <button 
                      onClick={() => toggleSelection(item.id)}
                      className="text-gray-400 hover:text-indigo-600 transition-colors"
                    >
                      {selectedIds.has(item.id) ? (
                        <CheckSquare size={20} className="text-indigo-600" />
                      ) : (
                        <Square size={20} />
                      )}
                    </button>
                  </div>

                  <div className="flex-1 min-w-0 flex flex-col gap-3">
                    {/* Top Row: Title, Status, ID, Owner */}
                    <div className="flex flex-wrap items-center gap-3">
                      <h3 className="text-lg font-bold text-gray-800 truncate">{title}</h3>
                      <span className={`px-2.5 py-0.5 rounded-full text-xs font-medium border ${getStatusClass(item.status)}`}>
                        {getStatusText(item.status)}
                      </span>
                      <div className="flex items-center gap-2 text-xs text-gray-400 font-mono bg-gray-50 px-2 py-0.5 rounded border border-gray-100">
                        <span>ID: {item.id}</span>
                        {item.owner && (
                          <>
                            <span className="w-px h-3 bg-gray-300"></span>
                            <span className="flex items-center gap-1 text-indigo-400">
                              <User size={10} />
                              {item.owner}
                            </span>
                          </>
                        )}
                      </div>
                    </div>

                    {/* URL */}
                    <div className="text-sm text-gray-500 truncate flex items-center gap-1">
                      <ExternalLink size={12} />
                      <a href={url} target="_blank" rel="noopener noreferrer" className="hover:text-indigo-600 hover:underline">
                        {url || '无 URL 信息'}
                      </a>
                    </div>

                    {/* Config Badges */}
                    {!isBatch && (
                      <div className="flex flex-wrap gap-2 text-xs">
                        <span className="bg-gray-100 text-gray-600 px-2 py-1 rounded border border-gray-200">
                          {config.runMode === 'enterprise_report' ? '企业报告' : '新闻舆情'}
                        </span>
                        <span className="bg-gray-100 text-gray-600 px-2 py-1 rounded border border-gray-200">
                          Agent 模式
                        </span>
                        {(config.startDate || config.endDate) && (
                          <span className="bg-gray-100 text-gray-600 px-2 py-1 rounded border border-gray-200 flex items-center gap-1">
                            <Calendar size={10} />
                            {config.startDate || '?'} ~ {config.endDate || '?'}
                          </span>
                        )}
                        {config.sourceCredibility && (
                          <span className="bg-blue-50 text-blue-700 px-2 py-1 rounded border border-blue-100">
                            信誉: {config.sourceCredibility}
                          </span>
                        )}
                        {config.downloadReport === 'yes' && (
                          <span className="bg-emerald-50 text-emerald-700 px-2 py-1 rounded border border-emerald-100 flex items-center gap-1">
                            <Download size={10} /> 下载报告
                          </span>
                        )}
                        {config.taskObjective && (
                          <span className="bg-amber-50 text-amber-800 px-2 py-1 rounded border border-amber-100 max-w-xs truncate" title={config.taskObjective}>
                            目标: {config.taskObjective}
                          </span>
                        )}
                      </div>
                    )}

                    {/* Time & Stats */}
                    <div className="flex flex-wrap items-center gap-4 text-xs text-gray-400 mt-1">
                      <span className="flex items-center gap-1" title="开始时间">
                        <Clock size={12} />
                        开始: {formatDate(item.createdAt)}
                      </span>
                      {item.endAt && (
                        <span className="flex items-center gap-1" title="结束时间">
                          <CheckCircle2 size={12} />
                          结束: {formatDate(item.endAt)}
                        </span>
                      )}
                      {typeof item.recordCount === 'number' && (
                        <span className="flex items-center gap-1 bg-gray-100 px-2 py-0.5 rounded text-gray-600 font-medium">
                          <FileText size={12} />
                          爬取数量: {item.recordCount}
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Actions */}
                  <div className="flex flex-col gap-2 justify-center border-l pl-4 border-gray-100 min-w-[120px]">
                    <button
                      onClick={() => onViewResult(item)}
                      className="flex items-center justify-center gap-2 px-3 py-1.5 bg-white border border-gray-200 text-gray-700 rounded-lg hover:bg-gray-50 hover:text-indigo-600 hover:border-indigo-200 transition-colors text-xs font-medium"
                    >
                      <Eye size={14} />
                      查看详情
                    </button>
                    
                    <button
                      onClick={() => handleDownloadScript(item)}
                      className="flex items-center justify-center gap-2 px-3 py-1.5 bg-white border border-gray-200 text-gray-700 rounded-lg hover:bg-gray-50 hover:text-blue-600 hover:border-blue-200 transition-colors text-xs font-medium"
                    >
                      <Download size={14} />
                      下载脚本
                    </button>

                    {!isBatch && (
                      <button
                        onClick={() => onRerun(config, item.id)}
                        className="flex items-center justify-center gap-2 px-3 py-1.5 bg-indigo-50 text-indigo-600 border border-indigo-100 rounded-lg hover:bg-indigo-100 transition-colors text-xs font-medium"
                      >
                        <RefreshCw size={14} />
                        重新运行
                      </button>
                    )}

                    <button
                      onClick={() => handleDeleteSingle(item)}
                      disabled={deletingIds.has(item.id)}
                      className="flex items-center justify-center gap-2 px-3 py-1.5 bg-white border border-red-100 text-red-600 rounded-lg hover:bg-red-50 transition-colors text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <Trash2 size={14} />
                      删除
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
};

export default HistoryView;