import { useState, useEffect, useContext, useRef } from 'react';
import * as yaml from 'js-yaml';
import { api } from '../api';
import { AuthContext } from '../App';
import type { Book, SkillPack, ReviewResult, AnalysisResult, WorkflowStep, RankingData, AIUsageStats, AIUsageLogItem } from '../types';
import { GENRES, GENRE_GROUPS, normalizeGenreKey } from '../constants';

type ToolTab = 'review' | 'skills' | 'analyze' | 'rankings' | 'ledger';

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
  // 【需求1-1：双击预览】技能包预览Modal状态
  const [previewPack, setPreviewPack] = useState<SkillPack | null>(null);
  // 【三类无污染】技能包市场分组折叠状态（默认全展开）
  const [skillGroupCollapsed, setSkillGroupCollapsed] = useState<Record<string, boolean>>({});

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

  const skillImportRef = useRef<HTMLInputElement>(null);

  // 【榜单风向】各平台排行榜趋势
  const [rankPlatform, setRankPlatform] = useState('fanqie');
  const [rankData, setRankData] = useState<RankingData | null>(null);
  const [rankLoading, setRankLoading] = useState(false);

  // 【AI 调用账本】
  const [usageStats, setUsageStats] = useState<AIUsageStats | null>(null);
  const [usageLogs, setUsageLogs] = useState<AIUsageLogItem[]>([]);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageDays, setUsageDays] = useState(7);
  const [usageOnlyFail, setUsageOnlyFail] = useState(false);

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

  // 加载榜单风向（默认平台）
  useEffect(() => { loadRankings('fanqie'); }, []);

  // 加载 AI 调用账本
  useEffect(() => { loadUsage(); }, []);

  async function loadRankings(platform: string) {
    setRankLoading(true);
    try { const r = await api.getRankings(platform); setRankData(r); }
    catch (e: any) { setRankData(null); }
    setRankLoading(false);
  }

  async function loadUsage(days?: number, onlyFail?: boolean) {
    const d = days ?? usageDays;
    const of = onlyFail ?? usageOnlyFail;
    setUsageLoading(true);
    try {
      const [stats, logs] = await Promise.all([
        api.getAiUsageStats(d),
        api.getAiUsage({ limit: 50, onlyFail: of }),
      ]);
      setUsageStats(stats);
      setUsageLogs(logs.items);
    } catch (e: any) { /* 后台未配置时静默 */ }
    setUsageLoading(false);
  }

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
    // 【需求2：编辑副本=保存才创建副本】
    // 不立即克隆到数据库，直接打开编辑器编辑副本内容（id=null表示新建）
    // 只有用户点击"保存"时，handleSaveSkill才会通过createSkillPack创建用户副本
    // 这样如果用户打开后放弃编辑（关闭不保存），不会产生多余的空副本
    setSkillEditor({
      id: null,
      name: `${pack.name}（我的副本）`,
      icon: pack.icon || '📦',
      genre: pack.genre || 'other',
      book_type: pack.book_type || 'novel',
      description: pack.description || '',
      workflow: pack.workflow?.length ? JSON.parse(JSON.stringify(pack.workflow)) : [{ step: 1, name: '', desc: '', prompt_key: '' }],
      prompts: pack.prompts ? JSON.parse(JSON.stringify(pack.prompts)) : {},
      category: (pack.category || 'master') as 'master' | 'style' | 'review',
      genre_target: pack.genre_target || '',
    });
    setSkillError('');
    setShowSkillEditor(true);
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
      const rawText = await file.text();
      const data = smartParseSkillPack(rawText, file.name);
      if (!data) {
        alert('无法解析文件内容：请粘贴或导入 ① skill.md(带---分隔的front-matter) ② JSON ③ YAML ④ 任意纯文本（会作为提示词自动转技能包）');
        return;
      }

      const ok = await requireAuth();
      if (!ok) return;
      // 【题材对齐】导入时一定按GENRES真相源归一化genre/genre_target，避免"穿越/修真/甜宠"等别名落库成unknown key
      const genre = normalizeGenreKey(data.genre);
      const genre_target = normalizeGenreKey(data.genre_target);
      const payload = {
        name: data.name,
        icon: data.icon || '📦',
        genre,
        genre_target: genre_target || undefined,
        book_type: data.book_type || 'novel',
        description: data.description || '',
        category: data.category || 'master',
        workflow: Array.isArray(data.workflow) ? data.workflow : [],
        prompts: data.prompts || {},
      };
      await api.createSkillPack(payload);
      alert(`技能包 "${payload.name}" 导入成功`);
      reloadSkillPacks();
    } catch (err: any) {
      alert('导入失败: ' + (err.message || '请检查文件格式'));
    }
    if (skillImportRef.current) skillImportRef.current.value = '';
  }

  /**
   * 多格式智能解析器：
   *  优先级 1) 代码块内的 JSON/YAML（```json / ```yaml）
   *  优先级 2) Markdown front-matter（---分隔块，兼容中文冒号"："）
   *  优先级 3) 整段 JSON
   *  优先级 4) 整段 YAML
   *  优先级 5) 纯文本兜底：自动转 {name, description, category, prompts:{default}}
   */
  function smartParseSkillPack(rawText: string, fileName: string = ''): any {
    if (!rawText || !rawText.trim()) return null;
    const trimmed = rawText.trim();
    let data: any = null;

    // ① md代码块先扫
    const yamlBlockMatch = trimmed.match(/```(?:yaml|yml)\s*\r?\n([\s\S]*?)\r?\n?```/i);
    if (yamlBlockMatch) {
      try { data = yaml.load(yamlBlockMatch[1].trim()) as any; } catch { /* ignore */ }
    }
    const jsonBlockMatch = !data ? trimmed.match(/```(?:json)?\s*\r?\n([\s\S]*?)\r?\n?```/i) : null;
    if (!data && jsonBlockMatch) {
      try { data = JSON.parse(jsonBlockMatch[1].trim()); } catch {
        try { data = yaml.load(jsonBlockMatch[1].trim()) as any; } catch { /* ignore */ }
      }
    }

    // ② front-matter（兼容用户的中文冒号 "name：xxx" 全角写法，必须在文本最开头）
    if (!data) {
      const fmMatch = trimmed.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n([\s\S]*))?$/);
      if (fmMatch) {
        const header = fmMatch[1];
        const body = fmMatch[2] || '';
        const normalized = header
          // 兼容：key：value (中文全角冒号/两边空格) 统一为 key: value
          .replace(/^([^\s：:][^：:\n]*?)[ \t]*[：:][ \t]*/gm, (_, k) => `${k}: `);
        try {
          data = yaml.load(normalized) as any;
          if (data && typeof data === 'object' && body && body.trim()) {
            // 把front-matter之后的markdown正文作为默认提示词注入
            if (!data.prompts || typeof data.prompts !== 'object') data.prompts = {};
            const defaultKey = data.category === 'style' ? 'style_default'
              : data.category === 'review' ? 'review_default' : 'master_default';
            if (!data.prompts[defaultKey]) data.prompts[defaultKey] = body.trim();
            if (!data.description) {
              // 正文前120字提炼描述
              data.description = body.trim().slice(0, 120).replace(/\s+/g, ' ');
            }
          }
        } catch { /* ignore */ }
      }
    }

    // ③ 整段JSON
    if (!data && (trimmed.startsWith('{') || trimmed.startsWith('['))) {
      try { data = JSON.parse(trimmed); } catch { /* ignore */ }
    }
    // ④ 整段YAML
    if (!data) {
      try { data = yaml.load(trimmed) as any; } catch { /* ignore */ }
    }

    // ⑤ 纯文本兜底：自动推断 name/description/prompts
    if (!data || typeof data !== 'object') {
      const body = (data && typeof data === 'string') ? data : rawText;
      if (!body || !String(body).trim()) return null;
      const firstLine = String(body).split(/\r?\n/).map(l => l.trim()).find(l => l.length) || '我的技能';
      const cleanName = firstLine.replace(/^[#\s\-*>`"']+|[#\s\-*>`"']+$/g, '').slice(0, 40) || '自定义技能包';
      const plainDesc = String(body).trim().replace(/\s+/g, ' ').slice(0, 160);
      data = {
        name: cleanName,
        description: plainDesc,
        category: inferCategoryFromText(body),
        prompts: {
          master_default: String(body).trim(),
        },
      };
    }

    // 格式规范：无name → 用fileName首行兜底
    if (!data.name) {
      data.name = fileName ? fileName.replace(/\.[^.]+$/, '') : '自定义技能包';
    }
    // 【题材对齐】最终把genre/genre_target归一化成GENRES真相源的合法key（兜底other），
    // 同时支持用户写中文标签/别名（如"修真"/"甜宠"/"末世"/"规则怪谈"）自动映射
    data.genre = normalizeGenreKey(data.genre);
    if (data.genre_target !== undefined && data.genre_target !== null) {
      data.genre_target = normalizeGenreKey(data.genre_target);
    }
    // 规范化：如果只有纯prompts没有workflow，就不强制创建workflow（空数组也可）
    if (!Array.isArray(data.workflow)) data.workflow = [];
    if (!data.prompts || typeof data.prompts !== 'object') {
      // 如果用户给的是string文本但走到这里，兜底塞进 prompts.default
      const fallback = typeof data.content === 'string' ? data.content
        : typeof data.prompt === 'string' ? data.prompt
        : typeof data.body === 'string' ? data.body : '';
      data.prompts = { default: fallback || String(rawText).trim().slice(0, 4000) };
    }
    return data;
  }

  /** 关键词轻量推断：正文/文风/审查 */
  function inferCategoryFromText(text: string): 'master' | 'style' | 'review' {
    const t = String(text);
    const styleHits = (t.match(/句式|文风|用词|口语|节奏|排比|短句|长句|方言|对白|叙述密度|标点|修辞|比喻|形容词|动词/g) || []).length;
    const reviewHits = (t.match(/一致性|去AI|审核|AI味|校验|冲突|矛盾|伏笔|设定检查|设定一致|错别字|病句|逻辑/g) || []).length;
    if (reviewHits >= styleHits && reviewHits >= 2) return 'review';
    if (styleHits >= 2) return 'style';
    return 'master';
  }

  /** 创建/编辑表单里：把用户粘贴的文本识别成字段（保留用户已手动填好的name/category/description不覆盖） */
  function applySmartParseToEditor(rawText: string) {
    const parsed = smartParseSkillPack(rawText, skillEditor.name + '.txt');
    if (!parsed) return;
    setSkillEditor(prev => {
      const next: typeof prev = { ...prev };
      // 不覆盖用户已经手动填的字段（只有空值才从解析结果里补）
      if (!next.name && parsed.name) next.name = parsed.name;
      if (!next.description && parsed.description) next.description = parsed.description;
      if (parsed.category && !next.category) next.category = parsed.category;
      if (parsed.icon && !next.icon) next.icon = parsed.icon;
      if (parsed.genre && !next.genre) next.genre = parsed.genre;
      if (parsed.workflow?.length) {
        // 用户已有步骤为空时覆盖，否则保留
        const userStepsEmpty = !next.workflow?.length || next.workflow.every(w => !w.name && !w.prompt_key);
        if (userStepsEmpty) next.workflow = parsed.workflow;
      }
      if (parsed.prompts && typeof parsed.prompts === 'object' && Object.keys(parsed.prompts).length) {
        next.prompts = { ...(parsed.prompts as Record<string, string>), ...next.prompts };
      }
      return next;
    });
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

  // 删除原来硬编码的迷你 GENRES 字典：引用 ../constants 的真相源 GENRES / GENRE_GROUPS
  // 原有的 'all': '全部' 只在筛选下拉里用"所有题材"空值代替，不再污染题材真相源。

  const TOOL_TABS = [
    { key: 'review' as ToolTab, label: 'AI 责编', icon: '🔍', desc: 'AI平台视角审稿打分' },
    { key: 'skills' as ToolTab, label: '技能包', icon: '📦', desc: '15+题材工作流套件' },
    { key: 'analyze' as ToolTab, label: '拆书分析', icon: '📊', desc: '导入文件分析提炼方法论' },
    { key: 'rankings' as ToolTab, label: '榜单风向', icon: '📈', desc: '各平台排行榜趋势洞察' },
    { key: 'ledger' as ToolTab, label: 'AI调用账本', icon: '🧾', desc: '每次AI调用的成本与成败' },
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
              {GENRE_GROUPS.map(g => (
                <optgroup key={g.label} label={g.label}>
                  {g.keys.map(k => <option key={k} value={k}>{GENRES[k] || k}</option>)}
                </optgroup>
              ))}
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
              <div key={pack.id} className={`skill-card ${selectedPack?.id === pack.id ? 'selected' : ''}`} onClick={() => setSelectedPack(pack)} onDoubleClick={() => setPreviewPack(pack)} title="单击选中 · 双击预览">
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
                <button
                  type="button"
                  className="skill-category-toggle"
                  onClick={() => setSkillGroupCollapsed(prev => ({ ...prev, [title]: !prev[title] }))}
                  style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, display: 'flex', alignItems: 'center', gap: 6, width: '100%', textAlign: 'left' }}
                >
                  <span style={{ fontSize: 12, color: 'var(--text-muted)', transition: 'transform 0.2s', transform: skillGroupCollapsed[title] ? 'rotate(-90deg)' : 'rotate(0deg)' }}>▼</span>
                  <h4 style={{ margin: 0, fontSize: 14, color: 'var(--text-primary)', flex: 1 }}>
                    {title} <span style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 400 }}>({packs.length})</span>
                  </h4>
                </button>
                {!skillGroupCollapsed[title] && (
                  <>
                    <p style={{ fontSize: 11, color: 'var(--text-muted)', margin: '4px 0 8px 18px' }}>{hint}</p>
                    <div className="skill-grid" style={{ marginLeft: 18 }}>{packs.map(renderSkillCard)}</div>
                  </>
                )}
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
                      {GENRE_GROUPS.map(g => (
                        <optgroup key={g.label} label={g.label}>
                          {g.keys.map(k => <option key={k} value={k}>{GENRES[k] || k}</option>)}
                        </optgroup>
                      ))}
                    </select>
                  </div>
                  {/* 文风类：额外显示「适用题材 (genre_target)」下拉，和创建小说表单题材条目完全对齐 */}
                  {skillEditor.category === 'style' && (
                    <div className="form-field">
                      <label>文风适用题材（正文阶段按题材优先匹配）</label>
                      <select className="input"
                        value={skillEditor.genre_target || ''}
                        onChange={e => setSkillEditor(prev => ({ ...prev, genre_target: e.target.value }))}>
                        <option value="">不指定（任意题材生效）</option>
                        {GENRE_GROUPS.map(g => (
                          <optgroup key={g.label} label={g.label}>
                            {g.keys.map(k => <option key={k} value={k}>{GENRES[k] || k}</option>)}
                          </optgroup>
                        ))}
                      </select>
                      <p className="text-muted" style={{fontSize:11, marginTop:4}}>
                        如果指定题材，仅当当前创作小说的题材与该值一致时才会在正文阶段注入，避免文风跨题材污染。
                      </p>
                    </div>
                  )}
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

                {/* 【用户诉求-易用性】直接粘贴任意文本 / skill.md / JSON/YAML，自动识别成技能包 */}
                <div className="form-field">
                  <label>💡 粘贴提示词或 skill.md 自动生成 <span className="text-muted" style={{fontSize:11}}>（支持 front-matter / JSON / YAML / 普通纯文本，不会覆盖已填好的名称/分类）</span></label>
                  <textarea
                    className="input"
                    rows={5}
                    placeholder={`示例1（skill.md front-matter）：
---
name：我的悬疑构思法
description：适合悬疑推理题材的大纲创作指南
category：master
---
1. 开篇三秒内抛出一个不可能的谜题...

示例2（纯文本）：
  短句占比 ≥60%，冲突场景每段≤15字。对白提示语放句中/句后比例 ≥70%。比喻拟人每千字≤3处。`}
                    onPasteCapture={(e) => {
                      // 粘贴时做一次识别填充（非破坏性：已填的 name/category/description 保留）
                      const text = e.clipboardData?.getData('text');
                      if (!text) return;
                      setTimeout(() => applySmartParseToEditor(text), 0);
                    }}
                    onBlur={(e) => {
                      // 失焦也做一次识别，覆盖粘贴时漏掉的
                      if (e.target.value && e.target.value.trim().length > 20) {
                        applySmartParseToEditor(e.target.value);
                        e.target.value = '';  // 识别清空，避免下次再跑
                      }
                    }}
                  />
                  <p className="text-muted" style={{fontSize:11, marginTop:4}}>
                    小技巧：格式越规范识别越准。普通纯文本也会自动转为默认提示词，分类按关键词自动猜测。
                  </p>
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

          {/* 【需求1-1：双击预览】技能包预览 Modal */}
          {previewPack && (
            <div className="skill-editor-overlay" onClick={() => setPreviewPack(null)}>
              <div className="skill-editor-modal" onClick={e => e.stopPropagation()} style={{ maxWidth: 760 }}>
                <div className="skill-editor-header">
                  <h3 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: 22 }}>{previewPack.icon}</span>
                    <span>{previewPack.name}</span>
                    {previewPack.is_builtin ? <span className="builtin-badge">系统</span> : <span className="custom-badge">自定义</span>}
                    <span style={{
                      fontSize: 11, fontWeight: 500, padding: '2px 8px', borderRadius: 999,
                      background: (previewPack.category || 'master') === 'master' ? 'rgba(74,144,217,0.12)'
                        : previewPack.category === 'style' ? 'rgba(217,119,6,0.12)' : 'rgba(5,150,105,0.12)',
                      color: (previewPack.category || 'master') === 'master' ? '#4a90d9'
                        : previewPack.category === 'style' ? '#d97706' : '#059669'
                    }}>
                      {(previewPack.category || 'master') === 'master' ? '构思类' : previewPack.category === 'style' ? '文风类' : '审查类'}
                    </span>
                  </h3>
                  <button className="btn-icon" onClick={() => setPreviewPack(null)}>✕</button>
                </div>

                <div style={{ padding: '0 4px 12px', fontSize: 12, color: 'var(--text-secondary)' }}>
                  {GENRES[previewPack.genre] || previewPack.genre} · {previewPack.book_type === 'novel' ? '长篇' : '短篇'}
                  {previewPack.github_source && <> · 🔗 <a href={previewPack.github_source} target="_blank" rel="noreferrer" style={{ color: 'var(--link)' }}>GitHub 源</a></>}
                </div>

                <div style={{ background: 'var(--bg-tertiary)', padding: 12, borderRadius: 8, marginBottom: 16 }}>
                  <div style={{ fontSize: 13, lineHeight: 1.7, color: 'var(--text-primary)' }}>{previewPack.description || '暂无描述'}</div>
                </div>

                <div className="form-field">
                  <label>工作流步骤（{previewPack.workflow?.length || 0}步）</label>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    {!previewPack.workflow?.length && <div className="text-muted" style={{fontSize:12}}>暂无工作流步骤</div>}
                    {previewPack.workflow?.map((step, i) => (
                      <div key={i} style={{
                        display: 'flex', alignItems: 'flex-start', gap: 10,
                        padding: '8px 10px', background: 'var(--bg-tertiary)', borderRadius: 6
                      }}>
                        <span style={{
                          flexShrink:0, width: 22, height:22, borderRadius:'50%',
                          background:'var(--accent)', color:'#fff', fontSize:11,
                          display:'flex',alignItems:'center',justifyContent:'center', fontWeight:600
                        }}>{step.step || i+1}</span>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>{step.name || '未命名'}</div>
                          {step.desc && <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 2 }}>{step.desc}</div>}
                          {step.prompt_key && (
                            <div style={{
                              fontSize: 11, color: 'var(--accent)', marginTop: 4,
                              fontFamily: 'var(--mono)', background: 'var(--bg-secondary)',
                              padding: '2px 6px', borderRadius: 4, display: 'inline-block'
                            }}>
                              🔑 {step.prompt_key}
                            </div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="form-field">
                  <label>提示词模板（{Object.keys(previewPack.prompts || {}).length}个）</label>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxHeight: 260, overflow: 'auto' }}>
                    {!Object.keys(previewPack.prompts || {}).length && <div className="text-muted" style={{fontSize:12}}>暂无提示词模板</div>}
                    {Object.entries(previewPack.prompts || {}).map(([key, val]) => (
                      <div key={key} style={{
                        background: 'var(--bg-tertiary)', borderRadius: 6, overflow: 'hidden',
                        border: '1px solid var(--border)'
                      }}>
                        <div style={{
                          padding: '6px 10px', background: 'var(--bg-secondary)',
                          fontSize: 12, fontWeight: 600, fontFamily: 'var(--mono)',
                          borderBottom: '1px solid var(--border)', color: 'var(--accent)'
                        }}>
                          📝 {key}
                        </div>
                        <pre style={{
                          margin: 0, padding: 10, fontSize: 12, lineHeight: 1.7,
                          color: 'var(--text-primary)', whiteSpace: 'pre-wrap',
                          wordBreak: 'break-word', fontFamily: 'var(--mono)',
                          maxHeight: 160, overflow: 'auto'
                        }}>{val || '（空）'}</pre>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="form-row" style={{ justifyContent: 'flex-end', marginTop: 8, gap: 8 }}>
                  {previewPack.is_builtin ? (
                    <button className="btn-secondary" onClick={() => { setPreviewPack(null); handleEditBuiltinSkill(previewPack); }}>✏️ 编辑副本</button>
                  ) : (
                    <button className="btn-secondary" onClick={() => { setPreviewPack(null); openEditSkill(previewPack); }}>✏️ 编辑</button>
                  )}
                  <button className="btn-primary" onClick={() => setPreviewPack(null)}>关闭</button>
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

      {activeTab === 'rankings' && (
        <div className="tool-panel">
          <h3>📈 榜单风向</h3>
          <p className="text-muted">抓取各平台排行榜数据，分析趋势、热门标签与上升关键词，帮你踩准市场节奏</p>

          <div className="form-row" style={{alignItems:'flex-end',gap:8}}>
            <div style={{flex:1}}>
              <label className="input-label">选择平台</label>
              <select className="input" value={rankPlatform} onChange={e => { setRankPlatform(e.target.value); loadRankings(e.target.value); }}>
                <option value="fanqie">番茄小说</option>
                <option value="qidian">起点中文网</option>
                <option value="qimao">七猫中文网</option>
              </select>
            </div>
            <button className="btn-primary" onClick={() => loadRankings(rankPlatform)} disabled={rankLoading}>
              {rankLoading ? '获取中...' : '获取榜单'}
            </button>
          </div>

          {rankData && (
            <div className="rank-result" style={{marginTop:16}}>
              <div className="rank-platform-banner" style={{
                display:'flex',alignItems:'center',gap:12,padding:14,background:'linear-gradient(135deg,var(--accent),var(--accent-hover))',
                color:'#fff',borderRadius:'var(--radius-sm)',marginBottom:12,
              }}>
                <span style={{fontSize:26}}>{rankData.icon}</span>
                <div>
                  <div style={{fontSize:16,fontWeight:700}}>{rankData.platform} · {rankData.note}</div>
                  <div style={{fontSize:12,opacity:.9}}>📌 风向 {rankData.trend_marker.label} · {rankData.trend_marker.tone}</div>
                </div>
              </div>

              <div className="rank-block">
                <h4 style={{fontSize:13,marginBottom:6}}>🔥 热门标签</h4>
                <div style={{display:'flex',flexWrap:'wrap',gap:6}}>
                  {rankData.hot_tags.map((t, i) => (
                    <span key={i} style={{padding:'4px 10px',borderRadius:14,background:'var(--bg-tertiary)',border:'1px solid var(--border-color)',fontSize:12}}>{t}</span>
                  ))}
                </div>
              </div>

              <div className="rank-block" style={{marginTop:12}}>
                <h4 style={{fontSize:13,marginBottom:6}}>🚀 上升关键词</h4>
                <div style={{display:'flex',flexWrap:'wrap',gap:6}}>
                  {rankData.rising_keywords.map((k, i) => (
                    <span key={i} style={{padding:'4px 10px',borderRadius:14,background:'color-mix(in srgb, var(--accent) 12%, transparent)',border:'1px solid color-mix(in srgb, var(--accent) 40%, transparent)',fontSize:12,color:'var(--accent)'}}>{k}</span>
                  ))}
                </div>
              </div>

              <div className="rank-block" style={{marginTop:12}}>
                <h4 style={{fontSize:13,marginBottom:6}}>📚 参考作品</h4>
                <div style={{display:'flex',flexDirection:'column',gap:8}}>
                  {rankData.examples.map((ex, i) => (
                    <div key={i} style={{padding:'10px 12px',background:'var(--bg-secondary)',border:'1px solid var(--border-color)',borderRadius:8}}>
                      <div style={{fontWeight:600,fontSize:13}}>{ex.title}</div>
                      <div style={{fontSize:11,color:'var(--text-muted)',marginTop:2}}>{ex.tag} · {ex.point}</div>
                    </div>
                  ))}
                </div>
              </div>

              <div className="rank-advice" style={{marginTop:12,padding:12,background:'color-mix(in srgb, var(--accent) 8%, transparent)',borderRadius:'var(--radius-sm)',fontSize:13,lineHeight:1.7}}>
                💡 <strong>创作建议：</strong>{rankData.advice}
              </div>
            </div>
          )}
        </div>
      )}

      {activeTab === 'ledger' && (
        <div className="tool-panel">
          <h3>🧾 AI 调用账本</h3>
          <p className="text-muted">记录每一次AI调用的场景、模型、字数、耗时与成败，便于审计和成本掌控</p>

          <div className="form-row" style={{alignItems:'flex-end',gap:8}}>
            <div style={{flex:1}}>
              <label className="input-label">统计周期</label>
              <select className="input" value={usageDays} onChange={e => { const d = Number(e.target.value); setUsageDays(d); loadUsage(d, usageOnlyFail); }}>
                <option value={7}>近 7 天</option>
                <option value={30}>近 30 天</option>
                <option value={90}>近 90 天</option>
              </select>
            </div>
            <label style={{display:'flex',alignItems:'center',gap:6,fontSize:13}}>
              <input type="checkbox" checked={usageOnlyFail} onChange={e => { const of = e.target.checked; setUsageOnlyFail(of); loadUsage(usageDays, of); }} /> 仅看失败
            </label>
            <button className="btn-primary" onClick={() => loadUsage()} disabled={usageLoading}>{usageLoading ? '加载中...' : '刷新'}</button>
          </div>

          {usageStats && (
            <>
              <div className="ledger-cards" style={{display:'grid',gridTemplateColumns:'repeat(4,1fr)',gap:10,marginTop:16}}>
                <div className="ledger-card" style={{padding:12,background:'var(--bg-secondary)',borderRadius:'var(--radius-sm)',border:'1px solid var(--border-color)'}}>
                  <div style={{fontSize:22,fontWeight:800,color:'var(--accent)'}}>{usageStats.total_calls}</div>
                  <div style={{fontSize:12,color:'var(--text-muted)'}}>总调用（{usageStats.days}天）</div>
                </div>
                <div className="ledger-card" style={{padding:12,background:'var(--bg-secondary)',borderRadius:'var(--radius-sm)',border:'1px solid var(--border-color)'}}>
                  <div style={{fontSize:22,fontWeight:800,color:'#27ae60'}}>{usageStats.success_rate}%</div>
                  <div style={{fontSize:12,color:'var(--text-muted)'}}>成功率（失败{usageStats.failed}）</div>
                </div>
                <div className="ledger-card" style={{padding:12,background:'var(--bg-secondary)',borderRadius:'var(--radius-sm)',border:'1px solid var(--border-color)'}}>
                  <div style={{fontSize:22,fontWeight:800}}>{(usageStats.total_output_chars / 10000).toFixed(2)}万字</div>
                  <div style={{fontSize:12,color:'var(--text-muted)'}}>累计输出字数</div>
                </div>
                <div className="ledger-card" style={{padding:12,background:'var(--bg-secondary)',borderRadius:'var(--radius-sm)',border:'1px solid var(--border-color)'}}>
                  <div style={{fontSize:22,fontWeight:800}}>{(usageStats.total_duration_ms / 60000 / 60).toFixed(1)}h</div>
                  <div style={{fontSize:12,color:'var(--text-muted)'}}>累计耗时时长</div>
                </div>
              </div>

              {usageStats.by_scene.length > 0 && (
                <div className="ledger-block" style={{marginTop:16}}>
                  <h4 style={{fontSize:14,marginBottom:8}}>📌 按场景分布</h4>
                  <div style={{display:'flex',flexDirection:'column',gap:6}}>
                    {usageStats.by_scene.map((s, i) => {
                      const max = Math.max(...usageStats.by_scene.map(x => x.count), 1);
                      return (
                        <div key={i} style={{display:'flex',alignItems:'center',gap:8,fontSize:13}}>
                          <span style={{width:140,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{s.scene}</span>
                          <div style={{flex:1,background:'var(--bg-tertiary)',borderRadius:4,height:14,overflow:'hidden'}}>
                            <div style={{width:`${(s.count / max) * 100}%`,height:'100%',background:'var(--accent)',borderRadius:4}} />
                          </div>
                          <span style={{minWidth:24,textAlign:'right',fontWeight:600}}>{s.count}</span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {usageLogs.length > 0 && (
                <div className="ledger-block" style={{marginTop:16}}>
                  <h4 style={{fontSize:14,marginBottom:8}}>🗒️ 最近调用明细</h4>
                  <div style={{maxHeight:300,overflowY:'auto',border:'1px solid var(--border-color)',borderRadius:8}}>
                    <table style={{width:'100%',fontSize:12,borderCollapse:'collapse'}}>
                      <thead>
                        <tr style={{background:'var(--bg-tertiary)',textAlign:'left'}}>
                          <th style={{padding:8}}>场景</th><th style={{padding:8}}>模型</th><th style={{padding:8}}>输出字</th>
                          <th style={{padding:8}}>耗时</th><th style={{padding:8}}>状态</th>
                        </tr>
                      </thead>
                      <tbody>
                        {usageLogs.map(log => (
                          <tr key={log.id} style={{borderTop:'1px solid var(--border-color)'}}>
                            <td style={{padding:'6px 8px',maxWidth:140,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}} title={log.scene}>{log.scene}</td>
                            <td style={{padding:'6px 8px',maxWidth:120,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}} title={log.model}>{log.model}</td>
                            <td style={{padding:'6px 8px'}}>{log.output_chars}</td>
                            <td style={{padding:'6px 8px'}}>{log.duration_ms}ms</td>
                            <td style={{padding:'6px 8px'}}>
                              {log.success ? <span style={{color:'#27ae60'}}>✓</span> : <span style={{color:'#e74c3c'}}>✗ {log.error_message.slice(0,20)}</span>}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </>
          )}

          {!usageStats && (
            <div className="empty-state" style={{padding:24,marginTop:12}}>
              <p>暂无AI调用记录。完成一次AI创作/修正后，这里会自动记录。</p>
            </div>
          )}
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
