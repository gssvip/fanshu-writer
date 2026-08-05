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
