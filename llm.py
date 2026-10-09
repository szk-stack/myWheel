"""
llm.py —— 负责和 DeepSeek 服务器说话

只用 Python 自带的工具（标准库），不用安装任何东西。
它的工作：把完整对话历史发过去，把 AI 的回答拿回来。
"""

import json
import urllib.error
import urllib.request

import config


def chat(messages):
    """把完整的消息列表发给模型，返回 AI 的回答（字符串）。

    messages 的格式像这样：
        [{"role": "user", "content": "你好"},
         {"role": "assistant", "content": "你好呀！"},
         {"role": "user", "content": "再讲个笑话"}]
    """

    # 1. 检查有没有填 Key，没填就给一句人话提示
    if not config.API_KEY:
        raise RuntimeError(
            "还没有填 API Key！请打开 config.py，把 API_KEY 填进引号里，然后重启服务。"
        )

    # 2. 按照 DeepSeek（OpenAI 兼容格式）的要求拼出请求内容
    body = json.dumps(
        {
            "model": config.MODEL,
            "messages": messages,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        config.API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + config.API_KEY,
        },
        method="POST",
    )

    # 3. 发出去，拿回答
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    except urllib.error.HTTPError as e:
        # Key 填错了、余额不足、被限流等等，都会走到这里
        detail = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"调用 API 失败（HTTP {e.code}）：{detail}") from e

    except urllib.error.URLError as e:
        # 网络不通（没联网、开了代理等等）
        raise RuntimeError(f"连不上 API 服务器，请检查网络：{e.reason}") from e
