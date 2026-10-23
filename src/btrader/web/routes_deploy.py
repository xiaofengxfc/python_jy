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
DNS_TIMEOUT = 5.0
PROBE_TIMEOUT = 5.0
SCRIPT_TIMEOUT = 120.0
TLS_VERIFY_SECONDS = 60.0
TLS_POLL_SECONDS = 2.0
LOG_LINES = 20
DOMAIN_MAX = 253

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_TLD = re.compile(r"^[a-z]{2,}$")
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
    domain = raw.lower().rstrip(".")
    labels = domain.split(".")
    if len(labels) < 2:
        raise ValueError(f"「{domain}」不是完整域名，至少要两段，例如 trade.example.com")
    for label in labels:
        if not _LABEL.match(label):
            raise ValueError(
                f"域名「{domain}」里的「{label}」不合法：每段只能用字母、数字和连字符，"
                "长度 1-63，且不能以连字符开头或结尾"
            )
    if not _TLD.match(labels[-1]):
        raise ValueError(
            f"域名「{domain}」的结尾「.{labels[-1]}」不像顶级域，应该是 .com / .net / .cn 这类"
        )
    return domain


def _domain_or_400(text: str) -> str:
    try:
        return normalize_domain(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------------------------------------------------ 外部动作
# 下面每个函数都是一个单独的外部动作，测试逐个换掉它们。


def run_command(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """跑一条外部命令。参数永远是列表：subprocess 默认 shell=False，用户输入进不了 shell。"""
    return subprocess.run(
        list(args),
        timeout=timeout,
        capture_output=True,
        text=True,
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
    try:
        answered = _ipify_get(IPIFY_URL, IPIFY_TIMEOUT).strip()
    except Exception as exc:  # noqa: BLE001 - 任何失败都只是「查不到」
        return (
            None,
            "none",
            f"连不上 {IPIFY_URL}（{IPIFY_TIMEOUT:g} 秒超时）：{type(exc).__name__}: {exc}",
        )
    if not _looks_like_ip(answered):
        return None, "none", f"{IPIFY_URL} 回的不是一个 IP：{answered!r}"
    return answered, "ipify", None


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
    """Caddy 目录里那张证书的到期时间（ISO-8601，UTC）。读不到就是 None，不猜。"""
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
    resolved: list[str] = []
    try:
        resolved = resolve_all(domain)
    except TimeoutError as exc:
        return {
            "ok": False,
            "domain": domain,
            "resolves_to": [],
            "public_ip": public_ip,
            "public_ip_source": source,
            "match": False,
            "message": f"{exc}。看看本机的 DNS（/etc/resolv.conf）通不通，或者过几分钟再试。",
        }
    except Exception as exc:  # noqa: BLE001 - 解析失败是常事，不是 500
        return {
            "ok": False,
            "domain": domain,
            "resolves_to": [],
            "public_ip": public_ip,
            "public_ip_source": source,
            "match": False,
            "message": (
                f"解析 {domain} 失败：{type(exc).__name__}: {exc}。"
                "域名拼错了，或者这台机器的 DNS 查询不通，都会是这样。"
            ),
        }
    match = bool(public_ip) and public_ip in resolved
    if not resolved:
        message = (
            f"{domain} 现在没有解析出任何地址：域名服务商那边还没加 A 记录，"
            "或者刚加上、TTL 还没到。"
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
        )
    return {
        "ok": bool(match),
        "domain": domain,
        "resolves_to": resolved,
        "public_ip": public_ip,
        "public_ip_source": source,
        "match": match,
        "message": message,
    }

def deploy_tls(payload: DeployDomainPayload) -> dict[str, Any]:
    """切到域名并签证书：写 Caddyfile（root 脚本）→ reload → 轮询 HTTPS 确认通。

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
    while True:
        reachable, detail = probe_https(url)
        if reachable:
            break
        if time.monotonic() >= deadline:
            log.warning("域名 %s 在 %.0f 秒内没有应答：%s", domain, TLS_VERIFY_SECONDS, detail)
            return _result(
                False,
                domain,
                url,
                None,
                caddy_log_tail(),
                output,
                f"Caddyfile 写好了、caddy 也 reload 了，但 {TLS_VERIFY_SECONDS:g} 秒内 "
                f"{url} 没有应答（最后一次：{detail}）。"
                "下面是 Caddy 自己的日志原文，原因以它为准。",
            )
        time.sleep(TLS_POLL_SECONDS)

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
