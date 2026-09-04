"""验证同一下载器在连续请求之间执行严格限速。"""

from typing import Any

import requests
import pytest

from app.fetcher import FeedFetchError, ForumFetcher


class FakeClock:
    """提供可推进的单调测试时钟。"""

    def __init__(self) -> None:
        """初始化零秒时钟和等待记录。

        参数：
            无。
        返回值：
            无。
        """
        self.now = 0.0
        self.sleep_calls: list[float] = []

    def read(self) -> float:
        """返回当前测试时间。

        参数：
            无。
        返回值：
            当前秒数。
        """
        return self.now

    def sleep(self, seconds: float) -> None:
        """记录等待并推进测试时间。

        参数：
            seconds: 等待秒数。
        返回值：
            无。
        """
        self.sleep_calls.append(seconds)
        self.now += seconds


class FakeResponse:
    """模拟成功的 requests 响应。"""

    text = "<html></html>"
    encoding = "utf-8"

    def raise_for_status(self) -> None:
        """模拟 HTTP 状态成功。

        参数：
            无。
        返回值：
            无。
        """


class FakeSession(requests.Session):
    """记录请求次数的测试会话。"""

    def __init__(self) -> None:
        """初始化请求计数。

        参数：
            无。
        返回值：
            无。
        """
        super().__init__()
        self.call_count = 0

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        """记录请求并返回成功响应。

        参数：
            url: 请求地址。
            kwargs: 请求选项。
        返回值：
            FakeResponse。
        """
        self.call_count += 1
        return FakeResponse()


class StreamingResponse:
    """模拟可迭代读取且可关闭的 HTTP 响应。"""

    encoding = "utf-8"

    def __init__(self, chunks: list[bytes]) -> None:
        """保存响应分块并初始化关闭状态。

        参数：
            chunks: 依次返回的字节分块。
        返回值：
            无。
        """
        self.chunks = chunks
        self.headers: dict[str, str] = {}
        self.is_closed = False

    def raise_for_status(self) -> None:
        """模拟 HTTP 状态成功。"""

    def iter_content(self, chunk_size: int):
        """返回响应字节分块。"""
        return iter(self.chunks)

    def close(self) -> None:
        """记录响应已关闭。"""
        self.is_closed = True


class StreamingSession:
    """返回固定流式响应并记录请求参数。"""

    def __init__(self, response: StreamingResponse) -> None:
        """保存待返回的响应。"""
        self.response = response
        self.kwargs: dict = {}

    def get(self, url: str, **kwargs):
        """记录请求选项并返回流式响应。"""
        self.kwargs = kwargs
        return self.response


def test_forum_fetcher_waits_between_shared_requests() -> None:
    """验证连续请求同一共享实例时至少等待配置间隔。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    clock = FakeClock()
    session = FakeSession()
    fetcher = ForumFetcher(
        min_interval_seconds=10,
        timeout_seconds=20,
        user_agent="test-agent",
        session=session,
        clock=clock.read,
        sleeper=clock.sleep,
    )

    fetcher.fetch_html("https://forum-b.example.com/?fid=19")
    fetcher.fetch_html("https://forum-b.example.com/?fid=4")

    assert session.call_count == 2
    assert clock.sleep_calls == [10.0]


def test_forum_fetcher_streams_and_closes_response() -> None:
    """验证下载器以流式方式读取并释放响应连接。"""
    response = StreamingResponse([b"<html>", b"ok</html>"])
    session = StreamingSession(response)
    fetcher = ForumFetcher(0, 20, "test-agent", session=session)

    assert fetcher.fetch_html("https://example.com/feed") == "<html>ok</html>"
    assert session.kwargs["stream"] is True
    assert response.is_closed is True


def test_forum_fetcher_rejects_oversized_stream() -> None:
    """验证响应超过上限时不会继续拼接全部正文。"""
    response = StreamingResponse([b"1234", b"5678"])
    session = StreamingSession(response)
    fetcher = ForumFetcher(
        0, 20, "test-agent", session=session, max_response_bytes=5
    )

    with pytest.raises(FeedFetchError, match="超过大小限制"):
        fetcher.fetch_html("https://example.com/feed")
    assert response.is_closed is True
