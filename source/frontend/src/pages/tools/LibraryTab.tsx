import { useState, useEffect, useContext } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../../api';
import { AuthContext } from '../../App';
import type { BookAnalysis, IdeaCard, IdeaGenerateResult, Template } from '../../types';
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
  background: 'rgba(76,110,245,0.12)', color: '#4c6ef5', whiteSpace: 'nowrap',
};
const badgeMutedStyle: React.CSSProperties = {
  fontSize: 11, padding: '2px 8px', borderRadius: 999,
  background: 'rgba(128,128,140,0.15)', color: 'var(--text-secondary, #888)', whiteSpace: 'nowrap',
};
const btnStyle: React.CSSProperties = {
  padding: '6px 14px', borderRadius: 8, border: '1px solid var(--border, rgba(120,120,130,0.3))',
  background: 'transparent', color: 'inherit', cursor: 'pointer', fontSize: 13,
};
const btnPrimaryStyle: React.CSSProperties = { ...btnStyle, background: '#4c6ef5', borderColor: '#4c6ef5', color: '#fff' };
const fieldStyle: React.CSSProperties = {
  width: '100%', padding: '8px 10px', borderRadius: 8, fontSize: 13, boxSizing: 'border-box',
  border: '1px solid var(--border, rgba(120,120,130,0.3))', background: 'transparent', color: 'inherit',
};

