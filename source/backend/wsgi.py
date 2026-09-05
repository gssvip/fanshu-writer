"""WSGI 生产入口（gunicorn wsgi:app）。

为什么需要这个文件：init_db() 原本只在 `python app.py` 的 __main__ 分支执行，
gunicorn 以 import 方式加载 app 模块时不会运行该分支——缺了这步，全新部署会
缺表。此处模块级调用一次 init_db()：
  - 版本门禁命中时秒级返回（见 app.init_db 注释）；
  - 配合 gunicorn --preload 在 master 进程只跑一次，worker 之间不会竞争建表。
本地开发仍然直接 `python app.py`，不受影响。
"""
from app import app, init_db

init_db()

# gunicorn 绑定地址/worker 参数见 gunicorn.conf.py（wsgi:app 只负责应用入口）
