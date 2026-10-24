"""命令行的更新入口：python run_update.py check | apply | status | rollback

和 run_web.py 一样，先把 src 放进 sys.path：服务器上是 pip install -r requirements.txt，
btrader 这个包本身没有装进 venv，所以「python -m btrader.update_cli」在服务器上根本
跑不起来（找不到模块）。装了包的环境用 bitrader update 也行，两条路都走 update_cli.main。

    sudo bash deploy/update.sh              # 检查 → 快进 → 装依赖 → 重启 → 确认
    sudo bash deploy/update.sh check        # 只看差几个提交
    sudo bash deploy/update.sh status       # 上一次更新做到哪一步
    sudo bash deploy/update.sh rollback --yes
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main(argv: list[str] | None = None) -> int:
    from btrader.update_cli import main as update_main

    return update_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
