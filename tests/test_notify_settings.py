"""Settings plumbing for the WeChat push.

Two failure modes are pinned here because both are silent: an update that is
rejected must not leave the live object half-changed, and a reloaded config must
not leave the running services pointing at a stale nested settings object.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import HTTPException

from btrader.core.config import AppSettings, ConfigStore, RiskSettings
from btrader.notify import NotifySettings
from btrader.web.routes import _apply_notify


def test_enabling_without_a_target_is_rejected() -> None:
    candidate = replace(NotifySettings(enabled=False, target=""), enabled=True)
    with pytest.raises(HTTPException, match="userid"):
        _apply_notify(candidate, {})


def test_a_rejected_patch_leaves_the_saved_object_untouched() -> None:
    """The route validates a copy, so a 400 never reaches live settings."""
    saved = NotifySettings(enabled=False, target="")
    candidate = replace(saved, enabled=True)
    with pytest.raises(HTTPException):
        _apply_notify(candidate, {})
    assert saved.enabled is False


def test_notify_settings_round_trip_through_the_config_file(tmp_path) -> None:
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.settings.notify = NotifySettings(
        enabled=True, target="wxid-9", channel="weixin", command=r"C:\npm\openclaw.cmd"
    )
    store.save()

    loaded = ConfigStore(path).settings.notify
    assert isinstance(loaded, NotifySettings)
    assert loaded.enabled is True
    assert loaded.target == "wxid-9"
    assert loaded.channel == "weixin"


def test_unknown_notify_keys_are_ignored() -> None:
    target = NotifySettings()
    _apply_notify(target, {"nonexistent_field": 1, "target": "wxid-1"})
    assert target.target == "wxid-1"


def test_missing_notify_block_keeps_defaults(tmp_path) -> None:
    """A config written before this feature existed must still load."""
    path = tmp_path / "config.json"
    path.write_text('{"network": "testnet"}', encoding="utf-8")
    settings = ConfigStore(path).settings
    assert isinstance(settings.notify, NotifySettings)
    assert settings.notify.enabled is False
    assert settings.notify.channel


def test_attach_settings_rebinds_replaced_nested_objects() -> None:
    """Reloading replaces ``risk``/``notify``; services must follow them.

    ``AppContext`` keeps both by reference. After a reload the old object is
    gone, so a service still holding it would silently ignore every later
    change — the exact bug that made a saved breaker limit inert.
    """

    class Risk:
        def __init__(self, settings: RiskSettings) -> None:
            self.settings = settings

    class Notifier:
        def __init__(self, settings: NotifySettings) -> None:
            self.settings = settings

    fresh = AppSettings()
    risk = Risk(RiskSettings())
    notifier = Notifier(NotifySettings())

    # The body of ``AppContext._attach_settings``.
    risk.settings = fresh.risk
    notifier.settings = fresh.notify

    assert risk.settings is fresh.risk
    assert notifier.settings is fresh.notify
