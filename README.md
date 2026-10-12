# 我的第一个 Agent（极简版）

一个**零框架、零依赖**的问答机器人：只要电脑上装了 Python，就能跑起来。
（前端有一个辅助文件 `static/marked.umd.js`——把 AI 回复渲染成 Markdown 格式的开源库，已下载到项目里，运行时不需要联网安装任何东西。）

- 前端：一个网页（`static/index.html`）
- 后端：纯 Python 标准库，不用装任何包
- 大模型：DeepSeek（任何 OpenAI 格式的接口都可以）
- 架构：**空槽 + 插件**，以后加功能就往 `plugins/` 里扔文件
- 数据：**事件日志**（`data.jsonl`，只追加不重写）——思路学自开源项目 Pi
- 输出：流式打字机效果（模型边生成边显示）
- 工具：模型能自己看文件、改文件了（四个文件工具 + 自动多轮调用）

---

## 一、跑起来（一共 3 步）

### 第 1 步：填 API Key
打开 `config.py`，把这一行改成你的 Key（保存在引号里）：

    API_KEY = "sk-你的key"

Key 在 https://platform.deepseek.com 创建，以 `sk-` 开头。

### 第 2 步：启动后端
在本文件夹里打开终端，运行：

    python server.py

看到 "服务已启动" 几个字就成功了。

### 第 3 步：打开网页
浏览器访问 http://localhost:8000 ，开聊！

> 注意：改了 `config.py`（比如填了 Key）之后，要**重启服务**才生效：
> 在终端按 Ctrl+C 停掉，再重新运行 `python server.py`。

---

## 二、每个文件是干嘛的

| 文件 | 作用 | 要动它吗 |
|---|---|---|
| `config.py` | 所有配置（Key、模型、人设、端口） | ★ 常改 |
| `server.py` | 启动入口，收发 HTTP 请求 | 基本不动 |
| `engine.py` | 引擎：把流程串起来 | 基本不动 |
| `llm.py` | 和 DeepSeek 说话 | 基本不动 |
| `hooks.py` | 空槽机制（核心就是几个列表） | 想加新槽时才动 |
| `storage.py` | 事件日志管理员：追加事件 + 回放推导状态 | 想换存储方式时动 |
| `toolbox.py` | 工具箱：注册表 + 调度 + 自动加载 tools/ | 基本不动 |
| `tools/` | ★ 工具目录：模型能用的"能力"都放这 | ★ 常加 |
| `plugins/` | ★ 插件目录，以后的功能都放这 | ★ 常加 |
| `static/index.html` | 聊天网页（含前端的全部逻辑） | 想改界面时动 |
| `data.jsonl` | 事件日志：发生过的一切都记在这（自动生成） | 可以打开看 |
| `data.json.bak` | 老格式数据的备份（第一次"搬家"后自动留下） | 不用管 |

---

## 三、一次对话都发生了什么

    浏览器（保存全部会话 + 当前会话的完整历史）
       │  POST /api/chat/stream，带上当前会话的全部历史
       ▼
    server.py   收请求，开一条"流式通道"（SSE）
       ▼
    engine.py   跑一轮"agent 循环"：
       ├─ ① before_llm 空槽 → system_prompt 插件插入人设
       ├─ ② 流式调用 DeepSeek：
       │       ├─ 模型直接回答 → 文字一段段流给浏览器（打字机效果）
       │       └─ 模型要求用工具 → 执行工具 → 结果塞回历史 → 再调模型（循环）
       └─ ③ after_llm 空槽 → logger 插件打印日志
       ▼
    浏览器边收边显示

    另外：页面上的每个动作（发消息、新建、删除、重命名、切换、工具调用）
    都会被翻译成一条条"事件"，追加到 data.jsonl 日志的末尾——
    只往后加行，从不重写文件。

---

## 四、对话数据存在哪？（事件日志，学自 Pi）

数据存在 `data.jsonl` 里。"jsonl" 的意思是**每行一条 JSON 记录**，
用记事本打开长这样（真实例子）：

    {"type": "session", "id": "c179...", "title": "新对话", "timestamp": "..."}
    {"type": "message", "conversation_id": "c179...", "role": "user", "content": "你好", "timestamp": "..."}
    {"type": "message", "conversation_id": "c179...", "role": "assistant", "content": "你好呀！", "timestamp": "..."}
    {"type": "rename", "id": "c179...", "title": "打个招呼", "timestamp": "..."}

全部事件类型（`type` 字段）：

