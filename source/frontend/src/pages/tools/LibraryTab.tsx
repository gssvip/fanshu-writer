import { useState, useEffect, useContext } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../../api';
import { AuthContext } from '../../App';
import type { BookAnalysis, IdeaCard, IdeaGenerateResult, LibraryNote, Template } from '../../types';
import Icon from '../../components/Icon';

// 资料库：协同创作链路（榜单风向→拆书分析→拆书模板→小说创意→智驾生成）的资产沉淀层。
// 四个分区：拆书模板 / 拆书档案 / 创意工坊 / 素材笔记（规划中）。

const sectionStyle: React.CSSProperties = { marginTop: 28 };
const sectionHeadStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12, flexWrap: 'wrap' };
const cardStyle: React.CSSProperties = {
  border: '1px solid var(--border, rgba(120,120,130,0.25))', borderRadius: 10,
  padding: '14px 16px', marginBottom: 10, background: 'var(--bg-secondary, rgba(128,128,140,0.05))',
};
const badgeStyle: React.CSSProperties = {
  fontSize: 11, padding: '2px 8px', borderRadius: 999,
  background: 'var(--accent-weak, rgba(76,110,245,0.12))', color: 'var(--accent, #4c6ef5)', whiteSpace: 'nowrap',
};
const badgeMutedStyle: React.CSSProperties = {
  fontSize: 11, padding: '2px 8px', borderRadius: 999,
  background: 'rgba(128,128,140,0.15)', color: 'var(--text-secondary, #888)', whiteSpace: 'nowrap',
};
const btnStyle: React.CSSProperties = {
  padding: '6px 14px', borderRadius: 8, border: '1px solid var(--border, rgba(120,120,130,0.3))',
  background: 'transparent', color: 'inherit', cursor: 'pointer', fontSize: 13,
};
const btnPrimaryStyle: React.CSSProperties = { ...btnStyle, background: 'var(--accent, #4c6ef5)', borderColor: 'var(--accent, #4c6ef5)', color: '#fff' };
const iconBtnStyle: React.CSSProperties = {
  padding: '5px 8px', borderRadius: 8, border: '1px solid var(--border, rgba(120,120,130,0.3))',
  background: 'transparent', color: 'inherit', cursor: 'pointer',
  display: 'inline-flex', alignItems: 'center', justifyContent: 'center', lineHeight: 1,
};
const fieldStyle: React.CSSProperties = {
  width: '100%', padding: '8px 10px', borderRadius: 8, fontSize: 13, boxSizing: 'border-box',
  border: '1px solid var(--border, rgba(120,120,130,0.3))', background: 'transparent', color: 'inherit',
};
const modalMaskStyle: React.CSSProperties = {
  position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)', zIndex: 1000,
  display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 16,
};
const modalStyle: React.CSSProperties = {
  background: 'var(--bg-primary, #fff)', borderRadius: 12, padding: 18,
  width: '100%', maxWidth: 720, height: 'calc(100vh - 32px)', maxHeight: 'calc(100vh - 32px)',
  overflowY: 'auto', boxSizing: 'border-box', margin: '0 auto',
};

type LibType = 'template' | 'analysis' | 'idea' | 'note';

