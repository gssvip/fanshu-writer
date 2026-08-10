import { useState, useEffect, useRef, useCallback, memo } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import type { ActionCard, ProgressMap, AIMessage, SkillPack, BookBible } from '../types';
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
  | { type: 'error'; error: string }
  | { type: 'meta'; kind: string; info?: any };

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
// 章节号解析（与后端 parse_chapter_number 口径对齐，写作/修改/去AI共用）
// ============================================================================
function parseChapterNumber(title?: string): number | null {
  if (!title) return null;
  const t = title.trim();
  // 第N章/第N回/第N节...（取最后一个匹配，如 第3卷第5章 → 5）
  const suffix = '章节回卷部篇话集幕折更段讲课夜日年季场';
  const re1 = new RegExp(`第\\s*([0-9零一二三四五六七八九十百千万亿两〇]+)\\s*[${suffix}]`, 'g');
  const matches = [...t.matchAll(re1)].map(m => m[1]);
  if (matches.length) {
    const n = chineseToInt(matches[matches.length - 1]);
    if (n !== null) return n;
  }
  // Chapter N / Ch.N
  const m2 = t.match(/(?:chapter|ch|episode|ep)\.?\s*(\d+)/i);
  if (m2) return parseInt(m2[1]);
  // 行首数字 + 分隔
  const m3 = t.match(/^\s*(\d+)(?:\s*[\.、:：\-\)\]】，;；]|\s+|\s*(?=[\u4e00-\u9fffA-Za-z]))/);
  if (m3) {
    const rest = t.slice(m3[1].length);
    if (!rest || (rest[0] !== '年' && rest[0] !== '月' && rest[0] !== '日' && rest[0] !== '时' && rest[0] !== '分' && rest[0] !== '秒' && rest[0] !== '号')) {
      return parseInt(m3[1]);
    }
  }
  return null;
}

