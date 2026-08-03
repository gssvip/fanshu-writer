import { useState, useRef, useEffect, useCallback } from 'react';
import { api } from '../api';
import type { Book, BookBible, SkillPack, AISession, AIMessage } from '../types';

// 维度 field → AI 创作 prompt（与 WritePage 内 FIELD_AI_PROMPTS 保持一致）
// 【P1修复】补充明确的「输出格式铁律」与平台 bible 字段格式对齐，避免模型自由发挥导致前后不搭
const FIELD_AI_PROMPTS: Record<string, string> = {
  concept: '将以下一句话构思扩展为完整的创意方案。\n【输出格式】纯文本，包含以下分项（用空行分隔）：1) 一句话核心概念；2) 核心卖点（3-5条）；3) 目标读者画像；4) 主线冲突；5) 独特亮点。',
  key_rules: '根据已确定的构思与世界背景，生成核心设定规则。\n【输出格式】编号列表，每条规则独占一行并以"① ② ③..."开头，规则之间用空行分隔。包括：世界必须遵循的铁律、人物能力边界、代价/反噬机制、禁忌事项。',
  worldbuilding: '根据已确定的构思与核心规则，生成详细的世界观设定。\n【输出格式】分小节（用二级标题"## 力量体系/## 社会结构/## 地理概况/## 历史脉络"），每小节下用编号列表。不要写成段落散文。',
  character_profiles: '根据已确定的世界观与构思，生成主要人物档案。\n【输出格式】每个角色一个"## 角色：<姓名>"二级标题，下方依次为：身份/性格（3-5个关键词）/背景故事（100-200字）/核心动机/与其他角色关系（用"→ 角色名：关系"格式）。主角 + 3-5个配角。',
  plot_design: '根据已确定的人物与世界，生成五幕式总纲。\n【输出格式】每幕一个"## 第N幕：<幕名>"二级标题，下方为：幕核心目标/主要冲突/卷入角色/关键转折点/幕尾悬念/对应分卷范围（"第X-Y卷"）。共5幕，对应全书所有分卷。',
  timeline: '根据已确定的大纲，生成各分卷详细剧情。\n【分卷铁律】**每卷固定 50 章**，全书卷数 = 总章数÷50（向上取整），卷序号从1开始连续。卷名格式"第N卷 副标题"。\n【输出格式】严格 JSON 数组，不要任何解释文字。结构：[{volume_index, volume, main_plot, core_conflict, ending_hook, nodes:[{title, chapters, type, summary, cool_type}]}]. 每卷 5-8 个 nodes 情节节点；chapters 字段格式"起始章-结束章"，全书 chapter 编号连续不重叠。',
  foreshadowing: '根据已确定的人物、剧情与世界，埋设伏笔线索。\n【输出格式】编号列表，每条格式"## 伏笔N：<伏笔标题>\\n- 埋设内容：xxx\\n- 埋设时机：第X卷Y章附近\\n- 预期回收：第X卷Y章附近\\n- 回收方式：xxx\\n- 对剧情的影响：xxx"。设计 3-5 条。',
  locations: '根据已确定的世界观，设计地点体系。\n【输出格式】严格 JSON 数组，三级结构：[一级大区域 {name, description, secondaries:[二级城市/门派 {name, description, scenes:[三级具体场景 {name, description, key_events}]}]}]. 设计 2-3 个一级大区域。',
  inventory: '根据已确定的人物与世界，生成主要物品/功法/法宝清单。\n【输出格式】严格 JSON 数组：[物品 {name, type, source, effect, owner, first_appearance}]. type 取值：法宝/功法/丹药/武器/防具/其他。设计 8-15 个核心物品。',
  dynamic_volumes: '根据已确定的设定、人物、剧情和章节内容，生成分卷动态文件摘要。\n【输出格式】严格 JSON 数组：[卷 {volume_id, volume, volume_index, summary, characters, events, timeline, locations, factions, foreshadowing, realms, relationships}]。summary 为本卷概述（100-200字），characters/events 等为该卷的关键变化（各50-100字）。每卷一条记录，全书所有卷都要覆盖。',
};

// 维度协同顺序：上游先做，下游基于上游产出
// 选定维度会按这个顺序串行执行；下游维度的 prompt 注入上游已生成内容作为上下文
const DIM_COLLAB_ORDER = [
  'concept', 'key_rules', 'worldbuilding', 'character_profiles',
  'plot_design', 'timeline', 'foreshadowing', 'locations', 'inventory', 'dynamic_volumes',
];

// 维度 field → 协同标签（用于上游上下文展示）
const DIM_COLLAB_LABELS: Record<string, string> = {
  concept: '构思',
  key_rules: '核心设定',
  worldbuilding: '世界观',
  character_profiles: '人物',
  plot_design: '总纲',
  timeline: '分卷剧情',
  foreshadowing: '伏笔',
  locations: '地点',
  inventory: '物资',
  dynamic_volumes: '动态文件',
};

