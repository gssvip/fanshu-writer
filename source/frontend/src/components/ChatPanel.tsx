import { useState, useEffect, useRef, useCallback, memo } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import type { ActionCard, ProgressMap, AIMessage } from '../types';

// ============================================================================
// 聊天驱动创作浮窗：边聊边写
// - 维度感知流式聊天（SSE）
// - Inline Action Card：采纳 / 编辑 / 忽略
// - 创作进度地图：感知完成度 + 下一步引导
// ============================================================================

// SSE 事件类型
type SseEvent =
  | { type: 'delta'; content: string }
  | { type: 'card'; card: ActionCard; session_id: string }
  | { type: 'done'; session_id: string }
  | { type: 'error'; error: string };

// SSE 流解析：后端发送 `data: {...}\n\n`，逐块解析
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
            const evt = JSON.parse(jsonStr);
            // 后端 ensure_ascii=False，已正常解析
            yield evt as SseEvent;
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
}

const CARD_ICON: Record<string, string> = {
  SAVE_WORLDSETTING: '🌍',
  SAVE_CHARACTER: '👤',
  SAVE_FORESHADOW: '🔮',
  SAVE_OUTLINE_NODE: '📋',
  SAVE_PLOT: '📖',
  SAVE_LOCATION: '🗺️',
  SAVE_RULE: '⚙️',
  APPLY_STYLE: '✍️',
  SAVE_CONCEPT: '💡',
};

