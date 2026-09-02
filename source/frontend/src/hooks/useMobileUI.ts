import { useState, useEffect, useCallback, useRef } from 'react';
import { legacyKey } from '../api';

/** 判断是否移动端视口 */
export function useIsMobile(breakpoint = 768): boolean {
  const [isMobile, setIsMobile] = useState(() =>
    typeof window !== 'undefined' && window.innerWidth < breakpoint
  );
  useEffect(() => {
    const mq = window.matchMedia(`(max-width: ${breakpoint - 1}px)`);
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    setIsMobile(mq.matches);
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, [breakpoint]);
  return isMobile;
}

/**
 * 监听软键盘弹起，把键盘高度写入 --kb-height CSS 变量，
 * 供编辑区底部留白跟随键盘，避免光标被遮挡。
 */
export function useKeyboardInset() {
  useEffect(() => {
    if (typeof window === 'undefined' || !window.visualViewport) return;
    const vv = window.visualViewport;
    const update = () => {
      const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      document.documentElement.style.setProperty('--kb-height', `${Math.round(kb)}px`);
    };
    update();
    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
    return () => {
      vv.removeEventListener('resize', update);
      vv.removeEventListener('scroll', update);
    };
  }, []);
}

/**
 * localStorage 本地草稿缓存：输入即写本地，断网不丢；
 * 检测是否存在比服务端更新的本地草稿，提示用户恢复。
 */
export function useDraftCache(key: string, serverContent: string) {
  const storageKey = `app-draft:${key}`;
  const legacyKey_ = legacyKey(`draft:${key}`);
  const [content, setContent] = useState<string>(() => {
    try {
      return localStorage.getItem(storageKey) ?? localStorage.getItem(legacyKey_) ?? serverContent;
    } catch {
      return serverContent;
    }
  });
  const [hasLocalDraft, setHasLocalDraft] = useState(false);

  // 当服务端内容变更（切换阶段/书籍）时，检测本地是否有未同步草稿
  useEffect(() => {
    try {
      const cached = localStorage.getItem(storageKey);
      if (cached != null && cached !== '' && cached !== serverContent) {
        setHasLocalDraft(true);
      } else {
        setHasLocalDraft(false);
        setContent(serverContent);
      }
    } catch {
      setContent(serverContent);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey, serverContent]);

  const update = useCallback((val: string) => {
    setContent(val);
    try {
      localStorage.setItem(storageKey, val);
    } catch { /* 配额满则忽略 */ }
  }, [storageKey]);

  const clear = useCallback(() => {
    try {
      localStorage.removeItem(storageKey);
    } catch { /* ignore */ }
    setHasLocalDraft(false);
  }, [storageKey]);

  const restoreLocal = useCallback(() => {
    setHasLocalDraft(false);
    // content 已是本地值，无需再设
  }, []);

  return { content, update, clear, hasLocalDraft, restoreLocal };
}

/** 中文字数统计：中文按字、英文按词、忽略空白与标点 */
export function countWordsZh(text: string): number {
  if (!text) return 0;
  const cleaned = text.replace(/\s+/g, '');
  const cjk = (cleaned.match(/[\u4e00-\u9fff\u3400-\u4dbf]/g) || []).length;
  const nonCjk = cleaned.replace(/[\u4e00-\u9fff\u3400-\u4dbf]/g, '');
  const enWords = nonCjk ? (nonCjk.match(/[a-zA-Z0-9]+/g) || []).length : 0;
  return cjk + enWords;
}

/**
 * 打字机滚动：输入时光标始终保持在视口中央偏上位置，
 * 避免软键盘遮挡，灵感来自 dumpsterfire.ink 的 typewriter scrolling。
 */
export function useTypewriterScroll(
  textareaRef: React.RefObject<HTMLTextAreaElement | null>,
  enabled: boolean
) {
  useEffect(() => {
    if (!enabled) return;
    const ta = textareaRef.current;
    if (!ta) return;

    const scrollToCursor = () => {
      const { scrollTop, scrollHeight, clientHeight } = ta;
      // 光标大致位置：基于 selectionStart 估算行偏移
      const textBeforeCursor = ta.value.substring(0, ta.selectionStart);
      const linesBefore = textBeforeCursor.split('\n').length;
      const totalLines = ta.value.split('\n').length || 1;
      const cursorRatio = linesBefore / totalLines;
      const targetScroll = cursorRatio * (scrollHeight - clientHeight);
      // 光标目标位置：视口 40% 处（偏上，留出键盘空间）
      const desiredOffset = clientHeight * 0.4;
      const newScroll = Math.max(0, targetScroll - desiredOffset);
      // 平滑滚动，避免跳动
      if (Math.abs(newScroll - scrollTop) > 5) {
        ta.scrollTo({ top: newScroll, behavior: 'smooth' });
      }
    };

    ta.addEventListener('input', scrollToCursor);
    ta.addEventListener('click', scrollToCursor);
    return () => {
      ta.removeEventListener('input', scrollToCursor);
      ta.removeEventListener('click', scrollToCursor);
    };
  }, [textareaRef, enabled]);
}

export interface SwipeHandlers {
  onSwipeLeft?: () => void;
  onSwipeRight?: () => void;
  onSwipeUp?: () => void;
  onSwipeDown?: () => void;
}

/**
 * 手势检测：在指定元素上监听 touch 滑动，
 * 阈值 50px + 300ms 内完成，避免误触。
 * 灵感来自移动端原生交互模式。
 */
export function useSwipeGesture(
  ref: React.RefObject<HTMLElement | null>,
  handlers: SwipeHandlers,
  threshold = 50
) {
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    let startX = 0, startY = 0, startTime = 0;

    const onTouchStart = (e: TouchEvent) => {
      const t = e.touches[0];
      startX = t.clientX;
      startY = t.clientY;
      startTime = Date.now();
    };

    const onTouchEnd = (e: TouchEvent) => {
      const t = e.changedTouches[0];
      const dx = t.clientX - startX;
      const dy = t.clientY - startY;
      const dt = Date.now() - startTime;
      if (dt > 500) return; // 超时不算滑动

      const absX = Math.abs(dx);
      const absY = Math.abs(dy);

      if (absX > threshold && absX > absY) {
        if (dx > 0) handlersRef.current.onSwipeRight?.();
        else handlersRef.current.onSwipeLeft?.();
      } else if (absY > threshold && absY > absX) {
        if (dy > 0) handlersRef.current.onSwipeDown?.();
        else handlersRef.current.onSwipeUp?.();
      }
    };

    el.addEventListener('touchstart', onTouchStart, { passive: true });
    el.addEventListener('touchend', onTouchEnd, { passive: true });
    return () => {
      el.removeEventListener('touchstart', onTouchStart);
      el.removeEventListener('touchend', onTouchEnd);
    };
  }, [ref, threshold]);
}

