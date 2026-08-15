"""探测目标站点可用性并识别自带 RSS 与建站程序。

用法：python scripts/probe_sources.py goal-objective.md [--cache-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
GENERATOR_PATTERNS = (
    (r"wordpress", "wordpress"),
    (r"xenforo", "xenforo"),
    (r"discuz", "discuz"),
    (r"phpbb", "phpbb"),
    (r"typecho", "typecho"),
)
RSS_REL_PATTERN = re.compile(
    r"application/(?:rss|atom)\+xml|text/xml", re.IGNORECASE
)


def extract_urls(markdown_path: Path) -> list[str]:
    """从 Markdown 目标文件提取去重后的站点地址。

    参数：
        markdown_path: 目标文件路径。
    返回值：
        按出现顺序去重后的 URL 列表。
    """
    text = markdown_path.read_text(encoding="utf-8")
    urls = re.findall(r"\]\((https?://[^)\s]+)\)", text)
    plain_urls = re.findall(r"(?<!\]\()https?://[^\s)\]]+", text)
    ordered = list(dict.fromkeys(urls + plain_urls))
    return [url.rstrip(".,;") for url in ordered]


def extract_title(html: str) -> str:
    """提取页面标题并截断为单行。

    参数：
        html: 页面 HTML 文本。
    返回值：
        清理后的标题；缺失时返回空字符串。
    """
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match is None:
        return ""
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title[:120]


def find_feeds(html: str, base_url: str) -> list[str]:
    """查找 HTML 中声明的 RSS/Atom 地址。

    参数：
        html: 页面 HTML 文本。
        base_url: 页面地址，用于补全相对链接。
    返回值：
        去重后的 feed 绝对地址列表。
    """
    soup = BeautifulSoup(html, "html.parser")
    feeds = []
    for link_element in soup.find_all("link", rel="alternate"):
        feed_type = str(link_element.get("type", ""))
        href = str(link_element.get("href", "")).strip()
        if RSS_REL_PATTERN.search(feed_type) and href:
            feeds.append(urljoin(base_url, href))
    return list(dict.fromkeys(feeds))


def detect_generator(html: str, path: str) -> str:
    """识别常见建站程序，辅助判断页面结构。

    参数：
        html: 页面 HTML 文本。
        path: 页面路径，用于路径特征识别。
    返回值：
        识别到的程序名；未识别时返回 "unknown"。
    """
    lowered_html = html.lower()
    for pattern, name in GENERATOR_PATTERNS:
        if pattern in lowered_html or re.search(pattern, path, re.IGNORECASE):
            return name
    return "unknown"


def probe_url(
    url: str, session: requests.Session, cache_dir: Path | None
) -> dict:
    """请求单个站点并收集可访问性与 RSS 线索。

    参数：
        url: 目标地址。
        session: 复用连接的 requests 会话。
        cache_dir: 可选的 HTML 缓存目录。
    返回值：
        包含状态、标题、feed 与程序识别的结果字典。
    """
    result = {
        "url": url,
        "ok": False,
        "status": None,
        "final_url": url,
        "title": "",
        "feeds": [],
        "generator": "unknown",
        "error": "",
    }
    try:
        response = session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
            stream=True,
        )
        result["status"] = response.status_code
        result["final_url"] = response.url
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type:
            result["ok"] = True
            return result

        raw_chunks = []
        total_bytes = 0
        for chunk in response.iter_content(chunk_size=65536):
            raw_chunks.append(chunk)
            total_bytes += len(chunk)
            if total_bytes >= MAX_RESPONSE_BYTES:
                break
        html = b"".join(raw_chunks).decode("utf-8", errors="replace")
        result["ok"] = True
        result["title"] = extract_title(html)
        result["feeds"] = find_feeds(html, response.url)[:8]
        result["generator"] = detect_generator(html, urlparse(response.url).path)
        if cache_dir is not None:
            hostname = urlparse(response.url).netloc.replace(":", "_")
            (cache_dir / f"{hostname}.html").write_text(
                html, encoding="utf-8", errors="replace"
            )
    except requests.RequestException as error:
        result["error"] = type(error).__name__
    except Exception as error:
        result["error"] = type(error).__name__
    return result


def main() -> None:
    """逐个探测目标站点并输出 JSON 结果。

    参数：
        无（参数从命令行读取）。
    返回值：
        无。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_file", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    urls = extract_urls(args.source_file)
    if not urls:
        print("no urls found", file=sys.stderr)
        sys.exit(2)

    cache_dir = None
    if args.cache_dir is not None:
        cache_dir = args.cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

    def probe_one(url: str) -> dict:
        """在线程池中创建独立会话探测单个地址。"""
        with requests.Session() as session:
            return probe_url(url, session, cache_dir)

    results = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        future_to_index = {
            executor.submit(probe_one, url): index for index, url in enumerate(urls)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            result = future.result()
            results[index] = result
            status = result["status"] or result["error"] or "error"
            print(
                f"{status}\t{result['generator']}\t{len(result['feeds'])}\t{result['url']}"
            )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
