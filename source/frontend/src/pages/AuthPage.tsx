import { useState } from 'react';
import { api } from '../api';
import { useStore } from '../store';

export default function AuthPage({ onAuth }: { onAuth: () => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [email, setEmail] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const setCurrentUser = useStore((s: any) => s.setCurrentUser);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const result = mode === 'login'
        ? await api.login(username, password)
        : await api.register(username, password, email);
      localStorage.setItem('fanshu-token', result.token);
      // 写入当前用户信息并触发书籍列表刷新（跨设备登录后立刻同步）
      if (result.user) {
        setCurrentUser(result.user);
      }
      onAuth();
    } catch (err: any) {
      setError(err.message);
    }
    setLoading(false);
  };

  return (
    <div style={{
      minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'var(--bg-primary)', padding: 20
    }}>
      <div style={{
        background: 'var(--bg-secondary)', borderRadius: 20, padding: '32px 28px',
        width: '100%', maxWidth: 400, boxShadow: 'var(--shadow-md)'
      }}>
        <div style={{ textAlign: 'center', marginBottom: 28 }}>
          <picture><img src="shouye.webp" alt="蚂蚁写作" style={{ width: 72, height: 72, borderRadius: 16, marginBottom: 8, objectFit: 'contain' }} /></picture>
          <h1 style={{ fontSize: 24, fontWeight: 700, marginBottom: 4 }}>蚂蚁写作</h1>
          <p style={{ fontSize: 13, color: 'var(--text-muted)' }}>AI 驱动的小说创作平台</p>
        </div>

        <div style={{ display: 'flex', marginBottom: 24, background: 'var(--bg-tertiary)', borderRadius: 10, padding: 3 }}>
          <button onClick={() => setMode('login')} style={{
            flex: 1, padding: '10px', borderRadius: 8, border: 'none', fontSize: 15, fontWeight: 600,
            background: mode === 'login' ? 'var(--bg-secondary)' : 'transparent',
            color: mode === 'login' ? 'var(--accent)' : 'var(--text-muted)',
            boxShadow: mode === 'login' ? 'var(--shadow-sm)' : 'none', cursor: 'pointer'
          }}>登录</button>
          <button onClick={() => setMode('register')} style={{
            flex: 1, padding: '10px', borderRadius: 8, border: 'none', fontSize: 15, fontWeight: 600,
            background: mode === 'register' ? 'var(--bg-secondary)' : 'transparent',
            color: mode === 'register' ? 'var(--accent)' : 'var(--text-muted)',
            boxShadow: mode === 'register' ? 'var(--shadow-sm)' : 'none', cursor: 'pointer'
          }}>注册</button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label>{mode === 'login' ? '用户名 / 邮箱' : '用户名'}</label>
            <input value={username} onChange={e => setUsername(e.target.value)}
              placeholder={mode === 'login' ? '输入用户名或邮箱' : '输入用户名'} required style={{ fontSize: 16, padding: '12px 16px' }} />
          </div>
          {mode === 'register' && (
            <div className="form-group">
              <label>邮箱</label>
              <input type="email" value={email} onChange={e => setEmail(e.target.value)}
                placeholder="your@email.com" required style={{ fontSize: 16, padding: '12px 16px' }} />
            </div>
          )}
          <div className="form-group">
            <label>密码</label>
            <input type="password" value={password} onChange={e => setPassword(e.target.value)}
              placeholder="输入密码" required style={{ fontSize: 16, padding: '12px 16px' }} />
          </div>

          {error && (
            <div style={{ color: 'var(--danger)', fontSize: 13, marginBottom: 12, padding: '8px 12px', background: '#fde8e8', borderRadius: 8 }}>
              {error}
            </div>
          )}

          <button type="submit" className="btn-primary" disabled={loading} style={{
            width: '100%', padding: '14px', fontSize: 16, fontWeight: 600, marginTop: 8
          }}>
            {loading ? '处理中...' : mode === 'login' ? '登录' : '注册'}
          </button>
        </form>

        <p style={{ textAlign: 'center', marginTop: 20, fontSize: 12, color: 'var(--text-muted)' }}>
          {mode === 'login' ? '还没有账号？' : '已有账号？'}
          <button onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError(''); }}
            style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', fontWeight: 600, padding: 0, marginLeft: 4 }}>
            {mode === 'login' ? '立即注册' : '去登录'}
          </button>
        </p>
      </div>
    </div>
  );
}
