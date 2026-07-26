import type { Book, Chapter, Character, Outline, Template, AIConfig, AISession, StatsData, StageItem, PromptT, BookBible, SkillPack, ReviewResult, AnalysisResult, BrainstormResult, DynamicReport } from './types';

// 后端 API 默认地址（内置，开箱即用）
// 其他用户无需手动配置即可使用。如需切换到自部署的后端，可在「我的 → 服务器」覆盖。
// 如需更改默认地址，修改此处并重新构建即可。
const DEFAULT_API_BASE = 'https://fanshu-writer-backend.onrender.com/api';

// 后端 API 地址解析优先级：
// 1. localStorage 中用户手动配置的地址（适用于 GitHub Pages 等静态托管场景）
// 2. Vite 环境变量 VITE_API_URL（构建时注入，优先级高于内置默认值）
// 3. 内置默认地址 DEFAULT_API_BASE（开箱即用）
// 4. /api（仅适用于前后端同域部署或开发环境）
export function getApiBaseUrl(): string {
  const saved = localStorage.getItem('fanshu-api-base-url');
  if (saved && saved.trim()) {
    let url = saved.trim().replace(/\/+$/, '');
    // 自动补全 /api 后缀：用户只需填后端根地址
    if (!url.endsWith('/api')) {
      url = url + '/api';
    }
    return url;
  }
  const env = (import.meta as any).env?.VITE_API_URL;
  if (env && env.trim()) return env.trim().replace(/\/+$/, '');
  // 同域部署（开发环境或后端托管前端时）走相对路径 /api
  // 否则使用内置的默认后端地址
  const host = window.location.hostname;
  const isStaticHost = host.endsWith('.github.io') || host.endsWith('.vercel.app') || host.endsWith('.netlify.app');
  if (isStaticHost) {
    return DEFAULT_API_BASE;
  }
  return '/api';
}

export function setApiBaseUrl(url: string) {
  if (url && url.trim()) {
    localStorage.setItem('fanshu-api-base-url', url.trim().replace(/\/+$/, ''));
  } else {
    localStorage.removeItem('fanshu-api-base-url');
  }
}

// 检测当前是否在静态托管环境（如 GitHub Pages）且未配置后端地址
// 注意：内置默认地址后，静态托管环境不再算 misconfigured
export function isApiMisconfigured(): boolean {
  // 已有内置默认地址，永远不算 misconfigured
  return false;
}

function getToken(): string | null {
  return localStorage.getItem('fanshu-token');
}

// 后端是否正在预热中（避免重复预热）
let warmingUp = false;

/**
 * 预热后端：发起一个轻量 GET 请求触发 Render 实例唤醒。
 * 静默执行，不抛错，不阻塞 UI。
 * 适用于页面加载时或长时间空闲后。
 */
export function warmUpBackend(): void {
  if (warmingUp) return;
  warmingUp = true;
  // 用 fetch 而非 request()，避免被重试逻辑影响
  fetch(`${getApiBaseUrl()}/templates`, { method: 'GET' })
    .catch(() => { /* 静默失败 */ })
    .finally(() => { warmingUp = false; });
}

/**
 * 带自动重试的 fetch：应对 Render 免费版冷启动超时。
 * - 第 1 次失败后等 3 秒重试
 * - 第 2 次失败后等 8 秒重试（共等待约 11 秒，覆盖大部分冷启动场景）
 * - 第 3 次仍失败则抛错
 */
async function fetchWithRetry(url: string, options: RequestInit, maxRetries = 2): Promise<Response> {
  const delays = [3000, 8000]; // 重试间隔（毫秒）
  let lastError: any;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 60000);
      const res = await fetch(url, { ...options, signal: controller.signal });
      clearTimeout(timeoutId);
      // 5xx 错误重试（可能是冷启动中），4xx 直接返回
      if (res.status >= 500 && attempt < maxRetries) {
        await new Promise(r => setTimeout(r, delays[attempt]));
        continue;
      }
      return res;
    } catch (e: any) {
      lastError = e;
      // 网络错误/超时重试（Render 冷启动典型表现）
      if (attempt < maxRetries) {
        await new Promise(r => setTimeout(r, delays[attempt]));
        continue;
      }
    }
  }
  throw lastError;
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json', ...(options?.headers as Record<string, string> || {}) };
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;

  try {
    const res = await fetchWithRetry(`${getApiBaseUrl()}${url}`, { ...options, headers });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: `请求失败 (HTTP ${res.status})` }));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    if (res.headers.get('content-type')?.includes('application/json')) {
      return res.json();
    }
    return {} as T;
  } catch (e: any) {
    if (e.name === 'AbortError') {
      throw new Error('请求超时（服务器可能正在冷启动，已自动重试仍失败，请稍后再试）');
    }
    if (e.message === 'Failed to fetch' || e.message?.includes('NetworkError')) {
      throw new Error('无法连接到服务器，可能正在冷启动中。请稍等几秒后重试，或检查「我的 → 服务器」配置');
    }
    throw e;
  }
}

