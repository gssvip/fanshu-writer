/** CharacterPanel —— 自 WritePage.tsx 拆出（P2b 巨石拆分，逻辑零改动）。 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api';
import type { BookBible, Chapter, SkillPack } from '../../types';
import { SkillPackGroupedList, extractSkillPrompt } from './write-shared';

/* ===== 人物及关系面板 ===== */
export interface CharacterData {
  name: string;
  role?: string;
  identity?: string;
  personality?: string;
  motivation?: string;
  background?: string;
  relationships?: string;
  abilities?: string;
  items?: string;
}


export function CharacterPanel(props: {
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
  const [characters, setCharacters] = useState<CharacterData[]>([]);
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [addingNew, setAddingNew] = useState(false);
  const [editForm, setEditForm] = useState<CharacterData>({ name: '', role: '', identity: '', personality: '', motivation: '', background: '', relationships: '', abilities: '', items: '' });
  const [analyzingName, setAnalyzingName] = useState('');
  const [aiMode, setAiMode] = useState(false);
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiAssisting, setAiAssisting] = useState(false);
  const [aiError, setAiError] = useState('');
  const [skillExpanded, setSkillExpanded] = useState(false);
  const [collapsedChars, setCollapsedChars] = useState<Set<number>>(new Set());
  // 全局人物批量管理
  const [charBatchMode, setCharBatchMode] = useState(false);
  const [charCheckedIds, setCharCheckedIds] = useState<Set<number>>(new Set());
  const [globalCharCollapsed, setGlobalCharCollapsed] = useState(false);
  // 按卷人物识别
  const [charVolumes, setCharVolumes] = useState<any[]>([]);
  const [analyzingVol, setAnalyzingVol] = useState('');
  const [collapsedVolChars, setCollapsedVolChars] = useState<Set<number>>(new Set());
  // 每次进入维度默认折叠所有卷（tab 切换重新挂载，ref 重置）
  const charCollapseInitRef = useRef(false);
  // 卷选择器
  const [volSelectorOpen, setVolSelectorOpen] = useState(false);
  // 按卷编辑：editingVolIdx 为正在编辑的卷索引，editVolJson 为编辑中的 JSON 文本
  const [editingVolIdx, setEditingVolIdx] = useState<number | null>(null);
  const [editVolJson, setEditVolJson] = useState('');
  // 关系图谱编辑
  const [relGraphCollapsed, setRelGraphCollapsed] = useState(true);
  const [relGraphEditing, setRelGraphEditing] = useState(false);
  const [relGraphValue, setRelGraphValue] = useState('');

  function toggleChar(idx: number) {
    setCollapsedChars(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  // 解析角色数据
  useEffect(() => {
    if (!bible?.character_profiles) { setCharacters([]); return; }
    try {
      const parsed = JSON.parse(bible.character_profiles);
      if (Array.isArray(parsed)) {
        setCharacters(parsed);
        return;
      }
    } catch { /* not JSON */ }
    // 纯文本模式：尝试按行或【】解析
    const text = bible.character_profiles;
    const chars: CharacterData[] = [];
    const blocks = text.split(/\n\s*\n/).filter(b => b.trim());
    for (const block of blocks) {
      const nameMatch = block.match(/[【\[](.+?)[】\]]/);
      const name = nameMatch ? nameMatch[1] : block.split(/[：:\n]/)[0].trim();
      if (name) chars.push({ name, role: '', personality: block.trim() });
    }
    if (chars.length === 0 && text.trim()) {
      setCharacters([{ name: '角色信息', personality: text.trim() }]);
    } else {
      setCharacters(chars);
    }
  }, [bible?.character_profiles]);

  // 解析按卷人物数据（character_volumes）
  useEffect(() => {
    if (!bible?.character_volumes) { setCharVolumes([]); return; }
    try {
      const parsed = JSON.parse(bible.character_volumes);
      if (Array.isArray(parsed)) { setCharVolumes(parsed); return; }
    } catch { /* not JSON */ }
    setCharVolumes([]);
  }, [bible?.character_volumes]);

  // chapters 表的卷（可识别）
  const volumeChapters = chapters.filter(c => c.is_volume);

  // 合并卷列表：chapters.is_volume 卷 + charVolumes 已有卷
  const displayCharVolumes = useMemo(() => {
    const result: any[] = [];
    const usedIds = new Set<string>();
    for (const vc of volumeChapters) {
      const cvData = charVolumes.find(v => v.volume_id === vc.id) || charVolumes.find(v => v.volume === vc.title);
      result.push({
        volume_id: vc.id,
        volume: vc.title,
        characters: cvData?.characters || [],
        chapter_count: chapters.filter(c => c.parent_id === vc.id).length,
      });
      if (cvData) { usedIds.add(cvData.volume_id || ''); usedIds.add(cvData.volume || ''); }
    }
    for (const v of charVolumes) {
      const id = v.volume_id || '';
      const name = v.volume || '';
      if (!usedIds.has(id) && !usedIds.has(name)) {
        result.push({ ...v, chapter_count: 0 });
      }
    }
    return result;
  }, [volumeChapters, charVolumes, chapters]);

  // 首次有卷数据时默认折叠全部卷（每次切换到该维度 tab 重新挂载，ref 重置，实现每次进入默认折叠）
  useEffect(() => {
    if (charCollapseInitRef.current) return;
    if (displayCharVolumes.length > 0) {
      charCollapseInitRef.current = true;
      setCollapsedVolChars(new Set(displayCharVolumes.map((_, idx) => idx)));
    }
  }, [displayCharVolumes]);

  function toggleVolChar(idx: number) {
    setCollapsedVolChars(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  // AI识别指定卷人物
  async function handleAnalyzeCharVolume(volId: string, volTitle: string) {
    showConfirm(`将用 AI 分析「${volTitle}」的章节内容，识别本卷出现的角色。是否继续？`, async () => {
      setAnalyzingVol(volId || volTitle);
      try {
        const result = await api.analyzeCharacterVolume(bookId, volId, volTitle, selectedSkillPackIds);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已为「${volTitle}」识别 ${result.volume_data?.characters?.length || 0} 个角色`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingVol('');
    });
  }

  // 删除某卷的人物数据
  async function deleteVolumeCharacters(idx: number) {
    const vol = displayCharVolumes[idx];
    if (!vol) return;
    showConfirm(`确定删除「${vol.volume || '该卷'}」的人物识别数据？`, async () => {
      const newList = charVolumes.filter((v: any) => {
        const vId = v.volume_id || '';
        const vName = v.volume || '';
        if (vol.volume_id && vId === vol.volume_id) return false;
        if (vol.volume && vName === vol.volume) return false;
        return true;
      });
      try {
        const updated = await api.updateBible(bookId, { character_volumes: JSON.stringify(newList, null, 2) } as any);
        onBibleUpdate(updated);
      } catch (e: any) {
        alert('删除失败: ' + e.message);
      }
    });
  }

  // 开始按卷编辑：将该卷的完整数据序列化为 JSON 供编辑
  function startEditVolCharacters(idx: number) {
    const vol = displayCharVolumes[idx];
    if (!vol) return;
    const editTarget = {
      volume_id: vol.volume_id || '',
      volume: vol.volume || '',
      characters: vol.characters || [],
    };
    setEditingVolIdx(idx);
    setEditVolJson(JSON.stringify(editTarget, null, 2));
    setCollapsedVolChars(prev => { const n = new Set(prev); n.delete(idx); return n; });
  }

  // 保存按卷编辑：解析编辑后的 JSON，写回 character_volumes
  async function saveEditVolCharacters(idx: number) {
    try {
      const parsed = JSON.parse(editVolJson);
      const vol = displayCharVolumes[idx];
      const matchKey = vol.volume_id || vol.volume;
      const newList = charVolumes.map((v: any) => {
        const vKey = v.volume_id || v.volume;
        if (vKey === matchKey) {
          return {
            volume_id: parsed.volume_id || v.volume_id || '',
            volume: parsed.volume || v.volume || '',
            characters: Array.isArray(parsed.characters) ? parsed.characters : (v.characters || []),
          };
        }
        return v;
      });
      // 若该卷尚未在 charVolumes 中（纯展示卷），则追加
      const exists = newList.some((v: any) => (v.volume_id || v.volume) === matchKey);
      if (!exists) {
        newList.push({
          volume_id: parsed.volume_id || vol.volume_id || '',
          volume: parsed.volume || vol.volume || '',
          characters: Array.isArray(parsed.characters) ? parsed.characters : [],
        });
      }
      const updated = await api.updateBible(bookId, { character_volumes: JSON.stringify(newList, null, 2) } as any);
      onBibleUpdate(updated);
      setEditingVolIdx(null);
      setEditVolJson('');
    } catch (e: any) {
      alert('保存失败：JSON 格式错误 - ' + e.message);
    }
  }

  // 将某卷识别的角色智能合并到全局人物档案（同名更新，新角色追加）
  async function mergeVolumeToGlobal(idx: number) {
    const vol = displayCharVolumes[idx];
    if (!vol || !vol.characters || vol.characters.length === 0) return;
    showConfirm(`将「${vol.volume}」识别的 ${vol.characters.length} 个角色与全局人物档案相互验证更新？\n• 同名角色：用本卷新信息补充更新（不覆盖已有非空字段）\n• 新角色：自动追加到全局档案`, async () => {
      const globalMap = new Map<string, { char: CharacterData; idx: number }>();
      characters.forEach((c, i) => { if (c.name) globalMap.set(c.name, { char: c, idx: i }); });
      let updatedCount = 0;
      let addedCount = 0;
      const newChars = [...characters];
      for (const c of vol.characters) {
        if (!c.name) continue;
        const existing = globalMap.get(c.name);
        if (existing) {
          // 同名角色：补充更新（仅填充全局档案中为空的字段）
          const merged = { ...existing.char };
          let changed = false;
          const fields: (keyof CharacterData)[] = ['role', 'identity', 'personality', 'motivation', 'background', 'relationships', 'abilities', 'items'];
          for (const f of fields) {
            const newVal = (c as any)[f];
            if (newVal && newVal.trim() && !(merged[f] && (merged[f] as string).trim())) {
              (merged as any)[f] = newVal;
              changed = true;
            }
          }
          if (changed) {
            newChars[existing.idx] = merged;
            updatedCount++;
          }
        } else {
          // 新角色：追加
          newChars.push({
            name: c.name,
            role: c.role || '',
            identity: c.identity || '',
            personality: c.personality || '',
            motivation: c.motivation || '',
            relationships: c.relationships || '',
            abilities: c.abilities || '',
            items: c.items || '',
          });
          globalMap.set(c.name, { char: newChars[newChars.length - 1], idx: newChars.length - 1 });
          addedCount++;
        }
      }
      if (updatedCount === 0 && addedCount === 0) {
        alert('该卷角色已全部在全局档案中且无新信息可更新');
        return;
      }
      await saveCharacters(newChars);
      const parts: string[] = [];
      if (addedCount > 0) parts.push(`新增 ${addedCount} 个角色`);
      if (updatedCount > 0) parts.push(`更新 ${updatedCount} 个角色信息`);
      alert(`相互验证完成：${parts.join('，')}`);
    });
  }

  // 将全局人物档案同步回写到所有分卷（同名更新，新角色追加到对应卷）
  async function syncGlobalToVolumes() {
    if (characters.length === 0) { alert('全局人物档案为空，无法同步'); return; }
    if (charVolumes.length === 0) { alert('暂无分卷人物数据，无法同步'); return; }
    showConfirm(`将全局人物档案（${characters.length}人）同步到所有分卷？\n• 同名角色：用全局信息补充更新分卷（不覆盖分卷已有非空字段）\n• 全局有但分卷没有的角色：追加到对应卷`, async () => {
      let totalUpdated = 0;
      let totalAdded = 0;
      const newList = charVolumes.map((vol: any) => {
        const volChars: any[] = vol.characters ? [...vol.characters] : [];
        const volMap = new Map<string, number>();
        volChars.forEach((c: any, i: number) => { if (c.name) volMap.set(c.name, i); });
        let updated = 0;
        let added = 0;
        for (const gc of characters) {
          if (!gc.name) continue;
          const existIdx = volMap.get(gc.name);
          if (existIdx !== undefined) {
            // 同名：补充更新
            const merged = { ...volChars[existIdx] };
            let changed = false;
            const fields = ['role', 'identity', 'personality', 'motivation', 'background', 'relationships', 'abilities', 'items'];
            for (const f of fields) {
              const gVal = (gc as any)[f];
              if (gVal && gVal.trim() && !(merged[f] && merged[f].trim())) {
                merged[f] = gVal;
                changed = true;
              }
            }
            if (changed) { volChars[existIdx] = merged; updated++; }
          } else {
            // 新角色：追加到该卷
            volChars.push({
              name: gc.name,
              role: gc.role || '',
              identity: gc.identity || '',
              personality: gc.personality || '',
              motivation: gc.motivation || '',
              relationships: gc.relationships || '',
              abilities: gc.abilities || '',
              items: gc.items || '',
              arc: '',
            });
            volMap.set(gc.name, volChars.length - 1);
            added++;
          }
        }
        totalUpdated += updated;
        totalAdded += added;
        return { ...vol, characters: volChars };
      });
      if (totalUpdated === 0 && totalAdded === 0) {
        alert('所有分卷人物已与全局档案一致，无需更新');
        return;
      }
      try {
        const updated = await api.updateBible(bookId, { character_volumes: JSON.stringify(newList, null, 2) } as any);
        onBibleUpdate(updated);
        const parts: string[] = [];
        if (totalUpdated > 0) parts.push(`更新 ${totalUpdated} 个角色`);
        if (totalAdded > 0) parts.push(`追加 ${totalAdded} 个角色到分卷`);
        alert(`同步完成：${parts.join('，')}`);
      } catch (e: any) {
        alert('同步失败: ' + e.message);
      }
    });
  }

  async function saveCharacters(newChars: CharacterData[]) {
    setCharacters(newChars);
    try {
      const updated = await api.updateBible(bookId, { character_profiles: JSON.stringify(newChars, null, 2) } as any);
      onBibleUpdate(updated);
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
  }

  function startAddNew() {
    setEditForm({ name: '', role: '配角', identity: '', personality: '', motivation: '', background: '', relationships: '', abilities: '', items: '' });
    setAddingNew(true);
    setEditingIdx(null);
  }

  function startEdit(idx: number) {
    setEditForm({ ...characters[idx] });
    setEditingIdx(idx);
    setAddingNew(false);
  }

  async function saveEdit() {
    if (!editForm.name.trim()) { alert('请输入角色名称'); return; }
    const newChars = [...characters];
    if (editingIdx !== null) {
      newChars[editingIdx] = { ...editForm };
    } else {
      newChars.push({ ...editForm });
    }
    await saveCharacters(newChars);
    setEditingIdx(null);
    setAddingNew(false);
  }

  async function deleteChar(idx: number) {
    const char = characters[idx];
    showConfirm(`确定删除角色「${char.name}」？`, async () => {
      const newChars = characters.filter((_, i) => i !== idx);
      await saveCharacters(newChars);
    });
  }

  // 批量删除选中角色
  async function deleteCheckedChars() {
    if (charCheckedIds.size === 0) return;
    showConfirm(`确定删除选中的 ${charCheckedIds.size} 个角色？`, async () => {
      const newChars = characters.filter((_, i) => !charCheckedIds.has(i));
      await saveCharacters(newChars);
      setCharCheckedIds(new Set());
      setCharBatchMode(false);
    });
  }

  function toggleCharCheck(idx: number) {
    setCharCheckedIds(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  // AI识别单个角色
  async function handleAnalyzeOne(charName: string) {
    showConfirm(`将用 AI 分析已有章节内容，自动识别并填充「${charName}」的详细信息。是否继续？`, async () => {
      setAnalyzingName(charName);
      try {
        const result = await api.analyzeCharacter(bookId, charName);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已填充「${charName}」的信息`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingName('');
    });
  }

  // AI协同创作
  async function executeAi() {
    if (!aiPrompt.trim()) { alert('请输入创作要求'); return; }
    setAiAssisting(true);
    setAiError('');
    try {
      const skillKeys = ['character_cognition', 'tomato_character'];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const contextConcept = bible?.concept || '暂无构思';
      const existingNames = characters.map(c => c.name).join('、');
      const messages = [
        { role: 'system', content: `你是专业网文创作助手。请根据用户要求生成角色档案。${skillNote}` },
        { role: 'user', content: `构思：${contextConcept}\n已有角色：${existingNames || '无'}\n\n用户要求：${aiPrompt}\n\n请生成角色档案，包括姓名、身份、性格、动机、背景、关系、能力、物品。用JSON数组格式输出。` },
      ];
      const result = await api.aiChat(messages);
      // 尝试解析AI返回的JSON
      let newChars: CharacterData[] = [];
      try {
        const match = result.content.match(/\[[\s\S]*\]/);
        if (match) newChars = JSON.parse(match[0]);
        else {
          const objMatch = result.content.match(/\{[\s\S]*\}/);
          if (objMatch) newChars = [JSON.parse(objMatch[0])];
        }
      } catch { /* parse fail */ }
      if (newChars.length > 0) {
        await saveCharacters([...characters, ...newChars]);
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

  // AI协同创作模式
  if (aiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <h3>👤 AI协同创作 · 人物</h3>
          <button className="btn-ghost-sm" onClick={() => { setAiMode(false); setAiError(''); }} disabled={aiAssisting}>取消</button>
        </div>
        {skillPacks.length > 0 && (
          <div className="skill-pack-collapsible">
            <button className="skill-pack-toggle" onClick={() => setSkillExpanded(v => !v)} disabled={aiAssisting}>
              <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
              <span>📦 协同技能包</span>
              {selectedSkillPackIds.length > 0 && <span className="skill-pack-toggle-badge">{selectedSkillPackIds.length}</span>}
              <span className="skill-pack-toggle-hint">{skillExpanded ? '收起' : '展开'}</span>
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
          <textarea
            className="input bible-ai-prompt-input"
            rows={6}
            value={aiPrompt}
            onChange={e => setAiPrompt(e.target.value)}
            onKeyDown={handlePromptKeyDown}
            placeholder="例如：为主角设计3个重要配角，包括反派、导师、恋人..."
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
            <p>AI正在生成人物档案...</p>
          </div>
        )}
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
                  <button className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeCharVolume('', '全部章节'); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📚 全部章节</button>
                  {displayCharVolumes.map((vol, idx) => (
                    <button key={idx} className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeCharVolume(vol.volume_id || '', vol.volume || `第${idx + 1}卷`); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📖 {vol.volume || `第${idx + 1}卷`}{vol.chapter_count ? ` (${vol.chapter_count}章)` : ''}</button>
                  ))}
                  <button onClick={() => setVolSelectorOpen(false)} style={{display:'block',width:'100%',textAlign:'center',padding:'4px',background:'transparent',border:'none',cursor:'pointer',color:'var(--text-muted)',fontSize:12,marginTop:2}}>取消</button>
                </div>
              )}
            </>
          )}
          <button className="btn-primary-sm" onClick={startAddNew}>＋ 添加角色</button>
        </div>
      </div>

      {/* 按卷人物识别 */}
      {displayCharVolumes.length > 0 && (
        <div className="plot-volume-list" style={{marginBottom:16}}>
          <p className="text-muted" style={{fontSize:12, marginBottom:8}}>
            📚 按卷识别人物：点击「🔍 AI识别」选择卷，识别结果自动归类到对应卷下，可合并到下方全局人物档案。
          </p>
          {displayCharVolumes.map((vol, idx) => (
            <div key={idx} className="plot-volume-card">
              <div className="plot-volume-header" onClick={() => toggleVolChar(idx)} style={{cursor:'pointer'}}>
                <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{collapsedVolChars.has(idx) ? '▶' : '▼'}</span>
                <h4>{vol.volume || `第${idx + 1}卷`}</h4>
                {vol.chapter_count !== undefined && <span className="text-muted" style={{fontSize:12}}>{vol.chapter_count}章</span>}
                <span className="text-muted" style={{fontSize:12}}>{(vol.characters || []).length}人</span>
                <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                  {analyzingVol === (vol.volume_id || vol.volume) && <span className="text-muted" style={{fontSize:12}}>🤖 识别中...</span>}
                  <button className="btn-ghost-sm" onClick={() => editingVolIdx === idx ? (setEditingVolIdx(null), setEditVolJson('')) : startEditVolCharacters(idx)} title={editingVolIdx === idx ? '取消编辑' : '编辑此卷人物数据（JSON）'}>{editingVolIdx === idx ? '取消' : '✏️'}</button>
                  {(vol.characters || []).length > 0 && (
                    <>
                      <button className="btn-ghost-sm" onClick={() => mergeVolumeToGlobal(idx)} title="与全局人物档案相互验证更新（同名补充，新角色追加）" style={{color:'#27ae60'}}>⇅ 验证更新</button>
                      <button className="btn-ghost-sm" onClick={() => deleteVolumeCharacters(idx)} style={{color:'#e74c3c'}} title="删除此卷人物数据">🗑️</button>
                    </>
                  )}
                </div>
              </div>
              {!collapsedVolChars.has(idx) && (
                <div className="plot-volume-body">
                  {editingVolIdx === idx ? (
                    <div style={{marginTop:8}}>
                      <p className="text-muted" style={{fontSize:12,marginBottom:6}}>编辑本卷人物数据（JSON 格式），可直接修改 characters 数组中各角色的字段。</p>
                      <textarea className="input" value={editVolJson} onChange={e => setEditVolJson(e.target.value)} rows={16} style={{fontFamily:'monospace',fontSize:12}} />
                      <div style={{display:'flex',gap:6,marginTop:8}}>
                        <button className="btn-primary-sm" onClick={() => saveEditVolCharacters(idx)}>💾 保存</button>
                        <button className="btn-ghost-sm" onClick={() => { setEditingVolIdx(null); setEditVolJson(''); }}>取消</button>
                      </div>
                    </div>
                  ) : (!vol.characters || vol.characters.length === 0) ? (
                    <p className="text-muted" style={{fontSize:13}}>暂无人物识别数据，点击「🔍 AI识别」选择此卷进行识别</p>
                  ) : (
                    <div className="character-cards-grid">
                      {vol.characters.map((char: any, ci: number) => (
                        <div key={ci} className="character-card">
                          <div className="character-card-header">
                            <span className="character-card-name">{char.name}</span>
                            {char.role && <span className="character-card-role">{char.role}</span>}
                          </div>
                          <div className="character-card-body">
                            {char.identity && <p><b>身份：</b>{char.identity}</p>}
                            {char.personality && <p><b>性格：</b>{char.personality}</p>}
                            {char.motivation && <p><b>动机：</b>{char.motivation}</p>}
                            {char.relationships && <p><b>关系：</b>{char.relationships}</p>}
                            {char.abilities && <p><b>能力：</b>{char.abilities}</p>}
                            {char.items && <p><b>物品：</b>{char.items}</p>}
                            {char.arc && <p><b>本卷弧线：</b>{char.arc}</p>}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 添加/编辑表单 */}
      {(addingNew || editingIdx !== null) && (
        <div className="character-edit-form" style={{background:'var(--bg-tertiary)',borderRadius:'var(--radius-sm)',padding:16,marginBottom:16}}>
          <h4 style={{marginBottom:10}}>{editingIdx !== null ? '编辑角色' : '添加角色'}</h4>
          <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:10}}>
            <div>
              <label className="input-label">角色名称 *</label>
              <input className="input" value={editForm.name} onChange={e => setEditForm({...editForm, name: e.target.value})} placeholder="如：林逸" autoFocus />
            </div>
            <div>
              <label className="input-label">角色定位</label>
              <select className="input" value={editForm.role || ''} onChange={e => setEditForm({...editForm, role: e.target.value})}>
                <option value="">— 选择 —</option>
                <option value="主角">主角</option>
                <option value="配角">配角</option>
                <option value="反派">反派</option>
                <option value="路人">路人</option>
              </select>
            </div>
            <div>
              <label className="input-label">身份职业</label>
              <input className="input" value={editForm.identity || ''} onChange={e => setEditForm({...editForm, identity: e.target.value})} placeholder="如：散修/皇子/商人" />
            </div>
            <div>
              <label className="input-label">性格特征</label>
              <input className="input" value={editForm.personality || ''} onChange={e => setEditForm({...editForm, personality: e.target.value})} placeholder="如：沉稳内敛，心思缜密" />
            </div>
            <div>
              <label className="input-label">核心动机</label>
              <input className="input" value={editForm.motivation || ''} onChange={e => setEditForm({...editForm, motivation: e.target.value})} placeholder="如：复仇/求道/守护家族" />
            </div>
            <div>
              <label className="input-label">人物关系</label>
              <input className="input" value={editForm.relationships || ''} onChange={e => setEditForm({...editForm, relationships: e.target.value})} placeholder="如：与XX是师徒" />
            </div>
            <div>
              <label className="input-label">能力/功法</label>
              <input className="input" value={editForm.abilities || ''} onChange={e => setEditForm({...editForm, abilities: e.target.value})} placeholder="如：剑道天赋/火焰术" />
            </div>
            <div>
              <label className="input-label">持有物品</label>
              <input className="input" value={editForm.items || ''} onChange={e => setEditForm({...editForm, items: e.target.value})} placeholder="如：寒霜剑/破界符" />
            </div>
          </div>
          <div style={{marginTop:10}}>
            <label className="input-label">背景故事</label>
            <textarea className="input" rows={3} value={editForm.background || ''} onChange={e => setEditForm({...editForm, background: e.target.value})} placeholder="角色的过往经历..." />
          </div>
          <div style={{display:'flex',gap:8,marginTop:12}}>
            <button className="btn-primary-sm" onClick={saveEdit}>💾 保存</button>
            <button className="btn-ghost-sm" onClick={() => { setEditingIdx(null); setAddingNew(false); }}>取消</button>
          </div>
        </div>
      )}

      {/* 全局人物档案 */}
      {characters.length === 0 ? (
        <div className="bible-empty">
          <span className="bible-empty-icon">👤</span>
          <p>暂无角色信息</p>
          <p className="text-muted">点击顶部「＋ 添加角色」或「✨ AI创作」生成人物档案</p>
        </div>
      ) : (
        <div className="plot-volume-list">
          <div className="plot-volume-card">
            <div className="plot-volume-header" onClick={() => setGlobalCharCollapsed(v => !v)} style={{cursor:'pointer'}}>
              <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{globalCharCollapsed ? '▶' : '▼'}</span>
              <h4>🌍 全局人物档案</h4>
              <span className="text-muted" style={{fontSize:12}}>{characters.length}人</span>
              <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                {charBatchMode ? (
                  <>
                    <button className="btn-ghost-sm" onClick={() => { setCharCheckedIds(new Set()); setCharBatchMode(false); }}>✕ 取消</button>
                    <button className="btn-ghost-sm" onClick={deleteCheckedChars} disabled={charCheckedIds.size === 0} style={{color:'#e74c3c'}} title="删除选中角色">🗑️ 删除选中({charCheckedIds.size})</button>
                  </>
                ) : (
                  <>
                    {charVolumes.length > 0 && (
                      <button className="btn-ghost-sm" onClick={syncGlobalToVolumes} title="将全局人物同步到所有分卷（同名补充，新角色追加）" style={{color:'#27ae60'}}>⇅ 同步到分卷</button>
                    )}
                    <button className="btn-ghost-sm" onClick={() => setCharBatchMode(true)} title="批量选择并删除角色">☑ 批量管理</button>
                  </>
                )}
              </div>
            </div>
            {!globalCharCollapsed && (
              <div className="plot-volume-body">
                <div className="character-cards-grid">
                  {characters.map((char, idx) => (
                    <div key={idx} className="character-card" style={charBatchMode && charCheckedIds.has(idx) ? {border:'2px solid var(--accent)'} : {}}>
                      <div className="character-card-header" onClick={() => charBatchMode ? toggleCharCheck(idx) : toggleChar(idx)} style={{cursor:'pointer'}}>
                        {charBatchMode && (
                          <input type="checkbox" checked={charCheckedIds.has(idx)} onChange={() => toggleCharCheck(idx)} style={{marginRight:6}} onClick={e => e.stopPropagation()} />
                        )}
                        {!charBatchMode && <span className="map-toggle" style={{fontSize:10,marginRight:4}}>{collapsedChars.has(idx) ? '▶' : '▼'}</span>}
                        <span className="character-card-name">{char.name}</span>
                        {char.role && <span className="character-card-role">{char.role}</span>}
                        {char.abilities && <span className="text-muted" style={{fontSize:10,marginLeft:4}}>{(char.abilities || '').slice(0, 12)}</span>}
                      </div>
                      {!charBatchMode && !collapsedChars.has(idx) && (
                        <div className="character-card-body">
                          {char.identity && <p><b>身份：</b>{char.identity}</p>}
                          {char.personality && <p><b>性格：</b>{char.personality}</p>}
                          {char.motivation && <p><b>动机：</b>{char.motivation}</p>}
                          {char.background && <p><b>背景：</b>{char.background}</p>}
                          {char.relationships && <p><b>关系：</b>{char.relationships}</p>}
                          {char.abilities && <p><b>能力：</b>{char.abilities}</p>}
                          {char.items && <p><b>物品：</b>{char.items}</p>}
                        </div>
                      )}
                      {!charBatchMode && !collapsedChars.has(idx) && (
                      <div className="character-card-actions">
                        <button className="btn-ghost-sm" onClick={() => handleAnalyzeOne(char.name)} disabled={analyzingName === char.name || !hasChapters} title={hasChapters ? 'AI识别此角色信息' : '需要先创建章节才能AI识别'}>
                          {analyzingName === char.name ? '🤖 识别中...' : '🔍 识别'}
                        </button>
                        <button className="btn-ghost-sm" onClick={() => startEdit(idx)}>✏️ 编辑</button>
                        <button className="btn-ghost-sm" onClick={() => deleteChar(idx)} style={{color:'#e74c3c'}}>🗑️</button>
                      </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* 关系图谱编辑区 */}
      <div className="plot-volume-list" style={{marginTop:16}}>
        <div className="plot-volume-card">
          <div className="plot-volume-header" onClick={() => setRelGraphCollapsed(v => !v)} style={{cursor:'pointer'}}>
            <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{relGraphCollapsed ? '▶' : '▼'}</span>
            <h4>🔗 人物关系图谱</h4>
            <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
              {relGraphEditing ? (
                <>
                  <button className="btn-primary-sm" onClick={async () => {
                    const updated = await api.updateBible(bookId, { relation_graph: relGraphValue } as any);
                    onBibleUpdate(updated);
                    setRelGraphEditing(false);
                  }}>💾 保存</button>
                  <button className="btn-ghost-sm" onClick={() => { setRelGraphEditing(false); setRelGraphValue(bible?.relation_graph || ''); }}>取消</button>
                </>
              ) : (
                <button className="btn-ghost-sm" onClick={() => { setRelGraphEditing(true); setRelGraphValue(bible?.relation_graph || ''); }} title="编辑人物关系图谱">✏️ 编辑</button>
              )}
            </div>
          </div>
          {!relGraphCollapsed && (
            <div className="plot-volume-body">
              {relGraphEditing ? (
                <textarea className="input" value={relGraphValue} onChange={e => setRelGraphValue(e.target.value)} rows={10} style={{fontFamily:'monospace',fontSize:12}} placeholder="用文本描述人物关系图谱，例如：\n张三 → 李四：师徒\n张三 → 王五：宿敌\n李四 → 赵六：恋人" />
              ) : (
                <div style={{fontSize:13,lineHeight:1.7,whiteSpace:'pre-wrap',color: bible?.relation_graph ? 'var(--text-primary)' : 'var(--text-muted)'}}>
                  {bible?.relation_graph || '暂无关系图谱，点击「编辑」手动输入或通过 AI 识别生成'}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
