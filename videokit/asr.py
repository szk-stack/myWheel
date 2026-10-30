"""
videokit/asr.py —— 语音转写（本地 faster-whisper）

为什么默认用 faster-whisper 而不是 FunASR：
  FunASR 中文确实更准（Paraformer + 标点 + 说话人分离），
  但它依赖 torch，装下来 2.5GB。这个功能现在的瓶颈是"链路通不通"，
  不是"专名准不准"。faster-whisper 只要 200MB，Python 3.13 也有轮子，
  先用它跑通；等确实要处理大批量、且对专名敏感时，在同一个 transcribe()
  接口后面再挂一个 FunASR 后端即可。

★ 国内装模型的坑（都踩过了，写在这里）：
  1. huggingface.co 基本连不上 → 用 hf-mirror.com 镜像（HF_ENDPOINT）。
  2. 新版 huggingface_hub 默认走 Xet 存储，镜像不支持，会报 401
     → 必须关掉（HF_HUB_DISABLE_XET=1）。
  3. **av 必须 < 16**。PyAV 19 把 av.open() 的 metadata_errors 参数删了，
     而 faster-whisper 1.2.1（目前最新）还在传它，于是报
     "open() got an unexpected keyword argument 'metadata_errors'"。
     装法：pip install faster-whisper "av==15.1.0"
  三个都设好，才跑得起来。
"""

import os
import time
from pathlib import Path

_models = {}
_device_of = {}
_dll_handles = []


def _prepare_env():
    """在 import faster_whisper 之前把镜像和 Xet 开关摆好。"""
    try:
        import config

        endpoint = getattr(config, "HF_ENDPOINT", "https://hf-mirror.com")
    except Exception:
        endpoint = "https://hf-mirror.com"

    if endpoint:
        os.environ.setdefault("HF_ENDPOINT", endpoint)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def available():
    """依赖装没装——工具用它决定要不要提示"先 pip install"。"""
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


def _local_model_path(size):
    """在 HF 缓存里直接找模型目录，找不到再返回 None。

    ★ 为什么不用 snapshot_download(local_files_only=True)：
      huggingface_hub 2.x 里它**仍然会请求一次 API** 去解析仓库信息，
      断网或代理挂了就抛 502——这个参数并没有真正挡住网络。
      直接扫缓存目录（models--<owner>--<repo>/snapshots/<hash>/model.bin）
      才是真的离线可用，也不受 mirror 开关影响。
    """
    base = os.environ.get("HF_HUB_CACHE")
    if not base:
        hf_home = os.environ.get("HF_HOME")
        if hf_home:
            base = str(Path(hf_home) / "hub")
        else:
            base = str(Path.home() / ".cache" / "huggingface" / "hub")

    repo_dir = Path(base) / f"models--Systran--faster-whisper-{size}"
    snaps = repo_dir / "snapshots"
    if not snaps.is_dir():
        return None
    for snap in snaps.iterdir():
        if (snap / "model.bin").exists():
            return str(snap)
    return None


def _cfg(key, default):
    try:
        import config

        return getattr(config, key, default)
    except Exception:
        return default


def resolve_target(size):
    """决定用哪个模型：配置里的本地目录优先，其次本地缓存，最后才联网按名字下。"""
    custom = _cfg("ASR_MODEL_DIR", "")
    if custom and Path(custom).exists():
        return custom
    return _local_model_path(size) or size


def pick_device():
    """选设备。auto = 有能用的 CUDA 就用，否则 CPU。

    ★ 有显卡不等于能用：ctranslate2 要求 CUDA 12 + cuDNN 9，
      系统只装了 CUDA 11、或没装 cuDNN，都会在加载时报错。
      所以这里只做"探测"，真正的失败回退放在 load_model 里。
    """
    want = (_cfg("ASR_DEVICE", "auto") or "auto").lower()
    if want == "cpu":
        return "cpu", "int8"
    if want == "cuda":
        return "cuda", _cfg("ASR_COMPUTE_TYPE", "float16")

    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def _add_dll_dirs():
    """把 CUDA 运行库所在目录加入 DLL 搜索路径。

    Windows 上 ctranslate2 找 cublas64_12.dll / cudnn64_9.dll 靠的是 DLL 搜索路径。
    两个来源都自动处理：
      ① pip 装的 nvidia-*-cu12（在 site-packages/nvidia/*/bin）
      ② 配置里手写的 CUDA_DLL_DIRS（比如借用某个打包应用自带的库）

    ★ os.add_dll_directory() 返回的句柄必须留着：一旦被垃圾回收，
      那个目录就会从搜索路径里撤掉，之后又变成"找不到 dll"。
      所以句柄存在模块级列表里。
    """
    dirs = []
    try:
        import nvidia

        for sub in Path(nvidia.__file__).parent.iterdir():
            bin_dir = sub / "bin"
            if bin_dir.is_dir():
                dirs.append(str(bin_dir))
    except Exception:
        pass

    dirs += [d for d in (_cfg("CUDA_DLL_DIRS", []) or []) if d]

    for d in dirs:
        try:
            if Path(d).is_dir():
                _dll_handles.append(os.add_dll_directory(d))
        except Exception:
            pass


