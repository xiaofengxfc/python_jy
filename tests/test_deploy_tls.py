"""HTTPS 域名与证书端点（routes_deploy）的离线测试。

整份测试不碰真实 Caddy、不发 DNS 查询、不发 HTTPS 请求：
`routes_deploy` 把每个外部动作都收在一个小函数里（`run_command` / `resolve_all` /
`_ipify_get` / `probe_https` / `cert_expires_at`），测试逐个换掉它们。
平台也换：本机是 Windows，不换的话三个接口全部 501。
"""

from __future__ import annotations

import json
import shutil
import socket
import time
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fastapi import HTTPException

from btrader.web import app as web_app
from btrader.web import routes_deploy as rd

ROOT = Path(__file__).resolve().parents[1]


def _walk(routes):
    """展开 app.routes：新版 FastAPI 把 include_router 的产物存成一个壳（_IncludedRouter）。"""
    for route in routes:
        inner = getattr(route, "original_router", None) or getattr(route, "router", None)
        if inner is not None and getattr(inner, "routes", None):
            yield from _walk(inner.routes)
        else:
            yield route


def _endpoint(path: str, method: str = "GET"):
    """按路径找端点的入口函数。找不到就说明路由没挂到 app 上。"""
    app = web_app.create_app()
    for route in _walk(app.routes):
        if getattr(route, "path", None) == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError("缺少 %s %s" % (method, path))

class _Run:
    """subprocess.CompletedProcess 的最小替身。"""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Recorder:
    """记下每一次外部命令，按顺序返回预设结果。"""

    def __init__(self, *results: _Run) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout=None, **kwargs):
        assert isinstance(args, (list, tuple)), "命令必须是列表，不是字符串：%r" % (args,)
        assert "shell" not in kwargs, "不许用 shell="
        self.calls.append(list(args))
        if not self.results:
            return _Run(0, "", "")
        return self.results.pop(0)


@pytest.fixture(autouse=True)
def _fresh_ipify_cache():
    """每个用例都从空的公网 IP 缓存起步，并且不把后台线程留给下一个用例。"""
    reset = getattr(rd, "reset_ipify_cache", None)
    if reset is not None:
        reset()
    yield
    if reset is not None:
        reset()


@pytest.fixture
def linux(monkeypatch):
    """默认把平台换成 linux，并把 sudo / DNS / HTTPS 全部换成替身。"""
    monkeypatch.setattr(rd, "platform_name", lambda: "linux")
    monkeypatch.setattr(rd, "probe_https", lambda url, timeout=5.0: (True, "200 OK"))
    monkeypatch.setattr(rd, "resolve_all", lambda host: ["1.2.3.4"])
    monkeypatch.setattr(rd, "public_ip_info", lambda: ("1.2.3.4", "ipify", None))
    monkeypatch.setattr(rd, "cert_expires_at", lambda domain=None: None)
    monkeypatch.setattr(rd, "caddy_state", lambda: (True, True))
    monkeypatch.setattr(rd, "read_domain", lambda: None)
    monkeypatch.setattr(rd, "_ipify_get", lambda url, timeout: "1.2.3.4")
    return monkeypatch


# --------------------------------------------------------------- 非 Linux


def test_every_endpoint_says_501_off_linux(monkeypatch):
    monkeypatch.setattr(rd, "platform_name", lambda: "win32")
    payload = rd.DeployDomainPayload(domain="a.example.com")
    for path, method, kwargs in (
        ("/api/deploy/info", "GET", {}),
        ("/api/deploy/check", "POST", {"payload": payload}),
        ("/api/deploy/tls", "POST", {"payload": payload}),
    ):
        endpoint = _endpoint(path, method)
        with pytest.raises(HTTPException) as caught:
            endpoint(**kwargs)
        assert caught.value.status_code == 501
        assert "只在 Linux" in str(caught.value.detail)


