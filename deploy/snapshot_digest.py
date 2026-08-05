"""全量抓取论坛 A 精华帖并生成 RSS 快照文件，供定时任务调用。

图片可能过期的旧帖也全部保留，不做图片过滤。

用法：
    python deploy/snapshot_digest.py [输出路径] [最大页数]
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.feed import build_rss
from app.fetcher import ForumFetcher
from app.parser import parse_forum_a_items

DIGEST_BASE_URL = os.getenv(
    "SNAPSHOT_BASE_URL",
    "https://forum-a.example.com/thread0806.php?fid=16&search=digest&page={page}",
)
PUBLIC_FEED_URL = os.getenv(
    "SNAPSHOT_PUBLIC_URL", "https://rss.example.com/rss/caoliu-digest.xml"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 200
EMPTY_PAGE_LIMIT = 2


def fetch_all_digest_items(
    fetcher: ForumFetcher, max_pages: int
) -> list:
    """遍历精华帖全部分页并跨页去重。

    参数：
        fetcher: 遵守限速的页面下载器。
        max_pages: 最大遍历页数，防止异常时无限抓取。
    返回值：
        按发现顺序排列的 FeedItem 列表。
    """
    all_items = []
    seen_thread_ids = set()
    empty_pages = 0
    page = 1
    while page <= max_pages:
        url = DIGEST_BASE_URL.format(page=page)
        html = fetcher.fetch_html(url)
        items = parse_forum_a_items(
            html, url, PAGE_SIZE, keep_image_posts_only=False
        )
        new_items = [item for item in items if item.thread_id not in seen_thread_ids]
        if not new_items:
            empty_pages += 1
            if empty_pages >= EMPTY_PAGE_LIMIT:
                break
        else:
            empty_pages = 0
            for item in new_items:
                seen_thread_ids.add(item.thread_id)
                all_items.append(item)
        page += 1
    return all_items


def main() -> None:
    """执行一次全量快照抓取并原子写入 RSS 文件。

    参数：
        无（从命令行读取输出路径与最大页数）。
    返回值：
        无。
    """
    output = sys.argv[1] if len(sys.argv) > 1 else "/opt/rss-feed/var/caoliu-digest.xml"
    max_pages = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_MAX_PAGES

    fetcher = ForumFetcher(
        min_interval_seconds=10,
        timeout_seconds=20,
        user_agent=USER_AGENT,
    )
    items = fetch_all_digest_items(fetcher, max_pages)

    content = build_rss(
        items,
        "草榴社区 - 达盖尔的旗帜 - 精华",
        DIGEST_BASE_URL.format(page=1),
        PUBLIC_FEED_URL,
        datetime.now(timezone.utc),
    )
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_bytes(content)
    os.replace(tmp_path, output_path)
    print(f"wrote {len(items)} items to {output}")


if __name__ == "__main__":
    main()
