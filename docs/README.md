# 技术文档中心

这里是「简」（Jian）项目的技术文档入口。全套文档与代码**同仓库维护**——
改代码时，相关文档要同步更新（更新规则见文末「写作规范」）。

> 只想把项目跑起来用一用？那不用往下读，直接看根目录的 [README.md](../README.md) 就够了。
> 这里写给的是：想读懂内部原理、想改代码、想扩展功能的人。

---

## 一、阅读路径（按你的目的挑一条）

| 你的目的 | 从哪读起 |
|---|---|
| 第一次接触这个项目 | 先读 [架构总览](architecture.md)，再按兴趣进各模块文档 |
| 想改某个模块的代码 | 直接进 [modules/](modules/) 找对应目录 |
| 查 HTTP 接口的格式 | api/http.md（规划中） |
| 查 SSE 流式事件的格式 | api/sse-events.md（规划中） |
| 查数据 / 事件的格式 | data/event-log.md（规划中） |
| 想知道"为什么这么设计" | adr/（决策记录，一条一个文件，规划中） |
| 遇到问题要排查 | ops/runbook.md（规划中） |
| 看项目是怎么一步步做出来的 | 根目录 CHANGELOG.md（规划中） |

---

## 二、文档地图与推进计划

| 文档 | 内容 | 状态 |
|---|---|---|
| [architecture.md](architecture.md) | 架构总览：模块划分、核心机制、关键流程 | **Stable** |
| [modules/llm/](modules/llm/README.md) | 模块：模型接入（流式解析、工具调用协议） | **Stable** |
| [modules/engine/](modules/engine/README.md) | 模块：引擎（agent 循环、事件流） | **Stable** |
| [modules/storage/](modules/storage/README.md) | 模块：存储（事件日志、回放推导） | **Stable** |
| [modules/toolbox/](modules/toolbox/README.md) | 模块：工具箱（注册表、工作区、禁用名单、附件槽） | **Stable** |
| [modules/tools/](modules/tools/README.md) | 模块：工具系统（总览 + 每个工具一页） | **Stable** |
| [modules/plugins/](modules/plugins/README.md) | 模块：插件系统（空槽机制 + 每个插件一页） | **Stable** |
| [design/](design/frontend-audit-2026-10.md) | 设计文档：前端审计报告（2026-10，双评分 + 问题清单） | **Stable** |
| modules/server/ | 模块：HTTP 服务（路由、SSE、静态文件） | 规划中 |
| modules/frontend/ | 模块：前端（状态驱动渲染、流式显示） | 规划中 |
| api/http.md · api/sse-events.md | 接口契约（HTTP 接口逐条 / SSE 事件协议） | 规划中 |
| data/event-log.md | 数据契约（data.jsonl 事件字典、回放规则） | 规划中 |
| adr/ | 架构决策记录（从工作日志提炼，约 10 条） | 规划中 |
| ops/runbook.md | 运维手册：配置全表 + 故障排查 | 规划中 |

**推进计划**（分批交付，每批都是完整可用的状态）：

- [x] 第 1 批：docs 骨架 + 模板 + 架构总览 + llm 样张
- [x] 第 2 批：engine、storage
- [x] 第 3 批：toolbox、tools、plugins
- [ ] 第 4 批：server、frontend
- [ ] 收尾批：api/、data/、adr/、ops/runbook、CHANGELOG

**状态含义**：`Stable` = 已写完并与代码核对 ｜ `Draft` = 初稿待完善 ｜ `规划中` = 尚未开写。

---

## 三、术语表（Glossary）

项目里反复出现的关键词，先用大白话钉死含义——看别的文档时不用猜。

| 术语 | 含义 |
|---|---|
| **Agent 循环**（Agent Loop） | "调模型 → 模型要用工具就执行 → 结果塞回历史 → 再调模型"，直到模型不再要工具为止 |
| **事件日志**（Event Log） | `data.jsonl` 的存储方式：只追加、不重写；"当前状态"是回放推导出来的（学自 Pi） |
| **回放**（Replay） | 从头到尾把事件日志读一遍、推导出当前状态的过程 |
| **空槽**（Hook） | `hooks.py` 里的几个函数列表；插件往里 `append` 函数，引擎在固定时机挨个执行 |
| **工具**（Tool） | 给模型调用的能力（读文件、查目录……）；放在 `tools/`，文件即工具 |
| **插件**（Plugin） | 挂在空槽上的功能模块；放在 `plugins/`，文件即插件 |
| **SSE** | Server-Sent Events，流式响应的标准格式——打字机效果的基础 |
| **流式**（Streaming） | 模型边生成、前端边显示，而不是等整段话生成完再发 |
| **工作区**（Workspace） | 会话级的"干活范围"，agent 的所有文件操作被锁在其内（新对话自动分配 `sandbox/<时间戳>`） |
| **上下文压缩**（Compaction） | 上下文用量超阈值时，把早期对话交给模型总结成摘要，之后只带"摘要 + 最近消息" |
| **子代理**（Subagent） | 主 agent 派出去的"小弟"：独立上下文干活、干完只带回一份结论（多 agent 的第一块积木） |
| **禁用名单**（Disabled List） | 临时禁掉某些工具的线程级名单——子代理干活时用它禁掉 `spawn_agent` 自己（防套娃） |
| **附件**（Attachments） | 工具执行时"捎带"的结构化数据；比如子代理把完整过程捎给前端（`attach` / `take_attachments`） |
| **ADR** | Architecture Decision Record，架构决策记录——记录"为什么这么选" |
| **mermaid** | 用纯文本描述图表的语法；GitHub、VS Code（装插件）可直接渲染 |

---

## 四、写作规范（所有文档统一遵守）

1. **元信息头**：每份文档开头的统一小表格：

   | 状态 | 最后更新 | 关联代码 | 代码基线 |
   |---|---|---|---|
   | Stable | YYYY-MM-DD | `xxx.py` | `0000000` |

   「代码基线」写文档核对时对应提交的 git 短 hash——这样将来能一眼看出
   "文档和代码是不是对得上"。

2. **图统一用 mermaid**：纯文本、可 diff、可版本管理；不用截图（截图会过期）。

3. **单一信息源**：同一件事只在一个地方写全，其他地方只放链接，不复制内容。

4. **详尽标准**：讲清"是什么、怎么用、为什么这么做、要改时动哪里"；
   不逐行复述代码，不写容易过时的行号。

5. **更新约定**：改动代码时，同步更新对应模块文档的正文 + 元信息表
   （「最后更新」「代码基线」两格要刷新）。

6. **模板**：写新文档时从 [`_templates/`](_templates/) 里拷——
   [module.md](_templates/module.md)（模块文档）、[adr.md](_templates/adr.md)（决策记录）。

---

## 五、这套文档怎么来的

- **提炼，不是从零写**：素材来自现有 README、代码注释、`.workbuddy/memory/`
  的工作日志（每轮的决策与踩坑记录）、git 提交历史。
- **按模块分目录**：一个代码模块对应 `modules/` 下一个目录；
  改代码时文档就在"隔壁"，同步更新几乎零成本。
- **决策留痕**：`adr/` 只增不改——推翻旧决策时新写一条并注明"取代"，历史不改写。
