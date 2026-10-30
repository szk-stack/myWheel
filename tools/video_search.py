"""
工具：video_search —— 按关键词或 UP 主搜 B站视频（只列候选，不下载）

设计上的一个刻意选择：**只返回元数据，不给推荐结论**。
选哪个视频是模型的判断——它得读标题、看时长、看简介才能决定。
工具一旦开始"帮你挑"，就会把用户带偏到一条没验证过的路上。
这和 list_dir 只列文件名、不替模型决定读哪个文件是同一个道理。
"""

import time

import config
import videokit.bili as bili
from toolbox import register

DESCRIPTION = {
    "name": "video_search",
    "description": (
        "在 B站 按关键词或 UP 主搜索视频，只返回候选清单（BV号、标题、时长、播放量、发布日期），不下载。"
        "先用它挑，再用 video_fetch 取具体某一条。"
        "可选参数：limit 返回几条（默认 15，最多 30）；min_minutes 只看时长不少于这个分钟数的；"
        "order 排序方式（totalrank 综合 / click 播放最多 / pubdate 最新）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词，如「RAG 原理」"},
            "author": {
                "type": "string",
                "description": "UP 主名字或 mid（空间地址 bilibili.com/space/<mid> 里那串数字）",
            },
            "limit": {"type": "integer", "description": "返回几条（默认 15，最多 30）"},
            "min_minutes": {
                "type": "integer",
                "description": "只要时长 ≥ 这个分钟数的（想系统学点什么就填 10）",
            },
            "order": {
                "type": "string",
                "enum": ["totalrank", "click", "pubdate"],
                "description": "排序：综合 / 播放 / 最新",
            },
        },
        "required": [],
    },
}


def _view(n):
    n = int(n or 0)
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _date(ts):
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d", time.localtime(int(ts)))


def _table(items):
    lines = ["| # | BV号 | 标题 | UP主 | 时长 | 播放 | 发布 |", "|---|---|---|---|---|---|---|"]
    for i, it in enumerate(items, 1):
        title = (it.get("title") or "").replace("|", "／")[:44]
        lines.append(
            f"| {i} | {it.get('bvid', '')} | {title} | {(it.get('author') or '')[:12]} "
            f"| {_fmt_dur(it.get('duration'))} | {_view(it.get('view'))} | {_date(it.get('pubdate'))} |"
        )
    return "\n".join(lines)


def _fmt_dur(sec):
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def run(arguments):
    query = (arguments.get("query") or "").strip()
    author = (arguments.get("author") or "").strip()
    if not query and not author:
        return "错误：query 和 author 至少给一个"

    limit = min(int(arguments.get("limit") or 15), 30)
    min_minutes = int(arguments.get("min_minutes") or 0)
    order = arguments.get("order") or "totalrank"

    try:
        if author:
            # 名字 → mid；已经是数字就直接用
            mid = author
            if not author.isdigit():
                users = bili.search_user(author, limit=5)
                if not users:
                    return f"错误：没搜到叫「{author}」的 UP 主"
                if len(users) > 1 and users[0].get("uname") != author:
                    rows = "\n".join(
                        f"- {u['uname']}（mid={u['mid']}，{_view(u['fans'])}粉丝，{u['videos']}个视频）"
                        for u in users
                    )
                    return (
                        f"「{author}」匹配到多个 UP 主，请用 mid 再调一次：\n\n{rows}"
                    )
                mid = users[0]["mid"]
            items = bili.user_videos(mid, limit=limit, order="pubdate")
            if min_minutes:
                items = [x for x in items if (x.get("duration") or 0) >= min_minutes * 60]
            title = f"UP主 mid={mid} 的视频"
        else:
            items = bili.search(query, limit=limit, order=order, min_minutes=min_minutes)
            title = f"关键词「{query}」"
    except Exception as e:
        return f"错误：搜索失败：{e}"

    if not items:
        extra = "（试试把 min_minutes 调小，或换个关键词）" if min_minutes else ""
        return f"错误：{title} 没搜到结果{extra}"

    return (
        f"{title} 找到 {len(items)} 条：\n\n{_table(items)}\n\n"
        "→ 用 video_fetch 传 BV号取其中一条（不要一次取很多条，先挑）。"
    )


if getattr(config, "ENABLE_VIDEO", True):
    register(DESCRIPTION, run)
