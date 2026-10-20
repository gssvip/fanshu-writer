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
from flask import Blueprint, jsonify, request
from datetime import datetime

# 健康检查 Blueprint（默认无 DB 依赖）
health_bp = Blueprint('health', __name__)


@health_bp.route('/api/health', methods=['GET'])
def health_check():
    """健康检查端点：轻量探活（默认**不触碰数据库**）。

    配额铁律（2026-10-20 线上事故）：本端点原先无条件执行 SELECT 1 唤醒 Neon，
    而 GitHub Actions 每 10 分钟 ping 一次 → Neon 计算节点全年无休 ≈ 720 小时/月，
    远超 Neon 免费版计算配额 → 触发 "Your account or project has exceeded the quota"，
    Neon 拒绝全部连接，登录等接口 500。

    现改为：默认只做进程级探活（供 Render 判断端口/服务存活），**不再唤醒数据库**；
    需要穿透 DB 时可显式加 ?db=1（人工排障用，不要放进定时保活）。
    """
    db_status = 'skipped'
    if request.args.get('db') == '1':
        try:
            from app import db
            db.session.execute(db.text('SELECT 1'))
            db.session.rollback()
            db_status = 'ok'
        except Exception:
            db_status = 'down'
    return jsonify({'status': 'ok', 'db': db_status, 'time': datetime.now().isoformat()}), 200
