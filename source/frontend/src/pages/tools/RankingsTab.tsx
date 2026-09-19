import { useState, useEffect, useRef } from 'react';
import { api } from '../../api';
import type { RankingData, NRPlatform, NRFilters, NRRankType, NRCategory, NRListResult, NRItem } from '../../types';

export default function RankingsTab() {
  // 【榜单风向 V2 · 移植自 easy-writing: NovelRank】
  const [nrPlatforms, setNrPlatforms] = useState<NRPlatform[]>([]);
  const [nrPlatform, setNrPlatform] = useState('fanqie');
  const [nrRankType, setNrRankType] = useState<string>('');
  const [nrGender, setNrGender] = useState<string>('');
  const [nrCategoryCode, setNrCategoryCode] = useState<string>('__all__');
  const [nrSubCategoryCode, setNrSubCategoryCode] = useState<string>('');
  const [nrFilters, setNrFilters] = useState<NRFilters | null>(null);
  const [nrFiltersLoading, setNrFiltersLoading] = useState(false);
  const [nrList, setNrList] = useState<NRListResult | null>(null);
  const [nrListLoading, setNrListLoading] = useState(false);
  const [nrListError, setNrListError] = useState('');
  const [nrKeyword, setNrKeyword] = useState('');
  const [nrPage, setNrPage] = useState(1);
  const [nrCrawling, setNrCrawling] = useState(false);
  const [nrCategoryCollapsed, setNrCategoryCollapsed] = useState(false); // 手机端主题分类折叠
  // 保留原 getRankings 返回的旧结构作为 banner
  const [rankBanner, setRankBanner] = useState<RankingData | null>(null);
  // #6 手动抓取控制：筛选/分类/关键词变化时不自动抓，仅点击「抓取本榜」/ 搜索按钮 / 分页 才抓
  const [nrFetchKey, setNrFetchKey] = useState(0); // 递增：触发一次请求

  // 加载榜单风向：平台列表 + 默认筛选（仅首次进榜单 Tab 触发一次抓取 + 移动端加载默认源）
  useEffect(() => {
    (async () => {
      try {
        const r = await api.nrListPlatforms();
        if (Array.isArray(r.platforms)) setNrPlatforms(r.platforms);
      } catch {}
    })();
  }, []);

  // 平台/榜单类型/男女频变化 → 只刷新 filters 与默认 category/rankType/gender，**不自动抓书**（#6 手动抓取才拉）
  useEffect(() => {
    (async () => {
      setNrFiltersLoading(true);
      setNrSubCategoryCode('');
      try {
        const f = await api.nrListFilters(nrPlatform, { rankType: nrRankType || undefined, gender: nrGender || undefined });
        setNrFilters(f);
        if (f.rankTypes.length && !f.rankTypes.find(r => r.value === nrRankType)) {
          setNrRankType(f.rankTypes[0].value);
          return; // 下一个 effect 会继续修正 filters 但仍不自动抓
        }
        const all = f.categories.find((c: NRCategory) => c.code === '__all__') || f.categories[0];
        const code = (all ? all.code : '__all__');
        if (code !== nrCategoryCode) setNrCategoryCode(code);
      } catch {}
      finally { setNrFiltersLoading(false); }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nrPlatform, nrRankType, nrGender]);

  // #6 真正抓书：只有 nrFetchKey 递增（点击「抓取本榜」/搜索/分页）才会执行一次 loadNrBooks
  useEffect(() => {
    if (nrFetchKey === 0) return; // 0 代表尚未点击抓取
    loadNrBooks(nrSubCategoryCode || nrCategoryCode, nrRankType, nrGender, nrPage, nrKeyword, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nrFetchKey]);

  // 进入榜单首次：自动拉一次默认平台的默认榜单给用户看，同时填好 banner 热词
  const _didInitialFetchRef = useRef(false);
  useEffect(() => {
    if (_didInitialFetchRef.current) return;
    _didInitialFetchRef.current = true;
    // filters 先加载 → 然后触发首次抓取（仅一次，用 ref 保证不再重复）
    const t = window.setTimeout(() => setNrFetchKey(k => k + 1), 180);
    return () => window.clearTimeout(t);
  }, []);

  async function loadNrBooks(catCode: string, rt: string, gd: string, page = 1, kw = '', force = false) {
    setNrListLoading(true);
    setNrListError('');
    try {
      const r = await api.nrListBooks({
        platform: nrPlatform,
        rankType: rt || undefined,
        gender: gd || undefined,
        categoryCode: catCode === '__all__' ? undefined : catCode,
        keyword: kw || undefined,
        page,
        pageSize: 20,
        force,
      });
      setNrList(r);
    } catch (e: any) { setNrListError(e?.message || '加载失败'); setNrList(null); }
    finally { setNrListLoading(false); }
    // 异步拉 banner（热门标签/上升关键词）——与 V2 钻取并行展示（#4手机端两栏各一行）
    try {
      const b = await api.getRankings(nrPlatform);
      setRankBanner(b);
    } catch { /* banner 失败不阻塞主视图 */ }
  }

  function triggerNrFetch(resetPage = false) {
    // 统一入口：点击「抓取本榜」 / 搜索按钮 / Enter 搜索 都走这个
    if (resetPage) setNrPage(1);
    // useLayoutEffect 前保证 state 都落盘后再触发：用 microtask 后 setNrFetchKey
    Promise.resolve().then(() => {
      setNrFetchKey(k => k + 1);
    });
  }

  async function handleNrCrawlNow() {
    // #6「抓取本榜」：即使没有 sourceId（未选中过榜单），也要根据当前筛选条件去爬一次（force=true 强制不走缓存）
    setNrCrawling(true);
    try {
      const sid = nrList?.sourceId;
      if (sid) {
        try { await api.nrForceCrawl(sid); } catch { /* 某些精选源无法 force，fallback 继续拉一次 force=nrListBooks 即可 */ }
      }
      await loadNrBooks(nrSubCategoryCode || nrCategoryCode, nrRankType, nrGender, nrPage, nrKeyword, true);
    } catch (e: any) { alert('刷新失败：' + (e?.message || String(e))); }
    finally { setNrCrawling(false); }
  }

  return (
    <div className="tool-panel nr-root">
      {/* #4 #5 标题行：📈 榜单风向 左侧；🔍 搜索框+按钮 紧接其右侧（桌面端 inline，移动端换行） */}
      <div className="nr-title-row" style={{
        display:'flex',flexWrap:'wrap',gap:10,alignItems:'center',
        marginBottom:10, width:'100%', boxSizing:'border-box', minWidth:0,
      }}>
        <h3 style={{margin:0,fontSize:17,fontWeight:800,color:'var(--text-primary)',
                    display:'inline-flex',alignItems:'center',gap:6,flex:'0 0 auto'}}>
          <span>📈</span><span>榜单风向</span>
        </h3>
        <div className="nr-title-search" style={{
          display:'flex', gap:8, alignItems:'center',
          flex:'1 1 260px', minWidth:200, maxWidth:'100%', boxSizing:'border-box',
        }}>
          <input
            className="input nr-search-input"
            value={nrKeyword}
            onChange={e => setNrKeyword(e.target.value)}
            placeholder="搜索书名 / 作者，找到你想仿写的同类书"
            style={{
              flex:'1 1 auto', minWidth:0, maxWidth:'100%',
              padding:'8px 12px', fontSize:13, minHeight:36,
              borderRadius:10, boxSizing:'border-box',
            }}
            onKeyDown={e => { if (e.key === 'Enter') triggerNrFetch(true); }}
          />
          <button
            className="chat-send primary nr-search-btn"
            onClick={() => triggerNrFetch(true)}
            disabled={nrListLoading || nrCrawling}
            style={{
              padding:'8px 16px',minHeight:36,fontSize:13,fontWeight:700,
              flex:'0 0 auto',borderRadius:10,
              background:'var(--accent-light)',
              color:'var(--accent)',border:'1px solid #d0d5dd',
            }}
          >🔍 搜索</button>
        </div>
      </div>

      {/* 筛选主卡：平台 / 抓取 / 抓取时间 / 榜单类型+频道 / 主题分类 */}
      <div className="fusion-card nr-filter-card" style={{marginBottom:12}}>
        {/* 1. 平台 Tab + 抓取本榜 + #4 抓取时间（抓取按钮右侧 inline，下方不再重复显示） */}
        <div className="nr-platform-row" style={{
          display:'flex',flexWrap:'wrap',gap:8,alignItems:'center',
          width:'100%', boxSizing:'border-box', minWidth:0,
        }}>
          <div className="nr-platform-tabs" style={{display:'flex',flexWrap:'wrap',gap:8,alignItems:'center',flex:'1 1 320px',minWidth:0}}>
            {(nrPlatforms.length ? nrPlatforms : [
              {code:'fanqie',name:'番茄小说网'},
              {code:'qidian',name:'起点中文网'},
            ]).map(p => (
              <button
                key={p.code}
                className={`chat-send nr-platform-tab ${nrPlatform === p.code ? 'primary active' : 'ghost'}`}
                onClick={() => setNrPlatform(p.code)}
                title={p.remark || p.name}
                style={{
                  padding:'8px 14px',minHeight:36,fontWeight: nrPlatform===p.code? 700: 500,
                  flex:'1 1 0', minWidth: 120, fontSize: 14,
                  borderRadius: 10,
                  background: nrPlatform===p.code
                    ? 'var(--accent-light)'
                    : 'var(--bg-tertiary)',
                  color: nrPlatform===p.code ? 'var(--accent)' : 'var(--text-secondary)',
                  border: nrPlatform===p.code
                    ? '1px solid #d0d5dd'
                    : '1px solid var(--border-color)',
                }}
              >
                <span style={{fontSize:15}}>{p.code==='fanqie'?'🍅':p.code==='qidian'?'🏯':'📚'}</span>
                <span style={{fontSize:13,marginLeft:4,whiteSpace:'nowrap'}}>{p.name}</span>
              </button>
            ))}
          </div>

          {/* 竖分隔线（桌面端可见） */}
          <span className="nr-divider-v" style={{display:'none'}} />

          {/* 抓取按钮 + #4 抓取时间 inline */}
          <div className="nr-crawl-row" style={{
            display:'flex',gap:10,alignItems:'center',
            flex:'1 1 auto', minWidth:0, justifyContent:'flex-end',
          }}>
            <button
              className={`chat-send primary nr-crawl-btn ${nrList?.sourceKind==='curated' ? 'is-curated' : 'is-crawl'}`}
              onClick={handleNrCrawlNow}
              disabled={nrCrawling || nrListLoading}
              title="按当前筛选条件重新抓取/刷新本榜单（#6：点击才抓，不自动触发）"
            >
              {nrCrawling ? '⏳ 抓取中…' : nrList?.sourceKind==='curated' ? '🔄 刷新精选' : '☁️ 抓取本榜'}
            </button>
            {/* #4 抓取时间：抓取按钮右侧 inline；只展示一个（下面不要了） */}
            <span className="nr-crawl-time" style={{
              fontSize:12,color:'var(--text-muted)',whiteSpace:'nowrap',
              display:'inline-flex',alignItems:'center',gap:4,flexShrink:0,
              padding:'6px 10px',borderRadius:999,
              background:'var(--bg-tertiary)',border:'1px solid var(--border-color)',
            }}>
              <span style={{opacity:.7}}>🕒</span>
              <span>
                {nrList?.fetchAt
                  ? new Date(nrList.fetchAt * 1000).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).replace(/\//g,'-')
                  : '尚未抓取'}
              </span>
            </span>
          </div>
        </div>

        {/* curate 提示 banner */}
        {nrList?.sourceKind==='curated' && (
          <div className="crawl-status crawl-tip" style={{fontSize:12,marginTop:8,padding:'6px 10px',borderRadius:8,background:'color-mix(in srgb, #f39c12 10%, transparent)',color:'#c87f10'}}>
            ℹ️ 当前榜单受目标站反爬限制，为平台精选快照
          </div>
        )}

        {/* 2. 榜单类型 & 频道：两列 title 对齐；手机端单列两行 label 仍同宽 对齐好看 (#4 标题与内容均匀对齐) */}
        <div className="nr-filter-section nr-grid-2col" style={{
          marginTop:14, display:'grid',gap:'10px 18px',
          gridTemplateColumns:'1fr 1fr',
        }}>
          {/* 左：榜单类型 */}
          <div className="nr-filter-row nr-row-rank-type nr-row-aligned"
               style={{display:'flex',gap:8,alignItems:'flex-start'}}>
            <span className="filter-label nr-filter-label nr-label-fixed"
                  style={{fontSize:13,color:'var(--text-muted)',fontWeight:600,
                          minWidth:72,width:72,flex:'0 0 72px',paddingTop:6,
                          textAlign:'right',paddingRight:8,boxSizing:'border-box'}}>榜单类型</span>
            <div style={{flex:'1 1 0',minWidth:0,display:'flex',flexWrap:'wrap',gap:8,alignItems:'center'}}>
              {nrFiltersLoading && <span style={{fontSize:12,color:'var(--text-muted)'}}>加载中…</span>}
              {!nrFiltersLoading && (!nrFilters?.rankTypes?.length) && <span style={{fontSize:12,color:'var(--text-muted)'}}>暂无类型</span>}
              {!nrFiltersLoading && nrFilters?.rankTypes?.map((rt: NRRankType) => {
                const active = nrRankType === rt.value;
                return (
                  <span
                    key={rt.value}
                    className={`filter-tag nr-filter-tag ${active ? 'filter-tag-active nr-filter-tag-active' : ''}`}
                    onClick={() => setNrRankType(rt.value)}
                    title={rt.label}
                    style={{
                      display:'inline-flex',alignItems:'center',justifyContent:'center', cursor:'pointer',
                      padding: '6px 14px', borderRadius: 999,
                      background: active ? 'var(--accent-light)' : 'var(--bg-tertiary)',
                      border: active ? '1px solid #d0d5dd' : '1px solid var(--border-color)',
                      color: active ? 'var(--accent)' : 'var(--text-secondary)',
                      fontWeight: active ? 700 : 500, fontSize: 13, lineHeight: 1.35,
                      whiteSpace: 'nowrap', transition:'all .15s ease-in-out', flexShrink:0,
                      boxShadow: active ? '0 2px 6px color-mix(in srgb, var(--accent) 25%, transparent)' : 'none',
                    }}
                  >{rt.label}</span>
                );
              })}
            </div>
          </div>
          {/* 右：频道（男频/女频） */}
          <div className="nr-filter-row nr-row-gender nr-row-aligned"
               style={{display:'flex',gap:8,alignItems:'flex-start'}}>
            <span className="filter-label nr-filter-label nr-label-fixed"
                  style={{fontSize:13,color:'var(--text-muted)',fontWeight:600,
                          minWidth:72,width:72,flex:'0 0 72px',paddingTop:6,
                          textAlign:'right',paddingRight:8,boxSizing:'border-box'}}>频道</span>
            <div style={{flex:'1 1 0',minWidth:0,display:'flex',flexWrap:'wrap',gap:8,alignItems:'center'}}>
              <span
                className={`filter-tag nr-filter-tag ${(!nrFilters?.genders?.length || nrFilters.genders.includes('male')) && nrGender !== 'female' ? 'filter-tag-active nr-filter-tag-active' : ''}`}
                onClick={() => setNrGender(nrGender === 'male' ? '' : 'male')}
                style={{
                  display:'inline-flex',alignItems:'center',justifyContent:'center',cursor:'pointer',
                  padding:'6px 14px',borderRadius:999,
                  background: ((!nrFilters?.genders?.length || nrFilters.genders.includes('male')) && nrGender !== 'female')
                    ? 'var(--accent-light)' : 'var(--bg-tertiary)',
                  border: '1px solid var(--border-color)',
                  color: ((!nrFilters?.genders?.length || nrFilters.genders.includes('male')) && nrGender !== 'female')
                    ? 'var(--accent)' : 'var(--text-secondary)',
                  fontWeight: ((!nrFilters?.genders?.length || nrFilters.genders.includes('male')) && nrGender !== 'female') ? 700 : 500,
                  fontSize: 13, flexShrink:0, transition:'all .15s ease-in-out',
                }}
              >男频</span>
              {(!nrFilters?.genders?.length || nrFilters.genders.includes('female')) && (
                <span
                  className={`filter-tag nr-filter-tag ${nrGender === 'female' ? 'filter-tag-active nr-filter-tag-active' : ''}`}
                  onClick={() => setNrGender(nrGender === 'female' ? '' : 'female')}
                  style={{
                    display:'inline-flex',alignItems:'center',justifyContent:'center',cursor:'pointer',
                    padding:'6px 14px',borderRadius:999,
                    background: nrGender === 'female'
                      ? 'var(--accent-light)' : 'var(--bg-tertiary)',
                    border: '1px solid var(--border-color)',
                    color: nrGender === 'female' ? 'var(--accent)' : 'var(--text-secondary)',
                    fontWeight: nrGender === 'female' ? 700 : 500,
                    fontSize: 13, flexShrink:0, transition:'all .15s ease-in-out',
                  }}
                >女频</span>
              )}
            </div>
          </div>
        </div>

        {/* 3. 主题分类：美化排版 + 手机端右侧折叠按钮收起分类网格 */}
        <div className="nr-row-category" style={{marginTop:14, display:'flex',gap:8, alignItems:'flex-start'}}>
          <span className="filter-label nr-filter-label nr-label-fixed nr-category-label-row"
                style={{
                  // 桌面端：label 固定宽 72px 右对齐；手机端：100% 宽左右布局（折叠按钮在右侧）
                  display:'flex', justifyContent:'space-between', alignItems:'center',
                  fontSize:13,color:'var(--text-muted)',fontWeight:600,
                  minWidth:72,width:'100%',flex:'0 0 100%',paddingTop:6,
                  textAlign:'left',paddingRight:8,boxSizing:'border-box'
                }}>
            <span style={{display:'inline-block'}}>主题分类</span>
            <button
              type="button"
              className="nr-category-collapse-toggle"
              onClick={() => setNrCategoryCollapsed(v => !v)}
              aria-expanded={!nrCategoryCollapsed}
              style={{
                // 手机端显示；桌面端默认隐藏
                display:'inline-flex', alignItems:'center', justifyContent:'center',
                fontSize:11.5, fontWeight:600,
                padding:'2px 10px', borderRadius: 999,
                background:'color-mix(in srgb, var(--accent) 10%, transparent)',
                color:'var(--accent)',
                border:'1px solid color-mix(in srgb, var(--accent) 35%, transparent)',
                whiteSpace:'nowrap', cursor:'pointer', lineHeight:1.2,
                flexShrink:0,
              }}
            >{nrCategoryCollapsed ? '展开 ▽' : '收起 △'}</button>
          </span>
          <div className={`tags-content nr-category-tags nr-category-body ${nrCategoryCollapsed ? 'is-collapsed' : ''}`}
               style={{
                 flex:'1 1 0',minWidth:0,
                 display:'grid',gridTemplateColumns:'repeat(auto-fill, minmax(86px,1fr))',
                 gap:'8px 8px',
                 transition:'max-height .35s ease, opacity .25s ease, margin .25s ease',
                 overflow:'hidden',
               }}>
            {nrFiltersLoading && <span style={{fontSize:12,color:'var(--text-muted)',gridColumn:'1 / -1'}}>加载分类中…</span>}
            {!nrFiltersLoading && (!nrFilters?.categories?.length) && <span style={{fontSize:12,color:'var(--text-muted)',gridColumn:'1 / -1'}}>暂无分类</span>}
            {!nrFiltersLoading && nrFilters?.categories?.map((c: NRCategory) => {
              const active = nrCategoryCode === c.code;
              return (
                <button
                  type="button"
                  key={c.id}
                  className={`filter-tag nr-filter-tag nr-category-pill ${active ? 'filter-tag-active nr-filter-tag-active' : ''}`}
                  onClick={() => { setNrCategoryCode(c.code); setNrSubCategoryCode(''); }}
                  title={c.scope==='all'?'平台总榜':`分类榜：${c.name}`}
                  style={{
                    display:'inline-flex',alignItems:'center',justifyContent:'center',cursor:'pointer',
                    padding:'7px 10px',borderRadius:999, minHeight:30,
                    background: active ? 'var(--accent-light)' : 'var(--bg-tertiary)',
                    border: active ? '1px solid #d0d5dd' : '1px solid var(--border-color)',
                    color: active ? 'var(--accent)' : 'var(--text-secondary)',
                    fontWeight: active ? 700 : 500, fontSize: 12.5, transition:'all .15s ease-in-out',
                    whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis',
                  }}
                >
                  {c.name || c.code}
                </button>
              );
            })}
          </div>
        </div>

        {/* 主题子类（起点等二级分类） — 跟主题分类网格一起折叠 */}
        <div className={`nr-category-body nr-sub-wrap ${nrCategoryCollapsed ? 'is-collapsed' : ''}`}
             style={{transition:'max-height .35s ease, opacity .25s ease, margin .25s ease',overflow:'hidden'}}>
        {(() => {
          const subs = (nrFilters?.subcategories || []).filter((s: NRCategory) => s.parentCode === nrCategoryCode);
          if (!subs.length) return null;
          return (
            <div className="nr-row-sub" style={{marginTop:10,display:'flex',gap:8,alignItems:'flex-start'}}>
              <span className="filter-label nr-filter-label nr-label-fixed"
                    style={{fontSize:13,color:'var(--text-muted)',fontWeight:600,
                            minWidth:72,width:72,flex:'0 0 72px',paddingTop:6,
                            textAlign:'right',paddingRight:8,boxSizing:'border-box'}}>主题细化</span>
              <div className="tags-content nr-sub-tags"
                   style={{flex:'1 1 0',minWidth:0,display:'grid',
                           gridTemplateColumns:'repeat(auto-fill, minmax(86px,1fr))',gap:'8px 8px'}}>
                <button
                  type="button"
                  className={`filter-tag nr-filter-tag nr-category-pill ${!nrSubCategoryCode ? 'filter-tag-active nr-filter-tag-active' : ''}`}
                  onClick={() => setNrSubCategoryCode('')}
                  style={{
                    display:'inline-flex',alignItems:'center',justifyContent:'center',cursor:'pointer',
                    padding:'7px 10px',borderRadius:999, minHeight:30,
                    background: !nrSubCategoryCode ? 'var(--accent-light)' : 'var(--bg-tertiary)',
                    border: !nrSubCategoryCode ? '1px solid #d0d5dd' : '1px solid var(--border-color)',
                    color: !nrSubCategoryCode ? 'var(--accent)' : 'var(--text-secondary)',
                    fontWeight: !nrSubCategoryCode ? 700 : 500, fontSize: 12.5,
                    transition:'all .15s ease-in-out', whiteSpace:'nowrap',
                  }}
                  title="全部该分类下的小说"
                >全部</button>
                {subs.map((s: NRCategory) => {
                  const active = nrSubCategoryCode === s.code;
                  return (
                    <button
                      type="button"
                      key={s.id}
                      className={`filter-tag nr-filter-tag nr-category-pill ${active ? 'filter-tag-active nr-filter-tag-active' : ''}`}
                      onClick={() => setNrSubCategoryCode(s.code === nrSubCategoryCode ? '' : s.code)}
                      title={`主题分类：${s.name}`}
                      style={{
                        display:'inline-flex',alignItems:'center',justifyContent:'center',cursor:'pointer',
                        padding:'7px 10px',borderRadius:999,minHeight:30,
                        background: active ? 'var(--accent-light)' : 'var(--bg-tertiary)',
                        border: active ? '1px solid transparent' : '1px solid var(--border-color)',
                        color: active ? '#fff' : 'var(--text-secondary)',
                        fontWeight: active ? 700 : 500, fontSize: 12.5,
                        transition:'all .15s ease-in-out', whiteSpace:'nowrap',
                        overflow:'hidden',textOverflow:'ellipsis',
                      }}
                    >{s.name || s.code}</button>
                  );
                })}
              </div>
            </div>
          );
        })()}
        </div>
      </div>

      {/* Banner：热门标签 / 上升关键词（#4 手机端各一行；桌面端两栏并排） */}
      {rankBanner && (
        <div className="nr-banner rank-result" style={{marginTop:4,marginBottom:12}}>
          <div className="nr-banner-grid"
               style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:10}}>
            <div className="rank-block nr-banner-hot nr-banner-row" style={{margin:0}}>
              <h4 style={{fontSize:13,margin:'0 0 8px',display:'flex',alignItems:'center',gap:6,color:'var(--text-secondary)'}}>
                🔥 热门标签
              </h4>
              <div className="nr-tags-line"
                   style={{display:'flex',flexWrap:'wrap',gap:6}}>
                {rankBanner.hot_tags.map((t,i) => (
                  <span key={i} style={{
                    padding:'4px 10px',borderRadius:999,
                    background:'var(--bg-tertiary)',
                    border:'1px solid var(--border-color)',
                    fontSize:12,color:'var(--text-secondary)',
                    whiteSpace:'nowrap',flexShrink:0,
                  }}>{t}</span>
                ))}
              </div>
            </div>
            <div className="rank-block nr-banner-rising nr-banner-row" style={{margin:0}}>
              <h4 style={{fontSize:13,margin:'0 0 8px',display:'flex',alignItems:'center',gap:6,color:'var(--text-secondary)'}}>
                🚀 上升关键词
              </h4>
              <div className="nr-tags-line"
                   style={{display:'flex',flexWrap:'wrap',gap:6}}>
                {rankBanner.rising_keywords.map((k,i) => (
                  <span key={i} style={{
                    padding:'4px 10px',borderRadius:999,
                    background:'color-mix(in srgb, var(--accent) 12%, transparent)',
                    border:'1px solid color-mix(in srgb, var(--accent) 40%, transparent)',
                    fontSize:12,color:'var(--accent)',fontWeight:600,
                    whiteSpace:'nowrap',flexShrink:0,
                  }}>{k}</span>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 提示/错误/加载 */}
      {nrList?.fetchError && (
        <div style={{margin:'0 0 10px',padding:'8px 10px',background:'#fdecec',borderRadius:6,fontSize:12,color:'#c0392b'}}>
          ⚠️ 抓取失败：{nrList.fetchError}
          {nrList.sourceKind === 'curated' && !nrList.items.length ? '。已尝试平台精选兜底。' : ''}
        </div>
      )}
      {nrListError && !nrList?.fetchError && (
        <div style={{margin:'0 0 10px',padding:'8px 10px',background:'#fdecec',borderRadius:6,fontSize:12,color:'#c0392b'}}>
          ⚠️ {nrListError}
        </div>
      )}
      {nrListLoading && (
        <div style={{textAlign:'center',padding:30,color:'var(--text-muted)',fontSize:13}}>⏳ 加载榜单书籍…</div>
      )}

      {/* 书籍列表：双套布局（移动端卡片 + 桌面端表格）互斥显示 */}
      {!nrListLoading && nrList?.items?.length ? (
        <>
          {/* 移动端卡片（仅手机端显示）— 手机端一行一卡，minWidth:0 max-width:100% 强制不溢出 (#2 根治横溢) */}
          <div className="nr-books nr-mobile-books rank-card-grid nr-mobile-only">
            {nrList.items.map((b: any, i: number) => {
              const change = Number(b.rankChange) || 0;
              const n = Number(b.rankNo) || i + 1;
              const cat = [b.categoryName, b.categorySubName].filter(Boolean).join(' / ');
              const rawMetric = b.metricText ?? b.metricValue;
              const metricText = (rawMetric == null || rawMetric === '' || rawMetric === 0) ? '—' : String(rawMetric);
              return (
                <a
                  key={i}
                  href={b.bookUrl || undefined}
                  target="_blank"
                  rel="noreferrer"
                  className="rank-card nr-mobile-card"
                  style={{
                    padding:'12px', gap:8, display:'flex',flexDirection:'column',
                    background:'var(--bg-secondary)', border:'1px solid var(--border-color)',
                    borderRadius:12, textDecoration:'none', color:'inherit',
                    width:'100%',boxSizing:'border-box',minWidth:0,maxWidth:'100%',overflow:'hidden',
                  }}
                >
                  <div className="rank-card-head" style={{display:'flex',gap:8,alignItems:'center',flexWrap:'wrap',width:'100%',maxWidth:'100%',minWidth:0}}>
                    {n <= 3
                      ? <span className={`rank-medal r${n}`} style={{
                          width:22,height:22,borderRadius:6,display:'inline-flex',
                          alignItems:'center',justifyContent:'center',fontSize:12,fontWeight:800,color:'#fff',flexShrink:0,
                          background: n===1 ? 'linear-gradient(135deg,#f6c453,#e67e22)' : n===2 ? 'linear-gradient(135deg,#cfd8dc,#90a4ae)' : 'linear-gradient(135deg,#f0a986,#c7792b)',
                        }}>{n}</span>
                      : <span className="rank-no" style={{minWidth:22,fontSize:14,fontWeight:800,color:'var(--text-muted)',flexShrink:0}}>{n}</span>}
                    <span className={`rank-delta ${change >= 0 ? 'up' : 'down'}`} style={{fontSize:11,fontWeight:700,color: change>=0?'#22a06b':'#c0392b'}}>
                      {change > 0 ? `↑${change}` : change < 0 ? `↓${-change}` : '—'}
                    </span>
                    {b.statusText && <span className="rank-status" style={{
                      marginLeft:'auto',fontSize:10,padding:'2px 8px',borderRadius:999,
                      background:'color-mix(in srgb, var(--accent) 12%, transparent)',color:'var(--accent)',
                      whiteSpace:'nowrap',flexShrink:0,
                    }}>{b.statusText}</span>}
                  </div>
                  <div className="rank-card-main" style={{display:'flex',gap:10,minWidth:0,width:'100%',maxWidth:'100%',boxSizing:'border-box'}}>
                    {b.coverUrl ? (
                      <img
                        className="rank-cover"
                        src={b.coverUrl}
                        alt=""
                        loading="lazy"
                        style={{
                          width:52,height:70,borderRadius:8,flexShrink:0,
                          objectFit:'cover',background:'var(--bg-tertiary)',
                        }}
                      />
                    ) : (
                      <div className="rank-cover-fallback" style={{
                        width:52,height:70,borderRadius:8,flexShrink:0,
                        background:'linear-gradient(135deg,var(--accent-light),var(--bg-tertiary))',
                        display:'flex',alignItems:'center',justifyContent:'center',fontSize:24,color:'var(--accent)',
                      }}>📚</div>
                    )}
                    <div className="rank-book-info" style={{flex:'1 1 0',minWidth:0,display:'flex',flexDirection:'column',gap:4,maxWidth:'calc(100% - 62px)'}}>
                      <div className="rank-card-title" style={{
                        fontSize:14.5,fontWeight:700,color:'var(--text-primary)',lineHeight:1.35,
                        width:'100%',minWidth:0,maxWidth:'100%',wordBreak:'break-word',
                        display:'-webkit-box',WebkitLineClamp:2,WebkitBoxOrient:'vertical',overflow:'hidden',
                      }}>{b.bookTitle || '未命名'}</div>
                      {b.intro && <p className="rank-card-desc" style={{
                        fontSize:11.5,color:'var(--text-muted)',lineHeight:1.5,margin:0,
                        display:'-webkit-box',WebkitLineClamp:2,WebkitBoxOrient:'vertical',overflow:'hidden',
                        width:'100%',minWidth:0,maxWidth:'100%',wordBreak:'break-word',
                      }}>{b.intro}</p>}
                      {b.lastChapterTitle && (
                        <div className="rank-card-sub" style={{
                          fontSize:11,color:'var(--text-muted)',
                          width:'100%',minWidth:0,maxWidth:'100%',
                          overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',
                          wordBreak:'break-all',
                        }}>📝 {b.lastChapterTitle}{b.lastUpdateTimeText ? ` · ${b.lastUpdateTimeText}` : ''}</div>
                      )}
                    </div>
                  </div>
                  <div className="rank-card-meta" style={{
                    display:'flex',flexWrap:'wrap',gap:'6px 10px',
                    fontSize:11,color:'var(--text-secondary)',
                    paddingTop:6,borderTop:'1px dashed var(--border-color)',
                    width:'100%',boxSizing:'border-box',minWidth:0,maxWidth:'100%',
                  }}>
                    <span style={{minWidth:0,maxWidth:'100%',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',wordBreak:'break-all'}}>
                      <b style={{color:'var(--text-muted)',fontWeight:500}}>作者</b> {b.authorName || '—'}
                    </span>
                    {cat && <span style={{minWidth:0,maxWidth:'100%',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',wordBreak:'break-all'}}>
                      <b style={{color:'var(--text-muted)',fontWeight:500}}>分类</b> {cat}
                    </span>}
                    {metricText !== '—' && (
                      <span className="rank-metric" style={{marginLeft:'auto',flexShrink:0,minWidth:'auto',maxWidth:'100%',overflow:'hidden'}}>
                        <em style={{
                          fontStyle:'normal',display:'inline-flex',alignItems:'center',gap:4,
                          fontSize:11.5,fontWeight:700,color:'var(--accent)',padding:'3px 10px',borderRadius:999,
                          background:'var(--accent-light)',
                          border:'1px solid #d0d5dd',
                        }}>{b.metricName ? `${b.metricName} ` : ''}{metricText}</em>
                      </span>
                    )}
                  </div>
                </a>
              );
            })}
          </div>

          {/* 桌面端表格（仅 ≥768px 显示，移动端隐藏） */}
          <div className="nr-books nr-desktop-books">
            <table className="rank-books-table nr-desktop-table" style={{
              width:'100%', borderCollapse:'separate',borderSpacing:0,
              fontSize:13.5, background:'var(--bg-secondary)',
              border:'1px solid var(--border-color)',
              borderRadius:12, overflow:'hidden',
              tableLayout:'fixed',
            }}>
              <thead>
                <tr style={{background:'linear-gradient(180deg, var(--bg-tertiary), color-mix(in srgb, var(--bg-tertiary) 60%, var(--bg-secondary)))', color:'var(--text-muted)'}}>
                  <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,width:62,fontSize:12.5}}>#</th>
                  <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,width:56,fontSize:12.5}}>封面</th>
                  <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,fontSize:12.5}}>作品信息</th>
                  <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,width:120,fontSize:12.5}}>作者</th>
                  <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,width:140,fontSize:12.5}}>分类</th>
                  <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,width:120,fontSize:12.5}}>{nrList?.items[0]?.metricName || '指标'}</th>
                  <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,width:90,fontSize:12.5}}>状态</th>
                </tr>
              </thead>
              <tbody>
                {nrList.items.map((b: NRItem, i: number) => {
                  const change = Number(b.rankChange) || 0;
                  const n = Number(b.rankNo) || i + 1;
                  return (
                    <tr key={i} style={{borderTop:'1px solid var(--border-color)', transition:'background .15s'}} className="nr-table-row">
                      <td style={{padding:'10px 12px',verticalAlign:'top',fontSize:13,
                                 fontWeight: n<=3 ? 800 : 600,
                                 color: n<=3 ? '#e67e22' : 'var(--text-secondary)'}}>
                        <div style={{display:'inline-flex',alignItems:'center',gap:6,flexWrap:'wrap'}}>
                          {n <= 3 ? (
                            <span style={{
                              width:24,height:24,borderRadius:6,
                              display:'inline-flex',alignItems:'center',justifyContent:'center',
                              fontSize:12.5,fontWeight:800,color:'#fff',
                              background: n===1 ? 'linear-gradient(135deg,#f6c453,#e67e22)' : n===2 ? 'linear-gradient(135deg,#cfd8dc,#90a4ae)' : 'linear-gradient(135deg,#f0a986,#c7792b)',
                            }}>{n}</span>
                          ) : n}
                          {change > 0 && <span style={{color:'#22a06b',fontSize:11.5,fontWeight:700}}>↑{change}</span>}
                          {change < 0 && <span style={{color:'#c0392b',fontSize:11.5,fontWeight:700}}>↓{-change}</span>}
                        </div>
                      </td>
                      <td style={{padding:'10px 12px',verticalAlign:'top'}}>
                        {b.coverUrl ? (
                          <img src={b.coverUrl} alt="" loading="lazy"
                            style={{width:40,height:54,borderRadius:6,objectFit:'cover',
                                    boxShadow:'0 2px 6px rgba(0,0,0,.08)',background:'var(--bg-tertiary)'}}/>
                        ) : (
                          <div style={{width:40,height:54,borderRadius:6,
                                      background:'linear-gradient(135deg,var(--accent-light),var(--bg-tertiary))',
                                      display:'flex',alignItems:'center',justifyContent:'center',fontSize:18,color:'var(--accent)',
                                      boxShadow:'0 2px 6px rgba(0,0,0,.04)'}}>📚</div>
                        )}
                      </td>
                      <td style={{padding:'10px 12px',verticalAlign:'top',minWidth:0,overflow:'hidden'}}>
                        <div style={{display:'flex',flexDirection:'column',gap:4,minWidth:0,width:'100%'}}>
                          {b.bookUrl ? (
                            <a href={b.bookUrl} target="_blank" rel="noreferrer"
                              style={{fontSize:14.5,fontWeight:700,color:'var(--text-primary)',
                                      textDecoration:'none',transition:'color .15s',
                                      overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',minWidth:0,display:'block',
                                      wordBreak:'break-word',
                              }}
                              onMouseEnter={e => e.currentTarget.style.color='var(--accent)'}
                              onMouseLeave={e => e.currentTarget.style.color='var(--text-primary)'}
                            >{b.bookTitle}</a>
                          ) : <span style={{fontWeight:700,color:'var(--text-primary)',fontSize:14.5,
                                           overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',display:'block'}}>{b.bookTitle}</span>}
                          {b.intro && <div style={{
                            fontSize:12,color:'var(--text-muted)',lineHeight:1.5,
                            width:'100%',minWidth:0,wordBreak:'break-word',
                            display:'-webkit-box',WebkitLineClamp:2,WebkitBoxOrient:'vertical',overflow:'hidden',
                          }}>{b.intro}</div>}
                          {b.lastChapterTitle && <div style={{
                            fontSize:12,color:'var(--text-secondary)',
                            width:'100%',minWidth:0,
                            overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',
                          }}>📝 {b.lastChapterTitle}{b.lastUpdateTimeText ? ` · ${b.lastUpdateTimeText}` : ''}</div>}
                        </div>
                      </td>
                      <td style={{padding:'10px 12px',verticalAlign:'top',
                                 color:'var(--text-secondary)',fontSize:12.5,
                                 overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                        {b.authorName || '—'}
                      </td>
                      <td style={{padding:'10px 12px',verticalAlign:'top',
                                 color:'var(--text-secondary)',fontSize:12.5,
                                 overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                        {[b.categoryName, b.categorySubName].filter(Boolean).join(' / ') || '—'}
                      </td>
                      <td style={{padding:'10px 12px',verticalAlign:'top',fontSize:12.5,fontWeight:800,color:'var(--accent)'}}>
                        {(b.metricText ?? (b.metricValue ?? 0)) || '—'}
                      </td>
                      <td style={{padding:'10px 12px',verticalAlign:'top',
                                 color:'var(--text-secondary)',fontSize:12.5,
                                 overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                        {b.statusText || '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* 分页：全部改用 triggerNrFetch（#6）；分页不再直接 loadNrBooks，走 nrFetchKey 触发，保证抓取策略一致 */}
          <div className="nr-pagination" style={{
            display:'flex',justifyContent:'space-between',alignItems:'center',
            marginTop:12,gap:10,flexWrap:'wrap',
            padding:'8px 10px',
            background:'var(--bg-secondary)',
            border:'1px solid var(--border-color)',
            borderRadius:10,
            width:'100%',boxSizing:'border-box',minWidth:0,
          }}>
            <span style={{fontSize:12.5,color:'var(--text-muted)',minWidth:0,flex:'1 1 auto',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
              📖 第 <b style={{color:'var(--accent)'}}>{nrList.page}</b> 页 · 共 <b style={{color:'var(--accent)'}}>{nrList.total}</b> 条线索
            </span>
            <div style={{display:'flex',gap:6,flex:'0 0 auto',flexWrap:'wrap'}}>
              <button
                className="chat-send ghost"
                onClick={() => {const p = Math.max(1,nrList.page-1); setNrPage(p); triggerNrFetch(false); }}
                disabled={nrList.page <= 1 || nrListLoading || nrCrawling}
                style={{padding:'6px 14px',minHeight:30,fontSize:12.5,borderRadius:8,
                        background: nrList.page<=1||nrListLoading ? 'var(--bg-tertiary)' : 'var(--bg-secondary)',
                        color: nrList.page<=1||nrListLoading ? 'var(--text-muted)' : 'var(--text-secondary)',
                        border:'1px solid var(--border-color)',cursor: nrList.page<=1||nrListLoading ? 'not-allowed' : 'pointer',
                }}
              >← 上一页</button>
              <button
                className="chat-send primary"
                onClick={() => {const p = nrList.page + 1; setNrPage(p); triggerNrFetch(false); }}
                disabled={(nrList.page * (nrList.pageSize||50)) >= (nrList.total || 0) || nrListLoading || nrCrawling}
                style={{padding:'6px 14px',minHeight:30,fontSize:12.5,borderRadius:8,fontWeight:700,
                        background: 'var(--accent-light)',
                        color: 'var(--accent)',border:'1px solid #d0d5dd',
                }}
              >下一页 →</button>
            </div>
          </div>
        </>
      ) : null}

      {!nrListLoading && nrList && (nrList.total ?? 0) === 0 && (
        <div style={{textAlign:'center',padding:30,color:'var(--text-muted)',fontSize:13,background:'var(--bg-secondary)',border:'1px dashed var(--border-color)',borderRadius:8}}>
          暂无线索 {nrKeyword ? `（关键词「${nrKeyword}」无匹配）` : '——请切换分类或手动点击☁️ 抓取本榜重试'}
        </div>
      )}
    </div>
  );
}