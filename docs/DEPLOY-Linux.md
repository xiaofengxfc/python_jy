# 在 Debian / Ubuntu 上部署 biTrader

目标：一台干净的 Debian 12 或 Ubuntu 22.04 服务器，跑后端 + 浏览器访问界面（不做 Electron 桌面包）。
域名和公网 IP 都有的情况，下面是「从零到 https」的完整序列；没有域名也能先跑起来（只监听回环，靠 SSH 隧道访问）。

> 验证边界（先说实话）：这份文档、`deploy/install.sh` 与 `deploy/update.sh` 是在 Windows 机器上写的。
> 写完之后做过的验证是：三个脚本 `bash -n` 语法通过；`deploy/install.sh --dry-run` 在本机 Git Bash 里把
> 11 个步骤全走了一遍（只打印不执行），`--port 9000`、坏域名、坏端口、带空格的 `--repo` 都推演过；
> 更新命令（`update_cli` / `run_update.py`）与证书、橙云判定有离线测试全绿。
> **没有在真实 Debian/Ubuntu 上跑过**——本机没有 Linux、没有 Docker/WSL。
> 所以第一次上服务器请按本文档分步执行，每步都有「怎么确认这一步成了」。

---

## 0. 准备

| 需要 | 说明 |
|---|---|
| 系统 | Debian 12（Python 3.11）或 Ubuntu 22.04（Python 3.10）/ 24.04（3.12） |
| 内存 | 1 GB 能跑，建议 2 GB（pandas + ccxt 常驻约 300-500 MB） |
| 磁盘 | 10 GB 起（SQLite + K 线缓存会慢慢长） |
| 域名 | 可选。有域名才能自动签 HTTPS 证书（证书不能签给 IP） |
| 端口 | 80 和 443 要能过（Caddy 申请证书用）。后端 8765 **不要**对公网开 |
| 时间 | 时钟必须准。币安签名容忍度很小，差 1 秒就回 `-1021` |
| 地区 | 出口 IP 不能落在币安不服务的地区（美国等）。落在那里，连 `/fapi/v1/exchangeInfo` 都是 **HTTP 451**，行情和下单全废——这跟代码无关，换机房或在设置页「网络代理」填一个能出去的代理 |

DNS：在域名服务商那里加一条 A 记录，指向服务器公网 IP。**先配 DNS 再跑脚本**，否则 Caddy 签证书会失败（见第 8 节排错）。

域名写法只有一条硬规矩：**不能带下划线**。DNS 和浏览器认 `python_jy.example.com`，但证书只发
字母、数字、连字符的名字，Caddy 拿它去申请会被 Let's Encrypt 以 `rejectedIdentifier` 拒掉，
一直签不出来。脚本会在参数校验那一关就拒，并给出连字符写法（`python-jy.example.com`）——
记得在 DNS 里也加这一条记录。

用 IP 直接打开时只有 `http://IP` 那一段：它会 301 到域名。不做 `https://IP`——ACME 不给 IP 签证书，
那个站点块永远拿不到证书，写了也不会生效。要 HTTPS 就用域名访问。

```
# 确认 DNS 生效（在你自己的电脑上执行，等它吐出服务器 IP）
nslookup trade.example.com 8.8.8.8
```

---

## 1. 把代码放到服务器上

两种都行，脚本都支持：

```bash
# A. 服务器上直接 clone（推荐，之后 --repo 就能升级）
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/xiaofengxfc/python_jy.git /root/biTrader

# B. 从你这台机器上传（scp / rsync，.venv 和 node_modules 不要传）
rsync -a --exclude .venv --exclude node_modules --exclude .git \
  ./ user@服务器:/root/biTrader/
```

前端产物已经在 `src/btrader/web/static/` 里，服务器上**不需要** npm / node。

---

## 2. 一键安装

```bash
cd /root/biTrader

# 先看一眼它要做什么（不改任何东西）
bash deploy/install.sh --dry-run --domain trade.example.com

# 真跑
sudo bash deploy/install.sh --domain trade.example.com
```

脚本做的事（11 步，任一步失败立刻停并说清是哪一步）：

1. 平台检查（不是 Debian/Ubuntu 直接退出）
2. root 检查
3. Python 版本（>= 3.10，与 pyproject 的 requires-python 一致；3.11/3.12 是实测过的）
4. `apt-get` 装 `ca-certificates curl gnupg git rsync python3-venv python3-pip sudo`
   （`sudo` 是必须的：第 9 步那条 NOPASSWD 之后，程序靠 `sudo -n` 执行那一个 root 脚本）
