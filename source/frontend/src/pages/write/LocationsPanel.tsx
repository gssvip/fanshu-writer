/** LocationsPanel —— 自 WritePage.tsx 拆出（P2b 巨石拆分，逻辑零改动）。 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api';
import type { BookBible, Chapter } from '../../types';
import { collapseNewlines } from './write-shared';

/* ===== 地图/地点面板（按卷） ===== */
export function LocationsPanel(props: {
  bookId: string;
  bible: BookBible | null;
  onBibleUpdate: (b: BookBible) => void;
  bookTitle: string;
  chapters: Chapter[];
  hasChapters: boolean;
  showConfirm: (message: string, onConfirm: () => void) => void;
  selectedSkillPackIds: string[];
  onMapUpdate: (val: string) => Promise<void>;
  onOpenAiCreate: () => void;
}) {
  const { bookId, bible, onBibleUpdate, chapters, hasChapters, showConfirm, selectedSkillPackIds, onMapUpdate } = props;
  const [locations, setLocations] = useState('');
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState('');
  const [saving, setSaving] = useState(false);
  const [locVolumes, setLocVolumes] = useState<any[]>([]);
  const [analyzingVol, setAnalyzingVol] = useState('');
  const [collapsedVols, setCollapsedVols] = useState<Set<number>>(new Set());
  // 每次进入维度默认折叠所有卷（tab 切换重新挂载，ref 重置）
  const locCollapseInitRef = useRef(false);
  const [volSelectorOpen, setVolSelectorOpen] = useState(false);
  const [editingVolIdx, setEditingVolIdx] = useState<number | null>(null);
  const [editVolJson, setEditVolJson] = useState('');
  const [globalLocCollapsed, setGlobalLocCollapsed] = useState(true); // 全局地点档案默认折叠

  useEffect(() => {
    setLocations(bible?.locations || '');
  }, [bible?.locations]);

  useEffect(() => {
    if (!bible?.locations_volumes) { setLocVolumes([]); return; }
    try {
      const parsed = JSON.parse(bible.locations_volumes);
      if (Array.isArray(parsed)) { setLocVolumes(parsed); return; }
    } catch { /* not JSON */ }
    setLocVolumes([]);
  }, [bible?.locations_volumes]);

  const volumeChapters = chapters.filter(c => c.is_volume);

  const displayVolumes = useMemo(() => {
    const result: any[] = [];
    const usedIds = new Set<string>();
    for (const vc of volumeChapters) {
      const lvData = locVolumes.find(v => v.volume_id === vc.id) || locVolumes.find(v => v.volume === vc.title);
      result.push({
        volume_id: vc.id,
        volume: vc.title,
        data: lvData?.data || null,
        chapter_count: chapters.filter(c => c.parent_id === vc.id).length,
      });
      if (lvData) { usedIds.add(lvData.volume_id || ''); usedIds.add(lvData.volume || ''); }
    }
    for (const v of locVolumes) {
      const id = v.volume_id || '';
      const name = v.volume || '';
      if (!usedIds.has(id) && !usedIds.has(name)) {
        result.push({ ...v, chapter_count: 0 });
      }
    }
    return result;
  }, [volumeChapters, locVolumes, chapters]);

  // 首次有卷数据时默认折叠全部卷（每次切换到该维度 tab 重新挂载，ref 重置，实现每次进入默认折叠）
  useEffect(() => {
    if (locCollapseInitRef.current) return;
    if (displayVolumes.length > 0) {
      locCollapseInitRef.current = true;
      setCollapsedVols(new Set(displayVolumes.map((_, idx) => idx)));
    }
  }, [displayVolumes]);

  function toggleVol(idx: number) {
    setCollapsedVols(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  async function saveLocations(val: string) {
    setSaving(true);
    try {
      await onMapUpdate(val);
      setLocations(val);
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setSaving(false);
  }

  async function handleAnalyzeVolume(volId: string, volTitle: string) {
    showConfirm(`将用 AI 分析「${volTitle}」的章节内容，识别本卷涉及的地点、场景、地理信息。是否继续？`, async () => {
      setAnalyzingVol(volId || volTitle);
      try {
        const result = await api.analyzeLocationsVolume(bookId, volId, volTitle, selectedSkillPackIds);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已为「${volTitle}」生成地点分析`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingVol('');
    });
  }

  async function deleteVolumeLoc(idx: number) {
    const vol = displayVolumes[idx];
    if (!vol) return;
    showConfirm(`确定删除「${vol.volume || '该卷'}」的地点识别数据？`, async () => {
      const newList = locVolumes.filter((v: any) => {
        const vId = v.volume_id || '';
        const vName = v.volume || '';
        if (vol.volume_id && vId === vol.volume_id) return false;
        if (vol.volume && vName === vol.volume) return false;
        return true;
      });
      try {
        const updated = await api.updateBible(bookId, { locations_volumes: JSON.stringify(newList, null, 2) } as any);
        onBibleUpdate(updated);
      } catch (e: any) {
        alert('删除失败: ' + e.message);
      }
    });
  }

  // 开始按卷编辑：将该卷的 data 序列化为 JSON 供编辑
  function startEditVolLoc(idx: number) {
    const vol = displayVolumes[idx];
    if (!vol) return;
    const editTarget = vol.data || { summary: '', locations: [], regions: [] };
    setEditingVolIdx(idx);
    setEditVolJson(JSON.stringify(editTarget, null, 2));
    setCollapsedVols(prev => { const n = new Set(prev); n.delete(idx); return n; });
  }

  // 保存按卷编辑：解析编辑后的 JSON，写回 locations_volumes
  async function saveEditVolLoc(idx: number) {
    try {
      const parsed = JSON.parse(editVolJson);
      const vol = displayVolumes[idx];
      const matchKey = vol.volume_id || vol.volume;
      const newList = [...locVolumes];
      const existIdx = newList.findIndex((v: any) => (v.volume_id || v.volume) === matchKey);
      const entry = {
        volume_id: vol.volume_id || '',
        volume: vol.volume || '',
        data: parsed,
      };
      if (existIdx >= 0) {
        newList[existIdx] = { ...newList[existIdx], ...entry };
      } else {
        newList.push(entry);
      }
      const updated = await api.updateBible(bookId, { locations_volumes: JSON.stringify(newList, null, 2) } as any);
      onBibleUpdate(updated);
      setEditingVolIdx(null);
      setEditVolJson('');
    } catch (e: any) {
      alert('保存失败：JSON 格式错误 - ' + e.message);
    }
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
        记录地点、场景、地理信息。点击「🔍 AI识别」选择卷，识别结果自动归类到对应卷下。
      </p>

      {/* 按卷地点识别 */}
      {displayVolumes.length > 0 && (
        <div className="plot-volume-list" style={{marginBottom:16}}>
          {displayVolumes.map((vol, idx) => {
            const d = vol.data || {};
            const hasData = vol.data && (d.summary || (d.locations && d.locations.length) || (d.regions && d.regions.length));
            return (
              <div key={idx} className="plot-volume-card">
                <div className="plot-volume-header" onClick={() => toggleVol(idx)} style={{cursor:'pointer'}}>
                  <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{collapsedVols.has(idx) ? '▶' : '▼'}</span>
                  <h4>{vol.volume || `第${idx + 1}卷`}</h4>
                  {vol.chapter_count !== undefined && <span className="text-muted" style={{fontSize:12}}>{vol.chapter_count}章</span>}
                  {hasData && <span className="text-muted" style={{fontSize:12}}>已识别</span>}
                  <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                    {analyzingVol === (vol.volume_id || vol.volume) && <span className="text-muted" style={{fontSize:12}}>🤖 识别中...</span>}
                    <button className="btn-ghost-sm" onClick={() => editingVolIdx === idx ? (setEditingVolIdx(null), setEditVolJson('')) : startEditVolLoc(idx)} title={editingVolIdx === idx ? '取消编辑' : '编辑此卷地点数据（JSON）'}>{editingVolIdx === idx ? '取消' : '✏️'}</button>
                    {hasData && (
                      <button className="btn-ghost-sm" onClick={() => deleteVolumeLoc(idx)} style={{color:'#e74c3c'}} title="删除此卷地点数据">🗑️</button>
                    )}
                  </div>
                </div>
                {!collapsedVols.has(idx) && (
                  <div className="plot-volume-body">
                    {editingVolIdx === idx ? (
                      <div style={{marginTop:8}}>
                        <p className="text-muted" style={{fontSize:12,marginBottom:6}}>编辑本卷地点数据（JSON 格式）：summary（地理概况）、locations（地点）、regions（区域）。</p>
                        <textarea className="input" value={editVolJson} onChange={e => setEditVolJson(e.target.value)} rows={16} style={{fontFamily:'monospace',fontSize:12}} />
                        <div style={{display:'flex',gap:6,marginTop:8}}>
                          <button className="btn-primary-sm" onClick={() => saveEditVolLoc(idx)}>💾 保存</button>
                          <button className="btn-ghost-sm" onClick={() => { setEditingVolIdx(null); setEditVolJson(''); }}>取消</button>
                        </div>
                      </div>
                    ) : !hasData ? (
                      <p className="text-muted" style={{fontSize:13}}>暂无地点识别数据，点击「🔍 AI识别」选择此卷进行识别</p>
                    ) : (
                      <div className="plot-events">
                        {d.summary && <p><b>地理概况：</b>{d.summary}</p>}
                        {d.locations && d.locations.length > 0 && (
                          <div><b>地点（{d.locations.length}）：</b><ul>
                            {d.locations.map((l: any, i: number) => (
                              <li key={i}><span style={{color:'#5b8def',fontWeight:600}}>{l.name}</span>{l.type && <span style={{color:'#27ae60'}}> · {l.type}</span>}{l.importance && <span style={{color:'#e74c3c'}}> · {l.importance}</span>}{l.description && <span style={{color:'#666'}}> — {l.description}</span>}{l.events && <span style={{color:'#888'}}> · 事件：{l.events}</span>}</li>
                            ))}
                          </ul></div>
                        )}
                        {d.regions && d.regions.length > 0 && (
                          <div><b>区域（{d.regions.length}）：</b><ul>
                            {d.regions.map((r: any, i: number) => (
                              <li key={i}><span style={{color:'#9b59b6',fontWeight:600}}>{r.name}</span>{r.scope && <span style={{color:'#888'}}> · {r.scope}</span>}{r.feature && <span style={{color:'#666'}}> — {r.feature}</span>}</li>
                            ))}
                          </ul></div>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* 全局地点编辑 */}
      <div className="bible-edit-section">
        <div
          style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:8,cursor:'pointer',userSelect:'none'}}
          onClick={() => setGlobalLocCollapsed(c => !c)}
        >
          <b>📝 全局地点档案 <span style={{fontSize:10,color:'var(--text-muted)'}}>{globalLocCollapsed ? '▶ 点击展开' : '▼ 点击折叠'}</span></b>
          {!editing ? (
            <button className="btn-ghost-sm" onClick={e => { e.stopPropagation(); setEditing(true); setEditValue(collapseNewlines(locations)); setGlobalLocCollapsed(false); }}>✏️ 编辑</button>
          ) : (
            <div style={{display:'flex',gap:6}} onClick={e => e.stopPropagation()}>
              <button className="btn-primary-sm" onClick={() => { saveLocations(collapseNewlines(editValue)); setEditing(false); }} disabled={saving}>{saving ? '保存中...' : '💾 保存'}</button>
              <button className="btn-ghost-sm" onClick={() => setEditing(false)}>取消</button>
            </div>
          )}
        </div>
        {!globalLocCollapsed && (
          editing ? (
            <textarea className="input" rows={12} value={editValue} onChange={e => setEditValue(e.target.value)} placeholder="记录主要地点、区域、地理特征..." />
          ) : (
            <div className="bible-content-view" style={{whiteSpace:'pre-wrap',minHeight:80,padding:12,background:'var(--bg-tertiary)',borderRadius:8}}>
              {collapseNewlines(locations) || <span className="text-muted">暂无全局地点档案，点击「编辑」手动添加</span>}
            </div>
          )
        )}
      </div>
    </div>
  );
}
