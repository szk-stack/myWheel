# 模块：tools（工具系统）

| 状态 | 最后更新 | 关联代码 | 代码基线 |
|---|---|---|---|
| Stable | 2026-10-16 | `tools/` 目录 | `515d604` |

## 1. 模块职责

`tools/` 目录存放**模型可以调用的所有能力**。"文件即工具"——
往目录里扔一个 `.py`，重启服务，模型就多了一项能力，
其余任何代码都不用动（自动加载机制在 [toolbox](../toolbox/README.md) §4.7）。

工具分两类：

| 类别 | 工具 | 说明 |
|---|---|---|
| **文件工具** | [list_dir](list_dir.md) / [read_file](read_file.md) / [write_file](write_file.md) / [edit_file](edit_file.md) | 都在**工作区**内操作（`safe_path` 锁定） |
| **系统工具** | [exec_command](exec_command.md) | 在工作区执行系统命令（跑脚本、git、查环境）——**不受路径锁保护**，见其安全说明 |
| **视觉工具** | [view_image](view_image.md) / [screenshot](screenshot.md) | 看图与网页截图——agent 自检闭环（生成 → 截图 → 看图 → 修） |
| **代理工具** | [spawn_agent](spawn_agent.md) | 派子代理独立干活（multi-agent 积木） |

## 2. 工具的"三段式"结构（每个工具文件都长这样）

```python
# ---- ① 工具说明书（会发给模型看）----
DESCRIPTION = {
    "name": "工具名",
    "description": "这个工具是干什么的、什么时候该用（写好它 = 让模型会用）",
    "parameters": {           # JSON Schema 格式
        "type": "object",
        "properties": { ... },
        "required": [...],
    },
}

# ---- ② 执行函数（模型点名时真正运行）----
def run(arguments):
    """接收参数字典，返回字符串。"""
    ...
    return "结果文字"

# ---- ③ 登记进注册表 ----
register(DESCRIPTION, run)
```

## 3. 工具 run() 的约定（写新工具必读）

1. **返回字符串**——成功是结果文字；失败是**以"错误"开头**的说明
   （引擎靠 `result.startswith("错误")` 判断调用是否成功，进而在事件里标 `ok`）；
2. **不要抛异常**——`toolbox.call` 会兜底把异常转成错误文本，但自己
   友好地返回错误更好（能带上"教模型怎么修正"的提示）；
3. **错误提示写给模型看**——它是模型自我修正的信息源。好例子：
   `edit_file` 的"没找到 old_text，请先用 read_file 看一遍真实内容"；
4. **参数自己校验**（缺 path、空内容等）——不依赖引擎做参数检查。

## 4. 工作区与 `safe_path`（文件工具的公共防线）

`tools/_common.py` 是所有文件工具共用的内部模块（`_` 开头，不会被加载成工具）：

- `safe_path(user_path)` 把模型给的相对路径转成真实路径，做**两层检查**：
  1. 当前工作区本身合规（黑名单兜底——就算设置环节被绕过，这里也拦）；
  2. 拼出来的真实路径必须仍在工作区内（`../` 越界一律拒）。
- 违规时抛 `ValueError`（消息直接反馈给模型）；
- 没设定工作区的会话 → 默认落在**项目目录**。

> 为什么是"每次操作都查"而不是"设置时查一次"？——设置环节和执行环节
> 是两道独立防线，任何一道单独失效都不至于出事。

## 5. 现有工具速览

| 工具 | 一句话 | 关键行为 |
|---|---|---|
| [list_dir](list_dir.md) | 列目录内容 | 跳过隐藏项与 `__pycache__`；目录带 `/` 后缀 |
| [read_file](read_file.md) | 读文件（带行号） | 支持 offset/limit 分段；8000 字符截断保护 |
| [write_file](write_file.md) | 创建/整体覆盖文件 | 自动建父目录；会**整份替换** |
| [edit_file](edit_file.md) | 精确查找替换 | `old_text` 必须**恰好出现一次**才动手 |
| [exec_command](exec_command.md) | 执行系统命令 | 工作区 cwd；危险命令黑名单；60s 超时；输出截断 |
| [screenshot](screenshot.md) | 网页截图 | Edge 无头模式；存工作区 `.screenshots/`；零依赖 |
| [view_image](view_image.md) | 看图 | 图片经"附件槽"以 user 消息递回模型（官方限制） |
| [spawn_agent](spawn_agent.md) | 派子代理干活 | 独立上下文；防套娃；完整过程可展开查看 |

## 6. 加一个新工具（完整示例）

比如做一个 `count_lines`（数文件行数）：

```python
# tools/count_lines.py —— （三步：说明书 / run / 登记）
from toolbox import register
from tools._common import safe_path

DESCRIPTION = {
    "name": "count_lines",
    "description": "数一数某个文件有多少行。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对项目根目录的文件路径"},
        },
        "required": ["path"],
    },
}

def run(arguments):
    target = safe_path(arguments.get("path", ""))
    if not target.exists():
        return f"错误：文件不存在：{arguments.get('path')}"
    lines = target.read_text(encoding="utf-8").splitlines()
    return f"{arguments['path']} 共 {len(lines)} 行"

register(DESCRIPTION, run)
```

放进 `tools/` → 重启服务 → 模型就学会了。说明书里 `description` 写得越
清楚"什么时候该用它"，模型用得越准。

## 7. 已知限制与坑

- **工具的运行权限 = 工作区**（`exec_command` 例外）：文件工具都被锁在工作区内；
  命令是任意程序，工作区只是它的默认执行目录，技术上挡不住越界——
  需要更严的管控时，挂 `before_tool` 插件做"命令确认/白名单"；
- **没有"只读工具集"之类的分级**（子代理场景用禁用名单做的是"减工具"，不是"降权限"）；
- **文件工具只认 UTF-8**：非 UTF-8 文本会返回解码错误（未做编码探测）；
- **工具是同步执行的**：一个工具没跑完，本轮不会开始下一个——
  没有并行工具调用；
- **写操作没有"预览/确认"步骤**：模型可以自主写入（工作区内）——
  要更强的管控可以在 `before_tool` 空槽加"审批插件"（预留的位置，
  详见 [toolbox](../toolbox/README.md) §4.3）。
