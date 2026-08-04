"""验证论坛索引解析的边界和去重行为。"""

from datetime import datetime, timezone

from app.parser import parse_forum_b_home_items, parse_forum_a_items


SAMPLE_HTML = """
<table>
  <tr class="tr3 t_one tac">
    <td class="tal"><h3><a id="t123" href="/htm_data/2607/16/123.html">帖子 A</a></h3></td>
    <td>10</td>
    <td><a class="bl">作者甲</a><div class="f12"><span data-timestamp="1710000000s"></span></div></td>
  </tr>
  <tr class="tr3 t_one tac">
    <td class="tal"><h3><a id="t123" href="/duplicate.html">重复帖子</a></h3></td>
    <td>5</td><td></td>
  </tr>
  <tr class="tr3 t_one tac">
    <td class="tal"><h3><a id="t456" href="/htm_data/2607/16/456.html">帖子 B</a></h3></td>
    <td>0</td><td><a class="bl">作者乙</a><div class="f12"><span title="置顶主题：2026-07-10 12:00:00"></span></div></td>
  </tr>
  <tr class="tr3 t_one tac">
    <td class="tal"><h3><a id="t999" href="/htm_data/2607/9/999.html">跨区置顶</a></h3></td>
    <td>0</td><td></td>
  </tr>
</table>
"""

IMAGE_FILTER_HTML = """
<table>
  <tr class="tr3 t_one tac">
    <td class="tal"><h3><a id="t1" href="/htm_data/2607/16/1.html">[原创]测试图片帖[36P]</a></h3></td>
    <td>10</td><td></td>
  </tr>
  <tr class="tr3 t_one tac">
    <td class="tal"><h3><a id="t2" href="/htm_data/2607/16/2.html">全角标记［22P+1V］</a></h3></td>
    <td>10</td><td></td>
  </tr>
  <tr class="tr3 t_one tac">
    <td class="tal"><h3><a id="t3" href="/htm_data/2607/16/3.html">纯文本求助帖</a></h3></td>
    <td>10</td><td></td>
  </tr>
</table>
"""


def test_parse_forum_a_items_extracts_metadata_and_deduplicates() -> None:
    """验证标题、绝对链接、作者、时间和 thread_id 去重。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    items = parse_forum_a_items(
        SAMPLE_HTML, "https://forum-a.example.com/thread0806.php?fid=16", 100
    )

    assert len(items) == 2
    assert items[0].thread_id == "123"
    assert items[0].link == "https://forum-a.example.com/htm_data/2607/16/123.html"
    assert items[0].author == "作者甲"
    assert items[0].published_at == datetime.fromtimestamp(1710000000, timezone.utc)
    assert items[1].published_at == datetime(2026, 7, 10, 4, 0, tzinfo=timezone.utc)


def test_parse_forum_a_items_handles_empty_or_invalid_input() -> None:
    """验证空页面、非法行和非正数上限不会产生条目。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    assert parse_forum_a_items("", "https://forum-a.example.com/", 10) == []
    assert parse_forum_a_items("<tr></tr>", "https://forum-a.example.com/", 10) == []
    assert parse_forum_a_items(SAMPLE_HTML, "https://forum-a.example.com/", 0) == []


def test_parse_forum_a_items_filters_pure_text_posts() -> None:
    """验证开启图片过滤后仅保留带图片/视频计数标记的帖子。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    items = parse_forum_a_items(
        IMAGE_FILTER_HTML,
        "https://forum-a.example.com/thread0806.php?fid=16",
        100,
        keep_image_posts_only=True,
    )

    assert [item.thread_id for item in items] == ["1", "2"]

HIGHLIGHT_HTML = """
<table>
  <thead class="category">
    <tr>
      <td><h3>≡ 最新精华 ≡</h3></td>
      <td><h3>≡ 最新点赞 ≡</h3></td>
      <td><h3>≡ 本周热门 ≡</h3></td>
      <td></td>
    </tr>
  </thead>
  <tr>
    <td>
      <div><a href="viewthread.php?tid=111">精华帖A</a></div>
      <div><a href="viewthread.php?tid=222">精华帖B</a></div>
    </td>
    <td>
      <div><a href="viewthread.php?tid=111">跨栏重复</a></div>
      <div><a href="viewthread.php?tid=333">点赞帖C</a></div>
    </td>
    <td>
      <div><a href="viewthread.php?tid=444">热门帖D</a></div>
    </td>
    <td></td>
  </tr>
</table>
"""


def test_parse_forum_b_home_items_extracts_three_blocks_and_deduplicates() -> None:
    """验证首页三栏解析、跨栏去重、标题前缀与规范链接。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    items = parse_forum_b_home_items(
        HIGHLIGHT_HTML, "https://forum-b.example.com/index.php", 100
    )

    assert [item.thread_id for item in items] == [
        "forum-b:home:111",
        "forum-b:home:222",
        "forum-b:home:333",
        "forum-b:home:444",
    ]
    assert items[0].title == "[最新精华] 精华帖A"
    assert items[0].link == "https://forum-b.example.com/viewthread.php?tid=111"
    assert items[0].author is None
    assert items[0].published_at is None
    assert items[2].title == "[最新点赞] 点赞帖C"
    assert items[3].title == "[本周热门] 热门帖D"


def test_parse_forum_b_home_items_handles_invalid_input() -> None:
    """验证空页面与缺失四格结构不会产生条目。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    assert parse_forum_b_home_items("", "https://forum-b.example.com/index.php", 10) == []
    assert parse_forum_b_home_items("<table></table>", "https://forum-b.example.com/", 10) == []
    assert parse_forum_b_home_items(HIGHLIGHT_HTML, "https://forum-b.example.com/", 0) == []