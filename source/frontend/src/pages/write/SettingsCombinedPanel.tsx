/** SettingsCombinedPanel —— 自 WritePage.tsx 拆出（P2b 巨石拆分，逻辑零改动）。 */
import { useState } from 'react';
import { api } from '../../api';
import type { BookBible, SkillPack } from '../../types';
import { SkillPackGroupedList, collapseNewlines, extractSkillPrompt } from './write-shared';

/* ===== 设定面板（核心规则 + 文风指南，双子Tab） ===== */
export function SettingsCombinedPanel(props: {
  bookId: string;
  bible: BookBible | null;
  onBibleUpdate: (b: BookBible) => void;
  concept: string;
  hasChapters: boolean;
  dimAnalyzing: boolean;
  onAnalyzeDimension: (dim: string) => void;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
  showConfirm: (message: string, onConfirm: () => void) => void;
  onOpenAiCreate: (field: string) => void;
}) {
  const { bookId, bible, onBibleUpdate, concept, hasChapters, dimAnalyzing, onAnalyzeDimension } = props;
  const [subTab, setSubTab] = useState<'rules' | 'style'>('rules');
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState('');
  const [saving, setSaving] = useState(false);
  const [aiAssisting, setAiAssisting] = useState(false);
  const [aiMode, setAiMode] = useState(false);
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiError, setAiError] = useState('');
  const [skillExpanded, setSkillExpanded] = useState(false);

  const { skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks } = props;
  const selectedCount = selectedSkillPackIds.length;

  const fieldMap = { rules: 'key_rules', style: 'style_guide' } as const;
  const labelMap = { rules: '设定', style: '文风' } as const;
  const iconMap = { rules: '⚙️', style: '🎨' } as const;
  const placeholderMap = {
    rules: '核心规则、能力限制、世界观禁忌、力量体系/科技树…每条规则单独列出。',
    style: '叙事风格、语言调性、节奏把控、常用句式、语感参考。例：冷硬直给/细腻抒情/幽默吐槽/古风雅致…',
  } as const;

  const currentField = fieldMap[subTab];
  const currentContent = bible ? (bible as any)[currentField] || '' : '';

  function startEdit() { setEditValue(collapseNewlines(currentContent)); setEditing(true); }  // 全局：段间不留空一行

  async function saveEdit() {
    if (!bookId) return;
    setSaving(true);
    try {
      // 全局：段间不留空一行 → 保存时压缩
      const updated = await api.updateBible(bookId, { [currentField]: collapseNewlines(editValue) } as any);
      onBibleUpdate(updated);
      setEditing(false);
      // 保存后派发自定义事件，通知AI智驾刷新创作进度
      try { window.dispatchEvent(new CustomEvent('app:progress-needs-refresh', { detail: { field: currentField } })); } catch {}
    } catch (e: any) { alert('保存失败: ' + e.message); }
    setSaving(false);
  }

  async function executeAi() {
    if (!bookId) return;
    if (!aiPrompt.trim()) { alert('请输入创作要求'); return; }
    setAiAssisting(true); setAiError('');
    try {
      const prompt = subTab === 'rules'
        ? '根据以下构思，生成核心设定规则。包括：世界观必须遵循的规则、人物能力限制、禁忌事项。每条规则单独列出。'
        : '根据以下构思，生成文风指南。包括：叙事风格、语言调性、常用句式、节奏把控、行文语感；给出2-3条示例短句示范语感。';
      const contextConcept = concept || bible?.concept || '暂无构思';
      const skillKeys = subTab === 'rules'
        ? ['lock_facts', 'tomato_setting']
        : ['style_tone_master'];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0
        ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const messages = [
        { role: 'system', content: `你是专业网文创作助手。${skillNote}` },
        { role: 'user', content: `${prompt}\n\n构思：${contextConcept}\n\n已有内容：${currentContent.slice(0, 1000) || '无'}\n\n用户具体要求：${aiPrompt}` },
      ];
      const result = await api.aiChat(messages);
      setEditValue(collapseNewlines(result.content));  // 全局：段间不留空一行
      setEditing(true);
      setAiMode(false);
    } catch (e: any) { setAiError(e.message || 'AI辅助失败'); }
    setAiAssisting(false);
  }

  const skillSelector = skillPacks.length > 0 && (
    <div className="skill-pack-collapsible">
      <button className="skill-pack-toggle" onClick={() => setSkillExpanded(v => !v)} disabled={aiAssisting}>
        <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
        <span>📦 协同技能包</span>
        {selectedCount > 0 && <span className="skill-pack-toggle-badge">{selectedCount}</span>}
        <span className="skill-pack-toggle-hint">{skillExpanded ? '收起' : '展开'}</span>
      </button>
      {skillExpanded && (
        <>
          <SkillPackGroupedList skillPacks={skillPacks} selectedSkillPackIds={selectedSkillPackIds} onToggleSkillPack={onToggleSkillPack} disabled={aiAssisting} />
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

  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!aiAssisting && aiPrompt.trim()) executeAi();
    }
  };

  if (aiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <h3>{iconMap[subTab]} AI协同创作 · {labelMap[subTab]}</h3>
          <button className="btn-ghost-sm" onClick={() => { setAiMode(false); setAiError(''); }} disabled={aiAssisting}>取消</button>
        </div>
        {skillSelector}
        <p className="text-muted" style={{marginBottom:8}}>告诉AI你想生成的{labelMap[subTab]}内容，AI会结合故事设定和已勾选的技能包来创作</p>
        <div className="ai-prompt-section ai-prompt-vertical">
          <textarea className="input bible-ai-prompt-input" rows={6} value={aiPrompt}
            onChange={e => setAiPrompt(e.target.value)} onKeyDown={handlePromptKeyDown}
            placeholder={`例如：为${labelMap[subTab]}生成详细内容…`}
            disabled={aiAssisting} autoFocus />
          <div className="ai-prompt-bottom-row">
            <button className="btn-primary ai-prompt-submit" onClick={executeAi} disabled={aiAssisting || !aiPrompt.trim()}>
              {aiAssisting ? '⏳ 创作中...' : '🚀 发送'}
            </button>
          </div>
        </div>
        {aiError && <div className="error-msg" style={{marginTop:8}}>{aiError}</div>}
        {aiAssisting && (
          <div className="bible-ai-loading">
            <div className="loading-spinner" />
            <p>AI正在结合{selectedSkillPacks.length > 0 ? selectedSkillPacks.map(p => p.name).join('、') : '设定'}生成{labelMap[subTab]}...</p>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="bible-edit-panel">
      {/* 单行：设定/文风/AI创作/AI识别 四按钮平铺，space-between 均匀分布，等高同字号 */}
      <div className="bible-edit-header dims-single-row">
        <button className={`outline-sub-tab ${subTab === 'rules' ? 'active' : ''}`} onClick={() => { setSubTab('rules'); setEditing(false); }}>
          ⚙️ 设定
        </button>
        <button className={`outline-sub-tab ${subTab === 'style' ? 'active' : ''}`} onClick={() => { setSubTab('style'); setEditing(false); }}>
          🎨 文风
        </button>
        {!editing ? (
          <>
            <button className="btn-primary-sm" onClick={() => { setAiMode(true); }} disabled={aiAssisting} title={`AI生成${labelMap[subTab]}内容`}>
              {aiAssisting ? '⏳ 生成中...' : '✨ AI创作'}
            </button>
            <button className="btn-ghost-sm" onClick={() => onAnalyzeDimension(currentField)} disabled={dimAnalyzing || !hasChapters} title={hasChapters ? `AI分析已有章节，自动识别${labelMap[subTab]}内容` : '需要先创建章节才能AI识别'}>
              {dimAnalyzing ? '🤖 识别中...' : '🔍 AI识别'}
            </button>
          </>
        ) : (
          <>
            <button className="btn-ghost-sm" onClick={() => setEditing(false)}>取消</button>
            <button className="btn-primary-sm" onClick={saveEdit} disabled={saving}>
              {saving ? '保存中...' : '💾 保存'}
            </button>
          </>
        )}
      </div>

      {editing ? (
        <textarea className="input bible-editor-textarea paper-textarea" rows={18} value={editValue}
          onChange={e => setEditValue(e.target.value)} placeholder={placeholderMap[subTab]} autoFocus />
      ) : currentContent ? (
        /* —— 笔记本类纸：设定 / 文风 落地内容；点击纸区域直接进入编辑 —— */
        <div className="paper-notebook bible-paper setting-paper" data-dim={labelMap[subTab]} onClick={startEdit} style={{cursor:'text'}}>
          <div className="paper-inner">
            <pre className="bible-text">{collapseNewlines(currentContent)}</pre>
            <div className="paper-footer" aria-hidden="true">
              <span className="paper-footer-left">— 蚂蚁世界观百科 —</span>
              <span className="paper-footer-right">— 智驾 —</span>
            </div>
          </div>
        </div>
      ) : (
        <div className="bible-empty" onClick={startEdit}>
          <p>暂无{labelMap[subTab]}内容</p>
          <p className="text-muted">点击此处编辑，或点击上方 AI创作</p>
        </div>
      )}
    </div>
  );
}
