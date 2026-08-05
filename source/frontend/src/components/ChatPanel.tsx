import { useState, useEffect, useRef, useCallback, memo } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import type { ActionCard, ProgressMap, AIMessage, SkillPack } from '../types';

// ============================================================================
// AI 智驾：四Tab（设定/正文/去AI/校审）统一创作平台
// 整合原 AI副驾 + AI总创作 + 章节AI创作 能力
// 手机优先：底部Tab栏 + 大按钮 + 紧凑输入区
// ============================================================================

type SmartTab = 'setting' | 'chapter' | 'deai' | 'review';

// 维度定义（与后端 SMART_DIMENSIONS 对齐）
interface DimSpec {
  key: string; label: string; field: string; card: string; icon: string; hint: string;
}

// SSE 事件类型
type SseEvent =
  | { type: 'delta'; content: string }
  | { type: 'card'; card: ActionCard; session_id: string; meta?: any }
  | { type: 'done'; session_id: string }
  | { type: 'error'; error: string };

// SSE 流解析
async function* parseSSE(response: Response): AsyncGenerator<SseEvent> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        for (const line of block.split('\n')) {
          const trimmed = line.trim();
          if (!trimmed.startsWith('data:')) continue;
          const jsonStr = trimmed.slice(5).trim();
          if (!jsonStr) continue;
          try {
            yield JSON.parse(jsonStr) as SseEvent;
          } catch { /* ignore malformed */ }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

// ============================================================================
// Action Card 单卡渲染（采纳 / 编辑 / 忽略）
// ============================================================================
interface CardViewProps {
  card: ActionCard;
  onAdopt: (card: ActionCard) => void;
  onEdit: (card: ActionCard, newContent: string) => void;
  onIgnore: (card: ActionCard) => void;
  applying: boolean;
  onReplaceChapter?: (card: ActionCard, meta: any) => void;
}

const CARD_ICON: Record<string, string> = {
  SAVE_WORLDSETTING: '🌍', SAVE_CHARACTER: '👤', SAVE_FORESHADOW: '🔮',
  SAVE_OUTLINE_NODE: '📋', SAVE_PLOT: '📖', SAVE_LOCATION: '🗺️',
  SAVE_RULE: '⚙️', APPLY_STYLE: '✍️', SAVE_CONCEPT: '💡', SAVE_CHAPTER: '📚',
};

const ActionCardView = memo(function ActionCardView({ card, onAdopt, onEdit, onIgnore, applying, onReplaceChapter }: CardViewProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(card.content);
  const status = card.status || 'pending';
  // 去AI味卡片：meta.replace=true 时主按钮变为"替换本章正文"
  const cardMeta = (card as any).__meta as any;
  const isReplaceMode = !!(onReplaceChapter && cardMeta?.replace && cardMeta?.chapter_id);

  const handleSaveEdit = () => {
    if (!draft.trim()) return;
    onEdit({ ...card, content: draft.trim(), status: 'edited' }, draft.trim());
    setEditing(false);
  };

  if (status === 'ignored') {
    return (
      <div className="chat-card chat-card-ignored">
        <div className="chat-card-head">
          <span className="chat-card-icon">{CARD_ICON[card.type] || '📌'}</span>
          <span className="chat-card-title">{card.title}</span>
          <span className="chat-card-status">已忽略</span>
        </div>
      </div>
    );
  }

  if (status === 'adopted' || status === 'edited') {
    return (
      <div className="chat-card chat-card-adopted">
        <div className="chat-card-head">
          <span className="chat-card-icon">{CARD_ICON[card.type] || '📌'}</span>
          <span className="chat-card-title">{card.title}</span>
          <span className="chat-card-status">✓ 已落地 · {card.target}</span>
        </div>
        <div className="chat-card-body">{card.content}</div>
      </div>
    );
  }

  return (
    <div className="chat-card">
      <div className="chat-card-head">
        <span className="chat-card-icon">{CARD_ICON[card.type] || '📌'}</span>
        <span className="chat-card-title">{card.title}</span>
        <span className="chat-card-target">→ {card.target}</span>
      </div>
      {editing ? (
        <>
          <textarea
            className="chat-card-edit"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            rows={Math.min(10, Math.max(4, draft.split('\n').length))}
          />
          <div className="chat-card-actions">
            <button className="chat-card-btn primary" onClick={handleSaveEdit} disabled={!draft.trim()}>
              保存并落地
            </button>
            <button className="chat-card-btn" onClick={() => { setEditing(false); setDraft(card.content); }}>
              取消
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="chat-card-body">{card.content}</div>
          <div className="chat-card-actions">
            {isReplaceMode ? (
              <button className="chat-card-btn primary" onClick={() => onReplaceChapter!(card, cardMeta)} disabled={applying}>
                {applying ? '替换中…' : '替换本章正文'}
              </button>
            ) : (
              <button className="chat-card-btn primary" onClick={() => onAdopt(card)} disabled={applying}>
                {applying ? '落地中…' : (card.type === 'SAVE_CHAPTER' ? '保存为新章节' : '采纳落地')}
              </button>
            )}
            <button className="chat-card-btn" onClick={() => setEditing(true)} disabled={applying}>
              {card.type === 'SAVE_CHAPTER' ? '编辑后保存' : '编辑'}
            </button>
            <button className="chat-card-btn ghost" onClick={() => onIgnore(card)} disabled={applying}>
              忽略
            </button>
          </div>
        </>
      )}
    </div>
  );
});

// ============================================================================
// 进度地图（设定Tab可展开）
// ============================================================================
const ProgressMapView = memo(function ProgressMapView({ progress, onClose }: { progress: ProgressMap | null; onClose?: () => void }) {
  if (!progress) return null;
  const statusLabel: Record<string, string> = { empty: '未开始', sketch: '草稿', partial: '进行中', solid: '已完善' };
  const statusColor: Record<string, string> = { empty: '#999', sketch: '#d97706', partial: '#2563eb', solid: '#16a34a' };
  return (
    <div className="chat-progress">
      <div className="chat-progress-head">
        <span>创作进度 · {progress.overall}%</span>
        <span className="chat-progress-count">{progress.filled}/{progress.total} 维度完善</span>
        {onClose && <button className="chat-progress-close" onClick={onClose}>×</button>}
      </div>
      <div className="chat-progress-bar">
        <div className="chat-progress-fill" style={{ width: `${progress.overall}%` }} />
      </div>
      {progress.next_step && (
        <div className="chat-progress-next">
          <strong>下一步建议：</strong>{progress.next_step.label}
          <div className="chat-progress-hint">{progress.next_step.hint}</div>
        </div>
      )}
      <div className="chat-progress-dims">
        {progress.dims.map(d => (
          <div key={d.field} className="chat-progress-dim">
            <div className="chat-progress-dim-head">
              <span>{d.label}</span>
              <span style={{ color: statusColor[d.status], fontSize: 12 }}>{statusLabel[d.status]}</span>
            </div>
            <div className="chat-progress-dim-bar">
              <div style={{ width: `${d.pct}%`, background: statusColor[d.status] }} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
});

// ============================================================================
// 消息气泡
// ============================================================================
interface MessageBubbleProps {
  message: AIMessage;
  onAdopt: (c: ActionCard) => void;
  onEdit: (c: ActionCard, content: string) => void;
  onIgnore: (c: ActionCard) => void;
  applyingCardId: string | null;
  streaming: boolean;
  onReplaceChapter?: (card: ActionCard, meta: any) => void;
}

const MessageBubble = memo(function MessageBubble({ message, onAdopt, onEdit, onIgnore, applyingCardId, streaming, onReplaceChapter }: MessageBubbleProps) {
  const isUser = message.role === 'user';
  return (
    <div className={`chat-msg ${isUser ? 'chat-msg-user' : 'chat-msg-ai'}`}>
      <div className="chat-msg-avatar">{isUser ? '我' : '🚗'}</div>
      <div className="chat-msg-body">
        {message.content ? (
          <div className="chat-msg-text">{message.content}{streaming && <span className="chat-cursor">▋</span>}</div>
        ) : streaming ? (
          <div className="chat-msg-text"><span className="chat-cursor">▋</span></div>
        ) : null}
        {message.cards && message.cards.length > 0 && (
          <div className="chat-msg-cards">
            {message.cards.map((c, idx) => (
              <ActionCardView
                key={c.id || idx}
                card={c}
                onAdopt={onAdopt}
                onEdit={onEdit}
                onIgnore={onIgnore}
                applying={applyingCardId === c.id}
                onReplaceChapter={onReplaceChapter}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
});

// ============================================================================
// 技能包选择器（精简版，按 category 分组）
// ============================================================================
function SkillPackSelector({ packs, selected, onToggle, compact }: {
  packs: SkillPack[];
  selected: string[];
  onToggle: (id: string) => void;
  compact?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  if (packs.length === 0) return null;
  const selectedCount = selected.length;
  return (
    <div className={`smart-skill-selector ${compact ? 'compact' : ''}`}>
      <button className="smart-skill-toggle" onClick={() => setExpanded(e => !e)}>
        📦 技能包 {selectedCount > 0 && <span className="smart-skill-badge">{selectedCount}</span>}
        <span className="smart-skill-arrow">{expanded ? '▲' : '▼'}</span>
      </button>
      {expanded && (
        <div className="smart-skill-list">
          {packs.map(p => (
            <label key={p.id} className={`smart-skill-item ${selected.includes(p.id) ? 'checked' : ''}`}>
              <input
                type="checkbox"
                checked={selected.includes(p.id)}
                onChange={() => onToggle(p.id)}
              />
              <span className="smart-skill-icon">{p.icon || '📦'}</span>
              <span className="smart-skill-name">{p.name}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

// ============================================================================
// 主组件：AI 智驾
// ============================================================================
export default function ChatPanel() {
  const { chatPanelOpen, chatPanelBookId, closeChatPanel, openChatPanel } = useStore() as any;
  const [activeTab, setActiveTab] = useState<SmartTab>('setting');
  const [messages, setMessages] = useState<AIMessage[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [streamError, setStreamError] = useState('');
  const [progress, setProgress] = useState<ProgressMap | null>(null);
  const [showProgress, setShowProgress] = useState(false);
  const [applyingCardId, setApplyingCardId] = useState<string | null>(null);
  const [historySessions, setHistorySessions] = useState<Array<{ id: string; title: string; updated_at: string | null; message_count: number }>>([]);
  const [showHistory, setShowHistory] = useState(false);

  // 智驾专属状态
  const [dimensions, setDimensions] = useState<DimSpec[]>([]);
  const [selectedDim, setSelectedDim] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<Array<{ id: string; title: string; preview: string }>>([]);
  const [loadingSuggest, setLoadingSuggest] = useState(false);
  const [skillPacks, setSkillPacks] = useState<SkillPack[]>([]);
  const [selectedSkillPacks, setSelectedSkillPacks] = useState<string[]>([]);
  const [latestChapter, setLatestChapter] = useState<{ id: string; title: string; order_index: number; word_count: number; status: string } | null>(null);
  const [nextChapterNum, setNextChapterNum] = useState(1);
  const [chapters, setChapters] = useState<Array<{ id: string; title: string; order_index: number; word_count: number; status: string }>>([]);
  const [selectedChapterId, setSelectedChapterId] = useState<string | null>(null);
  const [deaiPacks, setDeaiPacks] = useState<Array<{ id: string; name: string; description: string; icon: string; priority: number }>>([]);
  const [reviewing, setReviewing] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const streamBufferRef = useRef<string>('');
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const bookId = chatPanelBookId;

  // 自动滚动
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, streaming]);

  // 加载进度
  const refreshProgress = useCallback(async () => {
    if (!bookId) return;
    try {
      const p = await api.getProgressMap(bookId);
      setProgress(p);
    } catch { /* ignore */ }
  }, [bookId]);

  const refreshHistory = useCallback(async () => {
    if (!bookId) return;
    try {
      const r = await api.listBookChatSessions(bookId);
      setHistorySessions(r.sessions || []);
    } catch { /* ignore */ }
  }, [bookId]);

  // 打开时加载基础数据
  useEffect(() => {
    if (chatPanelOpen && bookId) {
      refreshProgress();
      refreshHistory();
      // 加载维度列表
      api.smartDimensions().then(r => setDimensions(r.dimensions || [])).catch(() => {});
      // 加载技能包（构思类 + 文风类，设定/正文用）
      api.listSkillPacks().then(all => setSkillPacks(all || [])).catch(() => {});
      // 加载去AI味技能包
      api.smartDeaiPacks().then(r => setDeaiPacks(r.packs || [])).catch(() => {});
      // 加载章节列表
      api.smartChapters(bookId).then(r => setChapters(r.chapters || [])).catch(() => {});
      // 加载最新章节
      api.smartLatestChapter(bookId).then(r => {
        setLatestChapter(r.latest);
        setNextChapterNum(r.next_chapter_num);
      }).catch(() => {});
    }
  }, [chatPanelOpen, bookId, refreshProgress, refreshHistory]);

  // 关闭时取消流
  useEffect(() => {
    if (!chatPanelOpen && abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
  }, [chatPanelOpen]);

  // 公共：消费 SSE 流
  const consumeSSE = useCallback(async (res: Response, ctrl: AbortController, onCardMeta?: (card: ActionCard, meta: any) => void) => {
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: `请求失败 (HTTP ${res.status})` }));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    let receivedSessionId = sessionId;
    for await (const evt of parseSSE(res)) {
      if (ctrl.signal.aborted) break;
      if (evt.type === 'delta') {
        streamBufferRef.current += evt.content;
        const buf = streamBufferRef.current;
        setMessages(prev => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === 'assistant') {
            next[next.length - 1] = { ...last, content: buf };
          }
          return next;
        });
      } else if (evt.type === 'card') {
        if (evt.session_id && !receivedSessionId) {
          receivedSessionId = evt.session_id;
          setSessionId(evt.session_id);
        }
        if (evt.meta && onCardMeta) {
          onCardMeta({ ...evt.card, status: 'pending' }, evt.meta);
        }
        setMessages(prev => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === 'assistant') {
            next[next.length - 1] = { ...last, cards: [...(last.cards || []), { ...evt.card, status: 'pending' }] };
          }
          return next;
        });
      } else if (evt.type === 'done') {
        if (evt.session_id) {
          setSessionId(evt.session_id);
          receivedSessionId = evt.session_id;
        }
      } else if (evt.type === 'error') {
        throw new Error(evt.error);
      }
    }
  }, [sessionId]);

  // 追加用户+AI占位消息
  const appendUserAi = useCallback((userText: string) => {
    setMessages(prev => [...prev,
      { role: 'user', content: userText },
      { role: 'assistant', content: '', cards: [] },
    ]);
  }, []);

  // 移除空AI占位
  const removeEmptyAi = useCallback(() => {
    setMessages(prev => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.role === 'assistant' && !last.content && !(last.cards || []).length) {
        next.pop();
      }
      return next;
    });
  }, []);

  // ========== 设定Tab：人机协作流 ==========

  // 1. 提需求 → AI给多选意见
  const handleSuggest = useCallback(async () => {
    const text = input.trim();
    if (!bookId || !selectedDim || streaming) return;
    setInput('');
    setStreamError('');
    setSuggestions([]);
    setLoadingSuggest(true);
    appendUserAi(`【${dimensions.find(d => d.key === selectedDim)?.label || selectedDim}】${text}`);
    try {
      const r = await api.smartSuggest(bookId, selectedDim, text, selectedSkillPacks);
      setSuggestions(r.suggestions || []);
      // 在AI消息位展示方案列表
      setMessages(prev => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === 'assistant') {
          next[next.length - 1] = {
            ...last,
            content: `已为你生成 ${r.suggestions.length} 个「${r.dimension_label}」方案，请选择一个（点击下方方案）：`,
          };
        }
        return next;
      });
    } catch (e: any) {
      setStreamError(e.message || '生成方案失败');
      removeEmptyAi();
    } finally {
      setLoadingSuggest(false);
    }
  }, [input, bookId, selectedDim, streaming, selectedSkillPacks, dimensions, appendUserAi, removeEmptyAi]);

  // 2. 选中意见 → 流式生成最终内容
  const handleGenerate = useCallback(async (suggestion: { id: string; title: string; preview: string }) => {
    if (!bookId || !selectedDim || streaming) return;
    setStreamError('');
    setSuggestions([]);
    streamBufferRef.current = '';
    appendUserAi(`选中方案：${suggestion.title} — ${suggestion.preview}`);
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await api.smartGenerateStream(bookId, selectedDim, suggestion.preview, '', selectedSkillPacks, sessionId || undefined, ctrl.signal);
      await consumeSSE(res, ctrl);
      refreshProgress();
      refreshHistory();
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        setStreamError(e.message || '生成失败');
        removeEmptyAi();
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [bookId, selectedDim, streaming, selectedSkillPacks, sessionId, dimensions, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

  // 3. 单独维度AI修改（基于已落地内容）
  const handleDimEdit = useCallback(async () => {
    const text = input.trim();
    if (!bookId || !selectedDim || streaming || !text) return;
    setInput('');
    setStreamError('');
    streamBufferRef.current = '';
    const dimLabel = dimensions.find(d => d.key === selectedDim)?.label || selectedDim;
    appendUserAi(`修订${dimLabel}：${text}`);
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await api.smartDimEditStream(bookId, selectedDim, '', text, selectedSkillPacks, sessionId || undefined, ctrl.signal);
      await consumeSSE(res, ctrl);
      refreshProgress();
      refreshHistory();
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        setStreamError(e.message || '修订失败');
        removeEmptyAi();
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [input, bookId, selectedDim, streaming, selectedSkillPacks, sessionId, dimensions, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

  // 4. 批量生成多维度
  const handleBatch = useCallback(async () => {
    if (!bookId || streaming) return;
    setStreamError('');
    streamBufferRef.current = '';
    const allDims = dimensions.map(d => d.key);
    appendUserAi(`批量生成全部 ${allDims.length} 个维度`);
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await api.smartBatchStream(bookId, allDims, input.trim(), selectedSkillPacks, sessionId || undefined, ctrl.signal);
      setInput('');
      await consumeSSE(res, ctrl);
      refreshProgress();
      refreshHistory();
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        setStreamError(e.message || '批量生成失败');
        removeEmptyAi();
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [bookId, streaming, dimensions, input, selectedSkillPacks, sessionId, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

  // ========== 正文Tab：续写/润色（自动定位最新章节）==========
  const triggerChapterAction = useCallback(async (action: 'continue' | 'polish') => {
    if (!bookId || streaming) return;
    setStreamError('');
    streamBufferRef.current = '';
    const label = action === 'continue' ? `续写第 ${nextChapterNum} 章` : `润色第 ${latestChapter?.order_index || nextChapterNum - 1} 章`;
    appendUserAi(label);
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await api.chatSmartAction(bookId, action, {
        target_chapter_num: action === 'continue' ? nextChapterNum : (latestChapter?.order_index || nextChapterNum - 1),
        session_id: sessionId || undefined,
      }, ctrl.signal);
      await consumeSSE(res, ctrl);
      refreshProgress();
      refreshHistory();
      // 刷新最新章节
      api.smartLatestChapter(bookId).then(r => {
        setLatestChapter(r.latest);
        setNextChapterNum(r.next_chapter_num);
      }).catch(() => {});
      api.smartChapters(bookId).then(r => setChapters(r.chapters || [])).catch(() => {});
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        setStreamError(e.message || `${label}失败`);
        removeEmptyAi();
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [bookId, streaming, nextChapterNum, latestChapter, sessionId, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

  // ========== 去AITab：对选中章节去AI味 ==========
  const handleDeai = useCallback(async () => {
    if (!bookId || !selectedChapterId || streaming) return;
    setStreamError('');
    streamBufferRef.current = '';
    const ch = chapters.find(c => c.id === selectedChapterId);
    appendUserAi(`去AI味：${ch?.title || '选中章节'}`);
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await api.smartDeaiStream(bookId, selectedChapterId, selectedSkillPacks, sessionId || undefined, ctrl.signal);
      await consumeSSE(res, ctrl, (card, meta) => {
        // 去AI味卡片落地时需要替换原章节，标记 meta
        (card as any).__meta = meta;
      });
      refreshHistory();
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        setStreamError(e.message || '去AI味失败');
        removeEmptyAi();
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [bookId, selectedChapterId, streaming, chapters, selectedSkillPacks, sessionId, appendUserAi, removeEmptyAi, consumeSSE, refreshHistory]);

  // ========== 校审Tab：防遗忘 / 一致性检查 ==========
  const handleReview = useCallback(async (mode: 'anti_forget' | 'consistency') => {
    if (!bookId || reviewing) return;
    setStreamError('');
    setReviewing(true);
    const label = mode === 'anti_forget' ? '防遗忘检查' : '一致性检查';
    appendUserAi(`执行${label}`);
    try {
      const r = await api.smartReview(bookId, mode, mode === 'consistency' ? (selectedChapterId || undefined) : undefined, selectedSkillPacks);
      setMessages(prev => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === 'assistant') {
          let reportText = '';
          if (mode === 'anti_forget' && r.report) {
            const rep = r.report;
            reportText = `## 防遗忘检查报告\n\n`;
            reportText += `**健康度评分：** ${rep.health_score ?? '未评'}\n\n`;
            if (rep.summary) reportText += `**摘要：** ${rep.summary}\n\n`;
            if (rep.violations && rep.violations.length) {
              reportText += `**违规问题（${rep.violations.length}）：**\n`;
              rep.violations.slice(0, 5).forEach((v: any, i: number) => {
                reportText += `${i + 1}. ${v.description || v.issue || JSON.stringify(v)}\n`;
              });
              reportText += '\n';
            }
            if (rep.pending_foreshadowing && rep.pending_foreshadowing.length) {
              reportText += `**待回收伏笔（${rep.pending_foreshadowing.length}）：**\n`;
              rep.pending_foreshadowing.slice(0, 5).forEach((f: any, i: number) => {
                reportText += `${i + 1}. ${f.description || f.title || JSON.stringify(f)}\n`;
              });
              reportText += '\n';
            }
            if (rep.suggestions && rep.suggestions.length) {
              reportText += `**改进建议：**\n`;
              rep.suggestions.slice(0, 3).forEach((s: any, i: number) => {
                reportText += `${i + 1}. ${typeof s === 'string' ? s : (s.description || s.suggestion || JSON.stringify(s))}\n`;
              });
            }
          } else if (mode === 'consistency') {
            reportText = `## 一致性检查报告\n\n`;
            reportText += `**章节：** ${r.chapter_title || '最新章节'}\n\n`;
            reportText += `**结果：** ${r.passed ? '✅ 通过' : '⚠️ 发现问题'}\n\n`;
            if (r.issues) reportText += `**问题详情：** ${r.issues}\n`;
          }
          next[next.length - 1] = { ...last, content: reportText || r.summary || `${label}完成` };
        }
        return next;
      });
      refreshHistory();
    } catch (e: any) {
      setStreamError(e.message || `${label}失败`);
      removeEmptyAi();
    } finally {
      setReviewing(false);
    }
  }, [bookId, reviewing, selectedChapterId, selectedSkillPacks, appendUserAi, removeEmptyAi, refreshHistory]);

  // ========== 卡片操作 ==========
  const handleAdopt = useCallback(async (card: ActionCard) => {
    if (!bookId) return;
    setApplyingCardId(card.id);
    try {
      const r = await api.applyChatCard(bookId, card);
      setMessages(prev => prev.map(m => {
        if (m.role !== 'assistant' || !m.cards) return m;
        return { ...m, cards: m.cards.map(c => c.id === card.id ? { ...c, status: 'adopted' as const } : c) };
      }));
      refreshProgress();
      if (card.type === 'SAVE_CHAPTER' && (r as any).chapter_id) {
        const ch = r as any;
        setStreamError('');
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `✅ 章节已保存：${ch.chapter_title}（${ch.word_count}字，第${ch.order_index}章）。可在「章节」Tab 查看。`,
        }]);
        // 刷新章节列表
        api.smartLatestChapter(bookId).then(rr => {
          setLatestChapter(rr.latest);
          setNextChapterNum(rr.next_chapter_num);
        }).catch(() => {});
        api.smartChapters(bookId).then(rr => setChapters(rr.chapters || [])).catch(() => {});
      }
    } catch (e: any) {
      setStreamError(e.message || '落地失败');
    } finally {
      setApplyingCardId(null);
    }
  }, [bookId, refreshProgress]);

  const handleEdit = useCallback(async (card: ActionCard, newContent: string) => {
    if (!bookId) return;
    setApplyingCardId(card.id);
    try {
      const editedCard = { ...card, content: newContent };
      const r = await api.applyChatCard(bookId, editedCard);
      setMessages(prev => prev.map(m => {
        if (m.role !== 'assistant' || !m.cards) return m;
        return { ...m, cards: m.cards.map(c => c.id === card.id ? { ...editedCard, status: 'edited' as const } : c) };
      }));
      refreshProgress();
      if (card.type === 'SAVE_CHAPTER' && (r as any).chapter_id) {
        const ch = r as any;
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `✅ 章节已保存：${ch.chapter_title}（${ch.word_count}字，第${ch.order_index}章）。`,
        }]);
        api.smartLatestChapter(bookId).then(rr => {
          setLatestChapter(rr.latest);
          setNextChapterNum(rr.next_chapter_num);
        }).catch(() => {});
        api.smartChapters(bookId).then(rr => setChapters(rr.chapters || [])).catch(() => {});
      }
    } catch (e: any) {
      setStreamError(e.message || '落地失败');
    } finally {
      setApplyingCardId(null);
    }
  }, [bookId, refreshProgress]);

  const handleIgnore = useCallback((card: ActionCard) => {
    setMessages(prev => prev.map(m => {
      if (m.role !== 'assistant' || !m.cards) return m;
      return { ...m, cards: m.cards.map(c => c.id === card.id ? { ...c, status: 'ignored' as const } : c) };
    }));
  }, []);

  // 去AI味卡片：替换原章节正文
  const handleReplaceChapter = useCallback(async (card: ActionCard, meta: any) => {
    if (!bookId || !meta?.chapter_id) return;
    setApplyingCardId(card.id);
    try {
      await api.smartChapterReplace(bookId, meta.chapter_id, card.content);
      setMessages(prev => prev.map(m => {
        if (m.role !== 'assistant' || !m.cards) return m;
        return { ...m, cards: m.cards.map(c => c.id === card.id ? { ...c, status: 'adopted' as const } : c) };
      }));
      setStreamError('');
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: `✅ 已用去AI味后的内容替换原章节正文（${card.content.length}字）。`,
      }]);
      api.smartChapters(bookId).then(r => setChapters(r.chapters || [])).catch(() => {});
    } catch (e: any) {
      setStreamError(e.message || '替换失败');
    } finally {
      setApplyingCardId(null);
    }
  }, [bookId]);

  // ========== 历史会话 ==========
  const handleSelectSession = useCallback(async (sid: string) => {
    if (streaming) stopStream();
    setSessionId(sid);
    setShowHistory(false);
    setStreamError('');
    setMessages([]);
    try {
      const r = await api.getChatSessionMessages(sid);
      const loaded: AIMessage[] = (r.messages || []).map((m: any) => ({
        role: m.role || 'assistant',
        content: m.content || '',
        cards: Array.isArray(m.cards) ? m.cards : undefined,
      }));
      setMessages(loaded);
    } catch (e: any) {
      setStreamError('加载历史会话失败：' + (e.message || ''));
    }
  }, [streaming]);

  const handleNewSession = useCallback(() => {
    if (streaming) stopStream();
    setMessages([]);
    setSessionId(null);
    setShowHistory(false);
    setStreamError('');
    setSuggestions([]);
  }, [streaming]);

  const stopStream = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setStreaming(false);
  }, []);

  // 技能包切换
  const toggleSkillPack = useCallback((id: string) => {
    setSelectedSkillPacks(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);

  // 输入框占位符
  const inputPlaceholder = (() => {
    if (activeTab === 'setting') {
      if (!selectedDim) return '请先选择上方维度按钮…';
      return `描述你对「${dimensions.find(d => d.key === selectedDim)?.label || selectedDim}」的需求或修改意见…`;
    }
    if (activeTab === 'chapter') return '补充本章写作要求（可选）…';
    if (activeTab === 'deai') return '点击上方「开始去AI味」按钮即可…';
    if (activeTab === 'review') return '点击上方检查按钮即可…';
    return '和 AI 智驾聊聊…';
  })();

  // 发送按钮是否可用
  const canSend = (() => {
    if (streaming || !input.trim()) return false;
    if (activeTab === 'setting') return !!selectedDim;
    return false;
  })();

  // 主发送动作（设定Tab：维度已有内容走dim-edit修订，否则走suggest生成多选意见）
  const handleMainSend = useCallback(() => {
    if (activeTab !== 'setting' || !selectedDim) return;
    if (suggestions.length > 0) return;
    const dimStatus = progress?.dims.find(d => d.field === selectedDim)?.status;
    if (dimStatus && dimStatus !== 'empty') {
      handleDimEdit();
    } else {
      handleSuggest();
    }
  }, [activeTab, selectedDim, suggestions, progress, handleSuggest, handleDimEdit]);

  // 设定Tab：选择维度后，第一次输入走 suggest（生成多选意见）
  // 选中意见后走 generate（流式生成）
  // 生成落地后，再输入走 dim-edit（修订）
  // 这里统一：如果有 suggestions 在显示，输入框禁用（只能选方案）
  // 否则，输入框 enter 触发 handleMainSend（根据维度是否已有内容自动选择 suggest/dim-edit）
  const onInputKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (activeTab === 'setting' && selectedDim && suggestions.length === 0) {
        handleMainSend();
      }
    }
  };

  const TABS: Array<{ key: SmartTab; label: string; icon: string }> = [
    { key: 'setting', label: '设定', icon: '⚙️' },
    { key: 'chapter', label: '正文', icon: '✍️' },
    { key: 'deai', label: '去AI', icon: '🧹' },
    { key: 'review', label: '校审', icon: '🔍' },
  ];

  return (
    <>
      <FloatingButton hidden={chatPanelOpen} onOpen={(bid) => openChatPanel(bid)} />

      {chatPanelOpen && bookId && (
        <div className="chat-panel-overlay">
          <div className="chat-panel smart-panel">
            {/* 头部 */}
            <div className="chat-panel-header">
              <div className="chat-panel-title">
                <span className="chat-panel-logo">🚗</span>
                <div>
                  <div className="chat-panel-name">AI 智驾</div>
                  <div className="chat-panel-sub">设定 · 正文 · 去AI · 校审</div>
                </div>
              </div>
              <div className="chat-panel-tools">
                <button className="chat-tool-btn" onClick={() => { setShowProgress(s => !s); }} title="创作进度">🗺️</button>
                <button className="chat-tool-btn" onClick={() => { setShowHistory(s => !s); refreshHistory(); }} title="历史会话">🕘</button>
                <button className="chat-tool-btn" onClick={handleNewSession} title="新会话">✨</button>
                <button className="chat-tool-btn close" onClick={closeChatPanel} title="关闭">✕</button>
              </div>
            </div>

            {/* 进度地图浮层 */}
            {showProgress && (
              <div className="chat-panel-side">
                <ProgressMapView progress={progress} onClose={() => setShowProgress(false)} />
              </div>
            )}

            {/* 历史会话浮层 */}
            {showHistory && (
              <div className="chat-panel-side">
                <div className="chat-history">
                  <div className="chat-history-head">
                    <span>历史会话</span>
                    <button className="chat-history-close" onClick={() => setShowHistory(false)}>×</button>
                  </div>
                  {historySessions.length === 0 ? (
                    <div className="chat-history-empty">还没有历史会话</div>
                  ) : (
                    historySessions.map(s => (
                      <button key={s.id} className={`chat-history-item ${s.id === sessionId ? 'active' : ''}`} onClick={() => handleSelectSession(s.id)}>
                        <div className="chat-history-title">{s.title || '未命名'}</div>
                        <div className="chat-history-meta">{s.message_count} 条 · {s.updated_at ? new Date(s.updated_at).toLocaleString() : ''}</div>
                      </button>
                    ))
                  )}
                </div>
              </div>
            )}

            {/* Tab 工具区（根据当前Tab显示不同工具） */}
            <div className="smart-toolbar">
              {activeTab === 'setting' && (
                <>
                  {/* 维度子按钮栏 */}
                  <div className="smart-dim-bar">
                    <button
                      className={`smart-dim-btn batch ${selectedDim === null && suggestions.length === 0 ? 'active' : ''}`}
                      onClick={handleBatch}
                      disabled={streaming || loadingSuggest}
                      title="一次性生成全部维度"
                    >⚡ 批量</button>
                    {dimensions.map(d => (
                      <button
                        key={d.key}
                        className={`smart-dim-btn ${selectedDim === d.key ? 'active' : ''}`}
                        onClick={() => { setSelectedDim(d.key); setSuggestions([]); setInput(''); }}
                        disabled={streaming || loadingSuggest}
                        title={d.hint}
                      >{d.icon} {d.label}</button>
                    ))}
                  </div>
                  <SkillPackSelector packs={skillPacks.filter(p => p.category === 'master')} selected={selectedSkillPacks} onToggle={toggleSkillPack} compact />
                </>
              )}

              {activeTab === 'chapter' && (
                <>
                  <div className="smart-chapter-info">
                    {latestChapter ? (
                      <span>📖 最新章节：<strong>{latestChapter.title}</strong>（{latestChapter.word_count}字，第{latestChapter.order_index}章）</span>
                    ) : (
                      <span>📖 还没有章节，将创建第 1 章</span>
                    )}
                  </div>
                  <div className="smart-chapter-actions">
                    <button
                      className="smart-action-btn primary"
                      onClick={() => triggerChapterAction('continue')}
                      disabled={streaming}
                    >✍️ 续写第 {nextChapterNum} 章</button>
                    {latestChapter && (
                      <button
                        className="smart-action-btn"
                        onClick={() => triggerChapterAction('polish')}
                        disabled={streaming}
                      >✨ 润色第 {latestChapter.order_index} 章</button>
                    )}
                  </div>
                  <SkillPackSelector packs={skillPacks.filter(p => p.category === 'style' || p.category === 'master')} selected={selectedSkillPacks} onToggle={toggleSkillPack} compact />
                </>
              )}

              {activeTab === 'deai' && (
                <>
                  <div className="smart-chapter-select">
                    <label>选择去AI味的章节：</label>
                    {chapters.length === 0 ? (
                      <span className="smart-empty-hint">暂无章节</span>
                    ) : (
                      <select
                        value={selectedChapterId || ''}
                        onChange={e => setSelectedChapterId(e.target.value)}
                        disabled={streaming}
                      >
                        <option value="">请选择章节…</option>
                        {chapters.map(c => (
                          <option key={c.id} value={c.id}>第{c.order_index}章 {c.title}（{c.word_count}字）</option>
                        ))}
                      </select>
                    )}
                  </div>
                  <SkillPackSelector packs={skillPacks.filter(p => p.category === 'review')} selected={selectedSkillPacks} onToggle={toggleSkillPack} compact />
                  {deaiPacks.length > 0 && selectedSkillPacks.length === 0 && (
                    <div className="smart-deai-hint">💡 检测到 {deaiPacks.length} 个去AI味技能包，可在上方勾选，未选将使用默认去AI味规则</div>
                  )}
                </>
              )}

              {activeTab === 'review' && (
                <>
                  <div className="smart-review-actions">
                    <button
                      className="smart-action-btn primary"
                      onClick={() => handleReview('anti_forget')}
                      disabled={reviewing || streaming}
                    >🔍 防遗忘检查</button>
                    <button
                      className="smart-action-btn"
                      onClick={() => handleReview('consistency')}
                      disabled={reviewing || streaming}
                    >⚖️ 一致性检查</button>
                  </div>
                  {activeTab === 'review' && (
                    <div className="smart-chapter-select">
                      <label>一致性检查章节（不选则检查最新）：</label>
                      {chapters.length > 0 && (
                        <select
                          value={selectedChapterId || ''}
                          onChange={e => setSelectedChapterId(e.target.value)}
                          disabled={reviewing || streaming}
                        >
                          <option value="">最新章节</option>
                          {chapters.map(c => (
                            <option key={c.id} value={c.id}>第{c.order_index}章 {c.title}</option>
                          ))}
                        </select>
                      )}
                    </div>
                  )}
                  <SkillPackSelector packs={skillPacks.filter(p => p.category === 'review')} selected={selectedSkillPacks} onToggle={toggleSkillPack} compact />
                </>
              )}
            </div>

            {/* 多选意见列表（设定Tab专属） */}
            {activeTab === 'setting' && suggestions.length > 0 && (
              <div className="smart-suggestions">
                <div className="smart-suggestions-head">请选择一个方案，AI 将基于它生成完整内容：</div>
                {suggestions.map(s => (
                  <button
                    key={s.id}
                    className="smart-suggestion-item"
                    onClick={() => handleGenerate(s)}
                    disabled={streaming}
                  >
                    <div className="smart-suggestion-title">{s.title}</div>
                    <div className="smart-suggestion-preview">{s.preview}</div>
                  </button>
                ))}
                <button className="smart-suggestion-cancel" onClick={() => setSuggestions([])}>取消，重新描述需求</button>
              </div>
            )}

            {/* 消息列表 */}
            <div className="chat-messages" ref={scrollRef}>
              {messages.length === 0 && !loadingSuggest && (
                <div className="chat-empty">
                  <div className="chat-empty-icon">🚗</div>
                  <p>AI 智驾已就绪。选择上方维度或操作，开始人机协作创作。</p>
                  {progress?.next_step && (
                    <div className="chat-empty-hint">
                      建议从 <strong>{progress.next_step.label}</strong> 开始：{progress.next_step.hint}
                    </div>
                  )}
                </div>
              )}
              {loadingSuggest && (
                <div className="chat-empty">
                  <div className="chat-empty-icon">⏳</div>
                  <p>AI 正在生成多选方案…</p>
                </div>
              )}
              {messages.map((m, i) => (
                <MessageBubble
                  key={i}
                  message={m}
                  onAdopt={handleAdopt}
                  onEdit={handleEdit}
                  onIgnore={handleIgnore}
                  applyingCardId={applyingCardId}
                  streaming={streaming && i === messages.length - 1 && m.role === 'assistant'}
                  onReplaceChapter={handleReplaceChapter}
                />
              ))}
              {streamError && <div className="chat-error">{streamError}</div>}
            </div>

            {/* 去AI/校审Tab的主操作按钮（在输入框上方） */}
            {activeTab === 'deai' && (
              <div className="smart-main-action-bar">
                <button
                  className="smart-main-action"
                  onClick={handleDeai}
                  disabled={streaming || !selectedChapterId}
                >{streaming ? '处理中…' : '🧹 开始去AI味'}</button>
                {!selectedChapterId && <span className="smart-main-hint">请先选择章节</span>}
              </div>
            )}

            {/* 输入区（仅设定Tab可输入；其他Tab通过按钮操作） */}
            {activeTab === 'setting' && (
              <div className="chat-input-area">
                <div className="chat-input-row">
                  <textarea
                    ref={inputRef}
                    className="chat-input"
                    value={input}
                    onChange={e => setInput(e.target.value)}
                    onKeyDown={onInputKeyDown}
                    placeholder={inputPlaceholder}
                    rows={1}
                    disabled={streaming || loadingSuggest || suggestions.length > 0 || !selectedDim}
                  />
                  {streaming ? (
                    <button className="chat-send stop" onClick={stopStream}>停止</button>
                  ) : (
                    <button
                      className="chat-send"
                      onClick={handleMainSend}
                      disabled={!canSend || loadingSuggest || suggestions.length > 0}
                    >{loadingSuggest ? '…' : '生成方案'}</button>
                  )}
                </div>
                {selectedDim && suggestions.length === 0 && (
                  <div className="chat-input-hint">描述需求 → AI 给多选方案 → 选中生成 → 可输入修改意见重新生成</div>
                )}
              </div>
            )}

            {activeTab !== 'setting' && (
              <div className="chat-input-area">
                <div className="chat-input-row">
                  <input
                    className="chat-input"
                    value={input}
                    onChange={e => setInput(e.target.value)}
                    placeholder={inputPlaceholder}
                    disabled={streaming}
                  />
                  {streaming ? (
                    <button className="chat-send stop" onClick={stopStream}>停止</button>
                  ) : null}
                </div>
              </div>
            )}

            {/* 底部 Tab 栏（手机友好） */}
            <div className="smart-tab-bar">
              {TABS.map(t => (
                <button
                  key={t.key}
                  className={`smart-tab ${activeTab === t.key ? 'active' : ''}`}
                  onClick={() => setActiveTab(t.key)}
                >
                  <span className="smart-tab-icon">{t.icon}</span>
                  <span className="smart-tab-label">{t.label}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

// ============================================================================
// FAB 悬浮按钮（仅在 /write?book=xxx 路由下显示）
// ============================================================================
function FloatingButton({ onOpen, hidden }: { onOpen: (bookId: string) => void; hidden: boolean }) {
  const [bookId, setBookId] = useState<string | null>(null);
  useEffect(() => {
    const check = () => {
      const hash = window.location.hash;
      if (!hash.startsWith('#/write')) { setBookId(null); return; }
      const qIdx = hash.indexOf('?');
      if (qIdx < 0) { setBookId(null); return; }
      const params = new URLSearchParams(hash.slice(qIdx + 1));
      setBookId(params.get('book'));
    };
    check();
    window.addEventListener('hashchange', check);
    return () => window.removeEventListener('hashchange', check);
  }, []);
  if (!bookId || hidden) return null;
  return (
    <button className="chat-fab" onClick={() => onOpen(bookId)} title="打开 AI 智驾">
      <span>🚗</span>
      <span className="chat-fab-label">AI 智驾</span>
    </button>
  );
}
