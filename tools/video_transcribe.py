"""
工具：video_transcribe —— 把音轨转成带时间轴的文字（本地 faster-whisper）

为什么单独一个工具、不并进 video_fetch：
  下载和转写的耗时差一个数量级（秒级 vs 分钟级），失败原因也完全不同
  （网络 vs 模型）。拆成两步，模型才能在"下完了但转写崩了"时只重试后半段，
  而不是把几百 MB 的音频再下一遍。这也是 README 里"一次做一件能验证的事"。
"""

import config
import toolbox
from toolbox import register
from videokit import asr, store, timeline, v2t

DESCRIPTION = {
    "name": "video_transcribe",
    "description": (
        "把 video_fetch 下回来的音轨转成带时间戳的文字，产出 videos/<BV号>/transcript.md"
        "（全文保真）和 segments.jsonl（时间轴）。"
        "默认走本机 voice2text 服务（自带 large-v3-turbo + GPU，又快又准，没启动会自动拉起）；"
        "backend=local 则用本地 faster-whisper（不依赖外部服务，但更慢）。"
        "可选参数：backend（voice2text/local）；model 模型名；"
        "hotwords 专有名词表（仅本地后端用，逗号分隔）；force 已有结果也重转。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "video_id": {"type": "string", "description": "BV号，如 BV1C69iBgEMk"},
            "backend": {
                "type": "string",
                "enum": ["voice2text", "local"],
                "description": "转写后端，默认取配置（voice2text）",
            },
            "model": {
                "type": "string",
                "description": "模型名：voice2text 后端用 large-v3-turbo 等；本地后端用 small/medium 等",
            },
            "hotwords": {
                "type": "string",
                "description": "专有名词，逗号分隔（仅本地后端生效）",
            },
            "force": {"type": "boolean", "description": "已有 transcript 也重转（默认 false）"},
        },
        "required": ["video_id"],
    },
}


def coverage_warning(covered, expect, vid):
    """转写结果的长度和视频时长对不上时，给一句明确的告警。

    ★ 单独抽出来，是为了能被测试直接盯住：
      这个判断救过一次——10 分钟的视频转出来是 1 分钟的内容，
      而段数、耗时、RTF 全都"正常"，只有内容是错的。
    """
    if not expect or covered >= expect * 0.9:
        return ""
    return (
        f"\n\n⚠️ 只覆盖到 {timeline.fmt_ts(covered)}，视频总长 "
        f"{timeline.fmt_ts(expect)}（{covered / expect:.0%}）——内容可能不对。\n"
        f"先确认视频有没有声音；若音轨正常，删掉 videos/{vid}/audio.m4a 后重抓重转。"
    )