export interface User { id: string; username: string; email: string; created_at: string; }

export const api = {
  // Auth
  register: (username: string, password: string, email?: string) =>
    request<{ user: User; token: string }>('/auth/register', { method: 'POST', body: JSON.stringify({ username, password, email }) }),
  login: (username: string, password: string) =>
    request<{ user: User; token: string }>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  getMe: () => request<User>('/auth/me'),
  logout: () => request<{ success: boolean }>('/auth/logout', { method: 'POST' }),
  // 修改密码（需登录）
  changePassword: (oldPassword: string, newPassword: string) =>
    request<{ success: boolean }>('/auth/change-password', { method: 'POST', body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }) }),
  // 找回密码：发送重置邮件（传入当前站点地址，用于构造重置链接）
  forgotPassword: (email: string) =>
    request<{ success: boolean; message?: string; reset_link?: string; dev_mode?: boolean }>('/auth/forgot-password', {
      method: 'POST',
      body: JSON.stringify({ email, site_url: window.location.origin }),
    }),
  // 校验重置令牌
  verifyResetToken: (token: string) =>
    request<{ valid: boolean }>('/auth/verify-reset-token', { method: 'POST', body: JSON.stringify({ token }) }),
  // 重置密码
  resetPassword: (token: string, newPassword: string) =>
    request<{ success: boolean; message?: string }>('/auth/reset-password', { method: 'POST', body: JSON.stringify({ token, new_password: newPassword }) }),

  // Books
  listBooks: () => request<Book[]>('/books'),
  getBook: (id: string) => request<Book>(`/books/${id}`),
  createBook: (data: Partial<Book>) => request<Book>('/books', { method: 'POST', body: JSON.stringify(data) }),
  updateBook: (id: string, data: Partial<Book>) => request<Book>(`/books/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteBook: (id: string) => request<{ success: boolean }>(`/books/${id}`, { method: 'DELETE' }),

  // Chapters
  listChapters: (bookId: string) => request<Chapter[]>(`/books/${bookId}/chapters`),
  getChapter: (bookId: string, chId: string) => request<Chapter>(`/books/${bookId}/chapters/${chId}`),
  createChapter: (bookId: string, data: Partial<Chapter>) => request<Chapter>(`/books/${bookId}/chapters`, { method: 'POST', body: JSON.stringify(data) }),
  updateChapter: (bookId: string, chId: string, data: Partial<Chapter>) => request<Chapter>(`/books/${bookId}/chapters/${chId}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteChapter: (bookId: string, chId: string) => request<{ success: boolean }>(`/books/${bookId}/chapters/${chId}`, { method: 'DELETE' }),
  reorderChapters: (bookId: string, order: { id: string; order_index: number }[]) =>
    request<{ success: boolean }>(`/books/${bookId}/chapters/reorder`, { method: 'POST', body: JSON.stringify({ order }) }),

  // Characters
  listCharacters: (bookId: string) => request<Character[]>(`/books/${bookId}/characters`),
  createCharacter: (bookId: string, data: Partial<Character>) => request<Character>(`/books/${bookId}/characters`, { method: 'POST', body: JSON.stringify(data) }),
  updateCharacter: (bookId: string, charId: string, data: Partial<Character>) => request<Character>(`/books/${bookId}/characters/${charId}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteCharacter: (bookId: string, charId: string) => request<{ success: boolean }>(`/books/${bookId}/characters/${charId}`, { method: 'DELETE' }),

  // Outlines
  listOutlines: (bookId: string) => request<{ flat: Outline[]; tree: Outline[] }>(`/books/${bookId}/outlines`),
  createOutline: (bookId: string, data: Partial<Outline>) => request<{ item: Outline; tree: Outline[] }>(`/books/${bookId}/outlines`, { method: 'POST', body: JSON.stringify(data) }),
  updateOutline: (bookId: string, outlineId: string, data: Partial<Outline>) => request<{ item: Outline; tree: Outline[] }>(`/books/${bookId}/outlines/${outlineId}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteOutline: (bookId: string, outlineId: string) => request<{ tree: Outline[] }>(`/books/${bookId}/outlines/${outlineId}`, { method: 'DELETE' }),

  // Templates
  listTemplates: () => request<Template[]>('/templates'),
  createTemplate: (data: Partial<Template>) => request<Template>('/templates', { method: 'POST', body: JSON.stringify(data) }),

  // AI
  getAIConfig: () => request<AIConfig>('/ai/config'),
  updateAIConfig: (data: Partial<AIConfig>) => request<AIConfig>('/ai/config', { method: 'PUT', body: JSON.stringify(data) }),
  fetchAIModels: (baseUrl: string, apiKey: string) =>
    request<{ models: { id: string; owned_by: string }[] }>('/ai/models', { method: 'POST', body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }) }),
  testAIConnection: (baseUrl: string, apiKey: string, model: string) =>
    request<{ success: boolean; reply: string; model: string; usage?: any }>('/ai/test', { method: 'POST', body: JSON.stringify({ base_url: baseUrl, api_key: apiKey, model }) }),
  aiChat: (messages: { role: string; content: string }[]) => request<{ content: string; usage?: any }>('/ai/chat', { method: 'POST', body: JSON.stringify({ messages }) }),
  aiChatStream: (messages: { role: string; content: string }[]) => {
    const cfg = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ messages }) };
    return fetch(`${getApiBaseUrl()}/ai/chat/stream`, cfg);
  },

  // AI Sessions
  listAISessions: (bookId?: string, scope?: string) => {
    const params = new URLSearchParams();
    if (bookId) params.set('book_id', bookId);
    if (scope) params.set('scope', scope);
    return request<AISession[]>(`/ai/sessions?${params}`);
  },
  createAISession: (data: Partial<AISession>) => request<AISession>('/ai/sessions', { method: 'POST', body: JSON.stringify(data) }),
  updateAISession: (id: string, data: Partial<AISession>) => request<AISession>(`/ai/sessions/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteAISession: (id: string) => request<{ success: boolean }>(`/ai/sessions/${id}`, { method: 'DELETE' }),

  // Stats
  getBookStats: (bookId: string) => request<StatsData>(`/books/${bookId}/stats`),

  // Export
  getExportUrl: (bookId: string, format: string) => {
    const token = getToken();
    return `${getApiBaseUrl()}/books/${bookId}/export?format=${format}${token ? `&token=${token}` : ''}`;
  },
  getExportZipUrl: (bookId: string) => {
    const token = getToken();
    return `${getApiBaseUrl()}/books/${bookId}/export-zip${token ? `?token=${token}` : ''}`;
  },
  getExportFullUrl: (bookId: string) => {
    const token = getToken();
    return `${getApiBaseUrl()}/books/${bookId}/export-full${token ? `?token=${token}` : ''}`;
  },

  // Cover
  uploadCover: async (bookId: string, file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    const res = await fetch(`${getApiBaseUrl()}/books/${bookId}/cover`, { method: 'POST', body: formData });
    return res.json();
  },

  // Count words
  countWords: (text: string) => request<{ count: number }>('/utils/count-words', { method: 'POST', body: JSON.stringify({ text }) }),

  // Import ZIP
  importZip: async (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    const res = await fetch(`${getApiBaseUrl()}/books/import-zip`, { method: 'POST', body: formData });
    return res.json();
  },

  // Import multiple text files as a new book (txt/md/docx/zip)
  importFiles: async (files: File[], opts?: { title?: string; book_type?: string; genre?: string }) => {
    const formData = new FormData();
    for (const f of files) formData.append('files', f);
    if (opts?.title) formData.append('title', opts.title);
    if (opts?.book_type) formData.append('book_type', opts.book_type);
    if (opts?.genre) formData.append('genre', opts.genre);
    const token = getToken();
    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(`${getApiBaseUrl()}/books/import-files`, { method: 'POST', body: formData, headers });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: 'Import failed' }));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    return res.json() as Promise<Book>;
  },

  // 追加导入章节到已有作品（txt/md/docx/zip，每个文件可含多章）
  importChapters: async (bookId: string, files: File[]) => {
    const formData = new FormData();
    for (const f of files) formData.append('files', f);
    const token = getToken();
    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(`${getApiBaseUrl()}/books/${bookId}/import-chapters`, { method: 'POST', body: formData, headers });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: '导入失败' }));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    return res.json() as Promise<{ success: boolean; added: number; total: number }>;
  },

  // Stages
  listStages: (bookId: string) => request<StageItem[]>(`/books/${bookId}/stages`),
  getStage: (bookId: string, stageKey: string) => request<{ id?: string; book_id: string; stage_key: string; content: string }>(`/books/${bookId}/stages/${stageKey}`),
  saveStage: (bookId: string, stageKey: string, content: string) => request<{ id: string; content: string }>(`/books/${bookId}/stages/${stageKey}`, { method: 'PUT', body: JSON.stringify({ content }) }),

  // Prompts
  listPrompts: (bookType?: string, agentId?: string) => {
    const params = new URLSearchParams();
    if (bookType) params.set('book_type', bookType);
    if (agentId) params.set('agent_id', agentId);
    return request<PromptT[]>(`/prompts?${params}`);
  },
  getPrompt: (id: string) => request<PromptT>(`/prompts/${id}`),
  createPrompt: (data: Partial<PromptT>) => request<PromptT>('/prompts', { method: 'POST', body: JSON.stringify(data) }),
  updatePrompt: (id: string, data: Partial<PromptT>) => request<PromptT>(`/prompts/${id}`, { method: 'PUT', body: JSON.stringify(data) }),

  // BookBible (项目宪法)
  getBible: (bookId: string) => request<BookBible>(`/books/${bookId}/bible`),
  updateBible: (bookId: string, data: Partial<BookBible>) => request<BookBible>(`/books/${bookId}/bible`, { method: 'PUT', body: JSON.stringify(data) }),
  syncBible: (bookId: string) => request<BookBible>(`/books/${bookId}/bible/sync`, { method: 'POST' }),

  // AI Review
  reviewBook: (bookId: string, scope?: string, content?: string) =>
    request<ReviewResult>(`/books/${bookId}/review`, { method: 'POST', body: JSON.stringify({ scope, content }) }),

  // AI Continue
  aiContinue: (bookId: string, instruction: string) =>
    request<{ content: string }>(`/books/${bookId}/ai-continue`, { method: 'POST', body: JSON.stringify({ instruction }) }),

  // AI Analyze Book
  analyzeBook: (content: string) =>
    request<AnalysisResult>('/analyze-book', { method: 'POST', body: JSON.stringify({ content }) }),

  // Skill Packs
  listSkillPacks: (genre?: string, bookType?: string) => {
    const params = new URLSearchParams();
    if (genre) params.set('genre', genre);
    if (bookType) params.set('book_type', bookType);
    return request<SkillPack[]>(`/skill-packs?${params}`);
  },
  getSkillPack: (id: string) => request<SkillPack>(`/skill-packs/${id}`),
  createSkillPack: (data: Partial<SkillPack>) => request<SkillPack>('/skill-packs', { method: 'POST', body: JSON.stringify(data) }),
  updateSkillPack: (id: string, data: Partial<SkillPack>) => request<SkillPack>(`/skill-packs/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteSkillPack: (id: string) => request<{ success: boolean }>(`/skill-packs/${id}`, { method: 'DELETE' }),
  cloneSkillPack: (id: string, name?: string) =>
    request<SkillPack>(`/skill-packs/${id}/clone`, { method: 'POST', body: JSON.stringify({ name }) }),
  publishSkillPack: (id: string) =>
    request<SkillPack>(`/skill-packs/${id}/publish`, { method: 'POST' }),
  // 从 GitHub 同步技能包（拉取最新 SKILL.md 更新提示词）
  syncSkillPackFromGitHub: (id: string) =>
    request<{ success: boolean; message: string; updated_count: number; errors?: string[]; synced_at: string }>(`/skill-packs/${id}/sync-github`, { method: 'POST' }),
  applySkillPack: (bookId: string, packId: string) =>
    request<{ success: boolean; pack: SkillPack }>(`/books/${bookId}/apply-skill-pack`, { method: 'POST', body: JSON.stringify({ pack_id: packId }) }),

  // AI Brainstorm (协同创作)
  brainstorm: (bookId: string, concept: string, dimension?: string) =>
    request<BrainstormResult>(`/books/${bookId}/brainstorm`, { method: 'POST', body: JSON.stringify({ concept, dimension: dimension || 'all' }) }),

  // AI Analyze Content (导入作品后自动识别)
  analyzeContent: (bookId: string) =>
    request<{ success: boolean; updated_fields: string[]; bible: BookBible }>(`/books/${bookId}/ai-analyze-content`, { method: 'POST' }),

  // AI Analyze Single Dimension (单维度AI识别)
  analyzeDimension: (bookId: string, dimension: string) =>
    request<{ success: boolean; dimension: string; field: string; value: string; bible: BookBible }>(`/books/${bookId}/ai-analyze-dimension`, {
      method: 'POST',
      body: JSON.stringify({ dimension }),
    }),

  // AI Analyze Character (单角色AI识别/全部角色识别)
  analyzeCharacter: (bookId: string, characterName?: string) =>
    request<{ success: boolean; character: any; characters: any[]; bible: BookBible }>(`/books/${bookId}/ai-analyze-character`, {
      method: 'POST',
      body: JSON.stringify({ character_name: characterName || '' }),
    }),

  // AI Analyze Plot Volume (按卷识别剧情)
  analyzePlotVolume: (bookId: string, volumeId: string, volumeTitle: string) =>
    request<{ success: boolean; volume_data: any; volumes: any[]; bible: BookBible }>(`/books/${bookId}/ai-analyze-plot-volume`, {
      method: 'POST',
      body: JSON.stringify({ volume_id: volumeId, volume_title: volumeTitle }),
    }),

  // Dynamic Memory (动态文件库 - 旧5文件JSON系统)
  getDynamicMemory: (bookId: string) =>
    request<{ id: string; book_id: string; narrative_engine: string; foreshadowing_tracker: string; character_ecosystem: string; ability_world: string; health_dashboard: string; updated_at: string }>(`/books/${bookId}/dynamic-memory`),
  updateDynamicMemoryFile: (bookId: string, fileKey: string, content: string) =>
    request<{ success: boolean; file_key: string; content: string }>(`/books/${bookId}/dynamic-memory/${fileKey}`, {
      method: 'PUT',
      body: JSON.stringify({ content }),
    }),
  initDynamicMemory: (bookId: string) =>
    request<{ success: boolean; data: any }>(`/books/${bookId}/dynamic-memory/init`, { method: 'POST' }),
  aiGenerateDynamicMemory: (bookId: string, fileKey: string) =>
    request<{ success: boolean; file_key: string; content: string; data: any }>(`/books/${bookId}/dynamic-memory/ai-generate`, {
      method: 'POST',
      body: JSON.stringify({ file_key: fileKey }),
    }),

  // Dynamic Reports (动态报告 - 防遗忘摘要系统)
  listDynamicReports: (bookId: string) =>
    request<DynamicReport[]>(`/books/${bookId}/dynamic-reports`),
  createDynamicReport: (bookId: string, data: { chapter_start: number; chapter_end: number; content?: string; title?: string }) =>
    request<DynamicReport>(`/books/${bookId}/dynamic-reports`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateDynamicReport: (bookId: string, reportId: string, data: Partial<DynamicReport>) =>
    request<DynamicReport>(`/books/${bookId}/dynamic-reports/${reportId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteDynamicReport: (bookId: string, reportId: string) =>
    request<{ success: boolean }>(`/books/${bookId}/dynamic-reports/${reportId}`, { method: 'DELETE' }),
  regenerateDynamicReport: (bookId: string, reportId: string) =>
    request<DynamicReport>(`/books/${bookId}/dynamic-reports/${reportId}/regenerate`, { method: 'POST' }),
  autoCheckDynamicReport: (bookId: string) =>
    request<{ success: boolean; message: string; report: DynamicReport | null }>(`/books/${bookId}/dynamic-reports/auto-check`, { method: 'POST' }),
  getDynamicReportContext: (bookId: string) =>
    request<{ reports: DynamicReport[]; context_text: string }>(`/books/${bookId}/dynamic-reports/context`),

  // AI Analyze from Reports (从动态文件报告提取维度信息，节省token)
  analyzeFromReports: (bookId: string, dimension: string) =>
    request<{ success: boolean; dimension: string; field: string; value: string; bible: BookBible; source: string }>(`/books/${bookId}/ai-analyze-from-reports`, {
      method: 'POST',
      body: JSON.stringify({ dimension }),
    }),

  // File Upload for Analysis
  uploadAnalyze: async (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    const token = getToken();
    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(`${getApiBaseUrl()}/upload-analyze`, { method: 'POST', body: formData, headers });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: 'Upload failed' }));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    return res.json() as Promise<{ filename: string; content: string; length: number }>;
  },

  // Export Analysis Result
  exportAnalysis: async (result: any) => {
    const res = await fetch(`${getApiBaseUrl()}/analyze/export`, { method: 'POST', body: JSON.stringify(result), headers: { 'Content-Type': 'application/json' } });
    if (!res.ok) throw new Error('Export failed');
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'analysis_result.json';
    a.click();
    URL.revokeObjectURL(url);
  },

  // Sync Analysis Result to Book Bible (for 仿写/同人文)
  syncAnalysisToBook: (bookId: string, analysis: AnalysisResult, mode: string) =>
    request<{ success: boolean; bible: BookBible; updated_fields: string[] }>(`/books/${bookId}/sync-analysis`, { method: 'POST', body: JSON.stringify({ analysis, mode }) }),
};
