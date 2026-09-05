/**
 * lib/parsing.ts 单测（vitest）：
 * 覆盖 SSE 流解析（心跳帧/半截包/HTML错误页）与章节号口径（历史事故高发区）。
 * 运行：npm test
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  parseSSE,
  parseChapterNumber,
  chineseToInt,
  formatChapterTitle,
  displayChapterNum,
  formatChapterOption,
} from './parsing';

// ---------------------------------------------------------------------------
// 工具：把字符串分块伪装成 SSE Response（模拟网络分包：半截 JSON、跨块帧）
// ---------------------------------------------------------------------------
function mockResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  let i = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i++]));
      } else {
        controller.close();
      }
    },
  });
  return new Response(stream);
}

async function collect(gen: AsyncGenerator<any>): Promise<any[]> {
  const out: any[] = [];
  for await (const evt of gen) out.push(evt);
  return out;
}

beforeEach(() => {
  // 每个用例重置 malformed 告警标记，避免用例间互相污染
  (parseSSE as any)._malformedWarned = false;
  vi.spyOn(console, 'warn').mockImplementation(() => { /* 静音 */ });
});

// ---------------------------------------------------------------------------
// parseSSE
// ---------------------------------------------------------------------------
describe('parseSSE', () => {
  it('正常解析 delta/card/done 帧', async () => {
    const res = mockResponse([
      'data: {"type":"delta","content":"你好"}\n\n',
      'data: {"type":"card","card":{"id":"c1"},"session_id":"s1"}\n\n',
      'data: {"type":"done","session_id":"s1"}\n\n',
    ]);
    const events = await collect(parseSSE(res));
    expect(events).toEqual([
      { type: 'delta', content: '你好' },
      { type: 'card', card: { id: 'c1' }, session_id: 's1' },
      { type: 'done', session_id: 's1' },
    ]);
  });

  it('跳过心跳注释帧（冒号开头，防 Render 30s idle timeout）', async () => {
    const res = mockResponse([
      ': keep-alive\n\ndata: {"type":"delta","content":"A"}\n\n: hb\n\n',
    ]);
    const events = await collect(parseSSE(res));
    expect(events).toEqual([{ type: 'delta', content: 'A' }]);
  });

  it('跨块半截 JSON 能正确拼接（网络分包）', async () => {
    // 一个完整 SSE 帧被切成 3 个网络块，中间还有半截 JSON
    const res = mockResponse([
      'data: {"type":"del',
      'ta","content":"拼接成功"}',
      '\n\n',
    ]);
    const events = await collect(parseSSE(res));
    expect(events).toEqual([{ type: 'delta', content: '拼接成功' }]);
  });

  it('一个块内多行 data 依次 yield', async () => {
    const res = mockResponse([
      'data: {"type":"delta","content":"A"}\ndata: {"type":"delta","content":"B"}\n\n',
    ]);
    const events = await collect(parseSSE(res));
    expect(events).toEqual([
      { type: 'delta', content: 'A' },
      { type: 'delta', content: 'B' },
    ]);
  });

  it('malformed JSON（HTML错误页/traceback）不抛错、不产出事件，且只告警一次', async () => {
    const warnSpy = vi.spyOn(console, 'warn');
    const res = mockResponse([
      'data: <!DOCTYPE html><html><body>500 Internal Server Error</body></html>\n\n',
      'data: <pre>Traceback (most recent call last):</pre>\n\n',
    ]);
    const events = await collect(parseSSE(res));
    expect(events).toEqual([]); // 不炸流
    expect(warnSpy).toHaveBeenCalledTimes(1); // 第二次不再刷屏
  });

  it('空 body 不产出事件', async () => {
    const res = new Response(null);
    const events = await collect(parseSSE(res));
    expect(events).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// parseChapterNumber（与后端 parse_chapter_number 口径对齐）
// ---------------------------------------------------------------------------
describe('parseChapterNumber', () => {
  it('阿拉伯数字：第3章 → 3', () => {
    expect(parseChapterNumber('第3章 风起云涌')).toBe(3);
  });

  it('取最后一个匹配：第3卷第5章 → 5', () => {
    expect(parseChapterNumber('第3卷 第5章 破晓')).toBe(5);
  });

  it('中文数字：第十章 → 10', () => {
    expect(parseChapterNumber('第十章')).toBe(10);
  });

  it('中文数字：第二十三章 → 23', () => {
    expect(parseChapterNumber('第二十三章 夜袭')).toBe(23);
  });

  it('中文数字：第一百零五章 → 105', () => {
    expect(parseChapterNumber('第一百零五章')).toBe(105);
  });

  it('英文：Chapter 12 → 12 / ch.5 → 5 / EP3 → 3', () => {
    expect(parseChapterNumber('Chapter 12 - The Fall')).toBe(12);
    expect(parseChapterNumber('ch.5 伏笔')).toBe(5);
    expect(parseChapterNumber('EP3 转折')).toBe(3);
  });

  it('行首数字+分隔：`12、宴会` → 12', () => {
    expect(parseChapterNumber('12、宴会风波')).toBe(12);
  });

  it('日期语义不误判：`2024年大事记` → null', () => {
    expect(parseChapterNumber('2024年大事记')).toBeNull();
  });

  it('普通标题无章号 → null', () => {
    expect(parseChapterNumber('楔子')).toBeNull();
    expect(parseChapterNumber('')).toBeNull();
    expect(parseChapterNumber(undefined)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// chineseToInt
// ---------------------------------------------------------------------------
describe('chineseToInt', () => {
  it('纯数字串直接转', () => {
    expect(chineseToInt('42')).toBe(42);
  });
  it('常见中文数字', () => {
    expect(chineseToInt('九')).toBe(9);
    expect(chineseToInt('十')).toBe(10);
    expect(chineseToInt('两')).toBe(2);
    expect(chineseToInt('二十三')).toBe(23);
    expect(chineseToInt('一百')).toBe(100);
    expect(chineseToInt('一百零五')).toBe(105);
    expect(chineseToInt('三千')).toBe(3000);
  });
  it('非法字符返回 null', () => {
    expect(chineseToInt('abc')).toBeNull();
    expect(chineseToInt('')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// formatChapterTitle（防「第N章」与标题内章号重复显示）
// ---------------------------------------------------------------------------
describe('formatChapterTitle', () => {
  it('title 已带合法章号 → 原样返回，不重复加前缀', () => {
    expect(formatChapterTitle({ order_index: 2, title: '第3章 风起' })).toBe('第3章 风起');
  });

  it('title 无章号 → 用 order_index+1 兜底', () => {
    expect(formatChapterTitle({ order_index: 2, title: '风起' })).toBe('第3章 风起');
  });

  it('title 为空 → 只显示兜底章号', () => {
    expect(formatChapterTitle({ order_index: 0, title: '' })).toBe('第1章');
    expect(formatChapterTitle({ order_index: 4, title: null })).toBe('第5章');
  });
});

// ---------------------------------------------------------------------------
// displayChapterNum（1-based 统一口径）
// ---------------------------------------------------------------------------
describe('displayChapterNum', () => {
  it('title 能解析出章号 → 直接采用', () => {
    expect(displayChapterNum({ order_index: 0, title: '第5章' })).toBe(5);
  });

  it('title 无章号 → order_index+1（0-based→1-based）', () => {
    expect(displayChapterNum({ order_index: 0, title: '楔子' })).toBe(1);
    expect(displayChapterNum({ order_index: 4, title: null })).toBe(5);
  });

  it('异常输入兜底为 1', () => {
    expect(displayChapterNum({ order_index: NaN, title: '' })).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// formatChapterOption
// ---------------------------------------------------------------------------
describe('formatChapterOption', () => {
  it('下拉选项格式：标题（N字）', () => {
    expect(formatChapterOption({ order_index: 2, title: '第3章 风起', word_count: 1200 })).toBe('第3章 风起（1200字）');
  });

  it('word_count 缺失按 0 处理', () => {
    expect(formatChapterOption({ order_index: 0, title: '风起' })).toBe('第1章 风起（0字）');
  });
});
