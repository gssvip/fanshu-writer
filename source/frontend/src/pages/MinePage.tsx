import { useState, useEffect, useContext, useRef } from 'react';
import { useStore } from '../store';
import { api, getApiBaseUrl, setApiBaseUrl } from '../api';
import { AuthContext } from '../App';
import type { AIConfig } from '../types';
import type { Book } from '../types';

export default function MinePage() {
  const { currentUser, theme, customColors, setTheme, setCustomColors, setCurrentUser, logout } = useStore() as any;
  const { requireAuth } = useContext(AuthContext);
  const [aiConfig, setAIConfig] = useState<AIConfig>({ id: '', name: '默认配置', is_active: true, provider: 'deepseek', model: 'deepseek-chat', recognition_model: '', api_key: '', base_url: 'https://api.deepseek.com/v1', temperature: 0.7, max_tokens: 4000, has_key: false });
  // 多配置支持：最多 3 个，可切换
  const [configList, setConfigList] = useState<AIConfig[]>([]);
  const [maxConfigs] = useState(3);
  const [showApiKey, setShowApiKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [activeSection, setActiveSection] = useState('');
  const [stats, setStats] = useState({ totalBooks: 0, totalWords: 0, totalChapters: 0 });
  const [writingHist, setWritingHist] = useState({ todayWords: 0, streak: 0 });

  // AI 模型拉取与测试连接
  const [fetchingModels, setFetchingModels] = useState(false);
  const [modelList, setModelList] = useState<{ id: string; owned_by: string }[]>([]);
  const [showModelList, setShowModelList] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; msg: string } | null>(null);

  // 识别模型独立设置
  const [useSeparateRecognition, setUseSeparateRecognition] = useState(false);
  const [showRecognitionModelList, setShowRecognitionModelList] = useState(false);
  const [fetchingRecModels, setFetchingRecModels] = useState(false);

  // 自定义大模型列表（本地存储）
  const [customModels, setCustomModels] = useState<{ name: string; base_url: string; model: string }[]>([]);
  const [showAddCustom, setShowAddCustom] = useState(false);
  const [customModelForm, setCustomModelForm] = useState({ name: '', base_url: '', model: '' });

  // 本地存储设置
  const [localPath, setLocalPath] = useState('');
  const [storageMode, setStorageMode] = useState<'cloud' | 'local'>('cloud');
  const [exporting, setExporting] = useState(false);
  const folderInputRef = useRef<HTMLInputElement>(null);

  // 修改密码
  const [pwdForm, setPwdForm] = useState({ old: '', new: '', confirm: '' });
  const [pwdSaving, setPwdSaving] = useState(false);
  const [pwdMsg, setPwdMsg] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  async function handleChangePassword() {
    setPwdMsg(null);
    if (!pwdForm.old || !pwdForm.new) { setPwdMsg({ type: 'error', text: '请输入原密码和新密码' }); return; }
    if (pwdForm.new.length < 4) { setPwdMsg({ type: 'error', text: '新密码至少4个字符' }); return; }
    if (pwdForm.new !== pwdForm.confirm) { setPwdMsg({ type: 'error', text: '两次输入的新密码不一致' }); return; }
    setPwdSaving(true);
    try {
      await api.changePassword(pwdForm.old, pwdForm.new);
      setPwdMsg({ type: 'success', text: '密码修改成功！下次请使用新密码登录' });
      setPwdForm({ old: '', new: '', confirm: '' });
    } catch (e: any) {
      setPwdMsg({ type: 'error', text: e.message || '修改失败' });
    }
    setPwdSaving(false);
  }

  // 后端服务器地址配置
  const [serverUrl, setServerUrl] = useState('');
  const [serverStatus, setServerStatus] = useState<'idle' | 'testing' | 'ok' | 'fail'>('idle');

  useEffect(() => {
    const savedPath = localStorage.getItem('fanshu-local-path') || '';
    const savedMode = (localStorage.getItem('fanshu-storage-mode') as 'cloud' | 'local') || 'cloud';
    setLocalPath(savedPath);
    setStorageMode(savedMode);
    // 加载自定义大模型列表
    try {
      const saved = localStorage.getItem('fanshu-custom-models');
      if (saved) setCustomModels(JSON.parse(saved));
    } catch { /* ignore */ }
    // 清理已废弃的背景图片配置（功能已移除）
    localStorage.removeItem('fanshu-bg-image');
    localStorage.removeItem('fanshu-bg-opacity');
    const bgDiv = document.getElementById('bg-image-layer');
    if (bgDiv) bgDiv.remove();
    document.body.style.backgroundImage = '';
    // 加载后端服务器地址配置
    setServerUrl(localStorage.getItem('fanshu-api-base-url') || '');
  }, []);

  useEffect(() => {
    // 当AI配置加载后，同步识别模型开关状态
    if (aiConfig.recognition_model) {
      setUseSeparateRecognition(true);
    }
  }, [aiConfig.recognition_model]);

  useEffect(() => {
    // 读取写作打卡数据
    try {
      const raw = localStorage.getItem('fanshu-writing-history');
      if (raw) {
        const hist = JSON.parse(raw);
        const today = new Date().toISOString().slice(0, 10);
        setWritingHist({
          todayWords: hist.lastDate === today ? hist.todayWords || 0 : 0,
          streak: hist.streak || 0,
        });
      }
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    if (!currentUser) return;
    // 已登录用户进入"我的"页面时，默认展开"账户安全"分区，方便修改密码
    setActiveSection('account');
    refreshConfigs();
    api.listBooks().then((books: Book[]) => {
      setStats({
        totalBooks: books.length,
        totalWords: books.reduce((s, b) => s + (b.word_count || 0), 0),
        totalChapters: books.reduce((s, b) => s + (b.chapter_count || 0), 0),
      });
    }).catch(() => {});
  }, [currentUser]);

  // 拉取全部配置列表 + 当前激活配置，保证 UI 与后端一致
  function refreshConfigs() {
    api.listAIConfigs().then((res) => {
      setConfigList(res.configs);
      const active = res.configs.find(c => c.is_active) || res.configs[0];
      if (active) setAIConfig(active);
    }).catch(() => {
      // 兼容旧后端：list 接口不存在时回退到单配置接口
      api.getAIConfig().then(setAIConfig).catch(() => {});
    });
  }

  // 切换激活配置：切换后立即重新加载该配置详情
  async function handleSwitchConfig(id: string) {
    if (id === aiConfig.id) return;
    try {
      const cfg = await api.activateAIConfig(id);
      setAIConfig(cfg);
      await refreshConfigs();
    } catch (e: any) {
      alert('切换失败: ' + e.message);
    }
  }

  // 新建配置：新建后自动激活并切换到编辑该配置
  async function handleNewConfig() {
    if (configList.length >= maxConfigs) {
      alert(`最多 ${maxConfigs} 个配置，请先删除一个`);
      return;
    }
    const name = prompt('新配置名称', `配置 ${configList.length + 1}`);
    if (!name) return;
    try {
      const cfg = await api.createAIConfig({ name });
      setAIConfig(cfg);
      await refreshConfigs();
    } catch (e: any) {
      alert('新建失败: ' + e.message);
    }
  }

  // 删除配置：删除激活配置时后端自动激活剩下首条，前端刷新
  async function handleDeleteConfig(id: string) {
    if (!confirm('确认删除该配置？')) return;
    try {
      await api.deleteAIConfig(id);
      await refreshConfigs();
    } catch (e: any) {
      alert('删除失败: ' + e.message);
    }
  }

  async function handleSaveAIConfig() {
    const ok = await requireAuth();
    if (!ok) return;
    setSaving(true);
    try {
      const cfg = await api.updateAIConfig(aiConfig);
      setAIConfig(cfg);
      await refreshConfigs();
      alert('AI配置已保存');
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setSaving(false);
  }

  async function handleFetchModels() {
    if (!aiConfig.base_url.trim()) { alert('请先填写 API 地址'); return; }
    if (!aiConfig.api_key.trim()) { alert('请先填写 API Key'); return; }
    setFetchingModels(true);
    setTestResult(null);
    try {
      const result = await api.fetchAIModels(aiConfig.base_url, aiConfig.api_key);
      setModelList(result.models);
      setShowModelList(true);
      if (result.models.length === 0) {
        alert('该接口未返回任何模型，可能是提供商不支持 /v1/models 接口');
      }
    } catch (e: any) {
      alert('拉取模型失败：' + e.message);
    }
    setFetchingModels(false);
  }

  async function handleTestConnection() {
    if (!aiConfig.base_url.trim()) { alert('请先填写 API 地址'); return; }
    if (!aiConfig.api_key.trim()) { alert('请先填写 API Key'); return; }
    if (!aiConfig.model.trim()) { alert('请先填写模型名称'); return; }
    setTesting(true);
    setTestResult(null);
    try {
      const result = await api.testAIConnection(aiConfig.base_url, aiConfig.api_key, aiConfig.model);
      setTestResult({ success: true, msg: `连接成功！模型回复：${result.reply}` });
    } catch (e: any) {
      setTestResult({ success: false, msg: e.message || '连接失败' });
    }
    setTesting(false);
  }

  function handleSaveCustomModel() {
    if (!customModelForm.name.trim() || !customModelForm.base_url.trim() || !customModelForm.model.trim()) {
      alert('请填写完整的名称、API地址和模型名称');
      return;
    }
    const updated = [...customModels, { ...customModelForm }];
    setCustomModels(updated);
    localStorage.setItem('fanshu-custom-models', JSON.stringify(updated));
    setCustomModelForm({ name: '', base_url: '', model: '' });
    setShowAddCustom(false);
    alert('自定义模型已保存');
  }

  function handleDeleteCustomModel(idx: number) {
    const updated = customModels.filter((_, i) => i !== idx);
    setCustomModels(updated);
    localStorage.setItem('fanshu-custom-models', JSON.stringify(updated));
  }

  function handleApplyCustomModel(m: { name: string; base_url: string; model: string }) {
    setAIConfig((prev: AIConfig) => ({ ...prev, provider: 'custom', base_url: m.base_url, model: m.model }));
  }

  function handleToggleRecognition(on: boolean) {
    setUseSeparateRecognition(on);
    if (!on) {
      // 关闭独立设置时，清空识别模型，回退使用创作模型
      setAIConfig((prev: AIConfig) => ({ ...prev, recognition_model: '' }));
    }
  }

  async function handleFetchRecModels() {
    if (!aiConfig.base_url.trim()) { alert('请先填写 API 地址'); return; }
    if (!aiConfig.api_key.trim()) { alert('请先填写 API Key'); return; }
    setFetchingRecModels(true);
    try {
      const result = await api.fetchAIModels(aiConfig.base_url, aiConfig.api_key);
      setModelList(result.models);
      setShowRecognitionModelList(true);
      if (result.models.length === 0) {
        alert('该接口未返回任何模型');
      }
    } catch (e: any) {
      alert('拉取模型失败：' + e.message);
    }
    setFetchingRecModels(false);
  }

  function handleFolderSelect(e: React.ChangeEvent<HTMLInputElement>) {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    // 通过 webkitRelativePath 获取文件夹路径
    const relativePath = files[0].webkitRelativePath;
    if (relativePath) {
      const folderName = relativePath.split('/')[0];
      setLocalPath(folderName);
    }
    if (folderInputRef.current) folderInputRef.current.value = '';
  }

  async function handlePickDirectory() {
    // 优先使用 File System Access API
    const w = window as any;
    if (w.showDirectoryPicker) {
      try {
        const dirHandle = await w.showDirectoryPicker();
        setLocalPath(dirHandle.name);
      } catch {
        // 用户取消，忽略
      }
    } else {
      // 回退到 input[webkitdirectory]
      folderInputRef.current?.click();
    }
  }

  function handleLogout() {
    api.logout().then(() => {
      setCurrentUser(null);
      logout?.();
    }).catch(() => {
      setCurrentUser(null);
      logout?.();
    });
  }

  const SECTIONS = [
    { key: 'server', label: '服务器', icon: '🌐' },
    { key: 'ai', label: 'AI 配置', icon: '🤖' },
    { key: 'storage', label: '本地存储', icon: '💾' },
    { key: 'theme', label: '主题', icon: '🎨' },
    { key: 'account', label: '账户安全', icon: '🔐' },
    { key: 'about', label: '关于', icon: 'ℹ️' },
  ];

  function toggleSection(key: string) {
    setActiveSection(prev => prev === key ? '' : key);
  }

  function handleSaveStorage() {
    localStorage.setItem('fanshu-local-path', localPath);
    localStorage.setItem('fanshu-storage-mode', storageMode);
    alert(storageMode === 'local' ? '已切换为本地存储模式，数据将保存在浏览器中' : '已切换为云端存储模式');
  }

  function handleSaveServerUrl() {
    const url = serverUrl.trim().replace(/\/+$/, '');
    setApiBaseUrl(url);
    alert(url ? '服务器地址已保存，即将刷新页面生效' : '已恢复默认服务器地址，即将刷新页面');
    setTimeout(() => window.location.reload(), 500);
  }

  async function handleTestServerUrl() {
    const url = serverUrl.trim().replace(/\/+$/, '');
    if (!url) { alert('请先填写服务器地址'); return; }
    setServerStatus('testing');
    try {
      // 用与实际 API 调用一致的 URL 构造方式测试
      // getApiBaseUrl() 会自动补全 /api 后缀
      const apiUrl = url.endsWith('/api') ? url : url + '/api';
      const res = await fetch(`${apiUrl}/templates`, { method: 'GET' });
      if (res.ok) {
        setServerStatus('ok');
      } else {
        setServerStatus('fail');
      }
    } catch (e: any) {
      setServerStatus('fail');
    }
  }

  async function handleExportLocal() {
    setExporting(true);
    try {
      const books = await api.listBooks();
      const allData: Record<string, any> = { books: {}, exportedAt: new Date().toISOString() };
      for (const book of books) {
        try {
          const chapters = await api.listChapters(book.id);
          let bible: any = null;
          try { bible = await api.getBible(book.id); } catch { /* ignore */ }
          allData.books[book.id] = { book, chapters, bible };
        } catch { /* skip */ }
      }
      const blob = new Blob([JSON.stringify(allData, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `fanshu-backup-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      alert(`已导出 ${Object.keys(allData.books).length} 部作品到本地`);
    } catch (e: any) {
      alert('导出失败: ' + e.message);
    }
    setExporting(false);
  }

  return (
    <div className="page mine-page">
      <header className="page-header">
        <h1
          onClick={() => currentUser && setActiveSection('account')}
          style={currentUser ? { cursor: 'pointer' } : undefined}
          title={currentUser ? '点击修改密码' : undefined}
        >
          设置
        </h1>
        <div style={{display:'flex',alignItems:'center',gap:8}}>
          {currentUser ? (
            <>
              <button
                className="btn-ghost-sm"
                onClick={() => setActiveSection('account')}
                title="点击修改密码"
                style={{ fontWeight: 600, color: 'var(--accent)' }}
              >
                👤 {currentUser.username}
              </button>
              <button className="btn-primary-sm" onClick={() => setActiveSection('account')} title="修改密码">
                🔐 修改密码
              </button>
              <button className="btn-ghost-sm" onClick={handleLogout}>退出</button>
            </>
          ) : (
            <button className="btn-primary-sm" onClick={() => requireAuth()}>登录 / 注册</button>
          )}
        </div>
      </header>

      {currentUser && (
        <>
          <div className="mine-stats">
            <div className="stat-item">
              <div className="stat-value">{stats.totalBooks}</div>
              <div className="stat-label">作品</div>
            </div>
            <div className="stat-item">
              <div className="stat-value">{stats.totalWords.toLocaleString()}</div>
              <div className="stat-label">总字数</div>
            </div>
            <div className="stat-item">
              <div className="stat-value">{stats.totalChapters}</div>
              <div className="stat-label">章节</div>
            </div>
          </div>

          {/* 写作打卡统计 */}
          <div className="stats-card">
            <div className="stats-row">
              <div className="stats-item">
                <div className="stats-value">{writingHist.todayWords}</div>
                <div className="stats-label">今日字数</div>
              </div>
              <div className="stats-item">
                <div className="stats-streak">
                  <span className="stats-streak-badge">{writingHist.streak}天</span>
                </div>
                <div className="stats-label">连续打卡</div>
              </div>
              <div className="stats-item">
                <div className="stats-value">{stats.totalWords > 0 ? Math.round(stats.totalWords / Math.max(1, stats.totalBooks)) : 0}</div>
                <div className="stats-label">篇均字数</div>
              </div>
            </div>
          </div>
        </>
      )}

      <nav className="mine-nav">
        {SECTIONS.map(s => (
          <button key={s.key} className={`mine-nav-item ${activeSection === s.key ? 'active' : ''}`} onClick={() => toggleSection(s.key)}>
            <span>{s.icon}</span>
            <span>{s.label}</span>
            <span className={`nav-arrow ${activeSection === s.key ? 'expanded' : ''}`}>▸</span>
          </button>
        ))}
      </nav>

      <div className="mine-content">
        {activeSection === 'ai' && (
          <div className="tool-panel">
            <h3>AI 配置</h3>
            <p className="text-muted">配置国产大模型 API，让AI帮你写作和审稿。所有提供商均兼容 OpenAI 接口格式。</p>

            {/* 多配置切换：最多 3 个，旧配置保留不丢 */}
            <div className="config-switcher" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12, padding: '8px 10px', background: 'var(--bg-soft, #f7f7f8)', borderRadius: 8, flexWrap: 'wrap' }}>
              <label style={{ fontSize: 13, color: 'var(--text-muted, #888)', marginRight: 4 }}>当前配置：</label>
              <select
                className="input"
                value={aiConfig.id}
                onChange={e => handleSwitchConfig(e.target.value)}
                style={{ flex: 1, minWidth: 160, maxWidth: 280 }}
              >
                {configList.map(c => (
                  <option key={c.id} value={c.id}>
                    {c.name}{c.is_active ? ' ✓' : ''} {c.has_key ? '🔑' : '🚫'}
                  </option>
                ))}
              </select>
              <button
                className="btn-primary"
                onClick={handleNewConfig}
                disabled={configList.length >= maxConfigs}
                style={{ padding: '6px 12px', fontSize: 13 }}
                title={configList.length >= maxConfigs ? `最多 ${maxConfigs} 个配置` : '新建配置'}
              >
                ＋ 新建
              </button>
              <button
                className="btn-icon"
                onClick={() => handleDeleteConfig(aiConfig.id)}
                disabled={configList.length <= 1}
                style={{ fontSize: 13, padding: '6px 10px' }}
                title={configList.length <= 1 ? '至少保留 1 个配置' : '删除当前配置'}
              >
                🗑️ 删除
              </button>
              <span style={{ fontSize: 12, color: 'var(--text-muted, #888)' }}>
                {configList.length} / {maxConfigs}
              </span>
            </div>

            {/* 配置名称编辑 */}
            <div className="form-row" style={{ marginBottom: 12 }}>
              <label>配置名称</label>
              <input
                className="input"
                value={aiConfig.name}
                onChange={e => setAIConfig((p: AIConfig) => ({ ...p, name: e.target.value }))}
                placeholder="如：DeepSeek / 备用 / 公司账号"
              />
            </div>

            {/* 自定义大模型 */}
            {customModels.length > 0 && (
              <div className="custom-models-section">
                <div className="custom-models-header">
                  <span className="custom-models-title">🏷️ 我的自定义模型</span>
                  <button className="btn-icon" title="删除" onClick={() => { if (confirm('清空所有自定义模型？')) { setCustomModels([]); localStorage.removeItem('fanshu-custom-models'); } }}>🗑️</button>
                </div>
                <div className="custom-models-list">
                  {customModels.map((m, i) => (
                    <div key={i} className={`custom-model-chip ${aiConfig.base_url === m.base_url && aiConfig.model === m.model ? 'active' : ''}`}>
                      <button className="custom-model-info" onClick={() => handleApplyCustomModel(m)}>
                        <span className="custom-model-name">{m.name}</span>
                        <span className="custom-model-detail">{m.model}</span>
                      </button>
                      <button className="btn-icon" title="删除" onClick={() => handleDeleteCustomModel(i)}>✕</button>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {showAddCustom && (
              <div className="custom-model-form">
                <input className="input" placeholder="模型名称（如：我的本地模型）" value={customModelForm.name} onChange={e => setCustomModelForm(prev => ({ ...prev, name: e.target.value }))} />
                <input className="input" placeholder="API地址（如：http://localhost:11434/v1）" value={customModelForm.base_url} onChange={e => setCustomModelForm(prev => ({ ...prev, base_url: e.target.value }))} />
                <input className="input" placeholder="模型ID（如：llama3:8b）" value={customModelForm.model} onChange={e => setCustomModelForm(prev => ({ ...prev, model: e.target.value }))} />
                <div className="form-row" style={{justifyContent:'flex-end'}}>
                  <button className="btn-ghost-sm" onClick={() => setShowAddCustom(false)}>取消</button>
                  <button className="btn-primary-sm" onClick={handleSaveCustomModel}>保存</button>
                </div>
              </div>
            )}

            <div className="form-field">
              <label>API提供商</label>
              <div className="input-row">
                <select className="input" value={aiConfig.provider} onChange={e => {
                  const val = e.target.value;
                  if (val === 'custom') {
                    setAIConfig((prev: AIConfig) => ({ ...prev, provider: 'custom', base_url: '', model: '' }));
                  } else {
                    const p = AI_PROVIDERS.find(x => x.value === val);
                    if (p) setAIConfig((prev: AIConfig) => ({ ...prev, provider: p.value, base_url: p.base_url, model: p.model }));
                  }
                }}>
                  {AI_PROVIDERS.map(p => <option key={p.value} value={p.value}>{p.label}</option>)}
                  <option value="custom">自定义</option>
                </select>
                <button className="btn-ghost-sm" onClick={() => setShowAddCustom(!showAddCustom)} title="添加自定义大模型配置">
                  + 自定义
                </button>
              </div>
            </div>
            <div className="form-field">
              <label>API地址</label>
              <input className="input" value={aiConfig.base_url} onChange={e => setAIConfig((p: AIConfig) => ({ ...p, base_url: e.target.value }))} placeholder="https://api.example.com/v1" />
            </div>
            <div className="form-field">
              <label>模型名称</label>
              <div className="input-row">
                <input className="input" value={aiConfig.model} onChange={e => setAIConfig((p: AIConfig) => ({ ...p, model: e.target.value }))} placeholder="model-name" />
                <button className="btn-ghost-sm" onClick={handleFetchModels} disabled={fetchingModels} title="根据API地址和Key拉取可用模型列表">
                  {fetchingModels ? '⏳' : '🔄 拉取'}
                </button>
              </div>
              {showModelList && modelList.length > 0 && (
                <div className="model-list-panel">
                  <div className="model-list-header">
                    <span>共 {modelList.length} 个可用模型</span>
                    <button className="btn-icon" onClick={() => setShowModelList(false)}>✕</button>
                  </div>
                  <div className="model-list-items">
                    {modelList.map(m => (
                      <button
                        key={m.id}
                        className={`model-item ${aiConfig.model === m.id ? 'active' : ''}`}
                        onClick={() => { setAIConfig((p: AIConfig) => ({ ...p, model: m.id })); setShowModelList(false); }}
                      >
                        <span className="model-item-id">{m.id}</span>
                        {m.owned_by && <span className="model-item-owner">{m.owned_by}</span>}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
            <div className="form-field">
              <label>API Key <span className={`key-status ${aiConfig.has_key ? 'set' : 'unset'}`}>{aiConfig.has_key ? '(已设置)' : '(未设置)'}</span></label>
              <div className="input-row">
                <input className="input" type={showApiKey ? 'text' : 'password'} value={aiConfig.api_key}
                  onChange={e => setAIConfig((p: AIConfig) => ({ ...p, api_key: e.target.value }))}
                  placeholder="输入你的API Key" />
                <button className="btn-ghost-sm" onClick={() => setShowApiKey(!showApiKey)}>{showApiKey ? '隐藏' : '显示'}</button>
              </div>
            </div>

            {/* 识别模型独立设置 */}
            <div className="recognition-model-section">
              <div className="recognition-toggle-row">
                <label className="recognition-toggle-label">
                  <input
                    type="checkbox"
                    checked={useSeparateRecognition}
                    onChange={e => handleToggleRecognition(e.target.checked)}
                  />
                  <span>🔍 为AI识别单独设置模型</span>
                </label>
                <p className="text-muted" style={{fontSize:11,marginTop:2}}>关闭时识别和创作使用同一模型</p>
              </div>
              {useSeparateRecognition && (
                <div className="form-field" style={{marginTop:8}}>
                  <label>识别专用模型</label>
                  <div className="input-row">
                    <input
                      className="input"
                      value={aiConfig.recognition_model}
                      onChange={e => setAIConfig((p: AIConfig) => ({ ...p, recognition_model: e.target.value }))}
                      placeholder="如：deepseek-chat（识别用模型）"
                    />
                    <button className="btn-ghost-sm" onClick={handleFetchRecModels} disabled={fetchingRecModels} title="拉取可用模型">
                      {fetchingRecModels ? '⏳' : '🔄 拉取'}
                    </button>
                  </div>
                  {showRecognitionModelList && modelList.length > 0 && (
                    <div className="model-list-panel">
                      <div className="model-list-header">
                        <span>共 {modelList.length} 个可用模型</span>
                        <button className="btn-icon" onClick={() => setShowRecognitionModelList(false)}>✕</button>
                      </div>
                      <div className="model-list-items">
                        {modelList.map(m => (
                          <button
                            key={m.id}
                            className={`model-item ${aiConfig.recognition_model === m.id ? 'active' : ''}`}
                            onClick={() => { setAIConfig((p: AIConfig) => ({ ...p, recognition_model: m.id })); setShowRecognitionModelList(false); }}
                          >
                            <span className="model-item-id">{m.id}</span>
                            {m.owned_by && <span className="model-item-owner">{m.owned_by}</span>}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>

            <div className="form-row">
              <div className="form-field">
                <label>温度 ({aiConfig.temperature})</label>
                <input className="input" type="range" min={0} max={2} step={0.1} value={aiConfig.temperature}
                  onChange={e => setAIConfig((p: AIConfig) => ({ ...p, temperature: parseFloat(e.target.value) }))} />
              </div>
              <div className="form-field">
                <label>最大Token ({aiConfig.max_tokens})</label>
                <input className="input" type="range" min={1000} max={64000} step={500} value={aiConfig.max_tokens}
                  onChange={e => setAIConfig((p: AIConfig) => ({ ...p, max_tokens: parseInt(e.target.value) }))} />
              </div>
            </div>

            <div className="ai-action-row">
              <button className="btn-primary" onClick={handleSaveAIConfig} disabled={saving}>
                {saving ? '保存中...' : '保存AI配置'}
              </button>
              <button className="btn-secondary ai-test-btn" onClick={handleTestConnection} disabled={testing}>
                {testing ? '⏳ 测试中...' : '🔌 测试连接'}
              </button>
            </div>
            {testResult && (
              <div className={`test-result ${testResult.success ? 'success' : 'error'}`}>
                <span className="test-result-icon">{testResult.success ? '✅' : '❌'}</span>
                <span className="test-result-msg">{testResult.msg}</span>
              </div>
            )}
          </div>
        )}

        {activeSection === 'storage' && (
          <div className="tool-panel">
            <h3>本地存储设置</h3>
            <p className="text-muted">将所有作品数据保存到手机本地，无需服务器即可离线使用</p>

            <div className="form-field">
              <label>存储模式</label>
              <div className="storage-mode-toggle">
                <button className={`storage-mode-btn ${storageMode === 'cloud' ? 'active' : ''}`} onClick={() => setStorageMode('cloud')}>
                  <span className="storage-mode-icon">☁️</span>
                  <span>云端同步</span>
                  <span className="storage-mode-desc">数据保存在服务器，多设备同步</span>
                </button>
                <button className={`storage-mode-btn ${storageMode === 'local' ? 'active' : ''}`} onClick={() => setStorageMode('local')}>
                  <span className="storage-mode-icon">📱</span>
                  <span>本地存储</span>
                  <span className="storage-mode-desc">数据保存在手机浏览器，离线可用</span>
                </button>
              </div>
            </div>

            {storageMode === 'local' && (
              <>
                <div className="form-field">
                  <label>本地存储路径（用于备份导出）</label>
                  <div className="input-row">
                    <input className="input" value={localPath} onChange={e => setLocalPath(e.target.value)} placeholder="点击选择文件夹" readOnly style={{flex:1}} />
                    <button className="btn-ghost-sm" onClick={handlePickDirectory} title="选择本地文件夹">📁 选择</button>
                  </div>
                  <input
                    ref={folderInputRef}
                    type="file"
                    style={{display:'none'}}
                    // @ts-ignore
                    webkitdirectory=""
                    directory=""
                    multiple
                    onChange={handleFolderSelect}
                  />
                  <p className="text-muted" style={{fontSize:11,marginTop:4}}>选择文件夹用于标识备份位置，实际数据存储在浏览器 IndexedDB 中</p>
                </div>

                <div className="storage-info-card">
                  <div className="storage-info-row">
                    <span>📦 已用空间</span>
                    <span>{((localStorage.length * 0.5 + JSON.stringify(localStorage).length) / 1024).toFixed(1)} KB</span>
                  </div>
                  <div className="storage-info-row">
                    <span>📊 缓存条目</span>
                    <span>{localStorage.length} 条</span>
                  </div>
                </div>

                <button className="btn-secondary" onClick={handleExportLocal} disabled={exporting} style={{marginTop:12,width:'100%'}}>
                  {exporting ? '⏳ 导出中...' : '📥 导出全部数据到本地文件'}
                </button>
                <p className="text-muted" style={{fontSize:11,marginTop:6}}>导出为 JSON 文件，可保存到手机任意位置</p>
              </>
            )}

            <button className="btn-primary" onClick={handleSaveStorage} style={{marginTop:12}}>保存设置</button>
          </div>
        )}

        {activeSection === 'theme' && (
          <div className="tool-panel">
            <h3>主题设置</h3>
            <div className="theme-options">
              <button className={`theme-card ${theme === 'light' ? 'active' : ''}`} onClick={() => setTheme('light')}>
                <div className="theme-preview light" />
                <span>浅色</span>
              </button>
              <button className={`theme-card ${theme === 'green' ? 'active' : ''}`} onClick={() => setTheme('green')}>
                <div className="theme-preview green" />
                <span>护眼绿</span>
              </button>
              <button className={`theme-card ${theme === 'dark' ? 'active' : ''}`} onClick={() => setTheme('dark')}>
                <div className="theme-preview dark" />
                <span>深色</span>
              </button>
              <button className={`theme-card ${theme === 'custom' ? 'active' : ''}`} onClick={() => setTheme('custom')}>
                <div className="theme-preview custom" style={{ background: customColors?.bgPrimary || '#f0f7f0' }} />
                <span>自定义</span>
              </button>
            </div>
            <p className="text-muted" style={{ marginTop: 10 }}>护眼绿适合长时间码字，深色适合夜间写作</p>

            {theme === 'custom' && customColors && (
              <div className="custom-theme-editor">
                <h4>自定义配色</h4>
                <div className="color-grid">
                  <label className="color-field">
                    <span>背景主色</span>
                    <input type="color" value={customColors.bgPrimary} onChange={e => setCustomColors({ bgPrimary: e.target.value })} />
                  </label>
                  <label className="color-field">
                    <span>背景次色</span>
                    <input type="color" value={customColors.bgSecondary} onChange={e => setCustomColors({ bgSecondary: e.target.value })} />
                  </label>
                  <label className="color-field">
                    <span>背景辅色</span>
                    <input type="color" value={customColors.bgTertiary} onChange={e => setCustomColors({ bgTertiary: e.target.value })} />
                  </label>
                  <label className="color-field">
                    <span>主文字</span>
                    <input type="color" value={customColors.textPrimary} onChange={e => setCustomColors({ textPrimary: e.target.value })} />
                  </label>
                  <label className="color-field">
                    <span>次文字</span>
                    <input type="color" value={customColors.textSecondary} onChange={e => setCustomColors({ textSecondary: e.target.value })} />
                  </label>
                  <label className="color-field">
                    <span>弱化文字</span>
                    <input type="color" value={customColors.textMuted} onChange={e => setCustomColors({ textMuted: e.target.value })} />
                  </label>
                  <label className="color-field">
                    <span>强调色</span>
                    <input type="color" value={customColors.accent} onChange={e => setCustomColors({ accent: e.target.value })} />
                  </label>
                  <label className="color-field">
                    <span>边框色</span>
                    <input type="color" value={customColors.borderColor} onChange={e => setCustomColors({ borderColor: e.target.value })} />
                  </label>
                </div>
                <button className="btn-ghost-sm" style={{ marginTop: 10 }} onClick={() => setCustomColors({
                  bgPrimary: '#f0f7f0', bgSecondary: '#e8f3e8', bgTertiary: '#d6e8d6',
                  textPrimary: '#2d3e2d', textSecondary: '#4a6b4a', textMuted: '#7a9a7a',
                  accent: '#4a8b4a', borderColor: '#c0d8c0',
                })}>恢复默认</button>
              </div>
            )}
          </div>
        )}

        {activeSection === 'server' && (
          <div className="tool-panel">
            <h3>🌐 后端服务器</h3>
            <p className="text-muted">系统已内置默认后端服务器，开箱即用。如需切换到自部署的后端，可在下方覆盖。</p>

            <div style={{ padding: '10px 12px', background: '#e8f7e8', color: '#27ae60', borderRadius: 8, fontSize: 13, marginBottom: 12 }}>
              ✅ 已使用内置默认后端服务器，无需配置即可使用登录注册等功能。
            </div>

            <div className="form-field">
              <label>自定义后端服务器地址 <span style={{color:'#888',fontSize:11}}>（可选，留空使用内置默认地址）</span></label>
              <div className="input-row">
                <input
                  className="input"
                  value={serverUrl}
                  onChange={e => setServerUrl(e.target.value)}
                  placeholder="留空使用默认地址，或填自部署后端地址"
                  style={{ flex: 1 }}
                />
                <button className="btn-ghost-sm" onClick={handleTestServerUrl} disabled={serverStatus === 'testing'}>
                  {serverStatus === 'testing' ? '⏳' : '🔌 测试'}
                </button>
              </div>
              {serverStatus === 'ok' && (
                <div style={{ color: '#27ae60', fontSize: 12, marginTop: 4 }}>✅ 连接成功</div>
              )}
              {serverStatus === 'fail' && (
                <div style={{ color: '#e74c3c', fontSize: 12, marginTop: 4 }}>❌ 连接失败，请检查地址或后端是否启动</div>
              )}
              <p className="text-muted" style={{ fontSize: 11, marginTop: 6 }}>
                当前生效地址：{getApiBaseUrl()}<br />
                填写后端根地址即可（无需带 <code>/api</code>），系统会自动拼接 <code>/api</code> 前缀。<br />
                留空则使用内置默认地址，普通用户无需配置。
              </p>
            </div>

            <div className="form-row" style={{ marginTop: 8 }}>
              <button className="btn-primary" onClick={handleSaveServerUrl}>保存并刷新</button>
              <button className="btn-ghost-sm" onClick={() => { setServerUrl(''); setServerStatus('idle'); }}>清空</button>
            </div>

            <div style={{ marginTop: 16, padding: 12, background: 'var(--bg-tertiary)', borderRadius: 8, fontSize: 12 }}>
              <h4 style={{ marginBottom: 8, fontSize: 13 }}>📦 自部署后端（可选）</h4>
              <p style={{ marginBottom: 8 }}>如果你想使用自己的后端服务器，可以参考以下步骤部署：</p>
              <ol style={{ paddingLeft: 18, lineHeight: 1.8 }}>
                <li>将 <code>source/backend</code> 目录部署到 Render / Railway / Hugging Face Spaces 等平台</li>
                <li>确保安装 <code>requirements.txt</code> 中的依赖</li>
                <li>启动命令：<code>python app.py</code>（端口通过 <code>PORT</code> 环境变量指定）</li>
                <li>将平台分配的域名填入上方，点击「测试」确认连接，再「保存并刷新」</li>
              </ol>
            </div>
          </div>
        )}

        {activeSection === 'account' && (
          <div className="tool-panel">
            <h3>🔐 账户安全</h3>

            {currentUser ? (
              <>
                <div style={{ padding: '10px 12px', background: 'var(--bg-tertiary)', borderRadius: 8, fontSize: 13, marginBottom: 16 }}>
                  <div>当前账号：<b>{currentUser.username}</b></div>
                  <div style={{ color: 'var(--text-muted)', marginTop: 4 }}>邮箱：{currentUser.email || '未设置'}</div>
                </div>

                <h4 style={{ margin: '12px 0 8px', fontSize: 14 }}>修改密码</h4>
                <div className="form-field">
                  <label>原密码</label>
                  <input className="input" type="password" placeholder="请输入当前密码" value={pwdForm.old}
                    onChange={e => setPwdForm(prev => ({ ...prev, old: e.target.value }))} />
                </div>
                <div className="form-field">
                  <label>新密码</label>
                  <input className="input" type="password" placeholder="至少4个字符" value={pwdForm.new}
                    onChange={e => setPwdForm(prev => ({ ...prev, new: e.target.value }))} />
                </div>
                <div className="form-field">
                  <label>确认新密码</label>
                  <input className="input" type="password" placeholder="再次输入新密码" value={pwdForm.confirm}
                    onChange={e => setPwdForm(prev => ({ ...prev, confirm: e.target.value }))}
                    onKeyDown={e => { if (e.key === 'Enter') handleChangePassword(); }} />
                </div>

                {pwdMsg && (
                  <div style={{
                    padding: '8px 12px', borderRadius: 6, fontSize: 13, marginBottom: 10,
                    background: pwdMsg.type === 'success' ? '#e8f7e8' : '#fde8e8',
                    color: pwdMsg.type === 'success' ? '#27ae60' : '#e74c3c'
                  }}>
                    {pwdMsg.type === 'success' ? '✅ ' : '❌ '}{pwdMsg.text}
                  </div>
                )}

                <div className="form-row">
                  <button className="btn-primary" onClick={handleChangePassword} disabled={pwdSaving}>
                    {pwdSaving ? '保存中...' : '保存新密码'}
                  </button>
                  <button className="btn-ghost-sm" onClick={() => { setPwdForm({ old: '', new: '', confirm: '' }); setPwdMsg(null); }}>清空</button>
                </div>

                <div style={{ marginTop: 16, padding: 12, background: 'var(--bg-tertiary)', borderRadius: 8, fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.7 }}>
                  <b>💡 找回密码</b><br />
                  忘记密码时，可在登录页点击「忘记密码？」通过注册邮箱接收重置链接，重置邮件将通过 <code>xiyiji@88.com</code> 发送。
                </div>
              </>
            ) : (
              <div className="empty-state" style={{padding:30}}>
                <div className="empty-icon">🔒</div>
                <p>请先登录后再管理账户安全</p>
                <button className="btn-primary-sm" style={{marginTop:12}} onClick={() => requireAuth()}>登录 / 注册</button>
              </div>
            )}
          </div>
        )}

        {activeSection === 'about' && (
          <div className="tool-panel">
            <h3>关于蚂蚁写作</h3>
            <p>AI原生创作工作台，专为中文网文作者打造。</p>
            <div className="about-features">
              <div className="about-feature"><b>项目宪法</b> - 世界观、人设、伏笔结构化管理，确保超长篇一致性</div>
              <div className="about-feature"><b>AI 责编</b> - 平台视角审稿，7维度打分</div>
              <div className="about-feature"><b>技能包</b> - 15+专业写作流程一键安装</div>
              <div className="about-feature"><b>拆书分析</b> - 导入文件分析，提炼爆款方法</div>
              <div className="about-feature"><b>Vibe Writing</b> - 作者主导，AI辅助，保留个人风格</div>
              <div className="about-feature"><b>文件导入</b> - 支持txt/md/docx/zip上传，提取内容写作</div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** 国产 AI 提供商预设（均兼容 OpenAI 接口格式） */
const AI_PROVIDERS = [
  { value: 'deepseek', label: 'DeepSeek 深度求索', icon: '🔵', base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
  { value: 'qwen', label: '通义千问 阿里', icon: '🟠', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' },
  { value: 'glm', label: '智谱GLM', icon: '🟢', base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash' },
  { value: 'kimi', label: 'Kimi 月之暗面', icon: '🌙', base_url: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-8k' },
  { value: 'ernie', label: '文心一言 百度', icon: '🔴', base_url: 'https://qianfan.baidubce.com/v2', model: 'ernie-4.0-8k-latest' },
  { value: 'spark', label: '讯飞星火', icon: '⭐', base_url: 'https://spark-api-open.xf-yun.com/v1', model: 'generalv3.5' },
  { value: 'yi', label: '零一万物', icon: '🟣', base_url: 'https://api.lingyiwanwu.com/v1', model: 'yi-large' },
  { value: 'minimax', label: 'MiniMax', icon: '⚫', base_url: 'https://api.minimax.chat/v1', model: 'abab6.5s-chat' },
  { value: 'hunyuan', label: '腾讯混元', icon: '🔷', base_url: 'https://api.hunyuan.cloud.tencent.com/v1', model: 'hunyuan-pro' },
  { value: 'openai', label: 'OpenAI', icon: '🤖', base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
  { value: 'opencode', label: 'OpenCode Zen 免费', icon: '⚡', base_url: 'https://opencode.ai/zen/v1', model: 'glm-4.7-free' },
];
