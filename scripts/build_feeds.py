"""生成全部 RSS 静态文件到 public/ 目录，供 CI 与 Pages 部署使用。

用法：
  python scripts/build_feeds.py [输出目录] [--only-forum] [--only-gallery]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_gallery_sources
from app.feed import build_opml, build_rss
from app.fetcher import DEFAULT_MAX_RESPONSE_BYTES, ForumFetcher
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
HTTP_SCHEMES = {"http", "https"}
GALLERY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def parse_int_env(name: str, default_value: int, minimum: int = 0) -> int:
    """读取并校验构建阶段的整数环境变量。

    参数：
        name: 环境变量名称。
        default_value: 变量缺失时使用的默认值。
        minimum: 允许的最小值（含边界）。
    返回值：
        校验后的整数。
    """
    raw_value = os.getenv(name)
    if raw_value is None:
        return default_value
    try:
        value = int(raw_value.strip())
    except (AttributeError, ValueError) as error:
        raise ValueError(f"环境变量 {name} 必须是整数") from error
    if value < minimum:
        raise ValueError(f"环境变量 {name} 必须大于等于 {minimum}")
    return value


def parse_bool_env(name: str, default_value: bool = False) -> bool:
    """读取构建阶段布尔环境变量并拒绝模糊值。

    参数：
        name: 环境变量名称。
        default_value: 变量缺失时使用的默认值。
    返回值：
        解析后的布尔值。
    """
    raw_value = os.getenv(name)
    if raw_value is None:
        return default_value
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"环境变量 {name} 必须是布尔值（0/1/true/false）")


def validate_http_url(value: str, field_name: str) -> str:
    """校验 HTTP(S) URL，统一拦截凭据、无效主机和端口。

    参数：
        value: 待校验地址。
        field_name: 错误消息中显示的字段名。
    返回值：
        去除首尾空白后的 URL。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是完整 HTTP(S) URL")
    normalized = value.strip()
    if len(normalized) > 2048 or any(
        character.isspace() or ord(character) < 0x20 for character in normalized
    ):
        raise ValueError(f"{field_name} URL 格式无效")
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        parsed.port
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} URL 格式无效") from error
    if parsed.scheme.lower() not in HTTP_SCHEMES or not parsed.netloc:
        raise ValueError(f"{field_name} 必须是完整 HTTP(S) URL")
    if hostname is None or parsed.username or parsed.password:
        raise ValueError(f"{field_name} 不得包含用户名或密码")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError(f"{field_name} 端口超出范围")
    return normalized


def normalize_public_base_url(public_base_url: str) -> str:
    """校验静态订阅使用的公开基础地址并去除尾斜杠。

    参数：
        public_base_url: 公开 RSS 的基础 URL。
    返回值：
        规范化后的基础 URL。
    """
    normalized = validate_http_url(public_base_url, "PUBLIC_BASE_URL").rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.query or parsed.fragment:
        raise ValueError("PUBLIC_BASE_URL 不得包含查询参数或片段")
    return normalized


def validate_gallery_sources(sources) -> tuple:
    """校验图站来源的唯一性、地址和筛选规则。

    参数：
        sources: `load_gallery_sources` 返回的 FeedSource 可迭代对象。
    返回值：
        原来源元组，便于调用方继续构建 feed。
    """
    seen_keys = set()
    for source in sources:
        key = str(source.key).strip()
        if not GALLERY_KEY_PATTERN.fullmatch(key):
            raise ValueError(
                f"图站来源 {key or '<empty>'} 的 key 只能包含字母、数字、_ 或 -"
            )
        normalized_key = key.casefold()
        if normalized_key in seen_keys:
            raise ValueError(f"图站来源 key 重复: {key}")
        seen_keys.add(normalized_key)

        if source.parser_kind not in {"auto", "rss", "links", "mzt"}:
            raise ValueError(
                f"图站来源 {key} 的 parser 必须是 auto、rss、links 或 mzt"
            )

        validate_http_url(source.source_url, f"图站来源 {key} 的 url")

        if source.parser_kind != "rss" and source.link_pattern:
            try:
                re.compile(source.link_pattern)
            except re.error as error:
                raise ValueError(f"图站来源 {key} 的 link_pattern 无效") from error
    return tuple(sources)