export default function LibraryTab({ selectedBookId }: { selectedBookId: string }) {
  const { requireAuth } = useContext(AuthContext);
  const navigate = useNavigate();

  const [error, setError] = useState('');
  const [templates, setTemplates] = useState<Template[]>([]);
  const [analyses, setAnalyses] = useState<BookAnalysis[]>([]);
  const [ideas, setIdeas] = useState<IdeaCard[]>([]);
  const [notes, setNotes] = useState<LibraryNote[]>([]);

  // 新建/编辑弹窗 + 条目选择器（编辑/加载多条时）
  const [editor, setEditor] = useState<{
    type: LibType; id: string | null;
    title: string; desc: string; source: string; content: string; logline: string; selling: string;
  } | null>(null);
  const [editorSaving, setEditorSaving] = useState(false);
  const [picker, setPicker] = useState<{ type: LibType } | null>(null);
  const [toast, setToast] = useState('');

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(''), 1800);
    return () => clearTimeout(t);
  }, [toast]);

  // 创意工坊生成器
  const [genOpen, setGenOpen] = useState(false);
  const [genTemplateId, setGenTemplateId] = useState('');
  const [genAnalysisId, setGenAnalysisId] = useState('');
  const [genBrief, setGenBrief] = useState('');
  const [generating, setGenerating] = useState(false);
  const [genError, setGenError] = useState('');
  const [genResult, setGenResult] = useState<IdeaGenerateResult | null>(null);
  const [busyKey, setBusyKey] = useState('');

  function reload() {
    Promise.all([api.listTemplates(), api.listBookAnalyses(), api.listIdeaCards(), api.listNotes()])
      .then(([t, a, i, n]) => {
        setTemplates(t);
        setAnalyses(a);
        setIdeas(i);
        setNotes(n);
        setError('');
      })
      .catch(e => setError(e.message || '加载失败'));
  }

  useEffect(() => { reload(); }, []);

  // 拆书模板：用户模板（含拆书生成的模板）
  const myTemplates = templates.filter(t => !t.is_builtin);

  function templateSourceLabel(t: Template): string {
    const src = t.source || {};
    if (src.type === 'teardown') {
      return src.source_label || (src.book_title ? `来自《${src.book_title}》拆书` : '拆书生成');
    }
    return '';
  }

  // ---- 拆书模板操作 ----
  async function createBookFromTemplate(t: Template, withAI: boolean) {
    const ok = await requireAuth();
    if (!ok) return;
    const title = prompt('给新书起个名字：', `${t.name.replace(/·?拆书模板$/, '')}·新作`);
    if (!title?.trim()) return;
    setBusyKey(t.id);
    try {
      const book = await api.createBook({ title: title.trim(), genre: t.genre, book_type: t.book_type, template_id: t.id, synopsis: '' });
      navigate(withAI ? `/write?book=${book.id}&ai=global` : `/write?book=${book.id}`);
    } catch (e: any) {
      alert('建书失败: ' + e.message);
      setBusyKey('');
    }
  }

  // ---- 拆书档案操作 ----
  async function handleAnalysisToTemplate(a: BookAnalysis) {
    const ok = await requireAuth();
    if (!ok) return;
    const name = prompt('拆书模板名称：', `${a.title || '拆书'}·拆书模板`);
    if (name === null) return;
    setBusyKey(a.id);
    try {
      const t = await api.analysisToTemplate(a.id, name.trim() || undefined);
      alert(`已生成拆书模板：${t.name}\n可在「拆书模板」分区查看，或在新建作品时选用。`);
      reload();
    } catch (e: any) {
      alert('生成失败: ' + e.message);
    }
    setBusyKey('');
  }

  async function handleDeleteAnalysis(a: BookAnalysis) {
    const ok = await requireAuth();
    if (!ok) return;
    if (!confirm(`确定删除拆书档案「${a.title}」？`)) return;
    try {
      await api.deleteBookAnalysis(a.id);
      reload();
    } catch (e: any) {
      alert('删除失败: ' + e.message);
    }
  }

  async function handleDeleteTemplate(t: Template) {
    const ok = await requireAuth();
    if (!ok) return;
    if (!confirm(`确定删除拆书模板「${t.name}」？`)) return;
    try {
      await api.deleteTemplate(t.id);
      reload();
    } catch (e: any) {
      alert('删除失败: ' + e.message);
    }
  }

  // ---- 创意工坊（P2：模板差异化再创作 + 防抄袭自检）----
  async function handleGenerateIdea() {
    if (!genTemplateId) { setGenError('请先选择一个拆书模板'); return; }
    const ok = await requireAuth();
    if (!ok) return;
    setGenerating(true);
    setGenError('');
    setGenResult(null);
    try {
      const res = await api.generateIdea({
        template_id: genTemplateId,
        analysis_id: genAnalysisId || undefined,
        brief: genBrief.trim() || undefined,
      });
      setGenResult(res);
    } catch (e: any) {
      setGenError(e.message || '生成失败');
    }
    setGenerating(false);
  }

  async function handleSaveIdea(goDrive: boolean) {
    if (!genResult) return;
    const ok = await requireAuth();
    if (!ok) return;
    setBusyKey('save-idea');
    try {
      const card = await api.saveIdeaCard({
        title: genResult.title,
        idea: genResult.idea,
        diff: genResult.diff,
        template_id: genTemplateId,
        analysis_id: genAnalysisId,
        source_label: genResult.source_label,
      });
      if (goDrive) {
        await sendIdeaToDrive(card);
      } else {
        alert('已保存到创意工坊');
        setGenResult(null);
        setGenOpen(false);
        reload();
      }
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setBusyKey('');
  }

  // ---- P3：创作包一键建书 → 智驾 ----
  async function sendIdeaToDrive(card: IdeaCard) {
    const ok = await requireAuth();
    if (!ok) return;
    setBusyKey(card.id);
    try {
      const res = await api.createBookFromIdea(card.id);
      // 创作包（模板骨架+差异清单）已注入作品 BookBible，智驾全程可感知创作来源
      navigate(`/write?book=${res.book_id}&ai=global`);
    } catch (e: any) {
      alert('建书失败: ' + e.message);
      setBusyKey('');
    }
  }

  async function handleDeleteIdea(card: IdeaCard) {
    const ok = await requireAuth();
    if (!ok) return;
    if (!confirm(`确定删除创意卡「${card.title}」？`)) return;
    try {
      await api.deleteIdeaCard(card.id);
      reload();
    } catch (e: any) {
      alert('删除失败: ' + e.message);
    }
  }

  // ---- 分区通用：新建 / 编辑 / 加载 ----
  function itemsOf(type: LibType): { id: string; label: string }[] {
    if (type === 'template') return myTemplates.map(t => ({ id: t.id, label: t.name }));
    if (type === 'analysis') return analyses.map(a => ({ id: a.id, label: a.title || '未命名档案' }));
    if (type === 'idea') return ideas.map(c => ({ id: c.id, label: c.title }));
    return notes.map(n => ({ id: n.id, label: n.title || '未命名笔记' }));
  }

  function analysisContent(a: BookAnalysis): string {
    const r: any = a.result || {};
    return r.raw || JSON.stringify(r, null, 2);
  }

  async function openEditor(type: LibType, id: string | null) {
    let init = { title: '', desc: '', source: '', content: '', logline: '', selling: '' };
    if (id && type === 'template') {
      const t = templates.find(x => x.id === id)!;
      init = { title: t.name, desc: t.description || '', content: (t.structure || []).map(s => s.title).join('\n'), source: '', logline: '', selling: '' };
    } else if (id && type === 'analysis') {
      const a = analyses.find(x => x.id === id)!;
      let content = '';
      try {
        const full = await api.getBookAnalysis(id);
        content = analysisContent(full);
      } catch { content = ''; }
      init = { title: a.title || '', source: a.source_label || '', content, desc: '', logline: '', selling: '' };
    } else if (id && type === 'idea') {
      const c = ideas.find(x => x.id === id)!;
      init = { title: c.title, logline: c.idea.logline || '', selling: (c.idea.selling_points || []).join('，'), desc: '', source: c.source_label || '', content: '' };
    } else if (id && type === 'note') {
      const n = notes.find(x => x.id === id)!;
      init = { title: n.title, content: n.content, desc: '', source: '', logline: '', selling: '' };
    }
    setEditor({ type, id, ...init });
  }

  function handleSectionAction(type: LibType, action: 'new' | 'load') {
    if (action === 'new') {
      if (type === 'idea') { setGenOpen(v => !v); setGenError(''); return; }
      openEditor(type, null);
      return;
    }
    const items = itemsOf(type);
    if (items.length === 0) return;
    if (items.length === 1) handleLoad(type, items[0].id);
    else setPicker({ type });
  }

  async function handleSaveEditor() {
    if (!editor) return;
    if (!editor.title.trim()) { alert('请填写标题'); return; }
    const ok = await requireAuth();
    if (!ok) return;
    setEditorSaving(true);
    try {
      if (editor.type === 'template') {
        const structure = editor.content.split('\n').map(s => s.trim()).filter(Boolean)
          .map((title, i) => ({ title, is_volume: false, parent_id: '', order_index: i }));
        if (editor.id) {
          await api.updateTemplate(editor.id, { name: editor.title.trim(), description: editor.desc, structure });
        } else {
          await api.createTemplate({ name: editor.title.trim(), description: editor.desc, genre: 'other', book_type: 'novel', structure });
        }
      } else if (editor.type === 'analysis') {
        if (editor.id) {
          await api.updateBookAnalysis(editor.id, { title: editor.title.trim(), source_label: editor.source, content: editor.content });
        } else {
          await api.saveBookAnalysis({ title: editor.title.trim(), source_label: editor.source, mode: 'manual', result: { raw: editor.content } });
        }
      } else if (editor.type === 'idea') {
        if (editor.id) {
          await api.updateIdeaCard(editor.id, {
            title: editor.title.trim(),
            idea: { logline: editor.logline, selling_points: editor.selling.split(/[，,、]/).map(s => s.trim()).filter(Boolean) },
          });
        }
      } else {
        if (editor.id) {
          await api.updateNote(editor.id, { title: editor.title.trim(), content: editor.content });
        } else {
          await api.createNote({ title: editor.title.trim(), content: editor.content });
        }
      }
      const isNew = !editor.id;
      setEditor(null);
      setToast(isNew ? '已创建' : '已保存');
      reload();
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setEditorSaving(false);
  }

  // ---- 加载：把条目内容送进所选作品的智驾会话 ----
  function loadContent(type: LibType, id: string, analysisResult?: any): { title: string; content: string } {
    if (type === 'template') {
      const t = templates.find(x => x.id === id)!;
      const beats = (t.structure || []).map((s, i) => `${i + 1}. ${s.title}`).join('\n');
      return {
        title: t.name,
        content: `【资料库·拆书模板】${t.name}\n${t.description ? t.description + '\n' : ''}节拍结构：\n${beats}`,
      };
    }
    if (type === 'analysis') {
      const a = analyses.find(x => x.id === id)!;
      const r = analysisResult || a.result || {};
      const body = r.raw || JSON.stringify(r, null, 2);
      return {
        title: a.title || '拆书档案',
        content: `【资料库·拆书档案】${a.title || '未命名档案'}\n${a.source_label ? '来源：' + a.source_label + '\n' : ''}${body}`,
      };
    }
    if (type === 'idea') {
      const c = ideas.find(x => x.id === id)!;
      return {
        title: c.title,
        content: `【资料库·创意卡】${c.title}\n${c.idea.logline || ''}\n卖点：${(c.idea.selling_points || []).join('、')}`,
      };
    }
    const n = notes.find(x => x.id === id)!;
    return { title: n.title, content: `【资料库·素材笔记】${n.title}\n${n.content}` };
  }

  async function handleLoad(type: LibType, id: string) {
    if (!selectedBookId) { alert('请先在上方「选择作品」选择要加载到的作品'); return; }
    const ok = await requireAuth();
    if (!ok) return;
    setBusyKey('load');
    try {
      let analysisResult: any;
      if (type === 'analysis') {
        const full = await api.getBookAnalysis(id);
        analysisResult = full.result;
      }
      const { title, content } = loadContent(type, id, analysisResult);
      const res = await api.loadToChat({ book_id: selectedBookId, title, content });
      navigate(`/write?book=${selectedBookId}&ai=global&session=${res.session_id}`);
    } catch (e: any) {
      alert('加载失败: ' + e.message);
      setBusyKey('');
    }
  }

  function sectionActions(type: LibType) {
    const hasItems = itemsOf(type).length > 0;
    return (
      <div style={{ display: 'flex', gap: 6, marginLeft: 'auto' }}>
        <button
          style={{ ...iconBtnStyle, background: 'var(--accent, #4c6ef5)', borderColor: 'var(--accent, #4c6ef5)', color: '#fff' }}
          title="新建"
          onClick={() => handleSectionAction(type, 'new')}
        >
          <Icon name="plus" size={15} />
        </button>
        {hasItems && (
          <button
            style={iconBtnStyle}
            title="加载到作品智驾"
            disabled={busyKey === 'load'}
            onClick={() => handleSectionAction(type, 'load')}
          >
            <Icon name="upload-cloud" size={15} />
          </button>
        )}
      </div>
    );
  }

  function tplName(id: string): string {
    return templates.find(t => t.id === id)?.name || '自定义模板';
  }

  function ideaRiskBadge(risk: string) {
    const map: Record<string, { label: string; style: React.CSSProperties }> = {
      low: { label: '抄袭风险低', style: { ...badgeStyle, background: 'rgba(46,184,92,0.14)', color: '#2eb85c' } },
      medium: { label: '抄袭风险中', style: { ...badgeStyle, background: 'rgba(250,176,5,0.16)', color: '#b8860b' } },
      high: { label: '抄袭风险高', style: { ...badgeStyle, background: 'rgba(232,70,58,0.14)', color: '#e8463a' } },
    };
    const m = map[risk] || { label: `风险:${risk || '未知'}`, style: badgeMutedStyle };
    return <span style={m.style}>{m.label}</span>;
  }

  const genTpl = templates.find(t => t.id === genTemplateId);

  return (
    <div className="tool-panel">
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <h2 style={{ margin: 0, fontSize: 18, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Icon name="book" size={18} /> 资料库
        </h2>
      </div>
      {error && <div className="error-msg" style={{ marginTop: 10 }}>{error}</div>}

      {/* ===== 拆书模板 ===== */}
      <section style={sectionStyle}>
        <div style={sectionHeadStyle}>
          <strong style={{ fontSize: 15 }}>拆书模板</strong>
          <span style={badgeMutedStyle}>{myTemplates.length} 个</span>
          {sectionActions('template')}
        </div>
        {myTemplates.map(t => {
          const srcLabel = templateSourceLabel(t);
          return (
            <div key={t.id} style={cardStyle} title="双击编辑" onDoubleClick={() => openEditor('template', t.id)}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <strong style={{ fontSize: 14 }}>{t.name}</strong>
                {srcLabel && <span style={badgeStyle}>{srcLabel}</span>}
                <span style={badgeMutedStyle}>{(t.structure || []).length} 个节拍节点</span>
                <button
                  style={{ ...iconBtnStyle, marginLeft: 'auto', padding: '3px 6px' }}
                  title="删除"
                  onClick={e => { e.stopPropagation(); handleDeleteTemplate(t); }}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
              {t.description && (
                <div style={{ fontSize: 12, color: 'var(--text-secondary, #888)', margin: '6px 0 10px' }}>{t.description}</div>
              )}
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }} onDoubleClick={e => e.stopPropagation()}>
                <button style={btnPrimaryStyle} disabled={busyKey === t.id} onClick={() => createBookFromTemplate(t, true)}>
                  建书·进智驾
                </button>
                <button style={btnStyle} disabled={busyKey === t.id} onClick={() => createBookFromTemplate(t, false)}>
                  建书开写
                </button>
              </div>
            </div>
          );
        })}
      </section>

      {/* ===== 拆书档案 ===== */}
      <section style={sectionStyle}>
        <div style={sectionHeadStyle}>
          <strong style={{ fontSize: 15 }}>拆书档案</strong>
          <span style={badgeMutedStyle}>{analyses.length} 份</span>
          {sectionActions('analysis')}
        </div>
        {analyses.map(a => (
          <div key={a.id} style={cardStyle} title="双击编辑" onDoubleClick={() => openEditor('analysis', a.id)}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <strong style={{ fontSize: 14 }}>{a.title || '未命名档案'}</strong>
              {a.source_label && <span style={badgeStyle}>{a.source_label}</span>}
              <span style={badgeMutedStyle}>{a.mode === 'competitor' ? '对标拆书' : a.mode === 'position' ? '轻拆定位' : '深拆'}</span>
              <span style={badgeMutedStyle}>{(a.created_at || '').slice(0, 10)}</span>
              <button
                style={{ ...iconBtnStyle, marginLeft: 'auto', padding: '3px 6px' }}
                title="删除"
                onClick={e => { e.stopPropagation(); handleDeleteAnalysis(a); }}
              >
                <Icon name="trash" size={14} />
              </button>
            </div>
            {a.summary?.logline && (
              <div style={{ fontSize: 12, color: 'var(--text-secondary, #888)', margin: '6px 0 10px' }}>
                {a.summary.logline}
              </div>
            )}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }} onDoubleClick={e => e.stopPropagation()}>
              <button style={btnPrimaryStyle} disabled={busyKey === a.id} onClick={() => handleAnalysisToTemplate(a)}>
                生成拆书模板
              </button>
            </div>
          </div>
        ))}
      </section>

      {/* ===== 创意工坊 ===== */}
      <section style={sectionStyle}>
        <div style={sectionHeadStyle}>
          <strong style={{ fontSize: 15 }}>创意工坊</strong>
          <span style={badgeMutedStyle}>{ideas.length} 张</span>
          {sectionActions('idea')}
        </div>

        {genOpen && (
          <div style={{ ...cardStyle, borderColor: 'var(--accent, #4c6ef5)' }}>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 10 }}>
              <label style={{ fontSize: 12 }}>
                拆书模板（必选）<span style={{ color: '#e8463a' }}>*</span>
                <select style={{ ...fieldStyle, marginTop: 4 }} value={genTemplateId} onChange={e => setGenTemplateId(e.target.value)}>
                  <option value="">选择模板…</option>
                  {myTemplates.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
                </select>
              </label>
              <label style={{ fontSize: 12 }}>
                对标拆书档案（可选）
                <select style={{ ...fieldStyle, marginTop: 4 }} value={genAnalysisId} onChange={e => setGenAnalysisId(e.target.value)}>
                  <option value="">不关联</option>
                  {analyses.map(a => <option key={a.id} value={a.id}>{a.title || '未命名档案'}</option>)}
                </select>
              </label>
            </div>
            <label style={{ fontSize: 12, display: 'block', marginTop: 10 }}>
              你的创作方向（可选）
              <textarea
                style={{ ...fieldStyle, marginTop: 4, minHeight: 56, resize: 'vertical' }}
                placeholder="如：想写都市高武，主角是重生的退役兵王…"
                value={genBrief}
                onChange={e => setGenBrief(e.target.value)}
              />
            </label>
            {genError && <div className="error-msg" style={{ marginTop: 8 }}>{genError}</div>}
            <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
              <button style={btnPrimaryStyle} disabled={generating} onClick={handleGenerateIdea}>
                {generating ? '生成中…（约1分钟）' : '生成创意（含差异清单）'}
              </button>
              {genTpl && <span style={{ ...badgeMutedStyle, alignSelf: 'center' }}>结构骨架来自「{genTpl.name}」</span>}
            </div>

            {genResult && (
              <div style={{ marginTop: 14, borderTop: '1px solid var(--border, rgba(120,120,130,0.2))', paddingTop: 12 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <strong style={{ fontSize: 15 }}>{genResult.title}</strong>
                  {ideaRiskBadge(genResult.plagiarism_risk)}
                </div>
                {genResult.idea.logline && (
                  <div style={{ fontSize: 13, margin: '8px 0' }}>{genResult.idea.logline}</div>
                )}
                {genResult.idea.selling_points?.length ? (
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
                    {genResult.idea.selling_points.map((s, i) => <span key={i} style={badgeStyle}>{s}</span>)}
                  </div>
                ) : null}
                {(['worldview', 'conflict', 'opening_hook'] as const).map(k => (
                  genResult.idea[k] ? (
                    <div key={k} style={{ fontSize: 12, color: 'var(--text-secondary, #888)', marginBottom: 4 }}>
                      <b style={{ color: 'inherit' }}>{k === 'worldview' ? '世界观/金手指' : k === 'conflict' ? '核心冲突' : '开篇钩子'}：</b>
                      {genResult.idea[k]}
                    </div>
                  ) : null
                ))}

                {/* 防抄袭自检：差异清单 */}
                {genResult.diff.length > 0 && (
                  <div style={{ marginTop: 10 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>防抄袭自检 · 与对标书的差异清单</div>
                    {genResult.diff.map((d, i) => (
                      <div key={i} style={{ display: 'flex', gap: 8, fontSize: 12, marginBottom: 4, alignItems: 'baseline' }}>
                        <span style={{ ...badgeMutedStyle, flexShrink: 0 }}>{d.aspect}</span>
                        <span>{d.ours}</span>
                        {d.passed === false && <span style={{ color: '#e8463a', flexShrink: 0 }}>未形成差异</span>}
                      </div>
                    ))}
                  </div>
                )}
                {genResult.risk_note && (
                  <div style={{ fontSize: 12, color: '#b8860b', marginTop: 6 }}>{genResult.risk_note}</div>
                )}
                <div style={{ display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
                  <button style={btnPrimaryStyle} disabled={busyKey === 'save-idea'} onClick={() => handleSaveIdea(true)}>
                    一键建书·送智驾
                  </button>
                  <button style={btnStyle} disabled={busyKey === 'save-idea'} onClick={() => handleSaveIdea(false)}>
                    保存创意卡
                  </button>
                  <button style={btnStyle} onClick={() => setGenResult(null)}>放弃</button>
                </div>
              </div>
            )}
          </div>
        )}

        {ideas.map(card => (
          <div key={card.id} style={cardStyle} title="双击编辑" onDoubleClick={() => openEditor('idea', card.id)}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <strong style={{ fontSize: 14 }}>{card.title}</strong>
              {card.source_label && <span style={badgeStyle}>{card.source_label}</span>}
              <span style={badgeMutedStyle}>骨架：{tplName(card.template_id)}</span>
              <span style={badgeMutedStyle}>{(card.created_at || '').slice(0, 10)}</span>
              {card.book_id
                ? <span style={{ ...badgeStyle, background: 'rgba(46,184,92,0.14)', color: '#2eb85c' }}>已建书</span>
                : <span style={badgeMutedStyle}>未建书</span>}
              <button
                style={{ ...iconBtnStyle, marginLeft: 'auto', padding: '3px 6px' }}
                title="删除"
                onClick={e => { e.stopPropagation(); handleDeleteIdea(card); }}
              >
                <Icon name="trash" size={14} />
              </button>
            </div>
            {card.idea.logline && (
              <div style={{ fontSize: 12, color: 'var(--text-secondary, #888)', margin: '6px 0 10px' }}>{card.idea.logline}</div>
            )}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }} onDoubleClick={e => e.stopPropagation()}>
              {card.book_id ? (
                <button style={btnPrimaryStyle} onClick={() => navigate(`/write?book=${card.book_id}&ai=global`)}>
                  进入作品·智驾
                </button>
              ) : (
                <button style={btnPrimaryStyle} disabled={busyKey === card.id} onClick={() => sendIdeaToDrive(card)}>
                  一键建书·送智驾
                </button>
              )}
            </div>
          </div>
        ))}
      </section>

      {/* ===== 素材笔记 ===== */}
      <section style={sectionStyle}>
        <div style={sectionHeadStyle}>
          <strong style={{ fontSize: 15 }}>素材笔记</strong>
          <span style={badgeMutedStyle}>{notes.length} 条</span>
          {sectionActions('note')}
        </div>
        {notes.map(n => (
          <div key={n.id} style={cardStyle} title="双击编辑" onDoubleClick={() => openEditor('note', n.id)}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <strong style={{ fontSize: 14 }}>{n.title || '未命名笔记'}</strong>
              <span style={badgeMutedStyle}>{(n.updated_at || '').slice(0, 10)}</span>
              <button
                style={{ ...iconBtnStyle, marginLeft: 'auto', padding: '3px 6px' }}
                title="删除"
                onClick={e => {
                  e.stopPropagation();
                  if (!confirm(`确定删除笔记「${n.title}」？`)) return;
                  api.deleteNote(n.id).then(reload).catch((e: any) => alert('删除失败: ' + e.message));
                }}
              >
                <Icon name="trash" size={14} />
              </button>
            </div>
            {n.content && (
              <div style={{ fontSize: 12, color: 'var(--text-secondary, #888)', margin: '6px 0 0', whiteSpace: 'pre-wrap' }}>
                {n.content.length > 120 ? n.content.slice(0, 120) + '…' : n.content}
              </div>
            )}
          </div>
        ))}
      </section>

      {/* ===== 条目选择器（加载多条时）===== */}
      {picker && (
        <div style={modalMaskStyle} onClick={() => setPicker(null)}>
          <div style={modalStyle} onClick={e => e.stopPropagation()}>
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 10 }}>
              选择要加载到作品智驾的条目
            </div>
            <div style={{ maxHeight: 320, overflowY: 'auto' }}>
              {itemsOf(picker.type).map(it => (
                <button
                  key={it.id}
                  style={{ ...btnStyle, display: 'block', width: '100%', textAlign: 'left', marginBottom: 6 }}
                  onClick={() => {
                    const { type } = picker;
                    setPicker(null);
                    handleLoad(type, it.id);
                  }}
                >
                  {it.label}
                </button>
              ))}
            </div>
            <button style={{ ...btnStyle, marginTop: 8 }} onClick={() => setPicker(null)}>取消</button>
          </div>
        </div>
      )}

      {/* ===== 新建 / 编辑弹窗 ===== */}
      {editor && (
        <div style={modalMaskStyle} onClick={() => setEditor(null)}>
          <div style={modalStyle} onClick={e => e.stopPropagation()}>
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 10 }}>
              {editor.id ? '编辑' : '新建'}{{ template: '拆书模板', analysis: '拆书档案', idea: '创意卡', note: '素材笔记' }[editor.type]}
            </div>
            <label style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
              标题
              <input style={{ ...fieldStyle, marginTop: 4 }} value={editor.title} onChange={e => setEditor({ ...editor, title: e.target.value })} />
            </label>
            {editor.type === 'template' && (
              <>
                <label style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                  简介
                  <input style={{ ...fieldStyle, marginTop: 4 }} value={editor.desc} onChange={e => setEditor({ ...editor, desc: e.target.value })} />
                </label>
                <label style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                  节拍结构（每行一个节拍）
                  <textarea style={{ ...fieldStyle, marginTop: 4, minHeight: 140, resize: 'vertical' }} value={editor.content} onChange={e => setEditor({ ...editor, content: e.target.value })} />
                </label>
              </>
            )}
            {editor.type === 'analysis' && (
              <>
                <label style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                  来源标签
                  <input style={{ ...fieldStyle, marginTop: 4 }} value={editor.source} onChange={e => setEditor({ ...editor, source: e.target.value })} />
                </label>
                <label style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                  拆书内容
                  <textarea style={{ ...fieldStyle, marginTop: 4, minHeight: 160, resize: 'vertical' }} value={editor.content} onChange={e => setEditor({ ...editor, content: e.target.value })} />
                </label>
              </>
            )}
            {editor.type === 'idea' && (
              <>
                <label style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                  一句话简介
                  <textarea style={{ ...fieldStyle, marginTop: 4, minHeight: 56, resize: 'vertical' }} value={editor.logline} onChange={e => setEditor({ ...editor, logline: e.target.value })} />
                </label>
                <label style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                  卖点（用顿号/逗号分隔）
                  <input style={{ ...fieldStyle, marginTop: 4 }} value={editor.selling} onChange={e => setEditor({ ...editor, selling: e.target.value })} />
                </label>
              </>
            )}
            {editor.type === 'note' && (
              <label style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
                内容
                <textarea style={{ ...fieldStyle, marginTop: 4, minHeight: 160, resize: 'vertical' }} value={editor.content} onChange={e => setEditor({ ...editor, content: e.target.value })} />
              </label>
            )}
            <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
              <button style={btnPrimaryStyle} disabled={editorSaving} onClick={handleSaveEditor}>
                {editorSaving ? '保存中…' : '保存'}
              </button>
              <button style={btnStyle} onClick={() => setEditor(null)}>取消</button>
            </div>
          </div>
        </div>
      )}

      {/* ===== 轻提示（保存/创建成功）===== */}
      {toast && (
        <div style={{
          position: 'fixed', bottom: 90, left: '50%', transform: 'translateX(-50%)',
          background: 'rgba(0,0,0,0.78)', color: '#fff', padding: '8px 20px',
          borderRadius: 8, fontSize: 13, zIndex: 2000, whiteSpace: 'nowrap',
        }}>
          {toast}
        </div>
      )}
    </div>
  );
}
