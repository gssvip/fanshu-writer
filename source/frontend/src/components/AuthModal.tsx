import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, isApiMisconfigured } from '../api';
import { useStore } from '../store';

export default function AuthModal({ onDone }: { onDone: () => void }) {
  const { setCurrentUser } = useStore() as any;
  const navigate = useNavigate();
  const [isRegister, setIsRegister] = useState(false);
  const [form, setForm] = useState({ username: '', password: '', email: '' });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const misconfigured = isApiMisconfigured();

  async function handleSubmit() {
    if (!form.username || !form.password) { setError('请输入用户名和密码'); return; }
    if (isRegister) {
      if (!form.email.trim()) { setError('请输入邮箱'); return; }
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email.trim())) { setError('邮箱格式不正确'); return; }
    }
    setLoading(true);
    setError('');
    try {
      const res = isRegister
        ? await api.register(form.username, form.password, form.email)
        : await api.login(form.username, form.password);
      localStorage.setItem('fanshu-token', res.token);
      setCurrentUser?.(res.user);
      onDone();
    } catch (e: any) {
      setError(e.message || '操作失败');
    }
    setLoading(false);
  }

  return (
    <div className="modal-overlay" onClick={onDone}>
      <div className="auth-modal" onClick={e => e.stopPropagation()}>
        <div className="auth-modal-brand">
          <span className="auth-modal-logo"><img src="/logo.jpg" alt="logo" style={{width:40,height:40,borderRadius:10,objectFit:'cover'}} /></span>
          <h2>番薯写作</h2>
          <p>登录后使用全部功能</p>
        </div>

        {misconfigured && (
          <div style={{ padding: '10px 12px', background: '#fde8e8', color: '#e74c3c', borderRadius: 8, fontSize: 12, marginBottom: 10, lineHeight: 1.6 }}>
            ⚠️ 当前为静态托管环境，未配置后端服务器地址，登录注册无法使用。
            <button
              onClick={() => { onDone(); navigate('/mine'); }}
              style={{ display: 'block', marginTop: 6, background: 'none', border: '1px solid #e74c3c', color: '#e74c3c', padding: '4px 10px', borderRadius: 6, cursor: 'pointer', fontSize: 12 }}
            >
              前往「我的 → 服务器」配置
            </button>
          </div>
        )}

        <div className="input" style={{marginBottom:10}}>
          <input className="input" placeholder={isRegister ? '用户名' : '用户名 / 邮箱'} value={form.username}
            onChange={e => setForm(p => ({ ...p, username: e.target.value }))} />
        </div>
        <div className="input" style={{marginBottom:10}}>
          <input className="input" type="password" placeholder="密码" value={form.password}
            onChange={e => setForm(p => ({ ...p, password: e.target.value }))}
            onKeyDown={e => { if (e.key === 'Enter') handleSubmit(); }} />
        </div>

        {isRegister && (
          <div className="input" style={{marginBottom:10}}>
            <input className="input" type="email" placeholder="邮箱（必填，可用于登录）" value={form.email}
              onChange={e => setForm(p => ({ ...p, email: e.target.value }))} />
          </div>
        )}

        {error && <div className="error-msg" style={{marginBottom:10}}>{error}</div>}

        <button className="btn-primary" style={{width:'100%',padding:12}} onClick={handleSubmit} disabled={loading}>
          {loading ? '处理中...' : isRegister ? '注册' : '登录'}
        </button>

        <div className="auth-toggle" style={{marginTop:16}}>
          {isRegister ? '已有账号？' : '没有账号？'}
          <button onClick={() => { setIsRegister(!isRegister); setError(''); }}>
            {isRegister ? '去登录' : '去注册'}
          </button>
        </div>

        <div className="auth-modal-hint">
          {isRegister ? '注册后可用用户名或邮箱登录' : '支持用户名或邮箱登录'}
        </div>
      </div>
    </div>
  );
}