| type | 含义 |
|---|---|
| `session` | 新建了一个会话 |
| `message` | 一条新消息（role 是 user 或 assistant） |
| `tool` | 一次工具调用记录（调了什么、结果如何——方便回溯） |
| `rename` | 会话改名 |
| `delete` | 删除会话（日志里的旧消息仍在，只是回放时忽略它） |
| `switch` | 切换当前打开的会话 |

**关键思想："当前数据"是算出来的，不是存的。**
每次打开页面，后端会从头到尾把日志**回放**一遍
（遇到 session 就新建、message 就追加、delete 就忽略……），
推导出会话列表再发给页面。这就是 Pi（github.com/earendil-works/pi）
的核心思路：**日志是唯一真相，状态只是它的投影**。

好处：写入快（只加一行）、不怕写坏（顶多丢最后一行）、历史完整可追溯。

---

## 五、工具调用（模型自己会看文件了）

模型现在会自己调用四个工具干活：

| 工具 | 作用 |
|---|---|
| `list_dir` | 列出目录里的文件 |
| `read_file` | 读文件（带行号，可用 offset/limit 分段读）|
| `write_file` | 创建 / 整体覆盖文件 |
| `edit_file` | 精确修改文件（要求原文唯一出现，防止改错地方）|

对话时它可能连续调用好几个（先列目录 → 再读文件 → 再回答你），
这叫作 **agent 循环**：调模型 → 要工具就执行 → 结果塞回去 → 再调模型。
最多循环 8 轮（`config.py` 里的 `MAX_TOOL_ROUNDS`，防死循环的保险丝）。

- **安全边界**：所有文件操作都被"关"在项目文件夹里（挡住 `../` 越界）；
- **一键关闭**：`config.py` 里 `ENABLE_TOOLS = False` 可以关掉全部工具；
- **工具记录**：每次工具调用都会变成对话里的一张灰色小卡片（点开看详情），
  也写进 `data.jsonl` 日志（`tool` 事件）——随时可以回溯"它当时干了什么"。

**怎么加一个新工具**：往 `tools/` 里扔一个文件，照着 `list_dir.py` 抄三段
（说明书 → 执行函数 → register 登记），重启服务就生效——和插件一样"文件即生效"。

---

## 六、怎么加一个新功能（模块化插入）

1. 在 `plugins/` 里新建一个 `.py` 文件
2. 写一个函数：改一改 `ctx`，然后 `append` 到对应空槽
3. 重启服务，插件自动生效，主流程一行都不用改

比如做一个"敏感词打码"插件：

    # plugins/bad_words.py
    from hooks import after_llm

    BAD_WORDS = ["某词1", "某词2"]

    def filter_words(ctx):
        for w in BAD_WORDS:
            ctx["reply"] = ctx["reply"].replace(w, "***")

    after_llm.append(filter_words)

---

## 七、练习题（从易到难）

1. 改 `config.py` 里的 `SYSTEM_PROMPT`，让 AI 变成"英语老师"
2. 写一个插件（挂 `after_llm`），统计每次回答有多少个字，打印出来
3. 写一个插件（挂 `before_llm`），历史超过 10 条时只保留最近 6 条（防止超出模型的上下文长度）
4. 在左侧列表底部加一个"删除全部对话"按钮（提示：照抄 `deleteConversation` 的写法）
5. 写一个小脚本：统计 `data.jsonl` 里一共有多少条 `message` 事件、你最常用的会话是哪个（提示：逐行读、数 `type` 字段）
6. 写一个新工具扔进 `tools/`：比如 `count_lines`（数一数某个文件有多少行）（提示：照抄 `read_file.py` 的三段结构）

---

## 八、常见问题

- **提示端口被占用？** 把 `config.py` 里 `PORT = 8000` 改成 `8001`。
- **提示没有 API Key？** 检查 `config.py` 里的引号是不是真的填了内容、有没有保存。
- **改了配置没生效？** config 是启动时读取的，改完要重启服务（Ctrl+C 再运行）。
- **Key 会不会泄露？** Key 只存在你电脑的 `config.py` 和后端内存里，前端网页看不到它。
- **`data.jsonl` 可以删吗？** 可以。删掉后重新打开页面会从空白开始（文件会自动重建）。
- **`data.json.bak` 是什么？** 老格式数据的备份。程序升级后第一次运行时，会自动把旧格式"搬家"成新的事件日志（就像 Pi 升级旧会话文件一样），原来的 `data.json` 会改名成 `.bak` 留档。
- **模型怎么突然开始"翻"我的文件？** 那是它在使用工具。每次工具调用都会显示成灰色小卡片，点开能看到它到底读了/写了什么；不想要这个能力就把 `config.py` 的 `ENABLE_TOOLS` 改成 `False`。
