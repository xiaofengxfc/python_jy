#!/usr/bin/env bash
# biTrader 域名切换（root 专用，只做一件事）
#
#   sudo /usr/local/lib/bitrader/set-domain.sh trade.example.com
#
# 谁在调它：程序里的 POST /api/deploy/tls，以 bitrader 用户身份
#   sudo -n /usr/local/lib/bitrader/set-domain.sh <域名>
# /etc/sudoers.d/bitrader 只授这一条 NOPASSWD（见 deploy/install.sh）。
#
# 因此这个脚本必须自己把住三件事，不能指望调用方：
#   1. 域名写法（sudoers 只能按命令授权，没法只允许「一个合法域名」这种参数）
#   2. 写坏了要能回去（validate 不过就还原上一份 Caddyfile）
#   3. 只碰 /etc/caddy/Caddyfile 这一个文件，别的什么都不动
#
# 它不申请证书：Caddy 自己按站点块里的域名走 ACME（HTTP-01），并在到期前自动续期。
# 这里只负责把站点块写对、校验通过、reload。

set -Eeuo pipefail

DOMAIN="${1:-}"
PORT="8765"
CADDYFILE="/etc/caddy/Caddyfile"
BACKUP=""
CADDY_BIN="$(command -v caddy || true)"

say() { printf "\n== %s\n" "$*"; }
note() { printf "   %s\n" "$*"; }
die() { printf "\n[失败] %s\n" "$*" >&2; exit 2; }

usage() {
  cat <<'USAGE'
用法: sudo /usr/local/lib/bitrader/set-domain.sh <域名>

  域名       只填域名本身，例如 trade.example.com（不要带 https:// 或路径）
  退出码     0 成功；2 参数或环境不对；其它非 0 来自 caddy validate / systemctl

这个脚本只改 /etc/caddy/Caddyfile，然后 caddy validate + systemctl reload caddy。
证书由 Caddy 自己申请和续期（ACME HTTP-01，需要 80/443 通、域名解析到本机）。
USAGE
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
esac

[ "$#" -eq 1 ] || { usage >&2; die "只接受一个参数：域名。这次给了 $# 个"; }
[ -n "$DOMAIN" ] || { usage >&2; die "没给域名"; }

# ---------------------------------------------------------------- 1. 域名写法
# 逐段校验：字母数字开头的段，结尾是 2 位以上的字母顶级域。
DOMAIN_RE='^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$'
IPV4_RE='^[0-9]{1,3}(\.[0-9]{1,3}){3}$'

case "$DOMAIN" in
  *' '*|*$'\t'*) die "域名里不能有空格：「$DOMAIN」" ;;
  *://*|*/*)     die "只填域名本身，不要带协议或路径：「$DOMAIN」" ;;
  *:*)           die "域名后面不要带端口：「$DOMAIN」" ;;
esac
if [ "${#DOMAIN}" -gt 253 ]; then
  die "域名最长 253 个字符，这个是 ${#DOMAIN} 个"
fi
if [[ "$DOMAIN" =~ $IPV4_RE ]]; then
  die "证书只能签给域名，签不了 IP：「$DOMAIN」。先给这台机器绑一个域名"
fi
DOMAIN="$(printf '%s' "$DOMAIN" | tr '[:upper:]' '[:lower:]')"
if ! [[ "$DOMAIN" =~ $DOMAIN_RE ]]; then
  die "域名写法不对：「$DOMAIN」要写成 trade.example.com 这种样子（字母、数字、连字符，结尾是 .com 这类）"
fi

# ---------------------------------------------------------------- 2. 环境
[ "$(id -u)" -eq 0 ] || die "要以 root 执行：sudo $0 $DOMAIN"
if [ -z "$CADDY_BIN" ]; then
  die "没找到 caddy 命令。先按 docs/DEPLOY-Linux.md 装 Caddy（install.sh 会装）"
