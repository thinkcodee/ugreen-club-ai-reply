"""绿联 NAS 社区自动回复 —— 第三方依赖引导。

青龙容器里一般能 pip；可用 UGREEN_AUTO_INSTALL_DEPS=0 关闭自动安装。
作为模块被导入（如单元测试）时，缺依赖会如实抛错，不擅自安装。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys


def _install_deps(packages: list[str]) -> bool:
    print(f"[依赖] 缺少 {', '.join(packages)}，尝试自动安装 ...", flush=True)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q",
             "--disable-pip-version-check", *packages],
            timeout=300,
        )
        importlib.invalidate_caches()
        print("[依赖] 安装完成", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[依赖] 自动安装失败：{exc}", flush=True)
        print("[依赖] 请手动安装：pip install -r requirements.txt", flush=True)
        return False


try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    if (__name__ == "__main__"
            and os.environ.get("UGREEN_AUTO_INSTALL_DEPS", "1").strip() != "0"):
        if not _install_deps(["requests", "beautifulsoup4"]):
            sys.exit(1)
        import requests
        from bs4 import BeautifulSoup
    else:  # 作为模块被导入（如单元测试）时，让错误如实抛出
        raise

try:
    import lxml  # noqa: F401
    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"
