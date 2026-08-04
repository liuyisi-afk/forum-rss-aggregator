"""解析论坛索引页，不进入帖子正文页面。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from app.models import FeedItem


THREAD_ID_PATTERN = re.compile(r"^t(?P<thread_id>\d+)$")
FORUM_B_THREAD_ID_PATTERN = re.compile(r"^normalthread_(?P<thread_id>\d+)$")
TIMESTAMP_PATTERN = re.compile(r"^(?P<timestamp>\d+)")
PINNED_TIME_PATTERN = re.compile(
    r"^置顶主题：(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$"
)
# 论坛 A 图片帖标题计数约定，兼容半角 [36P] 与全角 ［36P］、[10+1V] 等写法
IMAGE_COUNT_PATTERN = re.compile(
    r"[\[\uFF3B]\s*\d+\s*(?:P(?:\s*\+\s*\d+\s*V)?|V)\s*[\]\uFF3D]",
    re.IGNORECASE,
)
# 论坛 B 列表行中图片附件的图标，用于判断帖子是否包含图片
FORUM_B_ATTACH_ICON_SELECTOR = "img.attach[src*='attachicons']"
SITE_TIMEZONE = ZoneInfo("Asia/Shanghai")


def parse_timestamp(timestamp_element: Tag | None) -> datetime | None:
    """解析 data-timestamp，兼容站点偶发的尾随字符。

    参数：
        timestamp_element: 含 data-timestamp 属性的标签。
    返回值：
        UTC 时间；字段缺失或无效时返回 None。
    """
    if timestamp_element is None:
        return None

    raw_timestamp = str(timestamp_element.get("data-timestamp", ""))
    timestamp_match = TIMESTAMP_PATTERN.match(raw_timestamp)
    if timestamp_match is not None:
        try:
            value = int(timestamp_match.group("timestamp"))
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    title_match = PINNED_TIME_PATTERN.match(str(timestamp_element.get("title", "")))
    if title_match is None:
        return None

    try:
        local_time = datetime.strptime(title_match.group("date"), "%Y-%m-%d %H:%M:%S")
        return local_time.replace(tzinfo=SITE_TIMEZONE).astimezone(timezone.utc)
    except ValueError:
        return None


def get_expected_fid(base_url: str) -> str | None:
    """从索引页查询参数读取目标分区编号。

    参数：
        base_url: 带 fid 参数的论坛索引页地址。
    返回值：
        分区编号；缺失时返回 None。
    """
    values = parse_qs(urlparse(base_url).query).get("fid", [])
    return values[0] if values else None


def is_expected_forum_link(href: str, expected_fid: str | None) -> bool:
    """校验静态帖子链接属于目标分区，排除跨区全局置顶。

    参数：
        href: 帖子相对或绝对链接。
        expected_fid: 目标分区编号。
    返回值：
        链接属于目标分区时返回 True。
    """
    if expected_fid is None:
        return True

    path_parts = [part for part in urlparse(href).path.split("/") if part]
    return (
        len(path_parts) >= 4
        and path_parts[0] == "htm_data"
        and path_parts[2] == expected_fid
    )


def parse_row(row: Tag, base_url: str, expected_fid: str | None) -> FeedItem | None:
    """从单个列表行提取帖子元数据，缺少唯一标识时跳过。

    参数：
        row: 论坛帖子列表行。
        base_url: 用于补全相对链接的页面地址。
        expected_fid: 目标分区编号。
    返回值：
        完整的 FeedItem；无法可靠识别帖子时返回 None。
    """
    link_element = row.select_one("td.tal h3 a[id^='t']")
    if not isinstance(link_element, Tag):
        return None

    thread_match = THREAD_ID_PATTERN.match(str(link_element.get("id", "")))
    href = str(link_element.get("href", "")).strip()
    title = link_element.get_text(" ", strip=True)
    if (
        thread_match is None
        or not href
        or not title
        or not is_expected_forum_link(href, expected_fid)
    ):
        return None

    cells = row.find_all("td", recursive=False)
    author_cell = cells[2] if len(cells) > 2 else None
    author_element = author_cell.select_one("a.bl") if author_cell else None
    timestamp_element = None
    if author_cell:
        timestamp_element = author_cell.select_one(
            "div.f12 [data-timestamp], div.f12 [title^='置顶主题：']"
        )
    author = author_element.get_text(" ", strip=True) if author_element else None

    return FeedItem(
        thread_id=thread_match.group("thread_id"),
        title=title,
        link=urljoin(base_url, href),
        author=author or None,
        published_at=parse_timestamp(timestamp_element),
    )


def parse_forum_a_items(
    html: str,
    base_url: str,
    max_items: int,
    keep_image_posts_only: bool = False,
) -> list[FeedItem]:
    """解析第一页帖子列表，并按 thread_id 去重限制数量。

    参数：
        html: 论坛索引页 HTML。
        base_url: 当前索引页地址。
        max_items: RSS 最多保留的条目数。
        keep_image_posts_only: 仅保留标题含图片/视频计数标记的帖子。
    返回值：
        按页面顺序排列的帖子元数据列表。
    """
    if not html.strip() or max_items <= 0:
        return []

    soup = BeautifulSoup(html, "html.parser")
    items: list[FeedItem] = []
    seen_thread_ids: set[str] = set()
    expected_fid = get_expected_fid(base_url)

    for row in soup.select("tr.tr3.t_one.tac"):
        item = parse_row(row, base_url, expected_fid)
        if item is None or item.thread_id in seen_thread_ids:
            continue
        if keep_image_posts_only and not IMAGE_COUNT_PATTERN.search(item.title):
            continue

        seen_thread_ids.add(item.thread_id)
        items.append(item)
        if len(items) >= max_items:
            break

    return items


def parse_forum_b_date(date_element: Tag | None) -> datetime | None:
    """将 论坛 B 列表中的日期按上海时区当日零点转换为 UTC。

    参数：
        date_element: `td.author em` 日期标签。
    返回值：
        UTC 日期时间；字段缺失或无效时返回 None。
    """
    if date_element is None:
        return None

    date_text = date_element.get_text(" ", strip=True)
    try:
        local_time = datetime.strptime(date_text, "%Y-%m-%d")
        return local_time.replace(tzinfo=SITE_TIMEZONE).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_forum_b_row(row: Tag, base_url: str, fid: str) -> FeedItem | None:
    """解析一个 论坛 B 普通主题行并生成规范化元数据。

    参数：
        row: `normalthread_` 主题行。
        base_url: 当前板块列表地址。
        fid: 当前板块编号。
    返回值：
        FeedItem；字段不完整或 ID 不一致时返回 None。
    """
    thread_match = FORUM_B_THREAD_ID_PATTERN.match(str(row.get("id", "")))
    link_element = row.select_one(
        "th.subject > span[id^='thread_'] > a[href*='viewthread.php?tid=']"
    )
    if thread_match is None or not isinstance(link_element, Tag):
        return None

    thread_id = thread_match.group("thread_id")
    href = str(link_element.get("href", "")).strip()
    href_thread_ids = parse_qs(urlparse(href).query).get("tid", [])
    title = link_element.get_text(" ", strip=True)
    if not href or not title or href_thread_ids != [thread_id]:
        return None

    author_element = row.select_one("td.author cite a")
    date_element = row.select_one("td.author em")
    author = author_element.get_text(" ", strip=True) if author_element else None
    canonical_link = urljoin(base_url, f"viewthread.php?tid={thread_id}")
    return FeedItem(
        thread_id=f"forum-b:{fid}:{thread_id}",
        title=title,
        link=canonical_link,
        author=author or None,
        published_at=parse_forum_b_date(date_element),
    )


def parse_forum_b_items(
    html: str,
    base_url: str,
    max_items: int,
    keep_image_posts_only: bool = False,
) -> list[FeedItem]:
    """解析 论坛 B 第一页普通主题，排除跨板块共用置顶主题。

    参数：
        html: 论坛 B 板块列表 HTML。
        base_url: 带 fid 的板块列表地址。
        max_items: RSS 最多保留的条目数。
        keep_image_posts_only: 仅保留带图片附件图标的帖子。
    返回值：
        按页面顺序排列并去重的普通主题列表。
    """
    fid = get_expected_fid(base_url)
    if not html.strip() or max_items <= 0 or fid is None:
        return []

    soup = BeautifulSoup(html, "html.parser")
    items: list[FeedItem] = []
    seen_guids: set[str] = set()
    for row in soup.select("tbody[id^='normalthread_']"):
        if (
            keep_image_posts_only
            and row.select_one(FORUM_B_ATTACH_ICON_SELECTOR) is None
        ):
            continue
        item = parse_forum_b_row(row, base_url, fid)
        if item is None or item.thread_id in seen_guids:
            continue

        seen_guids.add(item.thread_id)
        items.append(item)
        if len(items) >= max_items:
            break
    return items

# 论坛 B 首页四格栏目标题，如 ≡ 最新精华 ≡
HIGHLIGHT_BLOCK_PATTERN = re.compile(r"^≡\s*(.+?)\s*≡$")


def parse_forum_b_home_items(
    html: str,
    base_url: str,
    max_items: int,
) -> list[FeedItem]:
    """解析 论坛 B 首页最新精华/最新点赞/本周热门三栏，跨栏按 thread id 去重。

    参数：
        html: 论坛 B 首页 HTML。
        base_url: 首页地址，用于补全链接。
        max_items: RSS 最多保留的条目数。
    返回值：
        按精华、点赞、热门顺序排列的帖子元数据列表。
    """
    if not html.strip() or max_items <= 0:
        return []

    soup = BeautifulSoup(html, "html.parser")
    category_thead = soup.find("thead", class_="category")
    if not isinstance(category_thead, Tag):
        return []
    headers = category_thead.find_all("td")
    data_row = category_thead.find_next_sibling()
    if data_row is None or data_row.name != "tr":
        return []
    columns = data_row.find_all("td", recursive=False)

    items: list[FeedItem] = []
    seen_thread_ids: set[str] = set()
    for header, column in zip(headers, columns):
        block_match = HIGHLIGHT_BLOCK_PATTERN.match(header.get_text(" ", strip=True))
        if block_match is None or not isinstance(column, Tag):
            continue
        block_name = block_match.group(1).strip()
        for link_element in column.select("a[href*='viewthread.php?tid=']"):
            href = str(link_element.get("href", "")).strip()
            title = link_element.get_text(" ", strip=True)
            tid_values = parse_qs(urlparse(href).query).get("tid", [])
            if not href or not title or not tid_values:
                continue
            thread_id = tid_values[0]
            if thread_id in seen_thread_ids:
                continue
            seen_thread_ids.add(thread_id)
            items.append(
                FeedItem(
                    thread_id=f"forum-b:home:{thread_id}",
                    title=f"[{block_name}] {title}",
                    link=urljoin(base_url, f"viewthread.php?tid={thread_id}"),
                    author=None,
                    published_at=None,
                )
            )
            if len(items) >= max_items:
                return items
    return items