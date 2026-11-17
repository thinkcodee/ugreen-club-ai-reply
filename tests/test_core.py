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
               "UGREEN_PER_FORUM_LIMIT": "1",
               "UGREEN_SEARCH": "0"}   # 端到端单测保持纯离线
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
# D：检索层
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
        self.assertEqual(m.parse_search_providers("ugnas-kb, local, bogus, tavily"),
                         ("ugnas-kb", "local", "tavily"))
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

    def test_local_knowledge_base(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ops.md"
            p.write_text("# 硬盘休眠排查\n先关闭 Docker 虚拟机，再检查后台任务。\n\n"
                         "# 无关章节\n随便写点别的。\n", encoding="utf-8")
            cfg = self._cfg()
            cfg.local_kb_dir = Path(d)
            evs = m.search_local_kb("硬盘休眠", cfg)
        self.assertTrue(evs)
        self.assertIn("Docker", evs[0].snippet)
        self.assertEqual(evs[0].source, "本地知识库/ops.md")


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
        cfg = self._cfg({"UGREEN_SEARCH_PROVIDERS": "ugnas-kb,local,tavily,bogus"})
        self.assertEqual(cfg.search_providers, ("ugnas-kb", "local", "tavily"))

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
        for kw in ("上下文（A）", "保守模式", "检索（D）", "绿联知识库", "ugnas-kb"):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
