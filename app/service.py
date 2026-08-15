"""编排抓取、解析、缓存和 RSS 生成。"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Protocol

from app.config import Settings
from app.feed import build_rss
from app.models import FeedItem, FeedResult, FeedSource


LOGGER = logging.getLogger(__name__)


class HtmlFetcher(Protocol):
    """约束 FeedService 所需的最小下载器接口。"""

    def fetch_html(self, url: str) -> str:
        """下载 HTML 文本。

        参数：
            url: 待下载地址。
        返回值：
            HTML 文本。
        """
        ...


class FeedParser(Protocol):
    """约束不同论坛解析器使用统一输入输出。"""

    def __call__(
        self, html: str, base_url: str, max_items: int
    ) -> list[FeedItem]:
        """解析论坛列表页。

        参数：
            html: 列表页 HTML。
            base_url: 来源页面地址。
            max_items: 最大条目数。
        返回值：
            解析后的 FeedItem 列表。
        """
        ...


class FeedServiceError(RuntimeError):
    """表示没有可用缓存且无法生成 RSS。"""


class FeedService:
    """使用缓存合并并发刷新，减少对上游页面的请求次数。"""

    def __init__(
        self,
        settings: Settings,
        source: FeedSource,
        fetcher: HtmlFetcher,
        parser: FeedParser,
    ) -> None:
        """初始化 RSS 服务状态。

        参数：
            settings: 已校验的运行时配置。
            source: 当前 RSS 来源配置。
            fetcher: 遵守限速的页面下载器。
            parser: 与论坛结构匹配的解析器。
        返回值：
            无。
        """
        self.settings = settings
        self.source = source
        self.fetcher = fetcher
        self.parser = parser
        self.refresh_lock = threading.Lock()
        self.cached_result: FeedResult | None = None
        self.next_refresh_at: datetime | None = None

    def is_cache_fresh(self, now: datetime) -> bool:
        """判断现有缓存是否仍在有效期内。

        参数：
            now: 当前 UTC 时间。
        返回值：
            缓存存在且未过期时返回 True。
        """
        if self.cached_result is None:
            return False
        expires_at = self.cached_result.generated_at + timedelta(
            seconds=self.settings.cache_seconds
        )
        return now < expires_at

    def build_fresh_result(self, now: datetime) -> FeedResult:
        """请求上游并生成新 RSS，空列表视为结构变化而明确失败。

        参数：
            now: 本次 RSS 的生成时间。
        返回值：
            新生成且非陈旧的 FeedResult。
        """
        html = self.fetcher.fetch_html(self.source.source_url)
        items = self.parser(html, self.source.source_url, self.settings.max_feed_items)
        if not items:
            raise FeedServiceError("未解析到帖子，可能是页面结构变化或访问被拦截")

        content = build_rss(
            items,
            self.source.feed_title,
            self.source.source_url,
            self.source.public_feed_url,
            now,
        )
        return FeedResult(
            content=content,
            is_stale=False,
            generated_at=now,
            items=items,
        )

    def is_retry_delayed(self, now: datetime) -> bool:
        """判断上次失败后的退避窗口是否仍有效。

        参数：
            now: 当前 UTC 时间。
        返回值：
            尚未到下次刷新时间时返回 True。
        """
        return self.next_refresh_at is not None and now < self.next_refresh_at

    def get_stale_result(self) -> FeedResult:
        """返回明确标记为陈旧的缓存，不改变原生成时间。

        参数：
            无。
        返回值：
            陈旧 FeedResult。
        """
        if self.cached_result is None:
            raise FeedServiceError("RSS 暂时不可用")
        return FeedResult(
            content=self.cached_result.content,
            is_stale=True,
            generated_at=self.cached_result.generated_at,
            items=self.cached_result.items,
        )

    def get_feed(self) -> FeedResult:
        """返回新鲜缓存；刷新失败时显式标记并返回旧缓存。

        参数：
            无。
        返回值：
            可响应给订阅客户端的 FeedResult。
        """
        now = datetime.now(timezone.utc)
        if self.is_cache_fresh(now):
            cached_result = self.cached_result
            if cached_result is not None:
                return cached_result
        if self.is_retry_delayed(now):
            return self.get_stale_result()

        with self.refresh_lock:
            now = datetime.now(timezone.utc)
            if self.is_cache_fresh(now):
                cached_result = self.cached_result
                if cached_result is not None:
                    return cached_result
            if self.is_retry_delayed(now):
                return self.get_stale_result()

            try:
                self.cached_result = self.build_fresh_result(now)
                self.next_refresh_at = None
                return self.cached_result
            except Exception as error:
                # 日志只记录异常类型，避免泄露 URL、代理或帖子内容。
                LOGGER.warning("RSS refresh failed: %s", type(error).__name__)
                self.next_refresh_at = now + timedelta(
                    seconds=self.settings.failure_retry_seconds
                )
                if self.cached_result is None:
                    raise FeedServiceError("RSS 暂时不可用") from error
                return self.get_stale_result()


class AggregateFeedService:
    """合并多个板块来源并按发布时间排序生成单一 RSS。"""

    def __init__(
        self,
        settings: Settings,
        feed_title: str,
        source_url: str,
        public_feed_url: str,
        children: list[FeedService],
    ) -> None:
        """初始化聚合服务。

        参数：
            settings: 已校验的运行时配置。
            feed_title: 聚合 RSS 标题。
            source_url: 聚合 RSS 的频道链接。
            public_feed_url: 对外 RSS 地址。
            children: 参与聚合的板块服务。
        返回值：
            无。
        """
        self.settings = settings
        self.feed_title = feed_title
        self.source_url = source_url
        self.public_feed_url = public_feed_url
        self.children = children

    @staticmethod
    def sort_items(items: list[FeedItem]) -> list[FeedItem]:
        """按发布时间倒序排列，缺失时间排最后。

        参数：
            items: 待排序条目。
        返回值：
            排序后的新列表。
        """
        return sorted(
            items,
            key=lambda item: (
                item.published_at is not None,
                item.published_at or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )

    def get_feed(self) -> FeedResult:
        """刷新所有子来源并生成合并 RSS，单源失败时跳过。

        参数：
            无。
        返回值：
            合并后的 FeedResult。
        """
        child_results = []
        for child in self.children:
            try:
                child_results.append(child.get_feed())
            except FeedServiceError:
                # 单个来源不可用时继续输出其余来源，避免聚合整体失效。
                LOGGER.warning("Aggregate child skipped: %s", type(child).__name__)
        if not child_results:
            raise FeedServiceError("RSS 暂时不可用")

        seen_thread_ids: set[str] = set()
        merged_items: list[FeedItem] = []
        for result in child_results:
            for item in result.items:
                if item.thread_id in seen_thread_ids:
                    continue
                seen_thread_ids.add(item.thread_id)
                merged_items.append(item)

        ordered_items = self.sort_items(merged_items)[: self.settings.max_feed_items]
        now = datetime.now(timezone.utc)
        content = build_rss(
            ordered_items,
            self.feed_title,
            self.source_url,
            self.public_feed_url,
            now,
        )
        return FeedResult(
            content=content,
            is_stale=any(result.is_stale for result in child_results),
            generated_at=now,
            items=ordered_items,
        )
