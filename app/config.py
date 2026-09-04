"""集中管理服务配置与边界校验。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import soupsieve

from app.models import FeedSource


DEFAULT_SOURCE_URL = "https://forum-a.example.com/thread0806.php?fid=16"
DEFAULT_PORT = 28888
MIN_ALLOWED_PORT = 20000
MIN_FETCH_INTERVAL_SECONDS = 10
FORUM_B_INDEX_URL = "https://forum-b.example.com/index.php"
# (key, fid, 板块名)：论坛 B 需要订阅的三个板块
FORUM_B_FEEDS = (
    ("forum-b-fid-19", "19", "板块一"),
    ("forum-b-fid-21", "21", "板块二"),
    ("forum-b-fid-33", "33", "板块三"),
)
GALLERY_SOURCES_FILE = Path(__file__).resolve().parent.parent / "config" / "gallery_sources.json"
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
GALLERY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_URL_LENGTH = 2048
MAX_USER_AGENT_LENGTH = 512
MAX_FEED_ITEMS = 10000
MAX_SELECTOR_LENGTH = 512
MAX_PATTERN_LENGTH = 1024
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    """保存运行时配置，避免业务模块重复读取环境变量。"""

    source_url: str
    feed_title: str
    public_feed_url: str
    public_base_url: str
    port: int
    cache_seconds: int
    failure_retry_seconds: int
    min_fetch_interval_seconds: int
    request_timeout_seconds: int
    max_feed_items: int
    user_agent: str
    keep_image_posts_only: bool = True
    gallery_sources: tuple[FeedSource, ...] = field(default_factory=tuple)
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES


def get_int_env(name: str, default_value: int) -> int:
    """读取整数环境变量并在格式错误时明确失败。

    参数：
        name: 环境变量名称。
        default_value: 变量缺失时使用的默认值。
    返回值：
        解析后的整数。
    """
    raw_value = os.getenv(name)
    if raw_value is None:
        return default_value

    try:
        return int(raw_value)
    except ValueError as error:
        raise ValueError(f"环境变量 {name} 必须是整数") from error


def get_bool_env(name: str, default_value: bool) -> bool:
    """读取布尔环境变量，仅接受 0/1/true/false。

    参数：
        name: 环境变量名称。
        default_value: 变量缺失时使用的默认值。
    返回值：
        解析后的布尔值。
    """
    raw_value = os.getenv(name)
    if raw_value is None:
        return default_value
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"环境变量 {name} 必须是布尔值（0/1/true/false）")


def validate_http_url(value: str, field_name: str) -> str:
    """校验并规范化一个公开 HTTP(S) 地址。

    参数：
        value: 待校验的 URL。
        field_name: 出错时显示的配置字段名。
    返回值：
        去除首尾空白后的 URL。
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} 不能为空")
    if len(normalized) > MAX_URL_LENGTH or any(
        character.isspace() or ord(character) < 0x20 for character in normalized
    ):
        raise ValueError(f"{field_name} URL 格式无效")
    try:
        parsed_url = urlsplit(normalized)
        hostname = parsed_url.hostname
        port = parsed_url.port
    except ValueError as error:
        raise ValueError(f"{field_name} URL 格式无效") from error
    if parsed_url.scheme.lower() not in ALLOWED_URL_SCHEMES or not parsed_url.netloc:
        raise ValueError(f"{field_name} 必须是完整的 HTTP(S) URL")
    if hostname is None or parsed_url.username is not None or parsed_url.password is not None:
        raise ValueError(f"{field_name} 不得包含凭据或无效主机名")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{field_name} 端口超出范围")
    return normalized


