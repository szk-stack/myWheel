# myWheel · 容器化部署总计划

建立：2026-11-06
目标：用 Docker 把项目部署到一台 Linux 服务器，最终做到「push 到 main → 自动上线」。
原则：**一次只推进一步**；上一阶段的「做完的标志」没达成，不进下一阶段。

## 阶段总览

| 阶段 | 做什么 | 做完的标志 | 状态 |
| --- | --- | --- | --- |
| 0 代码侧就绪 | `envconfig` / 数据目录 / `HOST` / `requirements.txt` / `.dockerignore` / `Dockerfile` | 81/81 测试通过、模拟无 `config.py` 启动返回 HTTP 200、代码已 push | ✅ 完成 |
| 1 本机跑通 | 启动 Docker Desktop → `docker build` → `docker run` → 打开 8000 | 浏览器能对话 | ✅ 完成 2026-11-06 |
| 2 数据卷验收 | 建卷 → 删容器 → 带卷重起 → 再删再起 | 删容器重跑后会话还在 | ⬅ **当前** |
| 3 搬上服务器 | 装 Docker → `git clone` → build → run | 服务器 `IP:8000` 能对话 | 待做 |
| 4 deploy.sh 半自动 | 一个脚本串起拉代码 / 构建 / 停旧 / 起新 | SSH 上去敲一次就更新完 | 待做 |
| 5 Actions 全自动 | push `main` → 自动构建推镜像 → SSH 重启 | 改完代码几分钟后自动生效 | 待做 |

## 阶段 1 命令速查（✅ 已完成）

2026-11-06 本机跑通：容器 `mywheel` 运行中、镜像 `mywheel:dev` 265 MB、
`0.0.0.0:8000->8000/tcp` 映射正常，`/app/data` 里已产生 `data.jsonl` / `traces.jsonl`。

（最初的卡点是 Docker Desktop 引擎没启动：`docker info` 报
`open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified`；
双击打开、等托盘图标稳定即可。）

```sh
# ① 先进入项目文件夹：后面 docker build . 里的那个点，指的就是「你现在所在的文件夹」
cd D:/project/朴素agent

# ② 照 Dockerfile 做一遍，产出一个镜像
#    -t mywheel:dev  给镜像起名字（mywheel）和标签（dev）；冒号后面是标签，不是端口
#    .               构建上下文 = 当前文件夹，COPY . . 就是从这里搬文件
docker build -t mywheel:dev .

# ③ 把镜像跑起来，得到一个容器
#    -d              后台运行，不占着终端（不加的话终端会被日志占住）
#    --name mywheel  给容器起名，后面的 logs / rm 就能用名字指它
#    -p 8000:8000   左边 = 你电脑的端口，右边 = 容器里的端口；这一句才让浏览器连得上
#    \               续行符：下面那行还属于这条命令，两行其实是一条
docker run -d --name mywheel -p 8000:8000 \
  -e MYWHEEL_API_KEY=sk-你的Key mywheel:dev
#    -e ...          启动时塞一个环境变量；API Key 靠它传进去，不写进镜像
#    sk-你的Key      换成你真实的 Key，不换的话页面能开、一对话就报错
#    mywheel:dev     最后这个是「用哪个镜像来跑」，就是 ② 做出来的那个

# ④ 去浏览器打开 http://localhost:8000 聊一句（# 开头是注释，不会执行）
#    这里的 8000 就是 -p 左边那个端口

# ⑤ 看容器里打印的启动日志和报错；页面打不开时先查这里
docker logs mywheel
```

这一阶段**先不挂数据卷**，少引入一个变量。`build` 输出里的 `[1/8]`…`[8/8]` 就是 Dockerfile 的行号；第一次要下载底座和依赖，慢是正常的。

第二次跑 `docker run` 之前要先 `docker rm -f mywheel`，否则会报「名字 mywheel 已被占用」——同一个名字只能有一个容器。

注：本机实测 `docker logs mywheel` 是空的，因为 Python 在容器里默认缓冲输出。想让日志实时可见，给 `docker run` 加一个 `-e PYTHONUNBUFFERED=1` 即可，不影响功能。

## 阶段 2 · 数据卷验收（⬅ 当前）

