/** ConceptPanel —— 自 WritePage.tsx 拆出（P2b 巨石拆分，逻辑零改动）。 */
import { useState } from 'react';
import { useStore } from '../../store';
import type { AISession, BookBible, BrainstormResult, BrainstormSuggestion, SkillPack } from '../../types';
import CarLogo from '../../components/CarLogo';
import { DIMENSION_LABELS, SkillPackGroupedList } from './write-shared';

/* ===== 构思面板 ===== */
export function ConceptPanel(props: {
  concept: string;
  setConcept: (v: string) => void;
  bible: BookBible | null;
  brainstorming: boolean;
  brainstormResult: BrainstormResult | null;
  brainstormError: string;
  adoptedSuggestions: Set<string>;
  onBrainstorm: () => void;
  onAdopt: (dim: string, s: BrainstormSuggestion) => void;
  bookId: string;
  bookTitle: string;
  onBibleUpdate: (b: BookBible) => void;
  hasChapters: boolean;
  conceptAiMode: boolean;
  conceptAiPrompt: string;
  conceptAiAssisting: boolean;
  conceptAiError: string;
  onStartConceptAi: () => void;
  onExecuteConceptAi: () => void;
  onCancelConceptAi: () => void;
  onEditConceptAiPrompt: (v: string) => void;
  onAnalyzeDimension: () => void;
  dimAnalyzing: boolean;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
  onOpenAiCreate: () => void;
  // AI总创作会话历史（需求3）
  aiSessions: AISession[];
  onRefreshSessions: () => void;
  onDeleteAiSessions: (ids: string[]) => void;
  onRenameAiSession: (id: string, title: string) => void;
  onResumeAiSession: (session: AISession) => void;
}) {
  const { brainstorming, brainstormResult, adoptedSuggestions, onAdopt,
    conceptAiMode, conceptAiPrompt, conceptAiAssisting, conceptAiError,
    onExecuteConceptAi, onCancelConceptAi, onEditConceptAiPrompt,
    skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks,
    bookId, aiSessions, onRefreshSessions, onDeleteAiSessions, onRenameAiSession, onResumeAiSession,
    concept: _concept, bookTitle: _bookTitle } = props;
  void _concept; void _bookTitle;
  const openChatPanel = useStore((s: any) => s.openChatPanel) as (bid: string, sessionId?: string | null, preset?: any) => void;

  const [skillExpanded, setSkillExpanded] = useState(false);
  const selectedCount = selectedSkillPackIds.length;

  // 聊天记录管理状态（需求3）
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [renamingId, setRenamingId] = useState<string>('');
  const [renameValue, setRenameValue] = useState('');
  const [historyExpanded, setHistoryExpanded] = useState(true);

  const toggleSelect = (id: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const toggleSelectAll = () => {
    setSelectedIds(prev => prev.size === aiSessions.length ? new Set() : new Set(aiSessions.map(s => s.id)));
  };
  const startRename = (id: string, currentTitle: string) => {
    setRenamingId(id); setRenameValue(currentTitle);
  };
  const submitRename = () => {
    if (renamingId && renameValue.trim()) onRenameAiSession(renamingId, renameValue);
    setRenamingId(''); setRenameValue('');
  };
  const handleBatchDelete = () => {
    if (selectedIds.size === 0) return;
    onDeleteAiSessions(Array.from(selectedIds));
    setSelectedIds(new Set());
  };

  // 提取会话预览（最后一条助手消息的首行）
  const getPreview = (s: AISession): string => {
    const msgs = s.messages || [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'assistant') {
        try {
          const payload = JSON.parse(msgs[i].content);
          if (payload && payload.outputs) {
            const firstOut = Object.values(payload.outputs)[0] as string | undefined;
            if (firstOut) return firstOut.replace(/\s+/g, ' ').slice(0, 60);
          }
        } catch {}
        return msgs[i].content.replace(/\s+/g, ' ').slice(0, 60);
      }
    }
    const firstUser = msgs.find(m => m.role === 'user');
    return firstUser ? firstUser.content.replace(/\s+/g, ' ').slice(0, 60) : '（空对话）';
  };

  const formatTime = (iso: string | null): string => {
    if (!iso) return '';
    const d = new Date(iso);
    const now = new Date();
    const diff = now.getTime() - d.getTime();
    if (diff < 60000) return '刚刚';
    if (diff < 3600000) return `${Math.floor(diff / 60000)}分钟前`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}小时前`;
    if (diff < 604800000) return `${Math.floor(diff / 86400000)}天前`;
    return `${d.getMonth() + 1}/${d.getDate()}`;
  };

  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!conceptAiAssisting && conceptAiPrompt.trim()) {
        onExecuteConceptAi();
      }
    }
  };

  // AI协同创作模式
  if (conceptAiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <button className="btn-ghost-sm" onClick={onCancelConceptAi} disabled={conceptAiAssisting}>取消</button>
        </div>
        {/* 技能包多选器（可折叠） */}
        {skillPacks.length > 0 && (
          <div className="skill-pack-collapsible">
            <button className="skill-pack-toggle" onClick={() => setSkillExpanded(v => !v)} disabled={conceptAiAssisting}>
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
                  disabled={conceptAiAssisting}
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
        <p className="text-muted" style={{marginBottom:8}}>告诉AI你想生成什么构思内容，AI会结合故事设定和已勾选的技能包来创作</p>
        <div className="ai-prompt-section ai-prompt-vertical">
          <textarea
            className="input bible-ai-prompt-input"
            rows={6}
            value={conceptAiPrompt}
            onChange={e => onEditConceptAiPrompt(e.target.value)}
            onKeyDown={handlePromptKeyDown}
            placeholder="例如：扩展当前构思，增加核心卖点、目标读者、主线冲突、独特亮点..."
            disabled={conceptAiAssisting}
            autoFocus
          />
          <div className="ai-prompt-bottom-row">
            <button className="btn-primary ai-prompt-submit" onClick={onExecuteConceptAi} disabled={conceptAiAssisting || !conceptAiPrompt.trim()}>
              {conceptAiAssisting ? '⏳ AI创作中...' : '🚀 发送'}
            </button>
          </div>
        </div>
        {conceptAiError && <div className="error-msg" style={{marginTop:8}}>{conceptAiError}</div>}
        {conceptAiAssisting && (
          <div className="bible-ai-loading">
            <div className="loading-spinner" />
            <p>AI正在结合{selectedSkillPacks.length > 0 ? selectedSkillPacks.map(p => p.name).join('、') : '设定'}生成构思内容...</p>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="concept-panel">
      {/* 构思维度：AI 智驾入口（四Tab：设定/正文/去AI/校审） */}
      <div className="concept-input-section" style={{ alignItems: 'center', justifyContent: 'center', flex: 0, gap: 10, flexDirection: 'column' }}>
        <button
          className="btn-primary concept-ai-create-cta ai-master-banner"
          onClick={() => { if (bookId) openChatPanel(bookId, undefined, { tab: 'setting' }); }}
          disabled={!bookId}
          title="打开 AI 智驾：设定/正文/去AI/校审 四 Tab 协作创作。对话框里直接说「扫一下番茄新书榜」或「先看起点市场风向再创作」即可触发扫榜联动。"
        >
          <div className="cta-main-row">
            <CarLogo size={90} />
            <span className="cta-title">
              <span className="cta-ai">Ai</span>
              <span>智</span>
              <span>驾</span>
            </span>
            <span className="cta-sub-text"><span className="cta-ai">Ai</span>领航，人机共创</span>
          </div>
        </button>
      </div>

      {brainstormResult && (
        <div className="brainstorm-results">
          {brainstormResult.concept_analysis && (
            <div className="concept-analysis">
              <h4>📋 构思分析</h4>
              <p>{brainstormResult.concept_analysis}</p>
            </div>
          )}
          {Object.entries(brainstormResult.suggestions).map(([dim, suggestions]) => (
            <div key={dim} className="suggestion-group">
              <h4>{DIMENSION_LABELS[dim] || dim} <span className="suggestion-count">{suggestions.length}个方案</span></h4>
              <div className="suggestion-cards">
                {suggestions.map((s, i) => {
                  const adopted = adoptedSuggestions.has(`${dim}-${s.title}`);
                  return (
                    <div key={i} className={`suggestion-card ${adopted ? 'adopted' : ''}`}>
                      <div className="suggestion-card-title">{s.title}</div>
                      <div className="suggestion-card-desc">{s.description}</div>
                      <button
                        className={`btn-sm ${adopted ? 'btn-ghost-sm' : 'btn-primary-sm'}`}
                        onClick={() => onAdopt(dim, s)}
                        disabled={adopted}
                      >
                        {adopted ? '✓ 已采纳' : '采纳'}
                      </button>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}

      {brainstorming && (
        <div className="brainstorm-loading">
          <div className="loading-spinner" />
          <p>AI正在为你生成多维度创作方案...</p>
        </div>
      )}

      {/* 历史对话（不限数量，支持批量删除/编辑/重命名/继续对话） */}
      <div className="ai-chat-history" style={{ marginTop: 0 }}>
        <div className="ai-history-bar" style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8, flexWrap: 'nowrap', overflowX: 'auto' }}>
          <button
            className="btn-ghost-sm"
            onClick={() => setHistoryExpanded(v => !v)}
            style={{ padding: '2px 8px', fontSize: 12, fontWeight: 600, flexShrink: 0 }}
            title={historyExpanded ? '收起' : '展开'}
          >
            {historyExpanded ? '▼' : '▶'} 💬 历史对话 {aiSessions.length > 0 && `(${aiSessions.length})`}
          </button>
          <div style={{ display: 'flex', gap: 6, marginLeft: 'auto', flexShrink: 0 }}>
            {aiSessions.length > 0 && (
              <>
                <button
                  className="btn-ghost-sm"
                  onClick={toggleSelectAll}
                  style={{ padding: '2px 8px', fontSize: 11 }}
                  title="全选/取消全选"
                >
                  {selectedIds.size === aiSessions.length ? '取消全选' : '全选'}
                </button>
                <button
                  className="btn-ghost-sm"
                  onClick={handleBatchDelete}
                  disabled={selectedIds.size === 0}
                  style={{ padding: '2px 8px', fontSize: 11, color: selectedIds.size === 0 ? undefined : '#e74c3c' }}
                  title={selectedIds.size === 0 ? '先选中要删除的对话' : `删除选中的 ${selectedIds.size} 条对话`}
                >
                  🗑️ 删除选中 {selectedIds.size > 0 && `(${selectedIds.size})`}
                </button>
              </>
            )}
            <button
              className="btn-ghost-sm"
              onClick={onRefreshSessions}
              style={{ padding: '2px 8px', fontSize: 11 }}
              title="刷新列表"
            >
              🔄
            </button>
          </div>
        </div>

        {historyExpanded && (
          aiSessions.length === 0 ? (
            <div style={{ padding: '16px 12px', textAlign: 'center', fontSize: 12, color: 'var(--text-muted)', background: 'var(--bg-tertiary)', borderRadius: 8, border: '1px dashed var(--border-color)' }}>
              暂无聊天记录。点击上方「AI 智驾」开始第一次创作，生成的内容会自动保存到这里。
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 360, overflowY: 'auto' }}>
              {aiSessions.map(s => {
                const checked = selectedIds.has(s.id);
                const isRenaming = renamingId === s.id;
                return (
                  <div
                    key={s.id}
                    className="chat-history-item"
                    style={{
                      display: 'flex', flexDirection: 'column', gap: 6, padding: '8px 10px',
                      background: 'var(--bg-secondary)', borderRadius: 8,
                      border: `1px solid ${checked ? 'var(--accent)' : 'var(--border-color)'}`,
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleSelect(s.id)}
                      style={{ marginTop: 3, flexShrink: 0 }}
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      {isRenaming ? (
                        <input
                          className="input"
                          value={renameValue}
                          onChange={e => setRenameValue(e.target.value)}
                          onBlur={submitRename}
                          onKeyDown={e => { if (e.key === 'Enter') submitRename(); if (e.key === 'Escape') { setRenamingId(''); setRenameValue(''); } }}
                          autoFocus
                          style={{ fontSize: 13, padding: '4px 8px', width: '100%' }}
                        />
                      ) : (
                        <div
                          style={{ fontSize: 13, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', cursor: 'pointer' }}
                          onClick={() => onResumeAiSession(s)}
                          title="点击继续对话"
                        >
                          {s.title || '未命名对话'}
                        </div>
                      )}
                      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {getPreview(s)}
                      </div>
                      <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>
                        {formatTime(s.updated_at || s.created_at)} · {(s.messages || []).length} 条消息
                      </div>
                    </div>
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'row', gap: 6, justifyContent: 'flex-end', flexShrink: 0, paddingTop: 2, borderTop: '1px dashed var(--border-color)' }}>
                      <button
                        className="btn-ghost-sm"
                        onClick={() => onResumeAiSession(s)}
                        style={{ padding: '3px 10px', fontSize: 11 }}
                        title="打开并继续与 AI 对话，可提修改意见"
                      >
                        💬 继续
                      </button>
                      <button
                        className="btn-ghost-sm"
                        onClick={() => startRename(s.id, s.title || '未命名对话')}
                        disabled={isRenaming}
                        style={{ padding: '3px 10px', fontSize: 11 }}
                        title="重命名"
                      >
                        ✏️ 重命名
                      </button>
                      <button
                        className="btn-ghost-sm"
                        onClick={() => onDeleteAiSessions([s.id])}
                        style={{ padding: '3px 10px', fontSize: 11, color: '#e74c3c' }}
                        title="删除"
                      >
                        🗑️ 删除
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )
        )}
      </div>
    </div>
  );
}
