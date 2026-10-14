import { useState, useEffect } from 'react';
import { api } from '../api';
import type { Book } from '../types';
import Icon from '../components/Icon';
import { useIsMobile } from '../hooks/useMobileUI';
import { GENRES, GENRE_GROUPS, normalizeGenreKey } from '../constants';
import ReviewTab from './tools/ReviewTab';
import SkillsTab from './tools/SkillsTab';
import AnalyzeTab from './tools/AnalyzeTab';
import RankingsTab, { type RankContext, type RankAnalyzeTarget } from './tools/RankingsTab';

type ToolTab = 'review' | 'skills' | 'analyze' | 'rankings';

const TOOL_TABS: { key: ToolTab; label: string; icon: string; desc: string }[] = [
  { key: 'review', label: 'AI 责编', icon: 'search', desc: 'AI平台视角审稿打分' },
  { key: 'skills', label: '技能包', icon: 'package', desc: '15+题材工作流套件' },
  { key: 'analyze', label: '拆书分析', icon: 'bar-chart', desc: '导入文件分析提炼方法论' },
  { key: 'rankings', label: '榜单风向', icon: 'trending-up', desc: '各平台排行榜趋势洞察' },
];

/** 榜单分类名（如「东方仙侠」「都市高武」「宫斗宅斗」）→ 作品题材 key 的子串匹配兜底 */
const CATEGORY_KEYWORDS: [string, string][] = [
  ['仙侠', 'xianxia'], ['修真', 'xianxia'],
  ['玄幻', 'fantasy'],
  ['奇幻', 'qihuan'],
  ['武侠', 'wuxia'],
  ['职场', 'urban_business'], ['商战', 'urban_business'],
  ['都市异能', 'urban_fantasy'], ['异能', 'urban_fantasy'],
  ['都市', 'urban'],
  ['历史', 'history'], ['架空', 'history'],
  ['军事', 'military'], ['抗战', 'military'],
  ['游戏', 'game'], ['电竞', 'game'],
  ['体育', 'sports'], ['竞技', 'sports'],
  ['科幻', 'scifi'], ['末世', 'scifi'],
  ['悬疑', 'mystery'], ['推理', 'mystery'], ['灵异', 'mystery'], ['脑洞', 'mystery'],
  ['诸天', 'infinite'], ['无限', 'infinite'],
  ['二次元', 'acg'], ['同人', 'acg'], ['衍生', 'acg'], ['动漫', 'acg'],
  ['古言', 'ancient_romance'], ['宫斗', 'ancient_romance'], ['宅斗', 'ancient_romance'], ['民国', 'ancient_romance'],
  ['幻言', 'fantasy_romance'], ['言情', 'romance'], ['甜宠', 'romance'], ['豪门', 'romance'], ['职场婚', 'romance'],
];
function guessGenreFromCategory(categoryName?: string, subName?: string): string {
  const text = `${categoryName || ''}${subName || ''}`;
  const direct = normalizeGenreKey(text);
  if (direct !== 'other') return direct;
  for (const [kw, key] of CATEGORY_KEYWORDS) {
    if (text.includes(kw)) return key;
  }
  return 'other';
}

const NEW_BOOK_VALUE = '__new__';

