#!/usr/bin/env bash
# 服务器上一条命令更新：sudo bash deploy/update.sh [check|status|apply|rollback] [--json] [--yes]
#
# 真正的逻辑在 src/btrader/update_cli.py（和界面共用同一条流水线、同一份状态文件），
# 这里只做三件事：
#   1. 找到代码目录和它自己的 venv；
#   2. 以 bitrader 用户跑 —— git 工作树、.venv、数据目录、update-state.json 都是它的；
#      用 root 跑会把状态文件写成 root:root，界面（bitrader）就读不到了；
#   3. 把数据目录对齐到 systemd unit 里那一份，参数原样传过去。
#
# 为什么不在这儿另写一条流水线：install.sh 里那句硬对齐就是「两份实现」漂出来的。
# 后台要执行的东西只能有一份，否则迟早对不上。
set -Eeuo pipefail

APP_DIR="${BITRADER_APP_DIR:-/opt/bitrader}"
APP_USER="${BITRADER_APP_USER:-bitrader}"
UNIT_FILE="${BITRADER_UNIT_FILE:-/etc/systemd/system/bitrader.service}"
PY="$APP_DIR/.venv/bin/python"
ENTRY="$APP_DIR/run_update.py"

die() { printf '\n[失败] %s\n' "$*" >&2; exit 1; }

[ -x "$PY" ] || die "找不到 $PY：先按 docs/DEPLOY-Linux.md 装一遍（sudo bash deploy/install.sh）"
[ -f "$ENTRY" ] || die "找不到 $ENTRY：代码不在 $APP_DIR？用 BITRADER_APP_DIR=... 指定"

# 数据目录必须和服务一致：更新状态、跨进程锁、重启确认全落在它里面。
# unit 里那一行是权威值，install.sh 装的时侯写进去的。
DATA_DIR=""
if [ -r "$UNIT_FILE" ]; then
  DATA_DIR="$(sed -n 's/^Environment=BITRADER_DATA_DIR=//p' "$UNIT_FILE" | head -n 1)"
fi

if [ "$(id -u)" -eq 0 ]; then
  id -u "$APP_USER" >/dev/null 2>&1 || die "没有 $APP_USER 这个用户：这台机器还没装过 biTrader 服务"
  # -H：把 HOME 也切到服务用户，别让 root 的 HOME 漏进路径兜底逻辑。
  if [ -n "$DATA_DIR" ]; then
    exec sudo -H -u "$APP_USER" env "BITRADER_DATA_DIR=$DATA_DIR" "$PY" "$ENTRY" "$@"
  fi
  exec sudo -H -u "$APP_USER" "$PY" "$ENTRY" "$@"
fi

# 已经是服务用户（或者有人直接 ./deploy/update.sh）就直跑，不折腾 sudo。
exec "$PY" "$ENTRY" "$@"
