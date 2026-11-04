"""
tests/test_envconfig.py —— 容器化部署相关的回归测试

覆盖三件最容易「本机跑得好好的、上容器就崩」的事：
  1. 容器里没有 config.py 时，能不能从 config.example.py 兜底；
  2. MYWHEEL_* 环境变量能不能覆盖白名单配置、且不会误注入陌生名字；
  3. MYWHEEL_DATA_DIR 能不能让数据路径整体搬家；环境变量里的 Key
     优先级高于界面保存的 api_key.json。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import envconfig
import keyring


class EnvconfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        # stop() 会把 os.environ 恢复原样，不污染别的测试
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for k in [k for k in os.environ if k.startswith("MYWHEEL_")]:
            del os.environ[k]

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_default_data_dir_is_project_root(self):
        """没设环境变量时，一切路径和行为都跟改动前一样。"""
        self.assertEqual(envconfig.data_dir(), envconfig.ROOT)
        self.assertEqual(envconfig.uploads_dir(), envconfig.ROOT / "uploads")

    def test_falls_back_to_example_when_config_py_is_missing(self):
        """容器里 clone 下来没有 config.py ——必须从模板兜底，不能崩在 import。

        用子进程 + 只放两个文件的沙盘目录来模拟：本进程 sys.path 上还挂着
        本机（甚至工作区里）的 config.py，光删 sys.modules 删不干净。
        """
        shutil.copy(envconfig.ROOT / "envconfig.py", self.tmp_path / "envconfig.py")
        shutil.copy(
            envconfig.ROOT / "config.example.py", self.tmp_path / "config.example.py"
        )
        self.assertFalse((self.tmp_path / "config.py").exists())

        code = (
            "import sys, envconfig;"
            "c = envconfig.apply();"
            "assert sys.modules['config'] is c;"
            "assert c.__file__.endswith('config.example.py'), c.__file__;"
            "print(c.HOST, c.PORT, repr(c.API_KEY))"
        )
        env = dict(os.environ)
        env.update({
            "MYWHEEL_HOST": "0.0.0.0",
            "MYWHEEL_PORT": "9001",
            "MYWHEEL_API_KEY": "sk-from-env",
            "MYWHEEL_DATA_DIR": str(self.tmp_path / "data"),
        })
        r = subprocess.run(
            [sys.executable, "-c", code], cwd=str(self.tmp_path), env=env,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0.0.0.0 9001 'sk-from-env'", r.stdout)
        self.assertTrue((self.tmp_path / "data").is_dir())

    def test_env_overrides_whitelisted_values_with_types(self):
        os.environ.update({
            "MYWHEEL_HOST": "0.0.0.0",
            "MYWHEEL_PORT": "9001",
            "MYWHEEL_API_KEY": "sk-from-env",
            "MYWHEEL_ENABLE_VIDEO": "false",
            "MYWHEEL_COMPACT_THRESHOLD_RATIO": "0.5",
        })
        with mock.patch.object(config, "HOST", "127.0.0.1", create=True), \
             mock.patch.object(config, "PORT", 8000), \
             mock.patch.object(config, "API_KEY", ""), \
             mock.patch.object(config, "ENABLE_VIDEO", True), \
             mock.patch.object(config, "COMPACT_THRESHOLD_RATIO", 0.7):
            cfg = envconfig.apply()
            self.assertEqual(cfg.HOST, "0.0.0.0")
            self.assertEqual(cfg.PORT, 9001)
            self.assertIsInstance(cfg.PORT, int)      # 不是字符串 "9001"
            self.assertEqual(cfg.API_KEY, "sk-from-env")
            self.assertIs(cfg.ENABLE_VIDEO, False)     # 不是字符串 "false"
            self.assertEqual(cfg.COMPACT_THRESHOLD_RATIO, 0.5)

    def test_unlisted_env_var_is_ignored(self):
        """拼错的环境变量名不该悄悄变成一个新配置项。"""
        os.environ["MYWHEEL_TOTALLY_MADE_UP"] = "boom"
        envconfig.apply()
        self.assertFalse(hasattr(config, "TOTALLY_MADE_UP"))

    def test_env_key_wins_over_saved_key_file(self):
        """部署时显式注入的 Key，别被数据卷里的旧 api_key.json 盖回去。"""
        keyfile = self.tmp_path / "api_key.json"
        keyfile.write_text(json.dumps({"api_key": "sk-from-file"}), encoding="utf-8")
        os.environ["MYWHEEL_API_KEY"] = "sk-from-env"
        with mock.patch.object(config, "API_KEY", "sk-from-env"), \
             mock.patch.object(keyring, "KEY_FILE", keyfile):
            keyring.load_into_config()
            self.assertEqual(config.API_KEY, "sk-from-env")

    def test_data_paths_move_together_in_a_fresh_process(self):
        """模块级常量在 import 时求值，用全新进程验证写入点确实一起搬家。"""
        code = (
            "import envconfig, storage, tracing, keyring, engine, server;"
            "d = envconfig.data_dir();"
            "assert storage.LOG_FILE.parent == d, storage.LOG_FILE;"
            "assert tracing.TRACE_FILE.parent == d, tracing.TRACE_FILE;"
            "assert keyring.KEY_FILE.parent == d, keyring.KEY_FILE;"
            "assert envconfig.uploads_dir() == d / 'uploads';"
            "print('OK')"
        )
        env = dict(os.environ)
        env["MYWHEEL_DATA_DIR"] = str(self.tmp_path / "data")
        r = subprocess.run(
            [sys.executable, "-c", code], cwd=str(envconfig.ROOT), env=env,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
