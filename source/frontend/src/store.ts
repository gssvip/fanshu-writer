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
  openChatPanel: (bookId: string, sessionId?: string | null) => void;
  closeChatPanel: () => void;
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
  theme: (() => {
    const t = localStorage.getItem('fanshu-theme');
    // 兼容旧数据：sepia 映射为 green
    if (t === 'sepia') { localStorage.setItem('fanshu-theme', 'green'); return 'green' as Theme; }
    return (t as Theme) || 'light';
  })(),
  customColors: loadCustomColors(),
  sidebarOpen: true,
  rightPanel: null,
  rightPanelWidth: 380,
  loading: false,
  chatPanelOpen: false,
  chatPanelBookId: null,
  chatPanelSessionId: null,

  setBooks: (books) => set({ books }),
  setCurrentBook: (book) => set({ currentBook: book }),
  setChapters: (chapters) => set({ chapters }),
  setCurrentChapter: (chapter) => set({ currentChapter: chapter }),
  setCurrentUser: (user) => set({ currentUser: user }),
  setTheme: (theme) => {
    localStorage.setItem('fanshu-theme', theme);
    set({ theme });
  },
  setCustomColors: (colors) => {
    set((state) => {
      const next = { ...state.customColors, ...colors };
      localStorage.setItem('fanshu-custom-colors', JSON.stringify(next));
      return { customColors: next };
    });
  },
  setSidebarOpen: (open) => set({ sidebarOpen: open }),
  setRightPanel: (panel) => set({ rightPanel: panel }),
  setRightPanelWidth: (width) => set({ rightPanelWidth: width }),
  setLoading: (loading) => set({ loading }),
  openChatPanel: (bookId, sessionId) => set({ chatPanelOpen: true, chatPanelBookId: bookId, chatPanelSessionId: sessionId ?? null }),
  closeChatPanel: () => set({ chatPanelOpen: false, chatPanelSessionId: null }),
  logout: () => {
    localStorage.removeItem('fanshu-token');
    set({ currentUser: null, books: [], currentBook: null });
  },
}));
