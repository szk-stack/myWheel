"""
videokit/v2t.py —— 调用本地 voice2text 服务（用户的现成项目）

★ 为什么走服务而不是自己装一套：
  那是个自包含的打包应用——自带 Python 3.10 运行时、faster-whisper、
  cuDNN 8、ffmpeg，还按 cuda + large-v3-turbo 配好了。
  在项目里另装一遍 ctranslate2 + CUDA 12 运行库，既多花 1GB，
  又和它抢显卡。直接调它的 HTTP 接口，等于白捡一个调好的 GPU 转写后端。

接口（从前端源码里读出来的，2026-10-30 验证）：
  POST /upload       multipart，字段名 audio → {code, data: "xxx.wav"}
  POST /process      wav_name + model + language → {code:0, msg:"ing"}
  POST /progressbar  同上参数 → {code:0, data: 0~1, result: "<SRT>"}

★ 两个坑：
  1. 本机调用必须**绕过系统代理**。这台机器的 http_proxy 指向 127.0.0.1:49856，
     requests 默认 trust_env=True，会把 127.0.0.1 的请求也发给代理，返回 502。
     所以这里建 Session 时显式 trust_env = False。
  2. /process 是异步的，必须轮询 /progressbar；进度到 1 才会带 result。
"""

import re
import subprocess
import time
from pathlib import Path

import requests

DEFAULT_BASE = "http://127.0.0.1:9977"
DEFAULT_MODEL = "large-v3-turbo"

_TIME_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
)


def base_url():
    try:
        import config

        return (getattr(config, "VOICE2TEXT_URL", "") or DEFAULT_BASE).rstrip("/")
    except Exception:
        return DEFAULT_BASE


def _session():
    s = requests.Session()
    s.trust_env = False          # ★ 不读 http_proxy：本机调用不该过代理
    return s


def available(timeout=2):
    """服务在不在。顺便返回一句人话，方便工具原样转给用户。"""
    try:
        r = _session().get(base_url() + "/", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _to_seconds(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_srt(text):
    """SRT → [{"t0","t1","text"}]。

    它的返回结果就是标准 SRT（带序号、时间轴、正文），
    正好和我们的 Segment 结构一一对应，不用猜格式。
    """
    out = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        idx = 0
        if "-->" not in lines[0]:
            idx = 1                      # 第一行是序号，跳过
        if idx >= len(lines):
            continue
        m = _TIME_RE.search(lines[idx])
        if not m:
            continue
        g = m.groups()
        body = " ".join(lines[idx + 1 :]).strip()
        if not body:
            continue
        out.append(
            {
                "t0": _to_seconds(*g[0:4]),
                "t1": _to_seconds(*g[4:8]),
                "text": body,
            }
        )
    return out


def upload(path, timeout=300):
    """上传音频/视频，返回服务端转换后的 wav 文件名。"""
    p = Path(path)
    with open(p, "rb") as f:
        r = _session().post(
            base_url() + "/upload",
            files={"audio": (p.name, f)},   # 字段名必须是 audio，前端也这么传
            timeout=timeout,
        )
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(d.get("msg") or "上传失败")
    return d["data"]


def run(wav_name, model=DEFAULT_MODEL, language="zh", max_wait=3600, on_progress=None):
    """提交识别并轮询，返回 SRT 文本。"""
    s = _session()
    form = {"wav_name": wav_name, "model": model, "language": language}

    r = s.post(base_url() + "/process", data=form, timeout=60)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(d.get("msg") or "提交识别失败")

    started = time.time()
    while time.time() - started < max_wait:
        time.sleep(2)
        p = s.post(base_url() + "/progressbar", data=form, timeout=30).json()
        if p.get("code") != 0:
            raise RuntimeError(p.get("msg") or "识别过程出错")
        data = p.get("data") or 0
        if on_progress:
            on_progress(data)
        if data >= 1:
            return p.get("result") or ""
    raise TimeoutError(f"等了 {max_wait} 秒还没转完")


def ensure_running(wait_seconds=120):
    """服务没起来就试着把它拉起来，返回 (是否可用, 说明)。

    ★ 为什么允许自动启动：这是个带界面的 exe，用户平时双击就用；
      但 agent 调的时候它可能没开着。自动拉起一次，比让对话卡在
      "请先手动打开某个 exe" 上体验好得多。不想这样就把
      config.py 的 VOICE2TEXT_AUTOSTART 设为 False。
    """
    if available():
        return True, "已在运行"

    try:
        import config

        autostart = getattr(config, "VOICE2TEXT_AUTOSTART", True)
        exe = getattr(config, "VOICE2TEXT_EXE", "") or ""
    except Exception:
        autostart, exe = True, ""

    if not autostart or not exe or not Path(exe).exists():
        return False, "服务没在运行（VOICE2TEXT_EXE 没配或路径不对）"

    try:
        subprocess.Popen(
            [exe],
            cwd=str(Path(exe).parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return False, f"启动失败：{e}"

    started = time.time()
    while time.time() - started < wait_seconds:
        if available(timeout=1):
            return True, f"已自动启动（{time.time() - started:.0f} 秒）"
        time.sleep(1)
    return False, f"启动了但 {wait_seconds} 秒内没就绪"


def transcribe(path, model=None, language="zh", on_progress=None):
    """一步到位：上传 → 识别 → 解析。返回 (句子列表, 信息字典)。

    返回结构和 videokit.asr.transcribe 完全一致，上层可以随便换后端。
    """
    model = model or DEFAULT_MODEL
    started = time.time()
    wav = upload(path)
    srt = run(wav, model=model, language=language)
    segs = parse_srt(srt)
    if not segs:
        raise RuntimeError("服务返回的结果解析不出时间轴（可能识别为空）")
    return segs, {
        "duration": segs[-1]["t1"] if segs else 0,
        "language": language,
        "elapsed": round(time.time() - started, 1),
        "model": model,
        "device": "voice2text 服务",
    }
