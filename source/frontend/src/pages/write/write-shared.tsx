/** write-shared —— 自 WritePage.tsx 拆出（P2b 巨石拆分，逻辑零改动）。 */
import type { SkillPack } from '../../types';

/**
 * 全局采纳落地内容 → 行与行之间「不留空一行」（用户明确要求）
 * 根因：用户数据 / AI生成结果 默认用 Markdown 段间空行（\n\n）分隔段落；
 *       在 pre-wrap / <textarea> / novel-paragraph 下 就产生"空一整条32px横格"。
 * 统一把「连续 2 个及以上换行」压缩为 1 个换行；同时归一化 CRLF → LF。
 * 覆盖：章节查看/编辑、设定查看/编辑、大纲/世界观查看/编辑、DynamicReport、GlobalLocations、所有 AI 创作结果写入、所有 save 写入后端。
 * 声明为顶层纯函数 → WritePage 内部所有子组件（BibleEditPanel / OutlineCombinedPanel / SettingsCombinedPanel / DynamicReportPanel / GlobalLocationsPanel）都可直接调用，无需传 props。
 */
export function collapseNewlines(s: unknown): string {
  if (typeof s !== 'string') return s == null ? '' : String(s);
  return s.replace(/\r\n?/g, '\n').replace(/\n{2,}/g, '\n');
}


// 两行 Tab 布局：上下各 5 个维度
export const TAB_ROW_1 = [
  { key: 'concept', label: '构思', icon: '💡', field: 'concept', placeholder: '一句话描述你的故事核心创意...' },
  { key: 'settings', label: '设定', icon: '⚙️', field: 'key_rules', placeholder: '核心规则、能力限制、世界观禁忌...' },
  { key: 'outline', label: '大纲', icon: '📋', field: 'plot_design', placeholder: '主线冲突、卷纲拆解、章节规划...' },
  { key: 'plot', label: '剧情', icon: '📖', field: 'timeline', placeholder: '按时间顺序列出关键事件...' },
  { key: 'characters', label: '人物及关系', icon: '👤', field: 'character_profiles', placeholder: '主角、配角的姓名、身份、性格、动机、人物关系...' },
];


export const TAB_ROW_2 = [
  { key: 'chapters', label: '章节', icon: '📚', field: '', placeholder: '' },
  { key: 'inventory', label: '物资库', icon: '🎒', field: 'inventory', placeholder: '按势力/角色记录物品、功法、法宝、境界...' },
  { key: 'dynamicMemory', label: '动态文件', icon: '🗂️', field: '', placeholder: '' },
  { key: 'foreshadowing', label: '伏笔', icon: '🔮', field: 'foreshadowing', placeholder: '伏笔内容、埋设时机、回收方式...' },
  { key: 'map', label: '地图', icon: '🗺️', field: 'locations', placeholder: '' },
];


export const ALL_TABS = [...TAB_ROW_1, ...TAB_ROW_2];


export const FIELD_AI_PROMPTS: Record<string, string> = {
  concept: '将以下一句话构思扩展为完整的创意方案，包含核心卖点、目标读者、主线冲突、独特亮点。',
  key_rules: '根据以下构思，生成核心设定规则。包括：世界观必须遵循的规则、人物能力限制、禁忌事项。每条规则单独列出。',
  plot_design: '根据以下构思，生成故事大纲。包括：核心主线、分卷规划（每卷目标）、关键转折点、高潮设计、结局走向。',
  worldbuilding: '根据以下构思，生成详细的世界观设定。包括：世界背景、力量体系/科技水平、社会结构、地理概况、历史脉络。',
  character_profiles: '根据以下构思，生成主要人物档案。包括：主角和3-5个重要配角的姓名、身份、性格特征、背景故事、核心动机、人物关系。',
  timeline: '根据以下构思，生成剧情时间线。按时间顺序列出关键事件，每个事件标注涉及的人物和地点。',
  foreshadowing: '根据以下构思，设计3-5条伏笔线索。每条包括：伏笔内容、埋设时机（大概章节）、预期回收方式、对剧情的影响。',
  locations: '根据以下构思，设计三级地点体系。第一级为大区域（如：东大陆、西荒漠），第二级为城市/门派，第三级为具体场景。用JSON格式输出。',
};


export const DIMENSION_LABELS: Record<string, string> = {
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
export const DIMENSION_SKILL_KEYS: Record<string, string[]> = {
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
export const CHAPTER_SKILL_KEYS: Record<string, string[]> = {
  write: ['write_chapter', 'draft_writing', 'context_pack', 'tomato_chapter', 'fantasy_draft', 'long_write', 'short_write', 'first_draft', 'writer', 'daily_adventure', 'chapter_structure'],
  continue: ['write_chapter', 'draft_writing', 'tomato_chapter', 'fantasy_draft', 'long_write', 'writer', 'first_draft'],
  polish: ['polish', 'de_ai_check', 'minimal_rewrite', 'humanize', 'final_check', 'tomato_deai', 'forbidden_words', 'rhythm_check', 'deslop', 'draft_rewrite', 'fidelity_check', 'final_polish', 'anti_ai_audit', 'reviser', 'style_analyzer', 'protect_rewrite', 'fidelity_read', 'residual_read'],
};


// 实时清洗 LLM 流式输出中的内部标签：后端要求正文后再输出标签，但流式显示时需前端实时剥离
export function stripInternalTags(content: string): string {
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
export function extractSkillPrompt(packs: SkillPack[], keys: string[]): string {
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
export interface MapRegion {
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
export function SkillPackGroupedList({
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


/* ===== 剧情面板（按卷） ===== */
// 防御性文本化：AI 生成的 timeline JSON 中某些字段可能为对象/数组而非字符串，
// 直接渲染会触发 React error #31（"Objects are not valid as a React child"）。
// 此函数将任意值安全转为可渲染的字符串。
export function safeText(v: any): string {
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
