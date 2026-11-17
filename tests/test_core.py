#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线单测：不联网、不发帖、不调模型。"""

import io
import json
import os
import re
import sys
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
               "UGREEN_PER_FORUM_LIMIT": "1"}
        argv = ["ugreen_ai_reply.py", "--hours", "48", "--per-forum", "1",
                "--limit", "3", "--no-notify", "--no-mark"]
        buf = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
                patch.object(m.UgnasClient, "fetch_rss", return_value=threads), \
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