def load_validated_gallery_sources(public_base_url: str) -> tuple:
    """加载并校验图站来源配置，避免无效配置进入网络抓取阶段。

    参数：
        public_base_url: 公开 RSS 的基础 URL。
    返回值：
        已校验的 FeedSource 元组。
    """
    return validate_gallery_sources(load_gallery_sources(public_base_url))


def get_gallery_host(source_url: str) -> str:
    """提取图站 URL 的小写主机名，作为限速隔离键。

    参数：
        source_url: 图站来源地址。
    返回值：
        主机名；缺失时返回规范化 netloc。
    """
    parsed = urlsplit(source_url)
    return (parsed.hostname or parsed.netloc).lower()


def create_host_fetcher(template: ForumFetcher) -> ForumFetcher:
    """复制下载器的限速配置，为另一个上游主机创建独立会话。

    参数：
        template: 提供限速、超时、User-Agent 和响应上限的模板。
    返回值：
        配置一致但锁和会话独立的新下载器。
    """
    return ForumFetcher(
        min_interval_seconds=template.min_interval_seconds,
        timeout_seconds=template.timeout_seconds,
        user_agent=template.user_agent,
        clock=template.clock,
        sleeper=template.sleeper,
        max_response_bytes=template.max_response_bytes,
    )


def get_host_fetcher(fetchers_by_host: dict, template, source_url: str):
    """为 URL 返回按主机共享的下载器，并兼容测试替身。

    参数：
        fetchers_by_host: 已创建的主机到下载器映射。
        template: 真实 ForumFetcher 或测试替身。
        source_url: 即将抓取的来源地址。
    返回值：
        此主机应复用的下载器。
    """
    host = get_gallery_host(source_url)
    existing_fetcher = fetchers_by_host.get(host)
    if existing_fetcher is not None:
        return existing_fetcher

    if not isinstance(template, ForumFetcher):
        host_fetcher = template
    elif not fetchers_by_host:
        host_fetcher = template
    else:
        host_fetcher = create_host_fetcher(template)
    fetchers_by_host[host] = host_fetcher
    return host_fetcher


def build_gallery_fetchers(
    fetcher: ForumFetcher, sources, fetchers_by_host: dict | None = None
) -> dict:
    """为每个上游主机创建共享限速、跨主机并行的下载器。

    参数：
        fetcher: 包含限速参数的模板下载器。
        sources: 图站来源配置。
        fetchers_by_host: 可选的已有映射，用于与论坛抓取共享主机限速。
    返回值：
        主机名到下载器的映射；同主机来源共享一个锁。
    """
    fetchers = fetchers_by_host if fetchers_by_host is not None else {}
    for source in sources:
        get_host_fetcher(fetchers, fetcher, source.source_url)
    return fetchers

# 关键源不受其他源失败/限速影响：单独限速器 + 优先抓取 + 更多重试
CRITICAL_GALLERY_KEYS = {"mzt", "91tutu", "91shenshi"}
CRITICAL_RETRIES = 5
DEFAULT_RETRIES = 3


def is_critical_gallery(source) -> bool:
    """判断图站来源是否属于需要优先保障的关键源。

    参数：
        source: 图站来源对象。
    返回值：
        key 不区分大小写命中关键集合时返回 True。
    """
    return str(source.key).casefold() in CRITICAL_GALLERY_KEYS


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
    raw_value = os.getenv("SECTIONS_B")
    if raw_value is None or not raw_value.strip():
        raw_value = "板块一;板块二;板块三"
    raw_names = raw_value.split(";")
    if len(raw_names) != len(FIDS_B) or any(not name.strip() for name in raw_names):
        raise ValueError(
            f"SECTIONS_B 必须提供 {len(FIDS_B)} 个非空板块名（使用分号分隔）"
        )
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