// 中文数字转 int（简化版，覆盖常见范围）
function chineseToInt(s: string): number | null {
  if (/^\d+$/.test(s)) return parseInt(s);
  const digitMap: Record<string, number> = { '零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '百': 100, '千': 1000, '万': 10000, '亿': 100000000 };
  if (!s) return null;
  let total = 0, section = 0, num = 0;
  for (const ch of s) {
    const v = digitMap[ch];
    if (v === undefined) return null;
    if (v >= 10) {
      section = (num || 1) * v;
      total += section;
      num = 0;
      if (v >= 10000) { total = section; section = 0; }
    } else {
      num = v;
    }
  }
  return total + num;
}

// 判断维度是否走"方案选择"流程：构思/核心设定/世界观 需要发散多方案；
// 大纲/剧情/人物/伏笔 等下游维度已由上游确定，直接生成或修改即可。
function shouldShowSuggestions(dimKey: string | null): boolean {
  if (!dimKey || dimKey === 'general') return false;
  const suggestDims = ['concept', 'key_rules', 'worldbuilding', 'locations', 'inventory'];
  return suggestDims.includes(dimKey);
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

// 已落地卡片（adopted/edited）：默认折叠，点击展开查看内容
const AdoptedCardCollapsed = memo(function AdoptedCardCollapsed({ card }: { card: ActionCard }) {
  const [expanded, setExpanded] = useState(false);
  const wc = (card.content || '').length;
  return (
    <div className="chat-card chat-card-adopted">
      <div
        className="chat-card-head"
        onClick={() => setExpanded(e => !e)}
        style={{ cursor: 'pointer' }}
      >
        <span className="chat-card-icon">{CARD_ICON[card.type] || '📌'}</span>
        <span className="chat-card-title">{card.title}</span>
        <span className="chat-card-status">✓ 已落地 · {card.target}{wc > 0 ? ` · ${wc}字` : ''}</span>
        <span className="chat-card-toggle" style={{ marginLeft: 'auto', fontSize: 12, color: '#999' }}>
          {expanded ? '收起 ▲' : '展开 ▼'}
        </span>
      </div>
      {expanded && <div className="chat-card-body">{card.content}</div>}
    </div>
  );
});

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
    // 已落地卡片默认折叠，点击展开/收起（避免重开聊天时已采纳内容占满屏幕）
    return <AdoptedCardCollapsed card={card} />;
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
                {applying ? '落地中…' : (card.type === 'SAVE_CHAPTER' ? '采纳(覆盖同章)' : '采纳(追加)')}
              </button>
            )}
            <button className="chat-card-btn" onClick={() => setEditing(true)} disabled={applying}>
              编辑后覆盖
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
  const { chatPanelOpen, chatPanelBookId, chatPanelSessionId, chatPanelPresetTab, chatPanelPresetInput, chatPanelPresetFixTasks, closeChatPanel } = useStore() as any;
  const setChatPanelSessionId = useStore((s: any) => s.setChatPanelSessionId) as (id: string | null) => void;
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
  const [bible, setBible] = useState<BookBible | null>(null);
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
  const [chapterTargetId, setChapterTargetId] = useState<string | null>(null);   // 正文Tab：修改目标章节
  const [reviewChapterId, setReviewChapterId] = useState<string | null>(null);   // 校审Tab：一致性检查章节
  const [reviewVolumeIds, setReviewVolumeIds] = useState<string[]>([]);          // 校审Tab：按卷检查
  const [deaiPacks, setDeaiPacks] = useState<Array<{ id: string; name: string; description: string; icon: string; priority: number }>>([]);
  // 自动上下文命中提示：meta 事件告知已定位并注入的章节/维度
  const [autoContextNotice, setAutoContextNotice] = useState<{ chapters: Array<{ id: string; title: string }>; dims: Array<{ key: string; label: string }> } | null>(null);
  const [reviewing, setReviewing] = useState(false);
  // 修正任务清单（从防遗忘报告违规项带入，支持多章/多维度连续修正并追踪进度）
  const [fixTasks, setFixTasks] = useState<Array<{ location: string; desc: string; fix: string; severity?: string; dimKey?: string; done?: boolean; chapterId?: string | null }>>([]);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const streamBufferRef = useRef<string>('');
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // 追踪正在修改的章节（用于修改完成后自动标记任务清单 done）
  const polishingChapterIdRef = useRef<string | null>(null);
  // 追踪正在修正的设定维度（设定Tab修正完成后自动标记任务清单 done）
  const fixingDimKeyRef = useRef<string | null>(null);

  const bookId = chatPanelBookId;

  // 自动滚动
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, streaming]);

  // 加载进度（同时刷新 bible，避免修改时拿到旧内容）
  const refreshProgress = useCallback(async () => {
    if (!bookId) return;
    try {
      const p = await api.getProgressMap(bookId);
      setProgress(p);
    } catch { /* ignore */ }
    try {
      const b = await api.getBible(bookId);
      setBible(b);
    } catch { /* ignore */ }
  }, [bookId]);

  const refreshHistory = useCallback(async () => {
    if (!bookId) return;
    try {
      const r = await api.listBookChatSessions(bookId);
      setHistorySessions(r.sessions || []);
    } catch { /* ignore */ }
  }, [bookId]);

  // 面板打开时加载基础元数据（维度、技能包、章节、卷等）——只要面板开着且有书就加载，不受会话影响
  useEffect(() => {
    if (!chatPanelOpen || !bookId) return;
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
  }, [chatPanelOpen, bookId, refreshProgress, refreshHistory]);

  // 加载聊天会话：优先 chatPanelSessionId 指定的，否则最新
  // 用 ref 记录上次已加载的 sessionId，避免 sessionId 同步回 store 后重复加载导致消息闪烁
  const loadedSessionRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (!chatPanelOpen || !bookId) return;
    // 仅在会话 id 变化时加载（避免相同 session 下因其他 state 抖动重复加载消息）
    if (loadedSessionRef.current === chatPanelSessionId) return;
    loadedSessionRef.current = chatPanelSessionId;
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
  }, [chatPanelOpen, bookId, chatPanelSessionId]);

  // 应用预设：从其它入口（如「修正正文」）跳转进来时切到指定 Tab 并预填输入框/任务清单
  useEffect(() => {
    if (!chatPanelOpen) return;
    if (chatPanelPresetTab) setActiveTab(chatPanelPresetTab);
    if (chatPanelPresetInput) setInput(chatPanelPresetInput);
    if (chatPanelPresetFixTasks) setFixTasks(chatPanelPresetFixTasks.map((t: any) => ({ ...t, done: false, chapterId: null })));
  }, [chatPanelOpen, chatPanelPresetTab, chatPanelPresetInput, chatPanelPresetFixTasks]);

  // 把本地 sessionId 同步回 store，供修正入口复用同一会话（首次新建后后续复用）
  // 同步后立即更新 loadedSessionRef，避免 store 变化触发上面的加载 effect 重复加载当前会话
  useEffect(() => {
    setChatPanelSessionId(sessionId);
    loadedSessionRef.current = sessionId;
  }, [sessionId, setChatPanelSessionId]);

  // 章节加载后，为 fixTasks 匹配对应 chapterId（按 location 中的章号/标题匹配）
  useEffect(() => {
    if (fixTasks.length === 0 || chapters.length === 0) return;
    setFixTasks(prev => prev.map(t => {
      if (t.chapterId) return t; // 已匹配过的跳过
      const numMatch = t.location.match(/第?\s*(\d+)\s*章/);
      let ch = null;
      if (numMatch) {
        const num = parseInt(numMatch[1]);
        ch = chapters.find(c => (parseChapterNumber(c.title) ?? c.order_index) === num) || null;
      }
      if (!ch) {
        // 回退：用 location 文本模糊匹配标题
        const loc = t.location.replace(/^第?\s*\d+\s*章\s*/, '').trim();
        if (loc) ch = chapters.find(c => c.title.includes(loc)) || null;
      }
      return { ...t, chapterId: ch ? ch.id : null };
    }));
  }, [chapters, fixTasks.length]);

  // 修改完成后自动标记对应任务为 done（streaming 从 true→false 且有记录的章节/维度）
  useEffect(() => {
    if (streaming) return;
    const chId = polishingChapterIdRef.current;
    const dimKey = fixingDimKeyRef.current;
    if (!chId && !dimKey) return;
    if (fixTasks.length === 0) return;
    polishingChapterIdRef.current = null;
    fixingDimKeyRef.current = null;
    setFixTasks(prev => prev.map(t => {
      if (t.done) return t;
      if (chId && t.chapterId === chId) return { ...t, done: true };
      if (dimKey && t.dimKey === dimKey) return { ...t, done: true };
      return t;
    }));
  }, [streaming, fixTasks.length]);

  // 设定维度匹配：根据任务 location/desc 文本推断对应维度 key（仅未匹配 dimKey 的任务）
  useEffect(() => {
    if (fixTasks.length === 0) return;
    setFixTasks(prev => prev.map(t => {
      if (t.dimKey) return t; // 已匹配过的跳过
      const text = `${t.location || ''} ${t.desc || ''} ${t.fix || ''}`;
      let dimKey: string | undefined;
      if (/人物|角色|主角|配角|性格|外貌|背景故事/.test(text)) dimKey = 'character_profiles';
      else if (/伏笔|铺垫|预示|回收|埋下/.test(text)) dimKey = 'foreshadowing';
      else if (/世界观|世界设定|地理|国家|大陆|城邦|历史/.test(text)) dimKey = 'worldbuilding';
      else if (/规则|能力|体系|科技|魔法|修炼|等级/.test(text)) dimKey = 'key_rules';
      else if (/剧情|大纲|情节|主线|支线|转折|高潮/.test(text)) dimKey = 'plot_design';
      else if (/时间线|时间|年代|顺序|先后/.test(text)) dimKey = 'timeline';
      else if (/构思|概念|主题|卖点|核心/.test(text)) dimKey = 'concept';
      else dimKey = 'general';
      return { ...t, dimKey };
    }));
  }, [fixTasks.length]);

  // 正文写作默认要求（切到正文Tab且输入框为空/为旧默认值时自动填入）
  const CHAPTER_DEFAULT_INPUT = '接上一章剧情，读取剧情维度本章剧情，保证剧情连贯、逻辑清晰。极致模仿人的写作习惯，自然没ai味儿。写事为主，景一笔带过，非必要不用比喻/拟人等修辞，句子阅读感强及顺畅。';

  // 切换 Tab 时清空选中章节（各Tab独立）；切到正文Tab时填入默认写作要求
  const switchTab = useCallback((tab: SmartTab) => {
    setActiveTab(tab);
    setInput(prev => {
      // 进入正文Tab：若输入为空或是正文默认值，填入默认要求；已有自定义内容则保留
      if (tab === 'chapter' && (!prev.trim() || prev.trim() === CHAPTER_DEFAULT_INPUT)) {
        return CHAPTER_DEFAULT_INPUT;
      }
      // 离开正文Tab：若仍是默认值则清空，避免带到其他Tab
      if (tab !== 'chapter' && prev.trim() === CHAPTER_DEFAULT_INPUT) {
        return '';
      }
      return prev;
    });
  }, []);

  // 各 Tab 独立的技能包切换
  const toggleSkillPack = useCallback((tab: SmartTab, id: string) => {
    const setter = tab === 'setting' ? setSettingPacks : tab === 'chapter' ? setChapterPacks : tab === 'deai' ? setDeaiPacksSelected : setReviewPacks;
    setter(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);

  // 关闭时取消流并重置会话加载标记（下次打开重新加载）
  useEffect(() => {
    if (!chatPanelOpen && abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
      loadedSessionRef.current = undefined;
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
      if (evt.type === 'meta') {
        if (evt.kind === 'auto_context' && evt.info) {
          setAutoContextNotice({
            chapters: Array.isArray(evt.info.chapters) ? evt.info.chapters : [],
            dims: Array.isArray(evt.info.dims) ? evt.info.dims : [],
          });
        }
      } else if (evt.type === 'delta') {
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
    // 仅方案选择型维度才走多选意见流程
    if (!shouldShowSuggestions(selectedDim)) return;
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
    // 记录正在修正的维度（用于任务清单自动标记 done）
    fixingDimKeyRef.current = selectedDim;
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

  // 3. 下游维度直接生成（不经过多选方案，把用户输入作为生成要求）
  const handleDirectGenerate = useCallback(async () => {
    const text = input.trim();
    if (!bookId || !selectedDim || streaming || !text) return;
    setInput('');
    setStreamError('');
    streamBufferRef.current = '';
    fixingDimKeyRef.current = selectedDim;
    const dimLabel = dimensions.find(d => d.key === selectedDim)?.label || selectedDim;
    appendUserAi(`生成${dimLabel}：${text}`);
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await api.smartGenerateStream(bookId, selectedDim, text, text, settingPacks, sessionId || undefined, ctrl.signal);
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
  }, [input, bookId, selectedDim, streaming, settingPacks, sessionId, dimensions, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

  // 4. 单独维度AI修改（基于已落地内容）
  const handleDimEdit = useCallback(async () => {
    const text = input.trim();
    if (!bookId || !selectedDim || streaming || !text) return;
    setInput('');
    setStreamError('');
    streamBufferRef.current = '';
    // 记录正在修正的维度（用于任务清单自动标记 done）
    fixingDimKeyRef.current = selectedDim;
    const dimLabel = dimensions.find(d => d.key === selectedDim)?.label || selectedDim;
    appendUserAi(`修订${dimLabel}：${text}`);
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const currentContent = (bible as any)?.[selectedDim] || '';
      const res = await api.smartDimEditStream(bookId, selectedDim, currentContent, text, settingPacks, sessionId || undefined, ctrl.signal);
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
  }, [input, bookId, selectedDim, streaming, settingPacks, sessionId, dimensions, bible, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

  // ========== 正文Tab：写作/修改（结合消息栏输入的要求创作）==========
  // 真正执行章节写作/修改（结合用户在消息栏输入的要求）
  const doChapterAction = useCallback(async (action: 'continue' | 'polish', targetChapterId: string | null, instruction?: string) => {
    if (!bookId || streaming) return;
    setStreamError('');
    streamBufferRef.current = '';
    // 记录正在修改的章节（修改完成后自动标记任务清单 done）
    polishingChapterIdRef.current = (action === 'polish' && targetChapterId) ? targetChapterId : null;
    // 统一口径：target_chapter_num 优先从标题解析章节号，回退 order_index
    // 写作=下一章号；修改=所选章节的章节号
    let targetNum: number;
    if (action === 'continue') {
      targetNum = nextChapterNum;
    } else {
      const targetCh = targetChapterId ? chapters.find(c => c.id === targetChapterId) : null;
      const fallback = latestChapter ? (parseChapterNumber(latestChapter.title) ?? latestChapter.order_index) : (nextChapterNum - 1);
      targetNum = targetCh ? (parseChapterNumber(targetCh.title) ?? targetCh.order_index) : fallback;
    }
    const label = action === 'continue' ? `写作第 ${nextChapterNum} 章` : `修改第 ${targetNum} 章`;
    const userNote = (instruction || input.trim());
    appendUserAi(userNote ? `${label}（${userNote.slice(0, 60)}）` : label);
    // 发送后重置为正文默认写作要求（而非清空），便于连续写作
    if (userNote) setInput(CHAPTER_DEFAULT_INPUT);
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
      // 统一口径：用章节号从标题解析匹配章节（与后端 parse_chapter_number 一致）
      const targetNum = numMatch ? parseInt(numMatch[1]) : (latestChapter ? (parseChapterNumber(latestChapter.title) ?? latestChapter.order_index) : 1);
      const ch = chapters.find(c => (parseChapterNumber(c.title) ?? c.order_index) === targetNum);
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

  // ========== 卡片操作 ==========
  const handleAdopt = useCallback(async (card: ActionCard) => {
    if (!bookId) return;
    setApplyingCardId(card.id);
    try {
      const r = await api.applyChatCard(bookId, card, sessionId || undefined);
      setMessages(prev => prev.map(m => {
        if (m.role !== 'assistant' || !m.cards) return m;
        return { ...m, cards: m.cards.map(c => c.id === card.id ? { ...c, status: 'adopted' as const } : c) };
      }));
      refreshProgress();
      if (card.type === 'SAVE_CHAPTER' && (r as any).chapter_id) {
        const ch = r as any;
        const actionLabel = ch.action === 'updated' ? '已覆盖同章号章节' : '已新建章节';
        setStreamError('');
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `✅ ${actionLabel}：${ch.chapter_title}（${ch.word_count}字，第${ch.order_index}章）。可在「章节」Tab 查看。`,
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
  }, [bookId, sessionId, refreshProgress]);

  const handleEdit = useCallback(async (card: ActionCard, newContent: string) => {
    if (!bookId) return;
    setApplyingCardId(card.id);
    try {
      const editedCard = { ...card, content: newContent };
      const r = await api.applyChatCard(bookId, editedCard, sessionId || undefined);
      setMessages(prev => prev.map(m => {
        if (m.role !== 'assistant' || !m.cards) return m;
        return { ...m, cards: m.cards.map(c => c.id === card.id ? { ...editedCard, status: 'edited' as const } : c) };
      }));
      refreshProgress();
      if (card.type === 'SAVE_CHAPTER' && (r as any).chapter_id) {
        const ch = r as any;
        const actionLabel = ch.action === 'updated' ? '已覆盖同章号章节' : '已新建章节';
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `✅ ${actionLabel}：${ch.chapter_title}（${ch.word_count}字，第${ch.order_index}章）。`,
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
  }, [bookId, sessionId, refreshProgress]);

  const handleIgnore = useCallback((card: ActionCard) => {
    setMessages(prev => prev.map(m => {
      if (m.role !== 'assistant' || !m.cards) return m;
      return { ...m, cards: m.cards.map(c => c.id === card.id ? { ...c, status: 'ignored' as const } : c) };
    }));
    // 持久化忽略状态（避免重开聊天又提示采纳）
    if (sessionId) {
      api.updateCardStatus(sessionId, card.id, 'ignored').catch(() => {});
    }
  }, [sessionId]);

  // 去AI味卡片：替换原章节正文
  const handleReplaceChapter = useCallback(async (card: ActionCard, meta: any) => {
    if (!bookId || !meta?.chapter_id) return;
    setApplyingCardId(card.id);
    try {
      await api.smartChapterReplace(bookId, meta.chapter_id, card.content, sessionId || undefined, card.id);
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
  }, [bookId, sessionId]);

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

  // 删除历史会话：删除后刷新列表；若删的是当前会话，切到最新或新建
  const handleDeleteSession = useCallback(async (sid: string, e: React.MouseEvent) => {
    e.stopPropagation();  // 阻止冒泡触发 handleSelectSession
    if (!window.confirm('确定删除该会话？此操作不可撤销。')) return;
    try {
      await api.deleteAISession(sid);
      // 刷新历史列表
      const r = await api.listBookChatSessions(bookId!);
      const remaining = r.sessions || [];
      setHistorySessions(remaining);
      // 若删的是当前会话，切到最新或清空
      if (sid === sessionId) {
        if (remaining.length > 0) {
          await handleSelectSession(remaining[0].id);
        } else {
          setSessionId(null);
          setMessages([]);
        }
      }
    } catch (err: any) {
      alert('删除失败：' + (err.message || '未知错误'));
    }
  }, [bookId, sessionId, handleSelectSession]);

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
      const dimLabel = dimensions.find(d => d.key === selectedDim)?.label || selectedDim;
      if (shouldShowSuggestions(selectedDim)) {
        return `描述你对「${dimLabel}」的构思方向，AI 会给出多个方案供选择…`;
      }
      return `输入要求直接生成或修改「${dimLabel}」（无需方案选择）…`;
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
    setAutoContextNotice(null);
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
    // 下游维度（大纲/剧情/人物/伏笔等）由上游确定，不再给出多选方案：
    // 有内容则修改，无内容则直接基于用户要求生成。
    if (!shouldShowSuggestions(selectedDim)) {
      if (dimStatus && dimStatus !== 'empty') {
        handleDimEdit();
      } else {
        handleDirectGenerate();
      }
      return;
    }
    if (dimStatus && dimStatus !== 'empty') {
      handleDimEdit();
    } else {
      handleSuggest();
    }
  }, [activeTab, selectedDim, suggestions, progress, handleSuggest, handleDimEdit, handleGeneral, handleDirectGenerate]);

  // 重新生成：找到该AI消息前最近的用户消息，重新触发对应动作
  const handleRegenerate = useCallback((index: number) => {
    setMessages(prev => {
      // 删除该AI消息及之后的，保留之前的用户消息
      const before = prev.slice(0, index);
      const userMsg = [...before].reverse().find(m => m.role === 'user');
      setMessages(before);
      if (userMsg && activeTab === 'setting' && selectedDim) {
        // 设定Tab：用用户消息内容重新触发（按维度判断走方案或直接生成/修改）
        const text = userMsg.content.replace(/^【[^】]+】/, '').trim();
        if (text) {
          setInput(text);
          setTimeout(() => handleMainSend(), 50);
        }
      }
      return before;
    });
  }, [activeTab, selectedDim, handleMainSend]);

  // 设定Tab：选择维度后
  // - 构思/设定/世界观 等上游维度：第一次输入走 suggest（生成多选意见），选中后 generate
  // - 大纲/剧情/人物/伏笔 等下游维度：由上游确定，直接 generate 或 dim-edit，不再给方案
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
                      <div key={s.id} className={`chat-history-item ${s.id === sessionId ? 'active' : ''}`} onClick={() => handleSelectSession(s.id)}>
                        <div className="chat-history-title">{s.title || '未命名'}</div>
                        <div className="chat-history-meta">{s.message_count} 条 · {s.updated_at ? new Date(s.updated_at).toLocaleString() : ''}</div>
                        <button
                          className="chat-history-del"
                          title="删除会话"
                          onClick={(e) => handleDeleteSession(s.id, e)}
                        >×</button>
                      </div>
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
                  {/* 修正任务清单（从防遗忘报告违规项带入，支持多维度连续修正并追踪进度） */}
                  {fixTasks.length > 0 && (
                    <div className="fix-tasks-panel">
                      <div className="fix-tasks-head">
                        <span className="fix-tasks-title">📋 设定修正任务清单</span>
                        <span className="fix-tasks-progress">
                          {fixTasks.filter(t => t.done).length}/{fixTasks.length} 已完成
                        </span>
                      </div>
                      <div className="fix-tasks-list">
                        {fixTasks.map((task, idx) => {
                          const dimLabel = task.dimKey ? (dimensions.find(d => d.key === task.dimKey)?.label || task.dimKey) : '未匹配';
                          const dimIcon = task.dimKey ? (dimensions.find(d => d.key === task.dimKey)?.icon || '📌') : '❓';
                          return (
                            <div key={idx} className={`fix-task-item ${task.done ? 'fix-task-done' : ''}`}>
                              <label className="fix-task-check">
                                <input
                                  type="checkbox"
                                  checked={!!task.done}
                                  onChange={() => setFixTasks(prev => prev.map((t, i) => i === idx ? { ...t, done: !t.done } : t))}
                                  disabled={streaming}
                                />
                              </label>
                              <div className="fix-task-content">
                                <div className="fix-task-location">
                                  {task.location || '（未指定位置）'}
                                  <span className="fix-task-dim-tag">{dimIcon} {dimLabel}</span>
                                  {task.severity && <span className={`fix-task-sev sev-${task.severity}`}>{task.severity}</span>}
                                </div>
                                <div className="fix-task-desc">{task.desc}</div>
                                {task.fix && <div className="fix-task-fix">建议：{task.fix}</div>}
                              </div>
                              {task.dimKey && task.dimKey !== 'general' && !task.done && (
                                <button
                                  className="fix-task-action"
                                  disabled={streaming || loadingSuggest}
                                  onClick={() => {
                                    setSelectedDim(task.dimKey!);
                                    setSuggestions([]);
                                    setInput(`修正意见：${task.desc}${task.fix ? `（${task.fix}）` : ''}`);
                                  }}
                                  title="定位到该维度并填充修正意见"
                                >去修正</button>
                              )}
                              {task.done && <span className="fix-task-done-tag">✓ 已处理</span>}
                            </div>
                          );
                        })}
                      </div>
                      {/* 一键继续下一个未完成的设定任务 */}
                      {fixTasks.some(t => !t.done) && (
                        <button
                          className="fix-tasks-continue"
                          disabled={streaming || loadingSuggest}
                          onClick={() => {
                            const next = fixTasks.find(t => !t.done && t.dimKey && t.dimKey !== 'general');
                            if (!next || !next.dimKey) return;
                            setSelectedDim(next.dimKey!);
                            setSuggestions([]);
                            setInput(`修正意见：${next.desc}${next.fix ? `（${next.fix}）` : ''}`);
                          }}
                          title="自动定位到第一个未完成的维度并填充意见"
                        >▶ 继续下一个未完成</button>
                      )}
                      {fixTasks.every(t => t.done) && (
                        <div className="fix-tasks-all-done">🎉 全部设定修正任务已完成</div>
                      )}
                    </div>
                  )}
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
                  {/* 章节选择器（用于「修改」模式定位目标章节） */}
                  <div className="smart-chapter-select">
                    <label>修改目标章节：</label>
                    {chapters.length === 0 ? (
                      <span className="smart-empty-hint">暂无章节</span>
                    ) : (
                      <select
                        value={chapterTargetId || ''}
                        onChange={e => setChapterTargetId(e.target.value)}
                        disabled={streaming}
                      >
                        <option value="">从输入框解析章节号</option>
                        {[...chapters].sort((a, b) => b.order_index - a.order_index).map(c => (
                          <option key={c.id} value={c.id}>第{c.order_index}章 {c.title}（{c.word_count}字）</option>
                        ))}
                      </select>
                    )}
                  </div>
                  {/* 修正任务清单（从防遗忘报告违规项带入，支持多章连续修正并追踪进度） */}
                  {fixTasks.length > 0 && (
                    <div className="fix-tasks-panel">
                      <div className="fix-tasks-head">
                        <span className="fix-tasks-title">📋 修正任务清单</span>
                        <span className="fix-tasks-progress">
                          {fixTasks.filter(t => t.done).length}/{fixTasks.length} 已完成
                        </span>
                      </div>
                      <div className="fix-tasks-list">
                        {fixTasks.map((task, idx) => (
                          <div key={idx} className={`fix-task-item ${task.done ? 'fix-task-done' : ''}`}>
                            <label className="fix-task-check">
                              <input
                                type="checkbox"
                                checked={!!task.done}
                                onChange={() => setFixTasks(prev => prev.map((t, i) => i === idx ? { ...t, done: !t.done } : t))}
                                disabled={streaming}
                              />
                            </label>
                            <div className="fix-task-content">
                              <div className="fix-task-location">
                                {task.location}
                                {task.severity && <span className={`fix-task-sev sev-${task.severity}`}>{task.severity}</span>}
                                {!task.chapterId && <span className="fix-task-unmatch">未匹配章节</span>}
                              </div>
                              <div className="fix-task-desc">{task.desc}</div>
                              {task.fix && <div className="fix-task-fix">建议：{task.fix}</div>}
                            </div>
                            {task.chapterId && !task.done && (
                              <button
                                className="fix-task-action"
                                disabled={streaming}
                                onClick={() => {
                                  setChapterTargetId(task.chapterId!);
                                  setInput(`修改意见：${task.desc}${task.fix ? `（${task.fix}）` : ''}`);
                                }}
                                title="定位到该章节并填充修改意见"
                              >去修改</button>
                            )}
                            {task.done && <span className="fix-task-done-tag">✓ 已处理</span>}
                          </div>
                        ))}
                      </div>
                      {/* 一键继续下一个未完成任务 */}
                      {fixTasks.some(t => !t.done) && (
                        <button
                          className="fix-tasks-continue"
                          disabled={streaming}
                          onClick={() => {
                            const next = fixTasks.find(t => !t.done && t.chapterId);
                            if (!next) return;
                            setChapterTargetId(next.chapterId!);
                            setInput(`修改意见：${next.desc}${next.fix ? `（${next.fix}）` : ''}`);
                          }}
                          title="自动定位到第一个未完成的章节并填充意见"
                        >▶ 继续下一个未完成</button>
                      )}
                      {fixTasks.every(t => t.done) && (
                        <div className="fix-tasks-all-done">🎉 全部修正任务已完成</div>
                      )}
                    </div>
                  )}
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
                        // 有选中章节时，直接修改该章节（输入框作为修改意见，可为空）
                        if (chapterTargetId) {
                          doChapterActionWithNote('polish', chapterTargetId, text || '按检查报告修正违规项');
                          return;
                        }
                        if (!text) {
                          setStreamError('请在输入框说明要修改哪一章及修改意见（如「第3章，增加主角心理描写」），或在上方下拉选择章节');
                          return;
                        }
                        handleChapterSend();
                      }}
                      disabled={streaming || chapters.length === 0}
                      title="修改已写章节（下拉选章或输入框写明章节号和修改意见）"
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

            {/* 多选意见列表（设定Tab专属，仅对方案选择型维度显示） */}
            {activeTab === 'setting' && suggestions.length > 0 && shouldShowSuggestions(selectedDim) && (
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

            {/* 自动上下文命中提示：已定位并注入章节/维度资料 */}
            {autoContextNotice && (autoContextNotice.chapters.length > 0 || autoContextNotice.dims.length > 0) && (
              <div className="auto-context-notice" onClick={() => setAutoContextNotice(null)} title="点击关闭">
                <span className="acn-icon">🎯</span>
                <span className="acn-text">
                  已自动定位并注入：
                  {autoContextNotice.dims.length > 0 && (
                    <span>「{autoContextNotice.dims.map(d => d.label).join('、')}」维度内容</span>
                  )}
                  {autoContextNotice.dims.length > 0 && autoContextNotice.chapters.length > 0 && <span> + </span>}
                  {autoContextNotice.chapters.length > 0 && (
                    <span>
                      {autoContextNotice.chapters.length} 章原文
                      {autoContextNotice.chapters.length <= 3 && (
                        <span className="acn-chips">
                          {autoContextNotice.chapters.map(c => (
                            <span key={c.id} className="acn-chip">{c.title}</span>
                          ))}
                        </span>
                      )}
                    </span>
                  )}
                  <span className="acn-sub"> — 智驾已直接读取资料，无需再粘贴。点击关闭本提示</span>
                </span>
                <span className="acn-close" style={{ marginLeft: 'auto', padding: '0 6px', color: '#999', cursor: 'pointer' }}>×</span>
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
                    >{
                      loadingSuggest ? '…' :
                      selectedDim === 'general' ? '发送' :
                      shouldShowSuggestions(selectedDim) ? '生成方案' : '生成/修改'
                    }</button>
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
