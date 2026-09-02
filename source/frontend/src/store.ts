import { create } from 'zustand';
import type { Book, Chapter } from './types';

interface User { id: string; username: string; email: string; }

type Theme = 'light' | 'dark' | 'green' | 'custom';

interface CustomThemeColors {
  bgPrimary: string;
  bgSecondary: string;
  bgTertiary: string;
  textPrimary: string;
  textSecondary: string;
  textMuted: string;
  accent: string;
  borderColor: string;
}

interface AppStore {
  books: Book[];
  currentBook: Book | null;
  chapters: Chapter[];
  currentChapter: Chapter | null;
  currentUser: User | null;
  theme: Theme;
  customColors: CustomThemeColors;
  sidebarOpen: boolean;
  rightPanel: 'ai' | 'characters' | 'outline' | 'stats' | null;
  rightPanelWidth: number;
  loading: boolean;
  // 聊天驱动创作浮窗（全局，跨页面可用）
  chatPanelOpen: boolean;
  chatPanelBookId: string | null;
  // 待打开的历史会话 id：从历史对话「继续」按钮传入，ChatPanel 读取后自动加载该会话
  chatPanelSessionId: string | null;
  // 标记首次跳转：AI修正/修正正文首次进入时新建会话，后续复用 chatPanelSessionId
  chatPanelFixSessionBound: boolean;
  // 打开时预设的 Tab 与输入框内容（用于从其它入口跳转预填，如「修正正文」从防遗忘报告跳入）
  chatPanelPresetTab: 'setting' | 'chapter' | 'deai' | 'review' | null;
  chatPanelPresetInput: string | null;
  // 打开时预设的通用助手角色 id（如 "node_designer"），并可选择是否 autoSubmit 预设输入
  chatPanelPresetRole: string | null;
  chatPanelPresetAutoSubmit: boolean;
  // 节点设计师浮层（在智驾助手窗口内展示分段流式生成视图）——保留兼容，实际不推荐
  nodeDesignView: { volumeIndex: number; volumeTitle: string } | null;
  // 智驾采纳卡片落地 Bible 后，用这个通知外部（如 WritePage）重新拉取 Bible；
  // 存单调递增序号，组件 effect 监听值变化就重新 getBible，避免旧 UI 看起来"采纳后还是空的"。
  bibleDirtySeq: number;
  // 预设的修正任务清单（从防遗忘报告违规项带入，支持多章/多维度连续修正并追踪进度）
  chatPanelPresetFixTasks: Array<{ location: string; desc: string; fix: string; severity?: string; dimKey?: string }> | null;
  // P0 榜单风向：打开智驾时带入的扫榜报告（首条消息渲染 RankScanCard + 所有请求 rank_scan 字段）
  chatPanelPresetRankScan: any | null;
  chatPanelPresetRankScanPlatform: 'fanqie' | 'qidian' | null;

  setBooks: (books: Book[]) => void;
  setCurrentBook: (book: Book | null) => void;
  setChapters: (chapters: Chapter[]) => void;
  setCurrentChapter: (chapter: Chapter | null) => void;
  setCurrentUser: (user: User | null) => void;
  setTheme: (theme: Theme) => void;
  setCustomColors: (colors: Partial<CustomThemeColors>) => void;
  setSidebarOpen: (open: boolean) => void;
  setRightPanel: (panel: 'ai' | 'characters' | 'outline' | 'stats' | null) => void;
  setRightPanelWidth: (width: number) => void;
  setLoading: (loading: boolean) => void;
  openChatPanel: (bookId: string, sessionId?: string | null, preset?: any) => void;
  setChatPanelSessionId: (sessionId: string | null) => void;
  closeChatPanel: () => void;
  openNodeDesignView: (volumeIndex: number, volumeTitle: string) => void;
  closeNodeDesignView: () => void;
  markBibleDirty: () => void;
  // 运行中更新 ChatPanel 的当前扫榜报告（用户点📈重扫后调用，请求时统一带）
  setChatPanelRankScan: (rankScan: any, platform?: 'fanqie' | 'qidian') => void;
  logout: () => void;
}

const DEFAULT_CUSTOM_COLORS: CustomThemeColors = {
  bgPrimary: '#f0f7f0',
  bgSecondary: '#e8f3e8',
  bgTertiary: '#d6e8d6',
  textPrimary: '#2d3e2d',
  textSecondary: '#4a6b4a',
  textMuted: '#7a9a7a',
  accent: '#4a8b4a',
  borderColor: '#c0d8c0',
};