def validate_settings(settings: Settings) -> Settings:
    """校验端口、限速和缓存配置，防止启动后违规访问上游。

    参数：
        settings: 待校验的配置对象。
    返回值：
        校验通过的原配置对象。
    """
    if not isinstance(settings, Settings):
        raise ValueError("settings 必须是 Settings 实例")
    validate_http_url(settings.source_url, "SOURCE_URL")
    validate_http_url(settings.public_feed_url, "PUBLIC_FEED_URL")
    validate_public_base_url(settings.public_base_url)
    if not isinstance(settings.feed_title, str) or not settings.feed_title.strip():
        raise ValueError("FEED_TITLE 不能为空")
    if (
        not isinstance(settings.user_agent, str)
        or not settings.user_agent.strip()
        or len(settings.user_agent) > MAX_USER_AGENT_LENGTH
    ):
        raise ValueError("USER_AGENT 不能为空且长度不得超过 512")
    if isinstance(settings.port, bool) or not isinstance(settings.port, int):
        raise ValueError("PORT 必须是整数")
    if not isinstance(settings.keep_image_posts_only, bool):
        raise ValueError("KEEP_IMAGE_POSTS_ONLY 必须是布尔值")
    if settings.port <= MIN_ALLOWED_PORT:
        raise ValueError(f"PORT 必须大于 {MIN_ALLOWED_PORT}")
    if settings.port > 65535:
        raise ValueError("PORT 不得超过 65535")
    integer_fields = (
        ("MIN_FETCH_INTERVAL_SECONDS", settings.min_fetch_interval_seconds),
        ("CACHE_SECONDS", settings.cache_seconds),
        ("FAILURE_RETRY_SECONDS", settings.failure_retry_seconds),
        ("REQUEST_TIMEOUT_SECONDS", settings.request_timeout_seconds),
        ("MAX_FEED_ITEMS", settings.max_feed_items),
        ("MAX_RESPONSE_BYTES", settings.max_response_bytes),
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for _, value in integer_fields):
        raise ValueError("缓存、限速和数量配置必须是整数")
    if settings.min_fetch_interval_seconds < MIN_FETCH_INTERVAL_SECONDS:
        raise ValueError("MIN_FETCH_INTERVAL_SECONDS 不得小于 10")
    if settings.cache_seconds < settings.min_fetch_interval_seconds:
        raise ValueError("CACHE_SECONDS 不得小于上游最小请求间隔")
    if settings.failure_retry_seconds < settings.min_fetch_interval_seconds:
        raise ValueError("FAILURE_RETRY_SECONDS 不得小于上游最小请求间隔")
    if settings.request_timeout_seconds <= 0:
        raise ValueError("REQUEST_TIMEOUT_SECONDS 必须大于 0")
    if settings.max_feed_items <= 0:
        raise ValueError("MAX_FEED_ITEMS 必须大于 0")
    if settings.max_feed_items > MAX_FEED_ITEMS:
        raise ValueError(f"MAX_FEED_ITEMS 不得超过 {MAX_FEED_ITEMS}")
    if settings.max_response_bytes <= 0:
        raise ValueError("MAX_RESPONSE_BYTES 必须大于 0")
    return settings


def get_url_origin(url: str) -> str:
    """提取公开 RSS 地址的协议与主机部分。

    参数：
        url: 完整公开 URL。
    返回值：
        不含末尾斜杠的 URL origin。
    """
    normalized = validate_http_url(url, "PUBLIC_FEED_URL")
    parsed_url = urlsplit(normalized)
    return f"{parsed_url.scheme.lower()}://{parsed_url.netloc}"


def validate_public_base_url(value: str) -> str:
    """校验用于拼接订阅路由的公开基础地址。

    参数：
        value: 公开基础 URL，可带路径前缀但不能带查询或片段。
    返回值：
        去除首尾空白和尾部斜杠后的基础 URL。
    """
    normalized = validate_http_url(value, "PUBLIC_BASE_URL").rstrip("/")
    parsed_url = urlsplit(normalized)
    if parsed_url.query or parsed_url.fragment:
        raise ValueError("PUBLIC_BASE_URL 不得包含查询参数或片段")
    return normalized


