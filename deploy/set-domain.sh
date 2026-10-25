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
# 端口跟着安装走，见下面 read_port 的注释。
HELPER_DIR="/usr/local/lib/bitrader"
PORT_CONF="$HELPER_DIR/port.conf"
PORT="8765"
CADDYFILE="/etc/caddy/Caddyfile"
BACKUP=""
CADDY_BIN="$(command -v caddy || true)"

# Cloudflare 公布的边缘网段（v4 + v6）。三处必须一致，改就一起改：
#   deploy/Caddyfile.example、本文件的 CF_RANGES、routes_deploy.CF_RANGES。
# 列在这里是为了让 {client_ip} 能还原真实客户端 —— 橙云下不信任 CF 网段的话，
# 反代看到的永远是同一个边缘 IP。
CF_RANGES="173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32"

say() { printf "\n== %s\n" "$*"; }
note() { printf "   %s\n" "$*"; }
die() { printf "\n[失败] %s\n" "$*" >&2; exit 2; }

# 端口：install.sh 第 9 步把 PORT=<端口> 写进 $PORT_CONF。以前这里写死 8765 ——
# 用 --port 9000 装的机器点一次「签发证书」，反代就被改回 8765，页面 502，而报错
# 看起来像证书的问题。只认 PORT=<数字> 这一行，别的内容一概不执行。
read_port() {
  [ -r "$PORT_CONF" ] || return 0
  local value
  value="$(sed -n 's/^PORT=\([0-9]\{1,5\}\)$/\1/p' "$PORT_CONF" | head -n 1)"
  case "$value" in
    ''|*[!0-9]*) return 0 ;;
  esac
  if [ "$value" -ge 1 ] && [ "$value" -le 65535 ]; then
    PORT="$value"
    note "后端端口从 $PORT_CONF 读到：$PORT"
  fi
}

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

read_port
say "切换域名：$DOMAIN → 127.0.0.1:$PORT"

# ---------------------------------------------------------------- 3. 公网 IP
# IP 跳转那一段需要它；拿不到就不写那一段，证书不受影响
PUBLIC_IP=""
if command -v curl >/dev/null 2>&1; then
  PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
fi
if [[ "$PUBLIC_IP" =~ $IPV4_RE ]]; then
  note "本机公网 IP：$PUBLIC_IP"
else
  PUBLIC_IP=""
  note "查不到本机公网 IP：跳过「IP 访问 301 到域名」那一段，证书不受影响"
fi

# ---------------------------------------------------------------- 4. Caddyfile
# 不直接往 /etc/caddy/Caddyfile 里写：先写同目录的临时文件，validate 过了再原子改名。
# 直接写的话，validate 失败时那个坏文件已经落在盘上，只要本次没有备份（第一次跑、
# 或者本来就没有 Caddyfile），就没人把它收回来——下一次 Caddy reload 直接失败。
if [ -f "$CADDYFILE" ]; then
  # 备份留在 Caddyfile 旁边，不用 /tmp：服务是 PrivateTmp=yes，sudo 出去的进程
  # 继承同一套私有 /tmp，那条路径在服务重启后就没了，写在提示里等于骗人。
  BACKUP="/etc/caddy/Caddyfile.bak"
  cp -a "$CADDYFILE" "$BACKUP"
  note "已备份现有 Caddyfile 到 $BACKUP"
else
  install -d -m 755 /etc/caddy
fi
# 访问日志目录：caddy 用户要能在这里建文件，否则 reload 之后配置写不进去。
# install.sh 会 chown 成 caddy:caddy，但这一份也可能被单独执行（目录是别人建的、
# 属主是 root:root 那种情况），所以每次顺手摆正。
if id -u caddy >/dev/null 2>&1; then
  install -d -m 755 -o caddy -g caddy /var/log/caddy
else
  install -d -m 755 /var/log/caddy
  note "警告：没有 caddy 这个用户，/var/log/caddy 仍属 root —— caddy 可能写不进访问日志"
