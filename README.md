# 论坛 RSS 聚合器

把公开论坛索引页转换为纯文本元数据的 RSS 2.0 订阅源：抓取列表第一页、过滤纯文字帖子、按来源生成独立 RSS。服务不进入帖子正文，不下载图片或附件，并遵守站点 robots.txt 的请求间隔限制。

## 功能

- **论坛 A**：解析帖子列表页，仅保留标题含图片/视频计数标记（如 `[36P]`、`[10+1V]`）的帖子。
- **论坛 B**：解析 Discuz 风格列表页，仅保留带图片附件图标的帖子；支持多板块独立订阅与合并聚合。
- **论坛 B 首页精选**：解析“最新精华 / 最新点赞 / 本周热门”三栏，跨栏按 thread id 去重。
- **精选图站**：支持站点自带 RSS 直通和首页链接列表解析，覆盖国模/中日韩/亚模擦边与 cos 图站，生成独立与聚合订阅。
- **OPML**：`/feeds.opml` 输出全部订阅，方便导入阅读器。

## 路由（本地运行示例）

| 路由 | 说明 |
| --- | --- |
| `/rss.xml`（别名 `/rss/forum-a.xml`） | 论坛 A 列表 |
| `/rss/forum-b-fid-19.xml` | 论坛 B 板块一 |
| `/rss/forum-b-fid-21.xml` | 论坛 B 板块二 |
| `/rss/forum-b-fid-33.xml` | 论坛 B 板块三 |
| `/rss/forum-b.xml` | 论坛 B 三个板块合并，按发布时间排序 |
| `/rss/forum-b-highlights.xml` | 论坛 B 首页精选 |
| `/gallery.xml` | 全部精选图站合并订阅 |
| `/gallery/<key>.xml` | 单个图站订阅 |
| `/feeds.opml` | 全部订阅的 OPML 列表 |
| `/healthz` | 健康检查 |

部署后把 `PUBLIC_FEED_URL` / `PUBLIC_BASE_URL` 指向你的对外地址，例如：

- `https://rss.example.com/rss.xml`
- `https://rss.example.com/gallery.xml`

## 纯文本过滤

`KEEP_IMAGE_POSTS_ONLY=1`（默认开启）时只保留带图帖子：

- 论坛 A：标题含图片/视频计数标记（如 `[36P]`、`［22P+1V］`）才保留，公告、教程等纯文字帖会被过滤。
- 论坛 B：列表行含图片附件图标才保留。

设为 `0` 可保留全部帖子。

## 架构

- 应用（Flask + Gunicorn）监听 `127.0.0.1:28888`，按主机级 10 秒限速抓取索引页。
- 图站来源配置在 `config/gallery_sources.json`，`rss` 表示直接转发站点自带 RSS，`links` 表示解析首页图集链接。
- nginx 反向代理到本地应用，80 端口 301 跳转 HTTPS，443 使用源站证书。
- 可选前置 CDN（如 Cloudflare）：源站证书可由 CDN 的 Origin CA 接口签发，Zone SSL 使用 Full (strict)。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m app.server
```

访问 `http://127.0.0.1:28888/healthz` 验证。

只生成图站静态订阅：

```powershell
$env:PUBLIC_BASE_URL='https://rss.example.com'
.\.venv\Scripts\python.exe scripts\build_feeds.py public --only-gallery
```

静态构建会在抓取前校验 `PUBLIC_BASE_URL`、分页参数和图站来源（key、URL、正则）；论坛与图站模式互斥，可分别在 CI 中运行。图站抓取按主机共享限速器，不同主机可并行执行。

## 配置

通过环境变量注入（参考 `.env.example` 与 `deploy/rss-feed.env.example`）：

| 变量 | 说明 |
| --- | --- |
| `SOURCE_URL` | 论坛 A 列表页地址 |
| `FEED_TITLE` | 论坛 A 订阅标题 |
| `PUBLIC_FEED_URL` | 对外 RSS 地址 |
| `PUBLIC_BASE_URL` | 对外基础地址，用于生成各分板块链接 |
| `BIND_HOST` / `PORT` | 监听地址与端口（端口必须大于 20000） |
| `CACHE_SECONDS` | 缓存秒数，不得小于上游最小请求间隔 |
| `FAILURE_RETRY_SECONDS` | 抓取失败后的重试退避秒数 |
| `MIN_FETCH_INTERVAL_SECONDS` | 任意两次上游请求的最小间隔（默认 10 秒） |
| `REQUEST_TIMEOUT_SECONDS` | 单次请求超时 |
| `MAX_RESPONSE_BYTES` | 单次上游响应最大字节数（默认 10 MiB） |
| `MAX_FEED_ITEMS` | 每个订阅最多条目数 |
| `USER_AGENT` | 请求 User-Agent |
| `KEEP_IMAGE_POSTS_ONLY` | `1` 只保留带图帖子，`0` 保留全部 |
| `GALLERY_SOURCES_FILE` | 可选：图站来源 JSON 文件路径 |

## 部署

生产配置只允许一个 Gunicorn worker：多进程会建立独立缓存并可能突破上游限速。

```bash
cp deploy/rss-feed.env.example /etc/rss-feed.env
chmod 600 /etc/rss-feed.env
# 编辑 /etc/rss-feed.env 填入真实来源地址
bash deploy/install.sh
curl --fail http://127.0.0.1:28888/healthz
```

nginx 反向代理（需先放置源站证书到 `/etc/nginx/certs/`，并修改 `deploy/nginx-rss.conf` 中的 `server_name`）：

```bash
bash deploy/install-nginx.sh
```

## 安全说明

- 仓库不含任何凭据；部署时通过 `/etc/rss-feed.env` 注入。
- 不在仓库、日志或文档中保存密码、API Token 或证书私钥。
- RSS 只输出标题、链接、作者、发布时间和 thread id（纯文本元数据）。
- 不绕过登录、验证码、反爬或访问限制。
- RSS 响应提供 `ETag` / `Last-Modified`，支持阅读器条件请求；快照文件默认位于 `/var/rss-feed/caoliu-digest.xml`。