const ActionCardView = memo(function ActionCardView({ card, onAdopt, onEdit, onIgnore, applying }: CardViewProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(card.content);
  const status = card.status || 'pending';

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
            rows={Math.min(8, Math.max(3, draft.split('\n').length))}
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
            <button className="chat-card-btn primary" onClick={() => onAdopt(card)} disabled={applying}>
              {applying ? '落地中…' : '采纳落地'}
            </button>
            <button className="chat-card-btn" onClick={() => setEditing(true)} disabled={applying}>
              编辑
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
// 进度地图
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
// 主组件
// ============================================================================
export default function ChatPanel() {
  const { chatPanelOpen, chatPanelBookId, closeChatPanel, openChatPanel } = useStore() as any;
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

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const streamBufferRef = useRef<string>('');

  const bookId = chatPanelBookId;

  // 自动滚动到底
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, streaming]);

  // 打开浮窗时加载进度
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

  useEffect(() => {
    if (chatPanelOpen && bookId) {
      refreshProgress();
      refreshHistory();
    }
  }, [chatPanelOpen, bookId, refreshProgress, refreshHistory]);

  // 关闭浮窗时取消流
  useEffect(() => {
    if (!chatPanelOpen && abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
  }, [chatPanelOpen]);

  // 发送消息
  const sendMessage = useCallback(async () => {
    const text = input.trim();
    if (!text || !bookId || streaming) return;
    setInput('');
    setStreamError('');
    streamBufferRef.current = '';

    // 乐观追加用户消息
    const userMsg: AIMessage = { role: 'user', content: text };
    const aiMsg: AIMessage = { role: 'assistant', content: '', cards: [] };
    setMessages(prev => [...prev, userMsg, aiMsg]);
    setStreaming(true);

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await api.chatSmartStream(bookId, text, sessionId || undefined, 'general', ctrl.signal);
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: `请求失败 (HTTP ${res.status})` }));
        throw new Error(err.error || `HTTP ${res.status}`);
      }

      const newCards: ActionCard[] = [];
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
          newCards.push({ ...evt.card, status: 'pending' });
          if (evt.session_id && !receivedSessionId) {
            receivedSessionId = evt.session_id;
            setSessionId(evt.session_id);
          }
          // 把卡片附加到当前 assistant 消息
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

      // 流结束后刷新进度
      refreshProgress();
      refreshHistory();
    } catch (e: any) {
      if (e.name === 'AbortError') {
        // 用户主动取消，不报错
      } else {
        setStreamError(e.message || '聊天失败');
        // 移除空的 assistant 占位
        setMessages(prev => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === 'assistant' && !last.content && !(last.cards || []).length) {
            next.pop();
          }
          return next;
        });
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [input, bookId, streaming, sessionId, refreshProgress, refreshHistory]);

  const stopStream = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setStreaming(false);
  }, []);

  // 卡片操作
  const handleAdopt = useCallback(async (card: ActionCard) => {
    if (!bookId) return;
    setApplyingCardId(card.id);
    try {
      await api.applyChatCard(bookId, card);
      setMessages(prev => prev.map(m => {
        if (m.role !== 'assistant' || !m.cards) return m;
        return { ...m, cards: m.cards.map(c => c.id === card.id ? { ...c, status: 'adopted' as const } : c) };
      }));
      refreshProgress();
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
      await api.applyChatCard(bookId, editedCard);
      setMessages(prev => prev.map(m => {
        if (m.role !== 'assistant' || !m.cards) return m;
        return { ...m, cards: m.cards.map(c => c.id === card.id ? { ...editedCard, status: 'edited' as const } : c) };
      }));
      refreshProgress();
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

  // 新建会话
  const handleNewSession = useCallback(() => {
    if (streaming) stopStream();
    setMessages([]);
    setSessionId(null);
    setShowHistory(false);
    setStreamError('');
  }, [streaming, stopStream]);

  // 快捷提问
  const QUICK_PROMPTS = progress?.next_step
    ? [`帮我想想${progress.next_step.label}：${progress.next_step.hint}`]
    : ['帮我把主角和核心配角定下来', '我们聊聊世界观设定吧', '梳理一下故事大纲'];

  return (
    <>
      {/* FAB 悬浮按钮（仅在 /write 路由下显示，从 URL 解析 bookId） */}
      <FloatingButton
        hidden={chatPanelOpen}
        onOpen={(bid) => openChatPanel(bid)}
      />

      {chatPanelOpen && bookId && (
        <div className="chat-panel-overlay">
          <div className="chat-panel">
            {/* 头部 */}
            <div className="chat-panel-header">
              <div className="chat-panel-title">
                <span className="chat-panel-logo">🤝</span>
                <div>
                  <div className="chat-panel-name">AI 副驾</div>
                  <div className="chat-panel-sub">边聊边写 · 讨论即落地</div>
                </div>
              </div>
              <div className="chat-panel-tools">
                <button className="chat-tool-btn" onClick={() => { setShowProgress(s => !s); }} title="创作进度">
                  🗺️
                </button>
                <button className="chat-tool-btn" onClick={() => { setShowHistory(s => !s); refreshHistory(); }} title="历史会话">
                  🕘
                </button>
                <button className="chat-tool-btn" onClick={handleNewSession} title="新会话">
                  ✨
                </button>
                <button className="chat-tool-btn close" onClick={closeChatPanel} title="关闭">
                  ✕
                </button>
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
                      <button key={s.id} className={`chat-history-item ${s.id === sessionId ? 'active' : ''}`} onClick={() => { setSessionId(s.id); setShowHistory(false); }}>
                        <div className="chat-history-title">{s.title || '未命名'}</div>
                        <div className="chat-history-meta">{s.message_count} 条 · {s.updated_at ? new Date(s.updated_at).toLocaleString() : ''}</div>
                      </button>
                    ))
                  )}
                </div>
              </div>
            )}

            {/* 消息列表 */}
            <div className="chat-messages" ref={scrollRef}>
              {messages.length === 0 && (
                <div className="chat-empty">
                  <div className="chat-empty-icon">💬</div>
                  <p>和 AI 副驾聊聊你的小说吧。讨论中形成的结论会变成「落地卡片」，一键采纳就写入对应设定维度。</p>
                  {progress?.next_step && (
                    <div className="chat-empty-hint">
                      建议从 <strong>{progress.next_step.label}</strong> 开始：{progress.next_step.hint}
                    </div>
                  )}
                  <div className="chat-quick-prompts">
                    {QUICK_PROMPTS.map(p => (
                      <button key={p} className="chat-quick-prompt" onClick={() => setInput(p)}>{p}</button>
                    ))}
                  </div>
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
                />
              ))}
              {streamError && <div className="chat-error">{streamError}</div>}
            </div>

            {/* 输入区 */}
            <div className="chat-input-area">
              <textarea
                className="chat-input"
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    sendMessage();
                  }
                }}
                placeholder="和 AI 聊聊你的小说…（Enter 发送，Shift+Enter 换行）"
                rows={1}
                disabled={streaming}
              />
              {streaming ? (
                <button className="chat-send stop" onClick={stopStream}>停止</button>
              ) : (
                <button className="chat-send" onClick={sendMessage} disabled={!input.trim()}>发送</button>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

// ============================================================================
// 消息气泡（含 Inline Action Cards）
// ============================================================================
interface MessageBubbleProps {
  message: AIMessage;
  onAdopt: (c: ActionCard) => void;
  onEdit: (c: ActionCard, content: string) => void;
  onIgnore: (c: ActionCard) => void;
  applyingCardId: string | null;
  streaming: boolean;
}

const MessageBubble = memo(function MessageBubble({ message, onAdopt, onEdit, onIgnore, applyingCardId, streaming }: MessageBubbleProps) {
  const isUser = message.role === 'user';
  return (
    <div className={`chat-msg ${isUser ? 'chat-msg-user' : 'chat-msg-ai'}`}>
      <div className="chat-msg-avatar">{isUser ? '我' : '🤝'}</div>
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
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
});

// ============================================================================
// FAB 悬浮按钮（仅在 /write?book=xxx 路由下显示）
// ============================================================================
function FloatingButton({ onOpen, hidden }: { onOpen: (bookId: string) => void; hidden: boolean }) {
  const [bookId, setBookId] = useState<string | null>(null);
  useEffect(() => {
    const check = () => {
      const hash = window.location.hash;
      if (!hash.startsWith('#/write')) { setBookId(null); return; }
      // hash 路由格式：#/write?book=xxx
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
    <button className="chat-fab" onClick={() => onOpen(bookId)} title="打开 AI 副驾">
      <span>🤝</span>
      <span className="chat-fab-label">AI 副驾</span>
    </button>
  );
}
