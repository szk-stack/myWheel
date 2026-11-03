"""
工具：video_search —— 按关键词或 UP 主搜 B站视频（只列候选，不下载）

设计上的一个刻意选择：**只返回元数据，不给推荐结论**。
选哪个视频是模型的判断——它得读标题、看时长、看简介才能决定。
工具一旦开始"帮你挑"，就会把用户带偏到一条没验证过的路上。
这和 list_dir 只列文件名、不替模型决定读哪个文件是同一个道理。
"""

import time

import config
import toolbox
import videokit.bili as bili
from toolbox import register
from videokit import store

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
                "description": (
                    "UP 主名字或 mid（bilibili.com/space/<mid> 里那串数字）。"
                    "用于「把这个 UP 主的东西都看一遍」这类批量需求。"
                    "★ 这个接口被限流，每翻一页要等 7 秒左右，且结果缓存一天；"
                    "知道 mid 就直接给 mid，省掉一次名字搜索。"
                ),
            },
            "limit": {
                "type": "integer",
                "description": "返回几条（默认 15）。关键词搜索最多 30；按作者列作品最多 200（一页 30 条，每页约 7 秒）",
            },
            "min_minutes": {
                "type": "integer",
                "description": "只要时长 ≥ 这个分钟数的（想系统学点什么就填 10）",
            },
            "days": {
                "type": "integer",
                "description": "只要最近 N 天发布的（0 = 不限）。「这两天的视频」就填 2",
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


def _name_match(query, name):
    """名字算不算对上了。

    ★ 为什么需要：B站的用户搜索**从不返回空**——搜"海运日记"它会给你
      "海燕的运动日记"，搜一串乱码也可能匹配到某个号。
      只判断 len(users)==1 就采用，等于把"唯一的模糊结果"当成"精确答案"，
      这是之前那个"找不到那个 UP 主却差点抓错人"的根因。
    """
    q, n = (query or "").strip(), (name or "").strip()
    return bool(q and n) and (q == n or q in n or n in q)


def run(arguments):
    query = (arguments.get("query") or "").strip()
    author = (arguments.get("author") or "").strip()
    if not query and not author:
        return "错误：query 和 author 至少给一个"

    # 关键词搜索最多 3 页（约 60 条）；按作者列作品可以要更多（一页 30 条、每页 7 秒）
    raw_limit = int(arguments.get("limit") or 15)
    limit = min(raw_limit, 200 if author else 30)
    min_minutes = int(arguments.get("min_minutes") or 0)
    days = int(arguments.get("days") or 0)
    order = arguments.get("order") or "totalrank"
    root = toolbox.current_workspace() or str(toolbox.PROJECT_ROOT)

    # ★ 这两个必须**在分支之前**初始化：
    #   total 原本只在 author 分支里赋值，结果 query 搜索走到最后那行
    #   `if total and ...` 就炸了（UnboundLocalError）——
    #   实测线上就是这样连续失败 8 次的，而且报错信息完全看不出根因。
    items, total = [], 0

    try:
        if author:
            # 名字 → mid；已经是数字就直接用
            mid = author
            uname = author          # 真实昵称拿到后会用搜索结果覆盖
            note = ""
            if not author.isdigit():
                try:
                    users = bili.search_user(author, limit=5)
                except Exception as e:
                    users = []
                    note = f"UP 主搜索不可用（{str(e)[:60]}）；"

                if not users:
                    # ★ 降级而不是报错：名字定位不到时，按关键词搜视频。
                    #   理由见文件顶部——把"要不要重试"的判断留在模型手里，
                    #   它只会换 limit 数字反复撞同一堵墙。
                    items = bili.search(
                        author, limit=limit, order="totalrank", min_minutes=min_minutes
                    )
                    if not items:
                        return f"错误：没搜到「{author}」相关的 UP 主或视频，换个说法或给个 BV号试试"
                    return (
                        f"{note}没定位到「{author}」这个 UP 主，已按**关键词**搜视频，"
                        f"结果里可能包含 TA 的作品：\n\n{_table(items)}\n\n"
                        "→ 找到目标后把 BV号 传给 video_fetch。"
                        "如果确实要 TA 的全部作品，给我空间地址里的 mid（bilibili.com/space/<mid>）。"
                    )

                top = users[0]
                if not _name_match(author, top.get("uname")):
                    rows = "\n".join(
                        f"- {u['uname']}（mid={u['mid']}，{_view(u['fans'])}粉丝，{u['videos']}个视频）"
                        for u in users
                    )
                    return (
                        f"没找到叫「{author}」的 UP 主，B站只给了这些相近的：\n\n{rows}\n\n"
                        "→ 先别当成就是它。三条路：\n"
                        "① 确实是其中之一 → 用它的 mid 再调一次；\n"
                        "② 给我 UP 主页链接（bilibili.com/space/<mid>）或任一视频链接，我从视频反查 mid；\n"
                        "③ 不确定号名 → 改用 query 按主题搜。"
                    )
                mid = top["mid"]
                uname = top.get("uname") or uname

            if mid:
                cache_dir = store.videos_root(root) / "_authors"
                try:
                    res = bili.user_videos(
                        mid, limit=limit, order="pubdate", cache_dir=cache_dir, days=days
                    )
                    items, total = res["items"], res["total"]
                    if res.get("cached"):
                        note += "列表来自今天的缓存；"
                    if res.get("partial"):
                        note += "后面几页被限流了，先给到这些；"
                except Exception as e:
                    note += f"作品列表没取到（{str(e)[:40]}）；"

            if not items:
                try:
                    pool = bili.search(uname, limit=30, order="totalrank")
                except Exception as e:
                    return f"错误：搜索失败：{e}"
                items = [x for x in pool if uname and uname in (x.get("author") or "")]
                note += f"已改用关键词搜索并按作者名「{uname}」过滤，可能不全。"
                if not items:
                    # ★ 这里必须说清"重试没用"：
                    #   否则模型会把 412 当成临时抖动，换个 limit 反复撞同一堵墙
                    #   （实测连撞 5 次，全是同一个 412）。
                    return (
                        f"错误：拿不到「{uname}」的作品列表——B站对这个接口有频控"
                        "（HTTP 412 / 风控 -352），连着用同样参数重试只会继续被拦。\n"
                        "可选：① 在 config.py 填 VIDEO_BILI_COOKIE（登录后复制 SESSDATA），"
                        "这条路径会稳很多；② 直接给我一个视频链接或 BV号，我取那一条；"
                        "③ 改用 query 关键词按主题搜（不按作者）。"
                    )

            if items and uname.isdigit():
                uname = items[0].get("author") or uname   # 用作品里的作者名当标题
            if min_minutes:
                items = [x for x in items if (x.get("duration") or 0) >= min_minutes * 60]
            title = f"「{uname}」的视频" + (f"（{note.rstrip('，')}）" if note else "")
        else:
            items = bili.search(query, limit=limit, order=order, min_minutes=min_minutes, days=days)
            title = f"关键词「{query}」"
    except Exception as e:
        return f"错误：搜索失败：{e}"

    if not items:
        extra = "（试试把 min_minutes 调小，或换个关键词）" if min_minutes else ""
        return f"错误：{title} 没搜到结果{extra}"

    head = f"{title} 找到 {len(items)} 条"
    if total and total > len(items):
        head += f"（TA 一共 {total} 个视频，按发布时间倒序取了前 {len(items)} 个）"

    if author:
        tail = (
            "→ 批量处理就逐个把 BV号 传给 video_fetch（它会自己走字幕优先）；"
            "要往后取就把 limit 调大，每页约 7 秒。"
        )
    else:
        tail = "→ 用 video_fetch 传 BV号取其中一条（不要一次取很多条，先挑）。"

    return f"{head}：\n\n{_table(items)}\n\n{tail}"


if getattr(config, "ENABLE_VIDEO", True):
    register(DESCRIPTION, run)