// 维度 field → 技能包 prompt_key 映射
const DIMENSION_SKILL_KEYS: Record<string, string[]> = {
  concept: ['one_line_concept', 'master_outline', 'tomato_plan', 'one_line_hook', 'story_setup'],
  key_rules: ['lock_facts', 'tomato_setting', 'base_rules', 'level_system', 'power_system', 'infinity_rules'],
  plot_design: ['master_outline', 'volume_breakdown', 'chapter_plan', 'tomato_outline', 'quick_outline', 'volume_plan', 'volume_outline'],
  worldbuilding: ['lock_facts', 'tomato_setting', 'base_rules', 'geography', 'history', 'cultures', 'era_setting', 'tech_tree', 'future_society', 'era_geopolitics'],
  character_profiles: ['character_cognition', 'tomato_character', 'cp_design', 'character_moe', 'faction_design', 'soldier_arc'],
  timeline: ['chapter_plan', 'tomato_outline', 'volume_breakdown'],
  foreshadowing: ['foreshadow_register', 'narrative_debt', 'truth_card', 'info_gap', 'red_herring'],
  locations: ['lock_facts', 'tomato_setting', 'geography'],
  inventory: ['lock_facts', 'level_system', 'power_system', 'ability_tree'],
  dynamic_volumes: ['narrative_debt', 'foreshadow_register', 'lock_facts'],
};

// 全部可创作的维度清单（全局模式选择用）
// 物资库和动态文件不在此列：它们根据章节正文提取，不走 AI 总创作
const ALL_DIMENSIONS = [
  { field: 'concept', label: '构思', icon: '💡' },
  { field: 'key_rules', label: '设定', icon: '⚙️' },
  { field: 'worldbuilding', label: '世界观', icon: '🌍' },
  { field: 'plot_design', label: '大纲', icon: '📋' },
  { field: 'character_profiles', label: '人物', icon: '👤' },
  { field: 'timeline', label: '剧情', icon: '📅' },
  { field: 'foreshadowing', label: '伏笔', icon: '🔮' },
  { field: 'locations', label: '地点', icon: '🗺️' },
];

const DIM_LABEL: Record<string, string> = Object.fromEntries(ALL_DIMENSIONS.map(d => [d.field, d.label]));

// 从多个技能包中提取匹配的提示词（合并）
function extractSkillPrompt(packs: SkillPack[], keys: string[]): string {
  const notes: string[] = [];
  let totalLen = 0;
  for (const pack of packs.slice(0, 3)) {
    if (!pack || !pack.prompts) continue;
    for (const key of keys) {
      if (pack.prompts[key]) {
        let p = pack.prompts[key].slice(0, 1500);
        if (totalLen + p.length > 5000) {
          p = p.slice(0, 5000 - totalLen);
        }
        if (p.length === 0) break;
        notes.push(`【${pack.name}】\n${p}`);
        totalLen += p.length;
        break;
      }
    }
    if (totalLen >= 5000) break;
  }
  return notes.length > 0 ? notes.join('\n\n') : '';
}

// 清理 timeline 维度的 JSON 输出：
// 1) 剥离 markdown 代码块包裹（```json ... ```）
// 2) 解包 {volumes:[...]} / {data:[...]} 等包装对象
// 3) 规范化为纯 JSON 数组字符串，保证剧情维度可正确解析
// 与后端 ai_master_create_stream 的清理逻辑保持一致，避免前端用原始输出覆盖后端已清理的版本
function cleanTimelineJson(content: string): string {
  if (!content || !content.trim()) return content;
  let cleaned = content.trim();
  const fence = cleaned.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
  if (fence) cleaned = fence[1].trim();
  try {
    let parsed = JSON.parse(cleaned);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      for (const k of ['volumes', 'data', 'result', 'items', 'list']) {
        if (Array.isArray((parsed as any)[k])) { parsed = (parsed as any)[k]; break; }
      }
    }
    if (Array.isArray(parsed)) {
      return JSON.stringify(parsed, null, 2);
    }
  } catch { /* 非合法 JSON，返回去 Fence 后的文本 */ }
  return cleaned;
}

interface AiCreateModalProps {
  mode: 'global' | 'single';
  dimension?: string; // single 模式锁定的维度 field
  bookId: string;
  book: Book | null;
  bible: BookBible | null;
  skillPacks: SkillPack[];
  selectedSkillPackIds: string[];
  onApply: (field: string, content: string) => Promise<void>; // 单维度填入
  onApplyMany: (results: { field: string; content: string }[]) => Promise<void>; // 全局批量填入
  onClose: () => void;
  resumeSession?: AISession | null; // 恢复历史会话（继续对话），传入完整会话对象
  onSessionSaved?: (sessionId: string) => void; // 会话保存后回调（刷新历史列表）
}

