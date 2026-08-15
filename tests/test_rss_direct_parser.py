"""验证 RSS/Atom 直通源解析的元数据提取、去重和边界行为。"""

from datetime import datetime, timezone

from app.parser import parse_rss_items


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <item>
      <title>Collection A</title>
      <link>https://example.com/threads/collection-a.111/</link>
      <guid>https://example.com/threads/collection-a.111/</guid>
      <pubDate>Sun, 04 Jan 2026 12:00:00 +0000</pubDate>
      <dc:creator>ArtistA</dc:creator>
    </item>
    <item>
      <title>Collection B</title>
      <link>https://example.com/threads/collection-b.222/</link>
      <guid>https://example.com/threads/collection-b.222/</guid>
      <pubDate>Mon, 15 Jul 2025 08:30:00 +0000</pubDate>
    </item>
    <item>
      <title>Duplicate</title>
      <link>https://example.com/threads/collection-a.111/</link>
      <guid>https://example.com/threads/collection-a.111/</guid>
    </item>
  </channel>
</rss>
"""

SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Atom Entry A</title>
    <link href="https://example.com/atom/a/"/>
    <id>tag:example.com,2026:atom-a</id>
    <updated>2026-01-04T12:00:00Z</updated>
    <author><name>ArtistA</name></author>
  </entry>
</feed>
"""


def test_parse_rss_items_extracts_metadata() -> None:
    """验证 RSS 条目标题、链接、作者、时间和去重行为。"""
    items = parse_rss_items(SAMPLE_RSS, "https://example.com/", 100)

    assert len(items) == 2
    assert items[0].title == "Collection A"
    assert items[0].author == "ArtistA"
    assert items[0].published_at == datetime(2026, 1, 4, 12, 0, tzinfo=timezone.utc)
    assert items[1].published_at == datetime(2025, 7, 15, 8, 30, tzinfo=timezone.utc)


def test_parse_rss_items_handles_atom_entries() -> None:
    """验证 Atom entry 与命名空间元素也能被解析。"""
    items = parse_rss_items(SAMPLE_ATOM, "https://example.com/", 100)

    assert len(items) == 1
    assert items[0].title == "Atom Entry A"
    assert items[0].link == "https://example.com/atom/a/"
    assert items[0].author == "ArtistA"
    assert items[0].published_at == datetime(2026, 1, 4, 12, 0, tzinfo=timezone.utc)


def test_parse_rss_items_respects_max_items() -> None:
    """验证条目数量上限生效。"""
    items = parse_rss_items(SAMPLE_RSS, "https://example.com/", 1)

    assert len(items) == 1
    assert items[0].title == "Collection A"


def test_parse_rss_items_handles_invalid_input() -> None:
    """验证空文本、非法 XML 和零上限时返回空列表。"""
    assert parse_rss_items("", "https://example.com/", 10) == []
    assert parse_rss_items("<html>not rss</html>", "https://example.com/", 10) == []
    assert parse_rss_items(SAMPLE_RSS, "https://example.com/", 0) == []
