"""验证健康检查、多 RSS 路由和主机级限速器复用。"""

from datetime import datetime, timezone

from app.config import Settings
from app.models import FeedResult
from app.server import create_app, create_feed_services


class StaticFeedService:
    """返回固定 RSS 的测试服务。"""

    def __init__(self, is_stale: bool = False) -> None:
        """初始化固定缓存状态。

        参数：
            is_stale: 是否模拟陈旧缓存。
        返回值：
            无。
        """
        self.is_stale = is_stale

    def get_feed(self) -> FeedResult:
        """返回固定且非陈旧的最小 RSS。

        参数：
            无。
        返回值：
            FeedResult 测试对象。
        """
        return FeedResult(
            content=b"<?xml version='1.0'?><rss version='2.0'><channel/></rss>",
            is_stale=self.is_stale,
            generated_at=datetime.now(timezone.utc),
        )


def build_test_settings() -> Settings:
    """创建不依赖外部环境的服务器测试配置。

    参数：
        无。
    返回值：
        合法的测试 Settings。
    """
    return Settings(
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


def test_caoliu_digest_returns_snapshot_or_404(monkeypatch, tmp_path) -> None:
    """验证精华快照端点：文件存在返回 RSS，缺失返回 404。

    参数：
        monkeypatch: pytest 环境变量隔离工具。
        tmp_path: 临时目录。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    snapshot = tmp_path / "digest.xml"
    app = create_app(build_test_settings())

    response_missing = app.test_client().get("/rss/caoliu-digest.xml")
    assert response_missing.status_code == 404

    snapshot.write_bytes(b"<?xml version='1.0'?><rss version='2.0'><channel/></rss>")
    monkeypatch.setenv("CAOLIU_DIGEST_FILE", str(snapshot))
    response_ready = app.test_client().get("/rss/caoliu-digest.xml")
    assert response_ready.status_code == 200
    assert response_ready.content_type == "application/rss+xml; charset=utf-8"
    assert response_ready.headers["Cache-Control"] == "public, max-age=3600"


def test_healthz_returns_ok_without_upstream_request() -> None:
    """验证健康检查仅反映进程状态，不触发目标站点抓取。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    app = create_app(build_test_settings())
    response = app.test_client().get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_create_app_registers_all_routes() -> None:
    """验证论坛 A 别名、论坛 B 聚合与三个板块端点均可访问。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    routes = (
        "/rss.xml",
        "/rss/forum-a.xml",
        "/rss/forum-b.xml",
        "/rss/forum-b-highlights.xml",
        "/rss/forum-b-fid-19.xml",
        "/rss/forum-b-fid-21.xml",
        "/rss/forum-b-fid-33.xml",
    )
    services = {route: StaticFeedService() for route in routes}
    app = create_app(build_test_settings(), services)

    for route in routes:
        response = app.test_client().get(route)
        assert response.status_code == 200
        assert response.content_type == "application/rss+xml; charset=utf-8"


def test_create_feed_services_reuses_fetcher_per_hostname() -> None:
    """验证三个 论坛 B 来源共享限速器且不阻塞 论坛 A 来源。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    services = create_feed_services(build_test_settings())
    discuz_fetchers = {
        id(service.fetcher)
        for route, service in services.items()
        if route.startswith("/rss/forum-b-fid-")
    }

    assert len(discuz_fetchers) == 1
    assert id(services["/rss.xml"].fetcher) not in discuz_fetchers


def test_create_feed_services_binds_correct_sources_and_parsers() -> None:
    """验证每个路由绑定正确 fid、公开链接、别名和 论坛 B 解析器。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    services = create_feed_services(build_test_settings())

    for fid in ("19", "21", "33"):
        route = f"/rss/forum-b-fid-{fid}.xml"
        service = services[route]
        assert service.source.source_url.endswith(f"fid={fid}")
        assert service.source.public_feed_url.endswith(route)
        assert service.parser.func.__name__ == "parse_forum_b_items"
    assert services["/rss.xml"].parser.func.__name__ == "parse_forum_a_items"
    assert services["/rss/forum-b-highlights.xml"].parser.__name__ == "parse_forum_b_home_items"
    assert services["/rss/forum-a.xml"] is services["/rss.xml"]


def test_aggregate_route_merges_three_discuz_sections() -> None:
    """验证 论坛 B 聚合端点合并三个指定板块。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    services = create_feed_services(build_test_settings())
    aggregate = services["/rss/forum-b.xml"]

    assert len(aggregate.children) == 3
    assert [child.source.key for child in aggregate.children] == [
        "forum-b-fid-19",
        "forum-b-fid-21",
        "forum-b-fid-33",
    ]


def test_stale_feed_uses_short_retry_cache() -> None:
    """验证陈旧 RSS 不会继续使用完整正常缓存周期。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    services = {"/rss.xml": StaticFeedService(is_stale=True)}
    app = create_app(build_test_settings(), services)

    response = app.test_client().get("/rss.xml")

    assert response.headers["X-Feed-Stale"] == "true"
    assert response.headers["Cache-Control"] == "public, max-age=60"


def test_feed_handler_supports_etag_conditional_requests() -> None:
    """验证客户端带 If-None-Match 时可复用未变化的 RSS 内容。"""
    services = {"/rss.xml": StaticFeedService()}
    app = create_app(build_test_settings(), services)
    client = app.test_client()

    first_response = client.get("/rss.xml")
    etag = first_response.headers.get("ETag")

    assert first_response.status_code == 200
    assert etag and etag.startswith('"')
    assert first_response.headers.get("Last-Modified")

    not_modified = client.get(
        "/rss.xml", headers={"If-None-Match": etag}
    )

    assert not_modified.status_code == 304
    assert not_modified.data == b""
    assert not_modified.headers["ETag"] == etag
    assert not_modified.headers["Cache-Control"] == "public, max-age=600"


def test_feed_handler_does_not_cache_initial_failure() -> None:
    """验证没有缓存时的 502 不会被代理缓存。"""

    class FailingService:
        """模拟尚未生成过内容的来源。"""

        def get_feed(self) -> FeedResult:
            """始终抛出统一的暂不可用错误。"""
            from app.service import FeedServiceError

            raise FeedServiceError("unavailable")

    app = create_app(build_test_settings(), {"/rss.xml": FailingService()})

    response = app.test_client().get("/rss.xml")

    assert response.status_code == 502
    assert response.headers["Cache-Control"] == "no-store"
