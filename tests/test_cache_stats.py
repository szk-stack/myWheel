"""
tests/test_cache_stats.py —— 用量采集与缓存命中率

三个口径必须钉死（算错了不会报错，只会一直给出一个错的数字）：
  ① 各家字段名不同：DeepSeek / OpenAI 风格 / Anthropic 风格都要认得
  ② 累加不能丢项：一个任务会调很多次模型，每次都要并进账本
  ③ 没报缓存的请求不能算成"未命中"——否则整体命中率被拉低成假的
"""

import json
import tempfile
import unittest
from pathlib import Path

import engine
import llm
import tracing


# DeepSeek 风格：命中/未命中成对给出
DEEPSEEK_USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 100,
    "total_tokens": 1100,
    "prompt_cache_hit_tokens": 800,
    "prompt_cache_miss_tokens": 200,
}

# OpenAI 风格：命中数藏在 prompt_tokens_details 里
OPENAI_USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 50,
    "total_tokens": 1050,
    "prompt_tokens_details": {"cached_tokens": 600},
}


class NormalizeUsageTest(unittest.TestCase):
    def test_deepseek_fields(self):
        u = tracing.normalize_usage(DEEPSEEK_USAGE)
        self.assertEqual(u["prompt_tokens"], 1000)
        self.assertEqual(u["cache_hit_tokens"], 800)
        self.assertEqual(u["cache_miss_tokens"], 200)
        self.assertEqual(u["cache_reported_requests"], 1)
        self.assertEqual(tracing.hit_rate(u), 80.0)

    def test_openai_style_fields(self):
        u = tracing.normalize_usage(OPENAI_USAGE)
        self.assertEqual(u["cache_hit_tokens"], 600)
        self.assertEqual(u["cache_miss_tokens"], 400)   # 由 prompt - hit 推出
        self.assertEqual(tracing.hit_rate(u), 60.0)

    def test_anthropic_style_fields(self):
        u = tracing.normalize_usage({
            "prompt_tokens": 500, "completion_tokens": 10,
            "cache_read_input_tokens": 300,
        })
        self.assertEqual(u["cache_hit_tokens"], 300)
        self.assertEqual(u["cache_miss_tokens"], 200)

    def test_no_cache_info_means_unknown_not_zero(self):
        """端点没报缓存字段 → 命中率是"未知"，不是 0%。"""
        u = tracing.normalize_usage({"prompt_tokens": 900, "completion_tokens": 20})
        self.assertEqual(u["prompt_tokens"], 900)
        self.assertEqual(u["cache_reported_requests"], 0)
        self.assertIsNone(tracing.hit_rate(u))

    def test_garbage_input(self):
        self.assertIsNone(tracing.normalize_usage(None))
        self.assertIsNone(tracing.normalize_usage({}))
        self.assertIsNone(tracing.normalize_usage("nope"))

    def test_hit_plus_miss_equals_prompt(self):
        u = tracing.normalize_usage(DEEPSEEK_USAGE)
        self.assertEqual(u["cache_hit_tokens"] + u["cache_miss_tokens"], u["prompt_tokens"])


class MergeUsageTest(unittest.TestCase):
    def test_accumulate_three_requests(self):
        acc = None
        for _ in range(3):
            acc = tracing.add_usage(acc, DEEPSEEK_USAGE)
        self.assertEqual(acc["requests"], 3)
        self.assertEqual(acc["prompt_tokens"], 3000)
        self.assertEqual(acc["cache_hit_tokens"], 2400)
        self.assertEqual(acc["completion_tokens"], 300)
        self.assertEqual(tracing.hit_rate(acc), 80.0)

    def test_merge_counts_unknown_cache_requests(self):
        """一次有缓存回报 + 一次没有 → 请求数算 2，但命中率只看有回报的那次。"""
        acc = tracing.add_usage(None, DEEPSEEK_USAGE)
        acc = tracing.add_usage(acc, {"prompt_tokens": 500, "completion_tokens": 5})
        self.assertEqual(acc["requests"], 2)
        self.assertEqual(acc["cache_reported_requests"], 1)
        self.assertEqual(tracing.hit_rate(acc), 80.0)


class UsageStatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_file = tracing.TRACE_FILE
        tracing.TRACE_FILE = Path(self.tmp.name) / "traces.jsonl"

    def tearDown(self):
        tracing.TRACE_FILE = self.old_file
        self.tmp.cleanup()

    def _write(self, *traces):
        with open(tracing.TRACE_FILE, "w", encoding="utf-8") as f:
            for t in traces:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")

    def _trace(self, trace_id, started, usage=None):
        t = tracing.new_trace("c1", None, "deepseek-flash")
        t["id"] = trace_id
        t["started_at"] = started
        t["status"] = "ok"
        t["rounds"] = [{"index": 0, "request": {
            "messages": [{"role": "user", "content": "测试用的问题"}]}}]
        if usage:
            t["usage"] = usage
        return t

    def test_empty_file(self):
        stats = tracing.usage_stats()
        self.assertEqual(stats["totals"]["requests"], 0)
        self.assertIsNone(stats["totals"]["hit_rate"])
        self.assertEqual(stats["series"], [])
        self.assertEqual(stats["recent"], [])

    def test_totals_and_daily_series(self):
        self._write(
            self._trace("t1", "2026-11-03T10:00:00", tracing.normalize_usage(DEEPSEEK_USAGE)),
            self._trace("t2", "2026-11-04T09:00:00", tracing.normalize_usage(OPENAI_USAGE)),
            self._trace("t3", "2026-11-04T11:00:00"),   # 老链路：没有 usage
        )
        stats = tracing.usage_stats()

        totals = stats["totals"]
        self.assertEqual(totals["requests"], 2)
        self.assertEqual(totals["prompt_tokens"], 2000)
        self.assertEqual(totals["cache_hit_tokens"], 1400)
        self.assertEqual(totals["cache_miss_tokens"], 600)
        self.assertEqual(totals["hit_rate"], 70.0)

        self.assertEqual([d["date"] for d in stats["series"]], ["2026-11-03", "2026-11-04"])
        self.assertEqual(stats["series"][0]["hit_rate"], 80.0)
        self.assertEqual(stats["series"][1]["hit_rate"], 60.0)

        # 最近任务：新的在前，且没有 usage 的链路不混进来
        self.assertEqual([r["id"] for r in stats["recent"]], ["t2", "t1"])

    def test_list_traces_carries_brief(self):
        self._write(self._trace("t1", "2026-11-04T10:00:00",
                                tracing.normalize_usage(DEEPSEEK_USAGE)))
        items = tracing.list_traces()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["usage"]["hit_rate"], 80.0)
        self.assertEqual(items[0]["usage"]["prompt_tokens"], 1000)

    def test_get_trace_decorates_hit_rate(self):
        """详情接口取出来时，链路和每一轮的 usage 都要带上算好的命中率。"""
        t = self._trace("t1", "2026-11-04T10:00:00", tracing.normalize_usage(DEEPSEEK_USAGE))
        t["rounds"][0]["usage"] = tracing.normalize_usage(DEEPSEEK_USAGE)
        self._write(t)

        got = tracing.get_trace("t1")
        self.assertEqual(got["usage"]["hit_rate"], 80.0)
        self.assertEqual(got["rounds"][0]["usage"]["hit_rate"], 80.0)

    def test_corrupted_lines_are_skipped(self):
        with open(tracing.TRACE_FILE, "w", encoding="utf-8") as f:
            f.write("{坏行\n")
            f.write(json.dumps(self._trace("t1", "2026-11-04T10:00:00",
                                           tracing.normalize_usage(DEEPSEEK_USAGE)),
                               ensure_ascii=False) + "\n")
        stats = tracing.usage_stats()
        self.assertEqual(stats["totals"]["requests"], 1)


class EngineRecordsUsageTest(unittest.TestCase):
    """引擎这一层：模型回了 usage，必须落到每一轮和整条链路上。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_file = tracing.TRACE_FILE
        self.old_stream = llm.chat_stream
        tracing.TRACE_FILE = Path(self.tmp.name) / "traces.jsonl"

    def tearDown(self):
        tracing.TRACE_FILE = self.old_file
        llm.chat_stream = self.old_stream
        self.tmp.cleanup()

    def test_usage_lands_on_round_and_trace(self):
        def fake_stream(messages, tools=None):
            yield {"type": "text", "text": "好了"}
            yield {"type": "usage", "usage": DEEPSEEK_USAGE}

        llm.chat_stream = fake_stream
        events = list(engine.chat_stream(
            [{"role": "user", "content": "你好"}], conversation_id="c1"))

        self.assertTrue(any(e.get("type") == "text" for e in events))

        trace = tracing._read_all()[0]
        self.assertEqual(trace["status"], "ok")
        self.assertEqual(trace["rounds"][0]["usage"]["cache_hit_tokens"], 800)
        self.assertEqual(trace["usage"]["requests"], 1)
        self.assertEqual(tracing.hit_rate(trace["usage"]), 80.0)

    def test_multi_round_usage_is_accumulated(self):
        state = {"n": 0}

        def fake_stream(messages, tools=None):
            state["n"] += 1
            if state["n"] == 1:
                # 第一轮：要求调一个不存在的工具 → 引擎会再问一轮
                yield {"type": "tool_calls", "tool_calls": [{
                    "id": "call_1", "name": "no_such_tool", "arguments": "{}"}]}
                yield {"type": "usage", "usage": DEEPSEEK_USAGE}
            else:
                yield {"type": "text", "text": "结束"}
                yield {"type": "usage", "usage": {
                    "prompt_tokens": 1500, "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 1400,
                    "prompt_cache_miss_tokens": 100}}

        llm.chat_stream = fake_stream
        list(engine.chat_stream([{"role": "user", "content": "你好"}], conversation_id="c1"))

        trace = tracing._read_all()[0]
        self.assertEqual(len(trace["rounds"]), 2)
        self.assertEqual(trace["usage"]["requests"], 2)
        self.assertEqual(trace["usage"]["prompt_tokens"], 2500)
        self.assertEqual(trace["usage"]["cache_hit_tokens"], 2200)
        self.assertEqual(tracing.hit_rate(trace["usage"]), 88.0)


if __name__ == "__main__":
    unittest.main()