**要证明的事**：容器可以随手删掉重建，但会话记录不跟着丢。

起点（2026-11-06 实测）：`docker volume ls` 为空，`/app/data/data.jsonl` 1001 B、
`traces.jsonl` 3915 B —— 这些数据现在写在**容器自己的可写层**里，删容器即消失。
下面正好拿它当反面例子。

用**命名卷**（由 Docker 保管，独立于容器寿命），不碰 Windows 盘符共享设置：

```sh
# ① 建一个数据卷：由 Docker 保管的存储空间，寿命和容器无关
docker volume create mywheel-data

# ② 把当前容器删掉（-f = 先停再删，因为它正在跑）
#    阶段 1 那个容器没挂卷，里面的 data.jsonl 会一起消失，
#    刷新网页会连不上 —— 这就是没挂卷的后果
docker rm -f mywheel

# ③ 用同一个镜像重新起，这次把卷接到 /app/data
#    -v mywheel-data:/app/data   左边 = 卷名，右边 = 容器里的位置
#    之后写到 /app/data 的东西，实际都写进卷里
docker run -d --name mywheel -p 8000:8000 \
  -v mywheel-data:/app/data \
  -e MYWHEEL_API_KEY=sk-你的Key mywheel:dev

# ④ 打开 http://localhost:8000 聊一句（会话列表是空的，正常）
#    然后看数据是否落进卷里，应能看到 data.jsonl / traces.jsonl
docker exec mywheel ls -l /app/data

# ⑤ 关键一刀：再删一次，把 ③ 的命令原样再敲一遍
docker rm -f mywheel
docker run -d --name mywheel -p 8000:8000 \
  -v mywheel-data:/app/data \
  -e MYWHEEL_API_KEY=sk-你的Key mywheel:dev
```

**做完的标志**：刷新 `http://localhost:8000`，第 ④ 步那句对话还在。
原因：删容器只删那一层薄薄的可写层；卷在容器之外没被删，新容器接上同一个卷就看到了旧数据。

想在资源管理器里直接看到文件，可把 `-v` 左边换成 `D:/mywheel-data`（Linux 用
`/srv/mywheel/data`），但 Docker Desktop 需要先在 Settings → Resources → File sharing
把 D 盘加进去，否则会挂载失败或挂成空目录。
`docker volume rm mywheel-data` 会**真删掉所有会话**，别急着敲。

## 阶段之间的门槛

- 0 → 1：代码已 push（已完成）
- 1 → 2：本机页面能正常对话（已达成）
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
| `PYTHONUNBUFFERED=1` | 让 `docker logs` 能看到实时输出 | 需要时 `docker run -e` |

阶段 2 的完整步骤见上面「阶段 2 · 数据卷验收」一节，用命名卷，不依赖本机盘符。

## 明确后置（不做也不挡主线）

- **Nginx / HTTPS / 登录认证**：等阶段 3 稳了再上；反代必须 `proxy_buffering off;`，否则流式回答会卡住。
- **Linux 版 ASR**：让视频转写可用需要另起服务 + 模型 + ffmpeg，先用 `MYWHEEL_ENABLE_VIDEO=false` 关掉。
- **Windows 专属工具**：`screenshot` 依赖 Edge、`exec_command` 的工具描述是 cmd，容器里会报错；对话 / 知识库 / 文件读写不受影响。

## 安全底线

阶段 3 之前 `8000` 只在本机或内网开。这个服务**没有登录认证**且 `ENABLE_EXEC=True`，直接对公网开放等于把「花你的 API Key + 读写文件 + 执行命令」对外开放。

## 备注

- 本文件在 `docs/` 下，会被 `KNOWLEDGE_SOURCES` 扫进知识库，agent 能直接回答「部署到哪一步了」。
- 每完成一个阶段，回来把「阶段总览」里的状态改掉。
- 2026-11-06 阶段 1 实测记录：`docker ps` 显示 `mywheel` Up、`0.0.0.0:8000->8000/tcp`；`docker images` 显示 `mywheel:dev` 265 MB；`docker volume ls` 为空；`docker exec mywheel ls -l /app/data` 有 `data.jsonl` 1001 B、`traces.jsonl` 3915 B。