5. 建系统用户 `bitrader`（不能登录）、数据目录 `/var/lib/bitrader`（750）
6. 代码放 `/opt/bitrader`（`--repo` 就给 git pull / clone，否则用当前目录 rsync 过去）
7. `/opt/bitrader/.venv` + `pip install -r requirements.txt`
8. 写 `/etc/systemd/system/bitrader.service`、`enable` + `restart`，并检查 `is-active`
9. 装域名切换脚本到 `/usr/local/lib/bitrader/set-domain.sh`（root:root 755），
   写 `/etc/sudoers.d/bitrader`（只授这一条 NOPASSWD）并用 `visudo -c` 校验
10. 装 Caddy（官方 apt 源）、按域名填 `/etc/caddy/Caddyfile`、`caddy validate`、reload
11. 查时钟同步、打印防火墙提示

**幂等**：每一步先看现状，已经在位就跳过。连跑两遍不会炸，也不会覆盖已有的 `secrets.bin`。

参数：

| 参数 | 说明 |
|---|---|
| `--domain trade.example.com` | 域名（不带 `http://`、不带路径、不带端口）。会被校验：小写、非 IPv4、`^[a-z0-9](...)` 那套正则不过就直接退出，什么都不动 |
| `--port 8765` | 后端监听端口（1–65535）。**它会被写进四个地方**：systemd unit 的 `ExecStart`、`/usr/local/lib/bitrader/port.conf`（界面点「签发证书」时重写 Caddyfile 要读它）、Caddyfile 的 `reverse_proxy 127.0.0.1:<端口>`、以及防火墙提示里的端口号。少写一处，装完就是「界面能开、接口全 502」 |
| `--repo https://...` | 代码来源。字符集有白名单（会被拼进 `bash -c`，带空格和分号直接拒） |
| `--no-caddy` | 不装 Caddy，自己拿 nginx 反代到 `127.0.0.1:<端口>` |
| `--dry-run` | 只打印要写什么，一个字节都不落盘 |

重复跑的时候，代码目录有三条路：已有 `.git` → `git fetch --depth 1` + `git merge --ff-only`
（只快进，和在线更新同一语义）；有文件但没 `.git`（rsync 装的老机器）→ `git init` 补上仓库壳再对齐；
目录基本是空的 → `git clone --depth 1`。数据目录 `/var/lib/bitrader` 三条路都不碰。

公网 IP 不用你填：脚本先问云厂商的元数据服务（阿里云 `100.100.100.200`、腾讯云
`metadata.tencentyun.com`、`169.254.169.254`），都没有才问 `https://api.ipify.org`，
把结果写进 Caddyfile 的 IP 跳转段。一个都查不到就**删掉那段**，不让占位符留在配置里。

没有域名时：

```bash
sudo bash deploy/install.sh            # 不带 --domain
# Caddy 只监听 127.0.0.1:8080，然后在你本机开隧道：
ssh -N -L 8080:127.0.0.1:8080 user@服务器
# 浏览器打开 http://127.0.0.1:8080
```

自己有 nginx：`sudo bash deploy/install.sh --no-caddy`，然后自己反代到 `127.0.0.1:8765`
（换了 `--port` 就换成那个端口；记得转发 `Upgrade` 头给 `/ws`）。

端口不是 8765 时记住它写在两处：unit 的 `ExecStart` 和 `/usr/local/lib/bitrader/port.conf`。
后者是界面点「签发证书」重写 Caddyfile 时读的——只改 unit 不改它，点一次证书端口就被改回 8765。

---

## 3. 确认装好了

```bash
# 服务在跑？
sudo systemctl status bitrader --no-pager

# 后端在本机通不通？（端口按 --port，默认 8765）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/          # 期望 200
curl -s http://127.0.0.1:8765/api/settings | head -c 200                # 期望一段 JSON

# 日志（这是唯一该看的地方，日志进 journald）
sudo journalctl -u bitrader -n 50 --no-pager
sudo journalctl -u bitrader -f

# Caddy 与证书
sudo systemctl status caddy --no-pager
sudo journalctl -u caddy -n 50 --no-pager
```

浏览器打开 `https://trade.example.com`，能看到界面就说明链路通了（TLS 由 Caddy 管，后端只监听回环）。

---

## 4. 配 API Key（服务器上这一条和 Windows 不一样）

设置页「API 凭据」卡片会写一行**存放位置**，只有两种：

| 显示 | 意思 |
|---|---|
| 系统密钥环 | 有 SecretService / DBus 时的正常情况（桌面发行版）。密钥进系统凭据库 |
| 本机文件存储 | **服务器上的常态**：没有图形界面、没有 DBus，keyring 没法用。密钥用 AES-256-GCM 加密后存在 `/var/lib/bitrader/secrets.bin` |

文件后端要知道的四件事：

1. 文件权限 600、目录 700，属主是 `bitrader`；只有 `bitrader` 和 root 读得到。
2. 加密钥匙由本机 `/etc/machine-id`（缺失时退回 `/var/lib/dbus/machine-id`）加服务名，用 PBKDF2-HMAC-SHA256 派生。
   **换机器、重装系统、把 `/var/lib/bitrader` 拷到别的服务器，密钥都解不开**——表现是「凭据显示未配置」，重新填一次即可，不会崩。
