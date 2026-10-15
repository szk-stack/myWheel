"""
工具：spawn_agent —— 派一个"子代理"去独立干活

这是"多 agent"的第一块积木，形态叫"主从式"：
  主 agent（正在跟你对话的那个）可以把一项任务"派"给一个子代理。
  子代理有它自己的独立上下文（看不见主对话），能自己调用各种工具
  （读文件、查目录、写文件……），干完之后只把一份精炼的结论带回来。

为什么有用？
  那些"要翻十几个文件、中间过程一大堆"的调查类任务，耗费的上下文
  全部留在子代理自己的"小本本"里，主对话只多一条结论——主对话的
  窗口因此保持清爽。
  （这和"上下文压缩"正好互补：压缩是事后收拾桌子；子代理是事前
    派实习生去跑腿——你的桌子根本乱不了。）

实现思路（理解本文件的关键）：
  没有造新引擎！run() 里直接把主引擎的流水线（engine.chat_stream）
  整段"消费"掉：子代理的对话发进去，内部照样走"调模型 ⇄ 执行工具"
  的循环，我们只负责收集它吐出来的文字，拼成最终结论。
"""

from collections import Counter

import config
import engine
import toolbox
from toolbox import register

# ---- ① 工具说明书（会发给模型看）----
DESCRIPTION = {
    "name": "spawn_agent",
    "description": (
        "派一个子代理去独立完成一项任务，然后只把它的结论带回来。"
        "适合「要翻很多文件、走很多步、但只需要结论」的调查类任务"
        "（比如：把项目里所有 TODO 找出来、总结某个模块的结构）。"
        "中间过程的上下文消耗会隔离在子代理自己的上下文里，主对话保持清爽。"
        "注意：子代理看不到你们当前的对话——任务描述要写完整、说清要什么。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "给子代理的任务描述：让它干什么、做完回报什么，越具体越好",
            }
        },
        "required": ["task"],
    },
}


# ---- ② 执行函数（模型点名时真正运行）----
def run(arguments):
    """派一个子代理干活：跑完一整段"迷你对话"，把结论带回来。"""
    task = (arguments.get("task") or "").strip()
    if not task:
        return "错误：任务描述（task）不能为空"

    # 子代理的"起点"：它自己的专属人设 + 主代理给的任务。
    # 这是一个全新的消息数组——和主对话完全隔离（它看不到主对话）
    sub_messages = [
        {"role": "system", "content": config.SUBAGENT_PROMPT},
        {"role": "user", "content": task},
    ]

    # ★ 防套娃：子代理干活期间，把 spawn_agent 自己禁用掉——
    #   小弟不能再招小弟（不然可能无限套娃、烧钱无底洞）
    old_disabled = toolbox.set_disabled({"spawn_agent"})

    text_buf = ""        # 正在累积的这一段文字
    final_answer = ""    # 最终结论（最后一段文字）
    tool_names = []      # 记下每次调用的工具名（整理"过程脚印"用）

    try:
        # ★ 核心：把主引擎的流水线整段跑完（"消费"这个生成器）。
        #   两个讲究：
        #   ① workspace 传"当前线程的工作区"原值——子代理继承主对话的
        #      工作区（注意：不传会把工作区清成默认值，不能图省事）
        #   ② skip_persona=True——子代理自带人设，不要再插项目人设
        for event in engine.chat_stream(
            sub_messages,
            workspace=toolbox.current_workspace(),
            skip_persona=True,
        ):
            if event["type"] == "text":
                text_buf += event["text"]
            elif event["type"] == "tool_call":
                # 要调工具了：把刚攒的文字"封段"（那多半是过程话）
                if text_buf:
                    final_answer = text_buf
                    text_buf = ""
                tool_names.append(event["name"])
            # 其他事件（tool_result 等）子代理内部消化，不需要往外传

        # 循环正常结束：最后一段没封段的文字 = 子代理的最终结论
        if text_buf:
            final_answer = text_buf

    finally:
        # 不管中途出没出错，都必须把禁用名单恢复（主对话还要继续用）
        toolbox.set_disabled(old_disabled)

    if not final_answer:
        # 兜底：子代理什么都没说（罕见——多半是中途出错了）
        return "错误：子代理没有产出结论（可能中途执行出错，可看服务端日志）"

    # 整理"过程脚印"：子代理干了多少活，让主 agent 和日志心里有数
    if tool_names:
        counts = Counter(tool_names)
        detail = "、".join(
            f"{name}×{n}" if n > 1 else name for name, n in counts.items()
        )
        footprint = f"\n\n——（子代理本次调用工具 {len(tool_names)} 次：{detail}）"
    else:
        footprint = "\n\n——（子代理本次没有调用工具）"

    return "【子代理报告】\n" + final_answer + footprint


# ---- ③ 登记进注册表 ----
# （总开关关掉时就不登记——模型连这个工具都看不到）
if config.ENABLE_SUBAGENT:
    register(DESCRIPTION, run)
