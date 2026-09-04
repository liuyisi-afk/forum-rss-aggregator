"""验证图站列表解析的链接过滤、去重和日期提取。"""

import json
from datetime import datetime, timezone

import pytest

from app import parser as parser_module
from app.parser import parse_link_gallery_items, parse_mzt_api_items


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


def test_parse_mzt_api_items_extracts_metadata() -> None:
    """验证 mzt API 条目的 ID、标题、链接和日期可正常转换。"""
    payload = json.dumps(
        {
            "items": [
                {
                    "id": 123,
                    "title": "  图集一  ",
                    "created_at": "2026-08-13T08:30:00Z",
                }
            ]
        }
    )

    items = parse_mzt_api_items(
        payload,
        "https://mzt.example.com/urls?page=1&pageSize=50",
        10,
    )

    assert len(items) == 1
    assert items[0].thread_id == "mzt:123"
    assert items[0].title == "图集一"
    assert items[0].link == "https://mzt.example.com/view/123"
    assert items[0].published_at == datetime(
        2026, 8, 13, 8, 30, tzinfo=timezone.utc
    )


def test_parse_mzt_api_items_deduplicates_ids() -> None:
    """验证重复 mzt ID 只保留首个条目。"""
    payload = json.dumps(
        {
            "items": [
                {"id": "same-id", "title": "首个标题"},
                {"id": "same-id", "title": "重复标题"},
            ]
        }
    )

    items = parse_mzt_api_items(payload, "https://mzt.example.com/urls", 10)

    assert [item.title for item in items] == ["首个标题"]


def test_parse_mzt_api_items_discards_epoch_placeholder_date() -> None:
    """验证 1970 年的上游占位日期会转换为 None。"""
    payload = json.dumps(
        {
            "items": [
                {
                    "id": "epoch-item",
                    "title": "无有效日期的图集",
                    "created_at": "1970-01-01T00:00:00Z",
                }
            ]
        }
    )

    items = parse_mzt_api_items(payload, "https://mzt.example.com/urls", 10)

    assert len(items) == 1
    assert items[0].published_at is None


def test_parse_mzt_api_items_falls_back_to_updated_date() -> None:
    """验证 created_at 无效或占位时会采用有效的 updated_at。"""
    payload = json.dumps(
        {
            "items": [
                {
                    "id": "updated-item",
                    "title": "更新时间图集",
                    "created_at": "not-a-date",
                    "updated_at": "2026-08-13T08:30:00Z",
                }
            ]
        }
    )

    items = parse_mzt_api_items(payload, "https://mzt.example.com/urls", 10)

    assert items[0].published_at == datetime(
        2026, 8, 13, 8, 30, tzinfo=timezone.utc
    )


def test_parse_mzt_api_items_removes_xml_invalid_title_chars() -> None:
    """验证标题中的控制字符不会进入生成的 RSS XML。"""
    payload = json.dumps(
        {"items": [{"id": "clean-item", "title": "\u0000安全标题\ud800"}]}
    )

    items = parse_mzt_api_items(payload, "https://mzt.example.com/urls", 10)

    assert [item.title for item in items] == ["安全标题"]


def test_parse_mzt_api_items_handles_deeply_nested_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证深层 JSON 解析异常会按无效响应安全返回空列表。"""
    def raise_recursion_error(_payload: str) -> object:
        """模拟 JSON 解码器达到最大嵌套深度。"""
        raise RecursionError("maximum JSON depth exceeded")

    monkeypatch.setattr(parser_module.json, "loads", raise_recursion_error)

    assert parse_mzt_api_items("{}", "https://mzt.example.com/urls", 10) == []


def test_parse_mzt_api_items_rejects_top_level_array() -> None:
    """验证 JSON 顶层不是对象时安全返回空列表。"""
    payload = json.dumps([{"id": "one", "title": "图集"}])

    assert parse_mzt_api_items(payload, "https://mzt.example.com/urls", 10) == []


def test_parse_mzt_api_items_rejects_unsafe_id_and_base_url() -> None:
    """验证非法 ID 或非 HTTP(S) 基础地址不会生成条目。"""
    unsafe_id_payload = json.dumps(
        {"items": [{"id": "../admin?token=x", "title": "非法图集"}]}
    )
    valid_payload = json.dumps(
        {"items": [{"id": "safe-id", "title": "正常图集"}]}
    )

    assert (
        parse_mzt_api_items(unsafe_id_payload, "https://mzt.example.com/urls", 10)
        == []
    )
    assert parse_mzt_api_items(valid_payload, "ftp://mzt.example.com/urls", 10) == []
