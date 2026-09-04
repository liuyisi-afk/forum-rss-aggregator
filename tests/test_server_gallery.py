"""验证图站来源在服务层生成独立与聚合路由。"""

from functools import partial

from app.config import Settings
from app.models import FeedSource
from app.parser import parse_mzt_api_items
from app.server import create_feed_services, select_parser


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


def test_select_parser_dispatches_mzt_sources() -> None:
    """验证服务端将显式 mzt 来源分发给 JSON API 解析器。"""
    source = FeedSource(
        key="mzt",
        source_url="https://mzt.example.com/urls?page=1&pageSize=50",
        feed_title="妹子图",
        route="/gallery/mzt.xml",
        public_feed_url="https://rss.example.com/gallery/mzt.xml",
        parser_kind="mzt",
    )

    assert select_parser(source, keep_image_posts_only=False) is parse_mzt_api_items


def test_select_parser_auto_gallery_falls_back_from_rss_path() -> None:
    """验证 auto 图站即使 URL 像 RSS 也会回退到链接解析。"""
    source = FeedSource(
        key="auto",
        source_url="https://gallery.example.com/feed",
        feed_title="自动图站",
        route="/gallery/auto.xml",
        public_feed_url="https://rss.example.com/gallery/auto.xml",
        parser_kind="auto",
        link_pattern="^/album/",
    )

    parser = select_parser(source, keep_image_posts_only=False)
    items = parser(
        "<a href='/album/1'>一个图集标题</a>", source.source_url, 10
    )

    assert [item.link for item in items] == [
        "https://gallery.example.com/album/1"
    ]
