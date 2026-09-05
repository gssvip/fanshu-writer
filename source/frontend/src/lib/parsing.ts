/**
 * 纯解析工具集（从 ChatPanel.tsx 抽出，无 React 依赖，可独立单测）。
 * 抽出原因：这些函数是 SSE 流/章节号解析的核心链路，历史上是事故高发区
 * （半截卡片、心跳帧、HTML 错误页混入流、章号口径不一致），必须有 vitest 覆盖。
 */
import type { ActionCard } from '../types';

// SSE 事件类型
export type SseEvent =
  | { type: 'delta'; content: string; speaker?: string }
  | { type: 'card'; card: ActionCard; session_id: string; meta?: any }
  | { type: 'done'; session_id: string; summary?: string }
  | { type: 'speaker_done'; speaker?: string; round?: number }
  | { type: 'error'; error: string }
  | { type: 'meta'; kind: string; info?: any };

// SSE 流解析
export async function* parseSSE(response: Response): AsyncGenerator<SseEvent> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        for (const line of block.split('\n')) {
          const trimmed = line.trim();
          if (trimmed.startsWith(':')) continue; // SSE 协议：冒号开头=注释心跳帧（后端防Render 30s idle timeout），直接跳过
          if (!trimmed.startsWith('data:')) continue;
          const jsonStr = trimmed.slice(5).trim();
          if (!jsonStr) continue;
          try {
            yield JSON.parse(jsonStr) as SseEvent;
          } catch (parseErr: any) {
            // malformed JSON：如果第一次遇到非 JSON 内容（例如 Werkzeug 返回的 HTML 错误页 / 裸 Python traceback），
            // 打一次日志，避免默默吞掉后读者最终只能看到"network error"无法定位
            if (!(parseSSE as any)._malformedWarned) {
              // eslint-disable-next-line no-console
              console.warn('[parseSSE] 收到非JSON data帧（可能是服务器返回HTML错误页/traceback），前200字样本：',
                jsonStr.slice(0, 200));
              (parseSSE as any)._malformedWarned = true;
            }
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

// ============================================================================
// 章节号解析（与后端 parse_chapter_number 口径对齐，写作/修改/去AI共用）
// ============================================================================
export function parseChapterNumber(title?: string): number | null {
  if (!title) return null;
  const t = title.trim();
  // 第N章/第N回/第N节...（取最后一个匹配，如 第3卷第5章 → 5）
  const suffix = '章节回卷部篇话集幕折更段讲课夜日年季场';
  const re1 = new RegExp(`第\\s*([0-9零一二三四五六七八九十百千万亿两〇]+)\\s*[${suffix}]`, 'g');
  const matches = [...t.matchAll(re1)].map(m => m[1]);
  if (matches.length) {
    const n = chineseToInt(matches[matches.length - 1]);
    if (n !== null) return n;
  }
  // Chapter N / Ch.N
  const m2 = t.match(/(?:chapter|ch|episode|ep)\.?\s*(\d+)/i);
  if (m2) return parseInt(m2[1]);
  // 行首数字 + 分隔
  const m3 = t.match(/^\s*(\d+)(?:\s*[\.、:：\-\)\]】，;；]|\s+|\s*(?=[\u4e00-\u9fffA-Za-z]))/);
  if (m3) {
    const rest = t.slice(m3[1].length);
    if (!rest || (rest[0] !== '年' && rest[0] !== '月' && rest[0] !== '日' && rest[0] !== '时' && rest[0] !== '分' && rest[0] !== '秒' && rest[0] !== '号')) {
      return parseInt(m3[1]);
    }
  }
  return null;
}

// 中文数字转 int（简化版，覆盖常见范围）
export function chineseToInt(s: string): number | null {
  if (/^\d+$/.test(s)) return parseInt(s);
  const digitMap: Record<string, number> = { '零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '百': 100, '千': 1000, '万': 10000, '亿': 100000000 };
  if (!s) return null;
  let total = 0, section = 0, num = 0;
  for (const ch of s) {
    const v = digitMap[ch];
    if (v === undefined) return null;
    if (v >= 10) {
      section = (num || 1) * v;
      total += section;
      num = 0;
      if (v >= 10000) { total = section; section = 0; }
    } else {
      num = v;
    }
  }
  return total + num;
}

// ============================================================================
// 章节显示格式化：避免「第{order_index}章」与「第N章」标题重复显示两次章节号
// - 若 title 开头就是合法 "第N章/回/卷…"（parseChapterNumber 能解出）→ 直接用 title
// - 否则用 order_index+1 兜底（内部 order_index 是 0-based）
// ============================================================================
export function formatChapterTitle(c: { order_index: number; title?: string | null }): string {
  const title = (c.title ?? '').trim();
  if (
    title &&
    parseChapterNumber(title) != null &&
    /^第\s*[0-9零一二三四五六七八九十百千万亿两〇]+\s*[章节回卷部篇话集幕折更段讲课夜日年季场]/.test(title)
  ) {
    return title;
  }
  const fallbackNum =
    typeof c.order_index === 'number' && !Number.isNaN(c.order_index)
      ? Math.max(1, c.order_index + 1)
      : 1;
  return `第${fallbackNum}章${title ? ' ' + title : ''}`;
}

// 统一口径：章节“显示章号”永远是 1-based
// - 若 title 能解析到合法章号（parseChapterNumber）→ 直接采用（如 title=第3章 → 3）
// - 否则 fallback：order_index + 1（内部 order_index 是 0-based）
// 任何 UI 章号显示、location 数字比对、消息栏 label 计算、下拉顺序都必须用这个函数。
export function displayChapterNum(c: { order_index: number; title?: string | null }): number {
  const n = parseChapterNumber(c.title ?? '');
  if (typeof n === 'number' && Number.isFinite(n) && n > 0) return Math.floor(n);
  const oi = Number(c.order_index);
  return Number.isFinite(oi) ? Math.max(1, oi + 1) : 1;
}

export function formatChapterOption(c: {
  order_index: number;
  title?: string | null;
  word_count?: number | null;
}): string {
  const head = formatChapterTitle(c);
  const wc = typeof c.word_count === 'number' ? c.word_count : 0;
  return `${head}（${wc}字）`;
}
