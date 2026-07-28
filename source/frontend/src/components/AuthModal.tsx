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

  // 找回密码模式
  const [forgotMode, setForgotMode] = useState(false);
  const [forgotEmail, setForgotEmail] = useState('');
  const [forgotSent, setForgotSent] = useState(false);
  const [resetLink, setResetLink] = useState('');
  const [devMode, setDevMode] = useState(false);

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

  async function handleForgotPassword() {
    if (!forgotEmail.trim()) { setError('请输入注册时使用的邮箱'); return; }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(forgotEmail.trim())) { setError('邮箱格式不正确'); return; }
    setLoading(true);
    setError('');
    try {
      const res = await api.forgotPassword(forgotEmail.trim());
      setForgotSent(true);
      setResetLink(res.reset_link || '');
      setDevMode(!!res.dev_mode);
    } catch (e: any) {
      setError(e.message || '发送失败，请稍后重试');
    }
    setLoading(false);
  }

  // 找回密码视图
  if (forgotMode) {
    return (
      <div className="modal-overlay" onClick={onDone}>
        <div className="auth-modal" onClick={e => e.stopPropagation()}>
          <div className="auth-modal-brand">
            <span className="auth-modal-logo"><img src="shouye.png" alt="logo" style={{width:40,height:40,borderRadius:10,objectFit:'contain'}} /></span>
            <h2>找回密码</h2>
            <p>输入注册邮箱，我们将发送重置链接</p>
          </div>

          {forgotSent ? (
            <div style={{ marginBottom: 10 }}>
              {devMode && resetLink ? (
                <div style={{ padding: '12px', background: '#fff8e1', color: '#8a6d3b', borderRadius: 8, fontSize: 13, marginBottom: 10, lineHeight: 1.6, wordBreak: 'break-all' }}>
                  ⚠️ 服务器未配置 SMTP 邮件服务，无法发送邮件。<br />
                  请直接点击下方链接重置密码（30 分钟内有效）：
                  <div style={{ marginTop: 8 }}>
                    <a href={resetLink} style={{ color: 'var(--accent)', wordBreak: 'break-all' }}>{resetLink}</a>
                  </div>
                </div>
              ) : (
                <div style={{ padding: '12px', background: '#e8f7e8', color: '#27ae60', borderRadius: 8, fontSize: 13, marginBottom: 10, lineHeight: 1.6 }}>
                  ✅ 重置邮件已发送至 <b>{forgotEmail}</b>，请在 30 分钟内查收并点击邮件中的链接重置密码。
                  <br />（如果没有收到，请检查垃圾邮件箱）
                </div>
              )}
              <button
                className="btn-primary"
                style={{width:'100%',padding:12}}
                onClick={() => { if (resetLink) window.location.href = resetLink; }}
                disabled={!resetLink}
              >
                {resetLink ? '前往重置密码 →' : '已发送邮件'}
              </button>
            </div>
          ) : (
            <>
              <div className="input" style={{marginBottom:10}}>
                <input className="input" type="email" placeholder="注册邮箱" value={forgotEmail}
                  onChange={e => setForgotEmail(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') handleForgotPassword(); }} />
              </div>
              {error && <div className="error-msg" style={{marginBottom:10}}>{error}</div>}
              <button className="btn-primary" style={{width:'100%',padding:12}} onClick={handleForgotPassword} disabled={loading}>
                {loading ? '发送中...' : '发送重置邮件'}
              </button>
            </>
          )}

          <div className="auth-toggle" style={{marginTop:16}}>
            想起密码了？
            <button onClick={() => { setForgotMode(false); setForgotSent(false); setError(''); setResetLink(''); setDevMode(false); }}>
              返回登录
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="modal-overlay" onClick={onDone}>
      <div className="auth-modal" onClick={e => e.stopPropagation()}>
        <div className="auth-modal-brand">
          <span className="auth-modal-logo"><img src="shouye.png" alt="logo" style={{width:40,height:40,borderRadius:10,objectFit:'contain'}} /></span>
          <h2>蚂蚁写作</h2>
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

        {!isRegister && (
          <div className="auth-toggle" style={{marginTop:8}}>
            <button onClick={() => { setForgotMode(true); setError(''); setForgotSent(false); }} style={{color:'var(--text-muted)'}}>
              忘记密码？
            </button>
          </div>
        )}

        <div className="auth-modal-hint">
          {isRegister ? '注册后可用用户名或邮箱登录' : '支持用户名或邮箱登录'}
        </div>
      </div>
    </div>
  );
}
