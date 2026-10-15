/**
 * RelationGraph —— 轻量级纯 SVG 关系图谱（零第三方依赖）。
 *
 * 用于"人物关系图 / 地图关系图"的图形化展示：圆形布局、带箭头的有向边、
 * 边关系标签、节点悬停看详情、点击节点高亮其关系网（再次点击取消）。
 * 数据缺失时展示空态文案，绝不白屏。
 */
import { useMemo, useState } from 'react';

export interface GraphNode {
  id: string;
  label: string;
  /** 节点色调：主角/反派/地点/区域/默认 */
  tone?: 'protagonist' | 'antagonist' | 'location' | 'region' | 'default';
  /** 悬停提示里的补充说明 */
  meta?: string;
}

export interface GraphEdge {
  source: string;
  target: string;
  label?: string;
  /** 虚线边表示归属/包含等弱关系 */
  dashed?: boolean;
}

const TONE_FILL: Record<string, string> = {
  protagonist: '#5b8def',
  antagonist: '#e76f51',
  location: '#27ae60',
  region: '#9b59b6',
  default: '#8a94a6',
};

const W = 920;
const H = 560;
const CX = W / 2;
const CY = H / 2;

function truncate(s: string, n = 7): string {
  s = (s || '').trim();
  return s.length > n ? s.slice(0, n) + '…' : s;
}

export function RelationGraph(props: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  height?: number;
  emptyText?: string;
}) {
  const { edges, nodes } = props;
  const [selected, setSelected] = useState<string | null>(null);

  // 圆形布局（节点按 id 排序保证位置稳定，不随渲染跳动）
  const positioned = useMemo(() => {
    const sorted = [...nodes].sort((a, b) => a.id.localeCompare(b.id, 'zh'));
    const n = sorted.length;
    const radius = n <= 8 ? 170 : Math.min(245, 150 + n * 4);
    return sorted.map((node, i) => {
      // 从 -90° 开始顺时针
      const angle = (i / Math.max(1, n)) * Math.PI * 2 - Math.PI / 2;
      return {
        ...node,
        x: CX + radius * Math.cos(angle),
        y: CY + radius * Math.sin(angle),
      };
    });
  }, [nodes]);

  const posMap = useMemo(() => {
    const m: Record<string, { x: number; y: number }> = {};
    for (const p of positioned) m[p.id] = { x: p.x, y: p.y };
    return m;
  }, [positioned]);

  if (!nodes.length) {
    return (
      <div style={{ padding: '28px 12px', textAlign: 'center', color: 'var(--text-muted)', fontSize: 13, lineHeight: 1.8 }}>
        {props.emptyText || '暂无可展示的关系数据'}
      </div>
    );
  }

  const neighbor = new Set<string>();
  if (selected) {
    neighbor.add(selected);
    for (const e of edges) {
      if (e.source === selected) neighbor.add(e.target);
      if (e.target === selected) neighbor.add(e.source);
    }
  }

  const dim = (id: string) => (selected && !neighbor.has(id) ? 0.2 : 1);

  return (
    <div className="relation-graph-wrap" style={{ width: '100%', overflowX: 'auto' }}>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', minWidth: 560, height: props.height || 460, display: 'block' }} role="img" aria-label="关系图谱">
        <defs>
          <marker id="rg-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#a8b4cc" />
          </marker>
        </defs>

        {/* 边 */}
        {edges.map((e, i) => {
          const a = posMap[e.source];
          const b = posMap[e.target];
          if (!a || !b) return null;
          const isLinked = !selected || e.source === selected || e.target === selected;
          // 标签沿边按序号在 0.42/0.5/0.58 错位，减少多条边中点标签互相叠压
          const frac = [0.5, 0.42, 0.58][i % 3];
          const mx = a.x + (b.x - a.x) * frac;
          const my = a.y + (b.y - a.y) * frac;
          const label = e.label ? truncate(e.label, 8) : '';
          const labelW = label ? label.length * 12 + 10 : 0;
          return (
            <g key={`e${i}`} opacity={isLinked ? 1 : 0.15}>
              <line
                x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                stroke="#a8b4cc" strokeWidth={selected && isLinked ? 2 : 1.4}
                strokeDasharray={e.dashed ? '5 4' : undefined}
                markerEnd="url(#rg-arrow)"
              />
              {label && (
                <g>
                  <rect x={mx - labelW / 2} y={my - 10} width={labelW} height={18} rx={9}
                        fill="var(--bg-secondary, #f7f8fb)" stroke="#d5dceb" strokeWidth={0.8} />
                  <text x={mx} y={my + 3} textAnchor="middle" fontSize={11} fill="#6b7686">{label}</text>
                </g>
              )}
            </g>
          );
        })}

        {/* 节点 */}
        {positioned.map(p => {
          const fill = TONE_FILL[p.tone || 'default'] || TONE_FILL.default;
          return (
            <g key={p.id} transform={`translate(${p.x},${p.y})`} opacity={dim(p.id)}
               style={{ cursor: 'pointer' }}
               onClick={() => setSelected(cur => (cur === p.id ? null : p.id))}>
              {p.id === selected && <circle r={30} fill="none" stroke={fill} strokeWidth={1.5} strokeDasharray="3 3" />}
              <circle r={24} fill={fill} opacity={0.92} />
              <text y={4} textAnchor="middle" fontSize={11} fill="#fff" fontWeight={600}
                    style={{ pointerEvents: 'none' }}>
                {truncate(p.label, 4)}
              </text>
              <text y={42} textAnchor="middle" fontSize={12} fill="var(--text-primary,#333)"
                    style={{ pointerEvents: 'none' }}>
                {truncate(p.label, 7)}
              </text>
              <title>{`${p.label}${p.meta ? '\n' + p.meta : ''}`}</title>
            </g>
          );
        })}
      </svg>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: -4, display: 'flex', gap: 14, flexWrap: 'wrap' }}>
        <span><i style={{ display: 'inline-block', width: 9, height: 9, borderRadius: 9, background: TONE_FILL.protagonist, marginRight: 4 }} />主角</span>
        <span><i style={{ display: 'inline-block', width: 9, height: 9, borderRadius: 9, background: TONE_FILL.antagonist, marginRight: 4 }} />反派</span>
        <span><i style={{ display: 'inline-block', width: 9, height: 9, borderRadius: 9, background: TONE_FILL.region, marginRight: 4 }} />区域/势力</span>
        <span><i style={{ display: 'inline-block', width: 9, height: 9, borderRadius: 9, background: TONE_FILL.location, marginRight: 4 }} />地点</span>
        <span>点击节点高亮关系网，再次点击取消</span>
      </div>
    </div>
  );
}

/** 解析 "张三 → 李四：师徒" / "张三->李四 师徒" / "张三—李四：宿敌" 形式的关系文本行 */
export function parseRelationLine(line: string): { source: string; target: string; label: string } | null {
  const s = (line || '').replace(/^[\s\-•·\d.、]+/, '').trim();
  if (!s) return null;
  const m = s.match(/^(.{1,12}?)\s*(?:→|->|—{1,2}|–|➜)\s*(.{1,12}?)(?:\s*[:：]\s*(.+))?$/);
  if (!m) return null;
  const source = m[1].trim();
  const target = m[2].trim();
  const label = (m[3] || '').trim();
  if (!source || !target || source === target) return null;
  return { source, target, label: label.slice(0, 20) };
}
