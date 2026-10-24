"""Cloudflare 橙云：解析到 CF 边缘不该被报成「解析不一致」。

橙云开着的时候，域名的 A 记录指向 CF 的边缘 IP，本来就不等于源站 IP。按老口径
（「解析结果里有没有本机公网 IP」）比较，装完第一屏就是「两边不一样，签证书会失败」，
而实际上证书能签、站点也能开。这里钉住三件事：

1. `deploy_check` 认得出 CF 网段，放行，并把「签证书前要确认什么」说清楚；
2. CF 网段在三处同源（`routes_deploy.CF_RANGES` / `deploy/Caddyfile.example` /
   `deploy/set-domain.sh`），漏改一处就红；
3. 本机 443 的 SNI 探测（证书到期时间的兜底）失败时老实返回，不抛异常。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path

import pytest

from btrader.web import routes_deploy as rd

ROOT = Path(__file__).resolve().parents[1]
CADDYFILE = ROOT / "deploy" / "Caddyfile.example"
SET_DOMAIN = ROOT / "deploy" / "set-domain.sh"

EDGE_V4 = "104.16.1.2"  # 104.16.0.0/13，橙云的常见边缘地址
EDGE_V6 = "2606:4700::6810:102"
ORIGIN = "203.0.113.9"  # 文档保留地址，肯定不在 CF 网段里


def _reset_cf() -> None:
    rd._cf_cache.update({"at": 0.0, "nets": [], "failed_at": 0.0})


@pytest.fixture(autouse=True)
def _fresh_cf_cache():
    """每个用例都从空缓存起步：网段缓存是模块级的，留着会串。"""
    _reset_cf()
    yield
    _reset_cf()


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(rd, "platform_name", lambda: "linux")
    monkeypatch.setattr(rd, "public_ip_info", lambda: (ORIGIN, "ipify", None))
    return monkeypatch


def _check(domain: str = "trade.example.com") -> dict:
    return rd.deploy_check(rd.DeployDomainPayload(domain=domain))


# --------------------------------------------------------------- 判定
def test_a_cloudflare_address_is_not_a_mismatch(linux):
    linux.setattr(rd, "resolve_all", lambda host: [EDGE_V4])
    answer = _check()
    assert answer["ok"] is True, answer["message"]
    assert answer["proxied"] is True
    assert answer["proxy"] == "cloudflare"
    # 源站 IP 确实对不上 —— 这个字段照实报，只是不再决定 ok。
    assert answer["match"] is False
    assert answer["origin_match"] is False
    assert "Cloudflare" in answer["message"]
    assert "Full" in answer["message"]
    assert "Flexible" in answer["message"], "Flexible 会让浏览器无限重定向，必须点名"
    assert "acme-challenge" in answer["message"]


def test_ipv6_edge_addresses_count_too(linux):
    linux.setattr(rd, "resolve_all", lambda host: [EDGE_V6])
    answer = _check()
    assert answer["ok"] is True
    assert answer["proxied"] is True


def test_another_relay_is_still_a_mismatch(linux):
    """换成别家 CDN 就还是不一致：只有 CF 网段才放行，别把这条判据放宽。"""
    linux.setattr(rd, "resolve_all", lambda host: ["198.51.100.7"])
    answer = _check()
    assert answer["ok"] is False
    assert answer["proxied"] is False
    assert answer["proxy"] is None
    assert "两边不一样" in answer["message"]


def test_a_direct_a_record_still_passes(linux):
    linux.setattr(rd, "resolve_all", lambda host: [ORIGIN])
    answer = _check()
    assert answer["ok"] is True
    assert answer["match"] is True
    assert answer["proxied"] is False
    assert "可以签证书了" in answer["message"]


def test_half_cloudflare_is_not_proxied():
    """一半 CF、一半别处：DNS 还没改完，别替它说「没问题」。"""
    nets = rd._parse_networks(rd.CF_RANGES)
    assert rd.cloudflare_proxied([EDGE_V4, "198.51.100.7"], nets) is False
    assert rd.cloudflare_proxied([EDGE_V4], nets) is True
    assert rd.cloudflare_proxied([], nets) is False
    assert rd.cloudflare_proxied(["不是地址"], nets) is False


def test_a_dns_failure_keeps_the_new_fields(linux):
    def boom(host):
        raise TimeoutError("解析超时")

    linux.setattr(rd, "resolve_all", boom)
    answer = _check()
    assert answer["ok"] is False
    assert answer["proxied"] is False
    assert answer["resolves_to"] == []


# --------------------------------------------------------------- 网段来源
def test_the_builtin_table_is_used_when_the_api_does_not_answer(monkeypatch):
    monkeypatch.setattr(rd, "_fetch_cf_networks", lambda: [])
    nets = rd.cf_networks()
    assert len(nets) == len(rd.CF_RANGES)
    assert rd.cloudflare_proxied([EDGE_V4], nets) is True


def test_a_fetched_table_replaces_the_builtin_one(monkeypatch):
    monkeypatch.setattr(
        rd, "_fetch_cf_networks", lambda: [ipaddress.ip_network("198.51.100.0/24")]
    )
    nets = rd.cf_networks()
    assert [str(net) for net in nets] == ["198.51.100.0/24"]
    assert rd.cloudflare_proxied(["198.51.100.7"], nets) is True


def test_the_ranges_are_the_same_in_all_three_places():
    """routes_deploy / Caddyfile.example / set-domain.sh 三份网段必须逐字一致。

    Caddyfile 里那份是给 {client_ip} 还原真实地址用的，python 那份是判定用的，
    set-domain.sh 那份是手点「签发证书」时重写配置用的：漏改一处，行为就分成两套。
    """
    text = CADDYFILE.read_text(encoding="utf-8")
    match = re.search(r"^\s*trusted_proxies static (.+)$", text, re.M)
    assert match, "Caddyfile.example 里没有 trusted_proxies"
    caddy = match.group(1).split()

    script = SET_DOMAIN.read_text(encoding="utf-8")
    field = re.search(r'^CF_RANGES="([^"]+)"$', script, re.M)
    assert field, "set-domain.sh 里没有 CF_RANGES"
    shell = field.group(1).split()

    assert caddy == shell == list(rd.CF_RANGES)


# --------------------------------------------------------------- 提示与探测
def test_the_public_failure_hint_names_cloudflare_specifics(monkeypatch):
    monkeypatch.setattr(rd, "resolve_all", lambda host: [EDGE_V4])
    hint = rd._public_failure_hint("trade.example.com", "HTTP 522")
    assert "52x" in hint
    assert "Cloudflare" in hint

    hint = rd._public_failure_hint("trade.example.com", "HTTP 301")
    assert "Flexible" in hint, "连续 301 就是 Flexible 的典型症状"

    plain = rd._public_failure_hint("trade.example.com", "URLError: timed out")
    assert "80/443" in plain


def test_the_local_handshake_returns_quietly_when_nothing_listens():
    """开发机上没有 443 监听：返回 False + 说明，不许抛，也不许把测试挂在超时上。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    ok, detail, expires = rd.local_tls_probe("trade.example.com", port=port, timeout=1.0)
    assert ok is False
    assert expires is None
    assert "127.0.0.1" in detail


def test_the_caddyfile_keeps_the_placeholder_and_uses_the_real_client_ip():
    text = CADDYFILE.read_text(encoding="utf-8")
    assert "REPLACE_WITH_SERVER_IP" in text
    assert "trusted_proxies" in text
    assert "{client_ip}" in text, "橙云下不能用 {remote_host}，那是边缘 IP"
