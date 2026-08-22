"""生成全部 RSS 静态文件到 public/ 目录，供 CI 与 Pages 部署使用。

用法：python scripts/build_feeds.py [输出目录] [--only-gallery]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_gallery_sources
from app.feed import build_opml, build_rss
from app.fetcher import ForumFetcher
from app.models import FeedItem
from app.parser import (
    parse_forum_a_items,
    parse_forum_b_home_items,
    parse_forum_b_items,
    parse_link_gallery_items,
    parse_mzt_api_items,
    parse_rss_items,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 200
EMPTY_PAGE_LIMIT = 2
FIDS_B = ("19", "21", "33")

# 关键源不受其他源失败/限速影响：单独限速器 + 优先抓取 + 更多重试
CRITICAL_GALLERY_KEYS = {"mzt", "91tutu", "91shenshi"}
CRITICAL_RETRIES = 5
DEFAULT_RETRIES = 3


def fetch_with_retry(fetcher: ForumFetcher, url: str, retries: int = 3) -> str:
    """带重试的抓取，容忍上游瞬时失败。

    参数：
        fetcher: 限速下载器。
        url: 页面地址。
        retries: 最大尝试次数。
    返回值：
        HTML 文本。
    """
    for attempt in range(retries):
        try:
            return fetcher.fetch_html(url)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(10)
    raise RuntimeError("unreachable")


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
    html = fetch_with_retry(fetcher, url)
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
        html = fetch_with_retry(fetcher, url)
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


def fetch_b_section_items(
    fetcher: ForumFetcher, base_url: str, fid: str, page_limit: int
) -> list:
    """抓取论坛 B 单个板块前若干页并按 thread_id 去重。

    参数：
        fetcher: 限速下载器。
        base_url: 板块地址前缀。
        fid: 板块编号。
        page_limit: 最多抓取页数。
    返回值：
        按发现顺序排列的 FeedItem 列表。
    """
    all_items = []
    seen_thread_ids = set()
    for page in range(1, page_limit + 1):
        url = f"{base_url}/forumdisplay.php?fid={fid}&page={page}"
        html = fetch_with_retry(fetcher, url)
        items = parse_forum_b_items(
            html, url, PAGE_SIZE, keep_image_posts_only=True
        )
        new_items = [item for item in items if item.thread_id not in seen_thread_ids]
        if not new_items:
            break
        for item in new_items:
            seen_thread_ids.add(item.thread_id)
            all_items.append(item)
    return all_items


def fetch_b_digest_items(
    fetcher: ForumFetcher, base_url: str, fid: str, page_limit: int
) -> list:
    """遍历论坛 B 板块精华帖全部分页并按 thread_id 去重。

    参数：
        fetcher: 限速下载器。
        base_url: 板块地址前缀。
        fid: 板块编号。
        page_limit: 最多抓取页数，0 表示不限制。
    返回值：
        按发现顺序排列的 FeedItem 列表。
    """
    all_items = []
    seen_thread_ids = set()
    empty_pages = 0
    page = 1
    while page_limit == 0 or page <= page_limit:
        url = f"{base_url}/forumdisplay.php?fid={fid}&filter=digest&page={page}"
        html = fetch_with_retry(fetcher, url)
        items = parse_forum_b_items(
            html, url, PAGE_SIZE, keep_image_posts_only=False
        )
        new_items = [item for item in items if item.thread_id not in seen_thread_ids]
        if not new_items:
            empty_pages += 1
            if empty_pages >= 2:
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
        filename: 文件名（含 .xml，可含子目录）。
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
    target = output_dir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    print(f"wrote {filename}: {len(items)} items")


def write_opml(output_dir: Path, entries: list[tuple[str, str]]) -> None:
    """生成 OPML 订阅列表文件。

    参数：
        output_dir: 输出目录。
        entries: (订阅标题, 订阅地址) 列表。
    返回值：
        无。
    """
    content = build_opml(entries)
    target = output_dir / "feeds.opml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    print(f"wrote feeds.opml: {len(entries)} entries")


def parse_gallery_source(source, html: str) -> list[FeedItem]:
    """按来源配置解析图站首页或自带 RSS。

    参数：
        source: FeedSource 图站来源。
        html: 抓取到的页面文本。
    返回值：
        FeedItem 列表。
    """
    if source.parser_kind == "rss":
        return parse_rss_items(html, source.source_url, PAGE_SIZE)
    if source.parser_kind == "mzt":
        return parse_mzt_api_items(html, source.source_url, PAGE_SIZE)
    return parse_link_gallery_items(
        html,
        source.source_url,
        PAGE_SIZE,
        link_pattern=source.link_pattern,
        link_selector=source.link_selector,
        parent_selector=source.parent_selector,
    )


def _get_fetcher_for_url(
    fetchers_by_host: dict[str, ForumFetcher], url: str
) -> ForumFetcher:
    """按 host 复用限速器，关键源与其他源互不阻塞。"""
    host = urlsplit(url).netloc.lower()
    fetcher = fetchers_by_host.get(host)
    if fetcher is None:
        fetcher = ForumFetcher(
            min_interval_seconds=10,
            timeout_seconds=20,
            user_agent=USER_AGENT,
        )
        fetchers_by_host[host] = fetcher
    return fetcher


def build_gallery_feeds(
    fetcher: ForumFetcher, output_dir: Path, public_base_url: str
) -> list:
    """抓取并生成全部图站 RSS 与聚合文件。

    参数：
        fetcher: 兼容旧签名的默认限速器（实际按 host 隔离，关键源优先）。
        output_dir: 输出目录。
        public_base_url: 对外基础地址。
    返回值：
        失败来源的 key 列表。
    """
    sources = load_gallery_sources(public_base_url)
    # 关键源优先，避免被前面失败/重试拖慢；其余按原顺序
    sources = sorted(sources, key=lambda s: (0 if s.key in CRITICAL_GALLERY_KEYS else 1))
    # 按 host 隔离限速器：91/mzt 等关键源不会被其他 host 的 10s 限速或重试阻塞
    # 传入的 fetcher 仅为兼容旧签名保留，实际使用 per-host 实例
    _ = fetcher  # 兼容未使用警告
    fetchers_by_host: dict[str, ForumFetcher] = {}
    failures = []
    collected_items = []
    for source in sources:
        is_critical = source.key in CRITICAL_GALLERY_KEYS
        host_fetcher = _get_fetcher_for_url(fetchers_by_host, source.source_url)
        retries = CRITICAL_RETRIES if is_critical else DEFAULT_RETRIES
        try:
            html = fetch_with_retry(host_fetcher, source.source_url, retries=retries)
            items = parse_gallery_source(source, html)
            if not items:
                raise RuntimeError("empty gallery list")
            collected_items.extend(items)
            write_feed(
                output_dir,
                f"gallery/{source.key}.xml",
                source.feed_title,
                source.source_url,
                items,
                public_base_url,
            )
        except Exception as error:
            failures.append(f"{source.key}: {type(error).__name__}")

    if collected_items:
        merged = []
        seen_guids = set()
        for item in collected_items:
            if item.thread_id not in seen_guids:
                seen_guids.add(item.thread_id)
                merged.append(item)
        write_feed(
            output_dir,
            "gallery.xml",
            "精选图站聚合",
            public_base_url,
            sort_by_published(merged),
            public_base_url,
        )
    return failures


def build_gallery_opml(output_dir: Path, public_base_url: str) -> None:
    """生成图站静态 OPML 文件。

    参数：
        output_dir: 输出目录。
        public_base_url: 对外基础地址。
    返回值：
        无。
    """
    gallery_sources = load_gallery_sources(public_base_url)
    entries = [
        (source.feed_title, f"{public_base_url}/gallery/{source.key}.xml")
        for source in gallery_sources
    ]
    entries.append(("精选图站聚合", f"{public_base_url}/gallery.xml"))
    write_opml(output_dir, entries)


def main() -> None:
    """抓取全部来源并生成静态 RSS 文件。

    参数：
        无（输出目录与模式从命令行读取）。
    返回值：
        无。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", nargs="?", default="public")
    parser.add_argument("--only-gallery", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    source_url_a = os.getenv("SOURCE_URL", "https://forum-a.example.com/thread0806.php?fid=16")
    forum_b_base = os.getenv("FORUM_B_BASE_URL", "https://forum-b.example.com").rstrip("/")
    forum_b_index = os.getenv("FORUM_B_INDEX_URL", "https://forum-b.example.com/index.php")
    public_base_url = os.getenv("PUBLIC_BASE_URL", "https://rss.example.com").rstrip("/")
    feed_title_a = os.getenv("FEED_TITLE_A", "论坛 A 示例订阅")
    feed_title_b = os.getenv("FEED_TITLE_B", "论坛 B")
    section_names = get_section_names()
    b_page_limit = int(os.getenv("B_PAGE_LIMIT", "5"))
    build_digest_b = os.getenv("BUILD_DIGEST_B", "0") == "1"
    digest_page_limit = int(os.getenv("DIGEST_PAGE_LIMIT", "0"))
    digest_base_url = os.getenv(
        "SNAPSHOT_BASE_URL",
        "https://forum-a.example.com/thread0806.php?fid=16&search=digest&page={page}",
    )

    # 按 host 隔离限速器：草榴/91 等关键源与其他源互不阻塞、互不因重试互相拖慢
    fetchers_by_host: dict[str, ForumFetcher] = {}

    def get_fetcher(url: str) -> ForumFetcher:
        return _get_fetcher_for_url(fetchers_by_host, url)

    # 兼容旧签名：build_gallery_feeds 仍需一个 fetcher，传入草榴（关键源）对应的 per-host 实例
    fallback_fetcher = get_fetcher(source_url_a)
    failures: list[str] = []

    if args.only_gallery:
        failures.extend(build_gallery_feeds(fallback_fetcher, output_dir, public_base_url))
        build_gallery_opml(output_dir, public_base_url)
        if failures:
            print("WARNINGS (non-fatal):", "; ".join(failures))
        return

    # 论坛 A 第一页带图帖（草榴关键源，独立 host 限速，不受其他源失败影响）
    try:
        items_a = fetch_items(
            get_fetcher(source_url_a), source_url_a, parse_forum_a_items, keep_images=True
        )
        write_feed(
            output_dir, "rss.xml", feed_title_a, source_url_a, items_a, public_base_url
        )
    except Exception as error:
        failures.append(f"forum-a: {type(error).__name__}")

    # 论坛 A 精华全量（草榴 digest，独立重试，不阻塞后续关键源）
    try:
        items_digest = fetch_digest_items(get_fetcher(digest_base_url), digest_base_url)
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

    # 论坛 B 三个板块（前若干页）
    section_items = {}
    for fid, section_name in section_names.items():
        try:
            url = f"{forum_b_base}/forumdisplay.php?fid={fid}"
            items = fetch_b_section_items(
                get_fetcher(url), forum_b_base, fid, b_page_limit
            )
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
        home_html = fetch_with_retry(get_fetcher(forum_b_index), forum_b_index)
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

    # 论坛 B 精华归档（每日低频任务，BUILD_DIGEST_B=1 时启用）
    if build_digest_b:
        digest_items = []
        digest_seen = set()
        for fid in section_names:
            try:
                items = fetch_b_digest_items(
                    get_fetcher(f"{forum_b_base}/forumdisplay.php?fid={fid}&filter=digest"),
                    forum_b_base,
                    fid,
                    digest_page_limit,
                )
                for item in items:
                    if item.thread_id not in digest_seen:
                        digest_seen.add(item.thread_id)
                        digest_items.append(item)
                print(f"forum-b-{fid} digest: {len(items)} items")
            except Exception as error:
                failures.append(f"forum-b-{fid}-digest: {type(error).__name__}")
        if digest_items:
            write_feed(
                output_dir,
                "forum-b-digest.xml",
                f"{feed_title_b} - 精华归档",
                f"{forum_b_base}/forumdisplay.php?fid=19&filter=digest",
                sort_by_published(digest_items),
                public_base_url,
            )

    # 精选图站 RSS（内部已按 host 隔离 + 关键源优先，91/草榴不受其他源失败影响）
    failures.extend(build_gallery_feeds(fallback_fetcher, output_dir, public_base_url))
    build_gallery_opml(output_dir, public_base_url)

    if failures:
        print("WARNINGS (non-fatal):", "; ".join(failures))


if __name__ == "__main__":
    main()
