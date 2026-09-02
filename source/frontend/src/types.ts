export interface Book {
  id: string; user_id: string; title: string; author: string; genre: string;
  book_type: string; synopsis: string; cover_path: string;
  template_id: string; word_count: number; chapter_count: number;
  status: string; target_words: number;
  total_volumes: number;       // 总卷数（用户自定义，不设上限）
  novel_styles: string[];      // 风格流派（最多3种叠加）
  // 【三类无污染】三类技能包分别存储，各阶段只读对应类别
  master_skill_ids: string[];  // 构思类技能包（大纲/规划/设定阶段）
  style_skill_ids: string[];   // 文风类技能包（正文生成阶段，通常选1个）
  review_skill_ids: string[];  // 审查类技能包（去AI味/一致性检查阶段）
  created_at: string; updated_at: string;
  metadata: Record<string, any>;
}

export interface Chapter {
  id: string; book_id: string; title: string;
  content?: string; order_index: number; word_count: number;
  status: string; is_volume: boolean; parent_id: string;
  created_at: string; updated_at: string; notes: string;
}

export interface Character {
  id: string; book_id: string; name: string; role: string;
  description: string; appearance: string; personality: string;
  background: string; relationships: Relationship[];
  created_at: string; updated_at: string;
}

export interface Relationship {
  target_id: string; target_name: string; relation: string;
}

export interface Outline {
  id: string; book_id: string; title: string; content: string;
  order_index: number; level: number; parent_id: string;
  children?: Outline[];
  created_at: string; updated_at: string;
}

export interface Template {
  id: string; name: string; description: string;
  genre: string; book_type: string;
  structure: TemplateChapter[];
  prompts: Record<string, string>;
  is_builtin: boolean; created_at: string;
}

export interface TemplateChapter {
  title: string; is_volume: boolean; parent_id: string; order_index: number;
}

export interface DailyStat {
  id: string; book_id: string; date: string;
  words_written: number; time_spent_minutes: number; chapters_completed: number;
}

export interface AIConfig {
  id: string; name: string; is_active: boolean;
  provider: string; model: string;
  recognition_model: string;
  api_key: string; base_url: string;
  temperature: number; max_tokens: number; has_key: boolean;
}

export interface AIConfigList {
  configs: AIConfig[];
  max: number;
}

export interface AISession {
  id: string; book_id: string; scope: string; scope_id: string;
  title: string; messages: AIMessage[];
  created_at: string; updated_at: string;
}

export type AIMessageRole = 'user' | 'assistant' | 'system';

export interface AIMessage {
  role: AIMessageRole;
  content: string;
  cards?: ActionCard[];  // AI 回复中携带的落地卡片
  reasoning?: string;    // 思考过程（可选）：独立展示，不参与复制/采纳
  roundtable?: {         // 圆桌会议讨论数据
    speech: Array<{ speaker: string; name: string; content: string }>;
    currentSpeaker: string;
    status: 'open' | 'done';
    summary?: string;
  };
}

// 聊天中的 Action Card（讨论即落地）
export interface ActionCard {
  id: string;
  type: string;        // SAVE_CHARACTER / SAVE_FORESHADOW / ...
  title: string;
  content: string;
  target: string;      // 目标维度标签
  status?: 'pending' | 'adopted' | 'edited' | 'ignored';
  subtitle?: string;   // 副标题（榜单风向来源说明）
  rankSourceLabel?: string; // 榜单风向标签（如 "🍅番茄新书榜·男频都市"）
}

// 创作进度地图
export interface ProgressDim {
  field: string; label: string; status: 'empty' | 'sketch' | 'partial' | 'solid';
  pct: number; hint: string;
}
export interface ProgressMap {
  dims: ProgressDim[];
  overall: number; filled: number; total: number;
  next_step: { field: string; label: string; hint: string } | null;
}

export interface StatsData {
  daily: DailyStat[];
  chapters: { title: string; word_count: number; date: string }[];
}

export interface StageItem {
  key: string; label: string; icon: string; desc: string;
  content: string; stage_id: string;
  is_parent?: boolean; parent?: string;
}

export interface PromptT {
  id: string; name: string; agent_id: string;
  book_type: string; genre: string;
  content: string; is_builtin: boolean;
  description: string; created_at: string;
}

