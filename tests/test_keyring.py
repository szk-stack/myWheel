"""界面配置 API Key 的离线测试。

重点验证三件事：
  1. 保存后 `config.API_KEY` 立刻生效（llm.py / cli.py 读的就是它），不用重启；
  2. status() 绝不能把明文 Key 漏给前端；
  3. 取消 / 无效值（空串、掩码串）不能把真 Key 覆盖掉。

所有测试都把 KEY_FILE 指向临时目录、并在结束后还原 config.API_KEY，
不会碰真实的 config.py，也不会在项目里留下 api_key.json。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import keyring


class KeyringTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key_file = Path(self.tmp.name) / "api_key.json"
        self._patch_file = mock.patch.object(keyring, "KEY_FILE", self.key_file)
        self._patch_file.start()
        self._orig_key = config.API_KEY
        config.API_KEY = ""

    def tearDown(self):
        config.API_KEY = self._orig_key
        self._patch_file.stop()
        self.tmp.cleanup()

    def test_empty_status(self):
        d = keyring.status()
        self.assertFalse(d["configured"])
        self.assertEqual(d["masked"], "")
        self.assertEqual(d["source"], "none")

    def test_key_from_config_py_is_reported_as_config(self):
        config.API_KEY = "sk-super-secret-123456"
        d = keyring.status()
        self.assertTrue(d["configured"])
        self.assertEqual(d["source"], "config")

    def test_save_writes_file_and_takes_effect_immediately(self):
        d = keyring.save("  sk-new-key-abcdef  ")
        self.assertTrue(keyring.KEY_FILE.exists())
        # ★ 关键：不重启就生效
        self.assertEqual(config.API_KEY, "sk-new-key-abcdef")
        self.assertTrue(d["configured"])
        self.assertEqual(d["source"], "saved")
        saved = json.loads(self.key_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["api_key"], "sk-new-key-abcdef")  # 首尾空格已去掉

    def test_status_never_leaks_plaintext(self):
        secret = "sk-THIS-MUST-NOT-LEAK-998877"
        keyring.save(secret)
        payload = json.dumps(keyring.status(), ensure_ascii=False)
        self.assertNotIn(secret, payload)
        self.assertNotIn("998877", payload)
        self.assertNotIn("THIS-MUST", payload)
        # 掩码必须是纯星号，连头尾字符都不带
        self.assertTrue(set(keyring.status()["masked"]) <= {"•"})

    def test_reject_empty_and_mask_values(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                keyring.save(bad)
        for bad in ("•" * 16, "****************", "···"):
            with self.assertRaises(ValueError):
                keyring.save(bad)
        # 一次都没成功过 → 文件不该被创建，真 Key 也不会被覆盖
        self.assertFalse(keyring.KEY_FILE.exists())

    def test_mask_cannot_overwrite_a_real_key(self):
        keyring.save("sk-real-key")
        with self.assertRaises(ValueError):
            keyring.save(keyring.MASK)
        self.assertEqual(config.API_KEY, "sk-real-key")

    def test_load_into_config_restores_saved_key(self):
        keyring.save("sk-from-ui")
        config.API_KEY = ""          # 模拟重启：进程内存里的值没了
        keyring.load_into_config()
        self.assertEqual(config.API_KEY, "sk-from-ui")

    def test_saved_key_wins_over_config_py(self):
        keyring.save("sk-ui-new")
        config.API_KEY = "sk-config-old"   # 模拟 config.py 里的旧值
        keyring.load_into_config()
        self.assertEqual(config.API_KEY, "sk-ui-new")

    def test_load_into_config_ignores_broken_file(self):
        self.key_file.write_text("not json at all", encoding="utf-8")
        config.API_KEY = "sk-from-config-py"
        keyring.load_into_config()   # 不抛异常，也不覆盖
        self.assertEqual(config.API_KEY, "sk-from-config-py")

    def test_load_into_config_no_file_is_noop(self):
        config.API_KEY = "sk-from-config-py"
        keyring.load_into_config()
        self.assertEqual(config.API_KEY, "sk-from-config-py")


if __name__ == "__main__":
    unittest.main()
