# myWheel 的装配说明书：在一台全新的机器上把项目跑起来
#
#   docker build -t mywheel:dev .
#   docker run -d --name mywheel -p 8000:8000 \
#     -e MYWHEEL_API_KEY=sk-你的Key mywheel:dev
#
# 下面 8 条指令，正好对应「你手动做一遍」的 8 个动作。

# ① 选底座：一个已经装好 Python 3.11 的精简 Linux。
#    别换 alpine —— numpy 在它没有现成安装包，要现场编译，很容易卡住。
FROM python:3.11-slim

# ② 定工作目录：相当于在镜像里 cd /app，后面所有相对路径都以它为基准。
WORKDIR /app

# ③ 先只拷依赖清单。单独一行是为了保护下一行的缓存：
#    这个文件不变，改业务代码就不会触发重新 pip install。
COPY requirements.txt .

# ④ 构建时装依赖，装完固化进镜像；容器启动时不再装。
RUN pip install --no-cache-dir -r requirements.txt

# ⑤ 把项目代码搬进去：第一个 . 是本机当前文件夹，第二个 . 是镜像里的 /app。
#    .dockerignore 会先划掉 config.py / api_key.json / data.jsonl / videos/，
#    所以镜像里没有 config.py，envconfig.py 会从 config.example.py 兜底。
COPY . .

# ⑥ 默认环境变量：HOST 让服务监听所有网卡（绑 127.0.0.1 容器外连不上），
#    DATA_DIR 把会话、Key、上传、cookie 集中到一个目录，方便只挂一个卷。
#    API Key 故意不写在这里，用 docker run -e 传进去。
ENV MYWHEEL_HOST=0.0.0.0 MYWHEEL_DATA_DIR=/app/data

# ⑦ 只是声明「服务在 8000」，不会真的开放端口；真正对外靠 docker run -p。
EXPOSE 8000

# ⑧ 容器一启动就跑这条。数组写法让 Python 成为 1 号进程，
#    docker stop 的信号能正确送到它，不会留下卡住的进程。
CMD ["python", "server.py"]
