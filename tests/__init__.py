"""
tests 包入口 —— 保证没有 config.py 的干净检出也能跑测试。

config.py 被 .gitignore 排除了，CI / 新克隆的仓库里通常没有它。
envconfig.apply() 会用 config.example.py 兜底并登记成 sys.modules["config"]，
必须赶在任何 test 模块 import config 之前执行；包初始化正好是最早的加载点
（pytest 和 unittest discover 都会先走到这里）。
"""

import envconfig

envconfig.apply()
