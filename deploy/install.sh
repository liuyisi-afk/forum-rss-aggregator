#!/usr/bin/env bash
set -euo pipefail

# 部署只允许 root 执行，因为需要创建系统用户和 systemd 单元。
if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 root 执行 deploy/install.sh" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "缺少 python3，请先使用系统包管理器安装 Python 3.11+" >&2
  exit 1
fi

if [[ ! -f "/etc/rss-feed.env" ]]; then
  echo "缺少 /etc/rss-feed.env，请从 deploy/rss-feed.env.example 创建" >&2
  exit 1
fi

if ! id rssfeed >/dev/null 2>&1; then
  useradd --system --home-dir /opt/rss-feed --shell /usr/sbin/nologin rssfeed
fi

install -d -o rssfeed -g rssfeed -m 0755 /opt/rss-feed
# 已从仓库根目录解压到 /opt/rss-feed 时跳过复制，避免 GNU cp 拒绝同名源目标。
if [[ "$(pwd)" != "/opt/rss-feed" ]]; then
  cp -a app deploy requirements.txt gunicorn.conf.py /opt/rss-feed/
fi
python3 -m venv /opt/rss-feed/.venv
/opt/rss-feed/.venv/bin/python -m pip install --disable-pip-version-check -r /opt/rss-feed/requirements.txt
chown -R rssfeed:rssfeed /opt/rss-feed
chmod 0600 /etc/rss-feed.env

install -m 0644 deploy/rss-feed.service /etc/systemd/system/rss-feed.service
systemctl daemon-reload
systemctl enable rss-feed.service
# 更新部署必须重启进程，否则 systemd 会继续运行旧代码。
systemctl restart rss-feed.service
systemctl --no-pager --full status rss-feed.service