export default function LibraryTab() {
  const { requireAuth } = useContext(AuthContext);
  const navigate = useNavigate();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [templates, setTemplates] = useState<Template[]>([]);
  const [analyses, setAnalyses] = useState<BookAnalysis[]>([]);
  const [ideas, setIdeas] = useState<IdeaCard[]>([]);

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
    setLoading(true);
    Promise.all([api.listTemplates(), api.listBookAnalyses(), api.listIdeaCards()])
      .then(([t, a, i]) => {
        setTemplates(t);
        setAnalyses(a);
        setIdeas(i);
        setError('');
      })
      .catch(e => setError(e.message || '加载失败'))
      .finally(() => setLoading(false));
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
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <h2 style={{ margin: 0, fontSize: 18, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Icon name="book" size={18} /> 资料库
        </h2>
        <span style={{ fontSize: 12, color: 'var(--text-secondary, #888)' }}>
          协同创作的资产沉淀层：拆书模板 · 拆书档案 · 创意工坊
        </span>
      </div>
      {error && <div className="error-msg" style={{ marginTop: 10 }}>{error}</div>}
      {loading && <div style={{ marginTop: 16, fontSize: 13, color: 'var(--text-secondary, #888)' }}>加载中…</div>}

      {/* ===== 拆书模板 ===== */}
      <section style={sectionStyle}>
        <div style={sectionHeadStyle}>
          <strong style={{ fontSize: 15 }}>拆书模板</strong>
          <span style={badgeMutedStyle}>{myTemplates.length} 个</span>
          <span style={{ fontSize: 12, color: 'var(--text-secondary, #888)' }}>拆书套路生成的结构骨架，带来源溯源</span>
        </div>
        {myTemplates.length === 0 && !loading && (
          <div style={cardStyle}>
            <div style={{ fontSize: 13, color: 'var(--text-secondary, #888)' }}>
              暂无拆书模板。到「拆书分析」拆解一本对标书，一键生成结构骨架模板。
            </div>
          </div>
        )}
        {myTemplates.map(t => {
          const srcLabel = templateSourceLabel(t);
          return (
            <div key={t.id} style={cardStyle}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <strong style={{ fontSize: 14 }}>{t.name}</strong>
                {srcLabel && <span style={badgeStyle}>{srcLabel}</span>}
                <span style={badgeMutedStyle}>{(t.structure || []).length} 个节拍节点</span>
              </div>
              {t.description && (
                <div style={{ fontSize: 12, color: 'var(--text-secondary, #888)', margin: '6px 0 10px' }}>{t.description}</div>
              )}
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
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
          <span style={{ fontSize: 12, color: 'var(--text-secondary, #888)' }}>对标书拆解报告，套路、节拍、爽点全留存</span>
        </div>
        {analyses.length === 0 && !loading && (
          <div style={cardStyle}>
            <div style={{ fontSize: 13, color: 'var(--text-secondary, #888)' }}>
              暂无拆书档案。在「拆书分析」点击「存入资料库」即可归档。
            </div>
          </div>
        )}
        {analyses.map(a => (
          <div key={a.id} style={cardStyle}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <strong style={{ fontSize: 14 }}>{a.title || '未命名档案'}</strong>
              {a.source_label && <span style={badgeStyle}>{a.source_label}</span>}
              <span style={badgeMutedStyle}>{a.mode === 'competitor' ? '对标拆书' : a.mode === 'position' ? '轻拆定位' : '深拆'}</span>
              <span style={badgeMutedStyle}>{(a.created_at || '').slice(0, 10)}</span>
            </div>
            {a.summary?.logline && (
              <div style={{ fontSize: 12, color: 'var(--text-secondary, #888)', margin: '6px 0 10px' }}>
                {a.summary.logline}
              </div>
            )}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button style={btnPrimaryStyle} disabled={busyKey === a.id} onClick={() => handleAnalysisToTemplate(a)}>
                生成拆书模板
              </button>
              <button style={btnStyle} onClick={() => handleDeleteAnalysis(a)}>删除</button>
            </div>
          </div>
        ))}
      </section>

      {/* ===== 创意工坊 ===== */}
      <section style={sectionStyle}>
        <div style={sectionHeadStyle}>
          <strong style={{ fontSize: 15 }}>创意工坊</strong>
          <span style={badgeMutedStyle}>{ideas.length} 张</span>
          <span style={{ fontSize: 12, color: 'var(--text-secondary, #888)' }}>基于拆书模板的差异化创意，附防抄袭差异清单</span>
          <button style={{ ...btnPrimaryStyle, marginLeft: 'auto' }} onClick={() => { setGenOpen(v => !v); setGenError(''); }}>
            {genOpen ? '收起' : '新建创意'}
          </button>
        </div>

        {genOpen && (
          <div style={{ ...cardStyle, borderColor: '#4c6ef5' }}>
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
          <div key={card.id} style={cardStyle}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <strong style={{ fontSize: 14 }}>{card.title}</strong>
              {card.source_label && <span style={badgeStyle}>{card.source_label}</span>}
              <span style={badgeMutedStyle}>骨架：{tplName(card.template_id)}</span>
              <span style={badgeMutedStyle}>{(card.created_at || '').slice(0, 10)}</span>
              {card.book_id
                ? <span style={{ ...badgeStyle, background: 'rgba(46,184,92,0.14)', color: '#2eb85c' }}>已建书</span>
                : <span style={badgeMutedStyle}>未建书</span>}
            </div>
            {card.idea.logline && (
              <div style={{ fontSize: 12, color: 'var(--text-secondary, #888)', margin: '6px 0 10px' }}>{card.idea.logline}</div>
            )}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {card.book_id ? (
                <button style={btnPrimaryStyle} onClick={() => navigate(`/write?book=${card.book_id}&ai=global`)}>
                  进入作品·智驾
                </button>
              ) : (
                <button style={btnPrimaryStyle} disabled={busyKey === card.id} onClick={() => sendIdeaToDrive(card)}>
                  一键建书·送智驾
                </button>
              )}
              <button style={btnStyle} onClick={() => handleDeleteIdea(card)}>删除</button>
            </div>
          </div>
        ))}
      </section>

      {/* ===== 素材笔记（规划中）===== */}
      <section style={sectionStyle}>
        <div style={sectionHeadStyle}>
          <strong style={{ fontSize: 15 }}>素材笔记</strong>
          <span style={badgeMutedStyle}>规划中</span>
        </div>
        <div style={cardStyle}>
          <div style={{ fontSize: 13, color: 'var(--text-secondary, #888)' }}>
            随手笔记、资料卡、金句、人物原型将在这里沉淀，敬请期待。
          </div>
        </div>
      </section>
    </div>
  );
}
