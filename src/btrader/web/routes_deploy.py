"""HTTPS 域名与证书：`/api/deploy/*`。

这套东西只在 Linux 服务器上成立：Caddy 反向代理 + 内置 ACME（HTTP-01）+ systemd。
Windows / macOS 上直接 501——那里没有 `/etc/caddy`、没有 systemd，装样子只会让人
以为证书已经在签了。

权限模型：服务以 `bitrader` 用户跑。写 `/etc/caddy/Caddyfile` 和 reload caddy 要 root，
所以仓库里有一个 root 脚本 `deploy/set-domain.sh`，install.sh 把它装到
`/usr/local/lib/bitrader/`（root:root 0755），并在 `/etc/sudoers.d/bitrader` 授一条
**只允许这一个脚本**的 NOPASSWD。这里只 `sudo -n <脚本> <域名>`，永远传列表、不用 shell，
域名进不了 shell 命令行。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import paths

log = logging.getLogger(__name__)

# root 脚本的位置。**不要**指向 /opt/bitrader/deploy/：那个目录属主是 bitrader 用户，
# 一个能被改的脚本 + NOPASSWD sudo 就等于把 root 送出去。
SET_DOMAIN_SCRIPT = "/usr/local/lib/bitrader/set-domain.sh"
SUDO = "sudo"
CADDYFILE = Path("/etc/caddy/Caddyfile")
CADDY_CERT_DIR = Path("/var/lib/caddy/.local/share/caddy/certificates")
CADDY_LOG_DIR = Path("/var/log/caddy")
IPIFY_URL = "https://api.ipify.org"
IPIFY_TIMEOUT = 3.0
# 查到就记 10 分钟；没查到记 1 分钟，免得每 2 秒刷新一次的界面每次都去打一遍。
IPIFY_CACHE_SECONDS = 600.0
IPIFY_RETRY_SECONDS = 60.0
DNS_TIMEOUT = 5.0
PROBE_TIMEOUT = 5.0
SCRIPT_TIMEOUT = 120.0
TLS_VERIFY_SECONDS = 60.0
TLS_POLL_SECONDS = 2.0
LOG_LINES = 20
DOMAIN_MAX = 253

# Cloudflare 公布的边缘网段（v4 + v6，https://www.cloudflare.com/ips/）。
# 三处必须一致，改就一起改：这里、deploy/Caddyfile.example、deploy/set-domain.sh 的 CF_RANGES
# （tests/test_deploy_cloudflare.py 会比对这三份，漏改一处就红）。
CF_RANGES: tuple[str, ...] = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)
# 网段是 CF 自己公布的，偶尔会加新段。内置表已经够用（判定「解析到 CF 边缘」不需要
# 精确到最新一段），所以刷新失败就静默用内置：这条路径不该因为外部接口挂了就报错。
CF_RANGES_URL = "https://api.cloudflare.com/client/v4/ips"
CF_RANGES_TIMEOUT = 3.0
CF_RANGES_TTL = 86400.0
CF_RANGES_RETRY = 3600.0
CF_PROXY = "cloudflare"
# 本机回环上的 TLS：证书签发成功的判据（不验链，只问「有没有这张域名的证书」）。
LOCAL_TLS_PORT = 443
LOCAL_TLS_TIMEOUT = 3.0
# 证书有了之后，再等「从公网真能打开」的时间。两段分开算：第一段说明证书签出来了，
# 第二段说明端到端通，混成一段会把「证书好了但 443 被封」报成「证书没签出来」。
PUBLIC_VERIFY_SECONDS = 45.0

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_TLD = re.compile(r"^[a-z]{2,}$")
# 转成 punycode 之后的 IDN 顶级域（.中国 -> xn--fiqs8s）。至少 4 位，不可能是纯数字。
_PUNYCODE_TLD = re.compile(r"^xn--[a-z0-9-]{4,}$")
_ADDRESS_LINE = re.compile(r"^(?P<address>[^{}#]+)\{")


# ------------------------------------------------------------------ 平台


def platform_name() -> str:
    """当前平台。单独一层是为了测试能换成 linux（本机是 Windows）。"""
    return sys.platform


def _require_linux() -> None:
    name = platform_name()
    if name.startswith("linux"):
        return
    raise HTTPException(
        status_code=501,
        detail=(
            "域名与 HTTPS 只在 Linux 服务器上用（这套部署是 Caddy + systemd + ACME），"
            f"当前系统是 {name}。Windows / macOS 上直接用本机地址访问，不需要证书；"
            "要证书就把程序装到 Debian/Ubuntu 上，见 docs/DEPLOY-Linux.md。"
        ),
    )


# ------------------------------------------------------------------ 域名


def _to_punycode(label: str, domain: str) -> str:
    """IDN 的那一段转成 punycode；纯 ASCII 的原样返回。

    .中国 这种合法 IDN 顶级域以前被 ^[a-z]{2,}$ 判死。转成 xn--fiqs8s 之后走同一条
    校验（长度、首尾连字符照旧管），Caddyfile 里写的也是 punycode —— Caddy 认这个。
    已经是 xn-- 的段不再转一次，否则会叠成 xn--xn----…。
    """
    if label.isascii():
        return label
    try:
        return "xn--" + label.encode("punycode").decode("ascii")
    except (UnicodeError, ValueError):
        raise ValueError(f"域名「{domain}」里的「{label}」转不成 punycode") from None


def normalize_domain(text: str) -> str:
    """把用户填的东西变成一个能进 Caddyfile 的域名，不合法就抛 ValueError。

    这里是第一道闸，宿主机上的 set-domain.sh 还有一道。两道都要：这一道给界面
    即时反馈，那一道是 root 脚本自己的保险（它能被单独调用）。
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("请填域名，例如 trade.example.com")
    if len(raw) > DOMAIN_MAX:
        raise ValueError(f"域名最长 {DOMAIN_MAX} 个字符，这个有 {len(raw)} 个")
    if any(ch.isspace() for ch in raw):
        raise ValueError(f"域名里不能有空格：「{raw}」")
    if "://" in raw or "/" in raw:
        raise ValueError("只填域名本身，不要带 http:// 或路径")
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        pass
    else:
        raise ValueError(
            "证书只能签给域名，签不了 IP（ACME 不给 IP 发证书）。"
            "先给这台机器绑一个域名，把 A 记录指过来"
        )
    if ":" in raw:
        raise ValueError("域名后面不要带端口，只填 trade.example.com 这一段")
    if "@" in raw:
        raise ValueError("这里要的是域名，不是邮箱地址")
    domain = raw.lower()
    # 只吃一个收尾点（FQDN 那种写法），中间的空段是写错。
    if domain.endswith("."):
        domain = domain[:-1]
    labels = domain.split(".")
    if len(labels) < 2:
        raise ValueError(f"「{domain}」不是完整域名，至少要两段，例如 trade.example.com")
    ascii_labels: list[str] = []
    for label in labels:
        if not label:
            raise ValueError(f"域名「{domain}」里有两个点挨在一起")
        ascii_label = _to_punycode(label, domain)
        if not _LABEL.match(ascii_label):
            raise ValueError(
                f"域名「{domain}」里的「{label}」不合法：每段只能用字母、数字和连字符，"
                "长度 1-63，且不能以连字符开头或结尾"
            )
        ascii_labels.append(ascii_label)
    ascii_domain = ".".join(ascii_labels)
    if len(ascii_domain) > DOMAIN_MAX:
        raise ValueError(
            f"域名最长 {DOMAIN_MAX} 个字符，这个转成 punycode 之后有 {len(ascii_domain)} 个"
        )
    tld = ascii_labels[-1]
    if not (_TLD.match(tld) or _PUNYCODE_TLD.match(tld)):
        raise ValueError(
            f"域名「{ascii_domain}」的结尾「.{tld}」不像顶级域，应该是 .com / .net / .cn 这类"
        )
    return ascii_domain


