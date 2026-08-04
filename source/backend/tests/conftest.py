"""pytest 公共 fixtures。

关键约束：
  - app.py 在 import 时会读 DATABASE_URL，生产环境无 DB 会 SystemExit
  - 测试必须在 import app 前注入 FANSHU_DATA_DIR 指向临时目录，强制走 SQLite
  - 不设 DATABASE_URL / PORT / RENDER，避免触发生产检测
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# backend 目录加入 sys.path，让 `import app` 可用
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(scope="session")
def _data_dir(tmp_path_factory):
    """session 级临时数据目录，隔离测试 SQLite 文件。"""
    return tmp_path_factory.mktemp("fanshu_data")


@pytest.fixture(scope="session")
def app(_data_dir):
    """加载 Flask app，强制使用 SQLite 本地开发模式。

    必须在 import app 前设置环境变量：
      - FANSHU_DATA_DIR: 指向临时目录
      - 清空 DATABASE_URL / PORT / RENDER，避免触发生产启动拒绝
    """
    # 保存原值，测试结束后恢复
    saved = {
        "FANSHU_DATA_DIR": os.environ.get("FANSHU_DATA_DIR"),
        "DATABASE_URL": os.environ.get("DATABASE_URL"),
        "PORT": os.environ.get("PORT"),
        "RENDER": os.environ.get("RENDER"),
        "HF_SPACE_ID": os.environ.get("HF_SPACE_ID"),
        "RAILWAY_PROJECT_ID": os.environ.get("RAILWAY_PROJECT_ID"),
    }
    os.environ["FANSHU_DATA_DIR"] = str(_data_dir)
    # 清空生产检测变量，强制走 SQLite 本地开发分支
    for k in ("DATABASE_URL", "PORT", "RENDER", "HF_SPACE_ID", "RAILWAY_PROJECT_ID"):
        os.environ.pop(k, None)

    try:
        # 延迟 import，确保环境变量先生效
        import app as app_module
        # init_db 建表（app.py 在 __main__ 里调，测试需手动调一次）
        if hasattr(app_module, "init_db"):
            app_module.init_db()
        yield app_module.app
    finally:
        # 恢复环境变量
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


@pytest.fixture()
def client(app):
    """每个测试独立的 test client + 独立事务回滚。"""
    from app import db

    # 每个测试前清表，保证隔离
    with app.app_context():
        for table in reversed(db.metadata.sorted_tables):
            db.session.execute(table.delete())
        db.session.commit()

    yield app.test_client()

    # 测试后清理
    with app.app_context():
        db.session.remove()
        for table in reversed(db.metadata.sorted_tables):
            db.session.execute(table.delete())
        db.session.commit()


@pytest.fixture()
def db_session(app):
    """直接拿 db.session 做模型层断言。"""
    from app import db
    with app.app_context():
        yield db.session