def infer_gallery_parser_kind(source_url: str) -> str:
    """根据图站地址形态推断 `auto` 来源应使用的解析器。

    参数：
        source_url: 图站列表或 RSS 地址。
    返回值：
        ``rss`` 表示直通 XML 源，否则返回 ``links``。
    """
    normalized_url = validate_http_url(source_url, "图站来源 url")
    path = urlsplit(normalized_url).path.lower().rstrip("/")
    if path.endswith((".xml", ".rss", ".atom", ".rdf")):
        return "rss"
    path_parts = {part for part in path.split("/") if part}
    basename = path.rsplit("/", 1)[-1]
    if basename in {"feed", "feeds", "rss", "atom", "feed.php", "rss.php"}:
        return "rss"
    if path_parts.intersection({"feed", "feeds", "rss", "atom"}):
        return "rss"
    return "links"


def load_gallery_sources(
    public_base_url: str, path: Path | None = None
) -> tuple[FeedSource, ...]:
    """从 JSON 文件加载图站来源，文件缺失时返回空集合。

    参数：
        public_base_url: 对外公开基础地址。
        path: 可选的来源配置文件路径。
    返回值：
        已生成独立路由的 FeedSource 元组。
    """
    normalized_base_url = validate_public_base_url(public_base_url)
    configured_path = os.getenv("GALLERY_SOURCES_FILE")
    source_file = Path(path) if path is not None else Path(
        configured_path or GALLERY_SOURCES_FILE
    )
    if not source_file.exists():
        return ()

    try:
        payload = json.loads(source_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"图站来源配置无法读取: {error}") from error

    if not isinstance(payload, dict):
        raise ValueError("图站来源配置顶层必须是对象")
    entries = payload.get("sources", [])
    if entries is None:
        raise ValueError("图站来源配置 sources 必须是数组")
    if not isinstance(entries, list):
        raise ValueError("图站来源配置 sources 必须是数组")

    sources = []
    seen_keys: set[str] = set()
    base_url = normalized_base_url
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"图站来源第 {index} 项必须是对象")
        raw_key = entry.get("key", "")
        raw_title = entry.get("title", "")
        raw_url = entry.get("url", "")
        raw_parser = entry.get("parser", "auto")
        if not all(
            isinstance(value, str)
            for value in (raw_key, raw_title, raw_url, raw_parser)
        ):
            raise ValueError(f"图站来源第 {index} 项字段必须是字符串")
        key = raw_key.strip()
        title = raw_title.strip()
        url = raw_url.strip()
        parser_kind = raw_parser.strip()
        if not key or not title or not url:
            raise ValueError("图站来源每项必须包含 key/title/url")
        if not GALLERY_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"图站来源 {key} 的 key 格式无效")
        normalized_key = key.casefold()
        if normalized_key in seen_keys:
            raise ValueError(f"图站来源 key 重复: {key}")
        seen_keys.add(normalized_key)
        if parser_kind not in {"auto", "rss", "links"}:
            raise ValueError(
                f"图站来源 {key} 的 parser 必须是 rss 或 links（也可使用 auto）"
            )
        validate_http_url(url, f"图站来源 {key} 的 url")

        optional_fields = {
            "link_pattern": entry.get("link_pattern", ""),
            "link_selector": entry.get("link_selector", ""),
            "parent_selector": entry.get("parent_selector", ""),
        }
        for field_name, field_value in optional_fields.items():
            if not isinstance(field_value, str):
                raise ValueError(f"图站来源 {key} 的 {field_name} 必须是字符串")
        link_pattern = optional_fields["link_pattern"] or ""
        if link_pattern:
            if len(link_pattern) > MAX_PATTERN_LENGTH:
                raise ValueError(f"图站来源 {key} 的 link_pattern 过长")
            try:
                re.compile(link_pattern)
            except re.error as error:
                raise ValueError(f"图站来源 {key} 的 link_pattern 无效") from error
        for selector_name in ("link_selector", "parent_selector"):
            selector = optional_fields[selector_name] or ""
            if len(selector) > MAX_SELECTOR_LENGTH:
                raise ValueError(f"图站来源 {key} 的 {selector_name} 过长")
            if selector:
                try:
                    soupsieve.compile(selector)
                except Exception as error:
                    raise ValueError(
                        f"图站来源 {key} 的 {selector_name} 无效"
                    ) from error

        route = f"/gallery/{key}.xml"
        sources.append(
            FeedSource(
                key=key,
                source_url=url,
                feed_title=title,
                route=route,
                public_feed_url=f"{base_url}{route}",
                parser_kind=parser_kind,
                link_pattern=link_pattern,
                link_selector=optional_fields["link_selector"] or "",
                parent_selector=optional_fields["parent_selector"] or "",
            )
        )
    return tuple(sources)


