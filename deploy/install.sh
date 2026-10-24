#!/usr/bin/env bash
# biTrader 部署脚本：Debian 12 / Ubuntu 22.04（干净机器上一把装完）
#
#   sudo bash deploy/install.sh --domain trade.example.com
#   sudo bash deploy/install.sh --repo https://github.com/you/biTrader.git --domain trade.example.com
#   bash deploy/install.sh --dry-run        # 只打印要做什么，一个字节都不改
#
# 三条设计约束：
#   * 幂等：每一步先看现状，已经在位就跳过。连跑两遍不炸，不覆盖已有的 secret。
#   * 失败即停：set -Eeuo pipefail + trap ERR，报「哪一步、哪条命令、退出码」。
#   * 不碰用户数据：/var/lib/bitrader 只在不存在时创建，脚本从不删除数据目录。

set -Eeuo pipefail

APP_USER="bitrader"
APP_DIR="/opt/bitrader"
DATA_DIR="/var/lib/bitrader"
SERVICE="bitrader"
PORT="8765"
# 域名与公网 IP 的写法：和 deploy/set-domain.sh 用同一套口径，在参数这一层就收死。
# 以前 --domain 原样进 sed 的替换串：带 & / # 的输入会写出畸形配置，而 --port 写个
# 字母会直接写进 systemd 的 ExecStart，服务起不来还要翻 journalctl 才知道是参数写错。
DOMAIN_RE='^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$'
IPV4_RE='^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$'
# 域名切换脚本：install.sh 把它以 root:root 装到这里，sudoers 也只授这一条。
# **不要**放在 $APP_DIR 下面：那个目录属主是 bitrader，改得动的脚本 + NOPASSWD sudo
# 等于把 root 送出去。
HELPER_DIR="/usr/local/lib/bitrader"
HELPER="$HELPER_DIR/set-domain.sh"
SUDOERS_FILE="/etc/sudoers.d/bitrader"
DOMAIN=""
REPO_URL=""
DRY_RUN=""
WITH_CADDY="1"
ASSUME_LINUX="${BI_RUN_DRY_ANYWHERE:-}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CURRENT_STEP="启动"

say() { printf "\n== %s\n" "$*"; }
note() { printf "   %s\n" "$*"; }
die() { printf "\n[失败] %s\n" "$*" >&2; exit 1; }

on_error() {
  local code=$?
  printf "\n[失败] 第 %s 步出错：%s\n        退出码 %s。修好这一步再重跑，脚本可以反复执行。\n" \
    "$CURRENT_STEP" "$BASH_COMMAND" "$code" >&2
  exit "$code"
}
trap on_error ERR

usage() {
  cat <<'USAGE'
用法: sudo bash deploy/install.sh [选项]

  --domain <域名>   有域名就填，用于 Caddy 自动 HTTPS（会校验写法，IP 直接拒）；
                    不填只监听 127.0.0.1
  --repo <git 地址> 服务器上没有代码时从仓库拉取（默认用当前目录这份代码）
  --port <端口>     后端监听端口，默认 8765（1-65535；Caddyfile 与 set-domain.sh 都按它写）
  --no-caddy        不装 Caddy、不写 Caddyfile（你自己有 nginx 时用）
                    同时也跳过域名切换脚本那一条 sudo；「重启服务」那条照写
  --dry-run         只打印将执行的命令与写入的内容，不做任何修改
  -h, --help        这份帮助

装完之后更新代码不用再跑本脚本（它做的是整套安装）：
  sudo bash deploy/update.sh            # 检查 -> 快进 -> 装依赖 -> 重启 -> 确认服务回来
  sudo bash deploy/update.sh check      # 只看本地与远端差几个提交

本机（非 Linux）推演脚本逻辑：
  BI_RUN_DRY_ANYWHERE=1 bash deploy/install.sh --dry-run
只打命令，不执行；真机部署必须到 Debian/Ubuntu 上跑。
USAGE
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --domain) DOMAIN="${2:-}"; [ -n "$DOMAIN" ] || die "--domain 需要一个域名"; shift 2 ;;
      --repo) REPO_URL="${2:-}"; [ -n "$REPO_URL" ] || die "--repo 需要一个 git 地址"; shift 2 ;;
      --port) PORT="${2:-}"; [ -n "$PORT" ] || die "--port 需要一个端口号"; shift 2 ;;
      --no-caddy) WITH_CADDY="0"; shift ;;
      --dry-run) DRY_RUN="1"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "不认识的参数：$1（用 --help 看用法）" ;;
    esac
  done
}