3. 同一台机器上的其它系统用户读不到（钥匙 + 权限两道）。root 读得到。
4. 备份数据目录时，`secrets.bin` 只有和同一台机器一起才有意义；要迁移服务器就重新填 Key。

权限自查：

```bash
sudo ls -l /var/lib/bitrader/secrets.bin   # -rw------- 1 bitrader bitrader
sudo ls -ld /var/lib/bitrader             # drwxr-x--- 1 bitrader bitrader
sudo -u bitrader test -r /var/lib/bitrader/secrets.bin && echo 可读
```

币安那边建议只勾「读取行情」+「现货/合约交易」，关掉提现，并绑定服务器 IP 白名单。

---

## 5. 日常运维

```bash
# 更新代码：一条命令（和界面那张卡走同一条流水线，见第 7 节）
sudo bash deploy/update.sh check     # 先看差几个提交
sudo bash deploy/update.sh           # 真的更新：fetch → 快进 → 装依赖 → 重启 → 确认起来了

# 出问题退回上一次更新之前的提交
sudo bash deploy/update.sh rollback --yes

# 千万不要用重跑 install.sh 来当更新手段：它会把 unit、Caddyfile、sudoers 一起重写一遍，
# 而更新只需要换代码。install.sh 只在装机和「补一条新 sudo 权限」时用。

# 手动重启（改过 Python 代码之后必须重启，没有热重载）
sudo systemctl restart bitrader && sudo journalctl -u bitrader -n 20 --no-pager

# 备份（数据目录里就是全部家当：SQLite、配置、策略、日志、密钥）
sudo systemctl stop bitrader
sudo tar czf /root/biTrader-$(date +%F).tar.gz -C /var/lib bitrader
sudo systemctl start bitrader
```

数据目录内容：

| 路径 | 内容 |
|---|---|
| `/var/lib/bitrader/bitrader.db` | SQLite：成交历史、成交账本、候选策略 |
| `/var/lib/bitrader/config.json` | 设置（**不含** API Key） |
| `/var/lib/bitrader/secrets.bin` | 没有系统密钥环时的加密密钥文件 |
| `/var/lib/bitrader/logs/` | 滚动日志（journald 里也有一份） |
| `/var/lib/bitrader/strategies/` | 用户策略 `.py`（和程序同权限，是代码） |

---

## 6. 换域名 / 重签证书（在界面里做，不用再登服务器）

装好之后换域名不需要改服务器上的文件：界面里填域名 → 检查解析 → 签发证书。三个接口：

| 接口 | 做什么 |
|---|---|
| `GET /api/deploy/info` | 当前域名、Caddy 装没装/在不在跑、本机公网 IP、证书到期时间、数据目录 |
| `POST /api/deploy/check {domain}` | 解析这个域名的 A/AAAA 记录，和本机公网 IP 比；不一致时把两个 IP 都念出来。解析**全部**落在 Cloudflare 边缘时不算不一致（`proxied: true`，见 6.3） |
| `POST /api/deploy/tls {domain}` | 写 Caddyfile + reload Caddy，然后分两段确认：先等本机 443 上有这张域名的证书（最多 60 秒），再确认公网 `/api/health` 通（最多 45 秒） |

- 公网 IP 先读本机记录（`/var/lib/bitrader/deploy.json`、Caddyfile 里的 IP 跳转段），都没有才问
  `https://api.ipify.org`（3 秒超时）。查不到就是 `null` + 一句为什么，不填 0.0.0.0。
- 证书到期时间先读 `/var/lib/caddy/.local/share/caddy/certificates/**/*.crt`；那个目录属 `caddy`（0700/0750），
  `bitrader` 读不到时改问本机 443（SNI 握手读证书的 notAfter），两边都没有才是 `null`。
- 签证书分两段判，别只看一段：**本机 443 上有没有这张域名的证书**（Caddy 到底签成没签成）和
  **公网打不打得开**（`/api/health` 回 2xx）是两件事。只看公网会被橙云的边缘缓存骗过——CF 用自己
  的证书摸到边缘就回 200，源站其实一个证书都没有；反过来只在本机确认，又漏掉了「证书有了但 80/443 被墙」。
  所以第一段老实等本机证书，第二段才探公网，两段各自的失败文案分开写。
- 失败时接口把 `journalctl -u caddy -n 20 --no-pager`（或 `/var/log/caddy/*.log` 尾部）的**原文**
  放进 `log_tail`，脚本自己的输出放进 `script_output`。原因以那两段为准，接口不替 Caddy 编理由。
- 非 Linux（Windows / macOS）这三个接口一律 501 并说明，不假装能签。

