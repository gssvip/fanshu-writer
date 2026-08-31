import { useEffect, useRef, useState } from 'react';
import { api } from '../api';

/**
 * 节点设计师视图（智驾助手窗口内的分段流式生成界面）
 *
 * 复用 node-design 后端「分段异步任务 + 渐进轮询」架构：
 *   - 提交 submit 秒回 job_id
 *   - 每 ~1.6s 轮询 status，节点逐段实时冒出来（类似圆桌会议的分段发言）
 *   - 生成完成后可「采纳全部」落地进 book_bible.timeline 对应卷的 nodes
 *   - 可对单个节点填写修改意见（revise 重生成并回填）
 *
 * 之所以不用一次性长生成：整卷一次性生成 5-15 分钟会撞 Cloudflare+Render 的双层
 * 网关超时（502 / network error），故后端按 main_event 一段一段生成，这里逐段呈现。
 */

const TYPE_LABEL: Record<string, string> = { M: '主线', C: '角色', W: '世界观', D: '日常', F: '伏笔' };
const TYPE_COLOR: Record<string, string> = { M: '#c25e00', C: '#2f6fbf', W: '#7a5bd0', D: '#2f8f5f', F: '#a33d6a' };
const ACCENT = '#0a7d4f';

export interface NodeDesignViewProps {
  bookId: string;
  volumeIndex: number;
  volumeTitle: string;
  onClose: () => void;
  /** 采纳落地成功后回调（用于刷新 bible / timeline 显示） */
  onApplied?: () => void;
}

type Phase = 'running' | 'done' | 'error' | 'cancelled' | 'adopted';

