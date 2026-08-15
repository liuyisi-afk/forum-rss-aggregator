"""验证图站来源 JSON 配置的解析与校验。"""

import json
from pathlib import Path

import pytest

from app.config import load_gallery_sources


def write_sources(tmp_path: Path, sources: list[dict]) -> Path:
    """写入测试来源文件并返回路径。

    参数：
        tmp_path: 临时目录。
        sources: 来源配置列表。
    返回值：
        配置文件路径。
    """
    path = tmp_path / "gallery_sources.json"
    path.write_text(json.dumps({"sources": sources}, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_gallery_sources_builds_routes(tmp_path: Path) -> None:
    """验证来源按 key 生成独立路由和公开地址。"""
    path = write_sources(
        tmp_path,
        [
            {"key": "one", "title": "站点一", "url": "https://a.example.com/rss.xml", "parser": "rss"},
            {
                "key": "two",
                "title": "站点二",
                "url": "https://b.example.com/",
                "parser": "links",
                "link_pattern": "^/album/",
            },
        ],
    )

    sources = load_gallery_sources("https://rss.example.com/", path)

    assert [source.key for source in sources] == ["one", "two"]
    assert sources[0].route == "/gallery/one.xml"
    assert sources[0].public_feed_url == "https://rss.example.com/gallery/one.xml"
    assert sources[1].parser_kind == "links"
    assert sources[1].link_pattern == "^/album/"


def test_load_gallery_sources_missing_file_returns_empty() -> None:
    """验证配置文件不存在时不报错并返回空集合。"""
    assert load_gallery_sources("https://rss.example.com", Path("missing.json")) == ()


def test_load_gallery_sources_rejects_invalid_parser(tmp_path: Path) -> None:
    """验证未知 parser 类型会在启动前被拒绝。"""
    path = write_sources(
        tmp_path,
        [{"key": "bad", "title": "坏配置", "url": "https://a.example.com/", "parser": "unknown"}],
    )

    with pytest.raises(ValueError, match="parser 必须是 rss 或 links"):
        load_gallery_sources("https://rss.example.com", path)