fi
if ! systemctl list-unit-files 2>/dev/null | grep -q '^caddy\.service'; then
  note "警告：没有 caddy.service 这个 unit，下面的 reload 会失败"
fi

say "切换域名：$DOMAIN → 127.0.0.1:$PORT"

# ---------------------------------------------------------------- 3. 公网 IP
# IP 跳转那一段需要它；拿不到就不写那两段，证书不受影响
PUBLIC_IP=""
if command -v curl >/dev/null 2>&1; then
  PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
fi
if [[ "$PUBLIC_IP" =~ $IPV4_RE ]]; then
  note "本机公网 IP：$PUBLIC_IP"
else
  PUBLIC_IP=""
  note "查不到本机公网 IP：跳过「IP 访问 301 到域名」那两段，证书不受影响"
fi

# ---------------------------------------------------------------- 4. Caddyfile
if [ -f "$CADDYFILE" ]; then
  # 备份留在 Caddyfile 旁边，不用 /tmp：服务是 PrivateTmp=yes，sudo 出去的进程
  # 继承同一套私有 /tmp，那条路径在服务重启后就没了，写在提示里等于骗人。
  BACKUP="/etc/caddy/Caddyfile.bak"
  cp -a "$CADDYFILE" "$BACKUP"
  note "已备份现有 Caddyfile 到 $BACKUP"
else
  install -d -m 755 /etc/caddy
fi
[ -d /var/log/caddy ] || install -d -m 755 /var/log/caddy

{
  printf '# 由 set-domain.sh 生成。别手改：下次点「签发证书」会重写这一份。\n'
  printf '# 域名 %s，证书由 Caddy 自己申请与续期；后端只监听 127.0.0.1:%s。\n\n' "$DOMAIN" "$PORT"
  printf '%s {\n' "$DOMAIN"
  printf '\treverse_proxy 127.0.0.1:%s {\n' "$PORT"
  printf '\t\theader_up X-Real-IP {remote_host}\n'
  printf '\t}\n'
  printf '\tencode gzip zstd\n'
  printf '\tlog {\n'
  printf '\t\toutput file /var/log/caddy/access.log {\n'
  printf '\t\t\troll_size 10MiB\n'
  printf '\t\t\troll_keep 5\n'
  printf '\t\t}\n'
  printf '\t}\n'
  printf '}\n'
  if [ -n "$PUBLIC_IP" ]; then
    printf '\n# 用 http(s)://IP 打开的人 301 到域名（证书签不了 IP）\n'
    printf 'http://%s {\n\tredir https://%s{uri} 301\n}\n' "$PUBLIC_IP" "$DOMAIN"
    printf 'https://%s {\n\tredir https://%s{uri} 301\n}\n' "$PUBLIC_IP" "$DOMAIN"
  fi
} > "$CADDYFILE"
chmod 644 "$CADDYFILE"
note "已写入 $CADDYFILE"

# ---------------------------------------------------------------- 5. 校验
# 校验不过是这里最危险的一步：Caddyfile 语法坏掉 caddy 就起不来，
# 连原来的入口一起没了。所以 validate 失败必须还原。
if ! "$CADDY_BIN" validate --config "$CADDYFILE"; then
  if [ -n "$BACKUP" ]; then
    cp -a "$BACKUP" "$CADDYFILE"
    note "caddy validate 没通过，已还原上一份 Caddyfile"
  fi
  die "caddy validate 没通过（上面是它自己的输出），Caddyfile 已还原"
fi

# ---------------------------------------------------------------- 6. reload
if ! systemctl reload caddy 2>/dev/null; then
  note "reload 没成功，退回 restart"
  systemctl restart caddy
fi

note "已 reload caddy；证书申请日志：journalctl -u caddy -n 40 --no-pager"
note "Caddy 会在后台申请证书（通常十几秒），并在到期前 30 天自动续期"
if [ -n "$BACKUP" ]; then
  note "上一份 Caddyfile 留在 $BACKUP"
fi
say "完成：https://$DOMAIN"
