"""
tests/test_cli.py —— 命令行入口的参数解析

只测"怎么理解命令行"这层，不碰网络和模型——
真正跑一次 agent 要花 token，不适合放进单元测试。
"""

import unittest
from pathlib import Path

import cli


class ParseArgsTest(unittest.TestCase):
    def test_plain_prompt(self):
        a = cli.parse_args(["把视频转成文字"])
        self.assertEqual(a.prompt, ["把视频转成文字"])
        self.assertFalse(a.quiet)
        self.assertFalse(a.json)
        self.assertFalse(a.full_access)

    def test_flags(self):
        a = cli.parse_args(["-p", "--json", "--full-access", "-w", "D:/x", "做事"])
        self.assertTrue(a.quiet)
        self.assertTrue(a.json)
        self.assertTrue(a.full_access)
        self.assertEqual(a.workspace, "D:/x")

    def test_dash_is_a_positional(self):
        """`-` 要能被当成"从 stdin 读"，不能被 argparse 当成选项。"""
        a = cli.parse_args(["-"])
        self.assertEqual(a.prompt, ["-"])

    def test_multiword_prompt_is_joined(self):
        """不加引号时会被 shell 拆成多个参数，这里要能拼回去。"""
        a = cli.parse_args(["把", "这个", "视频", "转成", "文字"])
        self.assertEqual(cli.resolve_prompt(a, stdin_text=""), "把 这个 视频 转成 文字")


class ResolvePromptTest(unittest.TestCase):
    def test_argv_wins(self):
        a = cli.parse_args(["来自参数"])
        self.assertEqual(cli.resolve_prompt(a, stdin_text="来自stdin"), "来自参数")

    def test_dash_reads_stdin(self):
        a = cli.parse_args(["-"])
        self.assertEqual(cli.resolve_prompt(a, stdin_text="来自stdin"), "来自stdin")

    def test_empty_reads_stdin(self):
        a = cli.parse_args([])
        self.assertEqual(cli.resolve_prompt(a, stdin_text=" 管道内容 \n"), "管道内容")

    def test_all_empty(self):
        a = cli.parse_args([])
        self.assertEqual(cli.resolve_prompt(a, stdin_text=""), "")


class WorkspaceTest(unittest.TestCase):
    def test_default_is_cwd(self):
        a = cli.parse_args(["做事"])
        self.assertEqual(cli._workspace_of(a), str(Path.cwd()))

    def test_explicit_workspace_is_resolved(self):
        a = cli.parse_args(["-w", ".", "做事"])
        self.assertEqual(cli._workspace_of(a), str(Path(".").resolve()))

    def test_project_flag_points_at_repo(self):
        a = cli.parse_args(["--project", "做事"])
        ws = Path(cli._workspace_of(a))
        self.assertTrue((ws / "engine.py").exists())
        self.assertTrue((ws / "cli.py").exists())


if __name__ == "__main__":
    unittest.main()
