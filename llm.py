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


def chat_stream(messages):
    """把消息发给模型，流式返回回答（生成器：一段一段"吐"出来）。

    ★ 这是"打字机效果"的源头。
    和上面的 chat() 有什么区别？
      - chat() 要等模型把整段话生成完，一口气返回；
      - chat_stream() 是模型边生成、我们边收，收到一小段就 yield 出去，
        调用方（server.py）就能立刻转发给浏览器，一个字一个字显示。

    Python 里带 yield 的函数叫"生成器函数"：调用它不会立刻执行，
    而是返回一个"可以逐个取值的东西"，用 for 循环取一次才走一步。
    """
    # 1. 检查有没有填 Key（和 chat() 一样的提示）
    if not config.API_KEY:
        raise RuntimeError(
            "还没有填 API Key！请打开 config.py，把 API_KEY 填进引号里，然后重启服务。"
        )

    # 2. 请求体里多了 "stream": True
    #    官方文档原话：partial message deltas will be sent
    #    （会一段段地发送增量文本，最后以 data: [DONE] 结束）
    body = json.dumps(
        {
            "model": config.MODEL,
            "messages": messages,
            "stream": True,        # ★ 就靠这一行开启流式
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

    # 3. 一行一行地接收 SSE 流（Server-Sent Events 格式）
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            while True:
                raw_line = response.readline()
                if not raw_line:
                    break    # 连接结束

                line = raw_line.decode("utf-8").strip()

                # SSE 格式：数据行都以 "data: " 开头（其他行直接跳过）
                if not line.startswith("data: "):
                    continue

                payload = line[len("data: "):]
                if payload == "[DONE]":
                    break    # ★ 官方规定的结束标记

                chunk = json.loads(payload)
                piece = chunk["choices"][0]["delta"].get("content")
                # 说明：第一块的 delta 里只有角色没有文字；中间每块是一小段文字；
                # 最后一块的 delta 是空的（可能带用量统计）。所以用 .get 取、判空。
                if piece:
                    yield piece

    except urllib.error.HTTPError as e:
        # Key 填错了、余额不足、被限流等等
        detail = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"调用 API 失败（HTTP {e.code}）：{detail}") from e

    except urllib.error.URLError as e:
        # 网络不通
        raise RuntimeError(f"连不上 API 服务器，请检查网络：{e.reason}") from e
