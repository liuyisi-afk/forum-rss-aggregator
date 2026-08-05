"""生成全部 RSS 静态文件到 public/ 目录，供 CI 与 Pages 部署使用。

用法：python scripts/build_feeds.py [输出目录]
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.feed import build_rss
from app.fetcher import ForumFetcher
from app.parser import (
    parse_forum_a_items,
    parse_forum_b_home_items,
    parse_forum_b_items,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 200
EMPTY_PAGE_LIMIT = 2
FIDS_B = ("19", "21", "33")


def get_section_names() -> dict:
    """从环境变量读取论坛 B 板块名，默认使用中性占位名。

    参数：
        无。
    返回值：
        fid 到板块名的映射。
    """
    raw_names = os.getenv("SECTIONS_B", "板块一;板块二;板块三").split(";")
    return {fid: name.strip() for fid, name in zip(FIDS_B, raw_names)}


def fetch_items(fetcher: ForumFetcher, url: str, parser, keep_images: bool) -> list:
    """抓取单个列表页并解析。

    参数：
        fetcher: 限速下载器。
        url: 列表页地址。
        parser: 解析函数。
        keep_images: 是否只保留带图帖。
    返回值：
        FeedItem 列表。
    """
    html = fetcher.fetch_html(url)
    return parser(html, url, PAGE_SIZE, keep_images)


def fetch_digest_items(fetcher: ForumFetcher, base_url: str) -> list:
    """遍历精华帖全部分页并去重。

    参数：
        fetcher: 限速下载器。
        base_url: 含 {page} 占位符的分页地址。
    返回值：
        按发现顺序排列的 FeedItem 列表。
    """
    all_items = []
    seen_thread_ids = set()
    empty_pages = 0
    page = 1
    while page <= DEFAULT_MAX_PAGES:
        url = base_url.format(page=page)
        html = fetcher.fetch_html(url)
        items = parse_forum_a_items(html, url, PAGE_SIZE, keep_image_posts_only=False)
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


def sort_by_published(items: list) -> list:
    """按发布时间倒序，无时间条目排最后。

    参数：
        items: FeedItem 列表。
    返回值：
        排序后的列表。
    """
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(
        items,
        key=lambda item: item.published_at or epoch,
        reverse=True,
    )


def write_feed(output_dir: Path, filename: str, title: str, source_url: str, items: list, public_base_url: str) -> None:
    """生成单个 RSS 文件。

    参数：
        output_dir: 输出目录。
        filename: 文件名（含 .xml）。
        title: RSS 标题。
        source_url: 频道链接。
        items: FeedItem 列表。
        public_base_url: 对外基础地址。
    返回值：
        无。
    """
    content = build_rss(
        items,
        title,
        source_url,
        f"{public_base_url}/{filename}",
        datetime.now(timezone.utc),
    )
    (output_dir / filename).write_bytes(content)
    print(f"wrote {filename}: {len(items)} items")


def main() -> None:
    """抓取全部来源并生成静态 RSS 文件。

    参数：
        无（输出目录从命令行读取）。
    返回值：
        无。
    """
    output_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "public")
    output_dir.mkdir(parents=True, exist_ok=True)

    source_url_a = os.getenv("SOURCE_URL", "https://forum-a.example.com/thread0806.php?fid=16")
    forum_b_base = os.getenv("FORUM_B_BASE_URL", "https://forum-b.example.com").rstrip("/")
    forum_b_index = os.getenv("FORUM_B_INDEX_URL", "https://forum-b.example.com/index.php")
    public_base_url = os.getenv("PUBLIC_BASE_URL", "https://rss.example.com").rstrip("/")
    feed_title_a = os.getenv("FEED_TITLE_A", "论坛 A 示例订阅")
    feed_title_b = os.getenv("FEED_TITLE_B", "论坛 B")
    section_names = get_section_names()
    digest_base_url = os.getenv(
        "SNAPSHOT_BASE_URL",
        "https://forum-a.example.com/thread0806.php?fid=16&search=digest&page={page}",
    )

    fetcher = ForumFetcher(
        min_interval_seconds=10,
        timeout_seconds=20,
        user_agent=USER_AGENT,
    )
    failures = []

    # 论坛 A（草榴）第一页带图帖
    try:
        items_a = fetch_items(
            fetcher, source_url_a, parse_forum_a_items, keep_images=True
        )
        write_feed(
            output_dir, "rss.xml", feed_title_a, source_url_a, items_a, public_base_url
        )
    except Exception as error:
        failures.append(f"forum-a: {type(error).__name__}")

    # 论坛 A 精华全量
    try:
        items_digest = fetch_digest_items(fetcher, digest_base_url)
        write_feed(
            output_dir,
            "caoliu-digest.xml",
            f"{feed_title_a} - 精华",
            digest_base_url.format(page=1),
            items_digest,
            public_base_url,
        )
    except Exception as error:
        failures.append(f"digest: {type(error).__name__}")

    # 论坛 B（91）三个板块
    section_items = {}
    for fid, section_name in section_names.items():
        try:
            url = f"{forum_b_base}/forumdisplay.php?fid={fid}"
            items = fetch_items(fetcher, url, parse_forum_b_items, keep_images=True)
            section_items[fid] = items
            write_feed(
                output_dir,
                f"forum-b-fid-{fid}.xml",
                f"{feed_title_b} - {section_name}",
                url,
                items,
                public_base_url,
            )
        except Exception as error:
            failures.append(f"forum-b-{fid}: {type(error).__name__}")

    # 论坛 B 首页精选（解析器无图片过滤参数，单独调用）
    try:
        home_html = fetcher.fetch_html(forum_b_index)
        items_home = parse_forum_b_home_items(home_html, forum_b_index, PAGE_SIZE)
        write_feed(
            output_dir,
            "forum-b-highlights.xml",
            f"{feed_title_b} - 最新精华/最新点赞/本周热门",
            forum_b_index,
            items_home,
            public_base_url,
        )
    except Exception as error:
        failures.append(f"forum-b-highlights: {type(error).__name__}")

    # 论坛 B 聚合（三板块合并按时间排序）
    if section_items:
        merged = []
        seen = set()
        for items in section_items.values():
            for item in items:
                if item.thread_id not in seen:
                    seen.add(item.thread_id)
                    merged.append(item)
        write_feed(
            output_dir,
            "forum-b.xml",
            f"{feed_title_b} - {'/'.join(section_names.values())}",
            forum_b_index,
            sort_by_published(merged),
            public_base_url,
        )

    if failures:
        print("FAILURES:", "; ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
