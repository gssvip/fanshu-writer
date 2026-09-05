/** InventoryPanel —— 自 WritePage.tsx 拆出（P2b 巨石拆分，逻辑零改动）。 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api';
import type { BookBible, Chapter, SkillPack } from '../../types';
import { SkillPackGroupedList, extractSkillPrompt, safeText } from './write-shared';

/* ===== 物资库面板（按卷） ===== */
export function InventoryPanel(props: {
  bookId: string;
  bible: BookBible | null;
  onBibleUpdate: (b: BookBible) => void;
  bookTitle: string;
  chapters: Chapter[];
  hasChapters: boolean;
  showConfirm: (message: string, onConfirm: () => void) => void;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
  onOpenAiCreate: () => void;
}) {
  const { bookId, bible, onBibleUpdate, chapters, hasChapters, showConfirm, skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks } = props;
  const [inventory, setInventory] = useState<any[]>([]);
  const [collapsedVols, setCollapsedVols] = useState<Set<number>>(new Set());
  // 每次进入维度默认折叠所有卷（tab 切换重新挂载，ref 重置）
  const invCollapseInitRef = useRef(false);
  const [analyzingVol, setAnalyzingVol] = useState('');
  const [aiMode, setAiMode] = useState(false);
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiAssisting, setAiAssisting] = useState(false);
  const [aiError, setAiError] = useState('');
  const [skillExpanded, setSkillExpanded] = useState(false);
  // 卷选择器
  const [volSelectorOpen, setVolSelectorOpen] = useState(false);
  // 按卷编辑
  const [editingVolIdx, setEditingVolIdx] = useState<number | null>(null);
  const [editVolJson, setEditVolJson] = useState('');

  // 从 chapters 表筛 is_volume 卷，作为可识别的卷列表
  const volumeChapters = chapters.filter(c => c.is_volume);

  // 解析 inventory（JSON 数组，每卷一条）
  useEffect(() => {
    if (!bible?.inventory) { setInventory([]); return; }
    try {
      const parsed = JSON.parse(bible.inventory);
      if (Array.isArray(parsed)) { setInventory(parsed); return; }
    } catch { /* not JSON */ }
    setInventory([]);
  }, [bible?.inventory]);

  async function saveInventory(newList: any[]) {
    setInventory(newList);
    try {
      const updated = await api.updateBible(bookId, { inventory: JSON.stringify(newList, null, 2) } as any);
      onBibleUpdate(updated);
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
  }

  function toggleVol(idx: number) {
    setCollapsedVols(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  // 合并卷列表：chapters.is_volume 卷 + inventory 已有卷
  const displayVolumes = useMemo(() => {
    const result: any[] = [];
    const usedIds = new Set<string>();
    // chapters 表的卷（可识别）
    for (const vc of volumeChapters) {
      const invData = inventory.find(v => v.volume_id === vc.id) || inventory.find(v => v.volume === vc.title);
      result.push({
        volume_id: vc.id,
        volume: vc.title,
        items: invData?.items || [],
        realms: invData?.realms || [],
        chapter_count: chapters.filter(c => c.parent_id === vc.id).length,
      });
      if (invData) { usedIds.add(invData.volume_id || ''); usedIds.add(invData.volume || ''); }
    }
    // inventory 已有但无对应章节卷
    for (const v of inventory) {
      const id = v.volume_id || '';
      const name = v.volume || '';
      if (!usedIds.has(id) && !usedIds.has(name)) {
        result.push({ ...v, chapter_count: 0 });
      }
    }
    return result;
  }, [volumeChapters, inventory, chapters]);

  // 首次有卷数据时默认折叠全部卷（每次切换到该维度 tab 重新挂载，ref 重置，实现每次进入默认折叠）
  useEffect(() => {
    if (invCollapseInitRef.current) return;
    if (displayVolumes.length > 0) {
      invCollapseInitRef.current = true;
      setCollapsedVols(new Set(displayVolumes.map((_, idx) => idx)));
    }
  }, [displayVolumes]);

  // AI识别指定卷的物资
  async function handleAnalyzeVolume(volId: string, volTitle: string) {
    showConfirm(`将用 AI 分析「${volTitle}」的章节内容，识别势力/角色拥有的物品、功法、法宝、境界等。是否继续？`, async () => {
      setAnalyzingVol(volId || volTitle);
      try {
        const result = await api.analyzeInventoryVolume(bookId, volId, volTitle, selectedSkillPackIds);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已为「${volTitle}」识别 ${result.volume_data?.items?.length || 0} 项物资`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingVol('');
    });
  }

  // 删除某卷的物资数据
  async function deleteVolumeInventory(idx: number) {
    const vol = displayVolumes[idx];
    if (!vol) return;
    showConfirm(`确定删除「${vol.volume || '该卷'}」的物资数据？`, async () => {
      const newList = inventory.filter((v: any) => {
        const vId = v.volume_id || '';
        const vName = v.volume || '';
        if (vol.volume_id && vId === vol.volume_id) return false;
        if (vol.volume && vName === vol.volume) return false;
        return true;
      });
      await saveInventory(newList);
    });
  }

  // 开始按卷编辑：将该卷的完整数据（items + realms）序列化为 JSON 供编辑
  function startEditVolInventory(idx: number) {
    const vol = displayVolumes[idx];
    if (!vol) return;
    const editTarget = {
      volume_id: vol.volume_id || '',
      volume: vol.volume || '',
      items: vol.items || [],
      realms: vol.realms || [],
    };
    setEditingVolIdx(idx);
    setEditVolJson(JSON.stringify(editTarget, null, 2));
    setCollapsedVols(prev => { const n = new Set(prev); n.delete(idx); return n; });
  }

  // 保存按卷编辑：解析编辑后的 JSON，写回 inventory
  async function saveEditVolInventory(idx: number) {
    try {
      const parsed = JSON.parse(editVolJson);
      const vol = displayVolumes[idx];
      const matchKey = vol.volume_id || vol.volume;
      const newList = [...inventory];
      const existIdx = newList.findIndex((v: any) => (v.volume_id || v.volume) === matchKey);
      const entry = {
        volume_id: parsed.volume_id || vol.volume_id || '',
        volume: parsed.volume || vol.volume || '',
        items: Array.isArray(parsed.items) ? parsed.items : (vol.items || []),
        realms: Array.isArray(parsed.realms) ? parsed.realms : (vol.realms || []),
      };
      if (existIdx >= 0) {
        newList[existIdx] = { ...newList[existIdx], ...entry };
      } else {
        newList.push(entry);
      }
      await saveInventory(newList);
      setEditingVolIdx(null);
      setEditVolJson('');
    } catch (e: any) {
      alert('保存失败：JSON 格式错误 - ' + e.message);
    }
  }

  // AI协同创作（生成物资数据）
  async function executeAi() {
    if (!aiPrompt.trim()) { alert('请输入创作要求'); return; }
    setAiAssisting(true);
    setAiError('');
    try {
      const skillKeys = ['lock_facts', 'tomato_setting'];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const contextConcept = bible?.concept || '暂无构思';
      const messages = [
        { role: 'system', content: `你是专业网文世界观分析师。请根据用户要求生成物资库（物品、功法、法宝、境界等）。${skillNote}` },
        { role: 'user', content: `构思：${contextConcept}\n已有物资：${bible?.inventory?.slice(0, 500) || '无'}\n\n用户要求：${aiPrompt}\n\n请生成按卷划分的物资库，用JSON数组格式输出，每个元素包含 volume(卷名)、items(物资数组，含owner/name/category/description/status)、realms(境界数组，含character/realm/progress)。` },
      ];
      const result = await api.aiChat(messages);
      let newVols: any[] = [];
      try {
        const match = result.content.match(/\[[\s\S]*\]/);
        if (match) newVols = JSON.parse(match[0]);
      } catch { /* parse fail */ }
      if (newVols.length > 0) {
        await saveInventory([...inventory, ...newVols]);
        setAiMode(false);
        setAiPrompt('');
      } else {
        setAiError('AI返回格式无法解析，请重试');
      }
    } catch (e: any) {
      setAiError(e.message || 'AI创作失败');
    }
    setAiAssisting(false);
  }

  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!aiAssisting && aiPrompt.trim()) executeAi();
    }
  };

  const CATEGORY_LABELS: Record<string, string> = {
    '物品': '📦', '功法': '📖', '法宝': '⚔️', '境界': '⚡', '灵宠': '🐾', '领地': '🗺️', '资源': '💎', '其他': '🔹',
  };

  // AI协同创作模式
  if (aiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <h3>🎒 AI协同创作 · 物资库</h3>
          <button className="btn-ghost-sm" onClick={() => { setAiMode(false); setAiError(''); }} disabled={aiAssisting}>取消</button>
        </div>
        {skillPacks.length > 0 && (
          <div className="skill-pack-collapsible">
            <button className="skill-pack-toggle" onClick={() => setSkillExpanded(v => !v)} disabled={aiAssisting}>
              <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
              <span>📦 协同技能包</span>
              {selectedSkillPackIds.length > 0 && <span className="skill-pack-toggle-badge">{selectedSkillPackIds.length}</span>}
            </button>
            {skillExpanded && (
              <SkillPackGroupedList
                skillPacks={skillPacks}
                selectedSkillPackIds={selectedSkillPackIds}
                onToggleSkillPack={onToggleSkillPack}
                disabled={aiAssisting}
              />
            )}
          </div>
        )}
        <div className="ai-prompt-vertical">
          <textarea className="input bible-ai-prompt-input" rows={6} value={aiPrompt} onChange={e => setAiPrompt(e.target.value)} onKeyDown={handlePromptKeyDown} placeholder="例如：生成三卷的物资库，每卷包含主角和主要势力的法宝、功法、境界..." disabled={aiAssisting} autoFocus />
          <div className="ai-prompt-bottom-row">
            <button className="btn-primary ai-prompt-submit" onClick={executeAi} disabled={aiAssisting || !aiPrompt.trim()}>{aiAssisting ? '⏳ 创作中...' : '🚀 发送'}</button>
          </div>
        </div>
        {aiError && <div className="error-msg" style={{marginTop:8}}>{aiError}</div>}
        {aiAssisting && <div className="bible-ai-loading"><div className="loading-spinner" /><p>AI正在生成物资库...</p></div>}
      </div>
    );
  }

  return (
    <div className="bible-edit-panel">
      <div className="bible-edit-header">
        <div className="bible-edit-actions" style={{position:'relative',flexShrink:0}}>
          {(
            <>
              <button className="btn-ghost-sm" onClick={() => setVolSelectorOpen(v => !v)} disabled={!!analyzingVol || !hasChapters} title={hasChapters ? '选择卷进行AI识别' : '需要先创建章节才能AI识别'}>
                {analyzingVol ? '🤖 识别中...' : '🔍 AI识别'}
              </button>
              {volSelectorOpen && (
                <div className="vol-selector-dropdown" style={{position:'absolute',top:'100%',right:0,marginTop:4,background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:8,padding:6,minWidth:180,zIndex:100,boxShadow:'0 4px 12px rgba(0,0,0,0.15)'}}>
                  <div style={{fontSize:12,color:'var(--text-muted)',padding:'4px 8px',borderBottom:'1px solid var(--border)',marginBottom:4}}>选择要识别的卷</div>
                  <button className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeVolume('', '全部章节'); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📚 全部章节</button>
                  {displayVolumes.map((vol, idx) => (
                    <button key={idx} className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeVolume(vol.volume_id || '', vol.volume || `第${idx + 1}卷`); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📖 {vol.volume || `第${idx + 1}卷`}{vol.chapter_count ? ` (${vol.chapter_count}章)` : ''}</button>
                  ))}
                  <button onClick={() => setVolSelectorOpen(false)} style={{display:'block',width:'100%',textAlign:'center',padding:'4px',background:'transparent',border:'none',cursor:'pointer',color:'var(--text-muted)',fontSize:12,marginTop:2}}>取消</button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
      <p className="text-muted" style={{fontSize:12, marginBottom:8}}>
        按卷记录主要势力和角色拥有的物品、功法、法宝、境界等。点击「🔍 AI识别」选择卷进行识别。
      </p>

      {displayVolumes.length === 0 ? (
        <div className="bible-empty">
          <span className="bible-empty-icon">🎒</span>
          <p>暂无物资信息</p>
          <p className="text-muted">先在剧情维度创建分卷，或用顶部 AI 智驾 生成物资库</p>
          <div className="bible-empty-actions">
          </div>
        </div>
      ) : (
        <div className="plot-volume-list">
          {displayVolumes.map((vol, idx) => (
            <div key={idx} className="plot-volume-card">
              <div className="plot-volume-header" onClick={() => toggleVol(idx)} style={{cursor:'pointer'}}>
                <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{collapsedVols.has(idx) ? '▶' : '▼'}</span>
                <h4>{vol.volume || `第${idx + 1}卷`}</h4>
                {vol.chapter_count !== undefined && <span className="text-muted" style={{fontSize:12}}>{vol.chapter_count}章</span>}
                <span className="text-muted" style={{fontSize:12}}>{(vol.items || []).length}项物资</span>
                <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                  {analyzingVol === (vol.volume_id || vol.volume) && <span className="text-muted" style={{fontSize:12}}>🤖 识别中...</span>}
                  <button className="btn-ghost-sm" onClick={() => editingVolIdx === idx ? (setEditingVolIdx(null), setEditVolJson('')) : startEditVolInventory(idx)} title={editingVolIdx === idx ? '取消编辑' : '编辑此卷物资数据（JSON）'}>{editingVolIdx === idx ? '取消' : '✏️'}</button>
                  {(vol.items || []).length > 0 && (
                    <button className="btn-ghost-sm" onClick={() => deleteVolumeInventory(idx)} style={{color:'#e74c3c'}} title="删除此卷物资数据">🗑️</button>
                  )}
                </div>
              </div>
              {!collapsedVols.has(idx) && (
                <div className="plot-volume-body">
                  {editingVolIdx === idx ? (
                    <div style={{marginTop:8}}>
                      <p className="text-muted" style={{fontSize:12,marginBottom:6}}>编辑本卷物资数据（JSON 格式）：items（物资清单）、realms（境界变化）。</p>
                      <textarea className="input" value={editVolJson} onChange={e => setEditVolJson(e.target.value)} rows={16} style={{fontFamily:'monospace',fontSize:12}} />
                      <div style={{display:'flex',gap:6,marginTop:8}}>
                        <button className="btn-primary-sm" onClick={() => saveEditVolInventory(idx)}>💾 保存</button>
                        <button className="btn-ghost-sm" onClick={() => { setEditingVolIdx(null); setEditVolJson(''); }}>取消</button>
                      </div>
                    </div>
                  ) : (!vol.items || vol.items.length === 0) && (!vol.realms || vol.realms.length === 0) ? (
                    <p className="text-muted" style={{fontSize:13}}>暂无物资数据，点击「🔍 AI识别」选择此卷进行识别</p>
                  ) : (
                    <>
                      {vol.items && vol.items.length > 0 && (
                        <div className="plot-events">
                          <b>物资清单（{vol.items.length}项）：</b>
                          <ul>
                            {vol.items.map((item: any, i: number) => (
                              <li key={i}>
                                <span style={{color:'#5b8def',fontWeight:600}}>{CATEGORY_LABELS[item.category] || '🔹'} {safeText(item.name)}</span>
                                {item.owner && <span style={{color:'#888'}}> · 持有：{safeText(item.owner)}</span>}
                                {item.category && <span style={{color:'#27ae60'}}> · {safeText(item.category)}</span>}
                                {item.status && <span style={{color:'#e87d3e'}}> · {safeText(item.status)}</span>}
                                {item.description && <span style={{color:'#666'}}> — {safeText(item.description)}</span>}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {vol.realms && vol.realms.length > 0 && (
                        <div className="plot-events">
                          <b>境界变化（{vol.realms.length}项）：</b>
                          <ul>
                            {vol.realms.map((r: any, i: number) => (
                              <li key={i}>
                                <span style={{color:'#9b59b6',fontWeight:600}}>{safeText(r.character)}</span>
                                {r.realm && <span style={{color:'#27ae60'}}> · {safeText(r.realm)}</span>}
                                {r.progress && <span style={{color:'#666'}}> — {safeText(r.progress)}</span>}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
