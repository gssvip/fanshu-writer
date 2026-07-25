import { useState, useEffect, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { api } from '../api';
import type { Book, Character, Outline, AISession, AIMessage, StatsData, StageItem, PromptT } from '../types';

export default function BookEditor() {
  const { bookId } = useParams<{ bookId: string }>();
  const navigate = useNavigate();
  const [book, setBook] = useState<Book|null>(null);
  const [stages, setStages] = useState<StageItem[]>([]);
  const [activeStage, setActiveStage] = useState('character_design');
  const [content, setContent] = useState('');
  const [saving, setSaving] = useState(false);
  const [lastSaved, setLastSaved] = useState<Date|null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(window.innerWidth>768);
  const [rightPanel, setRightPanel] = useState<'ai'|'characters'|'outline'|'stats'|null>(null);
  const saveTimer = useRef<ReturnType<typeof setTimeout>|null>(null);
  const [expandedParent, setExpandedParent] = useState('plot_design');
  const [showPrompts, setShowPrompts] = useState(false);
  const [prompts, setPrompts] = useState<PromptT[]>([]);
  const [mobileTab, setMobileTab] = useState<'stages'|'editor'|'tools'>('editor');
  const isMobile = window.innerWidth <= 768;

  useEffect(() => {
    if(!bookId)return;
    api.getBook(bookId).then(setBook);
    api.listStages(bookId).then(s=>{
      setStages(s);
      const first=s.find(x=>!x.parent)||s[0];
      if(first){setActiveStage(first.key);setContent(first.content||'');}
    });
  },[bookId]);

  const switchStage = async (stageKey:string) => {
    setActiveStage(stageKey);
    const stage=stages.find(s=>s.key===stageKey);
    setContent(stage?.content||'');
    try{const data=await api.getStage(bookId!,stageKey);setContent(data.content||'');}catch{}
    if(isMobile) setMobileTab('editor');
  };

  const doSave = async () => {
    if(!bookId)return; setSaving(true);
    try{await api.saveStage(bookId,activeStage,content);setLastSaved(new Date());}catch(e){console.error(e);}
    setSaving(false);
  };

  useEffect(()=>{
    const h=(e:KeyboardEvent)=>{if((e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();doSave();}};
    window.addEventListener('keydown',h); return ()=>window.removeEventListener('keydown',h);
  },[content,activeStage]);

  useEffect(()=>{
    if(saveTimer.current)clearTimeout(saveTimer.current);
    saveTimer.current=setTimeout(doSave,3000);
    return ()=>{if(saveTimer.current)clearTimeout(saveTimer.current);};
  },[content]);

  const loadPrompts = async () => {
    if(!book)return;
    const ps=await api.listPrompts(book.book_type,activeStage);
    setPrompts(ps); setShowPrompts(true);
  };

  const wc = content?content.replace(/\s+/g,' ').trim().split(/\s+/).filter(Boolean).length:0;
  const visibleStages = stages.filter(s=>!s.parent);
  const children = (pk:string)=>stages.filter(s=>s.parent===pk);
  const currentStage = stages.find(s=>s.key===activeStage);

  return (
    <div className="editor-root">
      {/* Header */}
      <header className="editor-header">
        <button className="btn-ghost" onClick={()=>navigate('/')}>&larr;</button>
        <div className="header-title"><h2>{book?.title||''}</h2></div>
        <div className="header-actions">
          <button className="btn-ghost mob-hide" onClick={loadPrompts}>提示词</button>
          <button className="btn-ghost" onClick={doSave} disabled={saving}>{saving?'...':'保存'}</button>
        </div>
      </header>

      <div className="editor-body">
        {/* Desktop Sidebar */}
        <aside className={`stage-sidebar ${sidebarOpen?'open':''}`}>
          <div className="sidebar-header">
            <span className="sidebar-title">创作阶段</span>
            <button className="btn-icon" onClick={()=>setSidebarOpen(false)}>&times;</button>
          </div>
          {visibleStages.map(s=>(
            <div key={s.key}>
              <div className={`stage-nav-item ${activeStage===s.key?'active':''}`}
                onClick={()=>{if(s.is_parent)setExpandedParent(expandedParent===s.key?'':s.key);switchStage(s.key);}}>
                <span className="stage-icon">{s.icon}</span>
                <div className="stage-info"><span className="stage-label">{s.label}</span></div>
                {s.is_parent&&<span>{expandedParent===s.key?'▾':'▸'}</span>}
              </div>
              {s.is_parent&&expandedParent===s.key&&children(s.key).map(cs=>(
                <div key={cs.key} className={`stage-nav-item sub ${activeStage===cs.key?'active':''}`}
                  onClick={()=>switchStage(cs.key)}>
                  <span className="stage-icon">{cs.icon}</span>
                  <div className="stage-info"><span className="stage-label">{cs.label}</span></div>
                </div>
              ))}
            </div>
          ))}
        </aside>

        {/* Center Editor */}
        <main className={`editor-main ${mobileTab==='stages'?'mob-hidden':''} ${mobileTab==='tools'&&rightPanel?'mob-hidden':''}`}>
          <div className="editor-toolbar">
            <button className="btn-icon mob-hide" onClick={()=>setSidebarOpen(!sidebarOpen)}>&#9776;</button>
            <div className="toolbar-info">
              <span className="stage-badge">{currentStage?.icon} {currentStage?.label}</span>
              <span className="wc-badge">{wc.toLocaleString()}字</span>
              {lastSaved&&<span className="saved-badge">已保存</span>}
            </div>
            <div className="toolbar-actions mob-hide">
              <PanelBtn icon="💬" label="AI" active={rightPanel==='ai'} onClick={()=>setRightPanel(rightPanel==='ai'?null:'ai')}/>
              <PanelBtn icon="👤" label="人物" active={rightPanel==='characters'} onClick={()=>setRightPanel(rightPanel==='characters'?null:'characters')}/>
              <PanelBtn icon="🌳" label="大纲" active={rightPanel==='outline'} onClick={()=>setRightPanel(rightPanel==='outline'?null:'outline')}/>
              <PanelBtn icon="📊" label="统计" active={rightPanel==='stats'} onClick={()=>setRightPanel(rightPanel==='stats'?null:'stats')}/>
            </div>
          </div>

          <div className="editor-content-area">
            <textarea className="stage-editor" value={content}
              onChange={e=>setContent(e.target.value)} placeholder="开始写作..."/>

            {/* Desktop Right Panel */}
            {rightPanel&&!isMobile&&(
              <div className="right-panel">
                <div className="panel-header">
                  <span>{rightPanel==='ai'?'AI助手':rightPanel==='characters'?'人物':rightPanel==='outline'?'大纲':'统计'}</span>
                  <button className="btn-icon" onClick={()=>setRightPanel(null)}>&times;</button>
                </div>
                <div className="panel-body">
                  {rightPanel==='ai'&&<AiPanel bookId={bookId!} book={book} stageKey={activeStage} stageContent={content}/>}
                  {rightPanel==='characters'&&<CharPanel bookId={bookId!}/>}
                  {rightPanel==='outline'&&<OutPanel bookId={bookId!}/>}
                  {rightPanel==='stats'&&<StatPanel bookId={bookId!}/>}
                </div>
              </div>
            )}
          </div>
        </main>

        {/* Mobile Fullscreen Panels */}
        {isMobile && rightPanel && mobileTab==='tools' && (
          <div className="mob-full-panel">
            <div className="panel-header">
              <span>{rightPanel==='ai'?'AI助手':rightPanel==='characters'?'人物':rightPanel==='outline'?'大纲':'统计'}</span>
              <button className="btn-icon" onClick={()=>{setRightPanel(null);setMobileTab('editor');}}>&times;</button>
            </div>
            <div className="panel-body">
              {rightPanel==='ai'&&<AiPanel bookId={bookId!} book={book} stageKey={activeStage} stageContent={content}/>}
              {rightPanel==='characters'&&<CharPanel bookId={bookId!}/>}
              {rightPanel==='outline'&&<OutPanel bookId={bookId!}/>}
              {rightPanel==='stats'&&<StatPanel bookId={bookId!}/>}
            </div>
          </div>
        )}

        {/* Mobile Stages Drawer */}
        {isMobile && mobileTab==='stages' && (
          <div className="mob-full-panel">
            <div className="panel-header">
              <span>创作阶段</span>
              <button className="btn-icon" onClick={()=>setMobileTab('editor')}>&times;</button>
            </div>
            <div className="panel-body">
              {visibleStages.map(s=>(
                <div key={s.key}>
                  <div className={`mob-stage-item ${activeStage===s.key?'active':''}`}
                    onClick={()=>{if(s.is_parent)setExpandedParent(expandedParent===s.key?'':s.key);switchStage(s.key);}}>
                    <span>{s.icon} {s.label}</span>
                    <span style={{fontSize:11,color:'var(--text-muted)'}}>{s.desc}</span>
                    {s.is_parent&&<span>{expandedParent===s.key?'▾':'▸'}</span>}
                  </div>
                  {s.is_parent&&expandedParent===s.key&&children(s.key).map(cs=>(
                    <div key={cs.key} className={`mob-stage-item sub ${activeStage===cs.key?'active':''}`}
                      onClick={()=>switchStage(cs.key)}>
                      <span>{cs.icon} {cs.label}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Mobile Bottom Tab Bar */}
      {isMobile && (
        <nav className="mob-tabs">
          <button className={`mob-tab ${mobileTab==='stages'?'active':''}`} onClick={()=>setMobileTab('stages')}>
            <span style={{fontSize:20}}>📋</span><span>阶段</span>
          </button>
          <button className={`mob-tab ${mobileTab==='editor'?'active':''}`} onClick={()=>{setRightPanel(null);setMobileTab('editor');}}>
            <span style={{fontSize:20}}>✏️</span><span>编辑</span>
          </button>
          <button className={`mob-tab ${mobileTab==='tools'?'active':''}`} onClick={()=>setMobileTab('tools')}>
            <span style={{fontSize:20}}>🛠</span><span>工具</span>
          </button>
        </nav>
      )}

      {/* Mobile Tools Picker */}
      {isMobile && mobileTab==='tools' && !rightPanel && (
        <div className="mob-full-panel">
          <div className="panel-header"><span>功能工具</span><button className="btn-icon" onClick={()=>setMobileTab('editor')}>&times;</button></div>
          <div className="panel-body" style={{padding:16}}>
            <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:12}}>
              <ToolCard icon="💬" label="AI 助手" desc="对话、续写、润色" onClick={()=>setRightPanel('ai')}/>
              <ToolCard icon="👤" label="人物管理" desc="创建和管理角色" onClick={()=>setRightPanel('characters')}/>
              <ToolCard icon="🌳" label="大纲管理" desc="幕/章/场景结构" onClick={()=>setRightPanel('outline')}/>
              <ToolCard icon="📊" label="写作统计" desc="字数趋势分析" onClick={()=>setRightPanel('stats')}/>
            </div>
            <button className="btn-primary" style={{width:'100%',marginTop:16}} onClick={loadPrompts}>📝 提示词模板库</button>
          </div>
        </div>
      )}

      {/* Prompts Modal */}
      {showPrompts&&(
        <div className="modal-overlay" onClick={()=>setShowPrompts(false)}>
          <div className="modal" onClick={e=>e.stopPropagation()}>
            <h2>提示词模板库</h2>
            <div style={{maxHeight:'60vh',overflow:'auto'}}>
              {prompts.map(p=>(
                <div key={p.id} className="prompt-card" onClick={()=>{setContent(p.content);setShowPrompts(false);}}>
                  <div style={{display:'flex',justifyContent:'space-between'}}>
                    <strong>{p.name}</strong>
                    {p.is_builtin&&<span className="builtin-tag">内置</span>}
                  </div>
                  <div style={{fontSize:12,color:'var(--text-secondary)'}}>{p.description}</div>
                </div>
              ))}
            </div>
            <div className="form-actions"><button className="btn-secondary" onClick={()=>setShowPrompts(false)}>关闭</button></div>
          </div>
        </div>
      )}
    </div>
  );
}

function PanelBtn({icon,label,active,onClick}:{icon:string;label:string;active:boolean;onClick:()=>void}) {
  return <button className={`panel-btn ${active?'active':''}`} onClick={onClick}>{icon} {label}</button>;
}

function ToolCard({icon,label,desc,onClick}:{icon:string;label:string;desc:string;onClick:()=>void}) {
  return (
    <div onClick={onClick} style={{
      padding:16,background:'var(--bg-tertiary)',borderRadius:'var(--radius-md)',cursor:'pointer',
      textAlign:'center',transition:'all .2s'
    }}>
      <div style={{fontSize:28,marginBottom:8}}>{icon}</div>
      <div style={{fontWeight:600,fontSize:14}}>{label}</div>
      <div style={{fontSize:11,color:'var(--text-muted)',marginTop:4}}>{desc}</div>
    </div>
  );
}

/* ======= SUB-PANELS ======= */

function AiPanel({bookId,book,stageKey,stageContent}:{bookId:string;book:Book|null;stageKey:string;stageContent:string}) {
  const [session,setSession]=useState<AISession|null>(null);
  const [msgs,setMsgs]=useState<AIMessage[]>([]);
  const [input,setInput]=useState('');
  const [loading,setLoading]=useState(false);
  const ref=useRef<HTMLDivElement>(null);
  useEffect(()=>{ref.current&&(ref.current.scrollTop=ref.current.scrollHeight);},[msgs]);

  const send = async () => {
    if(!input.trim()||loading)return;
    const um:AIMessage={role:'user',content:input};
    const nm=[...msgs,um];setMsgs(nm);setInput('');
    if(!session){const s=await api.createAISession({book_id:bookId,title:input.slice(0,30),scope:stageKey});setSession(s);await api.updateAISession(s.id,{messages:nm});}
    else await api.updateAISession(session.id,{messages:nm});
    setLoading(true);
    try{
      const sm:AIMessage={role:'system',content:`你是番薯写作助手。\n作品：《${book?.title||''}》\n阶段：${stageKey}\n\n${stageContent.slice(0,1500)}`};
      const res=await api.aiChatStream([sm,...nm]);
      const r=res.body?.getReader(); if(!r)throw new Error('X');
      let ac='';setMsgs(p=>[...p,{role:'assistant',content:''}]);
      const d=new TextDecoder();
      while(true){
        const{done,value}=await r.read();if(done)break;
        for(const l of d.decode(value,{stream:true}).split('\n')){
          if(l.startsWith('data: ')&&l!=='data: [DONE]'){
            try{ac+=JSON.parse(l.slice(6)).choices?.[0]?.delta?.content||'';}catch{}
            setMsgs(p=>{const c=[...p];c[c.length-1]={role:'assistant',content:ac};return c;});
          }
        }
      }
      const fm:AIMessage[]=[...nm,{role:'assistant' as const,content:ac}];
      if(session)await api.updateAISession(session.id,{messages:fm});
    }catch(e:any){setMsgs(p=>[...p,{role:'assistant',content:'错误: '+e.message}]);}
    setLoading(false);
  };

  return (
    <div className="ai-panel">
      <div className="ai-chat" ref={ref}>
        {msgs.length===0&&<div className="empty-state" style={{padding:32}}><p>向AI提问、续写、润色...</p></div>}
        {msgs.map((m,i)=>(
          <div key={i} className={`chat-msg ${m.role}`}>
            <div className="chat-role">{m.role==='user'?'你':'AI'}</div>
            <div style={{whiteSpace:'pre-wrap',fontSize:14}}>{m.content}</div>
          </div>
        ))}
      </div>
      <div className="ai-input">
        <input value={input} onChange={e=>setInput(e.target.value)} onKeyDown={e=>e.key==='Enter'&&!e.shiftKey&&send()} placeholder="输入消息..." disabled={loading}/>
        <button className="btn-primary" onClick={send} disabled={loading} style={{fontSize:13,flexShrink:0}}>{loading?'...':'发送'}</button>
      </div>
    </div>
  );
}

function CharPanel({bookId}:{bookId:string}) {
  const [chars,setChars]=useState<Character[]>([]);
  const [show,setShow]=useState(false);
  const [f,setF]=useState({name:'',role:'supporting',desc:'',app:'',per:'',bg:''});
  const [edit,setEdit]=useState<Character|null>(null);
  useEffect(()=>{api.listCharacters(bookId).then(setChars);},[bookId]);
  const save=async()=>{
    if(!f.name.trim())return;
    if(edit){const c=await api.updateCharacter(bookId,edit.id,f as any);setChars(p=>p.map(x=>x.id===c.id?c:x));}
    else{const c=await api.createCharacter(bookId,f as any);setChars(p=>[...p,c]);}
    setShow(false);setEdit(null);
  };
  const rl:Record<string,string>={protagonist:'主角',antagonist:'对手',supporting:'配角'};
  return (
    <div style={{padding:12}}>
      <button className="btn-primary" style={{width:'100%',fontSize:13,marginBottom:12}} onClick={()=>{setEdit(null);setShow(true);}}>+ 添加角色</button>
      {show&&(
        <div style={{marginBottom:12,padding:12,background:'var(--bg-tertiary)',borderRadius:8}}>
          <div className="form-group"><label>名字</label><input value={f.name} onChange={e=>setF({...f,name:e.target.value})}/></div>
          <div className="form-group"><label>定位</label><select value={f.role} onChange={e=>setF({...f,role:e.target.value})}>{Object.entries(rl).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></div>
          <div className="form-group"><label>描述</label><textarea rows={2} value={f.desc} onChange={e=>setF({...f,desc:e.target.value})}/></div>
          <div className="form-actions"><button className="btn-secondary" style={{fontSize:12}} onClick={()=>setShow(false)}>取消</button><button className="btn-primary" style={{fontSize:12}} onClick={save}>保存</button></div>
        </div>
      )}
      {chars.map(c=>(
        <div key={c.id} style={{padding:10,marginBottom:6,background:'var(--bg-tertiary)',borderRadius:8,cursor:'pointer'}}
          onClick={()=>{setEdit(c);setF({name:c.name,role:c.role,desc:c.description,app:c.appearance,per:c.personality,bg:c.background});setShow(true);}}>
          <div style={{display:'flex',justifyContent:'space-between'}}>
            <strong>{c.name}</strong>
            <span style={{fontSize:10,padding:'1px 6px',borderRadius:8,background:'var(--accent)',color:'#fff'}}>{rl[c.role]}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function OutPanel({bookId}:{bookId:string}) {
  const [tree,setTree]=useState<Outline[]>([]);
  const [flat,setFlat]=useState<Outline[]>([]);
  const [show,setShow]=useState(false);
  const [f,setF]=useState({title:'',content:'',level:0,parent_id:''});
  const [edit,setEdit]=useState<Outline|null>(null);
  const refresh=async()=>{const d=await api.listOutlines(bookId);setTree(d.tree);setFlat(d.flat);};
  useEffect(()=>{refresh();},[bookId]);
  const save=async()=>{
    if(edit)await api.updateOutline(bookId,edit.id,f);else await api.createOutline(bookId,f);
    setShow(false);setEdit(null);refresh();
  };
  const render=(n:Outline,d=0):any=>(
    <div key={n.id}>
      <div style={{padding:`8px 12px 8px ${12+d*16}px`,fontSize:13,cursor:'pointer',borderBottom:'1px solid var(--border-color)',display:'flex',justifyContent:'space-between'}}
        onClick={()=>{setEdit(n);setF({title:n.title,content:n.content,level:n.level,parent_id:n.parent_id});setShow(true);}}>
        <span>{d===0?'📘 ':d===1?'📄 ':'📝 '}{n.title}</span>
        <button className="btn-icon" style={{width:22,height:22}} onClick={e=>{e.stopPropagation();api.deleteOutline(bookId,n.id).then(refresh);}}>&times;</button>
      </div>
      {n.children?.map((c:Outline)=>render(c,d+1))}
    </div>
  );
  return (
    <div style={{padding:12}}>
      <button className="btn-primary" style={{width:'100%',fontSize:13,marginBottom:12}} onClick={()=>{setEdit(null);setF({title:'',content:'',level:0,parent_id:''});setShow(true);}}>+ 添加大纲</button>
      {show&&(
        <div style={{marginBottom:12,padding:12,background:'var(--bg-tertiary)',borderRadius:8}}>
          <div className="form-group"><label>标题</label><input value={f.title} onChange={e=>setF({...f,title:e.target.value})}/></div>
          <div className="form-group"><label>级别</label><select value={f.level} onChange={e=>setF({...f,level:+e.target.value})}><option value={0}>幕</option><option value={1}>章</option><option value={2}>场景</option></select></div>
          <div className="form-group"><label>父节点</label><select value={f.parent_id} onChange={e=>setF({...f,parent_id:e.target.value})}><option value="">无</option>{flat.filter(o=>o.level<f.level).map(o=><option key={o.id} value={o.id}>{o.title}</option>)}</select></div>
          <div className="form-group"><label>内容</label><textarea rows={2} value={f.content} onChange={e=>setF({...f,content:e.target.value})}/></div>
          <div className="form-actions"><button className="btn-secondary" style={{fontSize:12}} onClick={()=>setShow(false)}>取消</button><button className="btn-primary" style={{fontSize:12}} onClick={save}>保存</button></div>
        </div>
      )}
      {tree.map(n=>render(n))}
    </div>
  );
}

function StatPanel({bookId}:{bookId:string}) {
  const [s,setS]=useState<StatsData|null>(null);
  useEffect(()=>{api.getBookStats(bookId).then(setS);},[bookId]);
  if(!s)return <div style={{padding:20,textAlign:'center',color:'var(--text-muted)'}}>加载中...</div>;
  const tw=s.chapters.reduce((a,c)=>a+c.word_count,0);
  const td=s.daily.reduce((a,d)=>a+d.words_written,0);
  const rd=s.daily.slice(-14);
  return (
    <div style={{padding:16}}>
      <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:10,marginBottom:20}}>
        {[{l:'总字数',v:tw.toLocaleString()},{l:'总章节',v:s.chapters.length.toString()},{l:'今日',v:s.daily.find(d=>d.date===new Date().toISOString().slice(0,10))?.words_written?.toLocaleString()||'0'},{l:'日均',v:rd.length>0?Math.round(td/rd.length).toLocaleString():'0'}].map((x,i)=>(
          <div key={i} style={{padding:12,background:'var(--bg-tertiary)',borderRadius:8,textAlign:'center'}}>
            <div style={{fontSize:20,fontWeight:700,color:'var(--accent)'}}>{x.v}</div><div style={{fontSize:11,color:'var(--text-muted)'}}>{x.l}</div>
          </div>
        ))}
      </div>
      <div style={{fontSize:13,color:'var(--text-secondary)',marginBottom:10}}>近14天趋势</div>
      <div style={{display:'flex',alignItems:'flex-end',gap:3,height:80}}>
        {rd.map((d,i)=>{const mx=Math.max(...rd.map(x=>x.words_written),1);
          return <div key={i} style={{flex:1,display:'flex',flexDirection:'column',alignItems:'center',justifyContent:'flex-end',height:'100%'}}>
            <div style={{width:'100%',height:`${Math.max((d.words_written/mx)*100,2)}%`,background:'var(--accent)',borderRadius:'3px 3px 0 0'}}/>
            <span style={{fontSize:9,color:'var(--text-muted)',marginTop:2}}>{d.date.slice(5)}</span>
          </div>;
        })}
      </div>
    </div>
  );
}
