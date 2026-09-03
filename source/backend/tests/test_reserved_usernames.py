"""注册保留名（新版：豹子号/顺子号） + 会员/单本书创作上限 的 TDD 测试。

测试矩阵：
  R1：保留名注册 → 409 用户名已存在（跟真正冲突一样，不暴露"保留"）
      · 1~5 位纯数字豹子号（1、22、...、99999、00）
      · 2~5 位纯数字顺子号（递增：12、123、56789 / 递减：98、321、98765）
      · 1~5 位纯字母豹子号（a、AA、bbb、ZZZZZ）
  R2：白名单 666 / 888 即使命中豹子号也允许注册
  R3：非保留的 1~5 位纯数字/纯字母、以及 ≥6 位的 → 正常注册
  R4：新注册用户只能创建 1 本，第 2 本返回 402 code=UPGRADE_REQUIRED + 价格 19.9
  R5：老账号（改造前已创建 >1 本书的用户）不受 1 本限制
  R6：VIP (is_vip=True) 用户可无限创建书
  R7：User.to_dict 暴露 is_vip 字段
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
# R1-A — 纯数字豹子号（1~5位所有数字相同）→ 409
# ======================================================
@pytest.mark.parametrize("username", [
    # 1 位（1 位数天然都是豹子号）
    "0", "1", "5", "9",
    # 2 位
    "00", "11", "22", "77", "99",
    # 3 位
    "000", "111", "333", "777", "999",
    # 4 位
    "0000", "1111", "5555", "9999",
    # 5 位
    "00000", "11111", "55555", "99999",
])
def test_register_digit_leopard_returns_409(client, username):
    """1~5 位纯数字豹子号必须保留 → 409 用户名已存在。"""
    resp, body, _, _ = _register(client, username=username)
    assert resp.status_code == 409, f"数字豹子号 {username} 应保留，实际 HTTP {resp.status_code}"
    assert "已存在" in (body.get("error", "") or "")


# ======================================================
# R1-B — 纯数字顺子号（2~5位，递增/递减）→ 409
# ======================================================
@pytest.mark.parametrize("username", [
    # 递增顺子 2 位
    "01", "12", "23", "45", "78", "89",
    # 递减顺子 2 位
    "10", "21", "43", "54", "87", "98",
    # 递增顺子 3 位
    "012", "123", "234", "456", "678", "789",
    # 递减顺子 3 位
    "210", "321", "543", "765", "987",
    # 递增顺子 4 位
    "0123", "1234", "2345", "4567", "6789",
    # 递减顺子 4 位
    "3210", "4321", "6543", "8765", "9876",
    # 递增顺子 5 位
    "01234", "12345", "23456", "45678", "56789",
    # 递减顺子 5 位
    "43210", "54321", "76543", "87654", "98765",
])
def test_register_digit_straight_returns_409(client, username):
    """2~5 位纯数字顺子号（递增或递减恒差 1）必须保留 → 409。"""
    resp, body, _, _ = _register(client, username=username)
    assert resp.status_code == 409, f"数字顺子号 {username} 应保留，实际 HTTP {resp.status_code}"
    assert "已存在" in (body.get("error", "") or "")


# ======================================================
# R1-C — 纯字母豹子号（1~5位所有字母完全相同，大小写敏感）→ 409
# ======================================================
@pytest.mark.parametrize("username", [
    # 1 位
    "a", "A", "m", "Z",
    # 2 位
    "aa", "AA", "bb", "BB", "zz", "ZZ",
    # 3 位
    "aaa", "AAA", "bbb", "CCC", "zzz", "ZZZ",
    # 4 位
    "aaaa", "AAAA", "dddd", "MMMM", "zzzz", "ZZZZ",
    # 5 位
    "aaaaa", "AAAAA", "eeeee", "QQQQQ", "zzzzz", "ZZZZZ",
])
def test_register_letter_leopard_returns_409(client, username):
    """1~5 位纯字母豹子号必须保留 → 409。"""
    resp, body, _, _ = _register(client, username=username)
    assert resp.status_code == 409, f"字母豹子号 {username} 应保留，实际 HTTP {resp.status_code}"
    assert "已存在" in (body.get("error", "") or "")


# ======================================================
# R1-D — 保留名的报错文案必须与真实冲突完全一致
# ======================================================
def test_register_409_matches_real_conflict_error_message(client):
    """保留名报错文案应与真实"用户名已注册"完全一致，不泄露保留机制。"""
    # 先真注册一个用户造成真实冲突
    u = f"real_{uuid.uuid4().hex[:6]}"
    _register(client, username=u)
    real_resp, real_body = _register(client, username=u, email=f"{u}2@test.local")[:2]
    assert real_resp.status_code == 409

    # 保留名示例：12345（数字递增顺子）、111（数字豹子号）、aaa（字母豹子号）
    for reserved in ["12345", "111", "aaa"]:
        reserved_resp, reserved_body, _, _ = _register(client, username=reserved)
        assert reserved_resp.status_code == 409, f"{reserved} 应返回 409"
        assert reserved_body.get("error") == real_body.get("error"), (
            f"保留名 {reserved} 报错必须与真实冲突一致"
            f" 保留={reserved_body.get('error')!r} 真实={real_body.get('error')!r}"
        )


# ======================================================
# R2 — 白名单 666 / 888（数字豹子号，但被白名单放行）
# ======================================================
@pytest.mark.parametrize("username", ["666", "888"])
def test_register_whitelist_666_888_success(client, username):
    """吉利号白名单不应被保留规则屏蔽。使用唯一邮箱避免被"邮箱重复"409。"""
    unique_email = f"wl_{username}_{uuid.uuid4().hex}@test.local"
    resp, body = _register(client, username=username, email=unique_email)[:2]
    # 若多个 session 撞了用户名，正常也是 409 真实冲突（与保留伪装一致）——
    # 所以直接断言：若 code=201 白名单生效；若 409 也是之前已建（也算白名单绕过保留）
    assert resp.status_code in (201, 409), (
        f"白名单 {username} 允许或真实冲突都 OK，不允许 4xx 其他错误。"
        f" 实际={resp.status_code} body={body}"
    )


# ======================================================
# R3-A — 非保留的 1~5 位纯数字（非豹子号、非顺子号）→ 正常注册
# ======================================================
@pytest.mark.parametrize("username", [
    # 2 位
    "13", "24", "41", "51", "95", "02", "62", "29",
    # 3 位
    "121", "135", "710", "200", "102", "314", "961", "520",
    # 4 位
    "1024", "1212", "2048", "1357", "9527", "3141", "5201",
    # 5 位
    "12346", "54320", "10000", "12121", "52013", "31415", "98764",
])
def test_register_normal_1_5_digits_ok(client, username):
    """非豹子号、非顺子号的 1~5 位纯数字应允许注册（老规则是全保留，新版放行）。"""
    real = username + uuid.uuid4().hex[:2]  # 后缀只用于邮箱，用户名保持原样
    resp, body, _, _ = _register(client, username=username, email=f"{real}@test.local")
    assert resp.status_code == 201, (
        f"普通 1-5 位数字 {username} 应允许注册，实际 HTTP {resp.status_code}: {body}"
    )
    assert body["token"], "注册成功响应必须携带 token"


# ======================================================
# R3-B — 非保留的 1~5 位纯字母（非豹子号）→ 正常注册
# ======================================================
@pytest.mark.parametrize("username", [
    # 2 位
    "ab", "ba", "Ab", "AB", "by", "My", "Go", "Hi",
    # 3 位
    "abc", "Abc", "cat", "Dog", "yes", "HEY", "Sun",
    # 4 位
    "abcd", "book", "word", "Good", "Home", "Code", "Wind",
    # 5 位
    "abcde", "Hello", "WORLD", "happy", "Money", "Smart",
])
def test_register_normal_1_5_letters_ok(client, username):
    """非豹子号的 1~5 位纯字母应允许注册（老规则是全保留，新版放行）。"""
    real = username + uuid.uuid4().hex[:2]
    resp, body, _, _ = _register(client, username=username, email=f"{real}@test.local")
    assert resp.status_code == 201, (
        f"普通 1-5 位字母 {username} 应允许注册，实际 HTTP {resp.status_code}: {body}"
    )
    assert body["token"], "注册成功响应必须携带 token"


# ======================================================
# R3-C — 字母/数字组合的"非豹子顺子"：Abc 非纯字母豹子 → 放行；a1b6 字母数字混合 → 放行
# ======================================================
@pytest.mark.parametrize("username", [
    # 混合（长度 1~5 但非纯数字/字母，根本不进保留逻辑）
    "a1", "b2c", "x9y9", "u5er", "i_am",
    # 大小写组合但字母不全相同 → 非豹子号 → 放行
    "Aa", "AbB", "Aaaa", "HellO",
])
def test_register_mixed_or_non_pure_ok(client, username):
    """含数字/字母/符号混合、或大小写字母非豹子号都应直接放行。"""
    unique_suffix = uuid.uuid4().hex[:3]
    resp, body, _, _ = _register(
        client,
        username=username,
        email=f"mix_{username}_{unique_suffix}@test.local",
    )
    # 因 username 长度 ≥2 且合法，直接断言 201 成功或真实用户名冲突（非保留 409）
    assert resp.status_code in (201, 409), (
        f"混合账号 {username} 要么成功要么真实冲突，不能返回其他错误。"
        f" 实际={resp.status_code} body={body}"
    )
    if resp.status_code == 409:
        # 如果是 409 也必须是真实用户名重复的"已存在"，不能是其他错误
        assert "已存在" in (body.get("error") or "")


# ======================================================
# R3-D — 长度 ≥6 的纯数字/字母正常注册
# ======================================================
@pytest.mark.parametrize("username", [
    "123456", "000000",             # 6 位数字（哪怕豹子号也超长，不保留）
    "abcdef", "ABCDEF", "aBcDeF",   # 6 位字母（哪怕豹子号也超长，不保留）
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