def load_model(size="small", device=None, compute_type=None):
    """加载并缓存模型。同一进程里第二次调用直接命中缓存。

    device/compute_type 不传就走配置 + 自动探测；
    探测到 CUDA 但实际加载失败（缺 cuDNN 之类），自动退回 CPU 重试一次——
    用户不该因为"没装对运行库"就跑不了转写。
    """
    device = device or "auto"
    if device == "auto" or device is None:
        device, compute_type = pick_device()
    compute_type = compute_type or "int8"

    key = (size, device, compute_type)
    if key in _models:
        return _models[key]

    _prepare_env()
    _add_dll_dirs()
    from faster_whisper import WhisperModel

    target = resolve_target(size)
    try:
        model = WhisperModel(target, device=device, compute_type=compute_type)
    except Exception:
        if device == "cpu":
            raise
        # CUDA 用不了就退回 CPU，不把错误抛给用户
        device, compute_type = "cpu", "int8"
        key = (size, device, compute_type)
        model = WhisperModel(target, device=device, compute_type=compute_type)

    _models[key] = model
    _device_of[id(model)] = device
    return model


def normalize_text(text):
    """把 ASR 原文收拾成统一形状。

    ★ 关键的一步是繁→简转换，而且必须做后处理、不能靠提示词：
      Whisper 的中文输出在简体/繁体之间是**随机漂移**的——同一段音频，
      这次跑出简体、下次可能跑出一整段繁体（实测同一视频前后两半
      字体都不一致）。往 initial_prompt 里加"请用简体中文"有两个坏处：
      一是它会把指令本身当成前文复读出来，二是并不保证遵守。
      用 zhconv 做确定性转换，零风险、零成本。

    ★ 再做一次"确定性纠错"，但那一步放在渲染层（timeline.apply_fixes）：
      纠错表属于"解释"，改规则不该重跑两分钟的 ASR。
      这里只负责**字体归一**——那才是数据本身该有的形状。
    """
    text = (text or "").strip()
    if not text:
        return ""
    try:
        import zhconv

        text = zhconv.convert(text, "zh-cn")
    except Exception:
        pass  # 没装 zhconv 就跳过，不影响主流程

    return text.replace("  ", " ").strip()


def _run(model, audio_path, language, hotwords, on_progress, size="", device=""):
    """真正干活的部分：喂音轨、收句子。"""
    started = time.time()

    # 既接受文件路径，也接受 numpy 数组（切片试听、单元测试都用得上）
    source = str(audio_path) if isinstance(audio_path, (str, os.PathLike)) else audio_path

    segments, info = model.transcribe(
        source,
        language=language or None,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=5,
        hotwords=(hotwords or None),
        condition_on_previous_text=False,
    )

    out = []
    for seg in segments:
        text = normalize_text(seg.text)
        if not text:
            continue
        out.append({"t0": float(seg.start), "t1": float(seg.end), "text": text})
        if on_progress and len(out) % 20 == 0:
            on_progress(len(out), getattr(info, "duration", 0))

    return out, {
        "duration": float(getattr(info, "duration", 0) or 0),
        "language": getattr(info, "language", ""),
        "language_probability": float(getattr(info, "language_probability", 0) or 0),
        "elapsed": round(time.time() - started, 1),
        "model": size,
        "device": device,
    }


def _is_cuda_lib_error(exc):
    msg = str(exc).lower()
    return any(k in msg for k in ("cublas", "cudnn", "cuda", ".dll"))


def transcribe(audio_path, model_size="small", language="zh", hotwords=None, on_progress=None):
    """把音轨转成带时间戳的句子。

    返回 (句子列表, 信息字典)。句子是 {"t0", "t1", "text"}。

    ★ condition_on_previous_text=False 是刻意关掉的：
      开着它时，模型会用上一句去"猜"下一句，长音频里容易陷入复读
      （同一句话重复几十遍）。关掉后连贯性略降，但不会崩。

    ★ 用 hotwords 参数而不是 initial_prompt 塞术语：
      initial_prompt 的内容会被模型当成"上一段话"接着写，
      实测会在开头把术语表复读出来。hotwords 是独立参数，只做偏置。

    ★ CUDA 的回退放在这里而不是只在加载时：
      ctranslate2 缺 cublas64_12.dll 时**构造模型不报错**，
      要等第一次 encode 才抛 RuntimeError。所以必须兜住整段推理。
    """
    model = load_model(model_size)

    try:
        return _run(
            model, audio_path, language, hotwords, on_progress,
            model_size, _device_of.get(id(model), ""),
        )
    except RuntimeError as e:
        if _device_of.get(id(model)) != "cuda" or not _is_cuda_lib_error(e):
            raise
        # 显卡在，但运行库不全——退回 CPU 重来一次，别让用户卡在这
        fallback = load_model(model_size, device="cpu", compute_type="int8")
        return _run(
            fallback, audio_path, language, hotwords, on_progress, model_size, "cpu"
        )
