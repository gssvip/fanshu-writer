import { useState, useRef, useEffect, useCallback } from 'react';
import { api } from '../api';
import type { Book, BookBible, SkillPack } from '../types';

// 维度 field → AI 创作 prompt（与 WritePage 内 FIELD_AI_PROMPTS 保持一致）
const FIELD_AI_PROMPTS: Record<string, string> = {
  concept: '将以下一句话构思扩展为完整的创意方案，包含核心卖点、目标读者、主线冲突、独特亮点。',
  key_rules: '根据以下构思，生成核心设定规则。包括：世界观必须遵循的规则、人物能力限制、禁忌事项。每条规则单独列出。',
  plot_design: '根据以下构思，生成故事大纲。包括：核心主线、分卷规划（每卷目标）、关键转折点、高潮设计、结局走向。',
  worldbuilding: '根据以下构思，生成详细的世界观设定。包括：世界背景、力量体系/科技水平、社会结构、地理概况、历史脉络。',
  character_profiles: '根据以下构思，生成主要人物档案。包括：主角和3-5个重要配角的姓名、身份、性格特征、背景故事、核心动机、人物关系。',
  timeline: '根据以下构思，生成剧情时间线。按时间顺序列出关键事件，每个事件标注涉及的人物和地点。',
  foreshadowing: '根据以下构思，设计3-5条伏笔线索。每条包括：伏笔内容、埋设时机（大概章节）、预期回收方式、对剧情的影响。',
  locations: '根据以下构思，设计三级地点体系。第一级为大区域（如：东大陆、西荒漠），第二级为城市/门派，第三级为具体场景。',
  inventory: '根据以下构思，生成主要物品/功法/法宝清单。包括：名称、类型、来源、效果、归属角色。',
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
};

// 全部可创作的维度清单（全局模式选择用）
const ALL_DIMENSIONS = [
  { field: 'concept', label: '构思', icon: '💡' },
  { field: 'key_rules', label: '设定', icon: '⚙️' },
  { field: 'worldbuilding', label: '世界观', icon: '🌍' },
  { field: 'plot_design', label: '大纲', icon: '📋' },
  { field: 'character_profiles', label: '人物', icon: '👤' },
  { field: 'timeline', label: '剧情', icon: '📅' },
  { field: 'foreshadowing', label: '伏笔', icon: '🔮' },
  { field: 'locations', label: '地点', icon: '🗺️' },
  { field: 'inventory', label: '物资库', icon: '🎒' },
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
}

type Phase = 'input' | 'streaming' | 'done';

