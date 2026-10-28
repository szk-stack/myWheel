"""
工具：search_kb —— 在知识库里检索

【它和 read_file / grep 的分工】
  - grep / read_file  → 查**代码**：找某个函数在哪、某个变量叫什么
  - search_kb         → 查**文档**：某个机制是怎么设计的、为什么这么设计、
                        某个概念在本项目里是什么意思

【这属于 RAG 的哪一段】
整个 RAG 有"检索 + 生成"两半。这个工具只负责"检索"——把相关片段捞出来。
生成那一半交给主模型：它拿到片段后自己组织答案。
这叫 "Agentic RAG"：不是由代码写死"每次都先检索"，而是模型自己判断
"这问题我需要查一下知识库"，然后主动调用工具。好处是省 token、也更灵活。
"""

import config
import rag
from toolbox import register

# 单块正文最多返回多少字（防止一块就把上下文吃掉一半）
MAX_CHUNK_CHARS = 1200

# 一次检索总共最多返回多少字
MAX_TOTAL_CHARS = getattr(config, "KB_MAX_OUTPUT", 4000)


# ---- ① 工具说明书 ----
DESCRIPTION = {
    "name": "search_kb",
    "description": (
        "在项目知识库里检索文档片段。知识库包含 docs/ 下的技术文档"
        "和 knowledge/ 下的学习资料。"
        "当用户问「这个项目是怎么实现的」「某个机制的原理/参数/为什么这么设计」"
        "「某个概念是什么意思」时，先用它查一下再回答，不要凭印象猜。"
        "注意：查具体代码（函数在哪、变量叫什么）请用 grep 或 read_file，"
        "本工具查的是文档和笔记。"
        "如果第一次没搜到，换个更具体的关键词再试一次。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "检索关键词。用名词短语比用完整问句效果好，"
                    "比如搜「空槽机制 注册」而不是「工具是怎么注册的呢」"
                ),
            },
            "top_k": {
                "type": "integer",
                "description": "返回几块，默认 5。找不到时可以先调大到 10 看看",
            },
        },
        "required": ["query"],
    },
}


# ---- ② 执行函数 ----
def run(arguments):
    query = (arguments.get("query") or "").strip()
    if not query:
        return "错误：query 不能为空，请给出要搜的关键词"

    top_k = arguments.get("top_k")
    try:
        top_k = int(top_k) if top_k else None
    except (TypeError, ValueError):
        top_k = None

    hits = rag.search(query, top_k)

    # 索引还没建
    if hits is None:
        return (
            "知识库索引不存在。请让用户在项目目录下运行：\n"
            "    python rag.py build\n"
            "建好索引后再检索。"
        )

    if not hits:
        info = rag.stats() or {}
        return (
            f"知识库里没有找到和「{query}」相关的内容"
            f"（库里共 {info.get('chunks', '?')} 块）。\n"
            "可以试试：换成更通用的词、去掉口语化的部分，或者用 top_k=10 再搜一次。"
        )

    # ---- 拼装结果 ----
    info = rag.stats() or {}
    lines = [
        f"知识库检索「{query}」命中 {len(hits)} 块"
        f"（全库 {info.get('chunks', '?')} 块 / {info.get('files', '?')} 个文件）：",
        "",
    ]

    used = 0
    for i, h in enumerate(hits, 1):
        body = h["text"]
        truncated = ""
        if len(body) > MAX_CHUNK_CHARS:
            body = body[:MAX_CHUNK_CHARS]
            truncated = "\n…（本块还有后续，需要的话用 read_file 读原文）"

        block = (
            f"【{i}】{h['source']} · {h['heading']}\n"
            f"第 {h['line']} 行起 · 相关度 {h['score']}\n"
            f"{'-' * 46}\n"
            f"{body}{truncated}\n"
        )

        if used + len(block) > MAX_TOTAL_CHARS:
            lines.append(f"（还有 {len(hits) - i + 1} 块因长度限制未显示）")
            break

        lines.append(block)
        used += len(block)

    lines.append("提示：回答时请标注引用的来源（文件名 + 章节）。"
                 "如果这些片段没答到点上，换个关键词再搜一次。")

    return "\n".join(lines)


# ---- ③ 登记进注册表 ----
# （总开关关掉时就不登记——模型连这个工具都看不到）
if getattr(config, "ENABLE_KB", False):
    register(DESCRIPTION, run)
