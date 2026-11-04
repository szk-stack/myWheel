"""
tests/test_prompt_layers.py —— 系统提示词的分层加载

三个文件各管一段（抄 Pi 的 AGENTS.md / SYSTEM.md）：
  SYSTEM.md  整份替换人设
  AGENTS.md  追加项目规矩，从外到内叠加（越靠里越具体，覆盖外面的）
"""

import tempfile
import unittest
from pathlib import Path

import config
import toolbox
import plugins.system_prompt as sp


class PromptLayersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.old_root = toolbox.PROJECT_ROOT
        toolbox.PROJECT_ROOT = self.root          # 把"项目根"临时挪到沙盘里
        toolbox.set_workspace(None)

        (self.root / "sub").mkdir()
        self.sub = self.root / "sub"

    def tearDown(self):
        toolbox.PROJECT_ROOT = self.old_root
        toolbox.set_workspace(None)
        self.tmp.cleanup()

    # ---------- 基础 ----------
    def test_no_files_uses_default_persona(self):
        prompt = sp.build_prompt()
        self.assertIn(sp.PERSONA_MARK, prompt)
        self.assertIn(config.SYSTEM_PROMPT.strip()[:20], prompt)
        self.assertNotIn("<project-instructions>", prompt)

    # ---------- AGENTS.md ----------
    def test_agents_md_is_appended(self):
        (self.root / "AGENTS.md").write_text("- 回答用中文", encoding="utf-8")
        prompt = sp.build_prompt()
        self.assertIn("<project-instructions>", prompt)
        self.assertIn("回答用中文", prompt)

    def test_layers_go_outside_in(self):
        """项目根的在前面，工作区的在后面——后者更具体，应该压后。"""
        (self.root / "AGENTS.md").write_text("外层规矩", encoding="utf-8")
        (self.sub / "AGENTS.md").write_text("内层规矩", encoding="utf-8")
        toolbox.set_workspace(str(self.sub))

        prompt = sp.build_prompt()
        self.assertLess(prompt.index("外层规矩"), prompt.index("内层规矩"))

    def test_workspace_outside_project_only_takes_itself(self):
        """工作区在项目外时，只认它自己那一层，别把无关父目录扫进来。"""
        outside = Path(tempfile.mkdtemp())
        try:
            (outside / "AGENTS.md").write_text("外部目录的规矩", encoding="utf-8")
            toolbox.set_workspace(str(outside))
            prompt = sp.build_prompt()
            self.assertIn("外部目录的规矩", prompt)
        finally:
            import shutil

            shutil.rmtree(outside, ignore_errors=True)

    def test_empty_file_is_ignored(self):
        (self.root / "AGENTS.md").write_text("   \n\n", encoding="utf-8")
        self.assertNotIn("<project-instructions>", sp.build_prompt())

    # ---------- SYSTEM.md ----------
    def test_system_md_replaces_persona(self):
        (self.sub / "SYSTEM.md").write_text("你是海运行业分析师。", encoding="utf-8")
        toolbox.set_workspace(str(self.sub))

        prompt = sp.build_prompt()
        self.assertIn("你是海运行业分析师", prompt)
        self.assertNotIn(config.SYSTEM_PROMPT.strip()[:20], prompt)

    def test_inner_system_md_wins(self):
        (self.root / "SYSTEM.md").write_text("外层人设", encoding="utf-8")
        (self.sub / "SYSTEM.md").write_text("内层人设", encoding="utf-8")
        toolbox.set_workspace(str(self.sub))

        prompt = sp.build_prompt()
        self.assertIn("内层人设", prompt)
        self.assertNotIn("外层人设", prompt)

    # ---------- 结构 ----------
    def test_order_persona_then_agents_then_environment(self):
        (self.root / "AGENTS.md").write_text("规矩", encoding="utf-8")
        prompt = sp.build_prompt()
        self.assertLess(prompt.index("<persona>"), prompt.index("<project-instructions>"))
        self.assertLess(prompt.index("<project-instructions>"), prompt.index("<environment>"))


if __name__ == "__main__":
    unittest.main()
