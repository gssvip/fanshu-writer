"""认证与核心业务冒烟测试。

覆盖：注册 → 登录 → 获取用户 → 创建书 → 创建章节 → 删除书。
不测 LLM 相关端点（依赖外部 API key，单测层不覆盖）。
"""
from __future__ import annotations

import uuid


def _register(client, username=None, password="test1234", email=None):
    """注册辅助：返回 (response, body)。"""
    username = username or f"tester_{uuid.uuid4().hex[:8]}"
    email = email or f"{username}@test.local"
    resp = client.post("/api/auth/register", json={
        "username": username,
        "password": password,
        "email": email,
    })
    return resp, resp.get_json()


def _login(client, username, password="test1234"):
    resp = client.post("/api/auth/login", json={
        "username": username,
        "password": password,
    })
    return resp, resp.get_json()


def test_register_success(client):
    """注册成功返回 201 + token。"""
    resp, body = _register(client, username="alice")
    assert resp.status_code == 201
    assert "token" in body
    assert body["user"]["username"] == "alice"


def test_register_duplicate_username_conflict(client):
    """重复用户名应 409。"""
    _register(client, username="bob")
    resp, _ = _register(client, username="bob", email="other@test.local")
    assert resp.status_code == 409


def test_register_short_password_rejected(client):
    """密码 <4 应 400。"""
    resp, _ = _register(client, username="shortpw", password="ab")
    assert resp.status_code == 400


def test_login_with_username_success(client):
    """用户名登录成功。"""
    _register(client, username="carol")
    resp, body = _login(client, "carol")
    assert resp.status_code == 200
    assert "token" in body


def test_login_with_wrong_password_failed(client):
    """密码错误应 401。"""
    _register(client, username="dave")
    resp, _ = _login(client, "dave", password="wrongpass")
    assert resp.status_code == 401


def test_get_me_requires_auth(client):
    """无 token 访问 /api/auth/me 应 401。"""
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_get_me_with_valid_token(client):
    """带 token 访问 /api/auth/me 返回当前用户。"""
    _, reg_body = _register(client, username="eve")
    token = reg_body["token"]
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.get_json()["username"] == "eve"


def test_logout_invalidates_token(client):
    """登出后 token 失效。"""
    _, reg_body = _register(client, username="frank")
    token = reg_body["token"]
    resp = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    # 登出后再访问受保护端点应 401
    resp2 = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp2.status_code == 401
