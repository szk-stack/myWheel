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
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
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
        if self.path in ("/", "/index.html"):
            # 把网页文件发出去
            html = (ROOT / "static" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        elif self.path == "/api/state":
            # 读取接口：回放事件日志，把"推导出来的当前状态"发回去
            self._send_json(200, storage.load_state())

        elif self.path.startswith("/static/"):
            # 静态资源（比如前端用来渲染 Markdown 的 marked.min.js）
            self._serve_static_file(self.path[len("/static/"):])

        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        """前端"提交数据"时走这里。"""
        # 先把请求体里的 JSON 读出来（聊天和保存数据都需要这一步）
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

        elif self.path == "/api/events":
            # ---- 事件接口：把前端发来的一批事件追加到日志末尾 ----
            try:
                storage.append_events(data.get("events", []))
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        else:
            self.send_error(404, "Not Found")

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
            for event in engine.chat_stream(data.get("messages", [])):
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

    # ② 启动 HTTP 服务
    #    绑定 127.0.0.1 表示"只有本机能访问"，安全，而且不会弹防火墙提示
    address = ("127.0.0.1", config.PORT)
    try:
        server = HTTPServer(address, ChatHandler)
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
