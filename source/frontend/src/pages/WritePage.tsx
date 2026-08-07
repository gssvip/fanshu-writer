import { useState, useEffect, useRef, useMemo, useCallback, memo } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api } from '../api';
import { useStore } from '../store';
import type { Book, BookBible, BrainstormResult, BrainstormSuggestion, Chapter, SkillPack, DynamicReport, AISession } from '../types';
import AiCreateModal from './AiCreateModal';
import EntityRegistryModal from './EntityRegistryModal';
import CarLogo from '../components/CarLogo';
import { CHAPTER_LANG_STYLES } from '../constants';

// 两行 Tab 布局：上下各 5 个维度
const TAB_ROW_1 = [
  { key: 'concept', label: '构思', icon: '💡', field: 'concept', placeholder: '一句话描述你的故事核心创意...' },
  { key: 'settings', label: '设定', icon: '⚙️', field: 'key_rules', placeholder: '核心规则、能力限制、世界观禁忌...' },
  { key: 'outline', label: '大纲', icon: '📋', field: 'plot_design', placeholder: '主线冲突、卷纲拆解、章节规划...' },
  { key: 'plot', label: '剧情', icon: '📖', field: 'timeline', placeholder: '按时间顺序列出关键事件...' },
  { key: 'characters', label: '人物及关系', icon: '👤', field: 'character_profiles', placeholder: '主角、配角的姓名、身份、性格、动机、人物关系...' },
];

const TAB_ROW_2 = [
  { key: 'chapters', label: '章节', icon: '📚', field: '', placeholder: '' },
  { key: 'inventory', label: '物资库', icon: '🎒', field: 'inventory', placeholder: '按势力/角色记录物品、功法、法宝、境界...' },
  { key: 'dynamicMemory', label: '动态文件', icon: '🗂️', field: '', placeholder: '' },
  { key: 'foreshadowing', label: '伏笔', icon: '🔮', field: 'foreshadowing', placeholder: '伏笔内容、埋设时机、回收方式...' },
  { key: 'map', label: '地图', icon: '🗺️', field: 'locations', placeholder: '' },
];

const ALL_TABS = [...TAB_ROW_1, ...TAB_ROW_2];

const FIELD_AI_PROMPTS: Record<string, string> = {
  concept: '将以下一句话构思扩展为完整的创意方案，包含核心卖点、目标读者、主线冲突、独特亮点。',
  key_rules: '根据以下构思，生成核心设定规则。包括：世界观必须遵循的规则、人物能力限制、禁忌事项。每条规则单独列出。',
  plot_design: '根据以下构思，生成故事大纲。包括：核心主线、分卷规划（每卷目标）、关键转折点、高潮设计、结局走向。',
  worldbuilding: '根据以下构思，生成详细的世界观设定。包括：世界背景、力量体系/科技水平、社会结构、地理概况、历史脉络。',
  character_profiles: '根据以下构思，生成主要人物档案。包括：主角和3-5个重要配角的姓名、身份、性格特征、背景故事、核心动机、人物关系。',
  timeline: '根据以下构思，生成剧情时间线。按时间顺序列出关键事件，每个事件标注涉及的人物和地点。',
  foreshadowing: '根据以下构思，设计3-5条伏笔线索。每条包括：伏笔内容、埋设时机（大概章节）、预期回收方式、对剧情的影响。',
  locations: '根据以下构思，设计三级地点体系。第一级为大区域（如：东大陆、西荒漠），第二级为城市/门派，第三级为具体场景。用JSON格式输出。',
};

const DIMENSION_LABELS: Record<string, string> = {
  concept: '构思',
  settings: '设定',
  outline: '大纲',
  worldview: '世界观',
  characters: '人物',
  character: '人物',
  plot: '剧情',
  chapters: '章节',
  locations: '地点',
  foreshadowing: '伏笔',
  inventory: '物资库',
};

// 维度 → 技能包 prompt_key 映射（用于查找最匹配的技能提示词）
// P2-11/12: 统一前后端映射，补充之前缺失的维度和死包key
const DIMENSION_SKILL_KEYS: Record<string, string[]> = {
  concept: ['one_line_concept', 'master_outline', 'tomato_plan', 'one_line_hook', 'story_setup'],
  key_rules: ['lock_facts', 'tomato_setting', 'base_rules', 'level_system', 'power_system', 'infinity_rules'],
  plot_design: ['master_outline', 'volume_breakdown', 'chapter_plan', 'tomato_outline', 'quick_outline', 'volume_plan', 'volume_outline'],
  worldbuilding: ['lock_facts', 'tomato_setting', 'base_rules', 'geography', 'history', 'cultures', 'era_setting', 'tech_tree', 'future_society', 'era_geopolitics'],
  character_profiles: ['character_cognition', 'tomato_character', 'cp_design', 'character_moe', 'faction_design', 'soldier_arc'],
  timeline: ['chapter_plan', 'tomato_outline', 'volume_breakdown'],
  foreshadowing: ['foreshadow_register', 'narrative_debt', 'truth_card', 'info_gap', 'red_herring'],
  locations: ['lock_facts', 'tomato_setting', 'geography'],
  // P2-11: 新增之前无映射的维度
  inventory: ['lock_facts', 'level_system', 'power_system', 'ability_tree'],
  style_guide: ['style_anchor', 'fantasy_draft', 'style_import', 'forbidden_words', 'rhythm_check'],
  relation_graph: ['character_cognition', 'faction_design', 'cp_design'],
};

// 章节AI模式 → 技能包 prompt_key 映射
// P2-11/12: 补充死包key，让"大神写作/inkos/说人话/奇幻铸魂"等技能包能被调用
const CHAPTER_SKILL_KEYS: Record<string, string[]> = {
  write: ['write_chapter', 'draft_writing', 'context_pack', 'tomato_chapter', 'fantasy_draft', 'long_write', 'short_write', 'first_draft', 'writer', 'daily_adventure', 'chapter_structure'],
  continue: ['write_chapter', 'draft_writing', 'tomato_chapter', 'fantasy_draft', 'long_write', 'writer', 'first_draft'],
  polish: ['polish', 'de_ai_check', 'minimal_rewrite', 'humanize', 'final_check', 'tomato_deai', 'forbidden_words', 'rhythm_check', 'deslop', 'draft_rewrite', 'fidelity_check', 'final_polish', 'anti_ai_audit', 'reviser', 'style_analyzer', 'protect_rewrite', 'fidelity_read', 'residual_read'],
};

// 实时清洗 LLM 流式输出中的内部标签，确保用户看到的正文始终纯净
// 后端要求 LLM 先写正文再输出 <pre_write_check>/<chapter_changes> 标签，
// 但流式显示时标签仍会实时出现，需要前端实时剥离
function stripInternalTags(content: string): string {
  let s = content;
  // 剥离完整的 <pre_write_check>...</pre_write_check> 块
  s = s.replace(/<pre_write_check>[\s\S]*?<\/pre_write_check>/gi, '');
  // 剥离未闭合的 <pre_write_check> 标签（流式传输中可能只有开始标签）
  s = s.replace(/<pre_write_check>[^<]*$/gi, '');
  s = s.replace(/<pre_write_check>/gi, '');
  // 同理处理 <chapter_changes>
  s = s.replace(/<chapter_changes>[\s\S]*?<\/chapter_changes>/gi, '');
  s = s.replace(/<chapter_changes>[^<]*$/gi, '');
  s = s.replace(/<chapter_changes>/gi, '');
  // 剥离 【标题】 行
  s = s.replace(/【标题】[^\n]*/g, '');
  // 剥离孤立的标题 JSON 块
  s = s.replace(/\{[^{}]*"title"\s*:\s*"[^"]*"[^{}]*\}/g, '');
  return s.trim();
}

// 从多个技能包中提取匹配的提示词（合并）
// 优化：每个prompt最多1500字符，最多取前3个技能包，总长度不超过5000字符（防token爆炸）
function extractSkillPrompt(packs: SkillPack[], keys: string[]): string {
  const notes: string[] = [];
  let totalLen = 0;
  for (const pack of packs.slice(0, 3)) { // 最多取前3个技能包
    if (!pack || !pack.prompts) continue;
    for (const key of keys) {
      if (pack.prompts[key]) {
        let p = pack.prompts[key].slice(0, 1500); // 每个prompt最多1500字符
        if (totalLen + p.length > 5000) {
          p = p.slice(0, 5000 - totalLen); // 总长度不超过5000字符
        }
        if (p.length === 0) break;
        notes.push(`【${pack.name}】\n${p}`);
        totalLen += p.length;
        break; // 每个包只取第一个匹配的key
      }
    }
    if (totalLen >= 5000) break;
  }
  return notes.length > 0 ? notes.join('\n\n') : '';
}

// 地图数据结构
interface MapRegion {
  name: string;
  desc?: string;
  children?: MapRegion[];
  visited?: boolean;
  isCurrent?: boolean;
}

// 【三类无污染】分组技能包选择器：按 master/style/review 三类分组渲染
// 各子面板共用，确保每个创作阶段只注入对应类别的技能包
// onlyCategory: 只显示指定类别（'master'|'style'|'review'），不传则显示全部三类
// excludeCategory: 排除指定类别（与 onlyCategory 互斥，onlyCategory 优先）
function SkillPackGroupedList({
  skillPacks, selectedSkillPackIds, onToggleSkillPack, disabled,
  onlyCategory, excludeCategory,
}: {
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  disabled?: boolean;
  onlyCategory?: 'master' | 'style' | 'review';
  excludeCategory?: 'master' | 'style' | 'review';
}) {
  // 按 category 过滤：onlyCategory 优先于 excludeCategory
  const filterFn = (p: SkillPack) => {
    const cat = p.category || 'master';
    if (onlyCategory) return cat === onlyCategory;
    if (excludeCategory) return cat !== excludeCategory;
    return true;
  };
  const masterPacks = skillPacks.filter(p => (p.category || 'master') === 'master').filter(filterFn);
  const stylePacks = skillPacks.filter(p => p.category === 'style').filter(filterFn);
  const reviewPacks = skillPacks.filter(p => p.category === 'review').filter(filterFn);
  const renderGroup = (label: string, packs: SkillPack[], hint: string) => {
    if (packs.length === 0) return null;
    return (
      <div className="skill-pack-group">
        <div className="skill-pack-group-header">{label}<span className="skill-pack-group-hint">{hint}</span></div>
        <div className="skill-pack-group-items">
          {packs.map(p => (
            <label key={p.id} className={`skill-pack-checkbox-item ${selectedSkillPackIds.includes(p.id) ? 'checked' : ''}`}>
              <input type="checkbox" checked={selectedSkillPackIds.includes(p.id)} onChange={() => onToggleSkillPack(p.id)} disabled={disabled} />
              <span className="skill-pack-checkbox-icon">{p.icon}</span>
              <span className="skill-pack-checkbox-name">{p.name}</span>
            </label>
          ))}
        </div>
      </div>
    );
  };
  return (
    <div className="skill-pack-checkbox-list skill-pack-grouped">
      {renderGroup('构思类', masterPacks, '大纲/规划/设定阶段')}
      {renderGroup('文风类', stylePacks, '正文生成阶段（选1个）')}
      {renderGroup('审查类', reviewPacks, '去AI味/一致性检查阶段')}
    </div>
  );
}

export default function WritePage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const bookId = searchParams.get('book');
  const openChatPanel = useStore((s: any) => s.openChatPanel) as (bid: string, sessionId?: string | null) => void;

  const [book, setBook] = useState<Book | null>(null);
  const [books, setBooks] = useState<Book[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState('concept');
  const [bible, setBible] = useState<BookBible | null>(null);
  const [headerCollapsed, setHeaderCollapsed] = useState(false);

  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState('');
  const [saving, setSaving] = useState(false);

  const [concept, setConcept] = useState('');
  const [brainstorming, setBrainstorming] = useState(false);
  const [brainstormResult, setBrainstormResult] = useState<BrainstormResult | null>(null);
  const [brainstormError, setBrainstormError] = useState('');
  const [adoptedSuggestions, setAdoptedSuggestions] = useState<Set<string>>(new Set());

  const [aiAssisting, setAiAssisting] = useState(false);
  const [aiError, setAiError] = useState('');
  const [bibleAiPrompt, setBibleAiPrompt] = useState('');
  const [bibleAiMode, setBibleAiMode] = useState(false);

  // 通用确认弹窗：回到原生confirm()，彻底避免React状态循环导致白屏
  function showConfirm(message: string, onConfirm: () => void) {
    if (window.confirm(message)) {
      onConfirm();
    }
  }

  // 构思AI协同创作
  const [conceptAiMode, setConceptAiMode] = useState(false);
  const [conceptAiPrompt, setConceptAiPrompt] = useState('');
  const [conceptAiAssisting, setConceptAiAssisting] = useState(false);
  const [conceptAiError, setConceptAiError] = useState('');

  // 各维度AI识别（单维度）
  const [dimAnalyzing, setDimAnalyzing] = useState(false);

  // 技能包（多选）—— 【三类无污染】按 category 分三组存储，分别持久化到 Book 表三字段
  const [skillPacks, setSkillPacks] = useState<SkillPack[]>([]);
  const [masterSkillPackIds, setMasterSkillPackIds] = useState<string[]>([]);
  const [styleSkillPackIds, setStyleSkillPackIds] = useState<string[]>([]);
  const [reviewSkillPackIds, setReviewSkillPackIds] = useState<string[]>([]);
  // 派生：合并三组ID（供子组件/后端API使用，后端 _get_skill_prompts_by_category 会按 category 过滤）
  const selectedSkillPackIds = useMemo(
    () => Array.from(new Set([...masterSkillPackIds, ...styleSkillPackIds, ...reviewSkillPackIds])),
    [masterSkillPackIds, styleSkillPackIds, reviewSkillPackIds]
  );
  const selectedSkillPacks = useMemo(() => skillPacks.filter(p => selectedSkillPackIds.includes(p.id)), [skillPacks, selectedSkillPackIds]);

  // 切换技能包勾选：按 pack.category 分流到对应组，并持久化到 Book 表对应字段
  const toggleSkillPack = useCallback((id: string) => {
    const pack = skillPacks.find(p => p.id === id);
    if (!pack) return;
    const cat = pack.category || 'master';
    const setterMap = { master: setMasterSkillPackIds, style: setStyleSkillPackIds, review: setReviewSkillPackIds } as const;
    const setter = setterMap[cat];
    setter(prev => {
      const next = prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id];
      // 持久化到 Book 表对应字段（best-effort，失败不阻断）
      if (bookId) {
        const fieldMap = { master: 'master_skill_ids', style: 'style_skill_ids', review: 'review_skill_ids' } as const;
        api.updateBook(bookId, { [fieldMap[cat]]: next } as any).catch(() => {});
      }
      return next;
    });
  }, [skillPacks, bookId]);

  // 全屏 AI 创作弹窗（统一入口：总览全局创作 + 各维度单独创作）
  const [aiCreateModalState, setAiCreateModalState] = useState<{ mode: 'global' | 'single'; dimension?: string } | null>(null);
  const [showEntityRegistry, setShowEntityRegistry] = useState(false);

  // ===== AI总创作会话历史（需求1b/3：首页与创作页消息互通 + 历史聊天记录管理） =====
  const [aiSessions, setAiSessions] = useState<AISession[]>([]);
  const [resumeSession, setResumeSession] = useState<AISession | null>(null);

  const refreshAiSessions = useCallback(async () => {
    if (!bookId) { setAiSessions([]); return; }
    try {
      // 加载本书全部 AI 智驾会话（不限数量），按更新时间倒序
      const list = await api.listAISessions(bookId);
      const sorted = (list || [])
        .slice()
        .sort((a: AISession, b: AISession) => (b.updated_at || '').localeCompare(a.updated_at || ''));
      setAiSessions(sorted);
    } catch { /* 静默 */ }
  }, [bookId]);

  useEffect(() => { refreshAiSessions(); }, [refreshAiSessions]);

  // 批量删除会话
  const handleDeleteAiSessions = useCallback((ids: string[]) => {
    if (ids.length === 0) return;
    showConfirm(`确认删除选中的 ${ids.length} 条聊天记录？此操作不可撤销。`, async () => {
      try {
        await Promise.all(ids.map(id => api.deleteAISession(id)));
        await refreshAiSessions();
      } catch (e: any) { alert('删除失败：' + (e.message || '未知错误')); }
    });
  }, [refreshAiSessions, showConfirm]);

  // 重命名会话
  const handleRenameAiSession = useCallback(async (id: string, title: string) => {
    if (!title.trim()) return;
    try {
      await api.updateAISession(id, { title: title.trim() });
      await refreshAiSessions();
    } catch (e: any) { alert('重命名失败：' + (e.message || '未知错误')); }
  }, [refreshAiSessions]);

  // 打开历史会话继续对话（AI智驾有自己的历史会话加载）
  const handleResumeAiSession = useCallback((session: AISession) => {
    if (bookId) openChatPanel(bookId, session.id);
  }, [bookId, openChatPanel]);

  // 打开 AI 智驾（原 AI总创作入口已统一到 AI智驾）
  const openNewAiCreate = useCallback(() => {
    if (bookId) openChatPanel(bookId);
  }, [bookId, openChatPanel]);


  // 单维度填入：保存到对应 BookBible 字段
  const handleAiCreateApply = useCallback(async (field: string, content: string) => {
    if (!bookId) return;
    const updated = await api.updateBible(bookId, { [field]: content } as any);
    setBible(updated);
    // 同步本地 concept 状态（构思维度单独维护）
    if (field === 'concept') setConcept(content);
  }, [bookId]);

  // 全局多维度批量填入
  const handleAiCreateApplyMany = useCallback(async (results: { field: string; content: string }[]) => {
    if (!bookId || results.length === 0) return;
    const patch: any = {};
    for (const r of results) patch[r.field] = r.content;
    const updated = await api.updateBible(bookId, patch);
    setBible(updated);
    if (patch.concept) setConcept(patch.concept);
  }, [bookId]);

  // 章节管理状态
  const [chapters, setChapters] = useState<Chapter[]>([]);
  const [activeChapter, setActiveChapter] = useState<Chapter | null>(null);
  const [chapterEditing, setChapterEditing] = useState(false);
  const [chapterEditTitle, setChapterEditTitle] = useState('');
  const [chapterEditContent, setChapterEditContent] = useState('');
  const [chapterSaving, setChapterSaving] = useState(false);

  // AI创作面板状态
  const [aiCreateMode, setAiCreateMode] = useState<'write' | 'continue' | 'polish' | null>(null);
  const [aiGeneratedContent, setAiGeneratedContent] = useState('');
  const [aiCreating, setAiCreating] = useState(false);
  const [savingChapter, setSavingChapter] = useState(false);
  const [aiStreamError, setAiStreamError] = useState('');
  const [aiUserPrompt, setAiUserPrompt] = useState('');
  // 章节AI聊天历史（持久保留，类似聊天窗口）
  // type: 'content'=章节正文（可折叠）, 'status'=状态提示（如已保存），用户提问无type
  // 主存走后端 AISession（scope='chapter'），localStorage 降级为离线缓存
  const aiChatHistoryKey = bookId ? `fanshu-ai-chat-${bookId}` : '';
  const [aiChatHistory, setAiChatHistory] = useState<Array<{ role: 'user' | 'assistant'; content: string; chapterTitle?: string; type?: 'content' | 'status'; collapsed?: boolean }>>(() => {
    try {
      if (aiChatHistoryKey) {
        const raw = localStorage.getItem(aiChatHistoryKey);
        if (raw) return JSON.parse(raw);
      }
    } catch { /* ignore */ }
    return [];
  });
  // 当前章节AI会话 ID（首次发送时创建/复用，后端 upsert）
  const aiSessionRef = useRef<string | null>(null);
  // 当前AI创作锚定的目标章节（自动识别）
  const [aiTargetChapterId, setAiTargetChapterId] = useState<string | null>(null);
  // P0-1: 多Agent协同开关（开启时调用 ai-continue 后端管线，走章节计划+正文+去AI味+一致性检查）
  // 默认开启：审校评分/标题自动生成/连续创作/OOC检测 等能力依赖此管线，开箱即用
  const [useAgentPipeline, setUseAgentPipeline] = useState(true);
  const [agentMeta, setAgentMeta] = useState<any>(null); // 存放 aiContinue 返回的 chapter_plan/温度/卷信息等
  const [spotFixing, setSpotFixing] = useState(false); // P2-9：Spot-Fix 修订中
  const [spotFixMsg, setSpotFixMsg] = useState(''); // P2-9：修订结果提示
  // 连续创作模式：批量生成 N 章
  const [batchCount, setBatchCount] = useState(3);
  const [batchCreating, setBatchCreating] = useState(false);
  const [batchProgress, setBatchProgress] = useState<{ cur: number; total: number; done: number; message?: string }>({ cur: 0, total: 0, done: 0 });
  // 本章语言风格（行文文风，最多3个叠加），注入 AI 指导本章行文
  const [chapterLangStyles, setChapterLangStyles] = useState<string[]>([]);
  const toggleChapterLangStyle = useCallback((key: string) => {
    setChapterLangStyles(prev => {
      if (prev.includes(key)) return prev.filter(k => k !== key);
      if (prev.length >= 3) return prev; // 最多3个
      return [...prev, key];
    });
  }, []);
  // 中止 AI 创作：流式通过 AbortController 取消 fetch/reader；非流式分支立即退出等待态
  const aiAbortRef = useRef<AbortController | null>(null);
  const aiStoppedRef = useRef(false);

  // 缓存回调——必须在所有 useState 之后，防止每次渲染新建函数引用引发子组件无限循环
  const startConceptAi = useCallback(() => { setConceptAiMode(true); setConceptAiError(''); }, []);
  const cancelConceptAi = useCallback(() => { setConceptAiMode(false); setConceptAiError(''); setConceptAiPrompt(''); }, []);
  const cancelChapterEdit = useCallback(() => setChapterEditing(false), []);
  const backFromChapter = useCallback(() => { setActiveChapter(null); setChapterEditing(false); }, []);

  useEffect(() => {
    if (!bookId) {
      api.listBooks().then(b => { setBooks(b); setLoading(false); }).catch(() => setLoading(false));
      return;
    }
    api.getBook(bookId).then(b => {
      setBook(b);
      setLoading(false);
      // 【三类无污染】从 Book 表三字段加载已选技能包（兼容老数据：若三字段全空则置空，等用户重新勾选）
      setMasterSkillPackIds(b.master_skill_ids || []);
      setStyleSkillPackIds(b.style_skill_ids || []);
      setReviewSkillPackIds(b.review_skill_ids || []);
      // 加载所有技能包供用户勾选
      api.listSkillPacks().then(all => {
        setSkillPacks(all);
      }).catch(() => {});
    }).catch(() => setLoading(false));
    api.getBible(bookId).then(setBible).catch(() => {});
    api.listChapters(bookId).then(setChapters).catch(() => {});
  }, [bookId]);

  useEffect(() => {
    if (bible?.concept && !concept) {
      setConcept(bible.concept);
    }
  }, [bible]);

  // 从首页 AI总创作 入口跳转过来时，URL 带 ai=global，现统一打开 AI 智驾
  useEffect(() => {
    if (bookId && searchParams.get('ai') === 'global') {
      openNewAiCreate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bookId]);

  const currentTab = ALL_TABS.find(t => t.key === activeTab) || ALL_TABS[0];
  const currentContent = bible ? (bible as any)[currentTab.field] || '' : '';

  function startEdit() {
    setEditValue(currentContent);
    setEditing(true);
  }

  async function saveEdit() {
    if (!bookId) return;
    setSaving(true);
    try {
      const updated = await api.updateBible(bookId, { [currentTab.field]: editValue } as any);
      setBible(updated);
      setEditing(false);
      if (currentTab.field === 'concept') {
        setConcept(editValue);
      }
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setSaving(false);
  }

  async function handleBrainstorm() {
    if (!bookId || !concept.trim()) return;
    setBrainstorming(true);
    setBrainstormError('');
    setBrainstormResult(null);
    setAdoptedSuggestions(new Set());
    try {
      const result = await api.brainstorm(bookId, concept, undefined, selectedSkillPackIds);
      setBrainstormResult(result);
      if (concept !== bible?.concept) {
        const updated = await api.updateBible(bookId, { concept } as any);
        setBible(updated);
      }
    } catch (e: any) {
      setBrainstormError(e.message || 'AI构思失败，请检查AI配置后重试');
    }
    setBrainstorming(false);
  }

  async function adoptSuggestion(dimension: string, suggestion: BrainstormSuggestion) {
    if (!bookId || !bible) return;
    const fieldMap: Record<string, string> = {
      concept: 'concept',
      settings: 'key_rules',
      outline: 'plot_design',
      worldview: 'worldbuilding',
      character: 'character_profiles',
      plot: 'timeline',
      locations: 'locations',
      foreshadowing: 'foreshadowing',
    };
    const field = fieldMap[dimension];

    // 章节维度特殊处理：创建 Chapter 记录
    if (dimension === 'chapters') {
      try {
        // 解析章节方案，按行拆分为章节
        const lines = suggestion.description.split('\n').filter(l => l.trim());
        const chapterPattern = /第[零一二三四五六七八九十百千\d]+[章节回]|Chapter\s*\d+|^\d+[.、:]/i;
        let created = 0;
        for (const line of lines) {
          const match = line.match(chapterPattern);
          if (match) {
            const title = line.substring(0, 100);
            await api.createChapter(bookId, {
              title,
              content: '',
              order_index: chapters.length + created,
              is_volume: false,
              parent_id: '',
            });
            created++;
          }
        }
        // 如果没匹配到章节模式，创建单个章节
        if (created === 0) {
          await api.createChapter(bookId, {
            title: suggestion.title.substring(0, 100),
            content: suggestion.description,
            order_index: chapters.length,
            is_volume: false,
            parent_id: '',
          });
        }
        // 刷新章节列表
        const updated = await api.listChapters(bookId);
        setChapters(updated);
        setAdoptedSuggestions(prev => new Set([...prev, `${dimension}-${suggestion.title}`]));
      } catch (e: any) {
        alert('采纳章节失败: ' + e.message);
      }
      return;
    }

    if (!field) return;
    const existing = (bible as any)[field] || '';
    const newContent = existing
      ? existing + '\n\n' + `【${suggestion.title}】\n${suggestion.description}`
      : `【${suggestion.title}】\n${suggestion.description}`;
    try {
      const updated = await api.updateBible(bookId, { [field]: newContent } as any);
      setBible(updated);
      setAdoptedSuggestions(prev => new Set([...prev, `${dimension}-${suggestion.title}`]));
    } catch (e: any) {
      alert('采纳失败: ' + e.message);
    }
  }

  async function handleAIAssist() {
    if (!bookId) return;
    setBibleAiMode(true);
  }

  async function executeBibleAi() {
    if (!bookId) return;
    if (!bibleAiPrompt.trim()) {
      alert('请输入你的创作要求');
      return;
    }
    setAiAssisting(true);
    setAiError('');
    try {
      const prompt = FIELD_AI_PROMPTS[currentTab.field];
      const contextConcept = concept || bible?.concept || book?.synopsis || '暂无构思';
      // 提取已勾选技能包的提示词（合并多个）
      const skillKeys = DIMENSION_SKILL_KEYS[currentTab.field] || [];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const messages = [
        { role: 'system', content: `你是专业网文创作助手。用户正在创作一部${book?.book_type || '小说'}，题材为${book?.genre || '通用'}。${bible?.worldbuilding ? `\n已有世界观：${bible.worldbuilding.slice(0, 500)}` : ''}${skillNote}` },
        { role: 'user', content: `${prompt}\n\n构思：${contextConcept}\n\n已有内容：${currentContent.slice(0, 1000) || '无'}\n\n用户具体要求：${bibleAiPrompt}` },
      ];
      const result = await api.aiChat(messages);
      setEditValue(result.content);
      setEditing(true);
      setBibleAiMode(false);
    } catch (e: any) {
      setAiError(e.message || 'AI辅助失败，请检查AI配置');
    }
    setAiAssisting(false);
  }

  async function handleDeleteField() {
    if (!bookId) return;
    showConfirm(`确定清空「${currentTab.label}」的所有内容？此操作不可撤销。`, async () => {
      try {
        const updated = await api.updateBible(bookId, { [currentTab.field]: '' } as any);
        setBible(updated);
      } catch (e: any) {
        alert('删除失败: ' + e.message);
      }
    });
  }

  // AI识别作品内容到各维度
  const [analyzing, setAnalyzing] = useState(false);
  async function handleAnalyzeContent() {
    if (!bookId) return;
    showConfirm('将用 AI 分析已有章节内容，自动识别并填充构思、设定、大纲、世界观、人物、剧情、伏笔等维度。是否继续？', async () => {
      setAnalyzing(true);
      try {
        const result = await api.analyzeContent(bookId);
        if (result.bible) setBible(result.bible);
        alert(`AI识别完成！已填充 ${result.updated_fields.length} 个维度`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzing(false);
    });
  }

  // 构思AI协同创作
  async function executeConceptAi() {
    if (!bookId) return;
    if (!conceptAiPrompt.trim()) {
      alert('请输入你的创作要求');
      return;
    }
    setConceptAiAssisting(true);
    setConceptAiError('');
    try {
      const contextConcept = concept || bible?.concept || book?.synopsis || '暂无构思';
      const skillKeys = DIMENSION_SKILL_KEYS['concept'] || [];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const messages = [
        { role: 'system', content: `你是专业网文创作助手。用户正在创作一部${book?.book_type || '小说'}，题材为${book?.genre || '通用'}。请根据用户的要求，生成或优化构思内容。${skillNote}` },
        { role: 'user', content: `当前构思：${contextConcept}\n\n已有世界观：${bible?.worldbuilding?.slice(0, 300) || '无'}\n已有人物：${bible?.character_profiles?.slice(0, 300) || '无'}\n\n用户具体要求：${conceptAiPrompt}` },
      ];
      const result = await api.aiChat(messages);
      // 将AI生成的内容追加到构思
      const newConcept = concept
        ? concept.replace(/\s+$/, '') + '\n\n' + result.content
        : result.content;
      setConcept(newConcept);
      const updated = await api.updateBible(bookId, { concept: newConcept } as any);
      setBible(updated);
      setConceptAiMode(false);
      setConceptAiPrompt('');
    } catch (e: any) {
      setConceptAiError(e.message || 'AI创作失败，请检查AI配置');
    }
    setConceptAiAssisting(false);
  }

  // 单维度AI识别（从已有章节内容中识别填充当前维度）
  async function handleAnalyzeDimension(dimension: string) {
    if (!bookId) return;
    showConfirm(`将用 AI 分析已有章节内容，自动识别并填充「${DIMENSION_LABELS[dimension] || dimension}」维度。是否继续？`, async () => {
      setDimAnalyzing(true);
      try {
        const result = await api.analyzeDimension(bookId, dimension);
        if (result.bible) setBible(result.bible);
        if (dimension === 'concept' && result.value) setConcept(result.value);
        alert(`AI识别完成！已填充「${DIMENSION_LABELS[dimension] || dimension}」维度`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setDimAnalyzing(false);
    });
  }

  // 稳定的维度回调
  const onAnalyzeConcept = useCallback(() => handleAnalyzeDimension('concept'), [bookId]);

  // 地图更新回调 —— 必须在所有 early return 之前声明
  const handleMapUpdate = useCallback(async (val: string) => {
    if (!bookId) return;
    const updated = await api.updateBible(bookId, { locations: val } as any);
    setBible(updated);
  }, [bookId]);

  // 章节操作

  function startChapterEdit() {
    if (!activeChapter) return;
    setChapterEditTitle(activeChapter.title);
    setChapterEditContent(activeChapter.content || '');
    setChapterEditing(true);
  }

  async function loadChapterDetail(chId: string) {
    if (!bookId) return;
    try {
      const ch = await api.getChapter(bookId, chId);
      setActiveChapter(ch);
      setChapterEditTitle(ch.title);
      setChapterEditContent(ch.content || '');
    } catch (e: any) {
      alert('加载章节失败: ' + e.message);
    }
  }

  async function createNewChapter(parentId?: string) {
    if (!bookId) return;
    try {
      // 章节序号按全书连续编号（跨卷累加），避免新卷下重置为“第1章”
      const globalCount = chapters.filter(c => !c.is_volume).length;
      const ch = await api.createChapter(bookId, {
        title: `第${globalCount + 1}章`,
        content: '',
        order_index: chapters.length,
        is_volume: false,
        parent_id: parentId || '',
      });
      const updated = [...chapters, ch];
      setChapters(updated);
      setActiveChapter(ch);
      setChapterEditTitle(ch.title);
      setChapterEditContent('');
      setChapterEditing(true);
    } catch (e: any) {
      alert('创建章节失败: ' + e.message);
    }
  }

  async function createNewVolume(name?: string, chapterIds?: string[]) {
    if (!bookId) return null;
    const volCount = chapters.filter(c => c.is_volume).length;
    const title = name || `第${volCount + 1}卷`;
    // 安全的 order_index：取所有章节+卷的最大 order_index + 1，避免与现有项冲突导致排序错乱
    const maxOrder = chapters.reduce((m, c) => Math.max(m, c.order_index ?? 0), -1);
    try {
      const vol = await api.createChapter(bookId, {
        title,
        content: '',
        order_index: maxOrder + 1,
        is_volume: true,
        parent_id: '',
      });
      // 如果指定了章节，将章节归入此卷
      if (chapterIds && chapterIds.length > 0) {
        for (const chId of chapterIds) {
          await api.updateChapter(bookId, chId, { parent_id: vol.id } as any);
        }
      }
      const updated = await api.listChapters(bookId);
      setChapters(updated);
      return vol;
    } catch (e: any) {
      alert('创建卷失败: ' + e.message);
      return null;
    }
  }

  async function renameVolume(volId: string, newTitle: string) {
    if (!bookId) return;
    try {
      await api.updateChapter(bookId, volId, { title: newTitle });
      const updated = await api.listChapters(bookId);
      setChapters(updated);
    } catch (e: any) {
      alert('重命名失败: ' + e.message);
    }
  }

  async function deleteVolumeFn(volId: string) {
    if (!bookId) return;
    // 特殊处理：删除全部未分卷章节
    const isOrphanDelete = volId === '__orphan__';
    const children = isOrphanDelete
      ? chapters.filter(c => !c.is_volume && !c.parent_id)
      : chapters.filter(c => c.parent_id === volId && !c.is_volume);

    if (isOrphanDelete) {
      const confirmMsg = `确定删除全部 ${children.length} 章未分卷章节？此操作不可撤销。`;
      showConfirm(confirmMsg, async () => {
        try {
          for (const ch of children) {
            await api.deleteChapter(bookId, ch.id);
          }
          const updated = await api.listChapters(bookId);
          setChapters(updated);
        } catch (e: any) {
          alert('删除失败: ' + e.message);
        }
      });
      return;
    }

    // 卷下有章节时，让用户选择删除方式
    if (children.length > 0) {
      const choice = window.confirm(
        `该卷下有 ${children.length} 章正文。\n\n` +
        `【确定】= 连同章节一起删除（整卷清空，不可撤销）\n` +
        `【取消】= 仅删除卷，章节保留并移至未分卷`
      );
      if (choice) {
        // 连章一起删
        const inner = window.confirm(`⚠️ 确认连同 ${children.length} 章正文一起删除？此操作不可撤销！`);
        if (!inner) return;
        try {
          // 先删卷下所有章节，再删卷本身
          for (const ch of children) {
            await api.deleteChapter(bookId, ch.id);
          }
          await api.deleteChapter(bookId, volId);
          const updated = await api.listChapters(bookId);
          setChapters(updated);
        } catch (e: any) {
          alert('删除失败: ' + e.message);
        }
      } else {
        // 仅删卷，章节移至未分卷
        try {
          for (const ch of children) {
            await api.updateChapter(bookId, ch.id, { parent_id: '' } as any);
          }
          await api.deleteChapter(bookId, volId);
          const updated = await api.listChapters(bookId);
          setChapters(updated);
        } catch (e: any) {
          alert('删除失败: ' + e.message);
        }
      }
    } else {
      // 空卷直接删
      showConfirm('确定删除此空卷？', async () => {
        try {
          await api.deleteChapter(bookId, volId);
          const updated = await api.listChapters(bookId);
          setChapters(updated);
        } catch (e: any) {
          alert('删除失败: ' + e.message);
        }
      });
    }
  }

  async function saveChapter() {
    if (!bookId || !activeChapter) return;
    setChapterSaving(true);
    try {
      const updated = await api.updateChapter(bookId, activeChapter.id, {
        title: chapterEditTitle,
        content: chapterEditContent,
      });
      setChapters(prev => prev.map(c => c.id === updated.id ? updated : c));
      setActiveChapter(updated);
      setChapterEditing(false);

      // ==== 章节完成后自动提示生成下一卷大纲（滚动生成工作流） ====
      // 当当前章节序号是某卷的最后一章时（order_index % 每卷章节数 === 0），提示生成下一卷
      const CHAPTERS_PER_VOLUME = 50;
      const orderIndex = activeChapter.order_index;
      if (
        orderIndex > 0 &&
        orderIndex % CHAPTERS_PER_VOLUME === 0 &&
        bible?.plot_design && bible.plot_design.trim()
      ) {
        const completedVolume = Math.floor(orderIndex / CHAPTERS_PER_VOLUME);
        const nextVolume = completedVolume + 1;
        showConfirm(
          `第${completedVolume}卷已完成（共${orderIndex}章），是否生成第${nextVolume}卷的大纲及情节节点设计？`,
          async () => {
            try {
              const result = await api.aiOutlineVolume(bookId, nextVolume, `第${nextVolume}卷`, selectedSkillPackIds, CHAPTERS_PER_VOLUME);
              // P0-2修复：后端 result.timeline 已是完整合并后的全卷数组（按 volume_index upsert），
              // 后端 result.bible 也已正确落库。直接用后端返回的 bible，不再前端手动错位合并。
              if (result.bible) {
                setBible(result.bible);
              } else {
                const updatedBible = await api.updateBible(bookId, { timeline: result.timeline } as any);
                setBible(updatedBible);
              }
              alert(`第${nextVolume}卷大纲已生成并填入剧情`);
            } catch (e: any) {
              alert(`第${nextVolume}卷大纲生成失败: ${e.message}`);
            }
          }
        );
      }
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setChapterSaving(false);
  }

  // 进入AI创作面板（不自动生成，等用户提问）
  function startAiCreate(mode: 'write' | 'continue' | 'polish') {
    if (!bookId) return;
    // 自动识别当前写到哪一章：按 order_index 排序，找最后一个有效正文（≥100字）的章节作为进度锚点
    // 用字数阈值避免空标题章/极短占位章被误判为"已写"
    const progress = computeChapterProgress();
    if (mode === 'polish' && progress.anchorChapter && (progress.anchorChapter.word_count || 0) < 100) {
      alert('当前章节没有内容可润色');
      return;
    }
    applyChapterProgress(progress, mode);
    // 不进入编辑态，AI创作面板独立显示
    setChapterEditing(false);
    setAiCreateMode(mode);
    setAiCreating(false);
    setAiStreamError('');
    // 不清空 aiGeneratedContent：保留上次输出正文，关掉再打开仍在（问题2）
  }

  // 计算章节进度（识别当前写到哪一章，待写章是哪个）
  // 抽离为独立函数，供 startAiCreate 与手动刷新共用
  // 注意：列表接口不返回 content（节省流量），用 word_count 判断章节是否已写
  function computeChapterProgress(): {
    anchorChapter: Chapter | null;
    saveTarget: Chapter | null;
    targetNum: number;
    realChapters: Chapter[];
  } {
    const EFFECTIVE_WORDS = 100;
    const realChapters = chapters
      .filter(c => !c.is_volume)
      .slice()
      .sort((a, b) => a.order_index - b.order_index);
    let anchorChapter: Chapter | null = null;
    for (let i = realChapters.length - 1; i >= 0; i--) {
      if ((realChapters[i].word_count || 0) >= EFFECTIVE_WORDS) {
        anchorChapter = realChapters[i];
        break;
      }
    }
    // 确定待写章节 = 锚点之后的下一个需要写的章（字数不足100）；没有则新建
    let saveTarget: Chapter | null = null;
    if (anchorChapter) {
      const anchorIdx = realChapters.findIndex(c => c.id === anchorChapter!.id);
      saveTarget = realChapters.slice(anchorIdx + 1).find(c => (c.word_count || 0) < EFFECTIVE_WORDS) || null;
    } else {
      // 无锚点：第一个未写满的章节即为待写章
      saveTarget = realChapters.find(c => (c.word_count || 0) < EFFECTIVE_WORDS) || null;
    }
    // 计算待写章号（全书连续编号，供提示词使用）
    let targetNum: number;
    if (saveTarget) {
      targetNum = realChapters.findIndex(c => c.id === saveTarget!.id) + 1;
    } else {
      targetNum = realChapters.length + 1;
    }
    return { anchorChapter, saveTarget, targetNum, realChapters };
  }

  // 应用章节进度到状态（设置保存目标、编辑态、提示词）
  function applyChapterProgress(progress: ReturnType<typeof computeChapterProgress>, mode?: 'write' | 'continue' | 'polish') {
    const { anchorChapter, saveTarget, targetNum, realChapters } = progress;
    const m = mode || aiCreateMode || 'write';
    if (saveTarget) {
      setAiTargetChapterId(saveTarget.id);
      setActiveChapter(saveTarget);
      setChapterEditTitle(saveTarget.title);
      setChapterEditContent(saveTarget.content || '');
    } else {
      // 新建章节占位
      setAiTargetChapterId(null);
      setChapterEditTitle(`第${targetNum}章`);
      setChapterEditContent('');
      const baseOrder = anchorChapter
        ? anchorChapter.order_index + 1
        : (realChapters[realChapters.length - 1]?.order_index || 0) + 1;
      setActiveChapter({
        id: '__ai_new__', book_id: bookId || '', title: `第${targetNum}章`,
        content: '', order_index: baseOrder, word_count: 0,
        status: 'draft', is_volume: false, parent_id: '',
        created_at: '', updated_at: '', notes: '',
      });
    }
    // 预填提问：准确定位到待写章号
    if (m === 'write' || m === 'continue') {
      setAiUserPrompt(`请创作第${targetNum}章正文，要求前后文剧情连贯、剧情符合各维度设定、语句自然无ai味儿，字数2400±100字。`);
    }
  }

  // 手动刷新章节定位（顶部刷新按钮）：重新识别进度并同步提示词
  function refreshChapterAnchor() {
    if (!bookId) return;
    const progress = computeChapterProgress();
    applyChapterProgress(progress);
    setAiGeneratedContent('');
    setAgentMeta(null);
    setAiStreamError('');
  }

  // 重新生成：取最后一条用户提问，重新执行（覆盖当前结果）
  function regenerateAiContent() {
    if (aiCreating) return;
    // 找最后一条用户消息
    const lastUserMsg = [...aiChatHistory].reverse().find(m => m.role === 'user');
    if (!lastUserMsg || !lastUserMsg.content.trim()) {
      alert('没有可重新生成的提问记录');
      return;
    }
    // 捕获当前生成内容作为"上一版"，传给后端做剧情承接参考（避免重新生成时上下文断档）
    const prevContentForCtx = aiGeneratedContent;
    // 清空当前结果与上一条助手正文（重新生成会覆盖，避免历史里重复正文）
    setAiGeneratedContent('');
    setAgentMeta(null);
    setAiStreamError('');
    setAiChatHistory(prev => {
      const arr = [...prev];
      for (let i = arr.length - 1; i >= 0; i--) {
        if (arr[i].role === 'assistant' && arr[i].type === 'content') {
          arr.splice(i, 1);
          break;
        }
      }
      return arr;
    });
    executeAiCreate(lastUserMsg.content, prevContentForCtx);
  }

  // 连续创作模式：SSE 流式批量生成 N 章，自动保存。
  // 改用流式接口解决 Render 同步请求超时（约100s）导致 Failed to fetch：
  // 每章 LLM stream + 5s 心跳保持连接活跃，每章完成推送 chapter_done 事件。
  async function batchCreate() {
    if (!bookId || batchCreating) return;
    if (batchCount < 1 || batchCount > 10) { alert('章数需在 1-10 之间'); return; }
    setBatchCreating(true);
    setBatchProgress({ cur: 0, total: batchCount, done: 0 });
    setAiStreamError('');
    // 用于用户主动停止
    const abortCtrl = new AbortController();
    aiAbortRef.current = abortCtrl;
    aiStoppedRef.current = false;
    let doneCount = 0;
    let failedList: any[] = [];
    try {
      const progress = computeChapterProgress();
      const resp = await api.aiContinueBatchStream(
        bookId, aiUserPrompt || (concept || ''), selectedSkillPackIds, batchCount,
        { startChapterNum: progress.targetNum, chapterLangStyles },
        abortCtrl.signal,
      );
      if (!resp.ok || !resp.body) {
        const errText = await resp.text().catch(() => '');
        throw new Error(errText || `HTTP ${resp.status}`);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      while (true) {
        if (aiStoppedRef.current) { abortCtrl.abort(); break; }
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE 事件以 \n\n 分隔
        const events = buffer.split('\n\n');
        buffer = events.pop() || ''; // 最后一段可能不完整，留到下次
        for (const evt of events) {
          if (!evt.startsWith('data: ')) continue;
          const payload = evt.slice(6).trim();
          if (payload === '[DONE]') continue;
          try {
            const data = JSON.parse(payload);
            if (data.type === 'chapter_start') {
              setBatchProgress(prev => ({ ...prev, cur: doneCount, message: `正在生成第${data.chapter_num}章...` }));
            } else if (data.type === 'heartbeat') {
              // 心跳：更新进度提示，保持 UI 活跃
              setBatchProgress(prev => ({ ...prev, message: data.message || `正在生成第${data.chapter_num}章...` }));
            } else if (data.type === 'chapter_done') {
              doneCount++;
              setBatchProgress({ cur: doneCount, total: batchCount, done: doneCount, message: `第${data.chapter.chapter_num}章已完成` });
              // 去AI味状态提示：success=已修正 / failed=修正失败回滚初稿 / skipped=未启用
              const deaiTag = data.chapter.deai_status === 'success'
                ? '·已去AI味'
                : data.chapter.deai_status === 'failed'
                  ? '·去AI味失败用初稿'
                  : '';
              setAiChatHistory(prev => [...prev, { role: 'assistant', content: `✅ 第${data.chapter.chapter_num}章《${data.chapter.title}》已生成（${data.chapter.word_count}字${deaiTag ? ' ' + deaiTag : ''}）并自动保存`, type: 'status' as const }]);
            } else if (data.type === 'chapter_failed') {
              failedList.push(data);
              setAiChatHistory(prev => [...prev, { role: 'assistant', content: `❌ 第${data.chapter_num}章生成失败：${data.error || '未知错误'}`, type: 'status' as const }]);
            } else if (data.type === 'batch_stopped') {
              // 【铁律】前面章节失败，后端已自动停止后续章节
              setAiChatHistory(prev => [...prev, { role: 'assistant', content: `⏹️ ${data.reason || '前面章节失败，后续章节已自动停止'}`, type: 'status' as const }]);
              setBatchProgress(prev => ({ ...prev, message: data.reason || '已停止' }));
            } else if (data.type === 'batch_done') {
              doneCount = data.total;
              failedList = Array.isArray(data.failed) ? data.failed : [];
              setBatchProgress({ cur: data.total, total: batchCount, done: data.total, message: '完成' });
              break;
            } else if (data.type === 'error') {
              throw new Error(data.message || '生成失败');
            }
          } catch (parseErr: any) {
            // 单事件解析失败不中断整体流程
          }
        }
      }
      // 刷新章节列表
      try {
        const fresh = await api.listChapters(bookId);
        setChapters(fresh);
      } catch { /* ignore */ }
      // 展示汇总信息
      let statusMsg = `✅ 批量生成完成，共生成 ${doneCount} 章并已自动保存。`;
      if (failedList.length > 0) {
        const failList = failedList.map((f: any) => `第${f.chapter_num}章（${f.error || '未知错误'}）`).join('；');
        statusMsg = `⚠️ 批量生成完成：成功 ${doneCount} 章，失败 ${failedList.length} 章。失败章节：${failList}。失败章节未保存，可手动重试或检查 AI 配置。`;
      } else if (aiStoppedRef.current) {
        statusMsg = `⏹️ 已停止生成。成功 ${doneCount} 章已自动保存。`;
      }
      setAiChatHistory(prev => [...prev, { role: 'assistant', content: statusMsg, type: 'status' as const }]);
    } catch (e: any) {
      if (e.name === 'AbortError' || aiStoppedRef.current) {
        setAiChatHistory(prev => [...prev, { role: 'assistant', content: `⏹️ 已停止生成。成功 ${doneCount} 章已自动保存。`, type: 'status' as const }]);
      } else if (e.message === 'Failed to fetch' || e.message?.includes('NetworkError')) {
        setAiStreamError('连续创作连接失败，可能正在冷启动中。请稍等几秒后重试。');
      } else {
        setAiStreamError(e.message || '连续创作失败');
      }
    } finally {
      setBatchCreating(false);
      setAiCreating(false);
    }
  }

  // 执行AI创作（用户提问后触发）
  // overridePrompt：重新生成时直接传入上一次提问，跳过清空输入框等操作
  // prevContentForCtx：上一版生成内容（重新生成场景），传给后端做剧情承接参考，避免上下文断档
  async function executeAiCreate(overridePrompt?: string, prevContentForCtx?: string) {
    if (!bookId || !activeChapter || !aiCreateMode) return;
    const promptText = (overridePrompt ?? aiUserPrompt).trim();
    if (!promptText) {
      alert('请输入你的创作要求');
      return;
    }
    // 捕获上一版生成内容（修改意见场景注入上下文；重新生成场景由调用方传入 prevContentForCtx）
    const prevGenerated = prevContentForCtx || aiGeneratedContent;
    // 将用户提问加入聊天历史
    const userMsg = { role: 'user' as const, content: promptText, chapterTitle: chapterEditTitle };
    setAiChatHistory(prev => [...prev, userMsg]);
    const currentPrompt = promptText;
    if (!overridePrompt) setAiUserPrompt(''); // 清空输入框（重新生成场景由调用方处理）
    setAiCreating(true);
    setAiStreamError('');
    setAiGeneratedContent('');
    setAgentMeta(null);
    aiStoppedRef.current = false;
    aiAbortRef.current = new AbortController();
    const signal = aiAbortRef.current.signal;

    // P0-1: 多Agent协同管线分支（章节计划→正文→去AI味→一致性检查）
    if (useAgentPipeline && (aiCreateMode === 'write' || aiCreateMode === 'continue')) {
      try {
        // 修复上下文脱节：传入待写章号 + 上一版未保存内容
        // - targetChapterNum：前端基于 word_count>=100 已写判定，比后端 max+1 更准
        // - prevChapterContent：若上一版已生成未保存，让后端把它作为"最近一章"注入，避免剧情断档
        const progress = computeChapterProgress();
        const targetChapterNum = progress.targetNum;
        // 上一版未保存内容：只在重新生成/修改意见场景传（prevGenerated 有值时），
        // 正常首发生成不传（避免把当前正在生成的内容误当上一章）
        const prevChapterContent = prevGenerated.trim().length > 200 ? prevGenerated : undefined;
        const result = await api.aiContinue(bookId, currentPrompt, selectedSkillPackIds, true, signal, { targetChapterNum, prevChapterContent, chapterLangStyles });
        if (signal.aborted || aiStoppedRef.current) return;
        setAiGeneratedContent(result.content);
        setAiChatHistory(prev => [...prev, { role: 'assistant', content: result.content, chapterTitle: chapterEditTitle, type: 'content' }]);
        setAgentMeta({
          chapter_plan: result.chapter_plan,
          temperature: result.temperature,
          vol_title: result.vol_title,
          vol_index: result.vol_index,
          current_chapter_num: result.current_chapter_num,
          deai_status: result.deai_status,
          review_notes: result.review_notes,
          consistency_passed: result.consistency_passed,
          consistency_issues: result.consistency_issues,
          has_draft: !!result.draft,
          // P0-1 + P1-6/7 新增：后写校验报告 + 章级变更回写摘要
          post_validate: result.post_validate,
          changes_applied: result.changes_applied,
          // P2-10 新增：落地门禁结果
          gate_result: result.gate_result,
          // 审校评分制：0-100 分 + 等级 + 5 维明细 + auto_revise
          chapter_score: result.chapter_score,
          // 标题自动生成：AI 解析的标题
          suggested_title: result.suggested_title,
          // formatted_title: 后端统一格式化的标题（第X章 标题），优先使用
        });
        // 标题自动生成：若 AI 返回了标题且当前标题为空或为默认"第X章"，自动回填
        // 统一使用 formatted_title（第X章 标题 格式），与连续创作模式一致
        const finalTitle = (result as any).formatted_title || result.suggested_title;
        if (finalTitle) {
          const cur = chapterEditTitle || '';
          // 当前标题为空或纯"第X章"格式（无标题文本）时才覆盖
          const isDefault = /^第[一二三四五六七八九十百零0-9]+章\s*$/.test(cur.trim()) || !cur.trim();
          if (isDefault) {
            setChapterEditTitle(finalTitle);
          }
        }
      } catch (e: any) {
        // 不因 signal.aborted 跳过错误提示和状态重置：
        // fetchWithRetry 内部超时 abort 时 signal.aborted=false 但仍抛"请求已取消"，
        // 如果 return 会跳过 finally 的 setAiCreating(false) → 界面卡死无法再点发送
        if (!aiStoppedRef.current) {
          setAiStreamError(e.message || 'Agent管线调用失败，请检查AI配置');
        }
      } finally {
        // 无条件重置 aiCreating，防止任何异常路径导致界面卡死
        // （stopAiCreate 也会设 false，重复设无副作用）
        setAiCreating(false);
      }
      return;
    }

    try {
      // ===== write/continue 模式：统一走后端 /ai-continue/stream =====
      // 与多Agent同步/连续创作模式共用 _build_ai_continue_context，确保上下文注入一致
      if (aiCreateMode === 'write' || aiCreateMode === 'continue') {
        const progress = computeChapterProgress();
        const targetChapterNum = progress.targetNum;
        const prevChapterContent = prevGenerated.trim().length > 200 ? prevGenerated : undefined;

        const response = await api.aiContinueStream(bookId, currentPrompt, selectedSkillPackIds,
          { targetChapterNum, prevChapterContent, chapterLangStyles }, signal);
        if (signal.aborted) return;
        if (!response.ok) {
          const err = await response.json().catch(() => ({ error: '请求失败' }));
          throw new Error(err.error || `HTTP ${response.status}`);
        }

        const reader = response.body?.getReader();
        if (!reader) throw new Error('无法读取流');

        const decoder = new TextDecoder();
        let buffer = '';
        let fullContent = '';

        while (true) {
          if (signal.aborted) {
            try { reader.cancel(); } catch { /* ignore */ }
            break;
          }
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const chunk = line.slice(6).trim();
            if (chunk === '[DONE]') continue;
            try {
              const parsed = JSON.parse(chunk);
              if (parsed.error) throw new Error(parsed.error);
              // meta 事件：章号/卷信息/计划
              if (parsed.meta) {
                setAgentMeta({
                  chapter_plan: parsed.chapter_plan || '',
                  temperature: parsed.temperature || 0,
                  vol_title: parsed.vol_title || '',
                  vol_index: parsed.vol_index || 0,
                  current_chapter_num: parsed.current_chapter_num || 0,
                });
                continue;
              }
              // suggested_title 事件：格式化标题
              if (parsed.suggested_title !== undefined || parsed.formatted_title !== undefined) {
                const streamTitle = parsed.formatted_title || parsed.suggested_title || '';
                if (streamTitle) {
                  const cur = chapterEditTitle || '';
                  const isDefault = /^第[一二三四五六七八九十百零0-9]+章\s*$/.test(cur.trim()) || !cur.trim();
                  if (isDefault) setChapterEditTitle(streamTitle);
                  setAgentMeta((prev: any) => ({ ...prev, suggested_title: parsed.suggested_title || '' }));
                }
                continue;
              }
              // post_validate 事件
              if (parsed.post_validate) {
                setAgentMeta((prev: any) => ({ ...prev, post_validate: parsed.post_validate }));
                continue;
              }
              // 【P1-4】changes_applied 事件：章级变更回写摘要
              if (parsed.changes_applied) {
                setAgentMeta((prev: any) => ({ ...prev, changes_applied: parsed.changes_applied }));
                continue;
              }
              // 【P1-4】deai_start 事件：去AI味开始（流式模式补充）
              if (parsed.type === 'deai_start') {
                setAgentMeta((prev: any) => ({ ...prev, deai_status: 'running' }));
                continue;
              }
              // 【P1-4】deai_result 事件：去AI味完成，用修订后正文替换初稿
              if (parsed.type === 'deai_result' && parsed.content) {
                fullContent = parsed.content;
                setAiGeneratedContent(stripInternalTags(fullContent));
                setAgentMeta((prev: any) => ({ ...prev, deai_status: 'success' }));
                continue;
              }
              // 【修复】word_count_corrected 事件：字数修正完成，用修正后正文替换初稿
              if (parsed.type === 'word_count_corrected' && parsed.content) {
                fullContent = parsed.content;
                setAiGeneratedContent(stripInternalTags(fullContent));
                if (parsed.note) {
                  setAgentMeta((prev: any) => ({ ...prev, word_count_note: parsed.note }));
                }
                continue;
              }
              // heartbeat 事件：进度提示
              if (parsed.type === 'heartbeat' && parsed.message) {
                setAgentMeta((prev: any) => ({ ...prev, heartbeat: parsed.message }));
                continue;
              }
              // LLM chunk：拼接正文
              const delta = parsed.choices?.[0]?.delta?.content || '';
              if (delta) {
                fullContent += delta;
                // 实时清洗内部标签（pre_write_check/chapter_changes等），确保用户看到纯净正文
                setAiGeneratedContent(stripInternalTags(fullContent));
              }
            } catch (e: any) {
              if (e.message && !e.message.includes('JSON')) {
                setAiStreamError(e.message);
              }
            }
          }
        }
        if (fullContent.trim()) {
          setAiChatHistory(prev => [...prev, { role: 'assistant', content: fullContent, chapterTitle: chapterEditTitle, type: 'content' }]);
        }
      } else {
        // ===== polish 模式：走通用 /ai/chat/stream（润色不需要章节上下文构建）=====
        // 人物及关系
        let charactersText = bible?.character_profiles?.slice(0, 1000) || '';
        try {
          const chars = await api.listCharacters(bookId);
          if (Array.isArray(chars) && chars.length > 0) {
            const charLines = chars.slice(0, 12).map(c =>
              `· ${c.name}（${c.role || ''}）：${(c.description || '').slice(0, 80)}；性格：${(c.personality || '').slice(0, 60)}`
            ).join('\n');
            charactersText += `\n【出场人物】\n${charLines}`;
          }
        } catch { /* 忽略 */ }

        let foreshadowingText = bible?.foreshadowing?.slice(0, 600) || '';

        // 防遗忘诊断（精简）
        let afBrief = '';
        try {
          const afRaw = (bible as any)?.anti_forget_reports;
          if (afRaw) {
            const afList = JSON.parse(afRaw);
            if (Array.isArray(afList) && afList.length > 0) {
              const recent = afList.slice(-2);
              const lines: string[] = [];
              for (const r of recent) {
                const rp = r?.report || {};
                const vs = Array.isArray(rp.violations) ? rp.violations.slice(0, 4) : [];
                for (const v of vs) {
                  const msg = typeof v === 'string' ? v : (v?.issue || v?.message || v?.desc || '');
                  if (msg) lines.push(`- ${msg.slice(0, 100)}`);
                }
              }
              if (lines.length > 0) afBrief = `\n【防遗忘诊断（润色须规避）】\n${lines.join('\n')}`;
            }
          }
        } catch { /* ignore */ }

        // 技能包
        const skillKeys = CHAPTER_SKILL_KEYS[aiCreateMode] || [];
        const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
        const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';

        const systemContent = `你是专业网文编辑。请根据用户的润色要求对内容进行优化，保持原意不变，提升文采和节奏感，增强场景感与信息差悬念。直接输出润色后的全文。${skillNote}`;
        const userContent = `章节：${chapterEditTitle}

【人物与关系】${charactersText ? '\n' + charactersText : '无'}
【伏笔】${foreshadowingText || '无'}${afBrief}

用户润色要求：${currentPrompt}

原文：
${chapterEditContent}`;

        const messages = [
          { role: 'system', content: systemContent },
          { role: 'user', content: userContent },
        ];

        const response = await api.aiChatStream(messages, signal);
        if (signal.aborted) return;
        if (!response.ok) {
          const err = await response.json().catch(() => ({ error: '请求失败' }));
          throw new Error(err.error || `HTTP ${response.status}`);
        }

        const reader = response.body?.getReader();
        if (!reader) throw new Error('无法读取流');

        const decoder = new TextDecoder();
        let buffer = '';
        let fullContent = '';

        while (true) {
          if (signal.aborted) {
            try { reader.cancel(); } catch { /* ignore */ }
            break;
          }
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              const chunk = line.slice(6).trim();
              if (chunk === '[DONE]') break;
              try {
                const parsed = JSON.parse(chunk);
                if (parsed.error) throw new Error(parsed.error);
                const delta = parsed.choices?.[0]?.delta?.content || '';
                if (delta) {
                  fullContent += delta;
                  setAiGeneratedContent(stripInternalTags(fullContent));
                }
              } catch (e: any) {
                if (e.message && !e.message.includes('JSON')) {
                  setAiStreamError(e.message);
                }
              }
            }
          }
        }
        if (fullContent.trim()) {
          setAiChatHistory(prev => [...prev, { role: 'assistant', content: fullContent, chapterTitle: chapterEditTitle, type: 'content' }]);
        }
      }
    } catch (e: any) {
      // 同 Agent 管线分支：不因 signal.aborted 跳过状态重置，防止界面卡死
      if (!aiStoppedRef.current) {
        setAiStreamError(e.message || 'AI创作失败，请检查AI配置');
      }
    }
    // 无条件重置 aiCreating，防止任何异常路径导致界面卡死
    setAiCreating(false);
  }

  // 停止 AI 创作：流式时中断 fetch/reader；非流式分支仅退出等待态（后端可能继续运行）
  function stopAiCreate() {
    if (!aiAbortRef.current) return;
    aiStoppedRef.current = true;
    aiAbortRef.current.abort();
    aiAbortRef.current = null;
    setAiCreating(false);
    setAiStreamError('已停止生成');
  }

  // 确认AI生成内容，保存到目标章节；保存后面板保持开启并自动推进到下一章
  // 章节自动归卷：按 50 章/卷规则，新建章节时根据章号计算应归入的卷（无需手动新建章节/卷）
  async function confirmAiContent() {
    if (savingChapter) return; // 防重复点击
    if (!aiCreateMode || !aiGeneratedContent.trim() || !bookId) return;
    setSavingChapter(true);
    let content = aiGeneratedContent;
    if (aiCreateMode === 'continue') {
      content = chapterEditContent
        ? chapterEditContent.replace(/\s+$/, '') + '\n\n' + content
        : content;
    }
    const savedTitle = chapterEditTitle;
    const savedWordCount = content.length;

    // 计算本章节在全书（不含卷）的连续序号，用于归卷和章号
    const realChaptersBefore = chapters
      .filter(c => !c.is_volume)
      .slice()
      .sort((a, b) => a.order_index - b.order_index);
    // 当前待保存章的序号（1-based）：已有目标章用其位置，否则为新增章
    let chapterSeq: number;
    if (aiTargetChapterId) {
      chapterSeq = realChaptersBefore.findIndex(c => c.id === aiTargetChapterId) + 1;
    } else {
      chapterSeq = realChaptersBefore.length + 1;
    }

    // 按 50 章/卷归卷：章号 1-50 → 第1卷，51-100 → 第2卷...
    const CHAPTERS_PER_VOLUME = 50;
    const volumeIndex = Math.floor((chapterSeq - 1) / CHAPTERS_PER_VOLUME) + 1;
    // 查找或创建对应卷
    const volumes = chapters.filter(c => c.is_volume).sort((a, b) => a.order_index - b.order_index);
    let targetVolume = volumes.find(v => {
      const m = (v.title || '').match(/第(\d+)卷/);
      return m && parseInt(m[1]) === volumeIndex;
    });
    let targetVolumeId = '';
    if (!targetVolume) {
      // 卷不存在则新建
      try {
        const volOrder = volumes.length > 0 ? (volumes[volumes.length - 1].order_index + 100) : 0;
        targetVolume = await api.createChapter(bookId, {
          title: `第${volumeIndex}卷`,
          content: '',
          order_index: volOrder,
          is_volume: true,
          parent_id: '',
        });
        setChapters(prev => [...prev, targetVolume!]);
        targetVolumeId = targetVolume.id;
      } catch (e: any) {
        // 卷创建失败则归为未分卷
        targetVolumeId = '';
      }
    } else {
      targetVolumeId = targetVolume.id;
    }

    // 保存到目标章节
    try {
      let savedChapter: Chapter | null = null;
      if (aiTargetChapterId) {
        // 更新已有章节（同步归卷）
        await api.updateChapter(bookId, aiTargetChapterId, { title: chapterEditTitle, content, parent_id: targetVolumeId });
        setChapters(prev => prev.map(c => c.id === aiTargetChapterId ? { ...c, title: chapterEditTitle, content, word_count: content.length, parent_id: targetVolumeId } : c));
        savedChapter = chapters.find(c => c.id === aiTargetChapterId) || null;
        if (savedChapter) savedChapter = { ...savedChapter, title: chapterEditTitle, content, word_count: content.length, parent_id: targetVolumeId };
      } else {
        // 新建章节（自动归入对应卷）
        const realMaxOrder = realChaptersBefore.length > 0
          ? Math.max(...realChaptersBefore.map(c => c.order_index))
          : -1;
        const ch = await api.createChapter(bookId, {
          title: chapterEditTitle,
          content,
          order_index: realMaxOrder + 1,
          is_volume: false,
          parent_id: targetVolumeId,
        });
        setChapters(prev => [...prev, ch]);
        savedChapter = ch;
      }
      // 记录保存结果到聊天历史（不关闭面板，便于连续创作）
      // 同时将已确认章节的正文消息自动折叠，为下一章输出留出空间（方便手机阅读）
      setAiChatHistory(prev => [
        ...prev.map(m => m.type === 'content' ? { ...m, collapsed: true } : m),
        {
          role: 'assistant' as const,
          content: `✅ 已保存到「${savedTitle}」（${savedWordCount}字）。可继续输入要求创作下一章。`,
          chapterTitle: savedTitle,
          type: 'status' as const,
        },
      ]);

      // 自动推进到下一章：以刚保存的章节作为新锚点，找下一个需要写的章
      const EFFECTIVE_WORDS = 100;
      const realChapters = chapters
        .filter(c => !c.is_volume)
        .slice()
        .sort((a, b) => a.order_index - b.order_index);
      const savedIdx = savedChapter ? realChapters.findIndex(c => c.id === savedChapter!.id) : -1;
      let nextTarget: Chapter | null = null;
      if (savedIdx >= 0) {
        nextTarget = realChapters.slice(savedIdx + 1).find(c => (c.word_count || 0) < EFFECTIVE_WORDS) || null;
      }
      let nextNum: number;
      if (nextTarget) {
        setAiTargetChapterId(nextTarget.id);
        setActiveChapter(nextTarget);
        setChapterEditTitle(nextTarget.title);
        setChapterEditContent(nextTarget.content || '');
        nextNum = realChapters.findIndex(c => c.id === nextTarget!.id) + 1;
      } else {
        // 新建下一章占位
        setAiTargetChapterId(null);
        nextNum = realChapters.length + 1;
        setChapterEditTitle(`第${nextNum}章`);
        setChapterEditContent('');
        const baseOrder = (savedChapter?.order_index || realChapters[realChapters.length - 1]?.order_index || 0) + 1;
        setActiveChapter({
          id: '__ai_new__', book_id: bookId, title: `第${nextNum}章`,
          content: '', order_index: baseOrder, word_count: 0,
          status: 'draft', is_volume: false, parent_id: '',
          created_at: '', updated_at: '', notes: '',
        });
      }
      // 清空本次生成内容，但保留 aiCreateMode 与聊天历史
      setAiGeneratedContent('');
      setAiStreamError('');
      setAiUserPrompt(`请创作第${nextNum}章正文，要求前后文剧情连贯、剧情符合各维度设定、语句自然无ai味儿，字数2400±100字。`);
    } catch (e: any) {
      alert('保存章节失败: ' + e.message);
    } finally {
      setSavingChapter(false);
    }
  }

  // 取消AI创作（保留聊天历史 + 保留上次输出正文，下次打开仍在）
  function cancelAiCreate() {
    aiAbortRef.current?.abort();
    aiAbortRef.current = null;
    setAiCreateMode(null);
    // 不清空 aiGeneratedContent：保留上次正文，关掉再打开仍在
    setAiCreating(false);
    setAiStreamError('');
    setAiUserPrompt('');
  }

  // 清空AI聊天历史（同步删除后端会话，下次发送重建）
  function clearAiChatHistory() {
    setAiChatHistory([]);
    if (aiSessionRef.current) {
      api.deleteAISession(aiSessionRef.current).catch(() => {});
      aiSessionRef.current = null;
    }
    try { localStorage.removeItem(aiChatHistoryKey); } catch {}
  }

  // 折叠/展开某条聊天消息（用于章节正文长内容）
  function toggleChatMsgCollapse(index: number) {
    setAiChatHistory(prev => prev.map((m, i) => i === index ? { ...m, collapsed: !m.collapsed } : m));
  }

  // 组件卸载时中止进行中的请求
  useEffect(() => {
    return () => {
      aiAbortRef.current?.abort();
    };
  }, []);

  // 持久化聊天历史到 localStorage（按 bookId），刷新/重新打开仍在
  // 切换书籍时清空当前历史，优先从后端 AISession 加载，回退 localStorage
  useEffect(() => {
    if (!aiChatHistoryKey || !bookId) return;
    aiSessionRef.current = null;
    // 后端拉取章节会话（scope='chapter'，scope_id=bookId）
    api.listAISessions(bookId, 'chapter').then(sessions => {
      if (sessions && sessions.length > 0) {
        const s = sessions[0];
        aiSessionRef.current = s.id;
        // 过滤 system 消息，并转型为章节聊天历史格式
        const msgs = (s.messages || [])
          .filter((m: any) => m && m.content && (m.role === 'user' || m.role === 'assistant'))
          .map((m: any) => ({
            role: m.role as 'user' | 'assistant',
            content: m.content as string,
            chapterTitle: m.chapterTitle,
            type: m.type,
            collapsed: m.collapsed,
          }));
        if (msgs.length > 0) {
          setAiChatHistory(msgs);
          // 同步到 localStorage 作为离线缓存
          try { localStorage.setItem(aiChatHistoryKey, JSON.stringify(msgs)); } catch {}
          return;
        }
      }
      // 后端无会话，回退 localStorage
      loadFromLocalStorage();
    }).catch(() => loadFromLocalStorage());

    function loadFromLocalStorage() {
      try {
        const raw = localStorage.getItem(aiChatHistoryKey);
        const stored = raw ? JSON.parse(raw) : [];
        if (aiChatHistory.length === 0 && Array.isArray(stored) && stored.length > 0) {
          setAiChatHistory(stored);
        }
      } catch { /* ignore */ }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aiChatHistoryKey]);

  // 聊天历史变化时：写 localStorage + 异步写后端 AISession
  useEffect(() => {
    if (!aiChatHistoryKey) return;
    try {
      localStorage.setItem(aiChatHistoryKey, JSON.stringify(aiChatHistory));
    } catch { /* ignore quota */ }
    // 写后端（确保有 session id，没有则 upsert 创建）
    if (bookId && aiChatHistory.length > 0) {
      persistChapterSession().catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aiChatHistoryKey, aiChatHistory]);

  // 持久化章节会话到后端
  async function persistChapterSession() {
    if (!bookId) return;
    if (!aiSessionRef.current) {
      try {
        const s = await api.createAISession({ book_id: bookId, scope: 'chapter', scope_id: bookId, title: '章节AI创作' });
        aiSessionRef.current = s.id;
      } catch { return; }
    }
    try {
      await api.updateAISession(aiSessionRef.current!, { messages: aiChatHistory as any[] });
    } catch { /* ignore */ }
  }

  async function deleteChapter(chId: string) {
    if (!bookId) return;
    showConfirm('确定删除这一章吗？', async () => {
      try {
        await api.deleteChapter(bookId, chId);
        const updated = chapters.filter(c => c.id !== chId);
        setChapters(updated);
        if (activeChapter?.id === chId) setActiveChapter(null);
      } catch (e: any) {
        alert('删除失败: ' + e.message);
      }
    });
  }

  if (loading) return <div className="page loading-screen"><span>加载中...</span></div>;

  if (!bookId || !book) {
    return (
      <div className="page write-page">
        <header className="page-header">
          <h1>选择作品</h1>
        </header>
        <div className="book-grid">
          {books.map(b => (
            <div key={b.id} className="book-card" onClick={() => navigate(`/write?book=${b.id}`)}>
              <div className="book-card-cover">
                {b.cover_path ? <img src={b.cover_path} alt="" /> : <div className="cover-placeholder">📖</div>}
              </div>
              <div className="book-card-info">
                <h3>{b.title}</h3>
                <div className="book-card-meta">
                  <span>{b.book_type === 'novel' ? '长篇' : b.book_type === 'script' ? '剧本' : '短篇'}</span>
                  <span>{b.word_count}字</span>
                </div>
              </div>
            </div>
          ))}
          {books.length === 0 && (
            <div className="empty-state" style={{gridColumn:'1/-1'}}>
              <div className="empty-icon">📚</div>
              <p>还没有作品，去首页创建吧</p>
              <button className="btn-primary" onClick={() => navigate('/workbench')}>前往首页</button>
            </div>
          )}
        </div>
      </div>
    );
  }

  // 判断是否是图谱类 tab（已移除关系图谱/地点图谱/境界图谱）
  const isMapTab = activeTab === 'map';
  const isChapterTab = activeTab === 'chapters';
  const isOutlineTab = activeTab === 'outline';
  const isDynamicMemoryTab = activeTab === 'dynamicMemory';
  const isCharacterTab = activeTab === 'characters';
  const isPlotTab = activeTab === 'plot';
  const isInventoryTab = activeTab === 'inventory';
  const isForeshadowingTab = activeTab === 'foreshadowing';

  return (
    <div className={`page write-page${isChapterTab ? ' chapter-mode' : ''}`}>
      <header className={`page-header ${headerCollapsed ? 'header-collapsed' : ''}`}>
        <div className="page-header-left">
          <button className="btn-ghost" onClick={() => navigate('/workbench')}>←</button>
          <div>
            <h1>{book.title}</h1>
            <div className="book-meta">
              <span>{book.book_type === 'novel' ? '长篇' : book.book_type === 'script' ? '剧本' : '短篇'}</span>
              <span>{book.word_count}字</span>
            </div>
          </div>
        </div>
        <div className="page-header-right">
          <button
            className="btn-ghost-sm"
            onClick={() => setShowEntityRegistry(true)}
            disabled={!bookId}
            title="跨维度统一管理角色/势力/地点/物品，重命名/合并会同步到全部设定与正文"
          >
            <span aria-hidden>🏗️</span><span className="btn-label">实体管理</span>
          </button>
          <button className="btn-ghost-sm" onClick={handleAnalyzeContent} disabled={analyzing || dimAnalyzing || chapters.length === 0} title={chapters.length === 0 ? '需要先创建章节才能AI识别' : 'AI分析章节内容，一键识别全部维度'}>
            <span aria-hidden>{analyzing ? '🤖' : '🔍'}</span><span className="btn-label">{analyzing ? '识别中' : '全部识别'}</span>
          </button>
          <button className="btn-ghost-sm header-collapse-btn" onClick={() => setHeaderCollapsed(!headerCollapsed)} title={headerCollapsed ? '展开头部' : '收起头部'}>
            {headerCollapsed ? '▾' : '▴'}
          </button>
        </div>
      </header>

      {headerCollapsed && (
        <div className="compact-tab-bar">
          <button className="btn-ghost-sm" onClick={() => setHeaderCollapsed(false)} title="展开">
            ▾ {currentTab.icon} {currentTab.label}
          </button>
        </div>
      )}

      {!headerCollapsed && (
        <div className="write-tabs-two-rows">
          <div className="write-tab-row">
            {TAB_ROW_1.map(tab => (
              <button
                key={tab.key}
                className={`write-tab ${activeTab === tab.key ? 'active' : ''}`}
                onClick={() => { setActiveTab(tab.key); setEditing(false); setAiError(''); }}
              >
                <span className="write-tab-icon">{tab.icon}</span>
                <span className="write-tab-label">{tab.label}</span>
              </button>
            ))}
          </div>
          <div className="write-tab-row">
            {TAB_ROW_2.map(tab => (
              <button
                key={tab.key}
                className={`write-tab ${activeTab === tab.key ? 'active' : ''}`}
                onClick={() => { setActiveTab(tab.key); setEditing(false); setAiError(''); }}
              >
                <span className="write-tab-icon">{tab.icon}</span>
                <span className="write-tab-label">{tab.label}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="write-content">
        {activeTab === 'concept' ? (
          <ConceptPanel
            concept={concept}
            setConcept={setConcept}
            bible={bible}
            bookTitle={book?.title || ''}
            brainstorming={brainstorming}
            brainstormResult={brainstormResult}
            brainstormError={brainstormError}
            adoptedSuggestions={adoptedSuggestions}
            onBrainstorm={handleBrainstorm}
            onAdopt={adoptSuggestion}
            bookId={bookId}
            onBibleUpdate={setBible}
            hasChapters={chapters.length > 0}
            conceptAiMode={conceptAiMode}
            conceptAiPrompt={conceptAiPrompt}
            conceptAiAssisting={conceptAiAssisting}
            conceptAiError={conceptAiError}
            onStartConceptAi={startConceptAi}
            onExecuteConceptAi={executeConceptAi}
            onCancelConceptAi={cancelConceptAi}
            onEditConceptAiPrompt={setConceptAiPrompt}
            onAnalyzeDimension={onAnalyzeConcept}
            dimAnalyzing={dimAnalyzing}
            skillPacks={skillPacks}
            selectedSkillPackIds={selectedSkillPackIds}
            onToggleSkillPack={toggleSkillPack}
            selectedSkillPacks={selectedSkillPacks}
            onOpenAiCreate={openNewAiCreate}
            aiSessions={aiSessions}
            onRefreshSessions={refreshAiSessions}
            onDeleteAiSessions={handleDeleteAiSessions}
            onRenameAiSession={handleRenameAiSession}
            onResumeAiSession={handleResumeAiSession}
          />
        ) : isMapTab ? (
          <LocationsPanel
            bookId={bookId || ''}
            bible={bible}
            onBibleUpdate={setBible}
            bookTitle={book?.title || ''}
            chapters={chapters}
            hasChapters={chapters.length > 0}
            showConfirm={showConfirm}
            selectedSkillPackIds={selectedSkillPackIds}
            onMapUpdate={handleMapUpdate}
            onOpenAiCreate={() => setAiCreateModalState({ mode: 'single', dimension: 'locations' })}
          />
        ) : isForeshadowingTab ? (
          <ForeshadowingPanel
            bookId={bookId || ''}
            bible={bible}
            onBibleUpdate={setBible}
            bookTitle={book?.title || ''}
            chapters={chapters}
            hasChapters={chapters.length > 0}
            showConfirm={showConfirm}
            skillPacks={skillPacks}
            selectedSkillPackIds={selectedSkillPackIds}
            selectedSkillPacks={selectedSkillPacks}
            onOpenAiCreate={() => setAiCreateModalState({ mode: 'single', dimension: 'foreshadowing' })}
          />
        ) : isChapterTab ? (
          <ChapterPanel
            chapters={chapters}
            activeChapter={activeChapter}
            chapterEditing={chapterEditing}
            chapterEditTitle={chapterEditTitle}
            chapterEditContent={chapterEditContent}
            chapterSaving={chapterSaving}
            aiCreateMode={aiCreateMode}
            aiGeneratedContent={aiGeneratedContent}
            aiCreating={aiCreating}
            savingChapter={savingChapter}
            aiStreamError={aiStreamError}
            aiUserPrompt={aiUserPrompt}
            skillPacks={skillPacks}
            selectedSkillPackIds={selectedSkillPackIds}
            onToggleSkillPack={toggleSkillPack}
            selectedSkillPacks={selectedSkillPacks}
            onSelectChapter={loadChapterDetail}
            onCreateChapter={createNewChapter}
            onCreateVolume={createNewVolume}
            onSaveChapter={saveChapter}
            onDeleteChapter={deleteChapter}
            onCancelEdit={cancelChapterEdit}
            onStartEdit={startChapterEdit}
            onEditTitle={setChapterEditTitle}
            onEditContent={setChapterEditContent}
            onBackToList={backFromChapter}
            onStartAiCreate={startAiCreate}
            onExecuteAiCreate={executeAiCreate}
            onConfirmAiContent={confirmAiContent}
            onCancelAiCreate={cancelAiCreate}
            onStopAiCreate={stopAiCreate}
            onEditAiPrompt={setAiUserPrompt}
            onRenameVolume={renameVolume}
            onDeleteVolume={deleteVolumeFn}
            bookId={bookId}
            useAgentPipeline={useAgentPipeline}
            onToggleAgentPipeline={setUseAgentPipeline}
            agentMeta={agentMeta}
            spotFixing={spotFixing}
            spotFixMsg={spotFixMsg}
            onSpotFix={async (content, postValidate) => {
              if (!bookId || !content) return;
              setSpotFixing(true);
              try {
                const res = await api.aiSpotFix(bookId, content, postValidate, 'auto');
                if (res.strategy === 'spot_fix' && res.content) {
                  setAiGeneratedContent(res.content);
                  setAiChatHistory((prev: typeof aiChatHistory) => {
                    const next = [...prev];
                    const last = next[next.length - 1];
                    if (last && last.type === 'content') {
                      next[next.length - 1] = { ...last, content: res.content };
                    }
                    return next;
                  });
                  setAgentMeta((prev: any) => ({ ...prev, post_validate: res.post_validate || prev.post_validate }));
                  setSpotFixMsg(`✅ 已修订 ${res.patches_count || 0} 处，节省 ${res.token_saving?.saving_ratio ? Math.round(res.token_saving.saving_ratio * 100) : 0}% token`);
                } else if (res.strategy === 'rewrite') {
                  setSpotFixMsg('⚠️ 检测到结构性问题，建议整章重写');
                } else {
                  setSpotFixMsg(res.message || '无需修订');
                }
              } catch (e: any) {
                setSpotFixMsg(`❌ 修订失败：${e.message}`);
              } finally {
                setSpotFixing(false);
                setTimeout(() => setSpotFixMsg(''), 5000);
              }
            }}
            aiChatHistory={aiChatHistory}
            onClearAiChatHistory={clearAiChatHistory}
            onToggleChatMsgCollapse={toggleChatMsgCollapse}
            onRefreshChapterAnchor={refreshChapterAnchor}
            onRegenerateAiContent={regenerateAiContent}
            aiTargetChapterId={aiTargetChapterId}
            chapterLangStyles={chapterLangStyles}
            onToggleChapterLangStyle={toggleChapterLangStyle}
            batchCount={batchCount}
            onBatchCountChange={setBatchCount}
            batchCreating={batchCreating}
            batchProgress={batchProgress}
            onBatchCreate={batchCreate}
          />
        ) : isDynamicMemoryTab ? (
          <DynamicMemoryPanel
            bookId={bookId}
            concept={concept || bible?.concept || ''}
            bible={bible}
            onBibleUpdate={setBible}
            chapters={chapters}
            showConfirm={showConfirm}
            skillPacks={skillPacks}
            selectedSkillPackIds={selectedSkillPackIds}
            onToggleSkillPack={toggleSkillPack}
            selectedSkillPacks={selectedSkillPacks}
            onOpenAiCreate={() => setAiCreateModalState({ mode: 'single', dimension: 'dynamic_volumes' })}
          />
        ) : isOutlineTab ? (
          <OutlineCombinedPanel
            bookId={bookId}
            bible={bible}
            onBibleUpdate={setBible}
            concept={concept}
            hasChapters={chapters.length > 0}
            dimAnalyzing={dimAnalyzing}
            onAnalyzeDimension={(dim) => handleAnalyzeDimension(dim)}
            skillPacks={skillPacks}
            selectedSkillPackIds={selectedSkillPackIds}
            onToggleSkillPack={toggleSkillPack}
            selectedSkillPacks={selectedSkillPacks}
            showConfirm={showConfirm}
            onOpenAiCreate={(field) => setAiCreateModalState({ mode: 'single', dimension: field })}
          />
        ) : isCharacterTab ? (
          <CharacterPanel
            bookId={bookId || ''}
            bible={bible}
            onBibleUpdate={setBible}
            bookTitle={book?.title || ''}
            chapters={chapters}
            hasChapters={chapters.length > 0}
            showConfirm={showConfirm}
            skillPacks={skillPacks}
            selectedSkillPackIds={selectedSkillPackIds}
            onToggleSkillPack={toggleSkillPack}
            selectedSkillPacks={selectedSkillPacks}
            onOpenAiCreate={() => setAiCreateModalState({ mode: 'single', dimension: 'character_profiles' })}
          />
        ) : isPlotTab ? (
          <PlotPanel
            bookId={bookId || ''}
            bible={bible}
            onBibleUpdate={setBible}
            bookTitle={book?.title || ''}
            totalVolumes={book?.total_volumes || 0}
            chapters={chapters}
            hasChapters={chapters.length > 0}
            showConfirm={showConfirm}
            skillPacks={skillPacks}
            selectedSkillPackIds={selectedSkillPackIds}
            onToggleSkillPack={toggleSkillPack}
            selectedSkillPacks={selectedSkillPacks}
            concept={concept}
            onRefreshChapters={() => api.listChapters(bookId || '').then(setChapters).catch(() => {})}
            onOpenAiCreate={() => setAiCreateModalState({ mode: 'single', dimension: 'timeline' })}
          />
        ) : isInventoryTab ? (
          <InventoryPanel
            bookId={bookId || ''}
            bible={bible}
            onBibleUpdate={setBible}
            bookTitle={book?.title || ''}
            chapters={chapters}
            hasChapters={chapters.length > 0}
            showConfirm={showConfirm}
            skillPacks={skillPacks}
            selectedSkillPackIds={selectedSkillPackIds}
            onToggleSkillPack={toggleSkillPack}
            selectedSkillPacks={selectedSkillPacks}
            onOpenAiCreate={() => setAiCreateModalState({ mode: 'single', dimension: 'inventory' })}
          />
        ) : (
          <BibleEditPanel
            tab={currentTab}
            bookTitle={book?.title || ''}
            content={currentContent}
            editing={editing}
            editValue={editValue}
            saving={saving}
            aiAssisting={aiAssisting}
            aiError={aiError}
            bibleAiMode={bibleAiMode}
            bibleAiPrompt={bibleAiPrompt}
            skillPacks={skillPacks}
            selectedSkillPackIds={selectedSkillPackIds}
            onToggleSkillPack={toggleSkillPack}
            selectedSkillPacks={selectedSkillPacks}
            hasChapters={chapters.length > 0}
            dimAnalyzing={dimAnalyzing}
            onAnalyzeDimension={() => handleAnalyzeDimension(activeTab)}
            onStartEdit={startEdit}
            onSaveEdit={saveEdit}
            onCancelEdit={() => setEditing(false)}
            onEditChange={setEditValue}
            onAIAssist={handleAIAssist}
            onExecuteAi={executeBibleAi}
            onCancelAi={() => { setBibleAiMode(false); setAiError(''); }}
            onEditAiPrompt={setBibleAiPrompt}
            onDelete={handleDeleteField}
            onOpenAiCreate={(field) => setAiCreateModalState({ mode: 'single', dimension: field })}
          />
        )}
      </div>

      {/* 单维度 AI 创作弹窗（总创作入口已统一到 AI 智驾，此处仅保留 single 模式） */}
      {aiCreateModalState && aiCreateModalState.mode === 'single' && bookId && (
        <AiCreateModal
          mode={aiCreateModalState.mode}
          dimension={aiCreateModalState.dimension}
          bookId={bookId}
          book={book}
          bible={bible}
          skillPacks={skillPacks}
          selectedSkillPackIds={selectedSkillPackIds}
          onApply={handleAiCreateApply}
          onApplyMany={handleAiCreateApplyMany}
          onClose={() => { setAiCreateModalState(null); setResumeSession(null); }}
          resumeSession={resumeSession}
          onSessionSaved={refreshAiSessions}
        />
      )}

      {/* 实体注册表弹窗（跨维度重命名/合并） */}
      {showEntityRegistry && bookId && (
        <EntityRegistryModal
          bookId={bookId}
          onClose={() => setShowEntityRegistry(false)}
          onRenamed={async () => {
            // 重命名后重新拉取 bible 与章节列表
            try {
              const bb = await api.getBible(bookId);
              if (bb) setBible(bb);
            } catch {}
          }}
        />
      )}
    </div>
  );
}

/* ===== 构思面板 ===== */
function ConceptPanel(props: {
  concept: string;
  setConcept: (v: string) => void;
  bible: BookBible | null;
  brainstorming: boolean;
  brainstormResult: BrainstormResult | null;
  brainstormError: string;
  adoptedSuggestions: Set<string>;
  onBrainstorm: () => void;
  onAdopt: (dim: string, s: BrainstormSuggestion) => void;
  bookId: string;
  bookTitle: string;
  onBibleUpdate: (b: BookBible) => void;
  hasChapters: boolean;
  conceptAiMode: boolean;
  conceptAiPrompt: string;
  conceptAiAssisting: boolean;
  conceptAiError: string;
  onStartConceptAi: () => void;
  onExecuteConceptAi: () => void;
  onCancelConceptAi: () => void;
  onEditConceptAiPrompt: (v: string) => void;
  onAnalyzeDimension: () => void;
  dimAnalyzing: boolean;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
  onOpenAiCreate: () => void;
  // AI总创作会话历史（需求3）
  aiSessions: AISession[];
  onRefreshSessions: () => void;
  onDeleteAiSessions: (ids: string[]) => void;
  onRenameAiSession: (id: string, title: string) => void;
  onResumeAiSession: (session: AISession) => void;
}) {
  const { brainstorming, brainstormResult, adoptedSuggestions, onAdopt,
    conceptAiMode, conceptAiPrompt, conceptAiAssisting, conceptAiError,
    onExecuteConceptAi, onCancelConceptAi, onEditConceptAiPrompt,
    skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks,
    bookId, aiSessions, onRefreshSessions, onDeleteAiSessions, onRenameAiSession, onResumeAiSession } = props;
  const openChatPanel = useStore((s: any) => s.openChatPanel) as (bid: string, sessionId?: string | null) => void;

  const [skillExpanded, setSkillExpanded] = useState(false);
  const selectedCount = selectedSkillPackIds.length;

  // 聊天记录管理状态（需求3）
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [renamingId, setRenamingId] = useState<string>('');
  const [renameValue, setRenameValue] = useState('');
  const [historyExpanded, setHistoryExpanded] = useState(true);

  const toggleSelect = (id: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const toggleSelectAll = () => {
    setSelectedIds(prev => prev.size === aiSessions.length ? new Set() : new Set(aiSessions.map(s => s.id)));
  };
  const startRename = (id: string, currentTitle: string) => {
    setRenamingId(id); setRenameValue(currentTitle);
  };
  const submitRename = () => {
    if (renamingId && renameValue.trim()) onRenameAiSession(renamingId, renameValue);
    setRenamingId(''); setRenameValue('');
  };
  const handleBatchDelete = () => {
    if (selectedIds.size === 0) return;
    onDeleteAiSessions(Array.from(selectedIds));
    setSelectedIds(new Set());
  };

  // 提取会话预览（最后一条助手消息的首行）
  const getPreview = (s: AISession): string => {
    const msgs = s.messages || [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'assistant') {
        try {
          const payload = JSON.parse(msgs[i].content);
          if (payload && payload.outputs) {
            const firstOut = Object.values(payload.outputs)[0] as string | undefined;
            if (firstOut) return firstOut.replace(/\s+/g, ' ').slice(0, 60);
          }
        } catch {}
        return msgs[i].content.replace(/\s+/g, ' ').slice(0, 60);
      }
    }
    const firstUser = msgs.find(m => m.role === 'user');
    return firstUser ? firstUser.content.replace(/\s+/g, ' ').slice(0, 60) : '（空对话）';
  };

  const formatTime = (iso: string | null): string => {
    if (!iso) return '';
    const d = new Date(iso);
    const now = new Date();
    const diff = now.getTime() - d.getTime();
    if (diff < 60000) return '刚刚';
    if (diff < 3600000) return `${Math.floor(diff / 60000)}分钟前`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}小时前`;
    if (diff < 604800000) return `${Math.floor(diff / 86400000)}天前`;
    return `${d.getMonth() + 1}/${d.getDate()}`;
  };

  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!conceptAiAssisting && conceptAiPrompt.trim()) {
        onExecuteConceptAi();
      }
    }
  };

  // AI协同创作模式
  if (conceptAiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <button className="btn-ghost-sm" onClick={onCancelConceptAi} disabled={conceptAiAssisting}>取消</button>
        </div>
        {/* 技能包多选器（可折叠） */}
        {skillPacks.length > 0 && (
          <div className="skill-pack-collapsible">
            <button className="skill-pack-toggle" onClick={() => setSkillExpanded(v => !v)} disabled={conceptAiAssisting}>
              <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
              <span>📦 协同技能包</span>
              {selectedCount > 0 && <span className="skill-pack-toggle-badge">{selectedCount}</span>}
              <span className="skill-pack-toggle-hint">{skillExpanded ? '收起' : '展开'}</span>
            </button>
            {skillExpanded && (
              <>
                <SkillPackGroupedList
                  skillPacks={skillPacks}
                  selectedSkillPackIds={selectedSkillPackIds}
                  onToggleSkillPack={onToggleSkillPack}
                  disabled={conceptAiAssisting}
                />
                {selectedSkillPacks.length > 0 && (
                  <div className="skill-pack-info-list">
                    {selectedSkillPacks.map(pack => (
                      <div key={pack.id} className="skill-pack-info">
                        <span className="skill-pack-info-icon">{pack.icon}</span>
                        <div>
                          <div className="skill-pack-info-name">{pack.name}</div>
                          <div className="skill-pack-info-desc">{pack.description}</div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        )}
        <p className="text-muted" style={{marginBottom:8}}>告诉AI你想生成什么构思内容，AI会结合故事设定和已勾选的技能包来创作</p>
        <div className="ai-prompt-section ai-prompt-vertical">
          <textarea
            className="input bible-ai-prompt-input"
            rows={6}
            value={conceptAiPrompt}
            onChange={e => onEditConceptAiPrompt(e.target.value)}
            onKeyDown={handlePromptKeyDown}
            placeholder="例如：扩展当前构思，增加核心卖点、目标读者、主线冲突、独特亮点..."
            disabled={conceptAiAssisting}
            autoFocus
          />
          <div className="ai-prompt-bottom-row">
            <button className="btn-primary ai-prompt-submit" onClick={onExecuteConceptAi} disabled={conceptAiAssisting || !conceptAiPrompt.trim()}>
              {conceptAiAssisting ? '⏳ AI创作中...' : '🚀 发送'}
            </button>
          </div>
        </div>
        {conceptAiError && <div className="error-msg" style={{marginTop:8}}>{conceptAiError}</div>}
        {conceptAiAssisting && (
          <div className="bible-ai-loading">
            <div className="loading-spinner" />
            <p>AI正在结合{selectedSkillPacks.length > 0 ? selectedSkillPacks.map(p => p.name).join('、') : '设定'}生成构思内容...</p>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="concept-panel">
      {/* 构思维度：AI 智驾入口（四Tab：设定/正文/去AI/校审） */}
      <div className="concept-input-section" style={{ alignItems: 'center', justifyContent: 'center', flex: 0 }}>
        <button
          className="btn-primary concept-ai-create-cta"
          onClick={() => bookId && openChatPanel(bookId)}
          title="打开 AI 智驾：设定/正文/去AI/校审 四Tab协作创作"
          style={{ background: 'linear-gradient(135deg,#7cb89e 0%,#5ba3a8 100%)' }}
        >
          <div className="cta-main-row">
            <CarLogo size={90} />
            <span className="cta-title">
              <span className="cta-ai">Ai</span>
              <span>智</span>
              <span>驾</span>
            </span>
            <span className="cta-sub-text"><span className="cta-ai">Ai</span>领航，人机共创</span>
          </div>
        </button>
      </div>

      {brainstormResult && (
        <div className="brainstorm-results">
          {brainstormResult.concept_analysis && (
            <div className="concept-analysis">
              <h4>📋 构思分析</h4>
              <p>{brainstormResult.concept_analysis}</p>
            </div>
          )}
          {Object.entries(brainstormResult.suggestions).map(([dim, suggestions]) => (
            <div key={dim} className="suggestion-group">
              <h4>{DIMENSION_LABELS[dim] || dim} <span className="suggestion-count">{suggestions.length}个方案</span></h4>
              <div className="suggestion-cards">
                {suggestions.map((s, i) => {
                  const adopted = adoptedSuggestions.has(`${dim}-${s.title}`);
                  return (
                    <div key={i} className={`suggestion-card ${adopted ? 'adopted' : ''}`}>
                      <div className="suggestion-card-title">{s.title}</div>
                      <div className="suggestion-card-desc">{s.description}</div>
                      <button
                        className={`btn-sm ${adopted ? 'btn-ghost-sm' : 'btn-primary-sm'}`}
                        onClick={() => onAdopt(dim, s)}
                        disabled={adopted}
                      >
                        {adopted ? '✓ 已采纳' : '采纳'}
                      </button>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}

      {brainstorming && (
        <div className="brainstorm-loading">
          <div className="loading-spinner" />
          <p>AI正在为你生成多维度创作方案...</p>
        </div>
      )}

      {/* 历史对话（不限数量，支持批量删除/编辑/重命名/继续对话） */}
      <div className="ai-chat-history" style={{ marginTop: 0 }}>
        <div className="ai-history-bar" style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8, flexWrap: 'nowrap', overflowX: 'auto' }}>
          <button
            className="btn-ghost-sm"
            onClick={() => setHistoryExpanded(v => !v)}
            style={{ padding: '2px 8px', fontSize: 12, fontWeight: 600, flexShrink: 0 }}
            title={historyExpanded ? '收起' : '展开'}
          >
            {historyExpanded ? '▼' : '▶'} 💬 历史对话 {aiSessions.length > 0 && `(${aiSessions.length})`}
          </button>
          <div style={{ display: 'flex', gap: 6, marginLeft: 'auto', flexShrink: 0 }}>
            {aiSessions.length > 0 && (
              <>
                <button
                  className="btn-ghost-sm"
                  onClick={toggleSelectAll}
                  style={{ padding: '2px 8px', fontSize: 11 }}
                  title="全选/取消全选"
                >
                  {selectedIds.size === aiSessions.length ? '取消全选' : '全选'}
                </button>
                <button
                  className="btn-ghost-sm"
                  onClick={handleBatchDelete}
                  disabled={selectedIds.size === 0}
                  style={{ padding: '2px 8px', fontSize: 11, color: selectedIds.size === 0 ? undefined : '#e74c3c' }}
                  title={selectedIds.size === 0 ? '先选中要删除的对话' : `删除选中的 ${selectedIds.size} 条对话`}
                >
                  🗑️ 删除选中 {selectedIds.size > 0 && `(${selectedIds.size})`}
                </button>
              </>
            )}
            <button
              className="btn-ghost-sm"
              onClick={onRefreshSessions}
              style={{ padding: '2px 8px', fontSize: 11 }}
              title="刷新列表"
            >
              🔄
            </button>
          </div>
        </div>

        {historyExpanded && (
          aiSessions.length === 0 ? (
            <div style={{ padding: '16px 12px', textAlign: 'center', fontSize: 12, color: 'var(--text-muted)', background: 'var(--bg-tertiary)', borderRadius: 8, border: '1px dashed var(--border-color)' }}>
              暂无聊天记录。点击上方「AI 智驾」开始第一次创作，生成的内容会自动保存到这里。
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 360, overflowY: 'auto' }}>
              {aiSessions.map(s => {
                const checked = selectedIds.has(s.id);
                const isRenaming = renamingId === s.id;
                return (
                  <div
                    key={s.id}
                    className="chat-history-item"
                    style={{
                      display: 'flex', flexDirection: 'column', gap: 6, padding: '8px 10px',
                      background: 'var(--bg-secondary)', borderRadius: 8,
                      border: `1px solid ${checked ? 'var(--accent)' : 'var(--border-color)'}`,
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleSelect(s.id)}
                      style={{ marginTop: 3, flexShrink: 0 }}
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      {isRenaming ? (
                        <input
                          className="input"
                          value={renameValue}
                          onChange={e => setRenameValue(e.target.value)}
                          onBlur={submitRename}
                          onKeyDown={e => { if (e.key === 'Enter') submitRename(); if (e.key === 'Escape') { setRenamingId(''); setRenameValue(''); } }}
                          autoFocus
                          style={{ fontSize: 13, padding: '4px 8px', width: '100%' }}
                        />
                      ) : (
                        <div
                          style={{ fontSize: 13, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', cursor: 'pointer' }}
                          onClick={() => onResumeAiSession(s)}
                          title="点击继续对话"
                        >
                          {s.title || '未命名对话'}
                        </div>
                      )}
                      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {getPreview(s)}
                      </div>
                      <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>
                        {formatTime(s.updated_at || s.created_at)} · {(s.messages || []).length} 条消息
                      </div>
                    </div>
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'row', gap: 6, justifyContent: 'flex-end', flexShrink: 0, paddingTop: 2, borderTop: '1px dashed var(--border-color)' }}>
                      <button
                        className="btn-ghost-sm"
                        onClick={() => onResumeAiSession(s)}
                        style={{ padding: '3px 10px', fontSize: 11 }}
                        title="打开并继续与 AI 对话，可提修改意见"
                      >
                        💬 继续
                      </button>
                      <button
                        className="btn-ghost-sm"
                        onClick={() => startRename(s.id, s.title || '未命名对话')}
                        disabled={isRenaming}
                        style={{ padding: '3px 10px', fontSize: 11 }}
                        title="重命名"
                      >
                        ✏️ 重命名
                      </button>
                      <button
                        className="btn-ghost-sm"
                        onClick={() => onDeleteAiSessions([s.id])}
                        style={{ padding: '3px 10px', fontSize: 11, color: '#e74c3c' }}
                        title="删除"
                      >
                        🗑️ 删除
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )
        )}
      </div>
    </div>
  );
}

/* ===== 章节管理面板 ===== */
// 单个卷分组子组件：用 memo 包裹，折叠/展开某个卷时其他卷不会重渲染
interface VolumeGroupProps {
  volId: string;
  volTitle: string;
  volChs: Chapter[];
  expanded: boolean;
  renaming: boolean;
  renameValue: string;
  onToggle: (volId: string) => void;
  onSelectChapter: (id: string) => void;
  onCreateChapter: (volId: string) => void;
  onDeleteVolume: (volId: string) => void;
  onStartRename: (volId: string, currentTitle: string) => void;
  onRenameSubmit: (volId: string, newTitle: string) => void;
  onCancelRename: () => void;
  onRenameChange: (v: string) => void;
}
const VolumeGroup = memo(function VolumeGroup({
  volId, volTitle, volChs, expanded, renaming, renameValue,
  onToggle, onSelectChapter, onCreateChapter, onDeleteVolume,
  onStartRename, onRenameSubmit, onCancelRename, onRenameChange,
}: VolumeGroupProps) {
  return (
    <div className="chapter-volume-group">
      <div className="chapter-volume-header" onClick={() => !renaming && onToggle(volId)}>
        <span className="chapter-volume-arrow">
          {expanded ? '▼' : '▶'}
        </span>
        {renaming ? (
          <input
            className="input chapter-volume-rename-input"
            value={renameValue}
            onChange={e => onRenameChange(e.target.value)}
            onBlur={() => {
              if (renameValue.trim()) onRenameSubmit(volId, renameValue.trim());
              else onCancelRename();
            }}
            onKeyDown={e => { if (e.key === 'Enter') { (e.target as HTMLInputElement).blur(); } if (e.key === 'Escape') onCancelRename(); }}
            autoFocus
            onClick={e => e.stopPropagation()}
          />
        ) : (
          <span className="chapter-volume-title">📁 {volTitle}</span>
        )}
        <span className="chapter-volume-count">{volChs.length}章</span>
        <button className="btn-ghost-sm chapter-volume-add" onClick={e => { e.stopPropagation(); onStartRename(volId, volTitle); }} title="重命名">✏️</button>
        <button className="btn-ghost-sm chapter-volume-add" onClick={e => { e.stopPropagation(); onCreateChapter(volId); }} title="在此卷下添加章节">+</button>
        <button className="btn-ghost-sm chapter-volume-add" onClick={e => { e.stopPropagation(); onDeleteVolume(volId); }} title="删除此卷" style={{color:'#e74c3c'}}>🗑️</button>
      </div>
      {expanded && (
        <div className="chapter-volume-children">
          {volChs.length === 0 ? (
            <div className="chapter-volume-empty">暂无章节，点击 + 添加</div>
          ) : volChs.map((ch, i) => (
            <div key={ch.id} className="chapter-list-item" onClick={() => onSelectChapter(ch.id)}>
              <div className="chapter-list-index">{i + 1}</div>
              <div className="chapter-list-info">
                <div className="chapter-list-title">{ch.title}</div>
                <div className="chapter-list-meta">{ch.word_count} 字</div>
              </div>
              <div className="chapter-list-arrow">›</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
});

function ChapterPanel(props: {
  chapters: Chapter[];
  activeChapter: Chapter | null;
  chapterEditing: boolean;
  chapterEditTitle: string;
  chapterEditContent: string;
  chapterSaving: boolean;
  aiCreateMode: 'write' | 'continue' | 'polish' | null;
  aiGeneratedContent: string;
  aiCreating: boolean;
  savingChapter: boolean;
  aiStreamError: string;
  aiUserPrompt: string;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
  onSelectChapter: (id: string) => void;
  onCreateChapter: (parentId?: string) => void;
  onCreateVolume: (name?: string, chapterIds?: string[]) => Promise<any>;
  onSaveChapter: () => void;
  onDeleteChapter: (id: string) => void;
  onCancelEdit: () => void;
  onStartEdit: () => void;
  onEditTitle: (v: string) => void;
  onEditContent: (v: string) => void;
  onBackToList: () => void;
  onStartAiCreate: (mode: 'write' | 'continue' | 'polish') => void;
  onExecuteAiCreate: () => void;
  onConfirmAiContent: () => void;
  onCancelAiCreate: () => void;
  onStopAiCreate: () => void;
  onEditAiPrompt: (v: string) => void;
  onRenameVolume: (volId: string, newTitle: string) => Promise<void>;
  onDeleteVolume: (volId: string) => Promise<void>;
  bookId?: string;
  // P0-1: 多Agent协同开关与元信息
  useAgentPipeline?: boolean;
  onToggleAgentPipeline?: (v: boolean) => void;
  agentMeta?: any;
  // P2-9：Spot-Fix 修订
  spotFixing?: boolean;
  spotFixMsg?: string;
  onSpotFix?: (content: string, postValidate: any) => Promise<void>;
  // 聊天式AI创作：历史记录与目标章节
  aiChatHistory: Array<{ role: 'user' | 'assistant'; content: string; chapterTitle?: string; type?: 'content' | 'status'; collapsed?: boolean }>;
  onClearAiChatHistory: () => void;
  onToggleChatMsgCollapse: (index: number) => void;
  onRefreshChapterAnchor: () => void;
  onRegenerateAiContent: () => void;
  aiTargetChapterId: string | null;
  // 本章语言风格（行文文风，最多3个叠加）
  chapterLangStyles?: string[];
  onToggleChapterLangStyle?: (key: string) => void;
  // 连续创作模式
  batchCount?: number;
  onBatchCountChange?: (v: number) => void;
  batchCreating?: boolean;
  batchProgress?: { cur: number; total: number; done: number; message?: string };
  onBatchCreate?: () => void;
}) {
  const { chapters, activeChapter, chapterEditing, chapterEditTitle, chapterEditContent, chapterSaving,
    aiCreateMode, aiGeneratedContent, aiCreating, savingChapter, aiStreamError, aiUserPrompt,
    skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks,
    onSelectChapter, onCreateChapter, onCreateVolume, onSaveChapter, onDeleteChapter, onCancelEdit, onStartEdit,
    onEditTitle, onEditContent, onBackToList, onExecuteAiCreate, onConfirmAiContent, onCancelAiCreate, onStopAiCreate, onEditAiPrompt,
    onRenameVolume, onDeleteVolume, bookId,
    useAgentPipeline: useAgent, onToggleAgentPipeline, agentMeta,
    spotFixing, spotFixMsg, onSpotFix,
    aiChatHistory, onClearAiChatHistory, onToggleChatMsgCollapse, onRefreshChapterAnchor, onRegenerateAiContent, aiTargetChapterId,
    chapterLangStyles: langStyles, onToggleChapterLangStyle,
    batchCount, onBatchCountChange, batchCreating, batchProgress, onBatchCreate,
  } = props;
  const openChatPanel = useStore((s: any) => s.openChatPanel) as (bid: string, sessionId?: string | null) => void;

  const [skillExpanded, setSkillExpanded] = useState(false);
  const [langStyleExpanded, setLangStyleExpanded] = useState(false);
  const [expandedVolumes, setExpandedVolumes] = useState<Record<string, boolean>>({});
  const [renamingVolId, setRenamingVolId] = useState<string | null>(null);
  const [renameVolTitle, setRenameVolTitle] = useState('');
  const selectedCount = selectedSkillPackIds.length;
  // 手机端优化：配置抽屉开关、流式全屏阅读开关、agentMeta 折叠态
  const [configDrawerOpen, setConfigDrawerOpen] = useState(false);
  const [streamFullscreen, setStreamFullscreen] = useState(false);
  const [agentMetaExpanded, setAgentMetaExpanded] = useState(false);
  // 正文查看全屏模式（点击"展开全文"时弹出，电脑端+手机端统一生效）
  const [viewingContent, setViewingContent] = useState<{ content: string; title: string } | null>(null);
  // 手机端检测（SSR 安全：window 存在时检测一次，监听 resize 更新）
  const [isMobile, setIsMobile] = useState(() => typeof window !== 'undefined' && window.innerWidth < 600);
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onResize = () => setIsMobile(window.innerWidth < 600);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  // 流式生成时自动开启全屏阅读（手机端+电脑端统一生效）
  useEffect(() => {
    if (aiCreating && aiGeneratedContent.trim()) {
      setStreamFullscreen(true);
    } else if (!aiCreating) {
      setStreamFullscreen(false);
    }
  }, [aiCreating, aiGeneratedContent]);

  // 稳定回调（useCallback）：避免每次 ChapterPanel 重渲染时生成新函数引用，
  // 配合 VolumeGroup 的 memo，使折叠/展开某个卷时其他卷不重渲染。
  const toggleVolume = useCallback((volId: string) => {
    setExpandedVolumes(prev => ({ ...prev, [volId]: prev[volId] === false }));
  }, []);
  const startRenameVolume = useCallback((volId: string, currentTitle: string) => {
    setRenamingVolId(volId);
    setRenameVolTitle(currentTitle);
  }, []);
  const cancelRenameVolume = useCallback(() => {
    setRenamingVolId(null);
  }, []);
  const submitRenameVolume = useCallback(async (volId: string, newTitle: string) => {
    if (newTitle.trim()) {
      try { await onRenameVolume(volId, newTitle.trim()); } catch { /* 忽略，保持编辑态 */ }
    }
    setRenamingVolId(null);
  }, [onRenameVolume]);
  const createChapterInVolume = useCallback((volId: string) => {
    onCreateChapter(volId);
  }, [onCreateChapter]);
  const removeVolume = useCallback((volId: string) => {
    onDeleteVolume(volId);
  }, [onDeleteVolume]);

  // 追加导入章节（已有作品继续添加章节，尤其适合导入的小说继续更新）
  const importChaptersRef = useRef<HTMLInputElement>(null);
  const [importingChapters, setImportingChapters] = useState(false);
  const [importChaptersError, setImportChaptersError] = useState('');

  async function handleImportChapters(e: React.ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(e.target.files || []);
    // 清空 input 以便重复选择同一文件
    if (importChaptersRef.current) importChaptersRef.current.value = '';
    if (picked.length === 0) return;
    if (!bookId) return;
    const valid = picked.filter(f => /\.(txt|md|markdown|docx|zip|json)$/i.test(f.name));
    if (valid.length === 0) {
      setImportChaptersError('请选择 txt/md/docx/zip 格式的文件');
      return;
    }
    setImportChaptersError('');
    setImportingChapters(true);
    try {
      const result = await api.importChapters(bookId, valid);
      alert(`成功追加 ${result.added} 章，当前共 ${result.total} 章`);
      // 刷新页面以重新加载章节列表
      window.location.reload();
    } catch (err: any) {
      setImportChaptersError(err.message || '导入失败');
      alert('追加导入失败: ' + (err.message || '未知错误'));
    } finally {
      setImportingChapters(false);
    }
  }

  // 导入作品后，按文件名/章节标题AI自动识别填入各空维度
  const [aiImportRecognizing, setAiImportRecognizing] = useState(false);
  async function handleAiImportRecognize() {
    if (!bookId) return;
    const confirmFill = confirm(
      `将根据导入作品的【文件名/章节标题】+【内容样本】，AI自动识别并填充空的创作维度。\n\n仅填充空维度，不会覆盖已有内容。是否继续？`
    );
    if (!confirmFill) return;
    setAiImportRecognizing(true);
    try {
      // dimensions 传空数组，后端自动识别空维度并填充
      const result = await api.aiImportRecognize(bookId, [], selectedSkillPackIds);
      alert(result.message || '识别完成');
      // 刷新页面以重新加载维度数据
      window.location.reload();
    } catch (err: any) {
      alert('AI识别填充失败：' + (err.message || '请检查AI配置或网络'));
    } finally {
      setAiImportRecognizing(false);
    }
  }

  // 重新分卷：按50章/卷自动重新归入（清空现有卷结构后重建）
  const [rebinning, setRebinning] = useState(false);

  // 分离卷和章节（useMemo 必须在所有 early return 之前调用，否则违反 Rules of Hooks）
  const volumes = useMemo(() => chapters.filter(c => c.is_volume), [chapters]);
  // 按卷分组的章节（缓存，避免每次渲染都 filter）
  const chaptersByVolume = useMemo(() => {
    const map: Record<string, Chapter[]> = {};
    for (const v of volumes) map[v.id] = [];
    const orphans: Chapter[] = [];
    for (const c of chapters) {
      if (c.is_volume) continue;
      const pid = c.parent_id;
      if (pid && map[pid]) {
        map[pid].push(c);
      } else {
        orphans.push(c); // 无 parent_id 或指向已不存在的卷（孤儿章节）
      }
    }
    map['__orphan__'] = orphans;
    return map;
  }, [chapters, volumes]);

  async function handleRebinVolumes() {
    if (!bookId) return;
    const ok = confirm('将按 50 章/卷自动重新分卷：\n\n1. 清空所有章节的卷归属\n2. 删除现有卷\n3. 按章节号排序后每 50 章归入一卷\n\n此操作不可撤销，是否继续？');
    if (!ok) return;
    setRebinning(true);
    try {
      const result = await api.rebinVolumes(bookId);
      alert(`重新分卷完成：共 ${result.chapters} 章，分为 ${result.volumes} 卷`);
      // 刷新页面以重新加载章节列表
      window.location.reload();
    } catch (err: any) {
      alert('重新分卷失败：' + (err.message || '未知错误'));
    } finally {
      setRebinning(false);
    }
  }

  // Enter快捷发送（Shift+Enter换行）
  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!aiCreating && aiUserPrompt.trim()) {
        onExecuteAiCreate();
      }
    }
  };

  // AI创作面板（聊天式，历史记录保留）
  if (aiCreateMode) {
    const hasResult = aiGeneratedContent.trim().length > 0;
    const streaming = aiCreating && hasResult;

    // P2-5：流式生成全屏阅读模式（电脑端+手机端统一生效，覆盖整个面板）
    if (streamFullscreen && streaming) {
      const streamParas = aiGeneratedContent.split(/\n+/).filter(p => p.trim());
      return (
        <div className="ai-fullscreen-stream">
          <div className="ai-fullscreen-stream-header">
            <div className="ai-fullscreen-stream-title">
              <span className="loading-dot" /> 正在生成{chapterEditTitle ? `·${chapterEditTitle}` : ''}...
            </div>
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{aiGeneratedContent.length}字</span>
          </div>
          <div className="ai-fullscreen-stream-body" ref={el => { if (el) el.scrollTop = el.scrollHeight; }}>
            {streamParas.map((para, pi) => (<p key={pi}>{para.trim()}</p>))}
          </div>
          <div className="ai-fullscreen-stream-footer">
            <button
              className="ai-fullscreen-stop-btn"
              onClick={() => { onStopAiCreate(); setStreamFullscreen(false); }}
            >⏹ 停止生成</button>
          </div>
        </div>
      );
    }

    // 正文查看全屏模式（点击"展开全文"弹出，电脑端+手机端统一生效）
    if (viewingContent) {
      const viewParas = viewingContent.content.split(/\n+/).filter(p => p.trim());
      return (
        <div className="ai-fullscreen-stream ai-fullscreen-view">
          <div className="ai-fullscreen-stream-header">
            <div className="ai-fullscreen-stream-title">
              📖 {viewingContent.title || '正文阅读'}
            </div>
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{viewingContent.content.length}字</span>
          </div>
          <div className="ai-fullscreen-stream-body">
            {viewParas.map((para, pi) => (<p key={pi}>{para.trim()}</p>))}
          </div>
          <div className="ai-fullscreen-stream-footer">
            <button
              className="ai-fullscreen-stop-btn"
              onClick={() => setViewingContent(null)}
            >✕ 关闭阅读</button>
          </div>
        </div>
      );
    }

    return (
      <div className="ai-create-panel ai-chat-panel">
        {/* P2-6：连续创作进度顶部 toast 浮层（不占正文区空间） */}
        {batchCreating && batchProgress && batchProgress.total > 0 && (
          <div className="ai-batch-toast">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontWeight: 600, color: 'var(--accent)' }}>📚 连续创作 {batchProgress.done}/{batchProgress.total}</span>
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{Math.round((batchProgress.done / batchProgress.total) * 100)}%</span>
            </div>
            <div className="ai-batch-toast-progress">
              <div className="ai-batch-toast-progress-bar" style={{ width: `${(batchProgress.done / batchProgress.total) * 100}%` }} />
            </div>
            {batchProgress.message && <div className="ai-batch-toast-msg">{batchProgress.message}</div>}
          </div>
        )}
        {/* 顶部：标题栏 */}
        <div className="ai-create-header">
          <div className="ai-create-header-left">
            <button className="btn-ghost-sm" onClick={onCancelAiCreate} disabled={aiCreating}>← 返回</button>
            <span className="ai-chat-target" title="AI当前锚定的章节（点击刷新重新识别进度）">
              📍 {chapterEditTitle}
              <button
                className="ai-anchor-refresh"
                onClick={onRefreshChapterAnchor}
                disabled={aiCreating}
                title="重新识别当前章节数，刷新定位到待写章"
              >🔄</button>
            </span>
            {aiCreating && <span className="ai-create-status">生成中...</span>}
          </div>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            {aiChatHistory.length > 0 && (
              <button className="btn-ghost-sm" onClick={onClearAiChatHistory} disabled={aiCreating} title="清空全部聊天记录">
                ️ 清空记录
              </button>
            )}
          </div>
        </div>

        {/* 中间：聊天历史区（可滚动） */}
        <div className="ai-chat-history">
          {aiChatHistory.length === 0 && !aiCreating && (
            <div className="ai-create-empty">
              <span className="ai-create-empty-icon">✨</span>
              <p>告诉AI你想写什么，AI将根据你的要求和故事设定创作章节正文</p>
              <p className="text-muted">自动识别当前写到哪一章，历史记录会一直保留，可连续创作多章</p>
            </div>
          )}

          {aiChatHistory.map((msg, i) => {
            // 章节正文消息：支持折叠/展开（已确认章节自动折叠，为下一章留空间）
            const isContent = msg.type === 'content';
            const paras = msg.content.split(/\n+/).filter(p => p.trim());
            const isLong = paras.length > 3 || msg.content.length > 200;
            const collapsed = isContent && isLong && msg.collapsed !== false;
            const showToggle = isContent && isLong;
            return (
            <div key={i} className={`ai-chat-msg ai-chat-msg-${msg.role}`}>
              <div className="ai-chat-msg-avatar">{msg.role === 'user' ? '👤' : '🤖'}</div>
              <div className="ai-chat-msg-body">
                {msg.chapterTitle && <div className="ai-chat-msg-chapter">📍 {msg.chapterTitle}</div>}
                {/* 折叠时隐藏正文，只保留章名+全屏查看按钮；展开时原位显示全文 */}
                {!collapsed && (
                  <div className={`ai-chat-msg-content${showToggle ? ' ai-chat-msg-expanded' : ''}`}>
                    {paras.map((para, pi) => (
                      <p key={pi}>{para.trim()}</p>
                    ))}
                  </div>
                )}
                {showToggle && (
                  <button
                    className="ai-chat-msg-toggle"
                    onClick={() => {
                      if (collapsed) {
                        // 折叠态：点击弹出全屏阅读模式
                        setViewingContent({ content: msg.content, title: msg.chapterTitle || '' });
                      } else {
                        // 展开态：点击收起
                        onToggleChatMsgCollapse(i);
                      }
                    }}
                    title={collapsed ? '全屏查看正文' : '收起正文'}
                  >
                    {collapsed ? `📖 全屏阅读（${msg.content.length}字）` : '收起'}
                  </button>
                )}
              </div>
            </div>
            );
          })}

          {/* 流式生成中的助手消息 */}
          {streaming && (
            <div className="ai-chat-msg ai-chat-msg-assistant ai-chat-msg-streaming">
              <div className="ai-chat-msg-avatar">🤖</div>
              <div className="ai-chat-msg-body">
                <div className="ai-chat-msg-content">
                  {aiGeneratedContent.split(/\n+/).filter(p => p.trim()).map((para, pi) => (
                    <p key={pi}>{para.trim()}</p>
                  ))}
                  <span className="ai-streaming-cursor"><span className="loading-dot" /></span>
                </div>
              </div>
            </div>
          )}

          {/* 加载中（尚未产出内容） */}
          {aiCreating && !hasResult && (
            <div className="ai-create-loading">
              <div className="loading-spinner" />
              <p>AI正在结合{selectedSkillPacks.length > 0 ? selectedSkillPacks.map(p => p.name).join('、') : '设定'}创作中...</p>
            </div>
          )}

          {aiStreamError && <div className="error-msg" style={{ marginTop: 8 }}>{aiStreamError}</div>}

          {/* P0-1: 多Agent协同管线元信息展示（生成结果） */}
          {/* P1-4：默认折叠，只显示一行摘要，点击展开详情。手机端省 200-300px 空间 */}
          {onToggleAgentPipeline && aiCreateMode === 'write' && agentMeta && (
            <details
              open={agentMetaExpanded}
              onToggle={e => setAgentMetaExpanded((e.target as HTMLDetailsElement).open)}
              style={{ marginTop: 8, padding: '6px 10px', background: 'var(--bg-tertiary)', borderRadius: 8, fontSize: 12 }}
            >
              <summary style={{ cursor: 'pointer', fontWeight: 600, color: 'var(--text-secondary)', lineHeight: 1.7, listStyle: 'none' }}>
                <span style={{ fontSize: 10, marginRight: 4 }}>{agentMetaExpanded ? '▼' : '▶'}</span>
                📍 第{agentMeta.current_chapter_num}章 · {agentMeta.vol_title || `第${agentMeta.vol_index}卷`}
                {agentMeta.deai_status === 'success' && <span style={{ color: '#27ae60' }}> · ✅去AI味</span>}
                {agentMeta.deai_status === 'failed' && <span style={{ color: '#e67e22' }} title={agentMeta.review_notes}> · ⚠️去AI味失败</span>}
                {agentMeta.consistency_passed === false && <span style={{ color: '#e74c3c' }} title={agentMeta.consistency_issues}> · ❌一致性异常</span>}
                {agentMeta.consistency_passed === true && <span style={{ color: '#27ae60' }}> · ✅一致性</span>}
                {agentMeta.post_validate && agentMeta.post_validate.critical_count > 0 && (
                  <span style={{ color: '#e74c3c' }} title={`AI痕迹检测：${agentMeta.post_validate.critical_count}个严重问题`}>{' · ⚠️AI痕迹'}({agentMeta.post_validate.critical_count})</span>
                )}
                {agentMeta.changes_applied && agentMeta.changes_applied.applied && <span style={{ color: '#27ae60' }}> · 📝已回写</span>}
                {agentMeta.suggested_title && <span style={{ color: '#9b59b6' }}> · 🏷️{agentMeta.suggested_title}</span>}
              </summary>
              <div style={{ marginTop: 6 }}>
              {agentMeta.chapter_plan && (
                <div style={{ marginBottom: 4, padding: '6px 8px', background: 'var(--bg-secondary)', borderRadius: 4, borderLeft: '3px solid var(--accent)' }}>
                  <b>📋 章节计划：</b>{agentMeta.chapter_plan.slice(0, 200)}{agentMeta.chapter_plan.length > 200 ? '...' : ''}
                </div>
              )}
              <div style={{ fontSize: 11, color: 'var(--text-secondary)', lineHeight: 1.7 }}>
                📍 第{agentMeta.current_chapter_num}章 · {agentMeta.vol_title || `第${agentMeta.vol_index}卷`} · 温度{agentMeta.temperature}
                {agentMeta.deai_status === 'success' && <span style={{ color: '#27ae60' }}> · ✅去AI味成功</span>}
                {agentMeta.deai_status === 'failed' && <span style={{ color: '#e67e22' }} title={agentMeta.review_notes}> · ⚠️去AI味失败(用初稿)</span>}
                {agentMeta.deai_status === 'skipped' && <span className="text-muted"> · 未启用去AI味</span>}
                {agentMeta.consistency_passed === false && <span style={{ color: '#e74c3c' }} title={agentMeta.consistency_issues}> · ❌一致性异常</span>}
                {agentMeta.consistency_passed === true && <span style={{ color: '#27ae60' }}> · ✅一致性通过</span>}
                {agentMeta.post_validate && agentMeta.post_validate.critical_count > 0 && (
                  <span style={{ color: '#e74c3c' }} title={`AI痕迹检测：${agentMeta.post_validate.critical_count}个严重问题`}>
                    {' · ⚠️AI痕迹'}({agentMeta.post_validate.critical_count})
                  </span>
                )}
                {agentMeta.post_validate && agentMeta.post_validate.critical_count === 0 && agentMeta.post_validate.warning_count > 0 && (
                  <span style={{ color: '#f39c12' }} title={`检测到${agentMeta.post_validate.warning_count}个轻微问题`}>
                    {' · 🔍痕迹检测'}({agentMeta.post_validate.warning_count})
                  </span>
                )}
                {agentMeta.changes_applied && agentMeta.changes_applied.applied && (
                  <span style={{ color: '#27ae60' }} title={`状态回写：${(agentMeta.changes_applied.fields_updated||[]).join('、')}`}>
                    {' · 📝状态已回写'}
                  </span>
                )}
                {agentMeta.suggested_title && (
                  <span style={{ color: '#9b59b6' }} title={`AI自动生成标题：${agentMeta.suggested_title}`}>
                    {' · 🏷️标题已生成'}「{agentMeta.suggested_title}」
                  </span>
                )}
              </div>
              {/* 审校校验报告：折叠面板展示校验问题 */}
              {agentMeta.chapter_score && (() => {
                const sc = agentMeta.chapter_score;
                if (!sc.has_issues || !sc.issues || sc.issues.length === 0) {
                  return (
                    <div style={{ marginTop: 6, padding: '6px 8px', background: 'var(--bg-secondary)', borderRadius: 4, borderLeft: '3px solid #27ae60', fontSize: 12 }}>
                      ✅ 校验通过，未发现问题
                    </div>
                  );
                }
                return (
                  <details style={{ marginTop: 6, padding: '6px 8px', background: 'var(--bg-secondary)', borderRadius: 4, borderLeft: '3px solid #f39c12' }}>
                    <summary style={{ cursor: 'pointer', fontSize: 12, fontWeight: 600, color: '#f39c12' }}>
                      ⚠️ 发现 {sc.issues.length} 个校验问题（点击展开）
                    </summary>
                    <div style={{ marginTop: 6, fontSize: 11, lineHeight: 1.6 }}>
                      {sc.issues.map((iss: any, i: number) => (
                        <div key={i} style={{ color: iss.severity === 'critical' ? '#e74c3c' : '#f39c12', marginBottom: 4 }}>
                          {iss.severity === 'critical' ? '🔴' : ''} [{iss.type === 'ai_trace' ? 'AI痕迹' : '一致性'}] {iss.description}
                        </div>
                      ))}
                    </div>
                  </details>
                );
              })()}
              {/* P0-1：后写校验报告详情（可折叠） */}
              {agentMeta.post_validate && (agentMeta.post_validate.critical_count > 0 || agentMeta.post_validate.warning_count > 0) && (
                <div style={{ fontSize: 10, color: 'var(--text-secondary)', lineHeight: 1.6, marginTop: 4, padding: '4px 8px', background: 'var(--bg-secondary)', borderRadius: 4 }}>
                  {agentMeta.post_validate.issues.slice(0, 5).map((iss: any, i: number) => (
                    <div key={i} style={{ color: iss.severity === 'critical' ? '#e74c3c' : '#f39c12' }}>
                      {iss.severity === 'critical' ? '🔴' : '🟡'} [{iss.category}] {iss.pattern} — {iss.suggestion}
                    </div>
                  ))}
                  {agentMeta.post_validate.issues.length > 5 && <div>...还有 {agentMeta.post_validate.issues.length - 5} 条</div>}
                </div>
              )}
              {/* P2-9：一键 Spot-Fix 修订按钮 */}
              {agentMeta?.post_validate && (agentMeta.post_validate.critical_count > 0 || agentMeta.post_validate.warning_count > 0) && onSpotFix && (
                <div style={{ marginTop: 6 }}>
                  <button
                    className="btn-primary-sm"
                    disabled={!!spotFixing}
                    onClick={() => onSpotFix(aiGeneratedContent, agentMeta.post_validate)}
                  >
                    {spotFixing ? '⏳ 修订中...' : '🔧 一键Spot-Fix修订'}
                  </button>
                  {spotFixMsg && <span style={{ marginLeft: 8, fontSize: 11, color: 'var(--text-secondary)' }}>{spotFixMsg}</span>}
                </div>
              )}
              {/* P2-10：落地门禁结果展示 */}
              {agentMeta?.gate_result && !agentMeta.gate_result.passed && (
                <div style={{ fontSize: 10, color: 'var(--text-secondary)', lineHeight: 1.6, marginTop: 4, padding: '4px 8px', background: 'var(--bg-secondary)', borderRadius: 4 }}>
                  <div style={{ fontWeight: 600, color: '#e74c3c' }}>🚪 落地门禁告警（{agentMeta.gate_result.critical_count} critical / {agentMeta.gate_result.warning_count} warning）</div>
                  {agentMeta.gate_result.issues.map((iss: any, i: number) => (
                    <div key={i} style={{ color: iss.severity === 'critical' ? '#e74c3c' : '#f39c12' }}>
                      {iss.severity === 'critical' ? '🔴' : '🟡'} [{iss.gate}] {iss.message}
                    </div>
                  ))}
                </div>
              )}
            </div>
            </details>
          )}
        </div>

        {/* 底部：控制区（多Agent开关+本章语言风格+技能包+输入框，固定在底部） */}
        <div className="ai-create-control-bar">
          {/* P1-3：手机端配置抽屉触发按钮（4项配置收进抽屉，底部只留输入框） */}
          {isMobile && (
            <>
              <button
                className="ai-config-trigger"
                onClick={() => setConfigDrawerOpen(true)}
                disabled={aiCreating}
              >
                <span>⚙️</span>
                <span>创作配置</span>
                {(selectedCount + (langStyles?.length || 0) + (useAgent ? 1 : 0)) > 0 && (
                  <span className="ai-config-trigger-badge">{selectedCount + (langStyles?.length || 0) + (useAgent ? 1 : 0)}</span>
                )}
                <span className="ai-config-trigger-hint">点击展开</span>
              </button>
              {configDrawerOpen && (
                <div className="ai-config-drawer-backdrop open" onClick={() => setConfigDrawerOpen(false)} />
              )}
            </>
          )}
          {/* P1-3：4项配置 wrapper（桌面端 display:contents 透明，手机端 drawer-open 时变 fixed 抽屉） */}
          <div className={`ai-config-items-wrapper ${isMobile ? 'mobile' : ''} ${isMobile && configDrawerOpen ? 'drawer-open' : ''}`}>
            {isMobile && configDrawerOpen && (
              <div className="ai-config-drawer-header">
                <span className="ai-config-drawer-header-title">创作配置</span>
                <button className="ai-config-drawer-header-close" onClick={() => setConfigDrawerOpen(false)}>✕</button>
              </div>
            )}
          {/* P0-1: 多Agent协同管线开关（紧邻协同技能包，两行相邻） */}
          {onToggleAgentPipeline && aiCreateMode === 'write' && (
            <div className="agent-pipeline-toggle">
              <label>
                <input type="checkbox" checked={!!useAgent} onChange={e => onToggleAgentPipeline(e.target.checked)} disabled={aiCreating} />
                <span>🤖 多Agent协同管线</span>
                <span className="text-muted">（计划→正文→去AI味→一致性）</span>
              </label>
            </div>
          )}
          {/* 本章语言风格（行文文风，最多3个叠加）—— 指导AI本章行文基调（可折叠，手机端两行） */}
          {onToggleChapterLangStyle && (aiCreateMode === 'write' || aiCreateMode === 'continue') && (
            <div className="lang-style-collapsible" style={{ marginBottom: 6, background: 'var(--bg-tertiary)', borderRadius: 6, overflow: 'hidden' }}>
              <button
                className="lang-style-toggle"
                onClick={() => setLangStyleExpanded(v => !v)}
                disabled={aiCreating}
                style={{ display: 'flex', alignItems: 'center', gap: 6, width: '100%', padding: '6px 10px',
                  background: 'transparent', color: 'var(--text-secondary)', fontSize: 12, fontWeight: 500,
                  textAlign: 'left', border: 'none', cursor: aiCreating ? 'not-allowed' : 'pointer',
                  opacity: aiCreating ? 0.5 : 1 }}>
                <span style={{ fontSize: 10, flexShrink: 0, width: 12 }}>{langStyleExpanded ? '▼' : '▶'}</span>
                <span>🎨 本章语言风格</span>
                {(langStyles || []).length > 0 && (
                  <span style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                    minWidth: 20, height: 16, padding: '0 5px', borderRadius: 8,
                    background: 'var(--accent)', color: '#fff', fontSize: 10, fontWeight: 700, marginLeft: 2 }}>
                    {(langStyles || []).length}/3
                  </span>
                )}
                <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-muted)', fontWeight: 400 }}>
                  {langStyleExpanded ? '收起' : '展开'}
                </span>
              </button>
              {langStyleExpanded && (
                <div className="lang-style-buttons" style={{ padding: '4px 10px 8px', display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                  {Object.entries(CHAPTER_LANG_STYLES).map(([k, s]) => {
                    const sel = (langStyles || []).includes(k);
                    const disabled = !sel && (langStyles || []).length >= 3;
                    return (
                      <button key={k} type="button"
                        onClick={() => onToggleChapterLangStyle(k)}
                        disabled={aiCreating || disabled}
                        title={s.desc}
                        style={{ padding: '3px 9px', fontSize: 12, borderRadius: 12,
                          cursor: (aiCreating || disabled) ? 'not-allowed' : 'pointer',
                          border: `1px solid ${sel ? 'var(--accent)' : 'var(--border-color)'}`,
                          background: sel ? 'var(--accent)' : 'transparent',
                          color: sel ? '#fff' : 'var(--text-primary)',
                          opacity: (aiCreating || disabled) ? 0.4 : 1 }}>
                        {s.label}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          )}
          {/* 连续创作模式：批量生成 N 章（仅 write 模式，不依赖多Agent协同） */}
          {/* P2-6：进度条移到顶部 toast 浮层，不占配置区空间 */}
          {onBatchCreate && aiCreateMode === 'write' && (
            <div className="batch-create-row" style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6, padding: '6px 8px', background: 'var(--bg-tertiary)', borderRadius: 6, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>📚 连续创作</span>
              <input type="number" min={1} max={10} value={batchCount ?? 3}
                onChange={e => onBatchCountChange?.(Math.max(1, Math.min(10, Number(e.target.value) || 3)))}
                disabled={!!batchCreating} style={{ width: 56, padding: '2px 6px', fontSize: 12, borderRadius: 4, border: '1px solid var(--border-color)', background: 'var(--bg-secondary)', color: 'var(--text-primary)' }} />
              <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>章</span>
              <button className="btn-primary-sm" onClick={() => onBatchCreate?.()} disabled={!!batchCreating || aiCreating}
                style={{ padding: '4px 10px', fontSize: 12 }}>
                {batchCreating ? `⏳ 生成中...` : '🚀 开始连续创作'}
              </button>
            </div>
          )}
          {/* 技能包多选器（可折叠，紧凑模式） */}
          {skillPacks.length > 0 && (
            <div className="skill-pack-collapsible skill-pack-compact">
              <button
                className="skill-pack-toggle"
                onClick={() => setSkillExpanded(v => !v)}
                disabled={aiCreating}
              >
                <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
                <span>📦 协同技能包</span>
                {selectedCount > 0 && <span className="skill-pack-toggle-badge">{selectedCount}</span>}
                <span className="skill-pack-toggle-hint">{skillExpanded ? '收起' : '展开'}</span>
              </button>
              {skillExpanded && (
                <>
                  <SkillPackGroupedList
                    skillPacks={skillPacks}
                    selectedSkillPackIds={selectedSkillPackIds}
                    onToggleSkillPack={onToggleSkillPack}
                    disabled={aiCreating}
                    excludeCategory="master"
                  />
                  {selectedSkillPacks.length > 0 && (
                    <div className="skill-pack-info-list">
                      {selectedSkillPacks.map(pack => (
                        <div key={pack.id} className="skill-pack-info">
                          <span className="skill-pack-info-icon">{pack.icon}</span>
                          <div>
                            <div className="skill-pack-info-name">{pack.name}</div>
                            <div className="skill-pack-info-desc">{pack.description}</div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          )}
          </div>{/* end ai-config-items-wrapper */}

          {/* 用户提问输入区 */}
          <div className="ai-prompt-section ai-prompt-vertical">
            {hasResult && !aiCreating && (
              <div className="ai-prompt-tip">
                💡 已生成正文，可输入修改意见（如"节奏太快请放慢""开头改紧张些"）后点发送，AI将基于本次结果调整；或点上方"🔄 重新生成"重跑
              </div>
            )}
            <textarea
              className="input ai-prompt-input"
              value={aiUserPrompt}
              onChange={e => onEditAiPrompt(e.target.value)}
              onKeyDown={handlePromptKeyDown}
              placeholder={hasResult
                ? '在此输入修改意见...'
                : (aiTargetChapterId
                  ? `例如：请为「${chapterEditTitle}」继续创作下一章正文，剧情连贯、章末留悬念，约2400字...`
                  : '例如：请开篇创作第一章，主角登场，埋下伏笔，约2400字...')}
              rows={5}
              disabled={aiCreating}
            />
            <div className="ai-prompt-bottom-row">
                {aiCreating && (
                <button
                  className="btn-ghost-sm"
                  onClick={onStopAiCreate}
                  style={{ marginRight: 8, color: 'var(--accent)', borderColor: 'var(--accent)' }}
                  title="立即停止生成（已生成内容会保留）"
                >
                  ⏹ 停止
                </button>
              )}
              <button className="btn-primary ai-prompt-submit" onClick={() => onExecuteAiCreate()} disabled={aiCreating || !aiUserPrompt.trim()}>
                {aiCreating ? '⏳ 创作中...' : '🚀 发送'}
              </button>
            </div>

            {/* 完成态底部操作栏（与 Ai 总创作一致的修改意见工作流） */}
            {hasResult && !aiCreating && (
              <div className="ai-prompt-bottom-actions" style={{ display: 'flex', gap: 8, marginTop: 10, justifyContent: 'flex-end', alignItems: 'center', paddingTop: 10, borderTop: '1px solid var(--border-color)' }}>
                <span style={{ fontSize: 11, color: 'var(--text-muted)', marginRight: 'auto' }}>
                  💬 修改意见 → 发送重写，或直接重新生成/保存
                </span>
                <button
                  className="btn-primary"
                  onClick={onRegenerateAiContent}
                  title="基于上一次要求重新生成（覆盖当前结果）"
                  style={{ background: 'linear-gradient(135deg,#e67e22 0%,#d35400 100%)', boxShadow: '0 2px 8px rgba(211,84,0,0.35)' }}
                >
                  🔄 重新生成
                </button>
                <button
                  className="btn-primary-sm"
                  onClick={onConfirmAiContent}
                  disabled={savingChapter}
                  title={savingChapter ? '保存中...' : '将本次生成内容保存到目标章节'}
                  style={savingChapter ? { opacity: 0.6, cursor: 'not-allowed' } : undefined}
                >
                  {savingChapter ? '⏳ 保存中...' : '✓ 保存到章节'}
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // 章节详情查看（小说阅读排版）
  if (activeChapter && !chapterEditing) {
    const paragraphs = (activeChapter.content || '').split(/\n+/).filter(p => p.trim());
    return (
      <div className="chapter-detail-panel chapter-detail-scrollable">
        <div className="chapter-detail-header">
          <button className="btn-ghost-sm" onClick={onBackToList}>← 返回列表</button>
          <div className="chapter-detail-actions">
            <button className="btn-primary-sm" onClick={onStartEdit}>✏️ 编辑</button>
            <button className="btn-ghost-sm" style={{color:'#e74c3c'}} onClick={() => onDeleteChapter(activeChapter.id)}>🗑️</button>
          </div>
        </div>
        <h3 className="chapter-detail-title">{activeChapter.title}</h3>
        <div className="chapter-detail-meta">{activeChapter.word_count} 字</div>
        <div className="chapter-reading-content">
          {paragraphs.length > 0 ? paragraphs.map((para, i) => (
            <p key={i} className="novel-paragraph">{para.trim()}</p>
          )) : (
            <p className="novel-empty-hint">这一章还是空的，点击"编辑"开始写作</p>
          )}
        </div>
      </div>
    );
  }

  // 章节编辑
  if (activeChapter && chapterEditing) {
    return (
      <div className="chapter-edit-panel">
        <div className="chapter-edit-header">
          <button className="btn-ghost-sm" onClick={onCancelEdit}>取消</button>
          <button className="btn-primary-sm" onClick={onSaveChapter} disabled={chapterSaving}>
            {chapterSaving ? '保存中...' : '💾 保存'}
          </button>
        </div>
        <input
          className="input chapter-edit-title-input"
          value={chapterEditTitle}
          onChange={e => onEditTitle(e.target.value)}
          placeholder="章节标题"
        />
        <textarea
          className="input chapter-edit-textarea"
          value={chapterEditContent}
          onChange={e => onEditContent(e.target.value)}
          placeholder="开始写作..."
          rows={20}
        />
      </div>
    );
  }

  // 章节列表（按卷分组）

  // 未分卷 = 无 parent_id，或 parent_id 指向已不存在的卷（删除卷后避免章节变孤儿不可见）
  const orphanChapters = chaptersByVolume['__orphan__'] || [];
  const volumeChapters = (volId: string) => chaptersByVolume[volId] || [];

  return (
    <div className="chapter-list-panel">
      <div className="chapter-list-header">
        <div className="chapter-header-row1">
          <button className="btn-ghost-sm" onClick={() => onCreateVolume()} title="新建卷">📂 新卷</button>
          <button className="btn-ghost-sm" onClick={handleRebinVolumes} disabled={rebinning || !bookId || chapters.filter(c => !c.is_volume).length === 0} title="按50章/卷自动重新分卷（清空现有卷结构后重建）">
            {rebinning ? '⏳ 分卷中...' : '🔄 重新分卷'}
          </button>
          <button className="btn-secondary-sm" onClick={() => importChaptersRef.current?.click()} disabled={importingChapters || !bookId} title="从 txt/md/docx/zip 文件追加章节，不影响已有章节">
            {importingChapters ? '⏳ 导入中...' : '📥 导入章节'}
          </button>
          <button className="btn-secondary-sm" onClick={() => onCreateChapter()}>+ 新章节</button>
        </div>
        <div className="chapter-header-row2">
          <button
            className="btn-ghost-sm"
            onClick={handleAiImportRecognize}
            disabled={aiImportRecognizing || !bookId || chapters.filter(c => !c.is_volume).length === 0}
            title="根据导入作品的文件名/章节标题+内容样本，AI自动识别填入空的创作维度（不覆盖已有内容）"
            style={{background:'linear-gradient(135deg,#7cb89e 0%,#5ba3a8 100%)',color:'#fff',fontWeight:600}}
          >
            {aiImportRecognizing ? '⏳ 识别中...' : '🤖 AI识别填维度'}
          </button>
          <button
            className="btn-primary-sm"
            onClick={() => bookId && openChatPanel(bookId)}
            disabled={aiCreating}
            title="打开 AI 智驾·正文Tab：续写/润色本章"
          >
            <span style={{display:'inline-flex',alignItems:'center',gap:6}}><CarLogo size={20} /><span>AI</span><span>智驾</span></span>
          </button>
        </div>
        <input
          ref={importChaptersRef}
          type="file"
          multiple
          style={{display:'none'}}
          onChange={handleImportChapters}
        />
      </div>
      {importChaptersError && <div className="error-msg" style={{padding:'0 12px'}}>{importChaptersError}</div>}
      {chapters.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon">📖</div>
          <p>还没有章节，点击"新章节"开始写作</p>
        </div>
      ) : (
        <div className="chapter-list">
          {/* 按卷分组显示 - 使用 memo 化的 VolumeGroup 子组件，折叠某卷时其他卷不重渲染 */}
          {volumes.map(vol => {
            const volChs = volumeChapters(vol.id);
            const expanded = expandedVolumes[vol.id] !== false; // 默认展开
            const isRenaming = renamingVolId === vol.id;
            return (
              <VolumeGroup
                key={vol.id}
                volId={vol.id}
                volTitle={vol.title}
                volChs={volChs}
                expanded={expanded}
                renaming={isRenaming}
                renameValue={renameVolTitle}
                onToggle={toggleVolume}
                onSelectChapter={onSelectChapter}
                onCreateChapter={createChapterInVolume}
                onDeleteVolume={removeVolume}
                onStartRename={startRenameVolume}
                onRenameSubmit={submitRenameVolume}
                onCancelRename={cancelRenameVolume}
                onRenameChange={setRenameVolTitle}
              />
            );
          })}
          {/* 未分卷的章节 - 单独处理（不复用 VolumeGroup，因其重命名逻辑不同：转未分卷为命名卷） */}
          {orphanChapters.length > 0 && (
            <div className="chapter-volume-group">
              <div
                className="chapter-volume-header"
                onClick={() => volumes.length > 0 && toggleVolume('__orphan__')}
              >
                {volumes.length > 0 && (
                  <span className="chapter-volume-arrow">{expandedVolumes['__orphan__'] !== false ? '▼' : '▶'}</span>
                )}
                {renamingVolId === '__orphan__' ? (
                  <input
                    className="input chapter-volume-rename-input"
                    value={renameVolTitle}
                    onChange={e => setRenameVolTitle(e.target.value)}
                    onBlur={async () => {
                      const name = renameVolTitle.trim();
                      setRenamingVolId(null);
                      if (name) {
                        await onCreateVolume(name, orphanChapters.map(c => c.id));
                      }
                    }}
                    onKeyDown={e => { if (e.key === 'Enter') { (e.target as HTMLInputElement).blur(); } }}
                    autoFocus
                    onClick={e => e.stopPropagation()}
                    placeholder="输入卷名..."
                  />
                ) : (
                  <span className="chapter-volume-title">📋 未分卷</span>
                )}
                <span className="chapter-volume-count">{orphanChapters.length}章</span>
                <button className="btn-ghost-sm chapter-volume-add" onClick={e => { e.stopPropagation(); setRenamingVolId('__orphan__'); setRenameVolTitle(''); }} title="将未分卷转为命名卷">✏️</button>
                <button className="btn-ghost-sm chapter-volume-add" onClick={e => { e.stopPropagation(); onDeleteVolume('__orphan__'); }} title="删除全部未分卷章节" style={{color:'#e74c3c'}}>🗑️</button>
              </div>
              {(volumes.length === 0 || expandedVolumes['__orphan__'] !== false) && (
                <div className="chapter-volume-children">
                  {orphanChapters.map((ch, i) => (
                    <div key={ch.id} className="chapter-list-item" onClick={() => onSelectChapter(ch.id)}>
                      <div className="chapter-list-index">{i + 1}</div>
                      <div className="chapter-list-info">
                        <div className="chapter-list-title">{ch.title}</div>
                        <div className="chapter-list-meta">{ch.word_count} 字</div>
                      </div>
                      <div className="chapter-list-arrow">›</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ===== 人物及关系面板 ===== */
interface CharacterData {
  name: string;
  role?: string;
  identity?: string;
  personality?: string;
  motivation?: string;
  background?: string;
  relationships?: string;
  abilities?: string;
  items?: string;
}

function CharacterPanel(props: {
  bookId: string;
  bible: BookBible | null;
  onBibleUpdate: (b: BookBible) => void;
  bookTitle: string;
  chapters: Chapter[];
  hasChapters: boolean;
  showConfirm: (message: string, onConfirm: () => void) => void;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
  onOpenAiCreate: () => void;
}) {
  const { bookId, bible, onBibleUpdate, chapters, hasChapters, showConfirm, skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks } = props;
  const [characters, setCharacters] = useState<CharacterData[]>([]);
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [addingNew, setAddingNew] = useState(false);
  const [editForm, setEditForm] = useState<CharacterData>({ name: '', role: '', identity: '', personality: '', motivation: '', background: '', relationships: '', abilities: '', items: '' });
  const [analyzingName, setAnalyzingName] = useState('');
  const [aiMode, setAiMode] = useState(false);
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiAssisting, setAiAssisting] = useState(false);
  const [aiError, setAiError] = useState('');
  const [skillExpanded, setSkillExpanded] = useState(false);
  const [collapsedChars, setCollapsedChars] = useState<Set<number>>(new Set());
  // 全局人物批量管理
  const [charBatchMode, setCharBatchMode] = useState(false);
  const [charCheckedIds, setCharCheckedIds] = useState<Set<number>>(new Set());
  const [globalCharCollapsed, setGlobalCharCollapsed] = useState(false);
  // 按卷人物识别
  const [charVolumes, setCharVolumes] = useState<any[]>([]);
  const [analyzingVol, setAnalyzingVol] = useState('');
  const [collapsedVolChars, setCollapsedVolChars] = useState<Set<number>>(new Set());
  // 每次进入维度默认折叠所有卷（tab 切换重新挂载，ref 重置）
  const charCollapseInitRef = useRef(false);
  // 卷选择器
  const [volSelectorOpen, setVolSelectorOpen] = useState(false);
  // 按卷编辑：editingVolIdx 为正在编辑的卷索引，editVolJson 为编辑中的 JSON 文本
  const [editingVolIdx, setEditingVolIdx] = useState<number | null>(null);
  const [editVolJson, setEditVolJson] = useState('');
  // 关系图谱编辑
  const [relGraphCollapsed, setRelGraphCollapsed] = useState(true);
  const [relGraphEditing, setRelGraphEditing] = useState(false);
  const [relGraphValue, setRelGraphValue] = useState('');

  function toggleChar(idx: number) {
    setCollapsedChars(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  // 解析角色数据
  useEffect(() => {
    if (!bible?.character_profiles) { setCharacters([]); return; }
    try {
      const parsed = JSON.parse(bible.character_profiles);
      if (Array.isArray(parsed)) {
        setCharacters(parsed);
        return;
      }
    } catch { /* not JSON */ }
    // 纯文本模式：尝试按行或【】解析
    const text = bible.character_profiles;
    const chars: CharacterData[] = [];
    const blocks = text.split(/\n\s*\n/).filter(b => b.trim());
    for (const block of blocks) {
      const nameMatch = block.match(/[【\[](.+?)[】\]]/);
      const name = nameMatch ? nameMatch[1] : block.split(/[：:\n]/)[0].trim();
      if (name) chars.push({ name, role: '', personality: block.trim() });
    }
    if (chars.length === 0 && text.trim()) {
      setCharacters([{ name: '角色信息', personality: text.trim() }]);
    } else {
      setCharacters(chars);
    }
  }, [bible?.character_profiles]);

  // 解析按卷人物数据（character_volumes）
  useEffect(() => {
    if (!bible?.character_volumes) { setCharVolumes([]); return; }
    try {
      const parsed = JSON.parse(bible.character_volumes);
      if (Array.isArray(parsed)) { setCharVolumes(parsed); return; }
    } catch { /* not JSON */ }
    setCharVolumes([]);
  }, [bible?.character_volumes]);

  // chapters 表的卷（可识别）
  const volumeChapters = chapters.filter(c => c.is_volume);

  // 合并卷列表：chapters.is_volume 卷 + charVolumes 已有卷
  const displayCharVolumes = useMemo(() => {
    const result: any[] = [];
    const usedIds = new Set<string>();
    for (const vc of volumeChapters) {
      const cvData = charVolumes.find(v => v.volume_id === vc.id) || charVolumes.find(v => v.volume === vc.title);
      result.push({
        volume_id: vc.id,
        volume: vc.title,
        characters: cvData?.characters || [],
        chapter_count: chapters.filter(c => c.parent_id === vc.id).length,
      });
      if (cvData) { usedIds.add(cvData.volume_id || ''); usedIds.add(cvData.volume || ''); }
    }
    for (const v of charVolumes) {
      const id = v.volume_id || '';
      const name = v.volume || '';
      if (!usedIds.has(id) && !usedIds.has(name)) {
        result.push({ ...v, chapter_count: 0 });
      }
    }
    return result;
  }, [volumeChapters, charVolumes, chapters]);

  // 首次有卷数据时默认折叠全部卷（每次切换到该维度 tab 重新挂载，ref 重置，实现每次进入默认折叠）
  useEffect(() => {
    if (charCollapseInitRef.current) return;
    if (displayCharVolumes.length > 0) {
      charCollapseInitRef.current = true;
      setCollapsedVolChars(new Set(displayCharVolumes.map((_, idx) => idx)));
    }
  }, [displayCharVolumes]);

  function toggleVolChar(idx: number) {
    setCollapsedVolChars(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  // AI识别指定卷人物
  async function handleAnalyzeCharVolume(volId: string, volTitle: string) {
    showConfirm(`将用 AI 分析「${volTitle}」的章节内容，识别本卷出现的角色。是否继续？`, async () => {
      setAnalyzingVol(volId || volTitle);
      try {
        const result = await api.analyzeCharacterVolume(bookId, volId, volTitle, selectedSkillPackIds);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已为「${volTitle}」识别 ${result.volume_data?.characters?.length || 0} 个角色`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingVol('');
    });
  }

  // 删除某卷的人物数据
  async function deleteVolumeCharacters(idx: number) {
    const vol = displayCharVolumes[idx];
    if (!vol) return;
    showConfirm(`确定删除「${vol.volume || '该卷'}」的人物识别数据？`, async () => {
      const newList = charVolumes.filter((v: any) => {
        const vId = v.volume_id || '';
        const vName = v.volume || '';
        if (vol.volume_id && vId === vol.volume_id) return false;
        if (vol.volume && vName === vol.volume) return false;
        return true;
      });
      try {
        const updated = await api.updateBible(bookId, { character_volumes: JSON.stringify(newList, null, 2) } as any);
        onBibleUpdate(updated);
      } catch (e: any) {
        alert('删除失败: ' + e.message);
      }
    });
  }

  // 开始按卷编辑：将该卷的完整数据序列化为 JSON 供编辑
  function startEditVolCharacters(idx: number) {
    const vol = displayCharVolumes[idx];
    if (!vol) return;
    const editTarget = {
      volume_id: vol.volume_id || '',
      volume: vol.volume || '',
      characters: vol.characters || [],
    };
    setEditingVolIdx(idx);
    setEditVolJson(JSON.stringify(editTarget, null, 2));
    setCollapsedVolChars(prev => { const n = new Set(prev); n.delete(idx); return n; });
  }

  // 保存按卷编辑：解析编辑后的 JSON，写回 character_volumes
  async function saveEditVolCharacters(idx: number) {
    try {
      const parsed = JSON.parse(editVolJson);
      const vol = displayCharVolumes[idx];
      const matchKey = vol.volume_id || vol.volume;
      const newList = charVolumes.map((v: any) => {
        const vKey = v.volume_id || v.volume;
        if (vKey === matchKey) {
          return {
            volume_id: parsed.volume_id || v.volume_id || '',
            volume: parsed.volume || v.volume || '',
            characters: Array.isArray(parsed.characters) ? parsed.characters : (v.characters || []),
          };
        }
        return v;
      });
      // 若该卷尚未在 charVolumes 中（纯展示卷），则追加
      const exists = newList.some((v: any) => (v.volume_id || v.volume) === matchKey);
      if (!exists) {
        newList.push({
          volume_id: parsed.volume_id || vol.volume_id || '',
          volume: parsed.volume || vol.volume || '',
          characters: Array.isArray(parsed.characters) ? parsed.characters : [],
        });
      }
      const updated = await api.updateBible(bookId, { character_volumes: JSON.stringify(newList, null, 2) } as any);
      onBibleUpdate(updated);
      setEditingVolIdx(null);
      setEditVolJson('');
    } catch (e: any) {
      alert('保存失败：JSON 格式错误 - ' + e.message);
    }
  }

  // 将某卷识别的角色智能合并到全局人物档案（同名更新，新角色追加）
  async function mergeVolumeToGlobal(idx: number) {
    const vol = displayCharVolumes[idx];
    if (!vol || !vol.characters || vol.characters.length === 0) return;
    showConfirm(`将「${vol.volume}」识别的 ${vol.characters.length} 个角色与全局人物档案相互验证更新？\n• 同名角色：用本卷新信息补充更新（不覆盖已有非空字段）\n• 新角色：自动追加到全局档案`, async () => {
      const globalMap = new Map<string, { char: CharacterData; idx: number }>();
      characters.forEach((c, i) => { if (c.name) globalMap.set(c.name, { char: c, idx: i }); });
      let updatedCount = 0;
      let addedCount = 0;
      const newChars = [...characters];
      for (const c of vol.characters) {
        if (!c.name) continue;
        const existing = globalMap.get(c.name);
        if (existing) {
          // 同名角色：补充更新（仅填充全局档案中为空的字段）
          const merged = { ...existing.char };
          let changed = false;
          const fields: (keyof CharacterData)[] = ['role', 'identity', 'personality', 'motivation', 'background', 'relationships', 'abilities', 'items'];
          for (const f of fields) {
            const newVal = (c as any)[f];
            if (newVal && newVal.trim() && !(merged[f] && (merged[f] as string).trim())) {
              (merged as any)[f] = newVal;
              changed = true;
            }
          }
          if (changed) {
            newChars[existing.idx] = merged;
            updatedCount++;
          }
        } else {
          // 新角色：追加
          newChars.push({
            name: c.name,
            role: c.role || '',
            identity: c.identity || '',
            personality: c.personality || '',
            motivation: c.motivation || '',
            relationships: c.relationships || '',
            abilities: c.abilities || '',
            items: c.items || '',
          });
          globalMap.set(c.name, { char: newChars[newChars.length - 1], idx: newChars.length - 1 });
          addedCount++;
        }
      }
      if (updatedCount === 0 && addedCount === 0) {
        alert('该卷角色已全部在全局档案中且无新信息可更新');
        return;
      }
      await saveCharacters(newChars);
      const parts: string[] = [];
      if (addedCount > 0) parts.push(`新增 ${addedCount} 个角色`);
      if (updatedCount > 0) parts.push(`更新 ${updatedCount} 个角色信息`);
      alert(`相互验证完成：${parts.join('，')}`);
    });
  }

  // 将全局人物档案同步回写到所有分卷（同名更新，新角色追加到对应卷）
  async function syncGlobalToVolumes() {
    if (characters.length === 0) { alert('全局人物档案为空，无法同步'); return; }
    if (charVolumes.length === 0) { alert('暂无分卷人物数据，无法同步'); return; }
    showConfirm(`将全局人物档案（${characters.length}人）同步到所有分卷？\n• 同名角色：用全局信息补充更新分卷（不覆盖分卷已有非空字段）\n• 全局有但分卷没有的角色：追加到对应卷`, async () => {
      let totalUpdated = 0;
      let totalAdded = 0;
      const newList = charVolumes.map((vol: any) => {
        const volChars: any[] = vol.characters ? [...vol.characters] : [];
        const volMap = new Map<string, number>();
        volChars.forEach((c: any, i: number) => { if (c.name) volMap.set(c.name, i); });
        let updated = 0;
        let added = 0;
        for (const gc of characters) {
          if (!gc.name) continue;
          const existIdx = volMap.get(gc.name);
          if (existIdx !== undefined) {
            // 同名：补充更新
            const merged = { ...volChars[existIdx] };
            let changed = false;
            const fields = ['role', 'identity', 'personality', 'motivation', 'background', 'relationships', 'abilities', 'items'];
            for (const f of fields) {
              const gVal = (gc as any)[f];
              if (gVal && gVal.trim() && !(merged[f] && merged[f].trim())) {
                merged[f] = gVal;
                changed = true;
              }
            }
            if (changed) { volChars[existIdx] = merged; updated++; }
          } else {
            // 新角色：追加到该卷
            volChars.push({
              name: gc.name,
              role: gc.role || '',
              identity: gc.identity || '',
              personality: gc.personality || '',
              motivation: gc.motivation || '',
              relationships: gc.relationships || '',
              abilities: gc.abilities || '',
              items: gc.items || '',
              arc: '',
            });
            volMap.set(gc.name, volChars.length - 1);
            added++;
          }
        }
        totalUpdated += updated;
        totalAdded += added;
        return { ...vol, characters: volChars };
      });
      if (totalUpdated === 0 && totalAdded === 0) {
        alert('所有分卷人物已与全局档案一致，无需更新');
        return;
      }
      try {
        const updated = await api.updateBible(bookId, { character_volumes: JSON.stringify(newList, null, 2) } as any);
        onBibleUpdate(updated);
        const parts: string[] = [];
        if (totalUpdated > 0) parts.push(`更新 ${totalUpdated} 个角色`);
        if (totalAdded > 0) parts.push(`追加 ${totalAdded} 个角色到分卷`);
        alert(`同步完成：${parts.join('，')}`);
      } catch (e: any) {
        alert('同步失败: ' + e.message);
      }
    });
  }

  async function saveCharacters(newChars: CharacterData[]) {
    setCharacters(newChars);
    try {
      const updated = await api.updateBible(bookId, { character_profiles: JSON.stringify(newChars, null, 2) } as any);
      onBibleUpdate(updated);
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
  }

  function startAddNew() {
    setEditForm({ name: '', role: '配角', identity: '', personality: '', motivation: '', background: '', relationships: '', abilities: '', items: '' });
    setAddingNew(true);
    setEditingIdx(null);
  }

  function startEdit(idx: number) {
    setEditForm({ ...characters[idx] });
    setEditingIdx(idx);
    setAddingNew(false);
  }

  async function saveEdit() {
    if (!editForm.name.trim()) { alert('请输入角色名称'); return; }
    const newChars = [...characters];
    if (editingIdx !== null) {
      newChars[editingIdx] = { ...editForm };
    } else {
      newChars.push({ ...editForm });
    }
    await saveCharacters(newChars);
    setEditingIdx(null);
    setAddingNew(false);
  }

  async function deleteChar(idx: number) {
    const char = characters[idx];
    showConfirm(`确定删除角色「${char.name}」？`, async () => {
      const newChars = characters.filter((_, i) => i !== idx);
      await saveCharacters(newChars);
    });
  }

  // 批量删除选中角色
  async function deleteCheckedChars() {
    if (charCheckedIds.size === 0) return;
    showConfirm(`确定删除选中的 ${charCheckedIds.size} 个角色？`, async () => {
      const newChars = characters.filter((_, i) => !charCheckedIds.has(i));
      await saveCharacters(newChars);
      setCharCheckedIds(new Set());
      setCharBatchMode(false);
    });
  }

  function toggleCharCheck(idx: number) {
    setCharCheckedIds(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  // AI识别单个角色
  async function handleAnalyzeOne(charName: string) {
    showConfirm(`将用 AI 分析已有章节内容，自动识别并填充「${charName}」的详细信息。是否继续？`, async () => {
      setAnalyzingName(charName);
      try {
        const result = await api.analyzeCharacter(bookId, charName);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已填充「${charName}」的信息`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingName('');
    });
  }

  // AI协同创作
  async function executeAi() {
    if (!aiPrompt.trim()) { alert('请输入创作要求'); return; }
    setAiAssisting(true);
    setAiError('');
    try {
      const skillKeys = ['character_cognition', 'tomato_character'];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const contextConcept = bible?.concept || '暂无构思';
      const existingNames = characters.map(c => c.name).join('、');
      const messages = [
        { role: 'system', content: `你是专业网文创作助手。请根据用户要求生成角色档案。${skillNote}` },
        { role: 'user', content: `构思：${contextConcept}\n已有角色：${existingNames || '无'}\n\n用户要求：${aiPrompt}\n\n请生成角色档案，包括姓名、身份、性格、动机、背景、关系、能力、物品。用JSON数组格式输出。` },
      ];
      const result = await api.aiChat(messages);
      // 尝试解析AI返回的JSON
      let newChars: CharacterData[] = [];
      try {
        const match = result.content.match(/\[[\s\S]*\]/);
        if (match) newChars = JSON.parse(match[0]);
        else {
          const objMatch = result.content.match(/\{[\s\S]*\}/);
          if (objMatch) newChars = [JSON.parse(objMatch[0])];
        }
      } catch { /* parse fail */ }
      if (newChars.length > 0) {
        await saveCharacters([...characters, ...newChars]);
        setAiMode(false);
        setAiPrompt('');
      } else {
        setAiError('AI返回格式无法解析，请重试');
      }
    } catch (e: any) {
      setAiError(e.message || 'AI创作失败');
    }
    setAiAssisting(false);
  }

  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!aiAssisting && aiPrompt.trim()) executeAi();
    }
  };

  // AI协同创作模式
  if (aiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <h3>👤 AI协同创作 · 人物</h3>
          <button className="btn-ghost-sm" onClick={() => { setAiMode(false); setAiError(''); }} disabled={aiAssisting}>取消</button>
        </div>
        {skillPacks.length > 0 && (
          <div className="skill-pack-collapsible">
            <button className="skill-pack-toggle" onClick={() => setSkillExpanded(v => !v)} disabled={aiAssisting}>
              <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
              <span>📦 协同技能包</span>
              {selectedSkillPackIds.length > 0 && <span className="skill-pack-toggle-badge">{selectedSkillPackIds.length}</span>}
              <span className="skill-pack-toggle-hint">{skillExpanded ? '收起' : '展开'}</span>
            </button>
            {skillExpanded && (
              <SkillPackGroupedList
                skillPacks={skillPacks}
                selectedSkillPackIds={selectedSkillPackIds}
                onToggleSkillPack={onToggleSkillPack}
                disabled={aiAssisting}
              />
            )}
          </div>
        )}
        <div className="ai-prompt-vertical">
          <textarea
            className="input bible-ai-prompt-input"
            rows={6}
            value={aiPrompt}
            onChange={e => setAiPrompt(e.target.value)}
            onKeyDown={handlePromptKeyDown}
            placeholder="例如：为主角设计3个重要配角，包括反派、导师、恋人..."
            disabled={aiAssisting}
            autoFocus
          />
          <div className="ai-prompt-bottom-row">
            <button className="btn-primary ai-prompt-submit" onClick={executeAi} disabled={aiAssisting || !aiPrompt.trim()}>
              {aiAssisting ? '⏳ 创作中...' : '🚀 发送'}
            </button>
          </div>
        </div>
        {aiError && <div className="error-msg" style={{marginTop:8}}>{aiError}</div>}
        {aiAssisting && (
          <div className="bible-ai-loading">
            <div className="loading-spinner" />
            <p>AI正在生成人物档案...</p>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="bible-edit-panel">
      <div className="bible-edit-header">
        <div className="bible-edit-actions" style={{position:'relative',flexShrink:0}}>
          {(
            <>
              <button className="btn-ghost-sm" onClick={() => setVolSelectorOpen(v => !v)} disabled={!!analyzingVol || !hasChapters} title={hasChapters ? '选择卷进行AI识别' : '需要先创建章节才能AI识别'}>
                {analyzingVol ? '🤖 识别中...' : '🔍 AI识别'}
              </button>
              {volSelectorOpen && (
                <div className="vol-selector-dropdown" style={{position:'absolute',top:'100%',right:0,marginTop:4,background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:8,padding:6,minWidth:180,zIndex:100,boxShadow:'0 4px 12px rgba(0,0,0,0.15)'}}>
                  <div style={{fontSize:12,color:'var(--text-muted)',padding:'4px 8px',borderBottom:'1px solid var(--border)',marginBottom:4}}>选择要识别的卷</div>
                  <button className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeCharVolume('', '全部章节'); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📚 全部章节</button>
                  {displayCharVolumes.map((vol, idx) => (
                    <button key={idx} className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeCharVolume(vol.volume_id || '', vol.volume || `第${idx + 1}卷`); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📖 {vol.volume || `第${idx + 1}卷`}{vol.chapter_count ? ` (${vol.chapter_count}章)` : ''}</button>
                  ))}
                  <button onClick={() => setVolSelectorOpen(false)} style={{display:'block',width:'100%',textAlign:'center',padding:'4px',background:'transparent',border:'none',cursor:'pointer',color:'var(--text-muted)',fontSize:12,marginTop:2}}>取消</button>
                </div>
              )}
            </>
          )}
          <button className="btn-primary-sm" onClick={startAddNew}>＋ 添加角色</button>
        </div>
      </div>

      {/* 按卷人物识别 */}
      {displayCharVolumes.length > 0 && (
        <div className="plot-volume-list" style={{marginBottom:16}}>
          <p className="text-muted" style={{fontSize:12, marginBottom:8}}>
            📚 按卷识别人物：点击「🔍 AI识别」选择卷，识别结果自动归类到对应卷下，可合并到下方全局人物档案。
          </p>
          {displayCharVolumes.map((vol, idx) => (
            <div key={idx} className="plot-volume-card">
              <div className="plot-volume-header" onClick={() => toggleVolChar(idx)} style={{cursor:'pointer'}}>
                <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{collapsedVolChars.has(idx) ? '▶' : '▼'}</span>
                <h4>{vol.volume || `第${idx + 1}卷`}</h4>
                {vol.chapter_count !== undefined && <span className="text-muted" style={{fontSize:12}}>{vol.chapter_count}章</span>}
                <span className="text-muted" style={{fontSize:12}}>{(vol.characters || []).length}人</span>
                <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                  {analyzingVol === (vol.volume_id || vol.volume) && <span className="text-muted" style={{fontSize:12}}>🤖 识别中...</span>}
                  <button className="btn-ghost-sm" onClick={() => editingVolIdx === idx ? (setEditingVolIdx(null), setEditVolJson('')) : startEditVolCharacters(idx)} title={editingVolIdx === idx ? '取消编辑' : '编辑此卷人物数据（JSON）'}>{editingVolIdx === idx ? '取消' : '✏️'}</button>
                  {(vol.characters || []).length > 0 && (
                    <>
                      <button className="btn-ghost-sm" onClick={() => mergeVolumeToGlobal(idx)} title="与全局人物档案相互验证更新（同名补充，新角色追加）" style={{color:'#27ae60'}}>⇅ 验证更新</button>
                      <button className="btn-ghost-sm" onClick={() => deleteVolumeCharacters(idx)} style={{color:'#e74c3c'}} title="删除此卷人物数据">🗑️</button>
                    </>
                  )}
                </div>
              </div>
              {!collapsedVolChars.has(idx) && (
                <div className="plot-volume-body">
                  {editingVolIdx === idx ? (
                    <div style={{marginTop:8}}>
                      <p className="text-muted" style={{fontSize:12,marginBottom:6}}>编辑本卷人物数据（JSON 格式），可直接修改 characters 数组中各角色的字段。</p>
                      <textarea className="input" value={editVolJson} onChange={e => setEditVolJson(e.target.value)} rows={16} style={{fontFamily:'monospace',fontSize:12}} />
                      <div style={{display:'flex',gap:6,marginTop:8}}>
                        <button className="btn-primary-sm" onClick={() => saveEditVolCharacters(idx)}>💾 保存</button>
                        <button className="btn-ghost-sm" onClick={() => { setEditingVolIdx(null); setEditVolJson(''); }}>取消</button>
                      </div>
                    </div>
                  ) : (!vol.characters || vol.characters.length === 0) ? (
                    <p className="text-muted" style={{fontSize:13}}>暂无人物识别数据，点击「🔍 AI识别」选择此卷进行识别</p>
                  ) : (
                    <div className="character-cards-grid">
                      {vol.characters.map((char: any, ci: number) => (
                        <div key={ci} className="character-card">
                          <div className="character-card-header">
                            <span className="character-card-name">{char.name}</span>
                            {char.role && <span className="character-card-role">{char.role}</span>}
                          </div>
                          <div className="character-card-body">
                            {char.identity && <p><b>身份：</b>{char.identity}</p>}
                            {char.personality && <p><b>性格：</b>{char.personality}</p>}
                            {char.motivation && <p><b>动机：</b>{char.motivation}</p>}
                            {char.relationships && <p><b>关系：</b>{char.relationships}</p>}
                            {char.abilities && <p><b>能力：</b>{char.abilities}</p>}
                            {char.items && <p><b>物品：</b>{char.items}</p>}
                            {char.arc && <p><b>本卷弧线：</b>{char.arc}</p>}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 添加/编辑表单 */}
      {(addingNew || editingIdx !== null) && (
        <div className="character-edit-form" style={{background:'var(--bg-tertiary)',borderRadius:'var(--radius-sm)',padding:16,marginBottom:16}}>
          <h4 style={{marginBottom:10}}>{editingIdx !== null ? '编辑角色' : '添加角色'}</h4>
          <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:10}}>
            <div>
              <label className="input-label">角色名称 *</label>
              <input className="input" value={editForm.name} onChange={e => setEditForm({...editForm, name: e.target.value})} placeholder="如：林逸" autoFocus />
            </div>
            <div>
              <label className="input-label">角色定位</label>
              <select className="input" value={editForm.role || ''} onChange={e => setEditForm({...editForm, role: e.target.value})}>
                <option value="">— 选择 —</option>
                <option value="主角">主角</option>
                <option value="配角">配角</option>
                <option value="反派">反派</option>
                <option value="路人">路人</option>
              </select>
            </div>
            <div>
              <label className="input-label">身份职业</label>
              <input className="input" value={editForm.identity || ''} onChange={e => setEditForm({...editForm, identity: e.target.value})} placeholder="如：散修/皇子/商人" />
            </div>
            <div>
              <label className="input-label">性格特征</label>
              <input className="input" value={editForm.personality || ''} onChange={e => setEditForm({...editForm, personality: e.target.value})} placeholder="如：沉稳内敛，心思缜密" />
            </div>
            <div>
              <label className="input-label">核心动机</label>
              <input className="input" value={editForm.motivation || ''} onChange={e => setEditForm({...editForm, motivation: e.target.value})} placeholder="如：复仇/求道/守护家族" />
            </div>
            <div>
              <label className="input-label">人物关系</label>
              <input className="input" value={editForm.relationships || ''} onChange={e => setEditForm({...editForm, relationships: e.target.value})} placeholder="如：与XX是师徒" />
            </div>
            <div>
              <label className="input-label">能力/功法</label>
              <input className="input" value={editForm.abilities || ''} onChange={e => setEditForm({...editForm, abilities: e.target.value})} placeholder="如：剑道天赋/火焰术" />
            </div>
            <div>
              <label className="input-label">持有物品</label>
              <input className="input" value={editForm.items || ''} onChange={e => setEditForm({...editForm, items: e.target.value})} placeholder="如：寒霜剑/破界符" />
            </div>
          </div>
          <div style={{marginTop:10}}>
            <label className="input-label">背景故事</label>
            <textarea className="input" rows={3} value={editForm.background || ''} onChange={e => setEditForm({...editForm, background: e.target.value})} placeholder="角色的过往经历..." />
          </div>
          <div style={{display:'flex',gap:8,marginTop:12}}>
            <button className="btn-primary-sm" onClick={saveEdit}>💾 保存</button>
            <button className="btn-ghost-sm" onClick={() => { setEditingIdx(null); setAddingNew(false); }}>取消</button>
          </div>
        </div>
      )}

      {/* 全局人物档案 */}
      {characters.length === 0 ? (
        <div className="bible-empty">
          <span className="bible-empty-icon">👤</span>
          <p>暂无角色信息</p>
          <p className="text-muted">点击顶部「＋ 添加角色」或「✨ AI创作」生成人物档案</p>
        </div>
      ) : (
        <div className="plot-volume-list">
          <div className="plot-volume-card">
            <div className="plot-volume-header" onClick={() => setGlobalCharCollapsed(v => !v)} style={{cursor:'pointer'}}>
              <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{globalCharCollapsed ? '▶' : '▼'}</span>
              <h4>🌍 全局人物档案</h4>
              <span className="text-muted" style={{fontSize:12}}>{characters.length}人</span>
              <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                {charBatchMode ? (
                  <>
                    <button className="btn-ghost-sm" onClick={() => { setCharCheckedIds(new Set()); setCharBatchMode(false); }}>✕ 取消</button>
                    <button className="btn-ghost-sm" onClick={deleteCheckedChars} disabled={charCheckedIds.size === 0} style={{color:'#e74c3c'}} title="删除选中角色">🗑️ 删除选中({charCheckedIds.size})</button>
                  </>
                ) : (
                  <>
                    {charVolumes.length > 0 && (
                      <button className="btn-ghost-sm" onClick={syncGlobalToVolumes} title="将全局人物同步到所有分卷（同名补充，新角色追加）" style={{color:'#27ae60'}}>⇅ 同步到分卷</button>
                    )}
                    <button className="btn-ghost-sm" onClick={() => setCharBatchMode(true)} title="批量选择并删除角色">☑ 批量管理</button>
                  </>
                )}
              </div>
            </div>
            {!globalCharCollapsed && (
              <div className="plot-volume-body">
                <div className="character-cards-grid">
                  {characters.map((char, idx) => (
                    <div key={idx} className="character-card" style={charBatchMode && charCheckedIds.has(idx) ? {border:'2px solid var(--accent)'} : {}}>
                      <div className="character-card-header" onClick={() => charBatchMode ? toggleCharCheck(idx) : toggleChar(idx)} style={{cursor:'pointer'}}>
                        {charBatchMode && (
                          <input type="checkbox" checked={charCheckedIds.has(idx)} onChange={() => toggleCharCheck(idx)} style={{marginRight:6}} onClick={e => e.stopPropagation()} />
                        )}
                        {!charBatchMode && <span className="map-toggle" style={{fontSize:10,marginRight:4}}>{collapsedChars.has(idx) ? '▶' : '▼'}</span>}
                        <span className="character-card-name">{char.name}</span>
                        {char.role && <span className="character-card-role">{char.role}</span>}
                        {char.abilities && <span className="text-muted" style={{fontSize:10,marginLeft:4}}>{(char.abilities || '').slice(0, 12)}</span>}
                      </div>
                      {!charBatchMode && !collapsedChars.has(idx) && (
                        <div className="character-card-body">
                          {char.identity && <p><b>身份：</b>{char.identity}</p>}
                          {char.personality && <p><b>性格：</b>{char.personality}</p>}
                          {char.motivation && <p><b>动机：</b>{char.motivation}</p>}
                          {char.background && <p><b>背景：</b>{char.background}</p>}
                          {char.relationships && <p><b>关系：</b>{char.relationships}</p>}
                          {char.abilities && <p><b>能力：</b>{char.abilities}</p>}
                          {char.items && <p><b>物品：</b>{char.items}</p>}
                        </div>
                      )}
                      {!charBatchMode && !collapsedChars.has(idx) && (
                      <div className="character-card-actions">
                        <button className="btn-ghost-sm" onClick={() => handleAnalyzeOne(char.name)} disabled={analyzingName === char.name || !hasChapters} title={hasChapters ? 'AI识别此角色信息' : '需要先创建章节才能AI识别'}>
                          {analyzingName === char.name ? '🤖 识别中...' : '🔍 识别'}
                        </button>
                        <button className="btn-ghost-sm" onClick={() => startEdit(idx)}>✏️ 编辑</button>
                        <button className="btn-ghost-sm" onClick={() => deleteChar(idx)} style={{color:'#e74c3c'}}>🗑️</button>
                      </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* 关系图谱编辑区 */}
      <div className="plot-volume-list" style={{marginTop:16}}>
        <div className="plot-volume-card">
          <div className="plot-volume-header" onClick={() => setRelGraphCollapsed(v => !v)} style={{cursor:'pointer'}}>
            <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{relGraphCollapsed ? '▶' : '▼'}</span>
            <h4>🔗 人物关系图谱</h4>
            <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
              {relGraphEditing ? (
                <>
                  <button className="btn-primary-sm" onClick={async () => {
                    const updated = await api.updateBible(bookId, { relation_graph: relGraphValue } as any);
                    onBibleUpdate(updated);
                    setRelGraphEditing(false);
                  }}>💾 保存</button>
                  <button className="btn-ghost-sm" onClick={() => { setRelGraphEditing(false); setRelGraphValue(bible?.relation_graph || ''); }}>取消</button>
                </>
              ) : (
                <button className="btn-ghost-sm" onClick={() => { setRelGraphEditing(true); setRelGraphValue(bible?.relation_graph || ''); }} title="编辑人物关系图谱">✏️ 编辑</button>
              )}
            </div>
          </div>
          {!relGraphCollapsed && (
            <div className="plot-volume-body">
              {relGraphEditing ? (
                <textarea className="input" value={relGraphValue} onChange={e => setRelGraphValue(e.target.value)} rows={10} style={{fontFamily:'monospace',fontSize:12}} placeholder="用文本描述人物关系图谱，例如：\n张三 → 李四：师徒\n张三 → 王五：宿敌\n李四 → 赵六：恋人" />
              ) : (
                <div style={{fontSize:13,lineHeight:1.7,whiteSpace:'pre-wrap',color: bible?.relation_graph ? 'var(--text-primary)' : 'var(--text-muted)'}}>
                  {bible?.relation_graph || '暂无关系图谱，点击「编辑」手动输入或通过 AI 识别生成'}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ===== 剧情面板（按卷） ===== */
// 防御性文本化：AI 生成的 timeline JSON 中某些字段可能为对象/数组而非字符串，
// 直接渲染会触发 React error #31（"Objects are not valid as a React child"）。
// 此函数将任意值安全转为可渲染的字符串。
function safeText(v: any): string {
  if (v == null) return '';
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  if (Array.isArray(v)) return v.map(x => safeText(x)).filter(Boolean).join('；');
  if (typeof v === 'object') {
    // 优先取常见名称字段，否则 JSON 序列化
    const name = v.name || v.title || v.summary || v.description || '';
    if (typeof name === 'string' && name.trim()) return name;
    try { return JSON.stringify(v); } catch { return ''; }
  }
  return String(v);
}

function PlotPanel(props: {
  bookId: string;
  bible: BookBible | null;
  onBibleUpdate: (b: BookBible) => void;
  bookTitle: string;
  totalVolumes: number;
  chapters: Chapter[];
  hasChapters: boolean;
  showConfirm: (message: string, onConfirm: () => void) => void;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
  concept: string;
  onRefreshChapters: () => void;
  onOpenAiCreate: () => void;
}) {
  const { bookId, bible, onBibleUpdate, totalVolumes, chapters, hasChapters, showConfirm, skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks, concept, onRefreshChapters } = props;
  const [volumes, setVolumes] = useState<any[]>([]);
  const [editingVol, setEditingVol] = useState<string | null>(null);
  // editForm 持有完整卷对象的所有可编辑字段（main_plot/key_events/turning_points/climax/ending/foreshadowing）
  // 数组字段（key_events/turning_points/foreshadowing）在编辑框中以换行分隔的文本展示与编辑
  const [editForm, setEditForm] = useState<any>({});
  const [analyzingVol, setAnalyzingVol] = useState('');
  const [aiMode, setAiMode] = useState(false);
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiAssisting, setAiAssisting] = useState(false);
  const [aiError, setAiError] = useState('');
  const [skillExpanded, setSkillExpanded] = useState(false);
  const [collapsedVols, setCollapsedVols] = useState<Set<number>>(new Set());
  // 每次进入维度默认折叠所有卷（tab 切换重新挂载，ref 重置）
  const plotCollapseInitRef = useRef(false);
  const [editingVolName, setEditingVolName] = useState<string | null>(null);
  const [editVolName, setEditVolName] = useState('');

  // ==== 大纲工作流相关 state（从大纲维度迁移） ====
  const [extractLoading, setExtractLoading] = useState(false);
  const [importModalOpen, setImportModalOpen] = useState(false);
  const [importText, setImportText] = useState('');
  const [importLoading, setImportLoading] = useState(false);
  // 自动分卷规划
  const [targetWords, setTargetWords] = useState<number>(0);
  const [targetVolumeCount, setTargetVolumeCount] = useState<number>(0);
  const [showVolumeCalc, setShowVolumeCalc] = useState(false);
  // setTargetWords 暂未在 UI 接入，保留以备后续自动分卷输入框；此处引用避免 noUnusedLocals 报错
  void setTargetWords;
  const [volumeGeneratingIdx, setVolumeGeneratingIdx] = useState<number | null>(null);
  const [volumeData, setVolumeData] = useState<any[]>([]);
  const [expandedVol, setExpandedVol] = useState<Set<number>>(new Set());
  // 工作流状态（getter 仍被按钮 disabled 使用；setter 已废弃，因 generateOutlineMaster 已移除）
  const [outlineWorkflowLoading] = useState<'' | 'master' | 'volume' | 'all'>('');
  const [outlineWorkflowProgress] = useState('');
  // 工作流按钮区折叠（手机友好）
  const [workflowCollapsed, setWorkflowCollapsed] = useState(false);
  // 一键清空
  const [clearing, setClearing] = useState(false);

  // 五幕弧线模板
  const ARC_NAMES = ['立身', '立足', '立势', '立威', '立命'];
  const ARC_THEMES: Record<string, string> = {
    '立身': '底层→入门：觉醒金手指+首打脸+建立认知',
    '立足': '新人→站稳：配角登场+世界观展开+5-8章小闭环',
    '立势': '小角色→有分量：大舞台+强对手+团队建立',
    '立威': '有分量→威名：组织级冲突+感情推进+信念考验',
    '立命': '威名→蜕变：终极挑战+伏笔收束+续作种子',
  };
  const ARC_RATIOS = [0.05, 0.2, 0.25, 0.25, 0.25]; // 五幕占比

  // 爽点类型库
  const COOL_TYPES = ['实力碾压', '打脸装逼', '升级蜕变', '守护爆发', '荒诞反差', '社会认同', '信息差博弈', '扮猪吃虎'];

  // 章型配额
  const CHAPTER_TYPE_DESC = { M: '主线推进', C: '角色深挖', W: '世界观展开', D: '日常呼吸', F: '伏笔暗线' };

  function toggleVol(idx: number) {
    setCollapsedVols(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  // 获取卷列表（从chapters中筛选is_volume=true）
  const volumeChapters = chapters.filter(c => c.is_volume);

  // 解析timeline数据
  useEffect(() => {
    if (!bible?.timeline) { setVolumes([]); return; }
    // 容错：剥离 markdown 代码块包裹（```json ... ```），与后端/AiCreateModal 清理逻辑一致
    let raw = bible.timeline.trim();
    const fence = raw.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
    if (fence) raw = fence[1].trim();
    try {
      let parsed = JSON.parse(raw);
      // 容错：解包 {volumes:[...]} / {data:[...]} 等包装对象
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        for (const k of ['volumes', 'data', 'result', 'items', 'list']) {
          if (Array.isArray((parsed as any)[k])) { parsed = (parsed as any)[k]; break; }
        }
      }
      if (Array.isArray(parsed)) { setVolumes(parsed); return; }
    } catch { /* not JSON */ }
    // 纯文本模式：作为整体大纲
    if (bible.timeline.trim()) {
      setVolumes([{ volume: '全部剧情', main_plot: bible.timeline.trim(), volume_id: '' }]);
    } else {
      setVolumes([]);
    }
  }, [bible?.timeline]);

  async function saveVolumes(newVols: any[]) {
    setVolumes(newVols);
    try {
      const updated = await api.updateBible(bookId, { timeline: JSON.stringify(newVols, null, 2) } as any);
      onBibleUpdate(updated);
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
  }

  function startEditVol(volId: string, vol: any) {
    setEditingVol(volId);
    // 数组字段转为换行分隔的文本，便于在 textarea 中编辑
    const arrToText = (arr: any) => Array.isArray(arr) ? arr.join('\n') : (arr || '');
    setEditForm({
      main_plot: vol?.main_plot || vol?.core_goal || '',
      core_conflict: vol?.core_conflict || '',
      emotion_driver: vol?.emotion_driver || '',
      key_events: arrToText(vol?.key_events),
      turning_points: arrToText(vol?.turning_points),
      climax: vol?.climax || '',
      ending: vol?.ending || '',
      foreshadowing: arrToText(vol?.foreshadowing),
      foreshadow_recycle: arrToText(vol?.foreshadow_recycle),
    });
  }

  // ==== 节点单独编辑 ====
  const [editingNode, setEditingNode] = useState<{ volId: string; nodeIdx: number } | null>(null);
  const [editNodeForm, setEditNodeForm] = useState<any>({});
  function startEditNode(volId: string, nodeIdx: number, node: any) {
    setEditingNode({ volId, nodeIdx });
    setEditNodeForm({
      title: node?.title || node?.coreEvent || '',
      chapters: node?.chapters || node?.chRange || '',
      type: node?.type || 'M',
      summary: node?.summary || '',
      cool_type: node?.cool_type || node?.coolType || '',
      cool_structure: node?.cool_structure || '',
      cool_contrast: node?.cool_contrast || '',
      cool_level: node?.cool_level || '',
      hook: node?.hook || '',
    });
  }
  async function saveEditNode() {
    if (!editingNode) return;
    const { volId, nodeIdx } = editingNode;
    const newVols = volumes.map(v => {
      if ((v.volume_id || '') === volId || v.volume === volId) {
        const newNodes = [...(v.nodes || [])];
        newNodes[nodeIdx] = {
          ...(newNodes[nodeIdx] || {}),
          title: editNodeForm.title || '',
          chapters: editNodeForm.chapters || '',
          type: editNodeForm.type || 'M',
          summary: editNodeForm.summary || '',
          cool_type: editNodeForm.cool_type || '',
          cool_structure: editNodeForm.cool_structure || '',
          cool_contrast: editNodeForm.cool_contrast || '',
          cool_level: editNodeForm.cool_level || '',
          hook: editNodeForm.hook || '',
        };
        return { ...v, nodes: newNodes };
      }
      return v;
    });
    await saveVolumes(newVols);
    setEditingNode(null);
  }
  async function deleteEditNode() {
    if (!editingNode) return;
    const { volId, nodeIdx } = editingNode;
    showConfirm('确定删除此情节节点？', async () => {
      const newVols = volumes.map(v => {
        if ((v.volume_id || '') === volId || v.volume === volId) {
          const newNodes = [...(v.nodes || [])];
          newNodes.splice(nodeIdx, 1);
          return { ...v, nodes: newNodes };
        }
        return v;
      });
      await saveVolumes(newVols);
      setEditingNode(null);
    });
  }

  async function saveEditVol(volId: string) {
    // 文本转数组：按换行切分，去空，去重
    const textToArr = (text: string) => (text || '').split('\n').map(s => s.trim()).filter(Boolean);
    const newVols = volumes.map(v => {
      if ((v.volume_id || '') === volId || v.volume === volId) {
        return {
          ...v,
          main_plot: editForm.main_plot || '',
          core_conflict: editForm.core_conflict || '',
          emotion_driver: editForm.emotion_driver || '',
          key_events: textToArr(editForm.key_events),
          turning_points: textToArr(editForm.turning_points),
          climax: editForm.climax || '',
          ending: editForm.ending || '',
          foreshadowing: textToArr(editForm.foreshadowing),
          foreshadow_recycle: textToArr(editForm.foreshadow_recycle),
        };
      }
      return v;
    });
    await saveVolumes(newVols);
    setEditingVol(null);
  }

  async function addVolumeOutline() {
    const volName = `第${volumes.length + 1}卷`;
    const newVol = { volume: volName, volume_id: '', main_plot: '', key_events: [], turning_points: [], climax: '', ending: '', foreshadowing: [] };
    await saveVolumes([...volumes, newVol]);
    setEditingVol(volName);
    setEditForm({ main_plot: '', core_conflict: '', emotion_driver: '', key_events: '', turning_points: '', climax: '', ending: '', foreshadowing: '', foreshadow_recycle: '' });
  }

  async function deleteVolume(idx: number) {
    const vol = displayVolumes[idx];
    if (!vol) return;
    const volId = vol.volume_id || '';
    const volName = vol.volume || '';
    showConfirm(`确定删除「${volName || '该卷'}」的剧情？`, async () => {
      // 按 volume_id / volume 精确匹配删除，避免 displayVolumes 与 volumes 下标错位导致删错卷
      let newVols = volumes.filter((v: any) => {
        const vId = v.volume_id || '';
        const vName = v.volume || '';
        // 命中条件：id 相同（且非空）或 名称相同
        if (volId && vId === volId) return false;
        if (volName && vName === volName) return false;
        return true;
      });
      // 若精确匹配后未删掉任何项（说明该卷只存在于 chapters 表的 is_volume 行），
      // 回退到按下标删除 volumes[idx]
      let removedFromTimeline = newVols.length < volumes.length;
      if (!removedFromTimeline && idx < volumes.length) {
        newVols = volumes.filter((_, i) => i !== idx);
        removedFromTimeline = newVols.length < volumes.length;
      }
      await saveVolumes(newVols);

      // 若该卷来自 chapters 表（有 volume_id 对应 is_volume 章节），也一并删除该卷章节
      if (volId) {
        const vc = volumeChapters.find(c => c.id === volId);
        if (vc) {
          try {
            await api.deleteChapter(bookId, volId);
            onRefreshChapters();
          } catch (e: any) {
            // 章节删除失败不阻断，timeline 已删除
            console.warn('删除卷章节失败:', e.message);
          }
        }
      }
    });
  }

  // 一键清空全部分卷大纲（仅清空 timeline，不影响章节表）
  async function handleClearAllVolumes() {
    showConfirm('确定一键清空全部分卷大纲？此操作仅清空剧情维度的分卷数据，不影响章节表和大纲总纲。', async () => {
      setClearing(true);
      try {
        const updated = await api.clearTimeline(bookId);
        if (updated.bible) onBibleUpdate(updated.bible);
        alert('已清空全部分卷大纲');
      } catch (e: any) {
        alert('清空失败：' + (e.message || '请重试'));
      }
      setClearing(false);
    });
  }

  // AI识别指定卷剧情（数据源：设定+大纲+人物+规则+章节+动态文件）
  async function handleAnalyzeVolume(volId: string, volTitle: string) {
    showConfirm(`将用 AI 综合分析「设定/大纲/人物/规则/章节/动态文件」，识别「${volTitle}」的剧情和情节节点。是否继续？`, async () => {
      setAnalyzingVol(volId || volTitle);
      try {
        const result = await api.analyzePlotVolume(bookId, volId, volTitle, selectedSkillPackIds);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已填充「${volTitle}」的剧情和情节节点`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingVol('');
    });
  }

  // AI情节节点设计：基于总纲(若有)+卷剧情+设定，为指定卷生成5-8个情节节点（非强制，无总纲也能用）
  const [nodeDesigning, setNodeDesigning] = useState<string>('');
  async function handleDesignNodes(volId: string, volTitle: string, volIndex: number) {
    // 节点设计模式：若本卷已有卷剧情（main_plot 等），仅细化 nodes 不覆盖卷级字段
    const targetVol = volumes.find((v: any) => (v.volume_id || '') === volId || v.volume === volTitle);
    const hasVolPlot = !!(targetVol && (targetVol.main_plot || targetVol.core_goal || (targetVol.key_events && targetVol.key_events.length)));
    const hint = hasVolPlot
      ? `将基于本卷「${volTitle}」已有卷剧情，详细拆分为情节节点（保留原卷主线/关键事件/转折点/高潮/结局/伏笔，仅细化节点）。每个节点会配置爽点类型/结构/衬托方式和章尾钩子。是否继续？`
      : (bible?.plot_design && bible.plot_design.trim()
        ? `将基于五幕式总纲+设定+人物，为「${volTitle}」生成完整卷大纲+情节节点（含章型配额/爽点/钩子）。是否继续？`
        : `暂无五幕式总纲，将基于设定+人物，为「${volTitle}」生成完整卷大纲+情节节点。是否继续？`);
    showConfirm(hint, async () => {
      setNodeDesigning(volId || volTitle);
      try {
        const result = await api.aiOutlineVolume(bookId, volIndex, volTitle, selectedSkillPackIds, 50, hasVolPlot);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`情节节点设计完成！已为「${volTitle}」生成 ${result.volume_data?.nodes?.length || 0} 个情节节点`);
      } catch (e: any) {
        alert('情节节点设计失败：' + (e.message || '请检查AI配置'));
      }
      setNodeDesigning('');
    });
  }

  // AI协同创作
  async function executeAi() {
    if (!aiPrompt.trim()) { alert('请输入创作要求'); return; }
    setAiAssisting(true);
    setAiError('');
    try {
      const skillKeys = ['chapter_plan', 'tomato_outline'];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const contextConcept = bible?.concept || '暂无构思';
      const messages = [
        { role: 'system', content: `你是专业网文创作助手。请根据用户要求生成剧情大纲。${skillNote}` },
        { role: 'user', content: `构思：${contextConcept}\n已有大纲：${bible?.plot_design?.slice(0, 500) || '无'}\n\n用户要求：${aiPrompt}\n\n请生成按卷划分的剧情大纲，用JSON数组格式输出，每个元素包含volume(卷名)、main_plot(主线)、key_events(关键事件数组)。` },
      ];
      const result = await api.aiChat(messages);
      let newVols: any[] = [];
      try {
        const match = result.content.match(/\[[\s\S]*\]/);
        if (match) newVols = JSON.parse(match[0]);
      } catch { /* parse fail */ }
      if (newVols.length > 0) {
        await saveVolumes([...volumes, ...newVols]);
        setAiMode(false);
        setAiPrompt('');
      } else {
        setAiError('AI返回格式无法解析，请重试');
      }
    } catch (e: any) {
      setAiError(e.message || 'AI创作失败');
    }
    setAiAssisting(false);
  }

  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!aiAssisting && aiPrompt.trim()) executeAi();
    }
  };

  // ==== 大纲工作流相关函数（从大纲维度迁移） ====

  function generateVolumeBreakdown() {
    const CH_PER_VOL = 50;
    const WORDS_PER_CH = 2400;
    // 优先使用卷数输入；未填卷数时回退到字数推算
    let volCount: number;
    if (targetVolumeCount > 0) {
      volCount = targetVolumeCount;
    } else if (targetWords > 0) {
      const totalCh = Math.ceil(targetWords / WORDS_PER_CH);
      volCount = Math.ceil(totalCh / CH_PER_VOL);
    } else {
      alert('请先输入卷数（或目标字数）');
      return;
    }
    const totalCh = volCount * CH_PER_VOL;

    const data: any[] = [];
    for (let v = 0; v < volCount; v++) {
      const progress = (v + 1) / volCount;
      let arc = ARC_NAMES[0];
      for (let a = 0; a < ARC_RATIOS.length; a++) {
        if (progress <= ARC_RATIOS.slice(0, a + 1).reduce((s, r) => s + r, 0)) { arc = ARC_NAMES[a]; break; }
        if (a === ARC_RATIOS.length - 1) arc = ARC_NAMES[a];
      }
      const startCh = v * CH_PER_VOL + 1;
      const endCh = Math.min((v + 1) * CH_PER_VOL, totalCh);

      // 生成5-8个情节节点（仅框架占位，coreEvent 留空待 AI 补全）
      const nodeCount = 5 + (v % 4);
      const nodes: any[] = [];
      const nodeTypes = ['过渡', '蓄力', '高潮', '蓄力', '大高潮'];
      for (let n = 0; n < nodeCount; n++) {
        const typeIdx = Math.min(n, nodeTypes.length - 1);
        const nodeChStart = startCh + Math.floor(n * (CH_PER_VOL / nodeCount));
        const nodeChEnd = startCh + Math.floor((n + 1) * (CH_PER_VOL / nodeCount)) - 1;
        const isFirstNode = n === 0;
        const isLastNode = n === nodeCount - 1;
        nodes.push({
          index: n + 1,
          type: isFirstNode ? '过渡' : isLastNode ? '大高潮' : nodeTypes[typeIdx],
          chRange: `${nodeChStart}-${nodeChEnd}`,
          coreEvent: '',
          coolType: COOL_TYPES[(v * 7 + n * 3) % COOL_TYPES.length],
          chM: isFirstNode ? 45 : isLastNode ? 55 : 50,
          chC: isFirstNode ? 15 : isLastNode ? 5 : 10,
          chW: isFirstNode ? 5 : isLastNode ? 5 : 10,
          chD: isFirstNode ? 25 : isLastNode ? 15 : 20,
          chF: isFirstNode ? 10 : isLastNode ? 20 : 10,
          hook: '',
        });
      }

      data.push({
        index: v + 1,
        arc,
        arcTheme: ARC_THEMES[arc],
        chRange: `${startCh}-${endCh}`,
        words: (endCh - startCh + 1) * WORDS_PER_CH,
        cognChange: '',
        coreConflict: '',
        emotionDriver: '',
        boss: '',
        bossCost: '',
        foreshadowNew: 2,
        foreshadowRecycle: 1,
        hookType: ['悬念', '反转', '情感', '世界观'][v % 4],
        nodes,
      });
    }
    setVolumeData(data);
    setExpandedVol(new Set());
    setShowVolumeCalc(false);
  }

  // AI辅助生成卷大纲（逐卷补全：仅点击的卷显示补全中，其余卷仍可独立点击）
  async function aiGenerateVolumeOutline(volIdx: number) {
    if (!bookId) return;
    const vol = volumeData[volIdx];
    if (!vol) return;
    setVolumeGeneratingIdx(volIdx);
    try {
      const contextConcept = concept || bible?.concept || '暂无构思';
      // 读取各维度现有资料，主要是五幕式总纲；同时注入世界观/规则/人物保证一致性
      const existingOutline = bible?.plot_design?.slice(0, 1500) || '无';
      const worldSetting = bible?.worldbuilding?.slice(0, 600) || '无';
      const keyRules = bible?.key_rules?.slice(0, 500) || '无';
      const characters = bible?.character_profiles?.slice(0, 600) || '无';
      // 已有 timeline 各卷主线（保证卷间连贯）
      const existingVols = volumes
        .filter(v => v && v.volume !== '全部剧情')
        .map(v => `第${v.volume_index || ''}卷「${v.volume || ''}」：${(v.main_plot || v.core_goal || '').slice(0, 120)}`)
        .join('\n');
      const skillKeys = ['volume_breakdown', 'master_outline', 'tomato_outline'];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const actName = volIdx < volumeData.length ? ARC_NAMES[Math.min(Math.floor(volIdx / Math.ceil(volumeData.length / 5)), 4)] : ARC_NAMES[0];
      const msgs = [
        { role: 'system', content: `你是番茄小说金番作者。请按以下模板为第${vol.index}卷（第${vol.chRange}章，${actName}幕）填写分卷大纲。输出必须是纯JSON格式。${skillNote}\n\n【内容容量铁律】本卷固定50章约12万字，主线剧情必须足够丰满以支撑这一容量，不得过于单薄。` },
        { role: 'user', content: `【第${vol.index}卷大纲模板】
{
  "title": "卷标题（4-8字，吸引读者）",
  "cognChange": "主角从__状态→__状态（不可逆的变化）",
  "coreConflict": "本卷要解决的核心问题（一句话）",
  "emotionDriver": "让读者追读本卷的情绪：憋屈/期待/好奇/心疼",
  "boss": "比主角强1-2级+存在理由+击败需策略",
  "bossCost": "击败付出的代价/引发的后续问题",
  "foreshadowNew": 2,
  "foreshadowRecycle": 1,
  "hookType": "${['悬念', '反转', '情感', '世界观'][volIdx % 4]}",
  "nodes": [
    {"index":1,"type":"过渡","chRange":"${vol.nodes[0]?.chRange || ''}","coreEvent":"一句话核心事件","coolType":"${vol.nodes[0]?.coolType || '实力碾压'}","chM":45,"chC":15,"chW":5,"chD":25,"chF":10,"hook":"章末钩子"},
    {"index":2,"type":"蓄力","chRange":"${vol.nodes[1]?.chRange || ''}","coreEvent":"","coolType":"${vol.nodes[1]?.coolType || '荒诞反差'}","chM":50,"chC":10,"chW":10,"chD":20,"chF":10,"hook":""},
    {"index":3,"type":"高潮","chRange":"${vol.nodes[2]?.chRange || ''}","coreEvent":"","coolType":"${vol.nodes[2]?.coolType || '打脸装逼'}","chM":55,"chC":10,"chW":5,"chD":15,"chF":15,"hook":""},
    {"index":4,"type":"蓄力","chRange":"${vol.nodes[3]?.chRange || ''}","coreEvent":"","coolType":"${vol.nodes[3]?.coolType || '升级蜕变'}","chM":50,"chC":10,"chW":10,"chD":20,"chF":10,"hook":""},
    {"index":5,"type":"大高潮","chRange":"${vol.nodes[4]?.chRange || ''}","coreEvent":"","coolType":"${vol.nodes[4]?.coolType || '守护爆发'}","chM":55,"chC":5,"chW":5,"chD":15,"chF":20,"hook":"卷末大钩子"}
  ]
}

【五幕式总纲】（本卷必须符合总纲要求，不得偏离）
${existingOutline}

【已有构思】${contextConcept}
【世界观设定】${worldSetting}
【核心规则】${keyRules}
【人物档案】${characters}
【已有各卷主线】（本卷须与之连贯，不得矛盾）
${existingVols || '（暂无）'}
本卷章范围：第${vol.chRange}章，约${(vol.words / 10000).toFixed(1)}万字（50章×2400字）
所属幕：${vol.arc}（${vol.arcTheme}）

请填写完整的JSON，所有字段必填。核心冲突与主线必须足够支撑50章12万字的容量，与五幕式总纲及已有各卷保持连贯。情节节点的coreEvent要有具体内容。只输出JSON不要其他文字。` }
      ];
      const result = await api.aiChat(msgs);
      let parsed: any;
      try {
        const match = result.content.match(/\{[\s\S]*\}/);
        parsed = match ? JSON.parse(match[0]) : null;
      } catch { /* ignore */ }
      if (parsed && parsed.title) {
        const updated = [...volumeData];
        updated[volIdx] = { ...updated[volIdx], ...parsed, nodes: parsed.nodes?.length ? parsed.nodes : updated[volIdx].nodes };
        setVolumeData(updated);
      } else {
        alert('AI返回格式异常，请重试');
      }
    } catch (e: any) { alert('AI生成失败: ' + e.message); }
    setVolumeGeneratingIdx(null);
  }

  // 将当前分卷数据导出到 plot_design（通过 API 更新）
  async function exportVolumePlan() {
    if (volumeData.length === 0) { alert('请先生成分卷规划'); return; }
    const totalWords = volumeData.reduce((s, v) => s + (Number(v.words) || 0), 0);
    let text = `【分卷规划】${volumeData.length}卷 · ${volumeData.length * 50}章 · ${(totalWords / 10000).toFixed(1)}万字 · 每卷50章约12万字\n\n`;
    for (const vol of volumeData) {
      text += `━━━ 第${vol.index}卷${vol.title ? '：' + vol.title : ''} ━━━\n`;
      text += `章范围：${vol.chRange} · 约${(vol.words / 10000).toFixed(1)}万字 · ${vol.arc}幕\n`;
      if (vol.cognChange) text += `认知质变：${vol.cognChange}\n`;
      if (vol.coreConflict) text += `核心冲突：${vol.coreConflict}\n`;
      if (vol.emotionDriver) text += `情感驱动：${vol.emotionDriver}\n`;
      if (vol.boss) text += `卷BOSS：${vol.boss}\n`;
      if (vol.bossCost) text += `击败代价：${vol.bossCost}\n`;
      text += `伏笔：新埋${vol.foreshadowNew} · 回收${vol.foreshadowRecycle} · 钩子类型：${vol.hookType}\n`;
      if (vol.nodes?.length) {
        text += `  情节节点：\n`;
        for (const n of vol.nodes) {
          text += `    [节点${n.index}·${n.type}](${n.chRange})${n.coreEvent ? ' ' + n.coreEvent : ''}\n`;
          if (n.coolType) text += `      爽点：${n.coolType} · 章型：M${n.chM}% C${n.chC}% W${n.chW}% D${n.chD}% F${n.chF}%`;
          if (n.hook) text += ` · 钩子：${n.hook}`;
          text += `\n`;
        }
      }
      text += '\n';
    }
    try {
      if (!bookId) return;
      const updated = await api.updateBible(bookId, { plot_design: text } as any);
      onBibleUpdate(updated);
      alert('已将分卷规划导出到大纲（plot_design），切换到「大纲」Tab 可查看');
    } catch (e: any) {
      alert('导出失败: ' + e.message);
    }
  }

  // 从大纲总纲一次性提取各卷剧情
  // 仅构建各卷主线剧情（不生成节点），节点由用户手动点击「节点设计」生成
  async function handleExtractVolumes() {
    if (!bookId) return;
    if (!bible?.plot_design || !bible.plot_design.trim()) {
      alert('请先在大纲维度生成五幕式总纲');
      return;
    }
    setExtractLoading(true);
    try {
      const result = await api.extractVolumesFromOutline(bookId, selectedSkillPackIds, totalVolumes || undefined);
      if (result.bible) onBibleUpdate(result.bible);
      alert(`已从大纲提取 ${result.volumes?.length || 0} 卷剧情（仅主线，节点请逐卷点击「🎯 节点设计」）`);
    } catch (e: any) {
      alert('提取各卷失败：' + (e.message || '请检查AI配置'));
    }
    setExtractLoading(false);
  }

  // 导入剧情大纲文本，自动识别拆分到各卷
  async function handleImportPlotOutline() {
    if (!bookId) return;
    if (!importText.trim()) { alert('请粘贴大纲文本'); return; }
    setImportLoading(true);
    try {
      const result = await api.importPlotOutline(bookId, importText.trim(), selectedSkillPackIds);
      if (result.bible) onBibleUpdate(result.bible);
      setImportModalOpen(false);
      setImportText('');
      alert(`已导入 ${result.imported_count || 0} 卷剧情`);
    } catch (e: any) {
      alert('导入剧情大纲失败：' + (e.message || '请检查AI配置'));
    }
    setImportLoading(false);
  }

  // 反生成五幕式总纲：从各卷剧情(timeline)反向提炼，写入大纲维度(plot_design)
  const [reverseLoading, setReverseLoading] = useState(false);
  async function handleReverseGenerateOutline() {
    if (!bookId) return;
    if (!bible?.timeline || !bible.timeline.trim()) {
      alert('请先「导入剧情大纲」或「从大纲提取各卷」生成各卷剧情，再反生成总纲');
      return;
    }
    showConfirm('将根据已导入的各卷剧情，反向提炼生成五幕式总纲，并自动填入大纲维度。是否继续？', async () => {
      setReverseLoading(true);
      try {
        const result = await api.reverseGenerateOutline(bookId, selectedSkillPackIds);
        if (result.bible) onBibleUpdate(result.bible);
        alert('已反生成五幕式总纲并填入大纲维度，可切换到「大纲」Tab 查看');
      } catch (e: any) {
        alert('反生成总纲失败：' + (e.message || '请检查AI配置或先导入各卷剧情'));
      }
      setReverseLoading(false);
    });
  }

  // 合并卷列表和已有数据
  // 修复「导入分卷大纲后第1卷残留」：增强匹配，按 volume_id / volume 完整名 / 卷号 三级匹配，
  // 避免 chapters 表的「第1卷」(UUID) 与 timeline 的「第1卷」(volume_id="1") 因不匹配而重复显示。
  const displayVolumes = useMemo(() => {
    const result: any[] = [];
    const usedVolIds = new Set<string>();
    const usedVolNames = new Set<string>();
    const usedVolNums = new Set<number>();
    // 卷号提取：第X卷 / 卷X / 第X部 等开头的数字
    const extractVolNum = (s: string): number => {
      if (!s) return 0;
      const cn = '零一二三四五六七八九十';
      const m = s.match(/第?\s*([零一二三四五六七八九十百\d]+)\s*[卷部篇]/);
      if (!m) return 0;
      const raw = m[1];
      if (/^\d+$/.test(raw)) return parseInt(raw, 10);
      // 中文数字转换
      if (raw === '十') return 10;
      if (raw.startsWith('十')) return 10 + (cn.indexOf(raw[1]) >= 0 ? cn.indexOf(raw[1]) : 0);
      if (raw.endsWith('十')) return cn.indexOf(raw[0]) >= 0 ? cn.indexOf(raw[0]) * 10 : 0;
      if (raw.includes('十')) {
        const parts = raw.split('十');
        return (cn.indexOf(parts[0]) >= 0 ? cn.indexOf(parts[0]) : 0) * 10 + (cn.indexOf(parts[1]) >= 0 ? cn.indexOf(parts[1]) : 0);
      }
      let n = 0;
      for (const ch of raw) { const idx = cn.indexOf(ch); if (idx >= 0) n = n * 10 + idx; }
      return n;
    };
    // 先添加有章节的卷（来自 chapters 表 is_volume=true）
    for (const vc of volumeChapters) {
      const vcNum = extractVolNum(vc.title);
      // 三级匹配：volume_id 完全相等 → volume 名称相等 → 卷号相等
      const volData = volumes.find(v => v.volume_id === vc.id)
        || volumes.find(v => v.volume === vc.title)
        || (vcNum > 0 ? volumes.find(v => extractVolNum(v.volume) === vcNum) : undefined);
      result.push({
        volume_id: vc.id,
        volume: vc.title,
        main_plot: volData?.main_plot || '',
        key_events: volData?.key_events || [],
        turning_points: volData?.turning_points || [],
        climax: volData?.climax || '',
        ending: volData?.ending || '',
        foreshadowing: volData?.foreshadowing || [],
        nodes: volData?.nodes || [],
        chapter_count: chapters.filter(c => c.parent_id === vc.id).length,
      });
      if (volData) {
        usedVolIds.add(volData.volume_id || '');
        usedVolNames.add(volData.volume || '');
        const vn = extractVolNum(volData.volume);
        if (vn > 0) usedVolNums.add(vn);
      }
    }
    // 再添加没有对应章节卷的数据（来自 timeline，跳过已合并的）
    for (const v of volumes) {
      const id = v.volume_id || '';
      const name = v.volume || '';
      const vNum = extractVolNum(name);
      const alreadyUsed = (id && usedVolIds.has(id))
        || (name && usedVolNames.has(name))
        || (vNum > 0 && usedVolNums.has(vNum));
      if (!alreadyUsed && v.volume !== '全部剧情') {
        result.push({ ...v, chapter_count: 0 });
      }
    }
    // 如果没有卷，但有全部剧情
    if (result.length === 0 && volumes.length > 0) {
      result.push(...volumes);
    }
    // 按 volume_index / 卷号 排序，避免导入后卷前后颠倒
    result.sort((a, b) => {
      const ai = a.volume_index || extractVolNum(a.volume) || 9999;
      const bi = b.volume_index || extractVolNum(b.volume) || 9999;
      return ai - bi;
    });
    return result;
  }, [volumeChapters, volumes, chapters]);

  // 首次有卷数据时默认折叠全部卷（每次切换到该维度 tab 重新挂载，ref 重置，实现每次进入默认折叠）
  useEffect(() => {
    if (plotCollapseInitRef.current) return;
    if (displayVolumes.length > 0) {
      plotCollapseInitRef.current = true;
      setCollapsedVols(new Set(displayVolumes.map((_, idx) => idx)));
    }
  }, [displayVolumes]);

  // AI协同创作模式
  if (aiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <h3>📖 AI协同创作 · 剧情</h3>
          <button className="btn-ghost-sm" onClick={() => { setAiMode(false); setAiError(''); }} disabled={aiAssisting}>取消</button>
        </div>
        {skillPacks.length > 0 && (
          <div className="skill-pack-collapsible">
            <button className="skill-pack-toggle" onClick={() => setSkillExpanded(v => !v)} disabled={aiAssisting}>
              <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
              <span>📦 协同技能包</span>
              {selectedSkillPackIds.length > 0 && <span className="skill-pack-toggle-badge">{selectedSkillPackIds.length}</span>}
            </button>
            {skillExpanded && (
              <SkillPackGroupedList
                skillPacks={skillPacks}
                selectedSkillPackIds={selectedSkillPackIds}
                onToggleSkillPack={onToggleSkillPack}
                disabled={aiAssisting}
              />
            )}
          </div>
        )}
        <div className="ai-prompt-vertical">
          <textarea className="input bible-ai-prompt-input" rows={6} value={aiPrompt} onChange={e => setAiPrompt(e.target.value)} onKeyDown={handlePromptKeyDown} placeholder="例如：生成三卷的剧情大纲，每卷包含主线和关键事件..." disabled={aiAssisting} autoFocus />
          <div className="ai-prompt-bottom-row">
            <button className="btn-primary ai-prompt-submit" onClick={executeAi} disabled={aiAssisting || !aiPrompt.trim()}>{aiAssisting ? '⏳ 创作中...' : '🚀 发送'}</button>
          </div>
        </div>
        {aiError && <div className="error-msg" style={{marginTop:8}}>{aiError}</div>}
        {aiAssisting && <div className="bible-ai-loading"><div className="loading-spinner" /><p>AI正在生成剧情大纲...</p></div>}
      </div>
    );
  }

  return (
    <div className="bible-edit-panel">
      {/* 节点单独编辑浮层 */}
      {editingNode && (
        <div style={{position:'fixed', top:0, left:0, right:0, bottom:0, background:'rgba(0,0,0,0.5)', zIndex:1000, display:'flex', alignItems:'center', justifyContent:'center', padding:16}} onClick={() => setEditingNode(null)}>
          <div style={{background:'#fff', borderRadius:8, padding:16, maxWidth:560, width:'100%', maxHeight:'90vh', overflowY:'auto'}} onClick={e => e.stopPropagation()}>
            <h4 style={{margin:'0 0 12px', color:'#5b8def'}}>✏️ 编辑情节节点</h4>
            <div style={{display:'flex', flexDirection:'column', gap:10}}>
              <div>
                <label style={{fontSize:13, color:'#5b8def', fontWeight:600}}>节点标题</label>
                <input className="input" value={editNodeForm.title || ''} onChange={e => setEditNodeForm({...editNodeForm, title: e.target.value})} placeholder="动宾结构，如：街市遭袭反杀三名劫修" />
              </div>
              <div style={{display:'flex', gap:8}}>
                <div style={{flex:1}}>
                  <label style={{fontSize:13, color:'#5b8def', fontWeight:600}}>章节范围</label>
                  <input className="input" value={editNodeForm.chapters || ''} onChange={e => setEditNodeForm({...editNodeForm, chapters: e.target.value})} placeholder="如：1-10" />
                </div>
                <div style={{width:120}}>
                  <label style={{fontSize:13, color:'#5b8def', fontWeight:600}}>章型</label>
                  <select className="input" value={editNodeForm.type || 'M'} onChange={e => setEditNodeForm({...editNodeForm, type: e.target.value})}>
                    <option value="M">M 主线</option>
                    <option value="C">C 角色</option>
                    <option value="W">W 世界观</option>
                    <option value="D">D 日常</option>
                    <option value="F">F 伏笔</option>
                  </select>
                </div>
              </div>
              <div>
                <label style={{fontSize:13, color:'#666', fontWeight:600}}>节点剧情概要（summary）</label>
                <textarea className="input" rows={5} value={editNodeForm.summary || ''} onChange={e => setEditNodeForm({...editNodeForm, summary: e.target.value})} placeholder="起因→发展→高潮→收尾→钩子（200-400字）" />
              </div>
              <div style={{display:'flex', gap:8, flexWrap:'wrap'}}>
                <div style={{flex:1, minWidth:120}}>
                  <label style={{fontSize:13, color:'#e87d3e', fontWeight:600}}>爽点类型</label>
                  <select className="input" value={editNodeForm.cool_type || ''} onChange={e => setEditNodeForm({...editNodeForm, cool_type: e.target.value})}>
                    <option value="">—</option>
                    {COOL_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                  </select>
                </div>
                <div style={{flex:1, minWidth:120}}>
                  <label style={{fontSize:13, color:'#d97706', fontWeight:600}}>爽点结构</label>
                  <select className="input" value={editNodeForm.cool_structure || ''} onChange={e => setEditNodeForm({...editNodeForm, cool_structure: e.target.value})}>
                    <option value="">—</option>
                    <option value="先抑后扬">先抑后扬</option>
                    <option value="直接碾压">直接碾压</option>
                    <option value="默默装完逼">默默装完逼</option>
                  </select>
                </div>
                <div style={{flex:1, minWidth:120}}>
                  <label style={{fontSize:13, color:'#9b59b6', fontWeight:600}}>衬托方式</label>
                  <select className="input" value={editNodeForm.cool_contrast || ''} onChange={e => setEditNodeForm({...editNodeForm, cool_contrast: e.target.value})}>
                    <option value="">—</option>
                    <option value="旁人震惊">旁人震惊</option>
                    <option value="不敢置信">不敢置信</option>
                    <option value="事后佩服">事后佩服</option>
                  </select>
                </div>
                <div style={{flex:1, minWidth:120}}>
                  <label style={{fontSize:13, color:'#e74c3c', fontWeight:600}}>爽点层级</label>
                  <select className="input" value={editNodeForm.cool_level || ''} onChange={e => setEditNodeForm({...editNodeForm, cool_level: e.target.value})}>
                    <option value="">—</option>
                    <option value="微爽">微爽</option>
                    <option value="小爽">小爽</option>
                    <option value="中爽">中爽</option>
                    <option value="大爽">大爽</option>
                  </select>
                </div>
              </div>
              <div>
                <label style={{fontSize:13, color:'#27ae60', fontWeight:600}}>章尾钩子（hook）</label>
                <input className="input" value={editNodeForm.hook || ''} onChange={e => setEditNodeForm({...editNodeForm, hook: e.target.value})} placeholder="如：身份揭露/新危机/能力突破..." />
              </div>
              <div style={{display:'flex', gap:8, marginTop:4}}>
                <button className="btn-primary-sm" onClick={saveEditNode}>💾 保存</button>
                <button className="btn-ghost-sm" onClick={() => setEditingNode(null)}>取消</button>
                <button className="btn-ghost-sm" style={{color:'#e74c3c', marginLeft:'auto'}} onClick={deleteEditNode}>🗑️ 删除节点</button>
              </div>
            </div>
          </div>
        </div>
      )}
      <div className="bible-edit-header">
        <div className="bible-edit-actions" style={{flexShrink:0}}>
          {displayVolumes.length > 0 && (
            <button
              className="btn-ghost-sm"
              onClick={handleClearAllVolumes}
              disabled={clearing}
              title="一键清空全部分卷大纲（不影响章节表和大纲总纲）"
              style={{ color: '#e74c3c' }}
            >
              {clearing ? '⏳ 清空中...' : '🗑️ 一键清空'}
            </button>
          )}
          <button
            className="btn-ghost-sm header-collapse-btn"
            onClick={() => setWorkflowCollapsed(v => !v)}
            title={workflowCollapsed ? '展开工作流' : '折叠工作流（手机友好）'}
          >
            {workflowCollapsed ? '▾' : '▴'}
          </button>
        </div>
      </div>

      {/* 大纲工作流（从大纲维度迁移）：总纲→分卷规划→提取→导入→AI创作 全部在同一行，相互协作非强制。
          支持折叠，方便手机使用。 */}
      {!workflowCollapsed && (
        <div className="volume-calc-section" style={{ borderLeft: '3px solid #6c5ce7', paddingLeft: 10, marginBottom: 8 }}>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
            {/* 反生成五幕式总纲：从已导入的各卷剧情反向提炼，写入大纲维度 */}
            <button
              className="btn-ghost-sm"
              onClick={handleReverseGenerateOutline}
              disabled={reverseLoading || outlineWorkflowLoading !== ''}
              title="从已导入/提取的各卷剧情，反向提炼五幕式总纲，填入大纲维度"
              style={{ color: '#6c5ce7' }}
            >
              {reverseLoading ? '⏳ 反生成中...' : '🔄 反生成五幕式总纲'}
            </button>
            <button
              className="btn-ghost-sm"
              onClick={() => setShowVolumeCalc(s => !s)}
              disabled={outlineWorkflowLoading !== ''}
              title="输入卷数，按每卷50章×2400字自动生成分卷框架"
              style={showVolumeCalc ? { background: '#6c5ce7', color: '#fff' } : {}}
            >
              📊 自动分卷规划
            </button>
            <button
              className="btn-ghost-sm"
              onClick={handleExtractVolumes}
              disabled={extractLoading || outlineWorkflowLoading !== ''}
              title="从大纲总纲（五幕式/AI创作/AI识别/手编均可）一次性提取各卷剧情"
            >
              {extractLoading ? '⏳ 提取中...' : '📋 从大纲提取各卷'}
            </button>
            <button
              className="btn-ghost-sm"
              onClick={() => setImportModalOpen(true)}
              disabled={importLoading}
              title="导入剧情大纲文本，自动识别拆分到各卷"
            >
              📥 导入剧情大纲
            </button>
            <button
              className="btn-primary-sm"
              onClick={addVolumeOutline}
              title="手动添加一卷空大纲"
            >
              ＋ 添加卷大纲
            </button>
          </div>
          {/* 工作流提示：打通总纲→分卷→提取→导入→AI创作，相互反哺非强制 */}
          <div style={{ fontSize: 11, color: '#636e72', marginTop: 4, lineHeight: 1.6 }}>
            💡 工作流可任选起点、相互反哺：
            {bible?.plot_design?.trim() ? ' ✓有总纲' : ' ✗无总纲'}
            {bible?.timeline?.trim() ? ' ✓有各卷' : ' ✗无各卷'}
            <br />
            有总纲→提取各卷；有各卷→反生成总纲；无总纲→分卷规划/导入/AI创作任选；节点设计无需总纲即可用
          </div>
          {outlineWorkflowProgress && (
            <div style={{ fontSize: 12, color: '#0984e3', marginTop: 6 }}>{outlineWorkflowProgress}</div>
          )}
        </div>
      )}

      {/* 自动分卷规划表单（点击按钮后展开） */}
      {showVolumeCalc && (
        <div className="volume-calc-section" style={{ marginBottom: 8 }}>
          <div className="volume-calc-form">
            <label className="volume-calc-label">输入卷数，按每卷50章×2400字（约12万字）自动生成分卷框架</label>
            <div className="volume-calc-input-row">
              <input className="input" type="number" value={targetVolumeCount || ''} onChange={e => setTargetVolumeCount(parseInt(e.target.value) || 0)} placeholder="如：10（卷）" min={1} />
              <span className="volume-calc-unit">卷</span>
              <button className="btn-primary-sm" onClick={generateVolumeBreakdown}>生成分卷框架</button>
              <button className="btn-ghost-sm" onClick={() => setShowVolumeCalc(false)}>收起</button>
            </div>
            <p className="text-muted" style={{fontSize:11,marginTop:4}}>按金番作者体系：每卷50章×2400字≈12万字，五幕弧线自动分配。生成框架后逐卷点击 🤖 补全详情，再点击 🎯 节点设计。</p>
            {targetVolumeCount > 0 && (
              <div className="volume-calc-preview">
                预计 {targetVolumeCount}卷 · {targetVolumeCount * 50}章 · {((targetVolumeCount * 50 * 2400) / 10000).toFixed(1)}万字
              </div>
            )}
          </div>
        </div>
      )}

      {/* 导入剧情大纲弹窗 */}
      {importModalOpen && (
        <div className="volume-calc-section" style={{ marginBottom: 8 }}>
          <div className="volume-calc-form">
            <label className="volume-calc-label">粘贴大纲文本，AI 会自动识别并拆分到各卷</label>
            <textarea
              className="input"
              rows={8}
              value={importText}
              onChange={e => setImportText(e.target.value)}
              placeholder="将各卷大纲文本粘贴到这里..."
              autoFocus
            />
            <div className="volume-calc-input-row" style={{ marginTop: 8 }}>
              <button className="btn-primary-sm" onClick={handleImportPlotOutline} disabled={importLoading || !importText.trim()}>
                {importLoading ? '⏳ 导入中...' : '📥 提交导入'}
              </button>
              <button className="btn-ghost-sm" onClick={() => { setImportModalOpen(false); setImportText(''); }} disabled={importLoading}>取消</button>
            </div>
          </div>
        </div>
      )}

      {/* 分卷规划数据展示 */}
      {volumeData.length > 0 && (
        <div className="volume-plan-display">
          <div className="volume-plan-header">
            <h4>📚 分卷规划（{volumeData.length}卷 · 每卷50章约12万字）</h4>
            <div style={{display:'flex',gap:6}}>
              <button className="btn-ghost-sm" onClick={exportVolumePlan}>📝 导出到大纲</button>
            </div>
          </div>

            {volumeData.map((vol, idx) => {
              const expanded = expandedVol.has(idx);
              return (
                <div key={idx} className="volume-plan-card">
                  <div className="volume-plan-card-header" onClick={() => {
                    setExpandedVol(prev => { const n = new Set(prev); if (n.has(idx)) n.delete(idx); else n.add(idx); return n; });
                  }} style={{cursor:'pointer'}}>
                    <span className="volume-plan-arrow">{expanded ? '▼' : '▶'}</span>
                    <span className="volume-plan-vol-label">
                      第{safeText(vol.index)}卷{vol.title ? `·${safeText(vol.title)}` : ''}
                    </span>
                    <span className="volume-plan-badge">{safeText(vol.arc)}幕</span>
                    <span className="volume-plan-badge">{safeText(vol.chRange)}章</span>
                    <span className="volume-plan-badge">{(Number(vol.words) / 10000).toFixed(1)}万字</span>
                    <button className="btn-ghost-sm" onClick={e => { e.stopPropagation(); aiGenerateVolumeOutline(idx); }} disabled={volumeGeneratingIdx !== null} title="AI补全此卷详情（逐卷补全，读取五幕式总纲等各维度资料）" style={{marginLeft:'auto'}}>
                      {volumeGeneratingIdx === idx ? '⏳ 补全中' : '🤖 补全'}
                    </button>
                  </div>
                  {expanded && (
                    <div className="volume-plan-body">
                      <div className="volume-plan-grid">
                        <div className="volume-plan-field">
                          <span className="volume-plan-field-label">认知质变</span>
                          <span className="volume-plan-field-val">{safeText(vol.cognChange) || '—'}</span>
                        </div>
                        <div className="volume-plan-field">
                          <span className="volume-plan-field-label">核心冲突</span>
                          <span className="volume-plan-field-val">{safeText(vol.coreConflict) || '—'}</span>
                        </div>
                        <div className="volume-plan-field">
                          <span className="volume-plan-field-label">情感驱动</span>
                          <span className="volume-plan-field-val">{safeText(vol.emotionDriver) || '—'}</span>
                        </div>
                        <div className="volume-plan-field">
                          <span className="volume-plan-field-label">卷BOSS</span>
                          <span className="volume-plan-field-val">{safeText(vol.boss) || '—'}</span>
                        </div>
                        <div className="volume-plan-field">
                          <span className="volume-plan-field-label">击败代价</span>
                          <span className="volume-plan-field-val">{safeText(vol.bossCost) || '—'}</span>
                        </div>
                        <div className="volume-plan-field">
                          <span className="volume-plan-field-label">伏笔/钩子</span>
                          <span className="volume-plan-field-val">新埋{safeText(vol.foreshadowNew)}·回收{safeText(vol.foreshadowRecycle)}·{safeText(vol.hookType)}钩子</span>
                        </div>
                      </div>
                      {/* 情节节点 */}
                      {vol.nodes?.length > 0 && (
                        <div className="volume-plan-nodes">
                          <h5>🎯 情节节点（{vol.nodes.length}个）</h5>
                          <div className="node-list">
                            {vol.nodes.map((n: any, ni: number) => (
                              <div key={ni} className={`node-card node-type-${safeText(n.type)}`}>
                                <div className="node-card-header">
                                  <span className="node-index">{safeText(n.index)}</span>
                                  <span className="node-type-badge">{safeText(n.type)}</span>
                                  <span className="node-ch-range">{safeText(n.chRange)}章</span>
                                  <span className="node-cool-type">{safeText(n.coolType)}</span>
                                </div>
                                {n.coreEvent && <div className="node-core-event">{safeText(n.coreEvent)}</div>}
                                <div className="node-chap-breakdown">
                                  {(['M','C','W','D','F'] as const).map(t => (
                                    <span key={t} className="node-chap-type" title={`${CHAPTER_TYPE_DESC[t]}: ${(n as any)['ch'+t] || 0}%`}>
                                      {t}:{(n as any)['ch'+t] || 0}%
                                    </span>
                                  ))}
                                </div>
                                {n.hook && <div className="node-hook">🪝 {safeText(n.hook)}</div>}
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

      {displayVolumes.length === 0 ? (
        <div className="bible-empty">
          <span className="bible-empty-icon">📖</span>
          <p>暂无剧情信息</p>
          <p className="text-muted">点击「添加卷大纲」手动添加，或用AI识别自动提取</p>
          <div className="bible-empty-actions">
            <button className="btn-primary-sm" onClick={addVolumeOutline}>＋ 添加卷大纲</button>
            <button className="btn-ghost-sm" onClick={() => handleAnalyzeVolume('', '全部章节')} disabled={!hasChapters} title={hasChapters ? 'AI识别全部章节剧情' : '需要先创建章节才能AI识别'}>
              🔍 AI识别全部
            </button>
          </div>
        </div>
      ) : (
        <div className="plot-volume-list">
          {displayVolumes.map((vol, idx) => (
            <div key={idx} className="plot-volume-card">
              <div className="plot-volume-header" onClick={() => toggleVol(idx)} style={{cursor:'pointer'}}>
                <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{collapsedVols.has(idx) ? '▶' : '▼'}</span>
                {editingVolName === vol.volume ? (
                  <input
                    className="input"
                    value={editVolName}
                    onChange={e => setEditVolName(e.target.value)}
                    onBlur={async () => {
                      if (editVolName.trim() && editVolName.trim() !== vol.volume) {
                        const newVols = volumes.map((v: any) => v.volume === vol.volume ? { ...v, volume: editVolName.trim() } : v);
                        await saveVolumes(newVols);
                      }
                      setEditingVolName(null);
                    }}
                    onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
                    autoFocus
                    onClick={e => e.stopPropagation()}
                    style={{flex:1}}
                  />
                ) : (
                  <h4 onDoubleClick={() => { setEditingVolName(vol.volume); setEditVolName(vol.volume); }}>{safeText(vol.volume) || `第${idx + 1}卷`}</h4>
                )}
                {vol.chapter_count !== undefined && <span className="text-muted" style={{fontSize:12}}>{vol.chapter_count}章</span>}
                <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                  <button className="btn-ghost-sm" onClick={() => handleDesignNodes(vol.volume_id || '', vol.volume || `第${idx + 1}卷`, vol.volume_index || (idx + 1))} disabled={nodeDesigning === (vol.volume_id || vol.volume)} title="AI设计此卷情节节点">
                    {nodeDesigning === (vol.volume_id || vol.volume) ? '⏳ 节点中...' : '🎯 节点设计'}
                  </button>
                  <button className="btn-ghost-sm" onClick={() => handleAnalyzeVolume(vol.volume_id || '', vol.volume || `第${idx + 1}卷`)} disabled={analyzingVol === (vol.volume_id || vol.volume) || !hasChapters} title={hasChapters ? 'AI识别此卷剧情' : '需要先创建章节才能AI识别'}>
                    {analyzingVol === (vol.volume_id || vol.volume) ? '🤖 识别中...' : '🔍 识别'}
                  </button>
                  <button className="btn-ghost-sm" onClick={() => startEditVol(vol.volume_id || vol.volume, vol)}>✏️ 编辑</button>
                  <button className="btn-ghost-sm" onClick={() => deleteVolume(idx)} style={{color:'#e74c3c'}}>🗑️</button>
                </div>
              </div>
              {!collapsedVols.has(idx) && (editingVol === (vol.volume_id || vol.volume) ? (
                <div style={{marginTop:8, display:'flex', flexDirection:'column', gap:10}}>
                  <div>
                    <label style={{fontSize:13, color:'#5b8def', fontWeight:600}}>主线剧情（main_plot）</label>
                    <textarea className="input" rows={4} value={editForm.main_plot || ''} onChange={e => setEditForm({...editForm, main_plot: e.target.value})} placeholder="该卷主线剧情..." autoFocus />
                  </div>
                  <div>
                    <label style={{fontSize:13, color:'#5b8def', fontWeight:600}}>核心冲突（core_conflict）</label>
                    <input className="input" value={editForm.core_conflict || ''} onChange={e => setEditForm({...editForm, core_conflict: e.target.value})} placeholder="如：主角与XX势力的对立" />
                  </div>
                  <div>
                    <label style={{fontSize:13, color:'#5b8def', fontWeight:600}}>情感驱动力（emotion_driver）</label>
                    <input className="input" value={editForm.emotion_driver || ''} onChange={e => setEditForm({...editForm, emotion_driver: e.target.value})} placeholder="如：复仇/守护/求道" />
                  </div>
                  <div>
                    <label style={{fontSize:13, color:'#e87d3e', fontWeight:600}}>关键事件（每行一条）</label>
                    <textarea className="input" rows={3} value={editForm.key_events || ''} onChange={e => setEditForm({...editForm, key_events: e.target.value})} placeholder="每行一个关键事件..." />
                  </div>
                  <div>
                    <label style={{fontSize:13, color:'#e87d3e', fontWeight:600}}>转折点（每行一条）</label>
                    <textarea className="input" rows={3} value={editForm.turning_points || ''} onChange={e => setEditForm({...editForm, turning_points: e.target.value})} placeholder="每行一个转折点..." />
                  </div>
                  <div>
                    <label style={{fontSize:13, color:'#e74c3c', fontWeight:600}}>高潮（climax）</label>
                    <textarea className="input" rows={2} value={editForm.climax || ''} onChange={e => setEditForm({...editForm, climax: e.target.value})} placeholder="本卷高潮场景..." />
                  </div>
                  <div>
                    <label style={{fontSize:13, color:'#27ae60', fontWeight:600}}>结局/卷尾钩子（ending）</label>
                    <textarea className="input" rows={2} value={editForm.ending || ''} onChange={e => setEditForm({...editForm, ending: e.target.value})} placeholder="本卷结局与下一卷钩子..." />
                  </div>
                  <div>
                    <label style={{fontSize:13, color:'#9b59b6', fontWeight:600}}>新埋伏笔（每行一条）</label>
                    <textarea className="input" rows={2} value={editForm.foreshadowing || ''} onChange={e => setEditForm({...editForm, foreshadowing: e.target.value})} placeholder="每行一个新埋伏笔..." />
                  </div>
                  <div>
                    <label style={{fontSize:13, color:'#9b59b6', fontWeight:600}}>回收伏笔（每行一条）</label>
                    <textarea className="input" rows={2} value={editForm.foreshadow_recycle || ''} onChange={e => setEditForm({...editForm, foreshadow_recycle: e.target.value})} placeholder="每行一个回收伏笔..." />
                  </div>
                  <div style={{display:'flex',gap:8}}>
                    <button className="btn-primary-sm" onClick={() => saveEditVol(vol.volume_id || vol.volume)}>💾 保存</button>
                    <button className="btn-ghost-sm" onClick={() => setEditingVol(null)}>取消</button>
                  </div>
                </div>
              ) : (
                <div className="plot-volume-body">
                  {vol.main_plot ? <p>{safeText(vol.main_plot)}</p> : <p className="text-muted" style={{fontSize:13}}>暂无剧情，点击「编辑」或「识别」添加</p>}
                  {vol.core_conflict && <p><b>核心冲突：</b>{safeText(vol.core_conflict)}</p>}
                  {vol.emotion_driver && <p><b>情感驱动：</b>{safeText(vol.emotion_driver)}</p>}
                  {vol.key_events && vol.key_events.length > 0 && (
                    <div className="plot-events">
                      <b>关键事件：</b>
                      <ul>{vol.key_events.map((ev: any, i: number) => <li key={i}>{safeText(ev)}</li>)}</ul>
                    </div>
                  )}
                  {vol.turning_points && vol.turning_points.length > 0 && (
                    <div className="plot-events">
                      <b>转折点：</b>
                      <ul>{vol.turning_points.map((tp: any, i: number) => <li key={i}>{safeText(tp)}</li>)}</ul>
                    </div>
                  )}
                  {vol.climax && <p><b>高潮：</b>{safeText(vol.climax)}</p>}
                  {vol.ending && <p><b>结局：</b>{safeText(vol.ending)}</p>}
                  {vol.foreshadowing && vol.foreshadowing.length > 0 && (
                    <div className="plot-events">
                      <b>伏笔：</b>
                      <ul>{vol.foreshadowing.map((f: any, i: number) => <li key={i}>{safeText(f)}</li>)}</ul>
                    </div>
                  )}
                  {vol.foreshadow_recycle && vol.foreshadow_recycle.length > 0 && (
                    <div className="plot-events">
                      <b>回收伏笔：</b>
                      <ul>{vol.foreshadow_recycle.map((f: any, i: number) => <li key={i}>{safeText(f)}</li>)}</ul>
                    </div>
                  )}
                  {vol.nodes && vol.nodes.length > 0 && (
                    <div className="plot-events">
                      <b>情节节点（{vol.nodes.length}个）：</b>
                      <ul>
                        {vol.nodes.map((n: any, i: number) => (
                          <li key={i} style={{marginBottom:6}}>
                            <span style={{color:'#5b8def',fontWeight:600}}>[{safeText(n.type) || 'M'}]</span>{' '}
                            {n.chapters && <span style={{color:'#888'}}>{safeText(n.chapters)}章：</span>}
                            {safeText(n.title) || safeText(n.coreEvent) || '节点'}
                            {n.cool_type && <span style={{color:'#e87d3e'}}> · 爽点:{safeText(n.cool_type)}</span>}
                            {n.cool_structure && <span style={{color:'#d97706'}}> · 结构:{safeText(n.cool_structure)}</span>}
                            {n.cool_contrast && <span style={{color:'#9b59b6'}}> · 衬托:{safeText(n.cool_contrast)}</span>}
                            {n.cool_level && <span style={{color:'#e74c3c'}}> · {safeText(n.cool_level)}</span>}
                            {n.summary && <span style={{color:'#666', display:'block', marginTop:2}}> — {safeText(n.summary)}</span>}
                            {n.hook && <span style={{color:'#27ae60', display:'block'}}> 🪝 钩子:{safeText(n.hook)}</span>}
                            <button className="btn-ghost-sm" style={{marginLeft:6, padding:'0 6px', fontSize:11}} onClick={() => startEditNode(vol.volume_id || vol.volume, i, n)} title="编辑此节点">✏️</button>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ===== 物资库面板（按卷） ===== */
function InventoryPanel(props: {
  bookId: string;
  bible: BookBible | null;
  onBibleUpdate: (b: BookBible) => void;
  bookTitle: string;
  chapters: Chapter[];
  hasChapters: boolean;
  showConfirm: (message: string, onConfirm: () => void) => void;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
  onOpenAiCreate: () => void;
}) {
  const { bookId, bible, onBibleUpdate, chapters, hasChapters, showConfirm, skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks } = props;
  const [inventory, setInventory] = useState<any[]>([]);
  const [collapsedVols, setCollapsedVols] = useState<Set<number>>(new Set());
  // 每次进入维度默认折叠所有卷（tab 切换重新挂载，ref 重置）
  const invCollapseInitRef = useRef(false);
  const [analyzingVol, setAnalyzingVol] = useState('');
  const [aiMode, setAiMode] = useState(false);
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiAssisting, setAiAssisting] = useState(false);
  const [aiError, setAiError] = useState('');
  const [skillExpanded, setSkillExpanded] = useState(false);
  // 卷选择器
  const [volSelectorOpen, setVolSelectorOpen] = useState(false);
  // 按卷编辑
  const [editingVolIdx, setEditingVolIdx] = useState<number | null>(null);
  const [editVolJson, setEditVolJson] = useState('');

  // 从 chapters 表筛 is_volume 卷，作为可识别的卷列表
  const volumeChapters = chapters.filter(c => c.is_volume);

  // 解析 inventory（JSON 数组，每卷一条）
  useEffect(() => {
    if (!bible?.inventory) { setInventory([]); return; }
    try {
      const parsed = JSON.parse(bible.inventory);
      if (Array.isArray(parsed)) { setInventory(parsed); return; }
    } catch { /* not JSON */ }
    setInventory([]);
  }, [bible?.inventory]);

  async function saveInventory(newList: any[]) {
    setInventory(newList);
    try {
      const updated = await api.updateBible(bookId, { inventory: JSON.stringify(newList, null, 2) } as any);
      onBibleUpdate(updated);
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
  }

  function toggleVol(idx: number) {
    setCollapsedVols(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  // 合并卷列表：chapters.is_volume 卷 + inventory 已有卷
  const displayVolumes = useMemo(() => {
    const result: any[] = [];
    const usedIds = new Set<string>();
    // chapters 表的卷（可识别）
    for (const vc of volumeChapters) {
      const invData = inventory.find(v => v.volume_id === vc.id) || inventory.find(v => v.volume === vc.title);
      result.push({
        volume_id: vc.id,
        volume: vc.title,
        items: invData?.items || [],
        realms: invData?.realms || [],
        chapter_count: chapters.filter(c => c.parent_id === vc.id).length,
      });
      if (invData) { usedIds.add(invData.volume_id || ''); usedIds.add(invData.volume || ''); }
    }
    // inventory 已有但无对应章节卷
    for (const v of inventory) {
      const id = v.volume_id || '';
      const name = v.volume || '';
      if (!usedIds.has(id) && !usedIds.has(name)) {
        result.push({ ...v, chapter_count: 0 });
      }
    }
    return result;
  }, [volumeChapters, inventory, chapters]);

  // 首次有卷数据时默认折叠全部卷（每次切换到该维度 tab 重新挂载，ref 重置，实现每次进入默认折叠）
  useEffect(() => {
    if (invCollapseInitRef.current) return;
    if (displayVolumes.length > 0) {
      invCollapseInitRef.current = true;
      setCollapsedVols(new Set(displayVolumes.map((_, idx) => idx)));
    }
  }, [displayVolumes]);

  // AI识别指定卷的物资
  async function handleAnalyzeVolume(volId: string, volTitle: string) {
    showConfirm(`将用 AI 分析「${volTitle}」的章节内容，识别势力/角色拥有的物品、功法、法宝、境界等。是否继续？`, async () => {
      setAnalyzingVol(volId || volTitle);
      try {
        const result = await api.analyzeInventoryVolume(bookId, volId, volTitle, selectedSkillPackIds);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已为「${volTitle}」识别 ${result.volume_data?.items?.length || 0} 项物资`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingVol('');
    });
  }

  // 删除某卷的物资数据
  async function deleteVolumeInventory(idx: number) {
    const vol = displayVolumes[idx];
    if (!vol) return;
    showConfirm(`确定删除「${vol.volume || '该卷'}」的物资数据？`, async () => {
      const newList = inventory.filter((v: any) => {
        const vId = v.volume_id || '';
        const vName = v.volume || '';
        if (vol.volume_id && vId === vol.volume_id) return false;
        if (vol.volume && vName === vol.volume) return false;
        return true;
      });
      await saveInventory(newList);
    });
  }

  // 开始按卷编辑：将该卷的完整数据（items + realms）序列化为 JSON 供编辑
  function startEditVolInventory(idx: number) {
    const vol = displayVolumes[idx];
    if (!vol) return;
    const editTarget = {
      volume_id: vol.volume_id || '',
      volume: vol.volume || '',
      items: vol.items || [],
      realms: vol.realms || [],
    };
    setEditingVolIdx(idx);
    setEditVolJson(JSON.stringify(editTarget, null, 2));
    setCollapsedVols(prev => { const n = new Set(prev); n.delete(idx); return n; });
  }

  // 保存按卷编辑：解析编辑后的 JSON，写回 inventory
  async function saveEditVolInventory(idx: number) {
    try {
      const parsed = JSON.parse(editVolJson);
      const vol = displayVolumes[idx];
      const matchKey = vol.volume_id || vol.volume;
      const newList = [...inventory];
      const existIdx = newList.findIndex((v: any) => (v.volume_id || v.volume) === matchKey);
      const entry = {
        volume_id: parsed.volume_id || vol.volume_id || '',
        volume: parsed.volume || vol.volume || '',
        items: Array.isArray(parsed.items) ? parsed.items : (vol.items || []),
        realms: Array.isArray(parsed.realms) ? parsed.realms : (vol.realms || []),
      };
      if (existIdx >= 0) {
        newList[existIdx] = { ...newList[existIdx], ...entry };
      } else {
        newList.push(entry);
      }
      await saveInventory(newList);
      setEditingVolIdx(null);
      setEditVolJson('');
    } catch (e: any) {
      alert('保存失败：JSON 格式错误 - ' + e.message);
    }
  }

  // AI协同创作（生成物资数据）
  async function executeAi() {
    if (!aiPrompt.trim()) { alert('请输入创作要求'); return; }
    setAiAssisting(true);
    setAiError('');
    try {
      const skillKeys = ['lock_facts', 'tomato_setting'];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const contextConcept = bible?.concept || '暂无构思';
      const messages = [
        { role: 'system', content: `你是专业网文世界观分析师。请根据用户要求生成物资库（物品、功法、法宝、境界等）。${skillNote}` },
        { role: 'user', content: `构思：${contextConcept}\n已有物资：${bible?.inventory?.slice(0, 500) || '无'}\n\n用户要求：${aiPrompt}\n\n请生成按卷划分的物资库，用JSON数组格式输出，每个元素包含 volume(卷名)、items(物资数组，含owner/name/category/description/status)、realms(境界数组，含character/realm/progress)。` },
      ];
      const result = await api.aiChat(messages);
      let newVols: any[] = [];
      try {
        const match = result.content.match(/\[[\s\S]*\]/);
        if (match) newVols = JSON.parse(match[0]);
      } catch { /* parse fail */ }
      if (newVols.length > 0) {
        await saveInventory([...inventory, ...newVols]);
        setAiMode(false);
        setAiPrompt('');
      } else {
        setAiError('AI返回格式无法解析，请重试');
      }
    } catch (e: any) {
      setAiError(e.message || 'AI创作失败');
    }
    setAiAssisting(false);
  }

  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!aiAssisting && aiPrompt.trim()) executeAi();
    }
  };

  const CATEGORY_LABELS: Record<string, string> = {
    '物品': '📦', '功法': '📖', '法宝': '⚔️', '境界': '⚡', '灵宠': '🐾', '领地': '🗺️', '资源': '💎', '其他': '🔹',
  };

  // AI协同创作模式
  if (aiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <h3>🎒 AI协同创作 · 物资库</h3>
          <button className="btn-ghost-sm" onClick={() => { setAiMode(false); setAiError(''); }} disabled={aiAssisting}>取消</button>
        </div>
        {skillPacks.length > 0 && (
          <div className="skill-pack-collapsible">
            <button className="skill-pack-toggle" onClick={() => setSkillExpanded(v => !v)} disabled={aiAssisting}>
              <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
              <span>📦 协同技能包</span>
              {selectedSkillPackIds.length > 0 && <span className="skill-pack-toggle-badge">{selectedSkillPackIds.length}</span>}
            </button>
            {skillExpanded && (
              <SkillPackGroupedList
                skillPacks={skillPacks}
                selectedSkillPackIds={selectedSkillPackIds}
                onToggleSkillPack={onToggleSkillPack}
                disabled={aiAssisting}
              />
            )}
          </div>
        )}
        <div className="ai-prompt-vertical">
          <textarea className="input bible-ai-prompt-input" rows={6} value={aiPrompt} onChange={e => setAiPrompt(e.target.value)} onKeyDown={handlePromptKeyDown} placeholder="例如：生成三卷的物资库，每卷包含主角和主要势力的法宝、功法、境界..." disabled={aiAssisting} autoFocus />
          <div className="ai-prompt-bottom-row">
            <button className="btn-primary ai-prompt-submit" onClick={executeAi} disabled={aiAssisting || !aiPrompt.trim()}>{aiAssisting ? '⏳ 创作中...' : '🚀 发送'}</button>
          </div>
        </div>
        {aiError && <div className="error-msg" style={{marginTop:8}}>{aiError}</div>}
        {aiAssisting && <div className="bible-ai-loading"><div className="loading-spinner" /><p>AI正在生成物资库...</p></div>}
      </div>
    );
  }

  return (
    <div className="bible-edit-panel">
      <div className="bible-edit-header">
        <div className="bible-edit-actions" style={{position:'relative',flexShrink:0}}>
          {(
            <>
              <button className="btn-ghost-sm" onClick={() => setVolSelectorOpen(v => !v)} disabled={!!analyzingVol || !hasChapters} title={hasChapters ? '选择卷进行AI识别' : '需要先创建章节才能AI识别'}>
                {analyzingVol ? '🤖 识别中...' : '🔍 AI识别'}
              </button>
              {volSelectorOpen && (
                <div className="vol-selector-dropdown" style={{position:'absolute',top:'100%',right:0,marginTop:4,background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:8,padding:6,minWidth:180,zIndex:100,boxShadow:'0 4px 12px rgba(0,0,0,0.15)'}}>
                  <div style={{fontSize:12,color:'var(--text-muted)',padding:'4px 8px',borderBottom:'1px solid var(--border)',marginBottom:4}}>选择要识别的卷</div>
                  <button className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeVolume('', '全部章节'); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📚 全部章节</button>
                  {displayVolumes.map((vol, idx) => (
                    <button key={idx} className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeVolume(vol.volume_id || '', vol.volume || `第${idx + 1}卷`); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📖 {vol.volume || `第${idx + 1}卷`}{vol.chapter_count ? ` (${vol.chapter_count}章)` : ''}</button>
                  ))}
                  <button onClick={() => setVolSelectorOpen(false)} style={{display:'block',width:'100%',textAlign:'center',padding:'4px',background:'transparent',border:'none',cursor:'pointer',color:'var(--text-muted)',fontSize:12,marginTop:2}}>取消</button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
      <p className="text-muted" style={{fontSize:12, marginBottom:8}}>
        按卷记录主要势力和角色拥有的物品、功法、法宝、境界等。点击「🔍 AI识别」选择卷进行识别。
      </p>

      {displayVolumes.length === 0 ? (
        <div className="bible-empty">
          <span className="bible-empty-icon">🎒</span>
          <p>暂无物资信息</p>
          <p className="text-muted">先在剧情维度创建分卷，或用顶部 AI 智驾 生成物资库</p>
          <div className="bible-empty-actions">
          </div>
        </div>
      ) : (
        <div className="plot-volume-list">
          {displayVolumes.map((vol, idx) => (
            <div key={idx} className="plot-volume-card">
              <div className="plot-volume-header" onClick={() => toggleVol(idx)} style={{cursor:'pointer'}}>
                <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{collapsedVols.has(idx) ? '▶' : '▼'}</span>
                <h4>{vol.volume || `第${idx + 1}卷`}</h4>
                {vol.chapter_count !== undefined && <span className="text-muted" style={{fontSize:12}}>{vol.chapter_count}章</span>}
                <span className="text-muted" style={{fontSize:12}}>{(vol.items || []).length}项物资</span>
                <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                  {analyzingVol === (vol.volume_id || vol.volume) && <span className="text-muted" style={{fontSize:12}}>🤖 识别中...</span>}
                  <button className="btn-ghost-sm" onClick={() => editingVolIdx === idx ? (setEditingVolIdx(null), setEditVolJson('')) : startEditVolInventory(idx)} title={editingVolIdx === idx ? '取消编辑' : '编辑此卷物资数据（JSON）'}>{editingVolIdx === idx ? '取消' : '✏️'}</button>
                  {(vol.items || []).length > 0 && (
                    <button className="btn-ghost-sm" onClick={() => deleteVolumeInventory(idx)} style={{color:'#e74c3c'}} title="删除此卷物资数据">🗑️</button>
                  )}
                </div>
              </div>
              {!collapsedVols.has(idx) && (
                <div className="plot-volume-body">
                  {editingVolIdx === idx ? (
                    <div style={{marginTop:8}}>
                      <p className="text-muted" style={{fontSize:12,marginBottom:6}}>编辑本卷物资数据（JSON 格式）：items（物资清单）、realms（境界变化）。</p>
                      <textarea className="input" value={editVolJson} onChange={e => setEditVolJson(e.target.value)} rows={16} style={{fontFamily:'monospace',fontSize:12}} />
                      <div style={{display:'flex',gap:6,marginTop:8}}>
                        <button className="btn-primary-sm" onClick={() => saveEditVolInventory(idx)}>💾 保存</button>
                        <button className="btn-ghost-sm" onClick={() => { setEditingVolIdx(null); setEditVolJson(''); }}>取消</button>
                      </div>
                    </div>
                  ) : (!vol.items || vol.items.length === 0) && (!vol.realms || vol.realms.length === 0) ? (
                    <p className="text-muted" style={{fontSize:13}}>暂无物资数据，点击「🔍 AI识别」选择此卷进行识别</p>
                  ) : (
                    <>
                      {vol.items && vol.items.length > 0 && (
                        <div className="plot-events">
                          <b>物资清单（{vol.items.length}项）：</b>
                          <ul>
                            {vol.items.map((item: any, i: number) => (
                              <li key={i}>
                                <span style={{color:'#5b8def',fontWeight:600}}>{CATEGORY_LABELS[item.category] || '🔹'} {safeText(item.name)}</span>
                                {item.owner && <span style={{color:'#888'}}> · 持有：{safeText(item.owner)}</span>}
                                {item.category && <span style={{color:'#27ae60'}}> · {safeText(item.category)}</span>}
                                {item.status && <span style={{color:'#e87d3e'}}> · {safeText(item.status)}</span>}
                                {item.description && <span style={{color:'#666'}}> — {safeText(item.description)}</span>}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {vol.realms && vol.realms.length > 0 && (
                        <div className="plot-events">
                          <b>境界变化（{vol.realms.length}项）：</b>
                          <ul>
                            {vol.realms.map((r: any, i: number) => (
                              <li key={i}>
                                <span style={{color:'#9b59b6',fontWeight:600}}>{safeText(r.character)}</span>
                                {r.realm && <span style={{color:'#27ae60'}}> · {safeText(r.realm)}</span>}
                                {r.progress && <span style={{color:'#666'}}> — {safeText(r.progress)}</span>}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ===== 内容编辑面板 ===== */
function BibleEditPanel(props: {
  tab: typeof ALL_TABS[0];
  bookTitle: string;
  content: string;
  editing: boolean;
  editValue: string;
  saving: boolean;
  aiAssisting: boolean;
  aiError: string;
  bibleAiMode: boolean;
  bibleAiPrompt: string;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
  hasChapters: boolean;
  dimAnalyzing: boolean;
  onAnalyzeDimension: () => void;
  onStartEdit: () => void;
  onSaveEdit: () => void;
  onCancelEdit: () => void;
  onEditChange: (v: string) => void;
  onAIAssist: () => void;
  onExecuteAi: () => void;
  onCancelAi: () => void;
  onEditAiPrompt: (v: string) => void;
  onDelete: () => void;
  onOpenAiCreate: (field: string) => void;
}) {
  const { tab, content, editing, editValue, saving, aiAssisting, aiError, bibleAiMode, bibleAiPrompt,
    skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks,
    hasChapters, dimAnalyzing, onAnalyzeDimension,
    onStartEdit, onSaveEdit, onCancelEdit, onEditChange, onExecuteAi, onCancelAi, onEditAiPrompt, onDelete, onOpenAiCreate } = props;

  const [skillExpanded, setSkillExpanded] = useState(false);
  const [showTips, setShowTips] = useState(false);
  const selectedCount = selectedSkillPackIds.length;

  // 技能包多选器（可折叠）
  const skillSelector = skillPacks.length > 0 && (
    <div className="skill-pack-collapsible">
      <button
        className="skill-pack-toggle"
        onClick={() => setSkillExpanded(v => !v)}
        disabled={aiAssisting}
      >
        <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
        <span>📦 协同技能包</span>
        {selectedCount > 0 && <span className="skill-pack-toggle-badge">{selectedCount}</span>}
        <span className="skill-pack-toggle-hint">{skillExpanded ? '收起' : '展开'}</span>
      </button>
      {skillExpanded && (
        <>
          <SkillPackGroupedList
            skillPacks={skillPacks}
            selectedSkillPackIds={selectedSkillPackIds}
            onToggleSkillPack={onToggleSkillPack}
            disabled={aiAssisting}
          />
          {selectedSkillPacks.length > 0 && (
            <div className="skill-pack-info-list">
              {selectedSkillPacks.map(pack => (
                <div key={pack.id} className="skill-pack-info">
                  <span className="skill-pack-info-icon">{pack.icon}</span>
                  <div>
                    <div className="skill-pack-info-name">{pack.name}</div>
                    <div className="skill-pack-info-desc">{pack.description}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );

  // Enter快捷发送（Shift+Enter换行）
  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!aiAssisting && bibleAiPrompt.trim()) {
        onExecuteAi();
      }
    }
  };

  // AI协同创作模式
  if (bibleAiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <h3>{tab.icon} AI协同创作 · {tab.label}</h3>
          <button className="btn-ghost-sm" onClick={onCancelAi} disabled={aiAssisting}>取消</button>
        </div>
        {skillSelector}
        <p className="text-muted" style={{marginBottom:8}}>告诉AI你想生成什么内容，AI会结合故事设定和已勾选的技能包来创作</p>
        <div className="ai-prompt-section ai-prompt-vertical">
          <textarea
            className="input bible-ai-prompt-input"
            rows={6}
            value={bibleAiPrompt}
            onChange={e => onEditAiPrompt(e.target.value)}
            onKeyDown={handlePromptKeyDown}
            placeholder={`例如：为${tab.label}生成详细内容，包含3-5个关键要素...`}
            disabled={aiAssisting}
            autoFocus
          />
          <div className="ai-prompt-bottom-row">
            <button className="btn-primary ai-prompt-submit" onClick={onExecuteAi} disabled={aiAssisting || !bibleAiPrompt.trim()}>
              {aiAssisting ? '⏳ 创作中...' : '🚀 发送'}
            </button>
          </div>
        </div>
        {aiError && <div className="error-msg" style={{marginTop:8}}>{aiError}</div>}
        {aiAssisting && (
          <div className="bible-ai-loading">
            <div className="loading-spinner" />
            <p>AI正在结合{selectedSkillPacks.length > 0 ? selectedSkillPacks.map(p => p.name).join('、') : '设定'}生成{tab.label}内容...</p>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="bible-edit-panel">
      <div className="bible-edit-header">
        <div className="bible-edit-actions" style={{flexShrink:0}}>
          {!editing ? (
            <>
              <button className="btn-ghost-sm" onClick={onAnalyzeDimension} disabled={dimAnalyzing || !hasChapters} title={hasChapters ? 'AI分析已有章节，自动识别此维度内容' : '需要先创建章节才能AI识别'}>
                {dimAnalyzing ? '🤖 识别中...' : '🔍 AI识别'}
              </button>
              <button className="btn-primary-sm" onClick={onStartEdit}>编辑</button>
              {content && (
                <button className="btn-ghost-sm" onClick={onDelete} style={{color:'#e74c3c'}} title="清空此维度内容">🗑️</button>
              )}
            </>
          ) : (
            <>
              <button className="btn-ghost-sm" onClick={onCancelEdit}>取消</button>
              <button className="btn-primary-sm" onClick={onSaveEdit} disabled={saving}>
                {saving ? '保存中...' : '💾 保存'}
              </button>
            </>
          )}
        </div>
      </div>
      {aiError && <div className="error-msg" style={{marginBottom:8}}>{aiError}</div>}
      {editing ? (
        <>
          <textarea
            className="input bible-editor-textarea"
            rows={16}
            value={editValue}
            onChange={e => onEditChange(e.target.value)}
            placeholder={tab.placeholder}
            autoFocus
          />
          <div className="bible-edit-footer">
            <span className="bible-edit-count">{editValue.length}字符</span>
            <span className="bible-edit-hint">💡 {tab.placeholder}</span>
          </div>
        </>
      ) : content ? (
        <>
          <div className="bible-display" onClick={onStartEdit}>
            <pre className="bible-text">{content}</pre>
          </div>
          <button className="bible-tips-toggle" onClick={() => setShowTips(v => !v)}>
            {showTips ? '▼ 收起提示' : '▶ 创作提示'}
          </button>
          {showTips && (
            <div className="bible-tips-box">
              <p>✨ 点击「AI创作」可让AI根据构思和设定自动生成{tab.label}内容</p>
              <p>🔍 点击「AI识别」可从已有章节中提取{tab.label}信息（无章节时自动从设定/大纲/剧情维度提取）</p>
              <p>✏️ 点击内容区域可直接编辑</p>
              {selectedSkillPacks.length > 0 && <p>📦 已选{selectedCount}个技能包协同创作</p>}
              <p style={{marginTop:6,color:'var(--accent)',fontSize:12,borderTop:'1px dashed var(--border-color)',paddingTop:6}}>
                🔗 维度协同工作流（参考：番茄金番作者 / 长篇小说创作全流程 / 长篇小说防遗忘系统）：<br/>
                构思→设定→大纲→剧情→人物 相互反哺；大纲⇄剧情双向（提取各卷/反生成总纲）；<br/>
                各维度AI识别会读取其他维度作"已确认"上下文保持一致；<br/>
                🛡️ 伏笔面板「防遗忘检查」定期扫描一致性/伏笔/叙事债务，防长篇遗忘。
              </p>
            </div>
          )}
        </>
      ) : (
        <div className="bible-empty" onClick={onStartEdit}>
          <span className="bible-empty-icon">{tab.icon}</span>
          <p>暂无{tab.label}内容</p>
          <p className="text-muted">点击此处编辑，或使用上方按钮AI创作</p>
          <div className="bible-empty-actions">
            <button className="btn-primary-sm" onClick={(e) => { e.stopPropagation(); onOpenAiCreate(tab.field); }} disabled={aiAssisting}>
              {aiAssisting ? '⏳ 生成中...' : '✨ AI创作'}
            </button>
            <button className="btn-ghost-sm" onClick={(e) => { e.stopPropagation(); onAnalyzeDimension(); }} disabled={dimAnalyzing || !hasChapters} title={hasChapters ? 'AI分析已有章节，自动识别' : '需要先创建章节才能AI识别'}>
              {dimAnalyzing ? '⏳ 识别中...' : '🔍 AI识别'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ===== 大纲合并面板（大纲+世界观） ===== */
function OutlineCombinedPanel(props: {
  bookId: string;
  bible: BookBible | null;
  onBibleUpdate: (b: BookBible) => void;
  concept: string;
  hasChapters: boolean;
  dimAnalyzing: boolean;
  onAnalyzeDimension: (dim: string) => void;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
  showConfirm: (message: string, onConfirm: () => void) => void;
  onOpenAiCreate: (field: string) => void;
}) {
  const { bookId, bible, onBibleUpdate, concept, hasChapters, dimAnalyzing, onAnalyzeDimension, showConfirm } = props;
  const [subTab, setSubTab] = useState<'outline' | 'worldview'>('outline');
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState('');
  const [saving, setSaving] = useState(false);
  const [aiAssisting, setAiAssisting] = useState(false);
  const [aiMode, setAiMode] = useState(false);
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiError, setAiError] = useState('');
  const [skillExpanded, setSkillExpanded] = useState(false);

  const { skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks } = props;
  const selectedCount = selectedSkillPackIds.length;

  const fieldMap = { outline: 'plot_design', worldview: 'worldbuilding' } as const;
  const labelMap = { outline: '大纲', worldview: '世界观' } as const;
  const placeholderMap = {
    outline: '主线冲突、卷纲拆解、章节规划...',
    worldview: '世界背景、力量体系、社会结构、地理概况、历史脉络...',
  } as const;

  const currentField = fieldMap[subTab];
  const currentContent = bible ? (bible as any)[currentField] || '' : '';

  function startEdit() {
    setEditValue(currentContent);
    setEditing(true);
  }

  async function saveEdit() {
    if (!bookId) return;
    setSaving(true);
    try {
      const updated = await api.updateBible(bookId, { [currentField]: editValue } as any);
      onBibleUpdate(updated);
      setEditing(false);
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setSaving(false);
  }

  async function executeAi() {
    if (!bookId) return;
    if (!aiPrompt.trim()) { alert('请输入创作要求'); return; }
    setAiAssisting(true);
    setAiError('');
    try {
      const prompt = subTab === 'outline'
        ? '根据以下构思，生成故事大纲。包括：核心主线、分卷规划（每卷目标）、关键转折点、高潮设计、结局走向。'
        : '根据以下构思，生成详细的世界观设定。包括：世界背景、力量体系/科技水平、社会结构、地理概况、历史脉络。';
      const contextConcept = concept || bible?.concept || '暂无构思';
      const skillKeys = subTab === 'outline'
        ? ['master_outline', 'volume_breakdown', 'chapter_plan', 'tomato_outline']
        : ['lock_facts', 'tomato_setting'];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const messages = [
        { role: 'system', content: `你是专业网文创作助手。${skillNote}` },
        { role: 'user', content: `${prompt}\n\n构思：${contextConcept}\n\n已有内容：${currentContent.slice(0, 1000) || '无'}\n\n用户具体要求：${aiPrompt}` },
      ];
      const result = await api.aiChat(messages);
      setEditValue(result.content);
      setEditing(true);
      setAiMode(false);
    } catch (e: any) {
      setAiError(e.message || 'AI辅助失败');
    }
    setAiAssisting(false);
  }

  function handleDelete() {
    if (!bookId) return;
    showConfirm(`确定清空「${labelMap[subTab]}」的所有内容？此操作不可撤销。`, async () => {
      try {
        const updated = await api.updateBible(bookId, { [currentField]: '' } as any);
        onBibleUpdate(updated);
      } catch (e: any) {
        alert('删除失败: ' + e.message);
      }
    });
  }

  // ==== 滚动生成工作流状态 ====
  // outlineWorkflowLoading: '' | 'master' | 'volume' | 'all'
  const [outlineWorkflowLoading, setOutlineWorkflowLoading] = useState<'' | 'master' | 'volume' | 'all'>('');
  const [outlineWorkflowProgress, setOutlineWorkflowProgress] = useState('');
  // 每卷章节数（默认50，与后端约定一致）
  const CHAPTERS_PER_VOLUME = 50;

  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!aiAssisting && aiPrompt.trim()) executeAi();
    }
  };

  // 生成五幕式总纲（写入 plot_design）
  async function generateOutlineMaster() {
    if (!bookId) return;
    // 弹出输入框让用户填写预计卷数（不设上限，由用户自行决定）
    const input = window.prompt('请输入小说预计需要的卷数（≥1，不设上限）：', '10');
    if (input === null) return; // 用户取消
    const volumeCount = parseInt(input);
    if (!volumeCount || volumeCount < 1) {
      alert('卷数需为大于等于 1 的整数');
      return;
    }
    setOutlineWorkflowLoading('master');
    setOutlineWorkflowProgress(`⏳ 按五幕式生成总纲中（共 ${volumeCount} 卷）...`);
    try {
      const result = await api.aiOutlineMaster(bookId, selectedSkillPackIds, undefined, CHAPTERS_PER_VOLUME, volumeCount);
      // 把返回的 master_outline 填入大纲编辑器（设置 plotDesign state）
      const updated = await api.updateBible(bookId, { plot_design: result.master_outline } as any);
      onBibleUpdate(updated);
      setEditValue(result.master_outline);
      setEditing(true);
      setOutlineWorkflowProgress('');
      alert(`五幕式总纲已生成并填入大纲（共 ${result.volume_count} 卷）`);
    } catch (e: any) {
      alert('生成总纲失败: ' + e.message);
      setOutlineWorkflowProgress('');
    }
    setOutlineWorkflowLoading('');
  }

  // AI协同创作模式
  if (aiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <h3>📋 AI协同创作 · {labelMap[subTab]}</h3>
          <button className="btn-ghost-sm" onClick={() => { setAiMode(false); setAiError(''); }} disabled={aiAssisting}>取消</button>
        </div>
        {skillPacks.length > 0 && (
          <div className="skill-pack-collapsible">
            <button className="skill-pack-toggle" onClick={() => setSkillExpanded(v => !v)} disabled={aiAssisting}>
              <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
              <span>📦 协同技能包</span>
              {selectedCount > 0 && <span className="skill-pack-toggle-badge">{selectedCount}</span>}
              <span className="skill-pack-toggle-hint">{skillExpanded ? '收起' : '展开'}</span>
            </button>
            {skillExpanded && (
              <>
                <SkillPackGroupedList
                  skillPacks={skillPacks}
                  selectedSkillPackIds={selectedSkillPackIds}
                  onToggleSkillPack={onToggleSkillPack}
                  disabled={aiAssisting}
                />
                {selectedSkillPacks.length > 0 && (
                  <div className="skill-pack-info-list">
                    {selectedSkillPacks.map(pack => (
                      <div key={pack.id} className="skill-pack-info">
                        <span className="skill-pack-info-icon">{pack.icon}</span>
                        <div>
                          <div className="skill-pack-info-name">{pack.name}</div>
                          <div className="skill-pack-info-desc">{pack.description}</div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        )}
        <p className="text-muted" style={{marginBottom:8}}>告诉AI你想生成什么内容</p>
        <div className="ai-prompt-section ai-prompt-vertical">
          <textarea
            className="input bible-ai-prompt-input"
            rows={6}
            value={aiPrompt}
            onChange={e => setAiPrompt(e.target.value)}
            onKeyDown={handlePromptKeyDown}
            placeholder={`例如：为${labelMap[subTab]}生成详细内容...`}
            disabled={aiAssisting}
            autoFocus
          />
          <div className="ai-prompt-bottom-row">
            <button className="btn-primary ai-prompt-submit" onClick={executeAi} disabled={aiAssisting || !aiPrompt.trim()}>
              {aiAssisting ? '⏳ 创作中...' : '🚀 发送'}
            </button>
          </div>
        </div>
        {aiError && <div className="error-msg" style={{marginTop:8}}>{aiError}</div>}
        {aiAssisting && (
          <div className="bible-ai-loading">
            <div className="loading-spinner" />
            <p>AI正在生成{labelMap[subTab]}内容...</p>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="bible-edit-panel">
      <div className="bible-edit-header">
        <div className="bible-edit-actions" style={{flexShrink:0}}>
          {!editing ? (
            <>
              <button className="btn-ghost-sm" onClick={() => onAnalyzeDimension(subTab === 'outline' ? 'outline' : 'worldview')} disabled={dimAnalyzing || !hasChapters} title={hasChapters ? 'AI分析已有章节，自动识别' : '需要先创建章节才能AI识别'}>
                {dimAnalyzing ? '🤖 识别中...' : '🔍 AI识别'}
              </button>
              <button className="btn-primary-sm" onClick={startEdit}>编辑</button>
              {currentContent && (
                <button className="btn-ghost-sm" onClick={handleDelete} style={{color:'#e74c3c'}}>🗑️ 删除</button>
              )}
            </>
          ) : (
            <>
              <button className="btn-ghost-sm" onClick={() => setEditing(false)}>取消</button>
              <button className="btn-primary-sm" onClick={saveEdit} disabled={saving}>
                {saving ? '保存中...' : '保存'}
              </button>
            </>
          )}
        </div>
      </div>

      {/* 子Tab切换 */}
      <div className="outline-sub-tabs">
        <button className={`outline-sub-tab ${subTab === 'outline' ? 'active' : ''}`} onClick={() => { setSubTab('outline'); setEditing(false); }}>
          📋 大纲
        </button>
        <button className={`outline-sub-tab ${subTab === 'worldview' ? 'active' : ''}`} onClick={() => { setSubTab('worldview'); setEditing(false); }}>
          🌍 世界观
        </button>
      </div>

      {/* 五幕式总纲生成（仅大纲tab显示） */}
      {subTab === 'outline' && (
        <div className="volume-calc-section" style={{ borderLeft: '3px solid #6c5ce7', paddingLeft: 10, marginBottom: 8 }}>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            <button
              className="btn-primary-sm"
              onClick={generateOutlineMaster}
              disabled={outlineWorkflowLoading !== ''}
              title="生成五幕式总纲（写入大纲）"
            >
              {outlineWorkflowLoading === 'master' ? '⏳ 生成总纲中...' : '🎯 生成五幕式总纲'}
            </button>
          </div>
          {outlineWorkflowProgress && (
            <div style={{ fontSize: 12, color: '#0984e3', marginTop: 6 }}>{outlineWorkflowProgress}</div>
          )}
        </div>
      )}

      {editing ? (
        <textarea
          className="input bible-editor-textarea"
          rows={16}
          value={editValue}
          onChange={e => setEditValue(e.target.value)}
          placeholder={placeholderMap[subTab]}
          autoFocus
        />
      ) : currentContent ? (
        <div className="bible-display" onClick={startEdit}>
          <pre className="bible-text">{currentContent}</pre>
        </div>
      ) : (
        <div className="bible-empty" onClick={startEdit}>
          <span className="bible-empty-icon">{subTab === 'outline' ? '📋' : '🌍'}</span>
          <p>暂无{labelMap[subTab]}内容</p>
          <p className="text-muted">点击编辑或使用AI创作</p>
        </div>
      )}
    </div>
  );
}

/* ===== 动态文件面板（防遗忘摘要系统） ===== */
// 动态文件报告缓存（按 bookId），避免切换 tab 重新挂载时重复请求导致打开慢
const _dmReportsCache: Record<string, DynamicReport[]> = {};

function DynamicMemoryPanel(props: {
  bookId: string;
  concept: string;
  bible: BookBible | null;
  onBibleUpdate: (b: BookBible) => void;
  chapters: Chapter[];
  showConfirm: (message: string, onConfirm: () => void) => void;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
  onOpenAiCreate: () => void;
}) {
  const { bookId, chapters, showConfirm, selectedSkillPackIds } = props;
  const [reports, setReports] = useState<DynamicReport[]>(_dmReportsCache[bookId] || []);
  const [loading, setLoading] = useState(_dmReportsCache[bookId] ? false : true);
  const [error, setError] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editMode, setEditMode] = useState(false);
  const [editValue, setEditValue] = useState('');
  const [editTitle, setEditTitle] = useState('');
  const [editorCollapsed, setEditorCollapsed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createStart, setCreateStart] = useState<number | ''>('');
  const [createEnd, setCreateEnd] = useState<number | ''>('');
  const [batchMode, setBatchMode] = useState(false);
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const [batchDeleting, setBatchDeleting] = useState(false);
  // 按卷动态识别
  const [dynVolumes, setDynVolumes] = useState<any[]>([]);
  const [analyzingVol, setAnalyzingVol] = useState('');
  const [collapsedVolDyn, setCollapsedVolDyn] = useState<Set<number>>(new Set());
  // 每次进入维度默认折叠所有卷（tab 切换重新挂载，ref 重置）
  const dynCollapseInitRef = useRef(false);
  // 卷选择器
  const [volSelectorOpen, setVolSelectorOpen] = useState(false);
  // 按卷编辑
  const [editingVolIdx, setEditingVolIdx] = useState<number | null>(null);
  const [editVolJson, setEditVolJson] = useState('');
  // 防遗忘检查功能已迁移到伏笔面板（ForeshadowingPanel）

  const chapterCount = chapters.filter(c => !c.is_volume).length;

  // 拉取动态报告列表并更新缓存（供手动刷新场景使用）
  function refreshReports() {
    if (!bookId) return;
    api.listDynamicReports(bookId).then(data => {
      setReports(data);
      _dmReportsCache[bookId] = data;
      setLoading(false);
    }).catch(e => {
      setError(e.message || '加载失败');
      setLoading(false);
    });
  }

  useEffect(() => {
    // 有缓存：直接用缓存数据，不重复请求（避免每次切 tab 都等网络延迟导致卡顿）
    // 数据刷新依赖用户操作（自动检查/生成报告等会主动 refreshReports）
    if (_dmReportsCache[bookId]) {
      setReports(_dmReportsCache[bookId]);
      setLoading(false);
      return;
    }
    // 无缓存：显示加载态并发请求
    setLoading(true);
    refreshReports();
  }, [bookId]);

  // 自动选中第一份报告
  useEffect(() => {
    if (reports.length > 0 && !selectedId) {
      const r = reports[0];
      setSelectedId(r.id);
      setEditValue(r.content);
      setEditTitle(r.title);
    }
    if (reports.length === 0) setSelectedId(null);
  }, [reports]);

  // 解析按卷动态文件数据（dynamic_volumes）
  const { bible, onBibleUpdate } = props;
  useEffect(() => {
    if (!bible?.dynamic_volumes) { setDynVolumes([]); return; }
    try {
      const parsed = JSON.parse(bible.dynamic_volumes);
      if (Array.isArray(parsed)) { setDynVolumes(parsed); return; }
    } catch { /* not JSON */ }
    setDynVolumes([]);
  }, [bible?.dynamic_volumes]);

  // chapters 表的卷
  const volumeChapters = chapters.filter(c => c.is_volume);

  // 合并卷列表：chapters.is_volume 卷 + dynVolumes 已有卷
  const displayDynVolumes = useMemo(() => {
    const result: any[] = [];
    const usedIds = new Set<string>();
    for (const vc of volumeChapters) {
      const dvData = dynVolumes.find(v => v.volume_id === vc.id) || dynVolumes.find(v => v.volume === vc.title);
      result.push({
        volume_id: vc.id,
        volume: vc.title,
        data: dvData?.data || null,
        chapter_count: chapters.filter(c => c.parent_id === vc.id).length,
      });
      if (dvData) { usedIds.add(dvData.volume_id || ''); usedIds.add(dvData.volume || ''); }
    }
    for (const v of dynVolumes) {
      const id = v.volume_id || '';
      const name = v.volume || '';
      if (!usedIds.has(id) && !usedIds.has(name)) {
        result.push({ ...v, chapter_count: 0 });
      }
    }
    return result;
  }, [volumeChapters, dynVolumes, chapters]);

  // 首次有卷数据时默认折叠全部卷（每次切换到该维度 tab 重新挂载，ref 重置，实现每次进入默认折叠）
  useEffect(() => {
    if (dynCollapseInitRef.current) return;
    if (displayDynVolumes.length > 0) {
      dynCollapseInitRef.current = true;
      setCollapsedVolDyn(new Set(displayDynVolumes.map((_, idx) => idx)));
    }
  }, [displayDynVolumes]);

  // 【按卷分组报告】将扁平报告列表按卷归类，用于"动态文件按卷分类"展示。
  // 每个卷计算其下属章节的 order_index 范围（即 chapter_start 语义），
  // 报告 chapter_start 落在该范围则归入该卷；未归入任何卷的报告放入"未分卷"组。
  const reportsByVolume = useMemo(() => {
    type VolGroup = { key: string; title: string; reports: DynamicReport[]; collapsed: boolean };
    const groups: VolGroup[] = [];
    const used = new Set<string>();
    // 按 displayDynVolumes 顺序构建卷分组（与上方卷卡片顺序一致）
    for (const vol of displayDynVolumes) {
      const volId = vol.volume_id;
      const volTitle = vol.volume || `第${groups.length + 1}卷`;
      const childChapters = chapters.filter(c => c.parent_id === volId);
      if (!childChapters.length) continue;
      const minIdx = Math.min(...childChapters.map(c => c.order_index));
      const maxIdx = Math.max(...childChapters.map(c => c.order_index));
      // 报告 chapter_start 在 [minIdx, maxIdx] 视为属于该卷（order_index 从1开始与章号一致）
      const volReports = reports
        .filter(r => {
          const s = Number(r.chapter_start);
          return s >= minIdx && s <= maxIdx;
        })
        .sort((a, b) => Number(a.chapter_start) - Number(b.chapter_start));
      if (volReports.length === 0) continue;
      volReports.forEach(r => used.add(r.id));
      groups.push({ key: `vol-${volId}`, title: volTitle, reports: volReports, collapsed: false });
    }
    // 未归入任何卷的报告
    const orphan = reports.filter(r => !used.has(r.id)).sort((a, b) => Number(a.chapter_start) - Number(b.chapter_start));
    if (orphan.length > 0) {
      groups.push({ key: 'vol-orphan', title: '未分卷', reports: orphan, collapsed: false });
    }
    return groups;
  }, [reports, displayDynVolumes, chapters]);

  // 按卷分组折叠状态
  const [collapsedReportVols, setCollapsedReportVols] = useState<Set<string>>(new Set());
  // 首次有卷分组数据时默认折叠全部卷（每次进入维度重新挂载，ref 重置）
  const dynReportCollapseInitRef = useRef(false);
  useEffect(() => {
    if (dynReportCollapseInitRef.current) return;
    if (reportsByVolume.length > 0) {
      dynReportCollapseInitRef.current = true;
      setCollapsedReportVols(new Set(reportsByVolume.map(g => g.key)));
    }
  }, [reportsByVolume]);
  function toggleReportVol(key: string) {
    setCollapsedReportVols(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }

  function toggleVolDyn(idx: number) {
    setCollapsedVolDyn(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  // AI识别指定卷的动态文件：按5章一份批量生成动态报告
  async function handleAnalyzeDynVolume(volId: string, volTitle: string) {
    showConfirm(`将用 AI 分析「${volTitle}」的章节内容，按每5章一份自动生成该卷所有动态报告（已存在的将跳过）。是否继续？`, async () => {
      setAnalyzingVol(volId || volTitle);
      setError('');
      try {
        const result = await api.batchGenerateDynamicReports(bookId, {
          volume_id: volId,
          volume_title: volTitle,
          skill_pack_ids: selectedSkillPackIds,
          overwrite: false,
        });
        // 重新拉取报告列表（按章号排序）
        const fresh = await api.listDynamicReports(bookId);
        setReports(fresh.sort((a, b) => a.chapter_start - b.chapter_start));
        const msg = `✅ AI识别完成！\n卷「${result.volume_title}」（第${result.chapter_range[0]}-${result.chapter_range[1]}章）\n` +
          `本次生成 ${result.generated_count} 份报告，跳过已存在 ${result.skipped_count} 份` +
          (result.error_count > 0 ? `，失败 ${result.error_count} 份` : '');
        alert(msg);
      } catch (e: any) {
        setError(e.message || 'AI识别失败');
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingVol('');
    });
  }

  // P0-4: AI识别指定卷的动态摘要（人物/事件/时间/地点/势力/伏笔/境界/关系），写入 dynamic_volumes
  async function handleAnalyzeDynamicVolume(volId: string, volTitle: string) {
    showConfirm(`将用 AI 分析「${volTitle}」的章节内容，识别本卷的动态摘要（人物/事件/伏笔/关系等变化），结果写入按卷动态文件。是否继续？`, async () => {
      setAnalyzingVol(volId || volTitle);
      setError('');
      try {
        const result = await api.analyzeDynamicVolume(bookId, volId, volTitle, selectedSkillPackIds);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已为「${volTitle}」生成本卷动态摘要`);
      } catch (e: any) {
        setError(e.message || 'AI识别失败');
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingVol('');
    });
  }

  // 删除某卷的动态文件数据
  async function deleteVolumeDynamic(idx: number) {
    const vol = displayDynVolumes[idx];
    if (!vol) return;
    showConfirm(`确定删除「${vol.volume || '该卷'}」的动态文件数据？`, async () => {
      const newList = dynVolumes.filter((v: any) => {
        const vId = v.volume_id || '';
        const vName = v.volume || '';
        if (vol.volume_id && vId === vol.volume_id) return false;
        if (vol.volume && vName === vol.volume) return false;
        return true;
      });
      try {
        const updated = await api.updateBible(bookId, { dynamic_volumes: JSON.stringify(newList, null, 2) } as any);
        onBibleUpdate(updated);
      } catch (e: any) {
        alert('删除失败: ' + e.message);
      }
    });
  }

  // 开始按卷编辑：将该卷的 data 序列化为 JSON 供编辑
  function startEditVolDynamic(idx: number) {
    const vol = displayDynVolumes[idx];
    if (!vol) return;
    const editTarget = vol.data || { summary: '', characters: '', events: '', timeline: '', locations: '', factions: '', foreshadowing: '', realms: '', relationships: '' };
    setEditingVolIdx(idx);
    setEditVolJson(JSON.stringify(editTarget, null, 2));
    setCollapsedVolDyn(prev => { const n = new Set(prev); n.delete(idx); return n; });
  }

  // 保存按卷编辑：解析编辑后的 JSON，写回 dynamic_volumes
  async function saveEditVolDynamic(idx: number) {
    try {
      const parsed = JSON.parse(editVolJson);
      const vol = displayDynVolumes[idx];
      const matchKey = vol.volume_id || vol.volume;
      const newList = [...dynVolumes];
      const existIdx = newList.findIndex((v: any) => (v.volume_id || v.volume) === matchKey);
      const entry = {
        volume_id: vol.volume_id || '',
        volume: vol.volume || '',
        data: parsed,
      };
      if (existIdx >= 0) {
        newList[existIdx] = { ...newList[existIdx], ...entry };
      } else {
        newList.push(entry);
      }
      const updated = await api.updateBible(bookId, { dynamic_volumes: JSON.stringify(newList, null, 2) } as any);
      onBibleUpdate(updated);
      setEditingVolIdx(null);
      setEditVolJson('');
    } catch (e: any) {
      alert('保存失败：JSON 格式错误 - ' + e.message);
    }
  }

  const selectedReport = reports.find(r => r.id === selectedId) || null;

  function selectReport(r: DynamicReport) {
    setSelectedId(r.id);
    setEditMode(false);
    setEditValue(r.content);
    setEditTitle(r.title);
    setEditorCollapsed(false);
  }

  function startEditSelected() {
    if (!selectedReport) return;
    setEditMode(true);
    setEditValue(selectedReport.content);
    setEditTitle(selectedReport.title);
    setEditorCollapsed(false);
  }

  async function saveEdit() {
    if (!bookId || !selectedId) return;
    setSaving(true);
    try {
      const updated = await api.updateDynamicReport(bookId, selectedId, {
        content: editValue,
        title: editTitle,
      });
      setReports(prev => prev.map(r => r.id === selectedId ? updated : r));
      setEditMode(false);
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setSaving(false);
  }

  async function regenerate(r: DynamicReport) {
    if (!bookId) return;
    setGenerating(true);
    setError('');
    try {
      const updated = await api.regenerateDynamicReport(bookId, r.id);
      setReports(prev => prev.map(rep => rep.id === r.id ? updated : rep));
      if (selectedId === r.id) {
        setEditValue(updated.content);
        setEditTitle(updated.title);
      }
    } catch (e: any) {
      setError(e.message || '重新生成失败');
    }
    setGenerating(false);
  }

  function handleDelete(r: DynamicReport) {
    showConfirm(`确定删除「${r.title}」？此操作不可撤销。`, async () => {
      try {
        await api.deleteDynamicReport(bookId, r.id);
        setReports(prev => {
          const filtered = prev.filter(rep => rep.id !== r.id);
          if (selectedId === r.id && filtered.length > 0) {
            setSelectedId(filtered[0].id);
            setEditValue(filtered[0].content);
            setEditTitle(filtered[0].title);
          } else if (selectedId === r.id) {
            setSelectedId(null);
          }
          return filtered;
        });
      } catch (e: any) {
        alert('删除失败: ' + e.message);
      }
    });
  }

  function toggleChecked(id: string) {
    setCheckedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleSelectAll() {
    if (checkedIds.size === reports.length) {
      setCheckedIds(new Set());
    } else {
      setCheckedIds(new Set(reports.map(r => r.id)));
    }
  }

  function exitBatchMode() {
    setBatchMode(false);
    setCheckedIds(new Set());
  }

  function handleBatchDelete() {
    if (checkedIds.size === 0) return;
    showConfirm(`确定删除选中的 ${checkedIds.size} 份报告？此操作不可撤销。`, async () => {
      setBatchDeleting(true);
      try {
        const ids = Array.from(checkedIds);
        await api.batchDeleteDynamicReports(bookId, ids);
        const deletedSet = new Set(ids);
        setReports(prev => {
          const filtered = prev.filter(r => !deletedSet.has(r.id));
          if (selectedId && deletedSet.has(selectedId)) {
            if (filtered.length > 0) {
              setSelectedId(filtered[0].id);
              setEditValue(filtered[0].content);
              setEditTitle(filtered[0].title);
            } else {
              setSelectedId(null);
            }
          }
          return filtered;
        });
        exitBatchMode();
      } catch (e: any) {
        alert('批量删除失败: ' + e.message);
      }
      setBatchDeleting(false);
    });
  }

  async function handleCreate() {
    if (!bookId) return;
    if (createStart === '' || createEnd === '') {
      alert('请填写起始章号和结束章号');
      return;
    }
    if (createEnd < createStart) {
      alert('结束章号不能小于起始章号');
      return;
    }
    setGenerating(true);
    setError('');
    try {
      const newReport = await api.createDynamicReport(bookId, {
        chapter_start: createStart,
        chapter_end: createEnd,
      });
      setReports(prev => [...prev, newReport].sort((a, b) => a.chapter_start - b.chapter_start));
      setShowCreateModal(false);
    } catch (e: any) {
      setError(e.message || '创建失败');
    }
    setGenerating(false);
  }

  async function handleAutoCheck() {
    if (!bookId) return;
    setGenerating(true);
    setError('');
    try {
      const result = await api.autoCheckDynamicReport(bookId);
      if (result.report) {
        setReports(prev => {
          const filtered = prev.filter(r => r.id !== result.report!.id);
          return [...filtered, result.report!].sort((a, b) => a.chapter_start - b.chapter_start);
        });
      } else {
        alert('当前无需生成新报告（章节数未达到5的倍数，或该区间已有报告）');
      }
    } catch (e: any) {
      setError(e.message || '检查失败');
    }
    setGenerating(false);
  }

  if (loading) return <div className="page loading-screen"><span>加载动态文件...</span></div>;

  return (
    <div className="dm-panel">
      <div className="dm-header">
        <div className="dm-header-actions">
          <button className="btn-primary-sm" onClick={() => props.onOpenAiCreate()} title="AI创作动态文件内容">
            ✨ AI创作
          </button>
          <button className="btn-ghost-sm" onClick={handleAutoCheck} disabled={generating || batchMode} title="检查并自动生成缺失的报告">
            {generating ? '⏳ 处理中...' : '🔄 自动检查'}
          </button>
          <button
            className={batchMode ? 'btn-primary-sm' : 'btn-ghost-sm'}
            onClick={() => batchMode ? exitBatchMode() : setBatchMode(true)}
            disabled={batchDeleting || reports.length === 0}
            title="批量选择并删除报告"
          >
            {batchMode ? '✕ 退出批量' : '☑ 批量管理'}
          </button>
          <button className="btn-primary-sm" onClick={() => { setCreateStart(''); setCreateEnd(''); setShowCreateModal(true); }} disabled={generating || batchMode}>
            ＋ 生成报告
          </button>
          {(
            <div style={{position:'relative'}}>
              <button className="btn-ghost-sm" onClick={() => setVolSelectorOpen(v => !v)} disabled={!!analyzingVol || chapterCount === 0 || displayDynVolumes.length === 0} title={chapterCount === 0 ? '需要先创建章节才能AI识别' : '选择卷进行AI识别，识别结果自动归类到对应卷下'}>
                {analyzingVol ? '🤖 识别中...' : '🔍 AI识别'}
              </button>
              {volSelectorOpen && (
                <div className="vol-selector-dropdown" style={{position:'absolute',top:'100%',right:0,marginTop:4,background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:8,padding:6,minWidth:180,zIndex:100,boxShadow:'0 4px 12px rgba(0,0,0,0.15)'}}>
                  <div style={{fontSize:12,color:'var(--text-muted)',padding:'4px 8px',borderBottom:'1px solid var(--border)',marginBottom:4}}>选择要识别的卷</div>
                  <button className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeDynVolume('', '全部章节'); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📚 全部章节</button>
                  {displayDynVolumes.map((vol, idx) => (
                    <button key={idx} className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeDynVolume(vol.volume_id || '', vol.volume || `第${idx + 1}卷`); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📖 {vol.volume || `第${idx + 1}卷`}{vol.chapter_count ? ` (${vol.chapter_count}章)` : ''}</button>
                  ))}
                  <button onClick={() => setVolSelectorOpen(false)} style={{display:'block',width:'100%',textAlign:'center',padding:'4px',background:'transparent',border:'none',cursor:'pointer',color:'var(--text-muted)',fontSize:12,marginTop:2}}>取消</button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* 章节进度指示：只保留章数和报告数，移除 1-5/6-10 等 chips（下方已有可编辑报告目录） */}
      <div className="dm-progress-bar">
        <div className="dm-progress-info">
          <span>📊 已有 {chapterCount} 章 · {reports.length} 份报告</span>
          {chapterCount > 0 && (
            <span className="dm-progress-next">
              下次自动生成：第{(Math.floor(chapterCount / 5) + 1) * 5}章保存时
            </span>
          )}
        </div>
      </div>

      {error && <div className="error-msg" style={{ marginBottom: 8 }}>{error}</div>}

      {/* 按卷动态文件识别 */}
      {displayDynVolumes.length > 0 && (
        <div className="plot-volume-list" style={{marginBottom:16}}>
          <div style={{marginBottom:8}}>
            <p className="text-muted" style={{fontSize:12, margin:0}}>
              📚 点击「🔍 AI识别」选择卷，识别结果自动归类到对应卷下
            </p>
          </div>
          {displayDynVolumes.map((vol, idx) => {
            const d = vol.data || {};
            const hasData = vol.data && (d.summary || d.characters || d.events);
            return (
              <div key={idx} className="plot-volume-card">
                <div className="plot-volume-header" onClick={() => toggleVolDyn(idx)} style={{cursor:'pointer'}}>
                  <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{collapsedVolDyn.has(idx) ? '▶' : '▼'}</span>
                  <h4>{vol.volume || `第${idx + 1}卷`}</h4>
                  {vol.chapter_count !== undefined && <span className="text-muted" style={{fontSize:12}}>{vol.chapter_count}章</span>}
                  {hasData && <span className="text-muted" style={{fontSize:12}}>已识别</span>}
                  <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                    {analyzingVol === (vol.volume_id || vol.volume) && <span className="text-muted" style={{fontSize:12}}>🤖 识别中...</span>}
                    {/* P0-4: 新增"识别卷动态摘要"按钮，调用 analyzeDynamicVolume 写入 dynamic_volumes */}
                    <button className="btn-ghost-sm" onClick={() => handleAnalyzeDynamicVolume(vol.volume_id || '', vol.volume || `第${idx + 1}卷`)} disabled={!!analyzingVol} title="AI识别本卷动态摘要（人物/事件/伏笔/关系）写入按卷动态文件">📝 摘要</button>
                    <button className="btn-ghost-sm" onClick={() => editingVolIdx === idx ? (setEditingVolIdx(null), setEditVolJson('')) : startEditVolDynamic(idx)} title={editingVolIdx === idx ? '取消编辑' : '编辑此卷动态文件数据（JSON）'}>{editingVolIdx === idx ? '取消' : '✏️'}</button>
                    {hasData && (
                      <button className="btn-ghost-sm" onClick={() => deleteVolumeDynamic(idx)} style={{color:'#e74c3c'}} title="删除此卷动态文件数据">🗑️</button>
                    )}
                  </div>
                </div>
                {!collapsedVolDyn.has(idx) && (
                  <div className="plot-volume-body">
                    {editingVolIdx === idx ? (
                      <div style={{marginTop:8}}>
                        <p className="text-muted" style={{fontSize:12,marginBottom:6}}>编辑本卷动态文件数据（JSON 格式）：summary/characters/events/timeline/locations/factions/foreshadowing/realms/relationships。</p>
                        <textarea className="input" value={editVolJson} onChange={e => setEditVolJson(e.target.value)} rows={18} style={{fontFamily:'monospace',fontSize:12}} />
                        <div style={{display:'flex',gap:6,marginTop:8}}>
                          <button className="btn-primary-sm" onClick={() => saveEditVolDynamic(idx)}>💾 保存</button>
                          <button className="btn-ghost-sm" onClick={() => { setEditingVolIdx(null); setEditVolJson(''); }}>取消</button>
                        </div>
                      </div>
                    ) : !hasData ? (
                      <p className="text-muted" style={{fontSize:13}}>暂无动态文件数据，点击「🔍 AI识别」选择此卷生成摘要</p>
                    ) : (
                      <div className="plot-events">
                        {d.summary && <p><b>综合摘要：</b>{safeText(d.summary)}</p>}
                        {d.characters && <p><b>登场人物：</b>{safeText(d.characters)}</p>}
                        {d.events && <p><b>关键事件：</b>{safeText(d.events)}</p>}
                        {d.timeline && <p><b>时间线：</b>{safeText(d.timeline)}</p>}
                        {d.locations && <p><b>地点：</b>{safeText(d.locations)}</p>}
                        {d.factions && <p><b>势力动态：</b>{safeText(d.factions)}</p>}
                        {d.foreshadowing && <p><b>伏笔：</b>{safeText(d.foreshadowing)}</p>}
                        {d.realms && <p><b>境界变化：</b>{safeText(d.realms)}</p>}
                        {d.relationships && <p><b>关系变化：</b>{safeText(d.relationships)}</p>}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* 报告区域 */}
      {reports.length === 0 ? (
        <div className="bible-empty" onClick={() => { setCreateStart(''); setCreateEnd(''); setShowCreateModal(true); }}>
          <span className="bible-empty-icon">🗂️</span>
          <p>暂无动态报告</p>
          <p className="text-muted">
            {chapterCount >= 5
              ? '点击此处手动生成第一份报告'
              : `写满5章后自动生成，当前${chapterCount}章`}
          </p>
        </div>
      ) : (
        <>
          {batchMode ? (
            <>
              {/* 批量操作栏 */}
              <div className="dm-batch-bar">
                <button className="btn-ghost-sm" onClick={toggleSelectAll} disabled={batchDeleting}>
                  {checkedIds.size === reports.length && reports.length > 0 ? '取消全选' : '全选'}
                </button>
                <span className="dm-batch-count">
                  已选 {checkedIds.size}/{reports.length}
                </span>
                <button
                  className="btn-primary-sm dm-btn-danger"
                  onClick={handleBatchDelete}
                  disabled={batchDeleting || checkedIds.size === 0}
                >
                  {batchDeleting ? '⏳ 删除中...' : `🗑️ 删除选中(${checkedIds.size})`}
                </button>
              </div>
              {/* 带复选框的报告标签栏 - 按卷分类 */}
              <div className="dm-reports-by-volume">
                {reportsByVolume.map(group => {
                  const isCollapsed = collapsedReportVols.has(group.key);
                  return (
                    <div key={group.key} className="dm-volume-group">
                      <div
                        className="dm-volume-group-header"
                        onClick={() => toggleReportVol(group.key)}
                        style={{cursor:'pointer',display:'flex',alignItems:'center',gap:6,padding:'6px 4px',borderBottom:'1px solid var(--border)',marginBottom:6,userSelect:'none'}}
                      >
                        <span style={{fontSize:10,color:'var(--text-muted)'}}>{isCollapsed ? '▶' : '▼'}</span>
                        <span style={{fontWeight:600,fontSize:14}}>📖 {group.title}</span>
                        <span className="text-muted" style={{fontSize:12}}>{group.reports.length}份</span>
                      </div>
                      {!isCollapsed && (
                        <div className="dm-tab-bar dm-tab-bar-batch" style={{marginBottom:8}}>
                          {group.reports.map(r => (
                            <label
                              key={r.id}
                              className={`dm-tab-chip ${checkedIds.has(r.id) ? 'active' : ''} dm-tab-chip-checkable`}
                              title={r.title}
                            >
                              <input
                                type="checkbox"
                                checked={checkedIds.has(r.id)}
                                onChange={() => toggleChecked(r.id)}
                                disabled={batchDeleting}
                              />
                              <span className="dm-tab-chip-range">{r.chapter_start}-{r.chapter_end}</span>
                              {r.auto_generated && <span className="dm-tab-chip-badge">自</span>}
                            </label>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          ) : (
            <>
              {/* 报告列表 - 按卷分类（每卷下展示该卷的动态报告，可折叠） */}
              {reportsByVolume.length === 0 ? (
                <p className="text-muted" style={{fontSize:13,padding:'8px 0'}}>暂无动态报告，点击上方「自动检查」或「➕ 新建报告」生成。</p>
              ) : (
                <div className="dm-reports-by-volume">
                  {reportsByVolume.map(group => {
                    const isCollapsed = collapsedReportVols.has(group.key);
                    return (
                      <div key={group.key} className="dm-volume-group">
                        <div
                          className="dm-volume-group-header"
                          onClick={() => toggleReportVol(group.key)}
                          style={{cursor:'pointer',display:'flex',alignItems:'center',gap:6,padding:'6px 4px',borderBottom:'1px solid var(--border)',marginBottom:6,userSelect:'none'}}
                        >
                          <span style={{fontSize:10,color:'var(--text-muted)'}}>{isCollapsed ? '▶' : '▼'}</span>
                          <span style={{fontWeight:600,fontSize:14}}>📖 {group.title}</span>
                          <span className="text-muted" style={{fontSize:12}}>{group.reports.length}份</span>
                        </div>
                        {!isCollapsed && (
                          <div className="dm-tab-bar" style={{marginBottom:8}}>
                            {group.reports.map(r => (
                              <button
                                key={r.id}
                                className={`dm-tab-chip ${selectedId === r.id ? 'active' : ''}`}
                                onClick={() => selectReport(r)}
                                title={r.title}
                              >
                                <span className="dm-tab-chip-range">{r.chapter_start}-{r.chapter_end}</span>
                                {r.auto_generated && <span className="dm-tab-chip-badge">自</span>}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}

              {/* 折叠编辑器面板 */}
              {selectedReport && (
                <div className={`dm-editor-panel ${editorCollapsed ? 'collapsed' : ''}`}>
              <div className="dm-editor-panel-header" onClick={() => setEditorCollapsed(!editorCollapsed)}>
                <div className="dm-editor-panel-title">
                  <span className="dm-editor-toggle">{editorCollapsed ? '▶' : '▼'}</span>
                  {editMode ? (
                    <input
                      className="dm-report-title-input"
                      value={editTitle}
                      onChange={e => setEditTitle(e.target.value)}
                      onClick={e => e.stopPropagation()}
                    />
                  ) : (
                    <>
                      <span className="dm-report-icon">📄</span>
                      <span className="dm-editor-panel-name">{selectedReport.title}</span>
                      {selectedReport.auto_generated && <span className="dm-badge dm-badge-auto">自动</span>}
                    </>
                  )}
                </div>
                <div className="dm-editor-panel-actions" onClick={e => e.stopPropagation()}>
                  {editMode ? (
                    <>
                      <button className="btn-ghost-sm" onClick={() => setEditMode(false)} disabled={saving}>取消</button>
                      <button className="btn-primary-sm" onClick={saveEdit} disabled={saving}>
                        {saving ? '保存中...' : '💾 保存'}
                      </button>
                    </>
                  ) : (
                    <>
                      <button className="btn-ghost-sm" onClick={() => regenerate(selectedReport)} disabled={generating} title="AI重新生成">
                        {generating ? '⏳' : '🔄'}
                      </button>
                      <button className="btn-ghost-sm" onClick={startEditSelected} title="编辑">✏️</button>
                      <button className="btn-ghost-sm dm-btn-danger" onClick={() => handleDelete(selectedReport)} title="删除">🗑️</button>
                    </>
                  )}
                </div>
              </div>
              {!editorCollapsed && (
                <div className="dm-editor-panel-body">
                  {editMode ? (
                    <textarea
                      className="input dm-report-editor"
                      rows={12}
                      value={editValue}
                      onChange={e => setEditValue(e.target.value)}
                      autoFocus
                    />
                  ) : (
                    <div className="dm-report-content">
                      {selectedReport.content ? selectedReport.content.split('\n').map((line, i) => (
                        <p key={i}>{line}</p>
                      )) : <p className="text-muted">暂无内容</p>}
                    </div>
                  )}
                  <div className="dm-report-meta">
                    第{selectedReport.chapter_start}-{selectedReport.chapter_end}章 · {selectedReport.content.length}字
                  </div>
                </div>
              )}
            </div>
          )}
            </>
          )}
        </>
      )}

      {/* 创建报告弹窗 */}
      {showCreateModal && (
        <div className="modal-overlay" onClick={() => setShowCreateModal(false)}>
          <div className="modal-content" onClick={e => e.stopPropagation()}>
            <h3>生成动态报告</h3>
            <p className="text-muted" style={{ marginBottom: 12 }}>
              AI将分析指定范围的章节内容，自动汇总成≤500字的防遗忘报告
            </p>
            <div className="dm-create-form">
              <label>起始章号</label>
              <input
                type="number"
                min={1}
                max={chapterCount || undefined}
                value={createStart}
                placeholder="如 1"
                onChange={e => {
                  const v = e.target.value;
                  setCreateStart(v === '' ? '' : (parseInt(v) || ''));
                }}
              />
              <label>结束章号</label>
              <input
                type="number"
                min={1}
                max={chapterCount || undefined}
                value={createEnd}
                placeholder="如 5"
                onChange={e => {
                  const v = e.target.value;
                  setCreateEnd(v === '' ? '' : (parseInt(v) || ''));
                }}
              />
            </div>
            <div className="confirm-actions">
              <button className="btn-ghost-sm" onClick={() => setShowCreateModal(false)}>取消</button>
              <button className="btn-primary-sm" onClick={handleCreate} disabled={generating}>
                {generating ? '🤖 AI生成中...' : '✨ 生成'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ===== 伏笔面板（按卷） ===== */
function ForeshadowingPanel(props: {
  bookId: string;
  bible: BookBible | null;
  onBibleUpdate: (b: BookBible) => void;
  bookTitle: string;
  chapters: Chapter[];
  hasChapters: boolean;
  showConfirm: (message: string, onConfirm: () => void) => void;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  selectedSkillPacks: SkillPack[];
  onOpenAiCreate: () => void;
}) {
  const { bookId, bible, onBibleUpdate, chapters, hasChapters, showConfirm, skillPacks, selectedSkillPackIds, selectedSkillPacks } = props;
  const [foreshadowing, setForeshadowing] = useState('');
  const [foreVolumes, setForeVolumes] = useState<any[]>([]);
  const [analyzingVol, setAnalyzingVol] = useState('');
  const [volSelectorOpen, setVolSelectorOpen] = useState(false);
  const [aiMode, setAiMode] = useState(false);
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiAssisting, setAiAssisting] = useState(false);
  const [aiError, setAiError] = useState('');
  const [skillExpanded, setSkillExpanded] = useState(false);

  // ===== 防遗忘检查（迁移自动态文件面板）=====
  const [afReports, setAfReports] = useState<any[]>([]);
  const [afLoading, setAfLoading] = useState(false);
  const [afChecking, setAfChecking] = useState(false);
  const [afVolPickerOpen, setAfVolPickerOpen] = useState(false); // 分卷选择弹窗
  const [afSelectedVolIds, setAfSelectedVolIds] = useState<string[]>([]); // 多选分卷
  const [afCollapsed, setAfCollapsed] = useState<Set<string>>(new Set()); // 报告折叠状态
  const [afEditingId, setAfEditingId] = useState<string | null>(null); // 正在编辑内容的报告 id
  const [afEditValue, setAfEditValue] = useState('');
  const [afRenamingId, setAfRenamingId] = useState<string | null>(null); // 正在重命名的报告 id
  const [afRenameValue, setAfRenameValue] = useState('');
  const [afSectionOpen, setAfSectionOpen] = useState(true); // 防遗忘检查区折叠
  const [afScope, setAfScope] = useState<'reports' | 'dimensions'>('reports'); // 检查范围：动态文件/仅维度
  const [foreCollapsed, setForeCollapsed] = useState(false); // 伏笔按卷区折叠状态

  // 解析全局伏笔
  useEffect(() => {
    setForeshadowing(bible?.foreshadowing || '');
  }, [bible?.foreshadowing]);

  // 解析按卷伏笔
  useEffect(() => {
    if (!bible?.foreshadowing_volumes) { setForeVolumes([]); return; }
    try {
      const parsed = JSON.parse(bible.foreshadowing_volumes);
      if (Array.isArray(parsed)) { setForeVolumes(parsed); return; }
    } catch { /* not JSON */ }
    setForeVolumes([]);
  }, [bible?.foreshadowing_volumes]);

  const volumeChapters = chapters.filter(c => c.is_volume);

  const displayVolumes = useMemo(() => {
    const result: any[] = [];
    const usedIds = new Set<string>();
    for (const vc of volumeChapters) {
      const fvData = foreVolumes.find(v => v.volume_id === vc.id) || foreVolumes.find(v => v.volume === vc.title);
      result.push({
        volume_id: vc.id,
        volume: vc.title,
        data: fvData?.data || null,
        chapter_count: chapters.filter(c => c.parent_id === vc.id).length,
      });
      if (fvData) { usedIds.add(fvData.volume_id || ''); usedIds.add(fvData.volume || ''); }
    }
    for (const v of foreVolumes) {
      const id = v.volume_id || '';
      const name = v.volume || '';
      if (!usedIds.has(id) && !usedIds.has(name)) {
        result.push({ ...v, chapter_count: 0 });
      }
    }
    return result;
  }, [volumeChapters, foreVolumes, chapters]);

  async function saveForeshadowing(val: string) {
    try {
      const updated = await api.updateBible(bookId, { foreshadowing: val } as any);
      onBibleUpdate(updated);
      setForeshadowing(val);
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
  }

  async function handleAnalyzeVolume(volId: string, volTitle: string) {
    showConfirm(`将用 AI 分析「${volTitle}」的章节内容，识别本卷埋设/回收的伏笔。是否继续？`, async () => {
      setAnalyzingVol(volId || volTitle);
      try {
        const result = await api.analyzeForeshadowingVolume(bookId, volId, volTitle, selectedSkillPackIds);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已为「${volTitle}」生成伏笔分析`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingVol('');
    });
  }

  // ===== 防遗忘检查：加载报告列表 =====
  function sortAfReports(list: any[]): any[] {
    return [...list].sort((a, b) => (b.seq || 0) - (a.seq || 0));
  }
  function loadAfReports() {
    if (!bookId) return;
    setAfLoading(true);
    api.listAntiForgetReports(bookId).then(data => {
      setAfReports(sortAfReports(Array.isArray(data.reports) ? data.reports : []));
    }).catch(() => { setAfReports([]); })
      .finally(() => setAfLoading(false));
  }

  useEffect(() => {
    loadAfReports();
  }, [bookId]);

  // 点击「防遗忘检查」按钮：弹出分卷选择
  function openAfVolPicker() {
    setAfSelectedVolIds([]);
    setAfVolPickerOpen(true);
  }

  // 切换分卷多选
  function toggleAfVol(id: string) {
    setAfSelectedVolIds(prev => prev.includes(id) ? prev.filter(v => v !== id) : [...prev, id]);
  }

  // 执行防遗忘检查
  async function runAfCheck(volumeIds: string[]) {
    if (!hasChapters && afScope === 'reports') { alert('暂无章节内容，无法检查'); return; }
    setAfChecking(true);
    try {
      const result = await api.aiAntiForgetCheck(bookId, afScope, selectedSkillPackIds, volumeIds);
      // 刷新报告列表
      loadAfReports();
      // 自动展开新报告
      if (result.report_record?.id) {
        setAfCollapsed(prev => { const n = new Set(prev); n.delete(result.report_record.id); return n; });
        setAfSectionOpen(true);
      }
      const score = result.report?.health_score;
      const scopeName = afScope === 'dimensions' ? '仅维度' : '动态文件';
      alert(`✅ 防遗忘检查完成（${scopeName}${result.source_label ? ' · ' + result.source_label : ''}）${typeof score === 'number' ? `\n健康度评分：${score}` : ''}`);
    } catch (e: any) {
      alert('防遗忘检查失败：' + (e.message || '请检查AI配置'));
    }
    setAfChecking(false);
  }

  // 确认分卷选择后执行检查
  function confirmAfVolPicker() {
    setAfVolPickerOpen(false);
    runAfCheck(afSelectedVolIds);
  }

  function toggleAfReport(id: string) {
    setAfCollapsed(prev => { const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  }

  // 编辑报告内容
  function startAfEdit(r: any) {
    setAfEditingId(r.id);
    setAfEditValue(typeof r.report === 'string' ? r.report : JSON.stringify(r.report, null, 2));
    setAfCollapsed(prev => { const n = new Set(prev); n.delete(r.id); return n; });
  }

  async function saveAfEdit(r: any) {
    let parsed: any = afEditValue;
    try { parsed = JSON.parse(afEditValue); } catch { /* 允许纯文本保存 */ }
    try {
      const data = await api.updateAntiForgetReport(bookId, r.id, { report: parsed, summary: parsed?.summary || r.summary, health_score: parsed?.health_score ?? r.health_score });
      setAfReports(sortAfReports(Array.isArray(data.reports) ? data.reports : []));
      setAfEditingId(null);
      setAfEditValue('');
    } catch (e: any) {
      alert('保存失败：' + e.message);
    }
  }

  function startAfRename(r: any) {
    setAfRenamingId(r.id);
    setAfRenameValue(r.title || '');
  }

  async function saveAfRename(r: any) {
    const newTitle = afRenameValue.trim();
    if (!newTitle) { setAfRenamingId(null); return; }
    try {
      const data = await api.updateAntiForgetReport(bookId, r.id, { title: newTitle });
      setAfReports(sortAfReports(Array.isArray(data.reports) ? data.reports : []));
    } catch (e: any) {
      alert('重命名失败：' + e.message);
    }
    setAfRenamingId(null);
    setAfRenameValue('');
  }

  function deleteAfReport(r: any) {
    showConfirm(`确定删除检查报告「${r.title || r.id}」？此操作不可撤销。`, async () => {
      try {
        const data = await api.deleteAntiForgetReport(bookId, r.id);
        setAfReports(sortAfReports(Array.isArray(data.reports) ? data.reports : []));
      } catch (e: any) {
        alert('删除失败：' + e.message);
      }
    });
  }

  async function executeAi() {
    if (!aiPrompt.trim()) { alert('请输入创作要求'); return; }
    setAiAssisting(true);
    setAiError('');
    try {
      const skillKeys = ['foreshadow_register', 'narrative_debt'];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const messages = [
        { role: 'system', content: `你是专业网文伏笔设计师。请根据用户要求生成伏笔设计。${skillNote}` },
        { role: 'user', content: `构思：${bible?.concept || '暂无'}\n已有伏笔：${(foreshadowing || '').slice(0, 500) || '无'}\n\n用户要求：${aiPrompt}\n\n请生成伏笔设计，包括埋设时机、回收方式、关联角色。` },
      ];
      const result = await api.aiChat(messages);
      if (result.content) {
        await saveForeshadowing(result.content);
        setAiMode(false);
        setAiPrompt('');
      } else {
        setAiError('AI返回为空，请重试');
      }
    } catch (e: any) {
      setAiError(e.message || 'AI创作失败');
    }
    setAiAssisting(false);
  }

  const handlePromptKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!aiAssisting && aiPrompt.trim()) executeAi();
    }
  };

  if (aiMode) {
    return (
      <div className="bible-edit-panel">
        <div className="bible-edit-header">
          <h3>🔮 AI协同创作 · 伏笔</h3>
          <button className="btn-ghost-sm" onClick={() => { setAiMode(false); setAiError(''); }} disabled={aiAssisting}>取消</button>
        </div>
        {skillPacks.length > 0 && (
          <div className="skill-pack-collapsible">
            <button className="skill-pack-toggle" onClick={() => setSkillExpanded(v => !v)} disabled={aiAssisting}>
              <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
              <span>📦 协同技能包</span>
              {selectedSkillPackIds.length > 0 && <span className="skill-pack-toggle-badge">{selectedSkillPackIds.length}</span>}
            </button>
            {skillExpanded && (
              <SkillPackGroupedList
                skillPacks={skillPacks}
                selectedSkillPackIds={selectedSkillPackIds}
                onToggleSkillPack={() => {}}
                disabled={aiAssisting}
              />
            )}
          </div>
        )}
        <div className="ai-prompt-vertical">
          <textarea className="input bible-ai-prompt-input" rows={6} value={aiPrompt} onChange={e => setAiPrompt(e.target.value)} onKeyDown={handlePromptKeyDown} placeholder="例如：设计贯穿全书的核心伏笔，埋设3个关键悬念..." disabled={aiAssisting} autoFocus />
          <div className="ai-prompt-bottom-row">
            <button className="btn-primary ai-prompt-submit" onClick={executeAi} disabled={aiAssisting || !aiPrompt.trim()}>{aiAssisting ? '⏳ 创作中...' : '🚀 发送'}</button>
          </div>
        </div>
        {aiError && <div className="error-msg" style={{marginTop:8}}>{aiError}</div>}
        {aiAssisting && <div className="bible-ai-loading"><div className="loading-spinner" /><p>AI正在生成伏笔设计...</p></div>}
      </div>
    );
  }

  return (
    <div className="bible-edit-panel">
      <div className="bible-edit-header" style={{display:'flex',justifyContent:'space-between',alignItems:'center'}}>
        <div style={{display:'flex',alignItems:'center',gap:8,position:'relative'}}>
          {(
            <>
              <button className="btn-ghost-sm" onClick={() => setVolSelectorOpen(v => !v)} disabled={!!analyzingVol || !hasChapters} title={hasChapters ? '选择卷进行AI识别' : '需要先创建章节才能AI识别'}>
                {analyzingVol ? '🤖 识别中...' : '🔍 AI识别'}
              </button>
              {volSelectorOpen && (
                <div className="vol-selector-dropdown" style={{position:'absolute',top:'100%',left:0,marginTop:4,background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:8,padding:6,minWidth:180,zIndex:100,boxShadow:'0 4px 12px rgba(0,0,0,0.15)'}}>
                  <div style={{fontSize:12,color:'var(--text-muted)',padding:'4px 8px',borderBottom:'1px solid var(--border)',marginBottom:4}}>选择要识别的卷</div>
                  <button className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeVolume('', '全部章节'); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📚 全部章节</button>
                  {displayVolumes.map((vol, idx) => (
                    <button key={idx} className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeVolume(vol.volume_id || '', vol.volume || `第${idx + 1}卷`); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📖 {vol.volume || `第${idx + 1}卷`}{vol.chapter_count ? ` (${vol.chapter_count}章)` : ''}</button>
                  ))}
                  <button onClick={() => setVolSelectorOpen(false)} style={{display:'block',width:'100%',textAlign:'center',padding:'4px',background:'transparent',border:'none',cursor:'pointer',color:'var(--text-muted)',fontSize:12,marginTop:2}}>取消</button>
                </div>
              )}
            </>
          )}
        </div>
        <span className="text-muted" style={{fontSize:12,cursor:'pointer'}} onClick={() => setForeCollapsed(v => !v)}>
          {foreCollapsed ? '▶ 展开' : '▼ 收起'}
        </span>
      </div>
      {!foreCollapsed && (
        <>
          <p className="text-muted" style={{fontSize:12, marginBottom:8}}>
            记录伏笔的埋设时机、回收方式、关联角色。点击「🔍 AI识别」选择卷，识别结果自动归类到对应卷下。
          </p>

          {/* ===== 防遗忘检查（原全局伏笔档案位置）===== */}
      <div className="bible-edit-section" style={{marginTop:16}}>
        <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:8}}>
          <div style={{display:'flex',alignItems:'center',gap:8}}>
            <h4 style={{margin:0}}>🛡️ 防遗忘检查{afReports.length > 0 && <span className="text-muted" style={{fontSize:12,fontWeight:400}}>（{afReports.length}）</span>}</h4>
            <button
              className="btn-primary-sm"
              onClick={openAfVolPicker}
              disabled={afChecking}
              style={{fontSize:14,padding:'4px 14px'}}
            >
              {afChecking ? '⏳ 检查中...' : '🛡️ 开始检查'}
            </button>
          </div>
          <span className="text-muted" style={{fontSize:12,cursor:'pointer'}} onClick={() => setAfSectionOpen(v => !v)}>{afSectionOpen ? '▼ 收起' : '▶ 展开'}</span>
        </div>

        {afSectionOpen && (
          <>
            {/* 检查资料范围选择（动态文件 / 仅维度）*/}
            <div style={{display:'flex',alignItems:'center',gap:8,flexWrap:'wrap',marginBottom:8}}>
              <span style={{fontSize:13,color:'var(--text-muted)'}}>检查资料：</span>
              <button
                onClick={() => setAfScope('reports')}
                disabled={afChecking}
                title="检查所有动态报告"
                style={{padding:'4px 12px',fontSize:13,borderRadius:6,cursor:'pointer',border:`1px solid ${afScope==='reports'?'var(--accent)':'var(--border)'}`,background:afScope==='reports'?'var(--accent)':'transparent',color:afScope==='reports'?'#fff':'var(--text)',fontWeight:afScope==='reports'?600:400}}
              >📄 动态文件</button>
              <button
                onClick={() => setAfScope('dimensions')}
                disabled={afChecking}
                title="查阅除构思、章节外所有维度"
                style={{padding:'4px 12px',fontSize:13,borderRadius:6,cursor:'pointer',border:`1px solid ${afScope==='dimensions'?'var(--accent)':'var(--border)'}`,background:afScope==='dimensions'?'var(--accent)':'transparent',color:afScope==='dimensions'?'#fff':'var(--text)',fontWeight:afScope==='dimensions'?600:400}}
              >📐 仅维度</button>
            </div>
            <p className="text-muted" style={{fontSize:12,marginBottom:10}}>
              {afScope === 'reports'
                ? '当前：动态文件模式——检查所有动态报告，扫描一致性/伏笔/叙事债务。'
                : '当前：仅维度模式——查阅除构思、章节外所有维度。'}
              点击「开始检查」按卷选择检查范围，报告按"检查01/02..."自动命名存档，可折叠查看、编辑、重命名、删除。
            </p>

            {/* 报告列表 */}
            {afLoading ? (
              <p className="text-muted" style={{fontSize:13}}>加载报告中...</p>
            ) : afReports.length === 0 ? (
              <p className="text-muted" style={{fontSize:13}}>暂无检查报告，点击「🛡️ 开始检查」开始首次检查。</p>
            ) : (
              <div className="plot-volume-list">
                {afReports.map((r: any) => {
                  const rep = r.report || {};
                  const collapsed = afCollapsed.has(r.id);
                  const isEditing = afEditingId === r.id;
                  const isRenaming = afRenamingId === r.id;
                  const score = r.health_score ?? rep.health_score;
                  const scopeLabel = r.source_label || (r.volume_ids && r.volume_ids.length ? `指定${r.volume_ids.length}卷` : '全部章节');
                  return (
                    <div key={r.id} className="plot-volume-card">
                      <div className="plot-volume-header" style={{cursor:'pointer'}} onClick={() => !isEditing && !isRenaming && toggleAfReport(r.id)}>
                        <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{collapsed ? '▶' : '▼'}</span>
                        {isRenaming ? (
                          <input
                            type="text"
                            className="input"
                            value={afRenameValue}
                            onChange={e => setAfRenameValue(e.target.value)}
                            onClick={e => e.stopPropagation()}
                            onKeyDown={e => { if (e.key === 'Enter') saveAfRename(r); if (e.key === 'Escape') setAfRenamingId(null); }}
                            style={{flex:1,fontSize:13,padding:'2px 6px'}}
                            autoFocus
                          />
                        ) : (
                          <h4 style={{margin:0}}>{r.title || `检查${String(r.seq || 0).padStart(2,'0')}`}</h4>
                        )}
                        {typeof score === 'number' && !isRenaming && (
                          <span style={{fontSize:12,fontWeight:600,color: score >= 80 ? 'var(--success)' : score >= 60 ? 'var(--accent)' : 'var(--danger)'}}>健康度 {score}</span>
                        )}
                        {!isRenaming && !isEditing && <span className="text-muted" style={{fontSize:11}}>{scopeLabel}{r.ch_count ? ` · ${r.ch_count}章` : ''}</span>}
                        <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                          {isRenaming ? (
                            <>
                              <button className="btn-primary-sm" onClick={() => saveAfRename(r)}>💾</button>
                              <button className="btn-ghost-sm" onClick={() => setAfRenamingId(null)}>✕</button>
                            </>
                          ) : isEditing ? (
                            <>
                              <button className="btn-primary-sm" onClick={() => saveAfEdit(r)}>💾 保存</button>
                              <button className="btn-ghost-sm" onClick={() => { setAfEditingId(null); setAfEditValue(''); }}>取消</button>
                            </>
                          ) : (
                            <>
                              <button className="btn-ghost-sm" onClick={() => toggleAfReport(r.id)} title={collapsed ? '展开' : '折叠'}>{collapsed ? '📥 拉取' : '📂 折叠'}</button>
                              <button className="btn-ghost-sm" onClick={() => startAfEdit(r)} title="编辑报告内容">✏️</button>
                              <button className="btn-ghost-sm" onClick={() => startAfRename(r)} title="重命名">🏷️</button>
                              <button className="btn-ghost-sm" onClick={() => deleteAfReport(r)} style={{color:'#e74c3c'}} title="删除">🗑️</button>
                            </>
                          )}
                        </div>
                      </div>
                      {!collapsed && (
                        <div className="plot-volume-body">
                          <div className="text-muted" style={{fontSize:11,marginBottom:6}}>
                            {r.checked_at ? new Date(r.checked_at).toLocaleString('zh-CN') : ''} · 检查范围：{scopeLabel}
                          </div>
                          {isEditing ? (
                            <div>
                              <p className="text-muted" style={{fontSize:12,marginBottom:6}}>编辑报告内容（JSON 格式，保存时自动解析）：</p>
                              <textarea className="input" value={afEditValue} onChange={e => setAfEditValue(e.target.value)} rows={18} style={{fontFamily:'monospace',fontSize:12}} />
                            </div>
                          ) : (
                            <div className="plot-events">
                              {(r.summary || rep.summary) && <p style={{marginBottom:8}}><b>总览：</b>{r.summary || rep.summary}</p>}
                              {Array.isArray(rep.violations) && rep.violations.length > 0 && (
                                <div style={{marginBottom:8}}><b>⚠️ 一致性违规（{rep.violations.length}）：</b><ul>
                                  {rep.violations.map((v: any, i: number) => (
                                    <li key={i}><span style={{color:'var(--danger)',fontWeight:600}}>[{v.severity||'提示'}] {v.type||''}</span>{v.location && <span style={{color:'#888'}}> · {v.location}</span>}{v.desc && <span style={{color:'#666'}}> — {v.desc}</span>}{v.fix && <span style={{color:'var(--success)'}}> 💡{v.fix}</span>}</li>
                                  ))}
                                </ul></div>
                              )}
                              {Array.isArray(rep.pending_foreshadowing) && rep.pending_foreshadowing.length > 0 && (
                                <div style={{marginBottom:8}}><b>🔮 待回收伏笔（{rep.pending_foreshadowing.length}）：</b><ul>
                                  {rep.pending_foreshadowing.map((f: any, i: number) => (
                                    <li key={i}><span style={{color:'#e87d3e',fontWeight:600}}>{f.content||''}</span>{f.urgency && <span style={{color:'#888'}}> · {f.urgency}</span>}{f.suggest_chapter && <span style={{color:'#666'}}> — 建议回收于 {f.suggest_chapter}</span>}</li>
                                  ))}
                                </ul></div>
                              )}
                              {Array.isArray(rep.narrative_debt) && rep.narrative_debt.length > 0 && (
                                <div style={{marginBottom:8}}><b>📊 叙事债务（{rep.narrative_debt.length}）：</b><ul>
                                  {rep.narrative_debt.map((d: any, i: number) => (
                                    <li key={i}><span style={{color:'#e87d3e',fontWeight:600}}>{d.promise||''}</span>{d.status && <span style={{color:'#888'}}> · {d.status}</span>}{d.priority && <span style={{color:'#e74c3c'}}> · {d.priority}</span>}</li>
                                  ))}
                                </ul></div>
                              )}
                              {Array.isArray(rep.character_cognition_issues) && rep.character_cognition_issues.length > 0 && (
                                <div style={{marginBottom:8}}><b>👥 角色认知边界问题：</b><ul>
                                  {rep.character_cognition_issues.map((c: string, i: number) => <li key={i}>{c}</li>)}
                                </ul></div>
                              )}
                              {Array.isArray(rep.locked_facts) && rep.locked_facts.length > 0 && (
                                <div style={{marginBottom:8}}><b>🔒 锁定事实清单：</b><ul>
                                  {rep.locked_facts.map((f: string, i: number) => <li key={i}>{f}</li>)}
                                </ul></div>
                              )}
                              {Array.isArray(rep.suggestions) && rep.suggestions.length > 0 && (
                                <div><b>💡 改进建议：</b><ul>
                                  {rep.suggestions.map((s: string, i: number) => <li key={i}>{s}</li>)}
                                </ul></div>
                              )}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </>
        )}
      </div>
        </>
      )}

      {/* 防遗忘检查 · 分卷选择弹窗（单选/多选）*/}
      {afVolPickerOpen && (
        <div className="modal-overlay" onClick={() => setAfVolPickerOpen(false)}>
          <div className="modal-content" style={{maxWidth:460}} onClick={e => e.stopPropagation()}>
            <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:8}}>
              <h3 style={{margin:0}}>🛡️ 防遗忘检查 · 选择分卷</h3>
              <button className="btn-ghost-sm" onClick={() => setAfVolPickerOpen(false)}>✕</button>
            </div>
            <p className="text-muted" style={{fontSize:12,marginBottom:6}}>
              当前检查资料：<b>{afScope === 'dimensions' ? '📐 仅维度（除构思、章节外所有维度）' : '📄 动态文件（所有动态报告）'}</b>
            </p>
            <p className="text-muted" style={{fontSize:12,marginBottom:10}}>
              勾选要检查的分卷（可多选）；不勾选任何卷则检查全部章节。
            </p>
            <div style={{maxHeight:320,overflowY:'auto',border:'1px solid var(--border)',borderRadius:8,padding:6}}>
              {displayVolumes.length === 0 ? (
                <p className="text-muted" style={{fontSize:13,padding:8}}>暂无分卷，将检查全部章节。</p>
              ) : (
                <>
                  <label style={{display:'flex',alignItems:'center',gap:8,padding:'6px 8px',cursor:'pointer',borderBottom:'1px dashed var(--border)',marginBottom:4,fontWeight:600,fontSize:13}}>
                    <input
                      type="checkbox"
                      checked={afSelectedVolIds.length === 0}
                      onChange={e => { if (e.target.checked) setAfSelectedVolIds([]); }}
                    />
                    📚 全部章节
                  </label>
                  {displayVolumes.map((vol, idx) => {
                    const id = vol.volume_id || vol.volume || `vol${idx}`;
                    const checked = afSelectedVolIds.includes(id);
                    return (
                      <label key={idx} style={{display:'flex',alignItems:'center',gap:8,padding:'6px 8px',cursor:'pointer',fontSize:13}}>
                        <input type="checkbox" checked={checked} onChange={() => toggleAfVol(id)} />
                        📖 {vol.volume || `第${idx + 1}卷`}{vol.chapter_count ? ` (${vol.chapter_count}章)` : ''}
                      </label>
                    );
                  })}
                </>
              )}
            </div>
            <div className="confirm-actions" style={{marginTop:12}}>
              <button className="btn-ghost-sm" onClick={() => setAfVolPickerOpen(false)}>取消</button>
              <button className="btn-primary-sm" onClick={confirmAfVolPicker} disabled={afChecking}>
                {afChecking ? '⏳ 检查中...' : '🚀 开始检查'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ===== 地图/地点面板（按卷） ===== */
function LocationsPanel(props: {
  bookId: string;
  bible: BookBible | null;
  onBibleUpdate: (b: BookBible) => void;
  bookTitle: string;
  chapters: Chapter[];
  hasChapters: boolean;
  showConfirm: (message: string, onConfirm: () => void) => void;
  selectedSkillPackIds: string[];
  onMapUpdate: (val: string) => Promise<void>;
  onOpenAiCreate: () => void;
}) {
  const { bookId, bible, onBibleUpdate, chapters, hasChapters, showConfirm, selectedSkillPackIds, onMapUpdate } = props;
  const [locations, setLocations] = useState('');
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState('');
  const [saving, setSaving] = useState(false);
  const [locVolumes, setLocVolumes] = useState<any[]>([]);
  const [analyzingVol, setAnalyzingVol] = useState('');
  const [collapsedVols, setCollapsedVols] = useState<Set<number>>(new Set());
  // 每次进入维度默认折叠所有卷（tab 切换重新挂载，ref 重置）
  const locCollapseInitRef = useRef(false);
  const [volSelectorOpen, setVolSelectorOpen] = useState(false);
  const [editingVolIdx, setEditingVolIdx] = useState<number | null>(null);
  const [editVolJson, setEditVolJson] = useState('');
  const [globalLocCollapsed, setGlobalLocCollapsed] = useState(true); // 全局地点档案默认折叠

  useEffect(() => {
    setLocations(bible?.locations || '');
  }, [bible?.locations]);

  useEffect(() => {
    if (!bible?.locations_volumes) { setLocVolumes([]); return; }
    try {
      const parsed = JSON.parse(bible.locations_volumes);
      if (Array.isArray(parsed)) { setLocVolumes(parsed); return; }
    } catch { /* not JSON */ }
    setLocVolumes([]);
  }, [bible?.locations_volumes]);

  const volumeChapters = chapters.filter(c => c.is_volume);

  const displayVolumes = useMemo(() => {
    const result: any[] = [];
    const usedIds = new Set<string>();
    for (const vc of volumeChapters) {
      const lvData = locVolumes.find(v => v.volume_id === vc.id) || locVolumes.find(v => v.volume === vc.title);
      result.push({
        volume_id: vc.id,
        volume: vc.title,
        data: lvData?.data || null,
        chapter_count: chapters.filter(c => c.parent_id === vc.id).length,
      });
      if (lvData) { usedIds.add(lvData.volume_id || ''); usedIds.add(lvData.volume || ''); }
    }
    for (const v of locVolumes) {
      const id = v.volume_id || '';
      const name = v.volume || '';
      if (!usedIds.has(id) && !usedIds.has(name)) {
        result.push({ ...v, chapter_count: 0 });
      }
    }
    return result;
  }, [volumeChapters, locVolumes, chapters]);

  // 首次有卷数据时默认折叠全部卷（每次切换到该维度 tab 重新挂载，ref 重置，实现每次进入默认折叠）
  useEffect(() => {
    if (locCollapseInitRef.current) return;
    if (displayVolumes.length > 0) {
      locCollapseInitRef.current = true;
      setCollapsedVols(new Set(displayVolumes.map((_, idx) => idx)));
    }
  }, [displayVolumes]);

  function toggleVol(idx: number) {
    setCollapsedVols(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  async function saveLocations(val: string) {
    setSaving(true);
    try {
      await onMapUpdate(val);
      setLocations(val);
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setSaving(false);
  }

  async function handleAnalyzeVolume(volId: string, volTitle: string) {
    showConfirm(`将用 AI 分析「${volTitle}」的章节内容，识别本卷涉及的地点、场景、地理信息。是否继续？`, async () => {
      setAnalyzingVol(volId || volTitle);
      try {
        const result = await api.analyzeLocationsVolume(bookId, volId, volTitle, selectedSkillPackIds);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已为「${volTitle}」生成地点分析`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingVol('');
    });
  }

  async function deleteVolumeLoc(idx: number) {
    const vol = displayVolumes[idx];
    if (!vol) return;
    showConfirm(`确定删除「${vol.volume || '该卷'}」的地点识别数据？`, async () => {
      const newList = locVolumes.filter((v: any) => {
        const vId = v.volume_id || '';
        const vName = v.volume || '';
        if (vol.volume_id && vId === vol.volume_id) return false;
        if (vol.volume && vName === vol.volume) return false;
        return true;
      });
      try {
        const updated = await api.updateBible(bookId, { locations_volumes: JSON.stringify(newList, null, 2) } as any);
        onBibleUpdate(updated);
      } catch (e: any) {
        alert('删除失败: ' + e.message);
      }
    });
  }

  // 开始按卷编辑：将该卷的 data 序列化为 JSON 供编辑
  function startEditVolLoc(idx: number) {
    const vol = displayVolumes[idx];
    if (!vol) return;
    const editTarget = vol.data || { summary: '', locations: [], regions: [] };
    setEditingVolIdx(idx);
    setEditVolJson(JSON.stringify(editTarget, null, 2));
    setCollapsedVols(prev => { const n = new Set(prev); n.delete(idx); return n; });
  }

  // 保存按卷编辑：解析编辑后的 JSON，写回 locations_volumes
  async function saveEditVolLoc(idx: number) {
    try {
      const parsed = JSON.parse(editVolJson);
      const vol = displayVolumes[idx];
      const matchKey = vol.volume_id || vol.volume;
      const newList = [...locVolumes];
      const existIdx = newList.findIndex((v: any) => (v.volume_id || v.volume) === matchKey);
      const entry = {
        volume_id: vol.volume_id || '',
        volume: vol.volume || '',
        data: parsed,
      };
      if (existIdx >= 0) {
        newList[existIdx] = { ...newList[existIdx], ...entry };
      } else {
        newList.push(entry);
      }
      const updated = await api.updateBible(bookId, { locations_volumes: JSON.stringify(newList, null, 2) } as any);
      onBibleUpdate(updated);
      setEditingVolIdx(null);
      setEditVolJson('');
    } catch (e: any) {
      alert('保存失败：JSON 格式错误 - ' + e.message);
    }
  }

  return (
    <div className="bible-edit-panel">
      <div className="bible-edit-header">
        <div className="bible-edit-actions" style={{position:'relative',flexShrink:0}}>
          {(
            <>
              <button className="btn-ghost-sm" onClick={() => setVolSelectorOpen(v => !v)} disabled={!!analyzingVol || !hasChapters} title={hasChapters ? '选择卷进行AI识别' : '需要先创建章节才能AI识别'}>
                {analyzingVol ? '🤖 识别中...' : '🔍 AI识别'}
              </button>
              {volSelectorOpen && (
                <div className="vol-selector-dropdown" style={{position:'absolute',top:'100%',right:0,marginTop:4,background:'var(--bg-secondary)',border:'1px solid var(--border)',borderRadius:8,padding:6,minWidth:180,zIndex:100,boxShadow:'0 4px 12px rgba(0,0,0,0.15)'}}>
                  <div style={{fontSize:12,color:'var(--text-muted)',padding:'4px 8px',borderBottom:'1px solid var(--border)',marginBottom:4}}>选择要识别的卷</div>
                  <button className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeVolume('', '全部章节'); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📚 全部章节</button>
                  {displayVolumes.map((vol, idx) => (
                    <button key={idx} className="vol-selector-item" onClick={() => { setVolSelectorOpen(false); handleAnalyzeVolume(vol.volume_id || '', vol.volume || `第${idx + 1}卷`); }} style={{display:'block',width:'100%',textAlign:'left',padding:'6px 10px',background:'transparent',border:'none',borderRadius:4,cursor:'pointer',color:'var(--text)',fontSize:13}}>📖 {vol.volume || `第${idx + 1}卷`}{vol.chapter_count ? ` (${vol.chapter_count}章)` : ''}</button>
                  ))}
                  <button onClick={() => setVolSelectorOpen(false)} style={{display:'block',width:'100%',textAlign:'center',padding:'4px',background:'transparent',border:'none',cursor:'pointer',color:'var(--text-muted)',fontSize:12,marginTop:2}}>取消</button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
      <p className="text-muted" style={{fontSize:12, marginBottom:8}}>
        记录地点、场景、地理信息。点击「🔍 AI识别」选择卷，识别结果自动归类到对应卷下。
      </p>

      {/* 按卷地点识别 */}
      {displayVolumes.length > 0 && (
        <div className="plot-volume-list" style={{marginBottom:16}}>
          {displayVolumes.map((vol, idx) => {
            const d = vol.data || {};
            const hasData = vol.data && (d.summary || (d.locations && d.locations.length) || (d.regions && d.regions.length));
            return (
              <div key={idx} className="plot-volume-card">
                <div className="plot-volume-header" onClick={() => toggleVol(idx)} style={{cursor:'pointer'}}>
                  <span className="map-toggle" style={{fontSize:10,marginRight:6}}>{collapsedVols.has(idx) ? '▶' : '▼'}</span>
                  <h4>{vol.volume || `第${idx + 1}卷`}</h4>
                  {vol.chapter_count !== undefined && <span className="text-muted" style={{fontSize:12}}>{vol.chapter_count}章</span>}
                  {hasData && <span className="text-muted" style={{fontSize:12}}>已识别</span>}
                  <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                    {analyzingVol === (vol.volume_id || vol.volume) && <span className="text-muted" style={{fontSize:12}}>🤖 识别中...</span>}
                    <button className="btn-ghost-sm" onClick={() => editingVolIdx === idx ? (setEditingVolIdx(null), setEditVolJson('')) : startEditVolLoc(idx)} title={editingVolIdx === idx ? '取消编辑' : '编辑此卷地点数据（JSON）'}>{editingVolIdx === idx ? '取消' : '✏️'}</button>
                    {hasData && (
                      <button className="btn-ghost-sm" onClick={() => deleteVolumeLoc(idx)} style={{color:'#e74c3c'}} title="删除此卷地点数据">🗑️</button>
                    )}
                  </div>
                </div>
                {!collapsedVols.has(idx) && (
                  <div className="plot-volume-body">
                    {editingVolIdx === idx ? (
                      <div style={{marginTop:8}}>
                        <p className="text-muted" style={{fontSize:12,marginBottom:6}}>编辑本卷地点数据（JSON 格式）：summary（地理概况）、locations（地点）、regions（区域）。</p>
                        <textarea className="input" value={editVolJson} onChange={e => setEditVolJson(e.target.value)} rows={16} style={{fontFamily:'monospace',fontSize:12}} />
                        <div style={{display:'flex',gap:6,marginTop:8}}>
                          <button className="btn-primary-sm" onClick={() => saveEditVolLoc(idx)}>💾 保存</button>
                          <button className="btn-ghost-sm" onClick={() => { setEditingVolIdx(null); setEditVolJson(''); }}>取消</button>
                        </div>
                      </div>
                    ) : !hasData ? (
                      <p className="text-muted" style={{fontSize:13}}>暂无地点识别数据，点击「🔍 AI识别」选择此卷进行识别</p>
                    ) : (
                      <div className="plot-events">
                        {d.summary && <p><b>地理概况：</b>{d.summary}</p>}
                        {d.locations && d.locations.length > 0 && (
                          <div><b>地点（{d.locations.length}）：</b><ul>
                            {d.locations.map((l: any, i: number) => (
                              <li key={i}><span style={{color:'#5b8def',fontWeight:600}}>{l.name}</span>{l.type && <span style={{color:'#27ae60'}}> · {l.type}</span>}{l.importance && <span style={{color:'#e74c3c'}}> · {l.importance}</span>}{l.description && <span style={{color:'#666'}}> — {l.description}</span>}{l.events && <span style={{color:'#888'}}> · 事件：{l.events}</span>}</li>
                            ))}
                          </ul></div>
                        )}
                        {d.regions && d.regions.length > 0 && (
                          <div><b>区域（{d.regions.length}）：</b><ul>
                            {d.regions.map((r: any, i: number) => (
                              <li key={i}><span style={{color:'#9b59b6',fontWeight:600}}>{r.name}</span>{r.scope && <span style={{color:'#888'}}> · {r.scope}</span>}{r.feature && <span style={{color:'#666'}}> — {r.feature}</span>}</li>
                            ))}
                          </ul></div>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* 全局地点编辑 */}
      <div className="bible-edit-section">
        <div
          style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:8,cursor:'pointer',userSelect:'none'}}
          onClick={() => setGlobalLocCollapsed(c => !c)}
        >
          <b>📝 全局地点档案 <span style={{fontSize:10,color:'var(--text-muted)'}}>{globalLocCollapsed ? '▶ 点击展开' : '▼ 点击折叠'}</span></b>
          {!editing ? (
            <button className="btn-ghost-sm" onClick={e => { e.stopPropagation(); setEditing(true); setEditValue(locations); setGlobalLocCollapsed(false); }}>✏️ 编辑</button>
          ) : (
            <div style={{display:'flex',gap:6}} onClick={e => e.stopPropagation()}>
              <button className="btn-primary-sm" onClick={() => { saveLocations(editValue); setEditing(false); }} disabled={saving}>{saving ? '保存中...' : '💾 保存'}</button>
              <button className="btn-ghost-sm" onClick={() => setEditing(false)}>取消</button>
            </div>
          )}
        </div>
        {!globalLocCollapsed && (
          editing ? (
            <textarea className="input" rows={12} value={editValue} onChange={e => setEditValue(e.target.value)} placeholder="记录主要地点、区域、地理特征..." />
          ) : (
            <div className="bible-content-view" style={{whiteSpace:'pre-wrap',minHeight:80,padding:12,background:'var(--bg-tertiary)',borderRadius:8}}>
              {locations || <span className="text-muted">暂无全局地点档案，点击「编辑」手动添加</span>}
            </div>
          )
        )}
      </div>
    </div>
  );
}

/* ===== 地图面板（三级分类，已弃用，由 LocationsPanel 替代；保留导出避免tree-shake报错） ===== */
export function _MapPanel_unused(props: {
  bookId: string;
  locations: string;
  onUpdate: (val: string) => Promise<void>;
  showConfirm: (message: string, onConfirm: () => void) => void;
  onAnalyzeFromReports: () => void;
  dimAnalyzing: boolean;
}) {
  const { locations, onUpdate, showConfirm, onAnalyzeFromReports, dimAnalyzing } = props;
  const [regions, setRegions] = useState<MapRegion[]>([]);
  const [expandedL1, setExpandedL1] = useState<number | null>(null);
  const [expandedL2, setExpandedL2] = useState<string | null>(null);
  const [editingNode, setEditingNode] = useState<string | null>(null);
  const [editText, setEditText] = useState('');

  // 添加地点弹窗
  const [addModal, setAddModal] = useState<{ level: 1 | 2 | 3; l1Idx?: number; l2Idx?: number } | null>(null);
  const [addName, setAddName] = useState('');
  const [addDesc, setAddDesc] = useState('');
  const [saving, setSaving] = useState(false);

  // 解析 locations 数据
  useEffect(() => {
    if (!locations) {
      setRegions([]);
      return;
    }
    // 尝试 JSON 解析
    try {
      const parsed = JSON.parse(locations);
      if (Array.isArray(parsed)) {
        setRegions(parsed);
        return;
      }
    } catch { /* not JSON */ }
    // 如果是纯文本，按行解析为一级地点
    const lines = locations.split('\n').filter(l => l.trim());
    const parsedRegions: MapRegion[] = lines.map(line => {
      const cleanLine = line.replace(/^[【#\-*•]\s*/, '').replace(/】/g, '');
      const parts = cleanLine.split(/[：:：]/);
      return { name: parts[0].trim(), desc: parts[1]?.trim() || '', children: [] };
    });
    setRegions(parsedRegions);
  }, [locations]);

  async function saveRegions(newRegions: MapRegion[]) {
    setRegions(newRegions);
    try {
      await onUpdate(JSON.stringify(newRegions, null, 2));
    } catch (e) {
      // 保存失败时恢复原数据
      setRegions(regions);
    }
  }

  function openAddModal(level: 1 | 2 | 3, l1Idx?: number, l2Idx?: number) {
    setAddModal({ level, l1Idx, l2Idx });
    setAddName('');
    setAddDesc('');
  }

  function confirmAdd() {
    if (!addModal || !addName.trim()) return;
    setSaving(true);
    const name = addName.trim();
    const desc = addDesc.trim();
    const newRegions = JSON.parse(JSON.stringify(regions)) as MapRegion[];

    if (addModal.level === 1) {
      newRegions.push({ name, desc, children: [] });
    } else if (addModal.level === 2 && addModal.l1Idx !== undefined) {
      if (!newRegions[addModal.l1Idx].children) newRegions[addModal.l1Idx].children = [];
      newRegions[addModal.l1Idx].children!.push({ name, desc, children: [] });
      setExpandedL1(addModal.l1Idx);
    } else if (addModal.level === 3 && addModal.l1Idx !== undefined && addModal.l2Idx !== undefined) {
      const l2 = newRegions[addModal.l1Idx]?.children?.[addModal.l2Idx];
      if (l2) {
        if (!l2.children) l2.children = [];
        l2.children.push({ name, desc, children: [] });
        setExpandedL2(`${addModal.l1Idx}-${addModal.l2Idx}`);
      }
    }

    saveRegions(newRegions).then(() => {
      setSaving(false);
      setAddModal(null);
    });
  }

  function addL1() {
    openAddModal(1);
  }

  function addL2(l1Idx: number) {
    openAddModal(2, l1Idx);
  }

  function addL3(l1Idx: number, l2Idx: number) {
    openAddModal(3, l1Idx, l2Idx);
  }

  function deleteNode(l1Idx: number, l2Idx?: number, l3Idx?: number) {
    const level = l3Idx !== undefined ? '具体场景' : l2Idx !== undefined ? '小地点' : '大地点';
    showConfirm(`确定删除此${level}？`, () => {
      const newRegions = JSON.parse(JSON.stringify(regions)) as MapRegion[];
      if (l3Idx !== undefined && l2Idx !== undefined) {
        newRegions[l1Idx].children![l2Idx].children!.splice(l3Idx, 1);
      } else if (l2Idx !== undefined) {
        newRegions[l1Idx].children!.splice(l2Idx, 1);
      } else {
        newRegions.splice(l1Idx, 1);
      }
      saveRegions(newRegions);
    });
  }

  function startEditDesc(l1Idx: number, l2Idx?: number, l3Idx?: number) {
    const key = `${l1Idx}-${l2Idx ?? ''}-${l3Idx ?? ''}`;
    let currentDesc = '';
    if (l3Idx !== undefined && l2Idx !== undefined) {
      currentDesc = regions[l1Idx]?.children?.[l2Idx]?.children?.[l3Idx]?.desc || '';
    } else if (l2Idx !== undefined) {
      currentDesc = regions[l1Idx]?.children?.[l2Idx]?.desc || '';
    } else {
      currentDesc = regions[l1Idx]?.desc || '';
    }
    setEditingNode(key);
    setEditText(currentDesc);
  }

  async function saveDesc(l1Idx: number, l2Idx?: number, l3Idx?: number) {
    const newRegions = JSON.parse(JSON.stringify(regions)) as MapRegion[];
    if (l3Idx !== undefined && l2Idx !== undefined) {
      newRegions[l1Idx].children![l2Idx].children![l3Idx].desc = editText;
    } else if (l2Idx !== undefined) {
      newRegions[l1Idx].children![l2Idx].desc = editText;
    } else {
      newRegions[l1Idx].desc = editText;
    }
    await saveRegions(newRegions);
    setEditingNode(null);
  }

  const nodeKey = (a: number, b?: number, c?: number) => `${a}-${b ?? ''}-${c ?? ''}`;

  const addModalTitle = addModal?.level === 1 ? '添加大地点' : addModal?.level === 2 ? '添加小地点' : '添加具体场景';
  const addModalPlaceholder = addModal?.level === 1 ? '如：东大陆、西荒漠' : addModal?.level === 2 ? '如：长安城、青云门' : '如：皇宫、藏经阁';

  return (
    <div className="map-panel">
      <div className="map-header">
        <h3>🗺️ 世界地图</h3>
        <div className="map-header-actions">
          <button className="btn-ghost-sm" onClick={onAnalyzeFromReports} disabled={dimAnalyzing} title="从动态文件提取地点信息">
            {dimAnalyzing ? '⏳ 识别中...' : '🔍 AI识别'}
          </button>
          <button className="btn-primary-sm" onClick={addL1}>+ 大地点</button>
        </div>
      </div>
      <p className="text-muted" style={{marginBottom:12}}>三级分类：大地点 → 小地点 → 具体场景</p>

      {/* 地点层级缩略图 */}
      {regions.length > 0 && (
        <div className="map-overview" style={{display:'flex',gap:8,marginBottom:14,flexWrap:'wrap'}}>
          {regions.slice(0, 6).map((r, i) => (
            <div key={i} className="map-overview-card" style={{background:'var(--bg-secondary)',border:'1px solid var(--border-color)',borderRadius:'var(--radius-sm)',padding:'8px 12px',display:'flex',alignItems:'center',gap:8}}>
              <svg width="32" height="32" viewBox="0 0 32 32">
                <circle cx="16" cy="14" r="10" fill="#c8e6c9" stroke="#4a8b4a" strokeWidth="1.5" />
                <path d="M8 24 L12 20 L20 20 L24 24" fill="#e8dcc8" stroke="#c8b898" strokeWidth="1" />
                <rect x="13" y="5" width="6" height="7" fill="#efebe9" stroke="#8d6e63" strokeWidth="0.8" rx="1" />
                <rect x="14" y="6" width="4" height="4" fill="#bcaaa4" />
              </svg>
              <div>
                <div style={{fontSize:13,fontWeight:600}}>{r.name}</div>
                <div style={{fontSize:11,color:'var(--text-muted)'}}>{(r.children||[]).length} 个子地点</div>
              </div>
            </div>
          ))}
        </div>
      )}

      {regions.length === 0 ? (
        <div className="bible-empty" onClick={addL1}>
          <span className="bible-empty-icon">🗺️</span>
          <p>暂无地图数据</p>
          <p className="text-muted">点击添加大地点</p>
        </div>
      ) : (
        <div className="map-tree">
          {regions.map((r1, i1) => (
            <div key={i1} className="map-l1">
              <div className="map-node map-node-l1" onClick={() => setExpandedL1(expandedL1 === i1 ? null : i1)}>
                <span className="map-toggle">{expandedL1 === i1 ? '▼' : '▶'}</span>
                <span className="map-node-icon">🌍</span>
                <span className="map-node-name">{r1.name}</span>
                <div className="map-node-actions" onClick={e => e.stopPropagation()}>
                  <button className="btn-icon-sm" title="添加小地点" onClick={() => addL2(i1)}>＋</button>
                  <button className="btn-icon-sm" title="编辑描述" onClick={() => startEditDesc(i1)}>✏️</button>
                  <button className="btn-icon-sm" title="删除" onClick={() => deleteNode(i1)} style={{color:'#e74c3c'}}>🗑️</button>
                </div>
              </div>
              {editingNode === nodeKey(i1) && (
                <div className="map-desc-editor">
                  <textarea className="input" rows={2} value={editText} onChange={e => setEditText(e.target.value)} placeholder="地点描述..." autoFocus />
                  <div className="map-desc-actions">
                    <button className="btn-primary-sm" onClick={() => saveDesc(i1)}>保存</button>
                    <button className="btn-ghost-sm" onClick={() => setEditingNode(null)}>取消</button>
                  </div>
                </div>
              )}
              {r1.desc && editingNode !== nodeKey(i1) && (
                <div className="map-node-desc">{r1.desc}</div>
              )}

              {expandedL1 === i1 && (
                <div className="map-l2-list">
                  {r1.children && r1.children.length > 0 ? (
                    r1.children.map((r2, i2) => (
                      <div key={i2} className="map-l2">
                        <div className="map-node map-node-l2" onClick={() => setExpandedL2(expandedL2 === `${i1}-${i2}` ? null : `${i1}-${i2}`)}>
                          <span className="map-toggle">{expandedL2 === `${i1}-${i2}` ? '▼' : '▶'}</span>
                          <span className="map-node-icon">🏰</span>
                          <span className="map-node-name">{r2.name}</span>
                          <div className="map-node-actions" onClick={e => e.stopPropagation()}>
                            <button className="btn-icon-sm" title="添加场景" onClick={() => addL3(i1, i2)}>＋</button>
                            <button className="btn-icon-sm" title="编辑描述" onClick={() => startEditDesc(i1, i2)}>✏️</button>
                            <button className="btn-icon-sm" title="删除" onClick={() => deleteNode(i1, i2)} style={{color:'#e74c3c'}}>🗑️</button>
                          </div>
                        </div>
                        {editingNode === nodeKey(i1, i2) && (
                          <div className="map-desc-editor">
                            <textarea className="input" rows={2} value={editText} onChange={e => setEditText(e.target.value)} placeholder="地点描述..." autoFocus />
                            <div className="map-desc-actions">
                              <button className="btn-primary-sm" onClick={() => saveDesc(i1, i2)}>保存</button>
                              <button className="btn-ghost-sm" onClick={() => setEditingNode(null)}>取消</button>
                            </div>
                          </div>
                        )}
                        {r2.desc && editingNode !== nodeKey(i1, i2) && (
                          <div className="map-node-desc">{r2.desc}</div>
                        )}

                        {expandedL2 === `${i1}-${i2}` && (
                          <div className="map-l3-list">
                            {r2.children && r2.children.length > 0 ? (
                              r2.children.map((r3, i3) => (
                                <div key={i3} className="map-l3">
                                  <div className="map-node map-node-l3">
                                    <span className="map-node-icon">📍</span>
                                    <span className="map-node-name">{r3.name}</span>
                                    <div className="map-node-actions" onClick={e => e.stopPropagation()}>
                                      <button className="btn-icon-sm" title="编辑描述" onClick={() => startEditDesc(i1, i2, i3)}>✏️</button>
                                      <button className="btn-icon-sm" title="删除" onClick={() => deleteNode(i1, i2, i3)} style={{color:'#e74c3c'}}>🗑️</button>
                                    </div>
                                  </div>
                                  {editingNode === nodeKey(i1, i2, i3) ? (
                                    <div className="map-desc-editor">
                                      <textarea className="input" rows={2} value={editText} onChange={e => setEditText(e.target.value)} placeholder="地点描述..." autoFocus />
                                      <div className="map-desc-actions">
                                        <button className="btn-primary-sm" onClick={() => saveDesc(i1, i2, i3)}>保存</button>
                                        <button className="btn-ghost-sm" onClick={() => setEditingNode(null)}>取消</button>
                                      </div>
                                    </div>
                                  ) : r3.desc ? (
                                    <div className="map-node-desc">{r3.desc}</div>
                                  ) : null}
                                </div>
                              ))
                            ) : (
                              <p className="text-muted map-empty-hint">暂无场景，点击上方 ＋ 添加</p>
                            )}
                          </div>
                        )}
                      </div>
                    ))
                  ) : (
                    <p className="text-muted map-empty-hint">暂无小地点，点击上方 ＋ 添加</p>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 添加地点弹窗 */}
      {addModal && (
        <div className="modal-overlay" onClick={() => !saving && setAddModal(null)}>
          <div className="modal" onClick={e => e.stopPropagation()} style={{maxWidth:380}}>
            <h2>{addModalTitle}</h2>
            <input
              className="input"
              placeholder={addModalPlaceholder}
              value={addName}
              onChange={e => setAddName(e.target.value)}
              autoFocus
              onKeyDown={e => { if (e.key === 'Enter' && addName.trim() && !saving) confirmAdd(); }}
            />
            <textarea
              className="input"
              rows={2}
              placeholder="描述（可选）"
              value={addDesc}
              onChange={e => setAddDesc(e.target.value)}
              style={{marginTop:8}}
            />
            <div className="modal-actions">
              <button className="btn-ghost-sm" onClick={() => setAddModal(null)} disabled={saving}>取消</button>
              <button className="btn-primary-sm" onClick={confirmAdd} disabled={!addName.trim() || saving}>
                {saving ? '保存中...' : '确认添加'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
