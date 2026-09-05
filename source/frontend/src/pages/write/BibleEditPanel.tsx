/** BibleEditPanel —— 自 WritePage.tsx 拆出（P2b 巨石拆分，逻辑零改动）。 */
import { useState } from 'react';
import type { SkillPack } from '../../types';
import { ALL_TABS, SkillPackGroupedList, collapseNewlines } from './write-shared';

/* ===== 内容编辑面板 ===== */
export function BibleEditPanel(props: {
  tab: typeof ALL_TABS[0];
  bookTitle: string;
  content: string;
  editing: boolean;
  editValue: string;
  saving: boolean;
  aiAssisting: boolean;
  aiError: string;
  bibleAiMode: boolean;
  bibleAiPrompt: string;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
  hasChapters: boolean;
  dimAnalyzing: boolean;
  onAnalyzeDimension: () => void;
  onStartEdit: () => void;
  onSaveEdit: () => void;
  onCancelEdit: () => void;
  onEditChange: (v: string) => void;
  onAIAssist: () => void;
  onExecuteAi: () => void;
  onCancelAi: () => void;
  onEditAiPrompt: (v: string) => void;
  onDelete: () => void;
  onOpenAiCreate: (field: string) => void;
}) {
  const { tab, content, editing, editValue, saving, aiAssisting, aiError, bibleAiMode, bibleAiPrompt,
    skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks,
    hasChapters, dimAnalyzing, onAnalyzeDimension,
    onStartEdit, onSaveEdit, onCancelEdit, onEditChange, onExecuteAi, onCancelAi, onEditAiPrompt, onDelete, onOpenAiCreate } = props;

  const [skillExpanded, setSkillExpanded] = useState(false);
  const [showTips, setShowTips] = useState(false);
  const selectedCount = selectedSkillPackIds.length;

  // 技能包多选器（可折叠）
  const skillSelector = skillPacks.length > 0 && (
    <div className="skill-pack-collapsible">
      <button
        className="skill-pack-toggle"
        onClick={() => setSkillExpanded(v => !v)}
        disabled={aiAssisting}
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
            disabled={aiAssisting}
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
  );

  // Enter快捷发送（Shift+Enter换行）
  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!aiAssisting && bibleAiPrompt.trim()) {
        onExecuteAi();
      }
    }
  };

  // AI协同创作模式
  if (bibleAiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <h3>{tab.icon} AI协同创作 · {tab.label}</h3>
          <button className="btn-ghost-sm" onClick={onCancelAi} disabled={aiAssisting}>取消</button>
        </div>
        {skillSelector}
        <p className="text-muted" style={{marginBottom:8}}>告诉AI你想生成什么内容，AI会结合故事设定和已勾选的技能包来创作</p>
        <div className="ai-prompt-section ai-prompt-vertical">
          <textarea
            className="input bible-ai-prompt-input"
            rows={6}
            value={bibleAiPrompt}
            onChange={e => onEditAiPrompt(e.target.value)}
            onKeyDown={handlePromptKeyDown}
            placeholder={`例如：为${tab.label}生成详细内容，包含3-5个关键要素...`}
            disabled={aiAssisting}
            autoFocus
          />
          <div className="ai-prompt-bottom-row">
            <button className="btn-primary ai-prompt-submit" onClick={onExecuteAi} disabled={aiAssisting || !bibleAiPrompt.trim()}>
              {aiAssisting ? '⏳ 创作中...' : '🚀 发送'}
            </button>
          </div>
        </div>
        {aiError && <div className="error-msg" style={{marginTop:8}}>{aiError}</div>}
        {aiAssisting && (
          <div className="bible-ai-loading">
            <div className="loading-spinner" />
            <p>AI正在结合{selectedSkillPacks.length > 0 ? selectedSkillPacks.map(p => p.name).join('、') : '设定'}生成{tab.label}内容...</p>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="bible-edit-panel">
      <div className="bible-edit-header">
        <div className="bible-edit-actions" style={{flexShrink:0}}>
          {!editing ? (
            <>
              <button className="btn-ghost-sm" onClick={onAnalyzeDimension} disabled={dimAnalyzing || !hasChapters} title={hasChapters ? 'AI分析已有章节，自动识别此维度内容' : '需要先创建章节才能AI识别'}>
                {dimAnalyzing ? '🤖 识别中...' : '🔍 AI识别'}
              </button>
              {content && (
                <button className="btn-ghost-sm" onClick={onDelete} style={{color:'#e74c3c'}} title="清空此维度内容">🗑️</button>
              )}
            </>
          ) : (
            <>
              <button className="btn-ghost-sm" onClick={onCancelEdit}>取消</button>
              <button className="btn-primary-sm" onClick={onSaveEdit} disabled={saving}>
                {saving ? '保存中...' : '💾 保存'}
              </button>
            </>
          )}
        </div>
      </div>
      {aiError && <div className="error-msg" style={{marginBottom:8}}>{aiError}</div>}
      {editing ? (
        <>
          <textarea
            className="input bible-editor-textarea paper-textarea"
            rows={18}
            value={editValue}
            onChange={e => onEditChange(e.target.value)}
            placeholder={tab.placeholder}
            autoFocus
          />
          <div className="bible-edit-footer">
            <span className="bible-edit-count">{editValue.length}字符</span>
            <span className="bible-edit-hint">💡 {tab.placeholder}</span>
          </div>
        </>
      ) : content ? (
        <>
          {/* —— 笔记本类纸：维度落地内容；点击纸区域直接进入编辑 —— */}
          <div className="paper-notebook bible-paper" data-dim={tab.label} onClick={onStartEdit} style={{cursor:'text'}}>
            <div className="paper-inner">
              <pre className="bible-text">{collapseNewlines(content)}</pre>
              <div className="paper-footer" aria-hidden="true">
                <span className="paper-footer-left">— 蚂蚁世界观百科 —</span>
                <span className="paper-footer-right">— 智驾 —</span>
              </div>
            </div>
          </div>
          <button className="bible-tips-toggle" onClick={() => setShowTips(v => !v)}>
            {showTips ? '▼ 收起提示' : '▶ 创作提示'}
          </button>
          {showTips && (
            <div className="bible-tips-box">
              <p>✨ 点击「AI创作」可让AI根据构思和设定自动生成{tab.label}内容</p>
              <p>🔍 点击「AI识别」可从已有章节中提取{tab.label}信息（无章节时自动从设定/大纲/剧情维度提取）</p>
              <p>✏️ 点击内容区域可直接编辑</p>
              {selectedSkillPacks.length > 0 && <p>📦 已选{selectedCount}个技能包协同创作</p>}
              <p style={{marginTop:6,color:'var(--accent)',fontSize:12,borderTop:'1px dashed var(--border-color)',paddingTop:6}}>
                🔗 维度协同工作流（参考：番茄金番作者 / 长篇小说创作全流程 / 长篇小说防遗忘系统）：<br/>
                构思→设定→大纲→剧情→人物 相互反哺；大纲⇄剧情双向（提取各卷/反生成总纲）；<br/>
                各维度AI识别会读取其他维度作"已确认"上下文保持一致；<br/>
                🛡️ 伏笔面板「防遗忘检查」定期扫描一致性/伏笔/叙事债务，防长篇遗忘。
              </p>
            </div>
          )}
        </>
      ) : (
        <div className="bible-empty" onClick={onStartEdit}>
          <span className="bible-empty-icon">{tab.icon}</span>
          <p>暂无{tab.label}内容</p>
          <p className="text-muted">点击此处编辑，或使用上方按钮AI创作</p>
          <div className="bible-empty-actions">
            <button className="btn-primary-sm" onClick={(e) => { e.stopPropagation(); onOpenAiCreate(tab.field); }} disabled={aiAssisting}>
              {aiAssisting ? '⏳ 生成中...' : '✨ AI创作'}
            </button>
            <button className="btn-ghost-sm" onClick={(e) => { e.stopPropagation(); onAnalyzeDimension(); }} disabled={dimAnalyzing || !hasChapters} title={hasChapters ? 'AI分析已有章节，自动识别' : '需要先创建章节才能AI识别'}>
              {dimAnalyzing ? '⏳ 识别中...' : '🔍 AI识别'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
