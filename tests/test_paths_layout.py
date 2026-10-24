"""数据目录按构建形态分：桌面版 `biTrader`，源码运行 `biTrader-dev`。

这条行为对外只有一个后果：装好的桌面端不该看到开发时造的测试数据，反之亦然。
判据只认 `sys.frozen`——壳参数说明的是「谁拉起了它」，electron 的 dev 模式同样是壳
拉起来的，跑的却是仓库源码，它落开发目录。
"""

from __future__ import annotations

from btrader import paths


def _no_override(monkeypatch, tmp_path):
    # 夹具 isolated_data_dir 会设 BITRADER_DATA_DIR，这里要看默认分支，先清掉
    monkeypatch.delenv("BITRADER_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(paths.sys, "platform", "win32")


def test_a_frozen_build_uses_the_desktop_directory(tmp_path, monkeypatch):
    _no_override(monkeypatch, tmp_path)
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    assert paths._base_dir() == tmp_path / "biTrader"
    assert paths.data_dir_label() == "desktop"


def test_running_from_source_uses_the_dev_directory(tmp_path, monkeypatch):
    _no_override(monkeypatch, tmp_path)
    monkeypatch.delattr(paths.sys, "frozen", raising=False)
    assert paths._base_dir() == tmp_path / "biTrader-dev"
    assert paths.data_dir_label() == "dev"


def test_the_override_wins_over_both(tmp_path, monkeypatch):
    _no_override(monkeypatch, tmp_path)
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path / "custom"))
    assert paths._base_dir() == tmp_path / "custom"
