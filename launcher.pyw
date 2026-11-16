"""
launcher.pyw —— myWheel 启动器

双击 `启动.bat` 会打开一个小窗口：
    点「启动服务」→ 后端跑起来，自动打开浏览器
    关掉窗口     → 后端跟着停，端口释放

【为什么是 .pyw 而不是 .py】
Windows 上 .pyw 关联的是 pythonw.exe，运行时不弹黑色控制台窗口。
.py 会用 python.exe，双击后先冒出一个黑框，很难看。

【为什么需要 启动.bat 中转】
电脑上可能装了好几套 Python，其中有的没带 tkinter（比如精简版），
直接双击 .pyw 万一撞上没有 tkinter 的那个，就是"双击没反应"。
启动.bat 会先挑一个带 tkinter 的 Python，再用它打开本文件。

【进程是怎么被管住的】
subprocess.Popen 启动 server.py 后，本进程持有它的句柄（PID）。
窗口关闭时调 terminate()，Windows 会直接结束那个进程——所以"关了窗口就停服务"
不是靠约定，是靠父子进程关系保证的。万一 terminate 不奏效（极少数情况），
3 秒后会 kill() 兜底。
"""

import os
import queue
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox
except ImportError:
    # 没有 tkinter 连窗口都开不了。用系统弹窗把原因说清楚，
    # 否则用户只会看到"双击没反应"。
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0,
            "当前这个 Python 没有自带 tkinter，启动器无法显示窗口。\n\n"
            "解决办法：用启动.bat 打开（它会自动挑一个带 tkinter 的 Python）。\n"
            "或者直接在终端运行：python server.py",
            "myWheel 启动器",
            0x10,
        )
    except Exception:
        pass
    sys.exit(1)


ROOT = Path(__file__).resolve().parent

# ---- 配色：跟项目前端一个调子（冷调浅灰 + 单一靛青强调色）----
BG = "#f5f6f8"
CARD = "#ffffff"
LINE = "#e3e6eb"
TEXT = "#1b1f27"
MUTED = "#6b7280"
ACCENT = "#2f6fed"
OK = "#15a34a"
DANGER = "#d92d20"
WARN = "#b45309"

FONT = "Microsoft YaHei UI"
MONO = "Consolas"


def read_port():
    """从 config.py 读端口；读不到就用 8000。"""
    try:
        import config
        return int(getattr(config, "PORT", 8000))
    except Exception:
        return 8000


def port_open(port, host="127.0.0.1"):
    """这个端口上有没有人在监听。"""
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


class Server:
    """管住 server.py 这个子进程。"""

    def __init__(self, port):
        self.port = port
        self.proc = None
        self.lines = queue.Queue()      # 子进程的输出，交给界面线程去取
        self.started_at = None

    # ---------- 启动 ----------
    def start(self):
        env = os.environ.copy()
        # 让子进程立刻吐输出（不然日志要等缓冲区满才出现）
        env["PYTHONUNBUFFERED"] = "1"
        # 统一按 UTF-8 输出，避免 Windows 默认 GBK 导致中文乱码
        env["PYTHONIOENCODING"] = "utf-8"

        # 不弹控制台窗口（启动器本身已经是无控制台的了）
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # 错误也并进日志，出问题看得见
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=flags,
        )
        self.started_at = time.time()

        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        """在后台线程里一行行读子进程输出，塞进队列。

        tkinter 不是线程安全的——子线程不能直接改界面，
        所以这里只往队列里放，由界面线程定时来取。
        """
        try:
            for line in self.proc.stdout:
                self.lines.put(line.rstrip("\n"))
        except Exception:
            pass

    # ---------- 状态 ----------
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def uptime(self):
        if not self.running() or not self.started_at:
            return "—"
        s = int(time.time() - self.started_at)
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"

    # ---------- 停止 ----------
    def stop(self):
        if not self.running():
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()        # 还不停就硬杀
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


