"""负责遵守站点限速并下载公开索引页。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import requests


class FeedFetchError(RuntimeError):
    """表示上游页面下载失败。"""


class ForumFetcher:
    """串行访问上游，保证任意两次请求间隔不少于配置值。"""

    def __init__(
        self,
        min_interval_seconds: int,
        timeout_seconds: int,
        user_agent: str,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """初始化限速下载器并允许测试注入时钟和会话。

        参数：
            min_interval_seconds: 两次请求之间的最小秒数。
            timeout_seconds: 单次 HTTP 请求超时秒数。
            user_agent: 请求使用的 User-Agent。
            session: 可选的 requests 会话。
            clock: 单调时钟函数。
            sleeper: 等待函数。
        返回值：
            无。
        """
        self.min_interval_seconds = min_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.clock = clock
        self.sleeper = sleeper
        self.request_lock = threading.Lock()
        self.last_request_at: float | None = None

    def wait_for_rate_limit(self) -> None:
        """在锁内等待剩余限速时间，避免并发请求突破规则。

        参数：
            无。
        返回值：
            无。
        """
        if self.last_request_at is None:
            return

        elapsed_seconds = self.clock() - self.last_request_at
        remaining_seconds = self.min_interval_seconds - elapsed_seconds
        if remaining_seconds > 0:
            self.sleeper(remaining_seconds)

    def fetch_html(self, url: str) -> str:
        """下载索引页并将网络或状态码错误转换为明确异常。

        参数：
            url: 公开论坛索引页地址。
        返回值：
            UTF-8 HTML 文本。
        """
        with self.request_lock:
            self.wait_for_rate_limit()
            try:
                response = self.session.get(
                    url,
                    headers={"User-Agent": self.user_agent},
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                # 目标页响应头和 HTML 均声明 UTF-8，固定编码可避免内容探测误判。
                response.encoding = "utf-8"
                return response.text
            except requests.RequestException as error:
                raise FeedFetchError(f"上游索引页请求失败: {error}") from error
            finally:
                # 失败请求同样计入限速，避免异常时快速重试轰击站点。
                self.last_request_at = self.clock()
