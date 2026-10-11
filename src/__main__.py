"""让 `python -m src` 也能直接跑。

`python -m src.cli` 一直是标准用法，但少打一层更符合直觉，
也让 `pip install -e .` 之后暴露的 `mcd-butler` 命令有同一个入口。
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
