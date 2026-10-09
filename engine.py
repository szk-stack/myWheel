"""
engine.py —— 引擎：把整个流程串起来

一次对话的完整流程（关键就这几行）：

    before_llm 空槽  →  调用模型  →  after_llm 空槽

引擎自己不干具体活儿，只负责"按顺序调度"。
具体功能都由插件挂在空槽上实现。
"""

import importlib
import pkgutil

import hooks
import llm


def chat(messages):
    """处理一次对话，返回 AI 的回答。"""

    # 上下文：在整条流程里传递的"共享数据袋"
    # 三个键的含义：
    #   messages —— 完整对话历史（插件可以修改它）
    #   reply    —— AI 的回答（调用模型之后才有值）
    #   meta     —— 插件之间传递临时数据用（现在还没用到）
    ctx = {
        "messages": messages,
        "reply": None,
        "meta": {},
    }

    # ① 调模型之前：执行 before_llm 槽里的所有插件
    hooks.run(hooks.before_llm, ctx)

    # ② 调用 DeepSeek 模型（用的是插件处理过的 messages）
    ctx["reply"] = llm.chat(ctx["messages"])

    # ③ 模型回答之后：执行 after_llm 槽里的所有插件
    hooks.run(hooks.after_llm, ctx)

    # ④ 把最终回答返回出去
    return ctx["reply"]


def load_plugins():
    """自动加载 plugins/ 文件夹里的所有插件。

    原理：把 plugins/ 里每个 .py 文件都"执行"一遍。
    插件文件里那行 append 代码会随之执行，函数就自动挂到空槽上了。

    所以以后加功能 = 往 plugins/ 扔一个文件，别的都不用管。
    """
    import plugins

    for info in pkgutil.iter_modules(plugins.__path__):
        if info.name.startswith("_"):  # 跳过 __init__ 之类的内部文件
            continue
        importlib.import_module(f"plugins.{info.name}")
        print(f"[插件] 已加载：{info.name}")
