#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 ugreen/ 包打包成单个自包含的 ugreen_ai_reply.py。

为什么要打包：青龙面板既支持「订阅拉库」（整仓库拉下来），
也支持只下载一个 .py 文件直接放进 scripts 目录。后者要求脚本自包含、
不能依赖同目录下的其它模块。本脚本把包内各模块按依赖顺序内联成一个文件，
产物与仓库根目录的 ugreen_ai_reply.py 完全等价。

用法：
    python tools/build_single.py            # 生成 ugreen_ai_reply.py
    python tools/build_single.py --check    # 只校验是否已是最新（CI 用）
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "ugreen"
OUT = ROOT / "ugreen_ai_reply.py"


def pkg_version() -> str:
    """从包源码里读版本号（唯一真相源是 src/ugreen/__init__.py）。"""
    src = (PKG / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', src, re.M)
    if not m:
        raise SystemExit("在 src/ugreen/__init__.py 里找不到 __version__ 定义")
    return m.group(1)


# 内联顺序 = 依赖拓扑序（被依赖的在前）
ORDER = [
    "core/deps.py",
    "core/constants.py",
    "util/envutil.py",
    "util/textutil.py",
    "util/timeutil.py",
    "util/aiutil.py",
    "core/models.py",
    "forum/classify.py",
    "core/config.py",
    "forum/client.py",
    "search/providers.py",
    "ai/quality.py",
    "ai/client.py",
    "forum/revisit.py",
    "forum/state.py",
    "runtime/notify.py",
    "runtime/report.py",
    "runtime/cli.py",
]

HEADER = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""绿联 NAS 社区 · AI 自动回复（青龙面板 / 本地均可运行）。

本文件由 tools/build_single.py 自动生成 —— 请勿直接修改，
要改逻辑请改 ugreen/ 包内对应模块，然后重新运行该脚本。

主要能力：
  1. 抓帖走论坛导读（forum.php?mod=guide&view=newthread，不区分板块、按发布时间倒序），
     也可退回按板块 RSS 抓取；列表页仅作兜底且限定在 #threadlisttableid 容器内，
     避免把页脚的制度帖误当真实发帖。
  2. 由 OpenAI 兼容的大模型读标题 + 正文后生成/决定是否回复，而非随机灌固定语料。
  3. 以解析 Discuz 响应里的成功/失败标记判定发送结果，不只看 HTTP 200。
  4. 企业微信推送正确区分 sendNotify / QYWX_KEY / QYWX_AM 三种渠道。
  5. 状态持久化去重、黑名单、限流、dry-run 默认开启。
  6. A：把帖子里已有的楼层回复一起喂给模型，避免重复回答 / 识别已解决帖。
  7. B：保守模式——没有证据不得下确定性结论、不得编造版本号与参数；
     不确定时只追问一个关键信息或给排查方向。
  8. C：可插拔检索层，默认查询绿联官方知识库（support.ugnas.com 同源公开只读接口），
     也可叠加 Tavily / Bing / 百度 / SearXNG / 站内搜索 / 自定义端点，
     把检索到的片段作为证据注入 prompt，并要求回复标注引用了哪些证据。

默认只演练不发帖。确认无误后设置 UGREEN_DRY_RUN=0 才会真正回复。

版本：{version}

许可证：MIT License © 2026 thinkcodee
（单文件版内附许可证全文见文件末尾）
"""
'''


def header_text() -> str:
    """生成带版本号的文件头。"""
    return HEADER.replace("{version}", pkg_version())


def collapse_blank_lines(lines: list[str], max_blank: int = 2) -> list[str]:
    """把连续空行压到最多 max_blank 行。

    拼进单文件后，模块头部原本的 import 区（连同它周围的空行）会被整体
    搬走，源码里"docstring → import → 代码"的排版就断了，会留下好几行
    空白。这里统一收敛：顶层定义之间保留 2 个空行（PEP 8），其余不留。
    """
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if ln.strip() == "":
            blanks += 1
            if blanks > max_blank:
                continue
        else:
            blanks = 0
        out.append(ln)
    # 去掉首尾多余空行
    while out and out[0].strip() == "":
        out.pop(0)
    while out and out[-1].strip() == "":
        out.pop()
    return out


def strip_module(path: Path) -> tuple[list[str], list[str]]:
    """返回 (顶层 import 语句, 其余代码)。

    模块 docstring 不能直接留（单文件里只能有一个模块级 docstring），
    但内容有用 —— 转成注释块保留，这样单文件里仍能看出每段代码的来路。
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()

    imports: list[str] = []
    body_lines: list[str] = []
    skip_lines: set[int] = set()
    docstring: str = ""

    for node in tree.body:
        # 模块 docstring：转成注释保留
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and node is tree.body[0]):
            docstring = node.value.value.strip()
            skip_lines.update(range(node.lineno, node.end_lineno + 1))
            continue
        if isinstance(node, ast.ImportFrom):
            # from __future__ / 任何层级的相对导入：都不进正文
            # （单文件里所有名字都在同一模块，跨模块引用天然可见）
            if node.module == "__future__" or node.level >= 1:
                skip_lines.update(range(node.lineno, node.end_lineno + 1))
                continue
            imports.extend(lines[node.lineno - 1:node.end_lineno])
            skip_lines.update(range(node.lineno, node.end_lineno + 1))
            continue
        if isinstance(node, ast.Import):
            imports.extend(lines[node.lineno - 1:node.end_lineno])
            skip_lines.update(range(node.lineno, node.end_lineno + 1))
            continue

    # 被跳过的块（docstring / import）上方的连续空行也要一并丢弃，
    # 否则 import 之间的空行会残留在单文件里，堆成大片空白。
    for ln in sorted(skip_lines):
        j = ln - 1
        while j >= 1 and j not in skip_lines and lines[j - 1].strip() == "":
            skip_lines.add(j)
            j -= 1

    for i, ln in enumerate(lines, 1):
        if i in skip_lines:
            continue
        body_lines.append(ln)

    body_lines = collapse_blank_lines(body_lines)

    if docstring:
        # 模块说明与代码之间留 2 个空行（PEP 8 顶层定义间距）
        head = [f"# {d}" for d in docstring.splitlines()]
        body_lines = head + ["", ""] + body_lines

    return imports, body_lines


