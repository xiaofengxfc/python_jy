"""量化执行引擎与 AI 交易的互斥（INV-14）。

两者共用同一个纸面/实盘账户，同时跑只会互相把仓位改回去，所以启动前先互斥。
"""

from __future__ import annotations

from btrader.web.routes import _engine_clash


def test_ai_running_blocks_the_quant_engine():
    clash = _engine_clash(False, "running", starting="auto")
    assert "AI 交易正在运行" in clash


def test_paused_ai_also_blocks_the_quant_engine():
    assert _engine_clash(False, "paused", starting="auto")


def test_quant_engine_running_blocks_ai_trading():
    clash = _engine_clash(True, "stopped", starting="ai")
    assert "量化执行引擎" in clash


def test_stopped_engines_do_not_clash():
    assert _engine_clash(False, "stopped", starting="auto") == ""
    assert _engine_clash(False, "", starting="ai") == ""
    # 熔断/停止的 AI 实例不算占用账户：它已经不再下单。
    assert _engine_clash(False, "halted", starting="auto") == ""
