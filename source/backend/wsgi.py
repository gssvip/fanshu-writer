"""WSGI 生产入口（gunicorn wsgi:app）。

为什么需要这个文件：init_db() 原本只在 `python app.py` 的 __main__ 分支执行，
gunicorn 以 import 方式加载 app 模块时不会运行该分支——缺了这步，全新部署会
缺表。此处模块级调用一次 init_db()：
  - 版本门禁命中时秒级返回（见 app.init_db 注释）；
  - 配合 gunicorn --preload 在 master 进程只跑一次，worker 之间不会竞争建表。
本地开发仍然直接 `python app.py`，不受影响。
"""
from app import app, init_db, _set_boot_ready, _clear_boot_ready

# 清掉可能残留的旧就绪标记，避免"上次部署已就绪"的假象让 worker 跳过预热门禁
_clear_boot_ready()

init_db()

# 关键：gunicorn prefork 下，master 默认不执行 app.py 的 __main__ 后台预热线程，
# 不写 .boot_ready 标记的话，fork 出来的 worker 内存 _BOOT_READY 恒 False 且磁盘
# 标记不存在 → _gate_until_boot 对所有 /api 请求永远返回 503（业务全挂）。
# 此处 init_db 同步完成后写一次标记，worker 首次请求靠文件标记放行（见 app.py）。
_set_boot_ready()

# gunicorn 绑定地址/worker 参数见 gunicorn.conf.py（wsgi:app 只负责应用入口）
