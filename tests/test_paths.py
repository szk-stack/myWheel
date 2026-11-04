"""
tests/test_paths.py —— 路径围栏

对应两件事：
  1. 沙箱：agent 只能在会话的工作区里读写（越界要明确拒绝）；
  2. 「完整权限」开关：打开后可以出去，但仍然不许碰盘符根目录/系统目录。
"""

import tempfile
import unittest
from pathlib import Path

import toolbox
from tools._common import safe_path


class SafePathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        toolbox.set_workspace(str(self.root))
        toolbox.set_full_access(False)
        (self.root / "inside.txt").write_text("ok", encoding="utf-8")

    def tearDown(self):
        toolbox.set_full_access(False)
        toolbox.set_workspace(None)
        self.tmp.cleanup()

    def test_inside_workspace(self):
        self.assertEqual(safe_path("inside.txt"), self.root / "inside.txt")

    def test_relative_escape_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            safe_path("../../something.txt")
        self.assertIn("超出了工作区范围", str(ctx.exception))

    def test_absolute_outside_is_rejected(self):
        outside = Path(tempfile.gettempdir()).resolve() / "definitely-outside.txt"
        with self.assertRaises(ValueError):
            safe_path(str(outside))

    def test_full_access_allows_outside(self):
        toolbox.set_full_access(True)
        outside = Path(tempfile.gettempdir()).resolve() / "definitely-outside.txt"
        self.assertEqual(safe_path(str(outside)), outside)

    def test_full_access_still_blocks_drive_root(self):
        """放开的是"范围"，不是"保险"——盘符根目录仍然拒绝。"""
        toolbox.set_full_access(True)
        with self.assertRaises(ValueError) as ctx:
            safe_path("C:/")
        self.assertIn("不合规", str(ctx.exception))

    def test_full_access_off_again(self):
        toolbox.set_full_access(True)
        toolbox.set_full_access(False)
        with self.assertRaises(ValueError):
            safe_path("../escape.txt")


if __name__ == "__main__":
    unittest.main()
