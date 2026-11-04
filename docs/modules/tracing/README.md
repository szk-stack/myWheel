# 模块：tracing（链路观测）

| 状态 | 最后更新 | 关联代码 | 代码基线 |
|---|---|---|---|
| Stable | 2026-10-24 | `tracing.py` + `engine.py` 埋点 | `7d59fbe` |

## 1. 模块职责

"飞行记录仪"：把**一次任务**（用户发一条消息触发的完整 agent 执行，
可能包含多轮"调模型 ⇄ 执行工具"）的每一个关键环节记下来，供事后排查。

一次链路记录包含：

| 内容 | 说明 |
|---|---|
| 每轮上下文快照 | 该轮**实际发给模型**的 messages（压缩、协议转换之后）+ 工具名清单 |
| 模型思考过程 | 服务端通过 `reasoning_content` 下发的思考内容（时有时无，见 §5） |
| 工具调用 | 名称 / 参数 / 结果 / 耗时 / 成功与否 |
| token 用量 | 每轮与整条链路的输入 / 命中 / 未命中 / 输出 token（来自响应的 `usage`） |
| 输出与结果 | 每轮文字输出、最终回答、总耗时、状态（ok / error / interrupted） |

**为什么单独一个文件、不用事件日志**：`data.jsonl` 是"对话数据"（要回放、
是状态来源）；链路是"运行观测"（体积大、只管查询）。分开存互不拖累。
`traces.jsonl` 已在 `.gitignore` 里。

## 2. 存储：`traces.jsonl`（一行一条链路）

```json
{
  "id": "t1792...", "conversation_id": "c179...", "source": "main",
  "workspace": "…\\sandbox\\2026-10-24-22-49", "model": "deepseek-flash",
  "started_at": "2026-10-24T22:49:10", "finished_at": "…",
  "duration_ms": 8321, "status": "ok",
  "rounds": [
    {
      "index": 0, "duration_ms": 3120,
      "request": {"messages": [ … ], "tools": ["list_dir", "…"], "message_count": 3},
      "reasoning": "…", "text": "…",
      "tool_calls": [{"name": "read_file", "arguments": {…}, "ok": true,
                       "result": "…", "duration_ms": 12}],
      "usage": {"requests": 1, "cache_reported_requests": 1,
                "prompt_tokens": 8120, "cache_hit_tokens": 4096,
                "cache_miss_tokens": 4024, "completion_tokens": 210,
                "total_tokens": 8330}
    }
  ],
  "usage": { "…同上，整条链路累加…" },
  "final_text": "…", "error": null
}
```

体积控制：快照里单条消息 ≤ 4000 字符、工具结果 ≤ 2000 字符（超出注明原长）。
写入为追加，不自动清理（个人使用，可手动删文件）。

## 3. 采集是怎么实现的（埋点在 engine）

- **位置**：`engine.chat_stream` 内部（观测是引擎的横切关注点——插件拿不到
  "每轮调模型"的时机，所以不能做成插件）；
- **每轮调模型前**抓一份 `request` 快照（用 `_slim_message` 瘦身）；
- `llm.chat_stream` 的 `reasoning` 事件**只进链路、不转发前端**；
- **落盘在 `finally`**：成功（ok）、异常（error，含错误信息与已流出的部分）、
  生成器被提前关闭（interrupted）都会留痕；
- `conversation_id` / `trace_source` 由调用方传入：主对话传会话 id，
  子代理传 `trace_source="subagent"`（列表里会显示"子代理"徽章）。

## 4. 接口与前端

| 接口 | 说明 |
|---|---|
| `GET /api/traces` | 最近 50 条链路摘要（列表页用，带每条链路的命中率） |
| `GET /api/traces/<id>` | 单条完整链路（详情页用，命中率已算好） |
| `GET /api/cache` | 用量汇总：总账本 / 按天序列 / 最近任务（命中率面板用） |

前端入口：侧栏左下角「查看链路日志」按钮 → 弹层，顶部两个标签页：

- **任务链路**：列表 → 详情。详情按轮展示「① 本轮用量 ② 上下文结构
  （逐条消息可展开）③ 思考过程 ④ 每轮输出 ⑤ 工具调用」；
  编号是动态的——老链路没有 usage 时不会留下空号。
- **缓存命中**：KPI（命中率 / 输入 / 请求数 / 输出）+ 总体堆叠条 +
  按天柱状图 + 最近任务列表 + 口径说明。

折叠交互用原生 `<details>`，Esc / 点遮罩关闭。

## 4.1 缓存命中率的口径

```
命中率 = cache_hit_tokens ÷ (cache_hit_tokens + cache_miss_tokens)
```

- 只统计**服务端回了缓存字段**的请求（`cache_reported_requests`）；
  端点不支持缓存时命中率显示"—"，不能算成 0%——那会把整体数字拉低成假的。
- 字段名做了归一：DeepSeek 的 `prompt_cache_hit_tokens`、
  OpenAI 风格的 `prompt_tokens_details.cached_tokens`、
  Anthropic 风格的 `cache_read_input_tokens` 都认。
- 同一会话第 1 轮必然 0%（缓存要先把前缀写进去）；之后带着更长的历史重发，
  前缀没变的部分才会命中。压缩上下文 / 改系统提示词 / 换工作区都会让前缀变化。
- **流式响应默认不带 usage**：`llm.chat_stream` 必须发
  `stream_options: {"include_usage": true}`（开关 `ENABLE_USAGE_STATS`），
  且那一包 `choices` 是空数组——解析时要先处理 `usage` 再取 `choices[0]`。

## 5. 已知限制与坑

- **思考内容"时有时无"**：`reasoning_content` 是否出现取决于服务端
  （实测同一条件两次请求，一次 46 字符、一次为空；带全部工具说明书的
  请求更常为空）。链路日志如实显示"本轮没有思考内容"，这不是采集 bug；
- **回传要求**：官方文档要求"带 tools 的请求要把 reasoning_content
  随 assistant 消息回传，否则可能 400"——引擎已在工具循环内回传；
  跨请求（用户下一轮对话）的历史里**不存**思考内容（体积 + 隐私），
  实测当前 API 未因此报错，若将来遇到 400 再处理；
- **快照是截断版**：单条消息 4000 字符上限——超长内容看原文请查对话记录；
- **老链路没有用量**：命中率是 2026-11 才加上的采集，之前的 `traces.jsonl`
  里没有 `usage` 字段——统计面板会跳过它们，也无法回溯补算；
- **不自动清理**：`traces.jsonl` 只增不减，体积大了手动删（或写清理脚本）；
- **敏感内容**：链路里含完整上下文（可能包含文件内容）——已加入
  `.gitignore`，不要提交、不要随意外发。
