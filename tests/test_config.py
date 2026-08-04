"""验证强制端口和上游限速配置边界。"""

import pytest

from app.config import Settings, get_feed_sources, validate_settings


def build_settings(port: int = 28888, min_interval: int = 10) -> Settings:
    """构造测试配置，减少各测试的重复字段。

    参数：
        port: 服务监听端口。
        min_interval: 上游请求最小间隔。
    返回值：
        未校验的 Settings 对象。
    """
    return Settings(
        source_url="https://forum-a.example.com/thread0806.php?fid=16",
        feed_title="测试 RSS",
        public_feed_url="http://127.0.0.1:28888/rss.xml",
        public_base_url="http://127.0.0.1:28888",
        port=port,
        cache_seconds=600,
        failure_retry_seconds=60,
        min_fetch_interval_seconds=min_interval,
        request_timeout_seconds=20,
        max_feed_items=100,
        user_agent="test-agent",
    )


def test_validate_settings_requires_high_port() -> None:
    """验证 20000 及以下端口会在启动前被拒绝。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    with pytest.raises(ValueError, match="PORT 必须大于 20000"):
        validate_settings(build_settings(port=20000))


def test_validate_settings_enforces_robots_rate_limit() -> None:
    """验证低于 robots 要求的访问间隔会被拒绝。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    with pytest.raises(ValueError, match="不得小于 10"):
        validate_settings(build_settings(min_interval=9))


def test_validate_settings_requires_positive_timeout() -> None:
    """验证非正数请求超时会在启动前被拒绝。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    settings = build_settings()
    invalid_settings = Settings(**{**settings.__dict__, "request_timeout_seconds": 0})

    with pytest.raises(ValueError, match="必须大于 0"):
        validate_settings(invalid_settings)


def test_get_feed_sources_normalizes_public_base_url() -> None:
    """验证公开基础地址尾斜杠不会生成双斜杠链接。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    settings = build_settings()
    settings_with_slash = Settings(
        **{**settings.__dict__, "public_base_url": "https://rss.example.com/"}
    )

    sources = get_feed_sources(settings_with_slash)

    assert sources[1].public_feed_url == "https://rss.example.com/rss/forum-b-fid-19.xml"
