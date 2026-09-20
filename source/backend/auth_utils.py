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

# ===== 多用户信息隔离：全局鉴权 + 作品所有权守卫 =====
# 设计原则（参考 group_id 隔离经验）：
#   1. 隔离边界在服务端路由层强制，绝不依赖前端约定；
#   2. 作品所有权校验失败统一返回 404（不泄露作品是否存在）；
#   3. 未登录用户除白名单路径外一律 401，避免裸奔访问他人数据。
# 白名单：无需登录即可访问（公共数据 / 鉴权本身 / 健康检查 / 静态资源）。
_PUBLIC_PATH_PREFIXES = (
    '/api/auth/register',
    '/api/auth/login',
    '/api/auth/logout',
    '/api/auth/forgot-password',
    '/api/auth/reset-password',
    '/api/auth/verify-reset-token',
    '/api/auth/vip/upgrade-callback',
    '/api/health',
    '/api/rank/',
    '/api/rankings',
    '/api/covers/',
)
# 下载/导入路径允许通过 ?token= 鉴权（浏览器 <a>/window.open 无法带 header）。
_DOWNLOAD_PATH_INFIXES = (
    '/export', '/export-zip', '/export-full', '/analyze/export',
    '/cover', '/import-zip', '/import-files', '/import-chapters',
)


def _is_public_path(path: str) -> bool:
    if not path.startswith('/api/'):
        return True  # 静态资源 / SPA 路由不鉴权
    return any(path.startswith(p) for p in _PUBLIC_PATH_PREFIXES)


def _is_download_path(path: str) -> bool:
    return any(infix in path for infix in _DOWNLOAD_PATH_INFIXES)


def register_auth_guards(app):
    """注册全局 before_request：未登录拦截 + URL 路径参数 book_id 所有权校验。

    一次性覆盖所有缺少 @login_required 的路由（books/chapters/bible/characters/
    outlines/stages/dynamic-reports/ai-continue/ai-analyze/node-design 等），
    避免逐路由补装饰器的遗漏风险。
    """
    @app.before_request
    def _auth_and_ownership_guard():
        path = request.path or ''
        if _is_public_path(path):
            return None
        # 解析 token：优先 Authorization 头；下载/导入路径兜底 ?token=
        token = (request.headers.get('Authorization') or '').replace('Bearer ', '').strip()
        if not token and _is_download_path(path):
            token = (request.args.get('token') or '').strip()
        at, expired = _resolve_auth_token(token) if token else (None, False)
        if not at or expired:
            return _auth_error('登录已过期，请重新登录')
        uid = at.user_id
        request.current_user_id = uid

        # URL 路径参数里带 <book_id> 的路由：强制校验作品归属当前用户。
        # 校验失败返回 404，避免泄露"作品是否存在"。
        view_args = getattr(request, 'view_args', None) or {}
        book_id = view_args.get('book_id')
        if book_id:
            from app import Book  # 请求期导入，避免模块级循环依赖
            book = Book.query.get(book_id)
            if book is None or str(book.user_id) != str(uid):
                return jsonify({'error': 'Book not found'}), 404
            request.current_book = book
        return None


def get_owned_book(book_id):
    """返回属于当前登录用户的 Book 实例；不属于或不存在返回 None。

    用于请求体里带 book_id 的路由（chat/smart / apply-card / chat/general 等），
    它们的 book_id 不在 URL 路径里，无法被全局 before_request 拦截。"""
    if not book_id:
        return None
    from app import Book
    uid = getattr(request, 'current_user_id', None)
    if uid is None:
        return None
    book = Book.query.get(book_id)
    if book is None or str(book.user_id) != str(uid):
        return None
    return book


def owned_book_or_404(book_id):
    """请求体 book_id 的所有权校验：不通过返回 (jsonify, 404)，通过返回 Book。

    用法：book = owned_book_or_404(book_id); if isinstance(book, tuple): return book
    """
    book = get_owned_book(book_id)
    if book is None:
        return jsonify({'error': 'Book not found'}), 404
    return book


def get_user_book_ids(user_id):
    """返回当前用户所有作品 ID 集合，用于 AISession / AIUsageLog 等按作品过滤。"""
    if not user_id:
        return set()
    from app import Book, db
    rows = db.session.query(Book.id).filter(Book.user_id == user_id).all()
    return {r[0] for r in rows}


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
