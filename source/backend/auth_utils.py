"""鉴权工具集 — 从 app.py 拆出（app.py 巨石外迁第 1 批：auth 域）。

为什么独立成模块：login_required 系列装饰器被 app.py 58 处 + 多个蓝图共用，
抽到无 Flask-app 依赖的纯工具模块后，app.py 与 blueprints/ 均可安全单向导入。

依赖方向（无循环）：
  - 本模块顶层只依赖 flask/functools/hashlib/datetime；
  - AuthToken 模型定义在 app.py，故在请求期（函数体内）延迟导入 —— 装饰器注册
    时不触发导入，请求到来时 app 早已加载完毕（与 general_chat.py 同款模式）。
"""
from __future__ import annotations

import functools
import hashlib
import os
from datetime import datetime, timezone

from flask import jsonify, request

# 会员价格（全站统一，展示+接口回调共用）
VIP_LIFETIME_PRICE = 19.9


def generate_token():
    return hashlib.sha256(os.urandom(32)).hexdigest()


def hash_token(raw: str) -> str:
    """会话 token 哈希：数据库只存哈希不落明文（库泄露 ≠ 会话被劫持）。
    raw 本身是 256 位随机数的 hex，二次 sha256 不降低熵。"""
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _resolve_auth_token(token: str):
    """按哈希查库校验 token → (AuthToken|None, 过期bool)。"""
    from app import AuthToken  # 请求期导入，避免模块级循环依赖
    if not token:
        return None, False
    at = AuthToken.query.filter_by(token=hash_token(token)).first()
    if not at:
        return None, False
    exp = at.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return at, exp < datetime.now(timezone.utc)


def _auth_error(msg):
    return jsonify({'error': msg}), 401


def login_required(f):
    """标准鉴权：仅接受 Authorization: Bearer 头。
    URL ?token= 通道已收窄到 login_required_download（a 标签下载无法带 header）。"""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return _auth_error('请先登录')
        at, expired = _resolve_auth_token(token)
        if not at:
            return _auth_error('登录已过期，请重新登录')
        if expired:
            return _auth_error('登录已过期，请重新登录')
        request.current_user_id = at.user_id
        return f(*args, **kwargs)
    return decorated


def login_required_download(f):
    """下载专用鉴权：Authorization 头 或 ?token=（浏览器 <a href>/window.open 无法带 header）。
    仅限导出/下载路由使用，其余路由一律走 login_required。"""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            token = request.args.get('token', '')
        if not token:
            return _auth_error('请先登录')
        at, expired = _resolve_auth_token(token)
        if not at or expired:
            return _auth_error('登录已过期，请重新登录')
        request.current_user_id = at.user_id
        return f(*args, **kwargs)
    return decorated


def optional_login(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        at, expired = _resolve_auth_token(token)
        request.current_user_id = at.user_id if (at and not expired) else None
        return f(*args, **kwargs)
    return decorated
