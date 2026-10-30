"""
videokit/timeline.py —— 时间轴数据模型

整条链路只有一种中间结构：Segment。

    {t0, t1, kind, text, source}

  kind   —— speech（语音）/ caption（字幕）/ frame（画面）/ chapter（章节）
  source —— 这段文字从哪来：caption / faster-whisper / manual ……

★ 为什么"时间轴"要当唯一主键：
  字幕、ASR、关键帧 OCR、VLM 描述——四路产物的格式完全不同，
  但它们都天然带一个"发生在第几秒"。用 t0/t1 对齐，四路就能合成一份文档，
  而且任何一句话都能反查回视频里对应的时刻。没有这个主键，
  多路信息只能各写各的，最后谁也拼不到一起。
"""

import json
from dataclasses import asdict, dataclass, field


@dataclass
class Segment:
    t0: float
    t1: float
    kind: str = "speech"
    text: str = ""
    source: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        d = asdict(self)
        if not d["extra"]:
            d.pop("extra")
        return d


def fmt_ts(seconds):
    """秒 → mm:ss（超过一小时给 h:mm:ss）。"""
    seconds = max(0, int(round(float(seconds or 0))))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


_PUNCT = "，。？！、；：,.?!;:）」』】"


def _join(left, right):
    """拼接两句，顺手补标点。

    字幕和 ASR 都常常不给句末标点，直接拼会变成一长串没有停顿的流水句
    （"…的五个环节分片、索引、召回、重排、生成"）。中文里补一个逗号
    比留空好读得多，也让后面按句切片时有个抓手。
    """
    if not left:
        return right
    if left[-1].isascii() and right[0].isascii():
        return " " + right          # 英文之间用空格
    if left[-1] in _PUNCT or right[0] in _PUNCT:
        return right                # 已经有标点就不重复加
    return "，" + right


def merge_into_paragraphs(segments, max_chars=180, max_gap=2.5):
    """把碎片合并成可读的段落。

    ★ 这一步不是美化，是"能不能读"的分水岭：
      字幕一行往往只有六七个字（"那么接下来" / "我们看这里"），
      原样输出会变成几百行噪声，人读不下去，模型切片也切得稀碎。
      合并规则：字符数到上限，或者和下一句之间停顿超过 max_gap 秒，就断开。
    """
    out = []
    buf, buf_start, buf_end, buf_source = None, 0.0, 0.0, ""

    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue

        if buf is None:
            buf, buf_start, buf_end, buf_source = text, seg.t0, seg.t1, seg.source
            continue

        gap = seg.t0 - buf_end
        too_long = len(buf) + len(text) > max_chars
        if gap > max_gap or too_long:
            out.append(Segment(buf_start, buf_end, "speech", buf, buf_source))
            buf, buf_start, buf_end, buf_source = text, seg.t0, seg.t1, seg.source
        else:
            buf = buf + _join(buf, text)
            buf_end = seg.t1

    if buf is not None:
        out.append(Segment(buf_start, buf_end, "speech", buf, buf_source))
    return out


def write_jsonl(path, segments):
    with open(path, "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(json.dumps(seg.to_dict(), ensure_ascii=False) + "\n")


def read_jsonl(path):
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            out.append(Segment(**d))
    return out


_ASCII_PUNCT = {",": "，", "?": "？", "!": "！", ":": "：", ";": "；"}


def normalize_punct(text):
    """把中文句子里夹的半角标点换成全角。

    Whisper 输出的标点是中英混的（"技术,RAG," 和 "知识助手,智能客服" 同时出现），
    读起来很毛刺。这里只在**中文占多数**的句子里替换，
    英文内容保持原样（英文句子用半角逗号才是对的），
    数字里的逗号（1,000）也不动。
    """
    if not text:
        return text
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk < len(text) * 0.3:
        return text

    out = []
    for i, ch in enumerate(text):
        repl = _ASCII_PUNCT.get(ch)
        if repl:
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            out.append(ch if (prev.isdigit() and nxt.isdigit()) else repl)
        else:
            out.append(ch)
    return "".join(out)


def apply_fixes(text):
    """按配置里的纠错表做替换（config.ASR_FIXES，格式 "错=对,错=对"）。

    ★ 为什么放在渲染层而不是转写层：
      有些错是**稳定复现**的——整段视频里 RAG 被听成 REG（实测 6 处）。
      换个更大的模型未必修得掉，但一张替换表就能修。
      而这属于"解释"，不是"数据"：改了规则应当立刻重新渲染出结果，
      而不是把两分钟的 ASR 再跑一遍。所以它在渲染时生效。
    ★ 代价是它不看上下文，只放你确认没歧义的替换。
    """
    if not text:
        return text
    try:
        import config

        fixes = getattr(config, "ASR_FIXES", "") or ""
    except Exception:
        return text

    for pair in fixes.split(","):
        if "=" in pair:
            wrong, right = pair.split("=", 1)
            if wrong.strip():
                text = text.replace(wrong.strip(), right.strip())
    return text


def render_transcript(segments, meta, source_label):
    """渲染成 transcript.md。

    格式刻意保持极简：每段一行 `[mm:ss] 正文`。
    理由：这种形状既能被人读，也能被 grep，还能在切片时用正则抠出时间戳
    写进 chunk 元数据——三种用途一份格式搞定。
    """
    lines = [
        f"# {meta.get('title', '未命名视频')}",
        "",
        f"> 平台：B站 ｜ UP主：{meta.get('owner', '')} ｜ 时长：{fmt_ts(meta.get('duration', 0))}",
        f"> 链接：https://www.bilibili.com/video/{meta.get('bvid', '')}",
        f"> 文字来源：{source_label}",
        f"> 抓取时间：{meta.get('fetched_at', '')}",
        "",
    ]
    desc = (meta.get("desc") or "").strip()
    if desc:
        lines += ["> **简介**：" + desc.replace("\n", " ")[:300], ""]

    lines += ["## 正文", ""]
    for seg in segments:
        lines.append(f"[{fmt_ts(seg.t0)}] {apply_fixes(normalize_punct(seg.text))}")
    lines.append("")
    return "\n".join(lines)


def stats(segments):
    """给工具返回的一行统计：多少段、覆盖多长时间。"""
    if not segments:
        return "0 段"
    covered = sum(max(0.0, s.t1 - s.t0) for s in segments)
    chars = sum(len(s.text) for s in segments)
    return f"{len(segments)} 段 / {chars} 字 / 覆盖 {fmt_ts(covered)}"
