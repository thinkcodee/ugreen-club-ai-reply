"""GitHub API 发布脚本：推送 commit -> 附注 tag -> release + 资产上传。"""
import base64
import os
import sys

import requests

REPO = "thinkcodee/ugreen-club-ai-reply"
API = f"https://api.github.com/repos/{REPO}"
TOKEN = os.environ["GHTOKEN"]
H = {"Authorization": f"Bearer {TOKEN}",
     "Accept": "application/vnd.github+json"}


def api(method, path, **kw):
    r = requests.request(method, API + path, headers=H, timeout=60, **kw)
    if r.status_code >= 300:
        print(f"[FAIL] {method} {path} -> {r.status_code}: {r.text[:300]}")
        sys.exit(1)
    return r.json() if r.text else {}


def main():
    base_sha = api("GET", "/git/ref/heads/main")["object"]["sha"]
    print("remote main =", base_sha)

    files = ["pyproject.toml", "src/ugreen/__init__.py", "ugreen_ai_reply.py"]
    entries = []
    for f in files:
        content = base64.b64encode(open(f, "rb").read()).decode()
        sha = api("POST", "/git/blobs",
                  json={"content": content, "encoding": "base64"})["sha"]
        entries.append({"path": f, "mode": "100644", "type": "blob", "sha": sha})
        print("blob", f, sha[:10])

    base_tree = api("GET", f"/git/commits/{base_sha}")["tree"]["sha"]
    tree = api("POST", "/git/trees",
               json={"base_tree": base_tree, "tree": entries})["sha"]
    print("tree =", tree[:10])

    commit = api("POST", "/git/commits", json={
        "message": "chore: release v1.3.1",
        "tree": tree,
        "parents": [base_sha],
    })
    print("commit =", commit["sha"][:10])

    api("PATCH", "/git/refs/heads/main",
        json={"sha": commit["sha"], "force": False})
    print("main ->", commit["sha"][:10])

    tag = api("POST", "/git/tags", json={
        "tag": "v1.3.1",
        "message": "v1.3.1：思考模式默认开启；剥掉正文里的证据编号与图片占位符",
        "object": commit["sha"],
        "type": "commit",
        "tagger": {"name": "thinkcodee", "email": "1449688523@qq.com",
                   "date": "2026-11-18T10:35:00+08:00"},
    })
    print("tag object =", tag["sha"][:10])

    api("POST", "/git/refs", json={"ref": "refs/tags/v1.3.1",
                                   "sha": tag["sha"]})
    print("refs/tags/v1.3.1 ->", tag["sha"][:10])

    rel = api("POST", "/releases", json={
        "tag_name": "v1.3.1",
        "name": "v1.3.1",
        "body": RELEASE_NOTES,
        "draft": False,
        "prerelease": False,
    })
    print("release id =", rel["id"])

    upload = rel["upload_url"].split("{")[0]
    with open("ugreen_ai_reply.py", "rb") as fp:
        r = requests.post(
            upload,
            params={"name": "ugreen_ai_reply.py"},
            headers={**H,
                     "Content-Type": "text/x-python"},
            data=fp.read(), timeout=120)
    r.raise_for_status()
    asset = r.json()
    print("asset:", asset["name"], asset["size"], "bytes",
          "state=", asset["state"])


RELEASE_NOTES = """## v1.3.1

### 修复

- **思考模式默认开启**：请求显式带 `enable_thinking=true`（原先按模型名自动关闭，Xing / Qwen3 等思考型模型会被静默关掉思考）。实测开启思考后回复质量明显更好；`AI_DISABLE_THINKING=1` 可显式关闭，端点不认该参数时仍会自动去掉并重试。
- **剥掉泄漏进正文的证据编号**：模型偶尔把 `kb:919` / `web:ab12…` 这类证据编号原样写进回复正文，发帖前现在一律自动剥离；引用关系只走内部 `evidence_used` 字段。
- **修复「没有图片的图片」**：`[图片]` / `[示意图]` 是论坛编辑器的附件占位符，直接发出去并不会渲染成图片。已从默认图文白名单移除（旧配置里写了也会被过滤），模型写进正文时同样会被剥掉；白名单只保留真正的论坛表情码（`[赞] [加油] [鼓掌] [哈哈] [谢谢] [握手]`）。

### 下载

直接使用附件 `ugreen_ai_reply.py`（青龙用户唯一需要的文件），版本号 `1.3.1`。
"""


if __name__ == "__main__":
    main()