### 6.1 权限是怎么给的

服务以 `bitrader` 用户跑，写 `/etc/caddy/Caddyfile` 和 reload Caddy 要 root。所以：

```bash
# install.sh 装的东西（root:root 755，bitrader 改不了它）
sudo ls -l /usr/local/lib/bitrader/set-domain.sh

# sudoers 里和签发证书有关的就是这一条（另一条是「重启服务」，见 7.2）
sudo cat /etc/sudoers.d/bitrader
# 由 deploy/install.sh 写入：只授下面这几条，都是程序里的按钮在用，别的命令一条都不给
# 1) 签发证书：写 /etc/caddy/Caddyfile 再 reload caddy
#    域名写法与写坏回滚由 /usr/local/lib/bitrader/set-domain.sh 自己把关（sudoers 没法只允许一个合法参数）
bitrader ALL=(root) NOPASSWD: /usr/local/lib/bitrader/set-domain.sh
```

程序侧就是一条 `sudo -n /usr/local/lib/bitrader/set-domain.sh <域名>`，参数走列表、不过 shell，
域名进不了命令行解析。三个设计点别改回去：

1. **脚本放 `/usr/local/lib/bitrader/`，不放 `/opt/bitrader/`。** 后者属主是 bitrader 用户，
   一个改得动的脚本配上 NOPASSWD sudo 就等于把 root 送出去。
2. **sudoers 只能按命令授权**，没法表达「只允许一个合法域名参数」。所以脚本自己校验参数个数与
   域名写法，`caddy validate` 不通过就还原上一份 Caddyfile 再退出。
3. **systemd 单元里 `NoNewPrivileges=no`、`ProtectSystem=yes`**（原来是 `yes` / `full`）：
   NNP=yes 会让 `sudo` 直接失败；ProtectSystem=full 把 `/etc` 也挂成只读，root 子进程继承同一套
   挂载，脚本照样写不进 Caddyfile。让步的边界就是这一个脚本、这一个文件。

### 6.2 排错时手动重放

```bash
# 和界面上点「签发证书」做的是同一件事
sudo /usr/local/lib/bitrader/set-domain.sh trade.example.com

# 证书申请过程（接口失败时 log_tail 就是它）
sudo journalctl -u caddy -n 40 --no-pager

# 域名现在解析到哪、本机公网是多少
dig +short trade.example.com
curl -fsS https://api.ipify.org; echo
```

`sudo -n ...` 报 `a password is required`：sudoers 那一行没生效（`sudo -l -U bitrader` 查），
把 6.1 里那条重写一遍、`visudo -c -f /etc/sudoers.d/bitrader` 校验。

### 6.3 域名挂在 Cloudflare（橙云）

橙云（代理开着，解析结果是一堆 CF 边缘 IP）和「A 记录直接指源站」是两套行为，这里列全。
程序只能读到「解析落在 CF 网段」，**读不到 Cloudflare 面板里的开关**——面板上那几项要自己确认。

1. **SSL/TLS 加密模式必须是 Full 或 Full (strict)。**
   选 Flexible 会死循环：Caddy 把 http 301 到 https → CF 以 http 回源、看到 301 → 再回给浏览器 →
   `ERR_TOO_MANY_REDIRECTS`（`curl -I` 能看到一串 301）。Full 和 Full (strict) 都能用，
   推荐 strict：Caddy 签的是真证书（Let's Encrypt），strict 只是要求这张证书有效，不额外收钱。
2. **ACME 的验证路径不能被拦。** Caddy 走 HTTP-01，Cloudflare 必须能把
   `http://域名/.well-known/acme-challenge/<token>` 回源到这台机器的 80 端口：
   别在 WAF / Cache Rules / Page Rules 里拦它，也别给这个域名加强制跳转（Caddy 自己会处理 80 → 443）。
   橙云下这一条是被转发出来的，所以出问题时无法从外面分辨是 CF 拦的还是源站没起来：
   第一次签证书最省事的是先把这个子域的橙云关掉（灰云 / DNS only），签下来再打开——
   开关只决定流量走不走 CF，不决定证书有没有效。
3. **源站只对 Cloudflare 网段开 80/443。** 这样别人拿不到源站 IP 也就绕不过 CF。ufw 示例（**手动作业**：
   脚本不会替你改防火墙，云厂商安全组同样要自己开）：

```bash
# 先看清都有哪些网段，再一条条放行（v4 + v6）
curl -fsS https://www.cloudflare.com/ips-v4; echo
curl -fsS https://www.cloudflare.com/ips-v6; echo

for net in $(curl -fsS https://www.cloudflare.com/ips-v4) $(curl -fsS https://www.cloudflare.com/ips-v6); do
  sudo ufw allow proto tcp from "$net" to any port 80
  sudo ufw allow proto tcp from "$net" to any port 443
done
sudo ufw status numbered        # 看完再决定要不要删旧的 80/443 规则
```

