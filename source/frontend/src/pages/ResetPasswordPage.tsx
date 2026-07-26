import { useState, useEffect } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api } from '../api';

export default function ResetPasswordPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const token = searchParams.get('token') || '';

  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [verifying, setVerifying] = useState(true);
  const [tokenValid, setTokenValid] = useState<boolean | null>(null);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (!token) { setTokenValid(false); setVerifying(false); return; }
    api.verifyResetToken(token)
      .then(res => setTokenValid(res.valid))
      .catch(() => setTokenValid(false))
      .finally(() => setVerifying(false));
  }, [token]);

  async function handleReset() {
    if (!newPassword) { setError('请输入新密码'); return; }
    if (newPassword.length < 4) { setError('新密码至少4个字符'); return; }
    if (newPassword !== confirmPassword) { setError('两次输入的密码不一致'); return; }
    setLoading(true);
    setError('');
    try {
      await api.resetPassword(token, newPassword);
      setDone(true);
    } catch (e: any) {
      setError(e.message || '重置失败');
    }
    setLoading(false);
  }

  if (verifying) {
    return (
      <div className="page loading-screen"><span>正在校验重置链接...</span></div>
    );
  }

  return (
    <div className="page" style={{maxWidth:420,margin:'0 auto',padding:24}}>
      <div className="modal" style={{width:'100%',maxWidth:'100%'}}>
        <h2>🔐 重置密码</h2>

        {tokenValid === false && (
          <>
            <div className="error-msg" style={{marginBottom:12}}>
              该重置链接无效或已过期。请重新申请找回密码。
            </div>
            <div className="modal-actions">
              <button className="btn-primary" onClick={() => navigate('/')}>返回首页</button>
            </div>
          </>
        )}

        {tokenValid === true && !done && (
          <>
            <p className="text-muted" style={{marginBottom:12}}>请输入您的新密码</p>
            <div className="form-field">
              <input className="input" type="password" placeholder="新密码（至少4个字符）" value={newPassword}
                onChange={e => setNewPassword(e.target.value)} />
            </div>
            <div className="form-field">
              <input className="input" type="password" placeholder="再次输入新密码" value={confirmPassword}
                onChange={e => setConfirmPassword(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') handleReset(); }} />
            </div>
            {error && <div className="error-msg" style={{marginBottom:10}}>{error}</div>}
            <div className="modal-actions">
              <button className="btn-ghost-sm" onClick={() => navigate('/')}>取消</button>
              <button className="btn-primary" onClick={handleReset} disabled={loading}>
                {loading ? '提交中...' : '重置密码'}
              </button>
            </div>
          </>
        )}

        {done && (
          <>
            <div style={{ padding: '12px', background: '#e8f7e8', color: '#27ae60', borderRadius: 8, fontSize: 13, marginBottom: 12, lineHeight: 1.6 }}>
              ✅ 密码已成功重置！请使用新密码登录。
            </div>
            <div className="modal-actions">
              <button className="btn-primary" onClick={() => navigate('/')}>去登录</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
