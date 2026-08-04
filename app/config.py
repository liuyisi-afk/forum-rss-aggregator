"""集中管理服务配置与边界校验。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

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


def validate_settings(settings: Settings) -> Settings:
    """校验端口、限速和缓存配置，防止启动后违规访问上游。

    参数：
        settings: 待校验的配置对象。
    返回值：
        校验通过的原配置对象。
    """
    if settings.port <= MIN_ALLOWED_PORT:
        raise ValueError(f"PORT 必须大于 {MIN_ALLOWED_PORT}")
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
    get_url_origin(settings.public_base_url)
    return settings


def get_url_origin(url: str) -> str:
    """提取公开 RSS 地址的协议与主机部分。

    参数：
        url: 完整公开 URL。
    返回值：
        不含末尾斜杠的 URL origin。
    """
    parsed_url = urlsplit(url)
    if not parsed_url.scheme or not parsed_url.netloc:
        raise ValueError("PUBLIC_FEED_URL 必须是完整 URL")
    return f"{parsed_url.scheme}://{parsed_url.netloc}"


def get_feed_sources(settings: Settings) -> list[FeedSource]:
    """构建论坛 A 与论坛 B 的三个板块的来源配置。

    参数：
        settings: 已校验的运行时配置。
    返回值：
        论坛 A 与论坛 B 的三个板块的来源列表。
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
    for source_key, fid, section_name in FORUM_B_FEEDS:
        route = f"/rss/{source_key}.xml"
        sources.append(
            FeedSource(
                key=source_key,
                source_url=f"https://forum-b.example.com/forumdisplay.php?fid={fid}",
                feed_title=f"论坛 B - {section_name}",
                route=route,
                public_feed_url=f"{public_base_url}{route}",
            )
        )
    # 论坛 B 首页三栏（最新精华/最新点赞/本周热门）单独订阅
    sources.append(
        FeedSource(
            key="forum-b-highlights",
            source_url=FORUM_B_INDEX_URL,
            feed_title="论坛 B - 最新精华/最新点赞/本周热门",
            route="/rss/forum-b-highlights.xml",
            public_feed_url=f"{public_base_url}/rss/forum-b-highlights.xml",
        )
    )
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
    settings = Settings(
        source_url=os.getenv("SOURCE_URL", DEFAULT_SOURCE_URL),
        feed_title=os.getenv("FEED_TITLE", "论坛 A 示例订阅"),
        public_feed_url=public_feed_url,
        public_base_url=os.getenv("PUBLIC_BASE_URL", get_url_origin(public_feed_url)),
        port=get_int_env("PORT", DEFAULT_PORT),
        cache_seconds=get_int_env("CACHE_SECONDS", 600),
        failure_retry_seconds=get_int_env("FAILURE_RETRY_SECONDS", 60),
        min_fetch_interval_seconds=get_int_env(
            "MIN_FETCH_INTERVAL_SECONDS", MIN_FETCH_INTERVAL_SECONDS
        ),
        request_timeout_seconds=get_int_env("REQUEST_TIMEOUT_SECONDS", 20),
        max_feed_items=get_int_env("MAX_FEED_ITEMS", 100),
        user_agent=os.getenv(
            "USER_AGENT", "ForumRSSBot/1.0 (+private feed reader)"
        ),
        keep_image_posts_only=get_bool_env("KEEP_IMAGE_POSTS_ONLY", True),
    )
    return validate_settings(settings)
