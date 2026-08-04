"""验证缓存刷新失败后的退避和降级行为。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.models import FeedItem, FeedResult, FeedSource
from app.parser import parse_forum_a_items
from app.service import AggregateFeedService, FeedService, FeedServiceError


class FailingFetcher:
    """记录调用次数并始终失败的测试下载器。"""

    def __init__(self) -> None:
        """初始化调用计数。

        参数：
            无。
        返回值：
            无。
        """
        self.call_count = 0

    def fetch_html(self, url: str) -> str:
        """模拟上游失败并记录调用。

        参数：
            url: 被请求的地址。
        返回值：
            无；始终抛出 RuntimeError。
        """
        self.call_count += 1
        raise RuntimeError("sensitive upstream detail")


def build_service(fetcher: FailingFetcher) -> FeedService:
    """构造带失败下载器的 FeedService。

    参数：
        fetcher: 测试下载器。
    返回值：
        FeedService 实例。
    """
    settings = Settings(
        source_url="https://forum-a.example.com/thread0806.php?fid=16",
        feed_title="测试 RSS",
        public_feed_url="http://127.0.0.1:28888/rss.xml",
        public_base_url="http://127.0.0.1:28888",
        port=28888,
        cache_seconds=600,
        failure_retry_seconds=60,
        min_fetch_interval_seconds=10,
        request_timeout_seconds=20,
        max_feed_items=100,
        user_agent="test-agent",
    )
    source = FeedSource(
        key="test",
        source_url=settings.source_url,
        feed_title=settings.feed_title,
        route="/rss.xml",
        public_feed_url=settings.public_feed_url,
    )
    return FeedService(settings, source, fetcher, parse_forum_a_items)


def test_failure_backoff_returns_stale_without_repeated_fetch() -> None:
    """验证退避窗口内重复请求直接返回旧缓存。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    fetcher = FailingFetcher()
    service = build_service(fetcher)
    service.cached_result = FeedResult(
        content=b"<rss/>",
        is_stale=False,
        generated_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    first_result = service.get_feed()
    second_result = service.get_feed()

    assert first_result.is_stale is True
    assert second_result.is_stale is True
    assert fetcher.call_count == 1


def test_initial_failure_uses_generic_error_during_backoff() -> None:
    """验证无缓存失败时不向调用方泄露底层异常详情。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    fetcher = FailingFetcher()
    service = build_service(fetcher)

    with pytest.raises(FeedServiceError, match="RSS 暂时不可用"):
        service.get_feed()
    with pytest.raises(FeedServiceError, match="RSS 暂时不可用"):
        service.get_feed()

    assert fetcher.call_count == 1


class StubChildService:
    """返回固定条目列表的测试子来源。"""

    def __init__(self, items: list[FeedItem]) -> None:
        """保存固定条目。

        参数：
            items: 子来源条目。
        返回值：
            无。
        """
        self.items = items

    def get_feed(self) -> FeedResult:
        """返回包含固定条目的 FeedResult。

        参数：
            无。
        返回值：
            FeedResult。
        """
        return FeedResult(
            content=b"<rss/>",
            is_stale=False,
            generated_at=datetime.now(timezone.utc),
            items=self.items,
        )


def test_aggregate_feed_service_merges_deduplicates_and_sorts() -> None:
    """验证聚合服务去重并按发布时间倒序、缺失时间置后。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    settings = Settings(
        source_url="https://forum-a.example.com/thread0806.php?fid=16",
        feed_title="测试 RSS",
        public_feed_url="http://127.0.0.1:28888/rss.xml",
        public_base_url="http://127.0.0.1:28888",
        port=28888,
        cache_seconds=600,
        failure_retry_seconds=60,
        min_fetch_interval_seconds=10,
        request_timeout_seconds=20,
        max_feed_items=100,
        user_agent="test-agent",
    )
    older = FeedItem(
        thread_id="a", title="旧", link="http://x/a", author=None,
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    newer = FeedItem(
        thread_id="b", title="新", link="http://x/b", author=None,
        published_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    no_time = FeedItem(
        thread_id="c", title="无时间", link="http://x/c", author=None,
        published_at=None,
    )
    duplicate = FeedItem(
        thread_id="a", title="重复", link="http://x/a2", author=None,
        published_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    aggregate = AggregateFeedService(
        settings=settings,
        feed_title="聚合",
        source_url="https://forum-b.example.com/index.php",
        public_feed_url="http://127.0.0.1:28888/rss/forum-b.xml",
        children=[
            StubChildService([older, duplicate]),
            StubChildService([newer, no_time]),
        ],
    )

    result = aggregate.get_feed()

    assert [item.thread_id for item in result.items] == ["b", "a", "c"]
    assert len(result.items) == 3
