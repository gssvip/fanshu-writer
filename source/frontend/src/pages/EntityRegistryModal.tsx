import { useState, useEffect, useCallback } from 'react';
import { api } from '../api';

interface EntityRegistryModalProps {
  bookId: string;
  onClose: () => void;
  onRenamed?: () => void; // 重命名完成后刷新外部数据
}

type EntityType = 'characters' | 'factions' | 'locations' | 'items' | 'skills';
const ENTITY_LABELS: Record<EntityType, { label: string; icon: string; type: string }> = {
  characters: { label: '角色', icon: '👤', type: 'character' },
  factions: { label: '势力', icon: '⚔️', type: 'faction' },
  locations: { label: '地点', icon: '🗺️', type: 'location' },
  items: { label: '物品', icon: '🎒', type: 'item' },
  skills: { label: '技能', icon: '✨', type: 'skill' },
};

export default function EntityRegistryModal({ bookId, onClose, onRenamed }: EntityRegistryModalProps) {
  const [activeTab, setActiveTab] = useState<EntityType>('characters');
  const [entities, setEntities] = useState<{ characters: any[]; factions: any[]; locations: any[]; items: any[]; skills: any[] }>({
    characters: [], factions: [], locations: [], items: [], skills: [],
  });
  const [loading, setLoading] = useState(true);
  const [renaming, setRenaming] = useState(false);
  const [renameInput, setRenameInput] = useState<{ oldName: string; newName: string } | null>(null);
  const [mergeMode, setMergeMode] = useState<Set<string>>(new Set());
  const [msg, setMsg] = useState('');
  const [error, setError] = useState('');

  const loadEntities = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const res = await api.listEntities(bookId);
      setEntities(res);
    } catch (e: any) {
      setError(e.message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, [bookId]);

  useEffect(() => { loadEntities(); }, [loadEntities]);

  const currentList = entities[activeTab] || [];

  const startRename = (name: string) => {
    setRenameInput({ oldName: name, newName: name });
    setMergeMode(new Set());
  };

  const confirmRename = async () => {
    if (!renameInput || !renameInput.newName.trim()) return;
    if (renameInput.oldName === renameInput.newName) {
      setRenameInput(null);
      return;
    }
    setRenaming(true);
    setError('');
    setMsg('');
    try {
      const res = await api.renameEntity(bookId, renameInput.oldName, renameInput.newName, ENTITY_LABELS[activeTab].type);
      setMsg(`✅ 已替换 ${res.total_replacements} 处，涉及 ${res.fields_updated.length} 个字段、${res.chapters_affected} 章正文`);
      setRenameInput(null);
      await loadEntities();
      onRenamed?.();
    } catch (e: any) {
      setError(e.message || '重命名失败');
    } finally {
      setRenaming(false);
    }
  };

  const toggleMerge = (name: string) => {
    setMergeMode(prev => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const confirmMerge = async (mainName: string) => {
    const aliases = Array.from(mergeMode).filter(n => n !== mainName);
    if (!mainName || aliases.length === 0) {
      setError('请勾选要合并的别名（至少1个），并指定主名');
      return;
    }
    setRenaming(true);
    setError('');
    setMsg('');
    try {
      const res = await api.mergeEntities(bookId, mainName, aliases, ENTITY_LABELS[activeTab].type);
      setMsg(`✅ 合并完成：${res.merged.length} 个别名归并到「${mainName}」，共替换 ${res.total_replacements} 处`);
      setMergeMode(new Set());
      await loadEntities();
      onRenamed?.();
    } catch (e: any) {
      setError(e.message || '合并失败');
    } finally {
      setRenaming(false);
    }
  };

  return (
    <div className="modal-overlay" style={{ alignItems: 'center', justifyContent: 'center' }}>
      <div className="modal" style={{ width: '92vw', maxWidth: 720, maxHeight: '88vh', display: 'flex', flexDirection: 'column', padding: 0 }}>
        {/* 头部 */}
        <div style={{ padding: '14px 18px', borderBottom: '1px solid var(--border-color)', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0 }}>
          <div>
            <div style={{ fontSize: 16, fontWeight: 700 }}>🏗️ 实体注册表</div>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
              跨维度统一管理角色/势力/地点/物品/技能，重命名/合并会同步到全部设定与正文
            </div>
          </div>
          <button className="btn-ghost-sm" onClick={onClose}>✕</button>
        </div>

        {/* Tab 切换 */}
        <div style={{ display: 'flex', gap: 6, padding: '10px 18px 0', borderBottom: '1px solid var(--border-color)', flexShrink: 0 }}>
          {(Object.keys(ENTITY_LABELS) as EntityType[]).map(k => (
            <button
              key={k}
              onClick={() => { setActiveTab(k); setRenameInput(null); setMergeMode(new Set()); setMsg(''); setError(''); }}
              style={{
                padding: '6px 14px', fontSize: 13, fontWeight: 600,
                borderRadius: '6px 6px 0 0',
                background: activeTab === k ? 'var(--accent)' : 'var(--bg-secondary)',
                color: activeTab === k ? '#fff' : 'var(--text-secondary)',
                border: '1px solid var(--border-color)',
                borderBottom: 'none',
              }}
            >
              {ENTITY_LABELS[k].icon} {ENTITY_LABELS[k].label} ({entities[k]?.length || 0})
            </button>
          ))}
        </div>

        {/* 内容区 */}
        <div style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
          {loading && <div style={{ textAlign: 'center', padding: 24, color: 'var(--text-muted)' }}>加载中…</div>}

          {!loading && currentList.length === 0 && (
            <div style={{ textAlign: 'center', padding: 24, color: 'var(--text-muted)', fontSize: 13 }}>
              暂未从设定中识别到{ENTITY_LABELS[activeTab].label}。先在对应维度填写内容后再来管理。
            </div>
          )}

          {!loading && currentList.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {/* 合并模式提示条 */}
              {mergeMode.size > 0 && (
                <div style={{ padding: 10, background: 'var(--bg-secondary)', borderRadius: 6, fontSize: 12, border: '1px solid var(--accent)' }}>
                  <div style={{ fontWeight: 600, marginBottom: 6 }}>🔄 合并模式：已勾选 {mergeMode.size} 个</div>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                    <span>选主名：</span>
                    {Array.from(mergeMode).map(n => (
                      <button
                        key={n}
                        className="btn-primary-sm"
                        onClick={() => confirmMerge(n)}
                        disabled={renaming}
                        style={{ fontSize: 11 }}
                      >
                        以「{n}」为主
                      </button>
                    ))}
                    <button className="btn-ghost-sm" onClick={() => setMergeMode(new Set())} disabled={renaming}>取消</button>
                  </div>
                </div>
              )}

              {currentList.map(ent => (
                <div key={ent.name} style={{
                  padding: 10, borderRadius: 6, border: '1px solid var(--border-color)',
                  background: renameInput?.oldName === ent.name ? 'var(--bg-tertiary)' : 'var(--bg-secondary)',
                  display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap',
                }}>
                  {/* 合并勾选 */}
                  <input
                    type="checkbox"
                    checked={mergeMode.has(ent.name)}
                    onChange={() => toggleMerge(ent.name)}
                    disabled={!!renameInput || renaming}
                  />

                  <div style={{ flex: 1, minWidth: 120 }}>
                    <div style={{ fontWeight: 600, fontSize: 13 }}>{ent.name}</div>
                    <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>
                      出现于：{(ent.dim_refs || []).join('、')}
                    </div>
                  </div>

                  {/* 重命名输入 */}
                  {renameInput?.oldName === ent.name ? (() => {
                    const ri = renameInput!;
                    return (
                    <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      <input
                        className="input"
                        value={ri.newName}
                        onChange={e => setRenameInput({ oldName: ri.oldName, newName: e.target.value })}
                        style={{ width: 140, fontSize: 12, padding: '4px 8px' }}
                        autoFocus
                        onKeyDown={e => { if (e.key === 'Enter') confirmRename(); if (e.key === 'Escape') setRenameInput(null); }}
                      />
                      <button className="btn-primary-sm" onClick={confirmRename} disabled={renaming || !ri.newName.trim()} style={{ fontSize: 11 }}>
                        {renaming ? '⏳' : '✓'}
                      </button>
                      <button className="btn-ghost-sm" onClick={() => setRenameInput(null)} disabled={renaming} style={{ fontSize: 11 }}>×</button>
                    </div>
                    );
                  })() : (
                    <button
                      className="btn-ghost-sm"
                      onClick={() => startRename(ent.name)}
                      disabled={!!renameInput || renaming || mergeMode.size > 0}
                      style={{ fontSize: 11 }}
                    >
                      ✏️ 重命名
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

          {msg && <div className="success-msg" style={{ marginTop: 12, fontSize: 12 }}>{msg}</div>}
          {error && <div className="error-msg" style={{ marginTop: 12, fontSize: 12 }}>{error}</div>}
        </div>

        {/* 底部说明 */}
        <div style={{ padding: '10px 18px', borderTop: '1px solid var(--border-color)', fontSize: 11, color: 'var(--text-muted)', flexShrink: 0, paddingBottom: 'calc(10px + env(safe-area-inset-bottom, 0px))' }}>
          💡 勾选多个同名实体可合并；点"重命名"可整词替换到全部设定与正文（不可撤销）。
          <button className="btn-ghost-sm" onClick={loadEntities} disabled={loading} style={{ marginLeft: 8, fontSize: 11 }}>🔄 刷新</button>
        </div>
      </div>
    </div>
  );
}
