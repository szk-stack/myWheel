"""
server.py —— 启动入口（用这个文件启动整个项目）

在终端运行：      python server.py
然后浏览器打开：  http://localhost:8000

它负责三类事情：
  1. 把网页发给浏览器              （GET  /）
  2. 读取对话状态                  （GET  /api/state）   → storage 回放事件日志推导出来
  3. 处理前端发来的请求：
     - 聊天（普通）                （POST /api/chat）         → 交给 engine 处理
     - 聊天（流式）                （POST /api/chat/stream）  → 边收边发，打字机效果
     - 追加事件                    （POST /api/events）       → storage 往日志末尾写（只追加，不重写）
     - 设置工作区                  （POST /api/workspace）    → 校验并创建目录（agent 的活动范围）
"""

import json
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
import engine
import storage
import toolbox

# 项目根目录（就是本文件所在的位置）
ROOT = Path(__file__).resolve().parent


class ChatHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器：定义"收到什么请求，回什么内容"。"""

    # 用 HTTP/1.1（默认是 1.0）。流式接口需要它——这样才能"边生成边发送"，
    # 而不是等整个回答都准备好了才一次性发出去。
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        """浏览器"读取"时走这里。"""
        # 防御：万一有客户端给 GET 带了请求体，先把它读掉——
        # 否则残留字节会污染同一条连接上的下一个请求（经典坑，实测复现过）
        self._drain_body()

        if self.path in ("/", "/index.html"):
            # 把网页文件发出去
            html = (ROOT / "static" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        elif self.path == "/api/state":
            # 读取接口：回放事件日志，把"推导出来的当前状态"发回去；
            # 顺带带上"上下文配置"（前端画用量条和压缩阈值刻度要用）
            state = storage.load_state()
            import plugins.compact as compact_module
            state["context_config"] = {
                "window": config.CONTEXT_WINDOW,
                "threshold_ratio": config.COMPACT_THRESHOLD_RATIO,
                "keep_recent": config.KEEP_RECENT_MESSAGES,
                "enabled": config.ENABLE_COMPACT,
                # 每次请求的"固定基线占用量"（系统人设 + 工具说明书）——
                # 属于"上下文用量"（窗口占用）的固定部分，不是费用
                "baseline_tokens": compact_module.baseline_tokens(),
            }
            self._send_json(200, state)

        elif self.path.startswith("/static/"):
            # 静态资源（比如前端用来渲染 Markdown 的 marked.min.js）
            self._serve_static_file(self.path[len("/static/"):])

        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        """前端"提交数据"时走这里。"""
        # 特例：工作区选择接口不需要请求体（它只是"弹个对话框"），
        # 所以放在最前面，不经过下面"必须先读 JSON"的关卡
        # （否则空请求体会被判成 400，对话框就弹不出来了）
        if self.path == "/api/workspace/pick":
            # ★ 重要：先把它可能携带的请求体读掉，再处理——
            #   否则这几个字节会残留在连接里、粘到"下一个请求"的前面，
            #   造成诡异的 501 / 404 / 连接中断（实测复现过的坑！）
            self._drain_body()
            self._handle_workspace_pick()
            return

        # 其余接口都需要 JSON 请求体，先把它读出来
        try:
            data = self._read_json()
        except Exception:
            self._send_json(400, {"error": "请求格式不对：不是合法的 JSON"})
            return

        if self.path == "/api/chat":
            # ---- 聊天接口：交给引擎处理 ----
            try:
                reply = engine.chat(data.get("messages", []))
                self._send_json(200, {"reply": reply})
            except Exception as e:
                # 出任何错（比如还没填 Key），都把错误信息发回前端显示
                self._send_json(500, {"error": str(e)})

        elif self.path == "/api/chat/stream":
            # ---- 流式聊天接口：边收边发（打字机效果）----
            self._handle_chat_stream(data)

        elif self.path == "/api/workspace":
            # ---- 工作区接口：校验一个目录（不存在就创建），供界面设置工作区用 ----
            try:
                path, created = toolbox.prepare_workspace(data.get("path", ""))
                self._send_json(200, {"ok": True, "path": path, "created": created})
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        elif self.path == "/api/compact":
            # ---- 手动压缩接口：把较早的对话总结成摘要（界面"立即压缩"按钮用）----
            try:
                import plugins.compact as compact_module
                result = compact_module.make_compaction(
                    data.get("messages", []), data.get("compaction")
                )
                if result is None:
                    self._send_json(200, {"ok": False, "error": "消息还太少，暂时不需要压缩"})
                else:
                    self._send_json(200, {"ok": True, **result})
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        elif self.path == "/api/events":
            # ---- 事件接口：把前端发来的一批事件追加到日志末尾 ----
            try:
                storage.append_events(data.get("events", []))
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        else:
            self.send_error(404, "Not Found")

    # ---------- 工作区选择：弹出系统"选择文件夹"对话框 ----------

    def _handle_workspace_pick(self):
        """弹出一个系统"选择文件夹"对话框，把选中的路径发回前端。

        两个讲究（都是实测踩坑换来的）：
          ① 优先用 PowerShell 7（pwsh）：它带的对话框是和"打开文件"同款的
             现代样式；机器上没装就退回系统自带的 Windows PowerShell（旧样式）。
          ② 对话框要"抢到最前面"：服务是从后台弹框的，系统默认会让它
             躲在浏览器后面——用一个隐藏的"置顶小窗体"做它的挂靠窗口。
        """
        ps_script = (
            '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; '
            'Add-Type -AssemblyName System.Windows.Forms; '
            '$owner = New-Object System.Windows.Forms.Form; '
            '$owner.TopMost = $true; '
            '$owner.ShowInTaskbar = $false; '
            '$owner.WindowState = "Minimized"; '
            '$owner.Show(); '
            '$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; '
            '$dialog.Description = "选择 agent 的工作区目录（可点「新建文件夹」）"; '
            '$dialog.ShowNewFolderButton = $true; '
            'if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) '
            '{ Write-Output $dialog.SelectedPath } else { Write-Output "CANCELLED" }; '
            '$owner.Close()'
        )

        # 优先新版 PowerShell（现代对话框样式），没有就退回系统自带的
        ps_exe = "pwsh" if shutil.which("pwsh") else "powershell"

        try:
            completed = subprocess.run(
                [ps_exe, "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, timeout=600, encoding="utf-8",
                # 不闪黑窗口（对话框是 GUI，不受影响）
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self._send_json(200, {"ok": False, "error": f"无法弹出选择框：{e}"})
            return

        out = (completed.stdout or "").strip()
        if out == "CANCELLED":
            self._send_json(200, {"ok": False, "cancelled": True})
        elif not out:
            detail = (completed.stderr or "").strip()[:200]
            self._send_json(200, {"ok": False, "error": f"选择框没有返回结果（环境可能不支持图形界面）：{detail}"})
        else:
            self._send_json(200, {"ok": True, "path": out})

    # ---------- 流式聊天的三个小工具（打字机效果） ----------

    def _handle_chat_stream(self, data):
        """处理流式聊天请求：引擎每吐出一段文字，就立刻发给浏览器。

        技术上这叫 SSE（Server-Sent Events）：响应不设置总长度，
        而是一个数据块一个数据块地"挤"出去，浏览器边收边显示。
        """
        try:
            self._start_sse()
            # 边收边发：engine.chat_stream 是生成器，它吐出的本来就是
            # "事件字典"（文字增量 / 工具调用 / 工具结果），原样转发给浏览器，
            # 前端按里面的 type 字段分别处理
            # 注意：把前端传来的"工作区"一并交给引擎（None = 默认项目目录）
            for event in engine.chat_stream(
                data.get("messages", []),
                workspace=data.get("workspace"),
                compaction=data.get("compaction"),   # 压缩状态（compact 插件要用）
            ):
                self._send_sse(event)
            self._end_sse()
        except Exception as e:
            # 出错了（比如还没填 Key、网络断了）：
            # 此时响应头已经发出去了，只能把错误也包成一条流消息发回去，
            # 前端收到后会把它显示出来。
            try:
                self._send_sse({"error": str(e)})
                self._end_sse()
            except Exception:
                pass

    def _start_sse(self):
        """发送流式响应的开头（响应头）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # 手动使用"分块传输"（chunked）：回答总长度未知，
        # 只能发一块算一块。这是 HTTP/1.1 支持流式响应的基础。
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _send_sse(self, obj):
        """把一个字典作为一条 SSE 消息发出去。

        消息的文本格式是：  data: <内容> 加两个换行（这是 SSE 的规定）。
        内容用 JSON 包一层——这样就算文字里带换行也不会发乱。
        """
        payload = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
        data = payload.encode("utf-8")
        # 手动分块编码：先写"这一块有多少字节（十六进制）"，再写内容
        self.wfile.write(f"{len(data):X}\r\n".encode("utf-8") + data + b"\r\n")
        self.wfile.flush()   # ★ 立即推出去，不许攒在缓冲区

    def _end_sse(self):
        """发送结束标记 [DONE]，并结束整个分块响应。"""
        done = "data: [DONE]\n\n".encode("utf-8")
        self.wfile.write(f"{len(done):X}\r\n".encode("utf-8") + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")   # 分块编码的结束标记：长度为 0 的块
        self.wfile.flush()

    def _serve_static_file(self, relative_path):
        """把 static/ 目录里的文件发出去（路径限制在 static 目录内，防越界）。"""
        static_root = (ROOT / "static").resolve()
        target = (static_root / relative_path).resolve()

        if not target.is_relative_to(static_root) or not target.is_file():
            self.send_error(404, "Not Found")
            return

        data = target.read_bytes()
        # 简单的类型判断（够我们用了）
        if target.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        else:
            content_type = "application/octet-stream"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- 下面两个是给上面用的小工具函数 ----------

    def _drain_body(self):
        """把请求体读出来丢弃（当某个接口不需要 body 时的"清道夫"）。

        这是 HTTP keep-alive 的经典坑：连接被复用时，
        上一个请求的 body 如果没被读干净，会粘到下一个请求上，
        把请求行解析搞乱（表现为 501 "Unsupported method"、404 或连接中断）。
        """
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            self.rfile.read(length)

    def _read_json(self):
        """小工具：把请求体里的 JSON 读出来，变成字典。"""
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body)

    def _send_json(self, status, obj):
        """小工具：把一个字典转成 JSON 发出去。"""
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # 让终端里的日志更简洁一点（覆盖默认的格式）
        print(f"[HTTP] {fmt % args}")