export interface BookBible {
  id: string; book_id: string;
  worldbuilding: string; character_profiles: string;
  timeline: string; foreshadowing: string;
  style_guide: string; key_rules: string;
  locations: string; concept: string; plot_design: string;
  generated_summary: string; last_synced_at: string | null;
  relation_graph?: string;
  inventory?: string;
  character_volumes?: string;
  dynamic_volumes?: string;
  foreshadowing_volumes?: string;
  locations_volumes?: string;
  anti_forget_reports?: string; // 防遗忘检查报告 JSON 数组
}

// M4: 系统优化报告
export interface OptimizationSuggestion {
  bucket_key: string;           // 索引：category::dim_key
  category: string;
  category_cn?: string;
  dim_key?: string;
  count: number;
  severity?: 'high' | 'medium' | 'low';
  pattern?: string;            // 新字段
  problem_pattern: string;     // 兼容旧
  affected_dims: string[];
  suggestion?: string;         // 建议说明
  proposed_patch: string;      // 要追加到 prompt 的补丁文本（可编辑后采纳）
  sample_snippet?: string;     // 兼容旧
  examples?: Array<{ summary: string; snippet: string; chapter_num?: number; ts?: string }>;
}
export interface AppliedPatchItem {
  id: string;
  category: string;
  category_cn?: string;
  patch_text: string;
  applied_at?: string;
}
export interface OptimizationReport {
  ready: boolean;
  failure_count: number;
  ignored_bucket_count?: number;
  reason?: string;
  suggestions: OptimizationSuggestion[];
  how_to_use?: { step1?: string; step2?: string; step3?: string; step4?: string };
  applied_patches?: AppliedPatchItem[];
  applied_patch_count?: number;
  active_patch_preview?: string;
}

// M4: 动作影响预览
export interface ImpactConsistencyIssue {
  severity: 'critical' | 'warning' | 'note';
  rule: string;
  source_quote: string;
  target_quote: string;
  suggestion: string;
}
export interface ImpactTaskResult {
  status?: 'ok' | 'warn' | 'conflict' | string;
  critical?: number;
  warning?: number;
  note?: number;
  target_label?: string;
  issues?: ImpactConsistencyIssue[];
  preview_mode?: boolean;
  note_msg?: string;
  preview_error?: string;
  [k: string]: any;
}
export interface ImpactTask {
  id: string;
  action: string;
  op?: string;
  target_dim: string;
  target_label?: string;
  target_chapter?: number;
  args?: Record<string, any>;
  depends_on?: string[];
  reason: string;
  auto: boolean;
  status?: 'pending' | 'running' | 'done' | 'failed' | 'skipped';
  result?: ImpactTaskResult;
}
export interface ImpactPreview {
  action: string;
  summary: string;
  tasks: ImpactTask[];
  warnings: string[];
}

export interface SkillPack {
  id: string; name: string; description: string;
  genre: string; book_type: string;
  stage_keys: string[]; workflow: WorkflowStep[];
  prompts: Record<string, string>;
  is_builtin: boolean; icon: string;
  github_source?: string; github_synced_at?: string | null;
  // 【三类无污染】技能包分类：master=构思类 / style=文风类 / review=审查类
  category: 'master' | 'style' | 'review';
  genre_target?: string;  // 文风类专属题材标签（fantasy/urban_fantasy/mystery/history/scifi/romance...）
  priority?: number;      // 同类多包时的注入优先级（数字小的先注入），默认100
  created_at: string;
}

export interface WorkflowStep {
  step: number; name: string; desc: string;
  prompt_key: string;
  temperature?: number;   // 场景级温度：该提示词调用时使用的采样温度（0~2），缺省用全局配置
}

// AI 调用账本
export interface AIUsageLogItem {
  id: string; book_id?: string | null; chapter_id?: string | null;
  scene: string; task_type: string; model: string;
  prompt_chars: number; output_chars: number;
  success: boolean; error_message: string; duration_ms: number;
  created_at: string;
}
export interface AIUsageSceneStat { scene: string; count: number; output_chars: number }
export interface AIUsageModelStat { model: string; count: number }
export interface AIUsageStats {
  range?: string;            // today / 7d / 30d（新 range 模式字段；旧 days 模式可能缺失）
  days: number; total_calls: number; success_rate: number;
  success: number; failed: number;
  total_output_chars: number; total_prompt_chars: number; total_duration_ms: number; avg_output_chars: number;
  by_scene: AIUsageSceneStat[]; by_model: AIUsageModelStat[];
}

