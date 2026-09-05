/**
 * api.ts token 通道单测（vitest）：
 * 覆盖鉴权链路的三个关键面（历史事故高发区）：
 *   1. Authorization: Bearer 头注入（request 通道）
 *   2. 下载 URL ?token= 参数拼接（<a href> 无法带 header 的兜底通道）
 *   3. 旧版 localStorage key（fanshu-token）迁移兼容
 * 运行：npm test
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { api, getApiBaseUrl, setApiBaseUrl, legacyKey } from './api';

// node 环境无 localStorage：内存实现（仅测试用）
function createMemoryStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length() { return store.size; },
    clear: () => store.clear(),
    getItem: (k) => (store.has(k) ? store.get(k)! : null),
    key: (i) => Array.from(store.keys())[i] ?? null,
    removeItem: (k) => { store.delete(k); },
    setItem: (k, v) => { store.set(k, String(v)); },
  } as Storage;
}

// fetch mock：记录调用参数，默认返回成功 JSON
let fetchCalls: Array<{ url: string; init: RequestInit }> = [];

function mockFetchOnce(status = 200, body: any = { success: true }) {
  return vi.fn().mockResolvedValue({
    ok: status < 400,
    status,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: () => Promise.resolve(body),
  } as Response);
}

let fetchMock: ReturnType<typeof mockFetchOnce>;

beforeEach(() => {
  vi.stubGlobal('localStorage', createMemoryStorage());
  fetchCalls = [];
  fetchMock = mockFetchOnce();
  vi.stubGlobal('fetch', (url: any, init: any = {}) => {
    fetchCalls.push({ url: String(url), init });
    return fetchMock();
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// 下载 URL token 通道（login_required_download 对应前端侧）
// ---------------------------------------------------------------------------
describe('导出 URL token 拼接', () => {
  it('已登录：三个导出 URL 均携带 ?token=', () => {
    localStorage.setItem('app-token', 'tok123');
    expect(api.getExportUrl('b1', 'txt')).toBe('/api/books/b1/export?format=txt&token=tok123');
    expect(api.getExportZipUrl('b1')).toBe('/api/books/b1/export-zip?token=tok123');
    expect(api.getExportFullUrl('b1')).toBe('/api/books/b1/export-full?token=tok123');
  });

  it('未登录：URL 不拼空 token（不带尾随 &token=）', () => {
    expect(api.getExportUrl('b1', 'epub')).toBe('/api/books/b1/export?format=epub');
    expect(api.getExportZipUrl('b1')).toBe('/api/books/b1/export-zip');
    expect(api.getExportFullUrl('b1')).toBe('/api/books/b1/export-full');
  });

  it('旧版 key（fanshu-token）兼容：同样拼进 URL', () => {
    localStorage.setItem(legacyKey('token'), 'legacy-tok');
    expect(api.getExportZipUrl('b1')).toBe('/api/books/b1/export-zip?token=legacy-tok');
  });

  it('新 key 优先于旧 key', () => {
    localStorage.setItem('app-token', 'new-tok');
    localStorage.setItem(legacyKey('token'), 'old-tok');
    expect(api.getExportZipUrl('b1')).toBe('/api/books/b1/export-zip?token=new-tok');
  });
});

// ---------------------------------------------------------------------------
// request() 的 Bearer 头注入
// ---------------------------------------------------------------------------
describe('request Bearer 头注入', () => {
  it('已登录：请求自动带 Authorization: Bearer', async () => {
    localStorage.setItem('app-token', 'bearer-tok');
    await api.getBookStats('b1');
    expect(fetchCalls.length).toBeGreaterThan(0);
    const headers = fetchCalls[0].init.headers as Record<string, string>;
    expect(headers['Authorization']).toBe('Bearer bearer-tok');
  });

  it('未登录：不发送 Authorization 头', async () => {
    await api.getBookStats('b1');
    const headers = fetchCalls[0].init.headers as Record<string, string>;
    expect(headers['Authorization']).toBeUndefined();
  });

  it('旧版 key 兼容：Bearer 头使用 legacy token', async () => {
    localStorage.setItem(legacyKey('token'), 'legacy-bearer');
    await api.getBookStats('b1');
    const headers = fetchCalls[0].init.headers as Record<string, string>;
    expect(headers['Authorization']).toBe('Bearer legacy-bearer');
  });
});

// ---------------------------------------------------------------------------
// API base URL 解析优先级
// ---------------------------------------------------------------------------
describe('getApiBaseUrl 解析优先级', () => {
  it('默认同源 /api', () => {
    expect(getApiBaseUrl()).toBe('/api');
  });

  it('localStorage 覆盖优先；自动补 /api 后缀与去尾斜杠', () => {
    localStorage.setItem('app-api-base-url', 'https://example.com');
    expect(getApiBaseUrl()).toBe('https://example.com/api');
    localStorage.setItem('app-api-base-url', 'https://example.com/api/');
    expect(getApiBaseUrl()).toBe('https://example.com/api');
  });

  it('setApiBaseUrl 空串清除覆盖', () => {
    localStorage.setItem('app-api-base-url', 'https://x.com');
    setApiBaseUrl('');
    expect(getApiBaseUrl()).toBe('/api');
  });
});
