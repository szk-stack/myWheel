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


def chat_stream(messages):
    """流式版对话流程（生成器）：边收边把文字段交给调用方。

    ★ 一个要理解的架构取舍：
      流式输出时，每个字都已经实时"泼"到用户屏幕上了，
      after_llm 插件再也没法修改已经显示出来的内容。
      我们的处理是：
        - 分段照常流出去（用户要的打字机效果）
        - 流结束后，把完整回答交给 after_llm 跑一遍——
          供"记录、统计、日志"类插件使用（它们的修改不会再影响用户）。
      将来若要做"实时过滤"（如敏感词逐字打码），需要在每个分段上做，
      那是以后加一个 on_chunk 空槽的课题。
    """
    ctx = {
        "messages": messages,
        "reply": None,
        "meta": {},
    }

    # ① 调模型之前：before_llm 插件照常工作（可以改 messages）
    hooks.run(hooks.before_llm, ctx)

    # ② 流式调用模型：收到一段就转交给调用方（server 会立刻发给浏览器）
    pieces = []
    for piece in llm.chat_stream(ctx["messages"]):
        pieces.append(piece)
        yield piece

    # ③ 流结束后：拼出完整回答，交给 after_llm（只用于记录类用途）
    ctx["reply"] = "".join(pieces)
    try:
        hooks.run(hooks.after_llm, ctx)
    except Exception as e:
        # after_llm 插件出错不影响已经发出的内容，打个警告就好
        print(f"[警告] after_llm 插件出错：{e}")


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
