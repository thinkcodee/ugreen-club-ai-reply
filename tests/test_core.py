#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线单测：不联网、不发帖、不调模型。"""

import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ugreen_ai_reply as m  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402


class TextUtilsTest(unittest.TestCase):

    def test_clean_text(self):
        self.assertEqual(m.clean_text("  A&nbsp;<b>B</b>  &amp; C "), "A B & C")
        self.assertEqual(m.clean_text(None), "")

    def test_rss_date_converts_utc_to_local(self):
        raw = "Mon, 16 Nov 2026 14:51:58 +0000"
        expected = (datetime(2026, 11, 16, 14, 51, 58, tzinfo=timezone.utc)
                    .astimezone().replace(tzinfo=None))
        self.assertEqual(m.parse_rss_date(raw), expected)
        self.assertIsNone(m.parse_rss_date(""))
        self.assertIsNone(m.parse_rss_date("not a date"))

    def test_tid_from_url(self):
        self.assertEqual(m.tid_from_url("forum.php?mod=viewthread&tid=7342&extra=page%3D1"), "7342")
        self.assertEqual(m.tid_from_url("/thread-104-1-1.html"), "")

    def test_truncate_bytes(self):
        s = "中文" * 2000
        self.assertLessEqual(len(m.truncate_bytes(s, 100).encode("utf-8")), 100)


class AiParsingTest(unittest.TestCase):

    def test_fenced_json_and_tid_filter(self):
        payload = {"results": [
            {"tid": "7342", "should_reply": True, "reply": "具体建议", "reason": "具体问题"},
            {"tid": "99999", "should_reply": True, "reply": "不该出现", "reason": ""},
        ]}
        text = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        got = m.parse_ai_response(text, {"7342"})
        self.assertEqual(set(got), {"7342"})
        self.assertTrue(got["7342"]["should_reply"])
        self.assertEqual(got["7342"]["reply"], "具体建议")

    def test_single_object_response(self):
        row = {"tid": "7342", "should_reply": False, "reply": "", "reason": "纯签到帖"}
        got = m.parse_ai_response(json.dumps(row, ensure_ascii=False), {"7342"})
        self.assertFalse(got["7342"]["should_reply"])

    def test_invalid_json_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            m.parse_ai_response("这不是 JSON", {"7342"})

    def test_ai_payload_fields(self):
        t = m.Thread(tid="7342", title="求助", forum="用户互助", fid=13)
        t.content = "正文" * 500
        item = m.ai_payload([t])[0]
        self.assertEqual(item["tid"], "7342")
        self.assertLessEqual(len(item["content_preview"]), 800)


class QualityGateTest(unittest.TestCase):

    def test_good_reply(self):
        ok, text = m.sanitize_reply(
            "先看下容器的端口映射和应用日志，容器显示运行不代表服务已监听，"
            "通常是端口冲突或应用没起来。", [])
        self.assertTrue(ok)
        self.assertGreater(len(text), 8)

    def test_blocks_cliche(self):
        ok, why = m.sanitize_reply("感谢分享！这个教程太实用了", [])
        self.assertFalse(ok)
        self.assertIn("灌水", why)

    def test_blocks_link_and_short(self):
        self.assertFalse(m.sanitize_reply("详情见 https://example.com 的说明", [])[0])
        self.assertFalse(m.sanitize_reply("顶", [])[0])

    def test_blocks_ai_self_reference(self):
        self.assertFalse(m.sanitize_reply("作为AI语言模型，我建议检查网络设置。", [])[0])

    def test_blocks_duplicate(self):
        reply = "建议先查看容器日志，确认服务是否真的监听了端口，再看端口映射是否正确。"
        self.assertFalse(m.sanitize_reply(reply, [reply])[0])


class ReplyResponseTest(unittest.TestCase):

    def test_success_marker(self):
        body = ("<script>succeedhandle_fastpost('forum.php?mod=viewthread&tid=7342',"
                " '回复发布成功');</script>")
        ok, why = m.classify_reply_response(
            body, 200, m.DEFAULT_SUCCESS_MARKERS, m.DEFAULT_FAIL_MARKERS)
        self.assertTrue(ok)
        self.assertIn("成功", why)

    def test_failure_still_http_200(self):
        body = "<script>errorhandle_fastpost('抱歉，您目前无权进行此操作');</script>"
        ok, why = m.classify_reply_response(
            body, 200, m.DEFAULT_SUCCESS_MARKERS, m.DEFAULT_FAIL_MARKERS)
        self.assertFalse(ok)
        self.assertIn("失败", why)

    def test_unknown_response_is_not_success(self):
        ok, _ = m.classify_reply_response(
            "<html>空白页</html>", 200,
            m.DEFAULT_SUCCESS_MARKERS, m.DEFAULT_FAIL_MARKERS)
        self.assertFalse(ok)

    def test_http_error(self):
        ok, why = m.classify_reply_response(
            "", 500, m.DEFAULT_SUCCESS_MARKERS, m.DEFAULT_FAIL_MARKERS)
        self.assertFalse(ok)
        self.assertIn("500", why)


class FilterTest(unittest.TestCase):

    def setUp(self):
        self.cfg = m.Config()

    def test_blocked_tids(self):
        for tid in ("29", "30", "31", "104"):
            t = m.Thread(tid=tid, title="某个正常标题")
            self.assertIn("黑名单", m.is_blocked(t, self.cfg, set()))

    def test_blocked_title(self):
        t = m.Thread(tid="7500", title="绿联NAS社区隐私政策")
        self.assertIn("官方", m.is_blocked(t, self.cfg, set()))

    def test_already_replied(self):
        t = m.Thread(tid="7342", title="硬盘休眠问题求助")
        self.assertEqual(m.is_blocked(t, self.cfg, {"7342"}), "已回复过")

    def test_normal_thread_passes(self):
        t = m.Thread(tid="7342", title="硬盘休眠问题求助", fid=13)
        self.assertEqual(m.is_blocked(t, self.cfg, set()), "")

    def test_skip_events_flag(self):
        self.cfg.skip_events = True
        t = m.Thread(tid="7339", title="【学院体验】关于若干表述问题", fid=83)
        self.assertIn("社区活动", m.is_blocked(t, self.cfg, set()))

    def test_local_fallback_only_for_technical_posts(self):
        t = m.Thread(tid="1", title="怎么安装 wps office？Docker 部署")
        self.assertTrue(m.local_fallback_reply(t)[0])
        t2 = m.Thread(tid="2", title="签到打卡")
        self.assertFalse(m.local_fallback_reply(t2)[0])


class ListFallbackTest(unittest.TestCase):
    """验证兜底选择器不会把制度帖 / 页脚链接当成普通帖子。"""

    HTML = """
    <div id="layer_bbrules" style="display:none">
      <a href="/forum.php?mod=viewthread&tid=31">用户协议</a>
      <a href="/forum.php?mod=viewthread&tid=29">社区公约</a>
    </div>
    <table id="threadlisttableid">
      <tr><td><a href="forum.php?mod=viewthread&tid=7342">NAS app在Vivo手机上不能拉单独图标出来</a></td></tr>
      <tr><td><a href="forum.php?mod=viewthread&tid=7336">用 Home Assistant 接入小爱同学</a></td></tr>
    </table>
    <div class="foot">
      <a href="/thread-104-1-1.html">侵权投诉指引</a>
      <a href="/forum.php?mod=viewthread&tid=9999">页脚干扰链接</a>
    </div>
    """

    def test_only_threads_inside_container(self):
        cfg = m.Config()
        client = m.UgnasClient(cfg)
        soup = BeautifulSoup(self.HTML, m.HTML_PARSER)
        with patch.object(m.UgnasClient, "_soup", return_value=soup):
            got = client.fetch_list(10)
        self.assertEqual({t.tid for t in got}, {"7342", "7336"})
        self.assertTrue(all(t.forum == "功能讨论" for t in got))


class EmojiReplyTest(unittest.TestCase):
    """表情回复放行，但不能变成灌水口子。"""

    def test_pure_emoji_allowed(self):
        ok, reply = m.sanitize_reply("👍", [], kind="emoji")
        self.assertTrue(ok)
        self.assertEqual(reply, "👍")

    def test_emoji_with_short_phrase(self):
        self.assertTrue(m.sanitize_reply("这个思路很巧 👏", [], kind="emoji")[0])
        self.assertTrue(m.sanitize_reply("[赞]", [], kind="emoji")[0])

    def test_emoji_kind_without_actual_emoji_rejected(self):
        ok, why = m.sanitize_reply("写得不错", [], kind="emoji")
        self.assertFalse(ok)
        self.assertIn("没有表情", why)

    def test_plain_short_text_still_rejected(self):
        self.assertFalse(m.sanitize_reply("顶", [])[0])
        self.assertFalse(m.sanitize_reply("好", [], kind="text")[0])

    def test_cliche_still_blocked_for_emoji_kind(self):
        ok, why = m.sanitize_reply("感谢分享 👍", [], kind="emoji")
        self.assertFalse(ok)
        self.assertIn("灌水", why)

    def test_emoji_can_be_disabled(self):
        ok, _ = m.sanitize_reply("👍", [], kind="emoji", allow_emoji=False)
        self.assertFalse(ok)

    def test_no_four_identical_emoji_in_a_row(self):
        ok, why = m.sanitize_reply("👍", ["👍", "👍", "👍"], kind="emoji")
        self.assertFalse(ok)
        self.assertIn("连续重复", why)

    def test_ai_response_keeps_kind(self):
        row = {"tid": "7342", "should_reply": True, "kind": "emoji",
               "reply": "👍", "reason": "完整教程"}
        got = m.parse_ai_response(json.dumps({"results": [row]}), {"7342"})
        self.assertEqual(got["7342"]["kind"], "emoji")


class PerForumLatestTest(unittest.TestCase):

    @staticmethod
    def _t(fid: int, tid: int, minutes_ago: int) -> m.Thread:
        return m.Thread(tid=str(tid), title=f"帖子{tid}", fid=fid,
                        published=datetime.now() - timedelta(minutes=minutes_ago))

    def test_each_forum_gets_its_own_quota(self):
        # 板块 10 有 6 篇、板块 14 只有 2 篇
        pool = ([self._t(10, 100 + i, i * 10) for i in range(6)]
                + [self._t(14, 200 + i, i * 10) for i in range(2)])
        got = m.select_per_forum_latest(pool, 3)
        self.assertEqual(sum(1 for t in got if t.fid == 10), 3)
        self.assertEqual(sum(1 for t in got if t.fid == 14), 2)
        self.assertEqual(len(got), 5)

    def test_picks_the_newest_per_forum(self):
        pool = ([self._t(10, 100 + i, i * 10) for i in range(6)]
                + [self._t(14, 200 + i, i * 10) for i in range(2)])
        got = m.select_per_forum_latest(pool, 3)
        self.assertEqual({t.tid for t in got if t.fid == 10}, {"100", "101", "102"})
        self.assertEqual({t.tid for t in got if t.fid == 14}, {"200", "201"})

    def test_round_robin_interleaves_forums(self):
        pool = ([self._t(10, 100 + i, i * 10) for i in range(6)]
                + [self._t(14, 200 + i, i * 10) for i in range(2)])
        got = m.select_per_forum_latest(pool, 3)
        self.assertNotEqual(got[0].fid, got[1].fid)
        self.assertEqual([t.fid for t in got[:4]], [10, 14, 10, 14])

    def test_long_title_not_truncated_by_default(self):
        """日志里的标题默认完整显示，不再被硬切到 22 字。"""
        long_title = ("【自荐】给绿联NAS请了一个AI管家，从此动动嘴就能玩转NAS"
                      "，附完整配置说明")
        pool = [m.Thread(tid="1", title=long_title, fid=10,
                         published=datetime.now())]
        buf = io.StringIO()
        with redirect_stdout(buf):
            m.select_per_forum_latest(pool, 4)
        out = buf.getvalue()
        self.assertIn(long_title, out)
        self.assertNotIn("…", out)

    def test_title_truncation_marks_with_ellipsis(self):
        """显式限定长度时用省略号收尾，让人看得出是显示层截断。"""
        long_title = "标" * 40
        pool = [m.Thread(tid="1", title=long_title, fid=10,
                         published=datetime.now())]
        buf = io.StringIO()
        with redirect_stdout(buf):
            m.select_per_forum_latest(pool, 4, title_chars=20)
        out = buf.getvalue()
        self.assertIn("…", out)
        self.assertIn("标" * 19, out)


