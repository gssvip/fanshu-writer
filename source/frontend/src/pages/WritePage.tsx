import { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api } from '../api';
import type { Book, BookBible, BrainstormResult, BrainstormSuggestion, Chapter, SkillPack, DynamicReport } from '../types';

// 两行 Tab 布局
const TAB_ROW_1 = [
  { key: 'concept', label: '构思', icon: '💡', field: 'concept', placeholder: '一句话描述你的故事核心创意...' },
  { key: 'settings', label: '设定', icon: '⚙️', field: 'key_rules', placeholder: '核心规则、能力限制、世界观禁忌...' },
  { key: 'outline', label: '大纲', icon: '📋', field: 'plot_design', placeholder: '主线冲突、卷纲拆解、章节规划...' },
  { key: 'characters', label: '人物及关系', icon: '👤', field: 'character_profiles', placeholder: '主角、配角的姓名、身份、性格、动机、人物关系...' },
  { key: 'plot', label: '剧情', icon: '📖', field: 'timeline', placeholder: '按时间顺序列出关键事件...' },
  { key: 'dynamicMemory', label: '动态文件', icon: '🗂️', field: '', placeholder: '' },
];

const TAB_ROW_2 = [
  { key: 'chapters', label: '章节', icon: '📚', field: '', placeholder: '' },
  { key: 'foreshadowing', label: '伏笔', icon: '🔮', field: 'foreshadowing', placeholder: '伏笔内容、埋设时机、回收方式...' },
  { key: 'map', label: '地图', icon: '🗺️', field: 'locations', placeholder: '' },
  { key: 'relationGraph', label: '关系图谱', icon: '🕸️', field: 'character_profiles', placeholder: '' },
  { key: 'realmGraph', label: '境界图谱', icon: '⚡', field: 'worldbuilding', placeholder: '' },
  { key: 'locationGraph', label: '地点图谱', icon: '🗺️', field: 'locations', placeholder: '' },
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
  relationGraph: '人物关系图谱',
  realmGraph: '境界图谱',
  locationGraph: '地点图谱',
};

// 维度 → 技能包 prompt_key 映射（用于查找最匹配的技能提示词）
const DIMENSION_SKILL_KEYS: Record<string, string[]> = {
  concept: ['one_line_concept', 'master_outline', 'tomato_plan'],
  key_rules: ['lock_facts', 'tomato_setting'],
  plot_design: ['master_outline', 'volume_breakdown', 'chapter_plan', 'tomato_outline'],
  worldbuilding: ['lock_facts', 'tomato_setting'],
  character_profiles: ['character_cognition', 'tomato_character'],
  timeline: ['chapter_plan', 'tomato_outline'],
  foreshadowing: ['foreshadow_register', 'narrative_debt'],
  locations: ['lock_facts'],
};

// 章节AI模式 → 技能包 prompt_key 映射
const CHAPTER_SKILL_KEYS: Record<string, string[]> = {
  write: ['write_chapter', 'draft_writing', 'context_pack', 'tomato_chapter', 'fantasy_draft'],
  continue: ['write_chapter', 'draft_writing', 'tomato_chapter', 'fantasy_draft'],
  polish: ['polish', 'de_ai_check', 'minimal_rewrite', 'humanize', 'final_check', 'tomato_deai', 'forbidden_words', 'rhythm_check'],
};

// 从多个技能包中提取匹配的提示词（合并）
function extractSkillPrompt(packs: SkillPack[], keys: string[]): string {
  const found: string[] = [];
  for (const pack of packs) {
    if (!pack || !pack.prompts) continue;
    for (const key of keys) {
      if (pack.prompts[key]) {
        found.push(`【${pack.name}】\n${pack.prompts[key]}`);
        break; // 每个包只取第一个匹配的key
      }
    }
  }
  return found.length > 0 ? found.join('\n\n') : '';
}

// 地图数据结构
interface MapRegion {
  name: string;
  desc?: string;
  children?: MapRegion[];
  visited?: boolean;
  isCurrent?: boolean;
}

export default function WritePage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const bookId = searchParams.get('book');

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

  // 技能包（多选）
  const [skillPacks, setSkillPacks] = useState<SkillPack[]>([]);
  const [selectedSkillPackIds, setSelectedSkillPackIds] = useState<string[]>([]);
  const selectedSkillPacks = useMemo(() => skillPacks.filter(p => selectedSkillPackIds.includes(p.id)), [skillPacks, selectedSkillPackIds]);
  const toggleSkillPack = useCallback((id: string) => {
    setSelectedSkillPackIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);

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
  const [aiStreamError, setAiStreamError] = useState('');
  const [aiUserPrompt, setAiUserPrompt] = useState('');

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
      const result = await api.brainstorm(bookId, concept);
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

  // 从动态文件报告提取维度信息（地图/关系图谱/地点图谱/境界图谱等）
  const handleAnalyzeFromReports = useCallback(async (dimension: string) => {
    if (!bookId) return;
    const label = DIMENSION_LABELS[dimension] || dimension;
    showConfirm(`将用 AI 从动态文件报告中提取并填充「${label}」信息，节省token。是否继续？`, async () => {
      setDimAnalyzing(true);
      try {
        const result = await api.analyzeFromReports(bookId, dimension);
        if (result.bible) setBible(result.bible);
        alert(`AI识别完成！已从${result.source}提取并填充「${label}」信息`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setDimAnalyzing(false);
    });
  }, [bookId]);

  // 稳定的维度回调
  const onAnalyzeFromReportsLocations = useCallback(() => handleAnalyzeFromReports('locations'), [handleAnalyzeFromReports]);
  const onAnalyzeFromReportsGraph = useCallback(() => handleAnalyzeFromReports(activeTab), [handleAnalyzeFromReports, activeTab]);
  const onAnalyzeConcept = useCallback(() => handleAnalyzeDimension('concept'), [bookId]);

  // 图谱/地图更新回调 —— 必须在所有 early return 之前声明
  const handleGraphUpdate = useCallback(async (val: string) => {
    if (!bookId) return;
    const updated = await api.updateBible(bookId, { [currentTab.field]: val } as any);
    setBible(updated);
  }, [bookId, currentTab.field]);

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
      // 计算在当前卷下的章节序号
      const siblings = parentId
        ? chapters.filter(c => c.parent_id === parentId && !c.is_volume)
        : chapters.filter(c => !c.is_volume && !c.parent_id);
      const ch = await api.createChapter(bookId, {
        title: `第${siblings.length + 1}章`,
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
    try {
      const vol = await api.createChapter(bookId, {
        title,
        content: '',
        order_index: chapters.length,
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

    const confirmMsg = isOrphanDelete
      ? `确定删除全部 ${children.length} 章未分卷章节？此操作不可撤销。`
      : children.length > 0
        ? `该卷下还有 ${children.length} 章，删除卷后这些章节将变为未分卷。确定删除？`
        : '确定删除此卷？';

    showConfirm(confirmMsg, async () => {
      try {
        if (isOrphanDelete) {
          for (const ch of children) {
            await api.deleteChapter(bookId, ch.id);
          }
        } else {
          await api.deleteChapter(bookId, volId);
        }
        const updated = await api.listChapters(bookId);
        setChapters(updated);
      } catch (e: any) {
        alert('删除失败: ' + e.message);
      }
    });
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
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setChapterSaving(false);
  }

  // 进入AI创作面板（不自动生成，等用户提问）
  function startAiCreate(mode: 'write' | 'continue' | 'polish') {
    if (!bookId || !activeChapter) return;
    if (mode === 'polish' && !chapterEditContent.trim()) {
      alert('请先写一些内容再润色');
      return;
    }
    setAiCreateMode(mode);
    setAiGeneratedContent('');
    setAiCreating(false);
    setAiStreamError('');
    // 预填默认提问
    if (mode === 'write') {
      setAiUserPrompt(`请为「${chapterEditTitle}」创作完整章节内容，要求开篇吸引、对话自然、节奏紧凑、章末留悬念。`);
    } else if (mode === 'continue') {
      setAiUserPrompt('请继续往下写，保持风格一致，自然衔接已有内容。');
    } else {
      setAiUserPrompt('请润色优化以下内容，提升文采和节奏感，保持原意不变。');
    }
  }

  // 执行AI创作（用户提问后触发）
  async function executeAiCreate() {
    if (!bookId || !activeChapter || !aiCreateMode) return;
    if (!aiUserPrompt.trim()) {
      alert('请输入你的创作要求');
      return;
    }
    setAiCreating(true);
    setAiStreamError('');
    setAiGeneratedContent('');

    try {
      const contextConcept = concept || bible?.concept || book?.synopsis || '暂无构思';

      // 优先获取动态报告作为前文记忆（节省token），没有时回退到章节摘要
      let memoryContext = '';
      let prevChapters = '';
      try {
        const dmContext = await api.getDynamicReportContext(bookId);
        if (dmContext.context_text && dmContext.context_text.trim()) {
          memoryContext = dmContext.context_text;
          // 仍取最近1章尾部作为即时衔接
          const prevCh = chapters
            .filter(c => c.order_index < activeChapter.order_index)
            .slice(-1)[0];
          prevChapters = prevCh ? `【${prevCh.title}】${(prevCh.content || '').slice(-400)}` : '';
        }
      } catch {
        // 动态报告获取失败，回退到旧逻辑
      }

      // 如果没有动态报告，使用旧逻辑获取最近3章摘要
      if (!memoryContext) {
        prevChapters = chapters
          .filter(c => c.order_index < activeChapter.order_index)
          .slice(-3)
          .map(c => `【${c.title}】${(c.content || '').slice(-500)}`)
          .join('\n\n');
      }

      // 构建前文记忆段落
      const memorySection = memoryContext
        ? `前文动态记忆（防遗忘摘要）：\n${memoryContext}\n\n最近章节衔接：${prevChapters || '无'}`
        : `前文摘要：${prevChapters || '这是第一章'}`;

      let systemContent = '';
      let userContent = '';

      // 提取已勾选技能包的提示词（合并多个）
      const skillKeys = CHAPTER_SKILL_KEYS[aiCreateMode] || [];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';

      if (aiCreateMode === 'write') {
        systemContent = `你是专业网文作家，擅长${book?.genre || '通用'}题材。请根据用户的创作要求和故事设定，创作章节内容。要求：对话自然，避免说教和AI味，节奏紧凑，章末留悬念。输出1500-3000字。${skillNote}`;
        userContent = `作品：${book?.title}\n构思：${contextConcept}\n世界观：${bible?.worldbuilding?.slice(0, 400) || '无'}\n人物：${bible?.character_profiles?.slice(0, 400) || '无'}\n大纲：${bible?.plot_design?.slice(0, 400) || '无'}\n\n${memorySection}\n\n当前章节：${chapterEditTitle}\n已有内容：${chapterEditContent.slice(-400) || '（空白）'}\n\n用户创作要求：${aiUserPrompt}`;
      } else if (aiCreateMode === 'continue') {
        systemContent = `你是专业网文作家，擅长${book?.genre || '通用'}题材。请根据用户的续写要求和已有内容继续创作，保持风格一致。要求：对话自然，避免说教，节奏紧凑。输出800-1500字。${skillNote}`;
        userContent = `作品：${book?.title}\n构思：${contextConcept}\n世界观：${bible?.worldbuilding?.slice(0, 300) || '无'}\n人物：${bible?.character_profiles?.slice(0, 300) || '无'}\n\n${memorySection}\n\n当前章节：${chapterEditTitle}\n已有内容：${chapterEditContent.slice(-800) || '（空白，请开篇）'}\n\n用户续写要求：${aiUserPrompt}`;
      } else {
        systemContent = `你是专业网文编辑。请根据用户的润色要求对内容进行优化，保持原意不变，提升文采和节奏感。直接输出润色后的全文。${skillNote}`;
        userContent = `章节：${chapterEditTitle}\n\n用户润色要求：${aiUserPrompt}\n\n原文：\n${chapterEditContent}`;
      }

      const messages = [
        { role: 'system', content: systemContent },
        { role: 'user', content: userContent },
      ];

      const response = await api.aiChatStream(messages);
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
                setAiGeneratedContent(fullContent);
              }
            } catch (e: any) {
              if (e.message && !e.message.includes('JSON')) {
                setAiStreamError(e.message);
              }
            }
          }
        }
      }
    } catch (e: any) {
      setAiStreamError(e.message || 'AI创作失败，请检查AI配置');
    }
    setAiCreating(false);
  }

  // 确认AI生成内容，填入章节
  function confirmAiContent() {
    if (!aiCreateMode || !aiGeneratedContent.trim()) return;
    const content = aiGeneratedContent;
    if (aiCreateMode === 'continue') {
      const newContent = chapterEditContent
        ? chapterEditContent.replace(/\s+$/, '') + '\n\n' + content
        : content;
      setChapterEditContent(newContent);
    } else {
      setChapterEditContent(content);
    }
    setAiCreateMode(null);
    setAiGeneratedContent('');
    setAiStreamError('');
  }

  // 取消AI创作
  function cancelAiCreate() {
    setAiCreateMode(null);
    setAiGeneratedContent('');
    setAiCreating(false);
    setAiStreamError('');
    setAiUserPrompt('');
  }

  // 一键排版（按小说阅读习惯）
  function formatChapter() {
    if (!chapterEditContent.trim()) {
      alert('没有内容需要排版');
      return;
    }
    const original = chapterEditContent;
    let text = chapterEditContent;
    // 统一换行符
    text = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    // 去除全角空格和段首空格（网文不需要缩进）
    text = text.replace(/\u3000/g, '');
    text = text.split('\n').map((line: string) => line.replace(/^[\s]+/, '').replace(/[\s]+$/, '')).join('\n');
    // 中文引号统一
    text = text.replace(/"/g, '\u201C').replace(/"/g, '\u201D');
    // 修正省略号：。。。/.../··· → ……
    text = text.replace(/\.{3,}/g, '\u2026\u2026').replace(/。{3,}/g, '\u2026\u2026').replace(/·{3,}/g, '\u2026\u2026');
    // 修正破折号：-- → ——
    text = text.replace(/-{2,}/g, '\u2014\u2014');
    // 单个破折号变双破折号（用正则一次性处理，避免 while 循环）
    text = text.replace(/(?<!\u2014)\u2014(?!\u2014)/g, '\u2014\u2014');
    // 把三个以上破折号裁成两个
    text = text.replace(/\u2014{3,}/g, '\u2014\u2014');
    // 重复标点修正
    text = text.replace(/。{2,}(?!…)/g, '。').replace(/，{2,}/g, '，').replace(/！{2,}/g, '！').replace(/？{2,}/g, '？');
    // 合并连续空行为一个（段落间统一空一行）
    text = text.replace(/\n{2,}/g, '\n\n');
    // 去除首尾多余空白
    text = text.replace(/^[\s\n]+/, '').replace(/[\s\n]+$/, '');
    setChapterEditContent(text);
    // 给出反馈，让用户知道排版已完成
    const changed = text !== original;
    const paraCount = text.split(/\n\n+/).filter(p => p.trim()).length;
    alert(changed ? `✅ 排版完成（共 ${paraCount} 段）` : '✅ 内容已是规范格式，无需调整');
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

  // 判断是否是图谱类 tab
  const isGraphTab = ['relationGraph', 'realmGraph', 'locationGraph'].includes(activeTab);
  const isMapTab = activeTab === 'map';
  const isChapterTab = activeTab === 'chapters';
  const isOutlineTab = activeTab === 'outline';
  const isDynamicMemoryTab = activeTab === 'dynamicMemory';
  const isCharacterTab = activeTab === 'characters';
  const isPlotTab = activeTab === 'plot';

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
          {chapters.length > 0 && (
            <button className="btn-ghost-sm" onClick={handleAnalyzeContent} disabled={analyzing || dimAnalyzing} title="AI分析章节内容，一键识别全部维度">
              {analyzing ? '🤖 识别中...' : '🔍 全部识别'}
            </button>
          )}
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
          />
        ) : isMapTab ? (
          <MapPanel
            bookId={bookId}
            locations={bible?.locations || ''}
            onUpdate={handleMapUpdate}
            showConfirm={showConfirm}
            onAnalyzeFromReports={onAnalyzeFromReportsLocations}
            dimAnalyzing={dimAnalyzing}
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
            onEditAiContent={setAiGeneratedContent}
            onEditAiPrompt={setAiUserPrompt}
            onFormat={formatChapter}
            onRenameVolume={renameVolume}
            onDeleteVolume={deleteVolumeFn}
            bookId={bookId}
          />
        ) : isGraphTab ? (
          <GraphPanel
            type={activeTab as 'relationGraph' | 'realmGraph' | 'locationGraph'}
            data={currentContent}
            concept={concept || bible?.concept || ''}
            charactersData={bible?.character_profiles || ''}
            onAnalyzeFromReports={onAnalyzeFromReportsGraph}
            dimAnalyzing={dimAnalyzing}
            bookId={bookId}
            onUpdate={handleGraphUpdate}
          />
        ) : isDynamicMemoryTab ? (
          <DynamicMemoryPanel
            bookId={bookId}
            concept={concept || bible?.concept || ''}
            bible={bible}
            chapters={chapters}
            showConfirm={showConfirm}
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
          />
        ) : isCharacterTab ? (
          <CharacterPanel
            bookId={bookId || ''}
            bible={bible}
            onBibleUpdate={setBible}
            bookTitle={book?.title || ''}
            hasChapters={chapters.length > 0}
            showConfirm={showConfirm}
            skillPacks={skillPacks}
            selectedSkillPackIds={selectedSkillPackIds}
            onToggleSkillPack={toggleSkillPack}
            selectedSkillPacks={selectedSkillPacks}
          />
        ) : isPlotTab ? (
          <PlotPanel
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
          />
        )}
      </div>
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
}) {
  const { concept, setConcept, bible, bookTitle, brainstorming, brainstormResult, brainstormError, adoptedSuggestions, onBrainstorm, onAdopt, bookId, onBibleUpdate,
    hasChapters, conceptAiMode, conceptAiPrompt, conceptAiAssisting, conceptAiError,
    onStartConceptAi, onExecuteConceptAi, onCancelConceptAi, onEditConceptAiPrompt,
    onAnalyzeDimension, dimAnalyzing,
    skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks } = props;

  const [skillExpanded, setSkillExpanded] = useState(false);
  const selectedCount = selectedSkillPackIds.length;

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
        {bookTitle && (
          <div className="bible-context-bar">
            <span className="bible-context-book">📖 {bookTitle}</span>
            <span className="bible-context-sep">›</span>
            <span className="bible-context-dim">💡 AI创作 · 构思</span>
          </div>
        )}
        <div className="bible-edit-header">
          <h3>💡 AI协同创作 · 构思</h3>
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
                <div className="skill-pack-checkbox-list">
                  {skillPacks.map(p => (
                    <label key={p.id} className={`skill-pack-checkbox-item ${selectedSkillPackIds.includes(p.id) ? 'checked' : ''}`}>
                      <input type="checkbox" checked={selectedSkillPackIds.includes(p.id)} onChange={() => onToggleSkillPack(p.id)} disabled={conceptAiAssisting} />
                      <span className="skill-pack-checkbox-icon">{p.icon}</span>
                      <span className="skill-pack-checkbox-name">{p.name}</span>
                    </label>
                  ))}
                </div>
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
            <span className="ai-prompt-hint">Enter 发送 · Shift+Enter 换行</span>
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
      {bookTitle && (
        <div className="bible-context-bar">
          <span className="bible-context-book">📖 {bookTitle}</span>
          <span className="bible-context-sep">›</span>
          <span className="bible-context-dim">💡 构思</span>
        </div>
      )}
      <div className="concept-input-section">
        <div className="concept-label-row">
          <label className="concept-label">一句话构思</label>
          {hasChapters && (
            <button className="btn-ghost-sm concept-dim-analyze-btn" onClick={onAnalyzeDimension} disabled={dimAnalyzing} title="AI分析已有章节，自动识别构思">
              {dimAnalyzing ? '🤖 识别中...' : '🔍 AI识别'}
            </button>
          )}
        </div>
        <textarea
          className="input concept-textarea"
          rows={6}
          placeholder="例如：一个程序员穿越到修仙世界，用代码思维重新定义修炼体系..."
          value={concept}
          onChange={e => setConcept(e.target.value)}
        />
        <div className="concept-actions">
          <button className="btn-primary" onClick={onBrainstorm} disabled={!concept.trim() || brainstorming}>
            {brainstorming ? '🤖 AI构思中...' : '✨ AI 头脑风暴'}
          </button>
          <button className="btn-ghost-sm" onClick={onStartConceptAi} disabled={brainstorming} title="输入要求，AI协同生成构思内容">
            🤖 AI协同创作
          </button>
          {concept !== (bible?.concept || '') && (
            <button className="btn-ghost-sm" onClick={async () => {
              if (!bookId) return;
              const updated = await api.updateBible(bookId, { concept } as any);
              onBibleUpdate(updated);
            }}>保存构思</button>
          )}
        </div>
        {brainstormError && <div className="error-msg">{brainstormError}</div>}
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
    </div>
  );
}

