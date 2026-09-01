import type { Book, Chapter, Character, Outline, Template, AIConfig, AIConfigList, AISession, AIMessage, ActionCard, ProgressMap, StatsData, StageItem, PromptT, BookBible, SkillPack, ReviewResult, AnalysisResult, BrainstormResult, DynamicReport, OptimizationReport, AppliedPatchItem, ImpactPreview } from './types';

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
  // localStorage 访问失败（Tracking Prevention / 隐身模式 / PWA 清 storage）
  // 不抛错，直接退到环境变量 / 默认 / 同源。
  try {
    const saved = localStorage.getItem('fanshu-api-base-url');
    if (saved && saved.trim()) {
      let url = saved.trim().replace(/\/+$/, '');
      if (!url.endsWith('/api')) url = url + '/api';
      return url;
    }
  } catch { /* fallback */ }
  const env = (import.meta as any).env?.VITE_API_URL;
  if (env && env.trim()) return env.trim().replace(/\/+$/, '');
  const host = window.location.hostname;
  const isStaticHost = host.endsWith('.github.io') || host.endsWith('.vercel.app') || host.endsWith('.netlify.app');
  if (isStaticHost) return DEFAULT_API_BASE;
  return '/api';
}

export function setApiBaseUrl(url: string) {
  try {
    if (url && url.trim()) {
      localStorage.setItem('fanshu-api-base-url', url.trim().replace(/\/+$/, ''));
    } else {
      localStorage.removeItem('fanshu-api-base-url');
    }
  } catch { /* ignore */ }
}

// 检测当前是否在静态托管环境（如 GitHub Pages）且未配置后端地址
// 注意：内置默认地址后，静态托管环境不再算 misconfigured
export function isApiMisconfigured(): boolean {
  return false;
}

function getToken(): string | null {
  try { return localStorage.getItem('fanshu-token'); } catch { return null; }
}

// 后端是否正在预热中（避免重复预热）
let warmingUp = false;

/**
 * 预热后端：发起一个轻量 GET 请求触发 Render 实例唤醒。
 * 静默执行，不抛错，不阻塞 UI。
 * 适用于页面加载时或长时间空闲后。
 * 使用 /api/health 超轻量端点，不查数据库。
 */
export function warmUpBackend(): void {
  if (warmingUp) return;
  warmingUp = true;
  // 用 fetch 而非 request()，避免被重试逻辑影响
  fetch(`${getApiBaseUrl()}/health`, { method: 'GET' })
    .catch(() => { /* 静默失败 */ })
    .finally(() => { warmingUp = false; });
}

/**
 * 带自动重试的 fetch：应对 Render 免费版冷启动超时。
 * - 第 1 次失败后等 3 秒重试
 * - 第 2 次失败后等 8 秒重试
 * - 第 3 次失败后等 15 秒重试（累计约 26 秒，覆盖后端唤醒 + 数据库冷启动窗口）
 * - 第 4 次仍失败则抛错
 */
async function fetchWithRetry(url: string, options: RequestInit, externalSignal?: AbortSignal, maxRetries = 3): Promise<Response> {
  const delays = [3000, 8000, 15000]; // 重试间隔（毫秒）
  let lastError: any;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    if (externalSignal?.aborted) throw new DOMException('Aborted', 'AbortError');
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 60000);
      const onExternalAbort = () => controller.abort();
      if (externalSignal) externalSignal.addEventListener('abort', onExternalAbort);
      const res = await fetch(url, { ...options, signal: controller.signal });
      clearTimeout(timeoutId);
      if (externalSignal) externalSignal.removeEventListener('abort', onExternalAbort);
      // 5xx 错误重试（可能是冷启动中），4xx 直接返回
      if (res.status >= 500 && attempt < maxRetries) {
        await new Promise(r => setTimeout(r, delays[attempt]));
        continue;
      }
      return res;
    } catch (e: any) {
      lastError = e;
      // 网络错误/超时重试（Render 冷启动典型表现），但用户主动中止则直接抛错
      if (externalSignal?.aborted) throw e;
      if (attempt < maxRetries) {
        await new Promise(r => setTimeout(r, delays[attempt]));
        continue;
      }
    }
  }
  throw lastError;
}

/**
 * SSE 流式请求专用 fetch：带连接期自动重试 + 友好错误提示。
 * - 复用 fetchWithRetry：仅在「响应头到达前」的网络错误/5xx 重试（请求未真正建立，
 *   重试无副作用）；一旦流建立（fetch resolve）立即返回，绝不干扰流读取。
 * - 流中途断开（移动网络波动/代理掐断，移动端浏览器报 "TypeError: network error"）
 *   不在本层重试（重复生成有副作用），转为友好错误提示，用户点重试即可。
 */
async function fetchStream(url: string, cfg: RequestInit, signal?: AbortSignal): Promise<Response> {
  // cfg.signal 一律改走第三参：fetchWithRetry 内部会用自己的 controller.signal 覆盖
  // options.signal，若不抽出来外部中止信号会静默丢失
  const externalSignal = signal ?? (cfg.signal as AbortSignal | undefined);
  const { signal: _omit, ...rest } = cfg;
  try {
    return await fetchWithRetry(url, rest, externalSignal);
  } catch (e: any) {
    if (e?.name === 'AbortError') throw e;
    const msg = String(e?.message || '');
    if (e?.name === 'TypeError' || msg === 'Failed to fetch' || msg.includes('NetworkError') || msg.includes('network error')) {
      throw new Error('连接中断（网络波动或服务重启），已自动重试仍失败。已生成的部分内容已保存到会话历史，可稍后重试');
    }
    throw e;
  }
}