class AiConfigTest(unittest.TestCase):
    """环境变量：别名、厂商预设、baseURL 规范化、请求参数与脱敏。"""

    @staticmethod
    def _cfg(env=None, argv=None):
        args = m.build_parser().parse_args(argv or [])
        with patch.dict(os.environ, env or {}, clear=True):
            return m.Config.from_env(args)

    def test_canonical_variables(self):
        cfg = self._cfg({"AI_BASE_URL": "https://api.deepseek.com/v1",
                         "AI_API_KEY": "sk-test-1234567890",
                         "AI_MODEL": "deepseek-chat"})
        self.assertEqual(cfg.ai_base, "https://api.deepseek.com/v1")
        self.assertEqual(cfg.ai_key, "sk-test-1234567890")
        self.assertEqual(cfg.ai_model, "deepseek-chat")
        self.assertEqual(cfg.chat_url,
                         "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(cfg.ai_sources["model"], "AI_MODEL")

    def test_openai_standard_alias(self):
        cfg = self._cfg({"OPENAI_BASE_URL": "https://api.openai.com/v1",
                         "OPENAI_API_KEY": "sk-openai-abcdefgh",
                         "OPENAI_MODEL": "gpt-4o-mini"})
        self.assertEqual(cfg.ai_model, "gpt-4o-mini")
        self.assertEqual(cfg.ai_sources["apiKey"], "OPENAI_API_KEY")
        self.assertTrue(cfg.use_ai)

    def test_alias_priority(self):
        cfg = self._cfg({"AI_API_KEY": "primary", "SF_API_KEY": "secondary",
                         "API_KEY": "tertiary",
                         "AI_MODEL": "m1", "SF_MODEL": "m2"})
        self.assertEqual(cfg.ai_key, "primary")
        self.assertEqual(cfg.ai_model, "m1")

    def test_provider_preset(self):
        cfg = self._cfg({"AI_PROVIDER": "deepseek"})
        self.assertEqual(cfg.ai_base, "https://api.deepseek.com/v1")
        self.assertEqual(cfg.ai_model, "deepseek-chat")
        self.assertIn("AI_PROVIDER", cfg.ai_sources["baseURL"])

    def test_explicit_beats_preset(self):
        cfg = self._cfg({"AI_PROVIDER": "deepseek",
                         "AI_BASE_URL": "http://127.0.0.1:11434/v1",
                         "AI_MODEL": "qwen2.5:14b"})
        self.assertEqual(cfg.ai_base, "http://127.0.0.1:11434/v1")
        self.assertEqual(cfg.ai_model, "qwen2.5:14b")

    def test_base_url_normalization(self):
        cases = {
            "https://api.deepseek.com": "https://api.deepseek.com/v1",
            "https://api.deepseek.com/": "https://api.deepseek.com/v1",
            "https://api.deepseek.com/v1": "https://api.deepseek.com/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1":
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://api.deepseek.com/v1/chat/completions":
                "https://api.deepseek.com/v1",
        }
        for raw, expected in cases.items():
            self.assertEqual(m.normalize_ai_base(raw), expected, raw)
        with self.assertRaises(ValueError):
            m.normalize_ai_base("api.deepseek.com/v1")

    def test_endpoint_composed_and_overridden(self):
        cfg = self._cfg({"AI_BASE_URL": "https://x.example/v1",
                         "AI_CHAT_PATH": "v2/chat"})
        self.assertEqual(cfg.chat_url, "https://x.example/v1/v2/chat")
        cfg2 = self._cfg({"AI_ENDPOINT": "https://azure.example/openai/deployments/gpt4/chat"})
        self.assertIn("/chat", cfg2.chat_url)
        self.assertEqual(cfg2.ai_sources["endpoint"], "AI_ENDPOINT")

    def test_cli_overrides(self):
        cfg = self._cfg({"AI_MODEL": "from-env"},
                        ["--base-url", "https://cli.example",
                         "--model", "from-cli", "--api-key", "cli-key"])
        self.assertEqual(cfg.ai_model, "from-cli")
        self.assertEqual(cfg.ai_key, "cli-key")
        self.assertEqual(cfg.chat_url, "https://cli.example/v1/chat/completions")
        self.assertEqual(cfg.ai_sources["model"], "--model")

    def test_request_parameters(self):
        cfg = self._cfg({"AI_TEMPERATURE": "0.2", "AI_MAX_TOKENS": "4096",
                         "AI_TOP_P": "0.8", "AI_TIMEOUT": "60",
                         "AI_BATCH_SIZE": "2", "AI_RETRIES": "5",
                         "AI_JSON_MODE": "0"})
        self.assertEqual(cfg.ai_temperature, 0.2)
        self.assertEqual(cfg.ai_max_tokens, 4096)
        self.assertEqual(cfg.ai_top_p, 0.8)
        self.assertEqual(cfg.ai_timeout, 60)
        self.assertEqual(cfg.ai_batch, 2)
        self.assertEqual(cfg.ai_retries, 5)
        self.assertFalse(cfg.ai_json_mode)

    def test_extra_headers_and_body(self):
        cfg = self._cfg({"AI_EXTRA_HEADERS": '{"X-Foo":"bar","HTTP-Referer":"https://x"}',
                         "AI_EXTRA_BODY": '{"enable_thinking":false,"repetition_penalty":1.1}'})
        self.assertEqual(cfg.ai_headers["X-Foo"], "bar")
        self.assertFalse(cfg.ai_extra_body["enable_thinking"])
        cfg2 = self._cfg({"AI_EXTRA_HEADERS": "X-Foo: bar; X-Bar: baz"})
        self.assertEqual(cfg2.ai_headers["X-Bar"], "baz")
        with self.assertRaises(ValueError):
            self._cfg({"AI_EXTRA_BODY": "not-json"})

    def test_custom_auth_header(self):
        cfg = self._cfg({"AI_API_KEY": "azure-key-123456",
                         "AI_AUTH_HEADER": "api-key", "AI_AUTH_SCHEME": ""})
        h = m.ai_request_headers(cfg)
        self.assertEqual(h.get("api-key"), "azure-key-123456")
        self.assertNotIn("Authorization", h)

    def test_prompt_override(self):
        cfg = self._cfg({"AI_PROMPT_EXTRA": "所有回复都加上一句「仅供参考」"})
        self.assertIn("仅供参考", cfg.ai_system_prompt)
        self.assertTrue(cfg.ai_system_prompt.startswith("你是绿联"))
        cfg2 = self._cfg({"AI_SYSTEM_PROMPT": "完全自定义的提示词"})
        self.assertEqual(cfg2.ai_system_prompt, "完全自定义的提示词")

    def test_body_uses_all_config(self):
        cfg = self._cfg({"AI_TEMPERATURE": "0.1", "AI_MAX_TOKENS": "1024",
                         "AI_TOP_P": "0.5",
                         "AI_EXTRA_BODY": '{"enable_thinking":false}',
                         "AI_PROMPT_EXTRA": "用粤语回复"})
        t = m.Thread(tid="1", title="Docker 端口映射求助")
        body = m.ai_request_body([t], cfg)
        self.assertEqual(body["temperature"], 0.1)
        self.assertEqual(body["max_tokens"], 1024)
        self.assertEqual(body["top_p"], 0.5)
        self.assertFalse(body["enable_thinking"])
        self.assertIn("用粤语回复", body["messages"][0]["content"])

    def test_max_tokens_zero_means_unlimited(self):
        """AI_MAX_TOKENS=0 时请求体里干脆不带 max_tokens 字段。"""
        cfg = self._cfg({"AI_MAX_TOKENS": "0"})
        self.assertEqual(cfg.ai_max_tokens, 0)
        body = m.ai_request_body([m.Thread(tid="1", title="t")], cfg)
        self.assertNotIn("max_tokens", body)

    def test_max_tokens_default_is_128k(self):
        """默认给到 128K，避免思考过程把正文挤掉。"""
        cfg = self._cfg({})
        self.assertEqual(cfg.ai_max_tokens, 131072)
        self.assertEqual(cfg.ai_max_tokens_cap, 1048576)
        body = m.ai_request_body([m.Thread(tid="1", title="t")], cfg)
        self.assertEqual(body["max_tokens"], 131072)

    def test_thinking_default_enabled(self):
        """思考模式默认开启（显式带 enable_thinking=true）；AI_DISABLE_THINKING=1 才关。"""
        cfg = self._cfg({"AI_MODEL": "XingChenAGI/Xing4.0-29B"})
        self.assertFalse(cfg.ai_disable_thinking)
        body = m.ai_request_body([m.Thread(tid="1", title="t")], cfg)
        self.assertTrue(body["enable_thinking"])

        cfg2 = self._cfg({"AI_MODEL": "XingChenAGI/Xing4.0-29B",
                           "AI_DISABLE_THINKING": "1"})
        self.assertTrue(cfg2.ai_disable_thinking)
        body2 = m.ai_request_body([m.Thread(tid="1", title="t")], cfg2)
        self.assertFalse(body2["enable_thinking"])

        # AI_EXTRA_BODY 可强制覆盖任一字段
        cfg3 = self._cfg({"AI_EXTRA_BODY": '{"enable_thinking":false}'})
        body3 = m.ai_request_body([m.Thread(tid="1", title="t")], cfg3)
        self.assertFalse(body3["enable_thinking"])

    def test_enabled_flag_and_mask(self):
        cfg = self._cfg({"AI_ENABLED": "0", "AI_API_KEY": "sk-abcdefghijkl"})
        self.assertFalse(cfg.use_ai)
        self.assertEqual(m.mask_secret("sk-abcdefghijkl"), "sk-****ijkl")
        self.assertEqual(m.mask_secret(""), "(未配置)")

    def test_show_config_masks_key(self):
        secret = "sk-supersecret-abcdef1234567890"
        argv = ["ugreen_ai_reply.py", "--show-config"]
        buf = io.StringIO()
        with patch.dict(os.environ, {"AI_API_KEY": secret}, clear=True), \
                patch.object(sys, "argv", argv), redirect_stdout(buf):
            rc = m.main()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertNotIn(secret, out)
        self.assertIn("****", out)
        self.assertIn("AI_API_KEY", out)
        self.assertIn("chat/completions", out)

    def test_bad_base_url_reports_error(self):
        argv = ["ugreen_ai_reply.py", "--show-config"]
        buf = io.StringIO()
        with patch.dict(os.environ, {"AI_BASE_URL": "not-a-url"}, clear=True), \
                patch.object(sys, "argv", argv), redirect_stdout(buf):
            rc = m.main()
        self.assertEqual(rc, 1)
        self.assertIn("配置错误", buf.getvalue())

    def test_test_ai_uses_endpoint(self):
        class _R:
            status_code = 200
            text = ""

            def json(self):
                return {"choices": [{"message": {"content": "正常"}}]}

        argv = ["ugreen_ai_reply.py", "--test-ai"]
        buf = io.StringIO()
        with patch.dict(os.environ,
                        {"AI_API_KEY": "sk-test-1234567890",
                         "AI_BASE_URL": "http://127.0.0.1:9/v1"}, clear=True), \
                patch.object(m.requests, "post", return_value=_R()) as posted, \
                patch.object(sys, "argv", argv), redirect_stdout(buf):
            rc = m.main()
        self.assertEqual(rc, 0)
        self.assertEqual(posted.call_args.args[0],
                         "http://127.0.0.1:9/v1/chat/completions")
        self.assertIn("成功", buf.getvalue())


class AiCallTest(unittest.TestCase):
    """用假响应验证 模型调用 -> JSON 解析 -> 质检 的整条链路。"""

    class _Resp:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return {"choices": [{"message": {
                "content": json.dumps(self.payload, ensure_ascii=False)}}]}

    class _Resp400:
        status_code = 400
        text = '{"error": "response_format not supported"}'

    @classmethod
    def _batch(cls):
        return [m.Thread(tid="7342", title="Vivo 手机桌面快捷方式", forum="功能讨论"),
                m.Thread(tid="7323", title="ZFS 特性一览", forum="新手入门")]

    @classmethod
    def _payload(cls):
        return {"results": [
            {"tid": "7342", "should_reply": True, "kind": "text",
             "reply": "先在系统设置里确认是否给了创建桌面快捷方式的权限，"
                      "再看是不是被厂商桌面限制了。",
             "reason": "具体机型问题"},
            {"tid": "7323", "should_reply": True, "kind": "emoji",
             "reply": "👍", "reason": "教程完整，无可补充"},
        ]}

    def test_text_and_emoji_decisions_pass_quality_gate(self):
        cfg = m.Config()
        cfg.ai_key = "test-key"
        cfg.ai_base = "https://example.invalid/v1"
        with patch.object(m.requests, "post",
                          return_value=self._Resp(self._payload())):
            got = m.call_ai(self._batch(), cfg)
        self.assertEqual(set(got), {"7342", "7323"})
        self.assertEqual(got["7342"]["kind"], "text")
        self.assertTrue(m.sanitize_reply(got["7342"]["reply"], [])[0])
        self.assertEqual(got["7323"]["kind"], "emoji")
        self.assertTrue(m.sanitize_reply(got["7323"]["reply"], [],
                                         kind="emoji")[0])

    def test_unsupported_json_mode_falls_back(self):
        cfg = m.Config()
        cfg.ai_key = "test-key"
        cfg.ai_base = "https://example.invalid/v1"
        with patch.object(m.requests, "post",
                          side_effect=[self._Resp400(),
                                       self._Resp(self._payload())]) as posted:
            got = m.call_ai(self._batch(), cfg)
        self.assertFalse(cfg.ai_json_mode)
        self.assertNotIn("response_format", posted.call_args_list[1].kwargs["json"])
        self.assertEqual(set(got), {"7342", "7323"})


class MainFlowDryRunTest(unittest.TestCase):
    """完全离线的端到端演练：假 RSS + 假模型，验证整条 main 链路。"""

    class _Resp:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return {"choices": [{"message": {
                "content": json.dumps(self.payload, ensure_ascii=False)}}]}

    def test_ai_mode_end_to_end(self):
        def _thread(tid, title, fid, forum, minutes, content):
            return m.Thread(tid=tid, title=title, fid=fid, forum=forum,
                            content=content,
                            published=datetime.now() - timedelta(minutes=minutes))

        threads = [
            _thread("7101", "Docker 容器端口映射失败求助", 14, "Docker", 10,
                    "容器起来了但网页打不开"),
            _thread("7102", "ZFS 特性一览", 12, "新手入门", 20, "长篇教程" * 40),
            _thread("7103", "签到打卡", 83, "社区活动", 30, "签到"),
        ]

        def fake_post(url, **kwargs):
            body = kwargs.get("json") or {}
            text = body["messages"][1]["content"]
            tids = re.findall(r'"tid":\s*"(\d+)"', text)
            rows = []
            for i, tid in enumerate(tids):
                if tid == "7103":
                    rows.append({"tid": tid, "should_reply": False,
                                 "kind": "text", "reply": "", "reason": "纯签到"})
                elif i % 2:
                    rows.append({"tid": tid, "should_reply": True,
                                 "kind": "emoji", "reply": "👍",
                                 "reason": "教程完整"})
                else:
                    rows.append({"tid": tid, "should_reply": True,
                                 "kind": "text",
                                 "reply": "先确认容器端口是否真的监听，"
                                          "再看宿主机映射和防火墙规则是否放通。",
                                 "reason": "具体报错"})
            return self._Resp({"results": rows})

        env = {"AI_API_KEY": "test-key",
               "AI_BASE_URL": "http://127.0.0.1:9/v1",
               "UGREEN_DRY_RUN": "1", "UGREEN_NOTIFY": "0",
               "UGREEN_PER_FORUM_LIMIT": "1",
               "UGREEN_SEARCH": "0"}   # 端到端单测保持纯离线
        argv = ["ugreen_ai_reply.py", "--hours", "48", "--per-forum", "1",
                "--limit", "3", "--no-notify", "--no-mark"]
        buf = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
                patch.object(m.UgnasClient, "fetch_rss", return_value=threads), \
                patch.object(m.UgnasClient, "fetch_guide", return_value=threads), \
                patch.object(m.UgnasClient, "fetch_detail", side_effect=lambda t: t), \
                patch.object(m.requests, "post", side_effect=fake_post), \
                patch.object(sys, "argv", argv), \
                redirect_stdout(buf):
            rc = m.main()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("deepseek-ai/DeepSeek-V3", out)
        self.assertIn("回复 · 表情", out)
        self.assertIn("演练：未发送", out)
        self.assertIn("模型判定不值得回复", out)
        self.assertIn("均未发送", out)

    def test_share_post_gets_learn_reply_even_if_model_skips(self):
        """模型以软理由跳过分享帖时，脚本按规则补一条学习/点赞回复。"""

        def _thread(tid, title, fid, forum, minutes, content):
            return m.Thread(tid=tid, title=title, fid=fid, forum=forum,
                            content=content,
                            published=datetime.now() - timedelta(minutes=minutes))

        threads = [
            _thread("7201", "【自荐】给绿联NAS请了一个AI管家 WorkBuddy",
                    9, "玩法攻略", 10, "自荐内容"),
            _thread("7202", "【广告】某品牌NAS促销", 9, "玩法攻略", 20, "广告"),
        ]

        def fake_post(url, **kwargs):
            body = kwargs.get("json") or {}
            text = body["messages"][1]["content"]
            tids = re.findall(r'"tid":\s*"(\d+)"', text)
            rows = []
            for tid in tids:
                if tid == "7201":
                    # 软理由跳过 —— 应当被脚本补一条 learn 回复
                    rows.append({"tid": tid, "should_reply": False,
                                 "kind": "text", "reply": "",
                                 "reason": "发帖人已自行说明工具用途与用法"})
                else:
                    # 硬理由（广告）—— 应当尊重跳过
                    rows.append({"tid": tid, "should_reply": False,
                                 "kind": "text", "reply": "", "reason": "广告推广贴"})
            return self._Resp({"results": rows})

        env = {"AI_API_KEY": "test-key",
               "AI_BASE_URL": "http://127.0.0.1:9/v1",
               "UGREEN_DRY_RUN": "1", "UGREEN_NOTIFY": "0",
               "UGREEN_PER_FORUM_LIMIT": "2",
               "UGREEN_SEARCH": "0"}
        argv = ["ugreen_ai_reply.py", "--hours", "48", "--per-forum", "2",
                "--limit", "3", "--no-notify", "--no-mark"]
        buf = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
                patch.object(m.UgnasClient, "fetch_rss", return_value=threads), \
                patch.object(m.UgnasClient, "fetch_guide", return_value=threads), \
                patch.object(m.UgnasClient, "fetch_detail", side_effect=lambda t: t), \
                patch.object(m.requests, "post", side_effect=fake_post), \
                patch.object(sys, "argv", argv), \
                redirect_stdout(buf):
            rc = m.main()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        # 分享帖补了一条 learn 回复
        self.assertIn("学习点赞", out)
        self.assertIn("WorkBuddy", out)
        # 广告帖仍然被跳过
        self.assertIn("广告推广贴", out)


# ===========================================================================
# A：已有楼层解析
# ===========================================================================

class ReplyFloorTest(unittest.TestCase):

    HTML = """
    <div id="post_58820">
      <div class="pi"><strong><a id="postnum58820">楼主</a></strong>
        <div class="authi"><a href="home.php?mod=space&amp;uid=1">张三</a></div>
        <em id="authorposton58820">发表于 <span title="2026-11-16 20:00:00">昨天 20:00</span></em>
      </div>
      <td class="t_f" id="postmessage_58820">马上注册，结交更多好友。立即注册 × 首楼：硬盘无法休眠，求助。</td>
    </div>
    <div id="post_rate_div_58820"><span>评分层，不该被当成楼层</span></div>
    <div id="post_58826">
      <div class="pi"><strong><a id="postnum58826">沙发</a></strong>
        <div class="authi"><a href="home.php?mod=space&amp;uid=2">李四</a></div>
        <em id="authorposton58826">发表于 <span title="2026-11-17 00:09:05">10 小时前</span></em>
      </div>
      <td class="t_f" id="postmessage_58826">试试把 Docker 虚拟机和下载任务都关掉再看，多半是后台有读写。</td>
    </div>
    <div id="post_58835">
      <div class="pi"><strong><a id="postnum58835">板凳</a></strong>
        <div class="authi"><a href="home.php?mod=space&amp;uid=1">张三</a></div>
        <em id="authorposton58835">发表于 <span title="2026-11-17 08:00:00">2 小时前</span></em>
      </div>
      <td class="t_f" id="postmessage_58835">已解决，按你说的关掉后台任务就好了，多谢！</td>
    </div>
    """

    def setUp(self):
        self.cfg = m.Config()
        self.t = m.Thread(tid="7342", title="硬盘无法休眠求助", author="张三")
        self.soup = BeautifulSoup(self.HTML, m.HTML_PARSER)

    def test_excludes_first_floor_and_rate_div(self):
        got = m.parse_reply_floors(self.soup, self.t, self.cfg)
        self.assertEqual(len(got), 2)
        self.assertEqual([r.floor for r in got], ["沙发", "板凳"])
        self.assertEqual(got[0].author, "李四")
        self.assertIn("Docker", got[0].text)

    def test_op_solved_detection(self):
        got = m.parse_reply_floors(self.soup, self.t, self.cfg)
        self.assertFalse(got[0].is_op)
        self.assertTrue(got[1].is_op)
        self.assertTrue(self.t.likely_solved)

    def test_precise_time_from_span_title(self):
        got = m.parse_reply_floors(self.soup, self.t, self.cfg)
        self.assertEqual(got[0].when, "2026-11-17 00:09:05")

    def test_noise_removed_and_length_capped(self):
        self.cfg.floor_chars = 20
        got = m.parse_reply_floors(self.soup, self.t, self.cfg)
        self.assertLessEqual(len(got[0].text), 22)
        cleaned = m.REPLY_NOISE_RE.sub(
            "", "马上注册，结交更多好友。立即注册 × 真正正文")
        self.assertNotIn("马上注册", cleaned)
        self.assertIn("真正正文", cleaned)

    # 绿联实际用的是 comiis 皮肤：用户名在 a.km_name（display:none），
    # 而 div.authi 里第一个 a[href*=mod=space] 是没有文本的头像链接。
    COMIIS_HTML = """
    <div id="post_58820">
      <div class="pi">
        <div class="authi">
          <a href="home.php?mod=space&amp;uid=33029" class="xi2 z avt km_avt"
             style="display:none"><img src="x.jpg"></a>
          <a href="home.php?mod=space&amp;uid=33029" class="km_name"
             style="display:none">IK4JnC</a>
          <a href="home.php?mod=spacecp&amp;ac=usergroup&amp;gid=10"
             class="km_lvs" style="display:none">Lv.1</a>
        </div>
        <em id="authorposton58820" class="km_time">发表于
          <span title="2026-11-16 22:51:58">昨天&nbsp;22:51</span></em>
      </div>
      <td class="t_f" id="postmessage_58820">首楼正文：NAS app 不能拉单独图标。</td>
    </div>
    <div id="post_58835">
      <div class="pi">
        <div class="authi">
          <a href="home.php?mod=space&amp;uid=33029" class="xi2 z avt km_avt"
             style="display:none"><img src="y.jpg"></a>
          <a href="home.php?mod=space&amp;uid=33029" class="km_name"
             style="display:none">IK4JnC</a>
        </div>
        <em id="authorposton58835" class="km_time">发表于
          <span title="2026-11-17 08:00:00">2 小时前</span></em>
      </div>
      <td class="t_f" id="postmessage_58835">已解决，重启手机就好了，多谢！</td>
    </div>
    """

    def test_comiis_skin_author_not_avatar(self):
        """头像链接没有文本，不能把用户名取空。"""
        soup = BeautifulSoup(self.COMIIS_HTML, m.HTML_PARSER)
        t = m.Thread(tid="7342", title="NAS app 不能拉图标")
        got = m.parse_reply_floors(soup, t, self.cfg)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].author, "IK4JnC")
        # 首楼作者即楼主，回填后 is_op / 已解决判断才能生效
        self.assertEqual(t.author, "IK4JnC")
        self.assertTrue(got[0].is_op)
        self.assertTrue(t.likely_solved)

    def test_disabled_and_max_floors(self):
        self.cfg.fetch_replies = False
        self.assertEqual(m.parse_reply_floors(self.soup, self.t, self.cfg), [])
        self.cfg.fetch_replies = True
        self.cfg.max_floors = 1
        self.assertEqual(len(m.parse_reply_floors(self.soup, self.t, self.cfg)), 1)


