"""gunicorn 生产配置（替代 `python app.py` 的 Flask 内置 dev server）。

为什么换 gunicorn：Flask 内置服务器单进程、无并发治理，SSE 流式端点
（ai-continue/ai-chat 等）一条连接就占一个线程，多用户下吞吐见顶。

关键参数理由：
  - worker_class = gthread：纯线程模型，无 gevent monkey-patch 副作用
    （psycopg2 是 C 扩展，gevent 下裸阻塞会卡死整个 worker 的绿色线程）；
  - timeout = 0：SSE 长连接一开就是几分钟，默认 30s 会把 worker 杀掉
    （应用层已有 30s 心跳保活，见各 stream 端点）；
  - keepalive = 75：与 Render/HF Spaces 反向代理的默认空闲超时对齐；
  - preload_app = True：init_db 在 master 只跑一次（wsgi.py 模块级调用），
    避免多 worker 并发建表竞争；
  - post_fork 里 dispose 掉 fork 继承的 DB 连接池：父进程的 socket 不能
    跨进程共享（SQLAlchemy 官方要求 fork 后重建池，首个请求会自动重连，
    且 app 的 _ensure_db 钩子有 weakref 兜底）。
环境变量可覆盖 worker/thread 数（HF Spaces 免费 2vCPU 建议 2x8，
Render 512MB 内存建议 GUNICORN_WORKERS=1 GUNICORN_THREADS=8）。
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '7860')}"
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
timeout = 0  # SSE 长连接不设超时（应用层有心跳）
keepalive = 75
preload_app = True
accesslog = "-"
errorlog = "-"


def post_fork(server, worker):
    """fork 后释放父进程继承的数据库连接池（socket 不可跨进程复用）。"""
    try:
        from app import app as flask_app, db
        with flask_app.app_context():
            db.engine.dispose()
    except Exception:
        # 初始化失败不影响启动：_ensure_db 每请求会重注册引擎兜底
        server.log.info("post_fork: dispose db pool skipped (init not ready)")
