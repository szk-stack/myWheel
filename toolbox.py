"""
toolbox.py —— 工具箱（本项目第五块积木）

它管三件事：
  1. 【注册表】所有"工具"的登记处
  2. 【调度员】执行模型要求的工具调用
  3. 【自动加载】启动时把 tools/ 文件夹里的每个 .py 都执行一遍

★ 先说清楚"工具"到底是什么：
  大模型自己不会读文件、也不会写文件。它唯一的能力是"输出文字"——
  所以它只能通过"说话"来请求干活，比如：
      "帮我调用 read_file，参数是 {"path": "README.md"}"
  真正动手的是我们写好的 Python 函数。
  所以一个"工具"由两部分组成：
      ① 说明书（name/description/parameters）—— 发给模型看，帮它判断什么时候该用
      ② 执行函数（run）                      —— 模型点名时，我们真正去执行

★ 为什么要有注册表？（这就是给未来留的"空缺"）
  不管是今天本地写的工具，还是将来从 MCP 服务器拉来的工具、
  从 Skill 带来的工具——只要按同样的格式"登记"进来，
  引擎调用它们时就一视同仁、代码一行都不用改。
"""

import importlib
import pkgutil

import hooks

# 注册表：{ 工具名: {"description": 说明书字典, "run": 执行函数, "source": 来源} }
_registry = {}


def register(description, run, source="builtin"):
    """登记一个工具（每个工具文件在末尾调用它）。

    参数：
      description —— 工具说明书，字典格式：
                     {"name": "...", "description": "...", "parameters": {...}}
      run         —— 真正干活的函数：接收一个参数字典，返回一个字符串。
                     约定：成功返回结果文字；失败返回以"错误"开头的说明
                     （不要抛异常——让模型看到错误、自己调整）
      source      —— 工具来源："builtin"（本地自带）；
                     将来接 MCP 时会是 "mcp:服务器名"，接 Skill 时是 "skill:技能名"
    """
    _registry[description["name"]] = {
        "description": description,
        "run": run,
        "source": source,
    }


def all_descriptions():
    """取出所有工具的"说明书"，转换成 API 要求的格式，发给模型。

    ★ 注意这里做了一次"包装"（第一次测试时踩过坑）：
      API 要求的每条工具长这样——
          {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
      而工具文件里写的是里面那一层（更简洁、更好写）。
      这个"内部格式 → API 格式"的转换层就固定放在这里——
      将来接 MCP 时也一样：远端工具的格式先转换成本地格式，
      到这里再统一包一层，引擎不用关心工具从哪来。
    """
    return [
        {"type": "function", "function": item["description"]}
        for item in _registry.values()
    ]


def has(name):
    """注册表里有没有叫这个名字的工具？"""
    return name in _registry


def call(name, arguments):
    """执行一个工具调用，返回结果字符串。

    arguments 是模型给出的参数字典（从它输出的 JSON 字符串解析而来）。
    这里的任何异常都会被转成"给模型看的错误文本"——
    让模型自己看到错误、自己调整策略，而不是让整个对话崩掉。
    """
    if name not in _registry:
        return f"错误：没有叫「{name}」的工具"

    # 路过预留的"工具执行前"空槽（将来的权限确认、审计插件挂在这里）
    ctx = {"name": name, "arguments": arguments, "blocked": None}
    hooks.run(hooks.before_tool, ctx)
    if ctx["blocked"]:
        return f"工具调用被拦截：{ctx['blocked']}"

    try:
        return _registry[name]["run"](arguments)
    except Exception as e:
        return f"错误：工具执行失败：{e}"


def load_tools():
    """自动加载 tools/ 文件夹里的所有工具。

    原理和 plugins/ 一模一样：把每个 .py 文件"执行"一遍，
    文件末尾那行 register(...) 就会把工具登记进注册表。
    所以以后加工具 = 往 tools/ 扔一个文件，别的都不用管。
    """
    import tools as tools_package

    for info in pkgutil.iter_modules(tools_package.__path__):
        if info.name.startswith("_"):   # 跳过 __init__ 和 _common 这类内部文件
            continue
        importlib.import_module(f"tools.{info.name}")
        print(f"[工具] 已加载：{info.name}")