export interface WritingStats {
  words: number;
  duration: number; // 秒
  wpm: number; // 每分钟字数
  todayWords: number;
  streak: number; // 连续写作天数
}

/**
 * 写作统计：实时追踪字数、写作时长、WPM，
 * 并用 localStorage 记录每日写作量与连续打卡天数。
 * 灵感来自 dumpsterfire.ink 的 streak + activity grid。
 */
export function useWritingStats(currentWords: number, isWriting: boolean) {
  const [stats, setStats] = useState<WritingStats>({
    words: 0, duration: 0, wpm: 0, todayWords: 0, streak: 0,
  });
  const startWordsRef = useRef(0);
  const sessionStartRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 读取 localStorage 中的历史数据（兼容旧键）
  const readHistory = useCallback(() => {
    try {
      const raw = localStorage.getItem('app-writing-history') ?? localStorage.getItem(legacyKey('writing-history'));
      if (!raw) return { todayWords: 0, streak: 0, lastDate: '' };
      return JSON.parse(raw);
    } catch {
      return { todayWords: 0, streak: 0, lastDate: '' };
    }
  }, []);

  const saveHistory = useCallback((data: { todayWords: number; streak: number; lastDate: string }) => {
    try {
      localStorage.setItem('app-writing-history', JSON.stringify(data));
    } catch { /* ignore */ }
  }, []);

  // 初始化：读取今日数据与连续打卡
  useEffect(() => {
    const today = new Date().toISOString().slice(0, 10);
    const hist = readHistory();
    if (hist.lastDate === today) {
      setStats(prev => ({ ...prev, todayWords: hist.todayWords, streak: hist.streak }));
    } else {
      // 检查是否中断连续打卡
      const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
      const newStreak = hist.lastDate === yesterday ? hist.streak : 0;
      saveHistory({ todayWords: 0, streak: newStreak, lastDate: today });
      setStats(prev => ({ ...prev, todayWords: 0, streak: newStreak }));
    }
  }, [readHistory, saveHistory]);

  // 写作时长计时器
  useEffect(() => {
    if (!isWriting) {
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
      return;
    }
    sessionStartRef.current = Date.now();
    startWordsRef.current = currentWords;

    timerRef.current = setInterval(() => {
      const duration = Math.floor((Date.now() - sessionStartRef.current) / 1000);
      const sessionWords = currentWords - startWordsRef.current;
      const wpm = duration > 0 ? Math.round((sessionWords / duration) * 60) : 0;
      setStats(prev => ({ ...prev, words: sessionWords, duration, wpm }));
    }, 1000);

    return () => {
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isWriting]);

  // 当字数增加时，更新今日写作量并检查打卡
  const lastWordsRef = useRef(currentWords);
  useEffect(() => {
    const delta = currentWords - lastWordsRef.current;
    if (delta > 0) {
      const today = new Date().toISOString().slice(0, 10);
      const hist = readHistory();
      const newTodayWords = (hist.lastDate === today ? hist.todayWords : 0) + delta;
      const newStreak = hist.lastDate === today ? hist.streak : (hist.lastDate === new Date(Date.now() - 86400000).toISOString().slice(0, 10) ? hist.streak + 1 : 1);
      saveHistory({ todayWords: newTodayWords, streak: newStreak, lastDate: today });
      setStats(prev => ({ ...prev, todayWords: newTodayWords, streak: newStreak }));
    }
    lastWordsRef.current = currentWords;
  }, [currentWords, readHistory, saveHistory]);

  return stats;
}

/** 检测网络在线状态 */
export function useOnlineStatus(): boolean {
  const [online, setOnline] = useState(() =>
    typeof navigator !== 'undefined' ? navigator.onLine : true
  );
  useEffect(() => {
    const on = () => setOnline(true);
    const off = () => setOnline(false);
    window.addEventListener('online', on);
    window.addEventListener('offline', off);
    return () => {
      window.removeEventListener('online', on);
      window.removeEventListener('offline', off);
    };
  }, []);
  return online;
}
