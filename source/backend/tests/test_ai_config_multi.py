"""AI 多配置切换端到端测试。

验证：
  - 旧数据自动迁移为激活配置（兼容性）
  - 新增/切换/删除配置
  - 最多 10 个限制
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
    assert body["max"] == 10
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


def test_max_ten_configs_limit(client):
    """最多 10 个提供商配置，第 11 个返回 400。"""
    # provider 级：一行一个提供商；用 10 个不同 provider 建满
    for i in range(1, 11):
        resp = client.post("/api/ai/configs", json={"name": f"provider{i}", "provider": f"p{i}"})
        assert resp.status_code == 201, f"p{i} 应创建成功"
    # 第 11 个应拒绝
    resp = client.post("/api/ai/configs", json={"name": "provider11", "provider": "p11"})
    assert resp.status_code == 400
    assert "最多" in resp.get_json()["error"]
    # 确认 /api/ai/configs 返回的 max = 10
    body = client.get("/api/ai/configs").get_json()
    assert body["max"] == 10
    assert len(body["configs"]) == 10


def test_activate_switches_active(client):
    """PUT /configs/<id>/activate 切换激活配置。"""
    client.get("/api/ai/config")
    r2 = client.post("/api/ai/configs", json={"name": "第二配置", "provider": "kimi"}).get_json()
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
    r2 = client.post("/api/ai/configs", json={"name": "第二配置", "provider": "kimi"}).get_json()
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


def test_create_same_provider_never_overwrites(client):
    """同 provider 两次 POST → 两条独立配置，互不覆盖（核心回归：添加即覆盖 bug）。"""
    client.get("/api/ai/config")
    r1 = client.post("/api/ai/configs", json={
        "name": "智谱-主号", "provider": "zhipu",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-main", "api_key": "sk-main",
    }).get_json()
    r2 = client.post("/api/ai/configs", json={
        "name": "智谱-备用", "provider": "zhipu",
        "base_url": "https://proxy.example.com/v1",
        "model": "glm-4-air", "api_key": "sk-backup",
    }).get_json()
    assert r1["id"] != r2["id"], "同 provider 第二次添加必须新建独立配置，而非覆盖"
    # 两条都存在，各自的地址/模型/密钥状态互不影响
    configs = {c["id"]: c for c in client.get("/api/ai/configs").get_json()["configs"]}
    assert len([c for c in configs.values() if c["provider"] == "zhipu"]) == 2
    assert configs[r1["id"]]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert configs[r2["id"]]["base_url"] == "https://proxy.example.com/v1"
    # 后建的那条自动激活
    assert configs[r2["id"]]["is_active"] is True
    assert configs[r1["id"]]["is_active"] is False


def test_update_config_by_id_isolated(client):
    """PUT /configs/<id> 只改指定配置：不改激活状态、不串 key、不影响其他行。"""
    client.get("/api/ai/config")
    client.post("/api/ai/configs", json={
        "name": "主用", "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat", "api_key": "sk-first",
    })
    r2 = client.post("/api/ai/configs", json={
        "name": "备用", "provider": "kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k", "api_key": "sk-second",
    }).get_json()
    # r2 是激活的；编辑 r2 但传掩码 key
    resp = client.put(f"/api/ai/configs/{r2['id']}", json={
        "api_key": "***", "model": "moonshot-v1-32k", "name": "备用改名",
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["model"] == "moonshot-v1-32k"
    assert body["name"] == "备用改名"
    assert body["is_active"] is True, "编辑不应改变激活状态"
    assert body["has_key"] is True, "掩码 key 必须保留原密钥"
    # 激活配置仍是 r2，其他配置未被牵连
    active = client.get("/api/ai/config").get_json()
    assert active["id"] == r2["id"]


def test_update_config_by_id_404(client):
    """PUT 不存在的配置返回 404。"""
    resp = client.put("/api/ai/configs/not-exist-id", json={"model": "x"})
    assert resp.status_code == 404
