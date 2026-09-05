/** 创作页（P2b 拆分后）：本文件只保留主组件骨架，各维度面板在 ./write/ 目录。 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api } from '../api';
import { useStore } from '../store';
import type { AISession, Book, BookBible, BrainstormResult, BrainstormSuggestion, Chapter, SkillPack } from '../types';
import AiCreateModal from './AiCreateModal';
import EntityRegistryModal from './EntityRegistryModal';
import { ALL_TABS, CHAPTER_SKILL_KEYS, DIMENSION_LABELS, DIMENSION_SKILL_KEYS, FIELD_AI_PROMPTS, TAB_ROW_1, TAB_ROW_2, collapseNewlines, extractSkillPrompt, stripInternalTags } from './write/write-shared';
import { ConceptPanel } from './write/ConceptPanel';
import { LocationsPanel } from './write/LocationsPanel';
import { ForeshadowingPanel } from './write/ForeshadowingPanel';
import { ChapterPanel } from './write/ChapterPanel';
import { DynamicMemoryPanel } from './write/DynamicMemoryPanel';
import { SettingsCombinedPanel } from './write/SettingsCombinedPanel';
import { OutlineCombinedPanel } from './write/OutlineCombinedPanel';
import { CharacterPanel } from './write/CharacterPanel';
import { PlotPanel } from './write/PlotPanel';
import { InventoryPanel } from './write/InventoryPanel';
import { BibleEditPanel } from './write/BibleEditPanel';

export default function WritePage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const bookId = searchParams.get('book');
  const openChatPanel = useStore((s: any) => s.openChatPanel) as (bid: string, sessionId?: string | null, preset?: { tab?: 'setting' | 'chapter' | 'deai' | 'review'; input?: string; fixTasks?: Array<{ location: string; desc: string; fix: string; severity?: string; dimKey?: string }> }) => void;

  const [book, setBook] = useState<Book | null>(null);
  const [books, setBooks] = useState<Book[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState('concept');
  const [bible, setBible] = useState<BookBible | null>(null);
  const [headerCollapsed, setHeaderCollapsed] = useState(false);

  // 防遗忘检查红点和通知（由 ForeshadowingPanel 回调更新 + 顶层轮询兜底）
  const [afPendingCount, setAfPendingCount] = useState(0);
  const [afAlert, setAfAlert] = useState<{ reportId: string; title: string; score?: number; auto?: boolean } | null>(null);

  useEffect(() => {
    if (!bookId) return;
    function computeStatus(list: any[]) {
      const pending = list.filter(r => r.status === 'pending' || (!r.status && (r.fix_draft?.length > 0 || (typeof r.health_score === 'number' && r.health_score < 80))));
      const count = pending.length;
      const top = pending.find(r => r.auto_generated && !r.notified && (r.fix_draft?.length > 0 || (typeof r.health_score === 'number' && r.health_score < 80)));
      const alert = top ? { reportId: top.id, title: top.title, score: top.health_score, auto: true } : null;
      return { count, alert };
    }
    function check() {
      api.listAntiForgetReports(bookId || '').then(data => {
        const list = Array.isArray(data.reports) ? data.reports : [];
        const { count, alert } = computeStatus(list);
        setAfPendingCount(count);
        setAfAlert(alert);
      }).catch(() => {});
    }
    check();
    const id = setInterval(check, 30000);
    return () => clearInterval(id);
  }, [bookId]);

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
  const aiChatHistoryKey = bookId ? `app-ai-chat-${bookId}` : '';
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
    setEditValue(collapseNewlines(currentContent));  // 全局：段间不留空一行
    setEditing(true);
  }

  async function saveEdit() {
    if (!bookId) return;
    setSaving(true);
    try {
      // 全局：段间不留空一行 → 保存时压缩，后端存储就没有双换行
      const updated = await api.updateBible(bookId, { [currentTab.field]: collapseNewlines(editValue) } as any);
      setBible(updated);
      setEditing(false);
      if (currentTab.field === 'concept') {
        setConcept(editValue);
      }
      try { window.dispatchEvent(new CustomEvent('app:progress-needs-refresh', { detail: { field: currentTab.field } })); } catch {}
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
      setEditValue(collapseNewlines(result.content));  // 全局：段间不留空一行
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
        try { window.dispatchEvent(new CustomEvent('app:progress-needs-refresh', { detail: { field: currentTab.field } })); } catch {}
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
    setChapterEditContent(collapseNewlines(activeChapter.content || ''));  // 全局：段间不留空一行
    setChapterEditing(true);
  }

  async function loadChapterDetail(chId: string) {
    if (!bookId) return;
    try {
      const ch = await api.getChapter(bookId, chId);
      setActiveChapter(ch);
      setChapterEditTitle(ch.title);
      setChapterEditContent(collapseNewlines(ch.content || ''));  // 全局：段间不留空一行
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
        // 全局：段间不留空一行 → 保存时统一压缩，后端存的就没有双换行
        content: collapseNewlines(chapterEditContent),
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
              // P0-2修复：后端 timeline 已是全卷数组（upsert）、bible 已落库，直接用返回值不手动合并
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
      setChapterEditContent(collapseNewlines(saveTarget.content || ''));  // 全局：段间不留空一行
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
        // 修复上下文脱节：传待写章号（前端 word_count>=100 判定，比后端 max+1 准）+ 上一版未保存内容（注入为最近一章避免断档）
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
        // 不因 signal.aborted 跳过错误提示和状态重置（fetchWithRetry 内部超时 abort 时
        // signal.aborted=false 但仍抛"请求已取消"，return 会跳过 finally → 界面卡死无法再点发送）
        if (!aiStoppedRef.current) {
          setAiStreamError(e.message || 'Agent管线调用失败，请检查AI配置');
        }
      } finally {
        setAiCreating(false); // 无条件重置，防止异常路径界面卡死（重复设无副作用）
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
              // 【优化3】gate_result 事件：流式模式落地门禁（与非流式对齐，只告警不阻断）
              if (parsed.gate_result) {
                setAgentMeta((prev: any) => ({ ...prev, gate_result: parsed.gate_result }));
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
        } else if (!aiStoppedRef.current && !signal.aborted) {
          // 【空回复修复】流结束但零正文（后端未推送 error 帧的极端场景）→ 显式报错替代静默
          setAiStreamError('生成结果为空：LLM 未返回正文，请重试或检查 AI 配置/额度');
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
      const current = collapseNewlines(chapterEditContent);  // 段间不留空一行
      const incoming = collapseNewlines(content);            // 段间不留空一行
      content = current
        ? current.replace(/\s+$/, '') + '\n' + incoming      // 段间只用单换行（不用 \n\n 产生空横格）
        : incoming;
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
  const isSettingsTab = activeTab === 'settings';

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
                {tab.key === 'foreshadowing' && afPendingCount > 0 && (
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#ff4757', marginLeft: 4, display: 'inline-block' }} />
                )}
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
                {tab.key === 'foreshadowing' && afPendingCount > 0 && (
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#ff4757', marginLeft: 4, display: 'inline-block' }} />
                )}
              </button>
            ))}
          </div>
        </div>
      )}

      {afAlert && (
        <div style={{ background: '#fff3cd', borderBottom: '1px solid #ffeaa7', padding: '8px 12px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, fontSize: 13 }}>
          <div>
            <b>🛡️ 防遗忘检查提醒</b>：「{afAlert.title}」{typeof afAlert.score === 'number' ? `健康度 ${afAlert.score}` : ''}，AI 已生成修正草稿，请审阅后决定是否应用。
          </div>
          <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
            <button className="btn-primary-sm" onClick={() => setActiveTab('foreshadowing')}>立即查看</button>
            <button className="btn-ghost-sm" onClick={() => {
              if (bookId) api.updateAntiForgetReport(bookId, afAlert.reportId, { notified: true }).catch(() => {});
              setAfAlert(null);
            }}>忽略</button>
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
            onAfStatusChange={(pendingCount, alert) => {
              setAfPendingCount(pendingCount);
              setAfAlert(alert);
            }}
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
          />
        ) : isSettingsTab ? (
          <SettingsCombinedPanel
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
            totalVolumes={book?.total_volumes || 0}
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
            content={collapseNewlines(currentContent)}
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
            // 重命名后重新拉取 bible 与章节列表，并刷新正文阅读区当前章内容
            try {
              const bb = await api.getBible(bookId);
              if (bb) setBible(bb);
            } catch {}
            try {
              const list = await api.listChapters(bookId);
              setChapters(list);
              setActiveChapter(prev => {
                if (!prev) return prev;
                return list.find(c => c.id === prev.id) || prev;
              });
            } catch {}
          }}
        />
      )}
    </div>
  );
}
