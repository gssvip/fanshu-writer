// 节点设计师（node_designer）独立组件：进度解析纯函数 + 进度条工具栏。
// 自 ChatPanel.tsx 拆出（P2-7 前端巨石拆分），降低智驾面板复杂度：
//   - parseNodeDesignerProgress：纯函数（零 React 依赖），从 AI 消息流解析节点设计进度
//   - NodeDesignerProgress：命中 node_designer 角色时渲染的进度条 + 一键继续按钮
import type { AIMessage } from '../types';

// ========== 节点设计师进度解析（工具栏进度条 + 中途半截卡片复用）
export function parseNodeDesignerProgress(msgs: AIMessage[], defaultCPV = 50): { last_ch: number; cpv: number; vi: number | null; from_card: boolean } {
  let lastCh = 0;
  let cpv = defaultCPV;
  let vi: number | null = null;
  let fromCard = false;
  const chapterRE = /第[零一二三四五六七八九十百千万\d〇两]{1,8}[章节回话卷]/g;
  const toNumber = (s: string): number | null => {
    if (/^\d+$/.test(s)) return parseInt(s, 10);
    const map: Record<string, number> = { '零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10 };
    if (s.length === 1) return map[s] ?? null;
    if (s.startsWith('十')) {
      const rest = s.slice(1);
      return 10 + (rest ? (map[rest] ?? NaN) : 0);
    }
    if (s.includes('十')) {
      const [a, b] = s.split('十');
      const tens = map[a];
      const ones = b ? (map[b] ?? 0) : 0;
      if (typeof tens === 'number') return tens * 10 + ones;
    }
    if (/百/.test(s)) {
      const m = s.match(/^([一二三四五六七八九两])百([一二三四五六七八九十零〇两]{0,3})$/);
      if (m) {
        const h = map[m[1]];
        let tail = 0;
        if (m[2]) {
          const n = toNumber(m[2]);
          if (n) tail = n;
        }
        return h * 100 + tail;
      }
    }
    return null;
  };
  // 从文本里识别「第N卷」（阿拉伯数字 + 中文数字），用于流式输出阶段（尚无 SAVE_PLOT 卡片时）
  // 兜底卷号，否则中途进度条永远显示「第1卷」。
  const volumeRE = /第\s*([零一二三四五六七八九十百千万\d〇两]{1,8})\s*卷/g;
  let volFromText: number | null = null;
  for (const m of msgs) {
    // 跳过系统通知类消息（❌失败提示/⚠️/【连接中断·抢救】里含「第8章改XXX」等示例文案，会虚抬进度）
    const c0 = (m.content || '').trim();
    const isNotice = m.role === 'assistant' && (c0.startsWith('❌') || c0.startsWith('⚠️') || c0.startsWith('【连接中断'));
    if (m.role === 'assistant' && m.content && !isNotice) {
      const matches = m.content.match(chapterRE) || [];
      for (const raw of matches) {
        const head = raw.replace(/[章节回话卷]/g, '').replace(/^第/, '');
        if (!head) continue;
        const n = toNumber(head);
        if (typeof n === 'number' && n <= 1000 && n > lastCh) lastCh = n;
      }
      // 识别「第N卷」：取本消息中出现过的最大卷号（开场白「这是第7卷情节节点设计」最可靠）
      const volMatches = m.content.match(volumeRE) || [];
      for (const raw of volMatches) {
        const head = raw.replace(/[卷]/g, '').replace(/^第/, '').trim();
        if (!head) continue;
        const n = toNumber(head);
        if (typeof n === 'number' && n >= 1 && n <= 99) {
          if (volFromText === null || n > volFromText) volFromText = n;
        }
      }
    }
    // SAVE_PLOT 卡片里的 nodes 章号汇总（更精准）
    if (Array.isArray(m.cards)) {
      for (const c of m.cards) {
        if ((c as any).type !== 'SAVE_PLOT') continue;
        try {
          const content = ((c as any).content || '').trim();
          if (!content.startsWith('[')) continue;
          const arr = JSON.parse(content);
          if (Array.isArray(arr) && arr[0]) {
            const v: any = arr[0];
            if (typeof v.volume_index === 'number') vi = v.volume_index;
            if (typeof v.chapter_count === 'number' && v.chapter_count > 0) {
              cpv = v.chapter_count;
              fromCard = true;
            }
            if (Array.isArray(v.nodes)) {
              for (const n of v.nodes) {
                const chs = n?.chapters;
                if (typeof chs === 'number' && chs > lastCh) lastCh = chs;
                if (Array.isArray(chs) && typeof chs[0] === 'number' && typeof chs[1] === 'number') {
                  if (chs[1] > lastCh) lastCh = chs[1];
                }
              }
            }
          }
        } catch { /* ignore */ }
      }
    }
  }
  // 把「全书全局连续章号」归一化成「卷内号 1..cpv」：
  // 节点设计师第2卷起输出的就是全书全局号（第2卷=51~100），卡片 nodes.chapters 也统一全局号。
  // 已知 vi 时减去 (vi-1)*cpv 还原卷内号；未知 vi 时按 cpv 取模兜底。否则第2卷半截(51~75)会被
  // 误判成 lastCh=75 → done=min(50,75)=50 → 进度条虚报「全卷完成」。
  // 卷号优先用 SAVE_PLOT 卡片的 volume_index（最权威）；卡片尚无（流式中途）则回退到从开场白
  // 「这是第N卷情节节点设计」里扫描到的卷号，避免进度条永远显示「第1卷」。
  const viFinal = (vi && vi > 0) ? vi : volFromText;
  if (lastCh > cpv) {
    if (viFinal && viFinal > 0) {
      const local = lastCh - (viFinal - 1) * cpv;
      lastCh = (local >= 1 && local <= cpv) ? local : Math.min(cpv, (lastCh % cpv) || cpv);
    } else {
      lastCh = Math.min(cpv, (lastCh % cpv) || cpv);
    }
  }
  return { last_ch: lastCh, cpv, vi: viFinal, from_card: fromCard };
}

// ========== 节点设计师工具栏：进度条 + 一键继续按钮 ==========
export function NodeDesignerProgress({ messages, streaming, onQuickContinue }: {
  messages: AIMessage[];
  streaming: boolean;
  onQuickContinue: () => void;
}) {
  const prog = parseNodeDesignerProgress(messages, 50);
  const { last_ch, cpv, vi, from_card } = prog;
  const done = Math.max(0, Math.min(cpv, last_ch));
  // 只有 SAVE_PLOT 卡片证实的完整进度才算"完成"：
  // 正则兜底会被模型开场白（"将设计第1章~第50章"）虚抬 → 意外终止后误判完成 → 继续按钮消失
  const isDone = from_card && done >= cpv;
  const volNo = vi || 1;
  return (
    <div style={{
      marginTop: 10,
      padding: '8px 12px',
      borderRadius: 10,
      background: isDone
        ? 'linear-gradient(180deg,#ecfdf5 0%,#f0fdf4 100%)'
        : 'linear-gradient(180deg,#eff6ff 0%,#f5f3ff 100%)',
      border: `1px solid ${isDone ? '#a7f3d0' : '#bfdbfe'}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'nowrap', overflow: 'hidden' }}>
        <strong style={{ color: isDone ? '#047857' : 'var(--text-primary)', fontSize: 13, whiteSpace: 'nowrap' }}>
          {isDone ? '✅ 节点设计全卷完成' : `🎯 节点设计进度（第${volNo}卷）`}
        </strong>
        <span style={{ fontSize: 12, color: '#374151', whiteSpace: 'nowrap' }}>
          {done}/{cpv}章
        </span>
        <span style={{ flex: 1, minWidth: 6 }} />
        <button
          className="chat-send primary"
          style={{ padding: '4px 12px', minHeight: 26, fontSize: 13, whiteSpace: 'nowrap' }}
          onClick={onQuickContinue}
          disabled={streaming || isDone}
          title={isDone ? '整卷已完成，如需修改某章直接说「第X章改XXX」' : '从写到的最后一章继续生成（等同于发送「继续」）'}
        >
          {isDone ? '全卷已完成' : '继续'}
        </button>
      </div>
    </div>
  );
}