function loadCustomColors(): CustomThemeColors {
  try {
    const raw = localStorage.getItem('fanshu-custom-colors');
    if (raw) return { ...DEFAULT_CUSTOM_COLORS, ...JSON.parse(raw) };
  } catch { /* ignore */ }
  return DEFAULT_CUSTOM_COLORS;
}

export const useStore = create<AppStore>((set) => ({
  books: [],
  currentBook: null,
  chapters: [],
  currentChapter: null,
  currentUser: null,
  // theme / customColors 在模块加载阶段同步访问 localStorage。
  // 若用户浏览器开了 Edge Tracking Prevention / 隐身模式 / PWA 清 storage，
  // localStorage 会抛 SecurityError → 直接打断 React 根挂载 = 白屏，
  // 所以这里全部 try/catch 兜底到安全默认值。
  theme: (() => {
    try {
      const t = localStorage.getItem('fanshu-theme');
      // 兼容旧数据：sepia 映射为 green
      if (t === 'sepia') { try { localStorage.setItem('fanshu-theme', 'green'); } catch {} return 'green' as Theme; }
      return (t as Theme) || 'light';
    } catch {
      return 'light';
    }
  })(),
  customColors: loadCustomColors(),
  sidebarOpen: true,
  rightPanel: null,
  rightPanelWidth: 380,
  loading: false,
  chatPanelOpen: false,
  chatPanelBookId: null,
  chatPanelSessionId: null,
  chatPanelFixSessionBound: false,
  chatPanelPresetTab: null,
  chatPanelPresetInput: null,
  chatPanelPresetRole: null,
  chatPanelPresetAutoSubmit: false,
  nodeDesignView: null,
  bibleDirtySeq: 0,
  chatPanelPresetFixTasks: null,
  chatPanelPresetRankScan: null,
  chatPanelPresetRankScanPlatform: null,

  setBooks: (books) => set({ books }),
  setCurrentBook: (book) => set({ currentBook: book }),
  setChapters: (chapters) => set({ chapters }),
  setCurrentChapter: (chapter) => set({ currentChapter: chapter }),
  setCurrentUser: (user) => set({ currentUser: user }),
  setTheme: (theme) => {
    try { localStorage.setItem('fanshu-theme', theme); } catch {}
    set({ theme });
  },
  setCustomColors: (colors) => {
    set((state) => {
      const next = { ...state.customColors, ...colors };
      try { localStorage.setItem('fanshu-custom-colors', JSON.stringify(next)); } catch {}
      return { customColors: next };
    });
  },
  setSidebarOpen: (open) => set({ sidebarOpen: open }),
  setRightPanel: (panel) => set({ rightPanel: panel }),
  setRightPanelWidth: (width) => set({ rightPanelWidth: width }),
  setLoading: (loading) => set({ loading }),
  openChatPanel: (bookId, sessionId, preset) => set({
    chatPanelOpen: true,
    chatPanelBookId: bookId,
    chatPanelSessionId: sessionId ?? null,
    chatPanelPresetTab: preset?.tab ?? null,
    chatPanelPresetInput: preset?.input ?? null,
    chatPanelPresetFixTasks: preset?.fixTasks ?? null,
    chatPanelPresetRole: preset?.role ?? null,
    chatPanelPresetAutoSubmit: !!preset?.autoSubmit,
    chatPanelPresetRankScan: preset?.rankScan ?? null,
    chatPanelPresetRankScanPlatform: preset?.rankScanPlatform ?? null,
  }),
  setChatPanelSessionId: (sessionId) => set({ chatPanelSessionId: sessionId }),
  closeChatPanel: () => set({ chatPanelOpen: false, chatPanelSessionId: null, chatPanelFixSessionBound: false, chatPanelPresetTab: null, chatPanelPresetInput: null, chatPanelPresetFixTasks: null, chatPanelPresetRole: null, chatPanelPresetAutoSubmit: false, chatPanelPresetRankScan: null, chatPanelPresetRankScanPlatform: null, nodeDesignView: null }),
  openNodeDesignView: (volumeIndex, volumeTitle) => set({ nodeDesignView: { volumeIndex, volumeTitle } }),
  closeNodeDesignView: () => set({ nodeDesignView: null }),
  markBibleDirty: () => set(state => ({ bibleDirtySeq: (state.bibleDirtySeq || 0) + 1 })),
  setChatPanelRankScan: (rankScan, platform) => set({
    chatPanelPresetRankScan: rankScan ?? null,
    chatPanelPresetRankScanPlatform: platform ?? null,
  }),
  logout: () => {
    try { localStorage.removeItem('fanshu-token'); } catch {}
    set({ currentUser: null, books: [], currentBook: null });
  },
}));
