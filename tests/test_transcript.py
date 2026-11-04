"""
tests/test_transcript.py —— 转写结果的"形状"与自检

对应两件事：
  1. 服务返回的是 SRT，解析成时间轴不能错位；
  2. 转写结果明显短于视频时长时必须告警（今天那个"转出来是别的视频"的 bug，
     段数/耗时/RTF 全都正常，只有内容不对）。
"""

import unittest

from videokit import timeline, v2t


class SrtParseTest(unittest.TestCase):
    def test_parse_basic(self):
        srt = (
            "1\n00:00:01,500 --> 00:00:03,000\n第一句\n\n"
            "2\n00:01:02,250 --> 00:01:04,000\n第二句\n换了行\n"
        )
        segs = v2t.parse_srt(srt)
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0]["t0"], 1.5)
        self.assertAlmostEqual(segs[0]["t1"], 3.0)
        self.assertEqual(segs[0]["text"], "第一句")
        self.assertAlmostEqual(segs[1]["t0"], 62.25)
        self.assertEqual(segs[1]["text"], "第二句 换了行")

    def test_parse_empty(self):
        self.assertEqual(v2t.parse_srt(""), [])
        self.assertEqual(v2t.parse_srt("随便一段没有时间轴的文本"), [])

    def test_parse_missing_index_is_tolerated(self):
        srt = "00:00:02,000 --> 00:00:04,000\n直接给时间轴\n"
        segs = v2t.parse_srt(srt)
        self.assertEqual(len(segs), 1)
        self.assertAlmostEqual(segs[0]["t0"], 2.0)


class TimelineTest(unittest.TestCase):
    def _segs(self):
        return [
            timeline.Segment(0, 3, "speech", "第一句话。", "test"),
            timeline.Segment(3.2, 6, "speech", "第二句话。", "test"),
            timeline.Segment(60, 63, "speech", "隔了很久的一句。", "test"),
        ]

    def test_fmt_ts(self):
        self.assertEqual(timeline.fmt_ts(0), "00:00")
        self.assertEqual(timeline.fmt_ts(65), "01:05")
        # 有小时时是 H:MM:SS（小时不补零）——和正文里的写法保持一致
        self.assertEqual(timeline.fmt_ts(3661), "1:01:01")

    def test_merge_splits_on_long_gap(self):
        """时间隔得远的两句不该被并成一段——那是两个话题。"""
        paras = timeline.merge_into_paragraphs(self._segs())
        self.assertEqual(len(paras), 2)
        self.assertIn("第二句话", paras[0].text)
        self.assertIn("隔了很久", paras[1].text)

    def test_render_keeps_every_timestamp(self):
        paras = timeline.merge_into_paragraphs(self._segs())
        md = timeline.render_transcript(paras, {"title": "标题", "owner": "某UP", "duration": 63}, "测试")
        self.assertIn("[00:00]", md)
        self.assertIn("[01:00]", md)
        self.assertIn("测试", md)

    def test_apply_fixes_is_deterministic(self):
        """纠错表是不看上下文的粗暴替换——正因为粗暴，才要可预期。"""
        import config

        old = getattr(config, "ASR_FIXES", "")
        try:
            config.ASR_FIXES = "REG=RAG"
            self.assertEqual(timeline.apply_fixes("REG 是什么"), "RAG 是什么")
            self.assertEqual(timeline.apply_fixes("无关内容"), "无关内容")
        finally:
            config.ASR_FIXES = old


class CoverageCheckTest(unittest.TestCase):
    """转写覆盖度自检：判断逻辑在 tools/video_transcribe.coverage_warning 里。"""

    def setUp(self):
        from tools import video_transcribe

        self.fn = video_transcribe.coverage_warning

    def test_short_result_warns(self):
        """10 分钟视频只转了 66 秒 → 必须告警（真实事故的参数）。"""
        warn = self.fn(covered=66, expect=1021, vid="BV1C69iBgEMk")
        self.assertIn("⚠️", warn)
        self.assertIn("01:06", warn)
        self.assertIn("17:01", warn)       # 1021 秒
        self.assertIn("BV1C69iBgEMk", warn)

    def test_full_result_is_silent(self):
        self.assertEqual(self.fn(covered=1016, expect=1021, vid="BV1"), "")

    def test_unknown_duration_is_silent(self):
        """不知道总长时不该瞎告警（比如字幕路线拿不到时长）。"""
        self.assertEqual(self.fn(covered=10, expect=0, vid="BV1"), "")

    def test_boundary_is_90_percent(self):
        self.assertEqual(self.fn(covered=900, expect=1000, vid="BV1"), "")
        self.assertIn("⚠️", self.fn(covered=899, expect=1000, vid="BV1"))


if __name__ == "__main__":
    unittest.main()
