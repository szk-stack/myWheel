"""
tools/_common.py —— 文件工具们共用的小帮手

（说明：下划线开头的文件不会被当成"工具"加载，它只是内部模块，
  给 list_dir / read_file / write_file / edit_file 这些工具共用。）
"""

from pathlib import Path

# 工具能碰到的范围 = 项目根目录（也就是 tools/ 文件夹的上一级）。
# 所有文件路径都会被"关"在这个文件夹里——这是文件工具的第一安全红线。
WORKSPACE = Path(__file__).resolve().parent.parent


def safe_path(user_path):
    """把模型给的路径转成真实路径，并检查它没有"越狱"。

    模型给的 path 是像 "static/index.html" 这样的相对路径。
    这里要防的是它用 "../.." 之类的写法跑到项目文件夹外面去。

    规则：算出来的真实路径必须还在项目目录里面，否则报错。
    报错信息会作为工具结果还给模型，它看到后可以自己换个路径重试。
    """
    # 不传路径就当作项目根目录（比如 list_dir 不带参数时）
    candidate = WORKSPACE / (user_path or ".")

    # resolve() 会把 ".."、"." 和符号链接都算清楚，得到最终的真实路径
    real = candidate.resolve()

    # ★ 安全检查：必须落在项目目录里面，否则拒了
    if not real.is_relative_to(WORKSPACE):
        raise ValueError(f"路径越界：「{user_path}」超出了项目文件夹范围")

    return real