4. **日志里的真实 IP 靠 `trusted_proxies`。** Caddyfile 顶部 global 段写着 22 个 CF 网段
   （`servers { trusted_proxies static ... }`），反代用 `header_up X-Real-IP {client_ip}`。
   不写这一段，服务看到的全是边缘 IP：日志、失败计数、按 IP 限流全错。
   - Caddy 没有续行符，这一行必须**一整行写完**（`deploy/Caddyfile.example` 就是这么放的）；
   - `trusted_proxies` 要 Caddy ≥ 2.7（apt 源装的是 2.10，够用）；
   - 改完 `sudo caddy validate --config /etc/caddy/Caddyfile` 再 reload。
5. **网段会变。** `routes_deploy.py` 每天问一次 `https://api.cloudflare.com/client/v4/ips`，
   问不到就用内置表（并记下失败时间，一小时后重试）。同一份网段写在三处，必须一致：
   `web/routes_deploy.py`（判定用）、`deploy/Caddyfile.example`（还原真实 IP 用）、
   `deploy/set-domain.sh`（界面点「签发证书」时重写配置用）。`tests/test_deploy_cloudflare.py`
   把三份逐字比对，加网段时一起改。

排错对照：

| 现象 | 原因 |
|---|---|
| `curl` 一串 301 / 浏览器 `ERR_TOO_MANY_REDIRECTS` | Cloudflare 是 Flexible，改成 Full (strict) |
| HTTP 521 | CF 连不上源站的 80/443（TCP 被拒或丢包）：防火墙、云安全组没放行，或者 Caddy / bitrader 没在跑。在机器上确认：`ss -lntp \| grep -E ':(80\|443)'`、`systemctl status caddy bitrader` |
| HTTP 52x（522 / 523 / 524） | CF 回不到源站：服务没起来、80/443 没对 CF 网段开、端口不是 unit 里那个 |
| HTTP 526 | 源站证书无效：先在机器上确认 443 上有这张域名的证书（`openssl s_client -connect 127.0.0.1:443 -servername 域名`） |
| 页面能开，但接口全 502 | Caddyfile 里的反代端口和 unit 的 `--port` 不一致（`port.conf` 是不是旧值） |

---

## 7. 更新与回滚

升级不用重新部署，也不用登服务器敲 git：设置页那张「软件更新」卡就是这件事。它是**手动**的——
只有你点「检查更新」「立即更新」它才动，程序自己不会检查、也不会自己装。

两条路，同一条流水线 —— 界面点按钮和终端敲命令做的是同一件事（同一份闸、同一个状态文件、
同一批步骤输出），不存在「命令行更宽松」这回事：

| 在哪 | 怎么用 |
|---|---|
| 界面 | 设置页「软件更新」卡：检查更新 / 立即更新 / 回滚 |
| 终端 | `sudo bash deploy/update.sh check` / 不带参数就是更新 / `status` / `rollback --yes` |

命令行默认**把活交给正在跑的服务**（`POST /api/update/apply`，然后读 `/api/update/status`
把步骤一行行念出来）；服务没应答（坏了、正在停、还没起来）才在本进程里跑同一条流水线。
`--offline` 强制走后者，但**服务在跑时会被拒**：引擎跑在服务进程里，绕过它就等于绕过
「引擎在跑不许更新」那道闸。真要离线跑，先 `sudo systemctl stop bitrader`。

```bash
sudo bash deploy/update.sh check          # 本机 / 远端各是哪个提交，差几个，能不能更
sudo bash deploy/update.sh                # 更新（服务在跑就交给它）
sudo bash deploy/update.sh status         # 上一次做到哪一步（重启之后照样读得到）
sudo bash deploy/update.sh rollback --yes # 回滚，不加 --yes 什么都不做

sudo bash deploy/update.sh check --json   # 给脚本用的机器可读输出
```

退出码：`0` 成功 / `1` 失败（那一步的原始输出会打出来）/ `2` 前置条件不满足
（脏工作树、已有更新在跑、已经最新、分叉、桌面版……**什么都没动**）。

`deploy/update.sh` 只做三件事：找到 `/opt/bitrader` 和它的 venv、以 `bitrader` 用户跑
（用 root 跑会把 `update-state.json` 写成 root:root，界面再也读不到）、把 `BITRADER_DATA_DIR`
对齐到 unit 里那一份。真正的逻辑在 `src/btrader/update_cli.py`，入口是仓库根的 `run_update.py`
（服务器上 `btrader` 这个包没装进 venv，`python -m btrader.update_cli` 跑不起来）。
把路径换成别的目录用 `BITRADER_APP_DIR=/srv/biTrader sudo bash deploy/update.sh check`。


