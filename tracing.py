"""
tracing.py —— 链路观测（"飞行记录仪"）

把"一次任务"的完整执行过程记下来：一次任务 = 用户发一条消息触发的一整轮
agent 执行（可能包含多轮"调模型 ⇄ 执行工具"）。每条链路记录：

  - 每轮**实际发给模型的上下文结构**（请求快照）
  - 模型的**思考过程**（reasoning）
  - 模型的文字输出与**工具调用**（参数 / 结果 / 耗时）
  - 最终回答、错误、总耗时

存进 traces.jsonl（一行一条，追加写）。

为什么单独一个文件、不用事件日志？
  事件日志（data.jsonl）记的是"对话数据"——它要参与回放、是状态来源；
  链路是"运行观测"——体积大、只管查询、丢了不心疼。分开存互不拖累。
  traces.jsonl 已在 .gitignore 里（可能含敏感内容）。
"""

import json
import time
from datetime import datetime
from pathlib import Path

# 链路日志文件（JSONL 格式：一行一条链路）
TRACE_FILE = Path(__file__).resolve().parent / "traces.jsonl"

# 上下文快照里单条消息的字符上限（"完整结构"也要有个防爆上限）
MAX_MESSAGE_CHARS = 4000


def new_trace(conversation_id, workspace, model, source="main"):
    """开始记录一条链路：返回一个待填充的 trace 字典。

    source: "main"（主对话）或 "subagent"（子代理内部链路）
    """
    return {
        "id": f"t{time.time_ns()}",
        "conversation_id": conversation_id,
        "workspace": workspace,
        "model": model,
        "source": source,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "duration_ms": 0,
        "status": "running",     # running / ok / error / interrupted
        "rounds": [],
        "final_text": "",
        "error": None,
    }


def slim_text(text, limit=MAX_MESSAGE_CHARS):
    """截断过长文本并注明原文长度（上下文快照防爆用）。"""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（截断，原文 {len(text)} 字符）"


def save(trace):
    """把一条链路追加进 traces.jsonl（一行一条）。"""
    trace["finished_at"] = datetime.now().isoformat(timespec="seconds")
    with open(TRACE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(trace, ensure_ascii=False) + "\n")


def save_safe(trace):
    """安全落盘：观测本身失败不能影响主流程（只打警告）。"""
    try:
        save(trace)
    except Exception as e:
        print(f"[警告] 链路日志写入失败：{e}")


def list_traces(limit=50):
    """列出最近的链路摘要（不带完整上下文快照，给列表页用）。"""
    items = _read_all()
    items.reverse()          # 最新的在前
    result = []
    for t in items[:limit]:
        result.append({
            "id": t.get("id"),
            "conversation_id": t.get("conversation_id"),
            "source": t.get("source", "main"),
            "started_at": t.get("started_at"),
            "duration_ms": t.get("duration_ms"),
            "status": t.get("status"),
            "rounds": len(t.get("rounds") or []),
            "user_preview": _first_user_text(t),
            "final_preview": (t.get("final_text") or "")[:80],
        })
    return result


def get_trace(trace_id):
    """按 id 取一条完整链路；找不到返回 None。"""
    for t in _read_all():
        if t.get("id") == trace_id:
            return t
    return None


# ---------- 内部小工具 ----------

def _read_all():
    """读出日志里的所有链路（坏行跳过——和事件日志一样的容错哲学）。"""
    if not TRACE_FILE.exists():
        return []
    items = []
    with open(TRACE_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def _first_user_text(trace):
    """取本次任务里的第一条用户消息（列表摘要用）。"""
    for r in trace.get("rounds") or []:
        for m in (r.get("request") or {}).get("messages") or []:
            if m.get("role") == "user":
                return (m.get("content") or "")[:80]
    return ""
