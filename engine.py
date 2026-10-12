"""
engine.py —— 引擎：把整个流程串起来

一次对话的完整流程（升级成了"agent 循环"）：

    before_llm 空槽
        ↓
    [ 调模型（流式）→ 想调工具？→ 执行工具 → 结果塞回历史 → 再调模型 …… ]  ← 循环
                          └─ 不想调了 → 直接回答 → 结束
        ↓
    after_llm 空槽

引擎自己不干具体活儿，只负责"按顺序调度"。
具体功能由插件挂在空槽上、工具登记在 toolbox 注册表里。
"""

import importlib
import json
import pkgutil

import config
import hooks
import llm
import toolbox


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


def chat_stream(messages, workspace=None):
    """流式版对话流程（生成器）。

    参数 workspace：本次对话的工作区目录（None = 默认项目目录）——
    由用户在前端设定、随请求传进来；注意：模型没有权力改它，
    它只是被锁在工作区里干活的人。

    ★ 这次升级成了"agent 循环"——从"一问一答"变成"多轮干活"：

        调模型（流式）──► 模型直接回答？ → 结束
              │
              └─► 模型说"我要用工具" → 执行工具 → 结果塞回历史 → 再调模型 →……

    产出的事件（都是字典）：
      {"type": "text", "text": "..."}                           文字增量
      {"type": "tool_call", "name": "...", "arguments": {...}}  开始执行某个工具
      {"type": "tool_result", ...}                              工具执行完毕（带结果）

    两个从流式改造时延续下来的取舍（没变）：
      - before_llm 照常工作（可以改 messages）
      - after_llm 拿得到完整回答，但改不了已经流出去的内容（只服务记录类用途）
    """
    ctx = {
        "messages": messages,
        "reply": None,
        "meta": {},
    }

    # ★ 第 0 步：先把工作区告诉工具箱——接下来所有工具都被锁在它里面干活
    #   （用"线程本地"记录：多线程服务器里，不同对话的请求互不串台）
    toolbox.set_workspace(workspace)

    # ① 调模型之前：before_llm 插件照常工作
    hooks.run(hooks.before_llm, ctx)

    # 准备工具"说明书"（发不发给模型，看 config 里的总开关）
    tools_desc = toolbox.all_descriptions() if config.ENABLE_TOOLS else []

    # 工作副本：下面的工具对话都追加在它身上，不动原来的 messages 列表
    work_messages = list(ctx["messages"])

    # ② agent 循环
    text_pieces = []      # 攒完整回答（给 after_llm 用）
    used_rounds = 0       # 已经执行过几轮工具

    while True:
        pending_calls = None

        # 调一次模型（流式），把文字一段段往外转交
        for event in llm.chat_stream(work_messages, tools=tools_desc):
            if event["type"] == "text":
                text_pieces.append(event["text"])
                yield event                     # 文字直接往外流
            elif event["type"] == "tool_calls":
                pending_calls = event["tool_calls"]

        if not pending_calls:
            break    # 模型正常答完了 → 收工

        # 保险丝：模型连续调工具的轮数超上限就停（防死循环烧钱）
        if used_rounds >= config.MAX_TOOL_ROUNDS:
            print(f"[警告] 工具调用达到上限（{config.MAX_TOOL_ROUNDS} 轮），停止继续执行")
            break
        used_rounds += 1

        # ③ 按官方协议，把"模型要求调工具"这件事记进工作历史
        #    （OpenAI 兼容格式：assistant 消息带 tool_calls 字段）
        work_messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
                for call in pending_calls
            ],
        })

        # 逐个执行这次要求的工具（模型可能一次要调好几个）
        for call in pending_calls:
            # 模型给的参数是 JSON 字符串，解析成字典
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}

            # 告诉外面："要开始执行工具了"（前端用来显示状态）
            yield {"type": "tool_call", "name": call["name"], "arguments": arguments}

            result = toolbox.call(call["name"], arguments)   # ★ 真正执行！

            # 告诉外面："执行完了"（前端用来显示结果、并记账）
            yield {
                "type": "tool_result",
                "name": call["name"],
                "arguments": arguments,
                "ok": not result.startswith("错误"),
                "result": result,
            }

            # 按官方协议把结果塞回工作历史（role=tool + 对应的 tool_call_id）
            work_messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result,
            })

    # ④ 循环结束：把完整回答交给 after_llm（记录类用途，前面解释过）
    ctx["reply"] = "".join(text_pieces)
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
