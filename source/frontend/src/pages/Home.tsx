import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import type { Template } from '../types';
import { useStore } from '../store';
import { api } from '../api';
import CarLogo from '../components/CarLogo';
import { GENRES, GENRE_GROUPS, getStylesForGenre, filterStylesByGenre, getVolumeRange } from '../constants';

const S: Record<string,{label:string;color:string}> = { draft:{label:'草稿',color:'#9e8f6e'}, writing:{label:'连载',color:'#27ae60'}, completed:{label:'完结',color:'#2980b9'} };

export default function Home() {
  const { books, setBooks, currentUser, theme, setTheme } = useStore() as any;
  const navigate = useNavigate();
  const [showNew, setShowNew] = useState(false);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [nf, setNf] = useState({ title:'',author:'',genre:'other',book_type:'novel',synopsis:'',template_id:'',target_words:0,total_volumes:0,novel_styles:[] as string[] });
  const [menuOpen, setMenuOpen] = useState(false);
  const [showAiPicker, setShowAiPicker] = useState(false);
  // 首页操作：导入/导出/新建 一排
  const [showImport, setShowImport] = useState(false);
  const [importing, setImporting] = useState(false);
  const importFileRef = useRef<HTMLInputElement>(null);
  const [showExport, setShowExport] = useState(false);
  const [exportBookId, setExportBookId] = useState('');
  const [exportFormat, setExportFormat] = useState('zip');
  const [exporting, setExporting] = useState(false);

  useEffect(() => { api.listTemplates().then(setTemplates).catch(()=>{}); }, []);

  const refresh = () => api.listBooks().then(setBooks);

  // 切换类型时重置卷数，并过滤掉新类型+当前题材不支持的风格
  const handleBookTypeChange = (newType: string) => {
    const range = getVolumeRange(newType);
    setNf(prev => ({
      ...prev,
      book_type: newType,
      total_volumes: prev.total_volumes === 0 ? range.default : Math.max(range.min, prev.total_volumes),
      novel_styles: filterStylesByGenre(newType, prev.genre, prev.novel_styles),
    }));
  };

  // 切换题材时过滤掉新题材不支持的风格（题材-风格联动核心）
  const handleGenreChange = (newGenre: string) => {
    setNf(prev => ({
      ...prev,
      genre: newGenre,
      novel_styles: filterStylesByGenre(prev.book_type, newGenre, prev.novel_styles),
    }));
  };

  // 切换风格多选（最多3种叠加）
  const toggleStyle = (key: string) => {
    setNf(prev => {
      const has = prev.novel_styles.includes(key);
      if (has) return { ...prev, novel_styles: prev.novel_styles.filter(s => s !== key) };
      if (prev.novel_styles.length >= 3) return prev; // 最多3种
      return { ...prev, novel_styles: [...prev.novel_styles, key] };
    });
  };

  const create = async () => {
    try { const b = await api.createBook(nf); setShowNew(false); refresh(); navigate(`/write?book=${b.id}`); }
    catch(e:any){ alert(e.message); }
  };

  const del = async (id:string,t:string) => {
    if(!confirm(`删除《${t}》？`))return;
    await api.deleteBook(id); refresh();
  };

  const doLogout = () => { try { localStorage.removeItem('fanshu-token'); } catch {} window.location.reload(); };

  return (
    <div className="home-root">
      <header className="home-header">
        <div className="home-brand" style={{width:'100%',justifyContent:'center'}}>
          <picture>
            <img src="shouye.webp" alt="蚂蚁写作" style={{height:40,objectFit:'contain',boxShadow:'var(--shadow-sm)',borderRadius:6}} />
          </picture>
          <p className="home-subtitle">AI小说创作平台</p>
        </div>
        <div className="home-actions">
          <button
            className="btn-primary"
            onClick={() => setShowAiPicker(true)}
            disabled={books.length === 0}
            title={books.length === 0 ? '请先创建作品' : 'AI 智驾：选择作品进入设定/正文/去AI/校审四Tab协作'}
            style={{ background: 'linear-gradient(135deg,#7cb89e 0%,#5ba3a8 100%)' }}
          >
            <CarLogo size={22} />
          </button>
          <button className="btn-ghost mob-hide" onClick={() => setTheme(theme==='light'?'dark':'light')}>
            {theme==='light'?'🌙':'☀️'}
          </button>
          {currentUser && <span className="user-chip" onClick={() => setMenuOpen(!menuOpen)}>
            {currentUser.username.slice(0,1)}</span>}
          {menuOpen && (
            <div className="user-menu">
              <div className="user-menu-item">{currentUser?.username}</div>
              <div className="user-menu-item danger" onClick={doLogout}>退出登录</div>
            </div>
          )}
        </div>
      </header>

      <main className="home-main">
        {books.length===0 ? (
          <div className="empty-state">
            <div style={{fontSize:48,marginBottom:12}}>📖</div>
            <h3>开始你的创作之旅</h3>
            <p>创建第一本小说，使用 AI 辅助写作</p>
            <button className="btn-primary" style={{marginTop:16}} onClick={()=>setShowNew(true)}>+ 新建作品</button>
          </div>
        ) : (
          <div className="book-grid">
            {books.map((b:any) => (
              <div key={b.id} className="book-card" onClick={() => navigate(`/write?book=${b.id}`)}>
                <div className="book-cover">
                  {b.cover_path ? <img src={b.cover_path} alt=""/> : <span style={{fontSize:36}}>📚</span>}
                  <span className="book-status" style={{background:S[b.status]?.color}}>{S[b.status]?.label}</span>
                </div>
                <div className="book-info">
                  <h3>{b.title}</h3>
                  <div className="book-meta">{b.author&&b.author+' · '}{GENRES[b.genre]||b.genre}</div>
                  <div className="book-stats">
                    <span>{b.word_count.toLocaleString()}字 · {b.chapter_count}章</span>
                    <button className="del-btn" onClick={e=>{e.stopPropagation();del(b.id,b.title);}}>×</button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </main>

      <div className="home-fab">
        <button className="fab-btn" onClick={()=>setShowNew(true)} title="新建作品">+</button>
      </div>

      {showNew && (
        <div className="modal-overlay" onClick={()=>setShowNew(false)}>
          <div className="modal" onClick={e=>e.stopPropagation()}>
            <h2>新建作品</h2>
            <div className="form-group"><label>书名 *</label><input value={nf.title} onChange={e=>setNf({...nf,title:e.target.value})} placeholder="输入书名"/></div>
            <div className="form-group"><label>作者</label><input value={nf.author} onChange={e=>setNf({...nf,author:e.target.value})} placeholder="笔名"/></div>
            <div style={{display:'flex',gap:8}}>
              <div className="form-group" style={{flex:1}}><label>类型</label><select value={nf.book_type} onChange={e=>handleBookTypeChange(e.target.value)}><option value="novel">长篇</option><option value="short_story">短篇</option></select></div>
              <div className="form-group" style={{flex:1}}><label>题材</label><select value={nf.genre} onChange={e=>handleGenreChange(e.target.value)}>
                {GENRE_GROUPS.map(g => (
                  <optgroup key={g.label} label={g.label}>
                    {g.keys.map(k => <option key={k} value={k}>{GENRES[k] || k}</option>)}
                  </optgroup>
                ))}
              </select></div>
            </div>
            {(() => {
              const range = getVolumeRange(nf.book_type);
              const tv = nf.total_volumes || range.default;
              return (
                <div className="form-group">
                  <label>总卷数（{range.min}-{range.max}） · {range.perVolumeWords}</label>
                  <div style={{display:'flex',alignItems:'center',gap:10}}>
                    <input type="range" min={range.min} max={range.max} value={tv} onChange={e=>setNf({...nf,total_volumes:Number(e.target.value)})} style={{flex:1}}/>
                    <span style={{minWidth:48,textAlign:'center',fontWeight:600,color:'var(--accent)'}}>{tv} {nf.book_type==='short_story'?'篇':'卷'}</span>
                  </div>
                  {nf.book_type==='novel' && <div style={{fontSize:11,color:'var(--text-muted)',marginTop:4}}>预计总字数约 {tv*12} 万字（每卷约12万字，约50章/卷）</div>}
                </div>
              );
            })()}
            {(() => {
              const styles = getStylesForGenre(nf.book_type, nf.genre);
              return (
                <div className="form-group">
                  <label>风格流派（随题材变化 · 可多选最多3种 · 已选 {nf.novel_styles.length}/3）</label>
                  <div style={{display:'flex',flexWrap:'wrap',gap:6,maxHeight:120,overflowY:'auto'}}>
                    {Object.entries(styles).map(([k,v])=>{
                      const sel = nf.novel_styles.includes(k);
                      const disabled = !sel && nf.novel_styles.length>=3;
                      return (
                        <button key={k} type="button" onClick={()=>toggleStyle(k)} disabled={disabled}
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
            <div className="form-group"><label>简介</label><textarea rows={2} value={nf.synopsis} onChange={e=>setNf({...nf,synopsis:e.target.value})} placeholder="简单描述..."/></div>
            <div className="form-group"><label>模板</label><select value={nf.template_id} onChange={e=>setNf({...nf,template_id:e.target.value})}><option value="">不用模板</option>{templates.map(t=><option key={t.id} value={t.id}>{t.name}</option>)}</select></div>
            <div className="form-actions"><button className="btn-secondary" onClick={()=>setShowNew(false)}>取消</button><button className="btn-primary" onClick={create}>创建</button></div>
          </div>
        </div>
      )}

      {/* AI 智驾 - 作品选择 */}
      {showAiPicker && (
        <div className="modal-overlay" onClick={()=>setShowAiPicker(false)}>
          <div className="modal" onClick={e=>e.stopPropagation()} style={{maxWidth:480}}>
            <h2 style={{marginBottom:4, display:'flex', alignItems:'center', gap:8}}><CarLogo size={22} /> AI 智驾</h2>
            <p className="text-muted" style={{fontSize:13,marginBottom:16}}>选择要创作的作品，进入后可使用设定/正文/去AI/校审四Tab协作</p>
            {books.length === 0 ? (
              <div className="empty-state" style={{padding:24}}>
                <p>还没有作品，请先新建</p>
                <button className="btn-primary" style={{marginTop:12}} onClick={()=>{setShowAiPicker(false);setShowNew(true);}}>+ 新建作品</button>
              </div>
            ) : (
              <div className="ai-picker-list" style={{display:'flex',flexDirection:'column',gap:8,maxHeight:360,overflowY:'auto'}}>
                {books.map((b:any)=>(
                  <button
                    key={b.id}
                    className="ai-picker-item"
                    onClick={()=>{ setShowAiPicker(false); navigate(`/write?book=${b.id}&ai=global`); }}
                    style={{
                      display:'flex',alignItems:'center',gap:12,padding:'10px 12px',
                      background:'var(--bg-secondary)',border:'1px solid var(--border-color)',
                      borderRadius:8,cursor:'pointer',textAlign:'left',
                    }}
                  >
                    <span style={{fontSize:24}}>{b.cover_path ? '📖' : '📚'}</span>
                    <div style={{flex:1,minWidth:0}}>
                      <div style={{fontWeight:600,fontSize:14,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{b.title}</div>
                      <div style={{fontSize:11,color:'var(--text-muted)'}}>{b.author&&b.author+' · '}{GENRES[b.genre]||b.genre} · {b.word_count.toLocaleString()}字 · {b.chapter_count}章</div>
                    </div>
                    <span style={{color:'var(--accent)'}}>→</span>
                  </button>
                ))}
              </div>
            )}
            <div className="form-actions" style={{marginTop:16}}>
              <button className="btn-secondary" onClick={()=>setShowAiPicker(false)}>取消</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