# 参数校验：在动系统之前把域名、端口、仓库地址收死。它不改任何东西，所以 dry-run 也照跑。
validate_args() {
  if [ -n "$DOMAIN" ]; then
    DOMAIN="$(printf '%s' "$DOMAIN" | tr '[:upper:]' '[:lower:]')"
    case "$DOMAIN" in
      *' '*|*$'\t'*) die "--domain 里不能有空格：「$DOMAIN」" ;;
      *://*|*/*)     die "--domain 只填域名本身，不要带协议或路径：「$DOMAIN」" ;;
      *:*)           die "--domain 后面不要带端口，只填 trade.example.com 这一段：「$DOMAIN」" ;;
    esac
    if [ "${#DOMAIN}" -gt 253 ]; then
      die "--domain 最长 253 个字符，这个是 ${#DOMAIN} 个"
    fi
    if printf '%s' "$DOMAIN" | grep -Eq "$IPV4_RE"; then
      die "--domain 要填域名，不是 IP：「$DOMAIN」。证书只能签给域名，ACME 不给 IP 发证书"
    fi
    if ! printf '%s' "$DOMAIN" | grep -Eq "$DOMAIN_RE"; then
      die "--domain 写法不对：「$DOMAIN」要写成 trade.example.com 这种样子（字母、数字、连字符，结尾是 .com 这类）"
    fi
  fi
  case "$PORT" in
    ''|*[!0-9]*) die "--port 只收数字，这次给的是「$PORT」" ;;
  esac
  if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    die "--port 必须在 1-65535 之间，这次给的是 $PORT"
  fi
  # 仓库地址会拼进 run_sh 的 bash -c 字符串里，所以按白名单收：出现别的字符就当场拒。
  if [ -n "$REPO_URL" ]; then
    # 用白名单而不是黑名单：git 地址里该出现的就这几个字符，别的写法
    # （引号、分号、$、反引号、空白、反斜杠）一律当写错，不让它进 bash -c。
    if ! printf '%s' "$REPO_URL" | grep -Eq '^[A-Za-z0-9._~:/@%+,-]+$'; then
      die "--repo 里有不认识的字符：「$REPO_URL」。git 地址只用 字母 数字 . _ ~ : / @ % + , - 这几个"
    fi
    if [ "${#REPO_URL}" -gt 512 ]; then
      die "--repo 太长（${#REPO_URL} 个字符）：确认一下是不是粘错了"
    fi
  fi
}

# 所有改动系统的命令都要过 run / run_sh，--dry-run 时只打印
run() {
  if [ -n "$DRY_RUN" ]; then
    printf "[dry-run] %s\n" "$*"
    return 0
  fi
  "$@"
}

run_sh() {
  if [ -n "$DRY_RUN" ]; then
    printf "[dry-run] %s\n" "$*"
    return 0
  fi
  bash -c "$*"
}

# write_file <路径> <权限> <属主>：内容从标准输入来
write_file() {
  local path=$1 mode=$2 owner=$3
  if [ -n "$DRY_RUN" ]; then
    printf "[dry-run] 写入 %s（%s, %s）：\n" "$path" "$mode" "$owner"
    sed "s/^/           /"
    return 0
  fi
  install -m "$mode" -o "$owner" -g "$owner" /dev/stdin "$path"
}

# 本机公网 IP：先问云厂商元数据，再问公共接口，都拿不到就返回空串。
#
# 国内云上 https://api.ipify.org 经常超时（实测），而阿里云 / 腾讯云 / 华为云都能从
# 内网地址直接读到 EIP。以前只有 ipify 一条路，失败时把 REPLACE_WITH_SERVER_IP
# 原样 sed 回自己：装完的 Caddyfile 里留一段永远匹配不上的死配置，caddy validate
# 还照样通过，没人发现。现在拿不到就不写那一段（set-domain.sh 一直是这个口径）。
read_public_ip() {
  local url answer
  for url in \
    "http://100.100.100.200/latest/meta-data/eipv4" \
    "http://metadata.tencentyun.com/latest/meta-data/public-ipv4" \
    "http://169.254.169.254/latest/meta-data/public-ipv4" \
    "https://api.ipify.org"; do
    answer="$(curl -fsS --max-time 3 "$url" 2>/dev/null | tr -d '\r\n ' || true)"
    if printf '%s' "$answer" | grep -Eq "$IPV4_RE"; then
      printf '%s' "$answer"
      return 0
    fi
  done
  return 0
}

