/** ChapterPanel —— 自 WritePage.tsx 拆出（P2b 巨石拆分，逻辑零改动）。 */
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api';
import { useStore } from '../../store';
import type { Chapter, SkillPack } from '../../types';
import CarLogo from '../../components/CarLogo';
import { CHAPTER_LANG_STYLES } from '../../constants';
import { SkillPackGroupedList } from './write-shared';

/* ===== 章节管理面板 ===== */
// 单个卷分组子组件：用 memo 包裹，折叠/展开某个卷时其他卷不会重渲染
export interface VolumeGroupProps {
  volId: string;
  volTitle: string;
  volChs: Chapter[];
  expanded: boolean;
  renaming: boolean;
  renameValue: string;
  onToggle: (volId: string) => void;
  onSelectChapter: (id: string) => void;
  onCreateChapter: (volId: string) => void;
  onDeleteVolume: (volId: string) => void;
  onStartRename: (volId: string, currentTitle: string) => void;
  onRenameSubmit: (volId: string, newTitle: string) => void;
  onCancelRename: () => void;
  onRenameChange: (v: string) => void;
}

export const VolumeGroup = memo(function VolumeGroup({
  volId, volTitle, volChs, expanded, renaming, renameValue,
  onToggle, onSelectChapter, onCreateChapter, onDeleteVolume,
  onStartRename, onRenameSubmit, onCancelRename, onRenameChange,
}: VolumeGroupProps) {
  return (
    <div className="chapter-volume-group">
      <div className="chapter-volume-header" onClick={() => !renaming && onToggle(volId)}>
        <span className="chapter-volume-arrow">
          {expanded ? '▼' : '▶'}
        </span>
        {renaming ? (
          <input
            className="input chapter-volume-rename-input"
            value={renameValue}
            onChange={e => onRenameChange(e.target.value)}
            onBlur={() => {
              if (renameValue.trim()) onRenameSubmit(volId, renameValue.trim());
              else onCancelRename();
            }}
            onKeyDown={e => { if (e.key === 'Enter') { (e.target as HTMLInputElement).blur(); } if (e.key === 'Escape') onCancelRename(); }}
            autoFocus
            onClick={e => e.stopPropagation()}
          />
        ) : (
          <span className="chapter-volume-title">📁 {volTitle}</span>
        )}
        <span className="chapter-volume-count">{volChs.length}章</span>
        <button className="btn-ghost-sm chapter-volume-add" onClick={e => { e.stopPropagation(); onStartRename(volId, volTitle); }} title="重命名">✏️</button>
        <button className="btn-ghost-sm chapter-volume-add" onClick={e => { e.stopPropagation(); onCreateChapter(volId); }} title="在此卷下添加章节">+</button>
        <button className="btn-ghost-sm chapter-volume-add" onClick={e => { e.stopPropagation(); onDeleteVolume(volId); }} title="删除此卷" style={{color:'#e74c3c'}}>🗑️</button>
      </div>
      {expanded && (
        <div className="chapter-volume-children">
          {volChs.length === 0 ? (
            <div className="chapter-volume-empty">暂无章节，点击 + 添加</div>
          ) : volChs.map((ch, i) => (
            <div key={ch.id} className="chapter-list-item" onClick={() => onSelectChapter(ch.id)}>
              <div className="chapter-list-index">{i + 1}</div>
              <div className="chapter-list-info">
                <div className="chapter-list-title">{ch.title}</div>
                <div className="chapter-list-meta">{ch.word_count} 字</div>
              </div>
              <div className="chapter-list-arrow">›</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
});


export function ChapterPanel(props: {
  chapters: Chapter[];
  activeChapter: Chapter | null;
  chapterEditing: boolean;
  chapterEditTitle: string;
  chapterEditContent: string;
  chapterSaving: boolean;
  aiCreateMode: 'write' | 'continue' | 'polish' | null;
  aiGeneratedContent: string;
  aiCreating: boolean;
  savingChapter: boolean;
  aiStreamError: string;
  aiUserPrompt: string;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
  onSelectChapter: (id: string) => void;
  onCreateChapter: (parentId?: string) => void;
  onCreateVolume: (name?: string, chapterIds?: string[]) => Promise<any>;
  onSaveChapter: () => void;
  onDeleteChapter: (id: string) => void;
  onCancelEdit: () => void;
  onStartEdit: () => void;
  onEditTitle: (v: string) => void;
  onEditContent: (v: string) => void;
  onBackToList: () => void;
  onStartAiCreate: (mode: 'write' | 'continue' | 'polish') => void;
  onExecuteAiCreate: () => void;
  onConfirmAiContent: () => void;
  onCancelAiCreate: () => void;
  onStopAiCreate: () => void;
  onEditAiPrompt: (v: string) => void;
  onRenameVolume: (volId: string, newTitle: string) => Promise<void>;
  onDeleteVolume: (volId: string) => Promise<void>;
  bookId?: string;
  // P0-1: 多Agent协同开关与元信息
  useAgentPipeline?: boolean;
  onToggleAgentPipeline?: (v: boolean) => void;
  agentMeta?: any;
  // P2-9：Spot-Fix 修订
  spotFixing?: boolean;
  spotFixMsg?: string;
  onSpotFix?: (content: string, postValidate: any) => Promise<void>;
  // 聊天式AI创作：历史记录与目标章节
  aiChatHistory: Array<{ role: 'user' | 'assistant'; content: string; chapterTitle?: string; type?: 'content' | 'status'; collapsed?: boolean }>;
  onClearAiChatHistory: () => void;
  onToggleChatMsgCollapse: (index: number) => void;
  onRefreshChapterAnchor: () => void;
  onRegenerateAiContent: () => void;
  aiTargetChapterId: string | null;
  // 本章语言风格（行文文风，最多3个叠加）
  chapterLangStyles?: string[];
  onToggleChapterLangStyle?: (key: string) => void;
  // 连续创作模式
  batchCount?: number;
  onBatchCountChange?: (v: number) => void;
  batchCreating?: boolean;
  batchProgress?: { cur: number; total: number; done: number; message?: string };
  onBatchCreate?: () => void;
}) {
  const { chapters, activeChapter, chapterEditing, chapterEditTitle, chapterEditContent, chapterSaving,
    aiCreateMode, aiGeneratedContent, aiCreating, savingChapter, aiStreamError, aiUserPrompt,
    skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks,
    onSelectChapter, onCreateChapter, onCreateVolume, onSaveChapter, onDeleteChapter, onCancelEdit, onStartEdit,
    onEditTitle, onEditContent, onBackToList, onExecuteAiCreate, onConfirmAiContent, onCancelAiCreate, onStopAiCreate, onEditAiPrompt,
    onRenameVolume, onDeleteVolume, bookId,
    useAgentPipeline: useAgent, onToggleAgentPipeline, agentMeta,
    spotFixing, spotFixMsg, onSpotFix,
    aiChatHistory, onClearAiChatHistory, onToggleChatMsgCollapse, onRefreshChapterAnchor, onRegenerateAiContent, aiTargetChapterId,
    chapterLangStyles: langStyles, onToggleChapterLangStyle,
    batchCount, onBatchCountChange, batchCreating, batchProgress, onBatchCreate,
  } = props;
  const openChatPanel = useStore((s: any) => s.openChatPanel) as (bid: string, sessionId?: string | null, preset?: { tab?: 'setting' | 'chapter' | 'deai' | 'review'; input?: string; fixTasks?: Array<{ location: string; desc: string; fix: string; severity?: string; dimKey?: string }> }) => void;

  const [skillExpanded, setSkillExpanded] = useState(false);
  const [langStyleExpanded, setLangStyleExpanded] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [expandedVolumes, setExpandedVolumes] = useState<Record<string, boolean>>({});
  // 每次进入维度默认折叠所有卷（tab 切换重新挂载，ref 重置）
  const chapterCollapseInitRef = useRef(false);
  const [renamingVolId, setRenamingVolId] = useState<string | null>(null);
  const [renameVolTitle, setRenameVolTitle] = useState('');
  const selectedCount = selectedSkillPackIds.length;
  // 手机端优化：配置抽屉开关、流式全屏阅读开关、agentMeta 折叠态
  const [configDrawerOpen, setConfigDrawerOpen] = useState(false);
  const [streamFullscreen, setStreamFullscreen] = useState(false);
  const [agentMetaExpanded, setAgentMetaExpanded] = useState(false);
  // 正文查看全屏模式（点击"展开全文"时弹出，电脑端+手机端统一生效）
  const [viewingContent, setViewingContent] = useState<{ content: string; title: string } | null>(null);
  // 手机端检测（SSR 安全：window 存在时检测一次，监听 resize 更新）
  const [isMobile, setIsMobile] = useState(() => typeof window !== 'undefined' && window.innerWidth < 600);
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onResize = () => setIsMobile(window.innerWidth < 600);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  // 流式生成时自动开启全屏阅读（手机端+电脑端统一生效）
  useEffect(() => {
    if (aiCreating && aiGeneratedContent.trim()) {
      setStreamFullscreen(true);
    } else if (!aiCreating) {
      setStreamFullscreen(false);
    }
  }, [aiCreating, aiGeneratedContent]);

  // 稳定回调（useCallback）：避免每次 ChapterPanel 重渲染时生成新函数引用，
  // 配合 VolumeGroup 的 memo，使折叠/展开某个卷时其他卷不重渲染。
  const toggleVolume = useCallback((volId: string) => {
    setExpandedVolumes(prev => ({ ...prev, [volId]: prev[volId] === false }));
  }, []);
  const startRenameVolume = useCallback((volId: string, currentTitle: string) => {
    setRenamingVolId(volId);
    setRenameVolTitle(currentTitle);
  }, []);
  const cancelRenameVolume = useCallback(() => {
    setRenamingVolId(null);
  }, []);
  const submitRenameVolume = useCallback(async (volId: string, newTitle: string) => {
    if (newTitle.trim()) {
      try { await onRenameVolume(volId, newTitle.trim()); } catch { /* 忽略，保持编辑态 */ }
    }
    setRenamingVolId(null);
  }, [onRenameVolume]);
  const createChapterInVolume = useCallback((volId: string) => {
    onCreateChapter(volId);
  }, [onCreateChapter]);
  const removeVolume = useCallback((volId: string) => {
    onDeleteVolume(volId);
  }, [onDeleteVolume]);

  // 追加导入章节（已有作品继续添加章节，尤其适合导入的小说继续更新）
  const importChaptersRef = useRef<HTMLInputElement>(null);
  const [importingChapters, setImportingChapters] = useState(false);
  const [importChaptersError, setImportChaptersError] = useState('');

  async function handleImportChapters(e: React.ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(e.target.files || []);
    // 清空 input 以便重复选择同一文件
    if (importChaptersRef.current) importChaptersRef.current.value = '';
    if (picked.length === 0) return;
    if (!bookId) return;
    const valid = picked.filter(f => /\.(txt|md|markdown|docx|zip|json)$/i.test(f.name));
    if (valid.length === 0) {
      setImportChaptersError('请选择 txt/md/docx/zip 格式的文件');
      return;
    }
    setImportChaptersError('');
    setImportingChapters(true);
    try {
      const result = await api.importChapters(bookId, valid);
      alert(`成功追加 ${result.added} 章，当前共 ${result.total} 章`);
      // 刷新页面以重新加载章节列表
      window.location.reload();
    } catch (err: any) {
      setImportChaptersError(err.message || '导入失败');
      alert('追加导入失败: ' + (err.message || '未知错误'));
    } finally {
      setImportingChapters(false);
    }
  }

  // 导入作品后，按文件名/章节标题AI自动识别填入各空维度
  const [aiImportRecognizing, setAiImportRecognizing] = useState(false);
  async function handleAiImportRecognize() {
    if (!bookId) return;
    const confirmFill = confirm(
      `将根据导入作品的【文件名/章节标题】+【内容样本】，AI自动识别并填充空的创作维度。\n\n仅填充空维度，不会覆盖已有内容。是否继续？`
    );
    if (!confirmFill) return;
    setAiImportRecognizing(true);
    try {
      // dimensions 传空数组，后端自动识别空维度并填充
      const result = await api.aiImportRecognize(bookId, [], selectedSkillPackIds);
      alert(result.message || '识别完成');
      // 刷新页面以重新加载维度数据
      window.location.reload();
    } catch (err: any) {
      alert('AI识别填充失败：' + (err.message || '请检查AI配置或网络'));
    } finally {
      setAiImportRecognizing(false);
    }
  }

  // 重新分卷：按50章/卷自动重新归入（清空现有卷结构后重建）
  const [rebinning, setRebinning] = useState(false);

  // 分离卷和章节（useMemo 必须在所有 early return 之前调用，否则违反 Rules of Hooks）
  const volumes = useMemo(() => chapters.filter(c => c.is_volume), [chapters]);
  // 按卷分组的章节（缓存，避免每次渲染都 filter）
  const chaptersByVolume = useMemo(() => {
    const map: Record<string, Chapter[]> = {};
    for (const v of volumes) map[v.id] = [];
    const orphans: Chapter[] = [];
    for (const c of chapters) {
      if (c.is_volume) continue;
      const pid = c.parent_id;
      if (pid && map[pid]) {
        map[pid].push(c);
      } else {
        orphans.push(c); // 无 parent_id 或指向已不存在的卷（孤儿章节）
      }
    }
    map['__orphan__'] = orphans;
    return map;
  }, [chapters, volumes]);

  // 首次有卷数据时默认折叠全部卷（每次切换到该维度 tab 重新挂载，ref 重置，实现每次进入默认折叠）
  useEffect(() => {
    if (chapterCollapseInitRef.current) return;
    if (volumes.length > 0) {
      chapterCollapseInitRef.current = true;
      const init: Record<string, boolean> = { '__orphan__': false };
      for (const v of volumes) init[v.id] = false;
      setExpandedVolumes(init);
    }
  }, [volumes]);

  async function handleRebinVolumes() {
    if (!bookId) return;
    const ok = confirm('将按 50 章/卷自动重新分卷：\n\n1. 清空所有章节的卷归属\n2. 删除现有卷\n3. 按章节号排序后每 50 章归入一卷\n\n此操作不可撤销，是否继续？');
    if (!ok) return;
    setRebinning(true);
    try {
      const result = await api.rebinVolumes(bookId);
      alert(`重新分卷完成：共 ${result.chapters} 章，分为 ${result.volumes} 卷`);
      // 刷新页面以重新加载章节列表
      window.location.reload();
    } catch (err: any) {
      alert('重新分卷失败：' + (err.message || '未知错误'));
    } finally {
      setRebinning(false);
    }
  }

  // Enter快捷发送（Shift+Enter换行）
  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!aiCreating && aiUserPrompt.trim()) {
        onExecuteAiCreate();
      }
    }
  };

  // AI创作面板（聊天式，历史记录保留）
  if (aiCreateMode) {
    const hasResult = aiGeneratedContent.trim().length > 0;
    const streaming = aiCreating && hasResult;

    // P2-5：流式生成全屏阅读模式（电脑端+手机端统一生效，覆盖整个面板）
    if (streamFullscreen && streaming) {
      const streamParas = aiGeneratedContent.split(/\n+/).filter(p => p.trim());
      return (
        <div className="ai-fullscreen-stream">
          <div className="ai-fullscreen-stream-header">
            <div className="ai-fullscreen-stream-title">
              <span className="loading-dot" /> 正在生成{chapterEditTitle ? `·${chapterEditTitle}` : ''}...
            </div>
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{aiGeneratedContent.length}字</span>
          </div>
          <div className="ai-fullscreen-stream-body" ref={el => { if (el) el.scrollTop = el.scrollHeight; }}>
            {streamParas.map((para, pi) => (<p key={pi}>{para.trim()}</p>))}
          </div>
          <div className="ai-fullscreen-stream-footer">
            <button
              className="ai-fullscreen-stop-btn"
              onClick={() => { onStopAiCreate(); setStreamFullscreen(false); }}
            >⏹ 停止生成</button>
          </div>
        </div>
      );
    }

    // 正文查看全屏模式（点击"展开全文"弹出，电脑端+手机端统一生效）
    if (viewingContent) {
      const viewParas = viewingContent.content.split(/\n+/).filter(p => p.trim());
      return (
        <div className="ai-fullscreen-stream ai-fullscreen-view">
          <div className="ai-fullscreen-stream-header">
            <div className="ai-fullscreen-stream-title">
              📖 {viewingContent.title || '正文阅读'}
            </div>
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{viewingContent.content.length}字</span>
          </div>
          <div className="ai-fullscreen-stream-body">
            {viewParas.map((para, pi) => (<p key={pi}>{para.trim()}</p>))}
          </div>
          <div className="ai-fullscreen-stream-footer">
            <button
              className="ai-fullscreen-stop-btn"
              onClick={() => setViewingContent(null)}
            >✕ 关闭阅读</button>
          </div>
        </div>
      );
    }

    return (
      <div className="ai-create-panel ai-chat-panel">
        {/* P2-6：连续创作进度顶部 toast 浮层（不占正文区空间） */}
        {batchCreating && batchProgress && batchProgress.total > 0 && (
          <div className="ai-batch-toast">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontWeight: 600, color: 'var(--accent)' }}>📚 连续创作 {batchProgress.done}/{batchProgress.total}</span>
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{Math.round((batchProgress.done / batchProgress.total) * 100)}%</span>
            </div>
            <div className="ai-batch-toast-progress">
              <div className="ai-batch-toast-progress-bar" style={{ width: `${(batchProgress.done / batchProgress.total) * 100}%` }} />
            </div>
            {batchProgress.message && <div className="ai-batch-toast-msg">{batchProgress.message}</div>}
          </div>
        )}
        {/* 顶部：标题栏 */}
        <div className="ai-create-header">
          <div className="ai-create-header-left">
            <button className="btn-ghost-sm" onClick={onCancelAiCreate} disabled={aiCreating}>← 返回</button>
            <span className="ai-chat-target" title="AI当前锚定的章节（点击刷新重新识别进度）">
              📍 {chapterEditTitle}
              <button
                className="ai-anchor-refresh"
                onClick={onRefreshChapterAnchor}
                disabled={aiCreating}
                title="重新识别当前章节数，刷新定位到待写章"
              >🔄</button>
            </span>
            {aiCreating && <span className="ai-create-status">生成中...</span>}
          </div>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            {aiChatHistory.length > 0 && (
              <button className="btn-ghost-sm" onClick={onClearAiChatHistory} disabled={aiCreating} title="清空全部聊天记录">
                ️ 清空记录
              </button>
            )}
          </div>
        </div>

        {/* 中间：聊天历史区（可滚动） */}
        <div className="ai-chat-history">
          {aiChatHistory.length === 0 && !aiCreating && (
            <div className="ai-create-empty">
              <span className="ai-create-empty-icon">✨</span>
              <p>告诉AI你想写什么，AI将根据你的要求和故事设定创作章节正文</p>
              <p className="text-muted">自动识别当前写到哪一章，历史记录会一直保留，可连续创作多章</p>
            </div>
          )}

          {aiChatHistory.map((msg, i) => {
            // 章节正文消息：支持折叠/展开（已确认章节自动折叠，为下一章留空间）
            const isContent = msg.type === 'content';
            const paras = msg.content.split(/\n+/).filter(p => p.trim());
            const isLong = paras.length > 3 || msg.content.length > 200;
            const collapsed = isContent && isLong && msg.collapsed !== false;
            const showToggle = isContent && isLong;
            return (
            <div key={i} className={`ai-chat-msg ai-chat-msg-${msg.role}`}>
              <div className="ai-chat-msg-avatar">{msg.role === 'user' ? '👤' : '🤖'}</div>
              <div className="ai-chat-msg-body">
                {msg.chapterTitle && <div className="ai-chat-msg-chapter">📍 {msg.chapterTitle}</div>}
                {/* 折叠时隐藏正文，只保留章名+全屏查看按钮；展开时原位显示全文 */}
                {!collapsed && (
                  <div className={`ai-chat-msg-content${showToggle ? ' ai-chat-msg-expanded' : ''}`}>
                    {paras.map((para, pi) => (
                      <p key={pi}>{para.trim()}</p>
                    ))}
                  </div>
                )}
                {showToggle && (
                  <button
                    className="ai-chat-msg-toggle"
                    onClick={() => {
                      if (collapsed) {
                        // 折叠态：点击弹出全屏阅读模式
                        setViewingContent({ content: msg.content, title: msg.chapterTitle || '' });
                      } else {
                        // 展开态：点击收起
                        onToggleChatMsgCollapse(i);
                      }
                    }}
                    title={collapsed ? '全屏查看正文' : '收起正文'}
                  >
                    {collapsed ? `📖 全屏阅读（${msg.content.length}字）` : '收起'}
                  </button>
                )}
              </div>
            </div>
            );
          })}

          {/* 流式生成中的助手消息 */}
          {streaming && (
            <div className="ai-chat-msg ai-chat-msg-assistant ai-chat-msg-streaming">
              <div className="ai-chat-msg-avatar">🤖</div>
              <div className="ai-chat-msg-body">
                <div className="ai-chat-msg-content">
                  {aiGeneratedContent.split(/\n+/).filter(p => p.trim()).map((para, pi) => (
                    <p key={pi}>{para.trim()}</p>
                  ))}
                  <span className="ai-streaming-cursor"><span className="loading-dot" /></span>
                </div>
              </div>
            </div>
          )}

          {/* 加载中（尚未产出内容） */}
          {aiCreating && !hasResult && (
            <div className="ai-create-loading">
              <div className="loading-spinner" />
              <p>AI正在结合{selectedSkillPacks.length > 0 ? selectedSkillPacks.map(p => p.name).join('、') : '设定'}创作中...</p>
            </div>
          )}

          {aiStreamError && <div className="error-msg" style={{ marginTop: 8 }}>{aiStreamError}</div>}

          {/* P0-1: 多Agent协同管线元信息展示（生成结果） */}
          {/* P1-4：默认折叠，只显示一行摘要，点击展开详情。手机端省 200-300px 空间 */}
          {onToggleAgentPipeline && aiCreateMode === 'write' && agentMeta && (
            <details
              open={agentMetaExpanded}
              onToggle={e => setAgentMetaExpanded((e.target as HTMLDetailsElement).open)}
              style={{ marginTop: 8, padding: '6px 10px', background: 'var(--bg-tertiary)', borderRadius: 8, fontSize: 12 }}
            >
              <summary style={{ cursor: 'pointer', fontWeight: 600, color: 'var(--text-secondary)', lineHeight: 1.7, listStyle: 'none' }}>
                <span style={{ fontSize: 10, marginRight: 4 }}>{agentMetaExpanded ? '▼' : '▶'}</span>
                📍 第{agentMeta.current_chapter_num}章 · {agentMeta.vol_title || `第${agentMeta.vol_index}卷`}
                {agentMeta.deai_status === 'success' && <span style={{ color: '#27ae60' }}> · ✅去AI味</span>}
                {agentMeta.deai_status === 'failed' && <span style={{ color: '#e67e22' }} title={agentMeta.review_notes}> · ⚠️去AI味失败</span>}
                {agentMeta.consistency_passed === false && <span style={{ color: '#e74c3c' }} title={agentMeta.consistency_issues}> · ❌一致性异常</span>}
                {agentMeta.consistency_passed === true && <span style={{ color: '#27ae60' }}> · ✅一致性</span>}
                {agentMeta.post_validate && agentMeta.post_validate.critical_count > 0 && (
                  <span style={{ color: '#e74c3c' }} title={`AI痕迹检测：${agentMeta.post_validate.critical_count}个严重问题`}>{' · ⚠️AI痕迹'}({agentMeta.post_validate.critical_count})</span>
                )}
                {agentMeta.changes_applied && agentMeta.changes_applied.applied && <span style={{ color: '#27ae60' }}> · 📝已回写</span>}
                {agentMeta.suggested_title && <span style={{ color: '#9b59b6' }}> · 🏷️{agentMeta.suggested_title}</span>}
              </summary>
              <div style={{ marginTop: 6 }}>
              {agentMeta.chapter_plan && (
                <div style={{ marginBottom: 4, padding: '6px 8px', background: 'var(--bg-secondary)', borderRadius: 4, borderLeft: '3px solid var(--accent)' }}>
                  <b>📋 章节计划：</b>{agentMeta.chapter_plan.slice(0, 200)}{agentMeta.chapter_plan.length > 200 ? '...' : ''}
                </div>
              )}
              <div style={{ fontSize: 11, color: 'var(--text-secondary)', lineHeight: 1.7 }}>
                📍 第{agentMeta.current_chapter_num}章 · {agentMeta.vol_title || `第${agentMeta.vol_index}卷`} · 温度{agentMeta.temperature}
                {agentMeta.deai_status === 'success' && <span style={{ color: '#27ae60' }}> · ✅去AI味成功</span>}
                {agentMeta.deai_status === 'failed' && <span style={{ color: '#e67e22' }} title={agentMeta.review_notes}> · ⚠️去AI味失败(用初稿)</span>}
                {agentMeta.deai_status === 'skipped' && <span className="text-muted"> · 未启用去AI味</span>}
                {agentMeta.consistency_passed === false && <span style={{ color: '#e74c3c' }} title={agentMeta.consistency_issues}> · ❌一致性异常</span>}
                {agentMeta.consistency_passed === true && <span style={{ color: '#27ae60' }}> · ✅一致性通过</span>}
                {agentMeta.post_validate && agentMeta.post_validate.critical_count > 0 && (
                  <span style={{ color: '#e74c3c' }} title={`AI痕迹检测：${agentMeta.post_validate.critical_count}个严重问题`}>
                    {' · ⚠️AI痕迹'}({agentMeta.post_validate.critical_count})
                  </span>
                )}
                {agentMeta.post_validate && agentMeta.post_validate.critical_count === 0 && agentMeta.post_validate.warning_count > 0 && (
                  <span style={{ color: '#f39c12' }} title={`检测到${agentMeta.post_validate.warning_count}个轻微问题`}>
                    {' · 🔍痕迹检测'}({agentMeta.post_validate.warning_count})
                  </span>
                )}
                {agentMeta.changes_applied && agentMeta.changes_applied.applied && (
                  <span style={{ color: '#27ae60' }} title={`状态回写：${(agentMeta.changes_applied.fields_updated||[]).join('、')}`}>
                    {' · 📝状态已回写'}
                  </span>
                )}
                {agentMeta.suggested_title && (
                  <span style={{ color: '#9b59b6' }} title={`AI自动生成标题：${agentMeta.suggested_title}`}>
                    {' · 🏷️标题已生成'}「{agentMeta.suggested_title}」
                  </span>
                )}
              </div>
              {/* 审校校验报告：折叠面板展示校验问题 */}
              {agentMeta.chapter_score && (() => {
                const sc = agentMeta.chapter_score;
                if (!sc.has_issues || !sc.issues || sc.issues.length === 0) {
                  return (
                    <div style={{ marginTop: 6, padding: '6px 8px', background: 'var(--bg-secondary)', borderRadius: 4, borderLeft: '3px solid #27ae60', fontSize: 12 }}>
                      ✅ 校验通过，未发现问题
                    </div>
                  );
                }
                return (
                  <details style={{ marginTop: 6, padding: '6px 8px', background: 'var(--bg-secondary)', borderRadius: 4, borderLeft: '3px solid #f39c12' }}>
                    <summary style={{ cursor: 'pointer', fontSize: 12, fontWeight: 600, color: '#f39c12' }}>
                      ⚠️ 发现 {sc.issues.length} 个校验问题（点击展开）
                    </summary>
                    <div style={{ marginTop: 6, fontSize: 11, lineHeight: 1.6 }}>
                      {sc.issues.map((iss: any, i: number) => (
                        <div key={i} style={{ color: iss.severity === 'critical' ? '#e74c3c' : '#f39c12', marginBottom: 4 }}>
                          {iss.severity === 'critical' ? '🔴' : ''} [{iss.type === 'ai_trace' ? 'AI痕迹' : '一致性'}] {iss.description}
                        </div>
                      ))}
                    </div>
                  </details>
                );
              })()}
              {/* P0-1：后写校验报告详情（可折叠） */}
              {agentMeta.post_validate && (agentMeta.post_validate.critical_count > 0 || agentMeta.post_validate.warning_count > 0) && (
                <div style={{ fontSize: 10, color: 'var(--text-secondary)', lineHeight: 1.6, marginTop: 4, padding: '4px 8px', background: 'var(--bg-secondary)', borderRadius: 4 }}>
                  {agentMeta.post_validate.issues.slice(0, 5).map((iss: any, i: number) => (
                    <div key={i} style={{ color: iss.severity === 'critical' ? '#e74c3c' : '#f39c12' }}>
                      {iss.severity === 'critical' ? '🔴' : '🟡'} [{iss.category}] {iss.pattern} — {iss.suggestion}
                    </div>
                  ))}
                  {agentMeta.post_validate.issues.length > 5 && <div>...还有 {agentMeta.post_validate.issues.length - 5} 条</div>}
                </div>
              )}
              {/* P2-9：一键 Spot-Fix 修订按钮 */}
              {agentMeta?.post_validate && (agentMeta.post_validate.critical_count > 0 || agentMeta.post_validate.warning_count > 0) && onSpotFix && (
                <div style={{ marginTop: 6 }}>
                  <button
                    className="btn-primary-sm"
                    disabled={!!spotFixing}
                    onClick={() => onSpotFix(aiGeneratedContent, agentMeta.post_validate)}
                  >
                    {spotFixing ? '⏳ 修订中...' : '🔧 一键Spot-Fix修订'}
                  </button>
                  {spotFixMsg && <span style={{ marginLeft: 8, fontSize: 11, color: 'var(--text-secondary)' }}>{spotFixMsg}</span>}
                </div>
              )}
              {/* P2-10：落地门禁结果展示 */}
              {agentMeta?.gate_result && !agentMeta.gate_result.passed && (
                <div style={{ fontSize: 10, color: 'var(--text-secondary)', lineHeight: 1.6, marginTop: 4, padding: '4px 8px', background: 'var(--bg-secondary)', borderRadius: 4 }}>
                  <div style={{ fontWeight: 600, color: '#e74c3c' }}>🚪 落地门禁告警（{agentMeta.gate_result.critical_count} critical / {agentMeta.gate_result.warning_count} warning）</div>
                  {agentMeta.gate_result.issues.map((iss: any, i: number) => (
                    <div key={i} style={{ color: iss.severity === 'critical' ? '#e74c3c' : '#f39c12' }}>
                      {iss.severity === 'critical' ? '🔴' : '🟡'} [{iss.gate}] {iss.message}
                    </div>
                  ))}
                </div>
              )}
            </div>
            </details>
          )}
        </div>

        {/* 底部：控制区（多Agent开关+本章语言风格+技能包+输入框，固定在底部） */}
        <div className="ai-create-control-bar">
          {/* P1-3：手机端配置抽屉触发按钮（4项配置收进抽屉，底部只留输入框） */}
          {isMobile && (
            <>
              <button
                className="ai-config-trigger"
                onClick={() => setConfigDrawerOpen(true)}
                disabled={aiCreating}
              >
                <span>⚙️</span>
                <span>创作配置</span>
                {(selectedCount + (langStyles?.length || 0) + (useAgent ? 1 : 0)) > 0 && (
                  <span className="ai-config-trigger-badge">{selectedCount + (langStyles?.length || 0) + (useAgent ? 1 : 0)}</span>
                )}
                <span className="ai-config-trigger-hint">点击展开</span>
              </button>
              {configDrawerOpen && (
                <div className="ai-config-drawer-backdrop open" onClick={() => setConfigDrawerOpen(false)} />
              )}
            </>
          )}
          {/* P1-3：4项配置 wrapper（桌面端 display:contents 透明，手机端 drawer-open 时变 fixed 抽屉） */}
          <div className={`ai-config-items-wrapper ${isMobile ? 'mobile' : ''} ${isMobile && configDrawerOpen ? 'drawer-open' : ''}`}>
            {isMobile && configDrawerOpen && (
              <div className="ai-config-drawer-header">
                <span className="ai-config-drawer-header-title">创作配置</span>
                <button className="ai-config-drawer-header-close" onClick={() => setConfigDrawerOpen(false)}>✕</button>
              </div>
            )}
          {/* P0-1: 多Agent协同管线开关（紧邻协同技能包，两行相邻） */}
          {onToggleAgentPipeline && aiCreateMode === 'write' && (
            <div className="agent-pipeline-toggle">
              <label>
                <input type="checkbox" checked={!!useAgent} onChange={e => onToggleAgentPipeline(e.target.checked)} disabled={aiCreating} />
                <span>🤖 多Agent协同管线</span>
                <span className="text-muted">（计划→正文→去AI味→一致性）</span>
              </label>
            </div>
          )}
          {/* 本章语言风格（行文文风，最多3个叠加）—— 指导AI本章行文基调（可折叠，手机端两行） */}
          {onToggleChapterLangStyle && (aiCreateMode === 'write' || aiCreateMode === 'continue') && (
            <div className="lang-style-collapsible" style={{ marginBottom: 6, background: 'var(--bg-tertiary)', borderRadius: 6, overflow: 'hidden' }}>
              <button
                className="lang-style-toggle"
                onClick={() => setLangStyleExpanded(v => !v)}
                disabled={aiCreating}
                style={{ display: 'flex', alignItems: 'center', gap: 6, width: '100%', padding: '6px 10px',
                  background: 'transparent', color: 'var(--text-secondary)', fontSize: 12, fontWeight: 500,
                  textAlign: 'left', border: 'none', cursor: aiCreating ? 'not-allowed' : 'pointer',
                  opacity: aiCreating ? 0.5 : 1 }}>
                <span style={{ fontSize: 10, flexShrink: 0, width: 12 }}>{langStyleExpanded ? '▼' : '▶'}</span>
                <span>🎨 本章语言风格</span>
                {(langStyles || []).length > 0 && (
                  <span style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                    minWidth: 20, height: 16, padding: '0 5px', borderRadius: 8,
                    background: 'var(--accent)', color: '#fff', fontSize: 10, fontWeight: 700, marginLeft: 2 }}>
                    {(langStyles || []).length}/3
                  </span>
                )}
                <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-muted)', fontWeight: 400 }}>
                  {langStyleExpanded ? '收起' : '展开'}
                </span>
              </button>
              {langStyleExpanded && (
                <div className="lang-style-buttons" style={{ padding: '4px 10px 8px', display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                  {Object.entries(CHAPTER_LANG_STYLES).map(([k, s]) => {
                    const sel = (langStyles || []).includes(k);
                    const disabled = !sel && (langStyles || []).length >= 3;
                    return (
                      <button key={k} type="button"
                        onClick={() => onToggleChapterLangStyle(k)}
                        disabled={aiCreating || disabled}
                        title={s.desc}
                        style={{ padding: '3px 9px', fontSize: 12, borderRadius: 12,
                          cursor: (aiCreating || disabled) ? 'not-allowed' : 'pointer',
                          border: `1px solid ${sel ? 'var(--accent)' : 'var(--border-color)'}`,
                          background: sel ? 'var(--accent-light)' : 'transparent',
                          color: sel ? 'var(--accent)' : 'var(--text-primary)',
                          opacity: (aiCreating || disabled) ? 0.4 : 1 }}>
                        {s.label}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          )}
          {/* 连续创作模式：批量生成 N 章（仅 write 模式，不依赖多Agent协同） */}
          {/* P2-6：进度条移到顶部 toast 浮层，不占配置区空间 */}
          {onBatchCreate && aiCreateMode === 'write' && (
            <div className="batch-create-row" style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6, padding: '6px 8px', background: 'var(--bg-tertiary)', borderRadius: 6, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>📚 连续创作</span>
              <input type="number" min={1} max={10} value={batchCount ?? 3}
                onChange={e => onBatchCountChange?.(Math.max(1, Math.min(10, Number(e.target.value) || 3)))}
                disabled={!!batchCreating} style={{ width: 56, padding: '2px 6px', fontSize: 12, borderRadius: 4, border: '1px solid var(--border-color)', background: 'var(--bg-secondary)', color: 'var(--text-primary)' }} />
              <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>章</span>
              <button className="btn-primary-sm" onClick={() => onBatchCreate?.()} disabled={!!batchCreating || aiCreating}
                style={{ padding: '4px 10px', fontSize: 12 }}>
                {batchCreating ? `⏳ 生成中...` : '🚀 开始连续创作'}
              </button>
            </div>
          )}
          {/* 技能包多选器（可折叠，紧凑模式） */}
          {skillPacks.length > 0 && (
            <div className="skill-pack-collapsible skill-pack-compact">
              <button
                className="skill-pack-toggle"
                onClick={() => setSkillExpanded(v => !v)}
                disabled={aiCreating}
              >
                <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
                <span>📦 协同技能包</span>
                {selectedCount > 0 && <span className="skill-pack-toggle-badge">{selectedCount}</span>}
                <span className="skill-pack-toggle-hint">{skillExpanded ? '收起' : '展开'}</span>
              </button>
              {skillExpanded && (
                <>
                  <SkillPackGroupedList
                    skillPacks={skillPacks}
                    selectedSkillPackIds={selectedSkillPackIds}
                    onToggleSkillPack={onToggleSkillPack}
                    disabled={aiCreating}
                    excludeCategory="master"
                  />
                  {selectedSkillPacks.length > 0 && (
                    <div className="skill-pack-info-list">
                      {selectedSkillPacks.map(pack => (
                        <div key={pack.id} className="skill-pack-info">
                          <span className="skill-pack-info-icon">{pack.icon}</span>
                          <div>
                            <div className="skill-pack-info-name">{pack.name}</div>
                            <div className="skill-pack-info-desc">{pack.description}</div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          )}
          </div>{/* end ai-config-items-wrapper */}

          {/* 用户提问输入区 */}
          <div className="ai-prompt-section ai-prompt-vertical">
            {hasResult && !aiCreating && (
              <div className="ai-prompt-tip">
                💡 已生成正文，可输入修改意见（如"节奏太快请放慢""开头改紧张些"）后点发送，AI将基于本次结果调整；或点上方"🔄 重新生成"重跑
              </div>
            )}
            <textarea
              className="input ai-prompt-input"
              value={aiUserPrompt}
              onChange={e => onEditAiPrompt(e.target.value)}
              onKeyDown={handlePromptKeyDown}
              placeholder={hasResult
                ? '在此输入修改意见...'
                : (aiTargetChapterId
                  ? `例如：请为「${chapterEditTitle}」继续创作下一章正文，剧情连贯、章末留悬念，约2400字...`
                  : '例如：请开篇创作第一章，主角登场，埋下伏笔，约2400字...')}
              rows={5}
              disabled={aiCreating}
            />
            <div className="ai-prompt-bottom-row">
                {aiCreating && (
                <button
                  className="btn-ghost-sm"
                  onClick={onStopAiCreate}
                  style={{ marginRight: 8, color: 'var(--accent)', borderColor: 'var(--accent)' }}
                  title="立即停止生成（已生成内容会保留）"
                >
                  ⏹ 停止
                </button>
              )}
              <button className="btn-primary ai-prompt-submit" onClick={() => onExecuteAiCreate()} disabled={aiCreating || !aiUserPrompt.trim()}>
                {aiCreating ? '⏳ 创作中...' : '🚀 发送'}
              </button>
            </div>

            {/* 完成态底部操作栏（与 Ai 总创作一致的修改意见工作流） */}
            {hasResult && !aiCreating && (
              <div className="ai-prompt-bottom-actions" style={{ display: 'flex', gap: 8, marginTop: 10, justifyContent: 'flex-end', alignItems: 'center', paddingTop: 10, borderTop: '1px solid var(--border-color)' }}>
                <span style={{ fontSize: 11, color: 'var(--text-muted)', marginRight: 'auto' }}>
                  💬 修改意见 → 发送重写，或直接重新生成/保存
                </span>
                <button
                  className="btn-primary"
                  onClick={onRegenerateAiContent}
                  title="基于上一次要求重新生成（覆盖当前结果）"
                  style={{ background: 'linear-gradient(135deg,#e67e22 0%,#d35400 100%)', boxShadow: '0 2px 8px rgba(211,84,0,0.35)' }}
                >
                  🔄 重新生成
                </button>
                <button
                  className="btn-primary-sm"
                  onClick={onConfirmAiContent}
                  disabled={savingChapter}
                  title={savingChapter ? '保存中...' : '将本次生成内容保存到目标章节'}
                  style={savingChapter ? { opacity: 0.6, cursor: 'not-allowed' } : undefined}
                >
                  {savingChapter ? '⏳ 保存中...' : '✓ 保存到章节'}
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // 章节详情查看（笔记本类纸）
  if (activeChapter && !chapterEditing) {
    const paragraphs = (activeChapter.content || '').split(/\n+/).filter(p => p.trim());
    return (
      <div className="chapter-detail-panel chapter-detail-scrollable">
        <div className="chapter-detail-header">
          <button className="btn-ghost-sm" onClick={onBackToList}>← 返回列表</button>
          <div className="chapter-detail-actions">
            <button className="btn-ghost-sm" style={{color:'#e74c3c'}} onClick={() => onDeleteChapter(activeChapter.id)}>🗑️</button>
          </div>
        </div>

        {/* —— 笔记本类纸：章节落地正文；点击纸区域直接进入编辑 —— */}
        <article className="paper-notebook chapter-paper" onClick={onStartEdit} style={{cursor:'text'}}>

          <div className="paper-inner">
            <h3 className="chapter-detail-title paper-title">{activeChapter.title}</h3>
            <div className="chapter-reading-content">
              {paragraphs.length > 0 ? paragraphs.map((para, i) => (
                <p key={i} className="novel-paragraph">{para.trim()}</p>
              )) : (
                <p className="novel-empty-hint">这一章还是空的，点击纸面开始写作</p>
              )}
            </div>
            <div className="paper-footer" aria-hidden="true">
              <span className="paper-footer-left">— 蚂蚁写作笔记 —</span>
              <span className="paper-footer-right">— No. {activeChapter.order_index ?? '—'} —</span>
            </div>
          </div>
        </article>
      </div>
    );
  }

  // 章节编辑（编辑中也要保持笔记本类纸 + 横线 + 衬线大字体）
  if (activeChapter && chapterEditing) {
    return (
      <div className="chapter-edit-panel">
        <div className="chapter-edit-header">
          <button className="btn-ghost-sm" onClick={onCancelEdit}>取消</button>
          <button className="btn-primary-sm" onClick={onSaveChapter} disabled={chapterSaving}>
            {chapterSaving ? '保存中...' : '💾 保存'}
          </button>
        </div>

        {/* —— 笔记本类纸编辑纸 —— */}
        <article className="paper-notebook chapter-paper chapter-edit-paper">
          <div className="paper-inner">
            <input
              className="paper-title-input"
              value={chapterEditTitle}
              onChange={e => onEditTitle(e.target.value)}
              placeholder="章节标题（居中显示，保存后就是正文标题）"
            />
            <textarea
              ref={textareaRef}
              className="paper-textarea chapter-paper-textarea"
              value={chapterEditContent}
              onChange={e => onEditContent(e.target.value)}
              placeholder="开始写作…（保持笔记本横格纸效果，输入时光标会精准踩在每一条横线上）"
              rows={18}
              spellCheck={false}
            />
            <div className="paper-footer" aria-hidden="true">
              <span className="paper-footer-left">— 蚂蚁写作笔记 · 编辑中 —</span>
              <span className="paper-footer-right">— 字数 {(chapterEditContent||'').length} —</span>
            </div>
          </div>
        </article>
      </div>
    );
  }

  // 章节列表（按卷分组）

  // 未分卷 = 无 parent_id，或 parent_id 指向已不存在的卷（删除卷后避免章节变孤儿不可见）
  const orphanChapters = chaptersByVolume['__orphan__'] || [];
  const volumeChapters = (volId: string) => chaptersByVolume[volId] || [];

  return (
    <div className="chapter-list-panel">
      <div className="chapter-list-header">
        <div className="chapter-header-row">
          <button className="btn-ghost-sm" onClick={() => onCreateVolume()} title="新建卷">📂 新卷</button>
          <button className="btn-ghost-sm" onClick={handleRebinVolumes} disabled={rebinning || !bookId || chapters.filter(c => !c.is_volume).length === 0} title="按50章/卷自动重新分卷（清空现有卷结构后重建）">
            {rebinning ? '⏳ 分卷中...' : '🔄 重新分卷'}
          </button>
          <button className="btn-secondary-sm" onClick={() => importChaptersRef.current?.click()} disabled={importingChapters || !bookId} title="从 txt/md/docx/zip 文件追加章节，不影响已有章节">
            {importingChapters ? '⏳ 导入中...' : '📥 导入章节'}
          </button>
          <button className="btn-secondary-sm" onClick={() => onCreateChapter()}>+ 新章节</button>
          <button
            className="btn-ghost-sm btn-ai-recog"
            onClick={handleAiImportRecognize}
            disabled={aiImportRecognizing || !bookId || chapters.filter(c => !c.is_volume).length === 0}
            title="根据导入作品的文件名/章节标题+内容样本，AI自动识别填入空的创作维度（不覆盖已有内容）"
          >
            {aiImportRecognizing ? '⏳ 识别中...' : '🤖 AI识别填维度'}
          </button>
          <button
            className="btn-primary-sm"
            onClick={() => bookId && openChatPanel(bookId)}
            disabled={aiCreating}
            title="打开 AI 智驾·正文Tab：续写/润色本章"
          >
            <span style={{display:'inline-flex',alignItems:'center',gap:6}}><CarLogo size={20} /><span>AI</span><span>智驾</span></span>
          </button>
        </div>
        <input
          ref={importChaptersRef}
          type="file"
          multiple
          style={{display:'none'}}
          onChange={handleImportChapters}
        />
      </div>
      {importChaptersError && <div className="error-msg" style={{padding:'0 12px'}}>{importChaptersError}</div>}
      {chapters.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon">📖</div>
          <p>还没有章节，点击"新章节"开始写作</p>
        </div>
      ) : (
        <div className="chapter-list">
          {/* 按卷分组显示 - 使用 memo 化的 VolumeGroup 子组件，折叠某卷时其他卷不重渲染 */}
          {volumes.map(vol => {
            const volChs = volumeChapters(vol.id);
            const expanded = expandedVolumes[vol.id] !== false; // 默认展开
            const isRenaming = renamingVolId === vol.id;
            return (
              <VolumeGroup
                key={vol.id}
                volId={vol.id}
                volTitle={vol.title}
                volChs={volChs}
                expanded={expanded}
                renaming={isRenaming}
                renameValue={renameVolTitle}
                onToggle={toggleVolume}
                onSelectChapter={onSelectChapter}
                onCreateChapter={createChapterInVolume}
                onDeleteVolume={removeVolume}
                onStartRename={startRenameVolume}
                onRenameSubmit={submitRenameVolume}
                onCancelRename={cancelRenameVolume}
                onRenameChange={setRenameVolTitle}
              />
            );
          })}
          {/* 未分卷的章节 - 单独处理（不复用 VolumeGroup，因其重命名逻辑不同：转未分卷为命名卷） */}
          {orphanChapters.length > 0 && (
            <div className="chapter-volume-group">
              <div
                className="chapter-volume-header"
                onClick={() => volumes.length > 0 && toggleVolume('__orphan__')}
              >
                {volumes.length > 0 && (
                  <span className="chapter-volume-arrow">{expandedVolumes['__orphan__'] !== false ? '▼' : '▶'}</span>
                )}
                {renamingVolId === '__orphan__' ? (
                  <input
                    className="input chapter-volume-rename-input"
                    value={renameVolTitle}
                    onChange={e => setRenameVolTitle(e.target.value)}
                    onBlur={async () => {
                      const name = renameVolTitle.trim();
                      setRenamingVolId(null);
                      if (name) {
                        await onCreateVolume(name, orphanChapters.map(c => c.id));
                      }
                    }}
                    onKeyDown={e => { if (e.key === 'Enter') { (e.target as HTMLInputElement).blur(); } }}
                    autoFocus
                    onClick={e => e.stopPropagation()}
                    placeholder="输入卷名..."
                  />
                ) : (
                  <span className="chapter-volume-title">📋 未分卷</span>
                )}
                <span className="chapter-volume-count">{orphanChapters.length}章</span>
                <button className="btn-ghost-sm chapter-volume-add" onClick={e => { e.stopPropagation(); setRenamingVolId('__orphan__'); setRenameVolTitle(''); }} title="将未分卷转为命名卷">✏️</button>
                <button className="btn-ghost-sm chapter-volume-add" onClick={e => { e.stopPropagation(); onDeleteVolume('__orphan__'); }} title="删除全部未分卷章节" style={{color:'#e74c3c'}}>🗑️</button>
              </div>
              {(volumes.length === 0 || expandedVolumes['__orphan__'] !== false) && (
                <div className="chapter-volume-children">
                  {orphanChapters.map((ch, i) => (
                    <div key={ch.id} className="chapter-list-item" onClick={() => onSelectChapter(ch.id)}>
                      <div className="chapter-list-index">{i + 1}</div>
                      <div className="chapter-list-info">
                        <div className="chapter-list-title">{ch.title}</div>
                        <div className="chapter-list-meta">{ch.word_count} 字</div>
                      </div>
                      <div className="chapter-list-arrow">›</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
