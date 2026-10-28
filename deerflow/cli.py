"""在加载框架运行时之前分发项目初始化命令。"""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "init":
        from deerflow.project_init import main as init_main

        return init_main(arguments[1:])

    from deerflow.tui.cli import main as terminal_main

    return terminal_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
