# 插件：system_prompt（项目人设）

| 状态 | 最后更新 | 关联代码 | 代码基线 |
|---|---|---|---|
| Stable | 2026-10-16 | `plugins/system_prompt.py` | `515d604` |

> 所属：[插件系统总览](README.md)

## 1. 职责

把 `config.SYSTEM_PROMPT`（项目人设）插到消息列表的**最前面**——
每次调模型前自动执行，让模型始终带着"人设"说话。

## 2. 挂载点与触发

- 空槽：`before_llm`（每次调模型前）；
- 输入：`ctx["messages"]`（前端本轮带来的历史）；
- 输出：往最前插一条 `{"role": "system", "content": config.SYSTEM_PROMPT}`
  （原地修改 `ctx["messages"]`）。

## 3. 两个关键判断（都是踩坑换来的）

### ① 用"内容比对"判断人设是否已插过（而不是看"第一条是不是 system"）

```python
has_persona = any(
    m.get("role") == "system" and m.get("content") == config.SYSTEM_PROMPT
    for m in messages
)
```

**为什么**：上下文压缩产生的"摘要"也是 system 消息——
如果判断逻辑是"第一条是 system 就当已有"，压缩后的
`[摘要system, …]` 会被**误认成人设已存在**，人设就插不进去了。
内容比对（精确等于人设文本才算）能分清"摘要"和"人设"。

### ② 响应 `skip_persona` 标记（子代理专用）

`ctx["meta"]["skip_persona"]` 为真时直接返回、什么都不做——
子代理有自己的专门人设（`SUBAGENT_PROMPT`），不需要项目人设混进去。

## 4. 实现要点与坑

- **幂等**：重复执行不会插入多条人设（内容比对保证）；
- **改人设只改 config**：插件本身不含任何文案；重启服务生效；
- **插入位置是最前**（`insert(0)`）——人设永远排在摘要和其他消息之前；
- 依赖 `ctx["meta"]` 的存在——engine 保证 `meta` 始终是字典（空 dict 起步），
  所以 `ctx["meta"].get(...)` 永远安全。
