"""书与章节 CRUD 冒烟测试。

覆盖核心数据链路：建书 → 列书 → 建章 → 列章 → 改章 → 删书。
不测 LLM 端点（依赖外部 API key）。
"""
from __future__ import annotations

import uuid


def _auth_client(client, username=None):
    """注册并返回带 token 的请求头。"""
    username = username or f"u_{uuid.uuid4().hex[:8]}"
    resp = client.post("/api/auth/register", json={
        "username": username,
        "password": "test1234",
        "email": f"{username}@test.local",
    })
    assert resp.status_code == 201, resp.get_json()
    token = resp.get_json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _create_book(client, headers, title="测试书"):
    resp = client.post("/api/books", json={
        "title": title,
        "author": "测试作者",
        "genre": "fantasy",
        "book_type": "novel",
        "synopsis": "冒烟测试用",
        "total_volumes": 1,
    }, headers=headers)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def test_list_books_empty(client):
    """无书时列表为空数组。"""
    headers = _auth_client(client)
    resp = client.get("/api/books", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_create_and_get_book(client):
    """建书后能查到。"""
    headers = _auth_client(client)
    book = _create_book(client, headers, title="我的书")
    assert book["title"] == "我的书"
    assert book["genre"] == "fantasy"

    resp = client.get(f"/api/books/{book['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["title"] == "我的书"


def test_create_chapter_and_list(client):
    """建章后能在章节列表查到，字数正确统计。"""
    headers = _auth_client(client)
    book = _create_book(client, headers)

    resp = client.post(f"/api/books/{book['id']}/chapters", json={
        "title": "第1章 开端",
        "content": "这是一段测试正文，用于验证字数统计。" * 10,
    }, headers=headers)
    assert resp.status_code == 201, resp.get_json()
    ch = resp.get_json()
    assert ch["title"] == "第1章 开端"
    assert ch["word_count"] > 0

    # 列章
    resp2 = client.get(f"/api/books/{book['id']}/chapters", headers=headers)
    assert resp2.status_code == 200
    chapters = resp2.get_json()
    assert any(c["id"] == ch["id"] for c in chapters)


def test_update_chapter_creates_version(client):
    """改章节正文应生成历史版本。"""
    headers = _auth_client(client)
    book = _create_book(client, headers)

    create_resp = client.post(f"/api/books/{book['id']}/chapters", json={
        "title": "第1章",
        "content": "原始正文",
    }, headers=headers)
    ch_id = create_resp.get_json()["id"]

    # 改正文
    resp = client.put(f"/api/books/{book['id']}/chapters/{ch_id}", json={
        "content": "修改后的正文",
    }, headers=headers)
    assert resp.status_code == 200

    # 应有历史版本
    ver_resp = client.get(f"/api/books/{book['id']}/chapters/{ch_id}/versions", headers=headers)
    assert ver_resp.status_code == 200
    versions = ver_resp.get_json()
    assert len(versions) >= 1


def test_delete_book_cascades(client):
    """删书后查不到。"""
    headers = _auth_client(client)
    book = _create_book(client, headers)

    resp = client.delete(f"/api/books/{book['id']}", headers=headers)
    assert resp.status_code == 200

    resp2 = client.get(f"/api/books/{book['id']}", headers=headers)
    assert resp2.status_code == 404


def test_book_stats_after_chapter(client):
    """建章后书的统计应包含该章记录。"""
    headers = _auth_client(client)
    book = _create_book(client, headers)

    client.post(f"/api/books/{book['id']}/chapters", json={
        "title": "第1章",
        "content": "正文内容" * 50,
    }, headers=headers)

    resp = client.get(f"/api/books/{book['id']}/stats", headers=headers)
    assert resp.status_code == 200
    stats = resp.get_json()
    # /stats 返回 {chapters: [{title, word_count, date}], daily: [...]}
    chapters = stats.get("chapters", [])
    assert any(c.get("title") == "第1章" for c in chapters)
