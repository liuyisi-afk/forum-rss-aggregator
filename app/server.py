"""提供多个 RSS 来源和健康检查 HTTP 接口。"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from functools import partial
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request

from app.config import (
    FORUM_B_INDEX_URL,
    Settings,
    get_feed_sources,
    get_settings,
)
from app.feed import build_opml
from app.fetcher import ForumFetcher
from app.models import FeedItem, FeedResult, FeedSource
from app.parser import (
    parse_forum_b_home_items,
    parse_forum_b_items,
    parse_forum_a_items,
    parse_link_gallery_items,
    parse_mzt_api_items,
    parse_rss_items,
)
from app.service import AggregateFeedService, FeedParser, FeedService, FeedServiceError


class FeedProvider(Protocol):
    """约束路由处理器所需的最小 RSS 服务接口。"""

    def get_feed(self) -> FeedResult:
        """返回 RSS 内容与缓存状态。

        参数：
            无。
        返回值：
            FeedResult。
        """
        ...


def parse_auto_gallery_items(
    html: str,
    base_url: str,
    max_items: int,
    *,
    link_pattern: str = "",
    link_selector: str = "",
    parent_selector: str = "",
) -> list[FeedItem]:
    """解析自动图站来源，RSS 无条目时回退到首页链接。

    参数：
        html: 图站返回的页面文本。
        base_url: 来源页面地址。
        max_items: 最多保留的条目数。
        link_pattern: 可选的图集链接正则。
        link_selector: 可选的图集链接 CSS 选择器。
        parent_selector: 可选的祖先过滤选择器。
    返回值：
        RSS 或链接解析得到的条目列表。
    """
    rss_items = parse_rss_items(html, base_url, max_items)
    if rss_items:
        return rss_items
    return parse_link_gallery_items(
        html,
        base_url,
        max_items,
        link_pattern=link_pattern,
        link_selector=link_selector,
        parent_selector=parent_selector,
    )


def select_parser(source: FeedSource, keep_image_posts_only: bool) -> FeedParser:
    """根据来源配置选择解析器，图站显式配置优先于路径推断。

    参数：
        source: RSS 来源配置。
        keep_image_posts_only: 是否只保留带图帖子。
    返回值：
        绑定过滤开关的解析函数。
    """
    if source.parser_kind == "auto" and source.route.startswith("/gallery/"):
        return partial(
            parse_auto_gallery_items,
            link_pattern=source.link_pattern,
            link_selector=source.link_selector,
            parent_selector=source.parent_selector,
        )

    parser_kind = source.parser_kind
    if parser_kind == "rss":
        return parse_rss_items
    if parser_kind == "mzt":
        return parse_mzt_api_items
    if parser_kind == "links":
        return partial(
            parse_link_gallery_items,
            link_pattern=source.link_pattern,
            link_selector=source.link_selector,
            parent_selector=source.parent_selector,
        )

    path = urlparse(source.source_url).path.rstrip("/")
    if path.endswith("index.php"):
        return parse_forum_b_home_items
    if path.endswith("forumdisplay.php"):
        return partial(
            parse_forum_b_items, keep_image_posts_only=keep_image_posts_only
        )
    return partial(parse_forum_a_items, keep_image_posts_only=keep_image_posts_only)


def create_fetcher(settings: Settings) -> ForumFetcher:
    """创建一个主机级共享的串行限速下载器。

    参数：
        settings: 已校验的全局运行时配置。
    返回值：
        ForumFetcher 实例。
    """
    return ForumFetcher(
        min_interval_seconds=settings.min_fetch_interval_seconds,
        timeout_seconds=settings.request_timeout_seconds,
        user_agent=settings.user_agent,
        max_response_bytes=settings.max_response_bytes,
    )


def create_feed_services(settings: Settings) -> dict[str, FeedProvider]:
    """为每个端点创建独立缓存服务，并按主机复用限速器。

    参数：
        settings: 已校验的全局运行时配置。
    返回值：
        以路由为键的 FeedProvider 字典。
    """
    fetchers_by_host: dict[str, ForumFetcher] = {}
    services: dict[str, FeedService] = {}
    for source in get_feed_sources(settings):
        hostname = urlparse(source.source_url).netloc.lower()
        fetcher = fetchers_by_host.get(hostname)
        if fetcher is None:
            fetcher = create_fetcher(settings)
            fetchers_by_host[hostname] = fetcher
        services[source.route] = FeedService(
            settings=settings,
            source=source,
            fetcher=fetcher,
            parser=select_parser(source, settings.keep_image_posts_only),
        )

    # 论坛 A：/rss.xml 与 /rss/forum-a.xml 指向同一服务
    forum_a_service = services["/rss.xml"]
    providers: dict[str, FeedProvider] = {
        "/rss.xml": forum_a_service,
        "/rss/forum-a.xml": forum_a_service,
    }

    # 论坛 B：三个板块独立端点 + 合并聚合端点
    forum_b_routes = sorted(
        route for route in services if route.startswith("/rss/forum-b-fid-")
    )
    public_base_url = settings.public_base_url.rstrip("/")
    providers["/rss/forum-b.xml"] = AggregateFeedService(
        settings=settings,
        feed_title="论坛 B - 板块一/板块二/板块三",
        source_url=os.getenv("FORUM_B_INDEX_URL", FORUM_B_INDEX_URL),
        public_feed_url=f"{public_base_url}/rss/forum-b.xml",
        children=[services[route] for route in forum_b_routes],
    )
    for route, feed_service in services.items():
        providers.setdefault(route, feed_service)

    # 图站：独立订阅 + 合并聚合端点
    gallery_routes = sorted(
        route for route in services if route.startswith("/gallery/")
    )
    if gallery_routes:
        providers["/gallery.xml"] = AggregateFeedService(
            settings=settings,
            feed_title="精选图站聚合",
            source_url=public_base_url,
            public_feed_url=f"{public_base_url}/gallery.xml",
            children=[services[route] for route in gallery_routes],
        )
    return providers


def create_feed_handler(
    feed_service: FeedProvider,
    cache_seconds: int,
    failure_retry_seconds: int,
) -> Callable[[], Response | tuple[Response, int]]:
    """创建一个绑定指定来源的 RSS 响应处理函数。

    参数：
        feed_service: 当前路由使用的 RSS 服务。
        cache_seconds: 客户端缓存秒数。
        failure_retry_seconds: 陈旧缓存的客户端重试秒数。
    返回值：
        Flask 可调用的视图函数。
    """

    def handle_feed() -> Response | tuple[Response, int]:
        """返回 RSS XML，并在上游不可用时返回固定错误。

        参数：
            无。
        返回值：
            RSS XML 或 HTTP 502 JSON。
        """
        try:
            result = feed_service.get_feed()
        except FeedServiceError:
            response = jsonify({"error": "feed_temporarily_unavailable"})
            response.headers["Cache-Control"] = "no-store"
            return response, 502

        return make_feed_response(result, cache_seconds, failure_retry_seconds)

    return handle_feed


def build_content_etag(content: bytes) -> str:
    """根据响应字节生成稳定的强 ETag。

    参数：
        content: 要发送的响应正文。
    返回值：
        带双引号的 ETag 值。
    """
    digest = hashlib.sha256(content).hexdigest()
    return f'"{digest}"'


def format_http_date(value: datetime) -> str:
    """将时间规范化为 HTTP-date 格式。

    参数：
        value: 生成或修改时间。
    返回值：
        GMT 时区的 HTTP-date 字符串。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return format_datetime(normalized, usegmt=True)