def get_feed_sources(settings: Settings) -> list[FeedSource]:
    """构建论坛 A、论坛 B 与全部图站来源配置。

    参数：
        settings: 已校验的运行时配置。
    返回值：
        论坛 A、论坛 B 与图站来源列表。
    """
    public_base_url = settings.public_base_url.rstrip("/")
    sources = [
        FeedSource(
            key="forum-a-fid-16",
            source_url=settings.source_url,
            feed_title=settings.feed_title,
            route="/rss.xml",
            public_feed_url=settings.public_feed_url,
        )
    ]
    forum_b_base_url = validate_http_url(
        os.getenv("FORUM_B_BASE_URL", "https://forum-b.example.com"),
        "FORUM_B_BASE_URL",
    ).rstrip("/")
    forum_b_index_url = validate_http_url(
        os.getenv("FORUM_B_INDEX_URL", FORUM_B_INDEX_URL),
        "FORUM_B_INDEX_URL",
    )
    for source_key, fid, section_name in FORUM_B_FEEDS:
        route = f"/rss/{source_key}.xml"
        sources.append(
            FeedSource(
                key=source_key,
                source_url=f"{forum_b_base_url}/forumdisplay.php?fid={fid}",
                feed_title=f"论坛 B - {section_name}",
                route=route,
                public_feed_url=f"{public_base_url}{route}",
            )
        )
    # 论坛 B 首页三栏（最新精华/最新点赞/本周热门）单独订阅
    sources.append(
        FeedSource(
            key="forum-b-highlights",
            source_url=forum_b_index_url,
            feed_title="论坛 B - 最新精华/最新点赞/本周热门",
            route="/rss/forum-b-highlights.xml",
            public_feed_url=f"{public_base_url}/rss/forum-b-highlights.xml",
        )
    )
    sources.extend(settings.gallery_sources)
    return sources


def get_settings() -> Settings:
    """从环境变量构建并校验完整运行时配置。

    参数：
        无。
    返回值：
        可安全使用的配置对象。
    """
    public_feed_url = os.getenv(
        "PUBLIC_FEED_URL", f"http://127.0.0.1:{DEFAULT_PORT}/rss.xml"
    )
    public_base_url = os.getenv("PUBLIC_BASE_URL")
    if public_base_url is None:
        public_base_url = get_url_origin(public_feed_url)
    settings = Settings(
        source_url=os.getenv("SOURCE_URL", DEFAULT_SOURCE_URL),
        feed_title=os.getenv("FEED_TITLE", "论坛 A 示例订阅"),
        public_feed_url=public_feed_url,
        public_base_url=public_base_url,
        port=get_int_env("PORT", DEFAULT_PORT),
        cache_seconds=get_int_env("CACHE_SECONDS", 600),
        failure_retry_seconds=get_int_env("FAILURE_RETRY_SECONDS", 60),
        min_fetch_interval_seconds=get_int_env(
            "MIN_FETCH_INTERVAL_SECONDS", MIN_FETCH_INTERVAL_SECONDS
        ),
        request_timeout_seconds=get_int_env("REQUEST_TIMEOUT_SECONDS", 20),
        max_feed_items=get_int_env("MAX_FEED_ITEMS", 100),
        max_response_bytes=get_int_env(
            "MAX_RESPONSE_BYTES", DEFAULT_MAX_RESPONSE_BYTES
        ),
        user_agent=os.getenv(
            "USER_AGENT", "ForumRSSBot/1.0 (+private feed reader)"
        ),
        keep_image_posts_only=get_bool_env("KEEP_IMAGE_POSTS_ONLY", True),
        gallery_sources=load_gallery_sources(public_base_url),
    )
    return validate_settings(settings)
