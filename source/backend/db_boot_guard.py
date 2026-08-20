"""部署冷启动 DB 连接守护（从 app.py 拆出，架构门禁：app.py 行数禁止继续增长）。

背景（线上事故 2026-08-20）：Render 每次部署都重启进程跑 init_db，而 Neon 免费版
计算节点几分钟无流量就休眠。部署恰逢休眠时，首个查询连接超时/被拒——app.py 旧逻辑
把"连不上"误判成"新库"，落入全量初始化，create_all 对着不可用连接抛
OperationalError 进程退出 → Render 反复 "Deploy Error / Service Unavailable"，
新代码一直上不了线（旧实例持续跑旧代码）。

职责：init_db 跑版本门禁/迁移之前，先显式确认外部 PostgreSQL 连接可用：
  - 连接可用 → 立即放行（零额外开销，本地 SQLite 一次 SELECT 1 即通过）
  - 连不上 → 指数退避重试（2s/4s/8s/16s），给 Neon 冷唤醒留时间
  - 始终连不上 → 带明确可操作日志 SystemExit（数据铁律：绝不带坏连接静默启动；
    退出后 Render 会保留旧实例继续提供服务）
"""
from __future__ import annotations


def wait_for_db_ready(max_attempts: int = 5, base_delay: float = 2.0) -> None:
    """阻塞直到 db.session 能跑通 SELECT 1（连接确认可用）。

    仅在"连接建立失败"时重试；app_meta 表不存在等业务错误不会被本函数吞掉
    （SELECT 1 不涉及任何业务表）。
    """
    import time as _time
    from app import db

    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            db.session.execute(db.text('SELECT 1'))
            db.session.rollback()
            if attempt > 1:
                print(f'[INIT] ✅ PostgreSQL 冷唤醒完成（第 {attempt} 次尝试连上）', flush=True)
            return
        except Exception as e:
            last_err = e
            try:
                db.session.rollback()
            except Exception:
                pass
            delay = base_delay * (2 ** (attempt - 1))  # 2s/4s/8s/16s 退避
            if attempt < max_attempts:
                print(f'[INIT] ⏳ PostgreSQL 未就绪（第 {attempt}/{max_attempts} 次：'
                      f'{type(e).__name__}），{delay:.0f}s 后重试（Neon 冷唤醒属正常现象）', flush=True)
                _time.sleep(delay)
    print('=' * 70, flush=True)
    print('[INIT][FATAL] PostgreSQL 始终连不上，进程退出（Render 会保留旧实例继续服务）', flush=True)
    print(f'[INIT][FATAL] 最后错误：{type(last_err).__name__}: {last_err}', flush=True)
    print('[INIT][FATAL] 排查：① Render Environment 里 DATABASE_URL 是否正确 ② Neon/PG', flush=True)
    print('[INIT][FATAL]   免费版长期无流量会被暂停/回收，去控制台确认实例还在 ③ 网络白名单', flush=True)
    print('=' * 70, flush=True)
    raise SystemExit('[INIT] PostgreSQL 不可达，拒绝启动（保护用户数据，详见上方日志）')
