"""验证同一下载器在连续请求之间执行严格限速。"""

from typing import Any

import requests

from app.fetcher import ForumFetcher


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