# ---------------------------------------------------------------- 1. 平台
require_platform() {
  CURRENT_STEP="1/11 平台检查"
  say "$CURRENT_STEP"
  local kernel
  kernel="$(uname -s)"
  if [ "$kernel" != "Linux" ]; then
    if [ -n "$DRY_RUN" ] && [ -n "$ASSUME_LINUX" ]; then
      note "当前内核是 $kernel，不是 Linux：按 BI_RUN_DRY_ANYWHERE 只推演，不执行任何命令"
      return 0
    fi
    die "这个脚本只在 Debian/Ubuntu 上跑，当前内核是 $kernel。别在 Windows 的 Git Bash 里部署：把仓库拷到服务器再执行（步骤见 docs/DEPLOY-Linux.md）。"
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-unknown}" in
    debian|ubuntu) note "系统：${PRETTY_NAME:-$ID}" ;;
    *) die "只支持 Debian/Ubuntu，当前是 ${ID:-未知}。其它发行版的等价步骤见 docs/DEPLOY-Linux.md。" ;;
  esac
  note "架构：$(uname -m)"
}

# ---------------------------------------------------------------- 2. root
require_root() {
  CURRENT_STEP="2/11 root 权限"
  say "$CURRENT_STEP"
  if [ "$(id -u)" -eq 0 ]; then
    note "已经是 root"
    return 0
  fi
  if [ -n "$DRY_RUN" ]; then
    note "当前不是 root：正式跑会在这里退出，请用 sudo"
    return 0
  fi
  die "要以 root 执行：sudo bash deploy/install.sh ..."
}

# ---------------------------------------------------------------- 3. Python
PYTHON_BIN="python3"
require_python() {
  CURRENT_STEP="3/11 Python 版本"
  say "$CURRENT_STEP"
  local candidate version major minor
  PYTHON_BIN=""
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
  if [ -z "$PYTHON_BIN" ]; then
    if [ -n "$DRY_RUN" ]; then
      note "没有 python3：正式跑会先 apt 装 python3 / python3-venv"
      PYTHON_BIN="python3"
      return 0
    fi
    die "没找到 python3。先 apt-get install -y python3 python3-venv 再重跑。"
  fi
  version="$("$PYTHON_BIN" -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>/dev/null || echo 0.0)"
  major="${version%%.*}"
  minor="${version##*.}"
  note "$PYTHON_BIN → $version"
  # pyproject 的 requires-python 是 >=3.10；3.11/3.12 是实测过的版本
  if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 10 ]; }; then
    if [ -n "$DRY_RUN" ]; then
      note "$version 低于 3.10：正式跑会在这里退出（装 python3.11，或换 Ubuntu 24.04）"
      return 0
    fi
    die "需要 Python 3.10 以上（pyproject 的 requires-python），当前 $version。Ubuntu 22.04 请先装 python3.11。"
  fi
  if [ "$major" -eq 3 ] && [ "$minor" -lt 11 ]; then
    note "提示：$version 能跑（>=3.10），但只在 3.11/3.12 上实测过，建议升级"
  fi
}

# ------------------------------------------------------ 4. 系统依赖（apt）
install_packages() {
  CURRENT_STEP="4/11 系统依赖"
  say "$CURRENT_STEP"
  run env DEBIAN_FRONTEND=noninteractive apt-get update
  # sudo 是必须的：程序里的「签发证书」靠 sudo -n 执行那一个 root 脚本（见第 9 步），
  # 最小化装的 Debian 镜像上是没有 sudo 的。
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git rsync python3-venv python3-pip sudo
  note "pandas / numpy / pyarrow / ccxt / uvicorn / cryptography 都有 manylinux wheel，不需要编译"
  # 时钟：币安签名容忍度很小（差 1 秒就 -1021），装的时候就把它弄对。
  # 已经有 NTP 客户端在跑（Debian 默认 systemd-timesyncd）就不动它，别为了装 chrony
  # 把人家在用的时间服务删掉。
  if systemctl is-active --quiet chrony 2>/dev/null || systemctl is-active --quiet systemd-timesyncd 2>/dev/null; then
    note "时间同步服务已经在跑，不装 chrony"
  else
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends chrony
    run systemctl enable --now chrony
    note "已装 chrony 并启动：下单签名对时间敏感，这一步别省"
  fi
}

