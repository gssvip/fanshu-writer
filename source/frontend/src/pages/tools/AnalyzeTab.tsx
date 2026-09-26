import { useState, useContext, useRef } from 'react';
import { api } from '../../api';
import { AuthContext } from '../../App';
import type { Book, AnalysisResult } from '../../types';

const SYNC_FIELD_LABELS: Record<string, string> = {
  style_guide: '风格指南', plot_design: '大纲设计', character_profiles: '人物档案',
  foreshadowing: '伏笔', worldbuilding: '世界观', key_rules: '设定', timeline: '剧情',
};

export default function AnalyzeTab({ books }: { books: Book[] }) {
  const { requireAuth } = useContext(AuthContext);

  const [analyzeInput, setAnalyzeInput] = useState('');
  const [analyzeResult, setAnalyzeResult] = useState<AnalysisResult | null>(null);
  const [analyzeLoading, setAnalyzeLoading] = useState(false);
  // 竞品拆书模式：normal=普通拆书 / competitor=竞品对标拆解
  const [analyzeMode, setAnalyzeMode] = useState<'normal' | 'competitor'>('normal');
  const [uploadFilename, setUploadFilename] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [showSyncModal, setShowSyncModal] = useState(false);
  const [syncBookId, setSyncBookId] = useState('');
  const [syncMode, setSyncMode] = useState('imitate');
  const [syncing, setSyncing] = useState(false);

  async function handleUploadFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploadFilename(file.name);
    try {
      const r = await api.uploadAnalyze(file);
      setAnalyzeInput(r.content);
      alert(`导入成功：${r.filename}，${r.length} 字符`);
    } catch (err: any) { alert('导入失败: ' + err.message); }
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  async function handleAnalyze() {
    if (!analyzeInput.trim()) return;
    const ok = await requireAuth();
    if (!ok) return;
    setAnalyzeLoading(true);
    setAnalyzeResult(null);
    try { const r = await api.analyzeBook(analyzeInput, analyzeMode === 'competitor' ? 'competitor' : undefined); setAnalyzeResult(r); }
    catch (e: any) { alert('分析失败: ' + e.message); }
    setAnalyzeLoading(false);
  }

  function handleExportAnalysis() {
    if (!analyzeResult) return;
    api.exportAnalysis(analyzeResult);
  }

  async function handleSyncAnalysis() {
    if (!syncBookId || !analyzeResult) return;
    const ok = await requireAuth();
    if (!ok) return;
    setSyncing(true);
    try {
      const result = await api.syncAnalysisToBook(syncBookId, analyzeResult, syncMode);
      const modeLabel = syncMode === 'imitate' ? '仿写' : syncMode === 'fanfic' ? '同人文' : '参考';
      alert(`已同步到作品资料（${modeLabel}模式）！已更新 ${result.updated_fields.length} 个维度：${result.updated_fields.map((f: string) => SYNC_FIELD_LABELS[f] || f).join('、')}`);
      setShowSyncModal(false);
    } catch (e: any) {
      alert('同步失败：' + (e.message || '请检查AI配置'));
    }
    setSyncing(false);
  }

  return (
    <>
      <div className="tool-panel">
        <h3>📊 AI 拆书分析</h3>
        <div className="form-row" style={{alignItems:'center',gap:8,marginBottom:10}}>
          <button
            className={analyzeMode === 'normal' ? 'btn-primary' : 'btn-secondary'}
            style={{fontSize:12,padding:'6px 14px'}}
            onClick={() => setAnalyzeMode('normal')}
          >📖 普通拆书</button>
          <button
            className={analyzeMode === 'competitor' ? 'btn-primary' : 'btn-secondary'}
            style={{fontSize:12,padding:'6px 14px'}}
            onClick={() => setAnalyzeMode('competitor')}
            title="站在竞品对标角度，输出市场定位、核心优势、差异弱点与可复刻方案"
          >⚔️ 竞品拆书</button>
          {analyzeMode === 'competitor' && (
            <span className="text-muted" style={{fontSize:11}}>分析竞品爆款，输出对标定位 · 核心优势 · 差异化机会 · 复刻方案</span>
          )}
        </div>
        <div className="form-row" style={{marginBottom:10}}>
          <input ref={fileInputRef} type="file" accept=".txt,.md,.docx,.zip,.json" onChange={handleUploadFile} style={{display:'none'}} id="analyze-file-input" />
          <label htmlFor="analyze-file-input" className="btn-secondary" style={{cursor:'pointer',padding:'8px 16px',borderRadius:'var(--radius-sm)',display:'inline-block'}}>
            📁 导入文件
          </label>
          {uploadFilename && <span className="text-muted" style={{alignSelf:'center'}}>已导入: {uploadFilename}</span>}
          <span className="text-muted" style={{alignSelf:'center',fontSize:11}}>支持 txt/md/docx/zip</span>
        </div>
        <textarea className="input" rows={8} value={analyzeInput} onChange={e => setAnalyzeInput(e.target.value)}
          placeholder="粘贴要分析的作品片段（建议2000字以上），或导入文件自动填充..." />
        <div className="form-row">
          <button className="btn-primary" onClick={handleAnalyze} disabled={!analyzeInput.trim() || analyzeLoading}>
            {analyzeLoading ? '分析中...' : '开始分析'}
          </button>
          {analyzeResult && (
            <>
              <button className="btn-secondary" onClick={handleExportAnalysis}>📥 导出结果</button>
              <button className="btn-secondary" onClick={() => setShowSyncModal(true)} style={{borderColor:'var(--accent)',color:'var(--accent)'}}>
                📋 同步到作品
              </button>
            </>
          )}
        </div>
        {analyzeResult && (
          <div className="analyze-result">
            <div className="analyze-tags">
              {analyzeResult.genre_tags?.map((t, i) => <span key={i} className="tag">{t}</span>)}
              <span className="tag platform">{analyzeResult.target_platform}</span>
            </div>
            <div className="analyze-grid">
              <div className="analyze-item"><h4>文风特点</h4><p>{analyzeResult.style_analysis}</p></div>
              <div className="analyze-item"><h4>结构特点</h4><p>{analyzeResult.structure_analysis}</p></div>
              <div className="analyze-item"><h4>节奏特点</h4><p>{analyzeResult.rhythm_analysis}</p></div>
              <div className="analyze-item"><h4>人设特点</h4><p>{analyzeResult.character_design_analysis}</p></div>
            </div>
            <div className="review-section"><h4>钩子技巧</h4><ul>{analyzeResult.hook_techniques?.map((h, i) => <li key={i}>{h}</li>)}</ul></div>
            <div className="review-section"><h4>可学习的方法</h4><ul>{analyzeResult.learnable_points?.map((p, i) => <li key={i}>{p}</li>)}</ul></div>
            {analyzeMode === 'competitor' && analyzeResult.market_position && (
              <div className="review-section competitor-pos"><h4>🎯 市场定位</h4><p>{analyzeResult.market_position}</p></div>
            )}
            {analyzeMode === 'competitor' && (analyzeResult.strengths?.length || analyzeResult.weaknesses?.length) && (
              <div className="analyze-grid">
                {analyzeResult.strengths && analyzeResult.strengths.length > 0 && (
                  <div className="analyze-item"><h4>💪 核心优势</h4><ul className="compact-list">{(analyzeResult.strengths as string[]).map((s, i) => <li key={i}>{s}</li>)}</ul></div>
                )}
                {analyzeResult.weaknesses && analyzeResult.weaknesses.length > 0 && (
                  <div className="analyze-item"><h4>🕳 差异化机会（弱点切入）</h4><ul className="compact-list">{(analyzeResult.weaknesses as string[]).map((w, i) => <li key={i}>{w}</li>)}</ul></div>
                )}
              </div>
            )}
            {analyzeMode === 'competitor' && analyzeResult.copy_plan && (
              <div className="review-section"><h4>📝 复刻方案（借鉴爆点 + 规避同质化）</h4><p>{analyzeResult.copy_plan}</p></div>
            )}
            {analyzeResult.golden_lines?.length > 0 && (
              <div className="review-section"><h4>金句摘录</h4><ul>{analyzeResult.golden_lines.map((l, i) => <li key={i} className="golden-line">{l}</li>)}</ul></div>
            )}
          </div>
        )}
      </div>

      {showSyncModal && (
        <div className="modal-overlay" onClick={() => setShowSyncModal(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h2>📋 同步分析结果到作品</h2>
            <p className="text-muted" style={{marginBottom:12}}>将拆书分析的文风、结构、人设等方法论同步到作品资料，用于仿写或同人文创作。</p>

            <div className="form-field">
              <label>选择目标作品</label>
              <select className="input" value={syncBookId} onChange={e => setSyncBookId(e.target.value)}>
                <option value="">— 选择作品 —</option>
                {books.map(b => <option key={b.id} value={b.id}>{b.title} ({b.word_count}字)</option>)}
              </select>
            </div>

            <div className="form-field">
              <label>同步模式</label>
              <div className="sync-mode-options">
                <label className={`sync-mode-card ${syncMode === 'imitate' ? 'active' : ''}`}>
                  <input type="radio" name="syncMode" value="imitate" checked={syncMode === 'imitate'} onChange={e => setSyncMode(e.target.value)} />
                  <div className="sync-mode-info">
                    <div className="sync-mode-name">✍️ 仿写模式</div>
                    <div className="sync-mode-desc">提取原文风格、结构、节奏，作为创作参考填充到作品设定中</div>
                  </div>
                </label>
                <label className={`sync-mode-card ${syncMode === 'fanfic' ? 'active' : ''}`}>
                  <input type="radio" name="syncMode" value="fanfic" checked={syncMode === 'fanfic'} onChange={e => setSyncMode(e.target.value)} />
                  <div className="sync-mode-info">
                    <div className="sync-mode-name">📚 同人文模式</div>
                    <div className="sync-mode-desc">提取原文世界观、人物设定，作为同人文创作的基础资料</div>
                  </div>
                </label>
                <label className={`sync-mode-card ${syncMode === 'reference' ? 'active' : ''}`}>
                  <input type="radio" name="syncMode" value="reference" checked={syncMode === 'reference'} onChange={e => setSyncMode(e.target.value)} />
                  <div className="sync-mode-info">
                    <div className="sync-mode-name">💡 参考模式</div>
                    <div className="sync-mode-desc">仅提取可学习方法论，追加到作品风格指南中</div>
                  </div>
                </label>
              </div>
            </div>

            <div className="sync-preview" style={{background:'var(--bg-tertiary)',borderRadius:'var(--radius-sm)',padding:12,marginBottom:12,fontSize:12,color:'var(--text-muted)'}}>
              <b>将同步以下内容：</b>
              <ul style={{margin:'6px 0 0',paddingLeft:18}}>
                <li>文风特点 → 风格指南</li>
                <li>结构/节奏分析 → 大纲设计</li>
                <li>人设分析 → 人物档案{syncMode === 'fanfic' ? '（完整提取）' : '（参考）'}</li>
                <li>钩子技巧/可学方法 → 伏笔线索</li>
                {syncMode === 'fanfic' && <li>世界观/设定 → 世界观设定</li>}
              </ul>
            </div>

            <div className="modal-actions">
              <button className="btn-ghost" onClick={() => setShowSyncModal(false)}>取消</button>
              <button className="btn-primary" onClick={handleSyncAnalysis} disabled={!syncBookId || syncing}>
                {syncing ? '同步中...' : '确认同步'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}