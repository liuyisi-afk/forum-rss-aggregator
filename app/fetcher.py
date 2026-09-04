"""负责遵守站点限速并下载公开索引页。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import requests


class FeedFetchError(RuntimeError):
    """表示上游页面下载失败。"""


DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
MAX_URL_LENGTH = 2048


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
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        """初始化限速下载器并允许测试注入时钟和会话。

        参数：
            min_interval_seconds: 两次请求之间的最小秒数。
            timeout_seconds: 单次 HTTP 请求超时秒数。
            user_agent: 请求使用的 User-Agent。
            session: 可选的 requests 会话。
            clock: 单调时钟函数。
            sleeper: 等待函数。
            max_response_bytes: 单次响应允许读取的最大字节数。
        返回值：
            无。
        """
        if isinstance(min_interval_seconds, bool) or min_interval_seconds < 0:
            raise ValueError("最小请求间隔必须是非负数")
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("请求超时必须大于 0")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise ValueError("响应大小上限必须是正整数")
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise ValueError("User-Agent 不能为空")
        if any(character in user_agent for character in "\r\n"):
            raise ValueError("User-Agent 不得包含换行符")
        self.min_interval_seconds = min_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent.strip()
        self.max_response_bytes = max_response_bytes
        self.session = session if session is not None else requests.Session()
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
        if not isinstance(url, str):
            raise FeedFetchError("上游索引页 URL 无效")
        normalized_url = url.strip()
        if (
            not normalized_url
            or len(normalized_url) > MAX_URL_LENGTH
            or any(character.isspace() for character in normalized_url)
        ):
            raise FeedFetchError("上游索引页 URL 无效")
        try:
            parsed_url = urlsplit(normalized_url)
            hostname = parsed_url.hostname
            parsed_url.port
        except (AttributeError, TypeError, ValueError) as error:
            raise FeedFetchError("上游索引页 URL 无效") from error
        if (
            parsed_url.scheme.lower() not in ALLOWED_URL_SCHEMES
            or not parsed_url.netloc
            or hostname is None
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise FeedFetchError("上游索引页 URL 必须是 HTTP(S) 地址")

        with self.request_lock:
            self.wait_for_rate_limit()
            response = None
            try:
                response = self.session.get(
                    normalized_url,
                    headers={"User-Agent": self.user_agent},
                    timeout=self.timeout_seconds,
                    stream=True,
                )
                response.raise_for_status()
                return self.read_response_text(response)
            except FeedFetchError:
                raise
            except (requests.RequestException, UnicodeError, TypeError) as error:
                raise FeedFetchError(f"上游索引页请求失败: {error}") from error
            finally:
                if response is not None:
                    close_response = getattr(response, "close", None)
                    if callable(close_response):
                        close_response()
                # 失败请求同样计入限速，避免异常时快速重试轰击站点。
                self.last_request_at = self.clock()

    def read_response_text(self, response: requests.Response) -> str:
        """在大小上限内读取并解码一个 HTTP 响应。

        参数：
            response: 已完成状态码校验的 requests 响应。
        返回值：
            UTF-8 HTML 文本。
        """
        headers = getattr(response, "headers", {}) or {}
        content_length = headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > self.max_response_bytes:
                    raise FeedFetchError("上游响应超过大小限制")
            except (TypeError, ValueError, OverflowError):
                # 非标准 Content-Length 交给实际正文大小检查。
                pass

        # 目标页响应头和 HTML 均声明 UTF-8，固定编码可避免内容探测误判。
        response.encoding = "utf-8"
        iter_content = getattr(response, "iter_content", None)
        if callable(iter_content):
            chunks: list[bytes] = []
            total_bytes = 0
            for chunk in iter_content(chunk_size=65536):
                if not isinstance(chunk, (bytes, bytearray)):
                    raise FeedFetchError("上游响应正文不是字节流")
                total_bytes += len(chunk)
                if total_bytes > self.max_response_bytes:
                    raise FeedFetchError("上游响应超过大小限制")
                chunks.append(bytes(chunk))
            try:
                return b"".join(chunks).decode("utf-8", errors="replace")
            except UnicodeError as error:
                raise FeedFetchError("上游响应编码无效") from error

        # 兼容轻量测试替身或非 requests 会话实现。
        text = response.text
        if not isinstance(text, str):
            raise FeedFetchError("上游响应正文不是文本")
        try:
            response_size = len(text.encode(response.encoding or "utf-8"))
        except (LookupError, UnicodeError) as error:
            raise FeedFetchError("上游响应编码无效") from error
        if response_size > self.max_response_bytes:
            raise FeedFetchError("上游响应超过大小限制")
        return text
