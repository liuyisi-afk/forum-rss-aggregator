#!/usr/bin/env bash
set -euo pipefail
# 安装 nginx 反向代理站点并放行 80/443；需先放置源站证书。
if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 root 执行 deploy/install-nginx.sh" >&2
  exit 1
fi
if [[ ! -f /etc/nginx/certs/rss-origin.pem || ! -f /etc/nginx/certs/rss-origin.key ]]; then
  echo "缺少源站证书" >&2
  exit 1
fi
install -d -m 0755 /etc/nginx/certs
install -m 0644 deploy/nginx-rss.conf /etc/nginx/sites-available/rss
ln -sf /etc/nginx/sites-available/rss /etc/nginx/sites-enabled/rss
nginx -t
systemctl reload nginx
if command -v ufw >/dev/null 2>&1; then
  ufw allow 80/tcp || true
  ufw allow 443/tcp || true
fi
echo "nginx 站点已安装并重载"
