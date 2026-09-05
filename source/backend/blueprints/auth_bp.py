"""认证蓝图 — 从 app.py 拆出（app.py 巨石外迁第 1 批：auth 域，约 325 行）。

路由清单（10 个）：
  /api/auth/register|login|me|logout
  /api/auth/vip/info|vip/upgrade-callback
  /api/auth/change-password|forgot-password|reset-password|verify-reset-token

依赖方向（无循环）：
  - 顶层仅依赖 flask + auth_utils（鉴权装饰器/常量）；
  - 模型（User/AuthToken/PasswordResetToken/db）在路由函数体内延迟导入，
    请求期 app 早已加载完毕（与 general_chat.py 同款模式）。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request, current_app
from werkzeug.security import generate_password_hash, check_password_hash

from auth_utils import login_required, VIP_LIFETIME_PRICE

auth_bp = Blueprint('auth', __name__)


# ==== 保留用户名（会员短号） ====

# 保留用户名白名单（即使命中保留规则也允许注册）
_RESERVED_WHITELIST = frozenset({'666', '888'})


def _is_reserved_username(username: str) -> bool:
    """判断用户名是否属于"保留给未来会员"的短号（新版 · 仅保留豹子号/顺子号）。

    规则（命中任意一条 → 保留；白名单优先放行）：
      1) 白名单 {'666','888'} → 即使命中也"不"保留（放行）
      2) 1~5 位纯数字：
         · 豹子号（所有数字相同，如 1、22、333、99999）→ 保留
         · 顺子号（长度≥2，相邻数字逐位 +1 或逐位 -1，如 12、321、56789、98765、43210）→ 保留
         · 其他 1~5 位纯数字（如 12、13、100、121、1024…）→ 放行
      3) 1~5 位纯字母（大小写敏感）：
         · 豹子号（所有字母完全相同，如 a、AA、bbb、CCCCC、zzzzz）→ 保留
         · 其他 1~5 位纯字母（如 ab、Abc、hello、WORLD…）→ 放行
      4) 长度不在 1~5、或含非纯数字/纯字母组合 → 不进入保留逻辑，放行
    """
    if not isinstance(username, str):
        return False
    u = username.strip()
    if not u:
        return False
    # 白名单：即使命中保留规则也放行
    if u in _RESERVED_WHITELIST:
        return False
    if len(u) < 1 or len(u) > 5:
        return False

    # --- 纯数字：豹子号 / 顺子号 ---
    if u.isdigit():
        # 豹子号：所有数字相同
        all_same = all(c == u[0] for c in u)
        if all_same:
            return True
        # 顺子号：长度≥2，相邻数字差值恒为 +1 或 恒为 -1
        if len(u) >= 2:
            nums = [int(c) for c in u]
            inc = all(nums[i + 1] - nums[i] == 1 for i in range(len(nums) - 1))
            dec = all(nums[i + 1] - nums[i] == -1 for i in range(len(nums) - 1))
            if inc or dec:
                return True
        return False

    # --- 纯字母：仅豹子号 ---
    if u.isalpha():
        # 豹子号：所有字母完全相同（大小写敏感，a≠A）
        all_same = all(c == u[0] for c in u)
        if all_same:
            return True
        return False

    return False


# ==== 邮件发送（用于找回密码） ====
# SMTP 配置通过环境变量覆盖；默认发件邮箱为 xiyiji@88.com
SMTP_HOST = os.environ.get('FANSHU_SMTP_HOST', 'smtp.qiye.aliyun.com')
SMTP_PORT = int(os.environ.get('FANSHU_SMTP_PORT', '465'))
SMTP_USER = os.environ.get('FANSHU_SMTP_USER', 'xiyiji@88.com')
SMTP_PASSWORD = os.environ.get('FANSHU_SMTP_PASSWORD', '')
SMTP_FROM_NAME = os.environ.get('FANSHU_SMTP_FROM_NAME', '蚂蚁写作')
SMTP_FROM_ADDR = os.environ.get('FANSHU_SMTP_FROM_ADDR', 'xiyiji@88.com')
# 前端站点地址，用于拼接重置链接
SITE_BASE_URL = os.environ.get('FANSHU_SITE_BASE_URL', '')


def send_reset_email(to_email, reset_token, site_url=None):
    """发送密码重置邮件。如果 SMTP 未配置密码则降级为返回链接（开发模式）。
    返回 (ok: bool, msg: str, reset_link: str)。
    """
    # 优先使用前端传入的 site_url（前后端分离部署时至关重要），其次环境变量，最后回退到后端地址
    base = (site_url or SITE_BASE_URL or request.host_url.rstrip('/')).rstrip('/')
    # 使用 # 锚点，兼容 HashRouter：/ sometime/#/reset-password?token=xxx
    reset_link = f"{base}/#/reset-password?token={reset_token}"

    subject = '【蚂蚁写作】找回您的账号密码'
    body = (
        f"您好，\n\n"
        f"我们收到了您重置蚂蚁写作账号密码的请求。\n\n"
        f"请点击下方链接重置密码（链接 30 分钟内有效）：\n"
        f"{reset_link}\n\n"
        f"如果您没有发起过此请求，请忽略本邮件，您的账号密码不会变更。\n\n"
        f"—— 蚂蚁写作团队"
    )

    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from email.utils import formataddr

        msg = MIMEMultipart()
        msg['From'] = formataddr((SMTP_FROM_NAME, SMTP_FROM_ADDR))
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        if SMTP_PASSWORD:
            # 生产/已配置：使用 SSL 直连 SMTP 服务器
            if SMTP_PORT == 465:
                server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15)
            else:
                server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
                server.starttls()
            try:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM_ADDR, [to_email], msg.as_string())
            finally:
                server.quit()
            return True, '邮件已发送', reset_link
        else:
            # 开发环境降级：SMTP 未配置，无法实际发邮件，返回链接供前端展示
            current_app.logger.warning('[SMTP未配置] 密码重置邮件未实际发送，返回链接供开发调试。')
            current_app.logger.info('---- 重置邮件内容 ----\n%s\n--------------------', body)
            return True, 'SMTP未配置，已生成重置链接', reset_link
    except Exception as e:
        current_app.logger.exception('发送重置邮件失败')
        return False, f'邮件发送失败：{e}', reset_link


# ==== Auth API 路由 ====

@auth_bp.route('/api/auth/register', methods=['POST'])
def register():
    from app import db, User, AuthToken
    from auth_utils import generate_token, hash_token
    data = request.json
    username = (data.get('username', '')).strip()
    password = (data.get('password', '')).strip()
    email = (data.get('email', '')).strip()
    # 保留号检测放在最前面：命中就直接 409"用户名已存在"，文案与真实冲突保持一致，
    # 不对外暴露"这是预留号"的机制。
    if _is_reserved_username(username):
        return jsonify({'error': '用户名已存在'}), 409
    if len(username) < 2 or len(username) > 30:
        return jsonify({'error': '用户名需2-30个字符'}), 400
    if len(password) < 4:
        return jsonify({'error': '密码至少4个字符'}), 400
    if not email:
        return jsonify({'error': '邮箱不能为空'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'error': '用户名已存在'}), 409
    if User.query.filter_by(email=email).first():
        return jsonify({'error': '该邮箱已被注册'}), 409
    user = User(username=username, password_hash=generate_password_hash(password), email=email)
    db.session.add(user)
    db.session.commit()
    token = generate_token()
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    db.session.add(AuthToken(user_id=user.id, token=hash_token(token), expires_at=expires))
    db.session.commit()
    return jsonify({'user': user.to_dict(), 'token': token}), 201


@auth_bp.route('/api/auth/login', methods=['POST'])
def login():
    from app import db, User, AuthToken
    from auth_utils import generate_token, hash_token
    data = request.json
    username = (data.get('username', '')).strip()
    password = (data.get('password', '')).strip()
    # 支持用户名或邮箱登录：先按邮箱查，查不到再按用户名查
    user = User.query.filter_by(email=username).first()
    if not user:
        user = User.query.filter_by(username=username).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({'error': '用户名或密码错误'}), 401
    token = generate_token()
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    db.session.add(AuthToken(user_id=user.id, token=hash_token(token), expires_at=expires))
    db.session.commit()
    return jsonify({'user': user.to_dict(), 'token': token})


@auth_bp.route('/api/auth/me', methods=['GET'])
@login_required
def get_me():
    from app import User
    user = User.query.get(request.current_user_id)
    return jsonify(user.to_dict() if user else {'error': 'User not found'})


@auth_bp.route('/api/auth/logout', methods=['POST'])
def logout():
    from app import db, AuthToken
    from auth_utils import hash_token
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if token:
        AuthToken.query.filter_by(token=hash_token(token)).delete()
        db.session.commit()
    return jsonify({'success': True})


@auth_bp.route('/api/auth/vip/info', methods=['GET'])
@login_required
def vip_info():
    """前端展示会员信息：当前身份、永久会员价等（未开通时创建第二本小说的提示也用此价格）。"""
    from app import User
    user = User.query.get(request.current_user_id)
    return jsonify({
        'is_vip': bool(getattr(user, 'is_vip', False)) if user else False,
        'vip_price': VIP_LIFETIME_PRICE,
        'vip_tier': 'lifetime',
        'message': '开通永久会员，享无限创建作品等高级权益',
    })


@auth_bp.route('/api/auth/vip/upgrade-callback', methods=['POST'])
@login_required
def vip_upgrade_callback():
    """
    支付成功回调（演示/占位接口）：
    真实部署时应该走支付网关回调验签；这里做一个最小开关，接口需传入
    { admin_key: <APP_ADMIN_KEY> } 或 { proof: <支付平台验证签名的 payload> }
    目前仅支持本地/管理员使用 APP_ADMIN_KEY 环境变量开通，防止用户自己调接口绕过。
    """
    from app import db, User
    data = request.json or {}
    admin_key = os.environ.get('APP_ADMIN_KEY', '')
    if admin_key and data.get('admin_key') == admin_key:
        ok = True
    else:
        # 未来支付平台回调时在此处做签名/订单校验；如果既没有 admin_key 也没有验签通过，拒绝。
        ok = False
    if not ok:
        return jsonify({'error': '回调校验失败'}), 401
    user = User.query.get(request.current_user_id)
    if user is None:
        return jsonify({'error': '用户不存在'}), 404
    user.is_vip = True
    db.session.commit()
    return jsonify({'success': True, 'user': user.to_dict()})


# ==== 修改密码 / 找回密码 / 重置密码 ====

@auth_bp.route('/api/auth/change-password', methods=['POST'])
@login_required
def change_password():
    """已登录用户修改密码：需要原密码验证。"""
    from app import db, User
    data = request.json or {}
    old_password = (data.get('old_password', '') or '').strip()
    new_password = (data.get('new_password', '') or '').strip()
    if not old_password or not new_password:
        return jsonify({'error': '请输入原密码和新密码'}), 400
    if len(new_password) < 4:
        return jsonify({'error': '新密码至少4个字符'}), 400

    user = User.query.get(request.current_user_id)
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    if not check_password_hash(user.password_hash, old_password):
        return jsonify({'error': '原密码错误'}), 401

    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    return jsonify({'success': True})


@auth_bp.route('/api/auth/forgot-password', methods=['POST'])
def forgot_password():
    """用户输入邮箱，生成重置令牌并发送重置邮件。"""
    from app import db, User, PasswordResetToken
    from auth_utils import generate_token
    data = request.json or {}
    email = (data.get('email', '') or '').strip().lower()
    site_url = (data.get('site_url', '') or '').strip()
    if not email or '@' not in email:
        return jsonify({'error': '请输入有效的邮箱'}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        # 出于隐私保护，即使邮箱不存在也返回成功，避免被探测账号是否存在
        return jsonify({'success': True, 'message': '如果该邮箱已注册，重置邮件已发送'})

    # 失效旧的重置令牌
    PasswordResetToken.query.filter_by(user_id=user.id, used=False).update({'used': True})

    token = generate_token()
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)
    db.session.add(PasswordResetToken(user_id=user.id, token=token, expires_at=expires, used=False))
    db.session.commit()

    ok, msg, reset_link = send_reset_email(user.email, token, site_url=site_url)
    if not ok:
        return jsonify({'error': msg}), 500

    resp = {'success': True, 'message': msg}
    # SMTP 未配置时，返回重置链接给前端展示（开发/自部署环境降级方案）
    if not SMTP_PASSWORD:
        resp['reset_link'] = reset_link
        resp['dev_mode'] = True
    return jsonify(resp)


@auth_bp.route('/api/auth/reset-password', methods=['POST'])
def reset_password():
    """用户凭重置令牌设置新密码。"""
    from app import db, User, PasswordResetToken
    data = request.json or {}
    token = (data.get('token', '') or '').strip()
    new_password = (data.get('new_password', '') or '').strip()
    if not token or not new_password:
        return jsonify({'error': '令牌或新密码不能为空'}), 400
    if len(new_password) < 4:
        return jsonify({'error': '新密码至少4个字符'}), 400

    prt = PasswordResetToken.query.filter_by(token=token).first()
    if not prt or prt.used:
        return jsonify({'error': '重置链接无效或已使用'}), 400
    exp = prt.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        return jsonify({'error': '重置链接已过期，请重新申请'}), 400

    user = User.query.get(prt.user_id)
    if not user:
        return jsonify({'error': '用户不存在'}), 404

    user.password_hash = generate_password_hash(new_password)
    prt.used = True
    db.session.commit()
    return jsonify({'success': True, 'message': '密码已重置，请使用新密码登录'})


@auth_bp.route('/api/auth/verify-reset-token', methods=['POST'])
def verify_reset_token():
    """校验重置令牌是否有效（用于前端跳转后预检）。"""
    from app import PasswordResetToken
    data = request.json or {}
    token = (data.get('token', '') or '').strip()
    if not token:
        return jsonify({'valid': False}), 400
    prt = PasswordResetToken.query.filter_by(token=token).first()
    if not prt or prt.used:
        return jsonify({'valid': False}), 200
    exp = prt.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        return jsonify({'valid': False}), 200
    return jsonify({'valid': True}), 200
