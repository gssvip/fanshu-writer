"""Flask Blueprint 示范模块。

展示如何把 app.py 中的路由拆分到独立 Blueprint 文件。
后续拆分按此模式：每个业务域一个 Blueprint 文件，在 app.py 中注册。

拆分模式：
  1. 创建 blueprints/<domain>_bp.py
  2. 定义 bp = Blueprint('<domain>', __name__)
  3. 路由用 @bp.route 而非 @app.route
  4. 依赖通过参数注入或延迟 import
  5. 在 app.py 中 app.register_blueprint(bp)
"""
from flask import Blueprint, jsonify
from datetime import datetime

# 健康检查 Blueprint（最简单的示范：无 DB 依赖）
health_bp = Blueprint('health', __name__)


@health_bp.route('/api/health', methods=['GET'])
def health_check():
    """健康检查端点：轻量探活 + 穿透数据库（SELECT 1 唤醒 Neon）。

    背景：外部保活（GitHub Actions 每 10 分钟 / 内部 warmUp 首屏）若只 hit 本端点
    而不触碰数据库，只会保持 Flask 进程常温，Neon 免费版计算节点仍会 autosuspend——
    首页首个真实查询（listBooks 等）依旧要等数据库冷唤醒，表现为"保活做了还是慢"。
    故此处附带执行 SELECT 1 穿透连接池：失败不致命（冷启动窗口内属正常），仅把
    db 状态标记进响应体，HTTP 始终 200 以满足 Render 探活判定。
    """
    db_status = 'ok'
    try:
        from app import db
        db.session.execute(db.text('SELECT 1'))
        db.session.rollback()
    except Exception:
        db_status = 'warming'
    return jsonify({'status': 'ok', 'db': db_status, 'time': datetime.now().isoformat()}), 200