- **检查**：`GET /api/update/info` 比本地 HEAD 与远端分支（默认 `xf/main`）的提交 SHA，
  列出落后的条数与每条标题。远端读不到时 `remote_sha` 是 `null` 加一句为什么，不会显示「已是最新」。
- **一键更新**：`POST /api/update/apply`，后台线程按顺序做六步，每步的输出都显示在那张卡上：
  1. `git fetch --all --prune`；
  2. 记下更新前的 SHA（回滚要用它）；
  3. `git merge --ff-only <远端>/<分支>`——只做快进，不改写历史，也不 `--force`；
  4. 用服务自己的 venv 跑 `pip install -r requirements.txt`；
  5. 只有本机装了 Node 且 `web/` 这次真有改动，才跑 `npm run build`（服务器没 Node，跳过并写明原因）；
  6. 重启服务，并确认新进程真的起来了（见下一段）。
- **前置检查**：自动交易 / AI 量化 / 执行台在跑 → 直接拒（409），不替你停引擎；工作树有未提交改动 →
  400；已经是最新 → 400；本地有提交又落后（分叉）→ 400 让你手动处理。
  探针表（`routes.RUNNING_ENGINE_PROBES`）里只有「自动交易 / 执行台」和「AI 交易 / AI 量化」两项：
  **回测、参数优化在跑不拦**——它们跑完就结束，重启丢掉的是那一次结果，不是钱。
- **重启算不算成功，由新进程回答**。Linux 上程序跑的是 `sudo -n systemctl --no-block restart bitrader`：
  `--no-block` 让 systemctl 把重启交给 systemd 就返回，老进程**不等**结果。为什么不在这里等：默认的
  restart 要等本单元停完才返回，而本单元就是发命令的那个进程；老进程等 systemctl、systemctl 等老进程
  退出，卡住之后 `TimeoutStopSec=20` 一到就被 SIGKILL 硬切——而流水线前面几步最坏能跑 2000 秒以上，
  在老进程里等重启等于自己杀自己。于是确认挪到新进程：新进程起来读到「重启中」，自己打一次
  `/api/health`，回 2xx 才把这一步收成 done，超期收成 failed。
  **端口能连上不算成功**：只 `connect_ex` 会被「绑上 0.3 秒就关」的进程骗过，必须 `/api/health` 2xx。
- **服务器上不跑 `npm run build`**：部署的是仓库里已经构建好的 `src/btrader/web/static/`，前端产物
  跟着 git 一起来。只有本机（Windows）更新时，如果装了 Node 且 `web/` 真有改动，才顺带 build 一次；
  服务器上没装 Node，跳过并在步骤里写明原因。
- **失败就停在那一步**（`state=failed`）：哪一步非 0 退出就停在哪一步，后面的步骤不再跑，服务也就
  停在那一步的状态上（重启没做成 = 还在跑旧代码，代码没拉成功 = 服务没动过）。屏幕上贴的是那条命令的
  **原始输出**（git / pip / systemctl 自己说的话），不替你编原因。

日志看三处：

```bash
# 1) 设置页那张「软件更新」卡的步骤输出：每步一条，带耗时和原始输出
# 2) 服务自己的日志（Linux 上重启与启动失败都在这里）
sudo journalctl -u bitrader -n 80 --no-pager
# 3) 重启垫片的日志：每次一个唯一文件，路径写在失败那一步的输出里
ls -t /var/lib/bitrader/logs/restart-*.log | head -n 3
```

`<数据目录>/logs/restart-<时间>-<随机>.log` 是 Windows 上「拉起新进程」那个垫片的输出（Linux 上
没有垫片，服务日志进 journald）。重启那一步失败时，卡上会连路径和手动命令一起给出来：

```bash
cd /opt/bitrader
sudo systemctl restart bitrader          # 手动重启一次，等它起来
sudo systemctl status bitrader --no-pager
```

### 7.1 回滚

- 卡片上的「回滚」：`POST /api/update/rollback {confirm: true}`，回到这次更新**之前**的那个提交，
  依赖与重启照旧走一遍，步骤输出一样能看到。
- 终端：`sudo bash deploy/update.sh rollback --yes`。不加 `--yes` 什么都不做（回滚会真的改代码，
  值得多敲四个字符）。只有在 `state == done` 且有 `from_sha` 时才允许；`from_sha` 还得像提交
  SHA（`[0-9a-f]{7,40}`），不像就拒——它会被拼进 `git revert` 的命令行。
- 上一条命令自己都不好使了（服务起不来、`update-state.json` 丢了）？手工做同一件事：

```bash
cd /opt/bitrader
sudo -u bitrader git status                 # 工作树必须是干净的，否则先处理掉
sudo -u bitrader git revert --no-edit <更新前的提交>..HEAD   # 一条反向提交，把内容退回更新前
sudo -u bitrader .venv/bin/pip install -r requirements.txt
sudo systemctl restart bitrader
```

