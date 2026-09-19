// ChatPanelToolbars.tsx
// 从 ChatPanel.tsx 拆分的「工具栏」组件：技能包选择器 / 通用聊天助手选择器（含联网搜索 Key 配置）。
import { useState } from 'react';
import { createPortal } from 'react-dom';
import { api } from '../api';
import type { SkillPack } from '../types';

// ============================================================================
// 技能包选择器（精简版，按 category 分组）
// ============================================================================
export function SkillPackSelector({ packs, selected, onToggle, compact, onPreview }: {
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
export function GeneralAssistantSelector({ roles, currentId, onSelect }: {
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
      {/* 助手切换 + 联网搜索Key入口并排（Key 入口从列表底部移出：手机端 .smart-toolbar 有
          max-height:40vh 裁剪 + 三层嵌套滚动，列表底部的入口在小屏上根本滚不到/看不到） */}
      <div style={{ display: 'flex', gap: 6, alignItems: 'stretch' }}>
        <button
          className="smart-skill-toggle"
          data-gt-assistant-toggle
          onClick={(e) => { e.stopPropagation(); setOpen(o => !o); }}
          title="切换助手（10款内置角色）"
          style={{ flex: 1 }}
        >
          👤 <span style={{ fontWeight: 600 }}>{curRole.emoji}{curRole.name}</span>
          <span className="smart-skill-arrow">{open ? '▲' : '▼'}</span>
        </button>
        <button
          data-gt-searchcfg-toggle
          onClick={(e) => { e.stopPropagation(); setCfgOpen(true); loadCfg(); }}
          title="配置联网搜索 Key（Tavily / Exa / Brave）"
          style={{ border: '1px solid #bfe3d2', background: '#eafaf3', color: '#0a7d4f',
                   borderRadius: 6, padding: '0 10px', fontSize: 15, cursor: 'pointer', flex: '0 0 auto' }}
        >🌐</button>
      </div>
      {open && (
        <div className="smart-skill-list smart-assistant-list" data-gt-assistant-popover
          onClick={(e) => { e.stopPropagation(); }}
          style={{ zIndex: 102, position: 'relative' }}
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
        </div>
      )}

      {/* 🌐 联网搜索 Key 配置浮层：Portal 到 body，彻底摆脱手机端
          .smart-toolbar(max-height:40vh)/.smart-dim-collapsible(overflow:hidden) 的裁剪与层级竞争 */}
      {cfgOpen && createPortal(
        <div
          data-gt-searchcfg-popover
          onClick={(e) => { e.stopPropagation(); }}
          style={{
            position: 'fixed', left: '50%', top: '50%', transform: 'translate(-50%, -50%)', zIndex: 1300,
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
        </div>,
        document.body
      )}
    </div>
  );
}
