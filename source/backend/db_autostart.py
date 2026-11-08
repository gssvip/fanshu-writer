"""Aiven 数据库自动开机兜底（2026-11-08）。

背景：Aiven 免费版 PostgreSQL 无持续活动会自动关机（Powered off，域名停止解析，
业务请求报 "failed to resolve host"）。GitHub Actions 定时兜底（keep-alive.yml）
受 schedule 限流响应慢（实测 4~8 小时才跑一次），故在应用内再加一层：凡数据库
连接失败的路径（健康检查 ?db=1、全局异常、启动守护），后台自动调 Aiven API 开机，
用户首个失败请求即触发，无需人工到控制台点 Power on。

已实测的 API（2026-11-08，注意字段是 powered 不是 power_on）：
  PUT https://api.aiven.io/v1/project/{project}/service/{service}
  Authorization: Bearer <AVN_API_TOKEN>
  {"powered": true}  → 200 {service:{state: RUNNING}}（幂等，运行中调用也返回 200）

环境变量：
  AVN_API_TOKEN  Aiven 认证 token（必需；不配置则本模块静默禁用）
  AVN_PROJECT    Aiven 项目名（默认 fanshu）
  AVN_SERVICE    Aiven 服务名（默认 pg-fanshu）

安全约束：
  - fire-and-forget 后台线程，绝不阻塞业务请求；
  - 冷却 5 分钟（进程内去重），防高频失败打爆 Aiven API；
  - 只有开机动作、永无关机动作。
"""
from __future__ import annotations

import os
import threading
import time

_API_BASE = 'https://api.aiven.io/v1'
_COOLDOWN_SEC = 300.0

_lock = threading.Lock()
_last_fire = 0.0
_warned_no_token = False


def maybe_power_on_async() -> None:
    """数据库连接失败时调用：后台请求 Aiven 开机（冷却期内重复调用直接忽略）。"""
    global _last_fire, _warned_no_token
    token = os.environ.get('AVN_API_TOKEN', '').strip()
    if not token:
        if not _warned_no_token:
            _warned_no_token = True
            print('[AUTOSTART] AVN_API_TOKEN 未配置，自动开机兜底禁用'
                  '（需到 Aiven 控制台手动 Power on）', flush=True)
        return
    with _lock:
        now = time.monotonic()
        if now - _last_fire < _COOLDOWN_SEC:
            return
        _last_fire = now
    print('[AUTOSTART] 数据库连接失败，后台请求 Aiven 开机中…', flush=True)
    threading.Thread(target=_power_on, args=(token,), daemon=True).start()


def _power_on(token: str) -> None:
    project = os.environ.get('AVN_PROJECT', 'fanshu')
    service = os.environ.get('AVN_SERVICE', 'pg-fanshu')
    url = f'{_API_BASE}/project/{project}/service/{service}'
    try:
        import requests
        resp = requests.put(
            url,
            headers={'Authorization': f'Bearer {token}',
                     'Content-Type': 'application/json'},
            json={'powered': True},
            timeout=30,
        )
        if resp.status_code == 200:
            state = (resp.json().get('service') or {}).get('state', '?')
            print(f'[AUTOSTART] ✅ 开机指令已接受，服务状态: {state}', flush=True)
        else:
            print(f'[AUTOSTART] ⚠️ 开机请求返回 {resp.status_code}:'
                  f' {resp.text[:200]}', flush=True)
    except Exception as e:
        print(f'[AUTOSTART] ⚠️ 开机请求失败: {type(e).__name__}: {e}', flush=True)