type Phase = 'input' | 'streaming' | 'done';

export default function AiCreateModal({
  mode, dimension, bookId, book, bible, skillPacks, selectedSkillPackIds, onApply, onApplyMany, onClose,
  resumeSession, onSessionSaved,
}: AiCreateModalProps) {
  // 选中的维度（全局模式可多选，单维度模式锁定）
  const [selectedDims, setSelectedDims] = useState<string[]>(mode === 'single' && dimension ? [dimension] : ['concept']);
  const [instruction, setInstruction] = useState('');
  const [modification, setModification] = useState(''); // 修改意见
  const [outputs, setOutputs] = useState<Record<string, string>>({});
  const [warnings, setWarnings] = useState<Record<string, string>>({}); // 【P2-8】维度级警告
  const [currentDim, setCurrentDim] = useState<string>(''); // 当前流式生成中的维度
  const [phase, setPhase] = useState<Phase>('input');
  const [error, setError] = useState('');
  const [applying, setApplying] = useState(false);
  const [editedOutputs, setEditedOutputs] = useState<Record<string, string>>({}); // 用户手动编辑后的结果
  const [skillExpanded, setSkillExpanded] = useState(false); // 协同技能包折叠
  const [collapsedDims, setCollapsedDims] = useState<Record<string, boolean>>({}); // 各维度结果折叠状态
  const [localSkillPackIds, setLocalSkillPackIds] = useState<string[]>(selectedSkillPackIds); // 本地技能包选择（默认继承主页面勾选）
  const outputRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null); // 用于中断 AI 流式生成
  const stoppedRef = useRef(false); // 标记用户是否主动停止（避免 catch 时误报"生成失败"）
  // 记录最近一次生成参数（用于重新生成时带上修改意见）
  const lastGenRef = useRef<{ instruction: string; modification: string }>({ instruction: '', modification: '' });

  // ===== 会话持久化（需求1b：首页与创作页消息互通） =====
  // scope=global_create 的会话按书聚合，每次"开始生成/重新生成"追加一轮 user+assistant 消息
  const sessionIdRef = useRef<string>('');
  const sessionMessagesRef = useRef<AIMessage[]>([]);

  // 恢复历史会话：传入 resumeSession 时，回填维度/输出/指令，并直接进入 done 阶段（可提修改意见重新生成）
  useEffect(() => {
    if (!resumeSession) return;
    const msgs = resumeSession.messages || [];
    sessionIdRef.current = resumeSession.id;
    sessionMessagesRef.current = msgs;
    // 从最后一条 assistant 消息恢复 outputs/dims
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'assistant') {
        try {
          const payload = JSON.parse(msgs[i].content);
          if (payload && payload.type === 'ai_create') {
            if (Array.isArray(payload.dims) && payload.dims.length) {
              // 单维度模式保留锁定维度，全局模式恢复多选
              if (isGlobal) setSelectedDims(payload.dims);
            }
            if (payload.outputs && typeof payload.outputs === 'object') {
              setOutputs(payload.outputs);
              setEditedOutputs({});
            }
            break;
          }
        } catch { /* 非 JSON 文本，忽略 */ }
      }
    }
    // 恢复指令（取第一条 user 消息作为原始创作要求）
    const firstUser = msgs.find(m => m.role === 'user');
    if (firstUser && !firstUser.content.startsWith('修改意见')) {
      setInstruction(firstUser.content);
      lastGenRef.current = { instruction: firstUser.content, modification: '' };
    }
    setPhase('done');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resumeSession]);

  // 持久化一轮对话到 AISession（best-effort，失败不阻断主流程）
  const persistSession = useCallback(async (userMsg: string, dims: string[], outs: Record<string, string>) => {
    if (!bookId) return;
    // 跳过空输出（如用户主动停止且无内容）
    const hasContent = Object.values(outs).some(v => v && v.trim());
    if (!hasContent) return;
    try {
      const assistantContent = JSON.stringify({ type: 'ai_create', dims, outputs: outs });
      const newMessages: AIMessage[] = [
        ...sessionMessagesRef.current,
        { role: 'user', content: userMsg },
        { role: 'assistant', content: assistantContent },
      ];
      if (sessionIdRef.current) {
        await api.updateAISession(sessionIdRef.current, { messages: newMessages });
        sessionMessagesRef.current = newMessages;
        onSessionSaved?.(sessionIdRef.current);
      } else {
        const title = (userMsg || 'AI总创作对话').replace(/^修改意见：/, '').slice(0, 24).trim() || 'AI总创作对话';
        const created = await api.createAISession({ book_id: bookId, scope: 'global_create', scope_id: '', title });
        await api.updateAISession(created.id, { messages: newMessages });
        sessionIdRef.current = created.id;
        sessionMessagesRef.current = newMessages;
        onSessionSaved?.(created.id);
      }
    } catch {
      // 持久化失败静默处理
    }
  }, [bookId, onSessionSaved]);

  const selectedPacks = skillPacks.filter(p => localSkillPackIds.includes(p.id));
  const isGlobal = mode === 'global';

  // 切换技能包勾选
  const togglePack = useCallback((id: string) => {
    setLocalSkillPackIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);

  // 组装单维度的 messages
  // upstream：本轮已生成的上游维度内容（按 DIM_COLLAB_ORDER 顺序回流），用于保持各维度前后一致
  // bibleCtx：已有bible其他维度内容（单维度模式注入，解决"不读其他维度"问题）
  const buildMessages = useCallback((
    dim: string,
    userInstruction: string,
    modificationNote: string,
    prevOutput: string | undefined,
    upstream: Record<string, string>,
  ) => {
    const prompt = FIELD_AI_PROMPTS[dim] || `请为「${DIM_LABEL[dim] || dim}」维度生成内容。`;
    const skillKeys = DIMENSION_SKILL_KEYS[dim] || [];
    const skillPrompt = extractSkillPrompt(selectedPacks, skillKeys);
    const formatIntegrationRule = selectedPacks.length > 0 ? `\n\n【格式整合铁律·必读】下方「技能包内容」是创作方法论（指导原则），不是输出格式模板。**必须**严格按上方任务的「输出格式」骨架输出，技能包的要求**整合映射**到对应字段。例如：技能包要求"CDL角色档案"中的"外貌特征/战斗风格"应并入"## 角色：<姓名>"下方的"身份"或新增子项；技能包要求"五不妥协原则"应整合到"## 力量体系"等小节内。不要把技能包字段原样搬出来，要按平台格式重新组织。` : '';
    const skillNote = selectedPacks.length > 0 ? `\n\n【已加载技能包：${selectedPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}${formatIntegrationRule}` : '';
    const concept = bible?.concept || book?.synopsis || '暂无构思';

    // 拼接已有bible其他维度作为上下文（单维度模式时注入，弥补不走后端master_create的不足）
    const bibleCtxParts: string[] = [];
    for (const up of DIM_COLLAB_ORDER) {
      if (up === dim) continue;
      // 优先用本轮上游产物（如果有），否则用已有bible内容
      const v = upstream[up] || (bible as any)?.[up] || '';
      if (v && v.trim()) {
        bibleCtxParts.push(`【${DIM_COLLAB_LABELS[up] || up}（已确认）】\n${v.slice(0, 800)}`);
      }
    }
    const upstreamCtx = bibleCtxParts.length > 0
      ? '\n\n【已确认的其他维度产物（必须与本维度保持一致，不可矛盾；缺失维度视为未规划）】\n' + bibleCtxParts.join('\n\n')
      : '\n\n【其他维度】暂无已确认的产物，本维度为起点。';

    const system = `你是专业网文创作助手，正在参与多维度协同创作。用户正在创作一部${book?.book_type || '小说'}，题材为${book?.genre || '通用'}。
【你的当前任务】生成「${DIM_LABEL[dim] || dim}」维度内容。
【核心约束】必须与已确认维度保持一致；不可与其他维度矛盾；缺失的维度可在产物中预留对接点但不要凭空生成。${upstreamCtx}${skillNote}`;

    let user = `${prompt}\n\n【用户原始构思】${concept}\n\n【用户要求】${userInstruction || '请基于已确认维度自由发挥'}`;
    if (modificationNote) {
      user += `\n\n【上一轮生成结果】\n${(prevOutput || '').slice(0, 2000)}\n\n【用户修改意见】${modificationNote}\n请根据修改意见调整优化。`;
    }
    return [
      { role: 'system', content: system },
      { role: 'user', content: user },
    ];
  }, [bible, book, selectedPacks]);

  // 流式生成
  // 全局模式：走后端 ai-master-create/stream（注入已有bible+本轮上游维度，格式铁律更完善）
  // 单维度模式：走前端 aiChatStream（注入已有bible其他维度作为上下文，解决"不读其他维度"问题）
  const streamGenerate = useCallback(async (dims: string[], userInstruction: string, modificationNote: string, opts?: { keepOthers?: boolean }) => {
    if (dims.length === 0) {
      setError('请至少选择一个维度');
      return;
    }
    setError('');
    setPhase('streaming');
    stoppedRef.current = false;
    abortRef.current = new AbortController();
    lastGenRef.current = { instruction: userInstruction, modification: modificationNote };
    // keepOthers=true（单维度重做）：保留其他维度已生成内容，只重做指定维度
    const keepOthers = !!opts?.keepOthers;
    const newOutputs: Record<string, string> = keepOthers ? { ...outputs, ...editedOutputs } : {};
    if (!keepOthers) {
      setOutputs({});
      setEditedOutputs({});
    } else {
      // 清掉待重做维度的旧内容，保留其他
      for (const d of dims) { delete newOutputs[d]; }
      setOutputs({ ...newOutputs });
      setEditedOutputs({});
    }

    const orderedDims = DIM_COLLAB_ORDER.filter(d => dims.includes(d));

    if (isGlobal) {
      // ===== 全局模式：走后端流式接口 =====
      // 传入本轮已生成内容（含用户编辑），让后端注入为跨维度上下文，实现"先生成构思→再生成人物时能读构思"的实时互通
      // 优先级：用户编辑过的（editedOutputs）> 原始流式产出（outputs）
      const sessionOutputs: Record<string, string> = {};
      const allDims = Array.from(new Set([...Object.keys(outputs), ...Object.keys(editedOutputs)]));
      for (const d of allDims) {
        const v = editedOutputs[d] ?? outputs[d];
        if (v && v.trim()) sessionOutputs[d] = v;
      }
      try {
        const response = await api.aiMasterCreateStream(
          bookId, orderedDims, localSkillPackIds, userInstruction,
          abortRef.current?.signal,
          sessionOutputs,
        );
        if (!response.ok || !response.body) throw new Error(`请求失败 (HTTP ${response.status})`);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let curDim = '';
        while (true) {
          if (abortRef.current?.signal.aborted) { try { reader.cancel(); } catch {} break; }
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const chunk = line.slice(6).trim();
            if (chunk === '[DONE]') break;
            try {
              const parsed = JSON.parse(chunk);
              // 维度开始/结束信号
              if (parsed.dim && parsed.start) {
                curDim = parsed.dim;
                setCurrentDim(curDim);
                newOutputs[curDim] = newOutputs[curDim] || '';
                continue;
              }
              if (parsed.dim && parsed.done) {
                // 【P2-8】维度完成信号附带 warning（timeline卷数错位/DAG构建失败等）
                if (parsed.warning) {
                  setWarnings(prev => {
                    const next = { ...prev };
                    next[parsed.dim] = parsed.warning;
                    return next;
                  });
                }
                continue;
              }
              if (parsed.error) {
                setError(`生成失败：${parsed.error}`);
                break;
              }
              // 流式内容 chunk
              const delta = parsed.choices?.[0]?.delta?.content || '';
              if (delta && curDim) {
                newOutputs[curDim] = (newOutputs[curDim] || '') + delta;
                setOutputs({ ...newOutputs });
              }
            } catch { /* ignore parse error */ }
          }
        }
      } catch (e: any) {
        if (stoppedRef.current || abortRef.current?.signal.aborted) { /* 用户主动停止 */ }
        else setError(`全局创作失败：${e.message || '网络错误'}`);
      }
      setCurrentDim('');
      setPhase('done');
      // 持久化本轮对话（需求1b：首页与创作页消息互通）
      const gUserMsg = modificationNote ? `修改意见：${modificationNote}` : (userInstruction || 'AI总创作');
      persistSession(gUserMsg, orderedDims, newOutputs);
      return;
    }

    // ===== 单维度模式：走前端串行（注入已有bible其他维度上下文） =====
    const generatedSoFar: Record<string, string> = {};
    for (const dim of orderedDims) {
      if (abortRef.current?.signal.aborted) break;
      setCurrentDim(dim);
      newOutputs[dim] = '';
      const prev = modificationNote ? (outputs[dim] || '') : undefined;
      const messages = buildMessages(dim, userInstruction, modificationNote, prev, generatedSoFar);
      try {
        const response = await api.aiChatStream(messages, abortRef.current?.signal);
        if (abortRef.current?.signal.aborted) break;
        if (!response.ok || !response.body) throw new Error(`请求失败 (HTTP ${response.status})`);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
          if (abortRef.current?.signal.aborted) { try { reader.cancel(); } catch {} break; }
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
                const delta = parsed.choices?.[0]?.delta?.content || '';
                if (delta) {
                  newOutputs[dim] += delta;
                  setOutputs({ ...newOutputs });
                }
              } catch { /* ignore parse error */ }
            }
          }
        }
        generatedSoFar[dim] = newOutputs[dim] || '';
      } catch (e: any) {
        if (stoppedRef.current || abortRef.current?.signal.aborted) break;
        setError(`「${DIM_LABEL[dim] || dim}」生成失败：${e.message || '网络错误'}`);
        setCurrentDim('');
        setPhase('done');
        return;
      }
    }
    setCurrentDim('');
    setPhase('done');
    // 持久化本轮对话（需求1b：首页与创作页消息互通）
    const sUserMsg = modificationNote ? `修改意见：${modificationNote}` : (userInstruction || 'AI创作');
    persistSession(sUserMsg, orderedDims, newOutputs);
  }, [buildMessages, outputs, editedOutputs, isGlobal, bookId, localSkillPackIds, persistSession]);

  // 停止生成：中断 fetch 和 reader，保留已生成内容
  function stopGenerate() {
    if (!abortRef.current) return;
    stoppedRef.current = true;
    abortRef.current.abort();
    abortRef.current = null;
    setCurrentDim('');
    setPhase('done');
  }

  // 开始生成
  function handleStart() {
    if (!instruction.trim() && selectedDims.length > 0) {
      // 允许空 instruction（自由发挥），但建议填写
    }
    streamGenerate(selectedDims, instruction.trim(), '');
  }

  // 重新生成（带修改意见）
  function handleRegenerate() {
    streamGenerate(selectedDims, lastGenRef.current.instruction, modification.trim());
    setModification('');
  }

  // 单维度重新生成：只重做指定维度，其他维度已生成内容作为上下文注入（跨维度实时互通）
  function handleRegenerateDim(dim: string) {
    streamGenerate([dim], lastGenRef.current.instruction, '', { keepOthers: true });
  }

  // 追加维度：保留已生成的维度内容作为上下文，生成用户新勾选但尚未生成的维度
  // 用于"已生成构思，现在想基于构思再生成人物/大纲等"的场景
  function handleAppendDims() {
    const finalOutputs = { ...outputs, ...editedOutputs };
    const newDims = selectedDims.filter(d => !(finalOutputs[d] || '').trim());
    if (newDims.length === 0) {
      setError('没有新维度需要生成（已选维度均已生成，可改选其他维度后再追加）');
      return;
    }
    streamGenerate(newDims, lastGenRef.current.instruction, '', { keepOthers: true });
  }

  // 确定：填入对应维度
  async function handleConfirm() {
    setApplying(true);
    setError('');
    try {
      const finalOutputs = { ...outputs, ...editedOutputs }; // 编辑过的优先
      const results = selectedDims
        .map(d => {
          let content = (finalOutputs[d] || '').trim();
          // timeline 维度：清理 JSON（剥离 markdown 代码块、解包包装对象），
          // 保证剧情维度可正确解析为分卷结构，避免被当作纯文本整体显示
          if (d === 'timeline' && content) {
            content = cleanTimelineJson(content);
          }
          return { field: d, content };
        })
        .filter(r => r.content);
      if (results.length === 0) {
        setError('没有可填入的内容');
        setApplying(false);
        return;
      }
      if (results.length === 1) {
        await onApply(results[0].field, results[0].content);
      } else {
        await onApplyMany(results);
      }
      onClose();
    } catch (e: any) {
      setError('填入失败：' + (e.message || '未知错误'));
    }
    setApplying(false);
  }

  // 切换维度选择（全局模式）
  function toggleDim(field: string) {
    setSelectedDims(prev => prev.includes(field) ? prev.filter(f => f !== field) : [...prev, field]);
  }

  // 流式时自动滚动到底部
  useEffect(() => {
    if (phase === 'streaming' && outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
    }
  }, [outputs, phase]);

  // 组件卸载时中止进行中的请求
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  const hasOutput = Object.values(outputs).some(v => v && v.trim());
  const totalChars = Object.values(outputs).reduce((s, v) => s + (v?.length || 0), 0);

  // 包装关闭：流式中关闭时先停生成，避免后台 fetch/reader 泄漏
  const handleClose = useCallback(() => {
    if (phase === 'streaming') stopGenerate();
    onClose();
  }, [phase, onClose]);

  return (
    <div className="modal-overlay" onClick={() => phase !== 'streaming' && handleClose()}>
      <div className="master-create-modal" onClick={e => e.stopPropagation()}>
        {/* 顶部 Header */}
        <div className="master-create-modal-header">
          <h2 style={{ margin: 0, fontSize: 17, fontWeight: 700 }}>
            ✨ {isGlobal ? 'AI 总创作' : `AI 创作·${DIM_LABEL[dimension || ''] || ''}`}
          </h2>
          <button className="btn-ghost-sm" onClick={handleClose} disabled={phase === 'streaming'} title="关闭">✕</button>
        </div>

        {/* 主体 */}
        <div className="master-create-modal-body">
          {/* 维度选择（全局模式；输入阶段+完成阶段都显示，继续创作时可改选维度） */}
          {isGlobal && (phase === 'input' || phase === 'done') && (
            <div style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
                选择要创作的维度（可多选，将串行生成）{phase === 'done' && <span style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 400 }}> · 新勾选维度后点「追加维度」基于已生成内容生成</span>}
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {ALL_DIMENSIONS.map(d => (
                  <button
                    key={d.field}
                    className={selectedDims.includes(d.field) ? 'btn-primary-sm' : 'btn-ghost-sm'}
                    style={{ fontSize: 12 }}
                    onClick={() => toggleDim(d.field)}
                  >
                    {d.icon} {d.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* 需求输入（输入阶段） */}
          {phase === 'input' && (
            <>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>创作要求：</div>
              <textarea
                className="input master-create-textarea"
                placeholder={`请描述你对${isGlobal ? '各维度' : DIM_LABEL[dimension || ''] || ''}的创作要求，例如：\n- 主角是个失忆少年，在修仙世界崛起\n- 风格偏热血爽文，节奏明快\n- 三大势力争霸，主角夹在中间\n\n留空则由 AI 自由发挥`}
                value={instruction}
                onChange={e => setInstruction(e.target.value)}
                rows={6}
                autoFocus
              />

              {/* 协同技能包（可折叠多选）—— AI总创作只注入构思类（master）技能包 */}
              {skillPacks.filter(p => (p.category || 'master') === 'master').length > 0 && (
                <div className="skill-pack-collapsible" style={{ marginTop: 12 }}>
                  <button
                    type="button"
                    className="skill-pack-toggle"
                    onClick={() => setSkillExpanded(v => !v)}
                  >
                    <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
                    <span>📦 协同技能包（构思类）</span>
                    {selectedPacks.length > 0 && <span className="skill-pack-toggle-badge">{selectedPacks.length}</span>}
                    <span className="skill-pack-toggle-hint">{skillExpanded ? '收起' : '展开'}</span>
                  </button>
                  {skillExpanded && (
                    <>
                      <div className="skill-pack-checkbox-list">
                        {skillPacks.filter(p => (p.category || 'master') === 'master').map(p => (
                          <label key={p.id} className={`skill-pack-checkbox-item ${localSkillPackIds.includes(p.id) ? 'checked' : ''}`}>
                            <input
                              type="checkbox"
                              checked={localSkillPackIds.includes(p.id)}
                              onChange={() => togglePack(p.id)}
                            />
                            <span className="skill-pack-checkbox-icon">{p.icon}</span>
                            <span className="skill-pack-checkbox-name">{p.name}</span>
                          </label>
                        ))}
                      </div>
                      {selectedPacks.length > 0 && (
                        <div className="skill-pack-info-list">
                          {selectedPacks.map(pack => (
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

              <div style={{ display: 'flex', gap: 8, marginTop: 14, justifyContent: 'flex-end' }}>
                <button className="btn-ghost-sm" onClick={onClose}>取消</button>
                <button
                  className="btn-primary-sm"
                  onClick={handleStart}
                  disabled={selectedDims.length === 0}
                >
                  🚀 开始生成
                </button>
              </div>
            </>
          )}

          {/* 流式输出 + 结果展示 */}
          {(phase === 'streaming' || phase === 'done') && (
            <div ref={outputRef} style={{ flex: 1, overflow: 'auto' }}>
              {selectedDims.map(dim => {
                const content = editedOutputs[dim] !== undefined ? editedOutputs[dim] : (outputs[dim] || '');
                const isCurrent = currentDim === dim;
                const dimMeta = ALL_DIMENSIONS.find(d => d.field === dim);
                const hasContent = !!(content && content.trim());
                const isCollapsed = collapsedDims[dim] === true;
                // 当前正在流式生成的维度不折叠，确保用户能看到实时输出
                const canCollapse = phase === 'done' && hasContent && !isCurrent;
                return (
                  <div key={dim} style={{ marginBottom: 12, padding: 10, background: 'var(--bg-tertiary)', borderRadius: 8, border: `1px solid ${isCurrent ? 'var(--accent)' : 'var(--border-color)'}` }}>
                    <div
                      style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: isCollapsed ? 0 : 8, cursor: canCollapse ? 'pointer' : 'default', userSelect: 'none' }}
                      onClick={() => canCollapse && setCollapsedDims(prev => ({ ...prev, [dim]: !prev[dim] }))}
                    >
                      <div style={{ fontSize: 13, fontWeight: 700, display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                        {canCollapse && <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{isCollapsed ? '▶' : '▼'}</span>}
                        {dimMeta?.icon || '📝'} {DIM_LABEL[dim] || dim}
                        {isCurrent && <span style={{ color: 'var(--accent)', marginLeft: 4, fontSize: 11 }}>⏳ 生成中…</span>}
                        {hasContent && <span style={{ fontSize: 10, color: 'var(--text-muted)', fontWeight: 400 }}>{content.length} 字</span>}
                        {warnings[dim] && (
                          <span style={{ color: '#e67e22', fontSize: 11, fontWeight: 400, marginLeft: 4 }} title={warnings[dim]}>
                            ⚠️ {warnings[dim].slice(0, 40)}{warnings[dim].length > 40 ? '…' : ''}
                          </span>
                        )}
                      </div>
                      {phase === 'done' && hasContent && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }} onClick={e => e.stopPropagation()}>
                          <button
                            className="btn-ghost-sm"
                            onClick={() => handleRegenerateDim(dim)}
                            title="只重新生成此维度（其他维度已生成内容会作为上下文注入，保持一致）"
                            style={{ fontSize: 11, padding: '2px 8px' }}
                          >
                            🔄 重做
                          </button>
                        </div>
                      )}
                    </div>
                    {!isCollapsed && (phase === 'done' ? (
                      <textarea
                        className="input"
                        value={content}
                        onChange={e => setEditedOutputs(prev => ({ ...prev, [dim]: e.target.value }))}
                        rows={Math.min(20, Math.max(6, content.split('\n').length + 1))}
                        style={{ width: '100%', fontSize: 13, lineHeight: 1.7, resize: 'vertical' }}
                      />
                    ) : (
                      <div style={{ fontSize: 13, lineHeight: 1.7, whiteSpace: 'pre-wrap', minHeight: 40 }}>
                        {content || (isCurrent ? '（等待输出…）' : '（排队中）')}
                      </div>
                    ))}
                  </div>
                );
              })}
              {phase === 'streaming' && (
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 12, padding: 12, color: 'var(--text-muted)', fontSize: 12 }}>
                  <span>⏳ 正在生成「{DIM_LABEL[currentDim] || ''}」… 已输出 {totalChars} 字</span>
                  <button
                    className="btn-ghost-sm"
                    onClick={stopGenerate}
                    style={{ padding: '4px 12px', fontSize: 12, color: 'var(--accent)', borderColor: 'var(--accent)' }}
                    title="立即停止生成（已生成内容会保留）"
                  >
                    ⏹ 停止
                  </button>
                </div>
              )}
            </div>
          )}

          {/* 修改意见（完成后显示） */}
          {phase === 'done' && (
            <div style={{ marginTop: 14, padding: 12, background: 'var(--bg-secondary)', borderRadius: 8, border: '1px solid var(--border-color)' }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>💬 修改意见（可选）：</div>
              <textarea
                className="input"
                placeholder="对生成结果有什么不满意？写下修改意见，点「重新生成」会带上意见重新生成。例如：\n- 主角性格太冷漠，加些热血\n- 伏笔太多，精简到3条\n- 世界观再详细些"
                value={modification}
                onChange={e => setModification(e.target.value)}
                rows={3}
                style={{ width: '100%', fontSize: 12 }}
              />
            </div>
          )}

          {error && <div className="error-msg" style={{ marginTop: 10 }}>{error}</div>}
        </div>

        {/* 底部操作栏（完成后） */}
        {phase === 'done' && (
          <div style={{ padding: '12px 16px', borderTop: '1px solid var(--border-color)', display: 'flex', gap: 8, justifyContent: 'flex-end', flexShrink: 0, paddingBottom: 'calc(12px + env(safe-area-inset-bottom, 0px))' }}>
            <button className="btn-ghost-sm" onClick={handleClose}>取消</button>
            <button
              className="btn-ghost-sm"
              onClick={handleAppendDims}
              disabled={applying}
              title="保留已生成的维度内容，基于它们生成新勾选的维度（例如已生成构思，再基于构思生成人物/大纲）"
              style={{ background: 'linear-gradient(135deg,#27ae60 0%,#1e8449 100%)', color: '#fff', border: 'none' }}
            >
              ➕ 追加维度
            </button>
            <button
              className="btn-primary"
              onClick={handleRegenerate}
              disabled={!modification.trim()}
              title={modification.trim() ? '带上修改意见重新生成' : '请先填写修改意见'}
              style={!modification.trim() ? { opacity: 0.5, background: 'linear-gradient(135deg,#e67e22 0%,#d35400 100%)' } : { background: 'linear-gradient(135deg,#e67e22 0%,#d35400 100%)', boxShadow: '0 2px 8px rgba(211,84,0,0.35)' }}
            >
              🔄 重新生成
            </button>
            <button
              className="btn-primary-sm"
              onClick={handleConfirm}
              disabled={applying || !hasOutput}
            >
              {applying ? '⏳ 填入中…' : '✅ 确定·填入维度'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