# --------------------------------------------------- 5. 系统用户与数据目录
create_user() {
  CURRENT_STEP="5/11 系统用户与数据目录"
  say "$CURRENT_STEP"
  if id -u "$APP_USER" >/dev/null 2>&1; then
    note "用户 $APP_USER 已存在"
  else
    run useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
    note "已建系统用户 $APP_USER（不能登录，只跑服务）"
  fi
  # 数据目录只在不存在时创建；已经存在就只补属主，绝不删
  if [ -d "$DATA_DIR" ]; then
    note "数据目录已存在，保留现状：$DATA_DIR"
    run chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
  else
    run install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"
  fi
  note "数据目录：$DATA_DIR（SQLite / 配置 / 日志 / secrets.bin 都在这里）"
}

# ---------------------------------------------------------------- 6. 代码
place_code() {
  CURRENT_STEP="6/11 放置代码"
  say "$CURRENT_STEP"
  if [ -n "$REPO_URL" ]; then
    if [ -d "$APP_DIR/.git" ]; then
      note "已有代码，快进到远端最新：$APP_DIR"
      # 和 core/updater.py 同一语义：只做快进，不 reset、不 --force。本地分叉时
      # merge 会失败并停在这一步，输出里就是 git 自己说的话，不会静默丢掉本地提交。
      run_sh "git -C '$APP_DIR' fetch --depth 1 origin && git -C '$APP_DIR' merge --ff-only FETCH_HEAD"
    elif [ -n "$(ls -A "$APP_DIR" 2>/dev/null || true)" ]; then
      # 以前用 rsync 装的机器：目录里有代码但没 .git，git clone 到非空目录必失败
      # （destination path already exists and is not an empty directory）。这里补一个
      # 仓库壳再把工作树对齐到远端。这是全脚本唯一一次硬对齐，只覆盖仓库跟踪的文件；
      # 数据目录（$DATA_DIR）一个字节都不碰。
      note "$APP_DIR 里已经有代码但没有 .git（以前是 rsync / scp 装的）：补上远端再对齐"
      run_sh "git -C '$APP_DIR' init -q"
      run_sh "git -C '$APP_DIR' remote add origin '$REPO_URL' 2>/dev/null || git -C '$APP_DIR' remote set-url origin '$REPO_URL'"
      run_sh "git -C '$APP_DIR' fetch --depth 1 origin && git -C '$APP_DIR' reset --hard FETCH_HEAD"
      note "已对齐；以后在线更新（设置页那张卡，或 sudo bash deploy/update.sh）就走 git 了"
    else
      run install -d -m 755 "$APP_DIR"
      run git clone --depth 1 "$REPO_URL" "$APP_DIR"
    fi
  elif [ -f "$SRC_DIR/run_web.py" ]; then
    note "用当前这份代码：$SRC_DIR"
    run install -d -m 755 "$APP_DIR"
    # 注意：这里不拷 .git。rsync 装上来的机器没有仓库，在线更新用不了；
    # 要用更新就在这台机器上重跑一次带 --repo 的安装（第 6 步会补 .git 再对齐）。
    run rsync -a --delete \
      --exclude .git --exclude .venv --exclude node_modules \
      --exclude .devdata --exclude .probe --exclude __pycache__ \
      "$SRC_DIR/" "$APP_DIR/"
  else
    die "既没给 --repo，当前目录也不像仓库（找不到 run_web.py）：$SRC_DIR"
  fi
  note "代码目录：$APP_DIR"
}

# ------------------------------------------------------ 7. 虚拟环境与依赖
install_python_deps() {
  CURRENT_STEP="7/11 虚拟环境与 Python 依赖"
  say "$CURRENT_STEP"
  if [ -x "$APP_DIR/.venv/bin/python" ]; then
    note "虚拟环境已存在，复用：$APP_DIR/.venv"
  else
    run "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
  fi
  run "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip wheel
  run "$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
  run chown -R "$APP_USER:$APP_USER" "$APP_DIR"
  note "装完了；首次导入 pandas / ccxt 会慢几秒，属正常"
}

