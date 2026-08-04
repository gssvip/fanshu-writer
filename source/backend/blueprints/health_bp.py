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
    """超轻量健康检查端点，不查数据库，用于保活 ping。

    从 app.py 迁移到此 Blueprint，作为 Blueprint 拆分示范。
    """
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat()}), 200
