import type { Book, Chapter, Character, Outline, Template, AIConfig, AISession, StatsData, StageItem, PromptT, BookBible, SkillPack, ReviewResult, AnalysisResult, BrainstormResult, DynamicReport } from './types';

// 后端 API 地址解析优先级：
// 1. localStorage 中用户手动配置的地址（适用于 GitHub Pages 等静态托管场景）
// 2. Vite 环境变量 VITE_API_URL（构建时注入）
// 3. 默认 /api（开发环境通过 Vite proxy 代理到 localhost:5000）
//
// 注意：用户只需填后端根地址（如 https://xxx.onrender.com），
// 本函数会自动补全 /api 后缀，与 api.ts 中的相对路径（如 /auth/login）拼接。
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
export function isApiMisconfigured(): boolean {
  const saved = localStorage.getItem('fanshu-api-base-url');
  if (saved && saved.trim()) return false;
  const env = (import.meta as any).env?.VITE_API_URL;
  if (env && env.trim()) return false;
  // 当前在 GitHub Pages 或其他静态托管，且未配置后端地址
  const host = window.location.hostname;
  return host.endsWith('.github.io') || host.endsWith('.vercel.app') || host.endsWith('.netlify.app');
}

function getToken(): string | null {
  return localStorage.getItem('fanshu-token');
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json', ...(options?.headers as Record<string, string> || {}) };
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;

  // AbortController 实现 60 秒超时（Render 免费版冷启动需 30-60 秒）
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 60000);

  try {
    const res = await fetch(`${getApiBaseUrl()}${url}`, { ...options, headers, signal: controller.signal });
    clearTimeout(timeoutId);

    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: `请求失败 (HTTP ${res.status})` }));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    if (res.headers.get('content-type')?.includes('application/json')) {
      return res.json();
    }
    return {} as T;
  } catch (e: any) {
    clearTimeout(timeoutId);
    if (e.name === 'AbortError') {
      throw new Error('请求超时（服务器可能正在冷启动，请稍等 30-60 秒后重试）');
    }
    if (e.message === 'Failed to fetch' || e.message?.includes('NetworkError')) {
      throw new Error('无法连接到服务器，请检查「我的 → 服务器」中配置的后端地址是否正确');
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