# ===========================================================================
# C：检索层
# ===========================================================================

class SearchUtilsTest(unittest.TestCase):

    def test_extract_keywords(self):
        t = m.Thread(tid="1", title="Docker 容器端口映射失败",
                     content="用 qBittorrent 的时候 8080 端口冲突")
        kws = m.extract_keywords(t)
        self.assertIn("Docker", kws)
        self.assertIn("端口映射", kws)
        self.assertLessEqual(len(kws), 6)

    def test_build_queries(self):
        t = m.Thread(tid="1", title="【求助】硬盘休眠一直不生效")
        qs = m.build_search_queries(t, ["硬盘休眠", "硬盘"])
        self.assertEqual(len(qs), 2)
        self.assertEqual(qs[0], "硬盘休眠 硬盘")
        self.assertNotIn("【", qs[1])

    def test_parse_providers_filters_unknown(self):
        # local 是已移除的本地知识库源，现在跟 bogus 一样被过滤
        self.assertEqual(m.parse_search_providers("ugnas-kb, local, bogus, tavily"),
                         ("ugnas-kb", "tavily"))
        self.assertEqual(m.parse_search_providers("none"), ())
        self.assertEqual(m.parse_search_providers("a,a,b"), ())

    def test_stable_ref_is_reproducible(self):
        self.assertEqual(m.stable_ref("web", "https://x.example/a"),
                         m.stable_ref("web", "https://x.example/a"))
        self.assertTrue(m.stable_ref("web", "x").startswith("web:"))

    def test_dig_json_nested(self):
        obj = {"data": {"items": [{"title": "T", "content": "C"}]}}
        self.assertEqual(m.dig_json(obj, "data.items.0.title"), "T")
        self.assertIsNone(m.dig_json(obj, "data.missing.key"))


