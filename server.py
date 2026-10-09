"""
server.py —— 启动入口（用这个文件启动整个项目）

在终端运行：      python server.py
然后浏览器打开：  http://localhost:8000

它负责三类事情：
  1. 把网页发给浏览器              （GET  /）
  2. 读取对话状态                  （GET  /api/state）   → storage 回放事件日志推导出来
  3. 处理前端发来的请求：
     - 聊天                        （POST /api/chat）    → 交给 engine 处理
     - 追加事件                    （POST /api/events）  → storage 往日志末尾写（只追加，不重写）
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import config
import engine
import storage

# 项目根目录（就是本文件所在的位置）
ROOT = Path(__file__).resolve().parent


class ChatHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器：定义"收到什么请求，回什么内容"。"""

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

        elif self.path == "/api/events":
            # ---- 事件接口：把前端发来的一批事件追加到日志末尾 ----
            try:
                storage.append_events(data.get("events", []))
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"error": str(e)})

        else:
            self.send_error(404, "Not Found")

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
