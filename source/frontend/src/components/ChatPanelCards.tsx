// ChatPanelCards.tsx
// 从 ChatPanel.tsx 拆分的「卡片 + 消息气泡」渲染组件：
//   CardApplyMode / CardViewProps / TimelineCardBody / AdoptedCardCollapsed /
//   ActionCardView / ProgressMapView / RankScanCard / MessageBubble
// ChatPanel 主组件仅 import 使用 MessageBubble / RankScanCard / ProgressMapView / CardApplyMode。
import { useState, useEffect, useRef, memo, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
// P1-2 Markdown 增强：highlight.js CSS 从 node_modules 静态导入（Vite 打包到本地 assets），
// 彻底避免跨站 CDN 被 Edge/Safari Tracking Prevention 阻止或内网不可达导致样式白屏。
import 'highlight.js/styles/github.min.css';
import CarLogo from './CarLogo';
import { api } from '../api';
import type { ActionCard, ProgressMap, AIMessage } from '../types';

// highlight.js 按需语言注册：默认 rehype-highlight 会注册全部 ~190 种语言的语法高亮器，
// 主 chunk 体积显著膨胀。此处只注册 AI 创作/技能配置实际会出现的语言，其余代码块按
// 纯文本渲染（ignoreMissing），既保住可读性又砍掉大部分高亮器体积。
const CODE_LANGS: string[] = [
  'json', 'yaml', 'python', 'javascript', 'typescript', 'bash',
  'markdown', 'xml', 'html', 'css', 'sql', 'java', 'go', 'rust', 'cpp',
  'diff', 'ini', 'toml',
];
const REHYPE_HIGHLIGHT_OPTS = { detect: true, ignoreMissing: true, subset: CODE_LANGS };

// ============================================================================
// Action Card 单卡渲染（采纳(覆盖) / 追加 / 编辑 / 忽略 四按钮）
// ============================================================================
// 落地模式：overwrite=采纳/编辑后覆盖原内容；append=追加到原内容后不覆盖
export type CardApplyMode = 'overwrite' | 'append';

interface CardViewProps {
  card: ActionCard;
  onAdopt: (card: ActionCard, mode: CardApplyMode) => void;
  onEdit: (card: ActionCard, newContent: string, mode: CardApplyMode) => void;
  onIgnore: (card: ActionCard) => void;
  applying: boolean;
  onReplaceChapter?: (card: ActionCard, meta: any) => void;
  // 剧情维度专属：节点设计需要 bookId、写回 bible
  bookId?: string;
  bible?: any;
  onBibleUpdate?: (nextBible: any) => void;
  selectedSkillPackIds?: string[];
  chaptersPerVolume?: number;
  // 节点设计师中途半截卡片：一键发送『继续』
  onQuickContinue?: () => void;
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

// 字段可能是字符串或数组（后端把 characters 等规范成了数组），统一渲染为顿号连接文本，
// 避免数组直接渲染导致的人名粘连 + React key 告警。
function _fmt(v: any): string {
  if (v == null) return '';
  if (Array.isArray(v)) return v.map(x => (x && typeof x === 'object' ? `${x.name || ''}${x.relation ? `(${x.relation})` : ''}` : String(x ?? ''))).filter(Boolean).join('、');
  if (typeof v === 'object') {
    const name = (v as any).name;
    const rel = (v as any).relation;
    return name ? `${name}${rel ? `(${rel})` : ''}` : '';
  }
  return String(v);
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
  // 节点级折叠：默认折叠，只渲染标题行 + 关键短字段，降低首屏 DOM（50+ 节点场景）
  const [collapsedNodes, setCollapsedNodes] = useState<Record<string, boolean>>({});
  const toggleNode = (k: string) => setCollapsedNodes(prev => ({ ...prev, [k]: !prev[k] }));
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
                      {v.characters && <div><b>人物：</b>{_fmt(v.characters)}</div>}
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
                              {ev.characters && <div><b>人物：</b>{_fmt(ev.characters)}</div>}
                              {ev.events && <div><b>事件：</b>{ev.events}</div>}
                              {ev.time && <div><b>时间：</b>{ev.time}</div>}
                              {ev.location && <div><b>地点：</b>{ev.location}</div>}
                              {ev.realm_change && <div><b>境界：</b>{ev.realm_change}</div>}
                              {ev.age_change && <div><b>年龄/时程：</b>{ev.age_change}</div>}
                            </div>
                          )}
                          <div style={{ marginTop: 6, fontSize: 12, color: '#6b7280', display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                            {ev.bury && <span title="伏笔埋设（当前层按事件描述，精确章号在节点里）" style={{ color: '#c2410c' }}>🔸 埋：{ev.bury}</span>}
                            {ev.payoff && <span title="伏笔回收（当前层按事件描述，精确章号在节点里）" style={{ color: 'var(--text-primary)' }}>🔹 收：{ev.payoff}</span>}
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
                            background: mei ? 'var(--accent-light)' : 'var(--bg-tertiary)',
                            border: '1px solid var(--border-color)',
                            borderRadius: 6,
                          }}>
                            {mei ? <div style={{ fontSize: 12, color: 'var(--accent)', fontWeight: 600, marginBottom: 4 }}>归属：主要剧情事件 E{mei}</div> : null}
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                              {list.map((n: any, ni: number) => {
                                const nodeKey = `${mei}-${n.chapters ?? n.index ?? ni}`;
                                const nodeOpen = !!collapsedNodes[nodeKey];
                                return (
                                  <div key={`${mei}-${ni}`} style={{
                                    padding: 8,
                                    background: '#ffffff',
                                    border: '1px solid #e5e7eb',
                                    borderRadius: 5,
                                    fontSize: 12,
                                  }}>
                                    {/* 标题行常显：标题 + 章号 + 类型 + 爽点 + 钩子（截断），详情默认折叠 */}
                                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', cursor: 'pointer' }} onClick={() => toggleNode(nodeKey)}>
                                      <span style={{ fontWeight: 600, color: '#111827' }}>
                                        N{n.index || (ni + 1)} {n.title}
                                      </span>
                                      {n.chapters && <span style={{ color: 'var(--text-primary)', fontWeight: 500 }}>📖 {n.chapters}</span>}
                                      {n.type && <span style={{ color: '#059669' }}>类型 {n.type}</span>}
                                      {n.cool_type && <span style={{ color: '#c2410c' }}>爽点 {n.cool_type}</span>}
                                      {n.cool_level && <span style={{ color: '#7c3aed' }}>{n.cool_level}</span>}
                                      {!nodeOpen && n.hook && <span style={{ color: '#6b7280', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 200 }}>🪝 {n.hook}</span>}
                                      <span style={{ marginLeft: 'auto', color: '#999', fontSize: 11, minWidth: 40, textAlign: 'right' }}>{nodeOpen ? '收起 ▲' : '展开 ▼'}</span>
                                    </div>
                                    {nodeOpen && (
                                      <>
                                        {/* 节点 6 要素 */}
                                        {(n.characters || n.events || n.time || n.location || n.realm_change || n.age_change) && (
                                          <div style={{
                                            marginTop: 4,
                                            padding: 6,
                                            background: '#ecfeff',
                                            border: '1px solid #a5f3fc',
                                            borderRadius: 4,
                                            fontSize: 12,
                                            color: 'var(--text-primary)',
                                            display: 'grid',
                                            gridTemplateColumns: 'repeat(2, minmax(0,1fr))',
                                            gap: '4px 10px',
                                          }}>
                                            {n.characters && <div><b>人物：</b>{_fmt(n.characters)}</div>}
                                            {n.events && <div><b>事件：</b>{n.events}</div>}
                                            {n.time && <div><b>时间：</b>{n.time}</div>}
                                            {n.location && <div><b>地点：</b>{n.location}</div>}
                                            {n.realm_change && <div><b>境界：</b>{n.realm_change}</div>}
                                            {n.age_change && <div><b>年龄/时程：</b>{n.age_change}</div>}
                                          </div>
                                        )}
                                        {/* 资源获得/消耗 */}
                                        {((n.resources_gained && n.resources_gained.length) || (n.resources_used && n.resources_used.length) || (n.total_resources_owned && Object.keys(n.total_resources_owned).some(k => (n.total_resources_owned[k] || []).length))) && (
                                          <div style={{
                                            marginTop: 4,
                                            padding: 6,
                                            background: '#f0fdf4',
                                            border: '1px solid #bbf7d0',
                                            borderRadius: 4,
                                            fontSize: 12,
                                            color: '#14532d',
                                          }}>
                                            {n.resources_gained && n.resources_gained.length > 0 && (
                                              <div><b>资源获得：</b>{Array.isArray(n.resources_gained) ? n.resources_gained.join('、') : n.resources_gained}</div>
                                            )}
                                            {n.resources_used && n.resources_used.length > 0 && (
                                              <div><b>资源消耗：</b>{Array.isArray(n.resources_used) ? n.resources_used.join('、') : n.resources_used}</div>
                                            )}
                                            {n.total_resources_owned && Object.keys(n.total_resources_owned).some(k => (n.total_resources_owned[k] || []).length) && (
                                              <div style={{ marginTop: 2 }}>
                                                <b>总资源：</b>
                                                {Object.entries(n.total_resources_owned).filter(([, v]: any) => v && v.length).map(([k, v]: any) => `${k}:${v.join('、')}`).join('；')}
                                              </div>
                                            )}
                                          </div>
                                        )}
                                        {/* 爽点结构/衬托 */}
                                        {(n.cool_structure || n.cool_contrast) && (
                                          <div style={{ marginTop: 4, fontSize: 12, color: '#6b21a8' }}>
                                            {n.cool_structure && <span><b>爽点结构：</b>{n.cool_structure}</span>}
                                            {n.cool_contrast && <span style={{ marginLeft: 8 }}><b>衬托：</b>{n.cool_contrast}</span>}
                                          </div>
                                        )}
                                        {n.summary && <div style={{ color: '#374151', marginTop: 4, whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>{n.summary}</div>}
                                        <div style={{ marginTop: 6, color: '#6b7280', display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                                          {n.bury && <span style={{ color: '#c2410c' }} title="伏笔埋设（精确到章）">🔸 埋：{n.bury}</span>}
                                          {n.payoff && <span style={{ color: 'var(--text-primary)' }} title="伏笔回收（精确到章）">🔹 收：{n.payoff}</span>}
                                          {n.hook && <span>🪝 钩子：{n.hook}</span>}
                                        </div>
                                      </>
                                    )}
                                  </div>
                                );
                              })}
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
        style={{ cursor: 'pointer', flexWrap: 'wrap' }}
      >
        <span className="chat-card-icon">{CARD_ICON[card.type] || '📌'}</span>
        <span className="chat-card-title">{card.title}</span>
        {card.rankSourceLabel && (
          <span className="chat-card-rank-label" title="智驾已结合榜单风向生成" style={{ fontSize: 11, padding: '1px 7px', borderRadius: 999, background: '#fef3c7', color: '#92400e', border: '1px solid #fcd34d', marginLeft: 2 }}>
            📈 {card.rankSourceLabel}
          </span>
        )}
        <span className="chat-card-status">✓ {card.status === 'appended' ? '已追加落地' : '已采纳落地'} · {card.target}{wc > 0 ? ` · ${wc}字` : ''}</span>
        <span className="chat-card-toggle" style={{ marginLeft: 'auto', fontSize: 12, color: '#999' }}>
          {expanded ? '收起 ▲' : '展开 ▼'}
        </span>
      </div>
      {card.subtitle && (
        <div style={{ padding: '2px 14px 4px', fontSize: 12, color: '#6b7280' }}>{card.subtitle}</div>
      )}
      {expanded && <div className="chat-card-body">{card.content}</div>}
    </div>
  );
});

const ActionCardView = memo(function ActionCardView(props: CardViewProps) {
  const { card, onAdopt, onEdit, onIgnore, applying, onReplaceChapter,
    bookId, bible, onBibleUpdate, selectedSkillPackIds, chaptersPerVolume,
    onQuickContinue } = props;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(card.content);
  // node 设计后卡片内容会被 TimelineCardBody 异步改写，draft / content 都要同步
  const handleContentMutated = (nextContent: string) => {
    setDraft(nextContent);
    // 注：不直接 onEdit，留给用户在 UI 上点击"采纳(覆盖)/追加"来决定是否正式落盘。
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

  // ========== 【方案A+B · A(主)隐藏半拉子卡片采纳按钮+进度+继续；B(次级兜底)分批临时保存】
  // SAVE_PLOT 中途半截卡片判定：读取 JSON 里 nodes.length vs 所在卷 chapter_count（或 chaptersPerVolume 或 50）
  const savePlotInfo = useMemo(() => {
    if (card.type !== 'SAVE_PLOT') return null;
    try {
      const content = (draft || card.content || '').trim();
      if (!content.startsWith('[')) return null;
      const arr = JSON.parse(content);
      if (!Array.isArray(arr) || !arr.length) return null;
      const v: any = arr[0] || {};
      const nodes = Array.isArray(v.nodes) ? v.nodes : [];
      const cc = (typeof v.chapter_count === 'number' && v.chapter_count > 0) ? v.chapter_count : (chaptersPerVolume || 50);
      const vi = typeof v.volume_index === 'number' ? v.volume_index : null;
      return { nodes, cc, vi };
    } catch {
      return null;
    }
  }, [card.type, card.content, draft, chaptersPerVolume]);
  const isHalfwaySavePlot = !!(savePlotInfo && savePlotInfo.nodes.length > 0 && savePlotInfo.nodes.length < savePlotInfo.cc);

  // 编辑后二次选择：mode='overwrite' 采纳落地（覆盖）| mode='append' 追加落地
  const handleSaveEdit = (mode: CardApplyMode) => {
    if (!draft.trim()) return;
    onEdit({ ...card, content: draft.trim() }, draft.trim(), mode);
    setEditing(false);
  };

  if (status === 'ignored') {
    return (
      <div className="chat-card chat-card-ignored">
        <div className="chat-card-head" style={{ flexWrap: 'wrap' }}>
          <span className="chat-card-icon">{CARD_ICON[card.type] || '📌'}</span>
          <span className="chat-card-title">{card.title}</span>
          {card.rankSourceLabel && (
            <span style={{ fontSize: 11, padding: '1px 7px', borderRadius: 999, background: '#fef3c7', color: '#92400e', border: '1px solid #fcd34d', marginLeft: 2 }}>
              📈 {card.rankSourceLabel}
            </span>
          )}
          <span className="chat-card-status">已忽略</span>
        </div>
        {card.subtitle && <div style={{ padding: '2px 14px 6px', fontSize: 12, color: '#6b7280' }}>{card.subtitle}</div>}
      </div>
    );
  }

  if (status === 'adopted' || status === 'appended' || status === 'edited') {
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

  const halfwayBar = isHalfwaySavePlot && savePlotInfo ? (() => {
    const { nodes, cc, vi } = savePlotInfo;
    const done = Math.min(cc, nodes.length);
    return (
      <div style={{
        margin: '10px 2px 2px',
        padding: '10px 12px',
        borderRadius: 8,
        background: 'linear-gradient(180deg,#eff6ff 0%,#f5f3ff 100%)',
        border: '1px solid #bfdbfe',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'nowrap', overflow: 'hidden' }}>
          <strong style={{ color: 'var(--text-primary)', fontSize: 13, whiteSpace: 'nowrap' }}>
            🎯 中途进度（{vi ? `第${vi}卷` : '本卷'}）
          </strong>
          <span style={{ fontSize: 12, color: '#374151', whiteSpace: 'nowrap' }}>
            {done}/{cc}章
          </span>
          <span style={{ flex: 1, minWidth: 6 }} />
          <button
            className="chat-card-btn primary"
            style={{ padding: '4px 12px', minHeight: 26, fontSize: 13, whiteSpace: 'nowrap' }}
            onClick={() => onQuickContinue?.()}
            disabled={!onQuickContinue}
            title="从写到的最后一章继续生成（等同于发送「继续」）"
          >
            继续
          </button>
        </div>
        <div style={{ fontSize: 12, color: '#4b5563', lineHeight: 1.7, marginTop: 8 }}>
          💡 整卷 <strong>{cc} 章</strong> 全部设计完成后，会自动给出一张<strong style={{ color: '#16a34a' }}>全卷合并版统一采纳卡片</strong>，
          点一次即可完整落库。随时发送<strong>「继续」</strong>或点上面的按钮接着写。
          <br/>
          <span style={{ color: '#9ca3af' }}>如果想先把这 {done} 个节点临时存库（不推荐，后续还需再合并），可点下方「💾 分批临时保存」——后端会自动按章节号增量合并到已有节点，不会覆盖已存在章节。</span>
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
          {isReplaceMode ? null : (
            <button
              className="chat-card-btn"
              style={{ fontSize: 12, background: '#fef3c7', borderColor: '#fcd34d', color: '#92400e' }}
              onClick={() => onAdopt({ ...card, content: draft || card.content }, 'append')}
              disabled={applying}
              title="把当前半截卡片的节点按章节号增量合并到卷里（不会覆盖已存在章节的节点）。推荐整卷写完后再统一采纳。"
            >
              💾 分批临时保存（不推荐）
            </button>
          )}
          <button className="chat-card-btn ghost" onClick={() => setEditing(true)} disabled={applying}>
            编辑
          </button>
          <button className="chat-card-btn ghost" onClick={() => onIgnore(card)} disabled={applying}>
            忽略此快照
          </button>
        </div>
      </div>
    );
  })() : null;

  return (
    <div className="chat-card">
      <div className="chat-card-head" style={{ flexWrap: 'wrap' }}>
        <span className="chat-card-icon">{CARD_ICON[card.type] || '📌'}</span>
        <span className="chat-card-title">{card.title}</span>
        {card.rankSourceLabel && (
          <span title="智驾已结合榜单风向生成" style={{ fontSize: 11, padding: '1px 7px', borderRadius: 999, background: '#fef3c7', color: '#92400e', border: '1px solid #fcd34d' }}>
            📈 {card.rankSourceLabel}
          </span>
        )}
        <span className="chat-card-target">→ {card.target}</span>
      </div>
      {card.subtitle && (
        <div style={{ padding: '2px 14px 4px', fontSize: 12, color: '#6b7280', marginTop: -2 }}>{card.subtitle}</div>
      )}
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
            {card.type === 'SAVE_CHAPTER' ? (
              <button className="chat-card-btn primary" onClick={() => handleSaveEdit('overwrite')} disabled={!draft.trim()}>
                保存并落地
              </button>
            ) : (
              <>
                <button className="chat-card-btn primary" onClick={() => handleSaveEdit('overwrite')} disabled={!draft.trim()}>
                  采纳落地（覆盖原内容）
                </button>
                <button className="chat-card-btn" onClick={() => handleSaveEdit('append')} disabled={!draft.trim()}>
                  追加落地
                </button>
              </>
            )}
            <button className="chat-card-btn ghost" onClick={() => { setEditing(false); setDraft(card.content); }}>
              取消
            </button>
          </div>
        </>
      ) : (
        <>
          {renderBody(draft || card.content)}
          {halfwayBar ? halfwayBar : (
            <div className="chat-card-actions">
              {isReplaceMode ? (
                <button className="chat-card-btn primary" onClick={() => onReplaceChapter!(card, cardMeta)} disabled={applying}>
                  {applying ? '替换中…' : '替换本章正文'}
                </button>
              ) : (
                <button className="chat-card-btn primary" onClick={() => onAdopt({ ...card, content: draft || card.content }, 'overwrite')} disabled={applying}
                  title="直接落地并全覆盖该维度现有内容">
                  {applying ? '落地中…' : (card.type === 'SAVE_CHAPTER' ? '采纳(覆盖同章)' : '采纳')}
                </button>
              )}
              {!isReplaceMode && card.type !== 'SAVE_CHAPTER' && (
                <button className="chat-card-btn" onClick={() => onAdopt({ ...card, content: draft || card.content }, 'append')} disabled={applying}
                  title="追加到该维度现有内容之后，不覆盖">
                  追加
                </button>
              )}
              <button className="chat-card-btn" onClick={() => setEditing(true)} disabled={applying}>
                编辑
              </button>
              <button className="chat-card-btn ghost" onClick={() => onIgnore(card)} disabled={applying}>
                忽略
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
});

// ============================================================================
// 进度地图（设定Tab可展开）
// ============================================================================
export const ProgressMapView = memo(function ProgressMapView({ progress, onClose }: { progress: ProgressMap | null; onClose?: () => void }) {
  if (!progress) return null;
  const statusLabel: Record<string, string> = { empty: '未开始', sketch: '草稿', partial: '进行中', solid: '已完善' };
  const statusColor: Record<string, string> = { empty: '#999', sketch: '#d97706', partial: 'var(--text-primary)', solid: '#16a34a' };
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
// 榜单风向·市场情报卡片（RankScanCard）：智驾首条消息展示，含📈重扫+切换平台
// ============================================================================
interface RankScanCardProps {
  rankScan: any;
  platform: 'fanqie' | 'qidian' | null;
  concept?: string;
  onRescan: (platform: 'fanqie' | 'qidian') => void;
  rescanning?: boolean;
}
export const RankScanCard = memo(function RankScanCard({ rankScan, platform, concept, onRescan, rescanning }: RankScanCardProps) {
  const [expanded, setExpanded] = useState(true);
  const curPlatform: 'fanqie' | 'qidian' = (rankScan?.platform || platform || 'fanqie') as any;
  const platLabel = curPlatform === 'qidian' ? '📚 起点新书榜' : '🍅 番茄新书榜';
  const platColor = curPlatform === 'qidian'
    ? { from: '#1e40af', to: '#0369a1', soft: '#eff6ff', border: '#93c5fd' }
    : { from: '#dc2626', to: '#ea580c', soft: '#fef2f2', border: '#fca5a5' };

  if (!rankScan) return null;

  const ok = !!rankScan.ok && !rankScan.error;
  const intel = rankScan.market_intel || {};
  const cats = rankScan.matched_categories || [];
  const topCat = cats.length > 0 ? cats[0] : null;
  const sourcesLabel = rankScan.sources_label || `${platLabel} · 匹配 ${rankScan.matched_books_count || 0} 本新书`;

  const ItemList = ({ title, icon, items, color }: { title: string; icon: string; items: string[]; color: string }) => {
    if (!items || items.length === 0) return null;
    return (
      <div style={{ marginTop: 10 }}>
        <div style={{ fontSize: 12.5, fontWeight: 600, color, marginBottom: 4, display: 'flex', alignItems: 'center', gap: 4 }}>
          <span>{icon}</span><span>{title}</span>
          <span style={{ flex: 1 }} />
          <span style={{ fontSize: 11, fontWeight: 400, color: '#9ca3af' }}>共{items.length}项</span>
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
          {items.slice(0, expanded ? items.length : 5).map((it, i) => (
            <span key={i} style={{
              display: 'inline-flex', alignItems: 'center',
              padding: '3px 9px', borderRadius: 999,
              background: platColor.soft, border: `1px solid ${platColor.border}`,
              fontSize: 12.5, lineHeight: '18px',
              color: '#374151',
              maxWidth: '100%',
              wordBreak: 'break-all',
            }}>
              <span style={{ opacity: 0.55, marginRight: 4, fontSize: 10.5 }}>{i + 1}</span>
              {String(it).length > 32 ? String(it).slice(0, 32) + '…' : it}
            </span>
          ))}
          {!expanded && items.length > 5 && (
            <span style={{ fontSize: 11, color: '#9ca3af', alignSelf: 'center', marginLeft: 2 }}>+{items.length - 5}更多</span>
          )}
        </div>
      </div>
    );
  };

  return (
    <div style={{
      margin: '6px 2px 12px',
      borderRadius: 14,
      padding: '12px 12px 10px',
      background: `linear-gradient(135deg, ${platColor.from}14 0%, ${platColor.to}12 100%)`,
      border: `1px solid ${platColor.border}`,
      boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
    }}>
      {/* 头栏 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{
          padding: '3px 10px', borderRadius: 999,
          background: `linear-gradient(90deg, ${platColor.from}, ${platColor.to})`,
          color: '#fff', fontSize: 12.5, fontWeight: 600, letterSpacing: 0.2,
        }}>📈 榜单风向</span>
        <span style={{ fontSize: 13, fontWeight: 600, color: '#111827' }}>{platLabel}</span>
        {topCat && (
          <span style={{ fontSize: 12, color: '#4b5563', background: '#fff', padding: '2px 8px', borderRadius: 999, border: '1px solid #e5e7eb' }}>
            🎯 匹配分类：{topCat.name}{topCat.score ? `（置信 ${Math.round(topCat.score * 100)}%）` : ''}
          </span>
        )}
        <span style={{ flex: 1 }} />
        {/* 平台切换重扫 */}
        <div style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
          {(['fanqie', 'qidian'] as const).map(p => {
            const active = curPlatform === p;
            return (
              <button
                key={p}
                onClick={() => onRescan(p)}
                disabled={rescanning}
                title={`扫${p === 'fanqie' ? '番茄' : '起点'}新书榜`}
                style={{
                  padding: '3px 8px', borderRadius: 999, fontSize: 11.5,
                  border: active ? `1px solid ${platColor.border}` : '1px solid #e5e7eb',
                  background: active ? platColor.soft : '#fff',
                  color: active ? '#111827' : '#6b7280',
                  cursor: rescanning ? 'progress' : 'pointer',
                  fontWeight: active ? 600 : 400,
                }}
              >
                {p === 'fanqie' ? '🍅番茄' : '📚起点'}
              </button>
            );
          })}
          <button
            onClick={() => onRescan(curPlatform)}
            disabled={rescanning}
            style={{
              padding: '3px 10px', borderRadius: 999, fontSize: 11.5,
              border: `1px solid ${platColor.border}`,
              background: platColor.soft, color: '#111827',
              cursor: rescanning ? 'progress' : 'pointer',
              fontWeight: 600,
              display: 'inline-flex', alignItems: 'center', gap: 3,
            }}
          >
            {rescanning ? <><span className="loading-spinner-sm" />扫描中…</> : <>🔄 重扫</>}
          </button>
          <button
            onClick={() => setExpanded(e => !e)}
            title={expanded ? '收起详情' : '展开详情'}
            style={{
              padding: '3px 8px', borderRadius: 999, fontSize: 11.5,
              border: '1px solid #e5e7eb', background: '#fff', color: '#6b7280',
              cursor: 'pointer',
            }}
          >{expanded ? '▲收起' : '▼展开'}</button>
        </div>
      </div>

      {/* 数据来源 & 错误条 */}
      {!ok && rankScan.error && (
        <div style={{ marginTop: 8, fontSize: 12, color: '#b91c1c', padding: '6px 10px', borderRadius: 8, background: '#fef2f2', border: '1px solid #fecaca' }}>
          ⚠️ {rankScan.error}
        </div>
      )}
      <div style={{ marginTop: 6, fontSize: 11.5, color: '#6b7280' }}>
        {sourcesLabel}{concept ? ` · 基于构思「${concept.length > 28 ? concept.slice(0, 28) + '…' : concept}」匹配` : ''}
        {rankScan.cached && <span style={{ color: '#059669', marginLeft: 4 }}>🗄️ 命中缓存</span>}
      </div>

      {/* 展开内容：四项市场情报 */}
      {expanded && (
        <div style={{ marginTop: 2 }}>
          {rankScan.report && (
            <div style={{
              marginTop: 10, padding: '8px 11px', borderRadius: 10,
              fontSize: 13, lineHeight: 1.7, color: '#1f2937',
              background: '#fff', border: '1px solid #f3f4f6',
            }}>
              <strong style={{ color: platColor.from }}>📊 风向速览：</strong>
              <div style={{ whiteSpace: 'pre-wrap', marginTop: 4 }}>{rankScan.report}</div>
            </div>
          )}
          <ItemList title="开篇钩子套路" icon="🎣" items={intel.opening_patterns || []} color={platColor.from} />
          <ItemList title="热门元素·爽点" icon="🔥" items={intel.popular_elements || []} color="#b45309" />
          <ItemList title="雷区·毒点要素" icon="💣" items={intel.landmine_elements || []} color="#b91c1c" />
          <ItemList title="书名公式参考" icon="📘" items={intel.title_formulas || []} color="#0369a1" />
          <ItemList title="金手指类型拆解" icon="⚡" items={intel.golden_finger_types || []} color="#7c3aed" />
          <ItemList title="简介写法套路" icon="✍️" items={intel.intro_formulas || []} color="#0d9488" />
          <ItemList title="核心设定卖点" icon="🏗️" items={intel.setting_selling_points || []} color="#c2410c" />
          <ItemList title="黄金三章套路" icon="📖" items={intel.golden_three_patterns || []} color="#4338ca" />
        </div>
      )}
    </div>
  );
});

// ============================================================================
// 消息气泡（长按菜单 + 超两行折叠）
// ============================================================================
interface MessageBubbleProps {
  message: AIMessage;
  index: number;
  onAdopt: (c: ActionCard, mode: CardApplyMode) => void;
  onEdit: (c: ActionCard, content: string, mode: CardApplyMode) => void;
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
  // 节点设计师中途半截卡片：一键发送『继续』
  onQuickContinue?: () => void;
  // 圆桌会议专用：rt-header右上角『继续』按钮（对齐节点设计师"卡片右侧继续"）
  // 主 ChatPanel 组件收到回调后直接 POST /ai/chat/roundtable，不新增"继续"用户气泡
  onRoundtableResume?: (sessionId: string, messageId: string | number) => void;
}

// 长按计时器
const LONG_PRESS_MS = 500;

export const MessageBubble = memo(function MessageBubble({ message, index, onAdopt, onEdit, onIgnore, applyingCardId, streaming, onReplaceChapter, onEditMessage, onDeleteMessage, onRegenerate, bookId, bible, onBibleUpdate, selectedSkillPackIds, chaptersPerVolume, onQuickContinue, onRoundtableResume }: MessageBubbleProps) {
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
            <div className="rt-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span className="rt-title">🪑 圆桌会议</span>
                {message.roundtable?.status === 'open' && <span className="rt-live">● 进行中</span>}
                {message.roundtable?.status === 'done' && <span className="rt-done">✓ 已结束</span>}
              </div>
              {/* 断点续会"继续"按钮：对齐节点设计师卡片右侧的继续按钮，点击后不新增用户气泡 */}
              {onRoundtableResume && message.roundtable?.status !== 'done' && !streaming && (
                <button
                  className="chat-card-btn primary"
                  style={{
                    padding: '3px 14px',
                    minHeight: 28,
                    fontSize: 13,
                    fontWeight: 600,
                    borderRadius: 6,
                    background: 'linear-gradient(135deg, #52c41a 0%, #389e0d 100%)',
                    boxShadow: '0 2px 6px rgba(82,196,26,.3)',
                    border: 'none',
                  }}
                  onClick={(e) => {
                    e.stopPropagation();
                    // sessionId 主组件优先读 chatGeneralSessionId state；index 是 messages 数组下标 = 可靠定位器
                    onRoundtableResume('', index);
                  }}
                >继续</button>
              )}
            </div>
            {message.roundtable?.speech && message.roundtable.speech.length > 0 ? (
              <div className="rt-discussion">
                {message.roundtable.speech.map((seg, si) => (
                  <div key={si} className={(message.roundtable as any).currentSpeaker === seg.name && message.roundtable?.status === 'open' ? 'rt-seg speaking' : 'rt-seg'}>
                    <div className="rt-name">{seg.name || seg.speaker}</div>
                    <div className="rt-bubble">
                      {seg.content ? (
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          rehypePlugins={[[rehypeHighlight, REHYPE_HIGHLIGHT_OPTS]]}
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
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[[rehypeHighlight, REHYPE_HIGHLIGHT_OPTS]]}
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
                  remarkPlugins={[remarkGfm]}
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
                onQuickContinue={onQuickContinue}
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
