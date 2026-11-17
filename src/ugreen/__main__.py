"""支持 `python -m ugreen` 直接运行（等价于 python ugreen_ai_reply.py）。"""

from __future__ import annotations

import sys

from .runtime.cli import main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