def test_off_linux_never_calls_the_script(monkeypatch):
    monkeypatch.setattr(rd, "platform_name", lambda: "darwin")
    recorder = _Recorder()
    monkeypatch.setattr(rd, "run_command", recorder)
    endpoint = _endpoint("/api/deploy/tls", "POST")
    with pytest.raises(HTTPException):
        endpoint(payload=rd.DeployDomainPayload(domain="a.example.com"))
    assert recorder.calls == []


# ------------------------------------------------------------------- info


def test_info_without_caddy_is_null_not_an_error(linux, tmp_path):
    linux.setattr(rd, "caddy_state", lambda: (False, False))
    linux.setattr(rd, "public_ip_info", lambda: (None, "none", "连不上 https://api.ipify.org：timed out"))
    linux.setattr(rd, "deploy_record_path", lambda: tmp_path / "deploy.json")
    info = _endpoint("/api/deploy/info")()
    assert info["platform"] == "linux"
    assert info["caddy_installed"] is False
    assert info["caddy_active"] is False
    assert info["domain"] is None
    assert info["https_enabled"] is False
    assert info["public_ip"] is None
    assert info["public_ip_source"] == "none"
    assert "ipify" in info["public_ip_error"]
    assert info["cert_expires"] is None
    assert info["data_dir"]


def test_info_reports_the_domain_and_the_certificate(linux, tmp_path):
    linux.setattr(rd, "read_domain", lambda: "trade.example.com")
    linux.setattr(rd, "cert_expires_at", lambda domain=None: "2026-01-02T03:04:05+00:00")
    linux.setattr(rd, "deploy_record_path", lambda: tmp_path / "deploy.json")
    info = _endpoint("/api/deploy/info")()
    assert info["domain"] == "trade.example.com"
    assert info["https_enabled"] is True
    assert info["cert_expires"] == "2026-01-02T03:04:05+00:00"


def test_the_local_record_wins_over_ipify(monkeypatch, tmp_path):
    record = tmp_path / "deploy.json"
    record.write_text(json.dumps({"domain": "trade.example.com", "public_ip": "9.9.9.9"}), encoding="utf-8")
    monkeypatch.setattr(rd, "CADDYFILE", tmp_path / "missing-Caddyfile")
    monkeypatch.setattr(rd, "deploy_record_path", lambda: record)

    def refuse(url, timeout):
        raise AssertionError("本地已经有公网 IP，不该再问 ipify")

    monkeypatch.setattr(rd, "_ipify_get", refuse)
    assert rd.public_ip_info() == ("9.9.9.9", "deploy.json", None)


