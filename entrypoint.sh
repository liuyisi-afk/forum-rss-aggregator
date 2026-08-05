#!/bin/sh
# 容器入口：RUN_MODE=app 启动 RSS 服务；snapshot 执行一次快照；snapshot-loop 定时循环快照。
set -eu

MODE="${RUN_MODE:-app}"
SNAPSHOT_OUTPUT="${SNAPSHOT_OUTPUT:-/var/rss-feed/caoliu-digest.xml}"
SNAPSHOT_INTERVAL="${SNAPSHOT_INTERVAL:-86400}"

run_snapshot() {
    python /opt/rss-feed/deploy/snapshot_digest.py "${SNAPSHOT_OUTPUT}"
}

case "${MODE}" in
    snapshot)
        run_snapshot
        ;;
    snapshot-loop)
        mkdir -p "$(dirname "${SNAPSHOT_OUTPUT}")"
        while true; do
            run_snapshot || true
            sleep "${SNAPSHOT_INTERVAL}"
        done
        ;;
    *)
        exec gunicorn -c gunicorn.conf.py app.server:app
        ;;
esac
