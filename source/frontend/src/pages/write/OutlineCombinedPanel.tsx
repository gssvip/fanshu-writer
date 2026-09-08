/** OutlineCombinedPanel —— 自 WritePage.tsx 拆出（P2b 巨石拆分，逻辑零改动）。 */
import { useState } from 'react';
import { api } from '../../api';
import type { BookBible, SkillPack } from '../../types';
import { SkillPackGroupedList, collapseNewlines, extractSkillPrompt } from './write-shared';

/* ===== 大纲合并面板（大纲+世界观） ===== */
export function OutlineCombinedPanel(props: {
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
  totalVolumes: number;
}) {
  const { bookId, bible, onBibleUpdate, concept, hasChapters, dimAnalyzing, onAnalyzeDimension, showConfirm, totalVolumes } = props;
  const [subTab, setSubTab] = useState<'outline' | 'worldview'>('outline');
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

  const fieldMap = { outline: 'plot_design', worldview: 'worldbuilding' } as const;
  const labelMap = { outline: '大纲', worldview: '世界观' } as const;
  const placeholderMap = {
    outline: '主线冲突、卷纲拆解、章节规划...',
    worldview: '世界背景、力量体系、社会结构、地理概况、历史脉络...',
  } as const;

  const currentField = fieldMap[subTab];
  const currentContent = bible ? (bible as any)[currentField] || '' : '';

  function startEdit() {
    setEditValue(collapseNewlines(currentContent));  // 全局：段间不留空一行
    setEditing(true);
  }

  async function saveEdit() {
    if (!bookId) return;
    setSaving(true);
    try {
      // 全局：段间不留空一行 → 保存时压缩，后端存储就没有双换行
      const updated = await api.updateBible(bookId, { [currentField]: collapseNewlines(editValue) } as any);
      onBibleUpdate(updated);
      setEditing(false);
      try { window.dispatchEvent(new CustomEvent('app:progress-needs-refresh', { detail: { field: currentField } })); } catch {}
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setSaving(false);
  }

  async function executeAi() {
    if (!bookId) return;
    if (!aiPrompt.trim()) { alert('请输入创作要求'); return; }
    setAiAssisting(true);
    setAiError('');
    try {
      const prompt = subTab === 'outline'
        ? '根据以下构思，生成故事大纲。包括：核心主线、分卷规划（每卷目标）、关键转折点、高潮设计、结局走向。'
        : '根据以下构思，生成详细的世界观设定。包括：世界背景、力量体系/科技水平、社会结构、地理概况、历史脉络。';
      const contextConcept = concept || bible?.concept || '暂无构思';
      const skillKeys = subTab === 'outline'
        ? ['master_outline', 'volume_breakdown', 'chapter_plan', 'tomato_outline']
        : ['lock_facts', 'tomato_setting'];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const messages = [
        { role: 'system', content: `你是专业网文创作助手。${skillNote}` },
        { role: 'user', content: `${prompt}\n\n构思：${contextConcept}\n\n已有内容：${currentContent.slice(0, 1000) || '无'}\n\n用户具体要求：${aiPrompt}` },
      ];
      const result = await api.aiChat(messages);
      setEditValue(collapseNewlines(result.content));  // 全局：段间不留空一行
      setEditing(true);
      setAiMode(false);
    } catch (e: any) {
      setAiError(e.message || 'AI辅助失败');
    }
    setAiAssisting(false);
  }

  function handleDelete() {
    if (!bookId) return;
    showConfirm(`确定清空「${labelMap[subTab]}」的所有内容？此操作不可撤销。`, async () => {
      try {
        const updated = await api.updateBible(bookId, { [currentField]: '' } as any);
        onBibleUpdate(updated);
        try { window.dispatchEvent(new CustomEvent('app:progress-needs-refresh', { detail: { field: currentField } })); } catch {}
      } catch (e: any) {
        alert('删除失败: ' + e.message);
      }
    });
  }

  // ==== 滚动生成工作流状态 ====
  // outlineWorkflowLoading: '' | 'master' | 'volume' | 'all'
  const [outlineWorkflowLoading, setOutlineWorkflowLoading] = useState<'' | 'master' | 'volume' | 'all'>('');
  const [outlineWorkflowProgress, setOutlineWorkflowProgress] = useState('');
  // 每卷章节数（默认50，与后端约定一致）
  const CHAPTERS_PER_VOLUME = 50;

  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!aiAssisting && aiPrompt.trim()) executeAi();
    }
  };

  // 生成五幕式总纲（写入 plot_design）
  async function generateOutlineMaster() {
    if (!bookId) return;
    // 卷数默认值权威来源：book.total_volumes（用户创建小说时填的 25 卷），
    // 绝对不再默认写死 10——否则用户一弹窗回车就会把 25 卷覆盖成 10 卷。
    // 未设定时建议值给 5（五幕一幕一卷的最小映射），由用户确认/修改，禁止暗示十卷。
    const defaultFromBook = (totalVolumes && totalVolumes >= 1) ? String(totalVolumes) : '5';
    // 若用户创建时已经明确设定过卷数（≥1），**不再弹窗骚扰**直接使用；
    // 只有 totalVolumes 不可用时才弹窗让用户补填（避免用户手滑回车把 25 写成 10）。
    let volumeCount: number;
    if (totalVolumes && totalVolumes >= 1) {
      const confirmRes = window.confirm(`检测到创建小说时设定的卷数为 ${totalVolumes} 卷，是否按此生成五幕式总纲？（点「取消」可手动输入其他卷数）`);
      if (confirmRes) {
        volumeCount = totalVolumes;
      } else {
        const input = window.prompt('请输入小说预计需要的卷数（≥1，不设上限）：', defaultFromBook);
        if (input === null) return;
        const vc = parseInt(input);
        if (!vc || vc < 1) { alert('卷数需为大于等于 1 的整数'); return; }
        volumeCount = vc;
      }
    } else {
      const input = window.prompt('请输入小说预计需要的卷数（≥1，不设上限）：', defaultFromBook);
      if (input === null) return; // 用户取消
      const vc = parseInt(input);
      if (!vc || vc < 1) { alert('卷数需为大于等于 1 的整数'); return; }
      volumeCount = vc;
    }
    setOutlineWorkflowLoading('master');
    setOutlineWorkflowProgress(`⏳ 按五幕式生成总纲中（共 ${volumeCount} 卷）...`);
    try {
      const result = await api.aiOutlineMaster(bookId, selectedSkillPackIds, undefined, CHAPTERS_PER_VOLUME, volumeCount);
      // 把返回的 master_outline 填入大纲编辑器（设置 plotDesign state）
      const updated = await api.updateBible(bookId, { plot_design: collapseNewlines(result.master_outline) } as any);  // 段间不留空一行
      onBibleUpdate(updated);
      setEditValue(collapseNewlines(result.master_outline));  // 段间不留空一行
      setEditing(true);
      setOutlineWorkflowProgress('');
      alert(`五幕式总纲已生成并填入大纲（共 ${result.volume_count} 卷）`);
    } catch (e: any) {
      alert('生成总纲失败: ' + e.message);
      setOutlineWorkflowProgress('');
    }
    setOutlineWorkflowLoading('');
  }

  // AI协同创作模式
  if (aiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <h3>📋 AI协同创作 · {labelMap[subTab]}</h3>
          <button className="btn-ghost-sm" onClick={() => { setAiMode(false); setAiError(''); }} disabled={aiAssisting}>取消</button>
        </div>
        {skillPacks.length > 0 && (
          <div className="skill-pack-collapsible">
            <button className="skill-pack-toggle" onClick={() => setSkillExpanded(v => !v)} disabled={aiAssisting}>
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
        )}
        <p className="text-muted" style={{marginBottom:8}}>告诉AI你想生成什么内容</p>
        <div className="ai-prompt-section ai-prompt-vertical">
          <textarea
            className="input bible-ai-prompt-input"
            rows={6}
            value={aiPrompt}
            onChange={e => setAiPrompt(e.target.value)}
            onKeyDown={handlePromptKeyDown}
            placeholder={`例如：为${labelMap[subTab]}生成详细内容...`}
            disabled={aiAssisting}
            autoFocus
          />
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
            <p>AI正在生成{labelMap[subTab]}内容...</p>
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
              <button className="btn-ghost-sm" onClick={() => onAnalyzeDimension(subTab === 'outline' ? 'outline' : 'worldview')} disabled={dimAnalyzing || !hasChapters} title={hasChapters ? 'AI分析已有章节，自动识别' : '需要先创建章节才能AI识别'}>
                {dimAnalyzing ? '🤖 识别中...' : '🔍 AI识别'}
              </button>
              {currentContent && (
                <button className="btn-ghost-sm" onClick={handleDelete} style={{color:'#e74c3c'}}>🗑️ 删除</button>
              )}
            </>
          ) : (
            <>
              <button className="btn-ghost-sm" onClick={() => setEditing(false)}>取消</button>
              <button className="btn-primary-sm" onClick={saveEdit} disabled={saving}>
                {saving ? '保存中...' : '保存'}
              </button>
            </>
          )}
        </div>
      </div>

      {/* 子Tab切换 */}
      <div className="outline-sub-tabs">
        <button className={`outline-sub-tab ${subTab === 'outline' ? 'active' : ''}`} onClick={() => { setSubTab('outline'); setEditing(false); }}>
          📋 大纲
        </button>
        <button className={`outline-sub-tab ${subTab === 'worldview' ? 'active' : ''}`} onClick={() => { setSubTab('worldview'); setEditing(false); }}>
          🌍 世界观
        </button>
      </div>

      {/* 五幕式总纲生成（仅大纲tab显示） */}
      {subTab === 'outline' && (
        <div className="volume-calc-section" style={{ borderLeft: '3px solid var(--accent)', paddingLeft: 10, marginBottom: 8 }}>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            <button
              className="btn-primary-sm"
              onClick={generateOutlineMaster}
              disabled={outlineWorkflowLoading !== ''}
              title="生成五幕式总纲（写入大纲）"
            >
              {outlineWorkflowLoading === 'master' ? '⏳ 生成总纲中...' : '🎯 生成五幕式总纲'}
            </button>
          </div>
          {outlineWorkflowProgress && (
            <div style={{ fontSize: 12, color: 'var(--accent)', marginTop: 6 }}>{outlineWorkflowProgress}</div>
          )}
        </div>
      )}

      {editing ? (
        <textarea
          className="input bible-editor-textarea paper-textarea"
          rows={18}
          value={editValue}
          onChange={e => setEditValue(e.target.value)}
          placeholder={placeholderMap[subTab]}
          autoFocus
        />
      ) : currentContent ? (
        /* —— 笔记本类纸：大纲 / 世界观 落地内容；点击纸区域直接进入编辑 —— */
        <div className="paper-notebook bible-paper outline-paper" data-dim={labelMap[subTab]} onClick={startEdit} style={{cursor:'text'}}>
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
          <span className="bible-empty-icon">{subTab === 'outline' ? '📋' : '🌍'}</span>
          <p>暂无{labelMap[subTab]}内容</p>
          <p className="text-muted">点击编辑或使用AI创作</p>
        </div>
      )}
    </div>
  );
}
