import { useState, useEffect, useRef, useCallback, memo } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import type { ActionCard, ProgressMap, AIMessage, SkillPack } from '../types';
import CarLogo from './CarLogo';

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
// 消息气泡（长按菜单 + 超两行折叠）
// ============================================================================
interface MessageBubbleProps {
  message: AIMessage;
  index: number;
  onAdopt: (c: ActionCard) => void;
  onEdit: (c: ActionCard, content: string) => void;
  onIgnore: (c: ActionCard) => void;
  applyingCardId: string | null;
  streaming: boolean;
  onReplaceChapter?: (card: ActionCard, meta: any) => void;
  onEditMessage?: (index: number, newContent: string) => void;
  onDeleteMessage?: (index: number) => void;
  onRegenerate?: (index: number) => void;
}

// 长按计时器
const LONG_PRESS_MS = 500;

const MessageBubble = memo(function MessageBubble({ message, index, onAdopt, onEdit, onIgnore, applyingCardId, streaming, onReplaceChapter, onEditMessage, onDeleteMessage, onRegenerate }: MessageBubbleProps) {
  const isUser = message.role === 'user';
  const [collapsed, setCollapsed] = useState(true);
  const [showMenu, setShowMenu] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content);
  const [copied, setCopied] = useState(false);
  const pressTimer = useRef<number | null>(null);
  const movedRef = useRef(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const copyTimer = useRef<number | null>(null);

  // 判断是否需要折叠：内容超过两行（约80字或多行）
  const contentLines = (message.content || '').split('\n');
  const isLong = message.content.length > 80 || contentLines.length > 2;
  const showCollapsed = isLong && collapsed && !streaming;

  // 长按开始
  const startPress = () => {
    movedRef.current = false;
    if (pressTimer.current) window.clearTimeout(pressTimer.current);
    pressTimer.current = window.setTimeout(() => {
      if (!movedRef.current) setShowMenu(true);
    }, LONG_PRESS_MS);
  };
  const cancelPress = () => {
    if (pressTimer.current) { window.clearTimeout(pressTimer.current); pressTimer.current = null; }
  };
  const onMove = () => { movedRef.current = true; cancelPress(); };

  // 点击外部关闭菜单
  useEffect(() => {
    if (!showMenu) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setShowMenu(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [showMenu]);

  // 卸载时清理复制提示计时器
  useEffect(() => {
    return () => { if (copyTimer.current) window.clearTimeout(copyTimer.current); };
  }, []);

  const handleSaveEdit = () => {
    if (onEditMessage && draft.trim() !== message.content) {
      onEditMessage(index, draft.trim());
    }
    setEditing(false);
  };

  // 复制消息内容到剪贴板
  const handleCopy = async () => {
    const text = (message.content || '').trim();
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // 降级方案：用 textarea + execCommand
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch { /* ignore */ }
      document.body.removeChild(ta);
    }
    setCopied(true);
    if (copyTimer.current) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div
      className={`chat-msg ${isUser ? 'chat-msg-user' : 'chat-msg-ai'}`}
      onTouchStart={startPress}
      onTouchEnd={cancelPress}
      onTouchMove={onMove}
      onMouseDown={startPress}
      onMouseUp={cancelPress}
      onMouseLeave={cancelPress}
      onMouseMove={onMove}
      style={{ position: 'relative' }}
    >
      <div className="chat-msg-avatar">{isUser ? '我' : <CarLogo size={22} />}</div>
      <div className="chat-msg-body">
        {editing ? (
          <div className="chat-msg-edit-wrap">
            <textarea
              className="chat-msg-edit"
              value={draft}
              onChange={e => setDraft(e.target.value)}
              rows={Math.min(12, Math.max(3, draft.split('\n').length))}
              autoFocus
            />
            <div className="chat-msg-edit-actions">
              <button className="chat-card-btn primary" onClick={handleSaveEdit}>保存</button>
              <button className="chat-card-btn" onClick={() => { setEditing(false); setDraft(message.content); }}>取消</button>
            </div>
          </div>
        ) : message.content ? (
          <div
            className={`chat-msg-text ${showCollapsed ? 'chat-msg-collapsed' : ''}`}
            onClick={() => { if (isLong && !streaming) setCollapsed(c => !c); }}
          >
            {message.content}{streaming && <span className="chat-cursor">▋</span>}
          </div>
        ) : streaming ? (
          <div className="chat-msg-text"><span className="chat-cursor">▋</span></div>
        ) : null}
        {showCollapsed && <button className="chat-msg-expand" onClick={(e) => { e.stopPropagation(); setCollapsed(false); }}>展开全文 ▼</button>}
        {/* 消息操作栏：复制 / 重新生成 / 删除（流式生成中隐藏） */}
        {!streaming && !editing && (message.content || '') && (
          <div className={`chat-msg-actions ${isUser ? 'chat-msg-actions-user' : ''}`}>
            <button
              className="chat-msg-action-btn"
              onClick={handleCopy}
              title="复制"
            >{copied ? '✓ 已复制' : '📋 复制'}</button>
            {!isUser && onRegenerate && (
              <button
                className="chat-msg-action-btn"
                onClick={() => onRegenerate(index)}
                title="重新生成"
              >🔄 重新生成</button>
            )}
            {isUser && onEditMessage && (
              <button
                className="chat-msg-action-btn"
                onClick={() => setEditing(true)}
                title="编辑"
              >✏️ 编辑</button>
            )}
            {onDeleteMessage && (
              <button
                className="chat-msg-action-btn danger"
                onClick={() => {
                  if (window.confirm('确定删除这条消息？')) onDeleteMessage(index);
                }}
                title="删除"
              >🗑️ 删除</button>
            )}
          </div>
        )}
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
      {showMenu && (
        <div className="chat-msg-menu" ref={menuRef}>
          <button className="chat-msg-menu-item" onClick={() => { handleCopy(); setShowMenu(false); }}>📋 复制</button>
          {isUser && onEditMessage && <button className="chat-msg-menu-item" onClick={() => { setEditing(true); setShowMenu(false); }}>✏️ 编辑</button>}
          {!isUser && onRegenerate && <button className="chat-msg-menu-item" onClick={() => { onRegenerate(index); setShowMenu(false); }}>🔄 重新生成</button>}
          {onDeleteMessage && <button className="chat-msg-menu-item danger" onClick={() => { if (window.confirm('确定删除这条消息？')) onDeleteMessage(index); setShowMenu(false); }}>🗑️ 删除</button>}
        </div>
      )}
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
  const { chatPanelOpen, chatPanelBookId, chatPanelSessionId, closeChatPanel } = useStore() as any;
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
  // 各 Tab 独立的技能包选择（切换 Tab 互不干扰）
  const [settingPacks, setSettingPacks] = useState<string[]>([]);
  const [chapterPacks, setChapterPacks] = useState<string[]>([]);
  const [deaiPacks_selected, setDeaiPacksSelected] = useState<string[]>([]);
  const [reviewPacks, setReviewPacks] = useState<string[]>([]);
  const [latestChapter, setLatestChapter] = useState<{ id: string; title: string; order_index: number; word_count: number; status: string } | null>(null);
  const [nextChapterNum, setNextChapterNum] = useState(1);
  const [chapters, setChapters] = useState<Array<{ id: string; title: string; order_index: number; word_count: number; status: string }>>([]);
  const [volumes, setVolumes] = useState<Array<{ id: string; title: string; order_index: number; chapter_count: number }>>([]);
  // 正文/去AI/校审 各自的章节选择（独立）
  const [deaiTargetId, setDeaiTargetId] = useState<string | null>(null);         // 去AITab：去味目标章节
  const [reviewChapterId, setReviewChapterId] = useState<string | null>(null);   // 校审Tab：一致性检查章节
  const [reviewVolumeIds, setReviewVolumeIds] = useState<string[]>([]);          // 校审Tab：按卷检查
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

  // 打开时加载基础数据 + 自动加载指定/最新一次聊天会话
  // 若 store 中有 chatPanelSessionId（从历史对话「继续」按钮传入），优先加载该会话；否则加载最新
  useEffect(() => {
    if (chatPanelOpen && bookId) {
      refreshProgress();
      refreshHistory();
      api.smartDimensions().then(r => setDimensions(r.dimensions || [])).catch(() => {});
      api.listSkillPacks().then(all => setSkillPacks(all || [])).catch(() => {});
      api.smartDeaiPacks().then(r => setDeaiPacks(r.packs || [])).catch(() => {});
      api.smartChapters(bookId).then(r => setChapters(r.chapters || [])).catch(() => {});
      api.smartVolumes(bookId).then(r => setVolumes(r.volumes || [])).catch(() => {});
      api.smartLatestChapter(bookId).then(r => {
        setLatestChapter(r.latest);
        setNextChapterNum(r.next_chapter_num);
      }).catch(() => {});
      // 加载会话：优先 chatPanelSessionId 指定的，否则最新
      (async () => {
        try {
          const r = await api.listBookChatSessions(bookId);
          const sessions = r.sessions || [];
          let target = null;
          if (chatPanelSessionId) {
            target = sessions.find((s: any) => s.id === chatPanelSessionId) || null;
          }
          if (!target && sessions.length > 0) {
            target = sessions[0];
          }
          if (target) {
            setSessionId(target.id);
            const msgR = await api.getChatSessionMessages(target.id);
            const loaded: AIMessage[] = (msgR.messages || []).map((m: any) => ({
              role: m.role || 'assistant',
              content: m.content || '',
              cards: Array.isArray(m.cards) ? m.cards : undefined,
            }));
            setMessages(loaded);
          } else {
            setMessages([]);
            setSessionId(null);
          }
        } catch {
          setMessages([]);
        }
      })();
    }
  }, [chatPanelOpen, bookId, chatPanelSessionId, refreshProgress, refreshHistory]);

  // 切换 Tab 时清空选中章节（各Tab独立）
  const switchTab = useCallback((tab: SmartTab) => {
    setActiveTab(tab);
  }, []);

  // 各 Tab 独立的技能包切换
  const toggleSkillPack = useCallback((tab: SmartTab, id: string) => {
    const setter = tab === 'setting' ? setSettingPacks : tab === 'chapter' ? setChapterPacks : tab === 'deai' ? setDeaiPacksSelected : setReviewPacks;
    setter(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);

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
      const r = await api.smartSuggest(bookId, selectedDim, text, settingPacks);
      setSuggestions(r.suggestions || []);
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
  }, [input, bookId, selectedDim, streaming, settingPacks, dimensions, appendUserAi, removeEmptyAi]);

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
      const res = await api.smartGenerateStream(bookId, selectedDim, suggestion.preview, '', settingPacks, sessionId || undefined, ctrl.signal);
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
  }, [bookId, selectedDim, streaming, settingPacks, sessionId, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

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
      const res = await api.smartDimEditStream(bookId, selectedDim, '', text, settingPacks, sessionId || undefined, ctrl.signal);
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
  }, [input, bookId, selectedDim, streaming, settingPacks, sessionId, dimensions, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

  // ========== 正文Tab：写作/修改（结合消息栏输入的要求创作）==========
  // 真正执行章节写作/修改（结合用户在消息栏输入的要求）
  const doChapterAction = useCallback(async (action: 'continue' | 'polish', targetChapterId: string | null, instruction?: string) => {
    if (!bookId || streaming) return;
    setStreamError('');
    streamBufferRef.current = '';
    const targetNum = action === 'continue'
      ? nextChapterNum
      : (chapters.find(c => c.id === targetChapterId)?.order_index || latestChapter?.order_index || nextChapterNum - 1);
    const label = action === 'continue' ? `写作第 ${nextChapterNum} 章` : `修改第 ${targetNum} 章`;
    const userNote = (instruction || input.trim());
    appendUserAi(userNote ? `${label}（${userNote.slice(0, 60)}）` : label);
    if (userNote) setInput('');
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await api.chatSmartAction(bookId, action, {
        target_chapter_num: targetNum,
        session_id: sessionId || undefined,
        instruction: userNote || undefined,
      }, ctrl.signal);
      await consumeSSE(res, ctrl);
      refreshProgress();
      refreshHistory();
      // 刷新最新章节 + 章节列表（写后会自动填入新章节到目录）
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
  }, [bookId, streaming, nextChapterNum, chapters, latestChapter, sessionId, input, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

  // 带明确意见执行修改（从消息栏解析章节号后调用）
  const doChapterActionWithNote = useCallback((action: 'continue' | 'polish', chapterId: string, note: string) => {
    doChapterAction(action, chapterId, note);
  }, [doChapterAction]);

  // 正文Tab统一发送：直接从消息框输入，智能判断修改/续写，无需先点按钮切换模式
  const handleChapterSend = useCallback(() => {
    if (!bookId || streaming) return;
    const text = input.trim();
    if (!text) {
      // 无输入时直接续写下一章
      doChapterAction('continue', null);
      return;
    }
    // 智能判断：包含"第X章"或修改关键词 → 修改模式；否则 → 续写模式（输入作为写作要求）
    const hasChapterNum = /第\s*\d+\s*章/.test(text);
    const hasModifyKeyword = /修改|润色|改一?下|调整|增加|删掉|删除|替换|优化|扩充|精简/.test(text);
    if ((hasChapterNum || hasModifyKeyword) && chapters.length > 0) {
      const numMatch = text.match(/第?\s*(\d+)\s*章/);
      const targetNum = numMatch ? parseInt(numMatch[1]) : (latestChapter?.order_index || 1);
      const ch = chapters.find(c => c.order_index === targetNum);
      if (ch) {
        doChapterActionWithNote('polish', ch.id, text);
      } else {
        setStreamError(`未找到第 ${targetNum} 章，请检查章节号或直接输入写作要求`);
      }
    } else {
      // 续写：输入内容作为本章写作要求
      doChapterAction('continue', null, text);
    }
  }, [bookId, streaming, input, chapters, latestChapter, doChapterAction, doChapterActionWithNote]);

  // 章节点位刷新🔄：手动重新拉取最新章节和章节列表
  const refreshChapterAnchor = useCallback(() => {
    if (!bookId) return;
    api.smartLatestChapter(bookId).then(r => {
      setLatestChapter(r.latest);
      setNextChapterNum(r.next_chapter_num);
    }).catch(() => {});
    api.smartChapters(bookId).then(r => setChapters(r.chapters || [])).catch(() => {});
  }, [bookId]);

  // ========== 去AITab：对选中章节去AI味 ==========
  const handleDeai = useCallback(async () => {
    if (!bookId || streaming) return;
    // 优先使用下拉选中的章节；未选则从消息框解析章节号
    let targetId = deaiTargetId;
    let note = '';
    if (!targetId) {
      const text = input.trim();
      if (text) {
        const numMatch = text.match(/第?\s*(\d+)\s*章/);
        if (numMatch) {
          const targetNum = parseInt(numMatch[1]);
          const ch = chapters.find(c => c.order_index === targetNum);
          if (ch) {
            targetId = ch.id;
            note = text;
          } else {
            setStreamError(`未找到第 ${targetNum} 章，请检查章节号`);
            return;
          }
        } else {
          setStreamError('请先选择章节，或在输入框输入「第N章」指定');
          return;
        }
      } else {
        setStreamError('请先选择章节，或在输入框输入「第N章」指定');
        return;
      }
    }
    setStreamError('');
    streamBufferRef.current = '';
    const ch = chapters.find(c => c.id === targetId);
    appendUserAi(`去AI味：${ch?.title || '选中章节'}${note ? `（${note.slice(0, 40)}）` : ''}`);
    if (note) setInput('');
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await api.smartDeaiStream(bookId, targetId, deaiPacks_selected, sessionId || undefined, ctrl.signal);
      await consumeSSE(res, ctrl, (card, meta) => {
        (card as any).__meta = meta;
      });
      refreshHistory();
      api.smartChapters(bookId).then(r => setChapters(r.chapters || [])).catch(() => {});
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        setStreamError(e.message || '去AI味失败');
        removeEmptyAi();
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [bookId, deaiTargetId, streaming, chapters, deaiPacks_selected, sessionId, input, appendUserAi, removeEmptyAi, consumeSSE, refreshHistory]);

  // ========== 校审Tab：防遗忘 / 一致性检查（按卷，拉取动态文件+伏笔）==========
  const handleReview = useCallback(async (mode: 'anti_forget' | 'consistency') => {
    if (!bookId || reviewing) return;
    setStreamError('');
    setReviewing(true);
    const label = mode === 'anti_forget' ? '防遗忘检查' : '一致性检查';
    const volLabel = reviewVolumeIds.length ? `（按卷：${reviewVolumeIds.length}卷）` : '（全书）';
    appendUserAi(`执行${label}${volLabel}`);
    try {
      const r = await api.smartReview(
        bookId, mode,
        mode === 'consistency' ? (reviewChapterId || undefined) : undefined,
        reviewPacks,
        reviewVolumeIds
      );
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
  }, [bookId, reviewing, reviewChapterId, reviewVolumeIds, reviewPacks, appendUserAi, removeEmptyAi, refreshHistory]);

  // ========== 消息长按操作：编辑/删除/重新生成 ==========
  const handleEditMessage = useCallback((index: number, newContent: string) => {
    setMessages(prev => prev.map((m, i) => i === index ? { ...m, content: newContent } : m));
  }, []);

  const handleDeleteMessage = useCallback((index: number) => {
    setMessages(prev => prev.filter((_, i) => i !== index));
  }, []);

  const handleRegenerate = useCallback((index: number) => {
    // 重新生成：找到该AI消息前最近的用户消息，重新触发对应动作
    setMessages(prev => {
      // 删除该AI消息及之后的，保留之前的用户消息
      const before = prev.slice(0, index);
      const userMsg = [...before].reverse().find(m => m.role === 'user');
      setMessages(before);
      if (userMsg && activeTab === 'setting' && selectedDim) {
        // 设定Tab：用用户消息内容重新触发
        const text = userMsg.content.replace(/^【[^】]+】/, '').trim();
        if (text) {
          setInput(text);
          setTimeout(() => handleSuggest(), 50);
        }
      }
      return before;
    });
  }, [activeTab, selectedDim, handleSuggest]);

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

  // 输入框占位符
  const inputPlaceholder = (() => {
    if (activeTab === 'setting') {
      if (!selectedDim) return '请先选择上方维度按钮…';
      if (selectedDim === 'general') return '和 AI 智驾自由讨论小说/剧情…（提及人物/伏笔/世界观等会自动产出卡片）';
      return `描述你对「${dimensions.find(d => d.key === selectedDim)?.label || selectedDim}」的需求或修改意见…`;
    }
    if (activeTab === 'chapter') return '输入写作要求直接续写，或输入「第3章 增加心理描写」修改已写章节…';
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

  // 通用聊天：自由讨论，关键词触发填入维度
  const handleGeneral = useCallback(async () => {
    const text = input.trim();
    if (!bookId || !text || streaming) return;
    setInput('');
    setStreamError('');
    streamBufferRef.current = '';
    appendUserAi(text);
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await api.smartGeneralStream(bookId, text, settingPacks, sessionId || undefined, ctrl.signal);
      await consumeSSE(res, ctrl);
      refreshProgress();
      refreshHistory();
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        setStreamError(e.message || '通用聊天失败');
        removeEmptyAi();
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [input, bookId, streaming, settingPacks, sessionId, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

  // 主发送动作（设定Tab：通用走general，维度已有内容走dim-edit，否则走suggest）
  const handleMainSend = useCallback(() => {
    if (activeTab !== 'setting' || !selectedDim) return;
    if (suggestions.length > 0) return;
    if (selectedDim === 'general') {
      handleGeneral();
      return;
    }
    const dimStatus = progress?.dims.find(d => d.field === selectedDim)?.status;
    if (dimStatus && dimStatus !== 'empty') {
      handleDimEdit();
    } else {
      handleSuggest();
    }
  }, [activeTab, selectedDim, suggestions, progress, handleSuggest, handleDimEdit, handleGeneral]);

  // 设定Tab：选择维度后，第一次输入走 suggest（生成多选意见）
  // 选中意见后走 generate（流式生成）
  // 生成落地后，再输入走 dim-edit（修订）
  // 通用模式：直接走 general（流式聊天）
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
      {chatPanelOpen && bookId && (
        <div className="chat-panel-overlay">
          <div className="chat-panel smart-panel">
            {/* 头部（紧凑） */}
            <div className="chat-panel-header chat-panel-header-compact">
              <div className="chat-panel-title">
                <span className="chat-panel-logo"><CarLogo size={20} /></span>
                <div className="chat-panel-name">AI 智驾</div>
              </div>
              <div className="chat-panel-tools">
                <button className="chat-tool-btn" onClick={() => { setShowProgress(s => !s); }} title="创作进度">🗺️<span className="chat-tool-label">创作进度</span></button>
                <button className="chat-tool-btn" onClick={() => { setShowHistory(s => !s); refreshHistory(); }} title="历史会话">🕘<span className="chat-tool-label">历史会话</span></button>
                <button className="chat-tool-btn" onClick={handleNewSession} title="新会话">✨<span className="chat-tool-label">新会话</span></button>
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
                  {/* 维度子按钮栏：两行（通用/构思/设定/世界观 + 大纲/剧情/人物/伏笔） */}
                  <div className="smart-dim-rows">
                    <div className="smart-dim-row">
                      <button
                        className={`smart-dim-btn ${selectedDim === 'general' ? 'active' : ''}`}
                        onClick={() => { setSelectedDim('general'); setSuggestions([]); setInput(''); }}
                        disabled={streaming || loadingSuggest}
                        title="通用聊天：自由讨论小说/剧情，关键词触发填入各维度"
                      >💬 通用</button>
                      {dimensions.filter(d => ['concept', 'key_rules', 'worldbuilding'].includes(d.key)).map(d => (
                        <button
                          key={d.key}
                          className={`smart-dim-btn ${selectedDim === d.key ? 'active' : ''}`}
                          onClick={() => { setSelectedDim(d.key); setSuggestions([]); setInput(''); }}
                          disabled={streaming || loadingSuggest}
                          title={d.key === 'key_rules' ? '能力体系/科技树等硬规则（生成时同步产出文风指南）' : d.hint}
                        >{d.icon} {d.label}</button>
                      ))}
                    </div>
                    <div className="smart-dim-row">
                      {dimensions.filter(d => ['plot_design', 'timeline', 'character_profiles', 'foreshadowing'].includes(d.key)).map(d => (
                        <button
                          key={d.key}
                          className={`smart-dim-btn ${selectedDim === d.key ? 'active' : ''}`}
                          onClick={() => { setSelectedDim(d.key); setSuggestions([]); setInput(''); }}
                          disabled={streaming || loadingSuggest}
                          title={d.hint}
                        >{d.icon} {d.label}</button>
                      ))}
                    </div>
                  </div>
                  <SkillPackSelector packs={skillPacks.filter(p => p.category === 'master')} selected={settingPacks} onToggle={(id) => toggleSkillPack('setting', id)} compact />
                </>
              )}

              {activeTab === 'chapter' && (
                <>
                  {/* 最新章节信息行 + 内联🔄刷新按钮 */}
                  <div className="smart-chapter-info smart-chapter-info-row">
                    <span className="smart-chapter-info-text">
                      {latestChapter ? (
                        <>📖 最新：<strong>{latestChapter.title}</strong>（{latestChapter.word_count}字，第{latestChapter.order_index}章）</>
                      ) : (
                        <>📖 还没有章节，将创建第 1 章</>
                      )}
                    </span>
                    <button
                      className="smart-chapter-refresh"
                      onClick={refreshChapterAnchor}
                      disabled={streaming}
                      title="刷新章节定位（写作后会自动填入新章节到目录）"
                    >🔄</button>
                  </div>
                  {/* 写作 / 修改 两按钮一排（直接执行，无需切换模式） */}
                  <div className="smart-chapter-actions smart-chapter-actions-row">
                    <button
                      className="smart-action-btn primary"
                      onClick={() => doChapterAction('continue', null, input.trim() || undefined)}
                      disabled={streaming}
                      title="续写下一章（输入框内容作为写作要求）"
                    >✍️ 写作第 {nextChapterNum} 章</button>
                    <button
                      className="smart-action-btn"
                      onClick={() => {
                        const text = input.trim();
                        if (!text) {
                          setStreamError('请在输入框说明要修改哪一章及修改意见（如「第3章，增加主角心理描写」）');
                          return;
                        }
                        handleChapterSend();
                      }}
                      disabled={streaming || chapters.length === 0}
                      title="修改已写章节（在输入框写明章节号和修改意见）"
                    >✨ 修改</button>
                  </div>
                  <SkillPackSelector packs={skillPacks.filter(p => p.category === 'style')} selected={chapterPacks} onToggle={(id) => toggleSkillPack('chapter', id)} compact />
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
                        value={deaiTargetId || ''}
                        onChange={e => setDeaiTargetId(e.target.value)}
                        disabled={streaming}
                      >
                        <option value="">请选择章节…</option>
                        {[...chapters].sort((a, b) => b.order_index - a.order_index).slice(0, 10).map(c => (
                          <option key={c.id} value={c.id}>第{c.order_index}章 {c.title}（{c.word_count}字）</option>
                        ))}
                      </select>
                    )}
                  </div>
                  {chapters.length > 10 && (
                    <div className="smart-deai-hint">💡 仅显示最新10章，其他章节可在下方消息框输入「第N章」指定</div>
                  )}
                  <SkillPackSelector packs={skillPacks.filter(p => p.category === 'review')} selected={deaiPacks_selected} onToggle={(id) => toggleSkillPack('deai', id)} compact />
                  {deaiPacks.length > 0 && deaiPacks_selected.length === 0 && (
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
                  {/* 校审范围：两行下拉选择（按卷 + 一致性章节），样式统一对齐 */}
                  <div className="smart-review-scope">
                    {volumes.length > 0 && (
                      <div className="smart-chapter-select smart-review-scope-row">
                        <label>📚 按卷（不选=全书）</label>
                        <select
                          className="smart-review-scope-select"
                          value={reviewVolumeIds[0] || ''}
                          onChange={e => {
                            const v = e.target.value;
                            setReviewVolumeIds(v ? [v] : []);
                          }}
                          disabled={reviewing || streaming}
                        >
                          <option value="">全书</option>
                          {volumes.map(v => (
                            <option key={v.id} value={v.id}>{v.title}（{v.chapter_count}章）</option>
                          ))}
                        </select>
                      </div>
                    )}
                    {chapters.length > 0 && (
                      <div className="smart-chapter-select smart-review-scope-row">
                        <label>📖 一致性章节（不选=最新）</label>
                        <select
                          className="smart-review-scope-select"
                          value={reviewChapterId || ''}
                          onChange={e => setReviewChapterId(e.target.value || null)}
                          disabled={reviewing || streaming}
                        >
                          <option value="">最新章节</option>
                          {chapters.map(c => (
                            <option key={c.id} value={c.id}>第{c.order_index}章 {c.title}</option>
                          ))}
                        </select>
                      </div>
                    )}
                  </div>
                  <SkillPackSelector packs={skillPacks.filter(p => p.category === 'review')} selected={reviewPacks} onToggle={(id) => toggleSkillPack('review', id)} compact />
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
                  <div className="chat-empty-icon"><CarLogo size={56} /></div>
                  <p>AI 智驾已就绪。选择上方维度或操作，开始人机协作创作。</p>
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
                  index={i}
                  message={m}
                  onAdopt={handleAdopt}
                  onEdit={handleEdit}
                  onIgnore={handleIgnore}
                  applyingCardId={applyingCardId}
                  streaming={streaming && i === messages.length - 1 && m.role === 'assistant'}
                  onReplaceChapter={handleReplaceChapter}
                  onEditMessage={handleEditMessage}
                  onDeleteMessage={handleDeleteMessage}
                  onRegenerate={handleRegenerate}
                />
              ))}
              {streamError && <div className="chat-error">{streamError}</div>}
            </div>

            {/* 去AITab的主操作按钮（在输入框上方） */}
            {activeTab === 'deai' && (
              <div className="smart-main-action-bar">
                <button
                  className="smart-main-action"
                  onClick={handleDeai}
                  disabled={streaming}
                >{streaming ? '处理中…' : '🧹 开始去AI味'}</button>
                {!deaiTargetId && <span className="smart-main-hint">未选章节时可在输入框输入「第N章」</span>}
              </div>
            )}

            {/* 输入区（设定Tab可输入；正文Tab修改模式可输入） */}
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
                    >{loadingSuggest ? '…' : (selectedDim === 'general' ? '发送' : '生成方案')}</button>
                  )}
                </div>
              </div>
            )}

            {activeTab !== 'setting' && (
              <div className="chat-input-area">
                <div className="chat-input-row">
                  <input
                    className="chat-input"
                    value={input}
                    onChange={e => setInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        if (activeTab === 'chapter') {
                          // 正文Tab：直接发送，智能判断修改/续写
                          handleChapterSend();
                        } else if (activeTab === 'deai' && input.trim()) {
                          handleDeai();
                        }
                      }
                    }}
                    placeholder={inputPlaceholder}
                    disabled={streaming}
                  />
                  {streaming ? (
                    <button className="chat-send stop" onClick={stopStream}>停止</button>
                  ) : activeTab === 'chapter' ? (
                    <button
                      className="chat-send"
                      onClick={handleChapterSend}
                      disabled={chapters.length === 0 && !input.trim()}
                    >{input.trim() ? '发送' : '▶️ 写作'}</button>
                  ) : activeTab === 'deai' ? (
                    <button
                      className="chat-send"
                      onClick={handleDeai}
                      disabled={!input.trim() && !deaiTargetId}
                    >🧹 去味</button>
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
                  onClick={() => switchTab(t.key)}
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