def is_cache_not_modified(etag: str, last_modified: str) -> bool:
    """判断请求条件是否允许返回 304。

    参数：
        etag: 当前响应的 ETag。
        last_modified: 当前响应的 Last-Modified 值。
    返回值：
        条件匹配时返回 True。
    """
    if_none_match = request.headers.get("If-None-Match", "")
    if if_none_match:
        for candidate in if_none_match.split(","):
            normalized = candidate.strip()
            if normalized == "*" or normalized == etag:
                return True
            if normalized.startswith("W/") and normalized[2:] == etag:
                return True
        return False

    if_modified_since = request.headers.get("If-Modified-Since")
    if not if_modified_since:
        return False
    try:
        modified_since = parsedate_to_datetime(if_modified_since)
    except (TypeError, ValueError, OverflowError):
        return False
    if modified_since.tzinfo is None:
        modified_since = modified_since.replace(tzinfo=timezone.utc)
    try:
        current = parsedate_to_datetime(last_modified)
    except (TypeError, ValueError, OverflowError):
        return False
    return current <= modified_since.astimezone(timezone.utc)


def make_feed_response(
    result: FeedResult, cache_seconds: int, failure_retry_seconds: int
) -> Response:
    """创建带条件缓存头的 RSS 响应。

    参数：
        result: RSS 内容及缓存状态。
        cache_seconds: 新鲜内容的客户端缓存秒数。
        failure_retry_seconds: 陈旧内容的客户端重试秒数。
    返回值：
        RSS 正常响应或 304 Not Modified 响应。
    """
    etag = build_content_etag(result.content)
    last_modified = format_http_date(result.generated_at)
    max_age = failure_retry_seconds if result.is_stale else cache_seconds
    headers = {
        "Cache-Control": f"public, max-age={max_age}",
        "ETag": etag,
        "Last-Modified": last_modified,
        "X-Feed-Stale": "true" if result.is_stale else "false",
    }
    if is_cache_not_modified(etag, last_modified):
        response = Response(
            status=304, content_type="application/rss+xml; charset=utf-8"
        )
    else:
        response = Response(
            result.content, content_type="application/rss+xml; charset=utf-8"
        )
    response.headers.update(headers)
    return response


