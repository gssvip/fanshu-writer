import { useState, useEffect, useRef, useCallback, memo, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import rehypeHighlight from 'rehype-highlight';
import { useStore } from '../store';
import { api } from '../api';
import type { ActionCard, ProgressMap, AIMessage, SkillPack, BookBible, AIConfig } from '../types';
import CarLogo from './CarLogo';
// Q1：直接复用现有实体管理弹窗（跨维度重命名/合并），不再在 ChatPanel 里重复造"动作影响预览"轮子
import EntityRegistryModal from '../pages/EntityRegistryModal';
// P1-2 Markdown 增强：CDN 注入 KaTeX/Highlight.js 样式（避免 Vite 静态 import 缺失导致部署白屏）
(() => {
  if (typeof document === 'undefined') return;
  const K = 'https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css';
  const H = 'https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/github.min.css';
  [K, H].forEach(href => { if (!document.querySelector(`link[href="${href}"]`)) { const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = href; document.head.appendChild(l); } });
})();

const __BUILD_TAG__ = 'v3-0814';

// ============================================================================
// AI 智驾：四Tab（设定/正文/去AI/校审）统一创作平台
// 整合原 AI副驾 + AI总创作 + 章节AI创作 能力
// 手机优先：底部Tab栏 + 大按钮 + 紧凑输入区
// ============================================================================

type SmartTab = 'setting' | 'chapter' | 'deai' | 'review';

// 命中维度提示（通用聊天模式）类型定义
interface HitSuggestion {
  id?: string;
  dim: string; label: string; card_type: string; confidence: number;
  hits: string[]; suggested_title: string;
  quick_fill?: string;
}

// 维度定义（与后端 SMART_DIMENSIONS 对齐）
interface DimSpec {
  key: string; label: string; field: string; card: string; icon: string; hint: string;
}

// SSE 事件类型
type SseEvent =
  | { type: 'delta'; content: string; speaker?: string }
  | { type: 'card'; card: ActionCard; session_id: string; meta?: any }
  | { type: 'done'; session_id: string; summary?: string }
  | { type: 'speaker_done'; speaker?: string; round?: number }
  | { type: 'error'; error: string }
  | { type: 'meta'; kind: string; info?: any };

// SSE 流解析
async function* parseSSE(response: Response): AsyncGenerator<SseEvent> {
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
function parseChapterNumber(title?: string): number | null {
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
function chineseToInt(s: string): number | null {
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
function formatChapterTitle(c: { order_index: number; title?: string | null }): string {
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
function displayChapterNum(c: { order_index: number; title?: string | null }): number {
  const n = parseChapterNumber(c.title ?? '');
  if (typeof n === 'number' && Number.isFinite(n) && n > 0) return Math.floor(n);
  const oi = Number(c.order_index);
  return Number.isFinite(oi) ? Math.max(1, oi + 1) : 1;
}

function formatChapterOption(c: {
  order_index: number;
  title?: string | null;
  word_count?: number | null;
}): string {
  const head = formatChapterTitle(c);
  const wc = typeof c.word_count === 'number' ? c.word_count : 0;
  return `${head}（${wc}字）`;
}

// 判断维度是否走"方案选择"流程：与后端 SMART_DIMENSIONS 的 mode 字段对齐——
// suggest 型（构思/大纲/文风）是方向性选择，必须给多方案；
// direct 型（设定/世界观/人物/剧情/伏笔/地图）已由上游锁定方向，直接生成或修改即可，
// 不满意整体重新生成（后端 reroll：换展开角度，方向不变）。
function shouldShowSuggestions(dimKey: string | null, bible?: BookBible | null): boolean {
  if (!dimKey || dimKey === 'general') return false;
  void bible; // 依赖判断已由后端 direct 模式接管（构思已定→返回固定直生卡片），前端不再重复判断
  return ['concept', 'plot_design', 'style_guide', 'inventory'].includes(dimKey);
}

// ============================================================================
// Action Card 单卡渲染（采纳 / 编辑 / 忽略）
// ============================================================================
interface CardViewProps {
  card: ActionCard;
  onAdopt: (card: ActionCard) => void;
  onEdit: (card: ActionCard, newContent: string) => void;
  onIgnore: (card: ActionCard) => void;
  applying: boolean;
  onReplaceChapter?: (card: ActionCard, meta: any) => void;
  // 剧情维度专属：节点设计需要 bookId、写回 bible
  bookId?: string;
  bible?: any;
  onBibleUpdate?: (nextBible: any) => void;
  selectedSkillPackIds?: string[];
  chaptersPerVolume?: number;
}

const CARD_ICON: Record<string, string> = {
  SAVE_WORLDSETTING: '🌍', SAVE_CHARACTER: '👤', SAVE_FORESHADOW: '🔮',
  SAVE_OUTLINE_NODE: '📋', SAVE_PLOT: '📖', SAVE_LOCATION: '🗺️',
  SAVE_RULE: '⚙️', APPLY_STYLE: '✍️', SAVE_CONCEPT: '💡', SAVE_CHAPTER: '📚',
};

// 解析 timeline 文本为卷数组（容错：markdown 代码块、包装对象）
function _parseTimelineVols(content: string): any[] {
  if (!content) return [];
  let raw = content.trim();
  if (!raw) return [];
  // 剥离代码块围栏
  const fence = raw.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
  if (fence) raw = fence[1].trim();
  try {
    let parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      for (const k of ['volumes', 'data', 'result', 'items', 'list']) {
        if (Array.isArray((parsed as any)[k])) { parsed = (parsed as any)[k]; break; }
      }
    }
    if (Array.isArray(parsed)) return parsed;
  } catch { /* not json */ }
  return [];
}

// 剧情维度卡片 body：按卷可折叠、每卷显示概要/主要事件/节点，并提供「节点设计」按钮
const TimelineCardBody = memo(function TimelineCardBody({
  content, bookId, bible: _bible, onBibleUpdate, selectedSkillPackIds, chaptersPerVolume,
  onContentMutated,
}: {
  content: string;
  bookId?: string;
  bible?: any;
  onBibleUpdate?: (next: any) => void;
  selectedSkillPackIds?: string[];
  chaptersPerVolume?: number;
  // 当 nodes 被节点设计改写后，把新 content 回传给 ActionCardView（用于编辑/采纳的内容同步）
  onContentMutated?: (nextContent: string) => void;
}) {
  const [collapsed, setCollapsed] = useState<Record<number, boolean>>({});
  const [designing, setDesigning] = useState<number | null>(null);
  const vols = useMemo(() => _parseTimelineVols(content), [content]);
  const toggleVol = (idx: number) => setCollapsed(prev => ({ ...prev, [idx]: !prev[idx] }));

  // 点击某卷的「节点设计」：调用 api.aiOutlineVolume(node_only=true) → 用新 nodes 替换该卷并写回 Bible + 回传 content
  const handleDesignNodes = async (vol: any, idx: number) => {
    if (!bookId || !onBibleUpdate) {
      alert('缺少 bookId / bible 回调，无法进行节点设计');
      return;
    }
    const volIdx = vol.volume_index ?? vol.volume_id ?? (idx + 1);
    const volTitle = vol.volume || `第${volIdx}卷`;
    const vi_int = typeof volIdx === 'number' ? volIdx : parseInt(String(volIdx), 10) || (idx + 1);
    const hint = (vol.main_events?.length
      ? `检测到本卷《${volTitle}》已有 ${vol.main_events.length} 个主要剧情事件，将逐事件展开成 5-10 个子节点事件（总节点≈50-80个，满足${chaptersPerVolume || 50}章正文密度）。是否继续？`
      : `将基于《${volTitle}》已有卷剧情生成详细情节子节点。是否继续？`);
    if (!window.confirm(hint)) return;
    setDesigning(vi_int);
    try {
      const r = await api.aiOutlineVolume(bookId, vi_int, volTitle, selectedSkillPackIds || [], chaptersPerVolume || 50, true);
      // 后端返回 r.bible 是新 Bible（bb.timeline 已合并过 nodes）
      if (r?.bible) onBibleUpdate(r.bible);
      // 同时直接把最新 timeline 作为卡片内容，保证编辑/采纳看到的是最新 nodes 数据
      if (r?.timeline && onContentMutated) onContentMutated(r.timeline);
      const newNodesCount = r?.volume_data?.nodes?.length ?? 0;
      alert(`《${volTitle}》情节子节点事件设计完成！共生成 ${newNodesCount} 个子节点（覆盖约 ${chaptersPerVolume || 50} 章）`);
    } catch (e: any) {
      // 用户主动取消或超时取消 → 不显示"失败"红警，避免误导；取消状态UI上的loading也会在finally被清除
      const isCancelled = e?.name === 'AbortError' || e?.cancelled === true || String(e?.message || '') === '请求已取消';
      if (!isCancelled) {
        alert('节点设计失败：' + (e?.message || '请检查 AI 配置或稍候重试'));
      }
    } finally {
      setDesigning(null);
    }
  };

  if (vols.length === 0) {
    // 非 JSON 的普通文本：原样显示
    return <div className="chat-card-body">{content}</div>;
  }

  return (
    <div className="chat-card-body" style={{ padding: 8 }}>
      {vols.map((v: any, idx: number) => {
        const vIdx = v.volume_index ?? v.volume_id ?? (idx + 1);
        const vi_int = typeof vIdx === 'number' ? vIdx : parseInt(String(vIdx), 10) || idx + 1;
        const vTitle = v.volume || `第${vIdx}卷`;
        const isCollapsed = !!collapsed[idx];
        const main_events = Array.isArray(v.main_events) ? v.main_events : [];
        const nodes = Array.isArray(v.nodes) ? v.nodes : [];
        const designingThis = designing === vi_int;
        return (
          <div key={vIdx + '-' + idx} style={{
            marginBottom: 10,
            border: '1px solid #eaeaea',
            borderRadius: 8,
            overflow: 'hidden',
            background: '#fff',
          }}>
            <div
              onClick={() => toggleVol(idx)}
              style={{
                padding: '8px 10px',
                background: 'linear-gradient(90deg,#fafafa,#fff)',
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                fontSize: 13,
              }}
            >
              <span style={{ fontWeight: 600 }}>{vTitle}</span>
              {v.act && <span style={{ color: '#888', fontSize: 12 }}>[{v.act}]</span>}
              <span style={{ color: '#999', fontSize: 12 }}>
                · {main_events.length} 主要剧情事件 · {nodes.length} 节点
              </span>
              <div style={{ marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center' }} onClick={e => e.stopPropagation()}>
                <button
                  className="btn-ghost-sm"
                  disabled={designingThis || !bookId || !onBibleUpdate}
                  onClick={() => handleDesignNodes(v, idx)}
                  title="基于本卷主要剧情事件，AI 逐事件展开成 5-10 个详细情节子节点"
                  style={{ fontSize: 12 }}
                >
                  {designingThis ? '⏳ 节点设计中…' : '🎯 节点设计'}
                </button>
                <span style={{ fontSize: 12, color: '#999', minWidth: 48, textAlign: 'right' }}>
                  {isCollapsed ? '展开 ▼' : '收起 ▲'}
                </span>
              </div>
            </div>
            {!isCollapsed && (
              <div style={{ padding: '8px 12px', borderTop: '1px solid #f0f0f0', fontSize: 13 }}>
                {v.summary && (
                  <div style={{ marginBottom: 8 }}>
                    <div style={{ fontWeight: 600, color: '#374151', marginBottom: 2 }}>总体剧情概要</div>
                    <div style={{ color: '#4b5563', whiteSpace: 'pre-wrap' }}>{v.summary}</div>
                  </div>
                )}
                {v.main_plot && !v.summary && (
                  <div style={{ marginBottom: 8 }}>
                    <div style={{ fontWeight: 600, color: '#374151', marginBottom: 2 }}>主线剧情</div>
                    <div style={{ color: '#4b5563' }}>{v.main_plot}</div>
                  </div>
                )}

                {/* 卷级 6 要素展示 */}
                {(v.characters || v.timeline_anchor || v.location || v.realm_change || v.age_change) && (
                  <div style={{
                    margin: '6px 0 10px',
                    padding: 8,
                    borderRadius: 6,
                    background: 'linear-gradient(90deg,#f5f3ff,#faf5ff)',
                    border: '1px solid #ede9fe',
                  }}>
                    <div style={{ fontWeight: 600, color: '#5b21b6', marginBottom: 6 }}>📘 本卷 6 要素（节点阶段以这个为锚）</div>
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px 10px', fontSize: 12, color: '#4b5563' }}>
                      {v.characters && <div><b>人物：</b>{v.characters}</div>}
                      {v.timeline_anchor && <div><b>时间：</b>{v.timeline_anchor}</div>}
                      {v.location && <div><b>地点：</b>{v.location}</div>}
                      {v.realm_change && <div><b>境界变化：</b>{v.realm_change}</div>}
                      {v.age_change && <div style={{ gridColumn: '1 / -1' }}><b>年龄变化：</b>{v.age_change}</div>}
                    </div>
                  </div>
                )}

                {(v.core_conflict || v.ending_hook) && (
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 8 }}>
                    {v.core_conflict && (
                      <div>
                        <div style={{ fontWeight: 600, color: '#374151', marginBottom: 2 }}>核心冲突</div>
                        <div style={{ color: '#4b5563' }}>{v.core_conflict}</div>
                      </div>
                    )}
                    {v.ending_hook && (
                      <div>
                        <div style={{ fontWeight: 600, color: '#374151', marginBottom: 2 }}>卷尾钩子</div>
                        <div style={{ color: '#4b5563' }}>{v.ending_hook}</div>
                      </div>
                    )}
                  </div>
                )}

                {main_events.length > 0 && (
                  <div style={{ margin: '8px 0' }}>
                    <div style={{ fontWeight: 600, color: '#1f2937', marginBottom: 4 }}>
                      主要剧情事件（{main_events.length} 个，合计约{chaptersPerVolume || 50}章/12万字正文 · 事件层不含章节，由「节点设计」精确落章）
                    </div>
                    <ol style={{ paddingLeft: 22, margin: 0 }}>
                      {main_events.map((ev: any, ei: number) => (
                        <li key={ei} style={{ marginBottom: 10, padding: 8, background: '#fafafa', borderRadius: 6, border: '1px solid #f0f0f0' }}>
                          <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, flexWrap: 'wrap' }}>
                            <span style={{ fontWeight: 600, color: '#111827' }}>
                              事件{ev.index ?? (ei + 1)}《{ev.title || '未命名'}》
                            </span>
                            {typeof ev.estimated_chapters === 'number' && ev.estimated_chapters > 0 && (
                              <span style={{
                                color: '#dc2626',
                                fontSize: 12,
                                background: '#fef2f2',
                                border: '1px solid #fecaca',
                                padding: '1px 6px',
                                borderRadius: 4,
                                fontWeight: 500,
                              }}>预计支撑 {ev.estimated_chapters} 章</span>
                            )}
                          </div>
                          {ev.summary && <div style={{ color: '#1f2937', marginTop: 4, whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>{ev.summary}</div>}
                          {/* 事件级 6 要素 */}
                          {(ev.characters || ev.events || ev.time || ev.location || ev.realm_change || ev.age_change) && (
                            <div style={{
                              marginTop: 6,
                              padding: 6,
                              background: '#fffbeb',
                              border: '1px solid #fde68a',
                              borderRadius: 4,
                              fontSize: 12,
                              display: 'grid',
                              gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
                              gap: '4px 10px',
                              color: '#78350f',
                            }}>
                              {ev.characters && <div><b>人物：</b>{ev.characters}</div>}
                              {ev.events && <div><b>事件：</b>{ev.events}</div>}
                              {ev.time && <div><b>时间：</b>{ev.time}</div>}
                              {ev.location && <div><b>地点：</b>{ev.location}</div>}
                              {ev.realm_change && <div><b>境界：</b>{ev.realm_change}</div>}
                              {ev.age_change && <div><b>年龄/时程：</b>{ev.age_change}</div>}
                            </div>
                          )}
                          <div style={{ marginTop: 6, fontSize: 12, color: '#6b7280', display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                            {ev.bury && <span title="伏笔埋设（当前层按事件描述，精确章号在节点里）" style={{ color: '#c2410c' }}>🔸 埋：{ev.bury}</span>}
                            {ev.payoff && <span title="伏笔回收（当前层按事件描述，精确章号在节点里）" style={{ color: '#0369a1' }}>🔹 收：{ev.payoff}</span>}
                          </div>
                        </li>
                      ))}
                    </ol>
                  </div>
                )}

                {nodes.length > 0 && (() => {
                  // 按 main_event_index 分组展示，方便与父事件对照
                  const groups = new Map<number, any[]>();
                  nodes.forEach((n: any) => {
                    const k = Number(n.main_event_index) || 0;
                    if (!groups.has(k)) groups.set(k, []);
                    groups.get(k)!.push(n);
                  });
                  return (
                    <div style={{ margin: '10px 0' }}>
                      <div style={{ fontWeight: 600, color: '#1f2937', marginBottom: 4 }}>
                        情节子节点事件（共 {nodes.length} 个 · 已精确到章 · 可直接展开成章节正文）
                      </div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                        {Array.from(groups.entries()).sort((a, b) => a[0] - b[0]).map(([mei, list]) => (
                          <div key={mei} style={{
                            padding: 6,
                            background: mei ? '#eff6ff' : '#f9fafb',
                            border: '1px solid ' + (mei ? '#bfdbfe' : '#f3f4f6'),
                            borderRadius: 6,
                          }}>
                            {mei ? <div style={{ fontSize: 12, color: '#1d4ed8', fontWeight: 600, marginBottom: 4 }}>归属：主要剧情事件 E{mei}</div> : null}
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                              {list.map((n: any, ni: number) => (
                                <div key={ni} style={{
                                  padding: 8,
                                  background: '#ffffff',
                                  border: '1px solid #e5e7eb',
                                  borderRadius: 5,
                                  fontSize: 12,
                                }}>
                                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, flexWrap: 'wrap' }}>
                                    <span style={{ fontWeight: 600, color: '#111827' }}>
                                      N{n.index || (ni + 1)} {n.title}
                                    </span>
                                    {n.chapters && <span style={{ color: '#2563eb', fontWeight: 500 }}>📖 {n.chapters}</span>}
                                    {n.type && <span style={{ color: '#059669' }}>类型 {n.type}</span>}
                                    {n.cool_type && <span style={{ color: '#c2410c' }}>爽点 {n.cool_type}</span>}
                                    {n.cool_level && <span style={{ color: '#7c3aed' }}>{n.cool_level}</span>}
                                  </div>
                                  {/* 节点 6 要素 */}
                                  {(n.characters || n.events || n.time || n.location || n.realm_change || n.age_change) && (
                                    <div style={{
                                      marginTop: 4,
                                      padding: 6,
                                      background: '#ecfeff',
                                      border: '1px solid #a5f3fc',
                                      borderRadius: 4,
                                      fontSize: 12,
                                      color: '#155e75',
                                      display: 'grid',
                                      gridTemplateColumns: 'repeat(2, minmax(0,1fr))',
                                      gap: '4px 10px',
                                    }}>
                                      {n.characters && <div><b>人物：</b>{n.characters}</div>}
                                      {n.events && <div><b>事件：</b>{n.events}</div>}
                                      {n.time && <div><b>时间：</b>{n.time}</div>}
                                      {n.location && <div><b>地点：</b>{n.location}</div>}
                                      {n.realm_change && <div><b>境界：</b>{n.realm_change}</div>}
                                      {n.age_change && <div><b>年龄/时程：</b>{n.age_change}</div>}
                                    </div>
                                  )}
                                  {n.summary && <div style={{ color: '#374151', marginTop: 4, whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>{n.summary}</div>}
                                  <div style={{ marginTop: 6, color: '#6b7280', display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                                    {n.bury && <span style={{ color: '#c2410c' }} title="伏笔埋设（精确到章）">🔸 埋：{n.bury}</span>}
                                    {n.payoff && <span style={{ color: '#0369a1' }} title="伏笔回收（精确到章）">🔹 收：{n.payoff}</span>}
                                    {n.hook && <span>🪝 钩子：{n.hook}</span>}
                                  </div>
                                </div>
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  );
                })()}

                {nodes.length === 0 && (
                  <div style={{ color: '#9ca3af', fontSize: 12, padding: 6 }}>
                    尚未生成详细情节子节点事件。点击右上角「🎯 节点设计」，按每个主要剧情事件展开成 5-10 个子节点事件，子节点会补全所涉章节 + 精确到章的伏笔埋收 + 节点 6 要素。
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
});

// 已落地卡片（adopted/edited）：默认折叠，点击展开查看内容
const AdoptedCardCollapsed = memo(function AdoptedCardCollapsed({ card }: { card: ActionCard }) {
  const [expanded, setExpanded] = useState(false);
  const wc = (card.content || '').length;
  return (
    <div className="chat-card chat-card-adopted">
      <div
        className="chat-card-head"
        onClick={() => setExpanded(e => !e)}
        style={{ cursor: 'pointer' }}
      >
        <span className="chat-card-icon">{CARD_ICON[card.type] || '📌'}</span>
        <span className="chat-card-title">{card.title}</span>
        <span className="chat-card-status">✓ 已落地 · {card.target}{wc > 0 ? ` · ${wc}字` : ''}</span>
        <span className="chat-card-toggle" style={{ marginLeft: 'auto', fontSize: 12, color: '#999' }}>
          {expanded ? '收起 ▲' : '展开 ▼'}
        </span>
      </div>
      {expanded && <div className="chat-card-body">{card.content}</div>}
    </div>
  );
});

const ActionCardView = memo(function ActionCardView(props: CardViewProps) {
  const { card, onAdopt, onEdit, onIgnore, applying, onReplaceChapter,
    bookId, bible, onBibleUpdate, selectedSkillPackIds, chaptersPerVolume } = props;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(card.content);
  // node 设计后卡片内容会被 TimelineCardBody 异步改写，draft / content 都要同步
  const handleContentMutated = (nextContent: string) => {
    setDraft(nextContent);
    // 注：不直接 onEdit，留给用户在 UI 上点击"采纳/编辑后覆盖"来决定是否正式落盘。
    // 但 card.content 要同步更新，否则 TimelineCardBody 重渲染时会回到旧内容：
    try { card.content = nextContent; } catch { /* frozen */ }
  };
  const status = card.status || 'pending';
  const cardMeta = (card as any).__meta as any;
  const isReplaceMode = !!(onReplaceChapter && cardMeta?.replace && cardMeta?.chapter_id);
  const validation: any[] = cardMeta?.validation || [];
  const hasError = validation.some(v => v.severity === 'error');
  // 剧情维度：card.target==='剧情' 或 card.type==='SAVE_PLOT' 或 card.type==='SAVE_OUTLINE_NODE'
  const isTimelineCard = !!(
    card.target === '剧情' ||
    card.type === 'SAVE_PLOT' ||
    card.type === 'SAVE_OUTLINE_NODE'
  );

  const handleSaveEdit = () => {
    if (!draft.trim()) return;
    onEdit({ ...card, content: draft.trim(), status: 'edited' }, draft.trim());
    setEditing(false);
  };

  if (status === 'ignored') {
    return (
      <div className="chat-card chat-card-ignored">
        <div className="chat-card-head">
          <span className="chat-card-icon">{CARD_ICON[card.type] || '📌'}</span>
          <span className="chat-card-title">{card.title}</span>
          <span className="chat-card-status">已忽略</span>
        </div>
      </div>
    );
  }

  if (status === 'adopted' || status === 'edited') {
    return <AdoptedCardCollapsed card={card} />;
  }

  // 剧情卡片 adopted / edited 折叠态的内容 body：若能解析 timeline，仍然用 TimelineCardBody 美化展示
  const renderBody = (bodyContent: string) => {
    if (isTimelineCard) {
      return (
        <TimelineCardBody
          content={bodyContent}
          bookId={bookId}
          bible={bible}
          onBibleUpdate={onBibleUpdate}
          selectedSkillPackIds={selectedSkillPackIds}
          chaptersPerVolume={chaptersPerVolume}
          onContentMutated={handleContentMutated}
        />
      );
    }
    return <div className="chat-card-body">{bodyContent}</div>;
  };

  return (
    <div className="chat-card">
      <div className="chat-card-head">
        <span className="chat-card-icon">{CARD_ICON[card.type] || '📌'}</span>
        <span className="chat-card-title">{card.title}</span>
        <span className="chat-card-target">→ {card.target}</span>
      </div>
      {validation.length > 0 && (
        <div style={{
          padding: '6px 10px',
          margin: '6px 0',
          borderRadius: 6,
          fontSize: 12,
          background: hasError ? '#fef2f2' : '#fffbeb',
          border: `1px solid ${hasError ? '#fecaca' : '#fde68a'}`,
          color: hasError ? '#b91c1c' : '#92400e',
        }}>
          {hasError && <div style={{ fontWeight: 600, marginBottom: 4 }}>⚠ 自检未通过（已自动重试）</div>}
          {validation.map((v, i) => (
            <div key={i} style={{ marginTop: 2 }}>
              <span style={{ opacity: 0.7 }}>[{v.severity}]</span>{' '}
              <span style={{ fontWeight: 500 }}>{v.code}</span>: {v.message}
            </div>
          ))}
        </div>
      )}
      {editing ? (
        <>
          <textarea
            className="chat-card-edit"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            rows={Math.min(16, Math.max(4, draft.split('\n').length))}
          />
          <div className="chat-card-actions">
            <button className="chat-card-btn primary" onClick={handleSaveEdit} disabled={!draft.trim()}>
              保存并落地
            </button>
            <button className="chat-card-btn" onClick={() => { setEditing(false); setDraft(card.content); }}>
              取消
            </button>
          </div>
        </>
      ) : (
        <>
          {renderBody(draft || card.content)}
          <div className="chat-card-actions">
            {isReplaceMode ? (
              <button className="chat-card-btn primary" onClick={() => onReplaceChapter!(card, cardMeta)} disabled={applying}>
                {applying ? '替换中…' : '替换本章正文'}
              </button>
            ) : (
              <button className="chat-card-btn primary" onClick={() => onAdopt({ ...card, content: draft || card.content })} disabled={applying}>
                {applying ? '落地中…' : (card.type === 'SAVE_CHAPTER' ? '采纳(覆盖同章)' : '采纳(追加)')}
              </button>
            )}
            <button className="chat-card-btn" onClick={() => setEditing(true)} disabled={applying}>
              编辑后覆盖
            </button>
            <button className="chat-card-btn ghost" onClick={() => onIgnore(card)} disabled={applying}>
              忽略
            </button>
          </div>
        </>
      )}
    </div>
  );
});

// ============================================================================
// 进度地图（设定Tab可展开）
// ============================================================================
const ProgressMapView = memo(function ProgressMapView({ progress, onClose }: { progress: ProgressMap | null; onClose?: () => void }) {
  if (!progress) return null;
  const statusLabel: Record<string, string> = { empty: '未开始', sketch: '草稿', partial: '进行中', solid: '已完善' };
  const statusColor: Record<string, string> = { empty: '#999', sketch: '#d97706', partial: '#2563eb', solid: '#16a34a' };
  return (
    <div className="chat-progress">
      <div className="chat-progress-head">
        <span>创作进度 · {progress.overall}%</span>
        <span className="chat-progress-count">{progress.filled}/{progress.total} 维度完善</span>
        {onClose && <button className="chat-progress-close" onClick={onClose}>×</button>}
      </div>
      <div className="chat-progress-bar">
        <div className="chat-progress-fill" style={{ width: `${progress.overall}%` }} />
      </div>
      {progress.next_step && (
        <div className="chat-progress-next">
          <strong>下一步建议：</strong>{progress.next_step.label}
          <div className="chat-progress-hint">{progress.next_step.hint}</div>
        </div>
      )}
      <div className="chat-progress-dims">
        {progress.dims.map(d => (
          <div key={d.field} className="chat-progress-dim">
            <div className="chat-progress-dim-head">
              <span>{d.label}</span>
              <span style={{ color: statusColor[d.status], fontSize: 12 }}>{statusLabel[d.status]}</span>
            </div>
            <div className="chat-progress-dim-bar">
              <div style={{ width: `${d.pct}%`, background: statusColor[d.status] }} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
});

// ============================================================================
// 消息气泡（长按菜单 + 超两行折叠）
// ============================================================================
interface MessageBubbleProps {
  message: AIMessage;
  index: number;
  onAdopt: (c: ActionCard) => void;
  onEdit: (c: ActionCard, content: string) => void;
  onIgnore: (c: ActionCard) => void;
  applyingCardId: string | null;
  streaming: boolean;
  onReplaceChapter?: (card: ActionCard, meta: any) => void;
  onEditMessage?: (index: number, newContent: string) => void;
  onDeleteMessage?: (index: number) => void;
  onRegenerate?: (index: number) => void;
  // 剧情卡片节点设计专用
  bookId?: string;
  bible?: any;
  onBibleUpdate?: (next: any) => void;
  selectedSkillPackIds?: string[];
  chaptersPerVolume?: number;
}

// 长按计时器
const LONG_PRESS_MS = 500;

const MessageBubble = memo(function MessageBubble({ message, index, onAdopt, onEdit, onIgnore, applyingCardId, streaming, onReplaceChapter, onEditMessage, onDeleteMessage, onRegenerate, bookId, bible, onBibleUpdate, selectedSkillPackIds, chaptersPerVolume }: MessageBubbleProps) {
  const isUser = message.role === 'user';
  const isRoundtable = !isUser && message.roundtable !== undefined;
  const [collapsed, setCollapsed] = useState(true);
  const [showMenu, setShowMenu] = useState(false);
  const [editing, setEditing] = useState(false);
  const [showReasoning, setShowReasoning] = useState(false);
  const [draft, setDraft] = useState(message.content);
  const [copied, setCopied] = useState(false);
  const pressTimer = useRef<number | null>(null);
  const movedRef = useRef(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const copyTimer = useRef<number | null>(null);

  // 判断是否需要折叠：内容超过两行（约80字或多行）
  const contentLines = (message.content || '').split('\n');
  const isLong = message.content.length > 80 || contentLines.length > 2;
  const showCollapsed = isLong && collapsed && !streaming;

  // 长按开始
  const startPress = () => {
    movedRef.current = false;
    if (pressTimer.current) window.clearTimeout(pressTimer.current);
    pressTimer.current = window.setTimeout(() => {
      if (!movedRef.current) setShowMenu(true);
    }, LONG_PRESS_MS);
  };
  const cancelPress = () => {
    if (pressTimer.current) { window.clearTimeout(pressTimer.current); pressTimer.current = null; }
  };
  const onMove = () => { movedRef.current = true; cancelPress(); };

  // 点击外部关闭菜单
  useEffect(() => {
    if (!showMenu) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setShowMenu(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [showMenu]);

  // 卸载时清理复制提示计时器
  useEffect(() => {
    return () => { if (copyTimer.current) window.clearTimeout(copyTimer.current); };
  }, []);

  const handleSaveEdit = () => {
    if (onEditMessage && draft.trim() !== message.content) {
      onEditMessage(index, draft.trim());
    }
    setEditing(false);
  };

  // 复制消息内容到剪贴板
  const handleCopy = async () => {
    const text = (message.content || '').trim();
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // 降级方案：用 textarea + execCommand
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch { /* ignore */ }
      document.body.removeChild(ta);
    }
    setCopied(true);
    if (copyTimer.current) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div
      className={`chat-msg ${isUser ? 'chat-msg-user' : 'chat-msg-ai'}`}
      onTouchStart={startPress}
      onTouchEnd={cancelPress}
      onTouchMove={onMove}
      onMouseDown={startPress}
      onMouseUp={cancelPress}
      onMouseLeave={cancelPress}
      onMouseMove={onMove}
      style={{ position: 'relative' }}
    >
      <div className="chat-msg-avatar">{isUser ? '我' : <CarLogo size={22} />}</div>
      <div className="chat-msg-body">
        {isRoundtable && (
          <div className="rt-box">
            <div className="rt-header">
              <span className="rt-title">🪑 圆桌会议</span>
              {message.roundtable?.status === 'open' && <span className="rt-live">● 进行中</span>}
              {message.roundtable?.status === 'done' && <span className="rt-done">✓ 已结束</span>}
            </div>
            {message.roundtable?.speech && message.roundtable.speech.length > 0 ? (
              <div className="rt-discussion">
                {message.roundtable.speech.map((seg, si) => (
                  <div key={si} className={(message.roundtable as any).currentSpeaker === seg.name && message.roundtable?.status === 'open' ? 'rt-seg speaking' : 'rt-seg'}>
                    <div className="rt-name">{seg.name || seg.speaker}</div>
                    <div className="rt-bubble">
                      {seg.content ? (
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm, remarkMath]}
                          rehypePlugins={[rehypeKatex, [rehypeHighlight, { detect: true, ignoreMissing: true }]]}
                          components={{ a: (p: any) => <a {...p} target="_blank" rel="noopener noreferrer" /> }}
                        >{seg.content}</ReactMarkdown>
                      ) : streaming && (message.roundtable as any).currentSpeaker === seg.name ? (
                        <span className="chat-cursor">▋</span>
                      ) : null}
                    </div>
                  </div>
                ))}
                {streaming && <div className="rt-waiting">🔊 {message.roundtable?.currentSpeaker || '某位'} 正在发言…</div>}
              </div>
            ) : streaming ? (
              <div className="rt-waiting">🪑 会议即将开始，各位专家正在入座…</div>
            ) : null}
          </div>
        )}
        {editing ? (
          <div className="chat-msg-edit-wrap">
            <textarea
              className="chat-msg-edit"
              value={draft}
              onChange={e => setDraft(e.target.value)}
              rows={Math.min(12, Math.max(3, draft.split('\n').length))}
              autoFocus
            />
            <div className="chat-msg-edit-actions">
              <button className="chat-card-btn primary" onClick={handleSaveEdit}>保存</button>
              <button className="chat-card-btn" onClick={() => { setEditing(false); setDraft(message.content); }}>取消</button>
            </div>
          </div>
        ) : message.content ? (
          <div
            className={`chat-msg-text chat-msg-markdown ${showCollapsed ? 'chat-msg-collapsed' : ''}`}
            onClick={() => { if (isLong && !streaming) setCollapsed(c => !c); }}
          >
            <ReactMarkdown
              remarkPlugins={[remarkGfm, remarkMath]}
              rehypePlugins={[rehypeKatex, [rehypeHighlight, { detect: true, ignoreMissing: true }]]}
              components={{
                a: (p: any) => <a {...p} target="_blank" rel="noopener noreferrer" />,
                img: (p: any) => <img {...p} loading="lazy" style={{ maxWidth: '100%', borderRadius: 8 }} />,
                table: (p: any) => <div style={{ overflowX: 'auto' }}><table {...p} /></div>,
                pre: (p: any) => <pre style={{ background: '#f6f7fb', padding: 10, borderRadius: 8, overflowX: 'auto', fontSize: 13, fontFamily: 'ui-monospace, Menlo, Consolas, monospace' }} {...p} />,
                code: (p: any) => {
                  const { className, inline, children, ...rest } = p || ({} as any);
                  if (inline) return <code style={{ background: '#eef0f6', padding: '1px 5px', borderRadius: 4, fontSize: 13, fontFamily: 'ui-monospace, Menlo, Consolas, monospace' }} className={className} {...rest}>{children}</code>;
                  return <code className={className} {...rest}>{children}</code>;
                },
              }}
            >
              {message.content}
            </ReactMarkdown>
            {streaming && <span className="chat-cursor">▋</span>}
          </div>
        ) : streaming ? (
          <div className="chat-msg-text"><span className="chat-cursor">▋</span></div>
        ) : null}
        {/* 【思考过程】可切换展示：独立于正文，不参与复制/采纳 */}
        {!isUser && message.reasoning && message.reasoning.trim() ? (
          <div className="chat-msg-reasoning">
            <button
              className="chat-msg-reasoning-toggle"
              onClick={() => setShowReasoning(s => !s)}
              title={showReasoning ? '收起思考过程' : '查看模型的思考过程'}
            >
              <span className={`chat-msg-reasoning-chev ${showReasoning ? 'open' : ''}`}>▸</span>
              {showReasoning ? '收起' : '思考过程'} {`(${message.reasoning.length}字)`}
            </button>
            {showReasoning && (
              <div className="chat-msg-reasoning-content">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm, remarkMath]}
                  rehypePlugins={[rehypeKatex]}
                >{message.reasoning}</ReactMarkdown>
              </div>
            )}
          </div>
        ) : null}
        {showCollapsed && <button className="chat-msg-expand" onClick={(e) => { e.stopPropagation(); setCollapsed(false); }}>展开全文 ▼</button>}
        {/* 消息操作栏：复制 / 重新生成 / 删除（流式生成中隐藏） */}
        {!streaming && !editing && (message.content || '') && (
          <div className={`chat-msg-actions ${isUser ? 'chat-msg-actions-user' : ''}`}>
            <button
              className="chat-msg-action-btn"
              onClick={handleCopy}
              title="复制"
            >{copied ? '✓ 已复制' : '📋 复制'}</button>
            {onRegenerate && (
              <button
                className="chat-msg-action-btn"
                onClick={() => onRegenerate(index)}
                title="重新生成"
              >🔄 重新生成</button>
            )}
            {onDeleteMessage && (
              <button
                className="chat-msg-action-btn danger"
                onClick={() => {
                  if (window.confirm('确定删除这条消息？')) onDeleteMessage(index);
                }}
                title="删除"
              >🗑️ 删除</button>
            )}
          </div>
        )}
            {message.cards && message.cards.length > 0 && (
          <div className="chat-msg-cards">
            {message.cards.map((c, idx) => (
              <ActionCardView
                key={c.id || idx}
                card={c}
                onAdopt={onAdopt}
                onEdit={onEdit}
                onIgnore={onIgnore}
                applying={applyingCardId === c.id}
                onReplaceChapter={onReplaceChapter}
                bookId={bookId}
                bible={bible}
                onBibleUpdate={onBibleUpdate}
                selectedSkillPackIds={selectedSkillPackIds}
                chaptersPerVolume={chaptersPerVolume}
              />
            ))}
          </div>
        )}
      </div>
      {showMenu && (
        <div className="chat-msg-menu" ref={menuRef}>
          <button className="chat-msg-menu-item" onClick={() => { handleCopy(); setShowMenu(false); }}>📋 复制</button>
          {onRegenerate && <button className="chat-msg-menu-item" onClick={() => { onRegenerate(index); setShowMenu(false); }}>🔄 重新生成</button>}
          {onDeleteMessage && <button className="chat-msg-menu-item danger" onClick={() => { if (window.confirm('确定删除这条消息？')) onDeleteMessage(index); setShowMenu(false); }}>🗑️ 删除</button>}
        </div>
      )}
    </div>
  );
});

// ============================================================================
// 技能包选择器（精简版，按 category 分组）
// ============================================================================
function SkillPackSelector({ packs, selected, onToggle, compact, onPreview }: {
  packs: SkillPack[];
  selected: string[];
  onToggle: (id: string) => void;
  compact?: boolean;
  onPreview?: (pack: SkillPack) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  if (packs.length === 0) return null;
  const selectedCount = selected.length;
  return (
    <div className={`smart-skill-selector ${compact ? 'compact' : ''}`}>
      <button className="smart-skill-toggle" onClick={() => setExpanded(e => !e)}>
        📦 技能包 {selectedCount > 0 && <span className="smart-skill-badge">{selectedCount}</span>}
        <span className="smart-skill-arrow">{expanded ? '▲' : '▼'}</span>
      </button>
      {expanded && (
        <div className="smart-skill-list">
          {packs.map(p => (
            <label
              key={p.id}
              className={`smart-skill-item ${selected.includes(p.id) ? 'checked' : ''}`}
              onDoubleClick={(e) => { e.preventDefault(); onPreview?.(p); }}
              title={onPreview ? '单击勾选 · 双击预览' : '单击勾选'}
            >
              <input
                type="checkbox"
                checked={selected.includes(p.id)}
                onChange={() => onToggle(p.id)}
              />
              <span className="smart-skill-icon">{p.icon || '📦'}</span>
              <span className="smart-skill-name">{p.name}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

// ============================================================================
// 通用聊天·顶部"助手选择器"（折叠式，视觉对齐技能包；含联网搜索 Key 配置）
//   7款内置助手单选 + "🌐 联网搜索 Key"配置表单，全部做成可折叠列表
// ============================================================================
function GeneralAssistantSelector({ roles, currentId, onSelect }: {
  roles: readonly { id: string; name: string; emoji: string; brief: string }[];
  currentId: string;
  onSelect: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [cfgOpen, setCfgOpen] = useState(false);
  const [draft, setDraft] = useState<{ tavily: string; exa: string; brave: string }>({ tavily: '', exa: '', brave: '' });
  const [state, setState] = useState<{ keys: Record<string, string>; env: Record<string, boolean> } | null>(null);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState('');
  const curRole = roles.find(r => r.id === currentId) || roles[0];

  const loadCfg = async () => {
    try {
      const d = await api.getSearchConfig();
      setState(d);
      setMsg('');
    } catch (e: any) {
      setState({ keys: {}, env: {} });
      setMsg('读取配置失败：' + (e?.message || String(e)));
    }
  };
  const saveCfg = async () => {
    setSaving(true);
    try {
      const r = await api.saveSearchConfig({ tavily: draft.tavily.trim(), exa: draft.exa.trim(), brave: draft.brave.trim() });
      setMsg(`✅ 已保存（${Object.entries(r.updated).filter(([, v]) => v).map(([k]) => k).join(' / ') || '已清空'}）`);
      setDraft({ tavily: '', exa: '', brave: '' });
      loadCfg();
    } catch (e: any) {
      setMsg('❌ 保存失败：' + (e?.message || String(e)));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="smart-skill-selector compact" data-gt-assistant-selector>
      <button
        className="smart-skill-toggle"
        data-gt-assistant-toggle
        onClick={(e) => { e.stopPropagation(); setOpen(o => !o); }}
        title="切换助手（7款内置角色）· 下方可配置联网搜索 Key"
      >
        👤 <span style={{ fontWeight: 600 }}>{curRole.emoji}{curRole.name}</span>
        <span className="smart-skill-arrow">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div className="smart-skill-list" data-gt-assistant-popover
          onClick={(e) => { e.stopPropagation(); }}
          style={{ maxHeight: cfgOpen ? 'none' : 260, overflowY: 'auto', zIndex: 102, position: 'relative' }}
        >
          {roles.map(r => {
            const active = currentId === r.id;
            return (
              <div
                key={r.id}
                className={`smart-skill-item ${active ? 'checked' : ''}`}
                onClick={() => { onSelect(r.id); }}
                title={r.brief}
                style={{ cursor: 'pointer', opacity: 1 }}
              >
                <span className="smart-skill-icon">{r.emoji}</span>
                <span className="smart-skill-name" style={{ color: active ? '#c25e00' : '#333' }}>{r.name}</span>
                {active && <span style={{ marginLeft: 'auto', color: '#e97b00', fontSize: 12 }}>当前</span>}
              </div>
            );
          })}
          {/* 分隔：联网搜索 Key 配置 */}
          <div
            style={{ margin: '6px 2px 0', borderTop: '1px solid #eee', padding: '6px 2px 0', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
            data-gt-searchcfg-toggle
            onClick={(e) => { e.stopPropagation(); setCfgOpen(true); loadCfg(); }}
          >
            <span style={{ fontSize: 12, fontWeight: 600, color: '#0a7d4f' }}>🌐 联网搜索 Key</span>
            <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 999, background: '#eafaf3', color: '#0a7d4f' }}>设置 ›</span>
          </div>
        </div>
      )}

      {/* 🌐 联网搜索 Key 配置浮层（fixed 浮层，规避父容器裁剪；独立成层，点击必响应） */}
      {cfgOpen && (
        <div
          data-gt-searchcfg-popover
          onClick={(e) => { e.stopPropagation(); }}
          style={{
            position: 'fixed', left: '50%', top: '50%', transform: 'translate(-50%, -50%)', zIndex: 400,
            width: Math.min(360, typeof window !== 'undefined' ? window.innerWidth - 40 : 360), maxHeight: '80vh', overflowY: 'auto',
            background: '#fff', borderRadius: 14, padding: 14,
            border: '1px solid #e0e0ea', boxShadow: '0 10px 40px rgba(0,0,0,0.18)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <span style={{ fontWeight: 700, fontSize: 14 }}>🌐 联网搜索 Key</span>
            <button
              onClick={(e) => { e.stopPropagation(); setCfgOpen(false); }}
              style={{ border: 'none', background: '#f2f2f5', borderRadius: 8, width: 26, height: 26, cursor: 'pointer', fontSize: 13, color: '#666' }}
            >✕</button>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {(['tavily', 'exa', 'brave'] as const).map(k => (
              <label key={k} style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                <span style={{ fontSize: 12, fontWeight: 600, color: '#555' }}>{k === 'tavily' ? 'Tavily' : k === 'exa' ? 'Exa' : 'Brave Search'}
                  <span style={{ color: '#aaa', fontWeight: 400, marginLeft: 6 }}>{state?.keys[k] ? '（已保存）' : '（未配置）'}</span>
                </span>
                <input
                  type="password"
                  placeholder="填写 API Key；留空则不修改"
                  value={draft[k]}
                  onChange={(e) => setDraft(d => ({ ...d, [k]: e.target.value }))}
                  style={{ fontSize: 12, padding: '7px 9px', border: '1px solid #d9d9e4', borderRadius: 8, outline: 'none' }}
                />
              </label>
            ))}
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <button
                onClick={(e) => { e.stopPropagation(); saveCfg(); }}
                disabled={saving}
                style={{ fontSize: 12, padding: '7px 16px', borderRadius: 8, border: 'none', background: '#0a7d4f', color: '#fff', cursor: 'pointer' }}
              >{saving ? '保存中…' : '保存'}</button>
              <span style={{ fontSize: 11, color: msg.startsWith('✅') ? '#288f2b' : msg.startsWith('❌') ? '#c32e2e' : '#888' }}>{msg}</span>
            </div>
            <div style={{ fontSize: 11, color: '#aaa', lineHeight: 1.6 }}>
              不填也能联网（DuckDuckGo 兜底）；填 Tavily/Exa/Brave 任一生效，结果更稳更准。保存后立即生效。
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// 主组件：AI 智驾
// ============================================================================
export default function ChatPanel() {
  const { chatPanelOpen, chatPanelBookId, chatPanelSessionId, chatPanelPresetTab, chatPanelPresetInput, chatPanelPresetFixTasks, closeChatPanel } = useStore() as any;
  const setChatPanelSessionId = useStore((s: any) => s.setChatPanelSessionId) as (id: string | null) => void;
  const [activeTab, setActiveTab] = useState<SmartTab>('setting');
  // 手机端折叠：折叠"设定"Tab 的维度按钮三排（通用/构思/设定/世界观 + 大纲/剧情/人物/伏笔 + 助手选择器），
  // 为手机端输入/阅读区域留出更多空间
  const [dimRowsCollapsed, setDimRowsCollapsed] = useState(false);
  const [messages, setMessages] = useState<AIMessage[]>([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [streamError, setStreamError] = useState('');
  const [progress, setProgress] = useState<ProgressMap | null>(null);
  const [showProgress, setShowProgress] = useState(false);
  const [applyingCardId, setApplyingCardId] = useState<string | null>(null);
  const [historySessions, setHistorySessions] = useState<Array<{ id: string; title: string; updated_at: string | null; message_count: number }>>([]);
  const [showHistory, setShowHistory] = useState(false);

  // 智驾专属状态
  const [dimensions, setDimensions] = useState<DimSpec[]>([]);
  const [selectedDim, setSelectedDim] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<Array<{ id: string; title: string; preview: string; _from_user?: boolean; _full_content?: string }>>([]);
  const [selectedSuggestion, setSelectedSuggestion] = useState<{ id: string; title: string; preview: string; _from_user?: boolean; _full_content?: string } | null>(null);
  const [loadingSuggest, setLoadingSuggest] = useState(false);
  const [bible, setBible] = useState<BookBible | null>(null);
  const [skillPacks, setSkillPacks] = useState<SkillPack[]>([]);
  // 【需求1-2：ChatPanel双击预览】技能包预览Modal
  const [previewPack, setPreviewPack] = useState<SkillPack | null>(null);
  // 各 Tab 独立的技能包选择（切换 Tab 互不干扰）
  const [settingPacks, setSettingPacks] = useState<string[]>([]);
  const [chapterPacks, setChapterPacks] = useState<string[]>([]);
  const [deaiPacks_selected, setDeaiPacksSelected] = useState<string[]>([]);
  const [latestChapter, setLatestChapter] = useState<{ id: string; title: string; order_index: number; word_count: number; status: string } | null>(null);
  const [nextChapterNum, setNextChapterNum] = useState(1);
  const [chapters, setChapters] = useState<Array<{ id: string; title: string; order_index: number; word_count: number; status: string }>>([]);
  const [volumes, setVolumes] = useState<Array<{ id: string; title: string; order_index: number; chapter_count: number }>>([]);
  // 正文/去AI/校审 各自的章节选择（独立）
  const [deaiTargetId, setDeaiTargetId] = useState<string | null>(null);         // 正文Tab：修改目标章节
  const [chapterTargetId, setChapterTargetId] = useState<string | null>(null);
  // 正文章节修改：两步确认态（首次点"修改"仅进入待执行，再点才真正工作）
  const [chapterEditArmed, setChapterEditArmed] = useState(false);
  const [reviewChapterId, setReviewChapterId] = useState<string | null>(null);   // 校审Tab：一致性检查章节
  const [reviewVolumeIds, setReviewVolumeIds] = useState<string[]>([]);          // 校审Tab：按卷检查
  // B：两步确认机制——防止用户点了按钮就直接跑全书，避免误操作浪费 token
  //   Step 1: 首次点击 = 选中"待跑模式"，按钮提示"再点确认 + 显示当前范围"
  //   Step 2: 二次点击 = 真正执行
  //   用户换 scope 下拉（换卷/换章）→ 自动出确认态，避免按旧范围误跑
  const [reviewArmedMode, setReviewArmedMode] = useState<'anti_forget' | 'consistency' | null>(null);
  // 自动上下文命中提示：meta 事件告知已定位并注入的章节/维度
  const [autoContextNotice, setAutoContextNotice] = useState<{ chapters: Array<{ id: string; title: string }>; dims: Array<{ key: string; label: string }> } | null>(null);
  const [reviewing, setReviewing] = useState(false);
  // 修正任务清单（从防遗忘报告违规项带入，支持多章/多维度连续修正并追踪进度）
  const [fixTasks, setFixTasks] = useState<Array<{ location: string; desc: string; fix: string; severity?: string; dimKey?: string; done?: boolean; chapterId?: string | null }>>([]);
  // Q1：实体管理弹窗（复用 WritePage 里已有的 EntityRegistryModal）
  const [showEntityRegistry, setShowEntityRegistry] = useState(false);
  // Q2：系统优化建议（从顶栏移入「校审」Tab） + 新交互：使用说明/采纳/忽略/自定义编辑
  const [optimizationReport, setOptimizationReport] = useState<{
    ready: boolean; failure_count: number; suggestions: Array<any>;
    how_to_use?: any; applied_patches?: Array<any>; applied_patch_count?: number;
    active_patch_preview?: string; reason?: string; ignored_bucket_count?: number;
  } | null>(null);
  const [optBusyBucket, setOptBusyBucket] = useState<string | null>(null);
  // 自定义编辑：open=true 时在卡片里显示 textarea，改完点"保存后采纳"
  const [editingBucket, setEditingBucket] = useState<string | null>(null);
  const [editingPatch, setEditingPatch] = useState('');
  // 已采纳/忽略的 bucket 前端乐观 UI 隐藏（避免等 refresh 才消失）
  const [locallyDismissedBuckets, setLocallyDismissedBuckets] = useState<Set<string>>(new Set());
  // 系统学习面板折叠（默认收起 → 只占一行高度）
  const [showOptReport, setShowOptReport] = useState(false);

  // ──────── 通用对话Tab专属state（命中维度气泡） ────────
  // 命中维度提示气泡：接收到 hit_suggestions meta 时弹出
  const [hitSuggestionPopups, setHitSuggestionPopups] = useState<Array<{
    id: string; msg_index: number; suggestions: HitSuggestion[];
  }>>([]);
  // 命中气泡「📦入库」按钮落卡loading：key=popId+dim，避免重复点/多按钮同时落卡
  const [applyingHitKey, setApplyingHitKey] = useState<string | null>(null);
  // Q2 合并：事件日志重算（原先在工具栏浮层，现在合并进「校审」Tab 子面板）
  // 通用聊天专用会话ID（与设定Tab其他维度session隔离，避免串session/记忆丢失）
  // 根因：原代码传sessionId:undefined→后端每次新建会话→聊天记忆完全丢失；若复用全局sessionId，会跟其他维度（构思/设定/正文创作）会话互相覆盖导致混乱
  const [chatGeneralSessionId, setChatGeneralSessionId] = useState<string | null>(null);
  // P1-1 会话级切模型：从「我的」复用AIConfig列表，点选后仅对当前通用会话生效（不改全局激活）
  const [aiConfigList, setAiConfigList] = useState<AIConfig[]>([]);
  // 每个会话独立记忆上次选的模型：sessionId -> aiConfigId
  const [sessionModelMap, setSessionModelMap] = useState<Record<string, string>>({});
  const [showModelPicker, setShowModelPicker] = useState(false);
  // P1-3 内置角色 persona：6款常用人格（仅通用聊天生效），选中后发送给后端作为system persona前缀
  const BUILTIN_ROLES = [
    { id: 'default',    name: '默认助手', emoji: '🧠', brief: '正常智驾回答，不附加人格' },
    { id: 'roundtable', name: '圆桌会议', emoji: '🪑', brief: '6位专家Agent围坐开会，两轮轮流发言后产出总结报告' },
    { id: 'polish',     name: '润色编辑', emoji: '✍️', brief: '擅长文字润色，指出语病、节奏、结构问题，给出具体改写对比' },
    { id: 'toxic_critic', name: '毒舌读者', emoji: '🔥', brief: '极度挑剔的读者视角，不留情面，专挑AI味和套路化' },
    { id: 'architect',  name: '剧情架构师', emoji: '🧱', brief: '擅长分卷结构、张力曲线、伏笔回收、CDL角色三角' },
    { id: 'worldbuilder', name: '世界观策划', emoji: '🗺️', brief: '擅长能量体系、势力地图、科技树/修炼树、经济体系自洽' },
    { id: 'marketeer',  name: '爆款编辑', emoji: '📈', brief: '从书名/一句话梗/前3章钩子的工业化爆款视角把关' },
    { id: 'interviewer', name: '深度采访', emoji: '🎙️', brief: '连续追问直到挖透设定矛盾和人物动机，擅长逼出冰山' },
  ] as const;
  // 通用聊天每个会话独立记住上次选的角色：sessionId -> roleId
  const [sessionRoleMap, setSessionRoleMap] = useState<Record<string, string>>({});
  // P0-4 通用聊天工具栏开关：联网搜索🔍 / 深度思考🧠（深度思考有档位：0=关 1=标准 2=深度）
  const [generalWebSearch, setGeneralWebSearch] = useState(false);
  const [generalDeepThink, setGeneralDeepThink] = useState<number>(0);
  const [deepThinkOpen, setDeepThinkOpen] = useState(false);
  // P1-4 Silly Tavern 角色卡导入：隐藏的 file input ref + 解析工具
  const stCharCardRef = useRef<HTMLInputElement | null>(null);
  const [stImportMsg, setStImportMsg] = useState<string>('');  // 导入完成后的提示文本
  const [showBackfill, setShowBackfill] = useState(false);
  const [backfillLLM, setBackfillLLM] = useState<'auto' | 'always' | 'never'>('auto');
  const [backfillRunning, setBackfillRunning] = useState(false);
  const [backfillStatus, setBackfillStatus] = useState<{
    text: string;
    progress?: { done: number; total: number; added: number; llm: number; rule: number };
    log: string[];
  } | null>(null);
  // Q2 合并：系统优化建议（原先 showOptimizationReport 浮层 → 合并进校审 Tab，不需要独立浮层开关了）

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const streamBufferRef = useRef<string>('');
  // 思考过程累积缓冲：后端以 meta(kind=reasoning) 单独推送（不混正文），前端累计到当前 AI 气泡可展开展示
  const reasoningBufferRef = useRef<string>('');
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // 追踪正在修改的章节（用于修改完成后自动标记任务清单 done）
  const polishingChapterIdRef = useRef<string | null>(null);
  // 追踪正在修正的设定维度（设定Tab修正完成后自动标记任务清单 done）
  const fixingDimKeyRef = useRef<string | null>(null);

  const bookId = chatPanelBookId;

  // P1-1 会话级切模型：加载AIConfig列表（供模型chip下拉用）
  useEffect(() => {
    api.listAIConfigs().then((res) => {
      if (res && Array.isArray(res.configs)) setAiConfigList(res.configs);
    }).catch(() => {});
  }, []);

  // 【P0-5 通用工具栏浮层】模型 + 深度思考档位 两个浮层的点击外部关闭（capture 阶段，白名单）
  useEffect(() => {
    if (!showModelPicker && !deepThinkOpen) return;
    function handler(e: Event) {
      const path = e.composedPath ? e.composedPath() : [e.target as any];
      const within = (s: string) =>
        Array.from(document.querySelectorAll<HTMLElement>(s)).some(el => path.includes(el));
      if (within('[data-gt-model-chip]') || within('[data-gt-model-popover]')) return;
      if (within('[data-gt-deeppicker-chip]') || within('[data-gt-deeppicker-popover]')) return;
      setShowModelPicker(false);
      setDeepThinkOpen(false);
    }
    document.addEventListener('click', handler, true);
    return () => document.removeEventListener('click', handler, true);
  }, [showModelPicker, deepThinkOpen]);

  // 提示1：助手切换已上移到"通用顶部"，浮层选择器已删除（底部一排改为：模型/联网搜索/深度思考/导入角色卡）

  // 自动滚动
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, streaming]);

  // 加载进度（同时刷新 bible + book对象，避免修改时拿到旧内容）—— 【技能包生效关键】：
  // 每次打开面板/刷新进度，都拉一次 Book 对象，把三组 skill_ids 回填到 settingPacks/chapterPacks/deaiPacks_selected state
  //  （因为 ChatPanel 不在 WritePage 内部，没有直接监听 book.master_style_review ids）
  const refreshProgress = useCallback(async () => {
    if (!bookId) return;
    try {
      const p = await api.getProgressMap(bookId);
      setProgress(p);
    } catch { /* ignore */ }
    try {
      const b = await api.getBible(bookId);
      setBible(b);
    } catch { /* ignore */ }
    try {
      const r = await api.getOptimizationReport(bookId);
      setOptimizationReport(r);
    } catch { /* ignore */ }
    // 【三组技能包回填】：从Book表读取 master/style/review 三组 ids，保证与 WritePage 勾选状态一致
    try {
      const bk = await api.getBook(bookId);
      if (bk) {
        setSettingPacks(Array.isArray(bk.master_skill_ids) ? bk.master_skill_ids : []);
        setChapterPacks(Array.isArray(bk.style_skill_ids) ? bk.style_skill_ids : []);
        setDeaiPacksSelected(Array.isArray(bk.review_skill_ids) ? bk.review_skill_ids : []);
      }
    } catch { /* ignore */ }
  }, [bookId]);

  // P1-3: 事件全文重算（支持 JSON 与 SSE 两种模式）
  // 位置：原工具栏浮层，Q2 合并后进入「校审」Tab 的子面板。这里只保留函数本体不变。
  const runBackfill = useCallback(async () => {
    if (!bookId) return;
    setBackfillRunning(true);
    const log: string[] = [];
    const pushLog = (line: string) => log.push(`[${new Date().toLocaleTimeString()}] ${line}`);
    setBackfillStatus({ text: '准备请求…', log });
    try {
      // 走与 api.ts request() 完全相同的地址策略
      const { getApiBaseUrl } = await import('../api');
      const apiBase = getApiBaseUrl().replace(/\/+$/, '');
      const resp = await fetch(`${apiBase}/ai/smart/backfill-eventlog`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ book_id: bookId, use_llm: backfillLLM }),
      });
      const ct = resp.headers.get('content-type') || '';
      pushLog(`响应 content-type=${ct}`);
      if (!ct.includes('text/event-stream')) {
        const data = await resp.json();
        if (data.error) throw new Error(data.error);
        pushLog(`重算完成（非流式）：事件+${data.added_total} / LLM ${data.llm_count} / 正则 ${data.rule_count}`);
        setBackfillStatus({
          text: `✅ 完成：共 ${data.total} 章，新增事件 ${data.added_total} 条（LLM=${data.llm_count}章，正则=${data.rule_count}章）`,
          progress: { done: data.total, total: data.total, added: data.added_total, llm: data.llm_count, rule: data.rule_count },
          log,
        });
      } else {
        // SSE 流：逐行读 data: ... JSON
        const reader = resp.body?.getReader();
        if (!reader) throw new Error('流不可读');
        const decoder = new TextDecoder('utf-8');
        let buf = '';
        let total = 0;
        let added = 0;
        let llmDone = 0;
        let ruleDone = 0;
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const events = buf.split('\n\n');
          buf = events.pop() || '';
          for (const evRaw of events) {
            if (!evRaw.startsWith('data:')) continue;
            const payload = JSON.parse(evRaw.slice(5).trim());
            if (payload.type === 'start') {
              total = payload.total;
              pushLog(`流式任务启动：共 ${total} 章，策略=${payload.use_llm}`);
              setBackfillStatus({ text: `🏃 流式启动：${total} 章…`, progress: { done: 0, total, added: 0, llm: 0, rule: 0 }, log });
            } else if (payload.type === 'progress') {
              added = payload.added_so_far;
              llmDone = payload.llm_so_far;
              ruleDone = payload.rule_so_far;
              pushLog(`进度 ${payload.done}/${total}：事件+${added}`);
              setBackfillStatus({
                text: `🏃 流式运行中：${payload.done}/${total} 章`,
                progress: { done: payload.done, total, added, llm: llmDone, rule: ruleDone },
                log: [...log],
              });
            } else if (payload.type === 'warn') {
              pushLog(`⚠️ 第${payload.chapter}章${payload.title}失败：${payload.error}`);
            } else if (payload.type === 'done') {
              added = payload.added_total;
              llmDone = payload.llm_count;
              ruleDone = payload.rule_count;
              pushLog(`✅ 完成：共 ${payload.total} 章，新增事件 ${added} 条（LLM=${llmDone}章，正则=${ruleDone}章）`);
              setBackfillStatus({
                text: `✅ 完成：共 ${payload.total} 章，新增事件 ${added} 条（LLM=${llmDone}章，正则=${ruleDone}章）`,
                progress: { done: payload.total, total: payload.total, added, llm: llmDone, rule: ruleDone },
                log: [...log],
              });
            } else if (payload.type === 'error') {
              pushLog(`❌ 错误：${payload.error}`);
              setBackfillStatus({ text: `❌ 失败：${payload.error}`, log: [...log] });
            }
          }
        }
      }
    } catch (e: any) {
      pushLog(`❌ 异常：${e?.message || e}`);
      setBackfillStatus({ text: `❌ 失败：${e?.message || e}`, log });
    } finally {
      setBackfillRunning(false);
      // 触发进度条刷新（EventLog 变了）
      try { window.dispatchEvent(new CustomEvent('fanshu:progress-needs-refresh')); } catch {}
    }
  }, [bookId, backfillLLM]);

  const refreshHistory = useCallback(async () => {
    if (!bookId) return;
    try {
      const r = await api.listBookChatSessions(bookId);
      setHistorySessions(r.sessions || []);
    } catch { /* ignore */ }
  }, [bookId]);

  // 面板打开时加载基础元数据（维度、技能包、章节、卷等）——只要面板开着且有书就加载，不受会话影响
  useEffect(() => {
    if (!chatPanelOpen || !bookId) return;
    refreshProgress();
    refreshHistory();
    api.smartDimensions().then(r => setDimensions(r.dimensions || [])).catch(() => {});
    api.listSkillPacks().then(all => setSkillPacks(all || [])).catch(() => {});
    api.smartChapters(bookId).then(r => setChapters(r.chapters || [])).catch(() => {});
    api.smartVolumes(bookId).then(r => setVolumes(r.volumes || [])).catch(() => {});
    api.smartLatestChapter(bookId).then(r => {
      setLatestChapter(r.latest);
      setNextChapterNum(r.next_chapter_num);
    }).catch(() => {});
  }, [chatPanelOpen, bookId, refreshProgress, refreshHistory]);

  // 监听创作进度需要刷新事件（在各维度编辑面板保存后派发）
  useEffect(() => {
    const handler = () => {
      if (chatPanelOpen && bookId) {
        refreshProgress();
      }
    };
    window.addEventListener('fanshu:progress-needs-refresh', handler);
    return () => window.removeEventListener('fanshu:progress-needs-refresh', handler);
  }, [chatPanelOpen, bookId, refreshProgress]);

  // 加载聊天会话：优先 chatPanelSessionId 指定的，否则最新
  // 用 ref 记录上次已加载的 sessionId，避免 sessionId 同步回 store 后重复加载导致消息闪烁
  const loadedSessionRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (!chatPanelOpen || !bookId) return;
    // 仅在会话 id 变化时加载（避免相同 session 下因其他 state 抖动重复加载消息）
    if (loadedSessionRef.current === chatPanelSessionId) return;
    loadedSessionRef.current = chatPanelSessionId;
    (async () => {
      try {
        const r = await api.listBookChatSessions(bookId);
        const sessions = r.sessions || [];
        let target = null;
        if (chatPanelSessionId) {
          target = sessions.find((s: any) => s.id === chatPanelSessionId) || null;
        }
        if (!target && sessions.length > 0) {
          target = sessions[0];
        }
        if (target) {
          setSessionId(target.id);
          const msgR = await api.getChatSessionMessages(target.id);
          const loaded: AIMessage[] = (msgR.messages || []).map((m: any) => ({
            role: m.role || 'assistant',
            content: m.content || '',
            cards: Array.isArray(m.cards) ? m.cards : undefined,
          }));
          setMessages(loaded);
        } else {
          setMessages([]);
          setSessionId(null);
        }
      } catch {
        setMessages([]);
      }
    })();
  }, [chatPanelOpen, bookId, chatPanelSessionId]);

  // 应用预设：从其它入口（如「修正正文」）跳转进来时切到指定 Tab 并预填输入框/任务清单
  useEffect(() => {
    if (!chatPanelOpen) return;
    if (chatPanelPresetTab) setActiveTab(chatPanelPresetTab);
    if (chatPanelPresetInput) setInput(chatPanelPresetInput);
    if (Array.isArray(chatPanelPresetFixTasks) && chatPanelPresetFixTasks.length > 0) {
      setFixTasks(chatPanelPresetFixTasks.map((t: any) => ({
        location: String(t?.location ?? ''),
        desc: String(t?.desc ?? ''),
        fix: String(t?.fix ?? ''),
        severity: t?.severity,
        dimKey: t?.dimKey ?? null,
        done: false,
        chapterId: t?.chapterId ?? null,
      })));
    } else {
      // 防x.some is not a function：保证fixTasks始终是数组（外部store传null/object/string时重置为[]）
      setFixTasks([]);
    }
  }, [chatPanelOpen, chatPanelPresetTab, chatPanelPresetInput, chatPanelPresetFixTasks]);

  // 把本地 sessionId 同步回 store，供修正入口复用同一会话（首次新建后后续复用）
  // 同步后立即更新 loadedSessionRef，避免 store 变化触发上面的加载 effect 重复加载当前会话
  useEffect(() => {
    setChatPanelSessionId(sessionId);
    loadedSessionRef.current = sessionId;
  }, [sessionId, setChatPanelSessionId]);

  // 章节加载后，为 fixTasks 匹配对应 chapterId（按 location 中的章号/标题匹配）
  useEffect(() => {
    if (!Array.isArray(fixTasks) || fixTasks.length === 0 || chapters.length === 0) return;
    setFixTasks(prev => Array.isArray(prev) ? prev.map(t => {
      if (t.chapterId) return t; // 已匹配过的跳过
      const numMatch = t.location.match(/第?\s*(\d+)\s*章/);
      let ch = null;
      if (numMatch) {
        const num = parseInt(numMatch[1], 10);
        if (Number.isFinite(num) && num > 0) {
          // 统一口径：用 displayChapterNum（1-based）比对，防止 order_index 0-based 与章号混用
          ch = chapters.find(c => displayChapterNum(c) === num) || null;
        }
      }
      if (!ch) {
        // 回退：用 location 文本模糊匹配标题
        const loc = t.location.replace(/^第?\s*\d+\s*章\s*/, '').trim();
        if (loc) ch = chapters.find(c => c.title.includes(loc)) || null;
      }
      return { ...t, chapterId: ch ? ch.id : null };
    }) : prev);
  }, [chapters, fixTasks.length]);

  // 修改完成后自动标记对应任务为 done（streaming 从 true→false 且有记录的章节/维度）
  useEffect(() => {
    if (streaming) return;
    const chId = polishingChapterIdRef.current;
    const dimKey = fixingDimKeyRef.current;
    if (!chId && !dimKey) return;
    if (!Array.isArray(fixTasks) || fixTasks.length === 0) return;
    polishingChapterIdRef.current = null;
    fixingDimKeyRef.current = null;
    setFixTasks(prev => Array.isArray(prev) ? prev.map(t => {
      if (t.done) return t;
      if (chId && t.chapterId === chId) return { ...t, done: true };
      if (dimKey && t.dimKey === dimKey) return { ...t, done: true };
      return t;
    }) : prev);
  }, [streaming, fixTasks.length]);

  // 设定维度匹配：根据任务 location/desc 文本推断对应维度 key（仅未匹配 dimKey 的任务）
  useEffect(() => {
    if (!Array.isArray(fixTasks) || fixTasks.length === 0) return;
    setFixTasks(prev => Array.isArray(prev) ? prev.map(t => {
      if (t.dimKey) return t; // 已匹配过的跳过
      const text = `${t.location || ''} ${t.desc || ''} ${t.fix || ''}`;
      let dimKey: string | undefined;
      if (/人物|角色|主角|配角|性格|外貌|背景故事/.test(text)) dimKey = 'character_profiles';
      else if (/伏笔|铺垫|预示|回收|埋下/.test(text)) dimKey = 'foreshadowing';
      else if (/世界观|世界设定|地理|国家|大陆|城邦|历史/.test(text)) dimKey = 'worldbuilding';
      else if (/规则|能力|体系|科技|魔法|修炼|等级/.test(text)) dimKey = 'key_rules';
      else if (/剧情|大纲|情节|主线|支线|转折|高潮/.test(text)) dimKey = 'plot_design';
      else if (/时间线|时间|年代|顺序|先后/.test(text)) dimKey = 'timeline';
      else if (/构思|概念|主题|卖点|核心/.test(text)) dimKey = 'concept';
      else dimKey = 'general';
      return { ...t, dimKey };
    }) : prev);
  }, [fixTasks.length]);

  // 正文写作默认要求（切到正文Tab且输入框为空/为旧默认值时自动填入）
  const CHAPTER_DEFAULT_INPUT =
    '接上一章剧情，读取剧情维度里的「本章剧情节点」，保证ONE主钩子贯穿本章。禁止无目标流水账。';

  // 切换 Tab 时清空选中章节（各Tab独立）；切到正文Tab时填入默认写作要求
  const switchTab = useCallback((tab: SmartTab) => {
    setActiveTab(tab);
    // 离开正文Tab 时清掉"待修改"状态，避免 armed 泄漏到其他 Tab
    if (tab !== 'chapter') setChapterEditArmed(false);
    setInput(prev => {
      // 进入正文Tab：若输入为空或是正文默认值，填入默认要求；已有自定义内容则保留
      if (tab === 'chapter' && (!prev.trim() || prev.trim() === CHAPTER_DEFAULT_INPUT)) {
        return CHAPTER_DEFAULT_INPUT;
      }
      // 离开正文Tab：若仍是默认值则清空，避免带到其他Tab
      if (tab !== 'chapter' && prev.trim() === CHAPTER_DEFAULT_INPUT) {
        return '';
      }
      return prev;
    });
  }, []);

  // 各 Tab 独立的技能包切换
  // 【技能包生效关键】：点击切换时不仅改本地 state，还要调用 updateBook 持久化到 Book 表对应字段
  //   （这样下次打开 ChatPanel / 跳到 WritePage 都会看到相同勾选状态；后端 merge 也会自动取到）
  const toggleSkillPack = useCallback((tab: SmartTab, id: string) => {
    const setter = tab === 'setting' ? setSettingPacks
      : tab === 'chapter' ? setChapterPacks
      : setDeaiPacksSelected;  // 'deai' | 'review'
    setter(prev => {
      const next = prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id];
      if (bookId) {
        const fieldMap: Record<SmartTab, 'master_skill_ids' | 'style_skill_ids' | 'review_skill_ids'> = {
          setting: 'master_skill_ids',
          chapter: 'style_skill_ids',
          deai: 'review_skill_ids',
          review: 'review_skill_ids',
        };
        api.updateBook(bookId, { [fieldMap[tab]]: next } as any).catch(() => {});
      }
      return next;
    });
  }, [bookId]);

  // 关闭时取消流并重置会话加载标记（下次打开重新加载）
  useEffect(() => {
    if (!chatPanelOpen && abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
      loadedSessionRef.current = undefined;
    }
  }, [chatPanelOpen]);

  // 公共：消费 SSE 流
  // - onMeta: 可选扩展 meta 事件回调（通用聊天用：命中维度提示气泡）
  //          不传则沿用默认行为（向后兼容），不破坏其他4个调用点
  // - ignoreCards: 可选=true时跳过所有{type:card}帧（只展示内容、不显示采纳卡片）
  // - onSessionId: 可选=传入后，card/done帧的session_id只调此回调（更新调用方自己的会话ID），不再调全局setSessionId，避免不同调用链路之间串session
  const consumeSSE = useCallback(async (res: Response, ctrl: AbortController, onCardMeta?: (card: ActionCard, meta: any) => void, onMeta?: (kind: string, info: any) => void, ignoreCards = false, onSessionId?: (sid: string) => void) => {
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: `请求失败 (HTTP ${res.status})` }));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    let receivedSessionId = sessionId;
    let gotPayload = false; // 收到过 delta/card（空回复兜底用）
    // 追加（或整体替换）流式缓冲区文本并刷新 AI 气泡：依赖警告/自检重试/多轮尝试提示共用
    const pushNote = (text: string, replace = false) => {
      streamBufferRef.current = replace ? text : streamBufferRef.current + text;
      const buf = streamBufferRef.current;
      setMessages(prev => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === 'assistant') next[next.length - 1] = { ...last, content: buf };
        return next;
      });
    };
    for await (const evt of parseSSE(res)) {
      if (ctrl.signal.aborted) break;
      if (evt.type === 'meta') {
        if (evt.kind === 'auto_context' && evt.info) {
          setAutoContextNotice({
            chapters: Array.isArray(evt.info.chapters) ? evt.info.chapters : [],
            dims: Array.isArray(evt.info.dims) ? evt.info.dims : [],
          });
        }
        if (evt.kind === 'dependency_warning' && evt.info?.warning) {
          pushNote(`\n\n> ⚠ ${evt.info.warning}\n\n`); // 前置维度未完善提示
        }
        if (evt.kind === 'validation_retry' && evt.info) {
          const errs = (evt.info.issues || []).filter((v: any) => v.severity === 'error');
          if (errs.length) {
            pushNote(`\n\n> 🔄 首版自检未通过（${errs.map((v: any) => `${v.code}: ${v.message}`).join('；')}），正在带反馈重试…\n\n`);
          }
        }
        // 【聊天截断修复】服务端重试前清空上一轮半截内容，避免多轮输出拼接成"截断消息"
        if (evt.kind === 'attempt_reset' && evt.info) {
          const why = evt.info.reason ? `：${evt.info.reason}` : '';
          pushNote(`> 🔄 第${evt.info.attempt}/${evt.info.max_attempts || '?'}次尝试${why}\n\n`, true);
        }
        // 【P0-6 SSE中流续连】后端遇到503/断流后自动续接，前端显示"已续连N次"提示
        if (evt.kind === 'stream_retry' && evt.info) {
          const attempt = Number(evt.info.attempt) || 1;
          const reason = String(evt.info.reason || '断流');
          const chars = Number(evt.info.continued_chars) || 0;
          pushNote(`\n\n> 🔄 已自动续连（第${attempt}次 · 原因：${reason} · 已保存${chars}字，无缝续写中…）\n\n`);
        }
        // 【真联网搜索】后端触发搜索时先显示"搜索中"，完成后显示引擎+命中条数
        if (evt.kind === 'web_search_started' && evt.info) {
          const q = String(evt.info.query || '').slice(0, 80);
          pushNote(`\n\n> 🔍 正在联网搜索：${q}${q.length >= 80 ? '…' : ''}\n\n`);
        }
        if (evt.kind === 'web_search_done' && evt.info) {
          const ok = !!evt.info.ok;
          const engine = String(evt.info.engine || '').toUpperCase();
          const count = Number(evt.info.count) || 0;
          const ms = Number(evt.info.latency_ms) || 0;
          const err = String(evt.info.error || '');
          if (ok) {
            pushNote(`\n\n> ✅ 联网搜索完成（${engine || '未知引擎'} · ${count}条 · ${ms}ms）\n\n`);
          } else {
            const hint = err ? `：${err.slice(0, 60)}` : '（使用本地知识库兜底）';
            pushNote(`\n\n> ⚠ 联网搜索未命中${hint}\n\n`);
          }
        }
        // 【新思考过程展示】后端单独推的 reasoning meta帧 → 累积到当前 AI 气泡的可展开区（不混正文，
        // 因此复制、卡片、落盘都不会带上思考内容；思考帧也不计入 gotPayload 的"正文空回复"判定）
        if (evt.kind === 'reasoning' && evt.info && typeof evt.info.text === 'string' && evt.info.text) {
          reasoningBufferRef.current += evt.info.text;
          const rbuf = reasoningBufferRef.current;
          setMessages(prev => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last && last.role === 'assistant') next[next.length - 1] = { ...last, reasoning: rbuf };
            return next;
          });
        }
        // 【扩展钩子】如果传了 onMeta，把 meta 事件也转交外部处理（命中维度气泡）
        if (typeof onMeta === 'function') {
          try { onMeta(evt.kind || '', evt.info || null); } catch {}
        }
      } else if (evt.type === 'delta') {
        gotPayload = true;
        pushNote(evt.content);
      } else if (evt.type === 'card') {
        if (ignoreCards) continue;
        gotPayload = true;
        if (evt.session_id && !receivedSessionId) {
          receivedSessionId = evt.session_id;
          if (typeof onSessionId === 'function') {
            onSessionId(evt.session_id); // 调用方私有sessionId更新（通用聊天），不污染全局
          } else {
            setSessionId(evt.session_id); // 原全局逻辑（其他调用点保持不变）
          }
        }
        if (evt.meta && onCardMeta) {
          onCardMeta({ ...evt.card, status: 'pending' }, evt.meta);
        }
        // 【P0改进】把 meta（含 validation 校验结果）注入 card.__meta，供 ActionCardView 展示
        const cardWithMeta = { ...evt.card, status: 'pending' } as any;
        if (evt.meta) {
          (cardWithMeta as any).__meta = { ...((cardWithMeta as any).__meta || {}), ...evt.meta };
        }
        setMessages(prev => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === 'assistant') {
            next[next.length - 1] = { ...last, cards: [...(last.cards || []), cardWithMeta] };
          }
          return next;
        });
      } else if (evt.type === 'done') {
        if (evt.session_id) {
          receivedSessionId = evt.session_id;
          if (typeof onSessionId === 'function') {
            onSessionId(evt.session_id); // 调用方私有sessionId更新（通用聊天），不污染全局
          } else {
            setSessionId(evt.session_id); // 原全局逻辑
          }
        }
      } else if (evt.type === 'error') {
        gotPayload = true; // 收到 error 帧也算"有响应"，避免被下方空回复兜底覆盖真实错误
        throw new Error(evt.error);
      }
    }
    // 【空回复兜底】流正常结束但一帧正文/卡片都没有 → 显式报错（替代静默空消息/被移除的气泡）
    if (!gotPayload && !ctrl.signal.aborted) {
      throw new Error('AI 未返回任何内容（模型空回复或连接中断），请重试或检查模型配置');
    }
  }, [sessionId]);

  // 追加用户+AI占位消息
  const appendUserAi = useCallback((userText: string) => {
    setMessages(prev => [...prev,
      { role: 'user', content: userText },
      { role: 'assistant', content: '', cards: [] },
    ]);
  }, []);

  // 移除空AI占位
  const removeEmptyAi = useCallback(() => {
    setMessages(prev => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.role === 'assistant' && !last.content && !(last.cards || []).length) {
        next.pop();
      }
      return next;
    });
  }, []);

  // 只追加用户消息（不带空AI占位）：用于"选定方案"这类AI不需要立即回复的场景，
  // 避免留下永远填不上内容的空AI气泡（空内容+非流式会渲染成空，看起来像"AI截断不回复"）
  const appendUser = useCallback((userText: string) => {
    setMessages(prev => [...prev, { role: 'user', content: userText }]);
  }, []);

  // 追加一条静态AI提示消息：让"选定方案→等用户补充意见"阶段聊天区仍有活的反馈
  const appendAiNotice = useCallback((text: string) => {
    setMessages(prev => [...prev, { role: 'assistant', content: text, cards: [] }]);
  }, []);

  // ========== 设定Tab：人机协作流 ==========

  // 1. 提需求 → AI给多选意见
  const handleSuggest = useCallback(async () => {
    const text = input.trim();
    if (!bookId || !selectedDim || streaming) return;
    // 仅方案选择型维度才走多选意见流程
    if (!shouldShowSuggestions(selectedDim, bible)) return;
    const dimStatus = progress?.dims.find(d => d.field === selectedDim)?.status;
    const isUserPaste = dimStatus === 'empty' && text.length > 300;
    const userRawContent = text;
    setInput('');
    setStreamError('');
    setSuggestions([]);
    setSelectedSuggestion(null);
    setLoadingSuggest(true);
    appendUserAi(`【${dimensions.find(d => d.key === selectedDim)?.label || selectedDim}】${text}`);
    try {
      const r = await api.smartSuggest(bookId, selectedDim, text, settingPacks, isUserPaste ? userRawContent : undefined);
      // 给用户方案补充完整内容字段
      if (isUserPaste && r.suggestions?.length > 0 && r.suggestions[0]._from_user) {
        r.suggestions[0]._full_content = userRawContent;
      }
      setSuggestions(r.suggestions || []);
      setMessages(prev => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === 'assistant') {
          const userPasteHint = isUserPaste ? '（检测到你已粘贴完整创作内容，第一个方案「📝 我的创作内容」可直接按你的内容落地）' : '';
          next[next.length - 1] = {
            ...last,
            content: `已为你生成 ${r.suggestions.length} 个「${r.dimension_label}」方案，请选择一个（点击下方方案）：${userPasteHint}`,
          };
        }
        return next;
      });
    } catch (e: any) {
      setStreamError(e.message || '生成方案失败');
      removeEmptyAi();
    } finally {
      setLoadingSuggest(false);
    }
  }, [input, bookId, selectedDim, streaming, settingPacks, dimensions, progress, bible, appendUserAi, removeEmptyAi]);

  // 2. 选中方案 → 仅记录选中，绝不自动生成。
  //    用户可在下方消息框补充修改意见，再点「按方案生成」按钮手动触发；
  //    不填意见也可直接点按钮按原方案生成。
  const handleGenerate = useCallback((suggestion: { id: string; title: string; preview: string }, index: number) => {
    if (!bookId || !selectedDim || streaming) return;
    setStreamError('');
    setSuggestions([]);
    setSelectedSuggestion(suggestion);
    streamBufferRef.current = '';
    fixingDimKeyRef.current = selectedDim;
    // ⚠️ 不能用 appendUserAi：它带空AI占位气泡，无内容且非流式时渲染为空，
    // 聊天区会悬着一个永远不填充的AI头像——这正是"选定方案后像被截断/聊天中断"的观感根源。
    appendUser(`已选择方案${['一', '二', '三', '四', '五'][index] || (index + 1)}：${suggestion.title}\n${suggestion.preview}`);
    appendAiNotice('✅ 方案已锁定。如需调整，在下方输入框补充修改意见（例如“主角改为女性”“节奏再快些”），再点「按方案生成」；不需要调整就直接点「按方案生成」按原方案生成。');
    inputRef.current?.focus();
  }, [bookId, selectedDim, streaming, appendUser, appendAiNotice]);

  // 2b. 基于已选方案 + 用户（可选）修改意见 流式生成最终内容（仅「按方案生成」按钮手动触发）
  const handleGenerateFromSelected = useCallback(async () => {
    if (!bookId || !selectedDim || streaming || !selectedSuggestion) return;
    const modification = input.trim();
    setInput('');
    setStreamError('');
    setSelectedSuggestion(null);
    streamBufferRef.current = '';
    fixingDimKeyRef.current = selectedDim;
    appendUserAi(modification || '按此方案直接生成');
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const isFromUser = !!selectedSuggestion._from_user;
      const suggestionContent = isFromUser ? (selectedSuggestion._full_content || selectedSuggestion.preview) : selectedSuggestion.preview;
      const res = await api.smartGenerateStream(bookId, selectedDim, suggestionContent, modification, settingPacks, sessionId || undefined, ctrl.signal, isFromUser);
      await consumeSSE(res, ctrl);
      refreshProgress();
      refreshHistory();
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        setStreamError(`${e.name}: ${e.message || '生成失败'}`);
        // eslint-disable-next-line no-console
        console.error('[ChatPanel] 方案生成失败', e?.name, e?.message, e?.stack);
        removeEmptyAi();
        refreshHistory(); // 断流后刷新会话历史（后端已抢救保存部分内容，可从历史找回）
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [bookId, selectedDim, streaming, selectedSuggestion, input, settingPacks, sessionId, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

  // 3. 下游维度直接生成（不经过多选方案，把用户输入作为生成要求）
  const handleDirectGenerate = useCallback(async () => {
    const text = input.trim();
    if (!bookId || !selectedDim || streaming || !text) return;
    setInput('');
    setStreamError('');
    streamBufferRef.current = '';
    fixingDimKeyRef.current = selectedDim;
    const dimLabel = dimensions.find(d => d.key === selectedDim)?.label || selectedDim;
    appendUserAi(`生成${dimLabel}：${text}`);
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await api.smartGenerateStream(bookId, selectedDim, text, text, settingPacks, sessionId || undefined, ctrl.signal);
      await consumeSSE(res, ctrl);
      refreshProgress();
      refreshHistory();
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        setStreamError(e.message || '生成失败');
        removeEmptyAi();
        refreshHistory(); // 断流后刷新会话历史（后端已抢救保存部分内容，可从历史找回）
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [input, bookId, selectedDim, streaming, settingPacks, sessionId, dimensions, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

  // 4. 单独维度AI修改（基于已落地内容）
  const handleDimEdit = useCallback(async () => {
    const text = input.trim();
    if (!bookId || !selectedDim || streaming || !text) return;
    setInput('');
    setStreamError('');
    streamBufferRef.current = '';
    // 记录正在修正的维度（用于任务清单自动标记 done）
    fixingDimKeyRef.current = selectedDim;
    const dimLabel = dimensions.find(d => d.key === selectedDim)?.label || selectedDim;
    appendUserAi(`修订${dimLabel}：${text}`);
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const currentContent = (bible as any)?.[selectedDim] || '';
      const res = await api.smartDimEditStream(bookId, selectedDim, currentContent, text, settingPacks, sessionId || undefined, ctrl.signal);
      await consumeSSE(res, ctrl);
      refreshProgress();
      refreshHistory();
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        setStreamError(e.message || '修订失败');
        removeEmptyAi();
        refreshHistory(); // 断流后刷新会话历史（后端已抢救保存部分内容，可从历史找回）
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [input, bookId, selectedDim, streaming, settingPacks, sessionId, dimensions, bible, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

  // ========== 正文Tab：写作/修改（结合消息栏输入的要求创作）==========
  // 真正执行章节写作/修改（结合用户在消息栏输入的要求）
  const doChapterAction = useCallback(async (action: 'continue' | 'polish', targetChapterId: string | null, instruction?: string) => {
    if (!bookId || streaming) return;
    // 真正开始执行时清掉 armed 态（任何入口都清，包括从输入框发送/任务单点击/章节下拉）
    setChapterEditArmed(false);
    setStreamError('');
    streamBufferRef.current = '';
    // 记录正在修改的章节（修改完成后自动标记任务清单 done）
    polishingChapterIdRef.current = (action === 'polish' && targetChapterId) ? targetChapterId : null;
    // 统一口径：target_chapter_num 优先从标题解析章节号，回退 order_index
    // 写作=下一章号；修改=所选章节的章节号
    let targetNum: number;
    if (action === 'continue') {
      targetNum = nextChapterNum;
    } else {
      const targetCh = targetChapterId ? chapters.find(c => c.id === targetChapterId) : null;
      // 统一口径：章号一律 1-based（title 能解析则用 title 章号，否则 order_index+1）
      // 修正：之前 parse(title) ?? order_index 会把 order_index(0-based) 直接作为章号显示，导致“选第3章→消息写第4章”的偏差
      const fallback = latestChapter ? displayChapterNum(latestChapter) : Math.max(1, nextChapterNum - 1);
      targetNum = targetCh ? displayChapterNum(targetCh) : fallback;
    }
    const label = action === 'continue' ? `写作第 ${nextChapterNum} 章` : `修改第 ${targetNum} 章`;
    const userNote = (instruction || input.trim());
    appendUserAi(userNote ? `${label}（${userNote.slice(0, 60)}）` : label);
    // 发送后重置为正文默认写作要求（而非清空），便于连续写作
    if (userNote) setInput(CHAPTER_DEFAULT_INPUT);
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await api.chatSmartAction(bookId, action, {
        target_chapter_num: targetNum,
        session_id: sessionId || undefined,
        instruction: userNote || undefined,
        // doChapterAction 只有 continue/polish 两种，都是正文阶段 → 统一带 style（chapterTab）选中的技能包
        skill_pack_ids: chapterPacks,
      }, ctrl.signal);
      await consumeSSE(res, ctrl);
      refreshProgress();
      refreshHistory();
      // 刷新最新章节 + 章节列表（写后会自动填入新章节到目录）
      api.smartLatestChapter(bookId).then(r => {
        setLatestChapter(r.latest);
        setNextChapterNum(r.next_chapter_num);
      }).catch(() => {});
      api.smartChapters(bookId).then(r => setChapters(r.chapters || [])).catch(() => {});
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        // 真实错误先打 console（方便排查：例如 Werkzeug 双迭代导致 DetachedInstanceError / 响应畸形）
        // eslint-disable-next-line no-console
        console.error('[ChatPanel] 正文生成失败', e?.name, e?.message, e?.stack, { status: (e as any)?.status });
        const msg = e?.message
          ? (e?.name ? `${e.name}: ${e.message}` : e.message)
          : (e?.name || `${label}失败`);
        setStreamError(msg);
        removeEmptyAi();
        refreshHistory(); // 断流后刷新会话历史（后端已抢救保存部分内容，可从历史找回）
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [bookId, streaming, nextChapterNum, chapters, latestChapter, sessionId, input, appendUserAi, removeEmptyAi, consumeSSE, refreshProgress, refreshHistory]);

  // 带明确意见执行修改（从消息栏解析章节号后调用）
  const doChapterActionWithNote = useCallback((action: 'continue' | 'polish', chapterId: string, note: string) => {
    doChapterAction(action, chapterId, note);
  }, [doChapterAction]);

  // 正文Tab统一发送：直接从消息框输入，智能判断修改/续写，无需先点按钮切换模式
  const handleChapterSend = useCallback(() => {
    if (!bookId || streaming) return;
    const text = input.trim();
    // 若之前点过"修改"按钮进入 armed 态，从输入框发送时清掉 armed（视为二次确认）
    const wasArmed = chapterEditArmed;
    setChapterEditArmed(false);
    if (!text) {
      // 无输入时直接续写下一章
      doChapterAction('continue', null);
      return;
    }
    // 智能判断：包含"第X章"或修改关键词 → 修改模式；否则 → 续写模式（输入作为写作要求）
    const hasChapterNum = /第\s*\d+\s*章/.test(text);
    const hasModifyKeyword = /修改|润色|改一?下|调整|增加|删掉|删除|替换|优化|扩充|精简/.test(text);
    if ((hasChapterNum || hasModifyKeyword) && chapters.length > 0) {
      const numMatch = text.match(/第?\s*(\d+)\s*章/);
      // 统一口径：用户输入的章号视为 1-based，按 displayChapterNum 匹配章节
      const targetNum = numMatch ? parseInt(numMatch[1], 10) : (latestChapter ? displayChapterNum(latestChapter) : 1);
      const ch = chapters.find(c => displayChapterNum(c) === targetNum);
      if (ch) {
        doChapterActionWithNote('polish', ch.id, text);
      } else {
        setStreamError(`未找到第 ${targetNum} 章，请检查章节号或直接输入写作要求`);
        void wasArmed; // 暂未使用，保留以备后续埋点
      }
    } else {
      // 续写：输入内容作为本章写作要求
      doChapterAction('continue', null, text);
    }
  }, [bookId, streaming, input, chapters, latestChapter, doChapterAction, doChapterActionWithNote, chapterEditArmed]);

  // 章节点位刷新🔄：手动重新拉取最新章节和章节列表
  const refreshChapterAnchor = useCallback(() => {
    if (!bookId) return;
    api.smartLatestChapter(bookId).then(r => {
      setLatestChapter(r.latest);
      setNextChapterNum(r.next_chapter_num);
    }).catch(() => {});
    api.smartChapters(bookId).then(r => setChapters(r.chapters || [])).catch(() => {});
  }, [bookId]);

  // ========== 去AITab：对选中章节去AI味 ==========
  const handleDeai = useCallback(async () => {
    if (!bookId || streaming) return;
    // 优先使用下拉选中的章节；未选则从消息框解析章节号
    let targetId = deaiTargetId;
    let note = '';
    if (!targetId) {
      const text = input.trim();
      if (text) {
        const numMatch = text.match(/第?\s*(\d+)\s*章/);
        if (numMatch) {
          const targetNum = parseInt(numMatch[1], 10);
          // 统一口径：用户输入的章号视为 1-based，按 displayChapterNum 匹配章节
          const ch = chapters.find(c => displayChapterNum(c) === targetNum);
          if (ch) {
            targetId = ch.id;
            note = text;
          } else {
            setStreamError(`未找到第 ${targetNum} 章，请检查章节号`);
            return;
          }
        } else {
          setStreamError('请先选择章节，或在输入框输入「第N章」指定');
          return;
        }
      } else {
        setStreamError('请先选择章节，或在输入框输入「第N章」指定');
        return;
      }
    }
    setStreamError('');
    streamBufferRef.current = '';
    const ch = chapters.find(c => c.id === targetId);
    appendUserAi(`去AI味：${ch?.title || '选中章节'}${note ? `（${note.slice(0, 40)}）` : ''}`);
    if (note) setInput('');
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await api.smartDeaiStream(bookId, targetId, deaiPacks_selected, sessionId || undefined, ctrl.signal);
      await consumeSSE(res, ctrl, (card, meta) => {
        (card as any).__meta = meta;
      });
      refreshHistory();
      api.smartChapters(bookId).then(r => setChapters(r.chapters || [])).catch(() => {});
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        setStreamError(e.message || '去AI味失败');
        removeEmptyAi();
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [bookId, deaiTargetId, streaming, chapters, sessionId, input, appendUserAi, removeEmptyAi, consumeSSE, refreshHistory, deaiPacks_selected]);

  // ========== B3：去AI Tab·风格对齐诊断（12维评分+改点建议+范本并排） ==========
  const handleStyleAlign = useCallback(async () => {
    if (!bookId || streaming) return;
    let targetId = deaiTargetId;
    if (!targetId) {
      const text = input.trim();
      if (text) {
        const numMatch = text.match(/第?\s*(\d+)\s*章/);
        if (numMatch) {
          const targetNum = parseInt(numMatch[1], 10);
          const ch = chapters.find(c => displayChapterNum(c) === targetNum);
          if (ch) targetId = ch.id;
          else {
            setStreamError(`未找到第 ${targetNum} 章，请检查章节号`);
            return;
          }
        }
      }
    }
    if (!targetId) {
      setStreamError('请先选择章节，或在输入框输入「第N章」指定');
      return;
    }
    setStreamError('');
    const ch = chapters.find(c => c.id === targetId);
    appendUserAi(`风格对齐诊断：${ch?.title || '选中章节'}`);
    setStreaming(true);
    try {
      const r = await api.smartStyleAlign(bookId, targetId);
      // 格式化 12 维评分表 + 改点建议 + 风格包范本，以 Markdown 文本形式展示
      const gradeEmoji = r.avg_score >= 85 ? '🟢' : r.avg_score >= 70 ? '🟡' : r.avg_score >= 55 ? '🟠' : '🔴';
      const dimTable = r.dimensions.map(d => {
        const s = d.score;
        const mark = s >= 85 ? '🟢' : s >= 70 ? '🟡' : s >= 60 ? '🟠' : '🔴';
        return `${mark} **${d.name}**：${s}/100　— ${d.note}`;
      }).join('\n');
      const badBlock = r.bad_items?.length ?
        '### 🚨 不及格维度改法（<60 分）\n\n' + r.bad_items.map((b, i) =>
          `**${i + 1}. ${b.name}（${b.score}/100）**\n${b.note}\n\n💡 **${b.fix_suggestion}**`
        ).join('\n\n---\n\n') :
        '✅ 所有 12 个维度均 ≥60 分，无不及格项。';
      const spBlock = r.style_pack ?
        `\n---\n\n### 📚 配套范本·${r.style_pack.name}\n（题材识别：${r.book_genre}，本章节已自动注入此写作铁则）\n\n` +
        '```\n' + r.style_pack.content.slice(0, 3000) + (r.style_pack.content.length > 3000 ? '\n…（范本完整内容已后台自动注入写作 prompt，此处截断展示前 3000 字）' : '') + '\n```\n' :
        `\n（题材：${r.book_genre}，未匹配到专属风格包，使用通用规则）`;
      const md =
        `## ${gradeEmoji} 风格对齐度总评：${r.avg_score}/100\n\n` +
        `**《${r.chapter_title}》**\n\n` +
        `**总评**：${r.summary}\n\n` +
        `---\n\n### 📊 12 维风格评分表（低分在前）\n\n${dimTable}\n\n---\n\n${badBlock}${spBlock}`;
      setMessages(prev => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === 'assistant') {
          next[next.length - 1] = { ...last, content: md };
        }
        return next;
      });
    } catch (e: any) {
      setStreamError(e.message || '风格对齐诊断失败');
      removeEmptyAi();
    } finally {
      setStreaming(false);
    }
  }, [bookId, deaiTargetId, streaming, chapters, input, appendUserAi, removeEmptyAi]);

  // ========== 校审Tab：防遗忘 / 一致性检查（按卷，拉取动态文件+伏笔）==========
  // B：两步确认机制
  //   reviewArmedMode === null  → 第 1 次点击按钮仅进入"待确认态"，并显示当前范围给用户
  //   reviewArmedMode === mode  → 第 2 次点击同模式才真正执行
  //   切 scope（换卷/换章） → 自动解除 armed（避免用旧范围误跑）
  const _reviewVolume = reviewVolumeIds[0] || null;
  const _reviewScopeAntiForget = _reviewVolume
    ? volumes.find(v => v.id === _reviewVolume)?.title || '按卷检查'
    : '全书';
  const _reviewScopeConsistency = reviewChapterId
    ? (chapters.find(c => c.id === reviewChapterId)?.title || '指定章节')
    : '最新章节';
  const handleReview = useCallback(async (mode: 'anti_forget' | 'consistency') => {
    if (!bookId || reviewing) return;
    // Step 1：首次点击（未 armed）→ 不跑，仅让按钮变为「待确认」状态，提醒用户确认范围
    if (reviewArmedMode !== mode) {
      setReviewArmedMode(mode);
      return;
    }
    // Step 2：二次点击 → 真正执行
    setStreamError('');
    setReviewing(true);
    const label = mode === 'anti_forget' ? '防遗忘检查' : '一致性检查';
    const volLabel = reviewVolumeIds.length ? `（按卷：${reviewVolumeIds.length}卷）` : '（全书）';
    appendUserAi(`执行${label}${volLabel}`);
    try {
      const r = await api.smartReview(
        bookId, mode,
        mode === 'consistency' ? (reviewChapterId || undefined) : undefined,
        [],
        reviewVolumeIds
      );
      setMessages(prev => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === 'assistant') {
          let reportText = '';
          if (mode === 'anti_forget' && r.report) {
            const rep = r.report;
            reportText = `## 防遗忘检查报告\n\n`;
            reportText += `**健康度评分：** ${rep.health_score ?? '未评'}\n\n`;
            if (rep.summary) reportText += `**摘要：** ${rep.summary}\n\n`;
            if (rep.violations && rep.violations.length) {
              reportText += `**违规问题（${rep.violations.length}）：**\n`;
              rep.violations.slice(0, 5).forEach((v: any, i: number) => {
                reportText += `${i + 1}. ${v.description || v.issue || JSON.stringify(v)}\n`;
              });
              reportText += '\n';
            }
            if (rep.pending_foreshadowing && rep.pending_foreshadowing.length) {
              reportText += `**待回收伏笔（${rep.pending_foreshadowing.length}）：**\n`;
              rep.pending_foreshadowing.slice(0, 5).forEach((f: any, i: number) => {
                reportText += `${i + 1}. ${f.description || f.title || JSON.stringify(f)}\n`;
              });
              reportText += '\n';
            }
            if (rep.suggestions && rep.suggestions.length) {
              reportText += `**改进建议：**\n`;
              rep.suggestions.slice(0, 3).forEach((s: any, i: number) => {
                reportText += `${i + 1}. ${typeof s === 'string' ? s : (s.description || s.suggestion || JSON.stringify(s))}\n`;
              });
            }
          } else if (mode === 'consistency') {
            reportText = `## 一致性检查报告\n\n`;
            reportText += `**章节：** ${r.chapter_title || '最新章节'}\n\n`;
            reportText += `**结果：** ${r.passed ? '✅ 通过' : '⚠️ 发现问题'}\n\n`;
            if (r.issues) reportText += `**问题详情：** ${r.issues}\n`;
          }
          next[next.length - 1] = { ...last, content: reportText || r.summary || `${label}完成` };
        }
        return next;
      });
      refreshHistory();
    } catch (e: any) {
      setStreamError(e.message || `${label}失败`);
      removeEmptyAi();
    } finally {
      setReviewing(false);
      setReviewArmedMode(null);  // 执行完解除确认，下一轮从头来
    }
  }, [bookId, reviewing, reviewArmedMode, reviewChapterId, reviewVolumeIds, volumes, chapters, appendUserAi, removeEmptyAi, refreshHistory]);

  // ========== 消息长按操作：编辑/删除/重新生成 ==========
  const handleEditMessage = useCallback((index: number, newContent: string) => {
    setMessages(prev => prev.map((m, i) => i === index ? { ...m, content: newContent } : m));
  }, []);

  const handleDeleteMessage = useCallback((index: number) => {
    setMessages(prev => prev.filter((_, i) => i !== index));
  }, []);

  // ========== 卡片操作 ==========
  const handleAdopt = useCallback(async (card: ActionCard) => {
    if (!bookId) return;
    setApplyingCardId(card.id);
    try {
      const r = await api.applyChatCard(bookId, card, sessionId || undefined);
      setMessages(prev => prev.map(m => {
        if (m.role !== 'assistant' || !m.cards) return m;
        return { ...m, cards: m.cards.map(c => c.id === card.id ? { ...c, status: 'adopted' as const } : c) };
      }));
      refreshProgress();
      if (card.type === 'SAVE_CHAPTER' && (r as any).chapter_id) {
        const ch = r as any;
        const actionLabel = ch.action === 'updated' ? '已覆盖同章号章节' : '已新建章节';
        // 统一口径：用 displayChapterNum（后端返回字段是 chapter_title/order_index，做适配）
        const chNum = displayChapterNum({ order_index: ch.order_index, title: ch.chapter_title });
        setStreamError('');
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `✅ ${actionLabel}：${ch.chapter_title}（${ch.word_count}字，第${chNum}章）。可在「章节」Tab 查看。`,
        }]);
        // 刷新章节列表
        api.smartLatestChapter(bookId).then(rr => {
          setLatestChapter(rr.latest);
          setNextChapterNum(rr.next_chapter_num);
        }).catch(() => {});
        api.smartChapters(bookId).then(rr => setChapters(rr.chapters || [])).catch(() => {});
      }
    } catch (e: any) {
      setStreamError(e.message || '落地失败');
    } finally {
      setApplyingCardId(null);
    }
  }, [bookId, sessionId, refreshProgress]);

  const handleEdit = useCallback(async (card: ActionCard, newContent: string) => {
    if (!bookId) return;
    setApplyingCardId(card.id);
    try {
      const editedCard = { ...card, content: newContent };
      const r = await api.applyChatCard(bookId, editedCard, sessionId || undefined);
      setMessages(prev => prev.map(m => {
        if (m.role !== 'assistant' || !m.cards) return m;
        return { ...m, cards: m.cards.map(c => c.id === card.id ? { ...editedCard, status: 'edited' as const } : c) };
      }));
      refreshProgress();
      if (card.type === 'SAVE_CHAPTER' && (r as any).chapter_id) {
        const ch = r as any;
        const actionLabel = ch.action === 'updated' ? '已覆盖同章号章节' : '已新建章节';
        // 统一口径：用 displayChapterNum（后端返回字段是 chapter_title/order_index，做适配）
        const chNum2 = displayChapterNum({ order_index: ch.order_index, title: ch.chapter_title });
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `✅ ${actionLabel}：${ch.chapter_title}（${ch.word_count}字，第${chNum2}章）。`,
        }]);
        api.smartLatestChapter(bookId).then(rr => {
          setLatestChapter(rr.latest);
          setNextChapterNum(rr.next_chapter_num);
        }).catch(() => {});
        api.smartChapters(bookId).then(rr => setChapters(rr.chapters || [])).catch(() => {});
      }
    } catch (e: any) {
      setStreamError(e.message || '落地失败');
    } finally {
      setApplyingCardId(null);
    }
  }, [bookId, sessionId, refreshProgress]);

  const handleIgnore = useCallback((card: ActionCard) => {
    setMessages(prev => prev.map(m => {
      if (m.role !== 'assistant' || !m.cards) return m;
      return { ...m, cards: m.cards.map(c => c.id === card.id ? { ...c, status: 'ignored' as const } : c) };
    }));
    // 持久化忽略状态（避免重开聊天又提示采纳）
    if (sessionId) {
      api.updateCardStatus(sessionId, card.id, 'ignored').catch(() => {});
    }
  }, [sessionId]);

  // 去AI味卡片：替换原章节正文
  const handleReplaceChapter = useCallback(async (card: ActionCard, meta: any) => {
    if (!bookId || !meta?.chapter_id) return;
    setApplyingCardId(card.id);
    try {
      await api.smartChapterReplace(bookId, meta.chapter_id, card.content, sessionId || undefined, card.id);
      setMessages(prev => prev.map(m => {
        if (m.role !== 'assistant' || !m.cards) return m;
        return { ...m, cards: m.cards.map(c => c.id === card.id ? { ...c, status: 'adopted' as const } : c) };
      }));
      setStreamError('');
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: `✅ 已用去AI味后的内容替换原章节正文（${card.content.length}字）。`,
      }]);
      api.smartChapters(bookId).then(r => setChapters(r.chapters || [])).catch(() => {});
    } catch (e: any) {
      setStreamError(e.message || '替换失败');
    } finally {
      setApplyingCardId(null);
    }
  }, [bookId, sessionId]);

  // ========== 历史会话 ==========
  const handleSelectSession = useCallback(async (sid: string) => {
    if (streaming) stopStream();
    setSessionId(sid);
    setShowHistory(false);
    setStreamError('');
    setMessages([]);
    try {
      const r = await api.getChatSessionMessages(sid);
      const loaded: AIMessage[] = (r.messages || []).map((m: any) => ({
        role: m.role || 'assistant',
        content: m.content || '',
        cards: Array.isArray(m.cards) ? m.cards : undefined,
      }));
      setMessages(loaded);
    } catch (e: any) {
      setStreamError('加载历史会话失败：' + (e.message || ''));
    }
  }, [streaming]);

  // 删除历史会话：删除后刷新列表；若删的是当前会话，切到最新或新建
  const handleDeleteSession = useCallback(async (sid: string, e: React.MouseEvent) => {
    e.stopPropagation();  // 阻止冒泡触发 handleSelectSession
    if (!window.confirm('确定删除该会话？此操作不可撤销。')) return;
    try {
      await api.deleteAISession(sid);
      // 刷新历史列表
      const r = await api.listBookChatSessions(bookId!);
      const remaining = r.sessions || [];
      setHistorySessions(remaining);
      // 若删的是当前会话，切到最新或清空
      if (sid === sessionId) {
        if (remaining.length > 0) {
          await handleSelectSession(remaining[0].id);
        } else {
          setSessionId(null);
          setMessages([]);
        }
      }
    } catch (err: any) {
      alert('删除失败：' + (err.message || '未知错误'));
    }
  }, [bookId, sessionId, handleSelectSession]);

  const handleNewSession = useCallback(() => {
    if (streaming) stopStream();
    setMessages([]);
    setSessionId(null);
    setShowHistory(false);
    setStreamError('');
    setSuggestions([]);
    setSelectedSuggestion(null);
  }, [streaming]);

  const stopStream = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setStreaming(false);
  }, []);

  // 输入框占位符
  const inputPlaceholder = (() => {
    if (activeTab === 'setting') {
      if (!selectedDim) return '请先选择上方维度按钮…';
      if (selectedDim === 'general') return '💬 任意话题/构思/设定 回车发送';
      const dimLabel = dimensions.find(d => d.key === selectedDim)?.label || selectedDim;
      if (selectedSuggestion) {
        return `已选「${selectedSuggestion.title}」。可输入修改意见，不填则直接按此方案生成…`;
      }
      if (shouldShowSuggestions(selectedDim, bible)) {
        return `描述你对「${dimLabel}」的构思方向，AI 会给出多个方案供选择…`;
      }
      return `输入要求直接生成或修改「${dimLabel}」（无需方案选择）…`;
    }
    if (activeTab === 'chapter') return '接上一章剧情，读取剧情维度里的「本章剧情节点」，保证ONE主钩子贯穿本章。禁止无目标流水账。（可直接点「续写」，或追加要求，如：第3章 增加打斗细节）';
    if (activeTab === 'deai') return '点击上方「开始去AI味」按钮即可…';
    if (activeTab === 'review') return '点击上方检查按钮即可…';
    return '和 AI 智驾聊聊…';
  })();

  // 发送按钮是否可用
  const canSend = (() => {
    if (streaming || !input.trim()) return false;
    if (activeTab === 'setting') return !!selectedDim;
    return false;
  })();

  // 【设定】Tab内「通用」子维度（整合版）：调用 chatGeneralStream（任意话题+命中气泡入库）
  // 创作辅助入口已合并，直接让用户用自然语言聊天生成；命中创作维度自动气泡落卡
  // opts.text：重新生成时直接复用原提问（不读输入框）；opts.truncateHistoryTo：后端截断历史实现重新生成
  const handleGeneral = useCallback(async (opts?: { text?: string; truncateHistoryTo?: number }) => {
    const text = (opts?.text ?? input).trim();
    if (!bookId || !text || streaming) return;
    setInput('');
    setStreamError('');
    setAutoContextNotice(null);
    streamBufferRef.current = '';
    reasoningBufferRef.current = '';

    // ====== 圆桌会议分支：6个专家Agent两轮轮流发言，实时流式展示，最后出总结报告 ======
    const _PREKEY = '__general_pending_session__';
    const _sidReal = chatGeneralSessionId || '';
    const _sid = _sidReal || _PREKEY;
    const _rtRole = sessionRoleMap[_sid] || 'default';
    if (_rtRole === 'roundtable') {
      setMessages(prev => [...prev, { role: 'user', content: text }, {
        role: 'assistant', content: '', cards: [],
        roundtable: { speech: [], currentSpeaker: '', status: 'open' as const },
      }]);
      setStreaming(true);
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      const _cfgId = sessionModelMap[_sid] || undefined;
      try {
        const res = await api.chatRoundtableStream(text, { bookId: bookId || undefined, sessionId: chatGeneralSessionId || undefined, aiConfigId: _cfgId }, ctrl.signal);
        if (!res.ok) {
          const err = await res.json().catch(() => ({ error: `请求失败 (HTTP ${res.status})` }));
          throw new Error(err.error || `HTTP ${res.status}`);
        }
        let curName = '';
        for await (const evt of parseSSE(res)) {
          if (ctrl.signal.aborted) break;
          if (evt.type === 'error') throw new Error(evt.error);
          if (evt.type === 'meta' && evt.kind === 'roundtable_start' && evt.info) {
            curName = '';
          } else if (evt.type === 'meta' && evt.kind === 'roundtable_speaker' && evt.info) {
            // 切换发言人：记录当前发言人名字，接下来的delta都算它的发言
            curName = String(evt.info.speaker_name || '').trim();
            setMessages(prev => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === 'assistant' && last.roundtable) {
                const rt = { ...last.roundtable };
                rt.currentSpeaker = curName;
                next[next.length - 1] = { ...last, roundtable: rt };
              }
              return next;
            });
          } else if (evt.type === 'meta' && evt.kind === 'reasoning' && evt.info && typeof evt.info.text === 'string') {
            reasoningBufferRef.current += evt.info.text;
            const rbuf = reasoningBufferRef.current;
            setMessages(prev => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === 'assistant') next[next.length - 1] = { ...last, reasoning: rbuf };
              return next;
            });
          } else if (evt.type === 'delta' && typeof evt.content === 'string') {
            const sp = String((evt as any).speaker || '');
            const c = evt.content;
            setMessages(prev => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === 'assistant' && last.roundtable) {
                const rt = { ...last.roundtable };
                const speech = [...rt.speech];
                // 找当前发言人：若最后一节=同一人则追加，否则开新一节
                const lastSeg = speech[speech.length - 1];
                if (lastSeg && lastSeg.speaker === sp) {
                  lastSeg.content += c;
                  speech[speech.length - 1] = lastSeg;
                } else {
                  speech.push({ speaker: sp, name: curName || sp, content: c });
                }
                rt.speech = speech;
                rt.currentSpeaker = curName || rt.currentSpeaker;
                next[next.length - 1] = { ...last, roundtable: rt };
              }
              return next;
            });
          } else if (evt.type === 'speaker_done' && (evt as any).speaker === 'moderator_summary') {
            setMessages(prev => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === 'assistant' && last.roundtable) {
                next[next.length - 1] = { ...last, roundtable: { ...last.roundtable, status: 'done' as const } };
              }
              return next;
            });
          } else if (evt.type === 'done') {
            if (evt.session_id) setChatGeneralSessionId(evt.session_id);
          }
        }
      } catch (e: any) {
        if (e.name !== 'AbortError') {
          const msg = (e?.message || e?.error || '圆桌会议出错').trim() || '圆桌会议出错';
          appendAiNotice('❌ 圆桌会议出错：' + msg + '\n\n常见原因&解决：\n1) LLM上游503/限流 → 等30秒后重发\n2) 六个专家叠加调用可能触发模型配额上限 → 检查模型配置token配额');
          setStreamError('');
          removeEmptyAi();
        }
      } finally {
        setStreaming(false);
        abortRef.current = null;
      }
      return;
    }

    // ====== 所有消息统一走普通通用聊天（命中创作维度→气泡入库提示） ======
    appendUserAi(text);
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    // 命中气泡：挂在用户消息之后（msg_index = 倒数第2条=这条用户消息）
    const insertedPopupIndex = { idx: -1 };
    setMessages(prev => { insertedPopupIndex.idx = prev.length - 2; return prev; });
    try {
      // 修复：传 chatGeneralSessionId（通用聊天专用），不传 undefined → 否则后端每次新建会话=记忆全丢（用户投诉：通用比普通CHATBOX差远了，没记忆没上下文）
      // P1-1 会话级切模型 + P1-3 内置角色 persona → 一并传通用聊天
      // 【先发消息再切 → 允许打开页面就切】：用一个固定的"通用预会话key"作为占位，
      //   只要 chatGeneralSessionId 还没生成（还没发过任何消息），选择的模型/角色全部记到这个占位 key 下；
      //   等第一条消息发送完毕、chatGeneralSessionId 更新后，把占位里的记录迁移到真实 sessionId 下。
      const _PREKEY = '__general_pending_session__';
      const _sidReal = chatGeneralSessionId || '';
      const _sid = _sidReal || _PREKEY;
      const _cfgId = sessionModelMap[_sid] || undefined;
      const _roleId = sessionRoleMap[_sid] || 'default';
      const res = await api.chatGeneralStream(text, { bookId: bookId || undefined, sessionId: chatGeneralSessionId || undefined, aiConfigId: _cfgId, roleId: _roleId, deepThink: generalDeepThink, webSearch: generalWebSearch, truncateHistoryTo: opts?.truncateHistoryTo }, ctrl.signal);
      // consumeSSE 最后1个参数 onSessionId=setChatGeneralSessionId：把card/done帧带回的session_id只写入 chatGeneralSessionId，不污染全局 setSessionId（避免和其他创作维度会话互串）
      await consumeSSE(res, ctrl, undefined, (kind: string, info: any) => {
        // 【P1-3 角色同步】后端把真实生效的角色 + 上下文变量通过 kind=role_applied meta帧回传
        // 前端把 sessionRoleMap 跟后端对齐，避免出现"用户选了毒舌读者但chip显示的是默认助手"（尤其刷新后）
        if (kind === 'role_applied' && info && typeof info === 'object') {
          const _sid = chatGeneralSessionId || '';
          if (_sid) {
            const _backRole = String(info.role_id || 'default').trim() || 'default';
            setSessionRoleMap(m => (m[_sid] === _backRole ? m : { ...m, [_sid]: _backRole }));
            // 后端 meta_json 首次写入时也会写入 ai_config_id：若前端没记模型，一并对齐
            if (info.vars?.model_name) {
              // (只在未记录时做轻量提示：实际模型以 sessionModelMap 为主，避免覆盖用户未持久化的选择)
            }
          }
        }
        // 命中创作维度 → 气泡提示一键入库
        if (kind === 'hit_suggestions' && Array.isArray(info?.suggestions) && info.suggestions.length > 0) {
          const popId = 'hit-' + Math.random().toString(36).slice(2, 9);
          // 映射校正：后端hit_suggestions可能返回非正式ctype（'setting'/'worldview'/'concept'/'key_rules'/'character'），统一转CARD_REGISTRY合法值
          const normalized = (info.suggestions as Array<any>).map((s, i) => {
            const rawDim = String(s.dim || s.label || 'concept').trim().toLowerCase();
            const dimShort = rawDim.replace(/^save_/, '');
            let mappedCard = s.card_type || '';
            let mappedDim = s.dim || dimShort;
            let mappedLabel = s.label || s.dim || '创作内容';
            if (dimShort.includes('worldview') || dimShort.includes('world') || s.card_type === 'SAVE_WORLDSETTING') {
              mappedCard = 'SAVE_WORLDSETTING'; mappedDim = 'worldview'; mappedLabel = '世界观';
            } else if (dimShort.includes('setting') || dimShort === '设定' || dimShort.includes('设定')) {
              mappedCard = 'SAVE_SETTING'; mappedDim = 'setting'; mappedLabel = '设定';
            } else if (dimShort.includes('concept') || dimShort.includes('构思') || s.card_type === 'SAVE_CONCEPT') {
              mappedCard = 'SAVE_CONCEPT'; mappedDim = 'concept'; mappedLabel = mappedLabel || '构思';
            } else if (dimShort.includes('key_rule') || dimShort.includes('规则') || s.card_type === 'SAVE_RULE') {
              mappedCard = 'SAVE_RULE'; mappedDim = 'key_rules'; mappedLabel = mappedLabel || '核心规则';
            } else if (dimShort.includes('character') || dimShort.includes('人物') || s.card_type === 'SAVE_CHARACTER') {
              mappedCard = 'SAVE_CHARACTER'; mappedDim = 'character_profiles'; mappedLabel = mappedLabel || '人物';
            } else if (dimShort.includes('foreshadow') || dimShort.includes('伏笔') || s.card_type === 'SAVE_FORESHADOW') {
              mappedCard = 'SAVE_FORESHADOW'; mappedDim = 'foreshadowing'; mappedLabel = mappedLabel || '伏笔';
            } else if (dimShort.includes('plot') || dimShort.includes('剧情') || s.card_type === 'SAVE_PLOT') {
              mappedCard = 'SAVE_PLOT'; mappedDim = 'timeline'; mappedLabel = mappedLabel || '剧情';
            } else if (dimShort.includes('outline') || dimShort.includes('大纲') || s.card_type === 'SAVE_OUTLINE_NODE') {
              mappedCard = 'SAVE_OUTLINE_NODE'; mappedDim = 'plot_design'; mappedLabel = mappedLabel || '大纲';
            } else if (dimShort.includes('location') || dimShort.includes('地点') || s.card_type === 'SAVE_LOCATION') {
              mappedCard = 'SAVE_LOCATION'; mappedDim = 'locations'; mappedLabel = mappedLabel || '地点';
            } else if (dimShort.includes('style') || dimShort.includes('文风') || s.card_type === 'APPLY_STYLE') {
              mappedCard = 'APPLY_STYLE'; mappedDim = 'style_guide'; mappedLabel = mappedLabel || '文风';
            }
            if (!mappedCard) mappedCard = 'SAVE_CONCEPT';
            return {
              id: s.id || ('hit-sug-' + i + '-' + Date.now()),
              dim: mappedDim,
              label: mappedLabel,
              card_type: mappedCard,
              confidence: typeof s.confidence === 'number' ? s.confidence : 0.85,
              hits: Array.isArray(s.hits) && s.hits.length > 0 ? s.hits : [mappedLabel + '内容命中'],
              suggested_title: s.suggested_title || s.title || ('📦 ' + mappedLabel + '落卡'),
              quick_fill: s.quick_fill || s.content || s.text || '',
            } as HitSuggestion;
          });
          setHitSuggestionPopups(prev => [...prev, { id: popId, msg_index: insertedPopupIndex.idx, suggestions: normalized }]);
        }
      }, false, (freshSid: string) => {
        // 新建会话 → 把预会话里的模型/角色迁移到真实sid下，然后清除预会话占位
        if (freshSid && freshSid !== chatGeneralSessionId) {
          setSessionModelMap(prev => {
            const v = prev[_PREKEY];
            if (v === undefined) return prev;
            const { [_PREKEY]: _drop, ...rest } = prev;
            void _drop;
            return { ...rest, [freshSid]: v };
          });
          setSessionRoleMap(prev => {
            const v = prev[_PREKEY];
            if (v === undefined) return prev;
            const { [_PREKEY]: _drop, ...rest } = prev;
            void _drop;
            return { ...rest, [freshSid]: v };
          });
        }
        setChatGeneralSessionId(freshSid);
      });
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        const msg = (e?.message || e?.error || '聊天失败').trim() || '聊天失败';
        const hint = [
          '❌ 通用聊天出错：' + msg,
          '',
          '常见原因&解决：',
          '1) LLM上游503/限流 → 等30秒后重发',
          '2) 想让聊天内容落卡 → 先聊创作相关内容（如"帮我构思一个主角"），命中后再点📦入库气泡',
          '3) 想快速出结果 → 直接自然语言说需求（例如："给我出5个都市异能高武题材的书名和金手指组合"）',
        ].join('\n');
        appendAiNotice(hint);
        setStreamError(''); // 错误已在聊天气泡中完整提示，清除浮动红卡
        removeEmptyAi();
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [input, bookId, streaming, sessionId, chatGeneralSessionId, appendUserAi, removeEmptyAi, consumeSSE, generalDeepThink, generalWebSearch]);

  // 主发送动作（设定Tab：通用走general，维度已有内容走dim-edit，否则走suggest）
  const handleMainSend = useCallback(() => {
    if (activeTab !== 'setting' || !selectedDim) return;
    if (suggestions.length > 0) return;
    // 若已选择某个方案，则基于该方案 + 输入框修改意见生成
    if (selectedSuggestion) {
      handleGenerateFromSelected();
      return;
    }
    if (selectedDim === 'general') {
      handleGeneral();
      return;
    }
    const dimStatus = progress?.dims.find(d => d.field === selectedDim)?.status;
    // 下游维度（大纲/剧情/人物/伏笔等）由上游确定，不再给出多选方案：
    // 有内容则修改，无内容则直接基于用户要求生成。
    if (!shouldShowSuggestions(selectedDim, bible)) {
      if (dimStatus && dimStatus !== 'empty') {
        handleDimEdit();
      } else {
        handleDirectGenerate();
      }
      return;
    }
    if (dimStatus && dimStatus !== 'empty') {
      handleDimEdit();
    } else {
      handleSuggest();
    }
  }, [activeTab, selectedDim, suggestions, selectedSuggestion, progress, bible, handleSuggest, handleDimEdit, handleGeneral, handleDirectGenerate, handleGenerateFromSelected]);

  // 重新生成：无论点的是用户消息还是AI回复，都重新回答"目标用户提问"，
  // 丢弃其后的旧回复并重新触发对应动作（通用聊天走 truncate_history_to 截断历史）
  const handleRegenerate = useCallback((index: number) => {
    setMessages(prev => {
      const clicked = prev[index];
      // 目标用户消息：点AI回复→其前一条是提问；点用户消息→即它自己
      const targetUserIdx = (clicked && clicked.role === 'assistant') ? index - 1 : index;
      const targetMsg = prev[targetUserIdx];
      if (!targetMsg || targetMsg.role !== 'user') return prev;
      // 新消息列表：丢弃目标提问及其之后的所有（旧AI回复/后续），随后重新触发
      const next = prev.slice(0, targetUserIdx);
      if (activeTab === 'setting' && selectedDim === 'general') {
        const text = targetMsg.content.trim();
        if (text) {
          setTimeout(() => handleGeneral({ text, truncateHistoryTo: Math.max(0, targetUserIdx) }), 50);
        }
      } else if (activeTab === 'setting' && selectedDim) {
        const stripped = targetMsg.content.replace(/^【[^】]+】/, '').trim();
        if (stripped) {
          setInput(stripped);
          setTimeout(() => handleMainSend(), 50);
        }
      }
      return next;
    });
  }, [activeTab, selectedDim, handleMainSend, handleGeneral]);

  // 设定Tab：选择维度后
  // - 构思/大纲/文风 等方向性维度：第一次输入走 suggest（生成多选意见），选中后 generate
  // - 设定/世界观/人物/剧情/伏笔/地图 等执行性维度：由上游锁定方向，直接 generate 或 dim-edit，不再给方案
  // 生成落地后，再输入走 dim-edit（修订）
  // 通用模式：直接走 general（流式聊天）
  const onInputKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      // 【设定】Tab内「通用」子维度（新整合版）：直接发
      if (activeTab === 'setting' && selectedDim === 'general' && suggestions.length === 0) {
        handleGeneral();
        return;
      }
      if (activeTab === 'setting' && selectedDim && suggestions.length === 0) {
        handleMainSend();
      }
    }
  };

  const TABS: Array<{ key: SmartTab; label: string; icon: string }> = [
    { key: 'setting', label: '设定', icon: '⚙️' },
    { key: 'chapter', label: '正文', icon: '✍️' },
    { key: 'deai', label: '去AI', icon: '🧹' },
    { key: 'review', label: '校审', icon: '🔍' },
  ];

  return (
    <>
      {chatPanelOpen && bookId && (
        <div className="chat-panel-overlay" data-build={__BUILD_TAG__}>
          <div className="chat-panel smart-panel">
            {/* 头部（紧凑） */}
            <div className="chat-panel-header chat-panel-header-compact">
              <div className="chat-panel-title">
                <span className="chat-panel-logo"><CarLogo size={20} /></span>
                <div className="chat-panel-name">AI 智驾</div>
                <button
                  className="chat-dim-fold-btn"
                  onClick={() => setDimRowsCollapsed(s => !s)}
                  title={dimRowsCollapsed ? '展开维度栏' : '折叠维度栏'}
                  data-collapsed={dimRowsCollapsed}
                >{dimRowsCollapsed ? '▶' : '▼'}</button>
              </div>
              <div className="chat-panel-tools">
                <button className="chat-tool-btn" onClick={() => { setShowProgress(s => !s); }} title="创作进度">🗺️<span className="chat-tool-label">创作进度</span></button>
                <button className="chat-tool-btn" onClick={() => { setShowEntityRegistry(true); }} title="实体管理（跨维度重命名/合并实体）">
                  🏗️<span className="chat-tool-label">实体管理</span>
                </button>
                <button className="chat-tool-btn" onClick={() => { setShowHistory(s => !s); refreshHistory(); }} title="历史会话">🕘<span className="chat-tool-label">历史会话</span></button>
                <button className="chat-tool-btn" onClick={handleNewSession} title="新会话">✨<span className="chat-tool-label">新会话</span></button>
                <button className="chat-tool-btn close" onClick={closeChatPanel} title="关闭">✕</button>
              </div>
            </div>

            {/* 进度地图浮层 */}
            {showProgress && (
              <div className="chat-panel-side">
                <ProgressMapView progress={progress} onClose={() => setShowProgress(false)} />
              </div>
            )}

            {/* 历史会话浮层 */}
            {showHistory && (
              <div className="chat-panel-side">
                <div className="chat-history">
                  <div className="chat-history-head">
                    <span>历史会话</span>
                    <button className="chat-history-close" onClick={() => setShowHistory(false)}>×</button>
                  </div>
                  {historySessions.length === 0 ? (
                    <div className="chat-history-empty">还没有历史会话</div>
                  ) : (
                    historySessions.map(s => (
                      <div key={s.id} className={`chat-history-item ${s.id === sessionId ? 'active' : ''}`} onClick={() => handleSelectSession(s.id)}>
                        <div className="chat-history-title">{s.title || '未命名'}</div>
                        <div className="chat-history-meta">{s.message_count} 条 · {s.updated_at ? new Date(s.updated_at).toLocaleString() : ''}</div>
                        <button
                          className="chat-history-del"
                          title="删除会话"
                          onClick={(e) => handleDeleteSession(s.id, e)}
                        >×</button>
                      </div>
                    ))
                  )}
                </div>
              </div>
            )}

            {/* Q1：直接复用实体管理弹窗（跨维度重命名/合并），替换掉原来动作影响预览里的弱版 rename 界面 */}
            {showEntityRegistry && bookId && (
              <EntityRegistryModal
                bookId={bookId}
                onClose={() => setShowEntityRegistry(false)}
                onRenamed={() => { refreshProgress(); }}
              />
            )}

            {/* Tab 工具区（根据当前Tab显示不同工具） */}
            <div className="smart-toolbar">

              {activeTab === 'setting' && (
                <>
                  {selectedDim === 'general' && hitSuggestionPopups.length > 0 && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, padding: '6px 8px' }}>
                      {hitSuggestionPopups.map(pop => (
                        <div key={pop.id} className="auto-context-notice" style={{ borderRadius: 6, margin: 0 }} onClick={() => setHitSuggestionPopups(prev => prev.filter(x => x.id !== pop.id))}>
                          <span className="acn-icon">💡</span>
                          <span className="acn-text">
                            <strong>命中：</strong>
                            {pop.suggestions.map((s: HitSuggestion, i: number) => (
                              <span key={s.dim + i} style={{ marginLeft: 6, background: '#fff7ed', color: '#c2410c', padding: '1px 6px', borderRadius: 999, fontSize: 12 }}>
                                {s.label} {Math.round(s.confidence * 100)}%
                              </span>
                            ))}
                          </span>
                          <span className="acn-close" style={{ marginLeft: 8, padding: '0 6px', fontSize: 12 }}>×</span>
                        </div>
                      ))}
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                        {hitSuggestionPopups.flatMap(pop => pop.suggestions.map((s: HitSuggestion) => {
                          const hitKey = pop.id + '-' + s.dim + '-' + (s.id || Math.random().toString(36).slice(2,7));
                          const applying = applyingHitKey === hitKey;
                          return (
                            <button
                              key={hitKey}
                              className="btn-sm"
                              style={{ fontSize: 12, opacity: applying ? 0.7 : 1, minWidth: 140 }}
                              disabled={applying || !bookId}
                              onClick={async () => {
                                if (!bookId || applying) return;
                                if (!s.quick_fill || !String(s.quick_fill).trim()) {
                                  setMessages(prev => {
                                    const next = [...prev];
                                    next.push({
                                      role: 'assistant',
                                      content: '❌ 命中气泡缺少可落卡的填充内容(quick_fill为空)：请回到通用聊天区，重新生成方案/命中创作内容后再试。',
                                      cards: [],
                                    });
                                    return next;
                                  });
                                  setHitSuggestionPopups(prev => prev.filter(x => x.id !== pop.id));
                                  return;
                                }
                                setApplyingHitKey(hitKey);
                                const cardId = 'gen-' + Math.random().toString(36).slice(2, 10);
                                const realCard = {
                                  id: cardId,
                                  type: s.card_type,
                                  title: s.suggested_title || ('📦 ' + s.label + '落卡'),
                                  content: s.quick_fill,
                                  target: s.label,
                                  status: 'adopted',
                                };
                                try {
                                  // 命中气泡落卡：优先用chatGeneralSessionId（通用聊天专用会话，保证落卡记忆写入正确的session，供下一轮LLM"记得已经落过卡"），
                                  // 否则退化到全局sessionId（其他维度场景兼容）
                                  const effectiveSessionId = chatGeneralSessionId || sessionId;
                                  const r = await api.applyChatCard(bookId, realCard as any, effectiveSessionId ?? undefined);
                                  // 成功后刷新维度内容：Bible + 进度地图
                                  try { api.getBible(bookId).then(b => { if (b) setBible(b); }).catch(() => {}); } catch {}
                                  try { api.getProgressMap(bookId).then(p => { if (p) { setProgress(p); setShowProgress(true); setTimeout(() => setShowProgress(false), 2800); } }).catch(() => {}); } catch {}
                                  // 自动切 Tab + 选中对应子维度 → 真正打通"通用聊天→采纳→维度面板"工作流闭环
                                  setActiveTab('setting');
                                  const ct = String(s.card_type || '').toUpperCase();
                                  let targetDim: string | null = null;
                                  if (ct === 'SAVE_WORLDSETTING') targetDim = 'worldbuilding';
                                  else if (ct === 'SAVE_SETTING' || ct === 'SAVE_CONCEPT') targetDim = 'concept';
                                  else if (ct === 'SAVE_RULE') targetDim = 'key_rules';
                                  else if (ct === 'SAVE_CHARACTER') targetDim = 'character_profiles';
                                  else if (ct === 'SAVE_FORESHADOW') targetDim = 'foreshadowing';
                                  else if (ct === 'SAVE_PLOT') targetDim = 'timeline';
                                  else if (ct === 'SAVE_OUTLINE_NODE') targetDim = 'plot_design';
                                  else if (ct === 'SAVE_LOCATION') targetDim = 'locations';
                                  else if (ct === 'APPLY_STYLE') targetDim = 'style_guide';
                                  else targetDim = 'concept';
                                  if (targetDim) {
                                    setSelectedDim(targetDim);
                                    setSuggestions([]);
                                    setSelectedSuggestion(null);
                                  }
                                  // 聊天区留痕：插一张"✅ 已采纳落卡成功"的折叠卡片（显示 AdoptedCardCollapsed 绿条）
                                  setMessages(prev => {
                                    const next = [...prev];
                                    const afterIndex = pop.msg_index + 1;
                                    const successBody = [
                                      `✅ 已采纳落卡成功 → 已自动切到【${s.label}】子维度，可在上方面板直接查看/编辑刚写入的完整内容`,
                                      '',
                                      `📌 落地进度：整体 ${r?.progress?.overall ?? '--'}%（${r?.progress?.filled ?? '-'}/${r?.progress?.total ?? '-'} 维度完善）`,
                                      '',
                                      '【落卡字段】',
                                      s.quick_fill,
                                    ].join('\n');
                                    const adoptedCard = { ...realCard, content: successBody, status: 'adopted' as const };
                                    if (next[afterIndex]?.role === 'assistant') {
                                      next[afterIndex] = { ...next[afterIndex], cards: [...(next[afterIndex].cards || []), adoptedCard as any] };
                                    } else {
                                      next.splice(afterIndex, 0, { role: 'assistant', content: '', cards: [adoptedCard as any] });
                                    }
                                    return next;
                                  });
                                  setHitSuggestionPopups(prev => prev.filter(x => x.id !== pop.id));
                                  setInput('');
                                } catch (e: any) {
                                  const err = (e?.message || e?.error || String(e || '')).trim() || '未知错误';
                                  setMessages(prev => {
                                    const next = [...prev];
                                    next.push({
                                      role: 'assistant',
                                      content: '❌ 落卡失败：' + err,
                                      cards: [],
                                    });
                                    return next;
                                  });
                                } finally {
                                  setApplyingHitKey(prevKey => (prevKey === hitKey ? null : prevKey));
                                }
                              }}
                            >{applying ? '落卡中…' : `📦 以「${s.label}」入库`}</button>
                          );
                        }))}
                      </div>
                    </div>
                  )}
                  {/* 维度子按钮栏 + 助手选择器（手机端可折叠） */}
                  <div className="smart-dim-collapsible" data-collapsed={dimRowsCollapsed}>
                  {/* 维度子按钮栏：两行（通用/构思/设定/世界观 + 大纲/剧情/人物/伏笔） */}
                  <div className="smart-dim-rows">
                    <div className="smart-dim-row">
                      <button
                        className={`smart-dim-btn ${selectedDim === 'general' ? 'active' : ''}`}
                        onClick={() => {
                          // 切到【通用】子维度 = 清空消息/气泡/任务（独立会话池，和其他维度互不干扰）
                          setSelectedDim('general'); setSuggestions([]); setSelectedSuggestion(null); setInput('');
                          setMessages([]); setHitSuggestionPopups([]); setAutoContextNotice(null); setFixTasks([]); setStreamError('');
                        }}
                        disabled={streaming || loadingSuggest}
                        title="通用聊天：自由讨论任何话题，命中创作关键词自动提示一键入库。自然语言直接说需求即可。"
                      >💬 通用</button>
                      {dimensions.filter(d => ['concept', 'key_rules', 'worldbuilding'].includes(d.key)).map(d => (
                        <button
                          key={d.key}
                          className={`smart-dim-btn ${selectedDim === d.key ? 'active' : ''}`}
                          onClick={() => { setSelectedDim(d.key); setSuggestions([]); setSelectedSuggestion(null); setInput(''); }}
                          disabled={streaming || loadingSuggest}
                          title={d.key === 'key_rules' ? '能力体系/科技树等硬规则（生成时同步产出文风指南）' : d.hint}
                        >{d.icon} {d.label}</button>
                      ))}
                    </div>
                    <div className="smart-dim-row">
                      {['plot_design', 'character_profiles', 'timeline', 'foreshadowing'].map(key => {
                        const d = dimensions.find(dim => dim.key === key);
                        if (!d) return null;
                        return (
                          <button
                            key={d.key}
                            className={`smart-dim-btn ${selectedDim === d.key ? 'active' : ''}`}
                            onClick={() => { setSelectedDim(d.key); setSuggestions([]); setSelectedSuggestion(null); setInput(''); }}
                            disabled={streaming || loadingSuggest}
                            title={d.hint}
                          >{d.icon} {d.label}</button>
                        );
                      })}
                    </div>
                  </div>
                  {/* 通用维度：顶部"助手切换"（折叠式选择器，视感对齐技能包，含联网搜索Key配置）；其他维度保持技能包选择 */}
                  {selectedDim === 'general' ? (
                    <GeneralAssistantSelector
                      roles={BUILTIN_ROLES}
                      currentId={sessionRoleMap[chatGeneralSessionId || '__general_pending_session__'] || 'default'}
                      onSelect={(id) => setSessionRoleMap(m => ({ ...m, [chatGeneralSessionId || '__general_pending_session__']: id }))}
                    />
                  ) : (
                    <SkillPackSelector packs={skillPacks.filter(p => p.category === 'master')} selected={settingPacks} onToggle={(id) => toggleSkillPack('setting', id)} onPreview={(pack) => setPreviewPack(pack)} compact />
                  )}
                  </div>
                  {/* 修正任务清单（从防遗忘报告违规项带入，支持多维度连续修正并追踪进度） */}
                  {fixTasks.length > 0 && (
                    <div className="fix-tasks-panel">
                      <div className="fix-tasks-head">
                        <span className="fix-tasks-title">📋 设定修正任务清单</span>
                        <span className="fix-tasks-progress">
                          {fixTasks.filter(t => t.done).length}/{fixTasks.length} 已完成
                        </span>
                      </div>
                      <div className="fix-tasks-list">
                        {fixTasks.map((task, idx) => {
                          const dimLabel = task.dimKey ? (dimensions.find(d => d.key === task.dimKey)?.label || task.dimKey) : '未匹配';
                          const dimIcon = task.dimKey ? (dimensions.find(d => d.key === task.dimKey)?.icon || '📌') : '❓';
                          return (
                            <div key={idx} className={`fix-task-item ${task.done ? 'fix-task-done' : ''}`}>
                              <label className="fix-task-check">
                                <input
                                  type="checkbox"
                                  checked={!!task.done}
                                  onChange={() => setFixTasks(prev => prev.map((t, i) => i === idx ? { ...t, done: !t.done } : t))}
                                  disabled={streaming}
                                />
                              </label>
                              <div className="fix-task-content">
                                <div className="fix-task-location">
                                  {task.location || '（未指定位置）'}
                                  <span className="fix-task-dim-tag">{dimIcon} {dimLabel}</span>
                                  {task.severity && <span className={`fix-task-sev sev-${task.severity}`}>{task.severity}</span>}
                                </div>
                                <div className="fix-task-desc">{task.desc}</div>
                                {task.fix && <div className="fix-task-fix">建议：{task.fix}</div>}
                              </div>
                              {task.dimKey && task.dimKey !== 'general' && !task.done && (
                                <button
                                  className="fix-task-action"
                                  disabled={streaming || loadingSuggest}
                                  onClick={() => {
                                    setSelectedDim(task.dimKey!);
                                    setSuggestions([]);
                                    setSelectedSuggestion(null);
                                    setInput(`修正意见：${task.desc}${task.fix ? `（${task.fix}）` : ''}`);
                                  }}
                                  title="定位到该维度并填充修正意见"
                                >去修正</button>
                              )}
                              {task.done && <span className="fix-task-done-tag">✓ 已处理</span>}
                            </div>
                          );
                        })}
                      </div>
                      {/* 一键继续下一个未完成的设定任务 */}
                      {Array.isArray(fixTasks) && fixTasks.some(t => !t.done) && (
                        <button
                          className="fix-tasks-continue"
                          disabled={streaming || loadingSuggest}
                          onClick={() => {
                            const next = fixTasks.find(t => !t.done && t.dimKey && t.dimKey !== 'general');
                            if (!next || !next.dimKey) return;
                            setSelectedDim(next.dimKey!);
                            setSuggestions([]);
                            setSelectedSuggestion(null);
                            setInput(`修正意见：${next.desc}${next.fix ? `（${next.fix}）` : ''}`);
                          }}
                          title="自动定位到第一个未完成的维度并填充意见"
                        >▶ 继续下一个未完成</button>
                      )}
                      {fixTasks.every(t => t.done) && (
                        <div className="fix-tasks-all-done">🎉 全部设定修正任务已完成</div>
                      )}
                    </div>
                  )}

                  {/* (Q1) 原来的"⚡动作影响预览"面板已移除：
                      · rename_entity → 直接走顶栏「🏗️实体管理」打开 EntityRegistryModal（与创作页实体管理同一套API/UI）
                      · edit_dim → 未来若有需要再加，不与实体管理功能重复
                  */}
                </>
              )}

              {activeTab === 'chapter' && (
                <>
                  {/* 最新章节信息行 + 内联🔄刷新按钮 */}
                  <div className="smart-chapter-info smart-chapter-info-row">
                    <span className="smart-chapter-info-text">
                      {latestChapter ? (
                        <>📖 最新：<strong>{formatChapterTitle(latestChapter)}</strong>（{latestChapter.word_count}字，第{displayChapterNum(latestChapter)}章）</>
                      ) : (
                        <>📖 还没有章节，将创建第 1 章</>
                      )}
                    </span>
                    <button
                      className="smart-chapter-refresh"
                      onClick={refreshChapterAnchor}
                      disabled={streaming}
                      title="刷新章节定位（写作后会自动填入新章节到目录）"
                    >🔄</button>
                  </div>
                  {/* 章节选择器（用于「修改」模式定位目标章节） */}
                  <div className="smart-chapter-select">
                    <label>修改目标章节：</label>
                    {chapters.length === 0 ? (
                      <span className="smart-empty-hint">暂无章节</span>
                    ) : (
                      <select
                        value={chapterTargetId || ''}
                        onChange={e => setChapterTargetId(e.target.value)}
                        disabled={streaming}
                      >
                        <option value="">从输入框解析章节号</option>
                        {[...chapters].sort((a, b) => b.order_index - a.order_index).map(c => (
                          <option key={c.id} value={c.id}>{formatChapterOption(c)}</option>
                        ))}
                      </select>
                    )}
                  </div>
                  {/* 修正任务清单（从防遗忘报告违规项带入，支持多章连续修正并追踪进度） */}
                  {fixTasks.length > 0 && (
                    <div className="fix-tasks-panel">
                      <div className="fix-tasks-head">
                        <span className="fix-tasks-title">📋 修正任务清单</span>
                        <span className="fix-tasks-progress">
                          {fixTasks.filter(t => t.done).length}/{fixTasks.length} 已完成
                        </span>
                      </div>
                      <div className="fix-tasks-list">
                        {fixTasks.map((task, idx) => (
                          <div key={idx} className={`fix-task-item ${task.done ? 'fix-task-done' : ''}`}>
                            <label className="fix-task-check">
                              <input
                                type="checkbox"
                                checked={!!task.done}
                                onChange={() => setFixTasks(prev => prev.map((t, i) => i === idx ? { ...t, done: !t.done } : t))}
                                disabled={streaming}
                              />
                            </label>
                            <div className="fix-task-content">
                              <div className="fix-task-location">
                                {task.location}
                                {task.severity && <span className={`fix-task-sev sev-${task.severity}`}>{task.severity}</span>}
                                {!task.chapterId && <span className="fix-task-unmatch">未匹配章节</span>}
                              </div>
                              <div className="fix-task-desc">{task.desc}</div>
                              {task.fix && <div className="fix-task-fix">建议：{task.fix}</div>}
                            </div>
                            {task.chapterId && !task.done && (
                              <button
                                className="fix-task-action"
                                disabled={streaming}
                                onClick={() => {
                                  setChapterTargetId(task.chapterId!);
                                  setInput(`修改意见：${task.desc}${task.fix ? `（${task.fix}）` : ''}`);
                                }}
                                title="定位到该章节并填充修改意见"
                              >去修改</button>
                            )}
                            {task.done && <span className="fix-task-done-tag">✓ 已处理</span>}
                          </div>
                        ))}
                      </div>
                      {/* 一键继续下一个未完成任务 */}
                      {fixTasks.some(t => !t.done) && (
                        <button
                          className="fix-tasks-continue"
                          disabled={streaming}
                          onClick={() => {
                            const next = fixTasks.find(t => !t.done && t.chapterId);
                            if (!next) return;
                            setChapterTargetId(next.chapterId!);
                            setInput(`修改意见：${next.desc}${next.fix ? `（${next.fix}）` : ''}`);
                          }}
                          title="自动定位到第一个未完成的章节并填充意见"
                        >▶ 继续下一个未完成</button>
                      )}
                      {fixTasks.every(t => t.done) && (
                        <div className="fix-tasks-all-done">🎉 全部修正任务已完成</div>
                      )}
                    </div>
                  )}
                  {/* 写作 / 修改 两按钮一排（直接执行，无需切换模式） */}
                  <div className="smart-chapter-actions smart-chapter-actions-row">
                    <button
                      className="smart-action-btn primary"
                      onClick={() => doChapterAction('continue', null, input.trim() || undefined)}
                      disabled={streaming}
                      title="续写下一章（输入框内容作为写作要求）"
                    >✍️ 写作第 {nextChapterNum} 章</button>
                    <button
                      className={`smart-action-btn ${chapterEditArmed ? 'primary' : ''}`}
                      onClick={() => {
                        const text = input.trim();
                        // 第一次点击：进入"待执行"模式，让用户先在章节下拉里选目标章节（输入框可填修改意见）
                        if (!chapterEditArmed) {
                          if (!text && !chapterTargetId && chapters.length === 0) {
                            setStreamError('请先在下方"修改目标章节"下拉选章，或在输入框写明「第3章 …」');
                            return;
                          }
                          setChapterEditArmed(true);
                          return;
                        }
                        // 第二次点击：真正执行
                        if (chapterTargetId) {
                          doChapterActionWithNote('polish', chapterTargetId, text || '按检查报告修正违规项');
                          setChapterEditArmed(false);
                          return;
                        }
                        if (!text) {
                          setStreamError('请在输入框说明要修改哪一章及修改意见（如「第3章，增加主角心理描写」），或在上方下拉选择章节');
                          setChapterEditArmed(false);
                          return;
                        }
                        handleChapterSend();
                        setChapterEditArmed(false);
                      }}
                      disabled={streaming || chapters.length === 0}
                      title={chapterEditArmed
                        ? '再点一次开始修改（修改目标章节：' +
                          (chapterTargetId
                            ? (chapters.find(c => c.id === chapterTargetId) ? formatChapterOption(chapters.find(c => c.id === chapterTargetId)!) : '已选')
                            : '从输入框解析') + '）'
                        : '修改已写章节（先点此按钮进入"待修改"模式，再点一次才执行）'}
                    >{chapterEditArmed ? '🚀 开始修改' : '✨ 修改'}</button>
                    {chapterEditArmed && (
                      <button
                        className="smart-action-btn"
                        onClick={() => setChapterEditArmed(false)}
                        disabled={streaming}
                        title="退出待修改模式"
                      >取消</button>
                    )}
                  </div>
                  <SkillPackSelector packs={skillPacks.filter(p => p.category === 'style')} selected={chapterPacks} onToggle={(id) => toggleSkillPack('chapter', id)} onPreview={(pack) => setPreviewPack(pack)} compact />
                </>
              )}

              {activeTab === 'deai' && (
                <>
                  <div className="smart-chapter-select">
                    <label>选择去AI味的章节：</label>
                    {chapters.length === 0 ? (
                      <span className="smart-empty-hint">暂无章节</span>
                    ) : (
                      <select
                        value={deaiTargetId || ''}
                        onChange={e => setDeaiTargetId(e.target.value)}
                        disabled={streaming}
                      >
                        <option value="">请选择章节…</option>
                        {[...chapters].sort((a, b) => b.order_index - a.order_index).slice(0, 10).map(c => (
                          <option key={c.id} value={c.id}>{formatChapterOption(c)}</option>
                        ))}
                      </select>
                    )}
                  </div>
                  {chapters.length > 10 && (
                    <div className="smart-deai-hint">💡 仅显示最新10章，其他章节可在下方消息框输入「第N章」指定</div>
                  )}
                  <SkillPackSelector packs={skillPacks.filter(p => p.category === 'review')} selected={deaiPacks_selected} onToggle={(id) => toggleSkillPack('deai', id)} onPreview={(pack) => setPreviewPack(pack)} compact />
                </>
              )}

              {activeTab === 'review' && (
                <>
                  <div className="smart-review-actions">
                    <button
                      className={`smart-action-btn ${reviewArmedMode === 'anti_forget' ? 'primary armed' : 'primary'}`}
                      onClick={() => handleReview('anti_forget')}
                      disabled={reviewing || streaming}
                    >
                      {reviewArmedMode === 'anti_forget' ? <>✅ 确认：{_reviewScopeAntiForget}</> : <>🔍 防遗忘检查</>}
                    </button>
                    <button
                      className={`smart-action-btn ${reviewArmedMode === 'consistency' ? 'armed' : ''}`}
                      onClick={() => handleReview('consistency')}
                      disabled={reviewing || streaming}
                    >
                      {reviewArmedMode === 'consistency' ? <>✅ 确认：{_reviewScopeConsistency}</> : <>⚖️ 一致性检查</>}
                    </button>
                  </div>
                  {/* 校审范围：两行下拉选择（按卷 + 一致性章节），样式统一对齐 */}
                  <div className={`smart-review-scope ${reviewArmedMode ? 'armed' : ''}`}>
                    {volumes.length > 0 && (
                      <div className="smart-chapter-select smart-review-scope-row">
                        <label>📚 按卷（不选=全书）</label>
                        <select
                          className="smart-review-scope-select"
                          value={reviewVolumeIds[0] || ''}
                          onChange={e => {
                            const v = e.target.value;
                            setReviewVolumeIds(v ? [v] : []);
                            setReviewArmedMode(null);  // 切范围 → 出确认态，避免按旧范围误跑
                          }}
                          disabled={reviewing || streaming}
                        >
                          <option value="">全书</option>
                          {volumes.map(v => (
                            <option key={v.id} value={v.id}>{v.title}（{v.chapter_count}章）</option>
                          ))}
                        </select>
                      </div>
                    )}
                    {chapters.length > 0 && (
                      <div className="smart-chapter-select smart-review-scope-row">
                        <label>📖 一致性章节（不选=最新）</label>
                        <select
                          className="smart-review-scope-select"
                          value={reviewChapterId || ''}
                          onChange={e => {
                            setReviewChapterId(e.target.value || null);
                            setReviewArmedMode(null);  // 切章 → 出确认态
                          }}
                          disabled={reviewing || streaming}
                        >
                          <option value="">最新章节</option>
                          {chapters.map(c => (
                            <option key={c.id} value={c.id}>{formatChapterTitle(c)}</option>
                          ))}
                        </select>
                      </div>
                    )}
                  </div>

                  {/* 🧩 事件日志 + 🧠 系统学习：同一行（桌面+手机端都并排）*/}
                  <div className="review-grid-row">
                  {/* Q2 合并：🧩 事件日志素材管理（原工具栏浮层）
                     校审输入是"防遗忘/一致性检查"，事件日志是它们的素材来源与质量保障。
                     重算后事件日志更准，校审出的防遗忘/一致性报告也更准。 */}
                  <div className="impact-preview-panel review-grid-cell">
                    <div className="impact-preview-head" onClick={() => setShowBackfill(s => !s)}>
                      <span>🧩 事件日志</span>
                      <span className="impact-preview-toggle">{showBackfill ? '▲' : '▼'}</span>
                    </div>
                    {showBackfill && (
                      <div className="impact-preview-body">
                        <div className="impact-preview-actions">
                          <label style={{fontSize:12,color:'var(--text-secondary)'}}>抽取策略：</label>
                          <select value={backfillLLM} onChange={e => setBackfillLLM(e.target.value as any)} disabled={backfillRunning}>
                            <option value="auto">auto（推荐：卷首/高潮/卷末用LLM，其他章正则）</option>
                            <option value="always">always（全部LLM，成本高，保真度最佳）</option>
                            <option value="never">never（只用正则，零成本，速度最快）</option>
                          </select>
                          <button onClick={runBackfill} disabled={streaming || backfillRunning} style={{minWidth:120}}>
                            {backfillRunning ? '运行中…' : '🚀 全文重算'}
                          </button>
                        </div>
                        <div style={{fontSize:11,color:'var(--text-muted)',marginTop:4}}>
                          重算完成后，再点上方「🔍 防遗忘 / ⚖️ 一致性」会基于最新事件日志给出更准的校审报告。
                        </div>
                        {backfillStatus && (
                          <div className="backfill-status">
                            <div><strong>状态：</strong>{backfillStatus.text}</div>
                            {(backfillStatus.progress !== undefined) && (
                              <div className="backfill-progress-row">
                                <div className="backfill-progress-bar">
                                  <div className="backfill-progress-fill"
                                       style={{width: `${Math.round(100 * backfillStatus.progress.done / (backfillStatus.progress.total || 1))}%`}} />
                                </div>
                                <span>{backfillStatus.progress.done}/{backfillStatus.progress.total} ·
                                  事件+{backfillStatus.progress.added} · LLM {backfillStatus.progress.llm} · 正则 {backfillStatus.progress.rule}
                                </span>
                              </div>
                            )}
                            {backfillStatus.log.length > 0 && (
                              <details>
                                <summary>详细日志</summary>
                                <pre className="backfill-log">{backfillStatus.log.join('\n')}</pre>
                              </details>
                            )}
                          </div>
                        )}
                      </div>
                    )}
                  </div>

                  {/* Q2 合并 + C 闭环：🧠 系统学习与优化建议
                     本质：校审/门禁/章节 PostGenValidator 出错 → 自动写入 FailureDB → 累积 ≥2 条同类 → 出 prompt 补丁建议
                     用户可 ✅采纳（补丁自动追加到系统 prompt 末尾，后续所有维度/章节生成都生效）· 📝自定义编辑 · ❌忽略 */}
                  <div className="opt-report-inline impact-preview-panel review-grid-cell">
                    <div className="impact-preview-head">
                      <span className="smart-flex-fill" onClick={(e) => { e.stopPropagation(); setShowOptReport(s => !s); }} style={{cursor:'pointer'}}>
                        🧠 系统学习
                        {optimizationReport && optimizationReport.failure_count > 0 && (
                          <span className="chat-tool-badge" style={{marginLeft:6,fontSize:10}}>{optimizationReport.failure_count}</span>
                        )}
                        {optimizationReport && (optimizationReport.applied_patch_count || 0) > 0 && (
                          <span className="chat-tool-badge" style={{marginLeft:4,fontSize:10,background:'var(--accent-light)',color:'var(--accent)'}}>
                            ✓ {optimizationReport.applied_patch_count}
                          </span>
                        )}
                      </span>
                      <span style={{display:'flex',alignItems:'center',gap:4,flexShrink:0}}>
                        <button className="btn-ghost-sm" style={{padding:'2px 6px',fontSize:11,lineHeight:1}} onClick={async (e) => {
                          e.stopPropagation();
                          if (!bookId) return;
                          try {
                            const r: any = await api.getOptimizationReport(bookId);
                            setOptimizationReport(r);
                          } catch {}
                        }}>🔄 刷新</button>
                        <span className="impact-preview-toggle" style={{cursor:'pointer'}} onClick={(e) => { e.stopPropagation(); setShowOptReport(s => !s); }}>{showOptReport ? '▲' : '▼'}</span>
                      </span>
                    </div>

                    {showOptReport && (
                    <div className="impact-preview-body">
                      {!optimizationReport ? (
                        <div className="opt-report-empty" style={{padding: '10px 4px'}}>
                          <div className="opt-report-empty-icon" style={{fontSize:20}}>🪄</div>
                          <div style={{fontSize:12}}>点右上「🔄 刷新」扫描 FailureDB</div>
                          <div className="opt-report-empty-sub" style={{fontSize:11}}>
                            跑过校审/章节门禁后，这里会根据累积的失败记录自动生成优化建议。
                          </div>
                        </div>
                      ) : optimizationReport.failure_count === 0 ||
                           optimizationReport.suggestions.filter((s: any) => !locallyDismissedBuckets.has(s.bucket_key)).length === 0 ? (
                        <div className="opt-report-empty" style={{padding: '10px 4px'}}>
                          <div className="opt-report-empty-icon" style={{fontSize:20}}>✅</div>
                          <div style={{fontSize:12}}>
                            {optimizationReport.ready ? '当前没有可处理的建议' : '暂无高频失败模式'}
                          </div>
                          <div className="opt-report-empty-sub" style={{fontSize:11}}>
                            {optimizationReport.reason ||
                              '继续创作并多跑几次校审（防遗忘/一致性）、或章节生成，等同类问题出现 ≥2 次就会出具体建议。'}
                            {optimizationReport.ignored_bucket_count ? ` 已忽略 ${optimizationReport.ignored_bucket_count} 类。` : ''}
                          </div>
                        </div>
                      ) : (
                        <>
                          <div className="opt-report-summary">
                            累计发现 <strong>{optimizationReport.failure_count}</strong> 条失败记录
                            {optimizationReport.ignored_bucket_count ? `（已忽略 ${optimizationReport.ignored_bucket_count} 类）` : ''}
                            ，可处理 <strong>{optimizationReport.suggestions.filter((s:any)=>!locallyDismissedBuckets.has(s.bucket_key)).length}</strong> 条优化建议
                          </div>
                          <div className="opt-report-list">
                            {optimizationReport.suggestions
                              .filter((s: any) => !locallyDismissedBuckets.has(s.bucket_key))
                              .map((s: any, idx: number) => {
                                const isEditing = editingBucket === s.bucket_key;
                                const isBusy = optBusyBucket === s.bucket_key;
                                return (
                                  <div key={s.bucket_key || idx} className={`opt-report-item sev-${s.severity || 'medium'}`}>
                                    <div className="opt-report-item-head">
                                      <span className="opt-report-cat">{s.category_cn || s.category}</span>
                                      <span className="opt-report-count">{s.count} 次</span>
                                      {s.dim_key && s.affected_dims && s.affected_dims.length === 0 && (
                                        <span style={{fontSize:10, color:'var(--text-muted)'}}>
                                          维度：{dimensions.find(d=>d.key===s.dim_key)?.label || s.dim_key}
                                        </span>
                                      )}
                                    </div>
                                    <div className="opt-report-pattern">{s.problem_pattern || s.pattern}</div>
                                    {s.affected_dims && s.affected_dims.length > 0 && (
                                      <div className="opt-report-dims">
                                        影响维度：{s.affected_dims.map((dk: string) => dimensions.find(d => d.key === dk)?.label || dk).join('、')}
                                      </div>
                                    )}
                                    {/* 示例片段（支持多条） */}
                                    {s.examples && s.examples.length > 0 && (
                                      <details className="opt-report-snippet" style={{marginTop:4}}>
                                        <summary style={{cursor:'pointer', fontSize:11, color:'var(--text-muted)'}}>
                                          📎 失败片段（{s.examples.length} 条）
                                        </summary>
                                        {s.examples.map((ex:any, i:number) => (
                                          <div key={i} style={{marginTop:4, padding:'4px 6px', background:'var(--bg-secondary)', borderRadius:4}}>
                                            {ex.chapter_num ? <div style={{fontSize:10, color:'var(--accent)'}}>第{ex.chapter_num}章 · {ex.ts?.slice(0,16) || ''}</div> : null}
                                            <div style={{fontSize:11, marginBottom:2}}>{ex.summary}</div>
                                            {ex.snippet && <pre style={{margin:0, fontSize:11, whiteSpace:'pre-wrap', wordBreak:'break-word'}}>{ex.snippet}</pre>}
                                          </div>
                                        ))}
                                      </details>
                                    )}
                                    {(!s.examples || s.examples.length === 0) && s.sample_snippet && (
                                      <div className="opt-report-snippet">
                                        <div className="opt-report-snippet-title">示例片段：</div>
                                        <pre>{s.sample_snippet}</pre>
                                      </div>
                                    )}
                                    {/* 补丁区：可编辑/不可编辑两态 */}
                                    <div className="opt-report-patch">
                                      <div className="opt-report-patch-title">
                                        补丁（采纳后追加到系统 prompt 末尾）：
                                      </div>
                                      {isEditing ? (
                                        <>
                                          <textarea
                                            value={editingPatch}
                                            onChange={e => setEditingPatch(e.target.value)}
                                            disabled={isBusy}
                                            style={{
                                              width:'100%', minHeight:96, padding:'8px',
                                              fontFamily:'var(--font-mono, ui-monospace, monospace)', fontSize:12,
                                              border:'1px solid var(--accent)', borderRadius:6,
                                              background:'var(--bg-secondary)', color:'var(--text-primary)',
                                              resize:'vertical',
                                            }}
                                          />
                                          <div style={{marginTop:6, display:'flex', gap:6, flexWrap:'wrap'}}>
                                            <button className="btn-sm" disabled={isBusy || !editingPatch.trim()}
                                              onClick={async () => {
                                                if (!bookId) return;
                                                setOptBusyBucket(s.bucket_key);
                                                try {
                                                  await (api as any).adoptOptimizationSuggestion(bookId, {
                                                    bucket_key: s.bucket_key,
                                                    category: s.category,
                                                    dim_key: s.dim_key,
                                                    patch_text: editingPatch.trim(),
                                                  });
                                                  setLocallyDismissedBuckets(prev => { const n=new Set(prev); n.add(s.bucket_key); return n; });
                                                  // 刷新总面板（含 applied_patches 预览）
                                                  const r: any = await api.getOptimizationReport(bookId);
                                                  setOptimizationReport(r);
                                                  setEditingBucket(null);
                                                  setEditingPatch('');
                                                } catch (e: any) {
                                                  alert('采纳失败：' + (e.message || e));
                                                } finally {
                                                  setOptBusyBucket(null);
                                                }
                                              }}>💾 保存并采纳</button>
                                            <button className="btn-ghost-sm" onClick={() => { setEditingBucket(null); setEditingPatch(''); }}>
                                              取消
                                            </button>
                                          </div>
                                        </>
                                      ) : (
                                        <>
                                          <div className="opt-report-patch-body">{s.proposed_patch}</div>
                                          <div className="opt-report-actions" style={{marginTop:8, display:'flex', gap:6, flexWrap:'wrap'}}>
                                            <button className="btn-sm primary" disabled={isBusy}
                                              onClick={async () => {
                                                if (!bookId) return;
                                                setOptBusyBucket(s.bucket_key);
                                                try {
                                                  await (api as any).adoptOptimizationSuggestion(bookId, {
                                                    bucket_key: s.bucket_key,
                                                    category: s.category,
                                                    dim_key: s.dim_key,
                                                    patch_text: s.proposed_patch,
                                                  });
                                                  setLocallyDismissedBuckets(prev => { const n=new Set(prev); n.add(s.bucket_key); return n; });
                                                  const r: any = await api.getOptimizationReport(bookId);
                                                  setOptimizationReport(r);
                                                } catch (e: any) {
                                                  alert('采纳失败：' + (e.message || e));
                                                } finally {
                                                  setOptBusyBucket(null);
                                                }
                                              }}>
                                              {isBusy ? '处理中…' : '✅ 采纳建议'}
                                            </button>
                                            <button className="btn-ghost-sm" disabled={isBusy}
                                              onClick={() => {
                                                setEditingBucket(s.bucket_key);
                                                setEditingPatch(s.proposed_patch || '');
                                              }}>📝 自定义编辑</button>
                                            <button className="btn-ghost-sm" disabled={isBusy}
                                              onClick={async () => {
                                                if (!bookId) return;
                                                if (!window.confirm('确认忽略此建议？忽略后不会再显示，可刷新重新扫描。')) return;
                                                setOptBusyBucket(s.bucket_key);
                                                try {
                                                  await (api as any).dismissOptimizationSuggestion(bookId, s.bucket_key);
                                                  setLocallyDismissedBuckets(prev => { const n=new Set(prev); n.add(s.bucket_key); return n; });
                                                } catch (e: any) {
                                                  alert('忽略失败：' + (e.message || e));
                                                } finally {
                                                  setOptBusyBucket(null);
                                                }
                                              }}>❌ 忽略</button>
                                          </div>
                                        </>
                                      )}
                                    </div>
                                  </div>
                                );
                              })}
                          </div>
                        </>
                      )}
                    </div>
                    )}
                  </div>

                  </div>{/* /.review-grid-row */}
                </>
              )}
            </div>

            {/* 多选意见列表（设定Tab专属，仅对方案选择型维度显示） */}
            {activeTab === 'setting' && suggestions.length > 0 && shouldShowSuggestions(selectedDim, bible) && (
              <div className="smart-suggestions">
                <div className="smart-suggestions-head">请选择一个方案，AI 将基于它生成完整内容：</div>
                {suggestions.map((s, i) => {
                  let schemeLabel = ['方案一', '方案二', '方案三', '方案四', '方案五'][i] || `方案${i + 1}`;
                  if (s._from_user) {
                    const userSchemeCount = suggestions.filter(x => x._from_user).length;
                    schemeLabel = `📝 我的方案${userSchemeCount > 1 ? ` ${i + 1}` : ''}`;
                  }
                  return (
                    <button
                      key={s.id}
                      className={`smart-suggestion-item ${s._from_user ? 'user-suggestion' : ''}`}
                      onClick={() => handleGenerate(s, i)}
                      disabled={streaming}
                    >
                      {s._from_user && (
                        <div className="user-suggestion-badge">📝 我的创作内容 · 直接落地不改动原文</div>
                      )}
                      <div className="smart-suggestion-title">
                        {!s._from_user && <>{schemeLabel}：</>}
                        {s.title}
                      </div>
                      <div className="smart-suggestion-preview">{s.preview}</div>
                    </button>
                  );
                })}
                <button className="smart-suggestion-cancel" onClick={() => { setSuggestions([]); setSelectedSuggestion(null); }}>取消，重新描述需求</button>
              </div>
            )}

            {/* 消息列表 */}
            <div className="chat-messages" ref={scrollRef}>
              {messages.length === 0 && !loadingSuggest && (
                <div className="chat-empty">
                  <div className="chat-empty-icon"><CarLogo size={56} /></div>
                  {activeTab === 'setting' && selectedDim === 'general' ? (
                    <div style={{ textAlign: 'center' }}>
                      <p style={{ fontSize: 15, fontWeight: 600, color: '#1e1b4b' }}>💬 通用聊天模式 · 想聊啥就聊啥</p>
                      <p style={{ fontSize: 13, color: '#6b7280', margin: '8px 0 0' }}>命中创作关键词自动提示一键入库 📦。自然语言聊天，直接说需求即可。</p>
                    </div>
                  ) : (
                    <p>AI 智驾已就绪。选择上方维度或操作，开始人机协作创作。</p>
                  )}
                </div>
              )}
              {loadingSuggest && (
                <div className="chat-empty">
                  <div className="chat-empty-icon">⏳</div>
                  <p>AI 正在生成多选方案…</p>
                </div>
              )}
              {messages.map((m, i) => (
                <MessageBubble
                  key={i}
                  index={i}
                  message={m}
                  onAdopt={handleAdopt}
                  onEdit={handleEdit}
                  onIgnore={handleIgnore}
                  applyingCardId={applyingCardId}
                  streaming={streaming && i === messages.length - 1 && m.role === 'assistant'}
                  onReplaceChapter={handleReplaceChapter}
                  onEditMessage={handleEditMessage}
                  onDeleteMessage={handleDeleteMessage}
                  onRegenerate={handleRegenerate}
                  bookId={bookId || undefined}
                  bible={bible || undefined}
                  onBibleUpdate={setBible}
                  selectedSkillPackIds={activeTab === 'deai' ? deaiPacks_selected : activeTab === 'chapter' ? chapterPacks : settingPacks}
                  chaptersPerVolume={50}
                />
              ))}
              {streamError && <div className="chat-error">{streamError}</div>}
            </div>

            {/* 去AITab的主操作按钮（在输入框上方） */}
            {activeTab === 'deai' && (
              <div className="smart-main-action-bar" style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                <button
                  className="smart-main-action"
                  onClick={handleDeai}
                  disabled={streaming}
                  style={{ flex: '0 0 auto' }}
                >{streaming ? '处理中…' : '🧹 开始去AI味'}</button>
                <button
                  className="smart-main-action"
                  onClick={handleStyleAlign}
                  disabled={streaming}
                  style={{
                    flex: '0 0 auto',
                    background: streaming ? '#eee' : 'linear-gradient(135deg,#667eea 0%,#764ba2 100%)',
                    color: '#fff',
                    border: 'none',
                  }}
                >🎯 风格对齐诊断</button>
                {!deaiTargetId && <span className="smart-main-hint">未选章节时可在输入框输入「第N章」</span>}
              </div>
            )}

            {/* 自动上下文命中提示：已定位并注入章节/维度资料 */}
            {autoContextNotice && (autoContextNotice.chapters.length > 0 || autoContextNotice.dims.length > 0) && (
              <div className="auto-context-notice" onClick={() => setAutoContextNotice(null)} title="点击关闭">
                <span className="acn-icon">🎯</span>
                <span className="acn-text">
                  已自动定位并注入：
                  {autoContextNotice.dims.length > 0 && (
                    <span>「{autoContextNotice.dims.map(d => d.label).join('、')}」维度内容</span>
                  )}
                  {autoContextNotice.dims.length > 0 && autoContextNotice.chapters.length > 0 && <span> + </span>}
                  {autoContextNotice.chapters.length > 0 && (
                    <span>
                      {autoContextNotice.chapters.length} 章原文
                      {autoContextNotice.chapters.length <= 3 && (
                        <span className="acn-chips">
                          {autoContextNotice.chapters.map(c => (
                            <span key={c.id} className="acn-chip">{c.title}</span>
                          ))}
                        </span>
                      )}
                    </span>
                  )}
                  <span className="acn-sub"> — 智驾已直接读取资料，无需再粘贴。点击关闭本提示</span>
                </span>
                <span className="acn-close" style={{ marginLeft: 'auto', padding: '0 6px', color: '#999', cursor: 'pointer' }}>×</span>
              </div>
            )}

            {/* 输入区（设定Tab可输入；正文Tab修改模式可输入） */}
            {activeTab === 'setting' && (
              <div className="chat-input-area">
                {/* ── 通用Tab工具栏：🤖模型/🎭角色/📥导入卡 三个放同一排，手机端自动紧凑不占高 ── */}
                {selectedDim === 'general' && (() => {
                  // 【先发消息再切 → 打开页面就能切】
                  //   有真实会话用真实 chatGeneralSessionId；没建立会话 → 统一预会话 key，先把选择记住
                  //   等第一条消息发送完毕后，consumeSSE onSessionId 会自动把预会话迁移到真实 sid
                  const _PREKEY = '__general_pending_session__';
                  const _sidReal = chatGeneralSessionId || '';
                  const _sid = _sidReal || _PREKEY;
                  // ============== 🤖 模型 ==============
                  const _chosenId = sessionModelMap[_sid];
                  const _chosen = aiConfigList.length > 0
                    ? (aiConfigList.find(c => c.id === _chosenId) || aiConfigList.find(c => c.is_active) || aiConfigList[0])
                    : undefined;

                  // 按钮高度/基础样式：统一 height 28px，四按钮宽度等分 justify-content:space-between
                  const _chipBase: React.CSSProperties = {
                    padding: '4px 10px',
                    height: 28, lineHeight: '20px',
                    borderRadius: 999, fontSize: 12, display: 'inline-flex',
                    alignItems: 'center', justifyContent: 'center', gap: 4,
                    whiteSpace: 'nowrap',
                    boxSizing: 'border-box',
                    overflow: 'hidden',
                  };
                  return (
                    <>
                      {/* ── ① 外层：浮层定位参照物，overflow:visible 保证浮层不被 clip ── */}
                      <div
                        style={{
                          position: 'relative', overflow: 'visible',
                          padding: '3px 10px 4px 10px',
                        }}
                      >
                        {/* ── ② 按钮等分容器：三按钮各占 1/3（减去 8px*2 的间隙），桌面/手机都是一排不折 ── */}
                        <div
                          className="general-toolbar-row"
                          style={{
                            display: 'flex', alignItems: 'center',
                            justifyContent: 'space-between',
                            gap: 8,
                            flexWrap: 'nowrap',
                          }}
                        >
                          <style>{`
                            .general-toolbar-row .gt-4cell { width: calc((100% - 24px) / 4); }
                            .general-toolbar-row .gt-toggle-on { border-color: #3ecf8e !important; background: #eafaf3 !important; color: #0a7d4f !important; box-shadow: 0 0 0 1px #3ecf8e66 !important; }
                            .general-toolbar-row .gt-toggle-off { opacity: .72; }
                            @media (max-width: 640px) {
                              .general-toolbar-row .gt-4cell { width: calc((100% - 24px) / 4); }
                              .general-toolbar-row .gt-model-text { display: none; }
                              .general-toolbar-row .gt-model-dot { display: none; }
                              .general-toolbar-row .gt-import-text { display: none; }
                            }
                          `}</style>

                          {/* ── 🤖 模型 Button：占 1/4 ── */}
                          <button
                            className="gt-4cell"
                            data-gt-model-chip
                            onClick={(e) => { e.stopPropagation(); setShowModelPicker(s => !s); }}
                            title={`会话级切换模型（当前：${_chosen?.name || '默认模型'} · ${_chosen?.model || ''}）`}
                            style={{
                              ..._chipBase,
                              border: '1px solid #d6d6e0',
                              background: '#fafafa',
                              cursor: 'pointer',
                              color: '#333',
                            }}
                          >
                            🤖{' '}
                            <span className="gt-model-text" style={{
                              fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 100,
                            }}>
                              {_chosen?.name || '默认模型'}
                            </span>
                            <span className="gt-model-dot" style={{ color: '#888' }}>·</span>
                            <span className="gt-model-text" style={{
                              color: '#666', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 110,
                            }}>
                              {_chosen?.model || ''}
                            </span>
                            <span style={{ color: '#aaa' }}>{showModelPicker ? '▲' : '▼'}</span>
                          </button>

                          {/* ── 🔍 联网搜索 Toggle：占 1/4（高亮=强制联网搜，未亮=自动判定） ── */}
                          <button
                            className={`gt-4cell ${generalWebSearch ? 'gt-toggle-on' : 'gt-toggle-off'}`}
                            onClick={(e) => { e.stopPropagation(); setGeneralWebSearch(s => !s); }}
                            title={generalWebSearch ? '🔍 联网搜索已开启：每次提问都先联网搜索最新资料再回答' : '🔍 点击开启联网搜索：每次提问都先联网搜索最新资料（不开时按内容自动判定）'}
                            style={{ ..._chipBase, border: '1px solid #d6e4d8', background: generalWebSearch ? '#eafaf3' : '#fafafa', cursor: 'pointer', color: generalWebSearch ? '#0a7d4f' : '#555' }}
                          >
                            🔍<span style={{ fontWeight: 600 }}>联网</span>
                          </button>

                          {/* ── 🧠 深度思考 Button：占 1/4（三档：关/标准/深度，点开选档） ── */}
                          <button
                            className={`gt-4cell ${generalDeepThink > 0 ? 'gt-toggle-on' : 'gt-toggle-off'}`}
                            data-gt-deeppicker-chip
                            onClick={(e) => { e.stopPropagation(); setDeepThinkOpen(s => !s); }}
                            title={`深度思考${generalDeepThink > 0 ? '：' + ({1: '标准思考', 2: '深度思考'} as Record<number, string>)[generalDeepThink] : '（关闭）'}. 点击选择思考程度`}
                            style={{ ..._chipBase, border: '1px solid #d8d0ec', background: generalDeepThink > 0 ? '#f1ecfa' : '#fafafa', cursor: 'pointer', color: generalDeepThink > 0 ? '#6236c9' : '#555' }}
                          >
                            <span style={{ fontWeight: 600 }}>
                              🧠{generalDeepThink === 2 ? '深度' : generalDeepThink === 1 ? '标准' : '思考'}
                            </span>
                            <span style={{ color: '#bbb' }}>{deepThinkOpen ? '▲' : '▼'}</span>
                          </button>

                          {/* ── 📥 导入角色卡 Button：占 1/4 ── */}
                          <input
                            ref={stCharCardRef}
                            type="file"
                            accept="application/json,.json"
                            style={{ display: 'none' }}
                            onChange={async (e) => {
                              const f = e.target.files?.[0];
                              e.target.value = '';
                              if (!f) return;
                              try {
                                const text = await f.text();
                                const raw = JSON.parse(text);
                                const firstStr = (v: any) => Array.isArray(v) ? String(v[0] || '') : String(v || '');
                                const name = firstStr(raw.name || raw.data?.name) || '未命名角色';
                                const description = firstStr(raw.description || raw.data?.description);
                                const personality = firstStr(raw.personality || raw.data?.personality);
                                const scenario = firstStr(raw.scenario || raw.data?.scenario);
                                const first_mes = firstStr(raw.first_mes || raw.data?.first_mes);
                                const mes_example = firstStr(raw.mes_example || raw.data?.mes_example || raw.example_dialogue || raw.data?.example_dialogue);
                                const creator = firstStr(raw.creator || raw.data?.creator || raw.creator_notes || raw.data?.creator_notes);
                                const sections: string[] = [`【角色名】${name}`];
                                if (personality) sections.push(`【性格/人格】\n${personality.trim()}`);
                                if (description) sections.push(`【外貌/背景描述】\n${description.trim()}`);
                                if (scenario) sections.push(`【所处剧情场景/当前局面】\n${scenario.trim()}`);
                                if (mes_example) sections.push(`【对白示例】\n${mes_example.trim()}`);
                                if (first_mes) sections.push(`【角色开场第一句话/动作】\n${first_mes.trim()}`);
                                if (creator) sections.push(`【创作者备注】\n${creator.trim()}`);
                                sections.push(`【Silly Tavern 角色卡源文件名】${f.name}`);
                                const draft = sections.join('\n\n').slice(0, 6000);
                                const suggestion: any = {
                                  id: 'import_' + Math.random().toString(36).slice(2, 10),
                                  dim: 'character_profiles',
                                  label: `导入角色：${name}`,
                                  preview: draft,
                                  _full_content: draft,
                                  _from_user: true,
                                  card_type: 'SAVE_CHARACTER',
                                  card_title: `🧙‍人物 · ${name}（Silly Tavern导入）`,
                                };
                                streamBufferRef.current = '';
                                setMessages((prev) => {
                                  const next = [...prev];
                                  next.push({ role: 'user', content: `【导入Silly Tavern角色卡】${f.name}` });
                                  next.push({ role: 'assistant', content: '', cards: [] });
                                  return next;
                                });
                                queueMicrotask(() => {
                                  setMessages((prev) => {
                                    const next = [...prev];
                                    const last = next[next.length - 1];
                                    if (last && last.role === 'assistant') {
                                      next[next.length - 1] = {
                                        ...last,
                                        cards: [...(last.cards || []), {
                                          id: suggestion.id,
                                          type: 'SAVE_CHARACTER',
                                          title: suggestion.card_title,
                                          content: draft,
                                          target: '人物',
                                          status: 'pending',
                                        }],
                                        content: `✨ 已从 Silly Tavern 角色卡 **${f.name}** 解析出人物草稿。下方卡片确认无误后，点「采纳落地」即入库到智驾的【人物】维度。\n\n预览：\n\n${draft.slice(0, 260)}${draft.length>260?'…':''}`,
                                      };
                                    }
                                    return next;
                                  });
                                });
                                setStImportMsg(`✅ 已解析角色卡：${name}，请确认并落地。`);
                              } catch (err: any) {
                                setStImportMsg(`❌ 解析失败：${err?.message || String(err)}（请检查是否为标准Silly Tavern JSON角色卡）`);
                              }
                              setTimeout(() => setStImportMsg(''), 6000);
                            }}
                          />
                          <button
                            className="gt-4cell"
                            onClick={(e) => { e.stopPropagation(); stCharCardRef.current?.click(); }}
                            style={{
                              ..._chipBase,
                              border: '1px solid #cfd8ff',
                              background: '#f4f6ff',
                              cursor: 'pointer',
                              color: '#2f4fcf',
                            }}
                            title="导入 Silly Tavern V2/V3 格式的 JSON 角色卡（非多模态），自动生成人物草稿并一键入库到【人物】维度"
                          >
                            📥<span className="gt-import-text">导入角色卡</span>
                          </button>
                        </div>

                        {/* 导入角色卡结果提示（贴在按钮行下方，长度不足时也显示一行小字） */}
                        {stImportMsg && (
                          <div style={{ fontSize: 12, padding: '2px 2px 0 2px' }}>
                            <span style={{
                              color: stImportMsg.startsWith('✅') ? '#288f2b' : '#c32e2e',
                            }}>
                              {stImportMsg}
                            </span>
                          </div>
                        )}

                        {/* ── 模型下拉浮层：挂外层 overflow:visible，不被横滑裁剪 ── */}
                        {showModelPicker && (
                          <div
                            data-gt-model-popover
                            onClick={e => e.stopPropagation()}
                            style={{
                              position: 'absolute', left: 10, bottom: '100%', marginBottom: 4, zIndex: 100,
                              minWidth: 260, maxHeight: 320, overflowY: 'auto', padding: 6,
                              background: '#fff', border: '1px solid #e0e0ea', borderRadius: 12,
                              boxShadow: '0 6px 20px rgba(0,0,0,0.08)',
                            }}
                          >
                            {aiConfigList.map(c => {
                              const active = c.id === _chosen?.id;
                              return (
                                <div
                                  key={c.id}
                                  onClick={() => {
                                    setSessionModelMap(m => ({ ...m, [_sid]: c.id }));
                                    setShowModelPicker(false);
                                  }}
                                  style={{
                                    padding: '8px 10px', borderRadius: 8,
                                    cursor: c.has_key ? 'pointer' : 'not-allowed',
                                    display: 'flex', flexDirection: 'column', gap: 2,
                                    background: active ? '#eef3ff' : 'transparent',
                                    border: active ? '1px solid #829cff' : '1px solid transparent',
                                    opacity: c.has_key ? 1 : 0.55,
                                  }}
                                >
                                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                    <span style={{ fontWeight: 600, fontSize: 13, color: '#222' }}>
                                      {c.name || '未命名配置'}{c.is_active ? ' 🌐' : ''}
                                    </span>
                                    {active && <span style={{ color: '#36f', fontSize: 12 }}>当前</span>}
                                    {!c.has_key && <span style={{ color: '#e24', fontSize: 11 }}>未填Key</span>}
                                  </div>
                                  <div style={{ fontSize: 11, color: '#777' }}>{c.provider} · {c.model}</div>
                                </div>
                              );
                            })}
                          </div>
                        )}

                        {/* ── 🧠 深度思考档次浮层：关/标准/深度 三档 ── */}
                        {deepThinkOpen && (
                          <div
                            data-gt-deeppicker-popover
                            onClick={e => e.stopPropagation()}
                            style={{
                              position: 'absolute', left: '36%', bottom: '100%', marginBottom: 4, zIndex: 100,
                              minWidth: 220, padding: 6,
                              background: '#fff', border: '1px solid #e0e0ea', borderRadius: 12,
                              boxShadow: '0 6px 20px rgba(0,0,0,0.08)',
                            }}
                          >
                            {([
                              { level: 0, label: '⚡ 关闭', desc: '默认快速回答' },
                              { level: 1, label: '🔄 标准思考', desc: '先理清思路，言简意赅' },
                              { level: 2, label: '🧠 深度思考', desc: '拆假设·列逻辑·权衡取舍再给结论' },
                            ]).map(opt => {
                              const active = generalDeepThink === opt.level;
                              return (
                                <div
                                  key={opt.level}
                                  onClick={() => { setGeneralDeepThink(opt.level); setDeepThinkOpen(false); }}
                                  style={{
                                    padding: '8px 10px', borderRadius: 8, cursor: 'pointer', marginBottom: 2,
                                    display: 'flex', flexDirection: 'column', gap: 2,
                                    background: active ? '#f1ecfa' : 'transparent',
                                    border: active ? '1px solid #9b7ee0' : '1px solid transparent',
                                  }}
                                >
                                  <span style={{ fontWeight: 600, fontSize: 13, color: active ? '#6236c9' : '#333' }}>{opt.label}</span>
                                  <span style={{ fontSize: 11, color: '#888' }}>{opt.desc}</span>
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    </>
                  );
                })()}
                <div className="chat-input-row">
                  <textarea
                    ref={inputRef}
                    className="chat-input"
                    value={input}
                    onChange={e => setInput(e.target.value)}
                    onKeyDown={onInputKeyDown}
                    placeholder={inputPlaceholder}
                    rows={1}
                    disabled={streaming || loadingSuggest || (suggestions.length > 0 && !selectedSuggestion) || !selectedDim}
                  />
                  {streaming ? (
                    <button className="chat-send stop" onClick={stopStream}>停止</button>
                  ) : (
                    <button
                      className="chat-send"
                      onClick={handleMainSend}
                      disabled={!canSend && !selectedSuggestion || loadingSuggest || suggestions.length > 0}
                    >{
                      loadingSuggest ? '…' :
                      selectedSuggestion ? '按方案生成' :
                      selectedDim === 'general' ? '发送' :
                      shouldShowSuggestions(selectedDim, bible) ? '生成方案' : '生成/修改'
                    }</button>
                  )}
                </div>
              </div>
            )}

            {activeTab !== 'setting' && (
              <div className="chat-input-area">
                <div className="chat-input-row">
                  <input
                    className="chat-input"
                    value={input}
                    onChange={e => setInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        if (activeTab === 'chapter') {
                          // 正文Tab：直接发送，智能判断修改/续写
                          handleChapterSend();
                        } else if (activeTab === 'deai' && input.trim()) {
                          handleDeai();
                        }
                      }
                    }}
                    placeholder={inputPlaceholder}
                    disabled={streaming}
                  />
                  {streaming ? (
                    <button className="chat-send stop" onClick={stopStream}>停止</button>
                  ) : activeTab === 'chapter' ? (
                    <button
                      className="chat-send"
                      onClick={handleChapterSend}
                      disabled={chapters.length === 0 && !input.trim()}
                    >{input.trim() ? '发送' : '▶️ 写作'}</button>
                  ) : activeTab === 'deai' ? (
                    <button
                      className="chat-send"
                      onClick={handleDeai}
                      disabled={!input.trim() && !deaiTargetId}
                    >🧹 去味</button>
                  ) : null}
                </div>
              </div>
            )}

            {/* 底部 Tab 栏（手机友好） */}
            <div className="smart-tab-bar">
              {TABS.map(t => (
                <button
                  key={t.key}
                  className={`smart-tab ${activeTab === t.key ? 'active' : ''}`}
                  onClick={() => switchTab(t.key)}
                >
                  <span className="smart-tab-icon">{t.icon}</span>
                  <span className="smart-tab-label">{t.label}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* 【需求1-2：双击预览】技能包预览 Modal */}
      {previewPack && (
        <div className="skill-editor-overlay" onClick={() => setPreviewPack(null)}>
          <div className="skill-editor-modal" onClick={e => e.stopPropagation()} style={{ maxWidth: 720 }}>
            <div className="skill-editor-header">
              <h3 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: 22 }}>{previewPack.icon}</span>
                <span>{previewPack.name}</span>
                {previewPack.is_builtin ? <span className="builtin-badge">系统</span> : <span className="custom-badge">自定义</span>}
                <span style={{
                  fontSize: 11, fontWeight: 500, padding: '2px 8px', borderRadius: 999,
                  background: (previewPack.category || 'master') === 'master' ? 'rgba(74,144,217,0.12)'
                    : previewPack.category === 'style' ? 'rgba(217,119,6,0.12)' : 'rgba(5,150,105,0.12)',
                  color: (previewPack.category || 'master') === 'master' ? '#4a90d9'
                    : previewPack.category === 'style' ? '#d97706' : '#059669'
                }}>
                  {(previewPack.category || 'master') === 'master' ? '构思类' : previewPack.category === 'style' ? '文风类' : '审查类'}
                </span>
              </h3>
              <button className="btn-icon" onClick={() => setPreviewPack(null)}>✕</button>
            </div>

            <div style={{ background: 'var(--bg-tertiary)', padding: 12, borderRadius: 8, marginBottom: 12 }}>
              <div style={{ fontSize: 13, lineHeight: 1.7, color: 'var(--text-primary)' }}>{previewPack.description || '暂无描述'}</div>
            </div>

            <div style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 6 }}>工作流（{previewPack.workflow?.length || 0}步）</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {!previewPack.workflow?.length && <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>暂无</div>}
                {previewPack.workflow?.map((step, i) => (
                  <div key={i} style={{
                    display: 'flex', alignItems: 'flex-start', gap: 8,
                    padding: '6px 10px', background: 'var(--bg-tertiary)', borderRadius: 6
                  }}>
                    <span style={{
                      flexShrink: 0, width: 20, height: 20, borderRadius: '50%',
                      background: 'var(--accent)', color: '#fff', fontSize: 10,
                      display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 600
                    }}>{step.step || i + 1}</span>
                    <div style={{ flex: 1, minWidth: 0, fontSize: 12 }}>
                      <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{step.name || '未命名'}</div>
                      {step.desc && <div style={{ color: 'var(--text-secondary)', fontSize: 11, marginTop: 1 }}>{step.desc}</div>}
                      {step.prompt_key && (
                        <div style={{
                          marginTop: 3, fontFamily: 'var(--mono)', fontSize: 10,
                          color: 'var(--accent)', background: 'var(--bg-secondary)',
                          padding: '1px 5px', borderRadius: 3, display: 'inline-block'
                        }}>🔑 {step.prompt_key}</div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div>
              <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 6 }}>提示词模板（{Object.keys(previewPack.prompts || {}).length}个）</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 220, overflow: 'auto' }}>
                {!Object.keys(previewPack.prompts || {}).length && <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>暂无</div>}
                {Object.entries(previewPack.prompts || {}).map(([key, val]) => (
                  <details key={key} style={{
                    background: 'var(--bg-tertiary)', borderRadius: 6,
                    border: '1px solid var(--border)', overflow: 'hidden'
                  }}>
                    <summary style={{
                      padding: '5px 10px', fontSize: 12, fontWeight: 600,
                      fontFamily: 'var(--mono)', cursor: 'pointer', color: 'var(--accent)',
                      listStyle: 'none'
                    }}>📝 {key} <span style={{ float: 'right', color: 'var(--text-muted)', fontWeight: 400 }}>点击展开</span></summary>
                    <pre style={{
                      margin: 0, padding: 10, fontSize: 11, lineHeight: 1.7,
                      color: 'var(--text-primary)', whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word', fontFamily: 'var(--mono)',
                      borderTop: '1px solid var(--border)', maxHeight: 140, overflow: 'auto'
                    }}>{val || '（空）'}</pre>
                  </details>
                ))}
              </div>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 14, gap: 8 }}>
              <button className="btn-primary" onClick={() => setPreviewPack(null)}>关闭</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