class KnowledgeBaseTest(unittest.TestCase):

    class _R:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    PAYLOAD = {"code": 200, "message": "SUCCESS", "data": {"pageTotal": 1, "data": [
        {"id": 1, "articleInfoId": 437,
         "title": "如何开启<font color='#0055FF'>硬盘休眠</font>功能",
         "aiSummary": "<p>适用固件 1.17.0.0031 及以上。</p>",
         "contentText": "简介 <font color='#0055FF'>硬盘休眠</font>需要无读写任务。"},
        {"id": 2, "videoId": 10, "title": "Docker 视频教程", "url": "https://x/a.mp4"},
    ]}}

    def test_kb_text_removes_highlight_without_splitting(self):
        self.assertEqual(m.kb_text("开启<font color='#0055FF'>硬盘休眠</font>功能"),
                         "开启硬盘休眠功能")
        self.assertIn("适用固件", m.kb_text("<p>适用固件 1.17.0.0031 及以上。</p>"))

    def test_search_filters_video_and_keeps_ref(self):
        cfg = m.Config()
        cfg.kb_fetch_detail = False
        with patch.object(m.requests, "get", return_value=self._R(self.PAYLOAD)) as g:
            evs = m.search_ugnas_kb("硬盘休眠", cfg)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].ref, "kb:437")
        self.assertEqual(evs[0].title, "如何开启硬盘休眠功能")
        self.assertEqual(evs[0].source, "绿联官方知识库")
        self.assertEqual(g.call_args.kwargs["params"]["keywords"], "硬盘休眠")
        self.assertEqual(g.call_args.kwargs["params"]["language"], "zh-CN")

    def test_article_detail_is_appended(self):
        cfg = m.Config()
        cfg.kb_fetch_detail = True
        cfg.kb_detail_limit = 1

        def fake_get(url, **kwargs):
            if "article/info" in url:
                return self._R({"code": 200, "data": {
                    "content": "<p>完整正文：SSD 硬盘不支持休眠。</p>"}})
            return self._R(self.PAYLOAD)

        with patch.object(m.requests, "get", side_effect=fake_get):
            evs = m.search_ugnas_kb("硬盘休眠", cfg)
        self.assertIn("SSD 硬盘不支持休眠", evs[0].snippet)

    def test_non_200_returns_empty(self):
        cfg = m.Config()
        cfg.kb_fetch_detail = False
        with patch.object(m.requests, "get",
                          return_value=self._R({"code": 500, "message": "err"})):
            self.assertEqual(m.search_ugnas_kb("x", cfg), [])

    def test_web_providers_parse(self):
        cfg = m.Config()
        cfg.tavily_key = "k"
        cfg.custom_search_url = "http://search.example/?q={query}"
        tavily = {"results": [{"title": "T", "content": "C", "url": "https://a"}]}
        custom = {"results": [{"title": "T2", "content": "C2", "url": "https://b"}]}
        with patch.object(m.requests, "post", return_value=self._R(tavily)):
            evs = m.search_tavily("docker", cfg)
        self.assertEqual(evs[0].source, "Tavily")
        self.assertEqual(evs[0].snippet, "C")
        with patch.object(m.requests, "get", return_value=self._R(custom)):
            evs = m.search_custom("docker", cfg)
        self.assertEqual(evs[0].source, "自定义检索")
        self.assertEqual(evs[0].title, "T2")


class RetrieverTest(unittest.TestCase):

    @staticmethod
    def _cfg():
        cfg = m.Config()
        cfg.search_delay = 0
        cfg.search_providers = ("ugnas-kb",)
        cfg.search_max_evidence = 2
        cfg.search_snippet_chars = 100
        cfg.search_total_chars = 150
        cfg.search_min_relevance = 0.0   # 假数据标题极短，单独测相关性门槛
        return cfg

    @staticmethod
    def _evidence():
        return [
            m.Evidence(ref="kb:1", title="A", snippet="证据内容" * 100,
                       source="绿联官方知识库", score=9.0),
            m.Evidence(ref="kb:1", title="A-重复", snippet="重复项", source="KB",
                       score=1.0),
            m.Evidence(ref="kb:2", title="B", snippet="第二条" * 100,
                       source="绿联官方知识库", score=8.0),
        ]

    def test_cache_dedup_and_budget(self):
        cfg = self._cfg()
        t = m.Thread(tid="1", title="硬盘休眠")
        with patch.object(m, "search_ugnas_kb", return_value=self._evidence()) as fn:
            r = m.Retriever(cfg)
            got = r.collect(t, ["硬盘休眠", "硬盘"])
        self.assertEqual(fn.call_count, 2)          # 两条查询各一次
        self.assertEqual(len(got), 2)
        self.assertEqual({e.ref for e in got}, {"kb:1", "kb:2"})
        self.assertLessEqual(sum(len(e.snippet) for e in got), 150)
        self.assertLessEqual(len(got[0].snippet), 100)
        # 第二次完全相同 -> 命中缓存，不再发请求
        with patch.object(m, "search_ugnas_kb") as fn2:
            r.collect(t, ["硬盘休眠", "硬盘"])
        self.assertEqual(fn2.call_count, 0)

    def test_provider_failure_degrades(self):
        cfg = self._cfg()
        with patch.object(m, "search_ugnas_kb", side_effect=RuntimeError("boom")):
            r = m.Retriever(cfg)
            got = r.collect(m.Thread(tid="1", title="x"), ["q"])
        self.assertEqual(got, [])
        self.assertEqual(len(r.errors), 1)

    def test_relevance_filters_off_topic_evidence(self):
        t = m.Thread(tid="1", title="硬盘休眠一直不生效怎么办")
        kws = ["硬盘休眠", "硬盘"]
        on_topic = m.Evidence(ref="kb:1", title="为什么绿联NAS硬盘无法休眠")
        off_topic = m.Evidence(ref="kb:2", title="如何使用相册的AI智能分类")
        self.assertGreater(m.evidence_relevance(on_topic, t, kws), 0.34)
        self.assertLess(m.evidence_relevance(off_topic, t, kws), 0.34)
        # 端到端：开启门槛后跑题证据应被丢掉
        cfg = self._cfg()
        cfg.search_min_relevance = 0.34
        t.keywords = kws
        evs = [on_topic, off_topic]
        with patch.object(m, "search_ugnas_kb", return_value=evs):
            got = m.Retriever(cfg).collect(t, ["硬盘休眠"])
        self.assertEqual({e.ref for e in got}, {"kb:1"})
        self.assertIn("低相关丢弃", t.search_note)

    def test_local_provider_removed(self):
        """本地知识库检索源已移除 —— 配置里写了 local 会被拒绝。"""
        self.assertNotIn("local", m.KNOWN_SEARCH_PROVIDERS)
        self.assertFalse(hasattr(m, "search_local_kb"))
        # 解析时 local 被忽略，只剩合法的源
        got = m.parse_search_providers("ugnas-kb,local,tavily")
        self.assertEqual(got, ("ugnas-kb", "tavily"))
        self.assertNotIn("local", got)

    def test_known_providers_all_have_search_functions(self):
        """每个合法源都要有对应的检索函数，否则运行时会 KeyError。"""
        for provider, fname in m.SEARCH_FUNCS.items():
            self.assertTrue(hasattr(m, fname),
                            f"检索源 {provider} 对应的函数 {fname} 不存在")
        # 白名单与函数表必须一一对应，不能多也不能少
        self.assertEqual(set(m.KNOWN_SEARCH_PROVIDERS),
                         set(m.SEARCH_FUNCS.keys()),
                         "KNOWN_SEARCH_PROVIDERS 与 SEARCH_FUNCS 不一致")


# ===========================================================================
# B：保守模式
# ===========================================================================

class ConservativeTest(unittest.TestCase):

    def setUp(self):
        self.cfg = m.Config()
        self.t = m.Thread(tid="1", title="硬盘休眠求助")

    @staticmethod
    def _d(**kw):
        base = {"kind": "text", "confidence": "low", "answer_mode": "probe",
                "evidence_used": []}
        base.update(kw)
        return base

    def test_version_claim_without_evidence_blocked(self):
        ok, why = m.validate_claim("把固件升到 1.17.0.0031 就能休眠了",
                                   self._d(), self.t, self.cfg)
        self.assertFalse(ok)
        self.assertIn("版本号", why)

    def test_official_claim_without_evidence_blocked(self):
        ok, why = m.validate_claim("据官方文档说明，这个功能肯定支持所有机型",
                                   self._d(), self.t, self.cfg)
        self.assertFalse(ok)
        self.assertIn("确定性", why)

    def test_direct_mode_without_evidence_blocked(self):
        ok, why = m.validate_claim("可以直接在控制面板里开启这个选项",
                                   self._d(answer_mode="direct", confidence="high"),
                                   self.t, self.cfg)
        self.assertFalse(ok)
        self.assertIn("没有检索证据", why)

    def test_bogus_reference_blocked(self):
        ok, why = m.validate_claim("官方文档里提到过这一点",
                                   self._d(evidence_used=["kb:999"]),
                                   self.t, self.cfg)
        self.assertFalse(ok)
        self.assertIn("不存在的证据编号", why)

    def test_high_confidence_with_real_evidence_passes(self):
        self.t.evidence = [m.Evidence(ref="kb:437", title="硬盘休眠",
                                      snippet="SSD 不支持休眠", source="绿联官方知识库")]
        d = self._d(answer_mode="direct", confidence="high",
                    evidence_used=["kb:437"])
        ok, _ = m.validate_claim(
            "官方文档里提到 SSD 硬盘不支持休眠，可以先确认一下盘的类型。",
            d, self.t, self.cfg)
        self.assertTrue(ok)

    def test_probe_without_claim_passes(self):
        ok, _ = m.validate_claim(
            "可以先看下是不是后台有任务在读写，再逐项关掉排查，这个我不太确定。",
            self._d(), self.t, self.cfg)
        self.assertTrue(ok)

    def test_emoji_must_not_carry_claims(self):
        ok, why = m.validate_claim("官方已支持 👍",
                                   self._d(kind="emoji", answer_mode="emoji"),
                                   self.t, self.cfg)
        self.assertFalse(ok)
        self.assertIn("表情回复", why)

    def test_can_be_turned_off(self):
        self.cfg.conservative = False
        ok, _ = m.validate_claim("固件 1.17.0.0031 肯定支持", self._d(),
                                 self.t, self.cfg)
        self.assertTrue(ok)

    def test_response_new_fields_and_direct_downgrade(self):
        row = {"tid": "1", "should_reply": True, "kind": "text",
               "confidence": "low", "answer_mode": "direct",
               "evidence_used": "kb:437", "reply": "x", "reason": "y"}
        got = m.parse_ai_response(json.dumps({"results": [row]}), {"1"})
        self.assertEqual(got["1"]["answer_mode"], "probe")
        self.assertEqual(got["1"]["evidence_used"], ["kb:437"])
        self.assertEqual(got["1"]["confidence"], "low")

    def test_payload_includes_floors_and_evidence(self):
        self.t.replies = [m.Reply(floor="沙发", author="李四", text="试试关掉 Docker")]
        self.t.evidence = [m.Evidence(ref="kb:437", title="硬盘休眠",
                                      snippet="无读写才休眠", source="绿联官方知识库")]
        item = m.ai_payload([self.t])[0]
        self.assertEqual(len(item["existing_replies"]), 1)
        self.assertEqual(item["evidence"][0]["ref"], "kb:437")
        self.assertIn("likely_solved", item)
        self.assertIn("keywords", item)

    def test_prompt_states_fact_discipline(self):
        for kw in ("evidence", "confidence", "answer_mode", "绝不编造", "evidence_used"):
            self.assertIn(kw, m.SYSTEM_PROMPT)


