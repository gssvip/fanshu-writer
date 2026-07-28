import { useState, useEffect, useContext, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import { AuthContext } from '../App';
import type { Book, BookBible, SkillPack } from '../types';

export default function WorkbenchPage() {
  const navigate = useNavigate();
  const { requireAuth } = useContext(AuthContext);
  const [books, setBooks] = useState<Book[]>([]);
  const [loading, setLoading] = useState(true);
  const [showNewBook, setShowNewBook] = useState(false);
  const [newBookForm, setNewBookForm] = useState({ title: '', genre: 'other', book_type: 'short_story', synopsis: '' });
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
  const [editBookForm, setEditBookForm] = useState({ title: '', genre: 'other', book_type: 'short_story', synopsis: '' });
  const [editBookSaving, setEditBookSaving] = useState(false);

  // 总AI创作面板状态
  const [masterCreateBookId, setMasterCreateBookId] = useState('');
  const [masterCreatePacks, setMasterCreatePacks] = useState<SkillPack[]>([]);
  const [masterCreateSelectedPackIds, setMasterCreateSelectedPackIds] = useState<string[]>([]);
  const [masterCreateDims, setMasterCreateDims] = useState<string[]>(MASTER_DIMS.map(d => d.key));
  const [masterCreateInstruction, setMasterCreateInstruction] = useState('');
  const [masterCreateLoading, setMasterCreateLoading] = useState(false);
  const [masterCreateResults, setMasterCreateResults] = useState<Array<{ dimension: string; label: string; field: string; content?: string; error?: string }>>([]);
  const [masterCreatePacksExpanded, setMasterCreatePacksExpanded] = useState(false);
  const [showMasterCreateModal, setShowMasterCreateModal] = useState(false);

  async function handleRenameBook(book: Book) {
    setEditBookId(book.id);
    setEditBookForm({ title: book.title, genre: book.genre || 'other', book_type: book.book_type || 'short_story', synopsis: book.synopsis || '' });
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

  // 默认选中最近编辑的作品
  useEffect(() => {
    if (recentBook && !masterCreateBookId) {
      setMasterCreateBookId(recentBook.id);
    }
  }, [recentBook, masterCreateBookId]);

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
      setNewBookForm({ title: '', genre: 'other', book_type: 'short_story', synopsis: '' });
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

  // 切换技能包选中状态
  function toggleMasterPack(id: string) {
    setMasterCreateSelectedPackIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }

  // 切换维度选中状态
  function toggleMasterDim(key: string) {
    setMasterCreateDims(prev => prev.includes(key) ? prev.filter(x => x !== key) : [...prev, key]);
  }

  // 开始 AI 总创作
  async function handleMasterCreate() {
    if (!masterCreateBookId || masterCreateDims.length === 0) return;
    const ok = await requireAuth();
    if (!ok) return;
    setMasterCreateLoading(true);
    try {
      const dims = MASTER_DIMS.filter(d => masterCreateDims.includes(d.key)).map(d => d.key);
      const res = await api.aiMasterCreate(masterCreateBookId, dims, masterCreateSelectedPackIds, masterCreateInstruction);
      setMasterCreateResults(res.results || []);
      if ((res.results || []).length === 0) {
        alert('未返回任何创作结果');
      }
    } catch (e: any) {
      alert('AI 创作失败：' + (e.message || '请重试'));
    }
    setMasterCreateLoading(false);
  }

  // 编辑某个维度的创作结果内容
  function updateMasterResultContent(field: string, content: string) {
    setMasterCreateResults(prev => prev.map(r => r.field === field ? { ...r, content } : r));
  }

  // 确认填入单个维度到作品设定
  async function handleApplyMasterResult(field: string) {
    const r = masterCreateResults.find(x => x.field === field);
    if (!r || !r.content) return;
    const ok = await requireAuth();
    if (!ok) return;
    try {
      await api.updateBible(masterCreateBookId, { [field]: r.content } as Partial<BookBible>);
      alert(`✅ 已填入「${r.label}」`);
      setMasterCreateResults(prev => prev.filter(x => x.field !== field));
    } catch (e: any) {
      alert('填入失败：' + (e.message || '请重试'));
    }
  }

  // 丢弃单个维度的创作结果
  function handleDiscardMasterResult(field: string) {
    setMasterCreateResults(prev => prev.filter(x => x.field !== field));
  }

  // 一键填入所有创作结果
  async function handleApplyAllMasterResults() {
    if (masterCreateResults.length === 0) return;
    const ok = await requireAuth();
    if (!ok) return;
    const valid = masterCreateResults.filter(r => r.content);
    if (valid.length === 0) {
      alert('没有可填入的内容');
      return;
    }
    const succeededFields: string[] = [];
    let failed = 0;
    for (const r of valid) {
      try {
        await api.updateBible(masterCreateBookId, { [r.field]: r.content } as Partial<BookBible>);
        succeededFields.push(r.field);
      } catch {
        failed++;
      }
    }
    alert(`✅ 成功填入 ${succeededFields.length} 个维度${failed > 0 ? `，${failed} 个失败` : ''}`);
    setMasterCreateResults(prev => prev.filter(r => !succeededFields.includes(r.field)));
  }

  if (loading) return <div className="page loading-screen"><span>加载中...</span></div>;

  return (
    <div className="page home-page">
      <header className="page-header">
        <h1>蚂蚁写作</h1>
      </header>

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
        <div className="home-section-header">
          <h2>🤖 AI 总创作</h2>
        </div>
        <button
          className="master-create-entry"
          onClick={() => setShowMasterCreateModal(true)}
          disabled={!recentBook}
        >
          <div className="master-create-entry-icon">🤖</div>
          <div className="master-create-entry-content">
            <div className="master-create-entry-label">总览全局创作</div>
            <div className="master-create-entry-desc">{recentBook ? `为「${recentBook.title}」生成构思/设定/世界观/人物/大纲/剧情` : '请先创建作品'}</div>
          </div>
          <div className="master-create-entry-arrow">{recentBook ? '→' : ''}</div>
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
              <select className="input" value={newBookForm.book_type} onChange={e => setNewBookForm(prev => ({ ...prev, book_type: e.target.value }))}>
                <option value="short_story">短篇</option>
                <option value="novel">长篇</option>
                <option value="script">剧本</option>
              </select>
              <select className="input" value={newBookForm.genre} onChange={e => setNewBookForm(prev => ({ ...prev, genre: e.target.value }))}>
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
                  <option value="script">剧本</option>
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
              <select className="input" value={editBookForm.book_type} onChange={e => setEditBookForm(prev => ({ ...prev, book_type: e.target.value }))}>
                <option value="short_story">短篇</option>
                <option value="novel">长篇</option>
                <option value="script">剧本</option>
              </select>
              <select className="input" value={editBookForm.genre} onChange={e => setEditBookForm(prev => ({ ...prev, genre: e.target.value }))}>
                <optgroup label="通用"><option value="other">其他</option></optgroup>
                <optgroup label="男频">
                  <option value="urban">都市</option><option value="fantasy">玄幻</option><option value="xianxia">仙侠</option>
                  <option value="history">历史</option><option value="military">军事</option><option value="game">游戏</option>
                  <option value="sports">体育</option><option value="scifi">科幻</option><option value="mystery">悬疑</option>
                  <option value="light_novel">轻小说</option>
                </optgroup>
                <optgroup label="女频">
                  <option value="romance">现代言情</option><option value="ancient_romance">古代言情</option>
                  <option value="fantasy_romance">幻想言情</option><option value="danmei">纯爱</option>
                </optgroup>
              </select>
            </div>
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

      {/* AI 总创作半屏模态框 */}
      {showMasterCreateModal && (
        <div className="modal-overlay" onClick={() => setShowMasterCreateModal(false)}>
          <div className="master-create-modal" onClick={e => e.stopPropagation()}>
            <div className="master-create-modal-header">
              <h2>🤖 AI 总创作</h2>
              <button className="btn-ghost" onClick={() => setShowMasterCreateModal(false)}>✕</button>
            </div>
            <div className="master-create-modal-body">
              {/* 作品选择 */}
              <div className="form-field">
                <label>选择作品</label>
                <select className="input" value={masterCreateBookId} onChange={e => setMasterCreateBookId(e.target.value)} disabled={masterCreateLoading}>
                  {books.map(b => <option key={b.id} value={b.id}>{b.title}</option>)}
                </select>
              </div>

              {/* 折叠技能包选择器 */}
              <div style={{ marginTop: 12 }}>
                <div
                  style={{ cursor: 'pointer', userSelect: 'none', padding: '8px 0', fontWeight: 600, fontSize: 14 }}
                  onClick={() => setMasterCreatePacksExpanded(!masterCreatePacksExpanded)}
                >
                  📂 协同技能包（可选）{masterCreatePacksExpanded ? ' ▾' : ' ▸'}
                  {masterCreateSelectedPackIds.length > 0 && ` · 已选 ${masterCreateSelectedPackIds.length} 个`}
                </div>
                {masterCreatePacksExpanded && (
                  <div className="skill-pack-checkbox-list">
                    {masterCreatePacks.length === 0 ? (
                      <div className="text-muted" style={{ fontSize: 12, padding: '4px 0' }}>暂无可用技能包</div>
                    ) : masterCreatePacks.map(p => (
                      <label key={p.id} className={`skill-pack-checkbox-item ${masterCreateSelectedPackIds.includes(p.id) ? 'checked' : ''}`}>
                        <input type="checkbox" checked={masterCreateSelectedPackIds.includes(p.id)} onChange={() => toggleMasterPack(p.id)} disabled={masterCreateLoading} />
                        <span className="skill-pack-checkbox-icon">{p.icon}</span>
                        <span className="skill-pack-checkbox-name">{p.name}</span>
                      </label>
                    ))}
                  </div>
                )}
              </div>

              {/* 维度选择 */}
              <div className="form-field" style={{ marginTop: 12 }}>
                <label>创作维度（默认全选）</label>
                <div className="skill-pack-checkbox-list">
                  {MASTER_DIMS.map(d => (
                    <label key={d.key} className={`skill-pack-checkbox-item ${masterCreateDims.includes(d.key) ? 'checked' : ''}`}>
                      <input type="checkbox" checked={masterCreateDims.includes(d.key)} onChange={() => toggleMasterDim(d.key)} disabled={masterCreateLoading} />
                      <span className="skill-pack-checkbox-name">{d.label}</span>
                    </label>
                  ))}
                </div>
              </div>

              {/* 创作指令 */}
              <div className="form-field" style={{ marginTop: 12 }}>
                <label>额外指令（可选）</label>
                <textarea className="input master-create-textarea" rows={10} placeholder="如：主角是穿越者，背景设定在末世..." value={masterCreateInstruction} onChange={e => setMasterCreateInstruction(e.target.value)} disabled={masterCreateLoading} />
              </div>

              {/* 开始按钮 */}
              <div style={{ marginTop: 12 }}>
                <button className="btn-primary" onClick={handleMasterCreate} disabled={masterCreateLoading || !masterCreateBookId || masterCreateDims.length === 0}>
                  {masterCreateLoading ? '⏳ 创作中...' : '✨ 开始 AI 总创作'}
                </button>
              </div>

              {/* 结果展示区 */}
              {masterCreateResults.length > 0 && (
                <div style={{ marginTop: 16, borderTop: '1px solid #eee', paddingTop: 12 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, flexWrap: 'wrap', gap: 8 }}>
                    <strong>创作结果（{masterCreateResults.length}）</strong>
                    <button className="btn-secondary" onClick={handleApplyAllMasterResults} disabled={masterCreateLoading}>✅ 一键全部填入</button>
                  </div>
                  {masterCreateResults.map(r => (
                    <div key={r.field} style={{ border: '1px solid #e0e0e0', borderRadius: 8, padding: 12, marginBottom: 12 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, flexWrap: 'wrap', gap: 8 }}>
                        <strong>
                          {r.label}
                          {r.error && <span style={{ color: '#e74c3c', marginLeft: 6 }}>· {r.error}</span>}
                        </strong>
                        <div style={{ display: 'flex', gap: 6 }}>
                          <button className="btn-primary" onClick={() => handleApplyMasterResult(r.field)} disabled={!r.content || masterCreateLoading}>✅ 确认填入</button>
                          <button className="btn-ghost" onClick={() => handleDiscardMasterResult(r.field)}>❌ 丢弃</button>
                        </div>
                      </div>
                      {r.content !== undefined && (
                        <textarea className="input" rows={6} value={r.content} onChange={e => updateMasterResultContent(r.field, e.target.value)} disabled={masterCreateLoading} />
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
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

// 总AI创作支持的维度配置
const MASTER_DIMS: Array<{ key: string; label: string; field: string }> = [
  { key: 'concept', label: '构思', field: 'concept' },
  { key: 'key_rules', label: '设定/规则', field: 'key_rules' },
  { key: 'worldbuilding', label: '世界观', field: 'worldbuilding' },
  { key: 'character_profiles', label: '人物', field: 'character_profiles' },
  { key: 'plot_design', label: '大纲', field: 'plot_design' },
  { key: 'timeline', label: '剧情', field: 'timeline' },
];
