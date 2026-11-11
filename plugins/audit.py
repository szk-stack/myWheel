"""
插件：audit —— 完整权限审计

「完整权限」一旦打开，agent 就能读写工作区以外的文件。这是用户自己的
权力，但对外暴露的入口必须留下证据：谁、在哪个工作区、调了什么工具时
用的是完整权限；以及谁"要过"完整权限但被服务端策略降级了。

挂在 before_tool 空槽上——每次工具真正执行前记一行到 audit.jsonl。
审计失败绝不影响工具执行（这是观测，不是闸门）。
"""

import json
from datetime import datetime

import envconfig
import hooks
import toolbox

AUDIT_FILE = envconfig.data_dir() / "audit.jsonl"


def _write(record):
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            record.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print(f"[警告] 审计日志写入失败：{e}")


def audit_tool(ctx):
    """工具执行前：只有涉及「完整权限」的调用才落审计（其余不记，免得吵）。"""
    forced_off = toolbox.full_access_forced_off()
    if not toolbox.full_access() and not forced_off:
        return

    _write({
        "event": "full_access_denied" if forced_off else "tool_with_full_access",
        "tool": ctx.get("name"),
        "arguments": ctx.get("arguments"),
        "requested": toolbox.full_access_requested(),
        "granted": toolbox.full_access(),
        "workspace": toolbox.current_workspace(),
    })


hooks.before_tool.append(audit_tool)
