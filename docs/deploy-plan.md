# myWheel · 容器化部署总计划

建立：2026-11-06
目标：用 Docker 把项目部署到一台 Linux 服务器，最终做到「push 到 main → 自动上线」。
原则：**一次只推进一步**；上一阶段的「做完的标志」没达成，不进下一阶段。

## 阶段总览

| 阶段 | 做什么 | 做完的标志 | 状态 |
| --- | --- | --- | --- |
| 0 代码侧就绪 | `envconfig` / 数据目录 / `HOST` / `requirements.txt` / `.dockerignore` / `Dockerfile` | 81/81 测试通过、模拟无 `config.py` 启动返回 HTTP 200、代码已 push | ✅ 完成 |
| 1 本机跑通 | 启动 Docker Desktop → `docker build` → `docker run` → 打开 8000 | 浏览器能对话、`docker logs` 有启动输出 | ⬅ **当前** |
| 2 数据卷验收 | 加 `-v` 重跑 → 删容器 → 再起一次 | 会话记录还在 | 待做 |
| 3 搬上服务器 | 装 Docker → `git clone` → build → run | 服务器 `IP:8000` 能对话 | 待做 |
| 4 deploy.sh 半自动 | 一个脚本串起拉代码 / 构建 / 停旧 / 起新 | SSH 上去敲一次就更新完 | 待做 |
| 5 Actions 全自动 | push `main` → 自动构建推镜像 → SSH 重启 | 改完代码几分钟后自动生效 | 待做 |

## 当前卡点（阶段 0 → 1）

本机 Docker CLI 28.3.2 已安装，但 `docker info` 报
`open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified`
—— **Docker Desktop 引擎没启动**。双击打开它，等托盘图标稳定，然后：

```sh
cd D:/project/朴素agent
docker build -t mywheel:dev .
docker run -d --name mywheel -p 8000:8000 \
  -e MYWHEEL_API_KEY=sk-你的Key mywheel:dev
# 打开 http://localhost:8000 聊一句
docker logs mywheel
```

这一阶段**先不挂数据卷**，少引入一个变量。`build` 输出里的 `[1/8]`…`[8/8]` 就是 Dockerfile 的行号；第一次要下载底座和依赖，慢是正常的。

## 阶段之间的门槛

- 0 → 1：代码已 push（已完成）
- 1 → 2：本机页面能正常对话
- 2 → 3：删容器重跑后会话还在
- 3 → 4：服务器直接 IP 访问稳定
- 4 → 5：脚本手动跑过三次没出岔子

## 阶段 0 已完成明细

| 提交 | 内容 |
| --- | --- |
| `ab90e6b` | 新增 `envconfig.py`（缺 `config.py` 时从模板兜底、`MYWHEEL_*` 白名单覆盖、`data_dir()` 统一落盘）、`requirements.txt`、`.dockerignore`；`HOST` 可配；六个数据写入点统一；6 个回归测试 |
| `208849c` | 新增 `Dockerfile`（8 条指令，逐行中文注释；Key 用 `-e` 注入，不进镜像） |

验证记录：81/81 测试通过；带 `MYWHEEL_*` 启动返回 HTTP 200；复制出一份**不含 `config.py`** 的模拟 clone 目录启动，同样 HTTP 200 且数据目录被创建。

## 环境变量速查

| 变量 | 用途 | 在哪给 |
| --- | --- | --- |
| `MYWHEEL_HOST=0.0.0.0` | 监听所有网卡，容器外才连得上 | Dockerfile 里已设默认值 |
| `MYWHEEL_DATA_DIR=/app/data` | 会话、Key、上传、cookie 全落这里 | Dockerfile 里已设默认值 |
| `MYWHEEL_API_KEY` | 大模型 Key，**绝不写进镜像** | `docker run -e` |
| `MYWHEEL_PORT` | 默认 8000 | 需要时 `-e` |
| `MYWHEEL_ENABLE_VIDEO=false` | 服务器上先关掉视频转写 | `docker run -e` |

阶段 2 验收用的命令（Linux 上把 `D:/mywheel-data` 换成 `/srv/mywheel/data`）：

```sh
docker run -d --name mywheel -p 8000:8000 \
  -v D:/mywheel-data:/app/data \
  -e MYWHEEL_API_KEY=sk-你的Key mywheel:dev
docker exec mywheel ls /app/data      # 应能看到 data.jsonl
docker rm -f mywheel                  # 删掉容器，再跑一次同样的命令
```

## 明确后置（不做也不挡主线）

- **Nginx / HTTPS / 登录认证**：等阶段 3 稳了再上；反代必须 `proxy_buffering off;`，否则流式回答会卡住。
- **Linux 版 ASR**：让视频转写可用需要另起服务 + 模型 + ffmpeg，先用 `MYWHEEL_ENABLE_VIDEO=false` 关掉。
- **Windows 专属工具**：`screenshot` 依赖 Edge、`exec_command` 的工具描述是 cmd，容器里会报错；对话 / 知识库 / 文件读写不受影响。

## 安全底线

阶段 3 之前 `8000` 只在本机或内网开。这个服务**没有登录认证**且 `ENABLE_EXEC=True`，直接对公网开放等于把「花你的 API Key + 读写文件 + 执行命令」对外开放。

## 备注

- 本文件在 `docs/` 下，会被 `KNOWLEDGE_SOURCES` 扫进知识库，agent 能直接回答「部署到哪一步了」。
- 每完成一个阶段，回来把「阶段总览」里的状态改掉。