def _domain_or_400(text: str) -> str:
    try:
        return normalize_domain(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------------------------------------------------ 外部动作
# 下面每个函数都是一个单独的外部动作，测试逐个换掉它们。


def run_command(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """跑一条外部命令。参数永远是列表：subprocess 默认 shell=False，用户输入进不了 shell。"""
    # caddy / journalctl / sudo 的输出来自外部进程，一律按 UTF-8 解：text=True 走的是
    # 本机 locale（中文 Windows 上是 GBK），日志与域名里一有中文就 UnicodeDecodeError，
    # 整条部署排障信息丢掉。解不出来的字节用替换字符顶上，不能因为解码失败就没了输出。
    return subprocess.run(
        list(args),
        timeout=timeout,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        stdin=subprocess.DEVNULL,
    )


def resolve_all(host: str) -> list[str]:
    """解析一个域名，返回去重后的地址列表；超时就抛 TimeoutError。

    getaddrinfo 没有超时参数，卡住时会一直卡，所以放一个守护线程里等：
    超时了就直接返回（线程随进程结束被丢掉），不让接口跟着一起卡。
    """
    box: dict[str, Any] = {}
    done = threading.Event()

    def _work() -> None:
        try:
            box["ips"] = [str(info[4][0]) for info in socket.getaddrinfo(host, None)]
        except BaseException as exc:  # noqa: BLE001 - 原样交给调用方
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_work, name="deploy-dns", daemon=True).start()
    if not done.wait(DNS_TIMEOUT):
        raise TimeoutError(f"解析 {host} 超过 {DNS_TIMEOUT:g} 秒还没回来")
    if "error" in box:
        raise box["error"]
    found: list[str] = []
    for ip in box.get("ips") or ():
        if ip not in found:
            found.append(ip)
    return found


def _ipify_get(url: str, timeout: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 固定常量地址
        return resp.read(64).decode("utf-8", "replace").strip()


def _looks_like_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(str(text or "").strip())
    except ValueError:
        return False
    return True


_ipify_lock = threading.Lock()
_ipify_state: dict[str, Any] = {"ip": None, "error": None, "at": 0.0, "pending": False}


def reset_ipify_cache() -> None:
    """清掉公网 IP 缓存（测试用；换完域名想重新问一遍也可以调它）。"""
    with _ipify_lock:
        _ipify_state.update({"ip": None, "error": None, "at": 0.0, "pending": False})


def ipify_pending() -> bool:
    """后台那一次查询还在跑吗（自检与测试用）。"""
    with _ipify_lock:
        return bool(_ipify_state["pending"])


def _refresh_public_ip() -> None:
    """守护线程里问一次 ipify，结果记进缓存。失败也记，失败别每 2 秒重打一次。"""
    ip: str | None = None
    error: str | None = None
    try:
        answered = _ipify_get(IPIFY_URL, IPIFY_TIMEOUT).strip()
        if _looks_like_ip(answered):
            ip = answered
        else:
            error = f"{IPIFY_URL} 回的不是一个 IP：{answered!r}"
    except Exception as exc:  # noqa: BLE001 - 任何失败都只是「查不到」
        error = (
            f"连不上 {IPIFY_URL}（{IPIFY_TIMEOUT:g} 秒超时）："
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        with _ipify_lock:
            _ipify_state.update(
                {"ip": ip, "error": error, "at": time.monotonic(), "pending": False}
            )


def _cached_public_ip() -> tuple[str | None, str | None]:
    """(ip, 错误说明)。命中缓存立刻返回；没命中就起个后台线程去问，这次先返回 None。

    评审实测：info 在 Linux 上会同步卡 ipify 3 秒，首屏被它挂住。所以查询放到守护
    线程里做，结果记 10 分钟；失败记 1 分钟。首屏那几个字段（域名、Caddy 状态、
    证书到期）都不等它。
    """
    now = time.monotonic()
    with _ipify_lock:
        at = float(_ipify_state["at"] or 0.0)
        fresh_for = IPIFY_CACHE_SECONDS if _ipify_state["ip"] else IPIFY_RETRY_SECONDS
        if at and now - at < fresh_for:
            return _ipify_state["ip"], _ipify_state["error"]
        if not _ipify_state["pending"]:
            _ipify_state["pending"] = True
            threading.Thread(
                target=_refresh_public_ip, name="deploy-ipify", daemon=True
            ).start()
    return None, "正在后台查本机公网 IP，刷新一下就能看到（其它字段不受影响）。"

def _caddyfile_public_ip() -> str | None:
    """从 IP 跳转那一段里读出本机公网 IP（install.sh 写进去的）。"""
    try:
        text = CADDYFILE.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ADDRESS_LINE.match(line)
        if match is None:
            continue
        for token in match.group("address").replace(",", " ").split():
            candidate = token.strip()
            for scheme in ("https://", "http://"):
                if candidate.startswith(scheme):
                    candidate = candidate[len(scheme):]
            if _looks_like_ip(candidate):
                return candidate
    return None


def public_ip_info() -> tuple[str | None, str, str | None]:
    """本机公网 IP：先本地，再问 ipify。返回 (ip, 来源, 错误说明)。

    来源是 "deploy.json" / "caddyfile" / "ipify" / "none"。拿不到就是 None 加一句
    为什么——不要把 0.0.0.0 或空串填进这个字段。
    """
    recorded = str(_read_record().get("public_ip") or "").strip()
    if _looks_like_ip(recorded):
        return recorded, "deploy.json", None
    from_caddy = _caddyfile_public_ip()
    if from_caddy:
        return from_caddy, "caddyfile", None
    ip, error = _cached_public_ip()
    if ip:
        return ip, "ipify", None
    return None, "none", error


def caddy_state() -> tuple[bool, bool]:
    """(装没装, 在不在跑)。没装就是 (False, False)，不报错。"""
    installed = shutil.which("caddy") is not None or Path("/usr/bin/caddy").exists()
    if not installed:
        return False, False
    try:
        done = run_command(["systemctl", "is-active", "caddy"], timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        log.debug("systemctl is-active caddy 失败", exc_info=True)
        return installed, False
    return installed, done.stdout.strip() == "active"


_cf_lock = threading.Lock()
_cf_cache: dict[str, Any] = {"at": 0.0, "nets": [], "failed_at": 0.0}


def _parse_networks(texts: Any) -> list[Any]:
    nets: list[Any] = []
    for text in texts or []:
        try:
            nets.append(ipaddress.ip_network(str(text).strip(), strict=False))
        except ValueError:
            continue
    return nets


def _fetch_cf_networks() -> list[Any]:
    """问一次 CF 的公开接口。拿不到就返回空列表，调用方用内置表。"""
    try:
        with urllib.request.urlopen(CF_RANGES_URL, timeout=CF_RANGES_TIMEOUT) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - 网络问题不该让「检查解析」失败
        log.debug("读 Cloudflare 网段失败", exc_info=True)
        return []
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return []
    return _parse_networks(result.get("ipv4_cidr")) + _parse_networks(result.get("ipv6_cidr"))


def cf_networks() -> list[Any]:
    """Cloudflare 边缘网段：内置一份，问到新的就换成新的（缓存 24 小时，失败 1 小时后再试）。"""
    with _cf_lock:
        nets = _cf_cache["nets"]
        now = time.monotonic()
        if nets and now - float(_cf_cache["at"]) < CF_RANGES_TTL:
            return nets
        if nets and now - float(_cf_cache["failed_at"]) < CF_RANGES_RETRY:
            return nets
        fetched = _fetch_cf_networks()
        if fetched:
            _cf_cache.update({"at": now, "nets": fetched, "failed_at": 0.0})
            return fetched
        builtin = _parse_networks(CF_RANGES)
        _cf_cache.update({"at": now, "nets": builtin, "failed_at": now})
        return builtin


def cloudflare_proxied(addresses: list[str], nets: list[Any] | None = None) -> bool:
    """这些解析结果是不是**全部**落在 Cloudflare 边缘。

    全落在里面才算代理：一半 CF、一半别处，说明 DNS 还没改完或者有人手工加了一条，
    那种情况不该说「没问题」。空列表也不算。
    """
    if not addresses:
        return False
    networks = cf_networks() if nets is None else nets
    if not networks:
        return False
    for text in addresses:
        try:
            ip = ipaddress.ip_address(str(text).strip())
        except ValueError:
            return False
        if not any(ip.version == net.version and ip in net for net in networks):
            return False
    return True


def _cert_names(cert: Any) -> set[str]:
    from cryptography import x509

    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return set()
    return {str(name).lower() for name in ext.value.get_values_for_type(x509.DNSName)}


def local_tls_probe(
    domain: str,
    *,
    port: int = LOCAL_TLS_PORT,
    timeout: float = LOCAL_TLS_TIMEOUT,
) -> tuple[bool, str, str | None]:
    """连本机 443、带 SNI=域名握一次手，把对端证书读出来。

    返回 (有没有这张域名的证书, 说明, 到期时间 ISO-8601)。

    为什么不验链（CERT_NONE）：这一步要回答的是「Caddy 拿没拿到这张域名的证书」，
    可信与否由公网那一次探测回答。为什么要它：
      * Caddy 的证书目录属 caddy（通常 0700/0750），bitrader 读不到，到期时间只能这样问；
      * 云上 hairpin NAT / 安全组经常把「服务器访问自己的公网域名」挡掉，只靠公网探测
        会把一次成功的签发报成失败。
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=domain) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as exc:
        return False, f"本机 127.0.0.1:{port} 握手失败（{type(exc).__name__}: {exc}）", None
    if not der:
        return False, f"本机 127.0.0.1:{port} 没有给出证书", None
    try:
        from cryptography import x509

        cert = x509.load_der_x509_certificate(der)
        names = _cert_names(cert)
        moment = getattr(cert, "not_valid_after_utc", None)
        if moment is None:  # 老版本 cryptography
            moment = cert.not_valid_after.replace(tzinfo=timezone.utc)
    except Exception as exc:  # noqa: BLE001 - 证书读不出来也要说清是哪一步
        return True, f"拿到证书但读不出来（{type(exc).__name__}: {exc}）", None
    expires = moment.astimezone(timezone.utc).isoformat()
    if names and domain not in names:
        return False, f"本机 443 上的证书不是给 {domain} 的（覆盖 {', '.join(sorted(names))[:120]}）", expires
    return True, f"本机 443 已经在用 {domain} 的证书", expires


def probe_https(url: str, timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """发一次 HTTPS 请求：拿不到证书就是 False，具体错误原样带回去。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 域名来自校验过的输入
            return resp.status < 500, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return exc.code < 500, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 证书错误的种类太多，原样带走
        return False, f"{type(exc).__name__}: {exc}"


def cert_expires_at(domain: str | None = None) -> str | None:
    """证书到期时间（ISO-8601，UTC）。读不到就是 None，不猜。

    先读 Caddy 的证书目录，读不出来再问本机 443：那个目录属 caddy（通常 0700/0750），
    bitrader 用户进不去，所以服务器上这个字段以前恒为 null，界面也就不知道证书还剩多少天。
    本机 443 带 SNI 握一次手就能拿到对端证书，不需要任何额外权限。
    """
    found = _cert_expires_from_files(domain)
    if found:
        return found
    if domain:
        ok, _detail, expires = local_tls_probe(domain)
        if ok:
            return expires
    return None


def _cert_expires_from_files(domain: str | None = None) -> str | None:
    try:
        found = sorted(CADDY_CERT_DIR.rglob("*.crt"))
    except OSError:
        return None
    if not found:
        return None
    stamped: list[tuple[datetime, Path]] = []
    for path in found:
        try:
            moment = _cert_not_after(path)
        except Exception:  # noqa: BLE001 - 坏文件跳过，别让整个接口挂掉
            log.debug("读证书失败：%s", path, exc_info=True)
            continue
        stamped.append((moment, path))
    if not stamped:
        return None
    if domain:
        matching = [item for item in stamped if domain in str(item[1])]
        if matching:
            stamped = matching
    return max(item[0] for item in stamped).isoformat()


def _cert_not_after(path: Path) -> datetime:
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(path.read_bytes())
    moment = getattr(cert, "not_valid_after_utc", None)
    if moment is None:  # 老版本 cryptography
        moment = cert.not_valid_after.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def caddy_log_tail() -> str:
    """Caddy 日志的最后几行，**原样**返回。

    失败原因只有 Caddy 自己知道，所以这里不做任何加工：先试 journald，
    拿不到再读 /var/log/caddy/*.log。两边都空就返回空串，不编一句话。
    """
    try:
        done = run_command(
            ["journalctl", "-u", "caddy", "-n", str(LOG_LINES), "--no-pager"], timeout=10.0
        )
    except (OSError, subprocess.SubprocessError):
        log.debug("journalctl 取 Caddy 日志失败", exc_info=True)
    else:
        text = (done.stdout or "") + (done.stderr or "")
        if text.strip():
            return text
    chunks: list[str] = []
    try:
        files = sorted(CADDY_LOG_DIR.glob("*.log"))
    except OSError:
        files = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_LINES:]
        except OSError:
            continue
        chunks.append(f"== {path}\n" + "\n".join(lines))
    return "\n".join(chunks)


# ------------------------------------------------------------------ 本地记录


def deploy_record_path() -> Path:
    return paths.data_dir() / "deploy.json"


def _read_record() -> dict[str, Any]:
    try:
        data = json.loads(deploy_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_deploy_record(domain: str, public_ip: str | None, path: Path | None = None) -> None:
    """记下「这台机器现在用哪个域名」。写不进去只记一条日志，不影响证书本身。"""
    target = Path(path) if path is not None else deploy_record_path()
    payload = {
        "domain": domain,
        "public_ip": public_ip,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        holder = target.with_suffix(".json.tmp")
        holder.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        holder.replace(target)
    except OSError:
        log.warning("写 deploy.json 失败：%s", target, exc_info=True)


def read_domain() -> str | None:
    """当前在用的域名：先看自己的记录，再看 Caddyfile 的站点块。"""
    recorded = str(_read_record().get("domain") or "").strip()
    if recorded:
        return recorded
    return _caddyfile_site_domain()


def _caddyfile_site_domain() -> str | None:
    try:
        text = CADDYFILE.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ADDRESS_LINE.match(line)
        if match is None:
            continue
        for token in match.group("address").replace(",", " ").split():
            candidate = token.strip()
            if candidate.startswith("http://"):
                continue  # IP 跳转那一块
            if candidate.startswith("https://"):
                candidate = candidate[len("https://"):]
            if _looks_like_ip(candidate) or candidate.startswith("127.0.0.1"):
                continue
            try:
                return normalize_domain(candidate)
            except ValueError:
                continue
    return None


# ------------------------------------------------------------------ 接口


class DeployDomainPayload(BaseModel):
    domain: str = ""


def _result(
    ok: bool,
    domain: str,
    url: str,
    cert_expires: str | None,
    log_tail: str,
    script_output: str,
    message: str,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "domain": domain,
        "url": url,
        "cert_expires": cert_expires,
        "log_tail": log_tail,
        "script_output": script_output,
        "message": message,
    }



def deploy_info() -> dict[str, Any]:
    """这台机器现在的部署状态：装没装 Caddy、域名是什么、证书什么时候到期。"""
    _require_linux()
    installed, active = caddy_state()
    domain = read_domain()
    expires = cert_expires_at(domain)
    public_ip, source, error = public_ip_info()
    return {
        "platform": platform_name(),
        "caddy_installed": installed,
        "caddy_active": active,
        "domain": domain,
        "https_enabled": bool(active and domain),
        "public_ip": public_ip,
        "public_ip_source": source,
        "public_ip_error": error,
        "cert_expires": expires,
        "data_dir": str(paths.data_dir()),
    }

def deploy_check(payload: DeployDomainPayload) -> dict[str, Any]:
    """域名现在解析到哪、和本机公网 IP 一不一致。

    不一致是这一步最常见的坑（A 记录忘了改、CDN 橙云、TTL 没到），所以这里把
    两个 IP 都念出来，而不是只说一句「检查失败」。
    """
    _require_linux()
    domain = _domain_or_400(payload.domain)
    public_ip, source, error = public_ip_info()

    def _answer(
        ok: bool,
        match: bool,
        resolved: list[str],
        message: str,
        proxied: bool = False,
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "domain": domain,
            "resolves_to": resolved,
            "public_ip": public_ip,
            "public_ip_source": source,
            # match 是老字段（源站 IP 一不一致），origin_match 是它的同义写法：
            # 橙云下「不一致」是正常状态，别让它被读成失败。
            "match": match,
            "origin_match": match,
            "proxied": proxied,
            "proxy": CF_PROXY if proxied else None,
            "message": message,
        }

    try:
        resolved = resolve_all(domain)
    except TimeoutError as exc:
        return _answer(
            False, False, [], f"{exc}。看看本机的 DNS（/etc/resolv.conf）通不通，或者过几分钟再试。"
        )
    except Exception as exc:  # noqa: BLE001 - 解析失败是常事，不是 500
        return _answer(
            False,
            False,
            [],
            f"解析 {domain} 失败：{type(exc).__name__}: {exc}。"
            "域名拼错了，或者这台机器的 DNS 查询不通，都会是这样。",
        )

    proxied = cloudflare_proxied(resolved)
    match = bool(public_ip) and public_ip in resolved
    if not resolved:
        message = (
            f"{domain} 现在没有解析出任何地址：域名服务商那边还没加 A 记录，"
            "或者刚加上、TTL 还没到。"
        )
    elif proxied:
        message = (
            f"{domain} 解析到 {'、'.join(resolved)}（Cloudflare 边缘），橙云开着：算通过。"
            "源站 IP 不参与比较，但签证书前要确认三件事："
            "① Cloudflare 的 SSL/TLS 加密模式是 Full 或 Full (strict)——设成 Flexible 的话，"
            "CF 用 80 连源站、Caddy 又 301 回 https，浏览器会无限重定向；"
            "② /.well-known/acme-challenge/ 没被 WAF 或 Cache Rules 拦；"
            "③ 源站的 80/443 对 Cloudflare 网段放行（云安全组一起确认）。"
        )
    elif public_ip is None:
        message = (
            f"{domain} 解析到 {'、'.join(resolved)}，但本机公网 IP 查不到"
            f"（{error}），没法比较。先确认这台机器怎么出网，再点一次检查。"
        )
    elif match:
        message = f"{domain} 解析到 {public_ip}，和本机公网一致，可以签证书了。"
    else:
        message = (
            f"{domain} 现在解析到 {'、'.join(resolved)}，本机公网是 {public_ip}："
            "两边不一样，签证书会失败（ACME 要访问指到这台机器 80 端口的域名）。"
            "去域名服务商把它改成 A 记录 → 这个 IP，等生效再点一次。"
            "开着橙云（CDN 代理）时解析结果本来就不是源站 IP，那种情况上面会认成 Cloudflare 并放行；"
            "这里没认出来，说明解析到的是别的 CDN，或者 A 记录还没改。"
        )
    return _answer(bool(match or proxied), match, resolved, message, proxied)

def _public_failure_hint(domain: str, detail: str) -> str:
    """公网打不开时补一句方向，不冒充原因（原因仍在 log_tail 里）。"""
    notes: list[str] = []
    if detail.startswith("HTTP 52"):
        notes.append(
            "HTTP 52x 是 Cloudflare 打到源站失败：源站 443 没开，或者 CF 的加密模式比源站证书更严。"
        )
    elif detail.startswith("HTTP 30"):
        notes.append(
            "连续 30x 通常是 Cloudflare 加密模式设成了 Flexible：CF 用 80 连源站，"
            "Caddy 又 301 回 https，来回跳。把它改成 Full 或 Full (strict)。"
        )
    notes.append("检查这台机器的 80/443 是否对公网放行（云安全组 + 本机防火墙）。")
    try:
        if cloudflare_proxied(resolve_all(domain)):
            notes.append("这个域名解析在 Cloudflare 边缘（橙云），上面几条按橙云的口径看。")
    except Exception:  # noqa: BLE001 - 只是补一句方向，解析失败不影响结论
        log.debug("判断 Cloudflare 代理失败", exc_info=True)
    return "".join(notes)


def deploy_tls(payload: DeployDomainPayload) -> dict[str, Any]:
    """切到域名并签证书：写 Caddyfile（root 脚本）→ reload → 两次确认。

    确认分两段，别合成一段：
      ① 本机 443 带 SNI 握手，看 Caddy 到底有没有这张域名的证书（「签出来了」的判据）；
      ② 再探 https://域名/api/health（「从公网真能打开」的判据）。
    只做②会被两件事骗：云上 hairpin NAT / 安全组把「服务器访问自己的公网域名」挡掉，
    于是证书签好了却报失败；橙云下静态页面又可能被边缘缓存顶回来，源站没通也回 200。
    探 /api/health 而不是 /，就是因为它不会被缓存。

    失败时把 Caddy 的日志原文放在 log_tail 里，脚本自己的输出放在 script_output
    里——原因以它们为准，这里只报「看到了什么」。
    """
    _require_linux()
    domain = _domain_or_400(payload.domain)
    url = f"https://{domain}/"
    try:
        done = run_command([SUDO, "-n", SET_DOMAIN_SCRIPT, domain], timeout=SCRIPT_TIMEOUT)
    except FileNotFoundError as exc:
        return _result(
            False,
            domain,
            url,
            None,
            "",
            "",
            f"这台机器上没有 sudo（{exc}）：{SET_DOMAIN_SCRIPT} 必须以 root 执行，"
            "部署步骤见 docs/DEPLOY-Linux.md。",
        )
    except subprocess.TimeoutExpired:
        return _result(
            False,
            domain,
            url,
            None,
            caddy_log_tail(),
            "",
            f"{SET_DOMAIN_SCRIPT} 超过 {SCRIPT_TIMEOUT:g} 秒没结束，已经放弃等待。",
        )
    except OSError as exc:
        return _result(
            False, domain, url, None, caddy_log_tail(), "", f"执行 {SET_DOMAIN_SCRIPT} 失败：{exc}"
        )
    output = (done.stdout or "") + (done.stderr or "")
    if done.returncode != 0:
        if "password is required" in output or "a terminal is required" in output:
            message = (
                "这台机器的 bitrader 用户没有免密执行 set-domain.sh 的权限（sudo -n 被拒）。"
                "按 docs/DEPLOY-Linux.md 第 6 节在 /etc/sudoers.d/bitrader 补那一行 NOPASSWD，"
                "再试一次。"
            )
        else:
            message = (
                f"set-domain.sh 退出码 {done.returncode}，域名没切过去。"
                "下面是 Caddy 自己的日志原文，原因以它为准。"
            )
        log.warning("set-domain.sh 失败（%s）：%s", domain, output.strip()[:400])
        return _result(False, domain, url, None, caddy_log_tail(), output, message)

    deadline = time.monotonic() + TLS_VERIFY_SECONDS
    expires: str | None = None
    detail = ""
    while True:
        ready, detail, seen = local_tls_probe(domain)
        if seen:
            expires = seen
        if ready:
            break
        if time.monotonic() >= deadline:
            log.warning("域名 %s 在 %.0f 秒内没拿到证书：%s", domain, TLS_VERIFY_SECONDS, detail)
            return _result(
                False,
                domain,
                url,
                expires,
                caddy_log_tail(),
                output,
                f"Caddyfile 写好了、caddy 也 reload 了，但 {TLS_VERIFY_SECONDS:g} 秒内本机 443 上还没有 "
                f"{domain} 的证书（最后一次：{detail}）。"
                "下面是 Caddy 自己的日志原文，原因以它为准。",
            )
        time.sleep(TLS_POLL_SECONDS)

    health = f"{url}api/health"
    public_deadline = time.monotonic() + PUBLIC_VERIFY_SECONDS
    while True:
        reachable, public_detail = probe_https(health)
        if reachable:
            break
        if time.monotonic() >= public_deadline:
            log.warning("域名 %s 证书就绪但公网打不开：%s", domain, public_detail)
            return _result(
                False,
                domain,
                url,
                expires,
                caddy_log_tail(),
                output,
                f"证书已签好（本机 443 已经在用 {domain} 的证书），但 {PUBLIC_VERIFY_SECONDS:g} 秒内"
                f"从公网打开 {health} 没拿到 2xx（最后一次：{public_detail}）。"
                + _public_failure_hint(domain, public_detail),
            )
        time.sleep(TLS_POLL_SECONDS)

    if not expires:
        expires = cert_expires_at(domain)
    public_ip = public_ip_info()[0]
    write_deploy_record(domain, public_ip)
    return _result(
        True,
        domain,
        url,
        expires,
        "",
        output,
        f"证书已签好，{url} 能打开了。Caddy 会自动续期，到期时间：{expires or '（还没读到）'}。",
    )


def build_deploy_router() -> APIRouter:
    """把三个接口挂到 app 上。逻辑都在上面的模块级函数里，测试直接调它们。"""
    router = APIRouter()
    router.add_api_route("/api/deploy/info", deploy_info, methods=["GET"])
    router.add_api_route("/api/deploy/check", deploy_check, methods=["POST"])
    router.add_api_route("/api/deploy/tls", deploy_tls, methods=["POST"])
    return router
