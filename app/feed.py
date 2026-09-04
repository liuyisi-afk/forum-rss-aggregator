"""将帖子元数据转换为 RSS 2.0 与 OPML 文档。"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from xml.etree import ElementTree

from app.models import FeedItem


DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
ElementTree.register_namespace("dc", DC_NAMESPACE)


def strip_invalid_xml_chars(value: str) -> str:
    """移除 XML 1.0 不允许的控制字符和孤立代理字符。

    参数：
        value: 待写入 XML 的文本。
    返回值：
        仅包含 XML 1.0 合法字符的文本。
    """
    if not isinstance(value, str):
        return ""
    valid_characters = []
    for character in value:
        codepoint = ord(character)
        is_valid = (
            codepoint in (0x9, 0xA, 0xD)
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        )
        if is_valid:
            valid_characters.append(character)
    return "".join(valid_characters)


def append_text_element(parent: ElementTree.Element, name: str, value: str) -> None:
    """添加文本子元素，集中处理 XML 元素赋值。

    参数：
        parent: 父 XML 元素。
        name: 子元素名称。
        value: 子元素文本。
    返回值：
        无。
    """
    element = ElementTree.SubElement(parent, name)
    element.text = strip_invalid_xml_chars(value)


def append_item(channel: ElementTree.Element, item: FeedItem) -> None:
    """向 RSS channel 添加一个不含正文的帖子条目。

    参数：
        channel: RSS channel 元素。
        item: 帖子元数据。
    返回值：
        无。
    """
    item_element = ElementTree.SubElement(channel, "item")
    append_text_element(item_element, "title", item.title)
    append_text_element(item_element, "link", item.link)
    guid = ElementTree.SubElement(item_element, "guid", {"isPermaLink": "false"})
    guid.text = strip_invalid_xml_chars(item.thread_id)

    if item.author:
        append_text_element(item_element, f"{{{DC_NAMESPACE}}}creator", item.author)
    if item.published_at:
        append_text_element(item_element, "pubDate", format_datetime(item.published_at))


def build_rss(
    items: list[FeedItem],
    feed_title: str,
    source_url: str,
    public_feed_url: str,
    generated_at: datetime | None = None,
) -> bytes:
    """构建 RSS 2.0 文档，仅包含索引页公开元数据。

    参数：
        items: 待输出的帖子列表。
        feed_title: 订阅源标题。
        source_url: 论坛索引页地址。
        public_feed_url: 对外 RSS 地址。
        generated_at: 可选的生成时间。
    返回值：
        带 XML 声明的 UTF-8 字节串。
    """
    build_time = generated_at or datetime.now(timezone.utc)
    rss = ElementTree.Element("rss", {"version": "2.0"})
    channel = ElementTree.SubElement(rss, "channel")
    append_text_element(channel, "title", feed_title)
    append_text_element(channel, "link", source_url)
    append_text_element(channel, "description", f"Metadata-only feed: {public_feed_url}")
    append_text_element(channel, "lastBuildDate", format_datetime(build_time))

    for item in items:
        append_item(channel, item)

    return ElementTree.tostring(rss, encoding="utf-8", xml_declaration=True)


def build_opml(entries: list[tuple[str, str]]) -> bytes:
    """构建 OPML 2.0 订阅列表，供阅读器一键导入。

    参数：
        entries: (订阅标题, 订阅地址) 列表。
    返回值：
        带 XML 声明的 UTF-8 字节串。
    """
    opml = ElementTree.Element("opml", {"version": "2.0"})
    head = ElementTree.SubElement(opml, "head")
    append_text_element(head, "title", "RSS Feeds")
    body = ElementTree.SubElement(opml, "body")
    for title, url in entries:
        safe_title = strip_invalid_xml_chars(title)
        safe_url = strip_invalid_xml_chars(url)
        ElementTree.SubElement(
            body,
            "outline",
            {
                "text": safe_title,
                "title": safe_title,
                "type": "rss",
                "xmlUrl": safe_url,
            },
        )
    return ElementTree.tostring(opml, encoding="utf-8", xml_declaration=True)
