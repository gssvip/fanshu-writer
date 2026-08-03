import { useState, useEffect, useContext, useRef } from 'react';
import * as yaml from 'js-yaml';
import { api } from '../api';
import { AuthContext } from '../App';
import type { Book, SkillPack, ReviewResult, AnalysisResult, WorkflowStep } from '../types';

type ToolTab = 'review' | 'skills' | 'analyze' | 'export';

interface SkillEditorState {
  id: string | null;
  name: string;
  icon: string;
  genre: string;
  book_type: string;
  description: string;
  workflow: WorkflowStep[];
  prompts: Record<string, string>;
  // 【三类无污染】技能包分类：master=构思类 / style=文风类 / review=审查类
  category: 'master' | 'style' | 'review';
  genre_target?: string;  // 文风类专属题材标签
}

export default function ToolsPage() {
  const { requireAuth } = useContext(AuthContext);
  const [activeTab, setActiveTab] = useState<ToolTab>('review');
  const [books, setBooks] = useState<Book[]>([]);
  const [selectedBookId, setSelectedBookId] = useState('');
  const [loading, setLoading] = useState(false);

  const [reviewResult, setReviewResult] = useState<ReviewResult | null>(null);
  const [reviewError, setReviewError] = useState('');

  const [skillPacks, setSkillPacks] = useState<SkillPack[]>([]);
  const [skillGenreFilter, setSkillGenreFilter] = useState('');
  const [skillTypeFilter, setSkillTypeFilter] = useState('');
  const [selectedPack, setSelectedPack] = useState<SkillPack | null>(null);

  // 自定义技能编辑器
  const [showSkillEditor, setShowSkillEditor] = useState(false);
  const [skillEditor, setSkillEditor] = useState<SkillEditorState>(emptySkillEditor());
  const [skillSaving, setSkillSaving] = useState(false);
  const [skillError, setSkillError] = useState('');

  const [analyzeInput, setAnalyzeInput] = useState('');
  const [analyzeResult, setAnalyzeResult] = useState<AnalysisResult | null>(null);
  const [analyzeLoading, setAnalyzeLoading] = useState(false);
  const [uploadFilename, setUploadFilename] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [exportFormat, setExportFormat] = useState('txt');
  const skillImportRef = useRef<HTMLInputElement>(null);

  // 拆书分析同步到作品
  const [showSyncModal, setShowSyncModal] = useState(false);
  const [syncBookId, setSyncBookId] = useState('');
  const [syncMode, setSyncMode] = useState('imitate');
  const [syncing, setSyncing] = useState(false);

  useEffect(() => {
    api.listBooks().then(setBooks).catch(() => {});
    api.listSkillPacks().then(setSkillPacks).catch(() => {});
  }, []);

  useEffect(() => {
    api.listSkillPacks(skillGenreFilter || undefined, skillTypeFilter || undefined)
      .then(setSkillPacks).catch(() => {});
  }, [skillGenreFilter, skillTypeFilter]);

  async function handleReview() {
    if (!selectedBookId) return;
    const ok = await requireAuth();
    if (!ok) return;
    setLoading(true);
    setReviewError('');
    setReviewResult(null);
    try { const r = await api.reviewBook(selectedBookId); setReviewResult(r); }
    catch (e: any) { setReviewError(e.message || '审稿失败'); }
    setLoading(false);
  }

  async function handleApplySkillPack() {
    if (!selectedBookId || !selectedPack) return;
    const ok = await requireAuth();
    if (!ok) return;
    setLoading(true);
    try {
      await api.applySkillPack(selectedBookId, selectedPack.id);
      alert(`已应用技能包：${selectedPack.name}`);
      setSelectedPack(null);
    } catch (e: any) { alert('应用失败: ' + e.message); }
    setLoading(false);
  }

  // ---- 自定义技能包 CRUD ----
  function reloadSkillPacks() {
    api.listSkillPacks(skillGenreFilter || undefined, skillTypeFilter || undefined)
      .then(setSkillPacks).catch(() => {});
  }

  function openCreateSkill() {
    setSkillEditor(emptySkillEditor());
    setSkillError('');
    setShowSkillEditor(true);
  }

  function openEditSkill(pack: SkillPack) {
    setSkillEditor({
      id: pack.id,
      name: pack.name,
      icon: pack.icon || '📦',
      genre: pack.genre || 'other',
      book_type: pack.book_type || 'novel',
      description: pack.description || '',
      workflow: pack.workflow?.length ? pack.workflow : [{ step: 1, name: '', desc: '', prompt_key: '' }],
      prompts: pack.prompts || {},
      category: (pack.category || 'master') as 'master' | 'style' | 'review',
      genre_target: pack.genre_target || '',
    });
    setSkillError('');
    setShowSkillEditor(true);
  }

  async function handleSaveSkill() {
    if (!skillEditor.name.trim()) {
      setSkillError('请填写技能包名称');
      return;
    }
    const ok = await requireAuth();
    if (!ok) return;
    setSkillSaving(true);
    setSkillError('');
    try {
      const payload = {
        name: skillEditor.name.trim(),
        icon: skillEditor.icon || '📦',
        genre: skillEditor.genre,
        book_type: skillEditor.book_type,
        description: skillEditor.description,
        workflow: skillEditor.workflow.filter(w => w.name.trim()),
        prompts: skillEditor.prompts,
        category: skillEditor.category,  // 【三类无污染】保存分类
        genre_target: skillEditor.category === 'style' ? (skillEditor.genre_target || skillEditor.genre) : '',  // 文风类自动关联题材
      };
      if (skillEditor.id) {
        await api.updateSkillPack(skillEditor.id, payload);
      } else {
        await api.createSkillPack(payload);
      }
      setShowSkillEditor(false);
      reloadSkillPacks();
    } catch (e: any) {
      setSkillError(e.message || '保存失败');
    }
    setSkillSaving(false);
  }

  async function handleDeleteSkill(pack: SkillPack) {
    if (!confirm(`确定删除技能包"${pack.name}"？此操作不可撤销。`)) return;
    const ok = await requireAuth();
    if (!ok) return;
    try {
      await api.deleteSkillPack(pack.id);
      if (selectedPack?.id === pack.id) setSelectedPack(null);
      reloadSkillPacks();
    } catch (e: any) {
      alert('删除失败: ' + e.message);
    }
  }

  async function handleCloneSkill(pack: SkillPack) {
    const ok = await requireAuth();
    if (!ok) return;
    const name = prompt('为你的技能包命名：', `${pack.name}（我的副本）`);
    if (!name?.trim()) return;
    try {
      await api.cloneSkillPack(pack.id, name.trim());
      reloadSkillPacks();
      alert('已保存到我的技能包');
    } catch (e: any) {
      alert('克隆失败: ' + e.message);
    }
  }

  async function handleEditBuiltinSkill(pack: SkillPack) {
    const ok = await requireAuth();
    if (!ok) return;
    try {
      // 先克隆，再打开编辑器
      const cloned = await api.cloneSkillPack(pack.id, `${pack.name}（我的副本）`);
      openEditSkill(cloned);
      reloadSkillPacks();
    } catch (e: any) {
      alert('创建副本失败: ' + e.message);
    }
  }

  async function handlePublishSkill(pack: SkillPack) {
    const ok = await requireAuth();
    if (!ok) return;
    if (!confirm(`确定将"${pack.name}"分享到系统技能包？分享后所有用户都能使用。`)) return;
    try {
      await api.publishSkillPack(pack.id);
      reloadSkillPacks();
      alert('已分享到系统技能包，其他用户现在也可以使用了');
    } catch (e: any) {
      alert('分享失败: ' + e.message);
    }
  }

  // 从 GitHub 同步技能包（拉取最新 SKILL.md 更新提示词）
  const [syncingPackId, setSyncingPackId] = useState<string | null>(null);
  async function handleSyncFromGitHub(pack: SkillPack) {
    const ok = await requireAuth();
    if (!ok) return;
    if (!pack.github_source) { alert('该技能包未关联 GitHub 仓库'); return; }
    if (!confirm(`从 GitHub 拉取最新版本？\n源：${pack.github_source}\n这会更新技能包中各步骤的提示词。`)) return;
    setSyncingPackId(pack.id);
    try {
      const res = await api.syncSkillPackFromGitHub(pack.id);
      reloadSkillPacks();
      let msg = `✅ ${res.message}`;
      if (res.errors && res.errors.length > 0) msg += `\n\n部分失败：\n${res.errors.join('\n')}`;
      alert(msg);
    } catch (e: any) {
      alert('同步失败: ' + e.message);
    }
    setSyncingPackId(null);
  }

  function addWorkflowStep() {
    setSkillEditor(prev => ({
      ...prev,
      workflow: [...prev.workflow, { step: prev.workflow.length + 1, name: '', desc: '', prompt_key: '' }],
    }));
  }

  function updateWorkflowStep(idx: number, field: keyof WorkflowStep, val: string) {
    setSkillEditor(prev => ({
      ...prev,
      workflow: prev.workflow.map((w, i) => i === idx ? { ...w, [field]: val, step: i + 1 } : w),
    }));
  }

  function removeWorkflowStep(idx: number) {
    setSkillEditor(prev => ({
      ...prev,
      workflow: prev.workflow.filter((_, i) => i !== idx).map((w, i) => ({ ...w, step: i + 1 })),
    }));
  }

  function addPrompt() {
    setSkillEditor(prev => {
      const n = Object.keys(prev.prompts).length;
      const key = `prompt_${n + 1}`;
      return { ...prev, prompts: { ...prev.prompts, [key]: '' } };
    });
  }

  function updatePromptKey(oldKey: string, newKey: string) {
    setSkillEditor(prev => {
      const entries = Object.entries(prev.prompts);
      const updated: Record<string, string> = {};
      for (const [k, v] of entries) updated[k === oldKey ? newKey : k] = v;
      return { ...prev, prompts: updated };
    });
  }

  function updatePromptValue(key: string, val: string) {
    setSkillEditor(prev => ({ ...prev, prompts: { ...prev.prompts, [key]: val } }));
  }

  function removePrompt(key: string) {
    setSkillEditor(prev => {
      const { [key]: _, ...rest } = prev.prompts;
      return { ...prev, prompts: rest };
    });
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

  async function handleAnalyze() {
    if (!analyzeInput.trim()) return;
    const ok = await requireAuth();
    if (!ok) return;
    setAnalyzeLoading(true);
    setAnalyzeResult(null);
    try { const r = await api.analyzeBook(analyzeInput); setAnalyzeResult(r); }
    catch (e: any) { alert('分析失败: ' + e.message); }
    setAnalyzeLoading(false);
  }

  async function handleExportSkill(pack: SkillPack) {
    const exportData = {
      name: pack.name, icon: pack.icon, genre: pack.genre, book_type: pack.book_type,
      description: pack.description, workflow: pack.workflow, prompts: pack.prompts,
    };
    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${pack.name}-技能包.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function handleImportSkill(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      let rawText = await file.text();
      let data: any = null;

      const ext = file.name.split('.').pop()?.toLowerCase() || '';
      if (ext === 'md') {
        // 优先尝试从 Markdown 代码块中提取 YAML
        const yamlBlockMatch = rawText.match(/```(?:yaml|yml)\s*\r?\n([\s\S]*?)\r?\n?```/);
        if (yamlBlockMatch) {
          try {
            data = yaml.load(yamlBlockMatch[1].trim());
          } catch {
            // YAML 解析失败，继续尝试其他格式
          }
        }
        // 其次尝试 JSON 代码块
        if (!data) {
          const jsonMatch = rawText.match(/```(?:json)?\s*\r?\n([\s\S]*?)\r?\n?```/);
          if (jsonMatch) {
            rawText = jsonMatch[1].trim();
          } else {
            // 回退：尝试从文本中提取第一个 { ... } 块
            const braceMatch = rawText.match(/\{[\s\S]*\}/);
            if (braceMatch) {
              rawText = braceMatch[0];
            }
          }
        }
      }

      // 如果还没解析出数据，尝试 YAML 整体解析（适用于 .yaml/.yml 文件或无代码块的 md）
      if (!data) {
        const trimmed = rawText.trim();
        // 尝试 JSON
        try {
          data = JSON.parse(trimmed);
        } catch {
          // JSON 失败，尝试 YAML
          try {
            data = yaml.load(trimmed);
          } catch {
            alert('无法解析文件内容，请确保文件包含有效的 JSON 或 YAML 数据（md 文件可将 YAML 放在 ```yaml 代码块中）');
            return;
          }
        }
      }

      if (!data || !data.name) { alert('无效的技能包文件：缺少 name 字段'); return; }
      const ok = await requireAuth();
      if (!ok) return;
      const payload = {
        name: data.name,
        icon: data.icon || '📦',
        genre: data.genre || 'other',
        book_type: data.book_type || 'novel',
        description: data.description || '',
        workflow: Array.isArray(data.workflow) ? data.workflow : [],
        prompts: data.prompts || {},
      };
      await api.createSkillPack(payload);
      alert(`技能包 "${data.name}" 导入成功`);
      reloadSkillPacks();
    } catch (err: any) {
      alert('导入失败: ' + (err.message || '请检查文件格式'));
    }
    if (skillImportRef.current) skillImportRef.current.value = '';
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

  function getExportUrl() {
    if (!selectedBookId) return '';
    if (exportFormat === 'zip') return api.getExportZipUrl(selectedBookId);
    return api.getExportUrl(selectedBookId, exportFormat);
  }

  // 带认证的文件下载（避免<a>标签无法发送Authorization头）
  const [downloading, setDownloading] = useState(false);
  async function handleAuthDownload(url: string, fallbackName: string) {
    if (!url || url === '#') return;
    setDownloading(true);
    try {
      const token = localStorage.getItem('fanshu-token');
      const resp = await fetch(url, {
        headers: token ? { 'Authorization': `Bearer ${token}` } : {},
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: '下载失败' }));
        throw new Error(err.error || `HTTP ${resp.status}`);
      }
      const blob = await resp.blob();
      // 从响应头获取文件名，否则用fallback
      const disp = resp.headers.get('content-disposition') || '';
      const m = disp.match(/filename\*?=(?:UTF-8'')?["']?([^"';\n]+)/);
      const fileName = m ? decodeURIComponent(m[1]) : fallbackName;
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = fileName;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(blobUrl);
    } catch (e: any) {
      alert('导出失败: ' + (e.message || '请先登录'));
    }
    setDownloading(false);
  }

  const GENRES: Record<string, string> = {
    'all': '全部', 'other': '通用', 'romance': '言情', 'fantasy': '玄幻',
    'mystery': '悬疑', 'scifi': '科幻', 'history': '历史',
    'urban_business': '都市职场', 'urban_fantasy': '都市异能',
    'military': '军事', 'light_novel': '轻小说',
  };

  const TOOL_TABS = [
    { key: 'review' as ToolTab, label: 'AI 责编', icon: '🔍', desc: 'AI平台视角审稿打分' },
    { key: 'skills' as ToolTab, label: '技能包', icon: '📦', desc: '15+题材工作流套件' },
    { key: 'analyze' as ToolTab, label: '拆书分析', icon: '📊', desc: '导入文件分析提炼方法论' },
    { key: 'export' as ToolTab, label: '导出', icon: '📤', desc: '导出/备份作品' },
  ];

  return (
    <div className="page tools-page">
      <header className="page-header">
        <h1>工具箱</h1>
      </header>

      <div className="tools-grid">
        {TOOL_TABS.map(tab => (
          <button key={tab.key} className={`tool-card ${activeTab === tab.key ? 'active' : ''}`} onClick={() => setActiveTab(tab.key)}>
            <span className="tool-card-icon">{tab.icon}</span>
            <div className="tool-card-info">
              <div className="tool-card-name">{tab.label}</div>
              <div className="tool-card-desc">{tab.desc}</div>
            </div>
          </button>
        ))}
      </div>

      <div className="form-row" style={{margin:'0 16px',marginTop:12}}>
        <label className="input-label">选择作品</label>
        <select className="input" value={selectedBookId} onChange={e => setSelectedBookId(e.target.value)}>
          <option value="">— 选择要操作的作品 —</option>
          {books.map(b => <option key={b.id} value={b.id}>{b.title} ({b.word_count}字)</option>)}
        </select>
      </div>

      {activeTab === 'review' && (
        <div className="tool-panel">
          <h3>🔍 AI 责编审稿</h3>
          <p className="text-muted">从番茄/起点等平台审稿视角，对作品进行7维度打分和商业评估</p>
          <button className="btn-primary" onClick={handleReview} disabled={!selectedBookId || loading}>
            {loading ? '审稿中...' : '开始审稿'}
          </button>
          {reviewError && <div className="error-msg">{reviewError}</div>}
          {reviewResult && (
            <div className="review-result">
              <div className="review-score-header">
                <div className="review-total-score">{reviewResult.total_score}</div>
                <div className="review-grade">
                  <span className={`grade-badge grade-${reviewResult.grade?.toLowerCase()}`}>{reviewResult.grade}级</span>
                  <span>{reviewResult.platform_fit}</span>
                </div>
              </div>
              <div className="review-scores-grid">
                {Object.entries(reviewResult.scores).map(([key, score]) => (
                  <div key={key} className="review-score-item">
                    <div className="review-score-label">{REVIEW_LABELS[key] || key}</div>
                    <div className="review-score-bar"><div className="review-score-fill" style={{ width: `${score}%` }} /></div>
                    <div className="review-score-value">{score}</div>
                  </div>
                ))}
              </div>
              <div className="review-section"><h4>优点</h4><ul>{reviewResult.strengths?.map((s, i) => <li key={i}>{s}</li>)}</ul></div>
              <div className="review-section"><h4>改进建议</h4><ul>{reviewResult.weaknesses?.map((w, i) => <li key={i}>{w}</li>)}</ul></div>
              <div className="review-section"><h4>具体修改方案</h4><ul>{reviewResult.specific_suggestions?.map((s, i) => <li key={i}>{s}</li>)}</ul></div>
            </div>
          )}
        </div>
      )}

      {activeTab === 'skills' && (
        <div className="tool-panel">
          <div className="skills-header">
            <div>
              <h3>📦 技能包市场</h3>
              <p className="text-muted">一键安装专业写作提示词和工作流，包含完整创作链路</p>
            </div>
            <div className="skills-header-actions">
              <input ref={skillImportRef} type="file" accept=".json,.md,.yaml,.yml" onChange={handleImportSkill} style={{display:'none'}} id="skill-import-input" />
              <button className="btn-secondary" onClick={() => skillImportRef.current?.click()}>📥 导入技能</button>
              <button className="btn-primary" onClick={openCreateSkill}>+ 创建自定义</button>
            </div>
          </div>
          <div className="form-row">
            <select className="input" value={skillGenreFilter} onChange={e => setSkillGenreFilter(e.target.value)}>
              <option value="">所有题材</option>
              {Object.entries(GENRES).filter(([k]) => k !== 'all').map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            <select className="input" value={skillTypeFilter} onChange={e => setSkillTypeFilter(e.target.value)}>
              <option value="">所有类型</option>
              <option value="novel">长篇</option>
              <option value="short_story">短篇</option>
            </select>
          </div>
          {/* 【三类无污染】技能包市场按三类分组展示 */}
          {(() => {
            const renderSkillCard = (pack: SkillPack) => (
              <div key={pack.id} className={`skill-card ${selectedPack?.id === pack.id ? 'selected' : ''}`} onClick={() => setSelectedPack(pack)}>
                <div className="skill-card-header">
                  <span className="skill-card-icon">{pack.icon}</span>
                  <div>
                    <div className="skill-card-name">{pack.name}{pack.is_builtin ? <span className="builtin-badge">系统</span> : <span className="custom-badge">自定义</span>}</div>
                    <div className="skill-card-genre">{GENRES[pack.genre] || pack.genre} · {pack.book_type === 'novel' ? '长篇' : '短篇'}</div>
                  </div>
                  <div className="skill-card-actions" onClick={e => e.stopPropagation()}>
                    {pack.is_builtin ? (
                      <>
                        <button className="btn-icon" title="另存为我的技能" onClick={() => handleCloneSkill(pack)}>📋</button>
                        <button className="btn-icon" title="编辑副本" onClick={() => handleEditBuiltinSkill(pack)}>✏️</button>
                        <button className="btn-icon" title="导出" onClick={() => handleExportSkill(pack)}>📤</button>
                      </>
                    ) : (
                      <>
                        <button className="btn-icon" title="分享到系统" onClick={() => handlePublishSkill(pack)}>🌐</button>
                        <button className="btn-icon" title="导出" onClick={() => handleExportSkill(pack)}>📤</button>
                        <button className="btn-icon" title="编辑" onClick={() => openEditSkill(pack)}>✏️</button>
                        <button className="btn-icon" title="删除" onClick={() => handleDeleteSkill(pack)}>🗑️</button>
                      </>
                    )}
                  </div>
                </div>
                <div className="skill-card-desc">{pack.description}</div>
                {pack.github_source && (
                  <div style={{ marginTop: 6, padding: '6px 8px', background: 'var(--bg-tertiary)', borderRadius: 6, fontSize: 11, color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      🔗 GitHub: {pack.github_synced_at ? `已同步 ${new Date(pack.github_synced_at).toLocaleDateString()}` : '未同步'}
                    </span>
                    <button
                      className="btn-secondary-sm"
                      style={{ flexShrink: 0 }}
                      disabled={syncingPackId === pack.id}
                      onClick={(e) => { e.stopPropagation(); handleSyncFromGitHub(pack); }}
                      title={`从 ${pack.github_source} 拉取最新版本`}
                    >
                      {syncingPackId === pack.id ? '⏳ 同步中' : '🔄 同步GitHub'}
                    </button>
                  </div>
                )}
                <div className="skill-card-workflow">
                  {pack.workflow?.map((step, i) => (
                    <div key={i} className="workflow-step"><span className="workflow-step-num">{step.step}</span><span>{step.name}</span></div>
                  ))}
                </div>
              </div>
            );
            const masterPacks = skillPacks.filter(p => (p.category || 'master') === 'master');
            const stylePacks = skillPacks.filter(p => p.category === 'style');
            const reviewPacks = skillPacks.filter(p => p.category === 'review');
            const renderGroup = (title: string, packs: SkillPack[], hint: string, color: string) => packs.length === 0 ? null : (
              <div key={title} className="skill-category-group" style={{ borderLeft: `3px solid ${color}`, paddingLeft: 12, marginBottom: 16 }}>
                <h4 style={{ margin: '0 0 8px 0', fontSize: 14, color: 'var(--text-primary)' }}>
                  {title} <span style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 400 }}>({packs.length})</span>
                </h4>
                <p style={{ fontSize: 11, color: 'var(--text-muted)', margin: '0 0 8px 0' }}>{hint}</p>
                <div className="skill-grid">{packs.map(renderSkillCard)}</div>
              </div>
            );
            return (
              <>
                {renderGroup('构思类', masterPacks, '大纲/规划/设定/总创作阶段注入，提供创作方法论', '#4a90d9')}
                {renderGroup('文风类', stylePacks, '正文生成阶段注入，按题材锚定句式/节奏/用词风格', '#d97706')}
                {renderGroup('审查类', reviewPacks, '去AI味/一致性检查阶段注入，规范审校规则', '#059669')}
              </>
            );
          })()}
          {selectedPack && (
            <div className="skill-apply-bar">
              <span>将 "{selectedPack.name}" 应用到当前作品</span>
              <button className="btn-primary" onClick={handleApplySkillPack} disabled={!selectedBookId || loading}>
                {loading ? '应用中...' : '应用技能包'}
              </button>
              <button className="btn-ghost" onClick={() => setSelectedPack(null)}>取消</button>
            </div>
          )}

          {/* 自定义技能编辑器 */}
          {showSkillEditor && (
            <div className="skill-editor-overlay" onClick={() => setShowSkillEditor(false)}>
              <div className="skill-editor-modal" onClick={e => e.stopPropagation()}>
                <div className="skill-editor-header">
                  <h3>{skillEditor.id ? '编辑技能包' : '创建自定义技能包'}</h3>
                  <button className="btn-icon" onClick={() => setShowSkillEditor(false)}>✕</button>
                </div>

                {skillError && <div className="error-msg">{skillError}</div>}

                <div className="form-field">
                  <label>图标</label>
                  <div className="emoji-picker">
                    {EMOJI_CHOICES.map(em => (
                      <button key={em} className={`emoji-chip ${skillEditor.icon === em ? 'active' : ''}`} onClick={() => setSkillEditor(prev => ({ ...prev, icon: em }))}>{em}</button>
                    ))}
                  </div>
                </div>

                <div className="form-field">
                  <label>名称 *</label>
                  <input className="input" value={skillEditor.name} onChange={e => setSkillEditor(prev => ({ ...prev, name: e.target.value }))} placeholder="如：悬疑推理创作流" />
                </div>

                <div className="form-row">
                  <div className="form-field">
                    <label>题材</label>
                    <select className="input" value={skillEditor.genre} onChange={e => setSkillEditor(prev => ({ ...prev, genre: e.target.value }))}>
                      {Object.entries(GENRES).filter(([k]) => k !== 'all').map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                    </select>
                  </div>
                  <div className="form-field">
                    <label>类型</label>
                    <select className="input" value={skillEditor.book_type} onChange={e => setSkillEditor(prev => ({ ...prev, book_type: e.target.value }))}>
                      <option value="novel">长篇</option>
                      <option value="short_story">短篇</option>
                    </select>
                  </div>
                </div>

                {/* 【三类无污染】技能包分类选择：决定该包在哪个创作阶段注入 */}
                <div className="form-field">
                  <label>技能包分类 *</label>
                  <select className="input" value={skillEditor.category} onChange={e => setSkillEditor(prev => ({ ...prev, category: e.target.value as 'master' | 'style' | 'review' }))}>
                    <option value="master">构思类（大纲/规划/设定阶段注入）</option>
                    <option value="style">文风类（正文生成阶段注入，按题材锚定文风）</option>
                    <option value="review">审查类（去AI味/一致性检查阶段注入）</option>
                  </select>
                  <p className="text-muted" style={{fontSize:11, marginTop:4}}>
                    {skillEditor.category === 'master' && '构思类：在大纲/规划/设定/总创作时注入，提供创作方法论'}
                    {skillEditor.category === 'style' && '文风类：仅在正文生成时注入，锚定特定题材的句式/节奏/用词风格'}
                    {skillEditor.category === 'review' && '审查类：在去AI味/一致性检查时注入，规范审校规则'}
                  </p>
                </div>

                <div className="form-field">
                  <label>描述</label>
                  <textarea className="input" rows={2} value={skillEditor.description} onChange={e => setSkillEditor(prev => ({ ...prev, description: e.target.value }))} placeholder="简要描述这个技能包的用途和适用场景" />
                </div>

                <div className="form-field">
                  <label>创作工作流步骤</label>
                  <p className="text-muted" style={{fontSize:11}}>每个步骤对应创作中的一个环节，prompt_key 与下方提示词的键名对应</p>
                  {skillEditor.workflow.map((step, idx) => (
                    <div key={idx} className="workflow-editor-step">
                      <span className="workflow-step-num">{idx + 1}</span>
                      <input className="input" value={step.name} onChange={e => updateWorkflowStep(idx, 'name', e.target.value)} placeholder="步骤名称（如：设定构建）" />
                      <input className="input" value={step.desc} onChange={e => updateWorkflowStep(idx, 'desc', e.target.value)} placeholder="步骤说明" />
                      <input className="input" value={step.prompt_key} onChange={e => updateWorkflowStep(idx, 'prompt_key', e.target.value)} placeholder="prompt键名" />
                      <button className="btn-icon" onClick={() => removeWorkflowStep(idx)}>✕</button>
                    </div>
                  ))}
                  <button className="btn-secondary" onClick={addWorkflowStep}>+ 添加步骤</button>
                </div>

                <div className="form-field">
                  <label>提示词模板</label>
                  <p className="text-muted" style={{fontSize:11}}>键名对应工作流步骤的 prompt_key，值是发给AI的提示词模板</p>
                  {Object.entries(skillEditor.prompts).map(([key, val]) => (
                    <div key={key} className="prompt-editor-item">
                      <div className="prompt-editor-header">
                        <input className="input prompt-key-input" value={key} onChange={e => updatePromptKey(key, e.target.value)} placeholder="键名" />
                        <button className="btn-icon" onClick={() => removePrompt(key)}>✕</button>
                      </div>
                      <textarea className="input" rows={3} value={val} onChange={e => updatePromptValue(key, e.target.value)} placeholder="提示词内容，可用 {{变量}} 插入上下文..." />
                    </div>
                  ))}
                  <button className="btn-secondary" onClick={addPrompt}>+ 添加提示词</button>
                </div>

                <div className="form-row" style={{justifyContent:'flex-end',marginTop:16}}>
                  <button className="btn-ghost" onClick={() => setShowSkillEditor(false)}>取消</button>
                  <button className="btn-primary" onClick={handleSaveSkill} disabled={skillSaving}>
                    {skillSaving ? '保存中...' : '保存技能包'}
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {activeTab === 'analyze' && (
        <div className="tool-panel">
          <h3>📊 AI 拆书分析</h3>
          <p className="text-muted">导入作品文件或粘贴文本，分析文风/结构/节奏/人设，提炼可学习的创作方法论</p>
          <div className="form-row" style={{marginBottom:10}}>
            <input ref={fileInputRef} type="file" accept=".txt,.md,.docx,.zip,.json" onChange={handleUploadFile} style={{display:'none'}} id="analyze-file-input" />
            <label htmlFor="analyze-file-input" className="btn-secondary" style={{cursor:'pointer',padding:'8px 16px',borderRadius:'var(--radius-sm)',display:'inline-block'}}>
              📁 导入文件
            </label>
            {uploadFilename && <span className="text-muted" style={{alignSelf:'center'}}>已导入: {uploadFilename}</span>}
            <span className="text-muted" style={{alignSelf:'center',fontSize:11}}>支持 txt/md/docx/zip</span>
          </div>
          <textarea className="input" rows={8} value={analyzeInput} onChange={e => setAnalyzeInput(e.target.value)}
            placeholder="粘贴要分析的作品片段（建议2000字以上），或导入文件自动填充..." />
          <div className="form-row">
            <button className="btn-primary" onClick={handleAnalyze} disabled={!analyzeInput.trim() || analyzeLoading}>
              {analyzeLoading ? '分析中...' : '开始分析'}
            </button>
            {analyzeResult && (
              <>
                <button className="btn-secondary" onClick={handleExportAnalysis}>📥 导出结果</button>
                <button className="btn-secondary" onClick={() => setShowSyncModal(true)} style={{borderColor:'#6c5ce7',color:'#6c5ce7'}}>
                  📋 同步到作品
                </button>
              </>
            )}
          </div>
          {analyzeResult && (
            <div className="analyze-result">
              <div className="analyze-tags">
                {analyzeResult.genre_tags?.map((t, i) => <span key={i} className="tag">{t}</span>)}
                <span className="tag platform">{analyzeResult.target_platform}</span>
              </div>
              <div className="analyze-grid">
                <div className="analyze-item"><h4>文风特点</h4><p>{analyzeResult.style_analysis}</p></div>
                <div className="analyze-item"><h4>结构特点</h4><p>{analyzeResult.structure_analysis}</p></div>
                <div className="analyze-item"><h4>节奏特点</h4><p>{analyzeResult.rhythm_analysis}</p></div>
                <div className="analyze-item"><h4>人设特点</h4><p>{analyzeResult.character_design_analysis}</p></div>
              </div>
              <div className="review-section"><h4>钩子技巧</h4><ul>{analyzeResult.hook_techniques?.map((h, i) => <li key={i}>{h}</li>)}</ul></div>
              <div className="review-section"><h4>可学习的方法</h4><ul>{analyzeResult.learnable_points?.map((p, i) => <li key={i}>{p}</li>)}</ul></div>
              {analyzeResult.golden_lines?.length > 0 && (
                <div className="review-section"><h4>金句摘录</h4><ul>{analyzeResult.golden_lines.map((l, i) => <li key={i} className="golden-line">{l}</li>)}</ul></div>
              )}
            </div>
          )}
        </div>
      )}

      {activeTab === 'export' && (
        <div className="tool-panel">
          <h3>📤 导出作品</h3>
          <p className="text-muted">导出为不同格式或备份整个项目</p>

          {/* 全量导出：维度+章节 */}
          <div className="export-section" style={{background:'var(--bg-tertiary)',borderRadius:'var(--radius-sm)',padding:16,marginBottom:16}}>
            <h4 style={{fontSize:14,marginBottom:6}}>📦 全量导出（推荐）</h4>
            <p className="text-muted" style={{fontSize:12,marginBottom:10}}>将所有维度设定（构思/设定/大纲/世界观/人物/剧情/伏笔/地点/风格）和全部章节，各自导出为独立txt/md文件，打包到以小说名命名的文件夹中。</p>
            <button className={`btn-primary ${!selectedBookId || downloading ? 'disabled' : ''}`}
              disabled={!selectedBookId || downloading}
              onClick={() => { if (!selectedBookId) { alert('请先选择作品'); return; } handleAuthDownload(api.getExportFullUrl(selectedBookId), 'export.zip'); }}>
              {downloading ? '⏳ 导出中...' : '📦 全量导出到文件夹'}
            </button>
          </div>

          {/* 单文件导出 */}
          <div className="export-section">
            <h4 style={{fontSize:14,marginBottom:6}}>📄 单文件导出</h4>
            <div className="form-row">
              <label className="input-label">导出格式</label>
              <select className="input" value={exportFormat} onChange={e => setExportFormat(e.target.value)}>
                <option value="txt">纯文本 (.txt)</option>
                <option value="html">网页 (.html)</option>
                <option value="json">JSON数据 (.json)</option>
                <option value="zip">完整备份 (.zip)</option>
              </select>
            </div>
            <button className={`btn-secondary ${!selectedBookId || downloading ? 'disabled' : ''}`}
              disabled={!selectedBookId || downloading}
              onClick={() => { if (!selectedBookId) return; handleAuthDownload(getExportUrl(), `export.${exportFormat}`); }}>
              {downloading ? '⏳ 导出中...' : '下载单文件'}
            </button>
          </div>
        </div>
      )}

      {/* 拆书分析同步到作品弹窗 */}
      {showSyncModal && (
        <div className="modal-overlay" onClick={() => setShowSyncModal(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h2>📋 同步分析结果到作品</h2>
            <p className="text-muted" style={{marginBottom:12}}>将拆书分析的文风、结构、人设等方法论同步到作品资料，用于仿写或同人文创作。</p>

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
                    <div className="sync-mode-name">✍️ 仿写模式</div>
                    <div className="sync-mode-desc">提取原文风格、结构、节奏，作为创作参考填充到作品设定中</div>
                  </div>
                </label>
                <label className={`sync-mode-card ${syncMode === 'fanfic' ? 'active' : ''}`}>
                  <input type="radio" name="syncMode" value="fanfic" checked={syncMode === 'fanfic'} onChange={e => setSyncMode(e.target.value)} />
                  <div className="sync-mode-info">
                    <div className="sync-mode-name">📚 同人文模式</div>
                    <div className="sync-mode-desc">提取原文世界观、人物设定，作为同人文创作的基础资料</div>
                  </div>
                </label>
                <label className={`sync-mode-card ${syncMode === 'reference' ? 'active' : ''}`}>
                  <input type="radio" name="syncMode" value="reference" checked={syncMode === 'reference'} onChange={e => setSyncMode(e.target.value)} />
                  <div className="sync-mode-info">
                    <div className="sync-mode-name">💡 参考模式</div>
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
    </div>
  );
}

const REVIEW_LABELS: Record<string, string> = {
  opening_hook: '开篇钩子', character_motivation: '人物动机', pacing_rhythm: '节奏控制',
  chapter_ending: '章尾钩子', dialogue_quality: '对白质量',
  world_consistency: '设定一致性', commercial_potential: '商业潜力',
};

const SYNC_FIELD_LABELS: Record<string, string> = {
  style_guide: '风格指南', plot_design: '大纲设计', character_profiles: '人物档案',
  foreshadowing: '伏笔', worldbuilding: '世界观', key_rules: '设定', timeline: '剧情',
};

function emptySkillEditor(): SkillEditorState {
  return {
    id: null,
    name: '',
    icon: '📦',
    genre: 'other',
    book_type: 'novel',
    description: '',
    workflow: [{ step: 1, name: '', desc: '', prompt_key: '' }],
    prompts: {},
    category: 'master',  // 默认构思类
    genre_target: '',
  };
}

const EMOJI_CHOICES = ['📦', '✍️', '🎯', '🚀', '💡', '🔮', '⚔️', '🏰', '❤️', '🔍', '📊', '🎭', '📚', '🎨', '⚡'];
