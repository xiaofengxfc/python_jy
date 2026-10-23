"""t81：一键更新要能重启服务——部署侧补那一条 sudo 权限与文档。

这里钉的是「脚本和文档真的这么写」，不是「文案好看」：dry-run 的输出里必须能看见
sudoers 到底授了哪两条命令（而不是一句「已配置 sudo」），文档里的更新步骤必须和
updater 实际做的事一一对上（fetch / ff-only merge / pip install / 重启）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy" / "install.sh"
SERVICE = ROOT / "deploy" / "bitrader.service"
DEPLOY_DOC = ROOT / "docs" / "DEPLOY-Linux.md"
README = ROOT / "README.md"


def _bash() -> str:
    for candidate in (
        shutil.which("bash"),
        r"D:\tool\Git\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    pytest.skip("这台机器上没有 bash，跳过真跑 deploy/install.sh 的检查")


def _dry_run() -> str:
    """按 Linux 推演一遍安装，拿它打印的「要写入什么」当证据。"""
    env = dict(os.environ)
    env["BI_RUN_DRY_ANYWHERE"] = "1"
    done = subprocess.run(
        [_bash(), str(INSTALL), "--dry-run", "--domain", "x.example.com"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout + done.stderr


def test_the_dry_run_still_walks_all_eleven_steps():
    out = _dry_run()
    for step in range(1, 12):
        assert f"{step}/11 " in out, out[-2000:]


def test_the_dry_run_shows_a_second_nopasswd_rule_for_one_restart():
    """两条 NOPASSWD：一条给签发证书的脚本，一条只给「重启 bitrader」这一下。"""
    out = _dry_run()
    # 只挑真正的规则行：unit 文件里的注释也带着这串字，注释不是授权
    rules = [
        line.strip()
        for line in out.splitlines()
        if "NOPASSWD:" in line and not line.strip().startswith("#")
    ]
    assert any("set-domain.sh" in line for line in rules), rules
    restarts = [line for line in rules if "systemctl" in line]
    assert len(restarts) == 1, rules
    rule = restarts[0]
    assert rule.rstrip().endswith("systemctl restart bitrader"), rule
    # 只授这一条：没有通配符、没有分号，systemctl 的别的子命令都进不来
    assert "*" not in rule, rule
    assert ";" not in rule, rule


def test_the_restart_rule_uses_a_probed_absolute_path():
    out = _dry_run()
    rules = [
        line.strip()
        for line in out.splitlines()
        if "NOPASSWD:" in line and "systemctl" in line and not line.strip().startswith("#")
    ]
    assert rules, out[-2000:]
    path = rules[0].split("NOPASSWD:")[1].split()[0]
    assert path.startswith("/"), rules[0]
    if shutil.which("systemctl") is None:
        # 预览机上没有 systemctl：脚本要说清是按 Debian/Ubuntu 的路径推演的
        assert "探测不到 systemctl" in out, out[-2000:]


def test_the_sudoers_check_still_rolls_back_on_a_syntax_error():
    text = INSTALL.read_text(encoding="utf-8")
    assert "visudo -c -f" in text
    assert 'rm -f "$SUDOERS_FILE"' in text


def test_the_unit_file_explains_why_one_restart_is_allowed():
    text = SERVICE.read_text(encoding="utf-8")
    assert "systemctl restart bitrader" in text
    assert "Restart=always" in text
    assert "NOPASSWD" in text


def test_the_update_section_covers_every_step_the_updater_takes():
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    assert "更新与回滚" in text
    for needle in (
        "git fetch",
        "--ff-only",
        "pip install",
        "systemctl restart bitrader",
        "journalctl -u bitrader",
        "npm",
        "回滚",
    ):
        assert needle in text, needle


def test_the_docs_do_not_pretend_updates_are_automatic():
    for path in (DEPLOY_DOC, README):
        assert "自动更新" not in path.read_text(encoding="utf-8"), path.name


def test_the_readme_mentions_the_online_update():
    text = README.read_text(encoding="utf-8")
    assert "在线更新" in text or "一键更新" in text
