"""
tests/test_guard.py —— 工具失败闸

对应的真实事故：B站风控那晚，模型把 412 当成临时抖动，
换个数原样重撞 5 次，一分钱信息都没多。
"""

import unittest

import toolbox


class FailureGuardTest(unittest.TestCase):
    def setUp(self):
        toolbox.reset_fail_streak()
        self.calls = 0

        def boom(args):
            self.calls += 1
            return "错误：模拟失败"

        def fine(args):
            self.calls += 1
            return "成功"

        toolbox.register({"name": "_t_boom", "description": "", "parameters": {}}, boom)
        toolbox.register({"name": "_t_fine", "description": "", "parameters": {}}, fine)

    def _boom(self, args=None):
        return toolbox.call("_t_boom", args or {"x": 1})

    def test_third_identical_call_is_blocked(self):
        """前两次真执行，第三次直接拦下（不再调底层函数）。"""
        self.assertTrue(self._boom().startswith("错误：模拟失败"))
        self.assertTrue(self._boom().startswith("错误：模拟失败"))
        self.assertEqual(self.calls, 2)

        third = self._boom()
        self.assertIn("同一调用已连续失败 2 次", third)
        self.assertEqual(self.calls, 2, "被拦下的调用不应该真的执行")

    def test_blocked_call_keeps_blocking(self):
        """拦下之后继续撞，仍然是拦下的（不会莫名其妙放行一次）。"""
        self._boom()
        self._boom()
        for _ in range(3):
            self.assertIn("同一调用已连续失败", self._boom())
        self.assertEqual(self.calls, 2)

    def test_different_args_are_allowed(self):
        """改一个参数就算新尝试——这正是我们希望模型做的事。"""
        self._boom()
        self._boom()
        self._boom({"x": 2})
        self.assertEqual(self.calls, 3)

    def test_arg_order_does_not_matter(self):
        """{"a":1,"b":2} 和 {"b":2,"a":1} 算同一个调用。"""
        toolbox.call("_t_boom", {"a": 1, "b": 2})
        toolbox.call("_t_boom", {"b": 2, "a": 1})
        self.assertIn("同一调用已连续失败", toolbox.call("_t_boom", {"a": 1, "b": 2}))

    def test_success_resets_streak(self):
        """中间成功过一次，计数清零，后面还能再失败两次才拦。"""
        self._boom()                       # 1
        self._boom()                       # 2
        toolbox.call("_t_fine", {})        # 3（成功也算一次执行）→ 清零
        self._boom()                       # 4（真执行）
        self._boom()                       # 5（真执行）
        self.assertEqual(self.calls, 5)
        self.assertIn("同一调用已连续失败", self._boom())

    def test_reset_between_conversations(self):
        """新对话重置：上一个话题的失败不该连坐。"""
        self._boom()
        self._boom()
        toolbox.reset_fail_streak()
        self._boom()
        self.assertEqual(self.calls, 3)

    def test_other_call_breaks_the_streak(self):
        """只认"连续"：中间插了别的调用，就不再是同一串连击。"""
        self._boom()
        self._boom()
        toolbox.call("_t_fine", {})        # 插一条别的
        self._boom()                       # 又能执行了
        self.assertEqual(self.calls, 4)


if __name__ == "__main__":
    unittest.main()
