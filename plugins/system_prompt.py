"""
插件示例①：在调模型之前，插入"系统提示词"

系统提示词就是给 AI 定人设的那句话，比如"你是一个翻译助手"。
人设写在 config.py 的 SYSTEM_PROMPT 里，由本插件负责插进对话最前面。
"""

import config
from hooks import before_llm


def add_system_prompt(ctx):
    """把系统提示词插到消息列表的最前面。"""
    messages = ctx["messages"]

    # 如果最前面还没有系统提示词，就插一条
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": config.SYSTEM_PROMPT})


# ★ 注册：把函数挂到"调模型前"这个空槽上（功能就这么加上了）
before_llm.append(add_system_prompt)
