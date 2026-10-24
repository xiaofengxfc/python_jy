"""服务器上一条命令更新：check / status / apply / rollback。

两种跑法，默认自动选：

* **在线**（服务活着）：把活交给服务（POST /api/update/apply），终端只是把
  /api/update/status 里的步骤一行行念出来。闸（引擎在跑 → 409、工作树脏、分叉）、
  状态文件、界面看到的进度，和点「立即更新」完全一致 —— 同一份代码，没有第二套判断。
* **离线**（/api/health 不通，或者显式 --offline）：在本进程里跑同一条流水线
  （updater.run_job_sync）。引擎跑在服务进程里，服务没起来就不可能有引擎在跑，
  所以这条路上不存在「把正在下单的进程重启掉」的风险。

退出码：0 成功 / 1 失败 / 2 前置条件不满足（拒绝执行，什么都没动）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import paths
from .core import updater

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2

HTTP_TIMEOUT = 10.0
POLL_SECONDS = 2.0
# 界面那条路一次更新最坏能跑二十多分钟（pip 900 秒 + 构建 900 秒，各有超时）。
# CLI 等的时候按这个上限收；超了不当成失败，只说清任务还在跑、怎么看它。
WATCH_SECONDS = 45 * 60.0


class CliError(RuntimeError):
    """要翻成一行中文 + 退出码的错误，不是 bug。"""


# ------------------------------------------------------------------ 连服务
def _service_port() -> int:
    """服务监听哪个端口：读 systemd unit 里的 --port（install.sh 按实际端口写的）。

    读不到就用 updater 的兜底值。端口跟着真实进程走这件事在 updater 里已经栽过一次
    （写死 8765 的实例更新完就再也起不来），这里照着读，别再写死。
    """
    for folder in ("/etc/systemd/system", "/lib/systemd/system", "/usr/lib/systemd/system"):
        try:
            text = (Path(folder) / f"{updater.SERVICE_NAME}.service").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
        match = re.search(r"--port[ =](\d{1,5})", text)
        if match:
            port = int(match.group(1))
            if 1 <= port <= 65535:
                return port
    return updater.web_port()


def _base_url() -> str:
    return f"http://127.0.0.1:{_service_port()}"


def _request(path: str, *, payload: Any = None, timeout: float = HTTP_TIMEOUT) -> dict[str, Any]:
    url = _base_url() + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if payload is not None else "GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8", errors="replace"))
            detail = str(body.get("detail") or "")
        except Exception:  # noqa: BLE001 - 读不出 detail 就退回状态码
            detail = ""
        raise CliError(detail or f"{url} 回了 HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CliError(f"连不上 {url}：{reason}") from exc


def _unit_data_dir() -> str:
    """systemd unit 里那份 BITRADER_DATA_DIR（install.sh 按实际数据目录写的）。"""
    for folder in ("/etc/systemd/system", "/lib/systemd/system", "/usr/lib/systemd/system"):
        try:
            text = (Path(folder) / f"{updater.SERVICE_NAME}.service").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
        match = re.search(r"^Environment=BITRADER_DATA_DIR=(\S+)", text, re.M)
        if match:
            return match.group(1)
    return ""


def _data_dir_problem() -> str:
    """数据目录必须和服务一样：更新状态、跨进程锁、重启确认都落在它里面。

    不一样的时候两条流水线互相看不见：锁形同不存在，界面读不到这次更新，
    「新进程确认重启」也会落到另一个目录上。多半是有人直接 python run_update.py，
    没有把 unit 里那个 BITRADER_DATA_DIR 带上。
    """
    wanted = _unit_data_dir()
    if not wanted:
        return ""
    current = paths.data_dir()
    try:
        same = current.resolve() == Path(wanted).resolve()
    except OSError:
        same = False
    if same:
        return ""
    return (
        f"数据目录不一样：unit 里是 {wanted}，这个进程用的是 {current}。"
        "更新状态与锁都在数据目录里，两边不一样等于两条流水线互相看不见。"
        f"先 export BITRADER_DATA_DIR={wanted} 再跑（sudo bash deploy/update.sh 会自动带上）。"
    )


def _service_online() -> bool:
    try:
        _request(updater.HEALTH_PATH, timeout=2.0)
    except CliError:
        return False
    return True


# ------------------------------------------------------------------ 打印
def _print_step(step: dict[str, Any]) -> None:
    mark = "ok" if step.get("ok") else "!!"
    seconds = step.get("seconds")
    took = f"  {seconds:5.1f}s" if isinstance(seconds, (int, float)) else ""
    print(f"  [{mark}] {step.get('name') or '?'}{took}")
    for line in str(step.get("output") or "").splitlines():
        print(f"         {line}")


def _print_state(state: dict[str, Any]) -> None:
    print(f"任务：{state.get('job_id') or '-'}（{state.get('kind') or '-'}）")
    print(f"状态：{state.get('state') or updater.STATE_IDLE}")
    for key, label in (("started_at", "开始"), ("finished_at", "结束")):
        if state.get(key):
            print(f"{label}：{state[key]}")
    if state.get("from_sha"):
        print(f"从：{str(state['from_sha'])[:7]}  →  {str(state.get('to_sha') or '-')[:7]}")
    if state.get("error"):
        print(f"原因：{state['error']}")
    if state.get("manual_hint"):
        print(f"手动拉起：{state['manual_hint']}")
    for step in state.get("steps") or []:
        _print_step(step)


def _source_text(source: Any) -> str:
    text = str(source or "")
    if text == "github":
        return "GitHub API"
    if text:
        return "git ls-remote（没有提交标题）"
    return "读不到来源"


def _info(args: argparse.Namespace) -> dict[str, Any]:
    """本地与远端的差距：服务在跑就问它，否则自己数。"""
    if not getattr(args, "offline", False) and _service_online():
        try:
            return _request("/api/update/info?refresh=1", timeout=60.0)
        except CliError as exc:
            print(f"（问服务要信息失败，改自己数：{exc}）", file=sys.stderr)
    return updater.collect_info(refresh=True)


# ------------------------------------------------------------------ 前置检查
def _preflight(info: dict[str, Any]) -> str:
    """离线跑之前自己过一遍闸。返回空串表示可以走。"""
    if updater.is_desktop_build():
        return "这是桌面版（冻结包）：里面没有 .git，更新走 Releases 里的安装包。"
    if not info.get("supported"):
        return "这个目录不支持在线更新：" + str(info.get("error") or "不是 git 仓库（装的时候用了 rsync？）")
    dirty, why = updater.work_tree_state()
    if dirty:
        head = "\n  ".join(dirty[:20])
        more = "" if len(dirty) <= 20 else f"\n  ……（共 {len(dirty)} 个）"
        return f"工作树有没提交的改动，先 commit 或 stash 再更新：\n  {head}{more}"
    if why:
        return f"读不出工作树状态：{why}"
    holder = updater.lock_holder()
    if holder:
        return f"另一个更新正在跑（pid {holder.get('pid')}，任务 {holder.get('job_id') or '?'}），等它结束再来。"
    blocked = updater.refusal(info)
    if blocked:
        return blocked[1]
    return ""


# ------------------------------------------------------------------ 子命令
def cmd_check(args: argparse.Namespace) -> int:
    info = _info(args)
    if getattr(args, "json_out", False):
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return EXIT_OK
    if not info.get("supported"):
        desktop = info.get("desktop") or {}
        print("这是桌面版：更新走 Releases 里的安装包，不在服务器上跑流水线。")
        if desktop:
            print(f"  当前：{desktop.get('current') or '-'}    最新：{desktop.get('latest') or '（查不到）'}")
            if desktop.get("download_url"):
                print(f"  下载：{desktop['download_url']}")
            if desktop.get("error"):
                print(f"  注意：{desktop['error']}")
        return EXIT_OK
    print(f"本机：{info.get('current_short') or '-'}（分支 {info.get('branch') or '-'}）")
    print(f"远端：{info.get('remote_short') or '-'}（{_source_text(info.get('source'))}）")
    behind, ahead = info.get("behind_by"), info.get("ahead_by")
    if behind is None or ahead is None:
        print(f"差距：数不出来 —— {info.get('error') or '没有原因'}")
    else:
        print(f"差距：落后 {behind} 个提交，领先 {ahead} 个")
    for commit in info.get("commits") or []:
        first = str(commit.get("message") or "").strip().splitlines()
        print(f"  {str(commit.get('sha') or '')[:7]}  {first[0] if first else ''}")
    if info.get("error") and (behind is None or ahead is None):
        print(f"注意：{info['error']}")
    blocked = updater.refusal(info)
    print(f"现在不能更新：{blocked[1]}" if blocked else "可以更新：sudo bash deploy/update.sh apply")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    if not getattr(args, "offline", False) and _service_online():
        try:
            state = _request("/api/update/status")
        except CliError:
            state = updater.read_state()
    else:
        state = updater.read_state()
    if getattr(args, "json_out", False):
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return EXIT_OK
    _print_state(state)
    return EXIT_OK


def cmd_apply(args: argparse.Namespace) -> int:
    online = _service_online()
    if args.offline:
        if online:
            print(
                "服务正在跑：去掉 --offline，让它自己更新 —— 在线那条路才会检查「引擎在不在跑」。",
                file=sys.stderr,
            )
            print("确实要用命令行直接跑，先 systemctl stop bitrader。", file=sys.stderr)
            return EXIT_REFUSED
        return _apply_offline()
    if not online:
        print(f"服务没应答（{_base_url()}）：改走离线模式，在本进程里跑同一条流水线。")
        print("引擎跑在服务进程里，服务没起来就不存在「把正在下单的进程重启掉」。")
        return _apply_offline()
    return _apply_online()


def cmd_rollback(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "回滚会在这台机器上产生一条反向提交（不改写历史）。确认回滚就再加 --yes。",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    online = _service_online()
    if args.offline and online:
        print("服务正在跑：去掉 --offline，让服务自己处理回滚。", file=sys.stderr)
        return EXIT_REFUSED
    if online:
        try:
            answer = _request("/api/update/rollback", payload={"confirm": True})
        except CliError as exc:
            print(f"被拒绝：{exc}", file=sys.stderr)
            return EXIT_REFUSED
        print(f"回滚已经交给服务（任务 {answer.get('job_id') or '?'}）。")
        return _watch()
    return _rollback_offline()


def _apply_online() -> int:
    try:
        answer = _request("/api/update/apply", payload={})
    except CliError as exc:
        print(f"被拒绝：{exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"已经交给服务处理（任务 {answer.get('job_id') or '?'}），进度会一步步打出来。")
    return _watch()


def _watch() -> int:
    """跟着状态文件念步骤，直到 done / failed。重启那几十秒连不上是正常的。"""
    seen = 0
    deadline = time.monotonic() + WATCH_SECONDS
    offline_noted = False
    while True:
        try:
            state = _request("/api/update/status")
        except CliError:
            if not offline_noted:
                print("（服务正在重启，等它回来……）")
                offline_noted = True
            if time.monotonic() >= deadline:
                print("等太久了，自己看一眼：sudo bash deploy/update.sh status", file=sys.stderr)
                return EXIT_FAILED
            time.sleep(POLL_SECONDS)
            continue
        offline_noted = False
        steps = state.get("steps") or []
        while seen < len(steps):
            _print_step(steps[seen])
            seen += 1
        status = state.get("state")
        if status == updater.STATE_DONE:
            print("\n更新成功。")
            return EXIT_OK
        if status == updater.STATE_FAILED:
            print(f"\n更新失败：{state.get('error') or '（那一步没有输出）'}", file=sys.stderr)
            if state.get("manual_hint"):
                print(f"手动拉起：{state['manual_hint']}", file=sys.stderr)
            return EXIT_FAILED
        if time.monotonic() >= deadline:
            print(
                "任务还在跑（超过 CLI 的等待上限，但它没有停）：sudo bash deploy/update.sh status",
                file=sys.stderr,
            )
            return EXIT_FAILED
        time.sleep(POLL_SECONDS)


def _apply_offline() -> int:
    problem = _data_dir_problem()
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_REFUSED
    info = updater.collect_info(refresh=True)
    blocked = _preflight(info)
    if blocked:
        print(blocked, file=sys.stderr)
        return EXIT_REFUSED
    print(f"落后 {info.get('behind_by')} 个提交：开始更新（本进程直接跑）。")
    try:
        state = updater.run_job_sync(updater.KIND_APPLY, {}, on_step=_print_step)
    except updater.JobBusy as exc:
        print(f"被拒绝：{exc}", file=sys.stderr)
        return EXIT_REFUSED
    if state.get("state") == updater.STATE_DONE:
        print("\n更新成功。")
        return EXIT_OK
    print(f"\n更新失败：{state.get('error') or '（那一步没有输出）'}", file=sys.stderr)
    if state.get("manual_hint"):
        print(f"手动拉起：{state['manual_hint']}", file=sys.stderr)
    return EXIT_FAILED


def _rollback_offline() -> int:
    problem = _data_dir_problem()
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_REFUSED
    state = updater.read_state()
    if state.get("state") != updater.STATE_DONE:
        print(
            f"只有上一次更新成功了才能回滚（现在状态：{state.get('state') or 'idle'}）。",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    target = str(state.get("from_sha") or "")
    if not updater.is_commit_sha(target):
        print(f"记下的起始提交「{target}」不像提交 SHA，不拿它拼 git 命令。", file=sys.stderr)
        return EXIT_REFUSED
    print(f"回滚到 {target[:7]}（git revert，不改写历史）。")
    try:
        result = updater.run_job_sync(updater.KIND_ROLLBACK, {"target": target}, on_step=_print_step)
    except updater.JobBusy as exc:
        print(f"被拒绝：{exc}", file=sys.stderr)
        return EXIT_REFUSED
    if result.get("state") == updater.STATE_DONE:
        print("\n回滚成功。")
        return EXIT_OK
    print(f"\n回滚失败：{result.get('error') or '（那一步没有输出）'}", file=sys.stderr)
    if result.get("manual_hint"):
        print(f"手动拉起：{result['manual_hint']}", file=sys.stderr)
    return EXIT_FAILED


# ------------------------------------------------------------------ 入口
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bitrader update",
        description=(
            "检查 / 安装 / 回滚更新。服务在跑就交给它（和界面是同一个任务、同一份闸）；"
            "服务没起来就在本进程里跑同一条流水线。"
        ),
    )
    sub = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("check", "看本地和远端差几个提交"),
        ("status", "看上一次更新做到哪一步"),
    ):
        one = sub.add_parser(name, help=help_text)
        one.add_argument("--offline", action="store_true", help="不经过服务，直接读盘上 / 本地算")
        one.add_argument("--json", dest="json_out", action="store_true", help="只输出 JSON")
    apply_one = sub.add_parser("apply", help="更新到远端最新")
    apply_one.add_argument(
        "--offline", action="store_true", help="服务没起来时才用：本进程直接跑流水线"
    )
    rollback_one = sub.add_parser("rollback", help="把内容退回上一次更新之前的提交")
    rollback_one.add_argument("--offline", action="store_true", help="不经过服务，本进程直接回滚")
    rollback_one.add_argument("--yes", action="store_true", help="确认回滚（不加就什么都不做）")

    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_REFUSED
    handlers = {
        "check": cmd_check,
        "status": cmd_status,
        "apply": cmd_apply,
        "rollback": cmd_rollback,
    }
    try:
        return handlers[args.command](args)
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
