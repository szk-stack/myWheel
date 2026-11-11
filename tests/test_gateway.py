"""
tests/test_gateway.py —— M0 止血：服务端鉴权、工作区参数、事件归属、预算与并发

这些测试都在"对外入口"这一层较真：
  - 没令牌 / 令牌错 → 401；本机无令牌模式行为不变
  - 工作区只认 sandbox 内的名字；绝对路径与 .. 一律拒
  - full_access 由服务端策略裁决，客户端说了不算
  - /api/events 不能凭空建会话、不能引用不存在的会话（整批拒绝）
  - engine 的三重预算（轮次 / token / 墙钟）会中断并给出原因
  - 并发满了回 503，不再起新线程
"""

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import config
import engine
import server
import storage
import toolbox
import tracing


class _ServerMixin:
    """起一个真实的本地 HTTP 服务，所有状态落在临时目录里。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.sandbox = self.root / "sandbox"
        self.sandbox.mkdir()

        self._patches = [
            mock.patch.object(storage, "LOG_FILE", self.root / "data.jsonl"),
            mock.patch.object(storage, "OLD_JSON_FILE", self.root / "data.json"),
            mock.patch.object(tracing, "TRACE_FILE", self.root / "traces.jsonl"),
            mock.patch.object(toolbox, "PROJECT_ROOT", self.root),
            mock.patch.object(toolbox, "SANDBOX_ROOT", self.sandbox),
            mock.patch.object(config, "AUTH_TOKEN", ""),
            mock.patch.object(config, "HOST", "127.0.0.1"),
            mock.patch.object(config, "MAX_CONCURRENCY", 8),
            mock.patch.object(server.ChatHandler, "log_message", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()

        self.server = server.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), server.ChatHandler, max_workers=8
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, dict(resp.getheaders()), raw
        finally:
            conn.close()

    def json_request(self, method, path, body=None, headers=None):
        status, headers, raw = self.request(method, path, body, headers)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            data = {"_raw": raw.decode("utf-8", "ignore")}
        return status, headers, data


class AuthTest(_ServerMixin, unittest.TestCase):
    def test_local_without_token_is_unchanged(self):
        """没配令牌 + 本机来源：和改之前一样直接放行。"""
        status, _, data = self.json_request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("conversations", data)

    def test_api_requires_token_when_configured(self):
        with mock.patch.object(config, "AUTH_TOKEN", "s3cret-token"):
            for path in ("/api/state", "/api/cache"):
                status, _, _ = self.request("GET", path)
                self.assertEqual(status, 401, path)

            status, _, data = self.json_request(
                "POST", "/api/events", {"events": []},
            )
            self.assertEqual(status, 401)
            self.assertIn("未授权", data.get("error", ""))

            status, _, _ = self.request(
                "GET", "/api/state",
                headers={"Authorization": "Bearer s3cret-token"},
            )
            self.assertEqual(status, 200)

            status, _, _ = self.request(
                "GET", "/api/state",
                headers={"Authorization": "Bearer wrong-token"},
            )
            self.assertEqual(status, 401)

    def test_login_sets_cookie_and_cookie_is_accepted(self):
        with mock.patch.object(config, "AUTH_TOKEN", "s3cret-token"):
            status, headers, data = self.json_request(
                "POST", "/api/auth", {"token": "s3cret-token"}
            )
            self.assertEqual(status, 200)
            self.assertTrue(data.get("ok"))
            cookie_header = headers.get("Set-Cookie", "")
            self.assertIn("mywheel_token=s3cret-token", cookie_header)
            self.assertIn("HttpOnly", cookie_header)

            status, _, _ = self.request(
                "GET", "/api/state",
                headers={"Cookie": "mywheel_token=s3cret-token"},
            )
            self.assertEqual(status, 200)

            status, _, _ = self.json_request("POST", "/api/auth", {"token": "nope"})
            self.assertEqual(status, 401)

    def test_public_page_still_loads_without_token(self):
        """登录页 / 静态资源必须能匿名访问，否则连输令牌的界面都打不开。"""
        with mock.patch.object(config, "AUTH_TOKEN", "s3cret-token"):
            status, _, raw = self.request("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn(b"<html", raw.lower())


class WorkspaceTest(_ServerMixin, unittest.TestCase):
    def test_name_is_accepted_and_created_under_sandbox(self):
        status, _, data = self.json_request(
            "POST", "/api/workspace", {"path": "练习1"}
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["path"], "练习1")
        self.assertTrue((self.sandbox / "练习1").is_dir())

    def test_absolute_path_is_rejected(self):
        for bad in ("C:\\Windows", "/etc", "C:/Windows"):
            status, _, data = self.json_request("POST", "/api/workspace", {"path": bad})
            self.assertEqual(status, 400, bad)
            self.assertTrue(data.get("error"))

    def test_parent_traversal_is_rejected(self):
        for bad in ("../..", "..", "a/../../b", "sandbox\\..\\x"):
            status, _, data = self.json_request("POST", "/api/workspace", {"path": bad})
            self.assertEqual(status, 400, bad)
            self.assertTrue(data.get("error"))

    def test_chat_stream_rejects_bad_workspace_before_streaming(self):
        status, headers, data = self.json_request(
            "POST", "/api/chat/stream",
            {"messages": [], "workspace": "C:\\Windows"},
        )
        self.assertEqual(status, 400)
        self.assertIn("工作区", data.get("error", ""))
        self.assertNotIn("text/event-stream", headers.get("Content-Type", ""))

        status, _, data = self.json_request(
            "POST", "/api/chat/stream",
            {"messages": [], "workspace": "../.."},
        )
        self.assertEqual(status, 400)


class FullAccessPolicyTest(_ServerMixin, unittest.TestCase):
    def test_resolution_rules(self):
        self.assertTrue(server.resolve_full_access(True, "127.0.0.1", "127.0.0.1"))
        self.assertFalse(server.resolve_full_access(False, "127.0.0.1", "127.0.0.1"))
        # 对外监听：无论来源，一律强制关
        self.assertFalse(server.resolve_full_access(True, "127.0.0.1", "0.0.0.0"))
        # 非本机来源：即使监听在本机也不给
        self.assertFalse(server.resolve_full_access(True, "10.0.0.5", "127.0.0.1"))

    def _capture_chat_stream(self):
        captured = {}

        def fake_stream(messages, **kwargs):
            captured.update(kwargs)
            yield {"type": "text", "text": "ok"}

        return captured, mock.patch.object(engine, "chat_stream", fake_stream)

    def test_request_body_full_access_ignored_when_external(self):
        captured, patcher = self._capture_chat_stream()
        with patcher, mock.patch.object(config, "HOST", "0.0.0.0"), \
                mock.patch.object(config, "AUTH_TOKEN", "tok"):
            status, _, raw = self.request(
                "POST", "/api/chat/stream",
                {"messages": [], "full_access": True},
                headers={"Authorization": "Bearer tok"},
            )
            self.assertEqual(status, 200)
            self.assertIn(b"[DONE]", raw)

        self.assertIs(captured.get("full_access"), False)
        self.assertIs(captured.get("full_access_requested"), True)

    def test_local_request_keeps_full_access_switch(self):
        captured, patcher = self._capture_chat_stream()
        with patcher:
            status, _, _ = self.request(
                "POST", "/api/chat/stream",
                {"messages": [], "full_access": True},
            )
            self.assertEqual(status, 200)

        self.assertIs(captured.get("full_access"), True)


class EventsOwnershipTest(_ServerMixin, unittest.TestCase):
    def _create_conversation(self, title="t", workspace="evt"):
        status, _, data = self.json_request(
            "POST", "/api/conversations",
            {"title": title, "workspace": workspace},
        )
        self.assertEqual(status, 200, data)
        return data["id"]

    def _line_count(self):
        log = self.root / "data.jsonl"
        if not log.exists():
            return 0
        return len(log.read_text(encoding="utf-8").splitlines())

    def test_valid_event_for_existing_conversation_is_written(self):
        cid = self._create_conversation()
        before = self._line_count()
        status, _, data = self.json_request(
            "POST", "/api/events",
            {"events": [{"type": "message", "conversation_id": cid,
                         "role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(self._line_count(), before + 1)

    def test_delete_unknown_conversation_is_403_and_not_written(self):
        self._create_conversation()
        before = self._line_count()
        status, _, data = self.json_request(
            "POST", "/api/events",
            {"events": [{"type": "delete", "id": "不存在的id"}]},
        )
        self.assertEqual(status, 403)
        self.assertIn("不存在", data.get("error", ""))
        self.assertEqual(self._line_count(), before)

    def test_forged_session_is_403_and_not_written(self):
        before = self._line_count()
        status, _, _ = self.json_request(
            "POST", "/api/events",
            {"events": [{"type": "session", "id": "fake", "title": "伪造"}]},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self._line_count(), before)

    def test_batch_with_one_bad_event_is_rejected_whole(self):
        cid = self._create_conversation()
        before = self._line_count()
        status, _, _ = self.json_request(
            "POST", "/api/events",
            {"events": [
                {"type": "message", "conversation_id": cid,
                 "role": "user", "content": "ok"},
                {"type": "delete", "id": "ghost"},
            ]},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self._line_count(), before, "不能部分写入")

    def test_switch_and_rename_need_existing_id(self):
        before = self._line_count()
        for ev in (
            {"type": "switch", "id": "ghost"},
            {"type": "rename", "id": "ghost", "title": "x"},
            {"type": "message", "conversation_id": "ghost", "role": "user", "content": "x"},
        ):
            status, _, _ = self.json_request("POST", "/api/events", {"events": [ev]})
            self.assertEqual(status, 403, ev)
        self.assertEqual(self._line_count(), before)

    def test_workspace_event_cannot_smuggle_absolute_path(self):
        cid = self._create_conversation()
        before = self._line_count()
        status, _, _ = self.json_request(
            "POST", "/api/events",
            {"events": [{"type": "workspace", "conversation_id": cid,
                         "path": "C:\\Windows"}]},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self._line_count(), before)

    def test_created_conversation_replays_into_state(self):
        cid = self._create_conversation(title="回放", workspace="replay")
        self.json_request(
            "POST", "/api/events",
            {"events": [{"type": "message", "conversation_id": cid,
                         "role": "user", "content": "hello"}]},
        )
        status, _, data = self.json_request("GET", "/api/state")
        self.assertEqual(status, 200)
        conv = next(c for c in data["conversations"] if c["id"] == cid)
        self.assertEqual(conv["title"], "回放")
        self.assertEqual(conv["workspace"], "replay")
        self.assertEqual(conv["messages"][0]["content"], "hello")


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._trace_patch = mock.patch.object(
            tracing, "TRACE_FILE", Path(self.tmp.name) / "traces.jsonl"
        )
        self._trace_patch.start()
        toolbox.reset_fail_streak()
        toolbox.set_workspace(None)
        toolbox.set_full_access(False)
        toolbox.register(
            {"name": "_t_budget_noop", "description": "", "parameters": {}},
            lambda args: "ok",
        )
        self.calls = 0

    def tearDown(self):
        self._trace_patch.stop()
        self.tmp.cleanup()

    def _fake_model(self, usage_tokens=0):
        """每轮都要求调一次工具；可选地回一包 usage。"""
        def fake(messages, tools=None):
            self.calls += 1
            if usage_tokens:
                yield {"type": "usage", "usage": {"total_tokens": usage_tokens}}
            yield {
                "type": "tool_calls",
                "tool_calls": [{
                    "id": f"call_{self.calls}",
                    "name": "_t_budget_noop",
                    "arguments": "{}",
                }],
            }
        return fake

    def _run(self, **cfg):
        with mock.patch.object(engine.llm, "chat_stream", self._fake_model(**cfg)):
            return list(engine.chat_stream([{"role": "user", "content": "go"}]))

    def test_round_limit_emits_reason(self):
        with mock.patch.object(config, "MAX_TOOL_ROUNDS", 3):
            events = self._run()
        self.assertEqual(self.calls, 4, "第 4 轮应该先被拦下，不再执行工具")
        budget = [e for e in events if e.get("type") == "budget_exceeded"]
        self.assertEqual(len(budget), 1)
        self.assertIn("上限", budget[0]["reason"])
        # 前端不认新事件时也能看见说明
        self.assertTrue(any(e.get("type") == "text" and "任务提前结束" in e.get("text", "")
                            for e in events))

    def test_unset_round_limit_falls_back_to_nonzero_default(self):
        with mock.patch.object(config, "MAX_TOOL_ROUNDS", 0):
            events = self._run()
        self.assertLessEqual(self.calls, engine.DEFAULT_MAX_TOOL_ROUNDS + 1)
        self.assertTrue(any(e.get("type") == "budget_exceeded" for e in events))

    def test_token_limit_stops_before_next_model_call(self):
        with mock.patch.object(config, "MAX_TOOL_ROUNDS", 100), \
                mock.patch.object(config, "MAX_TASK_TOKENS", 150):
            with mock.patch.object(engine.llm, "chat_stream", self._fake_model(usage_tokens=100)), \
                    mock.patch.object(config, "MAX_TASK_SECONDS", 900):
                events = list(engine.chat_stream([{"role": "user", "content": "go"}]))
        self.assertEqual(self.calls, 2)
        budget = [e for e in events if e.get("type") == "budget_exceeded"]
        self.assertEqual(len(budget), 1)
        self.assertIn("token", budget[0]["reason"])

    def test_wall_clock_reason(self):
        self.assertIsNotNone(engine._budget_reason(0, 0, 100))
        self.assertIsNone(engine._budget_reason(engine.time.time() + 60, 0, 100))


class ConcurrencyTest(unittest.TestCase):
    def test_503_when_slots_are_full(self):
        srv = server.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), server.ChatHandler, max_workers=1
        )
        try:
            srv._slots.acquire()   # 占满唯一名额

            class FakeSocket:
                def __init__(self):
                    self.data = b""

                def sendall(self, data):
                    self.data += data

            sock = FakeSocket()
            with mock.patch.object(ThreadingHTTPServer, "process_request") as parent, \
                    mock.patch.object(srv, "shutdown_request"):
                srv.process_request(sock, ("1.2.3.4", 1234))

            parent.assert_not_called()
            self.assertIn(b"503 Service Unavailable", sock.data)
            self.assertIn(b"Retry-After", sock.data)
        finally:
            try:
                srv._slots.release()
            except ValueError:
                pass
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
