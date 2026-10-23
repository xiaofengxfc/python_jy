# 在 Debian / Ubuntu 上部署 biTrader

目标：一台干净的 Debian 12 或 Ubuntu 22.04 服务器，跑后端 + 浏览器访问界面（不做 Electron 桌面包）。
域名和公网 IP 都有的情况，下面是「从零到 https」的完整序列；没有域名也能先跑起来（只监听回环，靠 SSH 隧道访问）。

> 验证边界（先说实话）：这份文档和 `deploy/install.sh` 是在 Windows 机器上写的。
> 写完之后做过的验证是：脚本 `bash -n` 语法通过、`--dry-run` 在本机 Git Bash 里把 10 个步骤全走了一遍（只打印不执行）、
> Python 侧的新测试全绿。**没有在真实 Debian/Ubuntu 上跑过**——本机没有 Linux、没有 Docker/WSL。
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

DNS：在域名服务商那里加一条 A 记录，指向服务器公网 IP。**先配 DNS 再跑脚本**，否则 Caddy 签证书会失败（见第 8 节排错）。

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
sudo git clone https://github.com/you/biTrader.git /root/biTrader

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

没有域名时：

```bash
sudo bash deploy/install.sh            # 不带 --domain
# Caddy 只监听 127.0.0.1:8080，然后在你本机开隧道：
ssh -N -L 8080:127.0.0.1:8080 user@服务器
# 浏览器打开 http://127.0.0.1:8080
```

自己有 nginx：`sudo bash deploy/install.sh --no-caddy`，然后自己反代到 `127.0.0.1:8765`（记得转发 `Upgrade` 头给 `/ws`）。

---

## 3. 确认装好了

```bash
# 服务在跑？
sudo systemctl status bitrader --no-pager

# 后端在本机通不通？
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
# 更新代码（--repo 装的）。更省事的做法是设置页那张「软件更新」卡，见第 7 节
sudo bash deploy/install.sh --repo https://github.com/you/biTrader.git --domain trade.example.com

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
| `POST /api/deploy/check {domain}` | 解析这个域名的 A/AAAA 记录，和本机公网 IP 比；不一致时把两个 IP 都念出来 |
| `POST /api/deploy/tls {domain}` | 写 Caddyfile + reload Caddy，然后轮询 `https://域名/`（最多 60 秒）确认通 |

- 公网 IP 先读本机记录（`/var/lib/bitrader/deploy.json`、Caddyfile 里的 IP 跳转段），都没有才问
  `https://api.ipify.org`（3 秒超时）。查不到就是 `null` + 一句为什么，不填 0.0.0.0。
- 证书到期时间读 `/var/lib/caddy/.local/share/caddy/certificates/**/*.crt`，读不到就是 `null`。
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

---

## 7. 更新与回滚

升级不用重新部署，也不用登服务器敲 git：设置页那张「软件更新」卡就是这件事。它是**手动**的——
只有你点「检查更新」「立即更新」它才动，程序自己不会检查、也不会自己装。

- **检查**：`GET /api/update/info` 比本地 HEAD 与远端分支（默认 `xf/main`）的提交 SHA，
  列出落后的条数与每条标题。远端读不到时 `remote_sha` 是 `null` 加一句为什么，不会显示「已是最新」。
- **一键更新**：`POST /api/update/apply`，后台线程按顺序做四件事，每步的输出都显示在那张卡上：
  1. `git fetch --all --prune`，记下更新前的 SHA；
  2. `git merge --ff-only <远端>/<分支>`——只做快进，不改写历史，也不 `--force`；
  3. 用服务自己的 venv 跑 `pip install -r requirements.txt`；
  4. 重启服务：Linux 上是 `systemctl restart bitrader`，Windows 上拉起新进程后旧进程退出。
- **前置检查**：自动交易 / AI 量化 / 执行台在跑 → 直接拒（409），不替你停引擎；工作树有未提交改动 →
  400；已经是最新 → 400；本地有提交又落后（分叉）→ 400 让你手动处理。
  探针表（`routes.RUNNING_ENGINE_PROBES`）里只有「自动交易 / 执行台」和「AI 交易 / AI 量化」两项：
  **回测、参数优化在跑不拦**——它们跑完就结束，重启丢掉的是那一次结果，不是钱。
- **服务器上不跑 `npm run build`**：部署的是仓库里已经构建好的 `src/btrader/web/static/`，前端产物
  跟着 git 一起来。只有本机（Windows）更新时，如果装了 Node 且 `web/` 真有改动，才顺带 build 一次；
  服务器上没装 Node，跳过并在步骤里写明原因。
- **失败就停在那一步**（`state=failed`），屏幕上贴的是那条命令的**原始输出**（git / pip 自己说的话），
  不替你编原因。

日志看两处：

```bash
# 1) 设置页那张「软件更新」卡的步骤输出：每步一条，带耗时和原始输出
# 2) 服务自己的日志
sudo journalctl -u bitrader -n 80 --no-pager
```

### 7.1 回滚

- 卡片上的「回滚」：`POST /api/update/rollback {confirm: true}`，回到这次更新**之前**的那个提交，
  依赖与重启照旧走一遍，步骤输出一样能看到。
- 服务器上手动的等价做法：

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
#    只允许 restart 这一个子命令、只对 bitrader 这一个单元：start / stop / 其它单元都不给。
bitrader ALL=(root) NOPASSWD: /usr/bin/systemctl restart bitrader
```

`systemctl` 的路径是装的时候探测出来的（Debian / Ubuntu 一般是 `/usr/bin/systemctl`）；探测不到就不写
这一条并打印一句提示，那种机器上更新完自己敲一次 `systemctl restart bitrader`。

这条 sudo 跟「代码换了要重新起进程」有关：`Restart=always` 管的是进程自己崩了，它不会加载新代码。
t81 之前的安装没有这一条，重跑一次 `sudo bash deploy/install.sh --domain <你的域名>` 就补上（重复跑是幂等的）。

---
## 8. 排错

### 8.1 域名没生效 / 证书拿不到

表现：`https://域名` 打不开，`journalctl -u caddy` 里有 `no such host` 或 `HTTP 403` 之类的 ACME 报错。

```bash
dig +short trade.example.com            # 必须是服务器公网 IP
sudo journalctl -u caddy -n 80 --no-pager | grep -i -E "acme|certificate|tls"
```

- DNS 还没生效 → 等（TTL 通常几分钟到几小时），别急着重跑。
- 有 CDN / 代理（Cloudflare 橙云）→ 先关掉或确认它没拦 `/.well-known/acme-challenge/`。
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