class AbcConfigTest(unittest.TestCase):

    @staticmethod
    def _cfg(env=None, argv=None):
        args = m.build_parser().parse_args(argv or [])
        with patch.dict(os.environ, env or {}, clear=True):
            return m.Config.from_env(args)

    def test_defaults_enable_kb_and_conservative(self):
        cfg = self._cfg()
        # 默认带上知识库 + 两个联网源；未配 Key 时联网源直接返回空、不发请求
        self.assertEqual(cfg.search_providers, ("ugnas-kb", "tavily", "baidu"))
        self.assertTrue(cfg.search_ready)
        self.assertTrue(cfg.conservative)
        self.assertTrue(cfg.fetch_replies)
        self.assertTrue(cfg.skip_solved)
        self.assertEqual(cfg.search_language, "zh-CN")

    def test_provider_list_and_unknown_filtered(self):
        # local 已移除，配置里写它会被当作未知源过滤掉
        cfg = self._cfg({"UGREEN_SEARCH_PROVIDERS": "ugnas-kb,local,tavily,bogus"})
        self.assertEqual(cfg.search_providers, ("ugnas-kb", "tavily"))

    def test_disable_switches(self):
        self.assertFalse(self._cfg({"UGREEN_SEARCH": "0"}).search_ready)
        self.assertFalse(self._cfg({"UGREEN_SEARCH_PROVIDERS": "none"}).search_ready)
        cfg = self._cfg({}, ["--no-search", "--no-conservative", "--no-replies"])
        self.assertFalse(cfg.search_ready)
        self.assertFalse(cfg.conservative)
        self.assertFalse(cfg.fetch_replies)

    def test_quotas_and_credentials(self):
        cfg = self._cfg({"UGREEN_SEARCH_MAX_QUERIES": "3",
                         "UGREEN_SEARCH_MAX_EVIDENCE": "6",
                         "UGREEN_SEARCH_LANGUAGE": "en-US",
                         "TAVILY_API_KEY": "tv-123",
                         "UGREEN_KB_DETAIL": "0"})
        self.assertEqual(cfg.search_max_queries, 3)
        self.assertEqual(cfg.search_max_evidence, 6)
        self.assertEqual(cfg.search_language, "en-US")
        self.assertEqual(cfg.tavily_key, "tv-123")
        self.assertFalse(cfg.kb_fetch_detail)

    def test_show_config_has_abc_sections(self):
        buf = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(sys, "argv", ["ugreen_ai_reply.py", "--show-config"]), \
                redirect_stdout(buf):
            self.assertEqual(m.main(), 0)
        out = buf.getvalue()
        for kw in ("上下文（A）", "保守模式", "检索（C）", "绿联知识库", "ugnas-kb"):
            self.assertIn(kw, out)

    def test_test_search_is_offline(self):
        ev = [m.Evidence(ref="kb:437", title="硬盘休眠", snippet="无读写才休眠",
                         source="绿联官方知识库", score=9.0)]
        env = {"UGREEN_SEARCH_DELAY": "0", "UGREEN_KB_DETAIL": "0"}
        buf = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
                patch.object(m, "search_ugnas_kb", return_value=ev), \
                patch.object(m.UgnasClient, "fetch_rss",
                             side_effect=AssertionError("不该访问社区")), \
                patch.object(sys, "argv",
                             ["ugreen_ai_reply.py", "--test-search", "硬盘休眠"]), \
                redirect_stdout(buf):
            rc = m.main()
        self.assertEqual(rc, 0)
        self.assertIn("kb:437", buf.getvalue())
        self.assertIn("检索结果：1 条证据", buf.getvalue())


class ConservativeFlowTest(unittest.TestCase):
    """端到端：无证据却自称 direct 的回复必须在演练里被拦下。"""

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({"results": [{
                "tid": "8001", "should_reply": True, "kind": "text",
                "confidence": "high", "answer_mode": "direct",
                "evidence_used": ["kb:999"],
                "reply": "官方已经支持，把固件升到 1.17.0.0031 就能解决。",
                "reason": "有把握"}]}, ensure_ascii=False)}}]}

    def test_unsafe_reply_never_reaches_send(self):
        t = m.Thread(tid="8001", title="硬盘休眠求助", fid=13, forum="用户互助",
                     content="硬盘一直不休眠",
                     published=datetime.now() - timedelta(minutes=10))
        env = {"AI_API_KEY": "test-key",
               "AI_BASE_URL": "http://127.0.0.1:9/v1",
               "UGREEN_DRY_RUN": "1", "UGREEN_NOTIFY": "0",
               "UGREEN_SEARCH": "0"}
        argv = ["ugreen_ai_reply.py", "--hours", "48", "--per-forum", "1",
                "--limit", "1", "--no-notify", "--no-mark"]
        buf = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
                patch.object(m.UgnasClient, "fetch_rss", return_value=[t]), \
                patch.object(m.UgnasClient, "fetch_guide", return_value=[t]), \
                patch.object(m.UgnasClient, "fetch_detail", side_effect=lambda x: x), \
                patch.object(m.requests, "post", return_value=self._Resp()), \
                patch.object(sys, "argv", argv), redirect_stdout(buf):
            rc = m.main()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("保守模式拦截", out)
        self.assertNotIn("演练：未发送", out)


