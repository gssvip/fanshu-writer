import { useState, useEffect, useRef, useContext } from 'react';
import { api } from '../../api';
import { AuthContext } from '../../App';
import type { Book, AnalysisResult, RankPositionAnalysis } from '../../types';
import type { RankAnalyzeTarget } from './RankingsTab';
import Icon from '../../components/Icon';

const SYNC_FIELD_LABELS: Record<string, string> = {
  style_guide: '风格指南', plot_design: '大纲设计', character_profiles: '人物档案',
  foreshadowing: '伏笔', worldbuilding: '世界观', key_rules: '设定', timeline: '剧情',
  concept: '构思',
};

interface AnalyzeTabProps {
  books: Book[];
  selectedBookId?: string;
  /** 从榜单风向书行带来的对标书（元数据直拆，无需手动导入） */
  rankBook?: RankAnalyzeTarget | null;
  onClearRankBook?: () => void;
}

export default function AnalyzeTab({ books, selectedBookId = '', rankBook = null, onClearRankBook }: AnalyzeTabProps) {
  const { requireAuth } = useContext(AuthContext);

  const [analyzeInput, setAnalyzeInput] = useState('');
  const [analyzeResult, setAnalyzeResult] = useState<AnalysisResult | null>(null);
  const [analyzeLoading, setAnalyzeLoading] = useState(false);
  // 竞品拆书模式：normal=普通拆书 / competitor=竞品对标拆解
  const [analyzeMode, setAnalyzeMode] = useState<'normal' | 'competitor'>('normal');
  const [uploadFilename, setUploadFilename] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const [showSyncModal, setShowSyncModal] = useState(false);
  const [syncBookId, setSyncBookId] = useState('');
  const [syncMode, setSyncMode] = useState('imitate');
  const [syncing, setSyncing] = useState(false);

  // ===== 榜单书直拆（无需导入）=====
  const [positionResult, setPositionResult] = useState<RankPositionAnalysis | null>(null);
  const [positionLoading, setPositionLoading] = useState(false);
  const [positionError, setPositionError] = useState('');
  const [deepLoading, setDeepLoading] = useState(false);
  const [deepMsg, setDeepMsg] = useState('');
  const [deepError, setDeepError] = useState('');
  const [chapterN, setChapterN] = useState(3);
  const [fetchedMeta, setFetchedMeta] = useState<{ chapters: number; total: number; decodeRate: number; locked?: boolean; lockedChapterNo?: number } | null>(null);

  // 切换到竞品模式更符合榜单对标场景：榜单书进入时默认竞品拆书
  // 注意：进入时不再自动跑轻拆——等用户自己选择「轻拆」或「深拆」
  useEffect(() => {
    if (rankBook) {
      setAnalyzeMode('competitor');
      setPositionResult(null);
      setPositionError('');
      setDeepError('');
      setDeepMsg('');
      setFetchedMeta(null);
      setAnalyzeResult(null);
      setAnalyzeInput('');
      setUploadFilename('');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rankBook?.bookId, rankBook?.platform]);

  async function handlePositionAnalyze(silent = false) {
    if (!rankBook) return;
    const ok = await requireAuth();
    if (!ok) return;
    setPositionLoading(true);
    setPositionError('');
    try {
      const r = await api.nrPositionAnalyze({
        platform: rankBook.platform,
        bookId: rankBook.bookId,
        bookUrl: rankBook.bookUrl,
        title: rankBook.bookTitle,
        authorName: rankBook.authorName,
        intro: rankBook.intro,
        categoryName: rankBook.categoryName,
        categorySubName: rankBook.categorySubName,
        metricName: rankBook.metricName,
        metricText: rankBook.metricText ?? rankBook.readingText,
        metricValue: rankBook.metricValue,
        rankNo: rankBook.rankNo,
        statusText: rankBook.statusText,
        hotTags: rankBook.hotTags || [],
      });
      if (r.ok === false || r.error) {
        setPositionError(r.error || '定位分析失败');
        if (!silent) alert(r.error || '定位分析失败');
      } else {
        setPositionResult(r);
      }
    } catch (e: any) {
      setPositionError(e?.message || '定位分析失败');
    } finally {
      setPositionLoading(false);
    }
  }

  // 抓取前 N 章正文 → 灌入输入框 → 自动走现有拆书（普通/竞品跟随当前模式）
  async function handleDeepAnalyze() {
    if (!rankBook) return;
    const ok = await requireAuth();
    if (!ok) return;
    setDeepLoading(true);
    setDeepError('');
    setDeepMsg(chapterN >= 10
      ? `正在从${rankBook.platformName || '平台'}抓取前 ${chapterN} 章（章节较多，含礼貌限速，约需 30~90 秒，请勿离开）…`
      : `正在从${rankBook.platformName || '平台'}抓取前 ${chapterN} 章（目录+正文，约需几秒）…`);
    try {
      const r = await api.nrFetchBookContent({
        platform: rankBook.platform,
        bookId: rankBook.bookId,
        bookUrl: rankBook.bookUrl,
        chapters: chapterN,
      });
      if (!r.ok || !r.content) {
        setDeepError(r.error || '正文抓取失败，请改用粘贴方式');
        setDeepMsg('');
        return;
      }
      const got = r.fetchedChapters?.length || 0;
      setDeepMsg(r.locked
        ? `该书第 ${r.lockedChapterNo} 章起为付费章节，已自动抓全此前 ${got} 章免费正文，正在 AI 深拆…`
        : `已抓取 ${got} 章 / 共 ${r.chapterCount} 章，正在 AI 深拆…`);
      setAnalyzeInput(r.content);
      setFetchedMeta({
        chapters: got,
        total: r.chapterCount || 0,
        decodeRate: Math.round((r.decodeRate ?? 1) * 100),
        locked: r.locked,
        lockedChapterNo: r.lockedChapterNo,
      });
      setUploadFilename(`榜单直抓：《${rankBook.bookTitle}》前${got}章${r.locked ? '（免费章）' : ''}`);
      await runAnalyze(r.content, analyzeMode);
    } catch (e: any) {
      setDeepError(e?.message || '抓取/分析失败，请改用粘贴方式');
    } finally {
      setDeepLoading(false);
      setDeepMsg('');
    }
  }

  function handlePasteFallback() {
    setAnalyzeInput(rankBook?.intro ? `【对标书简介（参考，可删）】\n${rankBook.intro}\n\n【在此粘贴正文】\n` : '');
    setTimeout(() => textareaRef.current?.focus(), 50);
    setTimeout(() => textareaRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 80);
  }

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

  // 统一拆书内核：支持榜单直抓后以任意文本直接分析（规避 setState 异步）
  async function runAnalyze(text: string, mode: 'normal' | 'competitor') {
    if (!text.trim()) return;
    const ok = await requireAuth();
    if (!ok) return;
    setAnalyzeLoading(true);
    setAnalyzeResult(null);
    try {
      const r = await api.analyzeBook(text, mode === 'competitor' ? 'competitor' : undefined);
      setAnalyzeResult(r);
    } catch (e: any) { alert('分析失败: ' + e.message); }
    setAnalyzeLoading(false);
  }

  async function handleAnalyze() {
    return runAnalyze(analyzeInput, analyzeMode);
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
        {/* ===== 榜单书直拆：来源卡片（无需手动导入）===== */}
        {rankBook && (
          <div style={{
            marginBottom:14,borderRadius:12,border:'1px solid color-mix(in srgb, var(--accent) 30%, var(--border-color))',
            background:'color-mix(in srgb, var(--accent) 6%, var(--bg-secondary))',overflow:'hidden',
          }}>
            <div style={{display:'flex',alignItems:'flex-start',gap:10,padding:'12px 14px'}}>
              {rankBook.coverUrl
                ? <img src={rankBook.coverUrl} alt="" style={{width:44,height:60,borderRadius:6,objectFit:'cover',flexShrink:0,background:'var(--bg-tertiary)'}} />
                : <div style={{width:44,height:60,borderRadius:6,flexShrink:0,display:'flex',alignItems:'center',justifyContent:'center',fontSize:22,color:'var(--accent)',background:'var(--accent-light)'}}><Icon name="book" size={24} /></div>}
              <div style={{flex:1,minWidth:0}}>
                <div style={{display:'flex',alignItems:'center',gap:8,flexWrap:'wrap'}}>
                  <b style={{fontSize:15,color:'var(--text-primary)'}}>《{rankBook.bookTitle || '未命名'}》</b>
                  <span style={{fontSize:11,padding:'2px 8px',borderRadius:999,background:'var(--accent-light)',color:'var(--accent)',fontWeight:700}}>
                    <Icon name="trending-up" size={11} /> {rankBook.platformName || '榜单'}
                  </span>
                  {rankBook.rankNo ? <span style={{fontSize:11.5,color:'var(--text-muted)'}}>第{rankBook.rankNo}名</span> : null}
                  {(rankBook.metricText || rankBook.readingText) && (
                    <span style={{fontSize:11.5,color:'var(--accent)',fontWeight:700}}>
                      {rankBook.metricName ? `${rankBook.metricName} ` : ''}{rankBook.metricText || rankBook.readingText}
                    </span>
                  )}
                </div>
                <div style={{fontSize:12,color:'var(--text-muted)',marginTop:4}}>
                  {rankBook.authorName ? `作者：${rankBook.authorName}` : ''}
                  {[rankBook.categoryName, rankBook.categorySubName].filter(Boolean).length ? `　分类：${[rankBook.categoryName, rankBook.categorySubName].filter(Boolean).join(' / ')}` : ''}
                  {rankBook.statusText ? `　${rankBook.statusText}` : ''}
                </div>
                {rankBook.intro && (
                  <div style={{fontSize:12,color:'var(--text-secondary)',lineHeight:1.6,marginTop:6,
                    display:'-webkit-box',WebkitLineClamp:3,WebkitBoxOrient:'vertical',overflow:'hidden'}}>{rankBook.intro}</div>
                )}
              </div>
              <button type="button" title="退出榜单直拆，回到手动导入"
                onClick={() => {
                  setPositionResult(null); setPositionError('');
                  setDeepError(''); setDeepMsg(''); setFetchedMeta(null);
                  onClearRankBook?.();
                }}
                style={{flexShrink:0,border:'none',background:'transparent',color:'var(--text-muted)',cursor:'pointer',padding:4,fontSize:16,lineHeight:1}}>×</button>
            </div>

            {/* 拆书动作：用户手动选择「轻拆」或「深拆」，贴文为已有正文的兜底入口
                桌面：轻拆｜前N章+深拆 ........ 贴文（单行 flex）；手机：轻拆/深拆 二选一第一行 */}
            <div className={`nr-rank-actions${rankBook.platform === 'fanqie' ? ' is-fanqie' : ' no-fetch'}`}>
              <button type="button" className="btn-secondary nr-ra-btn nr-ra-light"
                onClick={() => handlePositionAnalyze(false)} disabled={positionLoading || deepLoading}
                title="仅凭榜单元数据快速定位：赛道、卖点、目标读者，无需正文">
                {positionLoading ? <><Icon name="hourglass" size={13} /> 轻拆中…</> : <><Icon name="target" size={13} /> 轻拆{positionResult ? '（重跑）' : ''}</>}
              </button>

              {rankBook.platform === 'fanqie' ? (
                <>
                  <select className="input nr-ra-select" value={chapterN} onChange={e => setChapterN(Number(e.target.value))}
                    disabled={deepLoading || positionLoading} aria-label="抓取章节数" title="深拆自动抓取的正文章节数">
                    {[3, 10, 30].map(n => <option key={n} value={n}>前 {n} 章</option>)}
                  </select>
                  <button type="button" className="btn-primary nr-ra-btn nr-ra-deep"
                    onClick={handleDeepAnalyze} disabled={deepLoading || positionLoading}
                    title="自动抓取前 N 章正文，做完整深度拆解">
                    {deepLoading ? <><Icon name="hourglass" size={13} /> 深拆中…</> : <><Icon name="download" size={13} /> 深拆</>}
                  </button>
                </>
              ) : (
                <button type="button" className="btn-secondary nr-ra-btn nr-ra-wide" disabled
                  title={`${rankBook.platformName || '该平台'}正文受版权/反爬限制，暂不支持自动抓取`}>
                  <Icon name="lock" size={13} /> 该平台暂不支持自动抓取
                </button>
              )}

              <button type="button" className="btn-secondary nr-ra-btn nr-ra-paste" onClick={handlePasteFallback}
                title="手上已有正文，粘贴到下方后开始分析">
                <Icon name="edit" size={13} /> 贴文
              </button>
            </div>

            {/* 未做任何拆解前的选择引导（开始轻拆/深拆/贴文后消失） */}
            {!positionResult && !analyzeResult && !positionLoading && !deepLoading && !deepError && (
              <div className="nr-ra-hint">
                <Icon name="info" size={12} />
                <span>请选择拆解方式：「轻拆」仅凭榜单数据秒出定位；「深拆」自动抓前 {chapterN} 章正文深度拆解；手上已有正文点「贴文」。</span>
              </div>
            )}

            {deepMsg && (
              <div style={{padding:'0 14px 10px',fontSize:12.5,color:'var(--accent)',display:'flex',alignItems:'center',gap:6}}>
                <Icon name="hourglass" size={13} /> {deepMsg}
              </div>
            )}
            {deepError && (
              <div style={{padding:'0 14px 10px',fontSize:12.5,color:'#c0392b',lineHeight:1.6}}>
                <Icon name="warning" size={12} /> {deepError}
              </div>
            )}
            {fetchedMeta && (
              <div style={{padding:'0 14px 10px',fontSize:12,color:'var(--text-muted)',lineHeight:1.7}}>
                {fetchedMeta.locked ? (
                  <span style={{color:'#b3541e'}}>
                    <Icon name="lock" size={12} /> 该书第 {fetchedMeta.lockedChapterNo} 章起为平台付费章节（仅开放百余字试读，无法自动抓取），
                    已抓全此前全部 <b>{fetchedMeta.chapters}</b> 章免费正文；文字解码率 {fetchedMeta.decodeRate}%，正文已填入下方，可直接分析或自行粘贴后续章节。
                  </span>
                ) : (
                  <>已抓取 {fetchedMeta.chapters} 章（全书共 {fetchedMeta.total} 章），文字解码率 {fetchedMeta.decodeRate}%，正文已填入下方，可直接修改后重新分析。</>
                )}
              </div>
            )}

            {/* 轻拆结果 */}
            {positionLoading && !positionResult && (
              <div style={{padding:'4px 14px 14px',fontSize:13,color:'var(--text-muted)'}}>
                <Icon name="hourglass" size={13} /> 正在基于榜单数据做轻拆定位分析…
              </div>
            )}
            {positionError && !positionResult && (
              <div style={{padding:'0 14px 12px',fontSize:12.5,color:'#c0392b'}}>
                <Icon name="warning" size={12} /> {positionError}
              </div>
            )}
            {positionResult && (
              <div style={{borderTop:'1px solid color-mix(in srgb, var(--accent) 22%, var(--border-color))',padding:'12px 14px'}}>
                <div style={{display:'flex',alignItems:'center',gap:6,marginBottom:8,fontSize:13,fontWeight:800,color:'var(--accent)'}}>
                  <Icon name="target" size={14} /> 轻拆（基于榜单数据，无需正文）
                </div>
                {positionResult.genre_tags?.length ? (
                  <div className="analyze-tags" style={{marginBottom:8}}>
                    {positionResult.genre_tags.map((t, i) => <span key={i} className="tag">{t}</span>)}
                  </div>
                ) : null}
                <div className="analyze-grid" style={{marginBottom:8}}>
                  {positionResult.positioning && <div className="analyze-item"><h4>赛道定位</h4><p>{positionResult.positioning}</p></div>}
                  {positionResult.one_liner_hook && <div className="analyze-item"><h4>一句话卖点</h4><p>{positionResult.one_liner_hook}</p></div>}
                  {positionResult.target_audience && <div className="analyze-item"><h4>目标读者</h4><p>{positionResult.target_audience}</p></div>}
                </div>
                {positionResult.selling_points?.length ? (
                  <div className="review-section"><h4><Icon name="flame" size={14} /> 卖点 / 爽点假设</h4><ul>{positionResult.selling_points.map((x, i) => <li key={i}>{x}</li>)}</ul></div>
                ) : null}
                {positionResult.opening_hypothesis?.length ? (
                  <div className="review-section"><h4>黄金三章开篇打法（推测）</h4><ul>{positionResult.opening_hypothesis.map((x, i) => <li key={i}>{x}</li>)}</ul></div>
                ) : null}
                {positionResult.learnable_points?.length ? (
                  <div className="review-section"><h4>可直接借鉴</h4><ul>{positionResult.learnable_points.map((x, i) => <li key={i}>{x}</li>)}</ul></div>
                ) : null}
                {positionResult.differentiation?.length ? (
                  <div className="review-section"><h4><Icon name="zap" size={14} /> 差异化机会</h4><ul>{positionResult.differentiation.map((x, i) => <li key={i}>{x}</li>)}</ul></div>
                ) : null}
                {positionResult.to_verify?.length ? (
                  <div className="review-section" style={{opacity:.85}}><h4><Icon name="warning" size={13} /> 需读正文验证</h4><ul>{positionResult.to_verify.map((x, i) => <li key={i}>{x}</li>)}</ul></div>
                ) : null}
              </div>
            )}
          </div>
        )}

        <div className="form-row analyze-mode-row" style={{alignItems:'stretch',gap:8,marginBottom:10,flexDirection:'row',flexWrap:'nowrap'}}>
          <button
            className={analyzeMode === 'normal' ? 'btn-primary' : 'btn-secondary'}
            onClick={() => setAnalyzeMode('normal')}
          ><Icon name="book" size={15} /> 普通拆书</button>
          <button
            className={analyzeMode === 'competitor' ? 'btn-primary' : 'btn-secondary'}
            onClick={() => setAnalyzeMode('competitor')}
            title="站在竞品对标角度，输出市场定位、核心优势、差异弱点与可复刻方案"
          ><Icon name="sword" size={15} /> 竞品拆书</button>
        </div>
        <div className="form-row" style={{marginBottom:10}}>
          <input ref={fileInputRef} type="file" accept=".txt,.md,.docx,.zip,.json" onChange={handleUploadFile} style={{display:'none'}} id="analyze-file-input" />
          <label htmlFor="analyze-file-input" className="btn-secondary" style={{cursor:'pointer',padding:'8px 16px',borderRadius:'var(--radius-sm)',display:'inline-block'}}>
            <Icon name="folder" size={14} /> 导入文件
          </label>
          {uploadFilename && <span className="text-muted" style={{alignSelf:'center'}}>已导入: {uploadFilename}</span>}
          <span className="text-muted" style={{alignSelf:'center',fontSize:11}}>支持 txt/md/docx/zip</span>
        </div>
        <textarea ref={textareaRef} className="input" rows={8} value={analyzeInput} onChange={e => setAnalyzeInput(e.target.value)}
          placeholder={rankBook ? '点上方「深拆」自动抓正文填入，或点「贴文」后在此粘贴已有正文…' : '粘贴要分析的作品片段（建议2000字以上），或导入文件自动填充...'} />
        <div className="form-row">
          <button className="btn-primary" onClick={handleAnalyze} disabled={!analyzeInput.trim() || analyzeLoading}>
            {analyzeLoading ? '分析中...' : '开始分析'}
          </button>
          {analyzeResult && (
            <>
              <button className="btn-secondary" onClick={handleExportAnalysis}><Icon name="inbox" size={14} /> 导出结果</button>
              <button className="btn-secondary" onClick={() => { setSyncBookId(prev => prev || selectedBookId || ''); setShowSyncModal(true); }} style={{borderColor:'var(--accent)',color:'var(--accent)'}}>
                <Icon name="clipboard" size={14} /> 同步到作品
              </button>
            </>
          )}
        </div>
        {analyzeResult && (
          <div className="analyze-result">
            {/* 一句话卖点 */}
            {analyzeResult.logline && (
              <div className="ar-logline">
                <Icon name="flame" size={15} />
                <span><b>一句话卖点：</b>{analyzeResult.logline}</span>
              </div>
            )}
            <div className="analyze-tags">
              {analyzeResult.genre_tags?.map((t, i) => <span key={i} className="tag">{t}</span>)}
              {analyzeResult.target_platform && <span className="tag platform">{analyzeResult.target_platform}</span>}
            </div>

            {/* 主线核心矛盾 */}
            {analyzeResult.core_conflict && (
              <div className="review-section ar-conflict"><h4><Icon name="sword" size={15} /> 主线核心矛盾</h4><p>{analyzeResult.core_conflict}</p></div>
            )}

            {/* 黄金三章 */}
            {analyzeResult.golden_three?.length ? (
              <div className="review-section">
                <h4><Icon name="book-open" size={15} /> 黄金三章拆解（入局 → 加压 → 承诺）</h4>
                <div className="ar-golden3">
                  {analyzeResult.golden_three.map((c, i) => (
                    <div key={i} className="ar-g3-card">
                      <div className="ar-g3-head">
                        <span className="ar-g3-no">{i + 1}</span>
                        <b>{c.chapter || `第${i + 1}章`}</b>
                        {c.mission && <span className="ar-g3-mission">{c.mission}</span>}
                      </div>
                      {c.opening && <p><em>开篇钩子</em>{c.opening}</p>}
                      {c.info && <p><em>信息释放</em>{c.info}</p>}
                      {c.ending && <p><em>章尾钩子</em>{c.ending}</p>}
                      {c.emotion && <p><em>追读情绪</em>{c.emotion}</p>}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {/* 金手指 / 核心设定 */}
            {analyzeResult.golden_finger && (analyzeResult.golden_finger.name || analyzeResult.golden_finger.rules) ? (
              <div className="review-section">
                <h4><Icon name="zap" size={15} /> 金手指 / 核心设定</h4>
                <div className="ar-finger">
                  {analyzeResult.golden_finger.name && <p><em>设定</em><b>{analyzeResult.golden_finger.name}</b>{analyzeResult.golden_finger.type ? `（${analyzeResult.golden_finger.type}）` : ''}</p>}
                  {analyzeResult.golden_finger.rules && <p><em>规则/代价</em>{analyzeResult.golden_finger.rules}</p>}
                  {analyzeResult.golden_finger.upgrade && <p><em>升级路径</em>{analyzeResult.golden_finger.upgrade}</p>}
                </div>
              </div>
            ) : null}

            {/* 剧情节拍表 */}
            {analyzeResult.plot_beats?.length ? (
              <div className="review-section">
                <h4><Icon name="bar-chart" size={15} /> 剧情节拍表</h4>
                <div className="ar-beats-table">
                  {analyzeResult.plot_beats.map((b, i) => (
                    <div key={i} className="ar-beat-row">
                      <span className="ar-beat-range">{b.range || `第${i + 1}段`}</span>
                      <span className={`ar-beat-func ar-func-${i}`}>{b.function || '—'}</span>
                      <span className="ar-beat-event">{b.event || ''}</span>
                      <span className="ar-beat-hook">{b.hook || ''}</span>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {/* 人物表 */}
            {analyzeResult.characters?.length ? (
              <div className="review-section">
                <h4><Icon name="users" size={15} /> 人物拆解</h4>
                <div className="ar-char-grid">
                  {analyzeResult.characters.map((c, i) => (
                    <div key={i} className="ar-char-card">
                      <div className="ar-char-head"><b>{c.name || `角色${i + 1}`}</b>{c.function && <span>{c.function}</span>}</div>
                      {c.identity && <p><em>身份</em>{c.identity}</p>}
                      {c.desire && <p><em>欲望</em>{c.desire}</p>}
                      {c.trait && <p><em>性格</em>{c.trait}</p>}
                      {c.contrast && <p><em>记忆点</em>{c.contrast}</p>}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {/* 爽点库 */}
            {analyzeResult.satisfaction_points?.length ? (
              <div className="review-section">
                <h4><Icon name="flame" size={15} /> 爽点设计库（铺垫 → 兑现）</h4>
                <div className="ar-sat-list">
                  {analyzeResult.satisfaction_points.map((s, i) => (
                    <div key={i} className="ar-sat-card">
                      <div className="ar-sat-top">
                        <span className="ar-sat-type">{s.type || `爽点${i + 1}`}</span>
                        {s.position && <span className="ar-sat-pos">{s.position}</span>}
                      </div>
                      {s.setup && <p><em>铺垫</em>{s.setup}</p>}
                      {s.payoff && <p><em>兑现</em>{s.payoff}</p>}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            <div className="analyze-grid">
              <div className="analyze-item"><h4>文风特点</h4><p>{analyzeResult.style_analysis}</p></div>
              <div className="analyze-item"><h4>结构特点</h4><p>{analyzeResult.structure_analysis}</p></div>
              <div className="analyze-item"><h4>节奏特点</h4><p>{analyzeResult.rhythm_analysis}</p></div>
              <div className="analyze-item"><h4>人设特点</h4><p>{analyzeResult.character_design_analysis}</p></div>
            </div>

            {/* 节奏体检 */}
            {analyzeResult.pacing && (analyzeResult.pacing.density || analyzeResult.pacing.tension || analyzeResult.pacing.issues?.length) ? (
              <div className="review-section">
                <h4><Icon name="clock" size={15} /> 节奏体检</h4>
                <div className="ar-pacing">
                  {analyzeResult.pacing.density && <p><em>爽点密度</em>{analyzeResult.pacing.density}</p>}
                  {analyzeResult.pacing.tension && <p><em>张弛配比</em>{analyzeResult.pacing.tension}</p>}
                  {analyzeResult.pacing.issues?.length ? (
                    <ul className="compact-list">{analyzeResult.pacing.issues.map((x, i) => <li key={i} className="ar-risk-item">{x}</li>)}</ul>
                  ) : null}
                </div>
              </div>
            ) : null}

            {analyzeResult.hook_system && (
              <div className="review-section"><h4><Icon name="link" size={15} /> 钩子编排体系</h4><p>{analyzeResult.hook_system}</p></div>
            )}
            <div className="review-section"><h4>钩子技巧</h4><ul>{analyzeResult.hook_techniques?.map((h, i) => <li key={i}>{h}</li>)}</ul></div>
            {analyzeResult.emotion_curve && (
              <div className="review-section"><h4><Icon name="trending-up" size={15} /> 期待感与情绪曲线</h4><p>{analyzeResult.emotion_curve}</p></div>
            )}
            {analyzeResult.worldview?.length ? (
              <div className="review-section"><h4><Icon name="globe" size={15} /> 世界观 / 力量体系</h4><ul>{analyzeResult.worldview.map((w, i) => <li key={i}>{w}</li>)}</ul></div>
            ) : null}
            <div className="review-section"><h4>可学习的方法</h4><ul>{analyzeResult.learnable_points?.map((p, i) => <li key={i}>{p}</li>)}</ul></div>

            {analyzeMode === 'competitor' && analyzeResult.market_position && (
              <div className="review-section competitor-pos"><h4><Icon name="target" size={15} /> 市场定位</h4><p>{analyzeResult.market_position}</p></div>
            )}
            {analyzeMode === 'competitor' && (analyzeResult.strengths?.length || analyzeResult.weaknesses?.length) && (
              <div className="analyze-grid">
                {analyzeResult.strengths && analyzeResult.strengths.length > 0 && (
                  <div className="analyze-item"><h4><Icon name="flame" size={15} /> 核心优势</h4><ul className="compact-list">{(analyzeResult.strengths as string[]).map((s, i) => <li key={i}>{s}</li>)}</ul></div>
                )}
                {analyzeResult.weaknesses && analyzeResult.weaknesses.length > 0 && (
                  <div className="analyze-item"><h4><Icon name="zap" size={15} /> 差异化机会（弱点切入）</h4><ul className="compact-list">{(analyzeResult.weaknesses as string[]).map((w, i) => <li key={i}>{w}</li>)}</ul></div>
                )}
              </div>
            )}
            {analyzeMode === 'competitor' && analyzeResult.copy_plan && (
              <div className="review-section ar-template-box"><h4><Icon name="edit" size={15} /> 复刻方案（借鉴爆点 + 规避同质化）</h4><p>{analyzeResult.copy_plan}</p></div>
            )}

            {/* 可复用模板 */}
            {analyzeResult.replicable_template && (
              <div className="review-section ar-template-box">
                <h4><Icon name="clipboard" size={15} /> 可直接套用的创作模板</h4>
                <p>{analyzeResult.replicable_template}</p>
              </div>
            )}
            {/* 行动清单 */}
            {analyzeResult.action_plan?.length ? (
              <div className="review-section ar-actions-box">
                <h4><Icon name="check-circle" size={15} /> 我的落地行动清单</h4>
                <ol className="ar-action-list">
                  {analyzeResult.action_plan.map((a, i) => <li key={i}>{a}</li>)}
                </ol>
              </div>
            ) : null}
            {/* 风险 */}
            {analyzeResult.risks?.length ? (
              <div className="review-section"><h4><Icon name="warning" size={15} /> 短板与避坑</h4><ul>{analyzeResult.risks.map((x, i) => <li key={i} className="ar-risk-item">{x}</li>)}</ul></div>
            ) : null}
            {analyzeResult.golden_lines?.length > 0 && (
              <div className="review-section"><h4>金句摘录</h4><ul>{analyzeResult.golden_lines.map((l, i) => <li key={i} className="golden-line">{l}</li>)}</ul></div>
            )}
          </div>
        )}
      </div>

      {showSyncModal && (
        <div className="modal-overlay" onClick={() => setShowSyncModal(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h2><Icon name="clipboard" size={16} /> 同步分析结果到作品</h2>

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
                    <div className="sync-mode-name"><Icon name="edit" size={14} /> 仿写模式</div>
                    <div className="sync-mode-desc">提取原文风格、结构、节奏，作为创作参考填充到作品设定中</div>
                  </div>
                </label>
                <label className={`sync-mode-card ${syncMode === 'fanfic' ? 'active' : ''}`}>
                  <input type="radio" name="syncMode" value="fanfic" checked={syncMode === 'fanfic'} onChange={e => setSyncMode(e.target.value)} />
                  <div className="sync-mode-info">
                    <div className="sync-mode-name"><Icon name="book-open" size={14} /> 同人文模式</div>
                    <div className="sync-mode-desc">提取原文世界观、人物设定，作为同人文创作的基础资料</div>
                  </div>
                </label>
                <label className={`sync-mode-card ${syncMode === 'reference' ? 'active' : ''}`}>
                  <input type="radio" name="syncMode" value="reference" checked={syncMode === 'reference'} onChange={e => setSyncMode(e.target.value)} />
                  <div className="sync-mode-info">
                    <div className="sync-mode-name"><Icon name="lightbulb" size={14} /> 参考模式</div>
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