def create_app(
    settings: Settings | None = None,
    feed_services: dict[str, FeedProvider] | None = None,
) -> Flask:
    """创建 Flask 应用并注册健康检查、OPML 与全部 RSS 路由。

    参数：
        settings: 可选的测试配置；缺失时从环境读取。
        feed_services: 可选的测试服务字典。
    返回值：
        配置完成的 Flask 应用。
    """
    runtime_settings = settings or get_settings()
    # 允许调用方显式传入空字典，便于健康检查或无来源部署的测试/探针场景。
    services = (
        feed_services
        if feed_services is not None
        else create_feed_services(runtime_settings)
    )
    app = Flask(__name__)
    app.extensions["feed_services"] = services

    @app.get("/feeds.opml")
    def feeds_opml() -> tuple[Response, int]:
        """输出全部订阅的 OPML 列表，供阅读器一键导入。

        参数：
            无。
        返回值：
            OPML XML 响应。
        """
        entries: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for feed_service in services.values():
            source = getattr(feed_service, "source", None)
            title = (
                getattr(source, "feed_title", None)
                or getattr(feed_service, "feed_title", None)
                or "RSS Feed"
            )
            url = (
                getattr(source, "public_feed_url", None)
                or getattr(feed_service, "public_feed_url", "")
            )
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            entries.append((title, url))

        response = Response(
            build_opml(entries), content_type="text/x-opml; charset=utf-8"
        )
        return response, 200

    @app.get("/healthz")
    def healthz() -> tuple[Response, int]:
        """返回进程健康状态且不触发任何上游抓取。

        参数：
            无。
        返回值：
            JSON 响应及 HTTP 状态码。
        """
        return jsonify({"status": "ok"}), 200

    @app.get("/rss/caoliu-digest.xml")
    def caoliu_digest() -> tuple[Response, int]:
        """返回论坛 A 精华帖快照 RSS（由定时快照任务生成，图片过期帖也保留）。

        参数：
            无。
        返回值：
            快照 RSS XML；快照尚未生成时返回 404。
        """
        configured_path = os.getenv("CAOLIU_DIGEST_FILE")
        snapshot_paths = (
            [Path(configured_path)]
            if configured_path
            else [
                # Docker/Compose 共享卷的标准路径。
                Path("/var/rss-feed/caoliu-digest.xml"),
                # 兼容旧版 systemd/容器配置，避免升级时丢失已有快照。
                Path("/opt/rss-feed/var/caoliu-digest.xml"),
            ]
        )
        content = None
        selected_path = None
        for snapshot_path in snapshot_paths:
            try:
                content = snapshot_path.read_bytes()
                selected_path = snapshot_path
                break
            except (OSError, ValueError):
                continue
        if content is None or selected_path is None:
            return jsonify({"error": "snapshot_not_ready"}), 404

        try:
            generated_at = datetime.fromtimestamp(
                selected_path.stat().st_mtime, timezone.utc
            )
        except OSError:
            generated_at = datetime.now(timezone.utc)
        result = FeedResult(
            content=content,
            is_stale=False,
            generated_at=generated_at,
        )
        return make_feed_response(result, 3600, 3600)

    for route, feed_service in services.items():
        endpoint = "feed_" + route.strip("/").replace("/", "_").replace(".", "_")
        app.add_url_rule(
            route,
            endpoint=endpoint,
            view_func=create_feed_handler(
                feed_service,
                runtime_settings.cache_seconds,
                runtime_settings.failure_retry_seconds,
            ),
            methods=["GET"],
        )
    return app


app = create_app()


def run_development_server() -> None:
    """仅供本地调试启动 Flask 内置服务器。

    参数：
        无。
    返回值：
        无；函数持续运行直至进程终止。
    """
    settings = get_settings()
    app.run(host="127.0.0.1", port=settings.port)


if __name__ == "__main__":
    run_development_server()
