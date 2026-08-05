"""提供多个 RSS 来源和健康检查 HTTP 接口。"""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from flask import Flask, Response, jsonify

from app.config import FORUM_B_INDEX_URL, Settings, get_feed_sources, get_settings
from app.fetcher import ForumFetcher
from app.models import FeedResult, FeedSource
from app.parser import parse_forum_b_home_items, parse_forum_b_items, parse_forum_a_items
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


def select_parser(source: FeedSource, keep_image_posts_only: bool) -> FeedParser:
    """根据索引页路径结构选择解析器，与站点域名解耦。

    参数：
        source: RSS 来源配置。
        keep_image_posts_only: 是否只保留带图帖子。
    返回值：
        绑定过滤开关的解析函数。
    """
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
        source_url=os.getenv(
            "FORUM_B_INDEX_URL", FORUM_B_INDEX_URL
        ),
        public_feed_url=f"{public_base_url}/rss/forum-b.xml",
        children=[services[route] for route in forum_b_routes],
    )
    for route, feed_service in services.items():
        providers.setdefault(route, feed_service)
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
            return jsonify({"error": "feed_temporarily_unavailable"}), 502

        response = Response(
            result.content, content_type="application/rss+xml; charset=utf-8"
        )
        response.headers["X-Feed-Stale"] = "true" if result.is_stale else "false"
        max_age = failure_retry_seconds if result.is_stale else cache_seconds
        response.headers["Cache-Control"] = f"public, max-age={max_age}"
        return response

    return handle_feed


def create_app(
    settings: Settings | None = None,
    feed_services: dict[str, FeedProvider] | None = None,
) -> Flask:
    """创建 Flask 应用并注册健康检查及全部 RSS 路由。

    参数：
        settings: 可选的测试配置；缺失时从环境读取。
        feed_services: 可选的测试服务字典。
    返回值：
        配置完成的 Flask 应用。
    """
    runtime_settings = settings or get_settings()
    services = feed_services or create_feed_services(runtime_settings)
    app = Flask(__name__)
    app.extensions["feed_services"] = services

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
        snapshot_path = os.getenv(
            "CAOLIU_DIGEST_FILE", "/opt/rss-feed/var/caoliu-digest.xml"
        )
        try:
            content = Path(snapshot_path).read_bytes()
        except OSError:
            return jsonify({"error": "snapshot_not_ready"}), 404

        response = Response(
            content, content_type="application/rss+xml; charset=utf-8"
        )
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response, 200


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