# ------------------------------------------------------------ 8. systemd
install_service() {
  CURRENT_STEP="8/11 systemd 服务"
  say "$CURRENT_STEP"
  if [ -f "$SRC_DIR/deploy/bitrader.service" ]; then
    sed "s/--port 8765/--port $PORT/" "$SRC_DIR/deploy/bitrader.service" \
      | sed "s#BITRADER_DATA_DIR=/var/lib/bitrader#BITRADER_DATA_DIR=$DATA_DIR#" \
      | write_file "/etc/systemd/system/$SERVICE.service" 644 root
  else
    die "找不到 deploy/bitrader.service，代码没放全？"
  fi
  run systemctl daemon-reload
  run systemctl enable "$SERVICE"
  run systemctl restart "$SERVICE"
  if [ -n "$DRY_RUN" ]; then
    note "正式跑会等 3 秒再查 systemctl is-active $SERVICE"
    return 0
  fi
  sleep 3
  if ! systemctl is-active --quiet "$SERVICE"; then
    printf "\n[失败] 服务没起来。看日志：journalctl -u %s -n 80 --no-pager\n" "$SERVICE" >&2
    exit 1
  fi
  note "服务已启动：systemctl status $SERVICE"
}

# ------------------------------------------- 9. 域名切换脚本与 sudoers
# 程序里有两处按钮要做 root 才能做的事，服务本身以 bitrader 用户跑，于是 sudoers 只授两条：
#   * 「填域名 → 签发证书」要写 /etc/caddy/Caddyfile 并 reload caddy：
#     脚本以 root:root 装到 /usr/local/lib/bitrader/（bitrader 改不了它），sudoers 授这一个脚本；
#     脚本自己校验域名、写坏就还原（见 deploy/set-domain.sh 的注释）。
#     sudoers 没法表达「只允许一个合法域名参数」，所以那一步的保险在脚本里。
#   * 「一键更新」拉完代码要重启服务：只授 `<systemctl 绝对路径> --no-block restart bitrader`
#     这一条，start / stop / 别的单元一条都不给。没有它，更新完代码服务还是旧进程。
#     `--no-block` 是程序里那条命令的一部分（core/updater.py::_restart_linux）：重启交给 systemd
#     就返回，不等本进程退出——老进程等 systemctl，systemctl 等老进程退出，而 TimeoutStopSec=20
#     一到 systemd 就 SIGKILL，两边互锁。sudoers 按命令逐字匹配，少一个参数就被拒。
install_domain_helper() {
  CURRENT_STEP="9/11 域名切换脚本与 sudoers"
  say "$CURRENT_STEP"
  # systemctl 的路径先探测：sudoers 里必须是绝对路径，写错一个字的后果是整份 sudo 不可用。
  # 探测不到（比如在 Windows 上推演）就不写这一条，只提示，不猜路径。
  local systemctl_bin=""
  local candidate
  for candidate in "$(command -v systemctl 2>/dev/null || true)" /usr/bin/systemctl /bin/systemctl; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      systemctl_bin="$candidate"
      break
    fi
  done
  if [ -z "$systemctl_bin" ]; then
    if [ -n "$DRY_RUN" ]; then
      systemctl_bin="/usr/bin/systemctl"
      note "探测不到 systemctl（这台机器不是 Linux）：按 Debian/Ubuntu 的 /usr/bin/systemctl 推演"
    else
      note "探测不到 systemctl：跳过「一键更新 → 重启服务」那条 sudo 权限"
      note "  后果：更新完代码后服务不会自己重启，命令行补一句 systemctl restart $SERVICE"
    fi
  fi
  local -a sudo_lines
  sudo_lines=(
    "# 由 deploy/install.sh 写入：只授下面这几条，都是程序里的按钮在用，别的命令一条都不给"
  )
  local granted=""
  if [ "$WITH_CADDY" = "0" ]; then
    note "按 --no-caddy 跳过签发证书那一路：不装 $HELPER，sudoers 里也不写它"
  else
    if [ ! -f "$SRC_DIR/deploy/set-domain.sh" ]; then
      die "找不到 deploy/set-domain.sh，代码没放全？"
    fi
    run install -d -m 755 -o root -g root "$HELPER_DIR"
    # 端口随安装走：set-domain.sh 读它来写 reverse_proxy。不写这份的话，--port 9000
    # 装的机器日后点一次「签发证书」就被改回 8765，页面直接 502。
    # root:root 0644 —— bitrader 只读得到、改不了（它只影响反代目标端口）。
    printf 'PORT=%s\n' "$PORT" | write_file "$HELPER_DIR/port.conf" 644 root
    run install -m 755 -o root -g root "$SRC_DIR/deploy/set-domain.sh" "$HELPER"
    note "已安装 $HELPER（root:root 755）"
    sudo_lines+=(
      "# 1) 签发证书：写 /etc/caddy/Caddyfile 再 reload caddy"
      "#    域名写法与写坏回滚由 $HELPER 自己把关（sudoers 没法只允许一个合法参数）"
      "$APP_USER ALL=(root) NOPASSWD: $HELPER"
    )
    granted="$HELPER"
  fi
  if [ -n "$systemctl_bin" ]; then
    sudo_lines+=(
      "# 2) 一键更新：代码拉完、依赖装完后重启本服务。"
      "#    只允许「--no-block restart」这一个子命令、只对 $SERVICE 这一个单元：start / stop / 其它单元都不给。"
      "#    --no-block 必须写上：程序敲的就是这一条（core/updater.py::_restart_linux），sudoers 少一个参数就拒。"
      "$APP_USER ALL=(root) NOPASSWD: $systemctl_bin --no-block restart $SERVICE"
    )
    granted="${granted:+$granted 与 }$systemctl_bin --no-block restart $SERVICE"
  fi
  if [ "${#sudo_lines[@]}" -le 1 ]; then
    note "没有要写的 sudo 权限（没有 Caddy，也探测不到 systemctl），跳过 $SUDOERS_FILE"
    return 0
  fi
  printf '%s\n' "${sudo_lines[@]}" | write_file "$SUDOERS_FILE" 440 root
  note "已写 sudoers：$SUDOERS_FILE（只授 $granted）"
  # 语法错会把 sudo 整体锁死（或者更糟：多授了东西），装完当场验一次
  if [ -n "$DRY_RUN" ]; then
    note "正式跑会执行 visudo -c -f $SUDOERS_FILE 校验语法"
    return 0
  fi
  visudo -c -f "$SUDOERS_FILE" >/dev/null || {
    rm -f "$SUDOERS_FILE"
    die "sudoers 语法没通过，已删掉这一条（否则 sudo 可能整体不可用）。检查 $SUDOERS_FILE 的写法"
  }
  note "sudoers 语法检查通过"
  note "程序里的「域名 → 签发证书」走：sudo -n $HELPER <域名>"
}

