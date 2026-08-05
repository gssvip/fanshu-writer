"""AI 多配置切换端到端测试。

验证：
  - 旧数据自动迁移为激活配置（兼容性）
  - 新增/切换/删除配置
  - 最多 3 个限制
  - get_active() 始终返回当前激活配置
"""
from __future__ import annotations


def test_get_config_creates_default_if_empty(client):
    """空库时 GET /api/ai/config 自动创建一条默认激活配置。"""
    resp = client.get("/api/ai/config")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["is_active"] is True
    assert body["name"]


def test_list_configs_returns_at_least_one(client):
    """GET /api/ai/configs 始终至少返回 1 条激活配置。"""
    # 先确保有一条
    client.get("/api/ai/config")
    resp = client.get("/api/ai/configs")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["max"] == 3
    assert len(body["configs"]) >= 1
    # 第一条应是激活的
    assert body["configs"][0]["is_active"] is True


def test_create_config_auto_activates(client):
    """POST 新增配置后自动激活，旧的取消激活。"""
    # 先建一条默认
    client.get("/api/ai/config")
    # 新增第二条
    resp = client.post("/api/ai/configs", json={
        "name": "测试配置2", "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o", "api_key": "sk-test-2",
    })
    assert resp.status_code == 201
    new_cfg = resp.get_json()
    assert new_cfg["is_active"] is True
    assert new_cfg["name"] == "测试配置2"

    # 列表里只有 1 条激活
    resp = client.get("/api/ai/configs")
    active = [c for c in resp.get_json()["configs"] if c["is_active"]]
    assert len(active) == 1
    assert active[0]["id"] == new_cfg["id"]


def test_max_three_configs_limit(client):
    """最多 3 个配置，第 4 个返回 400。"""
    client.get("/api/ai/config")  # 1
    client.post("/api/ai/configs", json={"name": "c2"})  # 2
    client.post("/api/ai/configs", json={"name": "c3"})  # 3
    resp = client.post("/api/ai/configs", json={"name": "c4"})  # 4 应拒绝
    assert resp.status_code == 400
    assert "最多" in resp.get_json()["error"]


def test_activate_switches_active(client):
    """PUT /configs/<id>/activate 切换激活配置。"""
    client.get("/api/ai/config")
    r2 = client.post("/api/ai/configs", json={"name": "第二配置"}).get_json()
    # 当前激活的是 r2（新增自动激活），切回第一条
    configs = client.get("/api/ai/configs").get_json()["configs"]
    first_id = [c for c in configs if c["id"] != r2["id"]][0]["id"]
    resp = client.put(f"/api/ai/configs/{first_id}/activate")
    assert resp.status_code == 200
    assert resp.get_json()["is_active"] is True

    # get_active 应返回切回去的那条
    active = client.get("/api/ai/config").get_json()
    assert active["id"] == first_id


def test_delete_active_config_promotes_next(client):
    """删除激活配置时自动激活剩下首条。"""
    client.get("/api/ai/config")
    r2 = client.post("/api/ai/configs", json={"name": "第二配置"}).get_json()
    # r2 是激活的，删除它
    resp = client.delete(f"/api/ai/configs/{r2['id']}")
    assert resp.status_code == 200
    # 删除后仍有一条激活
    active = client.get("/api/ai/config").get_json()
    assert active["is_active"] is True
    assert active["id"] != r2["id"]


def test_update_config_keeps_masked_key(client):
    """PUT 更新时 api_key 为 '***' 不覆盖真实密钥。"""
    client.get("/api/ai/config")
    client.put("/api/ai/config", json={"api_key": "sk-real-secret"})
    # 用掩码更新其他字段，密钥应保留
    client.put("/api/ai/config", json={"api_key": "***", "model": "new-model"})
    body = client.get("/api/ai/config").get_json()
    assert body["model"] == "new-model"
    assert body["has_key"] is True
