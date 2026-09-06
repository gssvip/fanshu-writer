/** DynamicMemoryPanel —— 自 WritePage.tsx 拆出（P2b 巨石拆分，逻辑零改动）。 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api';
import type { BookBible, Chapter, DynamicReport, SkillPack } from '../../types';
import { collapseNewlines, safeText } from './write-shared';

/* ===== 动态文件面板（防遗忘摘要系统） ===== */
// 动态文件报告缓存（按 bookId），避免切换 tab 重新挂载时重复请求导致打开慢
export const _dmReportsCache: Record<string, DynamicReport[]> = {};


export function DynamicMemoryPanel(props: {
  bookId: string;
  concept: string;
  bible: BookBible | null;
  onBibleUpdate: (b: BookBible) => void;
  chapters: Chapter[];
  showConfirm: (message: string, onConfirm: () => void) => void;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
}) {
  const { bookId, chapters, showConfirm, selectedSkillPackIds } = props;
  const [reports, setReports] = useState<DynamicReport[]>(_dmReportsCache[bookId] || []);
  const [loading, setLoading] = useState(_dmReportsCache[bookId] ? false : true);
  const [error, setError] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editMode, setEditMode] = useState(false);
  const [editValue, setEditValue] = useState('');
  const [editTitle, setEditTitle] = useState('');
  const [editorCollapsed, setEditorCollapsed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [batchMode, setBatchMode] = useState(false);
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const [batchDeleting, setBatchDeleting] = useState(false);
  // 按卷动态识别
  const [dynVolumes, setDynVolumes] = useState<any[]>([]);
  const [analyzingVol, setAnalyzingVol] = useState('');
  const [collapsedVolDyn, setCollapsedVolDyn] = useState<Set<number>>(new Set());
  // 每次进入维度默认折叠所有卷（tab 切换重新挂载，ref 重置）
  const dynCollapseInitRef = useRef(false);
  // 按卷编辑
  const [editingVolIdx, setEditingVolIdx] = useState<number | null>(null);
  const [editVolJson, setEditVolJson] = useState('');
  // 防遗忘检查功能已迁移到伏笔面板（ForeshadowingPanel）

  const chapterCount = chapters.filter(c => !c.is_volume).length;

  // 拉取动态报告列表并更新缓存（供手动刷新场景使用）
  function refreshReports() {
    if (!bookId) return;
    api.listDynamicReports(bookId).then(data => {
      setReports(data);
      _dmReportsCache[bookId] = data;
      setLoading(false);
    }).catch(e => {
      setError(e.message || '加载失败');
      setLoading(false);
    });
  }

  useEffect(() => {
    // 有缓存：先用缓存即时渲染（避免每次切 tab 都等网络延迟导致卡顿），
    // 再后台静默刷新一次——【dyn5】动态报告已改为全自动生成（写满每5章/导入后回填），
    // 新报告可能在缓存写入之后才落库，需要自动同步给用户看。
    if (_dmReportsCache[bookId]) {
      setReports(_dmReportsCache[bookId]);
      setLoading(false);
      api.listDynamicReports(bookId).then(data => {
        _dmReportsCache[bookId] = data;
        setReports(data);
      }).catch(() => { /* 静默失败，保持缓存数据 */ });
      return;
    }
    // 无缓存：显示加载态并发请求
    setLoading(true);
    refreshReports();
  }, [bookId]);

  // 自动选中第一份报告
  useEffect(() => {
    if (reports.length > 0 && !selectedId) {
      const r = reports[0];
      setSelectedId(r.id);
      setEditValue(collapseNewlines(r.content));
      setEditTitle(r.title);
    }
    if (reports.length === 0) setSelectedId(null);
  }, [reports]);

  // 解析按卷动态文件数据（dynamic_volumes）
  const { bible, onBibleUpdate } = props;
  useEffect(() => {
    if (!bible?.dynamic_volumes) { setDynVolumes([]); return; }
    try {
      const parsed = JSON.parse(bible.dynamic_volumes);
      if (Array.isArray(parsed)) { setDynVolumes(parsed); return; }
    } catch { /* not JSON */ }
    setDynVolumes([]);
  }, [bible?.dynamic_volumes]);

  // chapters 表的卷
  const volumeChapters = chapters.filter(c => c.is_volume);

  // 合并卷列表：chapters.is_volume 卷 + dynVolumes 已有卷
  const displayDynVolumes = useMemo(() => {
    const result: any[] = [];
    const usedIds = new Set<string>();
    for (const vc of volumeChapters) {
      const dvData = dynVolumes.find(v => v.volume_id === vc.id) || dynVolumes.find(v => v.volume === vc.title);
      result.push({
        volume_id: vc.id,
        volume: vc.title,
        data: dvData?.data || null,
        chapter_count: chapters.filter(c => c.parent_id === vc.id).length,
      });
      if (dvData) { usedIds.add(dvData.volume_id || ''); usedIds.add(dvData.volume || ''); }
    }
    for (const v of dynVolumes) {
      const id = v.volume_id || '';
      const name = v.volume || '';
      if (!usedIds.has(id) && !usedIds.has(name)) {
        result.push({ ...v, chapter_count: 0 });
      }
    }
    return result;
  }, [volumeChapters, dynVolumes, chapters]);

  // 首次有卷数据时默认折叠全部卷（每次切换到该维度 tab 重新挂载，ref 重置，实现每次进入默认折叠）
  useEffect(() => {
    if (dynCollapseInitRef.current) return;
    if (displayDynVolumes.length > 0) {
      dynCollapseInitRef.current = true;
      setCollapsedVolDyn(new Set(displayDynVolumes.map((_, idx) => idx)));
    }
  }, [displayDynVolumes]);

  // 【按卷分组报告】将扁平报告列表按卷归类，用于"动态文件按卷分类"展示。
  // 每个卷计算其下属章节的 order_index 范围（即 chapter_start 语义），
  // 报告 chapter_start 落在该范围则归入该卷；未归入任何卷的报告放入"未分卷"组。
  const reportsByVolume = useMemo(() => {
    type VolGroup = { key: string; title: string; reports: DynamicReport[]; collapsed: boolean };
    const groups: VolGroup[] = [];
    const used = new Set<string>();
    // 按 displayDynVolumes 顺序构建卷分组（与上方卷卡片顺序一致）
    for (const vol of displayDynVolumes) {
      const volId = vol.volume_id;
      const volTitle = vol.volume || `第${groups.length + 1}卷`;
      const childChapters = chapters.filter(c => c.parent_id === volId);
      if (!childChapters.length) continue;
      const minIdx = Math.min(...childChapters.map(c => c.order_index));
      const maxIdx = Math.max(...childChapters.map(c => c.order_index));
      // 报告 chapter_start 在 [minIdx, maxIdx] 视为属于该卷（order_index 从1开始与章号一致）
      const volReports = reports
        .filter(r => {
          const s = Number(r.chapter_start);
          return s >= minIdx && s <= maxIdx;
        })
        .sort((a, b) => Number(a.chapter_start) - Number(b.chapter_start));
      if (volReports.length === 0) continue;
      volReports.forEach(r => used.add(r.id));
      groups.push({ key: `vol-${volId}`, title: volTitle, reports: volReports, collapsed: false });
    }
    // 未归入任何卷的报告
    const orphan = reports.filter(r => !used.has(r.id)).sort((a, b) => Number(a.chapter_start) - Number(b.chapter_start));
    if (orphan.length > 0) {
      groups.push({ key: 'vol-orphan', title: '未分卷', reports: orphan, collapsed: false });
    }
    return groups;
  }, [reports, displayDynVolumes, chapters]);

  // 按卷分组折叠状态
  const [collapsedReportVols, setCollapsedReportVols] = useState<Set<string>>(new Set());
  // 首次有卷分组数据时默认折叠全部卷（每次进入维度重新挂载，ref 重置）
  const dynReportCollapseInitRef = useRef(false);
  useEffect(() => {
    if (dynReportCollapseInitRef.current) return;
    if (reportsByVolume.length > 0) {
      dynReportCollapseInitRef.current = true;
      setCollapsedReportVols(new Set(reportsByVolume.map(g => g.key)));
    }
  }, [reportsByVolume]);
  function toggleReportVol(key: string) {
    setCollapsedReportVols(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }

  function toggleVolDyn(idx: number) {
    setCollapsedVolDyn(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  // 【dyn5】动态报告已改为全自动：写满每5章 / 导入作品后按顺序自动生成，无需手动触发。
  // 保留的唯一 AI 入口是「📝 摘要」（按卷动态摘要写入 dynamic_volumes，属另一套数据）。

  // P0-4: AI识别指定卷的动态摘要（人物/事件/时间/地点/势力/伏笔/境界/关系），写入 dynamic_volumes
  async function handleAnalyzeDynamicVolume(volId: string, volTitle: string) {
    showConfirm(`将用 AI 分析「${volTitle}」的章节内容，识别本卷的动态摘要（人物/事件/伏笔/关系等变化），结果写入按卷动态文件。是否继续？`, async () => {
      setAnalyzingVol(volId || volTitle);
      setError('');
      try {
        const result = await api.analyzeDynamicVolume(bookId, volId, volTitle, selectedSkillPackIds);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已为「${volTitle}」生成本卷动态摘要`);
      } catch (e: any) {
        setError(e.message || 'AI识别失败');
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingVol('');
    });
  }

  // 删除某卷的动态文件数据
  async function deleteVolumeDynamic(idx: number) {
    const vol = displayDynVolumes[idx];
    if (!vol) return;
    showConfirm(`确定删除「${vol.volume || '该卷'}」的动态文件数据？`, async () => {
      const newList = dynVolumes.filter((v: any) => {
        const vId = v.volume_id || '';
        const vName = v.volume || '';
        if (vol.volume_id && vId === vol.volume_id) return false;
        if (vol.volume && vName === vol.volume) return false;
        return true;
      });
      try {
        const updated = await api.updateBible(bookId, { dynamic_volumes: JSON.stringify(newList, null, 2) } as any);
        onBibleUpdate(updated);
      } catch (e: any) {
        alert('删除失败: ' + e.message);
      }
    });
  }

  // 开始按卷编辑：将该卷的 data 序列化为 JSON 供编辑
  function startEditVolDynamic(idx: number) {
    const vol = displayDynVolumes[idx];
    if (!vol) return;
    const editTarget = vol.data || { summary: '', characters: '', events: '', timeline: '', locations: '', factions: '', foreshadowing: '', realms: '', relationships: '' };
    setEditingVolIdx(idx);
    setEditVolJson(JSON.stringify(editTarget, null, 2));
    setCollapsedVolDyn(prev => { const n = new Set(prev); n.delete(idx); return n; });
  }

  // 保存按卷编辑：解析编辑后的 JSON，写回 dynamic_volumes
  async function saveEditVolDynamic(idx: number) {
    try {
      const parsed = JSON.parse(editVolJson);
      const vol = displayDynVolumes[idx];
      const matchKey = vol.volume_id || vol.volume;
      const newList = [...dynVolumes];
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
      const updated = await api.updateBible(bookId, { dynamic_volumes: JSON.stringify(newList, null, 2) } as any);
      onBibleUpdate(updated);
      setEditingVolIdx(null);
      setEditVolJson('');
    } catch (e: any) {
      alert('保存失败：JSON 格式错误 - ' + e.message);
    }
  }

  const selectedReport = reports.find(r => r.id === selectedId) || null;

  function selectReport(r: DynamicReport) {
    setSelectedId(r.id);
    setEditMode(false);
    setEditValue(collapseNewlines(r.content));
    setEditTitle(r.title);
    setEditorCollapsed(false);
  }

  function startEditSelected() {
    if (!selectedReport) return;
    setEditMode(true);
    setEditValue(collapseNewlines(selectedReport.content));
    setEditTitle(selectedReport.title);
    setEditorCollapsed(false);
  }

  async function saveEdit() {
    if (!bookId || !selectedId) return;
    setSaving(true);
    try {
      const updated = await api.updateDynamicReport(bookId, selectedId, {
        content: collapseNewlines(editValue),
        title: editTitle,
      });
      setReports(prev => prev.map(r => r.id === selectedId ? updated : r));
      setEditMode(false);
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setSaving(false);
  }

  async function regenerate(r: DynamicReport) {
    if (!bookId) return;
    setGenerating(true);
    setError('');
    try {
      const updated = await api.regenerateDynamicReport(bookId, r.id);
      setReports(prev => prev.map(rep => rep.id === r.id ? updated : rep));
      if (selectedId === r.id) {
        setEditValue(collapseNewlines(updated.content));
        setEditTitle(updated.title);
      }
    } catch (e: any) {
      setError(e.message || '重新生成失败');
    }
    setGenerating(false);
  }

  function handleDelete(r: DynamicReport) {
    showConfirm(`确定删除「${r.title}」？此操作不可撤销。`, async () => {
      try {
        await api.deleteDynamicReport(bookId, r.id);
        setReports(prev => {
          const filtered = prev.filter(rep => rep.id !== r.id);
          if (selectedId === r.id && filtered.length > 0) {
            setSelectedId(filtered[0].id);
            setEditValue(collapseNewlines(filtered[0].content));
            setEditTitle(filtered[0].title);
          } else if (selectedId === r.id) {
            setSelectedId(null);
          }
          return filtered;
        });
      } catch (e: any) {
        alert('删除失败: ' + e.message);
      }
    });
  }

  function toggleChecked(id: string) {
    setCheckedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleSelectAll() {
    if (checkedIds.size === reports.length) {
      setCheckedIds(new Set());
    } else {
      setCheckedIds(new Set(reports.map(r => r.id)));
    }
  }

  function exitBatchMode() {
    setBatchMode(false);
    setCheckedIds(new Set());
  }

  function handleBatchDelete() {
    if (checkedIds.size === 0) return;
    showConfirm(`确定删除选中的 ${checkedIds.size} 份报告？此操作不可撤销。`, async () => {
      setBatchDeleting(true);
      try {
        const ids = Array.from(checkedIds);
        await api.batchDeleteDynamicReports(bookId, ids);
        const deletedSet = new Set(ids);
        setReports(prev => {
          const filtered = prev.filter(r => !deletedSet.has(r.id));
          if (selectedId && deletedSet.has(selectedId)) {
            if (filtered.length > 0) {
              setSelectedId(filtered[0].id);
              setEditValue(collapseNewlines(filtered[0].content));
              setEditTitle(filtered[0].title);
            } else {
              setSelectedId(null);
            }
          }
          return filtered;
        });
        exitBatchMode();
      } catch (e: any) {
        alert('批量删除失败: ' + e.message);
      }
      setBatchDeleting(false);
    });
  }

  if (loading) return <div className="page loading-screen"><span>加载动态文件...</span></div>;

  return (
    <div className="dm-panel">
      <div className="dm-header">
        <div className="dm-header-actions">
          <button
            className={batchMode ? 'btn-primary-sm' : 'btn-ghost-sm'}
            onClick={() => batchMode ? exitBatchMode() : setBatchMode(true)}
            disabled={batchDeleting || reports.length === 0}
            title="批量选择并删除报告"
          >
            {batchMode ? '✕ 退出批量' : '☑ 批量管理'}
          </button>
        </div>
      </div>

      {/* 章节进度指示：只保留章数和报告数，移除 1-5/6-10 等 chips（下方已有可编辑报告目录） */}
      <div className="dm-progress-bar">
        <div className="dm-progress-info">
          <span>📊 已有 {chapterCount} 章 · {reports.length} 份报告</span>
          {chapterCount > 0 && (
            <span className="dm-progress-next">
              ⚡ 每满5章自动生成一份（导入作品同样按顺序自动补齐），下次：第{(Math.floor(chapterCount / 5) + 1) * 5}章
            </span>
          )}
        </div>
      </div>

      {error && <div className="error-msg" style={{ marginBottom: 8 }}>{error}</div>}

          {displayDynVolumes.length > 0 && (
        <div className="plot-volume-list" style={{marginBottom:16}}>
          <div style={{marginBottom:8}}>
            <p className="text-muted" style={{fontSize:12, margin:0}}>
              📚 按卷查看：摘要（📝）→ 编辑（✏️）→ 删除（🗑️）；动态报告每满5章自动生成并显示在对应卷下方，无需手动触发。
            </p>
          </div>
          {displayDynVolumes.map((vol, idx) => {
            const d = vol.data || {};
            const hasData = vol.data && (d.summary || d.characters || d.events);
            // 从按卷分组报告里定位该卷对应分组（与"下方报告区域"共享同一个 reportsByVolume，不重复计算）
            const groupKey = `vol-${vol.volume_id}`;
            const volReportGroup = reportsByVolume.find(g => g.key === groupKey);
            const volReports = (volReportGroup?.reports || [])
              .slice()
              .sort((a, b) => Number(a.chapter_start) - Number(b.chapter_start));
            const isReportCollapsed = collapsedReportVols.has(groupKey);
            return (
              <div key={idx} className="plot-volume-card">
                <div className="plot-volume-header" onClick={() => toggleVolDyn(idx)} style={{cursor:'pointer'}}>
                  <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{collapsedVolDyn.has(idx) ? '▶' : '▼'}</span>
                  <h4>{vol.volume || `第${idx + 1}卷`}</h4>
                  {vol.chapter_count !== undefined && <span className="text-muted" style={{fontSize:12}}>{vol.chapter_count}章</span>}
                  {hasData && <span className="text-muted" style={{fontSize:12}}>已识别</span>}
                  <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                    {analyzingVol === (vol.volume_id || vol.volume) && <span className="text-muted" style={{fontSize:12}}>🤖 摘要识别中...</span>}
                    <button className="btn-ghost-sm" onClick={() => handleAnalyzeDynamicVolume(vol.volume_id || '', vol.volume || `第${idx + 1}卷`)} disabled={!!analyzingVol} title="AI识别本卷动态摘要（人物/事件/伏笔/关系）写入按卷动态文件">📝 摘要</button>
                    <button className="btn-ghost-sm" onClick={() => editingVolIdx === idx ? (setEditingVolIdx(null), setEditVolJson('')) : startEditVolDynamic(idx)} title={editingVolIdx === idx ? '取消编辑' : '编辑此卷动态文件数据（JSON）'}>{editingVolIdx === idx ? '取消' : '✏️'}</button>
                    {hasData && (
                      <button className="btn-ghost-sm" onClick={() => deleteVolumeDynamic(idx)} style={{color:'#e74c3c'}} title="删除此卷动态文件数据">🗑️</button>
                    )}
                  </div>
                </div>
                {!collapsedVolDyn.has(idx) && (
                  <div className="plot-volume-body">
                    {editingVolIdx === idx ? (
                      <div style={{marginTop:8}}>
                        <p className="text-muted" style={{fontSize:12,marginBottom:6}}>编辑本卷动态文件数据（JSON 格式）：summary/characters/events/timeline/locations/factions/foreshadowing/realms/relationships。</p>
                        <textarea className="input" value={editVolJson} onChange={e => setEditVolJson(e.target.value)} rows={18} style={{fontFamily:'monospace',fontSize:12}} />
                        <div style={{display:'flex',gap:6,marginTop:8}}>
                          <button className="btn-primary-sm" onClick={() => saveEditVolDynamic(idx)}>💾 保存</button>
                          <button className="btn-ghost-sm" onClick={() => { setEditingVolIdx(null); setEditVolJson(''); }}>取消</button>
                        </div>
                      </div>
                    ) : !hasData ? (
                      <p className="text-muted" style={{fontSize:13}}>暂无动态文件数据：点击「📝 摘要」AI识别本卷综合摘要；动态报告每满5章自动生成，无需手动触发。</p>
                    ) : (
                      <div className="plot-events">
                        {d.summary && <p><b>综合摘要：</b>{safeText(d.summary)}</p>}
                        {d.characters && <p><b>登场人物：</b>{safeText(d.characters)}</p>}
                        {d.events && <p><b>关键事件：</b>{safeText(d.events)}</p>}
                        {d.timeline && <p><b>时间线：</b>{safeText(d.timeline)}</p>}
                        {d.locations && <p><b>地点：</b>{safeText(d.locations)}</p>}
                        {d.factions && <p><b>势力动态：</b>{safeText(d.factions)}</p>}
                        {d.foreshadowing && <p><b>伏笔：</b>{safeText(d.foreshadowing)}</p>}
                        {d.realms && <p><b>境界变化：</b>{safeText(d.realms)}</p>}
                        {d.relationships && <p><b>关系变化：</b>{safeText(d.relationships)}</p>}
                      </div>
                    )}

                    {/* 报告位置：本卷下的动态报告分组（按5章一份），生成按钮点完后显示在这里 */}
                    <div style={{marginTop:12}}>
                      {volReports.length === 0 ? (
                        <p className="text-muted" style={{fontSize:12, margin:0}}>
                          🗒️ 本卷暂无动态报告：写满5章的整数倍后自动生成（如第5、10、15章…），结果显示在此。
                        </p>
                      ) : (
                        <div className="dm-volume-group" style={{borderTop:'1px solid var(--border)', paddingTop:8}}>
                          <div
                            className="dm-volume-group-header"
                            onClick={e => { e.stopPropagation(); toggleReportVol(groupKey); }}
                            style={{cursor:'pointer',display:'flex',alignItems:'center',gap:6,padding:'4px 0',marginBottom:6,userSelect:'none'}}
                          >
                            <span style={{fontSize:10,color:'var(--text-muted)'}}>{isReportCollapsed ? '▶' : '▼'}</span>
                            <span style={{fontWeight:600,fontSize:13}}>📄 本卷动态报告（{volReports.length}份）</span>
                          </div>
                          {!isReportCollapsed && (
                            <div className="dm-tab-bar" style={{marginBottom:4}}>
                              {volReports.map(r => (
                                <button
                                  key={r.id}
                                  className={`dm-tab-chip ${selectedId === r.id ? 'active' : ''}`}
                                  onClick={e => { e.stopPropagation(); selectReport(r); }}
                                  title={r.title}
                                >
                                  <span className="dm-tab-chip-range">{r.chapter_start}-{r.chapter_end}</span>
                                  {r.auto_generated && <span className="dm-tab-chip-badge">自</span>}
                                </button>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* 报告区域 */}
      {reports.length === 0 ? (
        <div className="bible-empty">
          <span className="bible-empty-icon">🗂️</span>
          <p>暂无动态报告</p>
          <p className="text-muted">
            {chapterCount >= 5
              ? '已有完整5章区间，报告将在下一次章节保存时自动补齐（导入的作品导入后即自动按顺序生成）'
              : `写满5章后自动生成，当前${chapterCount}章（导入的作品导入后自动按顺序生成）`}
          </p>
        </div>
      ) : (
        <>
          {batchMode ? (
            <>
              {/* 批量操作栏 */}
              <div className="dm-batch-bar">
                <button className="btn-ghost-sm" onClick={toggleSelectAll} disabled={batchDeleting}>
                  {checkedIds.size === reports.length && reports.length > 0 ? '取消全选' : '全选'}
                </button>
                <span className="dm-batch-count">
                  已选 {checkedIds.size}/{reports.length}
                </span>
                <button
                  className="btn-primary-sm dm-btn-danger"
                  onClick={handleBatchDelete}
                  disabled={batchDeleting || checkedIds.size === 0}
                >
                  {batchDeleting ? '⏳ 删除中...' : `🗑️ 删除选中(${checkedIds.size})`}
                </button>
              </div>
              {/* 带复选框的报告标签栏 - 按卷分类 */}
              <div className="dm-reports-by-volume">
                {reportsByVolume.map(group => {
                  const isCollapsed = collapsedReportVols.has(group.key);
                  return (
                    <div key={group.key} className="dm-volume-group">
                      <div
                        className="dm-volume-group-header"
                        onClick={() => toggleReportVol(group.key)}
                        style={{cursor:'pointer',display:'flex',alignItems:'center',gap:6,padding:'6px 4px',borderBottom:'1px solid var(--border)',marginBottom:6,userSelect:'none'}}
                      >
                        <span style={{fontSize:10,color:'var(--text-muted)'}}>{isCollapsed ? '▶' : '▼'}</span>
                        <span style={{fontWeight:600,fontSize:14}}>📖 {group.title}</span>
                        <span className="text-muted" style={{fontSize:12}}>{group.reports.length}份</span>
                      </div>
                      {!isCollapsed && (
                        <div className="dm-tab-bar dm-tab-bar-batch" style={{marginBottom:8}}>
                          {group.reports.map(r => (
                            <label
                              key={r.id}
                              className={`dm-tab-chip ${checkedIds.has(r.id) ? 'active' : ''} dm-tab-chip-checkable`}
                              title={r.title}
                            >
                              <input
                                type="checkbox"
                                checked={checkedIds.has(r.id)}
                                onChange={() => toggleChecked(r.id)}
                                disabled={batchDeleting}
                              />
                              <span className="dm-tab-chip-range">{r.chapter_start}-{r.chapter_end}</span>
                              {r.auto_generated && <span className="dm-tab-chip-badge">自</span>}
                            </label>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          ) : (
            <>
              {/* 报告列表 - 按卷分类（每卷下展示该卷的动态报告，可折叠） */}
              {reportsByVolume.length === 0 ? (
                <p className="text-muted" style={{fontSize:13,padding:'8px 0'}}>暂无动态报告：写满每5章自动生成，导入的作品导入后按顺序自动补齐。</p>
              ) : (
                <div className="dm-reports-by-volume">
                  {reportsByVolume.map(group => {
                    const isCollapsed = collapsedReportVols.has(group.key);
                    return (
                      <div key={group.key} className="dm-volume-group">
                        <div
                          className="dm-volume-group-header"
                          onClick={() => toggleReportVol(group.key)}
                          style={{cursor:'pointer',display:'flex',alignItems:'center',gap:6,padding:'6px 4px',borderBottom:'1px solid var(--border)',marginBottom:6,userSelect:'none'}}
                        >
                          <span style={{fontSize:10,color:'var(--text-muted)'}}>{isCollapsed ? '▶' : '▼'}</span>
                          <span style={{fontWeight:600,fontSize:14}}>📖 {group.title}</span>
                          <span className="text-muted" style={{fontSize:12}}>{group.reports.length}份</span>
                        </div>
                        {!isCollapsed && (
                          <div className="dm-tab-bar" style={{marginBottom:8}}>
                            {group.reports.map(r => (
                              <button
                                key={r.id}
                                className={`dm-tab-chip ${selectedId === r.id ? 'active' : ''}`}
                                onClick={() => selectReport(r)}
                                title={r.title}
                              >
                                <span className="dm-tab-chip-range">{r.chapter_start}-{r.chapter_end}</span>
                                {r.auto_generated && <span className="dm-tab-chip-badge">自</span>}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}

              {/* 折叠编辑器面板 */}
              {selectedReport && (
                <div className={`dm-editor-panel ${editorCollapsed ? 'collapsed' : ''}`}>
              <div className="dm-editor-panel-header" onClick={() => setEditorCollapsed(!editorCollapsed)}>
                <div className="dm-editor-panel-title">
                  <span className="dm-editor-toggle">{editorCollapsed ? '▶' : '▼'}</span>
                  {editMode ? (
                    <input
                      className="dm-report-title-input"
                      value={editTitle}
                      onChange={e => setEditTitle(e.target.value)}
                      onClick={e => e.stopPropagation()}
                    />
                  ) : (
                    <>
                      <span className="dm-report-icon">📄</span>
                      <span className="dm-editor-panel-name">{selectedReport.title}</span>
                      {selectedReport.auto_generated && <span className="dm-badge dm-badge-auto">自动</span>}
                    </>
                  )}
                </div>
                <div className="dm-editor-panel-actions" onClick={e => e.stopPropagation()}>
                  {editMode ? (
                    <>
                      <button className="btn-ghost-sm" onClick={() => setEditMode(false)} disabled={saving}>取消</button>
                      <button className="btn-primary-sm" onClick={saveEdit} disabled={saving}>
                        {saving ? '保存中...' : '💾 保存'}
                      </button>
                    </>
                  ) : (
                    <>
                      <button className="btn-ghost-sm" onClick={() => regenerate(selectedReport)} disabled={generating} title="AI重新生成">
                        {generating ? '⏳' : '🔄'}
                      </button>
                      <button className="btn-ghost-sm" onClick={startEditSelected} title="编辑">✏️</button>
                      <button className="btn-ghost-sm dm-btn-danger" onClick={() => handleDelete(selectedReport)} title="删除">🗑️</button>
                    </>
                  )}
                </div>
              </div>
              {!editorCollapsed && (
                <div className="dm-editor-panel-body">
                  {editMode ? (
                    <textarea
                      className="input dm-report-editor"
                      rows={12}
                      value={editValue}
                      onChange={e => setEditValue(e.target.value)}
                      autoFocus
                    />
                  ) : (
                    <div className="dm-report-content">
                      {selectedReport.content ? collapseNewlines(selectedReport.content).split('\n').filter((l: string) => l.trim() !== '').map((line: string, i: number) => (
                        <p key={i}>{line}</p>
                      )) : <p className="text-muted">暂无内容</p>}
                    </div>
                  )}
                  <div className="dm-report-meta">
                    第{selectedReport.chapter_start}-{selectedReport.chapter_end}章 · {selectedReport.content.length}字
                  </div>
                </div>
              )}
            </div>
          )}
            </>
          )}
        </>
      )}
    </div>
  );
}