export default function AiCreateModal({
  mode, dimension, book, bible, skillPacks, selectedSkillPackIds, onApply, onApplyMany, onClose,
}: AiCreateModalProps) {
  // 选中的维度（全局模式可多选，单维度模式锁定）
  const [selectedDims, setSelectedDims] = useState<string[]>(mode === 'single' && dimension ? [dimension] : ['concept']);
  const [instruction, setInstruction] = useState('');
  const [modification, setModification] = useState(''); // 修改意见
  const [outputs, setOutputs] = useState<Record<string, string>>({});
  const [currentDim, setCurrentDim] = useState<string>(''); // 当前流式生成中的维度
  const [phase, setPhase] = useState<Phase>('input');
  const [error, setError] = useState('');
  const [applying, setApplying] = useState(false);
  const [editedOutputs, setEditedOutputs] = useState<Record<string, string>>({}); // 用户手动编辑后的结果
  const [skillExpanded, setSkillExpanded] = useState(false); // 协同技能包折叠
  const [localSkillPackIds, setLocalSkillPackIds] = useState<string[]>(selectedSkillPackIds); // 本地技能包选择（默认继承主页面勾选）
  const outputRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null); // 用于中断 AI 流式生成
  const stoppedRef = useRef(false); // 标记用户是否主动停止（避免 catch 时误报"生成失败"）
  // 记录最近一次生成参数（用于重新生成时带上修改意见）
  const lastGenRef = useRef<{ instruction: string; modification: string }>({ instruction: '', modification: '' });

  const selectedPacks = skillPacks.filter(p => localSkillPackIds.includes(p.id));
  const isGlobal = mode === 'global';

  // 切换技能包勾选
  const togglePack = useCallback((id: string) => {
    setLocalSkillPackIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);

  // 组装单维度的 messages
  const buildMessages = useCallback((dim: string, userInstruction: string, modificationNote: string, prevOutput?: string) => {
    const prompt = FIELD_AI_PROMPTS[dim] || `请为「${DIM_LABEL[dim] || dim}」维度生成内容。`;
    const skillKeys = DIMENSION_SKILL_KEYS[dim] || [];
    const skillPrompt = extractSkillPrompt(selectedPacks, skillKeys);
    const skillNote = selectedPacks.length > 0 ? `\n\n【已加载技能包：${selectedPacks.map(p => p.name).join('、')}】${skillPrompt ? '\n\n技能指导：\n' + skillPrompt : ''}` : '';
    const concept = bible?.concept || book?.synopsis || '暂无构思';
    const existing = (bible as any)?.[dim] || '';

    const system = `你是专业网文创作助手。用户正在创作一部${book?.book_type || '小说'}，题材为${book?.genre || '通用'}。请根据用户要求生成「${DIM_LABEL[dim] || dim}」维度内容。${bible?.worldbuilding ? `\n已有世界观：${bible.worldbuilding.slice(0, 500)}` : ''}${skillNote}`;

    let user = `${prompt}\n\n【构思】${concept}\n\n【已有${DIM_LABEL[dim] || dim}内容】${(existing || '').slice(0, 1000) || '无'}\n\n【用户要求】${userInstruction || '请自由发挥'}`;
    if (modificationNote) {
      user += `\n\n【上一轮生成结果】\n${(prevOutput || '').slice(0, 2000)}\n\n【用户修改意见】${modificationNote}\n请根据修改意见调整优化。`;
    }
    return [
      { role: 'system', content: system },
      { role: 'user', content: user },
    ];
  }, [bible, book, selectedPacks]);

  // 流式生成（支持多维度串行）
  const streamGenerate = useCallback(async (dims: string[], userInstruction: string, modificationNote: string) => {
    if (dims.length === 0) {
      setError('请至少选择一个维度');
      return;
    }
    setError('');
    setPhase('streaming');
    stoppedRef.current = false;
    abortRef.current = new AbortController();
    lastGenRef.current = { instruction: userInstruction, modification: modificationNote };
    const newOutputs: Record<string, string> = {};
    setOutputs({});
    setEditedOutputs({});

    for (const dim of dims) {
      if (abortRef.current?.signal.aborted) break;
      setCurrentDim(dim);
      newOutputs[dim] = '';
      const prev = modificationNote ? (outputs[dim] || '') : undefined;
      const messages = buildMessages(dim, userInstruction, modificationNote, prev);
      try {
        const response = await api.aiChatStream(messages, abortRef.current?.signal);
        if (abortRef.current?.signal.aborted) break;
        if (!response.ok || !response.body) {
          throw new Error(`请求失败 (HTTP ${response.status})`);
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
          if (abortRef.current?.signal.aborted) {
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
                const delta = parsed.choices?.[0]?.delta?.content || '';
                if (delta) {
                  newOutputs[dim] += delta;
                  setOutputs({ ...newOutputs });
                }
              } catch { /* ignore parse error */ }
            }
          }
        }
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
  }, [buildMessages, outputs]);

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

  // 确定：填入对应维度
  async function handleConfirm() {
    setApplying(true);
    setError('');
    try {
      const finalOutputs = { ...outputs, ...editedOutputs }; // 编辑过的优先
      const results = selectedDims
        .map(d => ({ field: d, content: (finalOutputs[d] || '').trim() }))
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
          {/* 维度选择（全局模式 + 输入阶段） */}
          {isGlobal && phase === 'input' && (
            <div style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>选择要创作的维度（可多选，将串行生成）：</div>
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

              {/* 协同技能包（可折叠多选） */}
              {skillPacks.length > 0 && (
                <div className="skill-pack-collapsible" style={{ marginTop: 12 }}>
                  <button
                    type="button"
                    className="skill-pack-toggle"
                    onClick={() => setSkillExpanded(v => !v)}
                  >
                    <span className="skill-pack-toggle-icon">{skillExpanded ? '▼' : '▶'}</span>
                    <span>📦 协同技能包</span>
                    {selectedPacks.length > 0 && <span className="skill-pack-toggle-badge">{selectedPacks.length}</span>}
                    <span className="skill-pack-toggle-hint">{skillExpanded ? '收起' : '展开'}</span>
                  </button>
                  {skillExpanded && (
                    <>
                      <div className="skill-pack-checkbox-list">
                        {skillPacks.map(p => (
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
                return (
                  <div key={dim} style={{ marginBottom: 18, padding: 12, background: 'var(--bg-tertiary)', borderRadius: 8, border: `1px solid ${isCurrent ? 'var(--accent)' : 'var(--border-color)'}` }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                      <div style={{ fontSize: 13, fontWeight: 700 }}>
                        {dimMeta?.icon || '📝'} {DIM_LABEL[dim] || dim}
                        {isCurrent && <span style={{ color: 'var(--accent)', marginLeft: 6, fontSize: 11 }}>⏳ 生成中…</span>}
                      </div>
                      {phase === 'done' && content && (
                        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{content.length} 字</span>
                      )}
                    </div>
                    {phase === 'done' ? (
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
                    )}
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
              onClick={handleRegenerate}
              disabled={!modification.trim()}
              title={modification.trim() ? '带上修改意见重新生成' : '请先填写修改意见'}
              style={!modification.trim() ? { opacity: 0.5 } : {}}
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