async function request<T>(url: string, options?: RequestInit, signal?: AbortSignal): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json', ...(options?.headers as Record<string, string> || {}) };
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;

  try {
    const res = await fetchWithRetry(`${getApiBaseUrl()}${url}`, { ...options, headers }, signal);

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
      // 【关键】：保留 name='AbortError'（让所有外层 if (e.name !== 'AbortError') 过滤分支正常工作），
      // 不要因为包装成普通 Error 把 name 丢了 → 导致取消被当成失败渲染
      const err = new Error('请求已取消');
      (err as any).name = 'AbortError';
      (err as any).cancelled = true;
      throw err;
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
  rebinVolumes: (bookId: string) =>
    request<{ success: boolean; chapters: number; volumes: number }>(`/books/${bookId}/chapters/rebin-volumes`, { method: 'POST' }),

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
  listAIConfigs: () => request<AIConfigList>('/ai/configs'),
  createAIConfig: (data: Partial<AIConfig>) => request<AIConfig>('/ai/configs', { method: 'POST', body: JSON.stringify(data) }),
  activateAIConfig: (id: string) => request<AIConfig>(`/ai/configs/${id}/activate`, { method: 'PUT' }),
  deleteAIConfig: (id: string) => request<{ ok: boolean }>(`/ai/configs/${id}`, { method: 'DELETE' }),
  fetchAIModels: (baseUrl: string, apiKey: string) =>
    request<{ models: { id: string; owned_by: string }[] }>('/ai/models', { method: 'POST', body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }) }),
  testAIConnection: (baseUrl: string, apiKey: string, model: string) =>
    request<{ success: boolean; reply: string; model: string; usage?: any }>('/ai/test', { method: 'POST', body: JSON.stringify({ base_url: baseUrl, api_key: apiKey, model }) }),
  aiChat: (messages: { role: string; content: string }[]) => request<{ content: string; usage?: any }>('/ai/chat', { method: 'POST', body: JSON.stringify({ messages }) }),
  aiChatStream: (messages: { role: string; content: string }[], signal?: AbortSignal) => {
    const cfg: RequestInit = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ messages }) };
    if (signal) cfg.signal = signal;
    return fetchStream(`${getApiBaseUrl()}/ai/chat/stream`, cfg, signal);
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

  // AI Continue（14项优化版）：返回正文+审校状态+章节计划+一致性检查结果等
  // 注意：不走 fetchWithRetry（60s 超时），Agent 管线（章节计划→正文→去AI味→一致性检查）
  // 总耗时 80-180s，60s 超时必然触发 abort → "请求已取消"。改用直接 fetch + 300s 长超时保护。
  aiContinue: async (bookId: string, instruction: string, skillPackIds?: string[], enableConsistencyCheck?: boolean, signal?: AbortSignal, opts?: { targetChapterNum?: number; prevChapterContent?: string; chapterLangStyles?: string[] }): Promise<{
    content: string;
    draft?: string | null;
    review_notes: string;
    deai_status: 'skipped' | 'success' | 'failed';
    chapter_plan: string;
    current_chapter_num: number;
    vol_index: number;
    vol_title: string;
    temperature: number;
    consistency_passed: boolean;
    consistency_issues: string;
    post_validate?: {
      passed: boolean;
      score: number;
      issue_count: number;
      critical_count: number;
      warning_count: number;
      issues: Array<{ severity: string; category: string; pattern: string; count: number; position: string; suggestion: string }>;
      stats: Record<string, any>;
    } | null;
    changes_applied?: { applied: boolean; fields_updated: string[]; errors: string[]; } | null;
    gate_result?: { passed: boolean; critical_count: number; warning_count: number; issues: Array<{ gate: string; severity: string; message: string }>; } | null;
    chapter_score?: { score: number; grade: 'A' | 'B' | 'C' | 'D'; auto_revise: boolean; breakdown: Record<string, number>; } | null;
    suggested_title?: string;
  }> => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    // 300s 超时保护（防永久挂起），远大于 Agent 管线最大耗时
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 300000);
    const onExternalAbort = () => controller.abort();
    if (signal) signal.addEventListener('abort', onExternalAbort);
    try {
      const res = await fetch(`${getApiBaseUrl()}/books/${bookId}/ai-continue`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          instruction,
          skill_pack_ids: skillPackIds || [],
          enable_consistency_check: enableConsistencyCheck !== false,
          target_chapter_num: opts?.targetChapterNum,
          prev_chapter_content: opts?.prevChapterContent,
          chapter_lang_styles: opts?.chapterLangStyles || [],
        }),
        signal: controller.signal,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: `请求失败 (HTTP ${res.status})` }));
        throw new Error(err.error || `HTTP ${res.status}`);
      }
      return res.json();
    } catch (e: any) {
      if (signal?.aborted) {
        const userErr: any = new DOMException('Aborted', 'AbortError');
        userErr.cancelled = true;
        throw userErr;
      }
      if (e.name === 'AbortError') {
        // 内部 300s 超时触发 → 友好提示，但保留 cancelled 标记，外层过滤 if (e.name==='AbortError') 仍能识别为"非失败"
        const timeoutErr: any = new Error('请求超时，Agent管线处理时间过长，请稍后重试');
        timeoutErr.name = 'AbortError';
        timeoutErr.cancelled = true;
        timeoutErr.isTimeout = true;
        throw timeoutErr;
      }
      if (e.message === 'Failed to fetch' || e.message?.includes('NetworkError')) {
        throw new Error('无法连接到服务器，可能正在冷启动中。请稍等几秒后重试');
      }
      throw e;
    } finally {
      clearTimeout(timeoutId);
      if (signal) signal.removeEventListener('abort', onExternalAbort);
    }
  },

  // 连续创作模式：普通 POST 批量生成 N 章，自动保存。返回原始 Response（含 body 流）
  aiContinueBatch: (bookId: string, instruction: string, skillPackIds: string[], count: number, opts?: { startChapterNum?: number; chapterLangStyles?: string[] }) => {
    const token = getToken();
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = `Bearer ${token}`;
    return fetch(`${getApiBaseUrl()}/books/${bookId}/ai-continue-batch`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        instruction,
        skill_pack_ids: skillPackIds || [],
        count,
        start_chapter_num: opts?.startChapterNum,
        chapter_lang_styles: opts?.chapterLangStyles || [],
      }),
    });
  },

  // 连续创作流式版（SSE）：解决 Render 同步请求超时（约100s）导致 Failed to fetch。
  // 每章 LLM stream + 5s 心跳保持连接活跃，每章完成推送 chapter_done 事件。
  // 返回原始 Response，调用方用 ReadableStream 解析 SSE 事件。
  aiContinueBatchStream: (bookId: string, instruction: string, skillPackIds: string[], count: number, opts?: { startChapterNum?: number; chapterLangStyles?: string[] }, signal?: AbortSignal) => {
    const token = getToken();
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const cfg: RequestInit = {
      method: 'POST',
      headers,
      body: JSON.stringify({
        instruction,
        skill_pack_ids: skillPackIds || [],
        count,
        start_chapter_num: opts?.startChapterNum,
        chapter_lang_styles: opts?.chapterLangStyles || [],
      }),
    };
    if (signal) cfg.signal = signal;
    // 不设 timeout：SSE 流式持续推送心跳，不会触发浏览器/Render 空闲超时
    return fetchStream(`${getApiBaseUrl()}/books/${bookId}/ai-continue-batch/stream`, cfg, signal);
  },

  // P2-9：Spot-Fix 修订（按校验问题路由：local 类只修补问题段落，省 token）
  aiSpotFix: (bookId: string, content: string, postValidate: any, mode: string = 'auto', signal?: AbortSignal) =>
    request<{
      strategy: 'none' | 'spot_fix' | 'rewrite';
      content: string;
      message?: string;
      patches_count?: number;
      token_saving?: { full_rewrite_tokens: number; spot_fix_tokens: number; saved_tokens: number; saving_ratio: number };
      post_validate?: any;
      structural_issues?: any[];
    }>(`/books/${bookId}/ai-spot-fix`, {
      method: 'POST',
      body: JSON.stringify({ content, post_validate: postValidate, mode }),
    }, signal),

  // P2-Entity：实体注册表 - 抽取/重命名/合并
  listEntities: (bookId: string) =>
    request<{ characters: any[]; factions: any[]; locations: any[]; items: any[]; skills: any[] }>(`/books/${bookId}/entities`),
  renameEntity: (bookId: string, oldName: string, newName: string, entityType: string = 'character') =>
    request<{ success: boolean; fields_updated: string[]; chapters_affected: number; total_replacements: number; error?: string }>(
      `/books/${bookId}/entities/rename`,
      { method: 'POST', body: JSON.stringify({ old_name: oldName, new_name: newName, entity_type: entityType }) }
    ),
  mergeEntities: (bookId: string, mainName: string, aliasNames: string[], entityType: string = 'character') =>
    request<{ success: boolean; merged: any[]; total_replacements: number; chapters_affected: number }>(
      `/books/${bookId}/entities/merge`,
      { method: 'POST', body: JSON.stringify({ main_name: mainName, alias_names: aliasNames, entity_type: entityType }) }
    ),

  // AI Continue 流式版（#8：SSE 推送初稿）。返回原始 Response，前端用 ReadableStream 解析
  // 统一走后端 _build_ai_continue_context，与多Agent同步/连续创作模式注入相同上下文
  aiContinueStream: (bookId: string, instruction: string, skillPackIds?: string[], opts?: { targetChapterNum?: number; prevChapterContent?: string; chapterLangStyles?: string[] }, signal?: AbortSignal) => {
    const token = getToken();
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const cfg: RequestInit = {
      method: 'POST',
      headers,
      body: JSON.stringify({
        instruction,
        skill_pack_ids: skillPackIds || [],
        target_chapter_num: opts?.targetChapterNum,
        prev_chapter_content: opts?.prevChapterContent,
        chapter_lang_styles: opts?.chapterLangStyles || [],
      }),
    };
    if (signal) cfg.signal = signal;
    return fetchStream(`${getApiBaseUrl()}/books/${bookId}/ai-continue/stream`, cfg, signal);
  },

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
  brainstorm: (bookId: string, concept: string, dimension?: string, skillPackIds?: string[]) =>
    request<BrainstormResult>(`/books/${bookId}/brainstorm`, { method: 'POST', body: JSON.stringify({ concept, dimension: dimension || 'all', skill_pack_ids: skillPackIds || [] }) }),

  // AI Analyze Content (导入作品后自动识别)
  analyzeContent: (bookId: string) =>
    request<{ success: boolean; updated_fields: string[]; bible: BookBible }>(`/books/${bookId}/ai-analyze-content`, { method: 'POST' }),

  // AI Import Recognize (导入作品后按文件名/章节标题自动识别填入各空维度)
  aiImportRecognize: (bookId: string, dimensions: string[] = [], skillPackIds: string[] = []) =>
    request<{ success: boolean; message: string; filled: string[]; bible: BookBible }>(`/books/${bookId}/ai-import-recognize`, {
      method: 'POST',
      body: JSON.stringify({ dimensions, skill_pack_ids: skillPackIds }),
    }),

  // AI Anti-Forget Check (长篇小说防遗忘与一致性检查)
  // 【改造】支持分卷选择：volume_ids 为空数组表示不按卷筛选；非空则只检查指定卷
  aiAntiForgetCheck: (bookId: string, scope: 'reports' | 'dimensions' = 'reports', skillPackIds: string[] = [], volumeIds: string[] = []) =>
    request<{ success: boolean; report: any; report_record: any; scope: string; ch_count: number; source_label: string }>(`/books/${bookId}/ai-anti-forget-check`, {
      method: 'POST',
      body: JSON.stringify({ scope, skill_pack_ids: skillPackIds, volume_ids: volumeIds }),
    }),

  // 防遗忘检查报告 CRUD
  listAntiForgetReports: (bookId: string) =>
    request<{ reports: any[] }>(`/books/${bookId}/anti-forget-reports`),
  updateAntiForgetReport: (bookId: string, reportId: string, data: { title?: string; report?: any; summary?: string; health_score?: number; status?: 'pending' | 'reviewed' | 'applied' | 'ignored'; fix_draft?: any[] | null; text_fix_draft?: any[] | null; notified?: boolean }) =>
    request<{ success: boolean; reports: any[] }>(`/books/${bookId}/anti-forget-reports/${reportId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteAntiForgetReport: (bookId: string, reportId: string) =>
    request<{ success: boolean; reports: any[] }>(`/books/${bookId}/anti-forget-reports/${reportId}`, {
      method: 'DELETE',
    }),

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
  analyzePlotVolume: (bookId: string, volumeId: string, volumeTitle: string, skillPackIds?: string[]) =>
    request<{ success: boolean; volume_data: any; volumes: any[]; bible: BookBible }>(`/books/${bookId}/ai-analyze-plot-volume`, {
      method: 'POST',
      body: JSON.stringify({ volume_id: volumeId, volume_title: volumeTitle, skill_pack_ids: skillPackIds || [] }),
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
  // 按卷批量生成动态报告（每5章一份，自动补齐该卷所有5章区间）
  batchGenerateDynamicReports: (bookId: string, data: { volume_id?: string; volume_title?: string; skill_pack_ids?: string[]; overwrite?: boolean }) =>
    request<{ success: boolean; volume_title: string; chapter_range: number[]; total_intervals: number; generated_count: number; skipped_count: number; error_count: number; generated: DynamicReport[]; skipped: any[]; errors: any[] }>(`/books/${bookId}/dynamic-reports/batch-generate`, {
      method: 'POST',
      body: JSON.stringify({
        volume_id: data.volume_id || '',
        volume_title: data.volume_title || '',
        skill_pack_ids: data.skill_pack_ids || [],
        overwrite: data.overwrite || false,
      }),
    }),
  updateDynamicReport: (bookId: string, reportId: string, data: Partial<DynamicReport>) =>
    request<DynamicReport>(`/books/${bookId}/dynamic-reports/${reportId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteDynamicReport: (bookId: string, reportId: string) =>
    request<{ success: boolean }>(`/books/${bookId}/dynamic-reports/${reportId}`, { method: 'DELETE' }),
  batchDeleteDynamicReports: (bookId: string, reportIds: string[]) =>
    request<{ success: boolean; deleted_count: number; deleted_ids: string[] }>(`/books/${bookId}/dynamic-reports/batch-delete`, {
      method: 'POST',
      body: JSON.stringify({ report_ids: reportIds }),
    }),
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

  // ==== 大纲工作流：五幕式总纲 + 卷纲滚动生成 ====
  aiOutlineMaster: (bookId: string, skillPackIds?: string[], totalChapters?: number, chaptersPerVolume?: number, volumeCount?: number) =>
    request<{ master_outline: string; volume_count: number }>(`/books/${bookId}/ai-outline-master`, {
      method: 'POST',
      body: JSON.stringify({ skill_pack_ids: skillPackIds || [], total_chapters: totalChapters || 300, chapters_per_volume: chaptersPerVolume || 50, volume_count: volumeCount }),
    }),

  // 情节节点/大纲分卷：不走 fetchWithRetry 默认 60s 超时（节点设计是 Agent 管线，LLM 要生成 50-80 个节点+6要素，
  // 总耗时可达 80-180s+，60s 硬超时会被自动 abort → UI 上显示"情节节点设计失败：请求已取消"，实际用户根本没取消。
  // 并发(2)+逐事件拆分的整体耗时可能逼近 5-7 分钟，且 Render 请求上限≈500s：
  // 必须用直接 fetch + **600s** 长超时（> Render 500s），否则会在后端出结果前被客户端提前 abort，导致"节点设计静默失败无报错"。
  aiOutlineVolume: async (bookId: string, volumeIndex: number, volumeTitle?: string, skillPackIds?: string[], chaptersPerVolume?: number, nodeOnly?: boolean, onProgress?: (info: { type: string; message?: string }) => void): Promise<{ volume_data: any; timeline: string; bible: any }> => {
    // 后台异步任务 + 前端短轮询（2026-08-31 架构升级）：
    // 生成整卷大纲需 2-5 分钟，任何长连接(同步POST/SSE)都会被 Cloudflare+Render
    // 的双层代理在 ~100s 硬切（浏览器抛 "network error"）。故改为：
    //   1) POST 秒回 {job_id}
    //   2) 每 3s 轮询 GET /status，任务在后台守护线程解耦运行
    //   3) 完成后 GET /status 返回 {state:done, volume_data, timeline, bible}
    // 每次 HTTP 请求都 <50ms → 永不触发网关超时，同时根治 502 与 network error。
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const base = getApiBaseUrl();

    // 1) 提交任务，秒回 job_id
    let jobId: string;
    let submitResp: Response;
    try {
      submitResp = await fetch(`${base}/books/${bookId}/ai-outline-volume`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ volume_index: volumeIndex, volume_title: volumeTitle, skill_pack_ids: skillPackIds || [], chapters_per_volume: chaptersPerVolume || 50, node_only: !!nodeOnly }),
      });
    } catch (e: any) {
      if (e.message === 'Failed to fetch' || e.message?.includes('NetworkError')) {
        throw new Error('无法连接到服务器，可能正在冷启动中。请稍等几秒后重试');
      }
      throw e;
    }
    if (!submitResp.ok) {
      const err = await submitResp.json().catch(() => ({ error: `请求失败 (HTTP ${submitResp.status})` }));
      throw new Error(err.error || `HTTP ${submitResp.status}`);
    }
    const submitted = await submitResp.json().catch(() => ({}));
    if (!submitted.job_id) {
      throw new Error(submitted.error || '任务提交失败，请重试');
    }
    jobId = submitted.job_id;

    // 2) 每 3s 轮询任务状态（短请求，绝不长连）
    onProgress?.({ type: 'start', message: '正在生成卷大纲与情节节点...' });
    const deadline = Date.now() + 15 * 60 * 1000; // 15 分钟兜底
    let lastMsg = '';
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 3000));
      let sr: Response;
      try {
        sr = await fetch(`${base}/books/${bookId}/ai-outline-volume/status?job_id=${encodeURIComponent(jobId)}`, {
          headers,
          cache: 'no-store',
        });
      } catch {
        // 单次状态轮询网络抖动：继续下一轮，不打断整体
        continue;
      }
      if (!sr.ok) {
        const jerr = await sr.json().catch(() => ({}));
        // 404 job 过期等：直接报错
        if (sr.status === 404 || sr.status === 403) throw new Error(jerr.error || '任务已失效，请重新提交');
        continue;
      }
      const st = await sr.json().catch(() => ({}));
      if (st.state === 'running') {
        const msg = `正在生成卷大纲与情节节点...（后台处理中）`;
        if (msg !== lastMsg) { lastMsg = msg; onProgress?.({ type: 'heartbeat', message: msg }); }
        continue;
      }
      if (st.state === 'error') {
        throw new Error(st.error || '生成失败，请重试');
      }
      if (st.state === 'done' && st.volume_data) {
        onProgress?.({ type: 'finish' });
        return { volume_data: st.volume_data, timeline: st.timeline || '', bible: st.bible };
      }
    }
    throw new Error('生成超时（15分钟），请稍后重新提交');
  },

  // ====== 节点设计师：分段异步生成情节节点 ======
  // submit: 秒回 job_id（action='start' 分段生成全部节点；action='revise' 重生成单个节点）
  nodeDesignSubmit: async (bookId: string, volumeIndex: number, opts?: { action?: 'start' | 'revise'; sessionId?: string; nodeIndex?: number; feedback?: string }): Promise<{ ok: boolean; job_id?: string; session_id?: string; total?: number; kind?: string; error?: string }> => {
    // 强行归一为合法正整数：JSON.stringify 会把 undefined/NaN 的键整体丢弃，
    // 一旦 volume_index 被丢弃，后端就会报「缺少 volume_index」。这里兜底。
    const vol = Math.max(1, Math.trunc(Number(volumeIndex)) || 1);
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(`${getApiBaseUrl()}/books/${bookId}/node-design/submit`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        volume_index: vol,
        action: opts?.action || 'start',
        session_id: opts?.sessionId,
        node_index: opts?.nodeIndex,
        feedback: opts?.feedback,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `请求失败 (HTTP ${res.status})`);
    return data;
  },

  // 轮询任务状态：running 时返回部分 nodes，不抛错（短请求，不触发网关超时）
  nodeDesignStatus: async (bookId: string, jobId: string): Promise<{ state: string; done: number; total: number; nodes: any[]; result?: any; message?: string; error?: string; kind?: string; current_segment?: string | null }> => {
    const headers: Record<string, string> = {};
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    try {
      const res = await fetch(`${getApiBaseUrl()}/books/${bookId}/node-design/status?job_id=${encodeURIComponent(jobId)}`, { headers, cache: 'no-store' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        if (res.status === 404 || res.status === 403) throw new Error(data.error || '任务已失效，请重新提交');
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      return data;
    } catch (e: any) {
      // 单次轮询网络抖动：让调用方继续下一轮（不抛错打断整体进度）
      if (e.message === 'Failed to fetch' || e.message?.includes('NetworkError')) {
        return { state: 'running', done: 0, total: 0, nodes: [], message: '…' };
      }
      throw e;
    }
  },

  // 采纳落地：把生成节点按 index 合并进 book_bible.timeline 对应卷的 nodes
  nodeDesignApply: (bookId: string, volumeIndex: number, nodes: any[]) =>
    request<{ ok: boolean; volume_index?: number; node_count?: number; timeline?: string; error?: string }>(`/books/${bookId}/node-design/apply`, {
      method: 'POST',
      body: JSON.stringify({ volume_index: volumeIndex, nodes }),
    }),

  // 取消进行中的生成任务
  nodeDesignCancel: (bookId: string, jobId: string) =>
    request<{ ok: boolean; error?: string }>(`/books/${bookId}/node-design/cancel`, {
      method: 'POST',
      body: JSON.stringify({ job_id: jobId }),
    }),

  // 从大纲总纲一次性提取各卷剧情（替代逐卷循环，更稳定）
  extractVolumesFromOutline: (bookId: string, skillPackIds?: string[], volumeCount?: number) =>
    request<{ success: boolean; volumes: any[]; bible: any }>(`/books/${bookId}/ai-extract-volumes-from-outline`, {
      method: 'POST',
      body: JSON.stringify({ skill_pack_ids: skillPackIds || [], volume_count: volumeCount }),
    }),

  // 导入剧情大纲文本，自动识别拆分到各卷（正则优先，AI兜底）
  importPlotOutline: (bookId: string, outlineText: string, skillPackIds?: string[]) =>
    request<{ success: boolean; volumes: any[]; imported_count: number; bible: any }>(`/books/${bookId}/ai-import-plot-outline`, {
      method: 'POST',
      body: JSON.stringify({ outline_text: outlineText, skill_pack_ids: skillPackIds || [] }),
    }),

  // 反生成五幕式总纲：从各卷剧情(timeline)反向提炼总纲，写入大纲维度(plot_design)
  reverseGenerateOutline: (bookId: string, skillPackIds?: string[]) =>
    request<{ success: boolean; master_outline: string; bible: any }>(`/books/${bookId}/ai-reverse-generate-outline`, {
      method: 'POST',
      body: JSON.stringify({ skill_pack_ids: skillPackIds || [] }),
    }),

  // 一键清空剧情分卷大纲（timeline）
  clearTimeline: (bookId: string) =>
    request<{ success: boolean; bible: any }>(`/books/${bookId}/clear-timeline`, { method: 'POST' }),

  // AI识别指定卷的人物档案（按卷）
  analyzeCharacterVolume: (bookId: string, volumeId: string, volumeTitle: string, skillPackIds?: string[]) =>
    request<{ success: boolean; volume_data: any; character_volumes: any[]; bible: any }>(`/books/${bookId}/ai-analyze-character-volume`, {
      method: 'POST',
      body: JSON.stringify({ volume_id: volumeId, volume_title: volumeTitle, skill_pack_ids: skillPackIds || [] }),
    }),

  // AI识别指定卷的物资库（按卷）
  analyzeInventoryVolume: (bookId: string, volumeId: string, volumeTitle: string, skillPackIds?: string[]) =>
    request<{ success: boolean; volume_data: any; inventory: any[]; bible: any }>(`/books/${bookId}/ai-analyze-inventory-volume`, {
      method: 'POST',
      body: JSON.stringify({ volume_id: volumeId, volume_title: volumeTitle, skill_pack_ids: skillPackIds || [] }),
    }),

  // AI识别指定卷的动态文件分类（按卷）
  analyzeDynamicVolume: (bookId: string, volumeId: string, volumeTitle: string, skillPackIds?: string[]) =>
    request<{ success: boolean; volume_data: any; dynamic_volumes: any[]; bible: any }>(`/books/${bookId}/ai-analyze-dynamic-volume`, {
      method: 'POST',
      body: JSON.stringify({ volume_id: volumeId, volume_title: volumeTitle, skill_pack_ids: skillPackIds || [] }),
    }),

  // AI识别指定卷的伏笔（按卷）
  analyzeForeshadowingVolume: (bookId: string, volumeId: string, volumeTitle: string, skillPackIds?: string[]) =>
    request<{ success: boolean; volume_data: any; foreshadowing_volumes: any[]; bible: any }>(`/books/${bookId}/ai-analyze-foreshadowing-volume`, {
      method: 'POST',
      body: JSON.stringify({ volume_id: volumeId, volume_title: volumeTitle, skill_pack_ids: skillPackIds || [] }),
    }),

  // AI识别指定卷的地点/地图（按卷）
  analyzeLocationsVolume: (bookId: string, volumeId: string, volumeTitle: string, skillPackIds?: string[]) =>
    request<{ success: boolean; volume_data: any; locations_volumes: any[]; bible: any }>(`/books/${bookId}/ai-analyze-locations-volume`, {
      method: 'POST',
      body: JSON.stringify({ volume_id: volumeId, volume_title: volumeTitle, skill_pack_ids: skillPackIds || [] }),
    }),

  // ==== 总 AI 创作：总览全局各维度 ====
  aiMasterCreate: (bookId: string, dimensions: string[], skillPackIds?: string[], instruction?: string) =>
    request<{ results: Array<{ dimension: string; label: string; field: string; content?: string; error?: string }> }>(`/books/${bookId}/ai-master-create`, {
      method: 'POST',
      body: JSON.stringify({ dimensions, skill_pack_ids: skillPackIds || [], instruction: instruction || '' }),
    }),
  // 总AI创作流式版（SSE）：逐维度流式输出，注入已有维度上下文
  aiMasterCreateStream: (bookId: string, dimensions: string[], skillPackIds: string[], instruction: string, signal?: AbortSignal, sessionOutputs?: Record<string, string>) => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const cfg: RequestInit = { method: 'POST', headers, body: JSON.stringify({ dimensions, skill_pack_ids: skillPackIds, instruction, session_outputs: sessionOutputs || {} }) };
    if (signal) cfg.signal = signal;
    return fetchStream(`${getApiBaseUrl()}/books/${bookId}/ai-master-create/stream`, cfg, signal);
  },

  // ============================================================================
  // 聊天驱动创作（边聊边写）
  // ============================================================================
  // 维度感知流式聊天：SSE 流，事件格式见后端 chat_collab_bp.py
  chatSmartStream: (bookId: string, message: string, sessionId?: string, scope?: string, signal?: AbortSignal) => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const cfg: RequestInit = {
      method: 'POST',
      headers,
      body: JSON.stringify({ book_id: bookId, message, session_id: sessionId, scope: scope || 'general' }),
    };
    if (signal) cfg.signal = signal;
    return fetchStream(`${getApiBaseUrl()}/ai/chat/smart`, cfg, signal);
  },
  // 采纳 Action Card，落地到对应维度
  applyChatCard: (bookId: string, card: ActionCard, sessionId?: string) =>
    request<{ ok: boolean; field: string; label: string; progress: ProgressMap }>(
      '/ai/chat/smart/apply-card',
      { method: 'POST', body: JSON.stringify({ book_id: bookId, card, session_id: sessionId }) }
    ),
  // 创作进度地图
  getProgressMap: (bookId: string) => request<ProgressMap>(`/books/${bookId}/ai/progress`),
  // 列出该书所有聊天会话
  listBookChatSessions: (bookId: string) =>
    request<{ sessions: Array<{ id: string; scope: string; title: string; updated_at: string | null; message_count: number }> }>(
      `/books/${bookId}/ai/sessions`
    ),
  // 获取单个会话的全部消息（历史会话切换时加载聊天记录）
  getChatSessionMessages: (sessionId: string) =>
    request<{ id: string; title: string; scope: string; updated_at: string | null; messages: AIMessage[] }>(
      `/ai/sessions/${sessionId}/messages`
    ),
  // 副驾快捷动作（方案A：副驾做指挥官，调度总创作/章节创作能力）
  // action: master_create / continue / polish
  // 返回 SSE，统一副驾卡片协议（delta/card/done/error）
  chatSmartAction: (bookId: string, action: 'master_create' | 'continue' | 'polish', opts?: { instruction?: string; target_chapter_num?: number; prev_chapter_content?: string; session_id?: string; skill_pack_ids?: string[] }, signal?: AbortSignal) => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const cfg: RequestInit = {
      method: 'POST',
      headers,
      body: JSON.stringify({
        book_id: bookId,
        action,
        instruction: opts?.instruction || '',
        target_chapter_num: opts?.target_chapter_num,
        prev_chapter_content: opts?.prev_chapter_content,
        session_id: opts?.session_id,
        skill_pack_ids: opts?.skill_pack_ids || [],
      }),
    };
    if (signal) cfg.signal = signal;
    return fetchStream(`${getApiBaseUrl()}/ai/chat/smart/action`, cfg, signal);
  },

  // ============================================================================
  // AI 智驾（四Tab：设定/正文/去AI/校审）
  // ============================================================================
  // 设定Tab：维度列表
  smartDimensions: () =>
    request<{ dimensions: Array<{ key: string; label: string; field: string; card: string; icon: string; hint: string }> }>(
      '/ai/smart/dimensions'
    ),
  // 设定Tab：多选意见生成（用户提需求 → AI给 3-5 个方案）
  smartSuggest: (bookId: string, dimension: string, requirement: string, skillPackIds: string[] = [], userPaste?: string) =>
    request<{ suggestions: Array<{ id: string; title: string; preview: string; _from_user?: boolean; _full_content?: string }>; dimension: string; dimension_label: string; requirement: string }>(
      '/ai/smart/suggest',
      { method: 'POST', body: JSON.stringify({ book_id: bookId, dimension, requirement, skill_pack_ids: skillPackIds, user_paste: userPaste }) }
    ),
  // 设定Tab：基于选中意见生成最终内容（SSE 流式）
  smartGenerateStream: (bookId: string, dimension: string, suggestion: string, requirement: string, skillPackIds: string[] = [], sessionId?: string, signal?: AbortSignal, fromUserPaste?: boolean) => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const cfg: RequestInit = {
      method: 'POST',
      headers,
      body: JSON.stringify({ book_id: bookId, dimension, suggestion, requirement, skill_pack_ids: skillPackIds, session_id: sessionId, from_user_paste: fromUserPaste }),
    };
    if (signal) cfg.signal = signal;
    return fetchStream(`${getApiBaseUrl()}/ai/smart/generate`, cfg, signal);
  },
  // 设定Tab：单独维度AI修改（SSE 流式）
  smartDimEditStream: (bookId: string, dimension: string, currentContent: string, editRequest: string, skillPackIds: string[] = [], sessionId?: string, signal?: AbortSignal) => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const cfg: RequestInit = {
      method: 'POST',
      headers,
      body: JSON.stringify({ book_id: bookId, dimension, current_content: currentContent, edit_request: editRequest, skill_pack_ids: skillPackIds, session_id: sessionId }),
    };
    if (signal) cfg.signal = signal;
    return fetchStream(`${getApiBaseUrl()}/ai/smart/dim-edit`, cfg, signal);
  },
  // 设定Tab：批量生成多维度（SSE 流式）
  smartBatchStream: (bookId: string, dimensions: string[], requirement: string, skillPackIds: string[] = [], sessionId?: string, signal?: AbortSignal) => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const cfg: RequestInit = {
      method: 'POST',
      headers,
      body: JSON.stringify({ book_id: bookId, dimensions, requirement, skill_pack_ids: skillPackIds, session_id: sessionId }),
    };
    if (signal) cfg.signal = signal;
    return fetchStream(`${getApiBaseUrl()}/ai/smart/batch`, cfg, signal);
  },
  // 正文Tab：获取最新章节（自动定位）
  smartLatestChapter: (bookId: string) =>
    request<{ latest: { id: string; title: string; order_index: number; word_count: number; status: string } | null; next_chapter_num: number }>(
      `/ai/smart/latest-chapter?book_id=${bookId}`
    ),
  // 去AITab：拉取去AI味技能包列表
  smartDeaiPacks: () =>
    request<{ packs: Array<{ id: string; name: string; description: string; icon: string; priority: number }> }>(
      '/ai/smart/deai-packs'
    ),
  // 去AITab：对指定章节去AI味（SSE 流式）
  smartDeaiStream: (bookId: string, chapterId: string, skillPackIds: string[] = [], sessionId?: string, signal?: AbortSignal) => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const cfg: RequestInit = {
      method: 'POST',
      headers,
      body: JSON.stringify({ book_id: bookId, chapter_id: chapterId, skill_pack_ids: skillPackIds, session_id: sessionId }),
    };
    if (signal) cfg.signal = signal;
    return fetchStream(`${getApiBaseUrl()}/ai/smart/deai`, cfg, signal);
  },
  // B3：去AI Tab·风格对齐诊断（12维评分 + 范本并排 + 改点建议）
  smartStyleAlign: (bookId: string, chapterId: string) =>
    request<{
      chapter_title: string;
      chapter_num: number;
      dimensions: Array<{ key: string; name: string; score: number; note: string }>;
      avg_score: number;
      bad_items: Array<{ key: string; name: string; score: number; note: string; fix_suggestion: string }>;
      style_pack: { id: string; name: string; content: string } | null;
      book_genre: string;
      summary: string;
    }>(
      '/ai/smart/style-align',
      { method: 'POST', body: JSON.stringify({ book_id: bookId, chapter_id: chapterId }) }
    ),
  // 设定Tab·通用聊天：自由讨论，关键词触发填入维度（SSE 流式）
  smartGeneralStream: (bookId: string, message: string, skillPackIds: string[] = [], sessionId?: string, signal?: AbortSignal) => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const cfg: RequestInit = {
      method: 'POST',
      headers,
      body: JSON.stringify({ book_id: bookId, message, skill_pack_ids: skillPackIds, session_id: sessionId }),
    };
    if (signal) cfg.signal = signal;
    return fetchStream(`${getApiBaseUrl()}/ai/smart/general`, cfg, signal);
  },

  // 校审Tab：防遗忘 / 一致性检查（支持按卷）
  smartReview: (bookId: string, mode: 'anti_forget' | 'consistency', chapterId?: string, skillPackIds: string[] = [], volumeIds: string[] = []) =>
    request<{ mode: string; report?: any; summary?: string; health_score?: number; chapter_id?: string; chapter_title?: string; passed?: boolean; issues?: string }>(
      '/ai/smart/review',
      { method: 'POST', body: JSON.stringify({ book_id: bookId, mode, chapter_id: chapterId, skill_pack_ids: skillPackIds, volume_ids: volumeIds }) }
    ),
  // 校审Tab：列出分卷（按卷检查用）
  smartVolumes: (bookId: string) =>
    request<{ volumes: Array<{ id: string; title: string; order_index: number; chapter_count: number }> }>(
      `/ai/smart/volumes?book_id=${bookId}`
    ),
  // 列出书的所有章节（去AI/校审选章节用）
  smartChapters: (bookId: string) =>
    request<{ chapters: Array<{ id: string; title: string; order_index: number; word_count: number; status: string }> }>(
      `/ai/smart/chapters?book_id=${bookId}`
    ),
  // 用去AI味后的内容替换原章节正文
  smartChapterReplace: (bookId: string, chapterId: string, content: string, sessionId?: string, cardId?: string) =>
    request<{ ok: boolean; chapter_id: string; word_count: number }>(
      '/ai/smart/chapter-replace',
      { method: 'POST', body: JSON.stringify({ book_id: bookId, chapter_id: chapterId, content, session_id: sessionId, card_id: cardId }) }
    ),
  // 更新卡片状态（忽略等不落地操作持久化）
  updateCardStatus: (sessionId: string, cardId: string, status: 'ignored' | 'adopted' | 'edited') =>
    request<{ ok: boolean }>(
      '/ai/chat/smart/update-card-status',
      { method: 'POST', body: JSON.stringify({ session_id: sessionId, card_id: cardId, status }) }
    ),

  // 基于防遗忘检查报告生成设定修正方案（不落地，返回给用户确认）
  smartFixFromReport: (bookId: string, reportId?: string, skillPackIds: string[] = [], volumeIds?: string[], signal?: AbortSignal) =>
    request<{ plan: Array<{ dim: string; label: string; issues: string[]; action: string; new_content: string }>; report_title: string; report_id: string }>(
      '/ai/smart/fix-from-report',
      { method: 'POST', body: JSON.stringify({ book_id: bookId, report_id: reportId, skill_pack_ids: skillPackIds, volume_ids: volumeIds || [] }) },
      signal
    ),

  // 应用用户确认的修正方案到对应设定维度（落地）
  smartApplyFix: (bookId: string, fixes: Array<{ dim: string; new_content: string }>) =>
    request<{ ok: boolean; applied: Array<{ dim: string; label: string }> }>(
      '/ai/smart/apply-fix',
      { method: 'POST', body: JSON.stringify({ book_id: bookId, fixes }) }
    ),

  // 基于防遗忘检查报告生成正文改写补丁（定位章节段落，不落地）
  smartFixTextFromReport: (bookId: string, reportId?: string, skillPackIds: string[] = [], volumeIds?: string[], signal?: AbortSignal) =>
    request<{ fixes: Array<{ chapter_id: string; chapter_title: string; paragraph_index: number; original: string; rewritten: string; reason: string; violation_desc: string; report_id: string }>; report_title: string; report_id: string; empty_reason?: string }>(
      '/ai/smart/fix-text-from-report',
      { method: 'POST', body: JSON.stringify({ book_id: bookId, report_id: reportId, skill_pack_ids: skillPackIds, volume_ids: volumeIds || [] }) },
      signal
    ),

  // 应用用户确认的正文改写补丁到对应章节（落地）
  smartApplyTextFix: (bookId: string, fixes: Array<{ chapter_id: string; paragraph_index?: number; original: string; rewritten: string }>) =>
    request<{ ok: boolean; applied: Array<{ chapter_id: string; chapter_title: string; count: number }> }>(
      '/ai/smart/apply-text-fix',
      { method: 'POST', body: JSON.stringify({ book_id: bookId, fixes }) }
    ),

  // M4: 获取系统优化报告（基于 FailureDB 的 Meta-LLM 分析）
  getOptimizationReport: (bookId: string) =>
    request<OptimizationReport>(`/ai/smart/optimization-report?book_id=${bookId}`),

  // M4b: 采纳一条优化建议 → 写入 prompt_patches（该 bucket 不再提示）
  adoptOptimizationSuggestion: (bookId: string, args: {
    bucket_key: string;
    category: string;
    dim_key?: string;
    patch_text: string;
  }) =>
    request<{
      ok: boolean;
      patch: AppliedPatchItem;
      active_patch_preview: string;
    }>(
      '/ai/smart/adopt-optimization-suggestion',
      { method: 'POST', body: JSON.stringify({ book_id: bookId, ...args }) }
    ),

  // M4c: 忽略一条优化建议 → 该 bucket 不再提示
  dismissOptimizationSuggestion: (bookId: string, bucket_key: string) =>
    request<{ ok: boolean; bucket_key: string }>(
      '/ai/smart/dismiss-optimization-suggestion',
      { method: 'POST', body: JSON.stringify({ book_id: bookId, bucket_key }) }
    ),

  // M4: 预览动作级联影响（rename_entity / edit_dim）
  previewImpact: (bookId: string, action: 'rename_entity' | 'edit_dim', payload: Record<string, any>) =>
    request<ImpactPreview>(
      '/ai/smart/preview-impact',
      { method: 'POST', body: JSON.stringify({ book_id: bookId, action, ...payload }) }
    ),
  // P1-3: 全文重算事件日志（后台批处理），<20章直接 JSON，>=20 章或 use_llm==always 返回 EventSource 流
  backfillEventLog: (bookId: string, opts?: { use_llm?: 'auto' | 'always' | 'never'; start_chapter?: number; end_chapter?: number }) =>
    request<{
      status: string; total: number; added_total: number; llm_count: number; rule_count: number;
      chapters?: Array<{ order: number; title: string; added?: number; use_llm?: boolean; error?: string }>;
    }>(
      '/ai/smart/backfill-eventlog',
      { method: 'POST', body: JSON.stringify({ book_id: bookId, ...(opts || {}) }) }
    ),

  // ────────────────────────────────────────────────────────────────
  // 通用对话Tab新增能力
  // ────────────────────────────────────────────────────────────────

  // 通用聊天模式（SSE）：不强制创作上下文，任意话题；命中维度时 meta.hit_suggestions 回传提示气泡
  chatGeneralStream: (message: string, opts?: { bookId?: string; sessionId?: string; aiConfigId?: string; roleId?: string; deepThink?: number; webSearch?: boolean; truncateHistoryTo?: number }, signal?: AbortSignal) => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const body: Record<string, any> = { message };
    if (opts?.bookId) body.book_id = opts.bookId;
    if (opts?.sessionId) body.session_id = opts.sessionId;
    if (opts?.aiConfigId) body.ai_config_id = opts.aiConfigId;
    if (opts?.roleId) body.role_id = opts.roleId;
    if (opts?.deepThink && opts.deepThink > 0) body.deep_think = opts.deepThink; // 0=关 1=标准 2=深度
    if (opts?.webSearch) body.web_search_enabled = true;
    if (typeof opts?.truncateHistoryTo === 'number') body.truncate_history_to = opts.truncateHistoryTo; // 重新生成：截断到前N条
    const cfg: RequestInit = { method: 'POST', headers, body: JSON.stringify(body) };
    if (signal) cfg.signal = signal;
    return fetchStream(`${getApiBaseUrl()}/ai/chat/general`, cfg, signal);
  },

  // 圆桌会议：6个专家Agent按两轮依次发言（主持人开场→讨论→总结报告），全程流式推送
  chatRoundtableStream: (topic: string, opts?: { bookId?: string; sessionId?: string; aiConfigId?: string; rounds?: number }, signal?: AbortSignal) => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const body: Record<string, any> = { topic };
    if (opts?.bookId) body.book_id = opts.bookId;
    if (opts?.sessionId) body.session_id = opts.sessionId;
    if (opts?.aiConfigId) body.ai_config_id = opts.aiConfigId;
    if (typeof opts?.rounds === 'number' && opts.rounds > 0) body.rounds = opts.rounds;
    const cfg: RequestInit = { method: 'POST', headers, body: JSON.stringify(body) };
    if (signal) cfg.signal = signal;
    return fetchStream(`${getApiBaseUrl()}/ai/chat/roundtable`, cfg, signal);
  },

  // ===== 联网搜索 Key 配置（Tavily/Exa/Brave 可选填；保存后立即生效） =====
  getSearchConfig: () =>
    request<{ keys: Record<string, string>; env: Record<string, boolean> }>('/ai/search-config', { method: 'GET' }),
  saveSearchConfig: (keys: { tavily?: string; exa?: string; brave?: string }) =>
    request<{ ok: boolean; updated: Record<string, boolean> }>('/ai/search-config', {
      method: 'PUT', body: JSON.stringify(keys),
    }),
};
