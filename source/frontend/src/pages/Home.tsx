import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import type { Template } from '../types';
import { useStore } from '../store';
import { api } from '../api';

const GENRES: Record<string, string> = { fantasy:'玄幻', romance:'言情', mystery:'悬疑', scifi:'科幻', wuxia:'武侠', historical:'历史', horror:'恐怖', comedy:'喜剧', other:'其他' };
const S: Record<string,{label:string;color:string}> = { draft:{label:'草稿',color:'#9e8f6e'}, writing:{label:'连载',color:'#27ae60'}, completed:{label:'完结',color:'#2980b9'} };

export default function Home() {
  const { books, setBooks, currentUser, theme, setTheme } = useStore() as any;
  const navigate = useNavigate();
  const [showNew, setShowNew] = useState(false);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [nf, setNf] = useState({ title:'',author:'',genre:'other',book_type:'short_story',synopsis:'',template_id:'',target_words:0 });
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => { api.listTemplates().then(setTemplates).catch(()=>{}); }, []);

  const refresh = () => api.listBooks().then(setBooks);

  const create = async () => {
    try { const b = await api.createBook(nf); setShowNew(false); refresh(); navigate(`/write?book=${b.id}`); }
    catch(e:any){ alert(e.message); }
  };

  const del = async (id:string,t:string) => {
    if(!confirm(`删除《${t}》？`))return;
    await api.deleteBook(id); refresh();
  };

  const doLogout = () => { localStorage.removeItem('fanshu-token'); window.location.reload(); };

  return (
    <div className="home-root">
      <header className="home-header">
        <div className="home-brand">
          <img src="shouye.png" alt="蚂蚁写作" style={{height:40,objectFit:'contain',boxShadow:'var(--shadow-sm)',borderRadius:6}} />
          <div><h1>蚂蚁写作</h1><p className="home-subtitle">AI小说创作平台</p></div>
        </div>
        <div className="home-actions">
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
              <div className="form-group" style={{flex:1}}><label>类型</label><select value={nf.book_type} onChange={e=>setNf({...nf,book_type:e.target.value})}><option value="short_story">短篇</option><option value="novel">长篇</option><option value="script">剧本</option></select></div>
              <div className="form-group" style={{flex:1}}><label>题材</label><select value={nf.genre} onChange={e=>setNf({...nf,genre:e.target.value})}>{Object.entries(GENRES).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></div>
            </div>
            <div className="form-group"><label>简介</label><textarea rows={2} value={nf.synopsis} onChange={e=>setNf({...nf,synopsis:e.target.value})} placeholder="简单描述..."/></div>
            <div className="form-group"><label>模板</label><select value={nf.template_id} onChange={e=>setNf({...nf,template_id:e.target.value})}><option value="">不用模板</option>{templates.map(t=><option key={t.id} value={t.id}>{t.name}</option>)}</select></div>
            <div className="form-actions"><button className="btn-secondary" onClick={()=>setShowNew(false)}>取消</button><button className="btn-primary" onClick={create}>创建</button></div>
          </div>
        </div>
      )}
    </div>
  );
}
