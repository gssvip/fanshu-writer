import { useState, useEffect, useContext, useRef } from 'react';
import * as yaml from 'js-yaml';
import { api } from '../api';
import { AuthContext } from '../App';
import type { Book, SkillPack, ReviewResult, AnalysisResult, WorkflowStep, RankingData, NRPlatform, NRFilters, NRRankType, NRCategory, NRListResult, NRItem } from '../types';
import { GENRES, GENRE_GROUPS, normalizeGenreKey } from '../constants';

type ToolTab = 'review' | 'skills' | 'analyze' | 'rankings';

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
  // 竞品拆书模式：normal=普通拆书 / competitor=竞品对标拆解
  const [analyzeMode, setAnalyzeMode] = useState<'normal' | 'competitor'>('normal');
  const [uploadFilename, setUploadFilename] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);

  const skillImportRef = useRef<HTMLInputElement>(null);

  // 【榜单风向 V2 · 移植自 easy-writing: NovelRank】
  const [nrPlatforms, setNrPlatforms] = useState<NRPlatform[]>([]);
  const [nrPlatform, setNrPlatform] = useState('fanqie');
  const [nrRankType, setNrRankType] = useState<string>('');
  const [nrGender, setNrGender] = useState<string>('');
  const [nrCategoryCode, setNrCategoryCode] = useState<string>('__all__');
  const [nrSubCategoryCode, setNrSubCategoryCode] = useState<string>('');
  const [nrFilters, setNrFilters] = useState<NRFilters | null>(null);
  const [nrFiltersLoading, setNrFiltersLoading] = useState(false);
  const [nrList, setNrList] = useState<NRListResult | null>(null);
  const [nrListLoading, setNrListLoading] = useState(false);
  const [nrListError, setNrListError] = useState('');
  const [nrKeyword, setNrKeyword] = useState('');
  const [nrPage, setNrPage] = useState(1);
  const [nrCrawling, setNrCrawling] = useState(false);
  // 保留原 getRankings 返回的旧结构作为 banner
  const [rankBanner, setRankBanner] = useState<RankingData | null>(null);
  // #6 手动抓取控制：筛选/分类/关键词变化时不自动抓，仅点击「抓取本榜」/ 搜索按钮 / 分页 才抓
  const [nrFetchKey, setNrFetchKey] = useState(0); // 递增：触发一次请求
  // #3 移动端自动折叠：工具箱 + 选择作品 在 activeTab=rankings 时收起
  const [toolsCollapsedMobile, setToolsCollapsedMobile] = useState<boolean | null>(null); // null=未初始化

  // 工具类型 Tab 切换 → 移动端自动展开/折叠工具箱与作品选择
  useEffect(() => {
    const isMobile = typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(max-width: 767px)').matches;
    if (!isMobile) { setToolsCollapsedMobile(false); return; }
    if (activeTab === 'rankings') {
      // 需求 3：手机端点击榜单风向，选择作品上面的（工具箱 + 选择作品）自动折叠
      setToolsCollapsedMobile(true);
    } else if (toolsCollapsedMobile !== false) {
      setToolsCollapsedMobile(false);
    }
  }, [activeTab]); // eslint-disable-line react-hooks/exhaustive-deps

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

  // 加载榜单风向：平台列表 + 默认筛选（仅首次进榜单 Tab 触发一次抓取 + 移动端加载默认源）
  useEffect(() => {
    (async () => {
      try {
        const r = await api.nrListPlatforms();
        if (Array.isArray(r.platforms)) setNrPlatforms(r.platforms);
      } catch {}
    })();
  }, []);

  // 平台/榜单类型/男女频变化 → 只刷新 filters 与默认 category/rankType/gender，**不自动抓书**（#6 手动抓取才拉）
  useEffect(() => {
    (async () => {
      setNrFiltersLoading(true);
      setNrSubCategoryCode('');
      try {
        const f = await api.nrListFilters(nrPlatform, { rankType: nrRankType || undefined, gender: nrGender || undefined });
        setNrFilters(f);
        if (f.rankTypes.length && !f.rankTypes.find(r => r.value === nrRankType)) {
          setNrRankType(f.rankTypes[0].value);
          return; // 下一个 effect 会继续修正 filters 但仍不自动抓
        }
        const all = f.categories.find((c: NRCategory) => c.code === '__all__') || f.categories[0];
        const code = (all ? all.code : '__all__');
        if (code !== nrCategoryCode) setNrCategoryCode(code);
      } catch {}
      finally { setNrFiltersLoading(false); }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nrPlatform, nrRankType, nrGender]);

  // #6 真正抓书：只有 nrFetchKey 递增（点击「抓取本榜」/搜索/分页）才会执行一次 loadNrBooks
  useEffect(() => {
    if (!activeTab || nrFetchKey === 0) return; // 0 代表尚未点击抓取
    loadNrBooks(nrSubCategoryCode || nrCategoryCode, nrRankType, nrGender, nrPage, nrKeyword, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nrFetchKey, activeTab]);

  // 进入榜单 Tab 首次：自动拉一次默认平台的默认榜单给用户看，同时填好 banner 热词
  const _didInitialFetchRef = useRef(false);
  useEffect(() => {
    if (activeTab !== 'rankings') return;
    if (_didInitialFetchRef.current) return;
    _didInitialFetchRef.current = true;
    // filters 先加载 → 然后触发首次抓取（仅一次，用 ref 保证不再重复）
    const t = window.setTimeout(() => setNrFetchKey(k => k + 1), 180);
    return () => window.clearTimeout(t);
  }, [activeTab]);

  async function loadNrBooks(catCode: string, rt: string, gd: string, page = 1, kw = '', force = false) {
    setNrListLoading(true);
    setNrListError('');
    try {
      const r = await api.nrListBooks({
        platform: nrPlatform,
        rankType: rt || undefined,
        gender: gd || undefined,
        categoryCode: catCode === '__all__' ? undefined : catCode,
        keyword: kw || undefined,
        page,
        pageSize: 20,
        force,
      });
      setNrList(r);
    } catch (e: any) { setNrListError(e?.message || '加载失败'); setNrList(null); }
    finally { setNrListLoading(false); }
    // 异步拉 banner（热门标签/上升关键词）——与 V2 钻取并行展示（#4手机端两栏各一行）
    try {
      const b = await api.getRankings(nrPlatform);
      setRankBanner(b);
    } catch { /* banner 失败不阻塞主视图 */ }
  }

  function triggerNrFetch(resetPage = false) {
    // 统一入口：点击「抓取本榜」 / 搜索按钮 / Enter 搜索 都走这个
    if (resetPage) setNrPage(1);
    // useLayoutEffect 前保证 state 都落盘后再触发：用 microtask 后 setNrFetchKey
    Promise.resolve().then(() => {
      setNrFetchKey(k => k + 1);
    });
  }

  async function handleNrCrawlNow() {
    // #6「抓取本榜」：即使没有 sourceId（未选中过榜单），也要根据当前筛选条件去爬一次（force=true 强制不走缓存）
    setNrCrawling(true);
    try {
      const sid = nrList?.sourceId;
      if (sid) {
        try { await api.nrForceCrawl(sid); } catch { /* 某些精选源无法 force，fallback 继续拉一次 force=nrListBooks 即可 */ }
      }
      await loadNrBooks(nrSubCategoryCode || nrCategoryCode, nrRankType, nrGender, nrPage, nrKeyword, true);
    } catch (e: any) { alert('刷新失败：' + (e?.message || String(e))); }
    finally { setNrCrawling(false); }
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

  function updateWorkflowTemp(idx: number, temp: number) {
    setSkillEditor(prev => ({
      ...prev,
      workflow: prev.workflow.map((w, i) => i === idx ? { ...w, temperature: temp } : w),
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
    try { const r = await api.analyzeBook(analyzeInput, analyzeMode === 'competitor' ? 'competitor' : undefined); setAnalyzeResult(r); }
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
  ];

  return (
    <div className="page tools-page">
      <header className="page-header">
        <h1>工具箱</h1>
      </header>

      {/* #3 手机端：工具箱 + 选择作品 = 可折叠面板（进榜单风向时自动收起） */}
      <section className={`tools-top-section nr-collapsible ${toolsCollapsedMobile ? 'is-collapsed' : ''}`}
               data-collapsed={toolsCollapsedMobile ? '1' : '0'}>
        <button
          type="button"
          className="nr-collapse-toggle"
          aria-expanded={!toolsCollapsedMobile}
          onClick={() => setToolsCollapsedMobile((v: boolean | null) => !(v === true))}
        >
          <span className="nr-collapse-toggle__label">
            {toolsCollapsedMobile ? '▽ 展开 工具箱 & 选择作品' : '△ 收起 工具箱 & 选择作品'}
          </span>
          <span className="nr-collapse-toggle__icon">{toolsCollapsedMobile ? '＋' : '－'}</span>
        </button>
        <div className="nr-collapsible-body">
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
        </div>
      </section>

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
                    <label>创作工作流步骤（场景级：每步可独立设置温度）</label>
                    <p className="text-muted" style={{fontSize:11}}>每个步骤对应创作中的一个环节，prompt_key 与下方提示词的键名对应。temperature 为该场景调用AI时的采样温度（0~2，缺省跟随全局配置）。</p>
                    {skillEditor.workflow.map((step, idx) => (
                      <div key={idx} className="workflow-editor-step" style={{display:'flex',flexWrap:'wrap',gap:6}}>
                        <span className="workflow-step-num">{idx + 1}</span>
                        <input className="input" style={{flex:'1 1 120px'}} value={step.name} onChange={e => updateWorkflowStep(idx, 'name', e.target.value)} placeholder="步骤名称（如：设定构建）" />
                        <input className="input" style={{flex:'1 1 160px'}} value={step.desc} onChange={e => updateWorkflowStep(idx, 'desc', e.target.value)} placeholder="步骤说明" />
                        <input className="input" style={{flex:'1 1 100px'}} value={step.prompt_key} onChange={e => updateWorkflowStep(idx, 'prompt_key', e.target.value)} placeholder="prompt键名" />
                        <label style={{display:'flex',alignItems:'center',gap:4,fontSize:12,minWidth:110}}>
                          🌡 温度
                          <input type="number" min={0} max={2} step={0.1}
                            className="input" style={{width:52,padding:'4px 6px'}}
                            value={step.temperature ?? ''}
                            onChange={e => updateWorkflowTemp(idx, parseFloat(e.target.value))}
                            placeholder="默认" />
                        </label>
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
          <div className="form-row" style={{alignItems:'center',gap:8,marginBottom:10}}>
            <button
              className={analyzeMode === 'normal' ? 'btn-primary' : 'btn-secondary'}
              style={{fontSize:12,padding:'6px 14px'}}
              onClick={() => setAnalyzeMode('normal')}
            >📖 普通拆书</button>
            <button
              className={analyzeMode === 'competitor' ? 'btn-primary' : 'btn-secondary'}
              style={{fontSize:12,padding:'6px 14px'}}
              onClick={() => setAnalyzeMode('competitor')}
              title="站在竞品对标角度，输出市场定位、核心优势、差异弱点与可复刻方案"
            >⚔️ 竞品拆书</button>
            {analyzeMode === 'competitor' && (
              <span className="text-muted" style={{fontSize:11}}>分析竞品爆款，输出对标定位 · 核心优势 · 差异化机会 · 复刻方案</span>
            )}
          </div>
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
              {analyzeMode === 'competitor' && analyzeResult.market_position && (
                <div className="review-section competitor-pos"><h4>🎯 市场定位</h4><p>{analyzeResult.market_position}</p></div>
              )}
              {analyzeMode === 'competitor' && (analyzeResult.strengths?.length || analyzeResult.weaknesses?.length) && (
                <div className="analyze-grid">
                  {analyzeResult.strengths && analyzeResult.strengths.length > 0 && (
                    <div className="analyze-item"><h4>💪 核心优势</h4><ul className="compact-list">{(analyzeResult.strengths as string[]).map((s, i) => <li key={i}>{s}</li>)}</ul></div>
                  )}
                  {analyzeResult.weaknesses && analyzeResult.weaknesses.length > 0 && (
                    <div className="analyze-item"><h4>🕳 差异化机会（弱点切入）</h4><ul className="compact-list">{(analyzeResult.weaknesses as string[]).map((w, i) => <li key={i}>{w}</li>)}</ul></div>
                  )}
                </div>
              )}
              {analyzeMode === 'competitor' && analyzeResult.copy_plan && (
                <div className="review-section"><h4>📝 复刻方案（借鉴爆点 + 规避同质化）</h4><p>{analyzeResult.copy_plan}</p></div>
              )}
              {analyzeResult.golden_lines?.length > 0 && (
                <div className="review-section"><h4>金句摘录</h4><ul>{analyzeResult.golden_lines.map((l, i) => <li key={i} className="golden-line">{l}</li>)}</ul></div>
              )}
            </div>
          )}
        </div>
      )}

      {activeTab === 'rankings' && (
        <div className="tool-panel nr-root">
          {/* #4 #5 标题行：📈 榜单风向 左侧；🔍 搜索框+按钮 紧接其右侧（桌面端 inline，移动端换行） */}
          <div className="nr-title-row" style={{
            display:'flex',flexWrap:'wrap',gap:10,alignItems:'center',
            marginBottom:10, width:'100%', boxSizing:'border-box', minWidth:0,
          }}>
            <h3 style={{margin:0,fontSize:17,fontWeight:800,color:'var(--text-primary)',
                        display:'inline-flex',alignItems:'center',gap:6,flex:'0 0 auto'}}>
              <span>📈</span><span>榜单风向</span>
            </h3>
            <div className="nr-title-search" style={{
              display:'flex', gap:8, alignItems:'center',
              flex:'1 1 260px', minWidth:200, maxWidth:'100%', boxSizing:'border-box',
            }}>
              <input
                className="input nr-search-input"
                value={nrKeyword}
                onChange={e => setNrKeyword(e.target.value)}
                placeholder="搜索书名 / 作者，找到你想仿写的同类书"
                style={{
                  flex:'1 1 auto', minWidth:0, maxWidth:'100%',
                  padding:'8px 12px', fontSize:13, minHeight:36,
                  borderRadius:10, boxSizing:'border-box',
                }}
                onKeyDown={e => { if (e.key === 'Enter') triggerNrFetch(true); }}
              />
              <button
                className="chat-send primary nr-search-btn"
                onClick={() => triggerNrFetch(true)}
                disabled={nrListLoading || nrCrawling}
                style={{
                  padding:'8px 16px',minHeight:36,fontSize:13,fontWeight:700,
                  flex:'0 0 auto',borderRadius:10,
                  background:'linear-gradient(135deg,var(--accent),var(--accent-hover))',
                  color:'#fff',border:'1px solid transparent',
                }}
              >🔍 搜索</button>
            </div>
          </div>

          {/* 筛选主卡：平台 / 抓取 / 抓取时间 / 榜单类型+频道 / 主题分类 */}
          <div className="fusion-card nr-filter-card" style={{marginBottom:12}}>
            {/* 1. 平台 Tab + 抓取本榜 + #4 抓取时间（抓取按钮右侧 inline，下方不再重复显示） */}
            <div className="nr-platform-row" style={{
              display:'flex',flexWrap:'wrap',gap:8,alignItems:'center',
              width:'100%', boxSizing:'border-box', minWidth:0,
            }}>
              <div className="nr-platform-tabs" style={{display:'flex',flexWrap:'wrap',gap:8,alignItems:'center',flex:'1 1 320px',minWidth:0}}>
                {(nrPlatforms.length ? nrPlatforms : [
                  {code:'fanqie',name:'番茄小说网'},
                  {code:'qidian',name:'起点中文网'},
                ]).map(p => (
                  <button
                    key={p.code}
                    className={`chat-send nr-platform-tab ${nrPlatform === p.code ? 'primary active' : 'ghost'}`}
                    onClick={() => setNrPlatform(p.code)}
                    title={p.remark || p.name}
                    style={{
                      padding:'8px 14px',minHeight:36,fontWeight: nrPlatform===p.code? 700: 500,
                      flex:'1 1 0', minWidth: 120, fontSize: 14,
                      borderRadius: 10,
                      background: nrPlatform===p.code
                        ? 'linear-gradient(135deg, var(--accent), var(--accent-hover))'
                        : 'var(--bg-tertiary)',
                      color: nrPlatform===p.code ? '#fff' : 'var(--text-secondary)',
                      border: nrPlatform===p.code
                        ? '1px solid transparent'
                        : '1px solid var(--border-color)',
                    }}
                  >
                    <span style={{fontSize:15}}>{p.code==='fanqie'?'🍅':p.code==='qidian'?'🏯':'📚'}</span>
                    <span style={{fontSize:13,marginLeft:4,whiteSpace:'nowrap'}}>{p.name}</span>
                  </button>
                ))}
              </div>

              {/* 竖分隔线（桌面端可见） */}
              <span className="nr-divider-v" style={{display:'none'}} />

              {/* 抓取按钮 + #4 抓取时间 inline */}
              <div className="nr-crawl-row" style={{
                display:'flex',gap:10,alignItems:'center',
                flex:'1 1 auto', minWidth:0, justifyContent:'flex-end',
              }}>
                <button
                  className="chat-send primary nr-crawl-btn"
                  onClick={handleNrCrawlNow}
                  disabled={nrCrawling || nrListLoading}
                  style={{
                    padding:'8px 18px',minHeight:36,minWidth: 140,flex:'0 0 auto',
                    borderRadius: 10, fontWeight: 800, fontSize:14,
                    background: nrList?.sourceKind==='curated'
                      ? 'linear-gradient(135deg,#f39c12,#c0392b)'
                      : 'linear-gradient(135deg,#16a085,#22a06b)',
                    border: '1px solid transparent',
                    color: '#fff',
                    boxShadow:'0 2px 10px color-mix(in srgb, var(--accent) 25%, transparent)',
                  }}
                  title="按当前筛选条件重新抓取/刷新本榜单（#6：点击才抓，不自动触发）"
                >
                  {nrCrawling ? '⏳ 抓取中…' : nrList?.sourceKind==='curated' ? '🔄 刷新精选' : '☁️ 抓取本榜'}
                </button>
                {/* #4 抓取时间：抓取按钮右侧 inline；只展示一个（下面不要了） */}
                <span className="nr-crawl-time" style={{
                  fontSize:12,color:'var(--text-muted)',whiteSpace:'nowrap',
                  display:'inline-flex',alignItems:'center',gap:4,flexShrink:0,
                  padding:'6px 10px',borderRadius:999,
                  background:'var(--bg-tertiary)',border:'1px solid var(--border-color)',
                }}>
                  <span style={{opacity:.7}}>🕒</span>
                  <span>
                    {nrList?.fetchAt
                      ? new Date(nrList.fetchAt * 1000).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).replace(/\//g,'-')
                      : '尚未抓取'}
                  </span>
                </span>
              </div>
            </div>

            {/* curate 提示 banner */}
            {nrList?.sourceKind==='curated' && (
              <div className="crawl-status crawl-tip" style={{fontSize:12,marginTop:8,padding:'6px 10px',borderRadius:8,background:'color-mix(in srgb, #f39c12 10%, transparent)',color:'#c87f10'}}>
                ℹ️ 当前榜单受目标站反爬限制，为平台精选快照
              </div>
            )}

            {/* 2. 榜单类型 & 频道：两列 title 对齐；手机端单列两行 label 仍同宽 对齐好看 (#4 标题与内容均匀对齐) */}
            <div className="nr-filter-section nr-grid-2col" style={{
              marginTop:14, display:'grid',gap:'10px 18px',
              gridTemplateColumns:'1fr 1fr',
            }}>
              {/* 左：榜单类型 */}
              <div className="nr-filter-row nr-row-rank-type nr-row-aligned"
                   style={{display:'flex',gap:8,alignItems:'flex-start'}}>
                <span className="filter-label nr-filter-label nr-label-fixed"
                      style={{fontSize:13,color:'var(--text-muted)',fontWeight:600,
                              minWidth:72,width:72,flex:'0 0 72px',paddingTop:6,
                              textAlign:'right',paddingRight:8,boxSizing:'border-box'}}>榜单类型</span>
                <div style={{flex:'1 1 0',minWidth:0,display:'flex',flexWrap:'wrap',gap:8,alignItems:'center'}}>
                  {nrFiltersLoading && <span style={{fontSize:12,color:'var(--text-muted)'}}>加载中…</span>}
                  {!nrFiltersLoading && (!nrFilters?.rankTypes?.length) && <span style={{fontSize:12,color:'var(--text-muted)'}}>暂无类型</span>}
                  {!nrFiltersLoading && nrFilters?.rankTypes?.map((rt: NRRankType) => {
                    const active = nrRankType === rt.value;
                    return (
                      <span
                        key={rt.value}
                        className={`filter-tag nr-filter-tag ${active ? 'filter-tag-active nr-filter-tag-active' : ''}`}
                        onClick={() => setNrRankType(rt.value)}
                        title={rt.label}
                        style={{
                          display:'inline-flex',alignItems:'center',justifyContent:'center', cursor:'pointer',
                          padding: '6px 14px', borderRadius: 999,
                          background: active ? 'linear-gradient(135deg,var(--accent),var(--accent-hover))' : 'var(--bg-tertiary)',
                          border: active ? '1px solid transparent' : '1px solid var(--border-color)',
                          color: active ? '#fff' : 'var(--text-secondary)',
                          fontWeight: active ? 700 : 500, fontSize: 13, lineHeight: 1.35,
                          whiteSpace: 'nowrap', transition:'all .15s ease-in-out', flexShrink:0,
                          boxShadow: active ? '0 2px 6px color-mix(in srgb, var(--accent) 25%, transparent)' : 'none',
                        }}
                      >{rt.label}</span>
                    );
                  })}
                </div>
              </div>
              {/* 右：频道（男频/女频） */}
              <div className="nr-filter-row nr-row-gender nr-row-aligned"
                   style={{display:'flex',gap:8,alignItems:'flex-start'}}>
                <span className="filter-label nr-filter-label nr-label-fixed"
                      style={{fontSize:13,color:'var(--text-muted)',fontWeight:600,
                              minWidth:72,width:72,flex:'0 0 72px',paddingTop:6,
                              textAlign:'right',paddingRight:8,boxSizing:'border-box'}}>频道</span>
                <div style={{flex:'1 1 0',minWidth:0,display:'flex',flexWrap:'wrap',gap:8,alignItems:'center'}}>
                  <span
                    className={`filter-tag nr-filter-tag ${(!nrFilters?.genders?.length || nrFilters.genders.includes('male')) && nrGender !== 'female' ? 'filter-tag-active nr-filter-tag-active' : ''}`}
                    onClick={() => setNrGender(nrGender === 'male' ? '' : 'male')}
                    style={{
                      display:'inline-flex',alignItems:'center',justifyContent:'center',cursor:'pointer',
                      padding:'6px 14px',borderRadius:999,
                      background: ((!nrFilters?.genders?.length || nrFilters.genders.includes('male')) && nrGender !== 'female')
                        ? 'linear-gradient(135deg,var(--accent),var(--accent-hover))' : 'var(--bg-tertiary)',
                      border: '1px solid var(--border-color)',
                      color: ((!nrFilters?.genders?.length || nrFilters.genders.includes('male')) && nrGender !== 'female')
                        ? '#fff' : 'var(--text-secondary)',
                      fontWeight: ((!nrFilters?.genders?.length || nrFilters.genders.includes('male')) && nrGender !== 'female') ? 700 : 500,
                      fontSize: 13, flexShrink:0, transition:'all .15s ease-in-out',
                    }}
                  >男频</span>
                  {(!nrFilters?.genders?.length || nrFilters.genders.includes('female')) && (
                    <span
                      className={`filter-tag nr-filter-tag ${nrGender === 'female' ? 'filter-tag-active nr-filter-tag-active' : ''}`}
                      onClick={() => setNrGender(nrGender === 'female' ? '' : 'female')}
                      style={{
                        display:'inline-flex',alignItems:'center',justifyContent:'center',cursor:'pointer',
                        padding:'6px 14px',borderRadius:999,
                        background: nrGender === 'female'
                          ? 'linear-gradient(135deg,var(--accent),var(--accent-hover))' : 'var(--bg-tertiary)',
                        border: '1px solid var(--border-color)',
                        color: nrGender === 'female' ? '#fff' : 'var(--text-secondary)',
                        fontWeight: nrGender === 'female' ? 700 : 500,
                        fontSize: 13, flexShrink:0, transition:'all .15s ease-in-out',
                      }}
                    >女频</span>
                  )}
                </div>
              </div>
            </div>

            {/* 3. 主题分类：美化排版（更饱满 padding, grid 多行） (#4 均匀对齐) */}
            <div className="nr-row-category" style={{marginTop:14, display:'flex',gap:8, alignItems:'flex-start'}}>
              <span className="filter-label nr-filter-label nr-label-fixed"
                    style={{fontSize:13,color:'var(--text-muted)',fontWeight:600,
                            minWidth:72,width:72,flex:'0 0 72px',paddingTop:6,
                            textAlign:'right',paddingRight:8,boxSizing:'border-box'}}>主题分类</span>
              <div className="tags-content nr-category-tags"
                   style={{flex:'1 1 0',minWidth:0,
                           display:'grid',gridTemplateColumns:'repeat(auto-fill, minmax(86px,1fr))',
                           gap:'8px 8px'}}>
                {nrFiltersLoading && <span style={{fontSize:12,color:'var(--text-muted)',gridColumn:'1 / -1'}}>加载分类中…</span>}
                {!nrFiltersLoading && (!nrFilters?.categories?.length) && <span style={{fontSize:12,color:'var(--text-muted)',gridColumn:'1 / -1'}}>暂无分类</span>}
                {!nrFiltersLoading && nrFilters?.categories?.map((c: NRCategory) => {
                  const active = nrCategoryCode === c.code;
                  return (
                    <button
                      type="button"
                      key={c.id}
                      className={`filter-tag nr-filter-tag nr-category-pill ${active ? 'filter-tag-active nr-filter-tag-active' : ''}`}
                      onClick={() => { setNrCategoryCode(c.code); setNrSubCategoryCode(''); }}
                      title={c.scope==='all'?'平台总榜':`分类榜：${c.name}`}
                      style={{
                        display:'inline-flex',alignItems:'center',justifyContent:'center',cursor:'pointer',
                        padding:'7px 10px',borderRadius:999, minHeight:30,
                        background: active ? 'linear-gradient(135deg,var(--accent),var(--accent-hover))' : 'var(--bg-tertiary)',
                        border: active ? '1px solid transparent' : '1px solid var(--border-color)',
                        color: active ? '#fff' : 'var(--text-secondary)',
                        fontWeight: active ? 700 : 500, fontSize: 12.5, transition:'all .15s ease-in-out',
                        whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis',
                      }}
                    >
                      {c.name || c.code}
                    </button>
                  );
                })}
              </div>
            </div>

            {/* 主题子类（起点等二级分类） */}
            {(() => {
              const subs = (nrFilters?.subcategories || []).filter((s: NRCategory) => s.parentCode === nrCategoryCode);
              if (!subs.length) return null;
              return (
                <div style={{marginTop:10,display:'flex',gap:8,alignItems:'flex-start'}}>
                  <span className="filter-label nr-filter-label nr-label-fixed"
                        style={{fontSize:13,color:'var(--text-muted)',fontWeight:600,
                                minWidth:72,width:72,flex:'0 0 72px',paddingTop:6,
                                textAlign:'right',paddingRight:8,boxSizing:'border-box'}}>主题细化</span>
                  <div className="tags-content nr-sub-tags"
                       style={{flex:'1 1 0',minWidth:0,display:'grid',
                               gridTemplateColumns:'repeat(auto-fill, minmax(86px,1fr))',gap:'8px 8px'}}>
                    <button
                      type="button"
                      className={`filter-tag nr-filter-tag nr-category-pill ${!nrSubCategoryCode ? 'filter-tag-active nr-filter-tag-active' : ''}`}
                      onClick={() => setNrSubCategoryCode('')}
                      style={{
                        display:'inline-flex',alignItems:'center',justifyContent:'center',cursor:'pointer',
                        padding:'7px 10px',borderRadius:999, minHeight:30,
                        background: !nrSubCategoryCode ? 'linear-gradient(135deg,var(--accent),var(--accent-hover))' : 'var(--bg-tertiary)',
                        border: !nrSubCategoryCode ? '1px solid transparent' : '1px solid var(--border-color)',
                        color: !nrSubCategoryCode ? '#fff' : 'var(--text-secondary)',
                        fontWeight: !nrSubCategoryCode ? 700 : 500, fontSize: 12.5,
                        transition:'all .15s ease-in-out', whiteSpace:'nowrap',
                      }}
                      title="全部该分类下的小说"
                    >全部</button>
                    {subs.map((s: NRCategory) => {
                      const active = nrSubCategoryCode === s.code;
                      return (
                        <button
                          type="button"
                          key={s.id}
                          className={`filter-tag nr-filter-tag nr-category-pill ${active ? 'filter-tag-active nr-filter-tag-active' : ''}`}
                          onClick={() => setNrSubCategoryCode(s.code === nrSubCategoryCode ? '' : s.code)}
                          title={`主题分类：${s.name}`}
                          style={{
                            display:'inline-flex',alignItems:'center',justifyContent:'center',cursor:'pointer',
                            padding:'7px 10px',borderRadius:999,minHeight:30,
                            background: active ? 'linear-gradient(135deg,var(--accent),var(--accent-hover))' : 'var(--bg-tertiary)',
                            border: active ? '1px solid transparent' : '1px solid var(--border-color)',
                            color: active ? '#fff' : 'var(--text-secondary)',
                            fontWeight: active ? 700 : 500, fontSize: 12.5,
                            transition:'all .15s ease-in-out', whiteSpace:'nowrap',
                            overflow:'hidden',textOverflow:'ellipsis',
                          }}
                        >{s.name || s.code}</button>
                      );
                    })}
                  </div>
                </div>
              );
            })()}
          </div>

          {/* Banner：热门标签 / 上升关键词（#4 手机端各一行；桌面端两栏并排） */}
          {rankBanner && (
            <div className="nr-banner rank-result" style={{marginTop:4,marginBottom:12}}>
              <div className="nr-banner-grid"
                   style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:10}}>
                <div className="rank-block nr-banner-hot nr-banner-row" style={{margin:0}}>
                  <h4 style={{fontSize:13,margin:'0 0 8px',display:'flex',alignItems:'center',gap:6,color:'var(--text-secondary)'}}>
                    🔥 热门标签
                  </h4>
                  <div className="nr-tags-line"
                       style={{display:'flex',flexWrap:'wrap',gap:6}}>
                    {rankBanner.hot_tags.map((t,i) => (
                      <span key={i} style={{
                        padding:'4px 10px',borderRadius:999,
                        background:'var(--bg-tertiary)',
                        border:'1px solid var(--border-color)',
                        fontSize:12,color:'var(--text-secondary)',
                        whiteSpace:'nowrap',flexShrink:0,
                      }}>{t}</span>
                    ))}
                  </div>
                </div>
                <div className="rank-block nr-banner-rising nr-banner-row" style={{margin:0}}>
                  <h4 style={{fontSize:13,margin:'0 0 8px',display:'flex',alignItems:'center',gap:6,color:'var(--text-secondary)'}}>
                    🚀 上升关键词
                  </h4>
                  <div className="nr-tags-line"
                       style={{display:'flex',flexWrap:'wrap',gap:6}}>
                    {rankBanner.rising_keywords.map((k,i) => (
                      <span key={i} style={{
                        padding:'4px 10px',borderRadius:999,
                        background:'color-mix(in srgb, var(--accent) 12%, transparent)',
                        border:'1px solid color-mix(in srgb, var(--accent) 40%, transparent)',
                        fontSize:12,color:'var(--accent)',fontWeight:600,
                        whiteSpace:'nowrap',flexShrink:0,
                      }}>{k}</span>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* 提示/错误/加载 */}
          {nrList?.fetchError && (
            <div style={{margin:'0 0 10px',padding:'8px 10px',background:'#fdecec',borderRadius:6,fontSize:12,color:'#c0392b'}}>
              ⚠️ 抓取失败：{nrList.fetchError}
              {nrList.sourceKind === 'curated' && !nrList.items.length ? '。已尝试平台精选兜底。' : ''}
            </div>
          )}
          {nrListError && !nrList?.fetchError && (
            <div style={{margin:'0 0 10px',padding:'8px 10px',background:'#fdecec',borderRadius:6,fontSize:12,color:'#c0392b'}}>
              ⚠️ {nrListError}
            </div>
          )}
          {nrListLoading && (
            <div style={{textAlign:'center',padding:30,color:'var(--text-muted)',fontSize:13}}>⏳ 加载榜单书籍…</div>
          )}

          {/* 书籍列表：双套布局（移动端卡片 + 桌面端表格）互斥显示 */}
          {!nrListLoading && nrList?.items?.length ? (
            <>
              {/* 移动端卡片（仅手机端显示）— 手机端一行一卡，minWidth:0 max-width:100% 强制不溢出 (#2 根治横溢) */}
              <div className="nr-books nr-mobile-books rank-card-grid nr-mobile-only">
                {nrList.items.map((b: any, i: number) => {
                  const change = Number(b.rankChange) || 0;
                  const n = Number(b.rankNo) || i + 1;
                  const cat = [b.categoryName, b.categorySubName].filter(Boolean).join(' / ');
                  const rawMetric = b.metricText ?? b.metricValue;
                  const metricText = (rawMetric == null || rawMetric === '' || rawMetric === 0) ? '—' : String(rawMetric);
                  return (
                    <a
                      key={i}
                      href={b.bookUrl || undefined}
                      target="_blank"
                      rel="noreferrer"
                      className="rank-card nr-mobile-card"
                      style={{
                        padding:'12px', gap:8, display:'flex',flexDirection:'column',
                        background:'var(--bg-secondary)', border:'1px solid var(--border-color)',
                        borderRadius:12, textDecoration:'none', color:'inherit',
                        width:'100%',boxSizing:'border-box',minWidth:0,maxWidth:'100%',overflow:'hidden',
                      }}
                    >
                      <div className="rank-card-head" style={{display:'flex',gap:8,alignItems:'center',flexWrap:'wrap',width:'100%',maxWidth:'100%',minWidth:0}}>
                        {n <= 3
                          ? <span className={`rank-medal r${n}`} style={{
                              width:22,height:22,borderRadius:6,display:'inline-flex',
                              alignItems:'center',justifyContent:'center',fontSize:12,fontWeight:800,color:'#fff',flexShrink:0,
                              background: n===1 ? 'linear-gradient(135deg,#f6c453,#e67e22)' : n===2 ? 'linear-gradient(135deg,#cfd8dc,#90a4ae)' : 'linear-gradient(135deg,#f0a986,#c7792b)',
                            }}>{n}</span>
                          : <span className="rank-no" style={{minWidth:22,fontSize:14,fontWeight:800,color:'var(--text-muted)',flexShrink:0}}>{n}</span>}
                        <span className={`rank-delta ${change >= 0 ? 'up' : 'down'}`} style={{fontSize:11,fontWeight:700,color: change>=0?'#22a06b':'#c0392b'}}>
                          {change > 0 ? `↑${change}` : change < 0 ? `↓${-change}` : '—'}
                        </span>
                        {b.statusText && <span className="rank-status" style={{
                          marginLeft:'auto',fontSize:10,padding:'2px 8px',borderRadius:999,
                          background:'color-mix(in srgb, var(--accent) 12%, transparent)',color:'var(--accent)',
                          whiteSpace:'nowrap',flexShrink:0,
                        }}>{b.statusText}</span>}
                      </div>
                      <div className="rank-card-main" style={{display:'flex',gap:10,minWidth:0,width:'100%',maxWidth:'100%',boxSizing:'border-box'}}>
                        {b.coverUrl ? (
                          <img
                            className="rank-cover"
                            src={b.coverUrl}
                            alt=""
                            loading="lazy"
                            style={{
                              width:52,height:70,borderRadius:8,flexShrink:0,
                              objectFit:'cover',background:'var(--bg-tertiary)',
                            }}
                          />
                        ) : (
                          <div className="rank-cover-fallback" style={{
                            width:52,height:70,borderRadius:8,flexShrink:0,
                            background:'linear-gradient(135deg,var(--accent-light),var(--bg-tertiary))',
                            display:'flex',alignItems:'center',justifyContent:'center',fontSize:24,color:'var(--accent)',
                          }}>📚</div>
                        )}
                        <div className="rank-book-info" style={{flex:'1 1 0',minWidth:0,display:'flex',flexDirection:'column',gap:4,maxWidth:'calc(100% - 62px)'}}>
                          <div className="rank-card-title" style={{
                            fontSize:14.5,fontWeight:700,color:'var(--text-primary)',lineHeight:1.35,
                            width:'100%',minWidth:0,maxWidth:'100%',wordBreak:'break-word',
                            display:'-webkit-box',WebkitLineClamp:2,WebkitBoxOrient:'vertical',overflow:'hidden',
                          }}>{b.bookTitle || '未命名'}</div>
                          {b.intro && <p className="rank-card-desc" style={{
                            fontSize:11.5,color:'var(--text-muted)',lineHeight:1.5,margin:0,
                            display:'-webkit-box',WebkitLineClamp:2,WebkitBoxOrient:'vertical',overflow:'hidden',
                            width:'100%',minWidth:0,maxWidth:'100%',wordBreak:'break-word',
                          }}>{b.intro}</p>}
                          {b.lastChapterTitle && (
                            <div className="rank-card-sub" style={{
                              fontSize:11,color:'var(--text-muted)',
                              width:'100%',minWidth:0,maxWidth:'100%',
                              overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',
                              wordBreak:'break-all',
                            }}>📝 {b.lastChapterTitle}{b.lastUpdateTimeText ? ` · ${b.lastUpdateTimeText}` : ''}</div>
                          )}
                        </div>
                      </div>
                      <div className="rank-card-meta" style={{
                        display:'flex',flexWrap:'wrap',gap:'6px 10px',
                        fontSize:11,color:'var(--text-secondary)',
                        paddingTop:6,borderTop:'1px dashed var(--border-color)',
                        width:'100%',boxSizing:'border-box',minWidth:0,maxWidth:'100%',
                      }}>
                        <span style={{minWidth:0,maxWidth:'100%',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',wordBreak:'break-all'}}>
                          <b style={{color:'var(--text-muted)',fontWeight:500}}>作者</b> {b.authorName || '—'}
                        </span>
                        {cat && <span style={{minWidth:0,maxWidth:'100%',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',wordBreak:'break-all'}}>
                          <b style={{color:'var(--text-muted)',fontWeight:500}}>分类</b> {cat}
                        </span>}
                        {metricText !== '—' && (
                          <span className="rank-metric" style={{marginLeft:'auto',flexShrink:0,minWidth:'auto',maxWidth:'100%',overflow:'hidden'}}>
                            <em style={{
                              fontStyle:'normal',display:'inline-flex',alignItems:'center',gap:4,
                              fontSize:11.5,fontWeight:700,color:'#fff',padding:'3px 10px',borderRadius:999,
                              background:'linear-gradient(135deg, var(--accent), var(--accent-hover))',
                            }}>{b.metricName ? `${b.metricName} ` : ''}{metricText}</em>
                          </span>
                        )}
                      </div>
                    </a>
                  );
                })}
              </div>

              {/* 桌面端表格（仅 ≥768px 显示，移动端隐藏） */}
              <div className="nr-books nr-desktop-books">
                <table className="rank-books-table nr-desktop-table" style={{
                  width:'100%', borderCollapse:'separate',borderSpacing:0,
                  fontSize:13.5, background:'var(--bg-secondary)',
                  border:'1px solid var(--border-color)',
                  borderRadius:12, overflow:'hidden',
                  tableLayout:'fixed',
                }}>
                  <thead>
                    <tr style={{background:'linear-gradient(180deg, var(--bg-tertiary), color-mix(in srgb, var(--bg-tertiary) 60%, var(--bg-secondary)))', color:'var(--text-muted)'}}>
                      <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,width:62,fontSize:12.5}}>#</th>
                      <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,width:56,fontSize:12.5}}>封面</th>
                      <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,fontSize:12.5}}>作品信息</th>
                      <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,width:120,fontSize:12.5}}>作者</th>
                      <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,width:140,fontSize:12.5}}>分类</th>
                      <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,width:120,fontSize:12.5}}>{nrList?.items[0]?.metricName || '指标'}</th>
                      <th style={{padding:'10px 12px',textAlign:'left',fontWeight:700,width:90,fontSize:12.5}}>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {nrList.items.map((b: NRItem, i: number) => {
                      const change = Number(b.rankChange) || 0;
                      const n = Number(b.rankNo) || i + 1;
                      return (
                        <tr key={i} style={{borderTop:'1px solid var(--border-color)', transition:'background .15s'}} className="nr-table-row">
                          <td style={{padding:'10px 12px',verticalAlign:'top',fontSize:13,
                                     fontWeight: n<=3 ? 800 : 600,
                                     color: n<=3 ? '#e67e22' : 'var(--text-secondary)'}}>
                            <div style={{display:'inline-flex',alignItems:'center',gap:6,flexWrap:'wrap'}}>
                              {n <= 3 ? (
                                <span style={{
                                  width:24,height:24,borderRadius:6,
                                  display:'inline-flex',alignItems:'center',justifyContent:'center',
                                  fontSize:12.5,fontWeight:800,color:'#fff',
                                  background: n===1 ? 'linear-gradient(135deg,#f6c453,#e67e22)' : n===2 ? 'linear-gradient(135deg,#cfd8dc,#90a4ae)' : 'linear-gradient(135deg,#f0a986,#c7792b)',
                                }}>{n}</span>
                              ) : n}
                              {change > 0 && <span style={{color:'#22a06b',fontSize:11.5,fontWeight:700}}>↑{change}</span>}
                              {change < 0 && <span style={{color:'#c0392b',fontSize:11.5,fontWeight:700}}>↓{-change}</span>}
                            </div>
                          </td>
                          <td style={{padding:'10px 12px',verticalAlign:'top'}}>
                            {b.coverUrl ? (
                              <img src={b.coverUrl} alt="" loading="lazy"
                                style={{width:40,height:54,borderRadius:6,objectFit:'cover',
                                        boxShadow:'0 2px 6px rgba(0,0,0,.08)',background:'var(--bg-tertiary)'}}/>
                            ) : (
                              <div style={{width:40,height:54,borderRadius:6,
                                          background:'linear-gradient(135deg,var(--accent-light),var(--bg-tertiary))',
                                          display:'flex',alignItems:'center',justifyContent:'center',fontSize:18,color:'var(--accent)',
                                          boxShadow:'0 2px 6px rgba(0,0,0,.04)'}}>📚</div>
                            )}
                          </td>
                          <td style={{padding:'10px 12px',verticalAlign:'top',minWidth:0,overflow:'hidden'}}>
                            <div style={{display:'flex',flexDirection:'column',gap:4,minWidth:0,width:'100%'}}>
                              {b.bookUrl ? (
                                <a href={b.bookUrl} target="_blank" rel="noreferrer"
                                  style={{fontSize:14.5,fontWeight:700,color:'var(--text-primary)',
                                          textDecoration:'none',transition:'color .15s',
                                          overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',minWidth:0,display:'block',
                                          wordBreak:'break-word',
                                  }}
                                  onMouseEnter={e => e.currentTarget.style.color='var(--accent)'}
                                  onMouseLeave={e => e.currentTarget.style.color='var(--text-primary)'}
                                >{b.bookTitle}</a>
                              ) : <span style={{fontWeight:700,color:'var(--text-primary)',fontSize:14.5,
                                               overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',display:'block'}}>{b.bookTitle}</span>}
                              {b.intro && <div style={{
                                fontSize:12,color:'var(--text-muted)',lineHeight:1.5,
                                width:'100%',minWidth:0,wordBreak:'break-word',
                                display:'-webkit-box',WebkitLineClamp:2,WebkitBoxOrient:'vertical',overflow:'hidden',
                              }}>{b.intro}</div>}
                              {b.lastChapterTitle && <div style={{
                                fontSize:12,color:'var(--text-secondary)',
                                width:'100%',minWidth:0,
                                overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap',
                              }}>📝 {b.lastChapterTitle}{b.lastUpdateTimeText ? ` · ${b.lastUpdateTimeText}` : ''}</div>}
                            </div>
                          </td>
                          <td style={{padding:'10px 12px',verticalAlign:'top',
                                     color:'var(--text-secondary)',fontSize:12.5,
                                     overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                            {b.authorName || '—'}
                          </td>
                          <td style={{padding:'10px 12px',verticalAlign:'top',
                                     color:'var(--text-secondary)',fontSize:12.5,
                                     overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                            {[b.categoryName, b.categorySubName].filter(Boolean).join(' / ') || '—'}
                          </td>
                          <td style={{padding:'10px 12px',verticalAlign:'top',fontSize:12.5,fontWeight:800,color:'var(--accent)'}}>
                            {(b.metricText ?? (b.metricValue ?? 0)) || '—'}
                          </td>
                          <td style={{padding:'10px 12px',verticalAlign:'top',
                                     color:'var(--text-secondary)',fontSize:12.5,
                                     overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                            {b.statusText || '—'}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              {/* 分页：全部改用 triggerNrFetch（#6）；分页不再直接 loadNrBooks，走 nrFetchKey 触发，保证抓取策略一致 */}
              <div className="nr-pagination" style={{
                display:'flex',justifyContent:'space-between',alignItems:'center',
                marginTop:12,gap:10,flexWrap:'wrap',
                padding:'8px 10px',
                background:'var(--bg-secondary)',
                border:'1px solid var(--border-color)',
                borderRadius:10,
                width:'100%',boxSizing:'border-box',minWidth:0,
              }}>
                <span style={{fontSize:12.5,color:'var(--text-muted)',minWidth:0,flex:'1 1 auto',overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                  📖 第 <b style={{color:'var(--accent)'}}>{nrList.page}</b> 页 · 共 <b style={{color:'var(--accent)'}}>{nrList.total}</b> 条线索
                </span>
                <div style={{display:'flex',gap:6,flex:'0 0 auto',flexWrap:'wrap'}}>
                  <button
                    className="chat-send ghost"
                    onClick={() => {const p = Math.max(1,nrList.page-1); setNrPage(p); triggerNrFetch(false); }}
                    disabled={nrList.page <= 1 || nrListLoading || nrCrawling}
                    style={{padding:'6px 14px',minHeight:30,fontSize:12.5,borderRadius:8,
                            background: nrList.page<=1||nrListLoading ? 'var(--bg-tertiary)' : 'var(--bg-secondary)',
                            color: nrList.page<=1||nrListLoading ? 'var(--text-muted)' : 'var(--text-secondary)',
                            border:'1px solid var(--border-color)',cursor: nrList.page<=1||nrListLoading ? 'not-allowed' : 'pointer',
                    }}
                  >← 上一页</button>
                  <button
                    className="chat-send primary"
                    onClick={() => {const p = nrList.page + 1; setNrPage(p); triggerNrFetch(false); }}
                    disabled={(nrList.page * (nrList.pageSize||50)) >= (nrList.total || 0) || nrListLoading || nrCrawling}
                    style={{padding:'6px 14px',minHeight:30,fontSize:12.5,borderRadius:8,fontWeight:700,
                            background: 'linear-gradient(135deg,var(--accent),var(--accent-hover))',
                            color: '#fff',border:'1px solid transparent',
                    }}
                  >下一页 →</button>
                </div>
              </div>
            </>
          ) : null}

          {!nrListLoading && nrList && (nrList.total ?? 0) === 0 && (
            <div style={{textAlign:'center',padding:30,color:'var(--text-muted)',fontSize:13,background:'var(--bg-secondary)',border:'1px dashed var(--border-color)',borderRadius:8}}>
              暂无线索 {nrKeyword ? `（关键词「${nrKeyword}」无匹配）` : '——请切换分类或手动点击☁️ 抓取本榜重试'}
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