export default function ToolsPage() {
  const [activeTab, setActiveTab] = useState<ToolTab>('review');
  const [books, setBooks] = useState<Book[]>([]);
  const [selectedBookId, setSelectedBookId] = useState('');
  // #3 移动端自动折叠：工具箱 + 选择作品 在 activeTab=rankings 时收起
  const [toolsCollapsedMobile, setToolsCollapsedMobile] = useState<boolean | null>(null); // null=未初始化
  const isMobile = useIsMobile();

  // 轻量「新建作品」弹窗（工具页内联版：书名/作者/类型/题材/简介）
  const [showNewBook, setShowNewBook] = useState(false);
  const [creating, setCreating] = useState(false);
  const [nbForm, setNbForm] = useState({ title: '', author: '', genre: 'other', book_type: 'novel', synopsis: '' });
  // 榜单风向实时上报的当前赛道上下文（用于新建预填 + 随作品 metadata 落库）
  const [rankCtx, setRankCtx] = useState<RankContext | null>(null);
  // 榜单书 → 一键拆书：从榜单书行携带元数据跳到拆书分析 Tab
  const [pendingRankBook, setPendingRankBook] = useState<RankAnalyzeTarget | null>(null);

  // 工具类型 Tab 切换 → 移动端自动展开/折叠工具箱与作品选择
  useEffect(() => {
    if (!isMobile) { setToolsCollapsedMobile(false); return; }
    if (activeTab === 'rankings') {
      // 需求 3：手机端点击榜单风向，选择作品上面的（工具箱 + 选择作品）自动折叠
      setToolsCollapsedMobile(true);
    } else if (toolsCollapsedMobile !== false) {
      setToolsCollapsedMobile(false);
    }
  }, [activeTab, isMobile]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    api.listBooks().then(setBooks).catch(() => {});
  }, []);

  function refreshBooks() {
    return api.listBooks().then(setBooks).catch(() => {});
  }

  function openNewBookModal() {
    // 榜单风向入口：按当前榜单赛道预填题材与类型，并在简介处给出参考提示
    const fromRank = activeTab === 'rankings' && rankCtx;
    const genre = fromRank
      ? guessGenreFromCategory(rankCtx!.categoryName, rankCtx!.subCategoryName)
      : 'other';
    const bookType = rankCtx?.gender === 'female' && genre === 'other' ? 'short_story' : 'novel';
    setNbForm({
      title: '',
      author: '',
      genre,
      book_type: bookType,
      synopsis: '',
    });
    setShowNewBook(true);
  }

  async function handleCreateBook() {
    const title = nbForm.title.trim();
    if (!title) { alert('请填写书名'); return; }
    setCreating(true);
    try {
      const b = await api.createBook(nbForm);
      // 榜单风向新建：把当时赛道上下文（平台/榜单/分类/热词/抓取时间）随作品 metadata 落库，
      // 作为后续「榜单→选题→拆书对标」工作流的溯源依据
      if (activeTab === 'rankings' && rankCtx) {
        try {
          await api.updateBook(b.id, { metadata_json: { nr_origin: rankCtx } } as any);
        } catch { /* 上下文落库失败不阻塞创建结果 */ }
      }
      await refreshBooks();
      setSelectedBookId(b.id);
      setShowNewBook(false);
      alert(activeTab === 'rankings' && rankCtx
        ? `《${b.title}》已创建，并按「${rankCtx!.platformName}${rankCtx!.categoryName ? '·' + rankCtx!.categoryName : ''}」赛道预选了题材，已自动选中`
        : `《${b.title}》已创建，已自动选中`);
    } catch (e: any) {
      if (e?.status === 402 || e?.data?.code === 'UPGRADE_REQUIRED') {
        alert(e?.data?.message || '开通网站永久会员即可无限创建新书（可在首页操作开通）');
      } else {
        alert('创建失败：' + (e?.message || '请稍后重试'));
      }
    } finally {
      setCreating(false);
    }
  }

  function handleSelectChange(v: string) {
    if (v === NEW_BOOK_VALUE) {
      openNewBookModal();
      return; // 选中值保持原作品，弹窗取消时 select 自然回退
    }
    setSelectedBookId(v);
  }

  const selectedBook = books.find(b => b.id === selectedBookId);

  return (
    <div className="page tools-page">
      <header className="page-header">
        <h1>工具箱</h1>
        <button
          type="button"
          className="nr-header-toggle"
          aria-expanded={!toolsCollapsedMobile}
          onClick={() => setToolsCollapsedMobile((v: boolean | null) => !(v === true))}
        >
          <Icon name={toolsCollapsedMobile ? 'chevron-down' : 'chevron-up'} size={16} />
          <span>{toolsCollapsedMobile ? '展开' : '收起'}</span>
        </button>
      </header>

      {/* #3 手机端：工具箱 + 选择作品 = 可折叠面板（进榜单风向时自动收起） */}
      <section className={`tools-top-section nr-collapsible ${toolsCollapsedMobile ? 'is-collapsed' : ''}`}
               data-collapsed={toolsCollapsedMobile ? '1' : '0'}>
        <div className="nr-collapsible-body">
          <div className="tools-grid">
            {TOOL_TABS.map(tab => (
              <button key={tab.key} className={`tool-card ${activeTab === tab.key ? 'active' : ''}`} onClick={() => setActiveTab(tab.key)}>
                <span className="tool-card-icon"><Icon name={tab.icon as any} size={28} /></span>
                <div className="tool-card-info">
                  <div className="tool-card-name">{tab.label}</div>
                  <div className="tool-card-desc">{tab.desc}</div>
                </div>
              </button>
            ))}
          </div>

          <div className="form-row nr-select-book-row" style={{marginTop:12}}>
            <label className="input-label">选择作品</label>
            <select className="input" value={selectedBookId} onChange={e => handleSelectChange(e.target.value)}>
              <option value={NEW_BOOK_VALUE}>＋ 新建作品…</option>
              <option value="">— 选择要操作的作品 —</option>
              {books.map(b => <option key={b.id} value={b.id}>{b.title} ({b.word_count}字)</option>)}
            </select>
          </div>
        </div>
      </section>

      {activeTab === 'review' && <ReviewTab selectedBookId={selectedBookId} />}
      {activeTab === 'skills' && <SkillsTab selectedBookId={selectedBookId} />}
      {activeTab === 'analyze' && (
        <AnalyzeTab
          books={books}
          selectedBookId={selectedBookId}
          rankBook={activeTab === 'analyze' ? pendingRankBook : null}
          onClearRankBook={() => setPendingRankBook(null)}
        />
      )}
      {activeTab === 'rankings' && (
        <RankingsTab
          selectedBookId={selectedBookId}
          selectedBookTitle={selectedBook?.title || ''}
          onContextChange={setRankCtx}
          onAnalyzeBook={(target) => { setPendingRankBook(target); setActiveTab('analyze'); }}
        />
      )}

      {/* 工具页·轻量新建作品弹窗（首项「＋ 新建作品…」触发） */}
      {showNewBook && (
        <div className="modal-overlay" onClick={() => !creating && setShowNewBook(false)}>
          <div className="modal" onClick={e => e.stopPropagation()} style={{maxWidth:460}}>
            <h2 style={{display:'flex',alignItems:'center',gap:8}}>
              <Icon name="book" size={18} /> 新建作品
            </h2>
            {activeTab === 'rankings' && rankCtx && (
              <div style={{
                fontSize:12, color:'var(--accent)', background:'var(--accent-light)',
                border:'1px solid color-mix(in srgb, var(--accent) 30%, transparent)',
                borderRadius:8, padding:'8px 10px', marginBottom:12, lineHeight:1.6,
              }}>
                <Icon name="trending-up" size={13} /> 将按当前榜单赛道创建：
                <b>{rankCtx.platformName}{rankCtx.rankTypeName ? ` · ${rankCtx.rankTypeName}` : ''}{rankCtx.categoryName ? ` · ${rankCtx.categoryName}` : ''}{rankCtx.subCategoryName ? ` / ${rankCtx.subCategoryName}` : ''}</b>
                {rankCtx.gender === 'male' ? '（男频）' : rankCtx.gender === 'female' ? '（女频）' : ''}
                ，题材已预选，可手动修改。
              </div>
            )}
            <div className="form-group">
              <label>书名 *</label>
              <input className="input" value={nbForm.title} autoFocus
                     onChange={e => setNbForm({ ...nbForm, title: e.target.value })}
                     onKeyDown={e => { if (e.key === 'Enter') handleCreateBook(); }}
                     placeholder="输入书名" />
            </div>
            <div className="form-group">
              <label>作者</label>
              <input className="input" value={nbForm.author}
                     onChange={e => setNbForm({ ...nbForm, author: e.target.value })}
                     placeholder="笔名（可留空）" />
            </div>
            <div style={{display:'flex',gap:8}}>
              <div className="form-group" style={{flex:1}}>
                <label>类型</label>
                <select className="input" value={nbForm.book_type}
                        onChange={e => setNbForm({ ...nbForm, book_type: e.target.value })}>
                  <option value="novel">长篇</option>
                  <option value="short_story">短篇</option>
                </select>
              </div>
              <div className="form-group" style={{flex:1}}>
                <label>题材</label>
                <select className="input" value={nbForm.genre}
                        onChange={e => setNbForm({ ...nbForm, genre: e.target.value })}>
                  {GENRE_GROUPS.map(g => (
                    <optgroup key={g.label} label={g.label}>
                      {g.keys.map(k => <option key={k} value={k}>{GENRES[k] || k}</option>)}
                    </optgroup>
                  ))}
                </select>
              </div>
            </div>
            <div className="form-group">
              <label>简介</label>
              <textarea className="input" rows={3} value={nbForm.synopsis}
                        onChange={e => setNbForm({ ...nbForm, synopsis: e.target.value })}
                        placeholder={
                          activeTab === 'rankings' && rankCtx
                            ? `可结合当前榜单热门标签（${(rankCtx.hotTags || []).slice(0, 5).join('、') || '暂无'}）写一句定位…`
                            : '简单描述故事定位（可留空）'
                        } />
            </div>
            <div className="form-actions">
              <button className="btn-secondary" disabled={creating} onClick={() => setShowNewBook(false)}>取消</button>
              <button className="btn-primary" disabled={creating} onClick={handleCreateBook}>
                {creating ? '创建中…' : '创建并选中'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
