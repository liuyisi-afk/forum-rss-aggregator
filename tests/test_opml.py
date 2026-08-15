"""验证 OPML 订阅列表输出。"""

from app.feed import build_opml


def test_build_opml_outputs_outline_entries() -> None:
    """验证 OPML 包含全部订阅标题与地址。"""
    content = build_opml(
        [
            ("订阅 A", "https://rss.example.com/gallery/a.xml"),
            ("订阅 B", "https://rss.example.com/gallery/b.xml"),
        ]
    )
    text = content.decode("utf-8")

    assert 'version="2.0"' in text
    assert 'xmlUrl="https://rss.example.com/gallery/a.xml"' in text
    assert 'title="订阅 B"' in text
    assert text.count("<outline") == 2
