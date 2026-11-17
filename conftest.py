"""让 pytest / unittest 能直接从源码树导入 src/ugreen 包。

src 布局下，包不在仓库根，需要显式把 src 加入 sys.path。
装成包（pip install -e .）后则不需要这一步。
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