# --------------------------------------------------------------- 10. Caddy
install_caddy() {
  CURRENT_STEP="10/11 Caddy 反向代理"
  say "$CURRENT_STEP"
  if [ "$WITH_CADDY" = "0" ]; then
    note "按 --no-caddy 跳过。自己用 nginx 的话，反代到 127.0.0.1:$PORT 并转发 Upgrade 头"
    return 0
  fi
  # 官方 apt 源（Debian / Ubuntu 通用）
  run_sh "curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg"
  run_sh "curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt | tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null"
  run env DEBIAN_FRONTEND=noninteractive apt-get update
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends caddy
  run install -d -m 755 /var/log/caddy
  run chown -R caddy:caddy /var/log/caddy

  if [ -z "$DOMAIN" ]; then
    note "没给 --domain：Caddy 只监听 127.0.0.1:8080，公网访问请先配域名"
    printf "%s\n" \
      "# 生成于 deploy/install.sh（没有域名）" \
      "# 先在本地验证：ssh -N -L 8080:127.0.0.1:8080 你@服务器，然后开 http://127.0.0.1:8080" \
      "127.0.0.1:8080 {" \
      "\treverse_proxy 127.0.0.1:$PORT" \
      "}" \
      | write_file "/etc/caddy/Caddyfile" 644 root
  else
    local server_ip=""
    if [ -n "$DRY_RUN" ]; then
      server_ip="REPLACE_WITH_SERVER_IP"
    else
      server_ip="$(read_public_ip)"
    fi
    # 三处替换：域名、公网 IP、端口。端口也要换 —— 不换的话用 --port 9000 装的机器，
    # 反代还指着 8765，界面能打开、所有接口 502。
    if [ -n "$server_ip" ]; then
      note "域名：$DOMAIN，服务器公网 IP：$server_ip"
      sed -e "s/trade\.example\.com/$DOMAIN/g" \
          -e "s/REPLACE_WITH_SERVER_IP/$server_ip/g" \
          -e "s/127\.0\.0\.1:8765/127.0.0.1:$PORT/g" \
        "$SRC_DIR/deploy/Caddyfile.example" \
        | write_file "/etc/caddy/Caddyfile" 644 root
    else
      note "查不到公网 IP（云厂商元数据与 ipify 都没给）：不写「IP 访问 301 到域名」那一段"
      note "  证书不受影响；想补上就在服务器上 curl https://api.ipify.org 拿到 IP，再点一次「签发证书」"
      sed -e "s/trade\.example\.com/$DOMAIN/g" \
          -e "s/127\.0\.0\.1:8765/127.0.0.1:$PORT/g" \
          -e "/^http:\/\/REPLACE_WITH_SERVER_IP {/,/^}$/d" \
        "$SRC_DIR/deploy/Caddyfile.example" \
        | write_file "/etc/caddy/Caddyfile" 644 root
    fi
  fi
  run caddy validate --config /etc/caddy/Caddyfile
  run systemctl enable caddy
  # 刚装好时 caddy 通常已经在跑（apt 会 enable），reload 不行就 restart 兜底
  run_sh "systemctl reload caddy 2>/dev/null || systemctl restart caddy"
  note "Caddy 已就绪；证书申请日志：journalctl -u caddy -f"
}