class App:
    def __init__(self, root):
        self.root = root
        self.server = Server(read_port())
        self.external = port_open(self.server.port)   # 端口上已经有别人在跑了？
        self.url = f"http://127.0.0.1:{self.server.port}"

        root.title("myWheel 启动器")
        root.configure(bg=BG)
        root.resizable(False, False)
        self._center(560, 430)

        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        if self.external:
            self._log(f"检测到 {self.url} 已经有服务在跑（可能是在终端里手动启动的）。")
            self._log("这一个窗口不会重复启动；关掉它也不会停掉那个服务。")
            self._set_state("已在运行", WARN)
            self.btn_start.config(state="disabled")
        else:
            self._log("准备就绪。点「启动服务」开始。")

        self.root.after(150, self._tick)

    # ---------- 界面 ----------
    def _center(self, w, h):
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _build(self):
        # 顶部：标题 + 状态
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=24, pady=(22, 0))

        tk.Label(head, text="myWheel", bg=BG, fg=TEXT,
                 font=(FONT, 17, "bold")).pack(side="left")

        self.dot = tk.Label(head, text="●", bg=BG, fg=MUTED, font=(FONT, 11))
        self.dot.pack(side="right", padx=(0, 6))
        self.state = tk.Label(head, text="未启动", bg=BG, fg=MUTED, font=(FONT, 10))
        self.state.pack(side="right")

        tk.Label(self.root, text="一个零框架的极简 Agent", bg=BG, fg=MUTED,
                 font=(FONT, 9)).pack(anchor="w", padx=24, pady=(2, 0))

        # 信息卡
        card = tk.Frame(self.root, bg=CARD, highlightbackground=LINE,
                        highlightthickness=1)
        card.pack(fill="x", padx=24, pady=(16, 0))

        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=16, pady=14)

        left = tk.Frame(row, bg=CARD)
        left.pack(side="left", anchor="w")
        tk.Label(left, text="服务地址", bg=CARD, fg=MUTED,
                 font=(FONT, 9)).pack(anchor="w")
        tk.Label(left, text=self.url, bg=CARD, fg=TEXT,
                 font=(MONO, 11)).pack(anchor="w", pady=(2, 0))

        right = tk.Frame(row, bg=CARD)
        right.pack(side="right", anchor="e")
        tk.Label(right, text="运行时长", bg=CARD, fg=MUTED,
                 font=(FONT, 9)).pack(anchor="e")
        self.uptime = tk.Label(right, text="—", bg=CARD, fg=TEXT,
                               font=(MONO, 11))
        self.uptime.pack(anchor="e", pady=(2, 0))

        # 日志
        tk.Label(self.root, text="运行日志", bg=BG, fg=MUTED,
                 font=(FONT, 9)).pack(anchor="w", padx=24, pady=(16, 4))

        box = tk.Frame(self.root, bg=CARD, highlightbackground=LINE,
                       highlightthickness=1)
        box.pack(fill="both", expand=True, padx=24)

        # width/height 设成 1：Text 的默认尺寸是 80x24 字符，
        # 不压下去的话它会把整个窗口撑到 640x650，后面 pack 的自适应也白搭
        self.log = tk.Text(box, bg=CARD, fg="#3b4252", bd=0,
                           font=(MONO, 9), wrap="word", padx=12, pady=10,
                           highlightthickness=0, state="disabled",
                           width=1, height=1)
        self.log.pack(side="left", fill="both", expand=True)

        bar = tk.Scrollbar(box, command=self.log.yview, width=10)
        bar.pack(side="right", fill="y")
        self.log.config(yscrollcommand=bar.set)

        # 按钮
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=24, pady=(14, 20))

        self.btn_start = self._button(foot, "启动服务", self.on_start,
                                      primary=True)
        self.btn_start.pack(side="left")

        self.btn_open = self._button(foot, "打开界面", self.on_open)
        self.btn_open.pack(side="left", padx=(10, 0))
        self.btn_open.config(state="disabled")

        self.btn_stop = self._button(foot, "停止服务", self.on_stop,
                                     danger=True)
        self.btn_stop.pack(side="right")
        self.btn_stop.config(state="disabled")

    def _button(self, parent, text, command, primary=False, danger=False):
        if primary:
            bg, fg, hover = ACCENT, "#ffffff", "#2a63d6"
        elif danger:
            bg, fg, hover = CARD, DANGER, "#fdeceb"
        else:
            bg, fg, hover = CARD, TEXT, "#eef1f5"

        b = tk.Button(parent, text=text, command=command, bd=0,
                      relief="flat", cursor="hand2", padx=18, pady=8,
                      font=(FONT, 10), bg=bg, fg=fg,
                      activebackground=hover, activeforeground=fg,
                      highlightthickness=1,
                      highlightbackground=LINE if not primary else ACCENT,
                      disabledforeground="#b6bcc6")
        return b

    # ---------- 状态 ----------
    def _set_state(self, text, color):
        self.state.config(text=text, fg=color)
        self.dot.config(fg=color)

    def _log(self, line):
        self.log.config(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    # ---------- 动作 ----------
    def on_start(self):
        if port_open(self.server.port):
            self._log(f"端口 {self.server.port} 已被占用，无法启动。")
            messagebox.showwarning("端口被占用",
                                   f"{self.server.port} 上已经有程序在监听。\n"
                                   "可能是另开的一个 myWheel，先关掉它再试。")
            return

        self._log("正在启动服务……")
        self._set_state("启动中", WARN)
        self.btn_start.config(state="disabled")

        try:
            self.server.start()
        except Exception as e:
            self._log(f"启动失败：{e}")
            self._set_state("启动失败", DANGER)
            self.btn_start.config(state="normal")
            return

        self.root.after(200, self._wait_ready)

    def _wait_ready(self, tries=0):
        """轮询端口，等它真的能连上再算启动成功。"""
        if not self.server.running():
            self._log("进程退出了——看上面的日志。")
            self._set_state("已停止", DANGER)
            self.btn_start.config(state="normal")
            return

        if port_open(self.server.port):
            self._log(f"服务已就绪：{self.url}")
            self._set_state("运行中", OK)
            self.btn_open.config(state="normal")
            self.btn_stop.config(state="normal")
            webbrowser.open(self.url)
            return

        if tries > 60:      # 最多等 12 秒
            self._log("等了 12 秒还没就绪，可能启动有问题。")
            self._set_state("异常", DANGER)
            self.btn_stop.config(state="normal")
            return

        self.root.after(200, lambda: self._wait_ready(tries + 1))

    def on_open(self):
        webbrowser.open(self.url)

    def on_stop(self):
        if not self.server.running():
            self._set_state("已停止", MUTED)
            self.btn_stop.config(state="disabled")
            self.btn_start.config(state="normal")
            return

        self._set_state("停止中", WARN)
        self.root.update_idletasks()
        self.server.stop()

        # 等端口真的释放（有时要几十毫秒）
        for _ in range(20):
            if not port_open(self.server.port):
                break
            time.sleep(0.05)

        self._log("服务已停止。")
        self._set_state("已停止", MUTED)
        self.btn_stop.config(state="disabled")
        self.btn_open.config(state="disabled")
        self.btn_start.config(state="normal")

    def on_close(self):
        # 「关了窗口就停服务」——这是启动器存在的意义，不弹确认框
        if self.server.running():
            self.server.stop()
        self.root.destroy()

    # ---------- 定时刷新 ----------
    def _tick(self):
        while not self.server.lines.empty():
            self._log(self.server.lines.get())

        if self.server.running():
            self.uptime.config(text=self.server.uptime())

        self.root.after(150, self._tick)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


def _log_crash():
    """pythonw 没有控制台，异常会消失得无影无踪——界面不出、也没人告诉你为什么。

    所以把 traceback 落到文件里。下次再遇到"双击没反应"，
    先看项目根目录有没有 launcher_error.log，答案基本都在里面。
    """
    import traceback
    try:
        (ROOT / "launcher_error.log").write_text(
            time.strftime("%Y-%m-%d %H:%M:%S\n") + traceback.format_exc(),
            encoding="utf-8",
        )
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _log_crash()
        raise
