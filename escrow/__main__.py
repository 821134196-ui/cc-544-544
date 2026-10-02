"""命令行入口：

  python -m escrow demo   重置种子数据、启动服务并打开浏览器（拒收/延期样例）
  python -m escrow serve  不清空数据，直接启动
  python -m escrow test   运行全部测试（资金守恒/回执重放/验货时限/权限隔离）
"""

from __future__ import annotations

import sys
import unittest


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "demo"

    if cmd == "demo":
        from .demo import run_demo
        run_demo()
        return 0
    if cmd == "serve":
        from .web import serve
        port = int(argv[1]) if len(argv) > 1 else 8000
        serve("data/escrow.db", "photos", port=port)
        return 0
    if cmd == "test":
        loader = unittest.TestLoader()
        suite = loader.discover("tests", pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    print(f"未知命令: {cmd}\n")
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
