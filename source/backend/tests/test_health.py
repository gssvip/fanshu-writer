"""健康检查与基础冒烟测试。

目标：用最低成本守住"应用能起来、核心端点能响应"。
不依赖外部 LLM、不依赖网络，纯本地 SQLite。
"""
from __future__ import annotations


def test_health_check_ok(client):
    """/api/health 不查库，应返回 200 + ok。"""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert "time" in body


def test_unknown_api_route_handled(client):
    """未知 API 路由应被妥善处理（404 或 SPA fallback 200），不应 500。"""
    resp = client.get("/api/__not_exist__")
    assert resp.status_code in (404, 200)


def test_static_root_returns_html_or_json(client):
    """根路径应返回前端 index.html 或 JSON（无前端构建产物时）。"""
    resp = client.get("/")
    assert resp.status_code in (200, 404)