# ------------------------------------------------ 10. 时钟、防火墙、收尾
check_clock_and_finish() {
  CURRENT_STEP="11/11 时钟与收尾"
  say "$CURRENT_STEP"
  local synced=unknown
  if command -v timedatectl >/dev/null 2>&1; then
    synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
    note "时钟同步：$synced（时区 $(timedatectl show -p Timezone --value 2>/dev/null || echo 未知)）"
  fi
  if systemctl is-active --quiet chrony 2>/dev/null || systemctl is-active --quiet systemd-timesyncd 2>/dev/null; then
    note "时间服务在跑"
  else
    note "警告：chrony 和 systemd-timesyncd 都没在跑。币安签名对时间敏感（差 1 秒就 -1021）"
    note "      修：apt-get install -y chrony && systemctl enable --now chrony"
  fi
  note "防火墙要放 80 和 443（Caddy 申请证书走 80/443，后端 $PORT 不要对外开）："
  note "  ufw allow 80/tcp && ufw allow 443/tcp"
  note "  （云主机还要在控制台安全组里放行同一对端口）"
  if [ -n "$DRY_RUN" ]; then
    note "dry-run：以上都只是打印，未做任何修改"
    return 0
  fi
  printf "\n装好了。\n"
  printf "  打开： %s\n" "$(if [ -n "$DOMAIN" ]; then echo "https://$DOMAIN"; else echo "http://127.0.0.1:8080（SSH 隧道）"; fi)"
  printf "  日志： journalctl -u %s -f\n" "$SERVICE"
  printf "  重启： systemctl restart %s\n" "$SERVICE"
  printf "  数据： %s\n" "$DATA_DIR"
  printf "  密钥： 设置页「API 凭据」会写明用系统密钥环还是本机文件存储（文件在 %s/secrets.bin，权限 600）\n" "$DATA_DIR"
  if [ "$WITH_CADDY" = "1" ]; then
    printf "  换域名： 界面里填域名点「检查解析」→「签发证书」（sudo -n %s <域名>）\n" "$HELPER"
  fi
}

main() {
  parse_args "$@"
  validate_args
  if [ -n "$DRY_RUN" ]; then printf "\n[dry-run] 只打印，不改系统\n"; fi
  require_platform
  require_root
  require_python
  install_packages
  create_user
  place_code
  install_python_deps
  install_service
  install_domain_helper
  install_caddy
  check_clock_and_finish
}

main "$@"
