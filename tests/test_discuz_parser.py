"""验证 论坛 B 板块列表解析和置顶过滤。"""

from datetime import datetime, timezone

from app.parser import parse_forum_b_items


SAMPLE_HTML = """
<table>
  <tbody id="stickthread_900">
    <tr><th class="subject"><span id="thread_900"><a href="viewthread.php?tid=900">置顶</a></span></th></tr>
  </tbody>
  <tbody id="normalthread_123">
    <tr>
      <th class="subject"><span id="thread_123"><a href="viewthread.php?tid=123&amp;extra=page%3D1">主题 A</a></span></th>
      <td class="author"><cite><a>作者甲</a></cite><em>2026-7-11</em></td>
    </tr>
  </tbody>
  <tbody id="normalthread_456">
    <tr>
      <th class="subject"><span id="thread_456"><a href="viewthread.php?tid=456">主题 B</a></span></th>
      <td class="author"><cite><a>作者乙</a></cite><em>无效日期</em></td>
    </tr>
  </tbody>
  <tbody id="normalthread_789">
    <tr><th class="subject"><span id="thread_789"><a href="viewthread.php?tid=000">ID 不一致</a></span></th></tr>
  </tbody>
</table>
"""

IMAGE_FILTER_HTML = """
<table>
  <tbody id="normalthread_111">
    <tr>
      <th class="subject"><span id="thread_111"><a href="viewthread.php?tid=111">带图帖</a></span>
        <img src="images/attachicons/image_s.gif" alt="图片附件" class="attach" />
      </th>
    </tr>
  </tbody>
  <tbody id="normalthread_222">
    <tr>
      <th class="subject"><span id="thread_222"><a href="viewthread.php?tid=222">纯文本帖</a></span></th>
    </tr>
  </tbody>
  <tbody id="normalthread_333">
    <tr>
      <th class="subject"><span id="thread_333"><a href="viewthread.php?tid=333">仅加分类图标</a></span>
        <img src="images/default/agree.gif" class="attach" alt="帖子被加分" />
      </th>
    </tr>
  </tbody>
</table>
"""


def test_parse_forum_b_items_extracts_normal_threads_only() -> None:
    """验证普通主题字段、规范链接、GUID 及置顶排除。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    items = parse_forum_b_items(
        SAMPLE_HTML, "https://forum-b.example.com/forumdisplay.php?fid=19", 100
    )

    assert len(items) == 2
    assert items[0].thread_id == "forum-b:19:123"
    assert items[0].link == "https://forum-b.example.com/viewthread.php?tid=123"
    assert items[0].author == "作者甲"
    assert items[0].published_at == datetime(2026, 7, 10, 16, 0, tzinfo=timezone.utc)
    assert items[1].published_at is None


def test_parse_forum_b_items_handles_invalid_input() -> None:
    """验证缺少 fid、空页面和非正数上限不会生成条目。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    assert parse_forum_b_items("", "https://forum-b.example.com/?fid=19", 10) == []
    assert parse_forum_b_items(SAMPLE_HTML, "https://forum-b.example.com/", 10) == []
    assert parse_forum_b_items(SAMPLE_HTML, "https://forum-b.example.com/?fid=19", 0) == []


def test_parse_forum_b_items_filters_pure_text_posts() -> None:
    """验证开启图片过滤后仅保留带图片附件图标的帖子。

    参数：
        无。
    返回值：
        无；断言失败时由 pytest 报错。
    """
    items = parse_forum_b_items(
        IMAGE_FILTER_HTML,
        "https://forum-b.example.com/forumdisplay.php?fid=19",
        100,
        keep_image_posts_only=True,
    )

    assert [item.thread_id for item in items] == ["forum-b:19:111"]