// 榜单风向
export interface RankingExample { title: string; tag: string; point: string }
export interface RankBook {
  rankNo: number; title: string; author: string; bookId: string;
  bookUrl: string; coverUrl: string; metric: string; metricValue: number;
  status: string; category: string; words: string;
  lastChapter: string; updateTime: string; category2: string;
  point?: string;
}
export interface RankingData {
  platform: string; icon: string; note: string;
  trend_marker: { label: string; tone: string };
  hot_tags: string[]; rising_keywords: string[]; hot_genres: string[];
  examples: RankingExample[];
  advice: string;
  books: RankBook[];
  fetch_ok?: boolean;
  source?: string;
  fetch_error?: string;
}

// ========== 榜单风向（移植自 easy-writing: NovelRank）==========
export interface NRPlatform { code: string; name: string; baseUrl?: string; remark?: string; }

export interface NRRankType { value: string; label: string; }

export interface NRCategory {
  id: string;
  code: string;
  name: string;
  scope?: 'all' | 'category';
  sourceId?: number;
  categoryId?: number;
  gender?: 'male' | 'female';
  parentCode?: string;   // 主题子类所属父分类的 code
}

export interface NRFilters {
  platform: string;
  rankTypes: NRRankType[];
  genders: ('male' | 'female')[];
  categories: NRCategory[];
  subcategories?: NRCategory[];   // 主题子类（起点等二级分类）
}

export interface NRItem {
  rankNo: number;
  rankChange: number;
  bookTitle: string;
  bookId?: string | null;
  bookUrl: string;
  authorName?: string | null;
  coverUrl?: string | null;
  intro?: string | null;
  statusText?: string | null;
  readingCount: number;
  readingText?: string | null;
  metricName?: string | null;
  metricValue?: number | null;
  metricText?: string | null;
  lastChapterTitle?: string | null;
  lastChapterUrl?: string | null;
  lastUpdateTimeText?: string | null;
  categoryName?: string | null;
  categorySubName?: string | null;
}

export interface NRListResult {
  sourceId: number | null;
  siteCode?: string;
  rankType?: string;
  rankTitle?: string;
  pageTitle?: string;
  cutoffText?: string;
  fetchAt?: number;
  sourceKind?: 'live' | 'curated';
  fetchError?: string;
  page: number;
  pageSize: number;
  total: number;
  itemCount: number;
  items: NRItem[];
}

export interface ReviewResult {
  scores: Record<string, number>;
  total_score: number;
  grade: string;
  strengths: string[];
  weaknesses: string[];
  specific_suggestions: string[];
  platform_fit: string;
}

export interface AnalysisResult {
  style_analysis: string;
  structure_analysis: string;
  rhythm_analysis: string;
  character_design_analysis: string;
  hook_techniques: string[];
  golden_lines: string[];
  genre_tags: string[];
  target_platform: string;
  learnable_points: string[];
  // 竞品拆书模式专属（focus=competitor）
  market_position?: string;
  strengths?: string[];
  weaknesses?: string[];
  copy_plan?: string;
}

export interface BrainstormSuggestion {
  title: string;
  description: string;
}

export interface BrainstormResult {
  concept_analysis: string;
  suggestions: {
    concept?: BrainstormSuggestion[];
    settings?: BrainstormSuggestion[];
    outline?: BrainstormSuggestion[];
    worldview: BrainstormSuggestion[];
    character: BrainstormSuggestion[];
    plot: BrainstormSuggestion[];
    chapters?: BrainstormSuggestion[];
    locations: BrainstormSuggestion[];
    foreshadowing: BrainstormSuggestion[];
  };
}

export interface DynamicReport {
  id: string;
  book_id: string;
  title: string;
  content: string;
  chapter_start: number;
  chapter_end: number;
  auto_generated: boolean;
  created_at: string;
  updated_at: string;
}