**为什么用 `revert` 而不是 `reset --hard`**：回滚只是把内容退回去，历史照样往前走——留下一条反向提交，
谁都能看出「这里滚过一次」。`reset --hard` 是把分支指针往回掰、顺手清空工作区：本地没提交的改动
（改了一半的策略、临时脚本）会一起消失，之后想再往前走还得强推一次历史。程序里那次回滚执行的就是
`git revert`，手动操作照着来才和它对得上。

回滚不删更新带上来的提交（它们是历史的一部分）；数据目录 `/var/lib/bitrader`（SQLite、配置、策略、密钥）
一个字节都不动。

### 7.2 sudoers 允许了哪一条

更新要重启服务、重启要 root，而服务以 `bitrader` 用户跑。所以 `install.sh` 在
`/etc/sudoers.d/bitrader` 里**只多授一条**（`start` / `stop` / 别的单元一条都没有）：

```bash
sudo cat /etc/sudoers.d/bitrader
# 由 deploy/install.sh 写入：只授下面这几条，都是程序里的按钮在用，别的命令一条都不给
# 1) 签发证书：写 /etc/caddy/Caddyfile 再 reload caddy
#    域名写法与写坏回滚由 /usr/local/lib/bitrader/set-domain.sh 自己把关（sudoers 没法只允许一个合法参数）
bitrader ALL=(root) NOPASSWD: /usr/local/lib/bitrader/set-domain.sh
# 2) 一键更新：代码拉完、依赖装完后重启本服务。
#    只允许「--no-block restart」这一个子命令、只对 bitrader 这一个单元：start / stop / 其它单元都不给。
#    --no-block 必须写上：程序敲的就是这一条（core/updater.py::_restart_linux），sudoers 少一个参数就拒。
bitrader ALL=(root) NOPASSWD: /usr/bin/systemctl --no-block restart bitrader
```

这一行与程序里那条命令**逐字一致**，`--no-block` 也算在内：sudoers 按命令匹配，少一个参数就是
`sudo: a password is required`，而那一步看起来仍然像成功。`tests/test_deploy_update.py` 直接从
`core/updater.py` 读那条命令再和这里比对，改一处忘一处会被它抓住。

`systemctl` 的路径是装的时候探测出来的（Debian / Ubuntu 一般是 `/usr/bin/systemctl`）；探测不到就不写
这一条并打印一句提示，那种机器上更新完自己敲一次 `systemctl restart bitrader`（手动敲不必带
`--no-block`：那是给「本进程要被自己重启」这种情况用的，你在终端里等它重启完正好）。

这条 sudo 跟「代码换了要重新起进程」有关：`Restart=always` 管的是进程自己崩了，它不会加载新代码。
t81 之前的安装没有这一条，重跑一次 `sudo bash deploy/install.sh --domain <你的域名>` 就补上（重复跑是幂等的）。

---
## 8. 排错

### 8.1 域名没生效 / 证书拿不到

先看 Caddy 到底有没有在跑。`caddy validate` 通过**不代表**服务起来了——它只适配配置，不打开日志文件、
不绑端口。装完（或改完配置）第一件事：

```bash
systemctl is-active caddy                       # 要 active
journalctl -u caddy -n 40 --no-pager            # 没起来就看这一段
```

真机上撞过的一例：`systemctl is-active` 是 `failed`，日志里是
`open /var/log/caddy/access.log: permission denied` → 服务 exit 1 → 外面一路 521。
原因是 `/var/log/caddy` 不归 caddy 用户。修法：

```bash
sudo install -d -m 0755 -o caddy -g caddy /var/log/caddy
sudo systemctl restart caddy && systemctl is-active caddy
```

新的 install.sh 已经这么建目录，并且额外写一条 systemd drop-in
（`/etc/systemd/system/caddy.service.d/bitrader-logs.conf` 里的 `LogsDirectory=caddy`，
由 systemd 自己保证目录和属主），装完还会等 `is-active` 通过才算完——不通过就把日志原文贴出来并停下。

表现：`https://域名` 打不开，`journalctl -u caddy` 里有 `no such host` 或 `HTTP 403` 之类的 ACME 报错。

```bash
dig +short trade.example.com            # 必须是服务器公网 IP
sudo journalctl -u caddy -n 80 --no-pager | grep -i -E "acme|certificate|tls"
```

- DNS 还没生效 → 等（TTL 通常几分钟到几小时），别急着重跑。
- 挂在 Cloudflare 橙云上时，`dig` 出来的是一串 CF 边缘 IP —— **这是正常的**，不是「解析错了」。
  证书到底签成没签成，别从公网看，先在本机看 443：

