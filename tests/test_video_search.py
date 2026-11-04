"""
tests/test_video_search.py —— 搜索工具的两条路径

对应的真实事故（2026-11-04）：`total` 只在 author 分支里赋值，
于是**每次关键词搜索都会在最后一行崩**（UnboundLocalError），
连崩 8 次，报错信息完全看不出根因。
"""

import tempfile
import unittest
from unittest import mock

import toolbox
import videokit.bili as bili
from tools import video_search


def _item(bvid="BV1xx411c7mD", title="标题", author="某UP", dur=605, pub=None):
    return {
        "bvid": bvid,
        "title": title,
        "author": author,
        "duration": dur,
        "view": 1000,
        "pubdate": pub or 0,
        "desc": "简介",
    }


class VideoSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        toolbox.set_workspace(self.tmp.name)
        toolbox.set_full_access(False)

    def tearDown(self):
        toolbox.set_workspace(None)
        self.tmp.cleanup()

    # ---------- 关键词路径（崩过的那条）----------
    def test_query_path_does_not_crash(self):
        with mock.patch.object(bili, "search", return_value=[_item(), _item("BV1yy411c7mD")]):
            out = video_search.run({"query": "RAG", "limit": 5})
        self.assertFalse(out.startswith("错误"), out[:200])
        self.assertIn("找到 2 条", out)
        self.assertIn("BV1xx411c7mD", out)

    def test_query_path_empty_result_is_a_normal_error(self):
        with mock.patch.object(bili, "search", return_value=[]):
            out = video_search.run({"query": "绝对搜不到的东西"})
        self.assertTrue(out.startswith("错误"))
        self.assertIn("没搜到", out)

    def test_query_passes_filters_down(self):
        with mock.patch.object(bili, "search", return_value=[_item()]) as m:
            video_search.run({"query": "x", "min_minutes": 10, "days": 2})
        kwargs = m.call_args.kwargs
        self.assertEqual(kwargs.get("min_minutes"), 10)
        self.assertEqual(kwargs.get("days"), 2)

    # ---------- 按作者路径 ----------
    def test_author_by_mid_reports_total(self):
        payload = {"items": [_item(), _item("BV1yy411c7mD")], "total": 142,
                   "mid": "123", "cached": True, "partial": False}
        with mock.patch.object(bili, "user_videos", return_value=payload):
            out = video_search.run({"author": "123", "limit": 5})
        self.assertIn("一共 142 个视频", out)
        self.assertIn("找到 2 条", out)

    def test_author_partial_is_disclosed(self):
        payload = {"items": [_item()], "total": 142, "mid": "123",
                   "cached": False, "partial": True}
        with mock.patch.object(bili, "user_videos", return_value=payload):
            out = video_search.run({"author": "123"})
        self.assertIn("被限流", out)

    # ---------- 名字消歧（防"抓到不相干的号"）----------
    def test_fuzzy_name_is_not_accepted_silently(self):
        """搜「海运日记」会返回「海燕的运动日记」——不能当成精确答案直接用。"""
        users = [{"mid": 87398027, "uname": "海燕的运动日记", "fans": 48, "videos": 142}]
        with mock.patch.object(bili, "search_user", return_value=users), \
             mock.patch.object(bili, "user_videos") as uv:
            out = video_search.run({"author": "海运日记"})
        self.assertIn("没找到叫「海运日记」", out)
        uv.assert_not_called()          # 关键：没有真的去拉那个号的作品

    def test_exact_name_is_accepted(self):
        users = [{"mid": 1, "uname": "影视飓风", "fans": 100, "videos": 10}]
        payload = {"items": [_item()], "total": 10, "mid": "1",
                   "cached": False, "partial": False}
        with mock.patch.object(bili, "search_user", return_value=users), \
             mock.patch.object(bili, "user_videos", return_value=payload):
            out = video_search.run({"author": "影视飓风"})
        self.assertIn("找到 1 条", out)


if __name__ == "__main__":
    unittest.main()
