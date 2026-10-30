"""
工具：video_fetch —— 取一个 B站视频的文字来源

按设计文档的降级链，这里做第一步判断：
  有原生字幕 → 直接取字幕（零 ASR 误差、自带时间轴、不花算力）
  没有字幕   → 只下音轨（约 1–2MB / 分钟），等 video_transcribe 转

★ 不下视频本体：转文字用不上画面，一个 10 分钟的视频几百 MB，
  为了几百 KB 的字幕去拖几百 MB，是这类工具最常见的浪费。
  等以后做关键帧（阶段 3）再按需下载。

★ 实测结论（2026-10-30）：未登录状态下 B站的 AI 字幕接口返回空数组。
  这不是"视频没字幕"，是权限问题。在 config.py 填 VIDEO_BILI_COOKIE
  之后才能看到 AI 字幕轨。
"""

import config
import toolbox
import videokit.bili as bili
from toolbox import register
from videokit import store, timeline

DESCRIPTION = {
    "name": "video_fetch",
    "description": (
        "取一个 B站视频的文字来源：优先抓原生字幕（零误差、带时间轴），"
        "没有字幕就只下载音轨，等 video_transcribe 转写。不下载视频本体。"
        "参数 url 可以是 BV号、完整链接或 b23.tv 短链。"
        "已有产物时默认复用；想重新抓就传 refresh=true。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "BV号（BV1xx411c7mD）、视频链接或 b23.tv 短链",
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


def run(arguments):
    url = (arguments.get("url") or "").strip()
    if not url:
        return "错误：url 不能为空"

    refresh = bool(arguments.get("refresh"))
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

    # ---------- 路线 A：原生字幕 ----------
    try:
        subs = bili.subtitles(vid, info["cid"])
    except Exception:
        subs = []

    if subs:
        pick = _pick_subtitle(subs)
        try:
            raw = bili.subtitle_segments(pick.get("subtitle_url", ""))
        except Exception as e:
            raw = []
            sub_error = str(e)
        else:
            sub_error = ""

        if raw:
            segs = [
                timeline.Segment(
                    t0=r["t0"], t1=r["t1"], kind="caption", text=r["text"], source="caption"
                )
                for r in raw
            ]
            paras = timeline.merge_into_paragraphs(segs)
            # 落盘存原始句子，合并只用于渲染（理由见 video_transcribe.py 同处注释）
            timeline.write_jsonl(folder / "segments.jsonl", segs)
            label = f"B站字幕 · {pick.get('lan_doc') or pick.get('lan')}（未跑 ASR）"
            transcript.write_text(
                timeline.render_transcript(paras, meta, label), encoding="utf-8"
            )
            meta["text_source"] = "caption"
            meta["subtitle_lan"] = pick.get("lan", "")
            store.save_meta(root, meta)

            return (
                f"「{info['title']}」\n"
                f"时长 {timeline.fmt_ts(info['duration'])} ｜ UP主 {info['owner']} ｜ 字幕：{pick.get('lan_doc')}\n"
                f"已按时间轴合并成 {timeline.stats(paras)}\n"
                f"→ videos/{vid}/transcript.md\n\n"
                "这条走的是字幕，没有 ASR 误差。可以直接 read_file 读，需要结构化笔记再说。"
            )

    # ---------- 路线 B：下音轨，等转写 ----------
    try:
        import config

        max_minutes = int(getattr(config, "VIDEO_MAX_MINUTES", 120))
    except Exception:
        max_minutes = 120

    if info["duration"] > max_minutes * 60:
        return (
            f"错误：这个视频 {timeline.fmt_ts(info['duration'])}，超过上限 "
            f"{max_minutes} 分钟，且没有可用字幕。"
            "长视频全量转写要很久，先换个短一点的，或在 config.py 调大 VIDEO_MAX_MINUTES。"
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

    reason = "该视频没有开放字幕轨（未登录通常看不到 AI 字幕）"
    return (
        f"「{info['title']}」\n"
        f"时长 {timeline.fmt_ts(info['duration'])} ｜ UP主 {info['owner']}\n"
        f"没拿到字幕——{reason}。\n"
        f"已下载音轨：videos/{vid}/audio.m4a（{store.size_hint(audio)}）\n\n"
        f"→ 接下来调 video_transcribe，参数 video_id=\"{vid}\"。"
    )


if getattr(config, "ENABLE_VIDEO", True):
    register(DESCRIPTION, run)