def normalize_import(line: str) -> str:
    """把多行括号 import 归一化，用于去重。"""
    return re.sub(r"\s+", " ", line).strip().rstrip(",")


def build() -> str:
    all_imports: list[str] = []
    chunks: list[str] = []

    for name in ORDER:
        path = PKG / name
        if not path.exists():
            raise SystemExit(f"缺少模块：{path}")
        imports, body = strip_module(path)
        all_imports.extend(imports)
        text = "\n".join(body).strip("\n")
        # 包内 SCRIPT_DIR 指向仓库根（src/ugreen/core/constants.py 上溯 3 级），
        # 打成单文件后应指向脚本所在目录
        text = text.replace("Path(__file__).resolve().parents[3]",
                            "Path(__file__).resolve().parent")
        chunks.append(f"\n# ===== {name[:-3]} =====\n{text}")

    # 去重 import（按整条语句归一化）
    seen: set[str] = set()
    uniq: list[str] = []
    for imp in all_imports:
        key = normalize_import(imp)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(imp)

    imports_block = "\n".join(uniq)
    # from __future__ 必须紧跟 docstring，单独提到最前
    future = [i for i in uniq if "from __future__" in i]
    rest = [i for i in uniq if "from __future__" not in i]
    imports_block = "\n".join(future + rest)

    tail = '''

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\\n已中断")
        sys.exit(130)
'''

    # __version__ 放在模块 docstring 之后、import 之前，
    # 让单文件与包对外暴露同样的版本号（测试会校验三处一致）。
    version_line = f'\n__version__ = "{pkg_version()}"\n'

    return (header_text()
            + "\nfrom __future__ import annotations\n"
            + version_line
            + "\n"
            + imports_block + "\n"
            + "\n\n".join(chunks)
            + tail
            + license_block())


def license_block() -> str:
    """把 LICENSE 全文附到单文件末尾。

    单文件是用户实际拿到手的东西（青龙订阅只下一个 .py），
    附上许可证全文，MIT 的「保留版权声明」要求才算满足。
    """
    lic = ROOT / "LICENSE"
    if not lic.exists():
        return ""
    text = lic.read_text(encoding="utf-8").rstrip("\n")
    body = "\n".join(f"# {ln}" if ln.strip() else "#"
                     for ln in text.splitlines())
    return ("\n\n# " + "=" * 66 + "\n"
            "# 许可证（MIT License）—— 完整文本见仓库根目录的 LICENSE 文件\n"
            "# " + "=" * 66 + "\n"
            + body + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="打包成单文件")
    ap.add_argument("--check", action="store_true",
                    help="只校验根目录的 ugreen_ai_reply.py 是否已是最新")
    args = ap.parse_args()

    content = build()

    if args.check:
        cur = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if cur != content:
            print("[构建] 单文件已过期，请运行：python tools/build_single.py")
            return 1
        print("[构建] 单文件已是最新")
        return 0

    OUT.write_text(content, encoding="utf-8")
    print(f"[构建] 已生成 {OUT.name}（{len(content.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
