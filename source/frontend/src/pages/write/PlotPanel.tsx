/** PlotPanel —— 自 WritePage.tsx 拆出（P2b 巨石拆分，逻辑零改动）。 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api';
import { useStore } from '../../store';
import type { BookBible, Chapter, SkillPack } from '../../types';
import { SkillPackGroupedList, extractSkillPrompt, safeText } from './write-shared';

export function PlotPanel(props: {
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
  const openChatPanel = useStore((s: any) => s.openChatPanel) as (bid: string, sessionId?: string | null, preset?: { tab?: 'setting' | 'chapter' | 'deai' | 'review'; input?: string; fixTasks?: Array<{ location: string; desc: string; fix: string; severity?: string; dimKey?: string }>; role?: string; autoSubmit?: boolean }) => void;
  const bibleDirtySeq = useStore((s: any) => s.bibleDirtySeq) as number;
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

  // 智驾卡片采纳落地会让 bible 变脏，这里重新拉取最新 bible → volumes 自动刷新
  useEffect(() => {
    if (!bookId || !bibleDirtySeq) return;
    api.getBible(bookId).then(b => { onBibleUpdate(b); }).catch(() => {});
  }, [bookId, bibleDirtySeq]);

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

  // AI情节节点设计：切换到智驾助手的「节点设计师」助手，自动提交卷号，
  // 在聊天面板里分段流式生成节点、可随时看到生成进度，生成完后卡片「采纳」落剧情线。
  // 不再单独弹 NodeDesignView 浮层，保证体验和智驾其他助手一致。
  async function handleDesignNodes(_volId: string, _volTitle: string, volIndex: number) {
    if (!bookId) {
      alert('请先打开作品后再进行节点设计');
      return;
    }
    // 归一为合法正整数，避免把 undefined/NaN 传给后端导致「缺少 volume_index」
    const safeVol = Math.max(1, Math.trunc(Number(volIndex)) || 1);
    openChatPanel(bookId, undefined, {
      tab: 'setting',        // 智驾第一排 Tab：里面有通用/构思/设定/世界观 + 助手选择器
      role: 'node_designer',
      input: `第${safeVol}卷`,
      autoSubmit: true,
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
  // 修复「导入分卷大纲后第1卷残留」：按 volume_id/卷名/卷号三级匹配，避免 UUID 与 volume_id 不匹配重复显示
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
      {/* 工作流单行：一键清空 + 反生成/导入 + 自动分卷规划/从大纲提取 + 折叠按钮，全部平铺。
          桌面单行（CSS order 还原「清空→反生成→自动分卷→提取→导入→折叠靠右」）；
          手机端由 .plot-row-break 在「导入」后强制换行，固定两排：
          排1 = 一键清空(可选)+反生成+导入；排2 = 自动分卷规划+从大纲提取各卷+折叠按钮
          （折叠按钮永远排在「自动分卷规划」那排末位，不依赖一键清空是否出现）。 */}
      <div className="bible-edit-header">
        <div className="bible-edit-actions plot-workflow-row">
          {displayVolumes.length > 0 && (
            <button
              className="btn-ghost-sm btn-plot-clear"
              onClick={handleClearAllVolumes}
              disabled={clearing}
              title="一键清空全部分卷大纲（不影响章节表和大纲总纲）"
              style={{ color: '#e74c3c' }}
            >
              {clearing ? '⏳ 清空中...' : '🗑️ 一键清空'}
            </button>
          )}
          {!workflowCollapsed && (<>
            <button
              className="btn-ghost-sm btn-plot-reverse"
              onClick={handleReverseGenerateOutline}
              disabled={reverseLoading || outlineWorkflowLoading !== ''}
              title="从已导入/提取的各卷剧情，反向提炼五幕式总纲，填入大纲维度"
              style={{ color: 'var(--accent)' }}
            >
              {reverseLoading ? '⏳ 反生成中...' : '🔄 反生成五幕式总纲'}
            </button>
            <button
              className="btn-ghost-sm btn-plot-import"
              onClick={() => setImportModalOpen(true)}
              disabled={importLoading}
              title="导入剧情大纲文本，自动识别拆分到各卷"
            >
              📥 导入剧情大纲
            </button>
          </>)}
          <span className="plot-row-break" aria-hidden="true" />
          {!workflowCollapsed && (<>
            <button
              className="btn-ghost-sm btn-plot-calc"
              onClick={() => setShowVolumeCalc(s => !s)}
              disabled={outlineWorkflowLoading !== ''}
              title="输入卷数，按每卷50章×2400字自动生成分卷框架"
              style={showVolumeCalc ? { background: 'var(--accent-light)', color: 'var(--accent)', fontWeight: 700 } : {}}
            >
              📊 自动分卷规划
            </button>
            <button
              className="btn-ghost-sm btn-plot-extract"
              onClick={handleExtractVolumes}
              disabled={extractLoading || outlineWorkflowLoading !== ''}
              title="从大纲总纲（五幕式/AI创作/AI识别/手编均可）一次性提取各卷剧情"
            >
              {extractLoading ? '⏳ 提取中...' : '📋 从大纲提取各卷'}
            </button>
          </>)}
          <button
            className="btn-ghost-sm header-collapse-btn"
            onClick={() => setWorkflowCollapsed(v => !v)}
            title={workflowCollapsed ? '展开工作流' : '折叠工作流（手机友好）'}
          >
            {workflowCollapsed ? '▾' : '▴'}
          </button>
        </div>
      </div>

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
                  <button className="btn-ghost-sm" onClick={() => handleDesignNodes(vol.volume_id || '', vol.volume || `第${idx + 1}卷`, vol.volume_index || (idx + 1))} title="在智驾助手中分段流式设计此卷情节节点">
                    🎯 节点设计
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