class RevisitTest(unittest.TestCase):
    """需求 1：回复过的帖子默认不再回，除非被点名或出现了更优解法。"""

    @staticmethod
    def _state():
        now = datetime.now()
        return {"replied": {
            "7001": {"at": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
                     "forum": "用户互助", "title": "硬盘不休眠", "reply": "上次那句"},
            "7002": {"at": (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
                     "forum": "用户互助", "title": "远程连不上", "reply": "老回复"},
            "7003": {"at": (now - timedelta(hours=50)).strftime("%Y-%m-%d %H:%M:%S"),
                     "forum": "用户互助", "title": "很久以前", "reply": "过期"},
        }}

    def test_pick_revisit_candidates_respects_window_and_limit(self):
        cfg = m.Config()
        cfg.recheck_limit = 2
        cfg.revisit_hours = 48
        now = datetime.now()
        got = m.pick_revisit_candidates(self._state(), cfg, now)
        # 50 小时前那篇超出回溯窗口，不挑；最近的两篇按新到旧取
        self.assertEqual([t.tid for t in got], ["7001", "7002"])
        self.assertEqual(got[0].my_previous_reply, "上次那句")
        self.assertIn("tid=7001", got[0].url)

    def test_pick_revisit_empty_when_disabled(self):
        cfg = m.Config()
        cfg.recheck_limit = 0
        self.assertEqual(m.pick_revisit_candidates(self._state(), cfg,
                                                    datetime.now()), [])

    def test_trigger_on_mention(self):
        cfg = m.Config()
        cfg.my_username = "naser"
        t = m.Thread(tid="7001", title="x")
        t.replies = [m.Reply(floor="2楼", author="路人", when="",
                             text="@naser 你上次的方案我试了还是不行")]
        reason = m.detect_revisit_trigger(t, cfg)
        self.assertIn("点名", reason)

    def test_trigger_on_better_solution(self):
        cfg = m.Config()
        t = m.Thread(tid="7001", title="x")
        t.replies = [m.Reply(floor="2楼", author="大佬", when="",
                             text="其实可以先用 Docker 固定 IP，更简单")]
        self.assertIn("更优解法", m.detect_revisit_trigger(t, cfg))

    def test_no_trigger_on_plain_chat(self):
        cfg = m.Config()
        cfg.my_username = "naser"
        t = m.Thread(tid="7001", title="x")
        t.replies = [m.Reply(floor="2楼", author="路人", when="",
                             text="我也遇到一样的问题，同求解决办法")]
        self.assertEqual(m.detect_revisit_trigger(t, cfg), "")

    def test_revisit_blocks_near_duplicate_reply(self):
        cfg = m.Config()
        t = m.Thread(tid="7001", title="x", revisit=True,
                     my_previous_reply="你把固件升级到最新然后重启就好了")
        ok, why = m.validate_claim("你把固件升级到最新然后重启就行了",
                                    {"kind": "text"}, t, cfg)
        self.assertFalse(ok)
        self.assertIn("重复", why)

    def test_revisit_allows_different_reply(self):
        cfg = m.Config()
        t = m.Thread(tid="7001", title="x", revisit=True,
                     my_previous_reply="升级固件重启")
        ok, why = m.validate_claim("另一个思路：先看容器日志确认端口有没有监听",
                                    {"kind": "text"}, t, cfg)
        self.assertTrue(ok, why)


class CasualMediaSafetyTest(unittest.TestCase):
    """需求 2/3/4：水帖与图文、内容安全。"""

    def test_media_only_from_whitelist(self):
        ok, out = m.sanitize_reply("照这个顺序排查即可", [], kind="text",
                                   media="[赞]", media_codes=("[赞]", "[图片]"))
        self.assertTrue(ok)
        self.assertIn("[赞]", out)
        ok2, out2 = m.sanitize_reply("照这个顺序排查即可", [], kind="text",
                                     media="[外链]", media_codes=("[赞]",))
        self.assertTrue(ok2)
        self.assertNotIn("[外链]", out2)

    def test_inline_evidence_ref_stripped(self):
        """模型把证据编号写进正文时要剥掉，引用关系只走 evidence_used。"""
        ok, out = m.sanitize_reply(
            "先确认是否开启了自定义域名功能（kb:919），DH2600 可能存在固件兼容问题。",
            [], kind="text")
        self.assertTrue(ok)
        self.assertNotIn("kb:", out)
        self.assertNotIn("（", out)
        self.assertIn("自定义域名功能，DH2600", out)
        # 半角括号 / 方括号包裹、web 前缀同样剥掉
        for raw in ("建议先查一下系统日志 (web:ab12cd34ef) 再重启 NAS 试一次。",
                    "参考 [KB:919] 这篇官方文档里的排查步骤应该就能解决。"):
            ok2, out2 = m.sanitize_reply(raw, [], kind="text")
            self.assertTrue(ok2)
            self.assertNotIn(":", out2)

    def test_photo_placeholder_stripped(self):
        """[图片] 是编辑器附件占位符，发出去不渲染成图，出现即剥掉。"""
        ok, out = m.sanitize_reply(
            "建议回退到更新前版本或联系官方反馈。\n[图片]", [], kind="text")
        self.assertTrue(ok)
        self.assertNotIn("[图片]", out)
        self.assertTrue(out.endswith("反馈。"))
        self.assertEqual(out, "建议回退到更新前版本或联系官方反馈。")

    def test_media_codes_exclude_image_placeholder(self):
        """[图片] / [示意图] 不是可渲染的论坛表情码，默认白名单不再包含。"""
        self.assertNotIn("[图片]", m.DEFAULT_MEDIA_CODES)
        self.assertNotIn("[示意图]", m.DEFAULT_MEDIA_CODES)

    def test_casual_short_reply_allowed(self):
        ok, out = m.sanitize_reply("[哈哈]", [], kind="casual", allow_casual=True)
        self.assertTrue(ok)
        self.assertEqual(out, "[哈哈]")

    def test_casual_disabled_falls_back_to_text_rules(self):
        ok, why = m.sanitize_reply("嗯嗯好的", [], kind="casual", allow_casual=False)
        self.assertFalse(ok)
        self.assertIn("长度", why)

    def test_abusive_content_blocked(self):
        ok, why = m.sanitize_reply("你这操作真是傻逼", [], kind="text")
        self.assertFalse(ok)
        self.assertIn("辱骂", why)

    def test_unsafe_topic_blocked(self):
        ok, why = m.sanitize_reply("这个玩法涉及破解版资源，教程在这", [], kind="text")
        self.assertFalse(ok)
        self.assertIn("辱骂", why)

    def test_parse_ai_response_casual_and_media(self):
        got = m.parse_ai_response(json.dumps({"results": [{
            "tid": "1", "should_reply": True, "kind": "casual",
            "answer_mode": "direct", "confidence": "high",
            "format": "steps", "media": "[赞]", "reply": "打卡"}]}),
            {"1"})
        row = got["1"]
        self.assertEqual(row["kind"], "casual")
        # 水帖不能自称在下技术结论，置信度一律压低
        self.assertEqual(row["answer_mode"], "casual")
        self.assertEqual(row["confidence"], "low")
        self.assertEqual(row["format"], "steps")
        self.assertEqual(row["media"], "[赞]")

    def test_baidu_provider_config(self):
        args = m.build_parser().parse_args([])
        env = {"BAIDU_SEARCH_KEY": "bd-key", "BAIDU_SEARCH_TOP_K": "8",
               "BAIDU_SEARCH_RESULTS_PATH": "data.references",
               "BAIDU_SEARCH_URL": "https://example.com/search"}
        with patch.dict(os.environ, env, clear=True):
            cfg = m.Config.from_env(args)
        self.assertEqual(cfg.baidu_search_key, "bd-key")
        self.assertEqual(cfg.baidu_top_k, 8)
        self.assertEqual(cfg.baidu_results_path, "data.references")
        self.assertEqual(cfg.baidu_search_url, "https://example.com/search")

    def test_baidu_without_key_does_not_call_network(self):
        cfg = m.Config()
        cfg.baidu_search_key = ""
        with patch.object(m.requests, "post",
                          side_effect=AssertionError("不该发请求")):
            self.assertEqual(m.search_baidu("硬盘休眠", cfg), [])


class ModelOutputParsingTest(unittest.TestCase):
    """思考型模型返回各种形态时，必须都能抠出 JSON，不能整批丢弃。"""

    VALID = {"results": [{"tid": "7001", "should_reply": True, "kind": "text",
                          "reply": "可以先看权限设置", "reason": "权限问题"}]}

    def test_plain_json(self):
        got = m.parse_ai_response(json.dumps(self.VALID), {"7001"})
        self.assertTrue(got["7001"]["should_reply"])

    def test_markdown_fenced_json(self):
        text = "```json\n" + json.dumps(self.VALID, ensure_ascii=False) + "\n```"
        got = m.parse_ai_response(text, {"7001"})
        self.assertTrue(got["7001"]["should_reply"])

    def test_prose_before_json(self):
        """模型先说一句「好的，以下是结果」再给 JSON —— 旧代码在这里直接崩。"""
        text = "好的，以下是逐篇判断结果：\n\n" + json.dumps(self.VALID, ensure_ascii=False)
        got = m.parse_ai_response(text, {"7001"})
        self.assertTrue(got["7001"]["should_reply"])

    def test_prose_and_fence_mixed(self):
        text = ("思考完毕。\n```json\n" + json.dumps(self.VALID, ensure_ascii=False)
                + "\n```\n以上，希望有帮助。")
        got = m.parse_ai_response(text, {"7001"})
        self.assertTrue(got["7001"]["should_reply"])

    def test_brace_inside_string_does_not_break_extraction(self):
        payload = {"results": [{"tid": "7001", "should_reply": True, "kind": "text",
                                "reply": "路径是 C:\\目录\\{x} 这样", "reason": "路径"}]}
        text = "结果如下：\n" + json.dumps(payload, ensure_ascii=False)
        got = m.parse_ai_response(text, {"7001"})
        self.assertIn("目录", got["7001"]["reply"])

    def test_empty_content_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            m.parse_ai_response("", {"7001"})

    def test_extract_content_from_list_parts(self):
        resp = {"choices": [{"finish_reason": "stop", "message": {
            "content": [{"type": "text", "text": "hello"}]}}]}
        content, meta = m.extract_ai_content(resp)
        self.assertEqual(content, "hello")
        self.assertEqual(meta["finish_reason"], "stop")

    def test_extract_content_falls_back_to_reasoning(self):
        """思考模型把 token 花光、正文为空时，要能看到思考内容并被标记。"""
        resp = {"choices": [{"finish_reason": "length", "message": {
            "content": "", "reasoning_content": "<think>想了很久</think>"}}]}
        content, meta = m.extract_ai_content(resp)
        self.assertTrue(meta.get("reasoning_only"))
        self.assertEqual(meta["finish_reason"], "length")
        self.assertIn("想了很久", content)

    def test_extract_content_missing_choices(self):
        content, meta = m.extract_ai_content({"id": "x"})
        self.assertEqual(content, "")
        self.assertIn("reason", meta)

    def test_strict_nudge_appends_format_requirement(self):
        body = {"messages": [{"role": "system", "content": "s"},
                             {"role": "user", "content": "判一下"}]}
        out = m._strict_nudge(body)
        self.assertIn("严格格式要求", out["messages"][-1]["content"])
        # 原 body 不能被就地改坏
        self.assertNotIn("严格格式要求", body["messages"][-1]["content"])


class CallAiRobustTest(unittest.TestCase):
    """call_ai 的重试与降级行为。"""

    @staticmethod
    def _cfg():
        cfg = m.Config()
        cfg.ai_key = "test-key"
        cfg.ai_base = "http://127.0.0.1:9/v1"
        cfg.ai_chat_path = "/chat/completions"
        cfg.ai_model = "test-model"
        cfg.ai_retries = 2
        cfg.ai_timeout = 5
        cfg.ai_json_mode = False
        return cfg

    @staticmethod
    def _resp(content, finish_reason="stop", reasoning=None):
        msg = {"content": content}
        if reasoning is not None:
            msg["reasoning_content"] = reasoning
        return {"choices": [{"finish_reason": finish_reason, "message": msg}]}

    @staticmethod
    def _batch(n=1):
        return [m.Thread(tid=str(7000 + i), title="t", content="c", forum="f")
                for i in range(n)]

    def test_json_parse_failure_triggers_adaptive_retry(self):
        """第一次返回带前言的非 JSON，第二次正常 —— 应自动重试并成功。"""
        calls = []

        class R:
            def __init__(self, payload, code=200):
                self.status_code = code
                self._p = payload
                self.text = json.dumps(payload, ensure_ascii=False)

            def json(self):
                return self._p

        ok = {"results": [{"tid": "7000", "should_reply": True, "kind": "text",
                           "reply": "建议先重启服务", "reason": "服务异常"}]}
        # 第一次：完全无法解析的输出；第二次：正常 JSON
        seq = [R(self._resp("这不是 JSON，只是一句话")),
               R(self._resp(json.dumps(ok, ensure_ascii=False)))]

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)
            return seq[len(calls) - 1]

        with patch.object(m.requests, "post", side_effect=fake_post):
            got = m.call_ai(self._batch(1), self._cfg())
        self.assertIn("7000", got)
        self.assertEqual(len(calls), 2)
        # 第二次请求应带上严格格式要求
        self.assertIn("严格格式要求", calls[1]["messages"][-1]["content"])

    def test_empty_content_marks_reasoning_only(self):
        """正文为空 + 只有思考内容 -> 下一轮关闭思考模式。"""
        calls = []

        class R:
            status_code = 200
            text = "{}"

            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p

        seq = [R(self._resp("", finish_reason="length", reasoning="<think>想了</think>")),
               R(self._resp("", finish_reason="length", reasoning="<think>又想了</think>")),
               R(self._resp("", finish_reason="length", reasoning="<think>再想</think>"))]

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)
            return seq[len(calls) - 1]

        with patch.object(m.requests, "post", side_effect=fake_post):
            got = m.call_ai(self._batch(1), self._cfg())
        self.assertIsNone(got)
        self.assertTrue(calls)
        self.assertFalse(calls[0].get("enable_thinking"))
        # 第二次开始应已加上 enable_thinking=false
        self.assertFalse(calls[1].get("enable_thinking"))

    def test_batch_fallback_covers_missing_posts(self):
        """整批解析失败时，逐篇重试应把帖子补回来。"""
        cfg = self._cfg()
        cfg.ai_batch_fallback = True
        ok = {"results": [{"tid": "7000", "should_reply": True, "kind": "text",
                           "reply": "可以试试", "reason": "有解"}]}
        seq = [
            # 整批：带前言的坏 JSON
            _R({"choices": [{"finish_reason": "stop", "message": {
                "content": "稍等，我看看：\n这不是 JSON"}}]}),
            # 逐篇重试第一篇：正常
            _R({"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(ok, ensure_ascii=False)}}]}),
        ]
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)
            return seq[min(len(calls) - 1, len(seq) - 1)]

        with patch.object(m.requests, "post", side_effect=fake_post):
            # 直接验证主流程用的兜底逻辑：缺哪篇就补哪篇
            decisions = {}
            got = m.call_ai(self._batch(1), cfg)
            if got:
                decisions.update(got)
            missing = [t for t in self._batch(1) if t.tid not in decisions]
            if missing:
                for t in missing:
                    one = m.call_ai([t], cfg)
                    if one:
                        decisions.update(one)
        self.assertIn("7000", decisions)

    def test_server_rejecting_max_tokens_falls_back(self):
        """服务端嫌 max_tokens 太大而 400 时，去掉该字段重试。"""
        cfg = self._cfg()
        cfg.ai_max_tokens = 131072
        ok_body = json.dumps({"results": [
            {"tid": "7000", "should_reply": True, "kind": "text",
             "reply": "可以试试", "reason": "有解"}]}, ensure_ascii=False)
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(dict(json))
            if len(calls) == 1:
                return _R({"error": {"message": "max_tokens is too large"}},
                          code=400)
            return _R({"choices": [{"finish_reason": "stop", "message": {
                "content": ok_body}}]})

        with patch.object(m.requests, "post", side_effect=fake_post):
            got = m.call_ai(self._batch(1), cfg)
        self.assertIsNotNone(got)
        self.assertIn("7000", got)
        self.assertIn("max_tokens", calls[0])
        self.assertNotIn("max_tokens", calls[1])

    def test_unlimited_truncation_defers_to_per_post_retry(self):
        """不限制 max_tokens 还被截断时：先试关思考，仍不行才交给逐篇重试拆分。"""
        cfg = self._cfg()
        cfg.ai_max_tokens = 0
        cfg.ai_retries = 3
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(dict(json))
            return _R(self._resp("Let me analyze each post one by one.",
                                  finish_reason="length"))

        with patch.object(m.requests, "post", side_effect=fake_post):
            got = m.call_ai(self._batch(1), cfg)
        self.assertIsNone(got)
        # 请求体里本来就没有 max_tokens
        self.assertNotIn("max_tokens", calls[0])
        # 第一轮先关思考重试一次，还不行才放弃本批
        self.assertGreaterEqual(len(calls), 2)
        self.assertFalse(calls[1].get("enable_thinking"))

    def test_truncation_widens_max_tokens(self):
        """被截断时按比例放宽 max_tokens，且不超过配置的天花板。"""
        cfg = self._cfg()
        cfg.ai_max_tokens = 4096
        cfg.ai_max_tokens_cap = 6000
        cfg.ai_retries = 2
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(dict(json))
            return _R(self._resp("Let me analyze each post one by one.",
                                  finish_reason="length"))

        with patch.object(m.requests, "post", side_effect=fake_post):
            m.call_ai(self._batch(1), cfg)
        self.assertGreater(len(calls), 1)
        self.assertEqual(calls[0]["max_tokens"], 4096)
        self.assertEqual(calls[1]["max_tokens"], 6000)


class _R:
    """测试用的小响应替身。"""

    def __init__(self, payload, code=200):
        self.status_code = code
        self._p = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._p


class SharePostLearnTest(unittest.TestCase):
    """需求：分享帖 / 自荐帖可以回学习、点赞式回复（kind=learn）。

    同时验证：固定语料（感谢分享 / 赞一个…）从「一律拦死」改成
    「学习型回复与分享帖里可用，技术性回复里仍然拦」。
    """

    def _t(self, title, content="", replies=None):
        return m.Thread(tid="9001", title=title, content=content,
                         forum="玩法攻略", replies=replies or [])

    # ---- 分享帖识别 ----
    def test_self_intro_is_share_post(self):
        self.assertTrue(m.is_share_post(
            self._t("【自荐】给绿联NAS请了一个AI管家，从此动动嘴就能玩转NAS")))

    def test_sharing_markers_detected(self):
        for title in ("绿联 NAS 实战｜用 Home Assistant 接入小爱同学",
                      "NAS 玩法分享：我的远程访问方案",
                      "Docker 实战：三个容器的 Compose 编排",
                      "UGOS Pro 科普：硬盘休眠那些事",
                      "Docker部署【uglog】：绿联 NAS 的日志与状态推送工具",
                      "手把手教你用 Docker 部署 Jellyfin",
                      "从零搭建 NAS 家庭影音库"):
            self.assertTrue(m.is_share_post(self._t(title)), title)

    def test_learn_kind_allows_short_praise(self):
        """「赞」「nice」「学到了」这类短回应是合法的。"""
        for s in ("赞", "nice", "学到了", "👍", "赞一个"):
            ok, out = m.sanitize_reply(s, [], kind="learn")
            self.assertTrue(ok, s)
            self.assertEqual(out, s)

    def test_question_post_is_not_share(self):
        """求助 / 提问帖不能当分享帖，否则会给真问题回一句「感谢分享」。"""
        for title in ("求个AI Plugins.UPK",
                      "怎么安装wps office？",
                      "硬盘休眠问题求助",
                      "请问这个配置文件怎么改",
                      "有没有人遇到过同样问题"):
            self.assertFalse(m.is_share_post(self._t(title)), title)

    def test_share_marker_with_question_word_still_question(self):
        self.assertFalse(m.is_share_post(self._t("【自荐】请问这个项目怎么用？")))

    def test_tutorial_with_how_in_title_is_share(self):
        """教程标题里的「如何」不能被当成提问（ZFS特性一览…ZFS如何保护我的数据）。"""
        self.assertTrue(m.is_share_post(self._t(
            "【介绍ZFS】ZFS特性一览！一起来看看使用ZFS有哪些好处，"
            "以及ZFS如何保护我的数据")))

    def test_category_bracket_tag_is_not_share(self):
        """【学院体验】这类分类标记不代表分享。"""
        self.assertFalse(m.is_share_post(
            self._t("【学院体验】关于UGOS PRO学院中的若干表述问题")))

    def test_unknown_title_is_conservative(self):
        """看不出倾向的标题按提问帖处理 —— 宁可漏回，也不能答非所问。"""
        for title in ("相册备份", "TV终端播放视频",
                      "应用程序里面只有安装过的软件 官方的没有了"):
            self.assertFalse(m.is_share_post(self._t(title)), title)

    # ---- 固定语料的放行条件 ----
    def test_cliche_allowed_in_learn_kind(self):
        for s in ("感谢分享，学到了 👍", "赞一个", "学习了，这个思路很妙"):
            ok, out = m.sanitize_reply(s, [], kind="learn")
            self.assertTrue(ok, s)
            self.assertEqual(out, s)

    def test_cliche_allowed_on_share_post(self):
        """分享帖里即便模型用了 text 形态，固定语料也不该被拦。"""
        ok, _ = m.sanitize_reply("感谢分享，学到了 👍", [], kind="text",
                                  share_post=True)
        self.assertTrue(ok)

    def test_cliche_still_blocked_in_technical_reply(self):
        """技术性回复（非分享帖）里仍然拦，避免刷屏。"""
        ok, why = m.sanitize_reply("感谢分享！这个教程太实用了", [])
        self.assertFalse(ok)
        self.assertIn("灌水", why)

    def test_learn_kind_still_blocks_unsafe(self):
        ok, why = m.sanitize_reply("感谢分享，另外这玩法涉及破解版资源", [], kind="learn")
        self.assertFalse(ok)
        self.assertTrue("辱骂" in why or "公序良俗" in why)

    def test_learn_kind_still_blocks_link(self):
        ok, why = m.sanitize_reply("感谢分享，详见 https://example.com", [], kind="learn")
        self.assertFalse(ok)
        self.assertIn("外链", why)

    def test_learn_kind_rejects_empty(self):
        ok, why = m.sanitize_reply("", [], kind="learn")
        self.assertFalse(ok)

    def test_learn_kind_rejects_duplicate(self):
        s = "感谢分享，学到了 👍"
        ok, why = m.sanitize_reply(s, [s], kind="learn")
        self.assertFalse(ok)
        self.assertIn("重复", why)

    # ---- 保守模式 ----
    def test_learn_reply_cannot_contain_tech_claim(self):
        """学习/点赞型里不能夹带版本号之类的技术断言。"""
        cfg = m.Config()
        t = self._t("【自荐】我的工具")
        d = {"kind": "learn", "answer_mode": "learn", "confidence": "low",
             "evidence_used": []}
        ok, why = m.validate_claim("感谢分享，UGOS Pro 4.0.1 支持这个功能", d, t, cfg)
        self.assertFalse(ok)

    # ---- 生成回复 ----
    def test_share_learn_reply_mentions_specific_thing(self):
        r = m.share_learn_reply(
            self._t("【自荐】给绿联NAS请了一个AI管家 WorkBuddy"))
        self.assertIn("WorkBuddy", r)
        self.assertLessEqual(len(r), 60)

    def test_share_learn_reply_generic_fallback(self):
        r = m.share_learn_reply(self._t("【分享】"))
        self.assertTrue(r)
        self.assertIn("感谢分享", r)

    def test_token_extraction_prefers_specific_name(self):
        """要挑工具名，不能挑「的日志与状态推送工具」这种描述句。"""
        self.assertEqual(m.extract_title_token("Docker部署【uglog】：日志推送工具"), "uglog")
        self.assertEqual(
            m.extract_title_token("【自荐】绿联 NAS 实战｜用 Home Assistant 接入"),
            "Home Assistant")
        self.assertEqual(m.extract_title_token("【自荐】给NAS请了一个AI管家 WorkBuddy"),
                         "WorkBuddy")
        # 通用词不能被当成名字
        self.assertNotEqual(m.extract_title_token("NAS 应用设置"), "NAS")
        # 【介绍ZFS】这种「前缀+专名」只取专名
        self.assertEqual(m.extract_title_token("【介绍ZFS】ZFS特性一览"), "ZFS")
        # 描述性长句不能被当成名字
        self.assertNotIn("玩转", m.extract_title_token(
            "【自荐】给绿联NAS请了一个AI管家，从此动动嘴就能玩转NAS"))

    def test_already_praised_detects_existing_cheer(self):
        replies = [m.Reply(floor="2", author="a", text="感谢分享，学到了")]
        self.assertTrue(m.already_praised(self._t("【自荐】工具", replies=replies)))
        replies2 = [m.Reply(floor="2", author="a", text="我也有同样的问题，后来解决了")]
        self.assertFalse(m.already_praised(self._t("【自荐】工具", replies=replies2)))

    # ---- 解析 ----
    def test_parse_keeps_learn_kind(self):
        row = {"tid": "9001", "should_reply": True, "kind": "learn",
               "reply": "感谢分享，学到了", "reason": "自荐帖"}
        got = m.parse_ai_response(json.dumps({"results": [row]},
                                             ensure_ascii=False), {"9001"})
        self.assertEqual(got["9001"]["kind"], "learn")
        self.assertEqual(got["9001"]["answer_mode"], "learn")
        self.assertEqual(got["9001"]["confidence"], "low")

    def test_payload_includes_is_share(self):
        t = self._t("【自荐】我的工具")
        payload = m.ai_payload([t])
        self.assertTrue(payload[0]["is_share"])
        t2 = self._t("求个AI Plugins.UPK")
        self.assertFalse(m.ai_payload([t2])[0]["is_share"])

    # ---- 配置 ----
    def test_learn_config_defaults(self):
        cfg = m.Config()
        self.assertTrue(cfg.allow_learn_reply)
        self.assertEqual(cfg.max_learn, 3)


class GuideModeTest(unittest.TestCase):
    """论坛导读模式：不区分板块、按发布时间倒序。"""

    def _cfg(self):
        cfg = m.Config()
        cfg.use_guide = True
        cfg.guide_pages = 1
        cfg.guide_limit = 20
        return cfg

    # ---- 时间解析 ----
    def test_relative_minutes(self):
        now = datetime(2026, 11, 17, 15, 0, 0)
        got = m.parse_guide_time("发表于 27 分钟前", "2026-11-17", now)
        self.assertEqual(got, datetime(2026, 11, 17, 14, 33, 0))

    def test_half_hour(self):
        now = datetime(2026, 11, 17, 15, 0, 0)
        got = m.parse_guide_time("发表于 半小时前", "2026-11-17", now)
        self.assertEqual(got, datetime(2026, 11, 17, 14, 30, 0))

    def test_relative_hours_and_days(self):
        now = datetime(2026, 11, 17, 15, 0, 0)
        self.assertEqual(m.parse_guide_time("发表于 4 小时前", "2026-11-17", now),
                         datetime(2026, 11, 17, 11, 0, 0))
        self.assertEqual(m.parse_guide_time("发表于 3 天前", "2026-11-14", now),
                         datetime(2026, 11, 14, 15, 0, 0))

    def test_yesterday_clock(self):
        """「昨天 23:54」要用 title 里的绝对日期 + 文本里的钟点。"""
        got = m.parse_guide_time("发表于 昨天 23:54", "2026-11-16")
        self.assertEqual(got, datetime(2026, 11, 16, 23, 54, 0))

    def test_before_yesterday_clock(self):
        got = m.parse_guide_time("发表于 前天 09:14", "2026-11-15")
        self.assertEqual(got, datetime(2026, 11, 15, 9, 14, 0))

    def test_nbsp_is_normalized(self):
        """导读页的时间里有不换行空格 \\xa0，必须能识别。"""
        now = datetime(2026, 11, 17, 15, 0, 0)
        got = m.parse_guide_time("发表于 28\xa0分钟前", "2026-11-17", now)
        self.assertEqual(got, datetime(2026, 11, 17, 14, 32, 0))

    def test_date_only_fallback(self):
        got = m.parse_guide_time("发表于 2026-11-16", "2026-11-16")
        self.assertEqual(got, datetime(2026, 11, 16, 0, 0, 0))

    def test_unparseable_returns_none(self):
        self.assertIsNone(m.parse_guide_time("", ""))
        self.assertIsNone(m.parse_guide_time("发表于 未知", ""))

    # ---- 抓取 ----
    def test_fetch_guide_parses_items(self):
        html = """
        <div class="tl"><ul id="threadlisttableid">
          <li class="kmlist common">
            <a class="kmtit" href="forum.php?mod=viewthread&amp;tid=7351&amp;extra=">
              <span class="kmico kmicotxt">求回帖</span>
              <span class="km_subject">建议媒体库列表增加用户隔离功能</span>
            </a>
            <div class="kmfoot">
              <span class="kmpl">1</span>
              <a href="home.php?mod=space&amp;uid=3425">Vy5zgk</a>
              <span class="kmtime">发表于 <span title="2026-11-17">27 分钟前</span></span>
              <a class="kmbg kmico_bk" href="forum.php?mod=forumdisplay&amp;fid=10">功能讨论</a>
            </div>
          </li>
          <li class="kmlist common">
            <a class="kmtit" href="forum.php?mod=viewthread&amp;tid=7350&amp;extra=">
              <span class="km_subject">笔记多端同步</span>
            </a>
            <div class="kmfoot">
              <a href="home.php?mod=space&amp;uid=212">xAG8sU</a>
              <span class="kmtime">发表于 <span title="2026-11-17">半小时前</span></span>
              <a class="kmbg kmico_bk" href="forum.php?mod=forumdisplay&amp;fid=16">需求广场</a>
            </div>
          </li>
        </ul></div>
        """
        cfg = self._cfg()
        client = m.UgnasClient.__new__(m.UgnasClient)
        client.cfg = cfg
        client.request_delay = 0
        soup = m.BeautifulSoup(html, "html.parser")
        client._soup = lambda url: soup
        client._raw = lambda url: None

        got = client.fetch_guide()
        self.assertEqual(len(got), 2)
        by_tid = {t.tid: t for t in got}
        t1 = by_tid["7351"]
        self.assertEqual(t1.title, "建议媒体库列表增加用户隔离功能")
        self.assertEqual(t1.forum, "功能讨论")
        self.assertEqual(t1.fid, 10)
        self.assertEqual(t1.author, "Vy5zgk")
        self.assertIsNotNone(t1.published)
        self.assertEqual(t1.url, m.THREAD_URL.format(tid="7351"))
        # 不在 DEFAULT_FORUMS 里的板块也要能解析出来
        t2 = by_tid["7350"]
        self.assertEqual(t2.forum, "需求广场")
        self.assertEqual(t2.fid, 16)

    def test_fetch_guide_skips_duplicates(self):
        item = """
        <li class="kmlist common">
          <a class="kmtit" href="forum.php?mod=viewthread&amp;tid=7351&amp;extra=">
            <span class="km_subject">重复帖</span>
          </a>
        </li>
        """
        html = f'<div class="tl"><ul id="threadlisttableid">{item}{item}</ul></div>'
        cfg = self._cfg()
        client = m.UgnasClient.__new__(m.UgnasClient)
        client.cfg = cfg
        client.request_delay = 0
        soup = m.BeautifulSoup(html, "html.parser")
        client._soup = lambda url: soup
        client._raw = lambda url: None
        got = client.fetch_guide()
        self.assertEqual(len(got), 1)

    # ---- 配置 ----
    def test_guide_is_default_source(self):
        cfg = m.Config()
        self.assertTrue(cfg.use_guide)
        self.assertEqual(cfg.guide_limit, 20)
        self.assertEqual(cfg.guide_pages, 1)

    def test_forum_mode_still_available(self):
        """UGREEN_USE_GUIDE=0 时仍然走原来的板块 RSS。"""
        args = m.build_parser().parse_args([])
        with patch.dict(os.environ, {}, clear=True):
            cfg = m.Config.from_env(args)
        self.assertTrue(cfg.use_guide)

        args2 = m.build_parser().parse_args(["--forums"])
        with patch.dict(os.environ, {}, clear=True):
            cfg2 = m.Config.from_env(args2)
        self.assertFalse(cfg2.use_guide)

    def test_use_guide_env_switch(self):
        args = m.build_parser().parse_args([])
        with patch.dict(os.environ, {"UGREEN_USE_GUIDE": "0"}, clear=True):
            cfg = m.Config.from_env(args)
        self.assertFalse(cfg.use_guide)

    def test_guide_limits_from_env(self):
        args = m.build_parser().parse_args([])
        with patch.dict(os.environ,
                        {"UGREEN_GUIDE_PAGES": "3", "UGREEN_GUIDE_LIMIT": "40"},
                        clear=True):
            cfg = m.Config.from_env(args)
        self.assertEqual(cfg.guide_pages, 3)
        self.assertEqual(cfg.guide_limit, 40)


# ===========================================================================
# 每日配额
# ===========================================================================

class DailyQuotaTest(unittest.TestCase):
    """每日发送配额：跨运行累计、用满即整轮跳过、可配置为不限制。"""

    @staticmethod
    def _cfg(env=None, argv=None):
        args = m.build_parser().parse_args(argv or [])
        with patch.dict(os.environ, env or {}, clear=True):
            return m.Config.from_env(args)

    def test_default_quota_is_ten(self):
        self.assertEqual(self._cfg().daily_quota, 10)

    def test_quota_from_env(self):
        self.assertEqual(self._cfg({"UGREEN_DAILY_QUOTA": "3"}).daily_quota, 3)
        self.assertEqual(self._cfg({"UGREEN_DAILY_QUOTA": "0"}).daily_quota, 0)

    def test_quota_cli_overrides_env(self):
        cfg = self._cfg({"UGREEN_DAILY_QUOTA": "3"}, ["--daily-quota", "8"])
        self.assertEqual(cfg.daily_quota, 8)
        cfg0 = self._cfg({"UGREEN_DAILY_QUOTA": "3"}, ["--daily-quota", "0"])
        self.assertEqual(cfg0.daily_quota, 0)

    def test_negative_quota_falls_back_to_unlimited(self):
        self.assertEqual(self._cfg({"UGREEN_DAILY_QUOTA": "-5"}).daily_quota, 0)

    def test_daily_used_and_bump(self):
        state = {}
        self.assertEqual(m.daily_used(state, "2026-11-17"), 0)
        self.assertEqual(m.bump_daily_used(state, "2026-11-17"), 1)
        self.assertEqual(m.bump_daily_used(state, "2026-11-17", 2), 3)
        self.assertEqual(m.daily_used(state, "2026-11-17"), 3)
        # 别的日期互不影响
        self.assertEqual(m.daily_used(state, "2026-11-18"), 0)

    def test_daily_used_survives_broken_state(self):
        self.assertEqual(m.daily_used({}, "2026-11-17"), 0)
        self.assertEqual(m.daily_used({"daily": "坏数据"}, "2026-11-17"), 0)
        self.assertEqual(m.daily_used({"daily": {"2026-11-17": "abc"}},
                                      "2026-11-17"), 0)
        self.assertEqual(m.daily_used({"daily": {"2026-11-17": -3}},
                                      "2026-11-17"), 0)

    def test_bump_prunes_old_days(self):
        state = {"daily": {f"2026-01-{d:02d}": 3 for d in range(1, 29)}}
        state["daily"].update({f"2026-02-{d:02d}": 1 for d in range(1, 13)})
        m.bump_daily_used(state, "2026-11-17")
        self.assertEqual(len(state["daily"]), m.DAILY_KEEP_DAYS)
        self.assertIn("2026-11-17", state["daily"])
        self.assertNotIn("2026-01-01", state["daily"])

    def test_bump_repairs_broken_value(self):
        state = {"daily": {"2026-11-17": "坏数据"}}
        self.assertEqual(m.bump_daily_used(state, "2026-11-17"), 1)


class DailyQuotaFlowTest(unittest.TestCase):
    """实发模式下的每日配额：整轮跳过 / 剩余额度截断 / 计数落盘。"""

    # 每条回复内容必须互不相同，否则会被「与近期已发回复高度重复」闸门拦下
    _replies = {
        "9101": "先确认容器端口是否真的监听，再看宿主机映射和防火墙规则是否放通。",
        "9201": "先确认容器端口是否真的监听，再看宿主机映射和防火墙规则是否放通。",
        "9202": "换个思路：进容器手动 curl 本机端口，能通就说明网络链路没问题。",
        "9203": "建议改用桥接网络并在 compose 里显式写出对外端口，然后重建容器再验证。",
        "9204": "这类情况多半是容器内部服务没起来，看 logs 里的启动报错更快定位。",
        "9301": "先确认容器端口是否真的监听，再看宿主机映射和防火墙规则是否放通。",
        "9302": "换个思路：进容器手动 curl 本机端口，能通就说明网络链路没问题。",
        "9303": "建议改用桥接网络并在 compose 里显式写出对外端口，然后重建容器再验证。",
        "9401": "先确认容器端口是否真的监听，再看宿主机映射和防火墙规则是否放通。",
    }

    class _Resp:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return {"choices": [{"message": {
                "content": json.dumps(self.payload, ensure_ascii=False)}}]}

    @staticmethod
    def _thread(tid, title, minutes=10):
        t = m.Thread(tid=tid, title=title, fid=14, forum="Docker",
                     content="容器起来了但网页打不开",
                     published=datetime.now() - timedelta(minutes=minutes))
        return t

    def _fake_ai(self, url, **kwargs):
        body = kwargs.get("json") or {}
        text = body["messages"][1]["content"]
        tids = re.findall(r'"tid":\s*"(\d+)"', text)
        rows = []
        for tid in tids:
            rows.append({"tid": tid, "should_reply": True, "kind": "text",
                         "reply": self._replies[tid],
                         "reason": "具体报错",
                         "answer_mode": "probe", "confidence": "low",
                         "evidence_used": []})
        return self._Resp({"results": rows})

    def _run_live(self, tmpdir, state, threads, extra_env=None, argv=None):
        """跑一次实发 main()，返回 (退出码, 输出, 发帖记录)。"""
        state_path = Path(tmpdir) / "ugreen_state.json"
        state_path.write_text(json.dumps(state, ensure_ascii=False),
                              encoding="utf-8")
        env = {"AI_API_KEY": "test-key",
               "AI_BASE_URL": "http://127.0.0.1:9/v1",
               "UGREEN_COOKIE": "fake-cookie",
               "UGREEN_DRY_RUN": "0", "UGREEN_NOTIFY": "0",
               "UGREEN_STATE_DIR": str(tmpdir),
               "UGREEN_SEARCH": "0",
               "UGREEN_MIN_DELAY": "0", "UGREEN_MAX_DELAY": "0"}
        env.update(extra_env or {})
        posted = []

        def fake_post_reply(self, t, message):
            posted.append((t.tid, message))
            return True, "", "回复发布成功"

        def fake_detail(self, t, floors=None):
            t.can_reply = True
            t.detail_ok = True
            t.formhash = "hash"
            t.reply_action = "https://club.ugnas.com/post.php"
            return t

        buf = io.StringIO()
        argv = argv or ["ugreen_ai_reply.py", "--hours", "48",
                        "--per-forum", "10", "--no-notify"]
        with patch.dict(os.environ, env, clear=True), \
                patch.object(m.UgnasClient, "check_login", return_value=True), \
                patch.object(m.UgnasClient, "fetch_rss", return_value=threads), \
                patch.object(m.UgnasClient, "fetch_guide", return_value=threads), \
                patch.object(m.UgnasClient, "fetch_detail", fake_detail), \
                patch.object(m.UgnasClient, "post_reply", fake_post_reply), \
                patch.object(m.requests, "post", side_effect=self._fake_ai), \
                patch.object(sys, "argv", argv), \
                redirect_stdout(buf):
            rc = m.main()
        return rc, buf.getvalue(), posted, state_path

    def test_quota_exhausted_skips_whole_run(self):
        today = datetime.now().strftime("%Y-%m-%d")
        threads = [self._thread("9101", "Docker 容器端口映射失败求助")]
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, posted, _ = self._run_live(
                tmp, {"daily": {today: 10}}, threads,
                extra_env={"UGREEN_DAILY_QUOTA": "10"})
        self.assertEqual(rc, 0)
        self.assertEqual(posted, [])
        self.assertIn("今日配额已用满", out)
        self.assertIn("10/10", out)

    def test_remaining_quota_truncates_run(self):
        """今日已用 8 条、配额 10 条、单次上限 5 → 本次只能发 2 条。"""
        today = datetime.now().strftime("%Y-%m-%d")
        threads = [self._thread("9201", "Docker 容器端口映射失败求助 A"),
                   self._thread("9202", "Docker 容器端口映射失败求助 B"),
                   self._thread("9203", "Docker 容器端口映射失败求助 C"),
                   self._thread("9204", "Docker 容器端口映射失败求助 D")]
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, posted, state_path = self._run_live(
                tmp, {"daily": {today: 8}}, threads,
                extra_env={"UGREEN_DAILY_QUOTA": "10", "UGREEN_MAX_REPLIES": "5"})
            self.assertEqual(rc, 0)
            self.assertEqual(len(posted), 2)
            self.assertIn("本次最多发 2 条", out)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["daily"][today], 10)

    def test_unlimited_quota_ignores_daily_counter(self):
        """UGREEN_DAILY_QUOTA=0 时不受每日计数约束。"""
        today = datetime.now().strftime("%Y-%m-%d")
        threads = [self._thread("9301", "Docker 容器端口映射失败求助 A"),
                   self._thread("9302", "Docker 容器端口映射失败求助 B"),
                   self._thread("9303", "Docker 容器端口映射失败求助 C")]
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, posted, state_path = self._run_live(
                tmp, {"daily": {today: 99}}, threads,
                extra_env={"UGREEN_DAILY_QUOTA": "0", "UGREEN_MAX_REPLIES": "3"})
            self.assertEqual(rc, 0)
            self.assertEqual(len(posted), 3)
            self.assertIn("每日配额：不限制", out)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            # 不限制时也照常记账（99 + 3），方便以后改回限制
            self.assertEqual(saved["daily"][today], 102)

    def test_dry_run_does_not_consume_quota(self):
        """演练模式既不消耗配额，也不受配额限制。"""
        today = datetime.now().strftime("%Y-%m-%d")
        threads = [self._thread("9401", "Docker 容器端口映射失败求助 A")]
        state_path = Path(tempfile.mkdtemp()) / "ugreen_state.json"
        state_path.write_text(json.dumps({"daily": {today: 10}},
                                         ensure_ascii=False), encoding="utf-8")
        env = {"AI_API_KEY": "test-key",
               "AI_BASE_URL": "http://127.0.0.1:9/v1",
               "UGREEN_DRY_RUN": "1", "UGREEN_NOTIFY": "0",
               "UGREEN_STATE_DIR": str(state_path.parent),
               "UGREEN_SEARCH": "0", "UGREEN_DAILY_QUOTA": "10"}
        argv = ["ugreen_ai_reply.py", "--hours", "48", "--per-forum", "10",
                "--no-notify", "--no-mark"]
        buf = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
                patch.object(m.UgnasClient, "fetch_rss", return_value=threads), \
                patch.object(m.UgnasClient, "fetch_guide", return_value=threads), \
                patch.object(m.UgnasClient, "fetch_detail",
                             side_effect=lambda t: t), \
                patch.object(m.requests, "post", side_effect=self._fake_ai), \
                patch.object(sys, "argv", argv), \
                redirect_stdout(buf):
            rc = m.main()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        # 演练照常给出拟回复，不被配额拦下
        self.assertIn("演练：未发送", out)
        self.assertIn("演练不占用配额", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