def main():
    # ① 先加载所有插件（把插件函数挂到空槽上）
    engine.load_plugins()

    # ② 加载所有工具（把 tools/ 里的工具登记进工具箱）
    toolbox.load_tools()

    # ② 启动 HTTP 服务（★ 用"多线程"版本，这很重要！）
    #    为什么必须多线程？因为浏览器会保持 keep-alive 长连接：
    #    单线程时，服务器会"守"着一条空闲连接等它的下一个请求，
    #    其他新连接（比如浏览器并行加载多个脚本文件）会被无限排队 → 整页卡死。
    #    ThreadingHTTPServer 给每个连接分配一个线程，这个问题就不存在了。
    #    绑定 127.0.0.1 表示"只有本机能访问"，安全，而且不会弹防火墙提示
    address = ("127.0.0.1", config.PORT)
    try:
        server = ThreadingHTTPServer(address, ChatHandler)
    except OSError:
        print(f"启动失败：端口 {config.PORT} 被占用了！")
        print("解决办法：打开 config.py，把 PORT 改成 8001 再试。")
        return

    print("=" * 52)
    print("服务已启动！请用浏览器打开：")
    print(f"    http://localhost:{config.PORT}")
    print("（在这个终端按 Ctrl+C 可以停止服务）")
    print("=" * 52)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止，再见！")


if __name__ == "__main__":
    main()
