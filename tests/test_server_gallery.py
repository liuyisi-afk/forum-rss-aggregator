"""验证图站来源在服务层生成独立与聚合路由。"""

from functools import partial

from app.config import Settings
from app.models import FeedSource
from app.server import create_feed_services


def build_test_settings() -> Settings:
    """构造不含外部依赖的测试配置。"""
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
        gallery_sources=(
            FeedSource(
                key="one",
                source_url="https://a.example.com/rss.xml",
                feed_title="站点一",
                route="/gallery/one.xml",
                public_feed_url="http://127.0.0.1:28888/gallery/one.xml",
                parser_kind="rss",
            ),
            FeedSource(
                key="two",
                source_url="https://b.example.com/",
                feed_title="站点二",
                route="/gallery/two.xml",
                public_feed_url="http://127.0.0.1:28888/gallery/two.xml",
                parser_kind="links",
                link_pattern="^/album/",
            ),
        ),
    )


def test_create_feed_services_registers_gallery_routes() -> None:
    """验证图站独立路由、聚合路由与解析器绑定正确。"""
    services = create_feed_services(build_test_settings())

    assert "/gallery/one.xml" in services
    assert "/gallery/two.xml" in services
    assert "/gallery.xml" in services
    assert services["/gallery/one.xml"].parser.__name__ == "parse_rss_items"
    assert isinstance(services["/gallery/two.xml"].parser, partial)
    assert services["/gallery/two.xml"].parser.keywords["link_pattern"] == "^/album/"
