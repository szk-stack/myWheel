# 模块：plugins（插件系统）

| 状态 | 最后更新 | 关联代码 | 代码基线 |
|---|---|---|---|
| Stable | 2026-10-16 | `plugins/` 目录 + `hooks.py` | `515d604` |

## 1. 模块职责

插件系统 = **空槽机制**（`hooks.py`）+ **插件目录**（`plugins/`）。
它是整个项目"加功能不动主流程"的实现方式：插件把函数注册到"空槽"上，
引擎在固定时机挨个执行。

插件与工具的区别（两个容易混的概念）：

| | 工具（tools/） | 插件（plugins/） |
|---|---|---|
| 谁在用 | **模型**决定什么时候调 | **引擎**在固定时机自动执行 |
| 交互 | 模型"点单"，参数是模型给的 | 直接改流程数据（`ctx`），无参数无返回 |
| 例子 | read_file（模型想读文件时） | compact（每次调模型前自动检查压缩） |

## 2. 空槽机制（`hooks.py` 全文就三个列表 + 一个调度函数）

| 空槽 | 执行时机 | 典型用途 |
|---|---|---|
| `before_llm` | 每次调模型**之前** | 插人设、压缩上下文、将来 RAG 注入 |
| `after_llm` | 模型回答**之后** | 日志、统计、过滤（改不了已流出的内容） |
| `before_tool` | 每次**工具执行之前** | 拦截工具（把 `ctx["blocked"]` 设成字符串即可）、审计 |

**插件函数的约定**：

1. 接收 `ctx`（共享数据袋，结构见 [engine](../engine/README.md) §4.1）；
2. **原地修改**它（如 `ctx["messages"] = ...`）——没有返回值约定；
3. `hooks.run(slot, ctx)` 按 `append` 顺序依次执行。

**在引擎里的执行位置**（对照 [engine](../engine/README.md) §4.2 流程图）：
`before_llm` 在"应用旧压缩 → 准备工具清单"之前；`after_llm` 在循环收尾处。

## 3. 插件清单

| 插件 | 挂的槽 | 一句话 | 文档 |
|---|---|---|---|
| `system_prompt` | before_llm | 把项目人设插到消息最前（可被 `skip_persona` 标记跳过） | [system_prompt.md](system_prompt.md) |
| `compact` | before_llm | 上下文压缩：超阈值时生成"覆盖式摘要" | [compact.md](compact.md) |
| `logger` | after_llm | 在终端打印一行对话日志（教学示例） | [logger.md](logger.md) |

## 4. 加载机制（"文件即插件"）

`engine.load_plugins()`（启动时调用一次）：`pkgutil` 遍历 `plugins/`，
跳过 `_` 开头文件，逐个 `import`——文件被"执行"时，末尾的
`before_llm.append(...)` 之类的代码就把函数挂上了槽。

```python
# 加一个插件 = 往 plugins/ 扔一个文件：
from hooks import after_llm

def my_plugin(ctx):
    ...

after_llm.append(my_plugin)   # ← 这一行让它生效
```

## 5. 加新插件示例（"敏感词打码"）

```python
# plugins/bad_words.py
from hooks import after_llm

BAD_WORDS = ["某词1", "某词2"]

def filter_words(ctx):
    for w in BAD_WORDS:
        ctx["reply"] = ctx["reply"].replace(w, "***")

after_llm.append(filter_words)
```

## 6. 设计决策

- **空槽列表 + 引擎调度**（ADR-003）：扩展点是一等公民——加功能不需要
  改 engine；槽的"时机"是稳定的，槽里"装什么"完全自由；
- **`ctx` 共享数据袋**：插件之间通过对 `meta` 的约定键协作
  （如 compact 写 `new_compaction`、engine 读）——轻量、显式；
- **`before_tool` 的拦截约定**（`blocked` 字符串）：把"是否阻止执行"的
  决定权留给插件，`toolbox.call` 只负责检查——将来做"危险操作确认"
  不用改工具本身。

## 7. 扩展指南

- **换新时机**：`hooks.py` 里加一个新列表（注释里有示例位置），
  在 engine 流程里 `hooks.run(新槽, ctx)` 一次即可；
- **多个插件同槽**：按 `append` 顺序执行——**顺序有意义**
  （比如 compact 要在 system_prompt 之前/之后跑要想清楚）；
- **插件读别人的产物**：通过 `ctx["meta"]` 的约定键，不要猜。

## 8. 已知限制与坑

- **`before_llm` 里的异常没有兜底**：插件抛异常会一路向上（server 捕获后
  转成错误消息给用户）——写插件要自己 `try/except` 或者保证不抛
  （对比：`after_llm` 在 engine 里有容错，只打警告）；
- **`after_llm` 改不了已流出的内容**：流式的本质取舍——它只服务
  记录类用途；
- **插件加载发生在启动时**：改插件要**重启服务**；
- **槽没有"优先级"机制**：顺序 = `append` 顺序（目前靠人工约定，
  插件少，够用）。
