"""
工具：video_fetch —— 取一个 B站视频的文字来源

★ 默认路线：**下音轨，交给 video_transcribe 跑 ASR**（不是字幕优先）。

为什么改过来了（2026-11-01，之前的设计是"字幕优先"）：
  1. 字幕地址**时有时无**：同一个视频连调三次，两次返回空 subtitle_url，
     只有一次能下。要重试到拿稳，比直接转写还慢。
  2. 字幕可能**残缺**：拿到过一份只覆盖 3 分 18 秒的 AI 字幕（全长 17 分钟），
     而且看不出哪里缺了——这种"看起来成功了、其实丢了大半"最坑。
  3. 字幕是**第三方加工过的产物**（断句、改写、漏句），
     我们要的是"这个视频说了什么"，音频才是原始素材。
  4. 本地转写已经够快（RTF 0.11，17 分钟视频 114 秒），省这点算力不值当。

  字幕保留为**可选**（source="subtitle"）：想零误差、或者转写服务不可用时能用。

★ 仍然不下视频本体：转文字用不上画面，一个 10 分钟的视频几百 MB，
  为了文字去拖几百 MB 是这类工具最常见的浪费。等阶段 3 做关键帧再按需下载。
"""

import config
import toolbox
import videokit.bili as bili
from toolbox import register
from videokit import store, timeline

DESCRIPTION = {
    "name": "video_fetch",
    "description": (
        "取一个 B站视频的文字来源。默认下载音轨（供 video_transcribe 转写），"
        "这是主路线——不要因为看到「有字幕」就改道，B站字幕接口不稳定且可能残缺。"
        "也可传 source=\"subtitle\" 直接取字幕（零 ASR 误差，但不保证完整）。"
        "不下载视频本体。参数 url 可以是 BV号、完整链接或 b23.tv 短链。"
        "已有产物时默认复用；想重新抓就传 refresh=true。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "BV号（BV1xx411c7mD）、视频链接或 b23.tv 短链",
            },
            "source": {
                "type": "string",
                "enum": ["audio", "subtitle"],
                "description": "audio（默认，下音轨走 ASR）/ subtitle（只取字幕，可能残缺）",
            },
            "refresh": {
                "type": "boolean",
                "description": "已有产物也重新抓一遍（默认 false）",
            },
        },
        "required": ["url"],
    },
}


def _pick_subtitle(subs):
    """字幕轨可能有好几条，选一条。

    优先级：人工中文 > AI 中文 > 任意中文 > 第一条。
    人工字幕是校对过的，AI 字幕会有断句和错字——差一个档次。
    """
    def rank(s):
        lan = str(s.get("lan", ""))
        if lan in ("zh-CN", "zh-Hans", "zh"):
            return 0
        if lan.startswith("ai-zh"):
            return 1
        if "zh" in lan:
            return 2
        return 3

    return sorted(subs, key=rank)[0]


