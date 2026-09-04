"""验证静态 feed 构建脚本的模式、配置与并发行为。"""

import sys
from pathlib import Path

import pytest

from app.fetcher import ForumFetcher
from app.models import FeedItem, FeedSource
from scripts import build_feeds


def make_source(
    key: str,
    url: str,
    parser_kind: str = "links",
    link_pattern: str = r"^/album/",
) -> FeedSource:
    """构造最小图站来源，减少并发测试的样板配置。"""
    return FeedSource(
        key=key,
        source_url=url,
        feed_title=key,
        route=f"/gallery/{key}.xml",
        public_feed_url=f"https://rss.example.com/gallery/{key}.xml",
        parser_kind=parser_kind,
        link_pattern=link_pattern,
    )


def test_normalize_public_base_url_rejects_unsafe_values() -> None:
    """公开地址必须是无凭据、无查询参数的 HTTP(S) URL。"""
    assert (
        build_feeds.normalize_public_base_url("https://rss.example.com///")
        == "https://rss.example.com"
    )
    with pytest.raises(ValueError, match=r"完整 HTTP\(S\) URL"):
        build_feeds.normalize_public_base_url("rss.example.com")
    with pytest.raises(ValueError, match="查询参数"):
        build_feeds.normalize_public_base_url("https://rss.example.com/?token=x")
    with pytest.raises(ValueError, match="用户名或密码"):
        build_feeds.normalize_public_base_url("https://user:pass@rss.example.com")


def test_validate_gallery_sources_rejects_duplicates_and_bad_regex() -> None:
    """重复 key 与无效正则必须在抓取前被拒绝。"""
    with pytest.raises(ValueError, match="key 重复"):
        build_feeds.validate_gallery_sources(
            [
                make_source("same", "https://a.example.com"),
                make_source("same", "https://b.example.com"),
            ]
        )

    with pytest.raises(ValueError, match="link_pattern 无效"):
        build_feeds.validate_gallery_sources(
            [make_source("bad", "https://a.example.com", link_pattern="[")]
        )

    with pytest.raises(ValueError, match=r"完整 HTTP\(S\) URL"):
        build_feeds.validate_gallery_sources([make_source("bad-url", "//a.example.com")])

    with pytest.raises(ValueError, match="key 只能包含"):
        build_feeds.validate_gallery_sources(
            [make_source("../escape", "https://a.example.com")]
        )


def test_get_section_names_requires_all_forum_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    """论坛 B 的三个板块名缺失或为空时应立即报错。"""
    monkeypatch.setenv("SECTIONS_B", "板块一;板块二")
    with pytest.raises(ValueError, match="3 个非空板块名"):
        build_feeds.get_section_names()


def test_parse_int_env_enforces_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """分页上限环境变量必须是满足下界的整数。"""
    monkeypatch.setenv("B_PAGE_LIMIT", "0")
    with pytest.raises(ValueError, match="大于等于 1"):
        build_feeds.parse_int_env("B_PAGE_LIMIT", 5, minimum=1)

    monkeypatch.setenv("B_PAGE_LIMIT", "not-a-number")
    with pytest.raises(ValueError, match="必须是整数"):
        build_feeds.parse_int_env("B_PAGE_LIMIT", 5, minimum=1)

    monkeypatch.setenv("BUILD_DIGEST_B", "maybe")
    with pytest.raises(ValueError, match="必须是布尔值"):
        build_feeds.parse_bool_env("BUILD_DIGEST_B")


def test_parse_gallery_source_auto_detects_rss() -> None:
    """auto 模式遇到 RSS XML 时应使用直通解析器。"""
    source = make_source("auto", "https://a.example.com/feed", parser_kind="auto")
    html = """<rss version='2.0'><channel><item><title>一</title>
    <link>/album/1</link><guid>1</guid></item></channel></rss>"""

    items = build_feeds.parse_gallery_source(source, html)

    assert len(items) == 1
    assert items[0].thread_id == "rss:1"


def test_parse_gallery_source_auto_falls_back_to_links() -> None:
    """auto 模式遇到普通首页时应继续解析图集链接。"""
    source = make_source("auto", "https://a.example.com/", parser_kind="auto")
    html = "<a href='/album/1'>一个图集标题</a>"

    items = build_feeds.parse_gallery_source(source, html)

    assert [item.link for item in items] == ["https://a.example.com/album/1"]


def test_build_gallery_fetchers_share_by_host_and_isolate_hosts() -> None:
    """同主机来源复用锁，不同主机来源使用独立下载器以实现并行。"""
    template = ForumFetcher(
        min_interval_seconds=10,
        timeout_seconds=20,
        user_agent="test-agent",
    )
    sources = [
        make_source("one", "https://A.example.com/one"),
        make_source("two", "https://a.example.com/two"),
        make_source("three", "https://b.example.com/three"),
    ]

    fetchers = build_feeds.build_gallery_fetchers(template, sources)

    assert set(fetchers) == {"a.example.com", "b.example.com"}
    assert fetchers["a.example.com"] is not fetchers["b.example.com"]
    assert fetchers["a.example.com"].min_interval_seconds == 10
    assert fetchers["a.example.com"].timeout_seconds == 20


def test_only_forum_reports_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """论坛独立构建必须输出来源失败，避免 CI 静默发布旧文件。"""
    monkeypatch.setattr(
        build_feeds,
        "build_forum_feeds",
        lambda fetcher, output_dir, public_base_url: ["forum-a:FeedFetchError"],
    )
    monkeypatch.setattr(build_feeds, "ForumFetcher", lambda **kwargs: object())
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["build_feeds.py", str(tmp_path), "--only-forum"]
    )

    build_feeds.main()

    assert "WARNINGS (non-fatal): forum-a:FeedFetchError" in capsys.readouterr().out


def test_build_gallery_feeds_passes_host_specific_fetchers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """图站线程应收到按主机隔离的下载器，而不是共享全局锁。"""
    sources = [
        make_source("one", "https://one.example.com/"),
        make_source("two", "https://two.example.com/"),
    ]
    observed_fetchers = {}

    def fake_fetch_single(source, fetcher, output_dir, public_base_url):
        """记录线程收到的下载器并返回一个可合并条目。"""
        observed_fetchers[source.key] = fetcher
        item = FeedItem(
            thread_id=f"gallery:{source.key}",
            title=source.key,
            link=source.source_url,
            author=None,
            published_at=None,
        )
        return source, [item], None

    monkeypatch.setattr(build_feeds, "load_validated_gallery_sources", lambda _: tuple(sources))
    monkeypatch.setattr(build_feeds, "fetch_single_gallery", fake_fetch_single)
    template = ForumFetcher(10, 20, "test-agent")

    failures = build_feeds.build_gallery_feeds(
        template, tmp_path, "https://rss.example.com"
    )

    assert failures == []
    assert observed_fetchers["one"] is not observed_fetchers["two"]
    assert (tmp_path / "gallery.xml").exists()
