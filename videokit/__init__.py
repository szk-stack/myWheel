"""
videokit —— 视频分析的核心逻辑（工具层只是薄壳）

为什么单独成包，而不是全塞进 tools/：
  工具文件的价值是"给模型看的说明书"，几百行混在一起既难测也难读。
  和 rag.py / kb_vector.py 的分工一样：逻辑在包里可单测，工具只做参数解析。

当前只实现 B站（阶段 0/1），模块划分留给以后加小红书：
  bili.py       B站接口：WBI 签名、搜索、UP主列表、字幕、音频流
  asr.py        语音转写：faster-whisper（本地）
  timeline.py   Segment 数据模型、合并、渲染 markdown
  store.py      产物落盘（videos/<bvid>/）
"""

from . import asr, bili, store, timeline, v2t

__all__ = ["bili", "asr", "v2t", "timeline", "store"]