/* ===== 章节管理面板 ===== */
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
  onEditAiContent: (v: string) => void;
  onEditAiPrompt: (v: string) => void;
  onFormat: () => void;
  onRenameVolume: (volId: string, newTitle: string) => Promise<void>;
  onDeleteVolume: (volId: string) => Promise<void>;
  bookId?: string;
}) {
  const { chapters, activeChapter, chapterEditing, chapterEditTitle, chapterEditContent, chapterSaving,
    aiCreateMode, aiGeneratedContent, aiCreating, aiStreamError, aiUserPrompt,
    skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks,
    onSelectChapter, onCreateChapter, onCreateVolume, onSaveChapter, onDeleteChapter, onCancelEdit, onStartEdit,
    onEditTitle, onEditContent, onBackToList, onStartAiCreate, onExecuteAiCreate, onConfirmAiContent, onCancelAiCreate, onEditAiContent, onEditAiPrompt, onFormat,
    onRenameVolume, onDeleteVolume, bookId,
  } = props;

  const [skillExpanded, setSkillExpanded] = useState(false);
  const [expandedVolumes, setExpandedVolumes] = useState<Record<string, boolean>>({});
  const [renamingVolId, setRenamingVolId] = useState<string | null>(null);
  const [renameVolTitle, setRenameVolTitle] = useState('');
  const selectedCount = selectedSkillPackIds.length;

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
    const valid = picked.filter(f => /\.(txt|md|docx|zip|json)$/i.test(f.name));
    if (valid.length === 0) {
      setImportChaptersError('请选择 txt/md/docx/zip 格式的文件');
      return;
    }
    setImportChaptersError('');
    setImportingChapters(true);
    try {
      const result = await api.importChapters(bookId, valid);
      alert(`成功追加 ${result.added} 章，当前共 ${result.total} 章`);
      // 刷新章节列表
      try {
        const updated = await api.listChapters(bookId);
        // 重新加载逻辑由父组件的回调处理；这里通过页面刷新最简单
        window.location.reload();
      } catch { /* ignore */ }
    } catch (err: any) {
      setImportChaptersError(err.message || '导入失败');
      alert('追加导入失败: ' + (err.message || '未知错误'));
    } finally {
      setImportingChapters(false);
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

  // AI创作面板（用户提问协同创作模式）
  if (activeChapter && chapterEditing && aiCreateMode) {
    const modeLabels: Record<string, { title: string; icon: string; hint: string; placeholder: string }> = {
      write: { title: 'AI写作', icon: '🤖', hint: '告诉AI你想写什么，AI将根据你的要求和故事设定创作整章内容', placeholder: '例如：主角在森林中遇到神秘老人，获得传承，但代价是失去一段记忆...' },
      continue: { title: 'AI续写', icon: '✨', hint: '告诉AI接下来的剧情走向，AI将续写并自然衔接已有内容', placeholder: '例如：接下来主角进入城中，遇到反派挑衅，发生冲突...' },
      polish: { title: 'AI润色', icon: '💎', hint: '告诉AI你的润色要求，AI将优化文字但保持原意', placeholder: '例如：增加环境描写，让对话更口语化，加快节奏...' },
    };
    const info = modeLabels[aiCreateMode];
    const hasResult = aiGeneratedContent.trim().length > 0;
    return (
      <div className="ai-create-panel">
        {/* 顶部：标题栏 */}
        <div className="ai-create-header">
          <div className="ai-create-header-left">
            <button className="btn-ghost-sm" onClick={onCancelAiCreate} disabled={aiCreating}>← 返回编辑</button>
            <span className="ai-create-title">{info.icon} {info.title}</span>
            {aiCreating && <span className="ai-create-status">生成中...</span>}
          </div>
          {hasResult && !aiCreating && (
            <button className="btn-primary-sm" onClick={onConfirmAiContent}>
              ✓ 确认填入
            </button>
          )}
        </div>

        {/* 中间：内容展示区（可滚动） */}
        <div className="ai-create-content-wrap">
          {aiCreating && !aiGeneratedContent ? (
            <div className="ai-create-loading">
              <div className="loading-spinner" />
              <p>AI正在结合{selectedSkillPacks.length > 0 ? selectedSkillPacks.map(p => p.name).join('、') : '设定'}创作中，内容将实时显示...</p>
            </div>
          ) : hasResult ? (
            <>
              <textarea
                className="input ai-create-textarea"
                value={aiGeneratedContent}
                onChange={e => onEditAiContent(e.target.value)}
                placeholder="AI生成的内容将在这里显示，你可以编辑后再填入章节..."
                rows={20}
              />
              {aiCreating && (
                <div className="ai-create-streaming-hint">
                  <span className="loading-dot" /> 正在生成...
                </div>
              )}
              <div className="ai-create-footer">
                <span className="text-muted">字数：{aiGeneratedContent.length}</span>
                <span className="text-muted">章节：{chapterEditTitle}</span>
              </div>
            </>
          ) : (
            <div className="ai-create-empty">
              <span className="ai-create-empty-icon">{info.icon}</span>
              <p>{info.hint}</p>
              <p className="text-muted">在下方输入你的创作要求，AI会结合故事设定{selectedSkillPacks.length > 0 ? `和「${selectedSkillPacks.map(p => p.name).join('、')}」技能包` : ''}来生成内容</p>
            </div>
          )}
          {aiStreamError && <div className="error-msg" style={{marginTop:8}}>{aiStreamError}</div>}
        </div>

        {/* 底部：控制区（技能包+输入框，固定在底部） */}
        <div className="ai-create-control-bar">
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
                  <div className="skill-pack-checkbox-list">
                    {skillPacks.map(p => (
                      <label key={p.id} className={`skill-pack-checkbox-item ${selectedSkillPackIds.includes(p.id) ? 'checked' : ''}`}>
                        <input
                          type="checkbox"
                          checked={selectedSkillPackIds.includes(p.id)}
                          onChange={() => onToggleSkillPack(p.id)}
                          disabled={aiCreating}
                        />
                        <span className="skill-pack-checkbox-icon">{p.icon}</span>
                        <span className="skill-pack-checkbox-name">{p.name}</span>
                      </label>
                    ))}
                  </div>
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

          {/* 用户提问输入区 */}
          <div className="ai-prompt-section ai-prompt-vertical">
            <textarea
              className="input ai-prompt-input"
              value={aiUserPrompt}
              onChange={e => onEditAiPrompt(e.target.value)}
              onKeyDown={handlePromptKeyDown}
              placeholder={info.placeholder}
              rows={4}
              disabled={aiCreating}
            />
            <div className="ai-prompt-bottom-row">
              <span className="ai-prompt-hint">Enter 发送 · Shift+Enter 换行</span>
              <button className="btn-primary ai-prompt-submit" onClick={onExecuteAiCreate} disabled={aiCreating || !aiUserPrompt.trim()}>
                {aiCreating ? '⏳ 创作中...' : (hasResult ? '🔄 重新生成' : '🚀 发送')}
              </button>
            </div>
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
        <div className="chapter-edit-toolbar">
          <button className="btn-ghost-sm chapter-ai-btn chapter-ai-write-btn" onClick={() => onStartAiCreate('write')} disabled={aiCreating} title="AI根据构思和设定生成整章内容">
            🤖 AI创作
          </button>
          <button className="btn-ghost-sm chapter-ai-btn" onClick={() => onStartAiCreate('continue')} disabled={aiCreating} title="AI根据已有内容继续创作">
            ✨ 续写
          </button>
          <button className="btn-ghost-sm chapter-ai-btn" onClick={() => onStartAiCreate('polish')} disabled={aiCreating || !chapterEditContent.trim()} title="AI优化当前文字">
            💎 润色
          </button>
          <button className="btn-ghost-sm chapter-format-btn" onClick={onFormat} disabled={!chapterEditContent.trim()} title="一键排版：修正段落、标点、空行，适配小说阅读模式">
            📐 排版
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

  // 分离卷和章节
  const volumes = chapters.filter(c => c.is_volume);
  const orphanChapters = chapters.filter(c => !c.is_volume && !c.parent_id);
  const volumeChapters = (volId: string) => chapters.filter(c => !c.is_volume && c.parent_id === volId);

  return (
    <div className="chapter-list-panel">
      <div className="chapter-list-header">
        <h3>📚 章节 <span className="chapter-count">{chapters.filter(c => !c.is_volume).length}章</span></h3>
        <div style={{display:'flex',gap:6,flexWrap:'wrap',justifyContent:'flex-end'}}>
          <button className="btn-ghost-sm" onClick={() => onCreateVolume()} title="新建卷">📂 新卷</button>
          <button className="btn-secondary-sm" onClick={() => importChaptersRef.current?.click()} disabled={importingChapters || !bookId} title="从 txt/md/docx/zip 文件追加章节，不影响已有章节">
            {importingChapters ? '⏳ 导入中...' : '📥 导入章节'}
          </button>
          <button className="btn-primary-sm" onClick={() => onCreateChapter()}>+ 新章节</button>
        </div>
        <input
          ref={importChaptersRef}
          type="file"
          multiple
          accept=".txt,.md,.docx,.zip,.json"
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
          {/* 按卷分组显示 */}
          {volumes.map(vol => {
            const volChs = volumeChapters(vol.id);
            const expanded = expandedVolumes[vol.id] !== false; // 默认展开
            const isRenaming = renamingVolId === vol.id;
            return (
              <div key={vol.id} className="chapter-volume-group">
                <div
                  className="chapter-volume-header"
                >
                  <span className="chapter-volume-arrow" onClick={() => setExpandedVolumes(prev => ({ ...prev, [vol.id]: !expanded }))}>
                    {expanded ? '▼' : '▶'}
                  </span>
                  {isRenaming ? (
                    <input
                      className="input chapter-volume-rename-input"
                      value={renameVolTitle}
                      onChange={e => setRenameVolTitle(e.target.value)}
                      onBlur={async () => {
                        if (renameVolTitle.trim()) { await onRenameVolume(vol.id, renameVolTitle.trim()); }
                        setRenamingVolId(null);
                      }}
                      onKeyDown={e => { if (e.key === 'Enter') { (e.target as HTMLInputElement).blur(); } }}
                      autoFocus
                      onClick={e => e.stopPropagation()}
                    />
                  ) : (
                    <span className="chapter-volume-title">📁 {vol.title}</span>
                  )}
                  <span className="chapter-volume-count">{volChs.length}章</span>
                  <button className="btn-ghost-sm chapter-volume-add" onClick={e => { e.stopPropagation(); setRenamingVolId(vol.id); setRenameVolTitle(vol.title); }} title="重命名">✏️</button>
                  <button
                    className="btn-ghost-sm chapter-volume-add"
                    onClick={e => { e.stopPropagation(); onCreateChapter(vol.id); }}
                    title="在此卷下添加章节"
                  >+</button>
                  <button className="btn-ghost-sm chapter-volume-add" onClick={e => { e.stopPropagation(); onDeleteVolume(vol.id); }} title="删除此卷" style={{color:'#e74c3c'}}>🗑️</button>
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
          })}
          {/* 未分卷的章节 */}
          {orphanChapters.length > 0 && (
            <div className="chapter-volume-group">
              <div
                className="chapter-volume-header"
                onClick={() => volumes.length > 0 && setExpandedVolumes(prev => ({ ...prev, '__orphan__': prev['__orphan__'] === false }))}
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
  hasChapters: boolean;
  showConfirm: (message: string, onConfirm: () => void) => void;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onToggleSkillPack: (id: string) => void;
  selectedSkillPacks: SkillPack[];
}) {
  const { bookId, bible, onBibleUpdate, bookTitle, hasChapters, showConfirm, skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks } = props;
  const [characters, setCharacters] = useState<CharacterData[]>([]);
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [addingNew, setAddingNew] = useState(false);
  const [editForm, setEditForm] = useState<CharacterData>({ name: '', role: '', identity: '', personality: '', motivation: '', background: '', relationships: '', abilities: '', items: '' });
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzingName, setAnalyzingName] = useState('');
  const [aiMode, setAiMode] = useState(false);
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiAssisting, setAiAssisting] = useState(false);
  const [aiError, setAiError] = useState('');
  const [skillExpanded, setSkillExpanded] = useState(false);
  const [collapsedChars, setCollapsedChars] = useState<Set<number>>(new Set());

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

  // AI识别全部角色
  async function handleAnalyzeAll() {
    showConfirm('将用 AI 分析已有章节内容，自动识别所有角色。是否继续？', async () => {
      setAnalyzing(true);
      try {
        const result = await api.analyzeCharacter(bookId, '');
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已识别 ${result.characters.length} 个角色`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzing(false);
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
              <div className="skill-pack-checkbox-list">
                {skillPacks.map(p => (
                  <label key={p.id} className={`skill-pack-checkbox-item ${selectedSkillPackIds.includes(p.id) ? 'checked' : ''}`}>
                    <input type="checkbox" checked={selectedSkillPackIds.includes(p.id)} onChange={() => onToggleSkillPack(p.id)} disabled={aiAssisting} />
                    <span className="skill-pack-checkbox-icon">{p.icon}</span>
                    <span className="skill-pack-checkbox-name">{p.name}</span>
                  </label>
                ))}
              </div>
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
            <span className="ai-prompt-hint">Enter 发送 · Shift+Enter 换行</span>
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
      {bookTitle && (
        <div className="bible-context-bar">
          <span className="bible-context-book">📖 {bookTitle}</span>
          <span className="bible-context-sep">›</span>
          <span className="bible-context-dim">👤 人物及关系</span>
          {characters.length > 0 && <span className="bible-context-count">{characters.length}人</span>}
        </div>
      )}
      <div className="bible-edit-header">
        <h3>👤 人物及关系</h3>
        <div className="bible-edit-actions">
          <button className="btn-ghost-sm" onClick={() => { setAiMode(true); setAiError(''); setAiPrompt(''); }}>
            ✨ AI创作
          </button>
          {hasChapters && (
            <button className="btn-ghost-sm" onClick={handleAnalyzeAll} disabled={analyzing} title="AI分析已有章节，自动识别全部角色">
              {analyzing ? '🤖 识别中...' : '🔍 全部识别'}
            </button>
          )}
          <button className="btn-primary-sm" onClick={startAddNew}>＋ 添加角色</button>
        </div>
      </div>

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

      {/* 角色卡片列表 */}
      {characters.length === 0 ? (
        <div className="bible-empty">
          <span className="bible-empty-icon">👤</span>
          <p>暂无角色信息</p>
          <p className="text-muted">点击「添加角色」手动添加，或用AI识别自动提取</p>
          <div className="bible-empty-actions">
            <button className="btn-primary-sm" onClick={startAddNew}>＋ 添加角色</button>
            {hasChapters && (
              <button className="btn-ghost-sm" onClick={handleAnalyzeAll} disabled={analyzing}>
                {analyzing ? '⏳ 识别中...' : '🔍 AI识别'}
              </button>
            )}
          </div>
        </div>
      ) : (
        <div className="character-cards-grid">
          {characters.map((char, idx) => (
            <div key={idx} className="character-card">
              <div className="character-card-header" onClick={() => toggleChar(idx)} style={{cursor:'pointer'}}>
                <span className="map-toggle" style={{fontSize:10,marginRight:4}}>{collapsedChars.has(idx) ? '▶' : '▼'}</span>
                <span className="character-card-name">{char.name}</span>
                {char.role && <span className="character-card-role">{char.role}</span>}
                {char.abilities && <span className="text-muted" style={{fontSize:10,marginLeft:4}}>{(char.abilities || '').slice(0, 12)}</span>}
              </div>
              {!collapsedChars.has(idx) && (
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
              {!collapsedChars.has(idx) && (
              <div className="character-card-actions">
                {hasChapters && (
                  <button className="btn-ghost-sm" onClick={() => handleAnalyzeOne(char.name)} disabled={analyzingName === char.name} title="AI识别此角色信息">
                    {analyzingName === char.name ? '🤖 识别中...' : '🔍 识别'}
                  </button>
                )}
                <button className="btn-ghost-sm" onClick={() => startEdit(idx)}>✏️ 编辑</button>
                <button className="btn-ghost-sm" onClick={() => deleteChar(idx)} style={{color:'#e74c3c'}}>🗑️</button>
              </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ===== 剧情面板（按卷） ===== */
function PlotPanel(props: {
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
}) {
  const { bookId, bible, onBibleUpdate, bookTitle, chapters, hasChapters, showConfirm, skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks } = props;
  const [volumes, setVolumes] = useState<any[]>([]);
  const [editingVol, setEditingVol] = useState<string | null>(null);
  const [editForm, setEditForm] = useState('');
  const [analyzingVol, setAnalyzingVol] = useState('');
  const [aiMode, setAiMode] = useState(false);
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiAssisting, setAiAssisting] = useState(false);
  const [aiError, setAiError] = useState('');
  const [skillExpanded, setSkillExpanded] = useState(false);
  const [collapsedVols, setCollapsedVols] = useState<Set<number>>(new Set());
  const [editingVolName, setEditingVolName] = useState<string | null>(null);
  const [editVolName, setEditVolName] = useState('');

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
    try {
      const parsed = JSON.parse(bible.timeline);
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

  function startEditVol(volId: string, content: string) {
    setEditingVol(volId);
    setEditForm(content);
  }

  async function saveEditVol(volId: string) {
    const newVols = volumes.map(v => {
      if ((v.volume_id || '') === volId || v.volume === volId) {
        return { ...v, main_plot: editForm };
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
    setEditForm('');
  }

  async function deleteVolume(idx: number) {
    const vol = volumes[idx];
    showConfirm(`确定删除「${vol.volume || '该卷'}」的剧情？`, async () => {
      const newVols = volumes.filter((_, i) => i !== idx);
      await saveVolumes(newVols);
    });
  }

  // AI识别指定卷剧情
  async function handleAnalyzeVolume(volId: string, volTitle: string) {
    showConfirm(`将用 AI 分析「${volTitle}」的章节内容，自动识别剧情大纲。是否继续？`, async () => {
      setAnalyzingVol(volId || volTitle);
      try {
        const result = await api.analyzePlotVolume(bookId, volId, volTitle);
        if (result.bible) onBibleUpdate(result.bible);
        alert(`AI识别完成！已填充「${volTitle}」的剧情`);
      } catch (e: any) {
        alert('AI识别失败：' + (e.message || '请检查AI配置'));
      }
      setAnalyzingVol('');
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

  // 合并卷列表和已有数据
  const displayVolumes = useMemo(() => {
    const result: any[] = [];
    const usedVolIds = new Set<string>();
    // 先添加有章节的卷
    for (const vc of volumeChapters) {
      const volData = volumes.find(v => v.volume_id === vc.id) || volumes.find(v => v.volume === vc.title);
      result.push({
        volume_id: vc.id,
        volume: vc.title,
        main_plot: volData?.main_plot || '',
        key_events: volData?.key_events || [],
        turning_points: volData?.turning_points || [],
        climax: volData?.climax || '',
        ending: volData?.ending || '',
        foreshadowing: volData?.foreshadowing || [],
        chapter_count: chapters.filter(c => c.parent_id === vc.id).length,
      });
      usedVolIds.add(volData?.volume_id || '');
      if (volData) usedVolIds.add(volData.volume || '');
    }
    // 再添加没有对应章节卷的数据
    for (const v of volumes) {
      const id = v.volume_id || v.volume;
      if (!usedVolIds.has(id) && v.volume !== '全部剧情') {
        result.push({ ...v, chapter_count: 0 });
      }
    }
    // 如果没有卷，但有全部剧情
    if (result.length === 0 && volumes.length > 0) {
      result.push(...volumes);
    }
    return result;
  }, [volumeChapters, volumes, chapters]);

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
              <div className="skill-pack-checkbox-list">
                {skillPacks.map(p => (
                  <label key={p.id} className={`skill-pack-checkbox-item ${selectedSkillPackIds.includes(p.id) ? 'checked' : ''}`}>
                    <input type="checkbox" checked={selectedSkillPackIds.includes(p.id)} onChange={() => onToggleSkillPack(p.id)} disabled={aiAssisting} />
                    <span className="skill-pack-checkbox-icon">{p.icon}</span>
                    <span className="skill-pack-checkbox-name">{p.name}</span>
                  </label>
                ))}
              </div>
            )}
          </div>
        )}
        <div className="ai-prompt-vertical">
          <textarea className="input bible-ai-prompt-input" rows={6} value={aiPrompt} onChange={e => setAiPrompt(e.target.value)} onKeyDown={handlePromptKeyDown} placeholder="例如：生成三卷的剧情大纲，每卷包含主线和关键事件..." disabled={aiAssisting} autoFocus />
          <div className="ai-prompt-bottom-row">
            <span className="ai-prompt-hint">Enter 发送 · Shift+Enter 换行</span>
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
      {bookTitle && (
        <div className="bible-context-bar">
          <span className="bible-context-book">📖 {bookTitle}</span>
          <span className="bible-context-sep">›</span>
          <span className="bible-context-dim">📖 剧情</span>
          {displayVolumes.length > 0 && <span className="bible-context-count">{displayVolumes.length}卷</span>}
        </div>
      )}
      <div className="bible-edit-header">
        <h3>📖 剧情（按卷）</h3>
        <div className="bible-edit-actions">
          <button className="btn-ghost-sm" onClick={() => { setAiMode(true); setAiError(''); setAiPrompt(''); }}>✨ AI创作</button>
          <button className="btn-primary-sm" onClick={addVolumeOutline}>＋ 添加卷大纲</button>
        </div>
      </div>

      {displayVolumes.length === 0 ? (
        <div className="bible-empty">
          <span className="bible-empty-icon">📖</span>
          <p>暂无剧情信息</p>
          <p className="text-muted">点击「添加卷大纲」手动添加，或用AI识别自动提取</p>
          <div className="bible-empty-actions">
            <button className="btn-primary-sm" onClick={addVolumeOutline}>＋ 添加卷大纲</button>
            {hasChapters && (
              <button className="btn-ghost-sm" onClick={() => handleAnalyzeVolume('', '全部章节')}>
                🔍 AI识别全部
              </button>
            )}
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
                  <h4 onDoubleClick={() => { setEditingVolName(vol.volume); setEditVolName(vol.volume); }}>{vol.volume || `第${idx + 1}卷`}</h4>
                )}
                {vol.chapter_count !== undefined && <span className="text-muted" style={{fontSize:12}}>{vol.chapter_count}章</span>}
                <div className="plot-volume-actions" onClick={e => e.stopPropagation()}>
                  {hasChapters && (
                    <button className="btn-ghost-sm" onClick={() => handleAnalyzeVolume(vol.volume_id || '', vol.volume || `第${idx + 1}卷`)} disabled={analyzingVol === (vol.volume_id || vol.volume)} title="AI识别此卷剧情">
                      {analyzingVol === (vol.volume_id || vol.volume) ? '🤖 识别中...' : '🔍 识别'}
                    </button>
                  )}
                  <button className="btn-ghost-sm" onClick={() => startEditVol(vol.volume_id || vol.volume, vol.main_plot || '')}>✏️ 编辑</button>
                  <button className="btn-ghost-sm" onClick={() => deleteVolume(idx)} style={{color:'#e74c3c'}}>🗑️</button>
                </div>
              </div>
              {!collapsedVols.has(idx) && (editingVol === (vol.volume_id || vol.volume) ? (
                <div style={{marginTop:8}}>
                  <textarea className="input" rows={6} value={editForm} onChange={e => setEditForm(e.target.value)} placeholder="该卷主线剧情..." autoFocus />
                  <div style={{display:'flex',gap:8,marginTop:8}}>
                    <button className="btn-primary-sm" onClick={() => saveEditVol(vol.volume_id || vol.volume)}>💾 保存</button>
                    <button className="btn-ghost-sm" onClick={() => setEditingVol(null)}>取消</button>
                  </div>
                </div>
              ) : (
                <div className="plot-volume-body">
                  {vol.main_plot ? <p>{vol.main_plot}</p> : <p className="text-muted" style={{fontSize:13}}>暂无剧情，点击「编辑」或「识别」添加</p>}
                  {vol.key_events && vol.key_events.length > 0 && (
                    <div className="plot-events">
                      <b>关键事件：</b>
                      <ul>{vol.key_events.map((ev: string, i: number) => <li key={i}>{ev}</li>)}</ul>
                    </div>
                  )}
                  {vol.turning_points && vol.turning_points.length > 0 && (
                    <div className="plot-events">
                      <b>转折点：</b>
                      <ul>{vol.turning_points.map((tp: string, i: number) => <li key={i}>{tp}</li>)}</ul>
                    </div>
                  )}
                  {vol.climax && <p><b>高潮：</b>{vol.climax}</p>}
                  {vol.ending && <p><b>结局：</b>{vol.ending}</p>}
                  {vol.foreshadowing && vol.foreshadowing.length > 0 && (
                    <div className="plot-events">
                      <b>伏笔：</b>
                      <ul>{vol.foreshadowing.map((f: string, i: number) => <li key={i}>{f}</li>)}</ul>
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
}) {
  const { tab, bookTitle, content, editing, editValue, saving, aiAssisting, aiError, bibleAiMode, bibleAiPrompt,
    skillPacks, selectedSkillPackIds, onToggleSkillPack, selectedSkillPacks,
    hasChapters, dimAnalyzing, onAnalyzeDimension,
    onStartEdit, onSaveEdit, onCancelEdit, onEditChange, onAIAssist, onExecuteAi, onCancelAi, onEditAiPrompt, onDelete } = props;

  const [skillExpanded, setSkillExpanded] = useState(false);
  const [showTips, setShowTips] = useState(false);
  const selectedCount = selectedSkillPackIds.length;
  const wordCount = useMemo(() => {
    if (!content) return 0;
    const cn = (content.match(/[\u4e00-\u9fa5]/g) || []).length;
    const en = (content.match(/[a-zA-Z]+/g) || []).length;
    return cn + en;
  }, [content]);

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
          <div className="skill-pack-checkbox-list">
            {skillPacks.map(p => (
              <label key={p.id} className={`skill-pack-checkbox-item ${selectedSkillPackIds.includes(p.id) ? 'checked' : ''}`}>
                <input
                  type="checkbox"
                  checked={selectedSkillPackIds.includes(p.id)}
                  onChange={() => onToggleSkillPack(p.id)}
                  disabled={aiAssisting}
                />
                <span className="skill-pack-checkbox-icon">{p.icon}</span>
                <span className="skill-pack-checkbox-name">{p.name}</span>
              </label>
            ))}
          </div>
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
        {bookTitle && (
          <div className="bible-context-bar">
            <span className="bible-context-book">📖 {bookTitle}</span>
            <span className="bible-context-sep">›</span>
            <span className="bible-context-dim">{tab.icon} AI创作 · {tab.label}</span>
          </div>
        )}
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
            <span className="ai-prompt-hint">Enter 发送 · Shift+Enter 换行</span>
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
      {/* 上下文面包屑 */}
      {bookTitle && (
        <div className="bible-context-bar">
          <span className="bible-context-book">📖 {bookTitle}</span>
          <span className="bible-context-sep">›</span>
          <span className="bible-context-dim">{tab.icon} {tab.label}</span>
          {wordCount > 0 && <span className="bible-context-count">{wordCount}字</span>}
        </div>
      )}
      <div className="bible-edit-header">
        <h3>{tab.icon} {tab.label}</h3>
        <div className="bible-edit-actions">
          {!editing ? (
            <>
              <button className="btn-ghost-sm" onClick={onAIAssist} disabled={aiAssisting}>
                {aiAssisting ? '🤖 生成中...' : '✨ AI创作'}
              </button>
              {hasChapters && (
                <button className="btn-ghost-sm" onClick={onAnalyzeDimension} disabled={dimAnalyzing} title="AI分析已有章节，自动识别此维度内容">
                  {dimAnalyzing ? '🤖 识别中...' : '🔍 AI识别'}
                </button>
              )}
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
              <p>🔍 点击「AI识别」可从已有章节中提取{tab.label}信息</p>
              <p>✏️ 点击内容区域可直接编辑</p>
              {selectedSkillPacks.length > 0 && <p>📦 已选{selectedCount}个技能包协同创作</p>}
            </div>
          )}
        </>
      ) : (
        <div className="bible-empty" onClick={onStartEdit}>
          <span className="bible-empty-icon">{tab.icon}</span>
          <p>暂无{tab.label}内容</p>
          <p className="text-muted">点击此处编辑，或使用上方按钮AI创作</p>
          <div className="bible-empty-actions">
            <button className="btn-primary-sm" onClick={(e) => { e.stopPropagation(); onAIAssist(); }} disabled={aiAssisting}>
              {aiAssisting ? '⏳ 生成中...' : '✨ AI创作'}
            </button>
            {hasChapters && (
              <button className="btn-ghost-sm" onClick={(e) => { e.stopPropagation(); onAnalyzeDimension(); }} disabled={dimAnalyzing}>
                {dimAnalyzing ? '⏳ 识别中...' : '🔍 AI识别'}
              </button>
            )}
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
  // 自动分卷
  const [targetWords, setTargetWords] = useState<number>(0);
  const [showVolumeCalc, setShowVolumeCalc] = useState(false);

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

  // 自动分卷计算：每卷50章，每章2400字。按金番作者 Step 4 + Step 5 体系
  const [volumeGenerating, setVolumeGenerating] = useState(false);
  const [volumeData, setVolumeData] = useState<any[]>([]);
  const [expandedVol, setExpandedVol] = useState<Set<number>>(new Set());

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

  function generateVolumeBreakdown() {
    if (targetWords <= 0) { alert('请先输入小说目标字数'); return; }
    const CH_PER_VOL = 50;
    const WORDS_PER_CH = 2400;
    const totalCh = Math.ceil(targetWords / WORDS_PER_CH);
    const volCount = Math.ceil(totalCh / CH_PER_VOL);

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

      // 生成5-8个情节节点
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

  // AI辅助生成卷大纲
  async function aiGenerateVolumeOutline(volIdx: number) {
    if (!bookId) return;
    const vol = volumeData[volIdx];
    if (!vol) return;
    setVolumeGenerating(true);
    try {
      const contextConcept = concept || bible?.concept || '暂无构思';
      const existingOutline = bible?.plot_design?.slice(0, 800) || '无';
      const worldSetting = bible?.worldbuilding?.slice(0, 500) || '无';
      const skillKeys = ['volume_breakdown', 'master_outline', 'tomato_outline'];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
      const msgs = [
        { role: 'system', content: `你是番茄小说金番作者。请按以下模板为第${vol.index}卷（第${vol.chRange}章，${ARC_NAMES[volIdx < volumeData.length ? Math.min(Math.floor(volIdx / Math.ceil(volumeData.length / 5)), 4) : 0]}幕）填写分卷大纲。输出必须是纯JSON格式。${skillNote}` },
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

已有构思：${contextConcept}
已有世界观：${worldSetting}
已有大纲：${existingOutline}
小说题材：${bible?.worldbuilding ? '玄幻/仙侠' : '通用'}
本卷章范围：第${vol.chRange}章，约${(vol.words / 10000).toFixed(1)}万字
所属幕：${vol.arc}（${vol.arcTheme}）

请填写完整的JSON，所有字段必填，情节节点的coreEvent要有具体内容。只输出JSON不要其他文字。` }
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
    setVolumeGenerating(false);
  }

  // 将当前分卷数据导出到plot_design
  function exportVolumePlan() {
    if (volumeData.length === 0) { alert('请先生成分卷规划'); return; }
    let text = `【分卷规划】目标${(targetWords / 10000).toFixed(1)}万字 · ${volumeData.length}卷 · ${Math.ceil(targetWords / 2400)}章 · 每章2400字\n\n`;
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
    setEditValue(text);
    setEditing(true);
  }

  // 回填分卷规划到剧情（timeline），PlotPanel可读取展示
  async function exportToPlot() {
    if (volumeData.length === 0) { alert('请先生成分卷规划'); return; }
    const vols = volumeData.map(vol => ({
      volume: `第${vol.index}卷${vol.title ? '：' + vol.title : ''}`,
      volume_id: vol.chRange,
      main_plot: [
        vol.cognChange ? `认知质变：${vol.cognChange}` : '',
        vol.coreConflict ? `核心冲突：${vol.coreConflict}` : '',
        vol.emotionDriver ? `情感驱动：${vol.emotionDriver}` : '',
        vol.boss ? `卷BOSS：${vol.boss}` : '',
        vol.bossCost ? `击败代价：${vol.bossCost}` : '',
      ].filter(Boolean).join('；') || '待填充',
      key_events: vol.nodes?.map((n: any) => `[${n.type}] ${n.chRange}章 ${n.coreEvent || '待定'}`) || [],
      turning_points: vol.nodes?.filter((n: any) => n.type === '高潮' || n.type === '大高潮').map((n: any) => `${n.coreEvent || '高潮节点'}`) || [],
      climax: vol.nodes?.find((n: any) => n.type === '大高潮')?.coreEvent || '',
      ending: vol.hookType || '',
      foreshadowing: [
        `新埋${vol.foreshadowNew}个伏笔`,
        `回收${vol.foreshadowRecycle}个旧伏笔`,
      ],
    }));
    try {
      if (!bookId) return;
      const updated = await api.updateBible(bookId, { timeline: JSON.stringify(vols, null, 2) } as any);
      onBibleUpdate(updated);
      alert(`已回填 ${vols.length} 卷的分卷规划到剧情面板，切换到「剧情」Tab 即可查看`);
    } catch (e: any) {
      alert('回填失败: ' + e.message);
    }
  }

  // 全部AI生成（一键生成全部分卷）
  async function aiGenerateAllVolumes() {
    if (!bookId || volumeData.length === 0) return;
    setVolumeGenerating(true);
    try {
      const contextConcept = concept || bible?.concept || '暂无构思';
      const worldSetting = bible?.worldbuilding?.slice(0, 500) || '无';
      const skillKeys = ['volume_breakdown', 'master_outline', 'tomato_outline'];
      const skillPrompt = extractSkillPrompt(selectedSkillPacks, skillKeys);
      const skillNote = selectedSkillPacks.length > 0 ? `\n\n【已加载技能包：${selectedSkillPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';

      const volsSummary = volumeData.map(v =>
        `第${v.index}卷[${v.arc}幕](${v.chRange}章,约${(v.words / 10000).toFixed(1)}万字)`
      ).join('\n');

      const msgs = [
        { role: 'system', content: `你是番茄小说金番作者。按以下JSON数组格式生成全部${volumeData.length}卷的分卷大纲+情节节点。每卷50章=5-8个节点。严格按金番作者Step4+Step5模板。${skillNote}` },
        { role: 'user', content: `请生成完整的分卷大纲JSON数组（length=${volumeData.length}）：

每卷格式：
{
  "title": "卷标题(4-8字)",
  "cognChange": "主角从__→__（不可逆变化）",
  "coreConflict": "核心问题（一句）",
  "emotionDriver": "情绪驱动力",
  "boss": "卷BOSS+击败策略",
  "bossCost": "击败代价",
  "nodes": [5-8个节点，每个含：index,type(过渡/蓄力/高潮/大高潮),chRange,coreEvent,coolType,chM/chC/chW/chD/chF,hook]
}

卷结构：
${volsSummary}

构思：${contextConcept}
世界观：${worldSetting}
目标：${(targetWords / 10000).toFixed(1)}万字，分${volumeData.length}卷，每卷50章×2400字。

五幕弧线：立身(1-5%卷)→立足(5-25%)→立势(25-50%)→立威(50-75%)→立命(75-100%)

只输出JSON数组，不要其他文字。` }
      ];
      const result = await api.aiChat(msgs);
      let parsed: any;
      try {
        const match = result.content.match(/\[[\s\S]*\]/);
        parsed = match ? JSON.parse(match[0]) : null;
      } catch { /* ignore */ }
      if (parsed && Array.isArray(parsed) && parsed.length > 0) {
        const updated = volumeData.map((vol, i) => {
          const aiVol = parsed[i] || {};
          return {
            ...vol,
            title: aiVol.title || vol.title,
            cognChange: aiVol.cognChange || vol.cognChange,
            coreConflict: aiVol.coreConflict || vol.coreConflict,
            emotionDriver: aiVol.emotionDriver || vol.emotionDriver,
            boss: aiVol.boss || vol.boss,
            bossCost: aiVol.bossCost || vol.bossCost,
            nodes: aiVol.nodes?.length ? aiVol.nodes : vol.nodes,
          };
        });
        setVolumeData(updated);
        setExpandedVol(new Set(updated.map((_, i) => i)));
      } else {
        alert('AI返回格式无法解析，请重试');
      }
    } catch (e: any) { alert('AI生成失败: ' + e.message); }
    setVolumeGenerating(false);
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
                <div className="skill-pack-checkbox-list">
                  {skillPacks.map(p => (
                    <label key={p.id} className={`skill-pack-checkbox-item ${selectedSkillPackIds.includes(p.id) ? 'checked' : ''}`}>
                      <input type="checkbox" checked={selectedSkillPackIds.includes(p.id)} onChange={() => onToggleSkillPack(p.id)} disabled={aiAssisting} />
                      <span className="skill-pack-checkbox-icon">{p.icon}</span>
                      <span className="skill-pack-checkbox-name">{p.name}</span>
                    </label>
                  ))}
                </div>
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
            <span className="ai-prompt-hint">Enter 发送 · Shift+Enter 换行</span>
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
        <h3>📋 大纲</h3>
        <div className="bible-edit-actions">
          {!editing ? (
            <>
              <button className="btn-ghost-sm" onClick={() => { setAiMode(true); setAiError(''); setAiPrompt(''); }} disabled={aiAssisting}>
                {aiAssisting ? '🤖 生成中...' : '✨ AI创作'}
              </button>
              {hasChapters && (
                <button className="btn-ghost-sm" onClick={() => onAnalyzeDimension(subTab === 'outline' ? 'outline' : 'worldview')} disabled={dimAnalyzing} title="AI分析已有章节，自动识别">
                  {dimAnalyzing ? '🤖 识别中...' : '🔍 AI识别'}
                </button>
              )}
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

      {/* 自动分卷规划（仅大纲tab显示） */}
      {subTab === 'outline' && (
        <div className="volume-calc-section">
          {!showVolumeCalc ? (
            <button className="btn-ghost-sm volume-calc-btn" onClick={() => setShowVolumeCalc(true)}>
              📊 自动分卷规划
            </button>
          ) : (
            <div className="volume-calc-form">
              <label className="volume-calc-label">输入小说目标字数，按每卷50章×2400字自动分卷</label>
              <div className="volume-calc-input-row">
                <input className="input" type="number" value={targetWords || ''} onChange={e => setTargetWords(parseInt(e.target.value) || 0)} placeholder="如：1000000（100万字）" />
                <span className="volume-calc-unit">字</span>
                <button className="btn-primary-sm" onClick={generateVolumeBreakdown}>生成分卷框架</button>
                <button className="btn-ghost-sm" onClick={() => setShowVolumeCalc(false)}>取消</button>
              </div>
              <p className="text-muted" style={{fontSize:11,marginTop:4}}>按金番作者体系：每卷50章×2400字，五幕弧线自动分配，生成5-8个情节节点/卷</p>
              {targetWords > 0 && (
                <div className="volume-calc-preview">
                  预计 {Math.ceil(targetWords / 2400 / 50)}卷 · {Math.ceil(targetWords / 2400)}章 · {(targetWords / 10000).toFixed(1)}万字
                </div>
              )}
            </div>
          )}

          {/* 分卷数据展示 */}
          {volumeData.length > 0 && (
            <div className="volume-plan-display">
              <div className="volume-plan-header">
                <h4>📚 分卷规划（{(targetWords / 10000).toFixed(1)}万字 · {volumeData.length}卷）</h4>
                <div style={{display:'flex',gap:6}}>
                  <button className="btn-primary-sm" onClick={aiGenerateAllVolumes} disabled={volumeGenerating}>
                    {volumeGenerating ? '⏳ AI生成中...' : '🤖 AI一键补全'}
                  </button>
                  <button className="btn-ghost-sm" onClick={exportVolumePlan}>📝 导出到编辑器</button>
                  <button className="btn-ghost-sm" onClick={exportToPlot} disabled={volumeData.length === 0}>📖 回填剧情</button>
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
                        第{vol.index}卷{vol.title ? `·${vol.title}` : ''}
                      </span>
                      <span className="volume-plan-badge">{vol.arc}幕</span>
                      <span className="volume-plan-badge">{vol.chRange}章</span>
                      <span className="volume-plan-badge">{(vol.words / 10000).toFixed(1)}万字</span>
                      <button className="btn-ghost-sm" onClick={e => { e.stopPropagation(); aiGenerateVolumeOutline(idx); }} disabled={volumeGenerating} title="AI补全此卷详情" style={{marginLeft:'auto'}}>
                        {volumeGenerating ? '⏳' : '🤖'}
                      </button>
                    </div>
                    {expanded && (
                      <div className="volume-plan-body">
                        <div className="volume-plan-grid">
                          <div className="volume-plan-field">
                            <span className="volume-plan-field-label">认知质变</span>
                            <span className="volume-plan-field-val">{vol.cognChange || '—'}</span>
                          </div>
                          <div className="volume-plan-field">
                            <span className="volume-plan-field-label">核心冲突</span>
                            <span className="volume-plan-field-val">{vol.coreConflict || '—'}</span>
                          </div>
                          <div className="volume-plan-field">
                            <span className="volume-plan-field-label">情感驱动</span>
                            <span className="volume-plan-field-val">{vol.emotionDriver || '—'}</span>
                          </div>
                          <div className="volume-plan-field">
                            <span className="volume-plan-field-label">卷BOSS</span>
                            <span className="volume-plan-field-val">{vol.boss || '—'}</span>
                          </div>
                          <div className="volume-plan-field">
                            <span className="volume-plan-field-label">击败代价</span>
                            <span className="volume-plan-field-val">{vol.bossCost || '—'}</span>
                          </div>
                          <div className="volume-plan-field">
                            <span className="volume-plan-field-label">伏笔/钩子</span>
                            <span className="volume-plan-field-val">新埋{vol.foreshadowNew}·回收{vol.foreshadowRecycle}·{vol.hookType}钩子</span>
                          </div>
                        </div>
                        {/* 情节节点 */}
                        {vol.nodes?.length > 0 && (
                          <div className="volume-plan-nodes">
                            <h5>🎯 情节节点（{vol.nodes.length}个）</h5>
                            <div className="node-list">
                              {vol.nodes.map((n: any, ni: number) => (
                                <div key={ni} className={`node-card node-type-${n.type}`}>
                                  <div className="node-card-header">
                                    <span className="node-index">{n.index}</span>
                                    <span className="node-type-badge">{n.type}</span>
                                    <span className="node-ch-range">{n.chRange}章</span>
                                    <span className="node-cool-type">{n.coolType}</span>
                                  </div>
                                  {n.coreEvent && <div className="node-core-event">{n.coreEvent}</div>}
                                  <div className="node-chap-breakdown">
                                    {(['M','C','W','D','F'] as const).map(t => (
                                      <span key={t} className="node-chap-type" title={`${CHAPTER_TYPE_DESC[t]}: ${(n as any)['ch'+t] || 0}%`}>
                                        {t}:{(n as any)['ch'+t] || 0}%
                                      </span>
                                    ))}
                                  </div>
                                  {n.hook && <div className="node-hook">🪝 {n.hook}</div>}
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
function DynamicMemoryPanel(props: {
  bookId: string;
  concept: string;
  bible: BookBible | null;
  chapters: Chapter[];
  showConfirm: (message: string, onConfirm: () => void) => void;
}) {
  const { bookId, chapters, showConfirm } = props;
  const [reports, setReports] = useState<DynamicReport[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editMode, setEditMode] = useState(false);
  const [editValue, setEditValue] = useState('');
  const [editTitle, setEditTitle] = useState('');
  const [editorCollapsed, setEditorCollapsed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createStart, setCreateStart] = useState(1);
  const [createEnd, setCreateEnd] = useState(5);

  const chapterCount = chapters.filter(c => !c.is_volume).length;

  function loadReports() {
    if (!bookId) return;
    setLoading(true);
    api.listDynamicReports(bookId).then(data => {
      setReports(data);
      setLoading(false);
    }).catch(e => {
      setError(e.message || '加载失败');
      setLoading(false);
    });
  }

  useEffect(() => {
    loadReports();
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

  async function handleCreate() {
    if (!bookId) return;
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

  // 计算下一个应该生成的区间
  const nextIntervalStart = Math.floor(chapterCount / 5) * 5 + 1;
  const nextIntervalEnd = nextIntervalStart + 4;

  if (loading) return <div className="page loading-screen"><span>加载动态文件...</span></div>;

  return (
    <div className="dm-panel">
      <div className="dm-header">
        <h3>🗂️ 动态文件</h3>
        <div className="dm-header-actions">
          <button className="btn-ghost-sm" onClick={handleAutoCheck} disabled={generating} title="检查并自动生成缺失的报告">
            {generating ? '⏳ 处理中...' : '🔄 自动检查'}
          </button>
          <button className="btn-primary-sm" onClick={() => { setCreateStart(nextIntervalStart); setCreateEnd(nextIntervalEnd); setShowCreateModal(true); }} disabled={generating}>
            ＋ 生成报告
          </button>
        </div>
      </div>
      <p className="text-muted dm-desc">
        每5章自动汇总人物、事件、时间、地点、势力、伏笔、境界、关系等信息（≤500字/份），AI写作时优先读取动态文件替代全文，大幅降低token消耗
      </p>

      {/* 章节进度指示 */}
      <div className="dm-progress-bar">
        <div className="dm-progress-info">
          <span>📊 已有 {chapterCount} 章 · {reports.length} 份报告</span>
          {chapterCount > 0 && (
            <span className="dm-progress-next">
              下次自动生成：第{(Math.floor(chapterCount / 5) + 1) * 5}章保存时
            </span>
          )}
        </div>
        <div className="dm-progress-track">
          {Array.from({ length: Math.max(Math.ceil(chapterCount / 5), 1) }, (_, i) => {
            const start = i * 5 + 1;
            const end = (i + 1) * 5;
            const hasReport = reports.some(r => r.chapter_start === start && r.chapter_end === end);
            const isPartial = chapterCount >= start && chapterCount < end;
            return (
              <div
                key={i}
                className={`dm-progress-chip ${hasReport ? 'done' : isPartial ? 'partial' : 'pending'}`}
                title={`第${start}-${end}章${hasReport ? '（已生成）' : isPartial ? '（进行中）' : '（待生成）'}`}
              >
                {start}-{end}
              </div>
            );
          })}
        </div>
      </div>

      {error && <div className="error-msg" style={{ marginBottom: 8 }}>{error}</div>}

      {/* 报告区域 */}
      {reports.length === 0 ? (
        <div className="bible-empty" onClick={() => { setCreateStart(1); setCreateEnd(Math.min(5, chapterCount || 5)); setShowCreateModal(true); }}>
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
          {/* 报告切换标签栏 - 单击切换编辑 */}
          <div className="dm-tab-bar">
            {reports.map(r => (
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
                max={chapterCount}
                value={createStart}
                onChange={e => setCreateStart(parseInt(e.target.value) || 1)}
              />
              <label>结束章号</label>
              <input
                type="number"
                min={1}
                max={chapterCount}
                value={createEnd}
                onChange={e => setCreateEnd(parseInt(e.target.value) || 1)}
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

/* ===== 地图面板（三级分类） ===== */
function MapPanel(props: {
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

/* ===== 图谱面板 ===== */
interface GraphNode {
  id: string;
  label: string;
  x: number;
  y: number;
  color?: string;
  size?: number;
  desc?: string;
  nodeType?: string;
  // 境界图谱扩展字段
  isCurrent?: boolean;
  requirements?: string;
  materials?: string;
  techniques?: string;
  visited?: boolean;
}
interface GraphEdge {
  source: string;
  target: string;
  label?: string;
  style?: 'solid' | 'dashed' | 'dotted';
  color?: string;
  directed?: boolean;
}

// 简易力导向布局
function forceLayout(nodes: GraphNode[], edges: GraphEdge[], iterations: number = 100): void {
  if (nodes.length <= 1) return;
  const k = 140;
  const damping = 0.85;

  for (let iter = 0; iter < iterations; iter++) {
    const forces: Record<string, { x: number; y: number }> = {};
    nodes.forEach(n => { forces[n.id] = { x: 0, y: 0 }; });

    // 节点间斥力
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const dx = nodes[i].x - nodes[j].x;
        const dy = nodes[i].y - nodes[j].y;
        let dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 1) dist = 1;
        const force = (k * k) / dist;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        forces[nodes[i].id].x += fx;
        forces[nodes[i].id].y += fy;
        forces[nodes[j].id].x -= fx;
        forces[nodes[j].id].y -= fy;
      }
    }

    // 边的引力
    edges.forEach(e => {
      const s = nodes.find(n => n.id === e.source);
      const t = nodes.find(n => n.id === e.target);
      if (!s || !t) return;
      const dx = t.x - s.x;
      const dy = t.y - s.y;
      let dist = Math.sqrt(dx * dx + dy * dy);
      if (dist < 1) dist = 1;
      const force = (dist * dist) / k;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      forces[s.id].x += fx;
      forces[s.id].y += fy;
      forces[t.id].x -= fx;
      forces[t.id].y -= fy;
    });

    // 中心引力
    nodes.forEach(n => {
      forces[n.id].x += (200 - n.x) * 0.01;
      forces[n.id].y += (250 - n.y) * 0.01;
    });

    const t = 1 - iter / iterations;
    nodes.forEach(n => {
      n.x += forces[n.id].x * 0.08 * damping * t;
      n.y += forces[n.id].y * 0.08 * damping * t;
    });
  }

  // 居中并限制范围
  const minX = Math.min(...nodes.map(n => n.x));
  const maxX = Math.max(...nodes.map(n => n.x));
  const minY = Math.min(...nodes.map(n => n.y));
  const maxY = Math.max(...nodes.map(n => n.y));
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  nodes.forEach(n => { n.x = n.x - cx + 200; n.y = n.y - cy + 250; });
}

// 关系类型样式映射
const RELATION_STYLES: Record<string, { color: string; style: 'solid' | 'dashed' | 'dotted' }> = {
  '师徒': { color: '#5b8def', style: 'solid' },
  '师父': { color: '#5b8def', style: 'solid' },
  '师傅': { color: '#5b8def', style: 'solid' },
  '父子': { color: '#c44d58', style: 'solid' },
  '父女': { color: '#c44d58', style: 'solid' },
  '母子': { color: '#c44d58', style: 'solid' },
  '母女': { color: '#c44d58', style: 'solid' },
  '兄弟': { color: '#e87d3e', style: 'solid' },
  '姐妹': { color: '#e87d3e', style: 'solid' },
  '兄妹': { color: '#e87d3e', style: 'solid' },
  '姐弟': { color: '#e87d3e', style: 'solid' },
  '夫妻': { color: '#e91e63', style: 'solid' },
  '恋人': { color: '#e91e63', style: 'dashed' },
  '情侣': { color: '#e91e63', style: 'dashed' },
  '仇': { color: '#c0392b', style: 'dashed' },
  '敌': { color: '#c0392b', style: 'dashed' },
  '友': { color: '#27ae60', style: 'solid' },
  '盟': { color: '#27ae60', style: 'solid' },
  '主仆': { color: '#9b59b6', style: 'dotted' },
  '同门': { color: '#1abc9c', style: 'solid' },
  '同门师': { color: '#1abc9c', style: 'solid' },
  '上下级': { color: '#9b59b6', style: 'dotted' },
  '部下': { color: '#9b59b6', style: 'dotted' },
};

function getRelationStyle(label: string): { color: string; style: 'solid' | 'dashed' | 'dotted' } {
  for (const key of Object.keys(RELATION_STYLES)) {
    if (label.includes(key)) {
      return RELATION_STYLES[key];
    }
  }
  return { color: '#b0a890', style: 'solid' };
}

function GraphPanel(props: {
  type: 'relationGraph' | 'realmGraph' | 'locationGraph';
  data: string;
  concept: string;
  charactersData?: string;
  onAnalyzeFromReports: () => void;
  dimAnalyzing: boolean;
  bookId: string;
  onUpdate: (val: string) => Promise<void>;
}) {
  const { type, data, charactersData, onAnalyzeFromReports, dimAnalyzing, onUpdate } = props;
  const svgRef = useRef<SVGSVGElement>(null);
  const [editingNode, setEditingNode] = useState<GraphNode | null>(null);
  const [editName, setEditName] = useState('');
  const [editDesc, setEditDesc] = useState('');
  const [editExtra, setEditExtra] = useState<Record<string, string>>({});
  const [editIsCurrent, setEditIsCurrent] = useState(false);
  const [saving, setSaving] = useState(false);

  // 拖拽——ref 存位置，renderTick 驱重绘，避免状态依赖链导致无限循环
  const nodePositionsRef = useRef<Record<string, { x: number; y: number }>>({});
  const draggingRef = useRef<string | null>(null);
  const dragOffsetRef = useRef({ x: 0, y: 0 });
  const [renderTick, setRenderTick] = useState(0);
  const layoutCache = useRef<Map<string, {x:number;y:number}>>(new Map());
  // 连线模式
  const [connectMode, setConnectMode] = useState(false);
  const [connectSource, setConnectSource] = useState<string | null>(null);
  const [connectLabel, setConnectLabel] = useState('');
  const [activeEdgeTarget, setActiveEdgeTarget] = useState<string | null>(null);
  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const { nodes: rawNodes, edges, title } = useMemo(() => {
    return parseGraphData(type, data, charactersData);
  }, [type, data, charactersData]);

  // 稳定布局：已有节点保留缓存坐标，新节点用 forceLayout 坐标
  const nodes = useMemo(() => {
    const cache = layoutCache.current;
    const result = rawNodes.map(n => {
      const cached = cache.get(n.id);
      if (cached) return { ...n, x: cached.x, y: cached.y };
      return n;
    });
    return result;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rawNodes]);

  // 副作用移到 useEffect：更新布局缓存
  useEffect(() => {
    const nextCache = new Map<string, {x:number;y:number}>();
    nodes.forEach(n => { nextCache.set(n.id, { x: n.x, y: n.y }); });
    layoutCache.current = nextCache;
  }, [nodes]);

  // 直接读 ref 获取节点渲染坐标（renderTick 作为隐式依赖保证拖拽时重绘）
  const getPos = (nodeId: string, fallbackX: number, fallbackY: number) => {
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    void renderTick;
    return nodePositionsRef.current[nodeId] || { x: fallbackX, y: fallbackY };
  };

  // 全局拖拽事件——只在 draggingRef 变化时绑定
  useEffect(() => {
    const nodeId = draggingRef.current;
    if (!nodeId) return;
    const svgEl = svgRef.current;
    if (!svgEl) return;

    const handleMove = (clientX: number, clientY: number) => {
      const svgRect = svgEl.getBoundingClientRect();
      const vb = viewBox; // 闭包捕获当前 viewBox，不读 DOM 属性
      const scaleX = vb.vbW / svgRect.width;
      const scaleY = vb.vbH / svgRect.height;
      const svgX = vb.minX + (clientX - svgRect.left) * scaleX;
      const svgY = vb.minY + (clientY - svgRect.top) * scaleY;
      nodePositionsRef.current = {
        ...nodePositionsRef.current,
        [nodeId]: { x: svgX - dragOffsetRef.current.x, y: svgY - dragOffsetRef.current.y },
      };
      setRenderTick(t => t + 1);
    };

    const onMouseMove = (e: MouseEvent) => handleMove(e.clientX, e.clientY);
    const onTouchMove = (e: TouchEvent) => {
      e.preventDefault();
      if (e.touches.length > 0) handleMove(e.touches[0].clientX, e.touches[0].clientY);
    };
    const onEnd = () => { draggingRef.current = null; setRenderTick(t => t + 1); };

    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onEnd);
    window.addEventListener('touchmove', onTouchMove, { passive: false });
    window.addEventListener('touchend', onEnd);
    return () => {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onEnd);
      window.removeEventListener('touchmove', onTouchMove);
      window.removeEventListener('touchend', onEnd);
    };
  }, []); // 空依赖：通过 ref 通信

  function handleNodeMouseDown(e: React.MouseEvent, node: GraphNode) {
    if (connectMode) {
      if (connectSource) {
        if (connectSource !== node.id) setActiveEdgeTarget(node.id);
      } else {
        setConnectSource(node.id);
      }
      return;
    }
    e.stopPropagation();
    const svgEl = svgRef.current;
    if (!svgEl) return;
    const svgRect = svgEl.getBoundingClientRect();
    const vb = viewBox;
    const scaleX = vb.vbW / svgRect.width;
    const scaleY = vb.vbH / svgRect.height;
    const svgX = vb.minX + (e.clientX - svgRect.left) * scaleX;
    const svgY = vb.minY + (e.clientY - svgRect.top) * scaleY;
    const pos = nodePositionsRef.current[node.id] || { x: node.x, y: node.y };
    dragOffsetRef.current = { x: svgX - pos.x, y: svgY - pos.y };
    draggingRef.current = node.id;
    setRenderTick(t => t + 1);
  }

  function handleNodeTouchStart(e: React.TouchEvent, node: GraphNode) {
    if (connectMode) {
      if (connectSource) {
        if (connectSource !== node.id) setActiveEdgeTarget(node.id);
      } else {
        setConnectSource(node.id);
      }
      return;
    }
    // 长按 600ms 进入连线模式
    if (longPressTimer.current) clearTimeout(longPressTimer.current);
    longPressTimer.current = setTimeout(() => {
      setConnectMode(true);
      setConnectSource(node.id);
    }, 600);
    // 同时准备拖拽
    if (e.touches.length > 0) {
      const touch = e.touches[0];
      const svgEl = svgRef.current;
      if (!svgEl) return;
      const svgRect = svgEl.getBoundingClientRect();
      const vb = viewBox;
      const scaleX = vb.vbW / svgRect.width;
      const scaleY = vb.vbH / svgRect.height;
      const svgX = vb.minX + (touch.clientX - svgRect.left) * scaleX;
      const svgY = vb.minY + (touch.clientY - svgRect.top) * scaleY;
      const pos = nodePositionsRef.current[node.id] || { x: node.x, y: node.y };
      dragOffsetRef.current = { x: svgX - pos.x, y: svgY - pos.y };
      draggingRef.current = node.id;
      setRenderTick(t => t + 1);
    }
    const onTouchEnd = () => {
      if (longPressTimer.current) { clearTimeout(longPressTimer.current); longPressTimer.current = null; }
      window.removeEventListener('touchend', onTouchEnd);
    };
    window.addEventListener('touchend', onTouchEnd, { once: true });
  }

  async function confirmCreateEdge() {
    if (!connectSource || !activeEdgeTarget || !connectLabel.trim()) {
      alert('请选择关系类型');
      return;
    }
    try {
      // 尝试解析JSON格式并添加edges
      let newData = data;
      try {
        const parsed = JSON.parse(data);
        if (Array.isArray(parsed)) {
          // 原始格式节点在顶层，边信息不存在，需要附加存储
          // 使用简单策略：在数据末尾追加边定义
          newData = JSON.stringify(parsed, null, 2);
        }
      } catch { /* keep as is */ }
      // 将 edges 信息追加为注释格式存储（不改变原有数据格式）
      const edgeDef = `\n/*EDGE:${connectSource}->${activeEdgeTarget}:${connectLabel}:${'#ffd54f'}*/`;
      const existingEdges = (data.match(/\/\*EDGE:[^*]*\*\//g) || []).join('\n');
      let baseData = data;
      if (existingEdges) {
        baseData = baseData.replace(/\/\*EDGE:[^*]*\*\//g, '').trim();
      }
      newData = baseData + '\n' + existingEdges + edgeDef;
      await onUpdate(newData.trim());
    } catch (e: any) {
      alert('添加连线失败: ' + e.message);
    }
    setConnectSource(null);
    setActiveEdgeTarget(null);
    setConnectLabel('');
  }

  function openNodeEditor(node: GraphNode) {
    setEditingNode(node);
    setEditName(node.label);
    setEditDesc(node.desc || '');
    setEditIsCurrent(!!node.isCurrent);
    setEditExtra({
      requirements: node.requirements || '',
      materials: node.materials || '',
      techniques: node.techniques || '',
    });
  }

  async function saveNodeEdit() {
    if (!editingNode) return;
    setSaving(true);
    try {
      let newData = data;
      // 尝试JSON格式更新
      try {
        const parsed = JSON.parse(data);
        if (Array.isArray(parsed)) {
          const updated = parsed.map((item: any) => {
            const name = item.name || item.realm || item.level || item.id;
            if (name === editingNode.label || name === editingNode.id) {
              return {
                ...item,
                name: editName,
                desc: editDesc,
                description: editDesc,
                isCurrent: type === 'realmGraph' ? editIsCurrent : undefined,
                requirements: editExtra.requirements || undefined,
                materials: editExtra.materials || undefined,
                techniques: editExtra.techniques || undefined,
              };
            }
            return item;
          });
          newData = JSON.stringify(updated, null, 2);
        } else if (parsed.characters && Array.isArray(parsed.characters)) {
          parsed.characters = parsed.characters.map((item: any) => {
            if (item.name === editingNode.label) {
              return { ...item, name: editName, desc: editDesc, description: editDesc };
            }
            return item;
          });
          newData = JSON.stringify(parsed, null, 2);
        }
      } catch {
        // 纯文本格式：按行替换
        const lines = data.split('\n');
        const updatedLines = lines.map(line => {
          if (line.includes(editingNode.label)) {
            // 替换描述部分
            const colonIdx = line.search(/[：:]/);
            if (colonIdx > 0) {
              return line.slice(0, colonIdx + 1) + ' ' + editDesc;
            }
            return `${editName}：${editDesc}`;
          }
          return line;
        });
        newData = updatedLines.join('\n');
      }
      await onUpdate(newData);
      setEditingNode(null);
    } catch (e: any) {
      alert('保存失败: ' + e.message);
    }
    setSaving(false);
  }

  if (!data || nodes.length === 0) {
    const hints: Record<string, string> = {
      relationGraph: '在「人物及关系」中填写角色信息后，这里会自动生成人物关系图谱',
      realmGraph: '在「世界观」中填写境界/等级体系后，这里会自动生成晋升图谱',
    };
    const icons: Record<string, string> = {
      relationGraph: '🕸️',
      realmGraph: '⚡',
    };
    return (
      <div className="graph-panel">
        <div className="graph-header">
          <h3>{icons[type]} {title}</h3>
          <button className="btn-ghost-sm" onClick={onAnalyzeFromReports} disabled={dimAnalyzing} title="从动态文件提取信息">
            {dimAnalyzing ? '⏳ 识别中...' : '🔍 AI识别'}
          </button>
        </div>
        <div className="bible-empty">
          <span className="bible-empty-icon">{icons[type]}</span>
          <p>暂无图谱数据</p>
          <p className="text-muted">{hints[type]}</p>
          <button className="btn-primary-sm" style={{ marginTop: 8 }} onClick={onAnalyzeFromReports} disabled={dimAnalyzing}>
            {dimAnalyzing ? '⏳ 识别中...' : '🔍 从动态文件识别'}
          </button>
        </div>
      </div>
    );
  }

  // 自适应 viewBox（含拖拽后的位置）
  const allX: number[] = [];
  const allY: number[] = [];
  nodes.forEach(n => {
    const p = nodePositionsRef.current[n.id];
    if (p) { allX.push(p.x); allY.push(p.y); }
    allX.push(n.x); allY.push(n.y);
  });
  const minX = Math.min(...allX) - 50;
  const maxX = Math.max(...allX) + 50;
  const minY = Math.min(...allY) - 40;
  const maxY = Math.max(...allY) + 50;
  const vbW = Math.max(maxX - minX, 200);
  const vbH = Math.max(maxY - minY, 200);

  // viewBox 对象（稳定引用，供拖拽闭包使用，避免直接读 DOM 属性）
  const viewBox = useMemo(() => ({ minX, minY, vbW, vbH }), [minX, minY, vbW, vbH]);

  // 收集出现过的边样式用于图例
  const edgeLegendMap = new Map<string, { color: string; style: string }>();
  edges.forEach(e => {
    if (e.label) {
      const st = getRelationStyle(e.label);
      edgeLegendMap.set(e.label, { color: st.color, style: st.style });
    }
  });

  return (
    <div className="graph-panel">
      <div className="graph-header">
        <h3>{title}</h3>
        <div className="graph-header-actions">
          <button className="btn-ghost-sm" onClick={onAnalyzeFromReports} disabled={dimAnalyzing} title="从动态文件提取信息">
            {dimAnalyzing ? '⏳ 识别中...' : '🔍 AI识别'}
          </button>
          <span className="text-muted">{nodes.length}个节点 · {edges.length}条关系 · 点击节点可编辑</span>
        </div>
      </div>
      <div className="graph-svg-container graph-starry-bg">
        <svg ref={svgRef} viewBox={`${minX} ${minY} ${vbW} ${vbH}`} className="graph-svg graph-starry-svg" preserveAspectRatio="xMidYMid meet">
          <defs>
            {/* 星空背景渐变 */}
            <radialGradient id="space-bg" cx="50%" cy="50%" r="70%">
              <stop offset="0%" stopColor="#1a1a3e" stopOpacity="0.95" />
              <stop offset="60%" stopColor="#0d0d2b" stopOpacity="0.98" />
              <stop offset="100%" stopColor="#060614" stopOpacity="1" />
            </radialGradient>
            {/* 节点发光滤镜 */}
            <filter id="node-glow" x="-50%" y="-50%" width="200%" height="200%">
              <feGaussianBlur stdDeviation="3" result="coloredBlur" />
              <feMerge>
                <feMergeNode in="coloredBlur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
            {/* 星星节点渐变 */}
            <radialGradient id="star-gold" cx="35%" cy="35%" r="65%">
              <stop offset="0%" stopColor="#fff8e1" />
              <stop offset="40%" stopColor="#ffd54f" />
              <stop offset="100%" stopColor="#f9a825" />
            </radialGradient>
            <radialGradient id="star-blue" cx="35%" cy="35%" r="65%">
              <stop offset="0%" stopColor="#e3f2fd" />
              <stop offset="40%" stopColor="#64b5f6" />
              <stop offset="100%" stopColor="#1565c0" />
            </radialGradient>
            <radialGradient id="star-pink" cx="35%" cy="35%" r="65%">
              <stop offset="0%" stopColor="#fce4ec" />
              <stop offset="40%" stopColor="#f06292" />
              <stop offset="100%" stopColor="#c2185b" />
            </radialGradient>
            <radialGradient id="star-green" cx="35%" cy="35%" r="65%">
              <stop offset="0%" stopColor="#e8f5e9" />
              <stop offset="40%" stopColor="#66bb6a" />
              <stop offset="100%" stopColor="#2e7d32" />
            </radialGradient>
            <radialGradient id="star-purple" cx="35%" cy="35%" r="65%">
              <stop offset="0%" stopColor="#f3e5f5" />
              <stop offset="40%" stopColor="#ab47bc" />
              <stop offset="100%" stopColor="#6a1b9a" />
            </radialGradient>
            <radialGradient id="star-teal" cx="35%" cy="35%" r="65%">
              <stop offset="0%" stopColor="#e0f2f1" />
              <stop offset="40%" stopColor="#26a69a" />
              <stop offset="100%" stopColor="#00695c" />
            </radialGradient>
            <radialGradient id="star-orange" cx="35%" cy="35%" r="65%">
              <stop offset="0%" stopColor="#fff3e0" />
              <stop offset="40%" stopColor="#ff8a65" />
              <stop offset="100%" stopColor="#d84315" />
            </radialGradient>
            <marker id="arrow-default" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
              <path d="M0,0 L8,4 L0,8 Z" fill="#7e8da0" />
            </marker>
            <marker id="arrow-red" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
              <path d="M0,0 L8,4 L0,8 Z" fill="#ff6b6b" />
            </marker>
            <marker id="arrow-green" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
              <path d="M0,0 L8,4 L0,8 Z" fill="#66bb6a" />
            </marker>
            <marker id="arrow-blue" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
              <path d="M0,0 L8,4 L0,8 Z" fill="#64b5f6" />
            </marker>
            <marker id="arrow-purple" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
              <path d="M0,0 L8,4 L0,8 Z" fill="#ab47bc" />
            </marker>
            <marker id="arrow-pink" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
              <path d="M0,0 L8,4 L0,8 Z" fill="#f06292" />
            </marker>
            <marker id="arrow-orange" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
              <path d="M0,0 L8,4 L0,8 Z" fill="#ff8a65" />
            </marker>
            <marker id="arrow-teal" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
              <path d="M0,0 L8,4 L0,8 Z" fill="#26a69a" />
            </marker>
          </defs>
          {/* 星空背景 */}
          <rect x={minX} y={minY} width={vbW} height={vbH} fill="url(#space-bg)" />
          {/* 装饰星星 */}
          {Array.from({ length: 30 }).map((_, i) => {
            const seed = (i * 137.5) % 100;
            const sx = minX + (seed / 100) * vbW;
            const sy = minY + ((seed * 1.7) % 100 / 100) * vbH;
            const sr = (i % 3 === 0) ? 1.2 : 0.6;
            const op = 0.3 + (seed % 50) / 100;
            return <circle key={`star-${i}`} cx={sx} cy={sy} r={sr} fill="#fff" opacity={op} className="graph-twinkle-star" style={{ animationDelay: `${i * 0.3}s` }} />;
          })}
          {/* 绘制边 */}
          {edges.map((edge, i) => {
            const s = nodes.find(n => n.id === edge.source);
            const t = nodes.find(n => n.id === edge.target);
            if (!s || !t) return null;
            const edgeColor = edge.color || (edge.label ? getRelationStyle(edge.label).color : '#8e99ab');
            const edgeStyle = edge.style || (edge.label ? getRelationStyle(edge.label).style : 'solid');
            const markerRef = edge.directed ? (() => {
              if (edgeColor.includes('c0392b') || edgeColor.includes('c44d58') || edgeColor.includes('e74c3c')) return 'arrow-red';
              if (edgeColor.includes('27ae60') || edgeColor.includes('2ecc71') || edgeColor.includes('66bb6a')) return 'arrow-green';
              if (edgeColor.includes('5b8def') || edgeColor.includes('3498db') || edgeColor.includes('64b5f6')) return 'arrow-blue';
              if (edgeColor.includes('9b59b6') || edgeColor.includes('8e44ad') || edgeColor.includes('ab47bc')) return 'arrow-purple';
              if (edgeColor.includes('e91e63') || edgeColor.includes('f06292')) return 'arrow-pink';
              if (edgeColor.includes('e87d3e') || edgeColor.includes('ff8a65')) return 'arrow-orange';
              if (edgeColor.includes('1abc9c') || edgeColor.includes('26a69a')) return 'arrow-teal';
              return 'arrow-default';
            })() : '';
            const sPos = getPos(s.id, s.x, s.y);
            const tPos = getPos(t.id, t.x, t.y);
            const dx = tPos.x - sPos.x;
            const dy = tPos.y - sPos.y;
            const dist = Math.sqrt(dx * dx + dy * dy) || 1;
            const sR = (s.size || 24) + 2;
            const tR = (t.size || 24) + (edge.directed ? 6 : 2);
            const x1 = sPos.x + (dx / dist) * sR;
            const y1 = sPos.y + (dy / dist) * sR;
            const x2 = tPos.x - (dx / dist) * tR;
            const y2 = tPos.y - (dy / dist) * tR;
            const midX = (x1 + x2) / 2;
            const midY = (y1 + y2) / 2 - 6;
            return (
              <g key={`edge-${i}`}>
                <line
                  x1={x1} y1={y1} x2={x2} y2={y2}
                  stroke={edgeColor}
                  strokeWidth="2"
                  strokeDasharray={edgeStyle === 'dashed' ? '5,3' : edgeStyle === 'dotted' ? '2,2' : undefined}
                  markerEnd={markerRef ? `url(#${markerRef})` : undefined}
                  opacity="0.6"
                />
                {edge.label && (
                  <text x={midX} y={midY} className="graph-edge-label graph-star-edge-label" textAnchor="middle" fill={edgeColor}>
                    {edge.label.length > 6 ? edge.label.slice(0, 5) + '…' : edge.label}
                  </text>
                )}
              </g>
            );
          })}
          {/* 绘制节点 */}
          {nodes.map(node => {
            const r = Math.max(node.size || 28, 22);
            const isRealm = type === 'realmGraph';
            const isCharHeader = node.nodeType === 'char-header';
            const labelBelow = isCharHeader || (!isRealm && node.label.length > 3);
            const labelInside = isRealm && !isCharHeader;
            const isCurrentRealm = isRealm && node.isCurrent;
            const reqSummary = type === 'realmGraph' && node.requirements ? `需:${node.requirements.slice(0, 14)}` : '';
            const techSummary = type === 'realmGraph' && node.techniques ? `功:${node.techniques.slice(0, 12)}` : '';
            const matSummary = type === 'realmGraph' && node.materials ? `物:${node.materials.slice(0, 12)}` : '';
            // 根据节点颜色选择星星渐变
            const nodeColor = node.color || '#ffd54f';
            const starGrad = (() => {
              if (nodeColor.includes('c0392b') || nodeColor.includes('c44d58') || nodeColor.includes('e74c3c')) return 'star-orange';
              if (nodeColor.includes('27ae60') || nodeColor.includes('2ecc71')) return 'star-green';
              if (nodeColor.includes('5b8def') || nodeColor.includes('3498db')) return 'star-blue';
              if (nodeColor.includes('9b59b6') || nodeColor.includes('8e44ad')) return 'star-purple';
              if (nodeColor.includes('e91e63') || nodeColor.includes('e74c8c')) return 'star-pink';
              if (nodeColor.includes('1abc9c') || nodeColor.includes('16a085')) return 'star-teal';
              if (nodeColor.includes('e87d3e') || nodeColor.includes('f39c12')) return 'star-gold';
              return 'star-gold';
            })();
            const pos = getPos(node.id, node.x, node.y);
            return (
              <g key={node.id}
                className={`graph-node-group graph-star-node ${connectMode && connectSource === node.id ? 'graph-connect-source' : ''}`}
                style={{ cursor: connectMode ? 'crosshair' : 'grab' }}
                onClick={() => {
                  if (connectMode && connectSource && connectSource !== node.id) {
                    setActiveEdgeTarget(node.id);
                  } else if (connectMode && !connectSource) {
                    setConnectSource(node.id);
                  } else if (!connectMode) {
                    openNodeEditor(node);
                  }
                }}
                onMouseDown={(e) => handleNodeMouseDown(e, node)}
                onTouchStart={(e) => handleNodeTouchStart(e, node)}
              >
                {/* 外层光晕 */}
                <circle cx={pos.x} cy={pos.y} r={r + 6} fill={nodeColor} opacity="0.15" className="graph-star-halo" />
                {isCurrentRealm && (
                  <>
                    <circle cx={pos.x} cy={pos.y} r={r + 10} fill="none" stroke="#ffd54f" strokeWidth="2" opacity="0.3" className="graph-current-ring" />
                    <circle cx={pos.x} cy={pos.y} r={r + 6} fill="none" stroke="#ffd54f" strokeWidth="2.5" opacity="0.7" />
                  </>
                )}
                {/* 主节点 - 星星效果 */}
                <circle
                  cx={pos.x} cy={pos.y} r={r}
                  fill={`url(#${starGrad})`}
                  className="graph-node-circle graph-star-circle"
                  stroke={isCurrentRealm ? '#ffd54f' : 'rgba(255,255,255,0.4)'}
                  strokeWidth={isCurrentRealm ? '2.5' : '1'}
                  filter="url(#node-glow)"
                />
                <text
                  x={pos.x}
                  y={labelInside ? pos.y + 3 : (pos.y + (labelBelow ? 0 : 5))}
                  className={`graph-node-text graph-star-text ${labelInside ? 'graph-realm-label' : ''}`}
                  textAnchor="middle"
                  fontSize={labelInside ? Math.min(11, r * 0.5) : undefined}
                >
                  {labelInside ? (node.label.length > 3 ? node.label.slice(0, 2) + '…' : node.label) : (labelBelow ? '' : (node.label.length > 2 ? node.label.slice(0, 2) : node.label))}
                </text>
                {labelBelow && (
                  <text
                    x={pos.x}
                    y={pos.y + r + 16}
                    className="graph-node-label-below"
                    textAnchor="middle"
                  >
                    {node.label.length > 7 ? node.label.slice(0, 6) + '…' : node.label}
                  </text>
                )}
                {isCurrentRealm && (
                  <text x={pos.x} y={pos.y - r - 10} className="graph-current-badge" textAnchor="middle">📍当前</text>
                )}
                {reqSummary && (
                  <text x={pos.x} y={pos.y + r + (labelBelow ? 32 : 18)} className="graph-req-text" textAnchor="middle">{reqSummary}</text>
                )}
                {matSummary && (
                  <text x={pos.x} y={pos.y + r + (labelBelow ? 46 : 32)} className="graph-req-text" textAnchor="middle">{matSummary}</text>
                )}
                {techSummary && (
                  <text x={pos.x} y={pos.y + r + (labelBelow ? 60 : 46)} className="graph-req-text" textAnchor="middle">{techSummary}</text>
                )}
                {node.desc && (
                  <title>{node.label}：{node.desc}{node.requirements ? `\n所需：${node.requirements}` : ''}{node.materials ? `\n物品：${node.materials}` : ''}{node.techniques ? `\n功法：${node.techniques}` : ''}</title>
                )}
              </g>
            );
          })}
        </svg>
      </div>
      {/* 连线模式控制栏 */}
      <div className="graph-connect-bar">
        <button className={`btn-ghost-sm ${connectMode ? 'active' : ''}`}
          onClick={() => { setConnectMode(!connectMode); setConnectSource(null); }}
          style={{color: connectMode ? '#ffd54f' : undefined}}>
          {connectMode ? '🔗 连线模式：点击第一个节点' : '🔗 连线模式'}
        </button>
        {connectSource && (
          <span>已选起点：{nodes.find(n => n.id === connectSource)?.label || connectSource}，请点击终点</span>
        )}
        <span className="text-muted" style={{marginLeft:'auto',fontSize:10}}>长按节点也可进入连线</span>
      </div>
      {/* 连线标签编辑弹窗 */}
      {connectMode && connectSource && activeEdgeTarget && (
        <div className="modal-overlay" onClick={() => { setConnectSource(null); setActiveEdgeTarget(null); }}>
          <div className="modal-content graph-connect-editor" onClick={e => e.stopPropagation()}>
            <h4>🔗 添加连线</h4>
            <p style={{fontSize:13,color:'var(--text-secondary)'}}>
              {nodes.find(n => n.id === connectSource)?.label} → {nodes.find(n => n.id === activeEdgeTarget)?.label}
            </p>
            <div>
              <label>关系类型</label>
              <select value={connectLabel} onChange={e => setConnectLabel(e.target.value)} className="input">
                <option value="">— 选择关系 —</option>
                <option value="师徒">师徒</option>
                <option value="盟友">盟友</option>
                <option value="敌对">敌对</option>
                <option value="恋爱">恋爱</option>
                <option value="亲人">亲人</option>
                <option value="朋友">朋友</option>
                <option value="统领">统领</option>
                <option value="隶属">隶属</option>
                <option value="晋升">晋升</option>
                <option value="突破">突破</option>
                <option value="相邻">相邻</option>
                <option value="通行">通行</option>
                <option value="仇敌">仇敌</option>
              </select>
            </div>
            <div className="confirm-actions">
              <button className="btn-ghost-sm" onClick={() => { setConnectSource(null); setActiveEdgeTarget(null); }}>取消</button>
              <button className="btn-primary-sm" onClick={confirmCreateEdge}>确认添加</button>
            </div>
          </div>
        </div>
      )}
      {/* 关系类型图例 */}
      {edgeLegendMap.size > 0 && (
        <div className="graph-edge-legend">
          {Array.from(edgeLegendMap.entries()).map(([label, st]) => (
            <div key={label} className="graph-edge-legend-item">
              <svg width="24" height="10" style={{ flexShrink: 0 }}>
                <line x1="0" y1="5" x2="24" y2="5" stroke={st.color} strokeWidth="2"
                  strokeDasharray={st.style === 'dashed' ? '5,3' : st.style === 'dotted' ? '2,2' : undefined} />
              </svg>
              <span>{label}</span>
            </div>
          ))}
        </div>
      )}
      {/* 节点列表 - 可点击编辑 */}
      <div className="graph-legend">
        {nodes.map(node => (
          <div key={node.id} className="graph-legend-item graph-legend-clickable" title={node.desc || node.label} onClick={() => openNodeEditor(node)}>
            <span className="graph-legend-dot" style={{ background: node.color || 'var(--accent)' }} />
            <span>{node.label}{node.isCurrent ? ' ★当前' : ''}</span>
            {node.desc && <span className="graph-legend-desc">{node.desc.slice(0, 20)}</span>}
            {node.requirements && <span className="graph-legend-req">所需:{node.requirements.slice(0, 12)}</span>}
            {node.materials && <span className="graph-legend-req">物品:{node.materials.slice(0, 12)}</span>}
            {node.techniques && <span className="graph-legend-req">功法:{node.techniques.slice(0, 12)}</span>}
          </div>
        ))}
      </div>

      {/* 节点编辑弹窗 */}
      {editingNode && (
        <div className="modal-overlay" onClick={() => setEditingNode(null)}>
          <div className="modal-content graph-node-editor" onClick={e => e.stopPropagation()}>
            <h3>✏️ 编辑节点</h3>
            <div className="form-group">
              <label>名称</label>
              <input className="input" value={editName} onChange={e => setEditName(e.target.value)} />
            </div>
            <div className="form-group">
              <label>描述</label>
              <textarea className="input" rows={3} value={editDesc} onChange={e => setEditDesc(e.target.value)} />
            </div>
            {type === 'realmGraph' && (
              <>
                <div className="form-group">
                  <label>
                    <input type="checkbox" checked={editIsCurrent} onChange={e => setEditIsCurrent(e.target.checked)} style={{ marginRight: 6 }} />
                    这是主角当前境界
                  </label>
                </div>
                <div className="form-group">
                  <label>突破所需物资</label>
                  <input className="input" value={editExtra.requirements || ''} onChange={e => setEditExtra({ ...editExtra, requirements: e.target.value })} placeholder="如：灵石×100、天魂草×3" />
                </div>
                <div className="form-group">
                  <label>消耗物品</label>
                  <input className="input" value={editExtra.materials || ''} onChange={e => setEditExtra({ ...editExtra, materials: e.target.value })} placeholder="如：筑基丹、破境符" />
                </div>
                <div className="form-group">
                  <label>所需功法</label>
                  <input className="input" value={editExtra.techniques || ''} onChange={e => setEditExtra({ ...editExtra, techniques: e.target.value })} placeholder="如：混元功、太清诀" />
                </div>
              </>
            )}
            <div className="confirm-actions">
              <button className="btn-ghost-sm" onClick={() => setEditingNode(null)} disabled={saving}>取消</button>
              <button className="btn-primary-sm" onClick={saveNodeEdit} disabled={saving}>
                {saving ? '保存中...' : '💾 保存'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* 解析图谱数据 */
function parseGraphData(type: string, data: string, charactersData?: string): {
  nodes: GraphNode[];
  edges: GraphEdge[];
  title: string;
  emptyHint: string;
} {
  if (!data) return { nodes: [], edges: [], title: '', emptyHint: '' };

  const titles: Record<string, string> = {
    relationGraph: '人物关系图谱',
    realmGraph: '境界晋升图谱',
    locationGraph: '地点关联图谱',
  };

  if (type === 'relationGraph') {
    return parseRelationGraph(data, titles.relationGraph);
  } else if (type === 'realmGraph') {
    return parseRealmGraph(data, titles.realmGraph, charactersData);
  } else if (type === 'locationGraph') {
    return parseLocationGraph(data, titles.locationGraph);
  }
  return { nodes: [], edges: [], title: '', emptyHint: '' };
}

function parseRelationGraph(data: string, title: string): { nodes: GraphNode[]; edges: GraphEdge[]; title: string; emptyHint: string } {
  const nodes: GraphNode[] = [];
  const edges: GraphEdge[] = [];
  const lines = data.split('\n');
  const colors = ['#4a8b4a', '#e87d3e', '#5b8def', '#c44d58', '#9b59b6', '#1abc9c', '#f39c12', '#e91e63'];
  const nodeMap = new Map<string, GraphNode>();

  function ensureNode(name: string, desc?: string): GraphNode {
    const cleanName = name.trim();
    if (!cleanName) return nodes[0];
    if (nodeMap.has(cleanName)) {
      if (desc && !nodeMap.get(cleanName)!.desc) nodeMap.get(cleanName)!.desc = desc;
      return nodeMap.get(cleanName)!;
    }
    const i = nodeMap.size;
    const node: GraphNode = {
      id: cleanName,
      label: cleanName,
      x: 200 + (Math.random() - 0.5) * 200,
      y: 250 + (Math.random() - 0.5) * 200,
      color: colors[i % colors.length],
      size: 24,
      desc,
    };
    nodes.push(node);
    nodeMap.set(cleanName, node);
    return node;
  }

  function addEdge(source: string, target: string, label: string) {
    if (source === target || !source || !target) return;
    const exists = edges.some(e =>
      (e.source === source && e.target === target) ||
      (e.source === target && e.target === source && e.label === label)
    );
    if (exists) return;
    const st = getRelationStyle(label);
    edges.push({ source, target, label, style: st.style, color: st.color, directed: !!label });
  }

  // 尝试 JSON 解析
  try {
    const parsed = JSON.parse(data);
    const chars = Array.isArray(parsed) ? parsed : (parsed.characters || parsed.nodes || []);
    if (Array.isArray(chars) && chars.length > 0) {
      chars.forEach((char: any) => {
        const name = char.name || char.id;
        if (!name) return;
        ensureNode(name, char.desc || char.description || char.summary || '');
        if (Array.isArray(char.relationships)) {
          char.relationships.forEach((rel: any) => {
            const targetName = rel.target_name || rel.target || rel.name;
            if (targetName) {
              ensureNode(targetName, rel.target_desc || '');
              addEdge(name, targetName, rel.relation || rel.type || rel.label || '');
            }
          });
        }
      });
    }
  } catch {
    // 纯文本解析
    lines.forEach(line => {
      const trimmed = line.trim();
      if (!trimmed) return;

      // 模式1：【人物名】描述
      const bracketMatch = trimmed.match(/[【\[](.+?)[】\]]/);
      if (bracketMatch) {
        ensureNode(bracketMatch[1], trimmed.replace(/[【\[](.+?)[】\]]/, '').replace(/^[：:]\s*/, '').trim());
      }

      // 模式2：A→B：关系 或 A→B（关系）
      const arrowMatch = trimmed.match(/(.{1,10})\s*[→>➜]+\s*(.{1,10})(?:[：:（(]([^）)]+)[）)]?)?/);
      if (arrowMatch) {
        const a = arrowMatch[1].trim();
        const b = arrowMatch[2].trim().replace(/[：:（(].*$/, '');
        ensureNode(a);
        ensureNode(b);
        addEdge(a, b, arrowMatch[3]?.trim() || '');
      }

      // 模式3：A是B的XX
      const relMatch = trimmed.match(/(.{1,8})\s*是\s*(.{1,8})\s*的\s*(.+)/);
      if (relMatch) {
        const a = relMatch[1].trim();
        const b = relMatch[2].trim();
        const rel = relMatch[3].trim();
        ensureNode(a);
        ensureNode(b);
        addEdge(a, b, rel);
      }

      // 模式4：人物名：描述（冒号前短文本作为人物名）
      if (!bracketMatch && !arrowMatch && !relMatch) {
        const colonMatch = trimmed.match(/^(.{1,8})\s*[：:]/);
        if (colonMatch) {
          ensureNode(colonMatch[1].trim(), trimmed.slice(colonMatch[0].length).trim());
        }
      }
    });

    // 二次扫描：同行出现多个人物名则建立关联
    if (nodes.length > 0) {
      lines.forEach(line => {
        const mentioned = nodes.filter(n => line.includes(n.label));
        if (mentioned.length >= 2) {
          const relMatch = line.match(/[（(](.+?)[）)]/);
          const relLabel = relMatch ? relMatch[1] : '';
          for (let i = 0; i < mentioned.length; i++) {
            for (let j = i + 1; j < mentioned.length; j++) {
              addEdge(mentioned[i].id, mentioned[j].id, relLabel);
            }
          }
        }
      });
    }
  }

  // 根据连接数调整节点大小
  nodes.forEach(n => {
    const count = edges.filter(e => e.source === n.id || e.target === n.id).length;
    n.size = Math.min(36, 20 + count * 3);
  });

  // 力导向布局
  if (nodes.length > 1) {
    forceLayout(nodes, edges, 100);
  }

  const hint = nodes.length === 0 ? '在「人物及关系」中填写角色信息后，这里会自动生成关系图谱' : '';
  return { nodes, edges, title, emptyHint: hint };
}

function parseRealmGraph(data: string, title: string, charactersData?: string): { nodes: GraphNode[]; edges: GraphEdge[]; title: string; emptyHint: string } {
  const nodes: GraphNode[] = [];
  const edges: GraphEdge[] = [];
  const realmColors = ['#c44d58', '#e87d3e', '#f39c12', '#1abc9c', '#5b8def', '#9b59b6', '#4a8b4a', '#e91e63'];
  const charColors = ['#5b8def', '#e87d3e', '#1abc9c', '#9b59b6', '#f39c12', '#e91e63', '#4a8b4a', '#c44d58'];

  // 解析境界体系
  let realmLevels: any[] = [];
  try {
    const parsed = JSON.parse(data);
    if (Array.isArray(parsed)) realmLevels = parsed;
    else if (parsed.realms) realmLevels = parsed.realms;
  } catch {
    // 纯文本解析境界
    const lines = data.split('\n').filter(l => l.trim());
    lines.forEach(line => {
      const cleaned = line.replace(/^[#\d\.\-•*→>]\s*/, '');
      const match = cleaned.match(/[【\[](.+?)[】\]]/);
      const name = match ? match[1].trim() : cleaned.replace(/\s*[：:].+/, '').slice(0, 15).trim();
      if (name && !realmLevels.find((r: any) => r.name === name || r.realm === name)) {
        realmLevels.push({ name, level: realmLevels.length + 1 });
      }
    });
  }

  // 解析角色数据
  let characters: any[] = [];
  if (charactersData) {
    try {
      const parsed = JSON.parse(charactersData);
      if (Array.isArray(parsed)) characters = parsed;
    } catch { /* not JSON */ }
  }

  const charCount = Math.max(characters.length, 1);
  const colWidth = Math.min(140, Math.max(90, 800 / charCount));
  const realmHeight = 52;
  const topY = 50;
  const charNameY = topY;
  const firstRealmY = charNameY + 50;

  // 如果没有角色，创建一个通用列
  const displayChars = characters.length > 0 ? characters : [{ name: '通用', role: '' }];

  displayChars.forEach((char, ci) => {
    const colX = 50 + ci * colWidth + colWidth / 2;
    const charId = `char-${char.name}`;

    // 角色名节点（顶部）
    const charNode: GraphNode = {
      id: charId, label: char.name,
      x: colX, y: charNameY,
      color: charColors[ci % charColors.length], size: 22,
      desc: char.role || char.abilities || '',
      nodeType: 'char-header',
      requirements: char.abilities || '',
      materials: char.items || '',
      techniques: '',
    };
    nodes.push(charNode);

    // 找到该角色当前境界
    const currentRealmName = char.currentRealm || char.realm || '';
    const currentRealmIdx = realmLevels.findIndex((r: any) => {
      const rName = r.name || r.realm || '';
      return rName === currentRealmName || rName.includes(currentRealmName) || currentRealmName.includes(rName);
    });

    // 境界节点
    realmLevels.forEach((realm: any, ri: number) => {
      const rName = realm.name || realm.realm || `境界${ri + 1}`;
      const realmId = `${char.name}-${rName}`;
      const isCurrent = ri === currentRealmIdx;

      nodes.push({
        id: realmId, label: rName,
        x: colX, y: firstRealmY + ri * realmHeight,
        color: realmColors[ri % realmColors.length], size: isCurrent ? 26 : 22,
        desc: realm.desc || realm.description || realm.requirement || '',
        nodeType: 'realm',
        isCurrent,
        requirements: realm.requirements || realm.requirement || '',
        materials: realm.materials || realm.items || '',
        techniques: realm.techniques || realm.skills || realm.methods || '',
      });

      // 边：从角色名到第一个境界
      if (ri === 0) {
        edges.push({
          source: charId, target: realmId,
          label: isCurrent ? '当前' : '',
          color: charColors[ci % charColors.length], directed: true, style: 'solid',
        });
      }

      // 边：境界间的晋升
      if (ri > 0) {
        const prevName = realmLevels[ri - 1].name || realmLevels[ri - 1].realm || `境界${ri}`;
        const prevId = `${char.name}-${prevName}`;
        const reqStr = realm.requirements || realm.requirement || '';
        edges.push({
          source: prevId, target: realmId,
          label: reqStr ? `需:${reqStr.slice(0, 6)}` : '晋升',
          color: realmColors[ri % realmColors.length], directed: true, style: 'solid',
        });
      }
    });
  });

  const hint = nodes.length === 0 ? '在「世界观」中填写境界/等级体系，在「人物及关系」中添加角色后，这里会自动生成各角色的境界晋升图谱' : '';
  return { nodes, edges, title, emptyHint: hint };
}

/* ===== 地点图谱解析（从地图数据构建层级关联图） ===== */
function parseLocationGraph(data: string, title: string): { nodes: GraphNode[]; edges: GraphEdge[]; title: string; emptyHint: string } {
  const nodes: GraphNode[] = [];
  const edges: GraphEdge[] = [];
  const colors: Record<string, string> = { l1: '#4a8b4a', l2: '#5b8def', l3: '#f39c12' };
  const sizes: Record<string, number> = { l1: 26, l2: 22, l3: 18 };
  const yLevels: Record<string, number> = { l1: 60, l2: 200, l3: 350 };

  if (!data.trim()) return { nodes, edges, title, emptyHint: '在「地图」中添加地点后，这里会自动生成地点关联图谱' };

  let parsed: MapRegion[] = [];
  try {
    const p = JSON.parse(data);
    if (Array.isArray(p)) parsed = p;
  } catch {
    // 纯文本：按行解析为L1节点
    data.split('\n').filter(l => l.trim()).forEach(line => {
      const cleaned = line.replace(/^[#\d\.\-•*→>]\s*/, '');
      const match = cleaned.match(/[【\[](.+?)[】\]]/);
      parsed.push({ name: (match ? match[1] : cleaned.slice(0, 15)).trim() });
    });
  }

  if (parsed.length === 0) return { nodes, edges, title, emptyHint: '在「地图」中添加地点后，这里会自动生成地点关联图谱' };

  // 计算L2节点总数用于列间距
  const totalL2 = parsed.reduce((s, r1) => s + (r1.children?.length || 0), 0);
  const l2Spacing = Math.max(50, Math.min(80, 300 / Math.max(totalL2, 1)));
  const l3Spacing = 40;

  const visitedNodes: GraphNode[] = [];
  parsed.forEach((r1, i1) => {
    const childCount = r1.children?.length || 0;
    const x1 = 200 + (i1 - (parsed.length - 1) / 2) * Math.max(110, (childCount + 1) * l2Spacing);
    const r1Visited = !!r1.visited;
    const r1Id = `loc-${r1.name}`;
    const r1Node: GraphNode = {
      id: r1Id, label: r1.name, x: x1, y: yLevels.l1,
      color: colors.l1, size: sizes.l1, nodeType: 'l1', desc: r1.desc, visited: r1Visited,
    };
    nodes.push(r1Node);
    if (r1Visited) visitedNodes.push(r1Node);

    r1.children?.forEach((r2, i2) => {
      const x2 = x1 + (i2 - (childCount - 1) / 2) * l2Spacing;
      const r2Id = `loc-${r1.name}/${r2.name}`;
      const r2Visited = !!r2.visited;
      const r2Node: GraphNode = {
        id: r2Id, label: r2.name, x: x2, y: yLevels.l2,
        color: colors.l2, size: sizes.l2, nodeType: 'l2', desc: r2.desc, visited: r2Visited,
      };
      nodes.push(r2Node);
      if (r2Visited) visitedNodes.push(r2Node);
      edges.push({ source: r1Id, target: r2Id, color: colors.l1, style: 'solid' });

      const grandCount = r2.children?.length || 0;
      r2.children?.forEach((r3, i3) => {
        const x3 = x2 + (i3 - (grandCount - 1) / 2) * l3Spacing;
        const r3Id = `loc-${r1.name}/${r2.name}/${r3.name}`;
        const r3Visited = !!r3.visited;
        const r3Node: GraphNode = {
          id: r3Id, label: r3.name, x: x3, y: yLevels.l3,
          color: colors.l3, size: sizes.l3, nodeType: 'l3', desc: r3.desc, visited: r3Visited,
        };
        nodes.push(r3Node);
        if (r3Visited) visitedNodes.push(r3Node);
        edges.push({ source: r2Id, target: r3Id, color: colors.l2, style: 'dashed' });
      });
    });
  });

  // 主角路径：已访问地点之间用高亮虚线连接
  for (let i = 0; i < visitedNodes.length - 1; i++) {
    edges.push({
      source: visitedNodes[i].id, target: visitedNodes[i + 1].id,
      label: '路径', color: '#f39c12', style: 'dashed', directed: true,
    });
  }

  // 对L2和L3节点运行力导向布局改善间距
  const l2n = nodes.filter(n => n.nodeType === 'l2' || n.nodeType === 'l3');
  if (l2n.length > 1) forceLayout(l2n, edges, 50);

  return { nodes, edges, title, emptyHint: '' };
}