fi

# 临时文件建在同一个目录里（同一个文件系统，mv 才是原子改名），名字以点开头，
# 不会被 Caddy 当成配置读；mktemp 用 O_EXCL 建，撞名不会覆盖别人的东西。
STAGE="$(mktemp "/etc/caddy/.Caddyfile.new.XXXXXX")"
trap 'rm -f "$STAGE"' EXIT

{
  printf '# 由 set-domain.sh 生成。别手改：下次点「签发证书」会重写这一份。\n'
  printf '# 域名 %s，证书由 Caddy 自己申请与续期；后端只监听 127.0.0.1:%s。\n\n' "$DOMAIN" "$PORT"
  printf '# 全局：信任 Cloudflare 边缘。橙云下不写这一段，{client_ip} 还原不出来，\n'
  printf '# 访问日志里每个客户端都是同一个边缘 IP。整段必须一行写完（Caddyfile 没有续行符）。\n'
  printf '{\n'
  printf '\tservers {\n'
  printf '\t\ttrusted_proxies static %s\n' "$CF_RANGES"
  printf '\t}\n'
  printf '}\n\n'
  printf '%s {\n' "$DOMAIN"
  printf '\treverse_proxy 127.0.0.1:%s {\n' "$PORT"
  printf '\t\theader_up X-Real-IP {client_ip}\n'
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
    # 只写 http://IP。不写 https://IP：ACME 不给 IP 签证书，那个站点块永远
    # 拿不到证书、根本不会生效，写进配置只会让人以为「IP 也能开 HTTPS」。
    printf '\n# 用 http://IP 打开的人 301 到域名。IP 拿不到证书，所以不做 https://IP\n'
    printf 'http://%s {\n\tredir https://%s{uri} 301\n}\n' "$PUBLIC_IP" "$DOMAIN"
  fi
} > "$STAGE"
chmod 644 "$STAGE"
note "已生成新配置，先校验再说"

# ---------------------------------------------------------------- 5. 校验
# 校验不过是这里最危险的一步：Caddyfile 语法坏掉 caddy 就起不来，
# 连原来的入口一起没了。现在校验的是临时文件，$CADDYFILE 一个字节都还没动，所以：
#   * 本来有备份：磁盘上还是上一份能用的，报错里指一下备份在哪；
#   * 本来没有备份（第一次跑）：删掉临时文件，磁盘上不留坏配置。
# 以前是直接写 $CADDYFILE 再校验，没备份时那个坏文件就留在 /etc/caddy 里，
# 下一次 Caddy reload 直接失败。
if ! "$CADDY_BIN" validate --config "$STAGE" --adapter caddyfile; then
  rm -f "$STAGE"
  if [ -n "$BACKUP" ]; then
    die "caddy validate 没通过（上面是它自己的输出）。$CADDYFILE 没有被改过，还是上一份能用的（备份在 $BACKUP）"
  fi
  die "caddy validate 没通过（上面是它自己的输出）。$CADDYFILE 没有写入，磁盘上没留下坏文件"
fi

# 上面这一跑也是以 root 跑的，而 validate 会按 provision=true 真把配置装一遍：
# /var/log/caddy/access.log 原本不存在的话，就是刚才这一跑建出来的（属主 root、权限 0000）。
# 随后 caddy 以 caddy 用户 reload 同一个文件 → EACCES → reload 失败、新配置上不去，
# 界面上报的是「签发证书失败」，看着像证书的问题。install.sh 那边同一处，两边都别漏。
if id -u caddy >/dev/null 2>&1; then
  chown -R caddy:caddy /var/log/caddy
fi

# 校验过了才换上去：同一个目录里的 mv 是原子改名，Caddy 不会读到半个文件。
mv -f "$STAGE" "$CADDYFILE"
chmod 644 "$CADDYFILE"
trap - EXIT
note "已写入 $CADDYFILE"

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
