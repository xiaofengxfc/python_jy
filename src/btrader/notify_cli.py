"""Send a one-off WeChat push from the command line.

The app reports its own events (成交、熔断、保护单、自动交易启停、回测完成). This
covers everything *outside* the app: a backtest run in a terminal, a deploy
script, a long agent task. It reuses the saved settings, so there is still one
place to configure the channel and one place to look when the phone stays quiet.

Usage::

    python -m btrader.notify_cli --title 任务完成 "回测 500 根完成"
    python -m btrader.notify_cli "部署脚本失败：退出码 1"

Exit code is 0 when the gateway accepted the message, 1 when it did not, 2 on a
usage error — so a script can chain it with ``||`` and still be honest.
"""

from __future__ import annotations

import argparse
import sys

from .core.config import ConfigStore
from .notify import NotifyError, WeChatNotifier, spool_message

USAGE_HINT = '需要一条消息：btrader-notify --title 任务完成 "回测结束"'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btrader-notify",
        description="用已保存的微信设置发送一条通知。",
    )
    parser.add_argument("message", nargs="?", default="", help="正文")
    parser.add_argument("--title", default="", help="标题，显示为【标题】")
    parser.add_argument(
        "--line",
        action="append",
        default=[],
        help="附加一行（可重复），例如 --line 'win rate 54%%'",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="等待网关的秒数（默认 90，实测一次发送约 30 秒）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.message.strip() and not args.title.strip():
        print(USAGE_HINT, file=sys.stderr)
        return 2

    settings = ConfigStore().settings.notify
    if args.title.strip():
        text = WeChatNotifier.format_task(
            args.title.strip(), [args.message.strip(), *args.line]
        )
    else:
        text = "\n".join([args.message.strip(), *args.line])

    notifier = WeChatNotifier(settings, timeout=args.timeout, queue_size=1)
    try:
        notifier.send_now(text)
    except NotifyError as exc:
        # This process is about to exit, so an unsent report would be lost.
        # Leave it on disk: the app's worker replays the spool once the channel
        # is back (a re-scan, a restarted gateway, a flaky network).
        waiting = spool_message(text)
        hint = f"（已暂存 {waiting} 条，恢复后自动补发）" if waiting else ""
        print(f"发送失败：{exc}{hint}", file=sys.stderr)
        return 1
    print("已发送")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
