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


def _convert_tool_history(messages):
    """把历史里的"工具记录"转换成模型协议格式。

    我们平时把工具记录存成一条 role=tool 的条目（方便前端显示），
    但发给模型时要还原成标准的"成对"格式：
        assistant（带 tool_calls）  +  tool（结果）
    这样模型就能"看到"它之前读过什么文件、执行过什么——
    不用每次都重新读一遍。
    """
    out = []
    for m in messages:
        if m.get("role") == "tool":
            call_id = f"hist_{len(out)}"   # 历史调用的编号（只需在本次请求内成对）
            out.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": m.get("name", ""),
                        "arguments": json.dumps(m.get("arguments") or {}, ensure_ascii=False),
                    },
                }],
            })
            out.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": m.get("result") or "",
            })
        else:
            out.append(m)
    return out


def chat_stream(messages, workspace=None, compaction=None, skip_persona=False):
    """流式版对话流程（生成器）。

    参数 workspace：本次对话的工作区目录（None = 默认项目目录）——
    由用户在前端设定、随请求传进来；注意：模型没有权力改它，
    它只是被锁在工作区里干活的人。

    参数 compaction：会话的上下文压缩状态（{"summary", "first_kept_index"} 或 None）
    —— 由前端随请求带来，放进 ctx["meta"] 供 compact 插件使用。

    参数 skip_persona：跳过"项目人设"（默认 False）。
    —— 子代理（spawn_agent 工具）用它：子代理有自己的专门人设
    （config.SUBAGENT_PROMPT），不希望项目人设再混进去。

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

    # ★ 第 0.5 步：把"压缩状态"放进数据袋——compact 插件用它来
    #   "应用"已有的压缩（切成"摘要 + 最近消息"）
    ctx["meta"]["compaction"] = compaction

    # ★ 第 0.6 步：把"跳过项目人设"标记放进数据袋——
    #   system_prompt 插件看到它就收手（子代理自带人设）
    ctx["meta"]["skip_persona"] = skip_persona

    # ① 调模型之前：before_llm 插件照常工作
    #    （其中 compact 插件可能在这里做"上下文压缩"——它会应用旧压缩，
    #      并在超阈值时生成新摘要，结果记在 ctx["meta"] 里）
    hooks.run(hooks.before_llm, ctx)

    # ★ 如果刚才发生了压缩，把这件事告诉前端（记账 + 显示压缩卡片 + 用量回落）
    if ctx["meta"].get("new_compaction"):
        new_c = ctx["meta"]["new_compaction"]
        yield {
            "type": "compacted",
            "summary": new_c["summary"],
            "first_kept_index": new_c["first_kept_index"],
            "tokens_before": ctx["meta"].get("tokens_before_compact", 0),
        }

    # 准备工具"说明书"（发不发给模型，看 config 里的总开关）
    tools_desc = toolbox.all_descriptions() if config.ENABLE_TOOLS else []

    # 工作副本：先把历史里的"工具记录"转换成模型能读懂的标准格式
    # （工具结果从此会跟着上下文发给模型——它不用重复读文件了），
    # 之后的工具对话也追加在它身上，不动原来的 messages 列表
    work_messages = _convert_tool_history(ctx["messages"])

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
