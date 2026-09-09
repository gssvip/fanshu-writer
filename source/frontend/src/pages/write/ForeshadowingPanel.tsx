/** ForeshadowingPanel —— 自 WritePage.tsx 拆出（P2b 巨石拆分，逻辑零改动）。 */
import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api';
import { useStore } from '../../store';
import type { BookBible, Chapter, SkillPack } from '../../types';
import { SkillPackGroupedList, extractSkillPrompt } from './write-shared';

/* ===== 伏笔面板（按卷） ===== */
export function ForeshadowingPanel(props: {
  bookId: string;
  bible: BookBible | null;
  onBibleUpdate: (b: BookBible) => void;
  bookTitle: string;
  chapters: Chapter[];
  hasChapters: boolean;
  showConfirm: (message: string, onConfirm: () => void) => void;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  selectedSkillPacks: SkillPack[];
  onOpenAiCreate: () => void;
  onAfStatusChange?: (pendingCount: number, alert: { reportId: string; title: string; score?: number; auto?: boolean } | null) => void;
}) {
  const { bookId, bible, onBibleUpdate, chapters, hasChapters, showConfirm, skillPacks, selectedSkillPackIds, selectedSkillPacks, onAfStatusChange } = props;
  const openChatPanel = useStore((s: any) => s.openChatPanel) as (bid: string, sessionId?: string | null, preset?: { tab?: 'setting' | 'chapter' | 'deai' | 'review'; input?: string; fixTasks?: Array<{ location: string; desc: string; fix: string; severity?: string; dimKey?: string }> }) => void;
  // 读取当前智驾会话 id：修正入口首次进入时为 null（新建会话），后续复用同一会话
  const chatPanelSessionId = useStore((s: any) => s.chatPanelSessionId) as string | null;
  // 从 chapters 派生卷列表和按卷分组的章节（供修正入口的卷选择弹窗使用）
  const volumes = useMemo(() => chapters.filter(c => c.is_volume).sort((a, b) => a.order_index - b.order_index), [chapters]);
  const chaptersByVolume = useMemo(() => {
    const map: Record<string, Chapter[]> = {};
    for (const v of volumes) map[v.id] = [];
    for (const c of chapters) {
      if (c.is_volume) continue;
      const pid = c.parent_id;
      if (pid && map[pid]) map[pid].push(c);
    }
    return map;
  }, [chapters, volumes]);
  const [foreshadowing, setForeshadowing] = useState('');
  const [foreVolumes, setForeVolumes] = useState<any[]>([]);
  const [analyzingVol, setAnalyzingVol] = useState('');
  const [volSelectorOpen, setVolSelectorOpen] = useState(false);
  const [aiMode, setAiMode] = useState(false);

  // 按卷分批修正弹窗已随 AI修正/修正正文 改为跳转智驾而移除（fixVolSelector*/availableVolumes 状态已删）

  const [aiPrompt, setAiPrompt] = useState('');
  const [aiAssisting, setAiAssisting] = useState(false);
  const [aiError, setAiError] = useState('');
  const [skillExpanded, setSkillExpanded] = useState(false);

  // ===== 防遗忘检查（迁移自动态文件面板）=====
  const [afReports, setAfReports] = useState<any[]>([]);
  const [afLoading, setAfLoading] = useState(false);
  const [afChecking, setAfChecking] = useState(false);
  const [afVolPickerOpen, setAfVolPickerOpen] = useState(false); // 分卷选择弹窗
  const [afSelectedVolIds, setAfSelectedVolIds] = useState<string[]>([]); // 多选分卷
  // 修正入口的卷选择弹窗：从「AI修正/修正正文」点击时弹出，选定后按卷过滤违规项再跳转智驾
  const [fixVolPicker, setFixVolPicker] = useState<{ reportId: string; mode: 'setting' | 'chapter' } | null>(null);
  const [fixSelectedVolId, setFixSelectedVolId] = useState<string>(''); // '' = 全部
  const [afCollapsed, setAfCollapsed] = useState<Set<string>>(new Set()); // 报告折叠状态
  const [afEditingId, setAfEditingId] = useState<string | null>(null); // 正在编辑内容的报告 id
  const [afEditValue, setAfEditValue] = useState('');
  const [afRenamingId, setAfRenamingId] = useState<string | null>(null); // 正在重命名的报告 id
  const [afRenameValue, setAfRenameValue] = useState('');
  const [afSectionOpen, setAfSectionOpen] = useState(true); // 防遗忘检查区折叠
  const [afScope, setAfScope] = useState<'reports' | 'dimensions'>('reports'); // 检查范围：动态文件/仅维度
  const [foreCollapsed, setForeCollapsed] = useState(false); // 伏笔按卷区折叠状态

  // 「AI修正/修正正文」均改为跳转 AI智驾协同，不再使用独立面板（fix*/textFix* 状态已移除）

  // 解析全局伏笔
  useEffect(() => {
    setForeshadowing(bible?.foreshadowing || '');
  }, [bible?.foreshadowing]);

  // 解析按卷伏笔
  useEffect(() => {
    if (!bible?.foreshadowing_volumes) { setForeVolumes([]); return; }
    try {
      const parsed = JSON.parse(bible.foreshadowing_volumes);
      if (Array.isArray(parsed)) { setForeVolumes(parsed); return; }
    } catch { /* not JSON */ }
    setForeVolumes([]);
  }, [bible?.foreshadowing_volumes]);

  const volumeChapters = chapters.filter(c => c.is_volume);

  const displayVolumes = useMemo(() => {
    const result: any[] = [];
    const usedIds = new Set<string>();
    for (const vc of volumeChapters) {
      const fvData = foreVolumes.find(v => v.volume_id === vc.id) || foreVolumes.find(v => v.volume === vc.title);
      result.push({
        volume_id: vc.id,
        volume: vc.title,
        data: fvData?.data || null,
        chapter_count: chapters.filter(c => c.parent_id === vc.id).length,
      });
      if (fvData) { usedIds.add(fvData.volume_id || ''); usedIds.add(fvData.volume || ''); }
    }
    for (const v of foreVolumes) {
      const id = v.volume_id || '';
      const name = v.volume || '';
      if (!usedIds.has(id) && !usedIds.has(name)) {
        result.push({ ...v, chapter_count: 0 });
      }
    }
    return result;
  }, [volumeChapters, foreVolumes, chapters]);

  async function saveForeshadowing(val: string) {
    try {
      const updated = await api.updateBible(bookId, { foreshadowing: val } as any);
      onBibleUpdate(updated);
      setForeshadowing(val);
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
  }

  async function handleAnalyzeVolume(volId: string, volTitle: string) {
    showConfirm(`将用 AI 分析「${volTitle}」的章节内容，识别本卷埋设/回收的伏笔。是否继续？`, async () => {
      setAnalyzingVol(volId || volTitle);
      try {
        const result = await api.analyzeForeshadowingVolume(bookId, volId, volTitle, selectedSkillPackIds);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已为「${volTitle}」生成伏笔分析`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingVol('');
    });
  }

  // ===== 防遗忘检查：加载报告列表 =====
  function sortAfReports(list: any[]): any[] {
    return [...list].sort((a, b) => (b.seq || 0) - (a.seq || 0));
  }
  function computeAfStatus(list: any[]) {
    const pending = list.filter(r => r.status === 'pending' || (!r.status && (r.fix_draft?.length > 0 || (typeof r.health_score === 'number' && r.health_score < 80))));
    const count = pending.length;
    const top = pending.find(r => r.auto_generated && !r.notified && (r.fix_draft?.length > 0 || (typeof r.health_score === 'number' && r.health_score < 80)));
    const alert = top ? { reportId: top.id, title: top.title, score: top.health_score, auto: true } : null;
    return { count, alert };
  }
  function loadAfReports() {
    if (!bookId) return;
    setAfLoading(true);
    api.listAntiForgetReports(bookId).then(data => {
      const list = sortAfReports(Array.isArray(data.reports) ? data.reports : []);
      setAfReports(list);
      const { count, alert } = computeAfStatus(list);
      if (onAfStatusChange) onAfStatusChange(count, alert);
    }).catch(() => { setAfReports([]); if (onAfStatusChange) onAfStatusChange(0, null); })
      .finally(() => setAfLoading(false));
  }

  useEffect(() => {
    loadAfReports();
    const id = setInterval(loadAfReports, 30000);
    return () => clearInterval(id);
  }, [bookId]);

  // 点击「防遗忘检查」按钮：弹出分卷选择
  function openAfVolPicker() {
    setAfSelectedVolIds([]);
    setAfVolPickerOpen(true);
  }

  // 切换分卷多选
  function toggleAfVol(id: string) {
    setAfSelectedVolIds(prev => prev.includes(id) ? prev.filter(v => v !== id) : [...prev, id]);
  }

  // 执行防遗忘检查
  async function runAfCheck(volumeIds: string[]) {
    if (!hasChapters && afScope === 'reports') { alert('暂无章节内容，无法检查'); return; }
    setAfChecking(true);
    try {
      const result = await api.aiAntiForgetCheck(bookId, afScope, selectedSkillPackIds, volumeIds);
      // 刷新报告列表
      loadAfReports();
      // 自动展开新报告
      if (result.report_record?.id) {
        setAfCollapsed(prev => { const n = new Set(prev); n.delete(result.report_record.id); return n; });
        setAfSectionOpen(true);
      }
      const score = result.report?.health_score;
      const scopeName = afScope === 'dimensions' ? '仅维度' : '动态文件';
      alert(`✅ 防遗忘检查完成（${scopeName}${result.source_label ? ' · ' + result.source_label : ''}）${typeof score === 'number' ? `\n健康度评分：${score}` : ''}`);
    } catch (e: any) {
      alert('防遗忘检查失败：' + (e.message || '请检查AI配置'));
    }
    setAfChecking(false);
  }

  // 确认分卷选择后执行检查
  function confirmAfVolPicker() {
    setAfVolPickerOpen(false);
    runAfCheck(afSelectedVolIds);
  }

  function toggleAfReport(id: string) {
    setAfCollapsed(prev => { const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  }

  // 编辑报告内容
  function startAfEdit(r: any) {
    setAfEditingId(r.id);
    setAfEditValue(typeof r.report === 'string' ? r.report : JSON.stringify(r.report, null, 2));
    setAfCollapsed(prev => { const n = new Set(prev); n.delete(r.id); return n; });
  }

  async function saveAfEdit(r: any) {
    let parsed: any = afEditValue;
    try { parsed = JSON.parse(afEditValue); } catch { /* 允许纯文本保存 */ }
    try {
      const data = await api.updateAntiForgetReport(bookId, r.id, { report: parsed, summary: parsed?.summary || r.summary, health_score: parsed?.health_score ?? r.health_score });
      setAfReports(sortAfReports(Array.isArray(data.reports) ? data.reports : []));
      setAfEditingId(null);
      setAfEditValue('');
    } catch (e: any) {
      alert('保存失败：' + e.message);
    }
  }

  function startAfRename(r: any) {
    setAfRenamingId(r.id);
    setAfRenameValue(r.title || '');
  }

  async function saveAfRename(r: any) {
    const newTitle = afRenameValue.trim();
    if (!newTitle) { setAfRenamingId(null); return; }
    try {
      const data = await api.updateAntiForgetReport(bookId, r.id, { title: newTitle });
      setAfReports(sortAfReports(Array.isArray(data.reports) ? data.reports : []));
    } catch (e: any) {
      alert('重命名失败：' + e.message);
    }
    setAfRenamingId(null);
    setAfRenameValue('');
  }

  function deleteAfReport(r: any) {
    showConfirm(`确定删除检查报告「${r.title || r.id}」？此操作不可撤销。`, async () => {
      try {
        const data = await api.deleteAntiForgetReport(bookId, r.id);
        setAfReports(sortAfReports(Array.isArray(data.reports) ? data.reports : []));
      } catch (e: any) {
        alert('删除失败：' + e.message);
      }
    });
  }

  // 按卷分批选择弹窗已随 AI修正/修正正文 改为跳转 AI智驾而移除

  // 从违规项 location 解析章节号（与 ChatPanel 口径一致）
  function parseLocChapterNum(loc: string): number | null {
    if (!loc) return null;
    const m = loc.match(/第?\s*(\d+)\s*章/);
    return m ? parseInt(m[1]) : null;
  }

  // 「修正正文」改为跳转 AI智驾·正文Tab，带结构化任务清单，由用户逐章协同改写并追踪进度
  function jumpToChatForTextFix(reportId: string, volumeId?: string) {
    const rec = afReports.find((r: any) => r.id === reportId);
    const rep = rec?.report || {};
    const allViolations = Array.isArray(rep.violations) ? rep.violations : [];
    // 按卷过滤：若指定卷，则只保留 location 章节号落在该卷章节范围内的违规项
    let violations = allViolations;
    if (volumeId) {
      const volChapters = chaptersByVolume[volumeId] || [];
      const volNums = new Set(volChapters.map(c => c.order_index));
      violations = allViolations.filter((v: any) => {
        const n = parseLocChapterNum(String(v.location || ''));
        return n !== null && volNums.has(n);
      });
    }
    // 全部违规项转为任务（不限制条数，location 为空时用 type 作为位置标识）
    const fixTasks = violations.map((v: any) => ({
      location: String(v.location || v.type || '未指定位置'),
      desc: String(v.desc || ''),
      fix: String(v.fix || ''),
      severity: v.severity,
    }));
    const volLabel = volumeId ? `·${volumes.find(v => v.id === volumeId)?.title || ''}·` : '';
    const title = rec?.title ? `「${rec.title}${volLabel}」` : '防遗忘检查报告';
    const presetInput = fixTasks.length > 0
      ? `基于${title}的 ${fixTasks.length} 处违规，请在下方任务清单逐条「去修改」`
      : `参考${title}，请选择章节并说明修改意见后点「✨ 修改」`;
    // 首次进入 chatPanelSessionId 为 null（新建会话）；之后复用同一会话延续修正上下文
    openChatPanel(bookId, chatPanelSessionId, { tab: 'chapter', input: presetInput, fixTasks: fixTasks.length > 0 ? fixTasks : undefined });
  }

  // 「AI修正」改为跳转 AI智驾·设定Tab，带结构化任务清单，由用户逐维度协同修正并追踪进度
  function jumpToChatForSettingFix(reportId: string, volumeId?: string) {
    const rec = afReports.find((r: any) => r.id === reportId);
    const rep = rec?.report || {};
    const allViolations = Array.isArray(rep.violations) ? rep.violations : [];
    // 按卷过滤
    let violations = allViolations;
    if (volumeId) {
      const volChapters = chaptersByVolume[volumeId] || [];
      const volNums = new Set(volChapters.map(c => c.order_index));
      violations = allViolations.filter((v: any) => {
        const n = parseLocChapterNum(String(v.location || ''));
        return n === null || volNums.has(n);
      });
    }
    // 全部违规项转为任务（不限制条数）
    const fixTasks: any[] = violations.map((v: any) => ({
      location: String(v.location || v.type || '未指定位置'),
      desc: String(v.desc || ''),
      fix: String(v.fix || ''),
      severity: v.severity,
    }));
    // 待回收伏笔单列任务（强制归到 foreshadowing 维度）
    const pf = Array.isArray(rep.pending_foreshadowing) ? rep.pending_foreshadowing : [];
    pf.forEach((f: any) => {
      fixTasks.push({
        location: '伏笔',
        desc: `待回收伏笔：${f.content || ''}`,
        fix: f.suggest_chapter ? `建议回收于 ${f.suggest_chapter}` : '安排后续章节回收',
        severity: 'medium',
        dimKey: 'foreshadowing',
      });
    });
    // 叙事债务单列任务（归到 plot_design 维度）
    const nd = Array.isArray(rep.narrative_debt) ? rep.narrative_debt : [];
    nd.forEach((d: any) => {
      fixTasks.push({
        location: '叙事债务',
        desc: `叙事债务：${d.promise || ''}`,
        fix: d.status ? `当前状态：${d.status}` : '在后续章节补足兑现',
        severity: 'medium',
        dimKey: 'plot_design',
      });
    });
    const volLabel = volumeId ? `·${volumes.find(v => v.id === volumeId)?.title || ''}·` : '';
    const title = rec?.title ? `「${rec.title}${volLabel}」` : '防遗忘检查报告';
    const presetInput = fixTasks.length > 0
      ? `基于${title}的 ${fixTasks.length} 项设定诊断，请在下方任务清单逐条「去修正」`
      : `基于${title}，请检查并修正设定维度的一致性问题。`;
    // 首次进入 chatPanelSessionId 为 null（新建会话）；之后复用同一会话延续修正上下文
    openChatPanel(bookId, chatPanelSessionId, { tab: 'setting', input: presetInput, fixTasks: fixTasks.length > 0 ? fixTasks : undefined });
  }

  async function ignoreFixDraft(reportId: string) {
    if (!reportId) return;
    try {
      await api.updateAntiForgetReport(bookId, reportId, { status: 'ignored', fix_draft: null });
      setAfReports(prev => prev.map(r => r.id === reportId ? { ...r, status: 'ignored', fix_draft: null } : r));
      if (onAfStatusChange) {
        const next = afReports.map(r => r.id === reportId ? { ...r, status: 'ignored', fix_draft: null } : r);
        onAfStatusChange(computeAfStatus(next).count, computeAfStatus(next).alert);
      }
    } catch (e: any) {
      alert('标记忽略失败：' + (e.message || '未知错误'));
    }
  }

  // 「AI修正/修正正文」均改为跳转 AI智驾协同，独立生成/应用/草稿函数已移除

  async function executeAi() {
    if (!aiPrompt.trim()) { alert('请输入创作要求'); return; }
    setAiAssisting(true);
    setAiError('');
    try {
      const skillKeys = ['foreshadow_register', 'narrative_debt'];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const messages = [
        { role: 'system', content: `你是专业网文伏笔设计师。请根据用户要求生成伏笔设计。${skillNote}` },
        { role: 'user', content: `构思：${bible?.concept || '暂无'}\n已有伏笔：${(foreshadowing || '').slice(0, 500) || '无'}\n\n用户要求：${aiPrompt}\n\n请生成伏笔设计，包括埋设时机、回收方式、关联角色。` },
      ];
      const result = await api.aiChat(messages);
      if (result.content) {
        await saveForeshadowing(result.content);
        setAiMode(false);
        setAiPrompt('');
      } else {
        setAiError('AI返回为空，请重试');
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

  if (aiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <h3>🔮 AI协同创作 · 伏笔</h3>
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
                onToggleSkillPack={() => {}}
                disabled={aiAssisting}
              />
            )}
          </div>
        )}
        <div className="ai-prompt-vertical">
          <textarea className="input bible-ai-prompt-input" rows={6} value={aiPrompt} onChange={e => setAiPrompt(e.target.value)} onKeyDown={handlePromptKeyDown} placeholder="例如：设计贯穿全书的核心伏笔，埋设3个关键悬念..." disabled={aiAssisting} autoFocus />
          <div className="ai-prompt-bottom-row">
            <button className="btn-primary ai-prompt-submit" onClick={executeAi} disabled={aiAssisting || !aiPrompt.trim()}>{aiAssisting ? '⏳ 创作中...' : '🚀 发送'}</button>
          </div>
        </div>
        {aiError && <div className="error-msg" style={{marginTop:8}}>{aiError}</div>}
        {aiAssisting && <div className="bible-ai-loading"><div className="loading-spinner" /><p>AI正在生成伏笔设计...</p></div>}
      </div>
    );
  }

  return (
    <div className="bible-edit-panel">
      <div className="bible-edit-header" style={{display:'flex',justifyContent:'space-between',alignItems:'center'}}>
        <div className="bible-edit-actions" style={{display:'flex',alignItems:'center',gap:8,position:'relative'}}>
          {(
            <>
              <button className="btn-ghost-sm" onClick={() => setVolSelectorOpen(v => !v)} disabled={!!analyzingVol || !hasChapters} title={hasChapters ? '选择卷进行AI识别' : '需要先创建章节才能AI识别'}>
                {analyzingVol ? '🤖 识别中...' : '🔍 AI识别'}
              </button>
              {volSelectorOpen && (
                <div className="vol-selector-dropdown" style={{position:'absolute',top:'100%',left:0,marginTop:4,background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:8,padding:6,minWidth:180,zIndex:100,boxShadow:'0 4px 12px rgba(0,0,0,0.15)'}}>
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
        <span className="text-muted" style={{fontSize:12,cursor:'pointer'}} onClick={() => setForeCollapsed(v => !v)}>
          {foreCollapsed ? '▶ 展开' : '▼ 收起'}
        </span>
      </div>
      {!foreCollapsed && (
        <>
          <p className="text-muted" style={{fontSize:12, marginBottom:8}}>
            记录伏笔的埋设时机、回收方式、关联角色。点击「🔍 AI识别」选择卷，识别结果自动归类到对应卷下。
          </p>

          {/* ===== 防遗忘检查（原全局伏笔档案位置）===== */}
      <div className="bible-edit-section" style={{marginTop:16}}>
        <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:8}}>
          <div style={{display:'flex',alignItems:'center',gap:8}}>
            <h4 style={{margin:0}}>🛡️ 防遗忘检查{afReports.length > 0 && <span className="text-muted" style={{fontSize:12,fontWeight:400}}>（{afReports.length}）</span>}{(() => { const c = afReports.filter((r: any) => r.status === 'pending' || (!r.status && r.fix_draft?.length > 0)).length; return c > 0 ? <span style={{fontSize:11,background:'#ff4757',color:'#fff',borderRadius:10,padding:'2px 8px',marginLeft:6}}>待审阅 {c}</span> : null; })()}</h4>
            <button
              className="btn-primary-sm"
              onClick={openAfVolPicker}
              disabled={afChecking}
              style={{fontSize:14,padding:'4px 14px'}}
            >
              {afChecking ? '⏳ 检查中...' : '🛡️ 开始检查'}
            </button>
          </div>
          <span className="text-muted" style={{fontSize:12,cursor:'pointer'}} onClick={() => setAfSectionOpen(v => !v)}>{afSectionOpen ? '▼ 收起' : '▶ 展开'}</span>
        </div>

        {afSectionOpen && (
          <>
            {/* 检查资料范围选择（动态文件 / 仅维度）*/}
            <div style={{display:'flex',alignItems:'center',gap:8,flexWrap:'wrap',marginBottom:8}}>
              <span style={{fontSize:13,color:'var(--text-muted)'}}>检查资料：</span>
              <button
                onClick={() => setAfScope('reports')}
                disabled={afChecking}
                title="检查所有动态报告"
                style={{padding:'4px 12px',fontSize:13,borderRadius:6,cursor:'pointer',border:`1px solid ${afScope==='reports'?'var(--accent)':'var(--border)'}`,background:afScope==='reports'?'var(--accent-light)':'transparent',color:afScope==='reports'?'var(--accent)':'var(--text)',fontWeight:afScope==='reports'?600:400}}
              >📄 动态文件</button>
              <button
                onClick={() => setAfScope('dimensions')}
                disabled={afChecking}
                title="查阅除构思、章节外所有维度"
                style={{padding:'4px 12px',fontSize:13,borderRadius:6,cursor:'pointer',border:`1px solid ${afScope==='dimensions'?'var(--accent)':'var(--border)'}`,background:afScope==='dimensions'?'var(--accent-light)':'transparent',color:afScope==='dimensions'?'var(--accent)':'var(--text)',fontWeight:afScope==='dimensions'?600:400}}
              >📐 仅维度</button>
            </div>
            <p className="text-muted" style={{fontSize:12,marginBottom:10}}>
              选择范围后点击「开始检查」生成报告；AI 会在章节数达到 10 的倍数时自动检查。
            </p>

            {/* 自动检查草稿提示 */}
            {afReports.some((r: any) => r.status === 'pending' && r.auto_generated && r.fix_draft?.length > 0) && (
              <div style={{background:'#fff3cd',border:'1px solid #ffeaa7',borderRadius:6,padding:'8px 10px',marginBottom:10,fontSize:13}}>
                <b>🤖 AI 自动检查提醒</b>：检测到 {afReports.filter((r: any) => r.status === 'pending' && r.auto_generated && r.fix_draft?.length > 0).length} 份自动检查报告已生成修正草稿，请展开报告后点击「查看修正草稿」审阅并决定是否应用。
              </div>
            )}

            {/* 报告列表 */}
            {afLoading ? (
              <p className="text-muted" style={{fontSize:13}}>加载报告中...</p>
            ) : afReports.length === 0 ? (
              <p className="text-muted" style={{fontSize:13}}>暂无检查报告，点击「🛡️ 开始检查」开始首次检查。</p>
            ) : (
              <div className="plot-volume-list">
                {afReports.map((r: any) => {
                  const rep = r.report || {};
                  const collapsed = afCollapsed.has(r.id);
                  const isEditing = afEditingId === r.id;
                  const isRenaming = afRenamingId === r.id;
                  const score = r.health_score ?? rep.health_score;
                  const scopeLabel = r.source_label || (r.volume_ids && r.volume_ids.length ? `指定${r.volume_ids.length}卷` : '全部章节');
                  return (
                    <div key={r.id} className="plot-volume-card">
                      <div className="plot-volume-header" style={{cursor:'pointer'}} onClick={() => !isEditing && !isRenaming && toggleAfReport(r.id)}>
                        <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{collapsed ? '▶' : '▼'}</span>
                        {isRenaming ? (
                          <input
                            type="text"
                            className="input"
                            value={afRenameValue}
                            onChange={e => setAfRenameValue(e.target.value)}
                            onClick={e => e.stopPropagation()}
                            onKeyDown={e => { if (e.key === 'Enter') saveAfRename(r); if (e.key === 'Escape') setAfRenamingId(null); }}
                            style={{flex:1,fontSize:13,padding:'2px 6px'}}
                            autoFocus
                          />
                        ) : (
                          <h4 style={{margin:0}}>{r.title || `检查${String(r.seq || 0).padStart(2,'0')}`}</h4>
                        )}
                        {typeof score === 'number' && !isRenaming && (
                          <span style={{fontSize:12,fontWeight:600,color: score >= 80 ? 'var(--success)' : score >= 60 ? 'var(--accent)' : 'var(--danger)'}}>健康度 {score}</span>
                        )}
                        {!isRenaming && !isEditing && <span className="text-muted" style={{fontSize:11}}>{scopeLabel}{r.ch_count ? ` · ${r.ch_count}章` : ''}</span>}
                        {!isRenaming && !isEditing && r.status === 'pending' && <span style={{fontSize:11,background:'#ff4757',color:'#fff',borderRadius:10,padding:'1px 6px'}}>待审阅</span>}
                        {!isRenaming && !isEditing && r.status === 'reviewed' && <span style={{fontSize:11,background:'#74b9ff',color:'#fff',borderRadius:10,padding:'1px 6px'}}>已审阅</span>}
                        {!isRenaming && !isEditing && r.status === 'applied' && <span style={{fontSize:11,background:'#55efc4',color:'#006266',borderRadius:10,padding:'1px 6px'}}>已应用</span>}
                        {!isRenaming && !isEditing && r.status === 'ignored' && <span style={{fontSize:11,background:'#b2bec3',color:'#fff',borderRadius:10,padding:'1px 6px'}}>已忽略</span>}
                        <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                          {isRenaming ? (
                            <>
                              <button className="btn-primary-sm" onClick={() => saveAfRename(r)}>💾</button>
                              <button className="btn-ghost-sm" onClick={() => setAfRenamingId(null)}>✕</button>
                            </>
                          ) : isEditing ? (
                            <>
                              <button className="btn-primary-sm" onClick={() => saveAfEdit(r)}>💾 保存</button>
                              <button className="btn-ghost-sm" onClick={() => { setAfEditingId(null); setAfEditValue(''); }}>取消</button>
                            </>
                          ) : (
                            <>
                              <button className="btn-ghost-sm" onClick={() => toggleAfReport(r.id)} title={collapsed ? '展开' : '折叠'}>{collapsed ? '📥 拉取' : '📂 折叠'}</button>
                              <button className="btn-primary-sm" onClick={() => { setFixVolPicker({ reportId: r.id, mode: 'setting' }); setFixSelectedVolId(''); }} title="跳转 AI智驾·设定，协同修正设定维度">
                                🔧 AI修正
                              </button>
                              <button className="btn-ghost-sm" onClick={() => { setFixVolPicker({ reportId: r.id, mode: 'chapter' }); setFixSelectedVolId(''); }} title="跳转 AI智驾·正文，协同修正违规章节">📝 修正正文</button>
                              {r.status === 'pending' && (
                                <button className="btn-ghost-sm" onClick={() => ignoreFixDraft(r.id)} title="忽略此报告的修正草稿">🚫 忽略</button>
                              )}
                              <button className="btn-ghost-sm" onClick={() => startAfEdit(r)} title="编辑报告内容">✏️</button>
                              <button className="btn-ghost-sm" onClick={() => startAfRename(r)} title="重命名">🏷️</button>
                              <button className="btn-ghost-sm" onClick={() => deleteAfReport(r)} style={{color:'#e74c3c'}} title="删除">🗑️</button>
                            </>
                          )}
                        </div>
                      </div>
                      {!collapsed && (
                        <div className="plot-volume-body">
                          <div className="text-muted" style={{fontSize:11,marginBottom:6}}>
                            {r.checked_at ? new Date(r.checked_at).toLocaleString('zh-CN') : ''} · 检查范围：{scopeLabel}
                          </div>
                          {isEditing ? (
                            <div>
                              <p className="text-muted" style={{fontSize:12,marginBottom:6}}>编辑报告内容（JSON 格式，保存时自动解析）：</p>
                              <textarea className="input" value={afEditValue} onChange={e => setAfEditValue(e.target.value)} rows={18} style={{fontFamily:'monospace',fontSize:12}} />
                            </div>
                          ) : (
                            <div className="plot-events">
                              {(r.summary || rep.summary) && <p style={{marginBottom:8}}><b>总览：</b>{r.summary || rep.summary}</p>}
                              {Array.isArray(rep.violations) && rep.violations.length > 0 && (
                                <div style={{marginBottom:8}}><b>⚠️ 一致性违规（{rep.violations.length}）：</b><ul>
                                  {rep.violations.map((v: any, i: number) => (
                                    <li key={i}><span style={{color:'var(--danger)',fontWeight:600}}>[{v.severity||'提示'}] {v.type||''}</span>{v.location && <span style={{color:'#888'}}> · {v.location}</span>}{v.desc && <span style={{color:'#666'}}> — {v.desc}</span>}{v.fix && <span style={{color:'var(--success)'}}> 💡{v.fix}</span>}</li>
                                  ))}
                                </ul></div>
                              )}
                              {Array.isArray(rep.pending_foreshadowing) && rep.pending_foreshadowing.length > 0 && (
                                <div style={{marginBottom:8}}><b>🔮 待回收伏笔（{rep.pending_foreshadowing.length}）：</b><ul>
                                  {rep.pending_foreshadowing.map((f: any, i: number) => (
                                    <li key={i}><span style={{color:'#e87d3e',fontWeight:600}}>{f.content||''}</span>{f.urgency && <span style={{color:'#888'}}> · {f.urgency}</span>}{f.suggest_chapter && <span style={{color:'#666'}}> — 建议回收于 {f.suggest_chapter}</span>}</li>
                                  ))}
                                </ul></div>
                              )}
                              {Array.isArray(rep.narrative_debt) && rep.narrative_debt.length > 0 && (
                                <div style={{marginBottom:8}}><b>📊 叙事债务（{rep.narrative_debt.length}）：</b><ul>
                                  {rep.narrative_debt.map((d: any, i: number) => (
                                    <li key={i}><span style={{color:'#e87d3e',fontWeight:600}}>{d.promise||''}</span>{d.status && <span style={{color:'#888'}}> · {d.status}</span>}{d.priority && <span style={{color:'#e74c3c'}}> · {d.priority}</span>}</li>
                                  ))}
                                </ul></div>
                              )}
                              {Array.isArray(rep.character_cognition_issues) && rep.character_cognition_issues.length > 0 && (
                                <div style={{marginBottom:8}}><b>👥 角色认知边界问题：</b><ul>
                                  {rep.character_cognition_issues.map((c: string, i: number) => <li key={i}>{c}</li>)}
                                </ul></div>
                              )}
                              {Array.isArray(rep.locked_facts) && rep.locked_facts.length > 0 && (
                                <div style={{marginBottom:8}}><b>🔒 锁定事实清单：</b><ul>
                                  {rep.locked_facts.map((f: string, i: number) => <li key={i}>{f}</li>)}
                                </ul></div>
                              )}
                              {Array.isArray(rep.suggestions) && rep.suggestions.length > 0 && (
                                <div><b>💡 改进建议：</b><ul>
                                  {rep.suggestions.map((s: string, i: number) => <li key={i}>{s}</li>)}
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
          </>
        )}
      </div>

      {/* AI修正/修正正文 均改为跳转 AI智驾协同，独立设定修正面板已移除 */}
        </>
      )}

      {/* 防遗忘检查 · 分卷选择弹窗（单选/多选）*/}
      {afVolPickerOpen && (
        <div className="modal-overlay" onClick={() => setAfVolPickerOpen(false)}>
          <div className="modal-content" style={{maxWidth:460}} onClick={e => e.stopPropagation()}>
            <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:8}}>
              <h3 style={{margin:0}}>🛡️ 防遗忘检查 · 选择分卷</h3>
              <button className="btn-ghost-sm" onClick={() => setAfVolPickerOpen(false)}>✕</button>
            </div>
            <p className="text-muted" style={{fontSize:12,marginBottom:6}}>
              当前检查资料：<b>{afScope === 'dimensions' ? '📐 仅维度（除构思、章节外所有维度）' : '📄 动态文件（所有动态报告）'}</b>
            </p>
            <p className="text-muted" style={{fontSize:12,marginBottom:10}}>
              勾选要检查的分卷（可多选）；不勾选任何卷则检查全部章节。
            </p>
            <div style={{maxHeight:320,overflowY:'auto',border:'1px solid var(--border)',borderRadius:8,padding:6}}>
              {displayVolumes.length === 0 ? (
                <p className="text-muted" style={{fontSize:13,padding:8}}>暂无分卷，将检查全部章节。</p>
              ) : (
                <>
                  <label style={{display:'flex',alignItems:'center',gap:8,padding:'6px 8px',cursor:'pointer',borderBottom:'1px dashed var(--border)',marginBottom:4,fontWeight:600,fontSize:13}}>
                    <input
                      type="checkbox"
                      checked={afSelectedVolIds.length === 0}
                      onChange={e => { if (e.target.checked) setAfSelectedVolIds([]); }}
                    />
                    📚 全部章节
                  </label>
                  {displayVolumes.map((vol, idx) => {
                    const id = vol.volume_id || vol.volume || `vol${idx}`;
                    const checked = afSelectedVolIds.includes(id);
                    return (
                      <label key={idx} style={{display:'flex',alignItems:'center',gap:8,padding:'6px 8px',cursor:'pointer',fontSize:13}}>
                        <input type="checkbox" checked={checked} onChange={() => toggleAfVol(id)} />
                        📖 {vol.volume || `第${idx + 1}卷`}{vol.chapter_count ? ` (${vol.chapter_count}章)` : ''}
                      </label>
                    );
                  })}
                </>
              )}
            </div>
            <div className="confirm-actions" style={{marginTop:12}}>
              <button className="btn-ghost-sm" onClick={() => setAfVolPickerOpen(false)}>取消</button>
              <button className="btn-primary-sm" onClick={confirmAfVolPicker} disabled={afChecking}>
                {afChecking ? '⏳ 检查中...' : '🚀 开始检查'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 按卷分批修正弹窗已移除（改为跳转 AI智驾）*/}

      {/* 修正入口卷选择弹窗：AI修正/修正正文 点击后先选要修正哪一卷 */}
      {fixVolPicker && (
        <div className="modal-overlay" onClick={() => setFixVolPicker(null)}>
          <div className="modal-content" style={{maxWidth:440}} onClick={e => e.stopPropagation()}>
            <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:8}}>
              <h3 style={{margin:0}}>{fixVolPicker.mode === 'setting' ? '🔧 AI修正' : '📝 修正正文'} · 选择卷</h3>
              <button className="btn-ghost-sm" onClick={() => setFixVolPicker(null)}>✕</button>
            </div>
            <p className="text-muted" style={{fontSize:12,marginBottom:10}}>
              选择要修正的卷范围；选「全部」则修正报告中所有违规项。
            </p>
            <div style={{maxHeight:300,overflowY:'auto',border:'1px solid var(--border)',borderRadius:8,padding:6}}>
              {volumes.length === 0 ? (
                <p className="text-muted" style={{fontSize:13,padding:8}}>本书暂无分卷，将修正全部违规项。</p>
              ) : (
                <>
                  <label style={{display:'flex',alignItems:'center',gap:8,padding:'6px 8px',cursor:'pointer',borderBottom:'1px dashed var(--border)',marginBottom:4,fontWeight:600,fontSize:13}}>
                    <input
                      type="radio"
                      name="fix-vol"
                      checked={fixSelectedVolId === ''}
                      onChange={() => setFixSelectedVolId('')}
                    />
                    📚 全部章节
                  </label>
                  {volumes.map((vol) => {
                    const chs = chaptersByVolume[vol.id] || [];
                    return (
                      <label key={vol.id} style={{display:'flex',alignItems:'center',gap:8,padding:'6px 8px',cursor:'pointer',fontSize:13}}>
                        <input
                          type="radio"
                          name="fix-vol"
                          checked={fixSelectedVolId === vol.id}
                          onChange={() => setFixSelectedVolId(vol.id)}
                        />
                        📖 {vol.title}（{chs.length}章）
                      </label>
                    );
                  })}
                </>
              )}
            </div>
            <div className="confirm-actions" style={{marginTop:12}}>
              <button className="btn-ghost-sm" onClick={() => setFixVolPicker(null)}>取消</button>
              <button
                className="btn-primary-sm"
                onClick={() => {
                  if (!fixVolPicker) return;
                  const { reportId, mode } = fixVolPicker;
                  const vid = fixSelectedVolId || undefined;
                  setFixVolPicker(null);
                  setFixSelectedVolId('');
                  if (mode === 'setting') jumpToChatForSettingFix(reportId, vid);
                  else jumpToChatForTextFix(reportId, vid);
                }}
              >
                🚀 开始修正
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
