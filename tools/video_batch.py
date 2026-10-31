"""
工具：video_batch —— 批量处理某个 UP 主的视频

为什么单独一个工具，而不是让模型自己循环调 video_fetch / video_transcribe：
  一个 142 个视频的号，逐个调就是 284 次工具调用。上下文先炸，
  模型也会在中途迷失"我处理到第几个了"。批处理应该是**一次调用干一批**。

★ 三个关键设计：

1. **进度不另存状态文件，直接看文件系统。**
   transcript.md 在 = 转好了；audio.m4a 在 = 取过了。
   这和项目里"日志是唯一真相"是同一个思路：状态是推导出来的，不是维护出来的。
   好处是手动删掉某个目录、或者中途崩了，下次自动接上，不会错位。

2. **按时间预算切批，而不是按条数。**
   转写一个 10 分钟视频要 1~2 分钟，条数没法预估耗时。给一个 budget_seconds，
   到点就停、把剩下的列出来，模型可以再调一次继续。

3. **单个失败不中断整批。**
   批处理里最忌讳"第 7 个挂了，前 6 个白干"。这里逐条 try，
   失败的标红记在表里，继续下一个。
"""

import time

import config
import toolbox
import videokit.bili as bili
from toolbox import register
from tools import video_fetch as _fetch_tool
from tools import video_transcribe as _asr_tool
from videokit import store, timeline

DESCRIPTION = {
    "name": "video_batch",
    "description": (
        "批量处理某个 UP 主的视频：按时间预算一次处理一批，可反复调用续跑。"
        "先用 video_search(author=...) 看清单，再用它批量取/转。"
        "参数：author（mid 或名字）、limit（这轮最多处理几个，默认 10）、"
        "min_minutes（只看这个时长以上的）、"
        "action（fetch 只取字幕/音轨；transcribe 取+转写，默认）、"
        "budget_seconds（本轮时间上限，默认 300，最大 900）。"
        "已处理过的会自动跳过，所以重复调用就是「继续」的意思。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "author": {"type": "string", "description": "UP 主 mid 或名字"},
            "limit": {"type": "integer", "description": "本轮最多处理几个（默认 10，最多 50）"},
            "min_minutes": {"type": "integer", "description": "只看时长 ≥ 这个分钟数的"},
            "max_minutes": {
                "type": "integer",
                "description": "只看时长 ≤ 这个分钟数的（批量时用来避开几小时的超长视频）",
            },
            "action": {
                "type": "string",
                "enum": ["fetch", "transcribe"],
                "description": "fetch 只取字幕/音轨；transcribe 取完顺手转写",
            },
            "budget_seconds": {
                "type": "integer",
                "description": "本轮时间上限（默认 300，最大 900）——到点就停，剩下的下次继续",
            },
        },
        "required": ["author"],
    },
}


def _done(folder, path_name):
    return (folder / path_name).exists()


def run(arguments):
    author = (arguments.get("author") or "").strip()
    if not author:
        return "错误：author 不能为空"

    limit = min(int(arguments.get("limit") or 10), 50)
    min_minutes = int(arguments.get("min_minutes") or 0)
    max_minutes = int(arguments.get("max_minutes") or 0)
    action = arguments.get("action") or "transcribe"
    budget = min(int(arguments.get("budget_seconds") or 300), 900)

    root = toolbox.current_workspace() or str(toolbox.PROJECT_ROOT)
    started = time.time()

    # ① 先把清单拿到（走缓存，通常秒回）
    mid = author
    uname = author
    if not author.isdigit():
        try:
            users = bili.search_user(author, limit=5)
        except Exception as e:
            return f"错误：找不到这个 UP 主（{str(e)[:50]}）"
        if not users:
            return f"错误：没搜到叫「{author}」的 UP 主，给我 mid 更稳"
        mid, uname = users[0]["mid"], users[0].get("uname") or author

    try:
        listing = bili.user_videos(
            mid, limit=200, order="pubdate",
            cache_dir=store.videos_root(root) / "_authors",
        )
    except Exception as e:
        return f"错误：拿不到作品列表：{e}"

    videos = listing["items"]
    if videos and uname == mid:
        uname = videos[0].get("author") or uname      # 用作品里的昵称，别显示 mid
    if min_minutes:
        videos = [v for v in videos if (v.get("duration") or 0) >= min_minutes * 60]
    if max_minutes:
        videos = [v for v in videos if (v.get("duration") or 0) <= max_minutes * 60]
    if not videos:
        return f"错误：「{uname}」没有符合条件的视频（min_minutes={min_minutes}）"

    # ② 挑出还没做的
    pending = []
    for v in videos:
        folder = store.videos_root(root) / v["bvid"]
        already = _done(folder, "transcript.md") if action == "transcribe" else (
            _done(folder, "transcript.md") or _done(folder, "audio.m4a")
        )
        if not already:
            pending.append(v)

    if not pending:
        return (
            f"「{uname}」这 {len(videos)} 个视频都已经处理完了（action={action}）。\n"
            "要重做某一个，单独用 video_fetch / video_transcribe 并传 force。"
        )

    # ③ 按预算处理
    rows, ok, fail, stopped = [], 0, 0, False
    for v in pending[:limit]:
        if time.time() - started > budget:
            stopped = True
            break

        bvid, title = v["bvid"], (v.get("title") or "")[:30]
        status = ""
        try:
            fetched = _fetch_tool.run({"url": bvid})
            if fetched.startswith("错误"):
                status = "取失败：" + fetched[3:40]
                fail += 1
            elif action == "transcribe":
                converted = _asr_tool.run({"video_id": bvid})
                if converted.startswith("错误"):
                    status = "转写失败：" + converted[3:40]
                    fail += 1
                else:
                    status = "✅ 已转写"
                    ok += 1
            else:
                status = "✅ 已取（字幕或音轨）"
                ok += 1
        except Exception as e:
            status = f"异常：{str(e)[:36]}"
            fail += 1

        rows.append(f"| {len(rows) + 1} | {bvid} | {title} | {timeline.fmt_ts(v.get('duration'))} | {status} |")

    left = len(pending) - len(rows)
    elapsed = time.time() - started

    head = (
        f"「{uname}」批量处理（action={action}）：本轮完成 {ok} 个"
        + (f"、失败 {fail} 个" if fail else "")
        + f"，耗时 {elapsed:.0f} 秒\n"
        f"清单共 {len(videos)} 个（本轮开始前已完成 {len(videos) - len(pending)} 个），"
        f"还剩 {left} 个没处理"
    )
    table = (
        "| # | BV号 | 标题 | 时长 | 结果 |\n|---|---|---|---|---|\n" + "\n".join(rows)
        if rows else "（本轮没有处理任何视频）"
    )

    if left > 0:
        tail = (
            f"\n\n→ 再调一次 video_batch（参数不变）就会从第 {len(rows) + 1} 个继续。"
            + ("（本轮是到时间预算了才停的）" if stopped else "")
        )
    else:
        tail = "\n\n→ 全部处理完了。需要汇总时读各条 videos/<BV号>/transcript.md。"

    return f"{head}\n\n{table}{tail}"


if getattr(config, "ENABLE_VIDEO", True):
    register(DESCRIPTION, run)
