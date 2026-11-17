"""绿联 NAS 社区自动回复 —— 推送渠道：sendNotify / 群机器人 / 应用消息。"""

from __future__ import annotations

from pathlib import Path
import importlib.util
import json
import os

from ..core.constants import (
    SCRIPT_DIR,
)

from ..util.envutil import (
    env_str,
    log,
)
from ..util.textutil import (
    truncate_bytes,
)

def notify_via_sendnotify(title: str, content: str) -> bool:
    candidates: list[Path] = []
    ql_dir = env_str("QL_DIR")
    if ql_dir:
        candidates += [Path(ql_dir) / "sendNotify.py",
                       Path(ql_dir) / "scripts" / "sendNotify.py"]
    candidates += [Path("/ql/sendNotify.py"), Path("/ql/scripts/sendNotify.py"),
                   SCRIPT_DIR / "sendNotify.py"]
    for p in candidates:
        if not p.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location("_ql_sendnotify", str(p))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, "send", None)
            if not fn:
                continue
            try:
                fn(title, content)
            except TypeError:
                fn(title, content, "")
            log("已通过青龙 sendNotify 推送")
            return True
        except Exception as exc:  # noqa: BLE001
            log(f"sendNotify 调用失败（{p}）：{exc}")
    return False


def notify_via_qywx_key(title: str, content: str) -> bool:
    key = env_str("QYWX_KEY")
    if not key:
        return False
    try:
        r = requests.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}",
            json={"msgtype": "text",
                  "text": {"content": truncate_bytes(f"{title}\n\n{content}", 1900)}},
            timeout=20)
        ok = r.json().get("errcode") == 0
        log("企业微信机器人推送" + ("成功" if ok else f"失败：{r.text[:160]}"))
        return ok
    except Exception as exc:  # noqa: BLE001
        log(f"企业微信机器人推送异常：{exc}")
        return False


def notify_via_qywx_am(title: str, content: str) -> bool:
    """青龙 QYWX_AM 标准格式：corpid,corpsecret,touser,agentid（可含第 5 段）。"""
    am = env_str("QYWX_AM")
    if not am:
        return False
    parts = [x.strip() for x in am.split(",")]
    if len(parts) < 4:
        return False
    corpid, secret, touser, agentid = parts[0], parts[1], parts[2], parts[3]
    # 兼容部分脚本写反了的顺序：agentid 通常是纯数字，touser 常为 @all / 账号名
    if agentid.isdigit() and not touser.isdigit():
        touser, agentid = agentid, touser
    try:
        tr = requests.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                          params={"corpid": corpid, "corpsecret": secret},
                          timeout=20).json()
        token = tr.get("access_token")
        if not token:
            return False
        r = requests.post(
            "https://qyapi.weixin.qq.com/cgi-bin/message/send",
            params={"access_token": token}, timeout=20,
            json={"touser": touser or "@all", "msgtype": "text",
                  "agentid": agentid,
                  "text": {"content": truncate_bytes(f"{title}\n\n{content}", 1900)}})
        ok = r.json().get("errcode") == 0
        log("企业微信应用消息推送" + ("成功" if ok else f"失败：{r.text[:160]}"))
        return ok
    except Exception as exc:  # noqa: BLE001
        log(f"企业微信应用消息推送异常：{exc}")
        return False


def send_notification(title: str, content: str) -> None:
    if notify_via_sendnotify(title, content):
        return
    if notify_via_qywx_key(title, content):
        return
    if notify_via_qywx_am(title, content):
        return
    log("未找到可用推送渠道（sendNotify / QYWX_KEY / QYWX_AM），仅打印日志")