export default function NodeDesignView({ bookId, volumeIndex, volumeTitle, onClose, onApplied }: NodeDesignViewProps) {
  const [phase, setPhase] = useState<Phase>('running');
  const [nodes, setNodes] = useState<any[]>([]);
  const [done, setDone] = useState(0);
  const [total, setTotal] = useState(0);
  const [currentSegment, setCurrentSegment] = useState('');
  const [message, setMessage] = useState('正在提交节点设计任务…');
  const [error, setError] = useState('');
  const [applying, setApplying] = useState(false);
  const [reviseIndex, setReviseIndex] = useState<number | null>(null);
  const [reviseText, setReviseText] = useState('');
  const [revising, setRevising] = useState(false);
  const jobIdRef = useRef<string | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const aliveRef = useRef(false);

  useEffect(() => {
    let alive = true;
    aliveRef.current = alive;
    setPhase('running');
    setNodes([]);
    setDone(0);
    setTotal(0);
    setError('');
    setMessage('正在提交节点设计任务…');

    (async () => {
      try {
        const sub = await api.nodeDesignSubmit(bookId, volumeIndex, { action: 'start' });
        if (!alive) { api.nodeDesignCancel(bookId, sub.job_id || '').catch(() => {}); return; }
        if (!sub.ok) throw new Error(sub.error || '任务提交失败，请重试');
        jobIdRef.current = sub.job_id || null;
        sessionIdRef.current = sub.session_id || null;
        setTotal(sub.total || 0);
        setMessage(sub.total ? `已提交，共 ${sub.total} 段，正在分段生成…` : '分段生成中…');

        while (alive) {
          const st = await api.nodeDesignStatus(bookId, sub.job_id || '');
          if (!alive) break;
          if (st.nodes?.length) setNodes(st.nodes);
          if (st.total) setTotal(st.total);
          if (st.done !== undefined) setDone(st.done);
          if (st.current_segment) setCurrentSegment(st.current_segment);
          if (st.message) setMessage(st.message);
          if (st.state === 'done') {
            setPhase('done');
            if (st.nodes?.length) setNodes(st.nodes);
            setMessage(st.message || `已完成，共 ${st.nodes?.length || 0} 个情节节点。`);
            break;
          }
          if (st.state === 'error') { setPhase('error'); setError(st.error || '生成失败，请重试'); break; }
          if (st.state === 'cancelled') { setPhase('cancelled'); setMessage('任务已取消'); break; }
          await new Promise(r => setTimeout(r, 1600));
        }
      } catch (e: any) {
        if (!alive) return;
        setPhase('error');
        setError(e?.message || '节点设计失败，请重试');
      } finally {
        alive = false;
      }
    })();

    return () => { alive = false; aliveRef.current = false; };
  }, [bookId, volumeIndex]);

  const acceptAll = async () => {
    setApplying(true);
    setError('');
    try {
      const r = await api.nodeDesignApply(bookId, volumeIndex, nodes);
      if (!r.ok) throw new Error(r.error || '落地失败');
      setPhase('adopted');
      setMessage(`✅ 已采纳 ${r.node_count ?? nodes.length} 个节点到第${volumeIndex}卷剧情线。`);
      onApplied?.();
    } catch (e: any) {
      setError(e?.message || '落地失败，请重试');
    } finally {
      setApplying(false);
    }
  };

  const startRevise = (idx: number) => { setReviseIndex(idx); setReviseText(''); setError(''); };

  const confirmRevise = async () => {
    if (!reviseText.trim()) { setError('请填写修改意见'); return; }
    setRevising(true);
    setError('');
    const target = reviseIndex;
    try {
      const sub = await api.nodeDesignSubmit(bookId, volumeIndex, {
        action: 'revise',
        sessionId: sessionIdRef.current || undefined,
        nodeIndex: target!,
        feedback: reviseText.trim(),
      });
      if (!sub.ok || !sub.job_id) throw new Error(sub.error || '修改提交失败');
      setMessage(`正在按意见重写节点 #${target}…`);
      let alive = true;
      while (alive) {
        const st = await api.nodeDesignStatus(bookId, sub.job_id);
        if (!alive) break;
        if (st.state === 'done' && st.result) {
          setNodes(prev => (prev || []).map(n => (Number(n.index) === Number(target) && typeof st.result === 'object' ? { ...st.result } : n)));
          setReviseIndex(null);
          setMessage(`✅ 节点 #${target} 已按意见更新。`);
          break;
        }
        if (st.state === 'error') { setError(st.error || '修改失败'); setReviseIndex(null); break; }
        await new Promise(r => setTimeout(r, 1600));
      }
    } catch (e: any) {
      setError(e?.message || '修改失败，请重试');
    } finally {
      setRevising(false);
    }
  };

  const cancelTask = async () => {
    if (jobIdRef.current) api.nodeDesignCancel(bookId, jobIdRef.current).catch(() => {});
    onClose();
  };

  const segDone = total > 0 ? Math.min(done, total) : 0;
  const segPct = total > 0 ? Math.round((segDone / total) * 100) : nodes.length > 0 ? 60 : 0;
  const busy = phase === 'running';
  const canAdopt = !busy && phase !== 'error' && nodes.length > 0;

  return (
    <div style={{ position: 'fixed', inset: 0, zIndex: 2100, background: 'rgba(0,0,0,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 16 }} onClick={onClose}>
      <div
        onClick={e => e.stopPropagation()}
        style={{ width: 'min(640px, 100%)', maxHeight: '86vh', display: 'flex', flexDirection: 'column', background: '#fff', borderRadius: 14, boxShadow: '0 12px 40px rgba(0,0,0,0.25)', overflow: 'hidden', fontFamily: 'system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif' }}
      >
        {/* 头部 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px', background: '#f6faf6', borderBottom: '1px solid #e4efe4' }}>
          <span style={{ fontSize: 20 }}>🎯</span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 15, color: '#1f3d2a' }}>节点设计师</div>
            <div style={{ fontSize: 12, color: '#7a9a7a', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              第{volumeIndex}卷「{volumeTitle}」· 分段流式生成
            </div>
          </div>
          {busy ? <span style={{ fontSize: 12, color: ACCENT, fontWeight: 600 }}>● 生成中</span> : phase === 'done' || phase === 'adopted' ? <span style={{ fontSize: 12, color: '#288f2b', fontWeight: 600 }}>✓ 已完成</span> : null}
          <button onClick={onClose} style={{ border: 'none', background: 'transparent', fontSize: 20, color: '#7a9a7a', cursor: 'pointer', lineHeight: 1 }} title="关闭">×</button>
        </div>

        {/* 进度条 */}
        <div style={{ padding: '4px 16px 0' }}>
          <div style={{ height: 6, background: '#e4efe4', borderRadius: 3, overflow: 'hidden' }}>
            <div style={{ height: '100%', width: `${segPct}%`, background: ACCENT, borderRadius: 3, transition: 'width .3s ease' }} />
          </div>
          <div style={{ fontSize: 11, color: '#8aa88a', margin: '4px 0 0', display: 'flex', justifyContent: 'space-between' }}>
            <span>{busy && total > 0 ? `正在生成 ${segDone}/${total} 段…` : message}</span>
            <span>{total > 0 ? `${segPct}%` : ''}</span>
          </div>
          {currentSegment && busy && (
            <div style={{ fontSize: 12, color: ACCENT, marginTop: 4 }}>🔨 当前：{currentSegment}</div>
          )}
        </div>

        {/* 错误提示 */}
        {error && (
          <div style={{ margin: '10px 16px 0', padding: '8px 12px', background: '#fdeaea', color: '#c32e2e', borderRadius: 8, fontSize: 13 }}>{error}</div>
        )}

        {/* 节点列表 */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
          {busy && nodes.length === 0 && (
            <div style={{ fontSize: 13, color: '#8aa88a', padding: '24px 0', textAlign: 'center' }}>⏳ 正在读取卷剧情并分段生成，节点将逐段出现…</div>
          )}
          {nodes.map((n: any, i: number) => {
            const t = TYPE_LABEL[n?.type] ? n.type : 'M';
            const tColor = TYPE_COLOR[t] || ACCENT;
            const isRevising = reviseIndex === n?.index;
            return (
              <div key={i} style={{ border: '1px solid #e4efe4', borderRadius: 10, padding: '10px 12px', background: '#fbfefb' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <span style={{ fontSize: 12, background: tColor, color: '#fff', borderRadius: 4, padding: '1px 6px', fontWeight: 600 }}>{t ? TYPE_LABEL[t] : '节点'}</span>
                  <span style={{ fontSize: 12, color: '#7a9a7a' }}>#{n?.index ?? i + 1} · 第{n?.chapters ?? '?'}章</span>
                  <span style={{ flex: 1 }} />
                  {n?.cool_type ? <span style={{ fontSize: 11, background: '#eef6ee', color: ACCENT, borderRadius: 4, padding: '1px 6px' }}>⚡{n.cool_type}{n.cool_level ? '·' + n.cool_level : ''}</span> : null}
                  {('done' === phase || phase === 'adopted') && (
                    <button onClick={() => startRevise(Number(n?.index))} style={{ border: '1px solid #cddccd', background: '#fff', color: '#4a6b4a', fontSize: 12, borderRadius: 6, padding: '2px 8px', cursor: 'pointer' }} title="对当前节点提出修改意见">✏️ 修改</button>
                  )}
                </div>
                <div style={{ fontWeight: 600, fontSize: 14, color: '#1f3d2a', marginTop: 4 }}>{n?.title || '（未命名节点）'}</div>
                <div style={{ fontSize: 12.5, color: '#55655a', marginTop: 3, lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>{n?.summary || ''}</div>
                {(n?.hook || n?.conflict) && (
                  <div style={{ fontSize: 12, color: '#7a9a7a', marginTop: 4 }}>
                    {n?.conflict ? <span>⚔️ {n.conflict}</span> : null}
                    {n?.hook ? <span style={{ marginLeft: n?.conflict ? 12 : 0 }}>🪝 {n.hook}</span> : null}
                  </div>
                )}
                {isRevising && (
                  <div style={{ marginTop: 8, paddingTop: 8, borderTop: '1px dashed #d8e6d8' }}>
                    <textarea
                      value={reviseText}
                      onChange={e => setReviseText(e.target.value)}
                      placeholder={`填写对节点 #${n?.index} 的修改意见（如：打脸强度再大些、埋一个XXX伏笔、节奏加快）`}
                      rows={2}
                      style={{ width: '100%', boxSizing: 'border-box', border: '1px solid #cddccd', borderRadius: 8, fontSize: 13, padding: 8, outline: 'none' }}
                    />
                    <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
                      <button onClick={confirmRevise} disabled={revising} style={{ border: 'none', background: ACCENT, color: '#fff', fontSize: 13, borderRadius: 6, padding: '5px 12px', cursor: 'pointer' }}>{revising ? '修改中…' : '提交修改'}</button>
                      <button onClick={() => setReviseIndex(null)} style={{ border: '1px solid #cddccd', background: '#fff', color: '#4a6b4a', fontSize: 13, borderRadius: 6, padding: '5px 12px', cursor: 'pointer' }}>取消</button>
                    </div>
                  </div>
                )}
                {busy && i === nodes.length - 1 && <span style={{ display: 'inline-block', marginLeft: 6, color: ACCENT, animation: 'nd-cursor 1s step-end infinite' }}>▋</span>}
              </div>
            );
          })}
        </div>

        {/* 底部操作 */}
        <div style={{ padding: '10px 16px', borderTop: '1px solid #e4efe4', background: '#fbfefb', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          {canAdopt && (
            <button onClick={acceptAll} disabled={applying} style={{ border: 'none', background: ACCENT, color: '#fff', fontSize: 14, fontWeight: 600, borderRadius: 8, padding: '8px 18px', cursor: 'pointer' }}>
              {applying ? '落地中…' : `✅ 采纳全部（${nodes.length} 个节点）到剧情线`}
            </button>
          )}
          {busy && (
            <button onClick={cancelTask} style={{ border: '1px solid #cddccd', background: '#fff', color: '#4a6b4a', fontSize: 13, borderRadius: 6, padding: '6px 12px', cursor: 'pointer' }}>取消生成</button>
          )}
          <div style={{ flex: 1 }} />
          <button onClick={onClose} style={{ border: '1px solid #cddccd', background: '#fff', color: '#4a6b4a', fontSize: 13, borderRadius: 6, padding: '6px 12px', cursor: 'pointer' }}>关闭</button>
        </div>

        <style>{`@keyframes nd-cursor { 50% { opacity: 0; } }`}</style>
      </div>
    </div>
  );
}