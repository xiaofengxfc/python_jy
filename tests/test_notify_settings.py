"""通知设置的读写。

两个失败都是静默的，所以钉在这里：被拒的修改不能把保存好的对象改一半，
重新加载配置不能让运行中的服务指向旧的设置对象。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import HTTPException

from btrader.core.config import ConfigStore
from btrader.notify import NotifySettings
from btrader.web.routes import _apply_notify

URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcd1234efgh5678"


def test_enabling_without_an_address_is_rejected() -> None:
    candidate = replace(NotifySettings(enabled=False, webhook_url=""), enabled=True)
    with pytest.raises(HTTPException, match="Webhook 地址"):
        _apply_notify(candidate, {})


def test_a_rejected_patch_leaves_the_saved_object_untouched() -> None:
    """路由校验的是副本，400 永远碰不到已保存的设置。"""
    saved = NotifySettings(enabled=False, webhook_url="")
    candidate = replace(saved, enabled=True)
    with pytest.raises(HTTPException):
        _apply_notify(candidate, {})
    assert saved.enabled is False


def test_an_address_without_a_scheme_is_rejected() -> None:
    candidate = NotifySettings(enabled=False, webhook_url="")
    with pytest.raises(HTTPException, match="http"):
        _apply_notify(candidate, {"webhook_url": "qyapi.weixin.qq.com/send"})
    assert candidate.webhook_url == ""


def test_an_unknown_message_format_is_rejected() -> None:
    candidate = NotifySettings(enabled=False)
    with pytest.raises(HTTPException, match="消息格式"):
        _apply_notify(candidate, {"msg_format": "telegram"})
    assert candidate.msg_format == "auto"


def test_only_the_given_fields_change() -> None:
    candidate = NotifySettings(enabled=False)
    _apply_notify(
        candidate,
        {"webhook_url": URL, "msg_format": "qyapi_text", "on_task": False},
    )
    assert candidate.webhook_url == URL
    assert candidate.msg_format == "qyapi_text"
    assert candidate.on_task is False
    assert candidate.on_fill is True


def test_unknown_fields_are_ignored() -> None:
    """旧配置文件里的字段会一起被送进来，不能因为认不出来就 500。"""
    candidate = NotifySettings(enabled=False)
    _apply_notify(candidate, {"mode": "openclaw", "target": "XiaoJiaJian"})
    assert not hasattr(candidate, "mode")


def test_notify_settings_round_trip_through_the_config_file(tmp_path) -> None:
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.settings.notify = NotifySettings(
        enabled=True,
        webhook_url=URL,
        msg_format="qyapi_markdown",
        webhook_headers="Authorization: Bearer abc",
        webhook_template="",
        on_task=False,
    )
    store.save()

    loaded = ConfigStore(path).settings.notify
    assert isinstance(loaded, NotifySettings)
    assert loaded.enabled is True
    assert loaded.webhook_url == URL
    assert loaded.msg_format == "qyapi_markdown"
    assert loaded.webhook_headers == "Authorization: Bearer abc"
    assert loaded.on_task is False


def test_old_openclaw_fields_are_dropped_on_load(tmp_path) -> None:
    """旧配置文件里的 mode/target/corp_id 不再有含义，读的时候直接丢掉。"""
    import json

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "notify": {
                    "enabled": True,
                    "mode": "openclaw",
                    "target": "XiaoJiaJian",
                    "corp_id": "",
                    "webhook_url": URL,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = ConfigStore(path).settings.notify
    assert loaded.webhook_url == URL
    assert not hasattr(loaded, "mode")
    assert not hasattr(loaded, "target")

