"""Flask-SQLAlchemy 扩展实例（从 app.py 外迁）。

保持 `db = SQLAlchemy()` 未绑定 app 的形式，由 app.py 在完成配置后调用
`db.init_app(app)` 绑定；各模型、蓝图、工具模块统一 `from extensions import db`。
"""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()