def test_the_caddyfile_ip_redirect_is_a_local_source(monkeypatch, tmp_path):
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(
        "trade.example.com {\n\treverse_proxy 127.0.0.1:8765\n}\n\n"
        "http://203.0.113.7 {\n\tredir https://trade.example.com{uri} 301\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rd, "CADDYFILE", caddyfile)
    monkeypatch.setattr(rd, "deploy_record_path", lambda: tmp_path / "deploy.json")
    assert rd.public_ip_info() == ("203.0.113.7", "caddyfile", None)


def _wait_for_ipify(timeout=3.0):
    """等后台那一次查询落地。测试里不能真等 3 秒，也不能真联网。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not rd.ipify_pending():
            return
        time.sleep(0.01)
    raise AssertionError("后台查公网 IP 没有在 %.1f 秒内结束" % timeout)


def test_the_first_call_does_not_wait_for_ipify(monkeypatch, tmp_path):
    """评审实测：info 在 Linux 上会同步卡 ipify 3 秒，首屏被它挂住。

    现在第一下立刻返回（public_ip 为 null 加一句「正在后台查」），查询在守护线程里做。
    """
    monkeypatch.setattr(rd, "CADDYFILE", tmp_path / "missing-Caddyfile")
    monkeypatch.setattr(rd, "deploy_record_path", lambda: tmp_path / "deploy.json")

    def slow(url, timeout):
        time.sleep(0.5)  # 比 ipify 的 3 秒超时短，但足够证明「没在同步等」
        return " 1.2.3.4\n"

    monkeypatch.setattr(rd, "_ipify_get", slow)

    started = time.monotonic()
    ip, source, error = rd.public_ip_info()
    elapsed = time.monotonic() - started

    assert elapsed < 0.3, "第一次调用不该等 ipify，实际等了 %.2f 秒" % elapsed
    assert ip is None
    assert source == "none"
    assert error and "后台" in error

    # 后台那一次查完之后，下一次调用就有 IP 了（两端空白也要去掉）
    _wait_for_ipify()
    assert rd.public_ip_info() == ("1.2.3.4", "ipify", None)


def test_the_public_ip_is_cached_for_ten_minutes(monkeypatch, tmp_path):
    """查到就记 10 分钟：每 2 秒刷新一次的界面不该每次都去打 ipify。"""
    monkeypatch.setattr(rd, "CADDYFILE", tmp_path / "missing-Caddyfile")
    monkeypatch.setattr(rd, "deploy_record_path", lambda: tmp_path / "deploy.json")
    calls = []

    def once(url, timeout):
        calls.append(url)
        return "1.2.3.4"

    monkeypatch.setattr(rd, "_ipify_get", once)

    rd.public_ip_info()
    _wait_for_ipify()
    for _ in range(3):
        assert rd.public_ip_info() == ("1.2.3.4", "ipify", None)
    assert len(calls) == 1, "十次刷新只该问一次 ipify，实际问了 %d 次" % len(calls)


def test_a_failed_lookup_is_null_plus_a_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(rd, "CADDYFILE", tmp_path / "missing-Caddyfile")
    monkeypatch.setattr(rd, "deploy_record_path", lambda: tmp_path / "deploy.json")

    def boom(url, timeout):
        raise socket.timeout("timed out")

    monkeypatch.setattr(rd, "_ipify_get", boom)
    rd.public_ip_info()
    _wait_for_ipify()

    ip, source, error = rd.public_ip_info()
    assert ip is None
    assert source == "none"
    assert "api.ipify.org" in error and "timed out" in error


def test_a_failed_lookup_is_not_retried_on_every_refresh(monkeypatch, tmp_path):
    """查不到也别每 2 秒打一次：失败记 1 分钟。"""
    monkeypatch.setattr(rd, "CADDYFILE", tmp_path / "missing-Caddyfile")
    monkeypatch.setattr(rd, "deploy_record_path", lambda: tmp_path / "deploy.json")
    calls = []

    def boom(url, timeout):
        calls.append(url)
        raise socket.timeout("timed out")

    monkeypatch.setattr(rd, "_ipify_get", boom)
    rd.public_ip_info()
    _wait_for_ipify()
    for _ in range(3):
        rd.public_ip_info()
    assert len(calls) == 1, "失败后一小时内只该问一次，实际问了 %d 次" % len(calls)


def test_a_record_from_disk_is_read_back(monkeypatch, tmp_path):
    record = tmp_path / "deploy.json"
    monkeypatch.setattr(rd, "deploy_record_path", lambda: record)
    rd.write_deploy_record("trade.example.com", "1.2.3.4", path=record)
    assert json.loads(record.read_text(encoding="utf-8"))["domain"] == "trade.example.com"
    monkeypatch.setattr(rd, "CADDYFILE", tmp_path / "missing-Caddyfile")
    assert rd.read_domain() == "trade.example.com"


def test_domain_falls_back_to_the_caddyfile(monkeypatch, tmp_path):
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(
        "# 注释里的域名 key.example.com 不算\n"
        "http://203.0.113.7 {\n\tredir https://real.example.com{uri} 301\n}\n\n"
        "real.example.com {\n\treverse_proxy 127.0.0.1:8765\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rd, "CADDYFILE", caddyfile)
    monkeypatch.setattr(rd, "deploy_record_path", lambda: tmp_path / "deploy.json")
    assert rd.read_domain() == "real.example.com"


# ------------------------------------------------------------------ IDN
# 评审抓的：routes_deploy.py 的 TLD 判定是 ^[a-z]{2,}$，于是 .中国 这种合法 IDN
# 顶级域被判死。合法 IDN 应当归一成 punycode（xn--fiqs8s 这类）再放行，
# 非法的（长度、首尾连字符、纯数字 TLD、两个点挨着）照旧 400。


@pytest.mark.parametrize(
    "given,expected",
    [
        ("trade.中国", "trade.xn--fiqs8s"),
        ("www.trade.中国", "www.trade.xn--fiqs8s"),
        ("TRADE.中国", "trade.xn--fiqs8s"),
        ("trade.中国.", "trade.xn--fiqs8s"),
        ("münchen.de", "xn--mnchen-3ya.de"),
        ("trade.xn--fiqs8s", "trade.xn--fiqs8s"),
        ("trade.example.com", "trade.example.com"),
    ],
)
def test_an_idn_domain_is_normalised_to_punycode(given, expected):
    assert rd.normalize_domain(given) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "trade.123",          # 纯数字顶级域
        "trade..中国",         # 两个点挨着
        "trade.中国..",        # 两个收尾点：只允许一个
        "-bad.中国",
        "bad-.中国",
        "a" * 64 + ".中国",     # 单段太长
        "trade.中国/path",
    ],
)
def test_a_bad_idn_domain_is_still_refused(bad):
    with pytest.raises(ValueError):
        rd.normalize_domain(bad)


def test_punycode_is_not_double_encoded(linux):
    """已经在 punycode 里的域名不许再转一次（trade.xn--fiqs8s 别变成 xn--trade-...）。"""
    assert rd._domain_or_400("trade.xn--fiqs8s") == "trade.xn--fiqs8s"
    assert rd._domain_or_400("trade.中国") == "trade.xn--fiqs8s"


# ------------------------------------------------------------------ check


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "a b.example.com",
        "http://trade.example.com",
        "trade.example.com/login",
        "-bad.example.com",
        "bad-.example.com",
        "trade..example.com",
        "1.2.3.4",
        "trade.example.com:8443",
        "trade_example.com",
        "localhost",
        "a" * 64 + ".example.com",
    ],
)
def test_a_bad_domain_is_a_400(linux, bad):
    recorder = _Recorder()
    linux.setattr(rd, "run_command", recorder)
    endpoint = _endpoint("/api/deploy/check", "POST")
    with pytest.raises(HTTPException) as caught:
        endpoint(payload=rd.DeployDomainPayload(domain=bad))
    assert caught.value.status_code == 400
    assert recorder.calls == [], "写法不对的域名不许碰系统"


def test_check_mismatch_says_which_ip_where(linux):
    linux.setattr(rd, "resolve_all", lambda host: ["5.6.7.8"])
    linux.setattr(rd, "public_ip_info", lambda: ("1.2.3.4", "ipify", None))
    result = _endpoint("/api/deploy/check", "POST")(
        payload=rd.DeployDomainPayload(domain="trade.example.com")
    )
    assert result["ok"] is False
    assert result["match"] is False
    assert result["domain"] == "trade.example.com"
    assert result["resolves_to"] == ["5.6.7.8"]
    assert result["public_ip"] == "1.2.3.4"
    assert "5.6.7.8" in result["message"] and "1.2.3.4" in result["message"]


def test_check_match_is_ok(linux):
    linux.setattr(rd, "resolve_all", lambda host: ["1.2.3.4", "2001:db8::1"])
    result = _endpoint("/api/deploy/check", "POST")(
        payload=rd.DeployDomainPayload(domain="trade.example.com")
    )
    assert result["ok"] is True
    assert result["match"] is True
    assert result["resolves_to"] == ["1.2.3.4", "2001:db8::1"]
    assert "一致" in result["message"]


def test_check_without_any_record_is_a_clear_no(linux):
    linux.setattr(rd, "resolve_all", lambda host: [])
    result = _endpoint("/api/deploy/check", "POST")(
        payload=rd.DeployDomainPayload(domain="trade.example.com")
    )
    assert result["ok"] is False
    assert result["match"] is False
    assert "没有解析出" in result["message"]


def test_check_without_a_public_ip_says_so(linux):
    linux.setattr(rd, "public_ip_info", lambda: (None, "none", "连不上 https://api.ipify.org：timed out"))
    result = _endpoint("/api/deploy/check", "POST")(
        payload=rd.DeployDomainPayload(domain="trade.example.com")
    )
    assert result["ok"] is False
    assert result["public_ip"] is None
    assert "公网" in result["message"]


def test_a_dns_failure_is_a_message_not_a_500(linux):
    def boom(host):
        raise socket.gaierror(-2, "Name or service not known")

    linux.setattr(rd, "resolve_all", boom)
    result = _endpoint("/api/deploy/check", "POST")(
        payload=rd.DeployDomainPayload(domain="trade.example.com")
    )
    assert result["ok"] is False
    assert "Name or service not known" in result["message"]


# -------------------------------------------------------------------- tls


def test_the_script_is_called_with_a_list_and_dash_n(linux, tmp_path):
    recorder = _Recorder(_Run(0, "done", ""))
    linux.setattr(rd, "run_command", recorder)
    linux.setattr(rd, "deploy_record_path", lambda: tmp_path / "deploy.json")
    result = _endpoint("/api/deploy/tls", "POST")(
        payload=rd.DeployDomainPayload(domain="trade.example.com")
    )
    assert result["ok"] is True
    assert recorder.calls[0] == ["sudo", "-n", rd.SET_DOMAIN_SCRIPT, "trade.example.com"]
    assert result["url"] == "https://trade.example.com/"


def test_the_script_path_is_not_inside_the_app_directory():
    """能改的脚本 + NOPASSWD sudo 等于把 root 送出去。"""
    assert not rd.SET_DOMAIN_SCRIPT.startswith("/opt/bitrader"), rd.SET_DOMAIN_SCRIPT
    assert rd.SET_DOMAIN_SCRIPT.endswith(".sh")


def test_a_script_failure_keeps_the_caddy_log_verbatim(linux, tmp_path):
    journal = (
        '10月 23 12:00:00 host caddy[1]: {"level":"error","msg":"obtaining certificate"}\n'
        "10月 23 12:00:01 host caddy[1]: dial tcp 1.2.3.4:80: connect: connection refused\n"
    )
    recorder = _Recorder(_Run(3, "脚本自己的输出", "调用 caddy validate 失败"), _Run(0, journal, ""))
    linux.setattr(rd, "run_command", recorder)
    result = _endpoint("/api/deploy/tls", "POST")(
        payload=rd.DeployDomainPayload(domain="trade.example.com")
    )
    assert result["ok"] is False
    assert result["log_tail"] == journal, "日志必须原样透出，不许改写"
    assert "3" in result["message"]
    assert "脚本自己的输出" in result["script_output"]
    assert "connection refused" in result["log_tail"]


def test_the_poll_gives_up_after_a_minute(linux, tmp_path):
    linux.setattr(rd, "run_command", _Recorder(_Run(0, "", "")))
    linux.setattr(rd, "probe_https", lambda url, timeout=5.0: (False, "ssl: CERTIFICATE_VERIFY_FAILED"))
    linux.setattr(rd, "TLS_VERIFY_SECONDS", 0.05)
    linux.setattr(rd, "TLS_POLL_SECONDS", 0.01)
    linux.setattr(rd, "caddy_log_tail", lambda: "certificate is not trusted")
    linux.setattr(rd, "deploy_record_path", lambda: tmp_path / "deploy.json")
    result = _endpoint("/api/deploy/tls", "POST")(
        payload=rd.DeployDomainPayload(domain="trade.example.com")
    )
    assert result["ok"] is False
    assert "CERTIFICATE_VERIFY_FAILED" in result["message"]
    assert result["log_tail"] == "certificate is not trusted"


def test_success_records_the_domain_and_the_cert(linux, tmp_path):
    record = tmp_path / "deploy.json"
    linux.setattr(rd, "deploy_record_path", lambda: record)
    linux.setattr(rd, "run_command", _Recorder(_Run(0, "", "")))
    linux.setattr(rd, "cert_expires_at", lambda domain=None: "2026-01-02T03:04:05+00:00")
    result = _endpoint("/api/deploy/tls", "POST")(
        payload=rd.DeployDomainPayload(domain="trade.example.com")
    )
    assert result["ok"] is True
    assert result["cert_expires"] == "2026-01-02T03:04:05+00:00"
    assert json.loads(record.read_text(encoding="utf-8"))["domain"] == "trade.example.com"


def test_a_missing_sudo_explains_itself(linux, tmp_path):
    def missing(args, timeout=None, **kwargs):
        raise FileNotFoundError("sudo")

    linux.setattr(rd, "run_command", missing)
    result = _endpoint("/api/deploy/tls", "POST")(
        payload=rd.DeployDomainPayload(domain="trade.example.com")
    )
    assert result["ok"] is False
    assert "sudo" in result["message"]


def test_sudo_without_a_password_rule_points_at_the_sudoers_line(linux, tmp_path):
    output = "sudo: a password is required"
    linux.setattr(rd, "run_command", _Recorder(_Run(1, "", output), _Run(0, "no log", "")))
    result = _endpoint("/api/deploy/tls", "POST")(
        payload=rd.DeployDomainPayload(domain="trade.example.com")
    )
    assert result["ok"] is False
    assert "sudoers" in result["message"]


# ----------------------------------------------------------- 证书与脚本


def test_the_certificate_reader_parses_a_real_file(monkeypatch, tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "trade.example.com")])
    not_after = datetime(2031, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_after - timedelta(days=90))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    folder = tmp_path / "certificates" / "acme-v02" / "trade.example.com"
    folder.mkdir(parents=True)
    (folder / "trade.example.com.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setattr(rd, "CADDY_CERT_DIR", tmp_path / "certificates")
    assert rd.cert_expires_at("trade.example.com") == "2031-05-06T07:08:09+00:00"
    assert rd.cert_expires_at("other.example.com") == "2031-05-06T07:08:09+00:00"


def test_the_certificate_reader_returns_none_when_there_is_nothing_to_read(monkeypatch, tmp_path):
    monkeypatch.setattr(rd, "CADDY_CERT_DIR", tmp_path / "nope")
    assert rd.cert_expires_at("trade.example.com") is None


def test_the_root_script_guards_itself():
    """sudoers 授的是「这个脚本能跑」，所以脚本自己必须把住域名与失败回滚。"""
    script = (ROOT / "deploy" / "set-domain.sh").read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in script
    assert "caddy validate" in script
    assert "systemctl reload caddy" in script
    assert "eval" not in script
    assert '"$DOMAIN"' in script, "域名要加引号传参"
    assert "\r" not in script, "bash 脚本不能带 CR，Linux 上会报 command not found"


def test_install_sh_grants_one_command_only():
    """sudoers 那一行只授这一个脚本，而且脚本是 root 自己的副本。"""
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "sudoers.d/bitrader" in script
    assert "NOPASSWD" in script
    assert "set-domain.sh" in script
    assert "/usr/local/lib/bitrader" in script
    assert "visudo -c" in script


@pytest.mark.skipif(shutil.which("bash") is None, reason="这台机器没有 bash")
def test_the_root_script_rejects_a_bad_domain():
    script = str(ROOT / "deploy" / "set-domain.sh")
    for bad in ("a b", "http://x.example.com", "-bad.example.com", "1.2.3.4", ""):
        done = subprocess.run(["bash", script, bad], capture_output=True, text=True, timeout=30)
        assert done.returncode != 0, "%r 不该被接受" % (bad,)
        assert "域名" in (done.stdout + done.stderr)
