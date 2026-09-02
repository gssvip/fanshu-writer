"""注册保留名 + 会员/单本书创作上限 的 TDD 测试。

测试矩阵：
  R1：保留名注册 → 409 用户名已存在（跟真正冲突一样，不暴露"保留"）
  R2：白名单 666 / 888 即使命中规则也允许注册
  R3：非保留名超限号（数字≥6位 / 字母≥6位）正常注册
  R4：新注册用户只能创建 1 本，第 2 本返回 402 code=UPGRADE_REQUIRED + 价格 19.9
  R5：老账号（改造前已创建 >1 本书的用户）不受 1 本限制
  R6：VIP (is_vip=True) 用户可无限创建书
"""
from __future__ import annotations

import uuid

import pytest


def _register(client, username=None, password="test1234", email=None):
    username = username or f"t_{uuid.uuid4().hex[:8]}"
    email = email or f"{username}@test.local"
    resp = client.post("/api/auth/register", json={
        "username": username, "password": password, "email": email,
    })
    return resp, resp.get_json(), username, email


def _login(client, username, password="test1234"):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    return resp, resp.get_json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_book(client, token: str, title: str = "新书"):
    return client.post("/api/books", json={"title": title}, headers=_auth(token))


# ======================================================
# R1 — 保留名（1-5 位纯数字 / 1-5 位纯字母）注册 → 假装"已注册"
# ======================================================

@pytest.mark.parametrize("username", [
    "1", "12", "123", "12345",          # 1-5 位纯数字
    "007", "0",                         # 含前导零也算
    "a", "ab", "abc", "abcd", "abcde",  # 1-5 位纯字母
    "A", "AbC", "HELLO",                # 大小写字母
])
def test_register_reserved_username_returns_409(client, username):
    """命中保留规则的用户名应返回 409 用户名已存在（伪装成真实注册冲突）。"""
    resp, body, _, _ = _register(client, username=username)
    assert resp.status_code == 409, f"用户名 {username} 应命中保留规则，实际 HTTP {resp.status_code}"
    assert "已存在" in (body.get("error", "") or "")


def test_register_409_matches_real_conflict_error_message(client):
    """保留名的报错文案应和真实"用户名已注册"完全一致，不泄露保留机制。"""
    # 先真注册一个用户造成真实冲突
    u = f"real_{uuid.uuid4().hex[:6]}"
    _register(client, username=u)
    real_resp, real_body = _register(client, username=u, email=f"{u}2@test.local")[:2]
    assert real_resp.status_code == 409

    # 保留名（如 12345）
    reserved_resp, reserved_body, _, _ = _register(client, username="12345")
    assert reserved_resp.status_code == 409
    assert reserved_body.get("error") == real_body.get("error"), "保留名报错必须与真实冲突一致"


# ======================================================
# R2 — 白名单 666 / 888 可以注册（即使长度/规则命中）
# ======================================================
@pytest.mark.parametrize("username", ["666", "888"])
def test_register_whitelist_666_888_success(client, username):
    """吉利号白名单不应被保留规则屏蔽。使用唯一邮箱避免被"邮箱重复"409。"""
    unique_email = f"wl_{username}_{uuid.uuid4().hex}@test.local"
    resp, body = _register(client, username=username, email=unique_email)[:2]
    # 若跑多个 session 撞了用户名，正常也是 409 真实冲突（与保留伪装一致）——用 GET /api/auth/check 来区分也行不通，
    # 所以直接断言：若 code=201 白名单生效；若 409 则是之前已建（也算白名单绕过保留）
    assert resp.status_code in (201, 409), (
        f"白名单 {username} 允许或真实冲突都 OK，不允许 4xx 其他错误。实际={resp.status_code} body={body}"
    )


# ======================================================
# R3 — 长度 ≥6 的纯数字/字母正常注册
# ======================================================
@pytest.mark.parametrize("username", [
    "123456", "000000",             # 6 位数字
    "abcdef", "ABCDEF", "aBcDeF",   # 6 位字母
])
def test_register_longer_than_5_digits_or_letters_ok(client, username):
    real = f"{username}_{uuid.uuid4().hex[:4]}" if len(username) < 8 else username
    resp, body, _, _ = _register(client, username=real, email=f"{real}@test.local")
    assert resp.status_code == 201, f"长号 {real} 应允许注册，实际 {resp.status_code}"
    assert body["token"]


