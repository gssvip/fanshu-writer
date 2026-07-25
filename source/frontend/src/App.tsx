import { HashRouter, Routes, Route, Navigate, useNavigate, useLocation } from 'react-router-dom';
import { useState, useEffect, createContext, useContext, useCallback } from 'react';
import { useStore } from './store';
import { api } from './api';
import AuthModal from './components/AuthModal';
import WorkbenchPage from './pages/WorkbenchPage';
import WritePage from './pages/WritePage';
import ToolsPage from './pages/ToolsPage';
import MinePage from './pages/MinePage';
import './index.css';

const TABS = [
  { key: 'workbench', label: '首页', icon: '🏠', path: '/workbench' },
  { key: 'write', label: '创作', icon: '✍️', path: '/write' },
  { key: 'tools', label: '工具', icon: '🔧', path: '/tools' },
  { key: 'mine', label: '我的', icon: '👤', path: '/mine' },
];

export const AuthContext = createContext<{ requireAuth: () => Promise<boolean> }>({ requireAuth: async () => false });

function TabBar() {
  const navigate = useNavigate();
  const location = useLocation();
  const currentTab = TABS.find(t => location.pathname.startsWith(t.path))?.key || 'workbench';
  return (
    <nav className="tab-bar">
      {TABS.map(tab => (
        <button key={tab.key} className={`tab-item ${currentTab === tab.key ? 'active' : ''}`} onClick={() => navigate(tab.path)}>
          <span className="tab-icon">{tab.icon}</span>
          <span className="tab-label">{tab.label}</span>
        </button>
      ))}
    </nav>
  );
}

function DesktopSidebar() {
  const navigate = useNavigate();
  const location = useLocation();
  const currentTab = TABS.find(t => location.pathname.startsWith(t.path))?.key || 'workbench';
  const { currentUser, logout } = useStore() as any;
  const { requireAuth } = useContext(AuthContext);
  return (
    <aside className="desktop-sidebar">
      <div className="sidebar-brand">
        <span className="sidebar-logo"><img src="/logo.jpg" alt="logo" style={{width:28,height:28,borderRadius:6,objectFit:'cover'}} /></span>
        <span className="sidebar-title">番薯写作</span>
      </div>
      <nav className="sidebar-nav">
        {TABS.map(tab => (
          <button key={tab.key} className={`sidebar-item ${currentTab === tab.key ? 'active' : ''}`} onClick={() => navigate(tab.path)}>
            <span>{tab.icon}</span>
            <span>{tab.label}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar-footer">
        {currentUser ? (
          <div className="sidebar-user">
            <div className="avatar-circle">{currentUser.username?.[0]?.toUpperCase() || '?'}</div>
            <div className="sidebar-user-info">
              <div className="sidebar-username">{currentUser.username}</div>
            </div>
            <button className="btn-ghost-sm" onClick={() => { logout?.(); }} title="退出">↩</button>
          </div>
        ) : (
          <button className="sidebar-login-btn" onClick={() => requireAuth()}>登录 / 注册</button>
        )}
      </div>
    </aside>
  );
}

function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="app-layout">
      <DesktopSidebar />
      <main className="main-content">
        {children}
      </main>
      <TabBar />
    </div>
  );
}

export default function App() {
  const { theme, customColors, setCurrentUser, currentUser, setBooks } = useStore() as any;
  const [authChecked, setAuthChecked] = useState(false);
  const [showAuth, setShowAuth] = useState(false);
  const [authResolve, setAuthResolve] = useState<((v: boolean) => void) | null>(null);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    // 自定义主题：注入 CSS 变量
    if (theme === 'custom' && customColors) {
      const root = document.documentElement;
      root.style.setProperty('--bg-primary', customColors.bgPrimary);
      root.style.setProperty('--bg-secondary', customColors.bgSecondary);
      root.style.setProperty('--bg-tertiary', customColors.bgTertiary);
      root.style.setProperty('--text-primary', customColors.textPrimary);
      root.style.setProperty('--text-secondary', customColors.textSecondary);
      root.style.setProperty('--text-muted', customColors.textMuted);
      root.style.setProperty('--accent', customColors.accent);
      root.style.setProperty('--border-color', customColors.borderColor);
    } else {
      // 切换到预设主题时清除自定义变量
      const root = document.documentElement;
      root.style.removeProperty('--bg-primary');
      root.style.removeProperty('--bg-secondary');
      root.style.removeProperty('--bg-tertiary');
      root.style.removeProperty('--text-primary');
      root.style.removeProperty('--text-secondary');
      root.style.removeProperty('--text-muted');
      root.style.removeProperty('--accent');
      root.style.removeProperty('--border-color');
    }
  }, [theme, customColors]);

  // 应用启动时加载背景图片
  useEffect(() => {
    const savedBg = localStorage.getItem('fanshu-bg-image');
    if (savedBg) {
      document.body.style.backgroundImage = `url(${savedBg})`;
      document.body.style.backgroundSize = 'cover';
      document.body.style.backgroundPosition = 'center';
      document.body.style.backgroundAttachment = 'fixed';
    }
  }, []);

  useEffect(() => {
    api.getMe().then(u => { setCurrentUser?.(u); }).catch(() => {}).finally(() => setAuthChecked(true));
  }, []);

  useEffect(() => {
    if (currentUser) {
      api.listBooks().then(books => setBooks?.(books)).catch(() => {});
    }
  }, [currentUser]);

  const requireAuth = useCallback((): Promise<boolean> => {
    if (currentUser) return Promise.resolve(true);
    return new Promise(resolve => {
      setAuthResolve(() => resolve);
      setShowAuth(true);
    });
  }, [currentUser]);

  function handleAuthDone() {
    setShowAuth(false);
    if (authResolve) {
      authResolve(true);
      setAuthResolve(null);
    }
  }

  if (!authChecked) return <div className="loading-screen"><span>加载中...</span></div>;

  return (
    <AuthContext.Provider value={{ requireAuth }}>
      <HashRouter>
        <Layout>
          <Routes>
            <Route path="/" element={<Navigate to="/workbench" />} />
            <Route path="/workbench" element={<WorkbenchPage />} />
            <Route path="/write" element={<WritePage />} />
            <Route path="/tools" element={<ToolsPage />} />
            <Route path="/mine" element={<MinePage />} />
          </Routes>
        </Layout>
        {showAuth && <AuthModal onDone={handleAuthDone} />}
      </HashRouter>
    </AuthContext.Provider>
  );
}
