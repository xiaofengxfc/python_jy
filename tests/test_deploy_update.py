"""t81：一键更新要能重启服务——部署侧补那一条 sudo 权限与文档。

这里钉的是「脚本和文档真的这么写」，不是「文案好看」：dry-run 的输出里必须能看见
sudoers 到底授了哪两条命令（而不是一句「已配置 sudo」），文档里的更新步骤必须和
updater 实际做的事一一对上（fetch / ff-only merge / pip install / 重启）。
"""

from __future__ import annotations

import os
import re
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


def _dry_run(*extra: str) -> str:
    """按 Linux 推演一遍安装，拿它打印的「要写入什么」当证据。"""
    env = dict(os.environ)
    env["BI_RUN_DRY_ANYWHERE"] = "1"
    done = subprocess.run(
        [_bash(), str(INSTALL), "--dry-run", "--domain", "x.example.com", *extra],
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
    assert rule.rstrip().endswith(" ".join(_updater_restart_tokens())), rule
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


def _dry_run_rejects(*extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["BI_RUN_DRY_ANYWHERE"] = "1"
    return subprocess.run(
        [_bash(), str(INSTALL), "--dry-run", *extra],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def test_the_port_reaches_the_caddyfile_and_the_domain_script():
    """--port 不打通的话：装完界面能开、接口全 502，点一次「签发证书」还会被改回 8765。"""
    out = _dry_run("--port", "9000")
    assert "reverse_proxy 127.0.0.1:9000" in out, out[-3000:]
    assert "ExecStart=/opt/bitrader/.venv/bin/python run_web.py --host 127.0.0.1 --port 9000" in out
    assert "PORT=9000" in out, "port.conf 要写对，set-domain.sh 靠它"
    assert "127.0.0.1:8765" not in out, "还有地方写着默认端口"


def test_the_default_port_still_works():
    out = _dry_run()
    assert "reverse_proxy 127.0.0.1:8765" in out
    assert "PORT=8765" in out


def test_bad_arguments_are_refused_before_anything_is_written():
    for bad in (
        ("--domain", "1.2.3.4"),
        ("--domain", "a&b.example.com"),
        ("--domain", "http://x.example.com"),
        ("--domain", "python_jy.789446.xyz"),
        ("--port", "abc"),
        ("--port", "70000"),
        ("--repo", "https://example.com/a b.git"),
    ):
        done = _dry_run_rejects(*bad)
        assert done.returncode != 0, bad
        output = done.stdout + done.stderr
        assert "[dry-run]" not in output, "参数没过关就不许开始写任何东西：%r" % (bad,)


def test_a_domain_with_an_underscore_names_the_character_and_the_fix():
    """DNS 认下划线、证书不认。报一句笼统的「写法不对」等于让人去猜，要把这一条点名。"""
    done = _dry_run_rejects("--domain", "python_jy.789446.xyz")
    assert done.returncode != 0
    output = done.stdout + done.stderr
    assert "下划线" in output, output
    assert "python-jy.789446.xyz" in output, "要给出连字符写法"
    assert "rejectedIdentifier" in output, "说清是谁拒的，别只说不合规矩"
    assert "--domain 写法不对" not in output, "别退回那句笼统的"
    assert "[dry-run]" not in output


def test_the_ip_comes_from_the_cloud_metadata_before_a_public_service():
    text = INSTALL.read_text(encoding="utf-8")
    for needle in ("100.100.100.200", "metadata.tencentyun.com", "169.254.169.254", "api.ipify.org"):
        assert needle in text, needle
    # 查不到公网 IP 就把那段整段删掉，别把占位符原样写进 Caddyfile
    assert "REPLACE_WITH_SERVER_IP {/,/^}$/d" in text


def test_the_installer_fast_forwards_instead_of_resetting():
    """和 updater 同一语义：只快进。reset --hard 只留给「补 .git」的那一次迁移。"""
    text = INSTALL.read_text(encoding="utf-8")
    assert "merge --ff-only FETCH_HEAD" in text
    assert text.count("reset --hard") == 1, text.count("reset --hard")
    assert "init -q" in text, "rsync 装过的机器要能补上 .git"


def test_the_clock_is_set_up_unless_something_else_already_does_it():
    text = INSTALL.read_text(encoding="utf-8")
    assert "chrony" in text
    assert "systemd-timesyncd" in text, "已经有时间服务就别为了装 chrony 把它删掉"


def test_the_unit_file_explains_why_one_restart_is_allowed():
    text = SERVICE.read_text(encoding="utf-8")
    assert " ".join(_updater_restart_tokens()) in text
    assert "Restart=always" in text
    assert "NOPASSWD" in text


def test_the_update_section_covers_every_step_the_updater_takes():
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    assert "更新与回滚" in text
    for needle in (
        "git fetch",
        "--ff-only",
        "pip install",
        " ".join(_updater_restart_tokens()),
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

# ---------------------------------------------------------------- t86：口径

AGENTS = ROOT / "AGENTS.md"


def test_the_rollback_section_teaches_revert_not_reset():
    """回滚教的是 revert（不改写历史），不是 reset --hard——程序里也是 revert。"""
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    # 只禁「当命令教」：解释里说一句「为什么不用它」是可以的，敲出来才是错的
    commands = [
        line.strip()
        for line in text.splitlines()
        if "reset --hard" in line and line.strip().split()[0] in {"sudo", "git", "cd"}
    ]
    assert commands == [], f"文档里还在教 reset --hard：{commands}"
    assert "git revert --no-edit" in text
    assert "为什么用 `revert`" in text or "为什么是 `revert`" in text


def test_the_update_button_is_called_immediate_update_everywhere():
    """界面上的按钮叫「立即更新」，文档跟着叫，别一处一个名字。"""
    for path in (DEPLOY_DOC, README):
        text = path.read_text(encoding="utf-8")
        assert "更新到最新" not in text, path.name
        assert "立即更新" in text, path.name


def test_the_docs_say_backtests_do_not_block_an_update():
    """探针表里只有自动交易与 AI 交易：回测在跑不拦，文档不许说拦了。"""
    for path in (DEPLOY_DOC, README):
        text = path.read_text(encoding="utf-8")
        assert "回测" in text and "不拦" in text, path.name


def test_agents_lists_every_resettable_section_the_code_knows():
    """AGENTS §9 的 RESETTABLE_SECTIONS 清单要和 routes 里的那份一致。"""
    from btrader.web import routes

    text = AGENTS.read_text(encoding="utf-8")
    head = text.index("RESETTABLE_SECTIONS`（")
    listed = {
        part.strip()
        for part in text[head : text.index("）", head)].split("（", 1)[1].split("/")
    }
    assert listed == set(routes.RESETTABLE_SECTIONS), (
        f"AGENTS 的清单和代码不一致：文档 {sorted(listed)} / 代码 {sorted(routes.RESETTABLE_SECTIONS)}"
    )


def test_agents_counts_trade_among_the_finite_number_sections():
    """trade 的杠杆也过 `math.isfinite`，那份分段清单要带上它。"""
    text = AGENTS.read_text(encoding="utf-8")
    start = text.index("数字字段一律只收有限数")
    end = text.index("POST /api/settings/reset", start)
    block = text[start:end]
    assert "`trade`" in block


def test_agents_describes_the_shutdown_wiring_that_actually_runs():
    """§11 那句要说清两条启动路径共用一份装配，而不是只提 web/__main__。"""
    text = AGENTS.read_text(encoding="utf-8")
    assert "build_server" in text
    assert "run_web.py" in text
    start = text.index("两条启动路径")
    block = text[start : start + 400]
    assert "set_shutdown_hook" in block

# ------------------------------------------- t91：sudoers 与重启命令逐字对齐

UPDATER = ROOT / "src" / "btrader" / "core" / "updater.py"


def _updater_restart_tokens() -> list[str]:
    """从 core/updater.py 里读出那条重启命令的命令词（`sudo -n` 之后那一串）。

    断言不抄一份字面量：sudoers 的判据是「和代码真会敲的那条一模一样」。抄一份就等于
    同一个命令有两处各写一半——代码加了 `--no-block`、sudoers 忘改，sudo 直接拒，
    而界面显示的是「服务已重启」。
    """
    text = UPDATER.read_text(encoding="utf-8")
    match = re.search(r'\[\s*"sudo"\s*,\s*"-n"\s*,\s*([^\]]+)\]', text)
    assert match, "core/updater.py 里找不到那条 sudo -n 重启命令"
    tokens: list[str] = []
    for literal, name in re.findall(r'"([^"]+)"|([A-Za-z_]\w*)', match.group(1)):
        if literal:
            tokens.append(literal)
            continue
        const = re.search(rf'^{name}\s*=\s*"([^"]+)"', text, re.M)
        assert const, f"认不出 updater 里的常量 {name}，sudoers 没法比对"
        tokens.append(const.group(1))
    # match 抓的是 sudo -n 之后那一段，第一个命令词必须就是 systemctl
    assert tokens and tokens[0].endswith("systemctl"), tokens
    return tokens


def _sudoers_restart_rules(out: str) -> list[str]:
    """dry-run 输出里真正的 systemctl 授权行（注释不是授权）。"""
    return [
        line.strip()
        for line in out.splitlines()
        if "NOPASSWD:" in line and "systemctl" in line and not line.strip().startswith("#")
    ]


def test_the_sudoers_rule_is_the_command_the_updater_actually_runs():
    """逐字一致：少一个 `--no-block` 就被 sudo 拒，而那一步看起来是成功的。"""
    command = _updater_restart_tokens()
    rules = _sudoers_restart_rules(_dry_run())
    assert len(rules) == 1, rules
    granted = rules[0].split("NOPASSWD:")[1].split()
    assert granted[0].startswith("/"), rules[0]
    assert Path(granted[0]).name == command[0], rules[0]
    assert granted[1:] == command[1:], (
        f"sudoers 授的是 {' '.join(granted[1:])}，代码里敲的是 {' '.join(command[1:])}"
    )


def test_the_grant_is_still_only_that_one_restart():
    """对齐之后仍然是「一条命令、无通配、无分号」：start / stop / 别的单元都进不来。"""
    command = _updater_restart_tokens()
    rules = _sudoers_restart_rules(_dry_run())
    assert len(rules) == 1, rules
    rule = rules[0]
    for forbidden in ("*", "?", ";", "systemctl start", "systemctl stop", "systemctl disable"):
        assert forbidden not in rule, rule
    granted = rule.split("NOPASSWD:")[1].split()
    # 一条命令，一个参数都不多不少
    assert Path(granted[0]).name == command[0], granted
    assert granted[1:] == command[1:], granted


def test_the_unit_file_comment_shows_the_same_rule():
    """unit 文件里那句注释也要跟着改，否则下一个人照着注释改回旧命令。"""
    text = SERVICE.read_text(encoding="utf-8")
    shown = [line for line in text.splitlines() if "NOPASSWD:" in line]
    assert shown, text
    command = " ".join(_updater_restart_tokens())
    assert any(line.split("NOPASSWD:")[1].strip().endswith(command) for line in shown), shown


def test_the_deploy_doc_shows_the_same_rule():
    """§7.2 抄的那份 sudoers 内容要和代码一致（不然用户照着核对会以为没授）。"""
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    shown = [line for line in text.splitlines() if "NOPASSWD:" in line and "systemctl" in line]
    assert shown, text
    command = " ".join(_updater_restart_tokens())
    assert any(line.split("NOPASSWD:")[1].strip().endswith(command) for line in shown), shown


def test_the_program_restart_is_not_described_as_blocking_on_linux():
    """`--no-block` 的意义要写出来：老进程不等重启结果，确认在新进程里做。"""
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    start = text.index("## 7. 更新与回滚")
    block = text[start : text.index("### 7.1", start)]
    assert "--no-block" in block, block
    assert "TimeoutStopSec" in block, block
    assert "logs/restart-" in block, block


def test_the_update_section_lists_the_six_steps_the_pipeline_runs():
    """§7 写的是「四件事」，实际六步：更新卡片上就是六格。"""
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    start = text.index("## 7. 更新与回滚")
    block = text[start : text.index("### 7.1", start)]
    assert "四件事" not in block, "§7 还写着四件事"
    assert "六步" in block or "六件事" in block, block[:400]
    numbered = [
        match.group(1)
        for match in re.finditer(r"^\s*(\d+)\.\s", block, re.M)
    ]
    assert numbered == ["1", "2", "3", "4", "5", "6"], numbered
    for needle in ("git fetch", "--ff-only", "pip install", "npm", "health"):
        assert needle in block, needle


def test_the_agents_update_section_does_not_hardcode_the_port():
    """§11 不许再写死端口：同节后文说「端口跟着真实进程走」，两处自相矛盾。"""
    text = AGENTS.read_text(encoding="utf-8")
    start = text.index("## 11. 在线更新")
    block = text[start:]
    offenders = [
        line.strip()
        for line in block.splitlines()
        if "8765" in line and "兜底" not in line
    ]
    assert offenders == [], f"§11 里还写死了端口：{offenders}"
    assert "web_port()" in block, block[-400:]


def test_the_agents_update_section_documents_the_lock_and_the_late_confirmation():
    """更新锁与「重启确认在新进程里做」都要写进 §11（t88 的缺口）。"""
    text = AGENTS.read_text(encoding="utf-8")
    start = text.index("## 11. 在线更新")
    block = text[start:]
    assert "update.lock" in block, "没写更新锁"
    assert "文件锁" in block, "没写锁是两层"
    assert "_heal_state" in block and "api/health" in block, "没写重启确认在新进程里做"
    assert "TimeoutStopSec" in block, "没写为什么不能在老进程里等"