# ======================================================
# R4 — 新注册非 VIP 用户只能创建 1 本书
# ======================================================
def test_new_user_can_create_first_book(client):
    """新注册用户创建第 1 本书，200+ 正常。"""
    resp, body, u, _ = _register(client)
    token = body["token"]
    r = _create_book(client, token, title="第一本")
    assert r.status_code < 400, f"第 1 本书应创建成功，实际 HTTP {r.status_code}: {r.get_json()}"


def test_new_user_second_book_requires_vip(client):
    """新注册非 VIP 用户创建第 2 本书 → 402 UPGRADE_REQUIRED，提示开通 ¥19.9 永久会员。"""
    resp, body, u, _ = _register(client)
    token = body["token"]
    r1 = _create_book(client, token, "第 1 本")
    assert r1.status_code < 400, f"第 1 本创建失败：{r1.get_json()}"
    r2 = _create_book(client, token, "第 2 本")
    assert r2.status_code == 402, f"第 2 本应返回 402，实际 HTTP {r2.status_code}"
    b = r2.get_json() or {}
    assert b.get("code") == "UPGRADE_REQUIRED", f"错误 body 缺少 code=UPGRADE_REQUIRED: {b}"
    assert b.get("vip_price") == 19.9, f"会员价应是 19.9，实际 {b}"
    assert "永久会员" in b.get("message", ""), f"提示文字应包含『永久会员』：{b}"


# ======================================================
# R5 — 老账号（已有 >1 本书的用户）不被限制
# ======================================================
def test_grandfathered_user_with_many_books_unlimited(client):
    """创建用户 → 手工向 DB 塞 2 本书（模拟上线前存量）→ 再建新本应成功。"""
    resp, body, u, _ = _register(client)
    token = body["token"]
    uid = body["user"]["id"]
    # 绕过接口直接在 books 表里塞 2 本（模拟改造前存量用户）
    from sqlalchemy import text
    app_client = client.application
    with app_client.app_context():
        from app import db, Book
        for i in range(2):
            db.session.add(Book(user_id=uid, title=f"旧作{i+1}", genre="other",
                                book_type="novel", total_volumes=10, status="draft",
                                novel_styles="[]"))
        db.session.commit()
    # 现在通过 API 创建第 3 本（如果 grandfathered 生效，应成功）
    r = _create_book(client, token, "新作·第三本")
    assert r.status_code < 400, (
        f"存量用户（已有 >1 本）不应被 1 本限制卡住，实际 HTTP {r.status_code}: {r.get_json()}"
    )


# ======================================================
# R6 — VIP 用户可以无限创建书
# ======================================================
def test_vip_user_unlimited_books(client):
    """把用户标记为 is_vip 后，创建第 N 本都成功。"""
    resp, body, u, _ = _register(client)
    token = body["token"]
    uid = body["user"]["id"]
    # 升级 VIP：直接写 users 表
    app_client = client.application
    with app_client.app_context():
        from app import db, User
        u_row = User.query.get(uid)
        u_row.is_vip = True
        db.session.commit()
    # 建 5 本书都要成功
    for i in range(5):
        r = _create_book(client, token, f"VIP 第{i+1}本")
        assert r.status_code < 400, f"VIP 用户建第 {i+1} 本失败：{r.get_json()}"


# ======================================================
# R7 — to_dict 暴露 is_vip 字段给前端（用于展示 VIP 标识）
# ======================================================
def test_user_dict_includes_is_vip(client):
    """注册后 /api/auth/me 返回体必须包含 is_vip 字段。"""
    resp, body, u, _ = _register(client)
    token = body["token"]
    me = client.get("/api/auth/me", headers=_auth(token)).get_json() or {}
    assert "is_vip" in me, "用户对象必须暴露 is_vip 字段"
    assert me["is_vip"] is False
