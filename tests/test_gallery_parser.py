"""验证图站列表解析的链接过滤、去重和日期提取。"""

from datetime import datetime, timezone

from app.parser import parse_link_gallery_items


SAMPLE_HTML = """
<nav>
  <a href="/categories">Categories</a>
</nav>
<article>
  <h3 class="entry-title">
    <a href="/album/111">[Coser] Set A [20P] @Model 20</a>
  </h3>
</article>
<article>
  <a href="https://example.com/album/222">2026-08-13</a>
  <a href="https://example.com/album/222">[Coser] Set B [10P] @Model 10</a>
</article>
"""


def test_parse_link_gallery_items_extracts_and_deduplicates() -> None:
    """验证只保留图集链接、跳过日期占位并去重。"""
    items = parse_link_gallery_items(
        SAMPLE_HTML,
        "https://example.com/",
        100,
        link_pattern=r"(?:^|/)album/\d+$",
    )

    assert [item.title for item in items] == [
        "[Coser] Set A [20P] @Model 20",
        "[Coser] Set B [10P] @Model 10",
    ]
    assert items[0].link == "https://example.com/album/111"


def test_parse_link_gallery_items_uses_parent_selector() -> None:
    """验证祖先选择器可以排除导航和卡片外链接。"""
    items = parse_link_gallery_items(
        SAMPLE_HTML,
        "https://example.com/",
        100,
        link_pattern=r"^/album/\d+$",
        parent_selector="h3.entry-title",
    )

    assert [item.title for item in items] == ["[Coser] Set A [20P] @Model 20"]


def test_parse_link_gallery_items_extracts_date() -> None:
    """验证可从卡片文本中解析日期并转换为 UTC。"""
    items = parse_link_gallery_items(
        SAMPLE_HTML,
        "https://example.com/",
        100,
        link_pattern=r"(?:^|/)album/\d+$",
    )

    assert items[1].published_at == datetime(
        2026, 8, 12, 16, 0, tzinfo=timezone.utc
    )


def test_parse_link_gallery_items_handles_invalid_input() -> None:
    """验证空 HTML 和零上限时返回空列表。"""
    assert parse_link_gallery_items("", "https://example.com/", 10) == []
    assert parse_link_gallery_items(SAMPLE_HTML, "https://example.com/", 0) == []