def run(arguments):
    raw_id = (arguments.get("video_id") or "").strip()
    if not raw_id:
        return "错误：video_id 不能为空"

    try:
        vid = store.safe_id(raw_id)
    except ValueError as e:
        return f"错误：{e}"

    root = toolbox.current_workspace() or str(toolbox.PROJECT_ROOT)
    folder = store.videos_root(root) / vid
    transcript = folder / "transcript.md"

    if transcript.exists() and not bool(arguments.get("force")):
        return (
            f"「{vid}」已经转好了：videos/{vid}/transcript.md\n"
            "直接 read_file 读它；要重转就传 force=true。"
        )

    try:
        meta = store.load_meta(root, vid)
    except FileNotFoundError as e:
        return f"错误：{e}"

    audio = folder / "audio.m4a"
    if not audio.exists():
        return (
            f"错误：videos/{vid}/ 下没有 audio.m4a。"
            "这个视频可能走的是字幕路线（那就不需要转写），或者还没 fetch。"
        )

    backend = (
        (arguments.get("backend") or getattr(config, "ASR_BACKEND", "voice2text"))
        or "voice2text"
    ).lower()

    hotwords = (arguments.get("hotwords") or "").strip()
    if not hotwords:
        hotwords = getattr(config, "ASR_HOTWORDS", "") or ""

    if backend == "voice2text":
        # 走用户本机那套打包好的服务：自带 GPU + large-v3-turbo，不用另装东西
        ok, why = v2t.ensure_running()
        if not ok:
            return (
                f"错误：voice2text 服务不可用（{why}）。\n"
                "启动它，或在 config.py 里把 ASR_BACKEND 改成 local 用本地 faster-whisper。"
            )
        model = arguments.get("model") or getattr(
            config, "VOICE2TEXT_MODEL", v2t.DEFAULT_MODEL
        )
        try:
            raw, info = v2t.transcribe(audio, model=model, language="zh")
        except Exception as e:
            return f"错误：voice2text 转写失败：{e}"
        raw = [
            {"t0": r["t0"], "t1": r["t1"], "text": asr.normalize_text(r["text"])}
            for r in raw
        ]
        label = f"voice2text 服务 · {model}"
        source = "voice2text"
    else:
        if not asr.available():
            return (
                "错误：没装 faster-whisper——local 后端才需要它。\n"
                "默认走 voice2text 服务，一个字都不用装；确实要用本地兜底再执行：\n"
                '  python -m pip install faster-whisper "av==15.1.0"'
            )
        model = arguments.get("model") or getattr(config, "ASR_MODEL_SIZE", "small")
        try:
            raw, info = asr.transcribe(
                audio, model_size=model, language="zh", hotwords=hotwords or None
            )
        except Exception as e:
            return f"错误：转写失败：{e}"
        label = f"本地 ASR · faster-whisper {model}"
        source = "asr"

    if not raw:
        return "错误：转写结果为空——音轨可能没有声音，或者全是静音"

    segs = [
        timeline.Segment(
            t0=r["t0"],
            t1=r["t1"],
            kind="speech",
            text=r["text"],
            source=f"{source}:{model}",
        )
        for r in raw
    ]

    # ★ 落盘存原始句子，合并只发生在渲染时。
    #   理由：合并是"给人读"的排版，不是数据本身。原始粒度更细，
    #   以后要做按句检索、按时间跳转、改合并参数，都不用重跑一遍转写。
    timeline.write_jsonl(folder / "segments.jsonl", segs)
    paras = timeline.merge_into_paragraphs(segs)

    transcript.write_text(
        timeline.render_transcript(paras, meta, f"{label}（非字幕）"),
        encoding="utf-8",
    )

    meta["text_source"] = "asr"
    meta["asr_backend"] = source
    meta["asr_model"] = model
    meta["asr_elapsed"] = info["elapsed"]
    store.save_meta(root, meta)
    store.mark_seen(root, vid, meta.get("title", ""))

    duration = info.get("duration") or meta.get("duration") or 0
    rtf = (info["elapsed"] / duration) if duration else 0

    # ★ 覆盖度自检：转写结果的长度必须和视频时长对得上。
    #   这一条能抓住"转错文件"这种最阴的 bug——段数、耗时、RTF 全都正常，
    #   只有内容不对。实测踩过一次：10 分钟的视频转出来是上个 1 分钟视频的稿子，
    #   原因是上传文件名相同、服务端命中了旧缓存。
    warn = coverage_warning(covered=segs[-1].t1 if segs else 0,
                            expect=meta.get("duration") or 0, vid=vid)

    return (
        f"「{meta.get('title', vid)}」转写完成\n"
        f"音频 {timeline.fmt_ts(duration)} ｜ 用时 {info['elapsed']} 秒 ｜ "
        f"RTF {rtf:.2f} ｜ 后端 {label}\n"
        f"合并成 {timeline.stats(paras)}\n"
        f"→ videos/{vid}/transcript.md\n\n"
        "注意：这是语音识别结果，专名仍可能有错字；引用原话前先扫一眼。"
        f"{warn}"
    )


if getattr(config, "ENABLE_VIDEO", True):
    register(DESCRIPTION, run)
