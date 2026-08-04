"""定义抓取结果和服务返回值。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class FeedItem:
    """表示一个仅包含公开索引元数据的 RSS 条目。"""

    thread_id: str
    title: str
    link: str
    author: str | None
    published_at: datetime | None


@dataclass(frozen=True)
class FeedSource:
    """描述一个 RSS 来源及其公开访问端点。"""

    key: str
    source_url: str
    feed_title: str
    route: str
    public_feed_url: str


@dataclass(frozen=True)
class FeedResult:
    """表示生成后的 RSS 内容、条目列表及其缓存状态。"""

    content: bytes
    is_stale: bool
    generated_at: datetime
    items: list[FeedItem] = field(default_factory=list)
