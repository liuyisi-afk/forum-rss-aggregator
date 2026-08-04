"""验证 RSS XML 输出不包含正文并保持必要字段。"""

from datetime import datetime, timezone
from xml.etree import ElementTree

from app.feed import build_rss
from app.models import FeedItem


def test_build_rss_outputs_valid_metadata_only_feed() -> None:
    """验证 RSS 可解析且包含标题、链接、作者、时间和 GUID。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    generated_at = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    item = FeedItem(
        thread_id="123",
        title="示例标题",
        link="https://forum-a.example.com/htm_data/16/2607/123.html",
        author="作者甲",
        published_at=generated_at,
    )

    content = build_rss(
        [item],
        "测试 RSS",
        "https://forum-a.example.com/thread0806.php?fid=16",
        "https://rss.example.com/rss.xml",
        generated_at,
    )
    root = ElementTree.fromstring(content)

    assert root.tag == "rss"
    assert root.findtext("./channel/item/title") == "示例标题"
    assert root.findtext("./channel/item/guid") == "123"
    assert root.find("./channel/item/description") is None

