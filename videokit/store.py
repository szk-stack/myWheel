"""
videokit/store.py —— 产物落盘

目录约定（都落在当前工作区下，天然受 safe_path 的边界保护）：

    <workspace>/videos/<bvid>/
        meta.json        元信息 + 每一步用了什么参数
        subtitle.json    原始字幕（有的话）
        audio.m4a        音轨（有字幕时不下）
        segments.jsonl   中间层（时间轴）
        transcript.md    成品：全文保真

★ 为什么要留 meta.json：过两周回来看一份 transcript，第一个问题一定是
  "这到底是字幕还是 ASR 出来的"。答案必须落在文件里，不能只存在于当时的对话。
"""

import json
import re
import time
from pathlib import Path

# 视频 ID 只允许 B站那种形状。
# ★ 这不是洁癖：video_id 是模型给的参数，直接拼进路径就等于把
#   "../../config.py" 这种输入放进来了。和 safe_path 是同一道防线的两端。
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def safe_id(video_id):
    vid = (video_id or "").strip()
    if not _ID_RE.match(vid):
        raise ValueError(f"视频 ID 不合法：{video_id!r}（应形如 BV1xx411c7mD）")
    return vid


def videos_root(root):
    return Path(root) / "videos"


def video_dir(root, video_id):
    p = videos_root(root) / video_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_meta(root, meta):
    p = video_dir(root, meta["bvid"]) / "meta.json"
    p.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return p


def load_meta(root, video_id):
    p = videos_root(root) / video_id / "meta.json"
    if not p.exists():
        raise FileNotFoundError(
            f"本地没有 {video_id} 的产物，请先用 video_fetch 取一次"
        )
    return json.loads(p.read_text(encoding="utf-8"))


def touch(meta, key, value):
    """记一笔"这一步用了什么"（追加在 meta["steps"] 里，不覆盖历史）。"""
    meta.setdefault("steps", {})[key] = value
    return meta


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def seen_path(root):
    return videos_root(root) / "seen.json"


def load_seen(root):
    p = seen_path(root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def mark_seen(root, video_id, title=""):
    data = load_seen(root)
    data[video_id] = {"title": title, "at": now()}
    seen_path(root).parent.mkdir(parents=True, exist_ok=True)
    seen_path(root).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def size_hint(path):
    """给工具返回用的体积描述。"""
    try:
        n = Path(path).stat().st_size
    except OSError:
        return ""
    return f"{n / 1024 / 1024:.1f} MB" if n > 1024 * 1024 else f"{n // 1024} KB"
