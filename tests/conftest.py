"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import make_candles  # noqa: E402


@pytest.fixture
def candles() -> pd.DataFrame:
    return make_candles()


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch, tmp_path):
    """整轮测试都不许真的发 HTTP。

    通知只走一条 Webhook，发送就是一次真实的 POST：不拦住的话，跑测试会给
    某个群发消息，还会因为本机网络不同而时绿时红。需要验证发送的测试自己
    替换 ``notify._post_json``。
    """
    from btrader import notify

    def refuse(url, body, **kwargs):
        raise AssertionError(f"测试里不该发真实请求：{url}")

    monkeypatch.setattr(notify, "_post_json", refuse)
    # 暂存文件也一样：测试写的补发消息不能留在真实数据目录里，
    # 否则下一次 status() 会报出别的测试留下的条数。
    monkeypatch.setattr(notify, "_spool_path", lambda: tmp_path / "notify-spool.jsonl")