def write_bytes_atomic(target: Path, content: bytes) -> None:
    """将字节内容原子替换到目标文件，避免阅读器读到半个 XML。

    参数：
        target: 最终输出路径。
        content: 待写入的字节内容。
    返回值：
        无。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
        os.replace(temporary_path, target)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def write_feed(
    output_dir: Path,
    filename: str,
    title: str,
    source_url: str,
    items: list,
    public_base_url: str,
) -> None:
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
    write_bytes_atomic(target, content)
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
    write_bytes_atomic(target, content)
    print(f"wrote feeds.opml: {len(entries)} entries")


def parse_gallery_source(source, html: str) -> list[FeedItem]:
    """按来源配置解析图站首页或自带 RSS。

    参数：
        source: FeedSource 图站来源。
        html: 抓取到的页面文本。
    返回值：
        FeedItem 列表。
    """
    parser_kind = source.parser_kind
    if parser_kind == "mzt":
        return parse_mzt_api_items(html, source.source_url, PAGE_SIZE)
    if parser_kind == "auto":
        # 自动模式先尝试 RSS/Atom；非 XML 页面再回退到图集链接解析。
        rss_items = parse_rss_items(html, source.source_url, PAGE_SIZE)
        if rss_items:
            return rss_items
    elif parser_kind == "rss":
        return parse_rss_items(html, source.source_url, PAGE_SIZE)
    return parse_link_gallery_items(
        html,
        source.source_url,
        PAGE_SIZE,
        link_pattern=source.link_pattern,
        link_selector=source.link_selector,
        parent_selector=source.parent_selector,
    )


GALLERY_WORKERS = 8


def fetch_single_gallery(
    source, fetcher: ForumFetcher, output_dir: Path, public_base_url: str
):
    """抓取并写入单个图站 feed，返回 (source, items, error)。

    参数：
        source: FeedSource 图站来源。
        fetcher: 下载器。
        output_dir: 输出目录。
        public_base_url: 对外基础地址。
    返回值：
        (source, items, error) 三元组，成功时 error 为 None。
    """
    try:
        retries = CRITICAL_RETRIES if is_critical_gallery(source) else DEFAULT_RETRIES
        html = fetch_with_retry(fetcher, source.source_url, retries=retries)
        items = parse_gallery_source(source, html)
        if not items:
            raise RuntimeError("empty gallery list")
        write_feed(
            output_dir,
            f"gallery/{source.key}.xml",
            source.feed_title,
            source.source_url,
            items,
            public_base_url,
        )
        return (source, items, None)
    except Exception as error:
        return (source, [], f"{source.key}: {type(error).__name__}")


def build_gallery_feeds(
    fetcher: ForumFetcher,
    output_dir: Path,
    public_base_url: str,
    fetchers_by_host: dict | None = None,
) -> list:
    """并行抓取全部图站并生成独立 feed 与聚合文件。

    不同图站主机使用线程池并行抓取，同一主机仍共享限速器。
    参数：
        fetcher: 提供下载配置的模板下载器。
        output_dir: 输出目录。
        public_base_url: 对外基础地址。
        fetchers_by_host: 可选的已有主机映射，用于与论坛构建共享限速器。
    返回值：
        失败来源的 key 列表。
    """
    sources = load_validated_gallery_sources(public_base_url)
    failures = []
    results_by_key = {}
    host_fetchers = build_gallery_fetchers(fetcher, sources, fetchers_by_host)
    prioritized_sources = sorted(
        sources,
        key=lambda source: not is_critical_gallery(source),
    )

    with ThreadPoolExecutor(max_workers=GALLERY_WORKERS) as pool:
        futures = {
            pool.submit(
                fetch_single_gallery,
                source,
                host_fetchers[get_gallery_host(source.source_url)],
                output_dir,
                public_base_url,
            ): source
            for source in prioritized_sources
        }
        for future in as_completed(futures):
            source, items, error = future.result()
            if error:
                failures.append(error)
            results_by_key[source.key] = (items, error)

    # 按配置顺序合并，避免线程完成顺序变化导致静态文件无意义地抖动。
    collected_items = []
    for source in sources:
        items, error = results_by_key.get(source.key, ([], "missing result"))
        if error is None:
            collected_items.extend(items)
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
    gallery_sources = load_validated_gallery_sources(public_base_url)
    entries = [
        (source.feed_title, f"{public_base_url}/gallery/{source.key}.xml")
        for source in gallery_sources
    ]
    entries.append(("精选图站聚合", f"{public_base_url}/gallery.xml"))
    write_opml(output_dir, entries)


def build_forum_feeds(
    fetcher: ForumFetcher,
    output_dir: Path,
    public_base_url: str,
    fetchers_by_host: dict | None = None,
) -> list:
    """Fetch forum A and B feeds, write to output dir.

    Args:
        fetcher: Rate-limited fetcher.
        output_dir: Output directory.
        public_base_url: Public base URL.
        fetchers_by_host: Optional host map shared with gallery construction.
    Returns:
        List of failed source keys.
    """
    source_url_a = os.getenv("SOURCE_URL", "https://forum-a.example.com/thread0806.php?fid=16")
    forum_b_base = os.getenv("FORUM_B_BASE_URL", "https://forum-b.example.com").rstrip("/")
    forum_b_index = os.getenv("FORUM_B_INDEX_URL", "https://forum-b.example.com/index.php")
    feed_title_a = os.getenv("FEED_TITLE_A", "论坛 A 示例订阅")
    feed_title_b = os.getenv("FEED_TITLE_B", "论坛 B")
    section_names = get_section_names()
    b_page_limit = parse_int_env("B_PAGE_LIMIT", 5, minimum=1)
    build_digest_b = parse_bool_env("BUILD_DIGEST_B", False)
    digest_page_limit = parse_int_env("DIGEST_PAGE_LIMIT", 0, minimum=0)
    digest_base_url = os.getenv(
        "SNAPSHOT_BASE_URL",
        "https://forum-a.example.com/thread0806.php?fid=16&search=digest&page={page}",
    )
    validate_http_url(source_url_a, "SOURCE_URL")
    validate_http_url(forum_b_base, "FORUM_B_BASE_URL")
    validate_http_url(forum_b_index, "FORUM_B_INDEX_URL")
    try:
        digest_url = digest_base_url.format(page=1)
    except (IndexError, KeyError, ValueError) as error:
        raise ValueError("SNAPSHOT_BASE_URL 必须是可格式化的 URL") from error
    validate_http_url(digest_url, "SNAPSHOT_BASE_URL")
    host_fetchers = fetchers_by_host if fetchers_by_host is not None else {}

    def get_fetcher(url: str) -> ForumFetcher:
        return get_host_fetcher(host_fetchers, fetcher, url)

    failures: list[str] = []

    # 论坛 A 第一页带图帖；不同上游主机使用各自的限速器。
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
        items_digest = fetch_digest_items(get_fetcher(digest_url), digest_base_url)
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
                    get_fetcher(
                        f"{forum_b_base}/forumdisplay.php?fid={fid}&filter=digest"
                    ),
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

    return failures


def report_failures(failures: list[str]) -> None:
    """将抓取失败以非致命警告输出，便于 CI 日志发现问题。

    参数：
        failures: 来源失败描述列表。
    返回值：
        无。
    """
    if failures:
        print("WARNINGS (non-fatal):", "; ".join(failures))


def main() -> None:
    """抓取全部来源并生成静态 RSS 文件。

    参数：
        无（输出目录与模式从命令行读取）。
    返回值：
        无。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", nargs="?", default="public")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--only-forum", action="store_true")
    mode_group.add_argument("--only-gallery", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    public_base_url = normalize_public_base_url(
        os.getenv("PUBLIC_BASE_URL", "https://rss.example.com")
    )

    # 论坛构建不依赖图站配置，允许两个 CI job 独立运行；其余模式先校验
    # 图站配置，避免网络抓取完成后才发现 key/URL/正则错误。
    if not args.only_forum:
        load_validated_gallery_sources(public_base_url)

    fetcher = ForumFetcher(
        min_interval_seconds=parse_int_env(
            "MIN_FETCH_INTERVAL_SECONDS", 10, minimum=10
        ),
        timeout_seconds=parse_int_env("REQUEST_TIMEOUT_SECONDS", 20, minimum=1),
        user_agent=os.getenv("USER_AGENT", USER_AGENT),
        max_response_bytes=parse_int_env(
            "MAX_RESPONSE_BYTES", DEFAULT_MAX_RESPONSE_BYTES, minimum=1
        ),
    )
    failures = []

    if args.only_gallery:
        failures.extend(build_gallery_feeds(fetcher, output_dir, public_base_url))
        build_gallery_opml(output_dir, public_base_url)
        report_failures(failures)
        return

    if args.only_forum:
        # 之前这里丢弃了返回值，导致论坛来源全部失败时 CI 日志没有任何提示。
        failures.extend(build_forum_feeds(fetcher, output_dir, public_base_url))
        report_failures(failures)
        return

    fetchers_by_host: dict[str, ForumFetcher] = {}
    failures.extend(
        build_forum_feeds(
            fetcher,
            output_dir,
            public_base_url,
            fetchers_by_host,
        )
    )
    failures.extend(
        build_gallery_feeds(
            fetcher,
            output_dir,
            public_base_url,
            fetchers_by_host,
        )
    )
    build_gallery_opml(output_dir, public_base_url)

    report_failures(failures)


if __name__ == "__main__":
    main()
