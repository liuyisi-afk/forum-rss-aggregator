"""Gunicorn 生产配置；单进程保证全局缓存和 robots 限速一致。"""

import os


PORT = int(os.getenv("PORT", "28888"))
if PORT <= 20000:
    raise RuntimeError("PORT 必须大于 20000")

BIND_HOST = os.getenv("BIND_HOST", "127.0.0.1")
if BIND_HOST not in {"127.0.0.1", "0.0.0.0"}:
    raise RuntimeError("BIND_HOST 仅允许 127.0.0.1 或 0.0.0.0")

bind = f"{BIND_HOST}:{PORT}"
workers = 1
worker_class = "gthread"
threads = 4
timeout = 60
graceful_timeout = 30
accesslog = None
errorlog = "-"
