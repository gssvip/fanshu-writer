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
  const [showAiPicker, setShowAiPicker] = useState(false);

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
            title={books.length === 0 ? '请先创建作品' : '选择作品进入 AI 总创作（全维度协同生成）'}
            style={{ background: 'linear-gradient(135deg,#7cb89e 0%,#5ba3a8 100%)' }}
          >
            ✨ AI总创作
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
              <div className="form-group" style={{flex:1}}><label>类型</label><select value={nf.book_type} onChange={e=>setNf({...nf,book_type:e.target.value})}><option value="short_story">短篇</option><option value="novel">长篇</option><option value="script">剧本</option></select></div>
              <div className="form-group" style={{flex:1}}><label>题材</label><select value={nf.genre} onChange={e=>setNf({...nf,genre:e.target.value})}>{Object.entries(GENRES).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></div>
            </div>
            <div className="form-group"><label>简介</label><textarea rows={2} value={nf.synopsis} onChange={e=>setNf({...nf,synopsis:e.target.value})} placeholder="简单描述..."/></div>
            <div className="form-group"><label>模板</label><select value={nf.template_id} onChange={e=>setNf({...nf,template_id:e.target.value})}><option value="">不用模板</option>{templates.map(t=><option key={t.id} value={t.id}>{t.name}</option>)}</select></div>
            <div className="form-actions"><button className="btn-secondary" onClick={()=>setShowNew(false)}>取消</button><button className="btn-primary" onClick={create}>创建</button></div>
          </div>
        </div>
      )}

      {/* AI总创作 - 作品选择 */}
      {showAiPicker && (
        <div className="modal-overlay" onClick={()=>setShowAiPicker(false)}>
          <div className="modal" onClick={e=>e.stopPropagation()} style={{maxWidth:480}}>
            <h2 style={{marginBottom:4}}>✨ AI总创作</h2>
            <p className="text-muted" style={{fontSize:13,marginBottom:16}}>选择要创作的作品，进入后可对构思/设定/大纲/人物等维度协同生成</p>
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
