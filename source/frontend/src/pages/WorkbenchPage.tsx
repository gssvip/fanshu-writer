import { useState, useEffect, useContext, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import { AuthContext } from '../App';
import type { Book, BookBible, SkillPack } from '../types';
import AiCreateModal from './AiCreateModal';
import { getStylesForGenre, filterStylesByGenre, getVolumeRange } from '../constants';

export default function WorkbenchPage() {
  const navigate = useNavigate();
  const { requireAuth } = useContext(AuthContext);
  const [books, setBooks] = useState<Book[]>([]);
  const [loading, setLoading] = useState(true);
  const [showNewBook, setShowNewBook] = useState(false);
  const [newBookForm, setNewBookForm] = useState({ title: '', genre: 'other', book_type: 'novel', synopsis: '', total_volumes: 0, novel_styles: [] as string[] });
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');

  const [todayStats, setTodayStats] = useState({ words: 0, chapters: 0 });
  const [streak, setStreak] = useState(0);
  const [recentBook, setRecentBook] = useState<Book | null>(null);

  // 导入作品
  const [showImport, setShowImport] = useState(false);
  const [importForm, setImportForm] = useState({ title: '', book_type: 'novel', genre: 'other' });
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState('');
  const [importFiles, setImportFiles] = useState<File[]>([]);
  const fileImportRef = useRef<HTMLInputElement>(null);
  const folderImportRef = useRef<HTMLInputElement>(null);

  // 使用说明书弹窗
  const [showManual, setShowManual] = useState(false);

  // 书架作品操作
  const [editBookId, setEditBookId] = useState('');
  const [editBookForm, setEditBookForm] = useState({ title: '', genre: 'other', book_type: 'novel', synopsis: '', total_volumes: 0, novel_styles: [] as string[] });
  const [editBookSaving, setEditBookSaving] = useState(false);

  // 总AI创作面板状态（与创作页 AiCreateModal 完全一致，仅多一步"选择作品"）
  const [masterCreatePacks, setMasterCreatePacks] = useState<SkillPack[]>([]);
  const [showMasterCreateModal, setShowMasterCreateModal] = useState(false); // 作品选择弹窗
  const [aiModalBook, setAiModalBook] = useState<Book | null>(null); // 选中的作品
  const [aiBible, setAiBible] = useState<BookBible | null>(null);
  const [aiModalOpen, setAiModalOpen] = useState(false);

  // 切换类型时重置卷数和风格（确保符合新类型的范围限制）—— 新建表单
  const handleNewBookTypeChange = (newType: string) => {
    const range = getVolumeRange(newType);
    setNewBookForm(prev => ({
      ...prev,
      book_type: newType,
      total_volumes: prev.total_volumes === 0 ? range.default : Math.max(range.min, prev.total_volumes),
      novel_styles: filterStylesByGenre(newType, prev.genre, prev.novel_styles),
    }));
  };

  // 切换题材时过滤掉新题材不支持的风格（题材-风格联动核心）—— 新建表单
  const handleNewGenreChange = (newGenre: string) => {
    setNewBookForm(prev => ({
      ...prev,
      genre: newGenre,
      novel_styles: filterStylesByGenre(prev.book_type, newGenre, prev.novel_styles),
    }));
  };

  // 切换类型时重置卷数和风格 —— 编辑表单
  const handleEditBookTypeChange = (newType: string) => {
    const range = getVolumeRange(newType);
    setEditBookForm(prev => ({
      ...prev,
      book_type: newType,
      total_volumes: prev.total_volumes === 0 ? range.default : Math.max(range.min, prev.total_volumes),
      novel_styles: filterStylesByGenre(newType, prev.genre, prev.novel_styles),
    }));
  };

  // 切换题材时过滤掉新题材不支持的风格 —— 编辑表单
  const handleEditGenreChange = (newGenre: string) => {
    setEditBookForm(prev => ({
      ...prev,
      genre: newGenre,
      novel_styles: filterStylesByGenre(prev.book_type, newGenre, prev.novel_styles),
    }));
  };

  // 风格多选切换（最多3种叠加）—— 新建表单
  const toggleNewStyle = (key: string) => {
    setNewBookForm(prev => {
      const has = prev.novel_styles.includes(key);
      if (has) return { ...prev, novel_styles: prev.novel_styles.filter(s => s !== key) };
      if (prev.novel_styles.length >= 3) return prev;
      return { ...prev, novel_styles: [...prev.novel_styles, key] };
    });
  };

  // 风格多选切换 —— 编辑表单
  const toggleEditStyle = (key: string) => {
    setEditBookForm(prev => {
      const has = prev.novel_styles.includes(key);
      if (has) return { ...prev, novel_styles: prev.novel_styles.filter(s => s !== key) };
      if (prev.novel_styles.length >= 3) return prev;
      return { ...prev, novel_styles: [...prev.novel_styles, key] };
    });
  };

  async function handleRenameBook(book: Book) {
    setEditBookId(book.id);
    setEditBookForm({
      title: book.title,
      genre: book.genre || 'other',
      book_type: book.book_type || 'short_story',
      synopsis: book.synopsis || '',
      total_volumes: book.total_volumes || 0,
      novel_styles: Array.isArray(book.novel_styles) ? book.novel_styles : [],
    });
  }

  async function handleSaveEditBook() {
    if (!editBookId || !editBookForm.title.trim()) return;
    setEditBookSaving(true);
    try {
      const updated = await api.updateBook(editBookId, {
        title: editBookForm.title.trim(),
        genre: editBookForm.genre,
        book_type: editBookForm.book_type,
        synopsis: editBookForm.synopsis,
        total_volumes: editBookForm.total_volumes,
        novel_styles: editBookForm.novel_styles,
      });
      setBooks(prev => prev.map(b => b.id === updated.id ? updated : b));
      setEditBookId('');
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setEditBookSaving(false);
  }

  async function handleDeleteBook(bookId: string) {
    if (!confirm('确定删除这部作品？所有章节和设定将永久丢失，此操作不可撤销。')) return;
    try {
      await api.deleteBook(bookId);
      setBooks(prev => prev.filter(b => b.id !== bookId));
      if (recentBook?.id === bookId) setRecentBook(null);
    } catch (e: any) {
      alert('删除失败: ' + e.message);
    }
  }

  useEffect(() => {
    api.listBooks().then(b => {
      setBooks(b);
      setLoading(false);
      // 找最近编辑的作品（按 updated_at 排序）
      if (b.length > 0) {
        const sorted = [...b].sort((a, b2) => new Date(b2.updated_at).getTime() - new Date(a.updated_at).getTime());
        setRecentBook(sorted[0]);
      }
    }).catch(() => setLoading(false));

    // 读取今日统计
    try {
      const raw = localStorage.getItem('fanshu-writing-history');
      if (raw) {
        const hist = JSON.parse(raw);
        const today = new Date().toISOString().slice(0, 10);
        if (hist.lastDate === today) {
          setTodayStats({ words: hist.todayWords || 0, chapters: hist.todayChapters || 0 });
        }
        setStreak(hist.streak || 0);
      }
    } catch { /* ignore */ }
  }, []);

  // 加载所有技能包供总AI创作选择
  useEffect(() => {
    api.listSkillPacks().then(all => setMasterCreatePacks(all)).catch(() => { /* ignore */ });
  }, []);

  async function handleCreateBook() {
    if (!newBookForm.title) return;
    const ok = await requireAuth();
    if (!ok) return;
    setCreating(true);
    setCreateError('');
    try {
      const book = await api.createBook(newBookForm);
      setBooks(prev => [book, ...prev]);
      setShowNewBook(false);
      setNewBookForm({ title: '', genre: 'other', book_type: 'novel', synopsis: '', total_volumes: 0, novel_styles: [] });
      navigate(`/write?book=${book.id}`);
    } catch (e: any) {
      setCreateError(e.message || '创建失败，请重试');
    }
    setCreating(false);
  }

  function handleFilePick(e: React.ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(e.target.files || []);
    const valid = picked.filter(f => /\.(txt|md|docx|zip|json)$/i.test(f.name));
    if (valid.length === 0) {
      setImportError('请选择 txt/md/docx/zip 格式的文件');
      return;
    }
    setImportError('');
    setImportFiles(prev => [...prev, ...valid]);
    // 自动填充标题（如果为空）
    if (!importForm.title && valid.length > 0) {
      const name = valid[0].name.replace(/\.[^.]+$/, '');
      setImportForm(prev => ({ ...prev, title: name.slice(0, 50) }));
    }
    // 清空 input 以便重复选择同一文件
    if (e.target === fileImportRef.current && fileImportRef.current) fileImportRef.current.value = '';
    if (e.target === folderImportRef.current && folderImportRef.current) folderImportRef.current.value = '';
  }

  function removeImportFile(idx: number) {
    setImportFiles(prev => prev.filter((_, i) => i !== idx));
  }

  async function handleImport() {
    if (importFiles.length === 0) {
      setImportError('请先选择文件或文件夹');
      return;
    }
    const ok = await requireAuth();
    if (!ok) return;
    setImporting(true);
    setImportError('');
    try {
      const book = await api.importFiles(importFiles, {
        title: importForm.title || undefined,
        book_type: importForm.book_type,
        genre: importForm.genre,
      });
      setBooks(prev => [book, ...prev]);
      setShowImport(false);
      setImportFiles([]);
      setImportForm({ title: '', book_type: 'novel', genre: 'other' });

      // 导入成功后询问是否AI识别设定
      if (confirm('导入成功！是否立即用 AI 分析内容，自动识别构思、设定、大纲、世界观、人物、剧情、伏笔等维度？')) {
        setImporting(true);
        try {
          const result = await api.analyzeContent(book.id);
          alert(`AI识别完成！已填充 ${result.updated_fields.length} 个维度：${result.updated_fields.map((f: string) => FIELD_LABELS[f] || f).join('、')}`);
        } catch (e: any) {
          alert('AI识别失败：' + (e.message || '请稍后在创作页手动触发'));
        }
        setImporting(false);
      }
      navigate(`/write?book=${book.id}`);
    } catch (e: any) {
      setImportError(e.message || '导入失败，请重试');
    }
    setImporting(false);
  }

  // 选择作品后打开 AiCreateModal（与创作页完全一致，仅多一步"选择作品"）
  // 优化：先立即打开弹窗（bible 传 null，AiCreateModal 内部均为可选链安全访问），bible 后台异步加载
  async function handleSelectBookForAi(book: Book) {
    const ok = await requireAuth();
    if (!ok) return;
    // 先关闭作品选择弹窗，立即打开 AiCreateModal（bible=null 占位），让用户立刻看到界面
    setShowMasterCreateModal(false);
    setAiModalBook(book);
    setAiBible(null);
    setAiModalOpen(true);
    // 后台异步加载 bible，加载完成后回填（不阻塞弹窗显示）
    try {
      const bb = await api.getBible(book.id);
      setAiBible(bb);
    } catch (e: any) {
      alert('加载作品设定失败：' + (e.message || '请重试'));
    }
  }

  // 单维度填入：保存到对应 BookBible 字段（与创作页 handleAiCreateApply 一致）
  async function handleAiCreateApply(field: string, content: string) {
    if (!aiModalBook) return;
    try {
      const updated = await api.updateBible(aiModalBook.id, { [field]: content } as Partial<BookBible>);
      setAiBible(updated);
    } catch (e: any) {
      alert('填入失败：' + (e.message || '请重试'));
    }
  }

  // 全局多维度批量填入（与创作页 handleAiCreateApplyMany 一致）
  async function handleAiCreateApplyMany(results: { field: string; content: string }[]) {
    if (!aiModalBook || results.length === 0) return;
    const patch: Partial<BookBible> = {};
    for (const r of results) (patch as any)[r.field] = r.content;
    try {
      const updated = await api.updateBible(aiModalBook.id, patch);
      setAiBible(updated);
    } catch (e: any) {
      alert('填入失败：' + (e.message || '请重试'));
    }
  }

  // 关闭 AiCreateModal
  function handleCloseAiModal() {
    setAiModalOpen(false);
    setAiModalBook(null);
    setAiBible(null);
  }

  if (loading) return <div className="page loading-screen"><span>加载中...</span></div>;

  return (
    <div className="page home-page">
      {/* 手机端顶部：左 wangbiao logo + 右文字 */}
      <div className="home-topbar-mobile">
        <picture className="home-topbar-logo">
          <img src="wangbiao.webp" alt="蚂蚁写作" />
        </picture>
        <div className="home-topbar-text">
          <span className="home-topbar-title">蚂　蚁　写　作</span>
          <span className="home-topbar-sub">mayi.chat · 专业Ai小说创作平台</span>
        </div>
      </div>

      {/* 今日写作统计卡片 */}
      <div className="home-stats-banner">
        <div className="home-stats-hero">
          <div className="home-stats-hero-value">{todayStats.words.toLocaleString()}</div>
          <div className="home-stats-hero-label">今日字数</div>
        </div>
        <div className="home-stats-divider" />
        <div className="home-stats-item">
          <div className="home-stats-item-value">{todayStats.chapters}</div>
          <div className="home-stats-item-label">今日章节</div>
        </div>
        <div className="home-stats-divider" />
        <div className="home-stats-item">
          <div className="home-stats-item-value">{streak}</div>
          <div className="home-stats-item-label">连续天数</div>
        </div>
        <div className="home-stats-divider" />
        <div className="home-stats-item">
          <div className="home-stats-item-value">{books.length}</div>
          <div className="home-stats-item-label">作品总数</div>
        </div>
      </div>

      {/* 快捷操作区 */}
      <div className="home-quick-actions">
        <button className="home-action-btn home-action-import" onClick={() => setShowImport(true)}>
          <span className="home-action-icon">📥</span>
          <span className="home-action-label">导入作品</span>
          <span className="home-action-desc">txt/md/word/zip</span>
        </button>
        <button className="home-action-btn home-action-new" onClick={() => setShowNewBook(true)}>
          <span className="home-action-icon">✨</span>
          <span className="home-action-label">新建作品</span>
          <span className="home-action-desc">从零开始创作</span>
        </button>
      </div>

      {/* 最近编辑的作品 */}
      {/* 最近编辑的作品 / 使用说明书 */}
      {recentBook ? (
        <div className="home-section">
          <div className="home-section-header">
            <h2>📝 最近编辑</h2>
            <button className="btn-ghost-sm" onClick={() => navigate(`/write?book=${recentBook.id}`)}>继续写作 →</button>
          </div>
          <div className="recent-book-card" onClick={() => navigate(`/write?book=${recentBook.id}`)}>
            <div className="recent-book-cover">
              {recentBook.cover_path ? <img src={recentBook.cover_path} alt="" /> : <div className="cover-placeholder">📖</div>}
            </div>
            <div className="recent-book-info">
              <h3>{recentBook.title}</h3>
              <div className="recent-book-meta">
                <span className="tag-sm">{recentBook.book_type === 'novel' ? '长篇' : recentBook.book_type === 'script' ? '剧本' : '短篇'}</span>
                <span className="tag-sm">{GENRE_MAP[recentBook.genre] || recentBook.genre}</span>
                <span className="tag-sm">{recentBook.word_count}字</span>
              </div>
              <div className="recent-book-time">
                上次编辑：{formatTime(recentBook.updated_at)}
              </div>
            </div>
            <div className="recent-book-arrow">→</div>
          </div>
        </div>
      ) : (
        <div className="home-section">
          <div className="home-section-header">
            <h2>📖 使用说明书</h2>
            <button className="btn-ghost-sm" onClick={() => setShowManual(true)}>查看详情 →</button>
          </div>
          <div className="recent-book-card manual-card" onClick={() => setShowManual(true)}>
            <div className="recent-book-cover">
              <div className="cover-placeholder">📖</div>
            </div>
            <div className="recent-book-info">
              <h3>蚂蚁写作 · 快速上手指南</h3>
              <div className="recent-book-meta">
                <span className="tag-sm">新手必读</span>
                <span className="tag-sm">5分钟入门</span>
              </div>
              <div className="recent-book-time">
                从构思到成稿，AI辅助全流程创作
              </div>
            </div>
            <div className="recent-book-arrow">→</div>
          </div>
        </div>
      )}

      {/* AI 总创作入口 */}
      <div className="home-section">
        <button
          className="master-create-entry"
          onClick={() => setShowMasterCreateModal(true)}
          disabled={books.length === 0}
        >
          <div className="master-create-entry-icon">🤖</div>
          <div className="master-create-entry-content">
            <div className="master-create-entry-label">Ai总创作</div>
            <div className="master-create-entry-desc">{books.length > 0 ? '选择作品，对构思/设定/世界观/人物/大纲/剧情协同生成' : '请先创建作品'}</div>
          </div>
          <div className="master-create-entry-arrow">{books.length > 0 ? '→' : ''}</div>
        </button>
      </div>

      {/* 全部作品列表 */}
      {books.length > 0 && (
        <div className="home-section">
          <div className="home-section-header">
            <h2>📚 全部作品</h2>
            <button className="btn-ghost-sm" onClick={() => navigate('/write')}>前往创作 →</button>
          </div>
          <div className="home-book-list">
            {books.map(book => (
              <div key={book.id} className="home-book-item" onClick={() => navigate(`/write?book=${book.id}`)}>
                <div className="home-book-cover">
                  {book.cover_path ? <img src={book.cover_path} alt="" /> : <div className="cover-placeholder">📖</div>}
                </div>
                <div className="home-book-info">
                  <div className="home-book-title">{book.title}</div>
                  <div className="home-book-meta">
                    <span>{book.book_type === 'novel' ? '长篇' : book.book_type === 'script' ? '剧本' : '短篇'}</span>
                    <span>{GENRE_MAP[book.genre] || book.genre}</span>
                    <span>{book.word_count}字</span>
                  </div>
                </div>
                <div className="home-book-actions" onClick={e => e.stopPropagation()}>
                  <button className="btn-icon-sm" title="编辑信息" onClick={() => handleRenameBook(book)}>✏️</button>
                  <button className="btn-icon-sm" title="删除作品" onClick={() => handleDeleteBook(book.id)} style={{color:'#e74c3c'}}>🗑️</button>
                </div>
                <div className="home-book-arrow">›</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {showNewBook && (
        <div className="modal-overlay" onClick={() => { setShowNewBook(false); setCreateError(''); }}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h2>创建新作品</h2>
            <input className="input" placeholder="作品标题" value={newBookForm.title} onChange={e => setNewBookForm(prev => ({ ...prev, title: e.target.value }))} />
            <div className="form-row">
              <select className="input" value={newBookForm.book_type} onChange={e => handleNewBookTypeChange(e.target.value)}>
                <option value="novel">长篇</option>
                <option value="short_story">短篇</option>
              </select>
              <select className="input" value={newBookForm.genre} onChange={e => handleNewGenreChange(e.target.value)}>
                <optgroup label="通用">
                  <option value="other">其他</option>
                </optgroup>
                <optgroup label="男频">
                  <option value="urban">都市</option>
                  <option value="urban_business">都市职场</option>
                  <option value="urban_fantasy">都市异能</option>
                  <option value="fantasy">玄幻</option>
                  <option value="xianxia">仙侠</option>
                  <option value="qihuan">奇幻</option>
                  <option value="wuxia">武侠</option>
                  <option value="history">历史</option>
                  <option value="military">军事</option>
                  <option value="game">游戏</option>
                  <option value="sports">体育</option>
                  <option value="scifi">科幻</option>
                  <option value="mystery">悬疑</option>
                  <option value="infinite">诸天无限</option>
                  <option value="light_novel">轻小说</option>
                </optgroup>
                <optgroup label="女频">
                  <option value="romance">现代言情</option>
                  <option value="ancient_romance">古代言情</option>
                  <option value="fantasy_romance">幻想言情</option>
                  <option value="danmei">纯爱</option>
                  <option value="acg">二次元</option>
                </optgroup>
              </select>
            </div>
            {(() => {
              const range = getVolumeRange(newBookForm.book_type);
              const tv = newBookForm.total_volumes || range.default;
              return (
                <div className="form-field">
                  <label>总卷数（{range.min}-{range.max}） · {range.perVolumeWords}</label>
                  <div style={{display:'flex',alignItems:'center',gap:10}}>
                    <input type="range" min={range.min} max={range.max} value={tv} onChange={e => setNewBookForm(prev => ({ ...prev, total_volumes: Number(e.target.value) }))} style={{flex:1}}/>
                    <span style={{minWidth:48,textAlign:'center',fontWeight:600,color:'var(--accent)'}}>{tv} {newBookForm.book_type==='short_story'?'篇':'卷'}</span>
                  </div>
                  {newBookForm.book_type==='novel' && <div style={{fontSize:11,color:'var(--text-muted)',marginTop:4}}>预计总字数约 {tv*12} 万字（每卷约12万字，约50章/卷）</div>}
                </div>
              );
            })()}
            {(() => {
              const styles = getStylesForGenre(newBookForm.book_type, newBookForm.genre);
              return (
                <div className="form-field">
                  <label>风格流派（随题材变化 · 可多选最多3种 · 已选 {newBookForm.novel_styles.length}/3）</label>
                  <div style={{display:'flex',flexWrap:'wrap',gap:6,maxHeight:120,overflowY:'auto'}}>
                    {Object.entries(styles).map(([k,v])=>{
                      const sel = newBookForm.novel_styles.includes(k);
                      const disabled = !sel && newBookForm.novel_styles.length>=3;
                      return (
                        <button key={k} type="button" onClick={()=>toggleNewStyle(k)} disabled={disabled}
                          style={{padding:'4px 10px',fontSize:12,borderRadius:14,cursor:disabled?'not-allowed':'pointer',
                            border:`1px solid ${sel?'var(--accent)':'var(--border-color)'}`,
                            background:sel?'var(--accent)':'transparent',
                            color:sel?'#fff':'var(--text-primary)',opacity:disabled?0.4:1}}>
                          {v}
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            })()}
            <textarea className="input" rows={3} placeholder="简介（可选）" value={newBookForm.synopsis} onChange={e => setNewBookForm(prev => ({ ...prev, synopsis: e.target.value }))} />
            {createError && <div className="error-msg" style={{marginBottom:8}}>{createError}</div>}
            <div className="modal-actions">
              <button className="btn-ghost" onClick={() => { setShowNewBook(false); setCreateError(''); }}>取消</button>
              <button className="btn-primary" onClick={handleCreateBook} disabled={!newBookForm.title || creating}>
                {creating ? '创建中...' : '创建'}
              </button>
            </div>
          </div>
        </div>
      )}

      {showImport && (
        <div className="modal-overlay" onClick={() => { setShowImport(false); setImportError(''); setImportFiles([]); }}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h2>📥 导入作品</h2>
            <p className="text-muted" style={{marginBottom:12}}>支持导入 txt/md/docx/zip 文件，或选择整个文件夹批量导入。系统会自动识别章节并创建作品。</p>

            <div className="import-file-zone">
              <input ref={fileImportRef} type="file" accept=".txt,.md,.docx,.zip,.json" multiple onChange={handleFilePick} style={{display:'none'}} id="import-file-input" />
              <input ref={folderImportRef} type="file" onChange={handleFilePick} style={{display:'none'}} id="import-folder-input" {...({ webkitdirectory: '', directory: '' } as any)} />
              <div className="import-zone-buttons">
                <label htmlFor="import-file-input" className="btn-secondary import-zone-btn">
                  <span className="import-zone-icon">📄</span>
                  <span>选择文件</span>
                </label>
                <label htmlFor="import-folder-input" className="btn-secondary import-zone-btn">
                  <span className="import-zone-icon">📁</span>
                  <span>选择文件夹</span>
                </label>
              </div>
              <p className="text-muted" style={{fontSize:11,textAlign:'center',marginTop:6}}>可多选，文件夹将导入其中所有 txt/md 文件</p>
            </div>

            {importFiles.length > 0 && (
              <div className="import-file-list">
                {importFiles.map((f, i) => (
                  <div key={i} className="import-file-item">
                    <span className="import-file-icon">{/\.(docx)$/i.test(f.name) ? '📘' : /\.(zip)$/i.test(f.name) ? '🗜️' : '📄'}</span>
                    <span className="import-file-name">{f.name}</span>
                    <span className="import-file-size">{(f.size / 1024).toFixed(1)}KB</span>
                    <button className="btn-icon-sm" onClick={() => removeImportFile(i)}>✕</button>
                  </div>
                ))}
              </div>
            )}

            <div className="form-field">
              <label>作品标题（留空则从文件名自动推断）</label>
              <input className="input" value={importForm.title} onChange={e => setImportForm(prev => ({ ...prev, title: e.target.value }))} placeholder="如：仙路独行" />
            </div>
            <div className="form-row">
              <div className="form-field">
                <label>类型</label>
                <select className="input" value={importForm.book_type} onChange={e => setImportForm(prev => ({ ...prev, book_type: e.target.value }))}>
                  <option value="novel">长篇</option>
                  <option value="short_story">短篇</option>
                </select>
              </div>
              <div className="form-field">
                <label>题材</label>
                <select className="input" value={importForm.genre} onChange={e => setImportForm(prev => ({ ...prev, genre: e.target.value }))}>
                  <optgroup label="通用">
                    <option value="other">其他</option>
                  </optgroup>
                  <optgroup label="男频">
                    <option value="urban">都市</option>
                    <option value="fantasy">玄幻</option>
                    <option value="xianxia">仙侠</option>
                    <option value="history">历史</option>
                    <option value="military">军事</option>
                    <option value="game">游戏</option>
                    <option value="sports">体育</option>
                    <option value="scifi">科幻</option>
                    <option value="mystery">悬疑</option>
                    <option value="light_novel">轻小说</option>
                    <option value="urban_business">都市职场</option>
                    <option value="urban_fantasy">都市异能</option>
                  </optgroup>
                  <optgroup label="女频">
                    <option value="romance">现代言情</option>
                    <option value="ancient_romance">古代言情</option>
                    <option value="fantasy_romance">幻想言情</option>
                    <option value="danmei">纯爱</option>
                    <option value="acg">二次元</option>
                  </optgroup>
                </select>
              </div>
            </div>

            {importError && <div className="error-msg" style={{marginBottom:8}}>{importError}</div>}
            <div className="modal-actions">
              <button className="btn-ghost" onClick={() => { setShowImport(false); setImportError(''); setImportFiles([]); }}>取消</button>
              <button className="btn-primary" onClick={handleImport} disabled={importFiles.length === 0 || importing}>
                {importing ? '导入中...' : `导入 ${importFiles.length} 个文件`}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 使用说明书弹窗 */}
      {showManual && (
        <div className="modal-overlay" onClick={() => setShowManual(false)}>
          <div className="modal manual-modal" onClick={e => e.stopPropagation()}>
            <div className="manual-header">
              <h2>📖 蚂蚁写作 · 使用说明书</h2>
              <button className="btn-icon" onClick={() => setShowManual(false)}>✕</button>
            </div>
            <div className="manual-content">
              <div className="manual-section">
                <h3>🚀 三步开始创作</h3>
                <ol>
                  <li><b>新建作品</b>：点击「✨ 新建作品」，填写标题、类型和题材</li>
                  <li><b>构思设定</b>：进入创作页，在「构思」栏写一句话创意，点「AI 头脑风暴」自动扩展</li>
                  <li><b>开始写章</b>：切到「章节」标签，新建章节，用 AI 续写/润色辅助写作</li>
                </ol>
              </div>
              <div className="manual-section">
                <h3>💡 创作页功能</h3>
                <ul>
                  <li><b>构思</b>：一句话创意 → AI 头脑风暴生成全套方案</li>
                  <li><b>设定 / 大纲 / 世界观 / 人物</b>：每个维度都可手动编辑或让 AI 辅助生成</li>
                  <li><b>章节</b>：支持新建、编辑、AI续写、AI润色、一键排版</li>
                  <li><b>伏笔 / 地图</b>：管理伏笔线索和三级地点体系</li>
                  <li><b>图谱</b>：关系图谱、地点图谱、境界图谱可视化</li>
                </ul>
              </div>
              <div className="manual-section">
                <h3>🔧 工具箱</h3>
                <ul>
                  <li><b>AI 责编</b>：7维度审稿打分，给出商业评估</li>
                  <li><b>技能包</b>：安装题材工作流，导入支持 JSON/YAML/MD 格式</li>
                  <li><b>拆书分析</b>：导入他人作品分析文风结构，可同步到作品资料做仿写</li>
                  <li><b>导出</b>：支持 txt/html/json/zip 导出</li>
                </ul>
              </div>
              <div className="manual-section">
                <h3>⚙️ AI 配置</h3>
                <p>在「我的 → AI设置」中配置 API。支持 DeepSeek、通义千问、智谱GLM、Kimi 等10+国内厂商，一键填充。</p>
              </div>
              <div className="manual-section">
                <h3>📱 移动端技巧</h3>
                <ul>
                  <li>左右滑动切换章节</li>
                  <li>支持 PWA 离线使用，添加到桌面即可</li>
                  <li>所有数据自动保存在本地</li>
                </ul>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 编辑作品信息弹窗 */}
      {editBookId && (
        <div className="modal-overlay" onClick={() => setEditBookId('')}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h2>编辑作品信息</h2>
            <input className="input" placeholder="作品标题" value={editBookForm.title} onChange={e => setEditBookForm(prev => ({ ...prev, title: e.target.value }))} />
            <div className="form-row">
              <select className="input" value={editBookForm.book_type} onChange={e => handleEditBookTypeChange(e.target.value)}>
                <option value="novel">长篇</option>
                <option value="short_story">短篇</option>
              </select>
              <select className="input" value={editBookForm.genre} onChange={e => handleEditGenreChange(e.target.value)}>
                <optgroup label="通用"><option value="other">其他</option></optgroup>
                <optgroup label="男频">
                  <option value="urban">都市</option><option value="urban_business">都市职场</option><option value="urban_fantasy">都市异能</option>
                  <option value="fantasy">玄幻</option><option value="xianxia">仙侠</option><option value="qihuan">奇幻</option><option value="wuxia">武侠</option>
                  <option value="history">历史</option><option value="military">军事</option><option value="game">游戏</option><option value="sports">体育</option>
                  <option value="scifi">科幻</option><option value="mystery">悬疑</option><option value="infinite">诸天无限</option><option value="light_novel">轻小说</option>
                </optgroup>
                <optgroup label="女频">
                  <option value="romance">现代言情</option><option value="ancient_romance">古代言情</option>
                  <option value="fantasy_romance">幻想言情</option><option value="danmei">纯爱</option><option value="acg">二次元</option>
                </optgroup>
              </select>
            </div>
            {(() => {
              const range = getVolumeRange(editBookForm.book_type);
              const tv = editBookForm.total_volumes || range.default;
              return (
                <div className="form-field">
                  <label>总卷数（{range.min}-{range.max}） · {range.perVolumeWords}</label>
                  <div style={{display:'flex',alignItems:'center',gap:10}}>
                    <input type="range" min={range.min} max={range.max} value={tv} onChange={e => setEditBookForm(prev => ({ ...prev, total_volumes: Number(e.target.value) }))} style={{flex:1}}/>
                    <span style={{minWidth:48,textAlign:'center',fontWeight:600,color:'var(--accent)'}}>{tv} {editBookForm.book_type==='short_story'?'篇':'卷'}</span>
                  </div>
                  {editBookForm.book_type==='novel' && <div style={{fontSize:11,color:'var(--text-muted)',marginTop:4}}>预计总字数约 {tv*12} 万字（每卷约12万字，约50章/卷）</div>}
                  <div style={{fontSize:11,color:'var(--text-muted)',marginTop:4}}>提示：题材、卷数、风格将作为后续五幕总纲、剧情大纲等所有创作维度的核心依据</div>
                </div>
              );
            })()}
            {(() => {
              const styles = getStylesForGenre(editBookForm.book_type, editBookForm.genre);
              return (
                <div className="form-field">
                  <label>风格流派（随题材变化 · 可多选最多3种 · 已选 {editBookForm.novel_styles.length}/3）</label>
                  <div style={{display:'flex',flexWrap:'wrap',gap:6,maxHeight:120,overflowY:'auto'}}>
                    {Object.entries(styles).map(([k,v])=>{
                      const sel = editBookForm.novel_styles.includes(k);
                      const disabled = !sel && editBookForm.novel_styles.length>=3;
                      return (
                        <button key={k} type="button" onClick={()=>toggleEditStyle(k)} disabled={disabled}
                          style={{padding:'4px 10px',fontSize:12,borderRadius:14,cursor:disabled?'not-allowed':'pointer',
                            border:`1px solid ${sel?'var(--accent)':'var(--border-color)'}`,
                            background:sel?'var(--accent)':'transparent',
                            color:sel?'#fff':'var(--text-primary)',opacity:disabled?0.4:1}}>
                          {v}
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            })()}
            <textarea className="input" rows={3} placeholder="简介（可选）" value={editBookForm.synopsis} onChange={e => setEditBookForm(prev => ({ ...prev, synopsis: e.target.value }))} />
            <div className="modal-actions">
              <button className="btn-ghost" onClick={() => setEditBookId('')}>取消</button>
              <button className="btn-primary" onClick={handleSaveEditBook} disabled={!editBookForm.title.trim() || editBookSaving}>
                {editBookSaving ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* AI 总创作 - 作品选择弹窗（首页入口多出的一步，选完作品后进入与创作页完全一致的 AiCreateModal） */}
      {showMasterCreateModal && (
        <div className="modal-overlay" onClick={() => setShowMasterCreateModal(false)}>
          <div className="modal" onClick={e => e.stopPropagation()} style={{ maxWidth: 480 }}>
            <div className="master-create-modal-header">
              <h2>🤖 Ai总创作</h2>
              <button className="btn-ghost" onClick={() => setShowMasterCreateModal(false)}>✕</button>
            </div>
            <p className="text-muted" style={{ fontSize: 13, marginBottom: 16 }}>
              选择要创作的作品，进入后可对构思/设定/大纲/世界观/人物/剧情等维度协同生成（与创作界面入口为同一功能）
            </p>
            {books.length === 0 ? (
              <div className="empty-state" style={{ padding: 24 }}>
                <p>还没有作品，请先新建</p>
                <button className="btn-primary" style={{ marginTop: 12 }} onClick={() => { setShowMasterCreateModal(false); setShowNewBook(true); }}>+ 新建作品</button>
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 360, overflowY: 'auto' }}>
                {books.map(b => (
                  <button
                    key={b.id}
                    className="ai-picker-item"
                    onClick={() => handleSelectBookForAi(b)}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 12, padding: '10px 12px',
                      background: 'var(--bg-secondary)', border: '1px solid var(--border-color)',
                      borderRadius: 8, cursor: 'pointer', textAlign: 'left',
                    }}
                  >
                    <span style={{ fontSize: 24 }}>{b.cover_path ? '📖' : '📚'}</span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontWeight: 600, fontSize: 14, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{b.title}</div>
                      <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                        {b.book_type === 'novel' ? '长篇' : b.book_type === 'script' ? '剧本' : '短篇'} · {GENRE_MAP[b.genre] || b.genre} · {b.word_count.toLocaleString()}字 · {b.chapter_count}章
                      </div>
                    </div>
                    <span style={{ color: 'var(--accent)' }}>→</span>
                  </button>
                ))}
              </div>
            )}
            <div className="modal-actions" style={{ marginTop: 16 }}>
              <button className="btn-ghost" onClick={() => setShowMasterCreateModal(false)}>取消</button>
            </div>
          </div>
        </div>
      )}

      {/* AiCreateModal：与创作页完全一致的全屏 AI 创作弹窗 */}
      {aiModalOpen && aiModalBook && (
        <AiCreateModal
          mode="global"
          bookId={aiModalBook.id}
          book={aiModalBook}
          bible={aiBible}
          skillPacks={masterCreatePacks}
          selectedSkillPackIds={[]}
          onApply={handleAiCreateApply}
          onApplyMany={handleAiCreateApplyMany}
          onClose={handleCloseAiModal}
        />
      )}
    </div>
  );
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  const minutes = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  const days = Math.floor(diff / 86400000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes}分钟前`;
  if (hours < 24) return `${hours}小时前`;
  if (days < 7) return `${days}天前`;
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

const GENRE_MAP: Record<string, string> = {
  other: '其他', romance: '现代言情', ancient_romance: '古代言情', fantasy_romance: '幻想言情',
  danmei: '纯爱', acg: '二次元',
  urban: '都市', fantasy: '玄幻', xianxia: '仙侠', history: '历史', military: '军事',
  game: '游戏', sports: '体育', scifi: '科幻', mystery: '悬疑', light_novel: '轻小说',
  urban_business: '都市职场', urban_fantasy: '都市异能',
};

const FIELD_LABELS: Record<string, string> = {
  concept: '构思', key_rules: '设定', plot_design: '大纲', worldbuilding: '世界观',
  character_profiles: '人物及关系', timeline: '剧情', foreshadowing: '伏笔',
  locations: '地图/地点', generated_summary: '内容摘要',
};