```bash
openssl s_client -connect 127.0.0.1:443 -servername trade.example.com 2>/dev/null \
  | openssl x509 -noout -subject -dates
```

  有证书说明 Caddy 签成了，剩下的问题在 CF 那一侧（SSL/TLS 模式、回源端口、`/.well-known/acme-challenge/`
  被拦），对照 6.3 的排错表。界面点「签发证书」的结果本来就分两段写（本机证书 / 公网可达），
  失败文案会直接说是哪一段没过。
- 证书申请失败次数太多会被限流，等一会儿再 `sudo systemctl restart caddy`。

### 8.2 80 / 443 被墙

表现：`curl http://服务器IP` 超时，或者 Caddy 日志里 ACME 一直卡在 `tls-alpn` / `http-01`。

```bash
sudo ufw status                           # 要放 80/443（以及你 ssh 的端口）
sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
sudo ss -lntp | grep -E ":80|:443"        # 应该是 caddy 在听
```

云主机还要在控制台的**安全组**里放行同样两个端口——本机 ufw 开了、安全组没开，外部一样进不来。

### 8.3 界面能开，但图表不拉数据 / 保存 Key 出错

```bash
# 后端是否真的活着
curl -s http://127.0.0.1:8765/api/settings > /dev/null && echo 后端 OK

# 密钥存哪儿（设置页也显示同样的话）
curl -s "http://127.0.0.1:8765/api/credentials" | python3 -m json.tool | head -20
# 期望看到 "storage": {"kind": "file" 或 "keyring", "label": ..., "detail": ...}
```

| 现象 | 原因 | 办法 |
|---|---|---|
| 卡片显示「本机文件存储」 | 服务器没有 SecretService/DBus，是预期行为 | 不用管；想用系统密钥环就装 `gnome-keyring` 并跑在同一个会话里（服务器上没必要） |
| 显示「不可用」 | 既没有密钥环，也没装 `cryptography` | `sudo /opt/bitrader/.venv/bin/pip install cryptography` 然后重启服务 |
| 保存 Key 报 500 | 数据目录写不进去 | `sudo chown -R bitrader:bitrader /var/lib/bitrader` |

### 8.4 时钟 / 时区

表现：所有签名请求报 `-1021 Timestamp for this request is outside of the recvWindow`，界面上往往只是一句「无法读取当前持仓」。

```bash
timedatectl                                # System clock synchronized: yes 才对
sudo systemctl status chrony --no-pager    # 或者 systemd-timesyncd
sudo timedatectl set-timezone Asia/Shanghai
```

时区只影响显示（日志、图表刻度）；**时间准不准**才影响下单。时间不准就装 chrony：

```bash
sudo apt-get install -y chrony && sudo systemctl enable --now chrony
chronyc tracking                           # System time 偏差应该在毫秒级
```

### 8.5 服务起不来

```bash
sudo systemctl status bitrader --no-pager
sudo journalctl -u bitrader -n 80 --no-pager
# 直接当前台跑一遍，报错看得最清楚：
sudo -u bitrader env BITRADER_DATA_DIR=/var/lib/bitrader \
  /opt/bitrader/.venv/bin/python /opt/bitrader/run_web.py --host 127.0.0.1 --port 8765
```

常见原因：依赖没装全（重跑 `pip install -r requirements.txt`）、`/opt/bitrader` 属主不对（`chown -R bitrader:bitrader /opt/bitrader`）、端口被占（`ss -lntp | grep 8765`）。

---

## 9. 其它发行版

脚本只支持 Debian/Ubuntu（apt）。换 Fedora / Arch 的话，等价步骤是：

1. `python3 -m venv` 建虚拟环境，`pip install -r requirements.txt`；
2. 照抄 `deploy/bitrader.service` 写你自己的 systemd unit（`User` / `WorkingDirectory` / `Environment=BITRADER_DATA_DIR=...` 三样别漏）；
3. 装 Caddy（`dnf install caddy` / `pacman -S caddy`）或 nginx，照 `deploy/Caddyfile.example` 反代到 `127.0.0.1:8765`；
4. 时钟同步用 `chronyd`；
5. 数据目录换成 `BITRADER_DATA_DIR` 指到的地方（默认在 `$XDG_DATA_HOME/biTrader`，服务里建议显式给绝对路径）。

---

## 10. Windows 上没有的东西

原来桌面上依赖的两样在 Linux 上走的是另一条路（都在代码里分好了）：

| Windows | Linux |
|---|---|
| `%APPDATA%/biTrader` | `$XDG_DATA_HOME/biTrader` 或 systemd unit 里的 `BITRADER_DATA_DIR` |
| Windows 凭据管理器 | 系统密钥环（SecretService）或 `/var/lib/bitrader/secrets.bin`（见第 4 节） |
| `msvcrt` 文件锁 | `fcntl.flock`（`engine/ai_trader.py` 里两套都在） |