def _by_subtitle(vid, info, folder, transcript, meta, root):
    """可选路线：直接取字幕。会在结果里如实报告覆盖度。"""
    try:
        subs = bili.subtitles(vid, info["cid"])
    except Exception as e:
        return f"错误：查字幕失败：{e}"

    usable = [s for s in subs if (s.get("subtitle_url") or "").strip()]
    if not usable:
        return (
            "错误：没拿到可用的字幕地址。\n"
            "B站这个接口会给「有轨道但地址为空」的结果，重试几次可能就有，"
            "但更可靠的做法是走默认路线（下音轨 + ASR）：把 source 去掉重调一次。"
        )

    pick = _pick_subtitle(usable)
    try:
        raw = bili.subtitle_segments(pick["subtitle_url"])
    except Exception as e:
        return f"错误：下载字幕失败：{e}"

    if not raw:
        return "错误：字幕文件是空的"

    segs = [
        timeline.Segment(t0=r["t0"], t1=r["t1"], kind="caption", text=r["text"], source="caption")
        for r in raw
    ]
    paras = timeline.merge_into_paragraphs(segs)
    timeline.write_jsonl(folder / "segments.jsonl", segs)

    # ★ 覆盖度检查：字幕可能只覆盖前几分钟，这种残缺比没有更危险
    covered = max((s.t1 for s in segs), default=0)
    duration = info["duration"] or 1
    ratio = covered / duration
    note = ""
    if ratio < 0.85:
        note = (
            f"\n\n⚠️ 注意：字幕只覆盖到 {timeline.fmt_ts(covered)}，"
            f"占全长 {ratio:.0%}，后面是缺的。要完整的就改走 ASR（去掉 source 重调）。"
        )

    label = f"B站字幕 · {pick.get('lan_doc') or pick.get('lan')}（未跑 ASR）"
    transcript.write_text(timeline.render_transcript(paras, meta, label), encoding="utf-8")
    meta["text_source"] = "caption"
    meta["subtitle_lan"] = pick.get("lan", "")
    meta["subtitle_coverage"] = round(ratio, 3)
    store.save_meta(root, meta)

    return (
        f"「{info['title']}」\n"
        f"时长 {timeline.fmt_ts(duration)} ｜ UP主 {info['owner']} ｜ 字幕：{pick.get('lan_doc')}\n"
        f"已按时间轴合并成 {timeline.stats(paras)}\n"
        f"→ videos/{vid}/transcript.md{note}"
    )


def run(arguments):
    url = (arguments.get("url") or "").strip()
    if not url:
        return "错误：url 不能为空"

    refresh = bool(arguments.get("refresh"))
    source = (arguments.get("source") or "audio").lower()
    root = toolbox.current_workspace() or str(toolbox.PROJECT_ROOT)

    try:
        target = bili.resolve(url)
        info = bili.video_info(**target)
    except Exception as e:
        return f"错误：{e}"

    vid = info["bvid"]
    folder = store.video_dir(root, vid)
    transcript = folder / "transcript.md"

    if transcript.exists() and not refresh:
        return (
            f"「{vid}」之前已经取过了：videos/{vid}/transcript.md\n"
            "直接用 read_file 读它，或者传 refresh=true 重新抓。"
        )

    meta = {
        "bvid": vid,
        "aid": info["aid"],
        "cid": info["cid"],
        "title": info["title"],
        "desc": info["desc"],
        "duration": info["duration"],
        "owner": info["owner"],
        "mid": info["mid"],
        "pubdate": info["pubdate"],
        "url": f"https://www.bilibili.com/video/{vid}",
        "fetched_at": store.now(),
        "text_source": "pending",
    }
    store.save_meta(root, meta)
    store.mark_seen(root, vid, info["title"])

    if source == "subtitle":
        return _by_subtitle(vid, info, folder, transcript, meta, root)

    # ---------- 默认路线：下音轨，等转写 ----------
    max_minutes = int(getattr(config, "VIDEO_MAX_MINUTES", 120) or 120)
    if info["duration"] > max_minutes * 60:
        return (
            f"错误：这个视频 {timeline.fmt_ts(info['duration'])}，超过单条上限 "
            f"{max_minutes} 分钟。长视频全量转写要很久，"
            "先换个短一点的，或在 config.py 调大 VIDEO_MAX_MINUTES。"
            f"（确实只想看文字的话，可以试 source=\"subtitle\"）"
        )

    try:
        stream = bili.audio_url(vid, info["cid"])
    except Exception as e:
        return f"错误：拿不到音频流：{e}"

    audio = folder / "audio.m4a"
    if refresh or not audio.exists():
        try:
            bili.download(stream, audio)
        except Exception as e:
            return f"错误：音轨下载失败：{e}"

    meta["text_source"] = "audio"
    meta["audio_size"] = store.size_hint(audio)
    store.save_meta(root, meta)

    return (
        f"「{info['title']}」\n"
        f"时长 {timeline.fmt_ts(info['duration'])} ｜ UP主 {info['owner']}\n"
        f"已下载音轨：videos/{vid}/audio.m4a（{store.size_hint(audio)}）\n\n"
        f"→ 接下来调 video_transcribe，参数 video_id=\"{vid}\"。"
    )


if getattr(config, "ENABLE_VIDEO", True):
    register(DESCRIPTION, run)
