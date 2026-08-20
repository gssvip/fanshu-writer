"""db_boot_guard 单元测试：部署冷启动 PG 连接守护。

覆盖：
  - 连接可用时立即放行（快速路径，零退避等待）
  - 连接先失败后恢复（Neon 冷唤醒场景）：重试后放行
  - 始终连不上：SystemExit 且日志包含可操作排查信息
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class TestWaitForDbReady:
    def test_fast_path_no_retry_when_db_ok(self, app, monkeypatch, capsys):
        """连接可用：第 1 次尝试即通过，无等待、无告警日志。"""
        from db_boot_guard import wait_for_db_ready
        import app as app_module

        calls = []

        def fake_execute(stmt):
            calls.append(str(stmt))
            return []

        # SELECT 1 直接成功
        monkeypatch.setattr(app_module.db.session, 'execute', fake_execute)
        monkeypatch.setattr(app_module.db.session, 'rollback', lambda: None)
        # 若出现 sleep 说明误入了重试路径
        monkeypatch.setattr('time.sleep', lambda s: pytest.fail('不应退避等待'))

        wait_for_db_ready(max_attempts=5, base_delay=2.0)

        assert len(calls) == 1
        out = capsys.readouterr().out
        assert '未就绪' not in out

    def test_retry_then_success_on_cold_wake(self, app, monkeypatch, capsys):
        """前 2 次连不上（Neon 冷唤醒），第 3 次成功：退避重试后放行。"""
        from db_boot_guard import wait_for_db_ready
        import app as app_module

        state = {'n': 0}
        sleeps = []

        def fake_execute(stmt):
            state['n'] += 1
            if state['n'] < 3:
                raise RuntimeError('connection timed out')
            return []

        monkeypatch.setattr(app_module.db.session, 'execute', fake_execute)
        monkeypatch.setattr(app_module.db.session, 'rollback', lambda: None)
        monkeypatch.setattr('time.sleep', sleeps.append)

        wait_for_db_ready(max_attempts=5, base_delay=2.0)

        assert state['n'] == 3
        # 指数退避：2s、4s
        assert sleeps == [2.0, 4.0]
        out = capsys.readouterr().out
        assert '冷唤醒完成（第 3 次尝试连上）' in out

    def test_gives_up_with_system_exit_after_all_attempts(self, app, monkeypatch, capsys):
        """始终连不上：SystemExit，且打印可操作排查日志（Render Events 可见）。"""
        from db_boot_guard import wait_for_db_ready
        import app as app_module

        def always_fail(stmt):
            raise RuntimeError('server closed the connection')

        monkeypatch.setattr(app_module.db.session, 'execute', always_fail)
        monkeypatch.setattr(app_module.db.session, 'rollback', lambda: None)
        monkeypatch.setattr('time.sleep', lambda s: None)

        with pytest.raises(SystemExit):
            wait_for_db_ready(max_attempts=3, base_delay=0.01)

        out = capsys.readouterr().out
        assert '[INIT][FATAL]' in out
        assert 'DATABASE_URL' in out
        assert 'server closed the connection' in out

    def test_init_db_wires_boot_guard(self, app, monkeypatch):
        """init_db 必须先过 boot guard 再跑版本门禁（防回归：部署冷启动崩进程）。"""
        import app as app_module

        order = []
        real_init = app_module.init_db

        # 记录调用顺序：guard → 版本门禁查询
        monkeypatch.setattr(
            'db_boot_guard.wait_for_db_ready',
            lambda *a, **kw: order.append('guard'),
        )

        orig_execute = app_module.db.session.execute

        def spy_execute(stmt, *a, **kw):
            order.append('gate_query')
            return orig_execute(stmt, *a, **kw)

        monkeypatch.setattr(app_module.db.session, 'execute', spy_execute)

        with app_module.app.app_context():
            real_init()

        assert order[0] == 'guard'
        assert 'gate_query' in order
