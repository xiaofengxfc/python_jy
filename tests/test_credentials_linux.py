"""headless Linux 上没有 SecretService/DBus 时，密钥也要存得下、说得清。

Debian/Ubuntu 服务器（systemd 跑后端、没有图形界面）没有 DBus 会话，keyring
会退化成 keyring.backends.fail.Keyring：set_password 直接抛 NoKeyringError。
以前 core/credentials.py 是直接 import keyring 就用，于是服务器上的表现是
「API Key 存不进去」，而且报错是一句英文。

现在的口径（core/secret_store.py）：

* 启动时探测一次真实后端（写一条探针、读回来、删掉），探不明白就落到数据目录的
  secrets.bin：AES-256-GCM，钥匙由 machine-id + 服务名经 PBKDF2 派生，
  文件 600、目录 700；
* 换了机器、文件被手改坏：回空凭据，不抛异常；
* /api/credentials 的 storage 字段告诉设置页现在用的是哪一种存储。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from btrader import llm
from btrader.core import secret_store
from btrader.core.credentials import CredentialStore, Credentials
from btrader.core.types import MarketType, Network
from fastapi import HTTPException

from btrader.web import routes as routes_mod

FAIL_MESSAGE = (
    "No recommended backend was available. Install a third-party keyring backend "
    "that supports your environment (e.g. SecretStorage)"
)


class FailKeyring:
    """keyring.backends.fail.Keyring 的替身：一句话英文 + 抛异常。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_password(self, service: str, name: str):
        self.calls.append("get")
        raise RuntimeError(FAIL_MESSAGE)

    def set_password(self, service: str, name: str, value: str) -> None:
        self.calls.append("set")
        raise RuntimeError(FAIL_MESSAGE)

    def delete_password(self, service: str, name: str) -> None:
        self.calls.append("delete")
        raise RuntimeError(FAIL_MESSAGE)


class WorkingKeyring:
    """能用的系统密钥环（桌面上那种）：字典就行。"""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str):
        return self.items.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.items[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        self.items.pop((service, name), None)


def _use_fail_keyring(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setitem(sys.modules, "keyring", FailKeyring())
    target = tmp_path / "nested" / "secrets.bin"
    monkeypatch.setattr(secret_store, "file_path", lambda: target)
    return target


# ------------------------------------------- 一、坏密钥环自动降级到加密文件
def test_a_broken_keyring_falls_back_to_the_encrypted_file(monkeypatch, tmp_path):
    target = _use_fail_keyring(monkeypatch, tmp_path)
    store = CredentialStore()

    assert store.describe()["kind"] == "file"
    assert "本机文件存储" in store.describe()["label"]
    assert store.save(
        Network.TESTNET, MarketType.SPOT, Credentials("key-abcdefgh", "secret-1234567890")
    )
    creds = store.load(Network.TESTNET, MarketType.SPOT)
    assert creds.api_key == "key-abcdefgh"
    assert creds.api_secret == "secret-1234567890"
    assert target.exists()


def test_the_fallback_file_does_not_keep_the_secret_in_plain_text(monkeypatch, tmp_path):
    target = _use_fail_keyring(monkeypatch, tmp_path)
    store = CredentialStore()
    store.save(
        Network.TESTNET, MarketType.SPOT, Credentials("key-abcdefgh", "secret-1234567890")
    )

    raw = target.read_bytes()
    assert b"secret-1234567890" not in raw
    assert b"key-abcdefgh" not in raw


def test_the_fallback_file_asks_for_600_and_its_directory_700(monkeypatch, tmp_path):
    """权限位：文件 600、目录 700（记录的是 chmod 调用，Windows 上只切只读位）。"""
    target = _use_fail_keyring(monkeypatch, tmp_path)
    seen: list[tuple[str, int]] = []
    real_chmod = os.chmod
    monkeypatch.setattr(
        secret_store.os,
        "chmod",
        lambda path, mode: (seen.append((str(path), mode)), real_chmod(path, mode))[1],
    )

    store = CredentialStore()
    store.save(
        Network.TESTNET, MarketType.SPOT, Credentials("key-abcdefgh", "secret-1234567890")
    )

    modes = dict(seen)
    assert modes[str(target)] == 0o600
    assert modes[str(target.parent)] == 0o700


@pytest.mark.skipif(os.name == "nt", reason="Windows 上 chmod 只切只读位，权限位要在 Linux 上复核")
def test_the_file_mode_really_is_600_on_a_posix_box(monkeypatch, tmp_path):
    target = _use_fail_keyring(monkeypatch, tmp_path)
    store = CredentialStore()
    store.save(Network.TESTNET, MarketType.SPOT, Credentials("key-abcdefgh", "secret-12345"))

    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert oct(target.parent.stat().st_mode & 0o777) == "0o700"


# ------------------------------------------------------- 二、坏文件不许崩
def test_a_corrupt_file_returns_empty_credentials_instead_of_raising(monkeypatch, tmp_path):
    """读回空、不抛异常；但**不许**把「读不出」当成空库接着覆盖（t75）。"""
    target = _use_fail_keyring(monkeypatch, tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00\x01 not json at all \xff")

    store = CredentialStore()
    creds = store.load(Network.TESTNET, MarketType.SPOT)
    assert not creds.is_set
    # 这一次保存必须被拒：拒绝之后旧文件已经留档，再保存才重建一份
    with pytest.raises(secret_store.ReadError):
        store.save(Network.TESTNET, MarketType.SPOT, Credentials("key-abcdefgh", "s" * 12))
    assert list(target.parent.glob("secrets.bin.bad-*"))
    assert store.save(Network.TESTNET, MarketType.SPOT, Credentials("key-abcdefgh", "s" * 12))
    assert store.load(Network.TESTNET, MarketType.SPOT).api_key == "key-abcdefgh"


def test_the_file_cannot_be_decrypted_on_another_machine(tmp_path):
    """钥匙由 machine-id 派生：换台机器拷过去也解不开。"""
    path = tmp_path / "secrets.bin"
    here = secret_store.FileSecretBackend(path=path, machine="machine-a")
    here.set_password("svc", "api_key", "key-abcdefgh")

    again = secret_store.FileSecretBackend(path=path, machine="machine-a")
    assert again.get_password("svc", "api_key") == "key-abcdefgh"

    elsewhere = secret_store.FileSecretBackend(path=path, machine="machine-b")
    assert not elsewhere.get_password("svc", "api_key")

# ============================================ 二之二、读不出 ≠ 空库（t75，P0）
# 改前：_read_all 三条失败路都 return {}，而 set_password 是「读整份 → 改一条 → 写回整份」。
# 于是把 secrets.bin 截一半、或者 machine-id 变了之后，一次正常保存就把交易所 Key 和
# AI Key 一起清掉，界面还报「保存成功」。现在：读不出 = ReadError，写入拒绝，旧文件留一份
# secrets.bin.bad-<时间戳> 备份。文件不存在才是合法的空库。
def _seed_two_keys(path: Path, machine: str = "machine-a") -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    backend = secret_store.FileSecretBackend(path=path, machine=machine)
    backend.set_password("svc", "api_key", "key-abcdefgh")
    backend.set_password("svc", "api_secret", "secret-123456")
    return path.read_bytes()


def _truncate(path: Path) -> bytes:
    good = path.read_bytes()
    half = good[: len(good) // 2]
    path.write_bytes(half)
    return half


def test_a_truncated_file_refuses_to_be_overwritten(tmp_path):
    """截一半之后保存：抛错、旧文件改成 .bad-*、原字节一个不少。"""
    path = tmp_path / "secrets.bin"
    _seed_two_keys(path)
    half = _truncate(path)

    backend = secret_store.FileSecretBackend(path=path, machine="machine-a")
    with pytest.raises(secret_store.ReadError) as caught:
        backend.set_password("svc", "api_key", "new-key-ffff")

    message = str(caught.value)
    backups = sorted(tmp_path.glob("secrets.bin.bad-*"))
    assert len(backups) == 1, backups
    assert backups[0].name in message  # 报错里说清备份到哪了
    assert "读不出" in message
    assert "本次没有写入" in message
    assert backups[0].read_bytes() == half  # 坏文件的字节原样留档
    # 拒绝覆盖：原路径上没有被写进新的一份
    assert not path.exists()
    assert not list(tmp_path.glob("secrets.bin.tmp*"))


def test_the_next_save_after_a_backup_starts_a_clean_file(tmp_path):
    """留档之后下一次保存重新起一份：只装新写的那个 Key，备份还在。"""
    path = tmp_path / "secrets.bin"
    _seed_two_keys(path)
    half = _truncate(path)
    backend = secret_store.FileSecretBackend(path=path, machine="machine-a")
    with pytest.raises(secret_store.ReadError):
        backend.set_password("svc", "api_key", "new-key-ffff")

    backend.set_password("svc", "api_key", "new-key-ffff")
    assert backend.get_password("svc", "api_key") == "new-key-ffff"
    assert backend.get_password("svc", "api_secret") == ""
    backups = sorted(tmp_path.glob("secrets.bin.bad-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == half


def test_a_changed_machine_id_goes_down_the_same_path(tmp_path):
    """machine-id 变了就是「解不开」：读回空、保存拒绝、留档。"""
    path = tmp_path / "secrets.bin"
    _seed_two_keys(path, machine="machine-a")
    moved = secret_store.FileSecretBackend(path=path, machine="machine-b")

    assert moved.get_password("svc", "api_key") == ""
    with pytest.raises(secret_store.ReadError):
        moved.set_password("svc", "api_key", "key-from-the-new-box")
    assert list(tmp_path.glob("secrets.bin.bad-*"))


def test_reading_a_broken_file_warns_and_leaves_the_file_alone(tmp_path, caplog):
    """读路径只落 warning，不许动磁盘（留档是写路径的事）。"""
    path = tmp_path / "secrets.bin"
    _seed_two_keys(path)
    _truncate(path)
    backend = secret_store.FileSecretBackend(path=path, machine="machine-a")

    with caplog.at_level(logging.WARNING, logger=secret_store.__name__):
        assert backend.get_password("svc", "api_key") == ""

    assert any("读不出" in record.getMessage() for record in caplog.records)
    assert path.exists()
    assert not list(tmp_path.glob("secrets.bin.bad-*"))


def test_an_empty_file_is_not_a_missing_file(tmp_path):
    """0 字节的文件 = 读不出；文件不存在 = 合法的空库。两者不能混。"""
    empty = tmp_path / "empty" / "secrets.bin"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_bytes(b"")
    broken = secret_store.FileSecretBackend(path=empty, machine="machine-a")
    assert broken.get_password("svc", "api_key") == ""
    with pytest.raises(secret_store.ReadError):
        broken.set_password("svc", "api_key", "k-1")

    missing = tmp_path / "missing" / "secrets.bin"
    fresh = secret_store.FileSecretBackend(path=missing, machine="machine-a")
    assert fresh.get_password("svc", "api_key") == ""
    fresh.set_password("svc", "api_key", "k-1")
    assert fresh.get_password("svc", "api_key") == "k-1"
    assert not list(missing.parent.glob("secrets.bin.bad-*"))


def test_deleting_a_key_from_a_broken_file_is_refused_too(tmp_path):
    """删除也是「读整份 → 改 → 写回」：读不出就不许动磁盘。"""
    path = tmp_path / "secrets.bin"
    _seed_two_keys(path)
    half = _truncate(path)
    backend = secret_store.FileSecretBackend(path=path, machine="machine-a")

    with pytest.raises(secret_store.ReadError):
        backend.delete_password("svc", "api_key")
    assert path.read_bytes() == half
    assert not list(tmp_path.glob("secrets.bin.bad-*"))


def test_the_backup_is_asked_for_600_and_the_directory_stays_700(monkeypatch, tmp_path):
    """备份文件的权限要和原件一样。"""
    recorded: list[tuple[str, int]] = []
    monkeypatch.setattr(
        secret_store,
        "_chmod",
        lambda path, mode: recorded.append((Path(path).name, mode)),
    )
    path = tmp_path / "secrets.bin"
    _seed_two_keys(path)
    _truncate(path)
    backend = secret_store.FileSecretBackend(path=path, machine="machine-a")
    with pytest.raises(secret_store.ReadError):
        backend.set_password("svc", "api_key", "k-2")

    assert any(".bad-" in name and mode == 0o600 for name, mode in recorded), recorded


def test_the_credentials_store_surfaces_the_refusal(monkeypatch, tmp_path):
    """CredentialStore.save 不再把拒绝吞成 False：调用方拿得到那句话。"""
    target = _use_fail_keyring(monkeypatch, tmp_path)
    store = CredentialStore()
    assert store.save(
        Network.TESTNET, MarketType.SPOT, Credentials("key-abcdefgh", "secret-1234")
    )
    _truncate(target)

    with pytest.raises(secret_store.ReadError) as caught:
        store.save(
            Network.TESTNET, MarketType.SPOT, Credentials("key-new-0000", "secret-5678")
        )
    assert "读不出" in str(caught.value)
    # 读路径照旧回空凭据，不抛
    assert not store.load(Network.TESTNET, MarketType.SPOT).is_set


def test_the_save_api_shows_the_backend_message(monkeypatch, tmp_path):
    """界面看到的是后端那句话：不是「保存成功」，也不是我们编的解释。"""
    target = _use_fail_keyring(monkeypatch, tmp_path)
    store = CredentialStore()
    store.save(Network.TESTNET, MarketType.SPOT, Credentials("key-abcdefgh", "secret-1234"))
    _truncate(target)

    class _Ctx:
        network = Network.TESTNET
        market_type = MarketType.USDM
        platform = "binance"
        credentials = store
        creds = Credentials()

    state = SimpleNamespace(
        ctx=_Ctx(),  # type: ignore[arg-type]
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    endpoint = None
    for route in routes_mod.build_router(lambda: state).routes:
        if getattr(route, "path", "") == "/api/credentials" and "POST" in getattr(
            route, "methods", ()
        ):
            endpoint = route.endpoint
            break
    assert endpoint is not None

    with pytest.raises(HTTPException) as caught:
        endpoint(
            routes_mod.CredentialsPayload(
                market=MarketType.SPOT.value,
                network=Network.TESTNET.value,
                api_key="key-new-0000",
                api_secret="secret-5678",
                verify=False,
            )
        )
    assert caught.value.status_code == 500
    detail = str(caught.value.detail)
    assert "读不出" in detail, detail
    assert ".bad-" in detail, detail


# --------------------------------------------- 三、能用的密钥环优先，探针不留痕
def test_a_working_keyring_is_preferred_and_no_file_is_written(monkeypatch, tmp_path):
    backend = WorkingKeyring()
    monkeypatch.setitem(sys.modules, "keyring", backend)
    target = tmp_path / "secrets.bin"
    monkeypatch.setattr(secret_store, "file_path", lambda: target)

    store = CredentialStore()
    assert store.describe()["kind"] == "keyring"
    assert "系统密钥环" in store.describe()["label"]
    assert store.save(
        Network.TESTNET, MarketType.SPOT, Credentials("key-abcdefgh", "secret-1234567890")
    )
    assert store.load(Network.TESTNET, MarketType.SPOT).api_key == "key-abcdefgh"
    assert not target.exists()


def test_the_probe_does_not_leave_its_test_entry_behind(monkeypatch, tmp_path):
    backend = WorkingKeyring()
    monkeypatch.setitem(sys.modules, "keyring", backend)
    monkeypatch.setattr(secret_store, "file_path", lambda: tmp_path / "secrets.bin")

    secret_store.open_store()
    assert backend.items == {}


# --------------------------------- 四、设置页能看出当前是哪种存储（接口字段）
def test_the_settings_api_reports_which_storage_is_in_use(monkeypatch, tmp_path):
    _use_fail_keyring(monkeypatch, tmp_path)
    store = CredentialStore()
    store.save(Network.TESTNET, MarketType.SPOT, Credentials("key-abcdefgh", "secret-1234567890"))

    class _Ctx:
        network = Network.TESTNET
        market_type = MarketType.USDM
        credentials = store

    state = SimpleNamespace(
        ctx=_Ctx(),  # type: ignore[arg-type]
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    endpoint = None
    for route in routes_mod.build_router(lambda: state).routes:
        if getattr(route, "path", "") == "/api/credentials" and "GET" in getattr(
            route, "methods", ()
        ):
            endpoint = route.endpoint
            break
    assert endpoint is not None

    data = endpoint(network=None)
    assert data["storage"]["kind"] == "file"
    assert "本机文件存储" in data["storage"]["label"]
    assert data["storage"]["path"]
    assert data["storage"]["detail"]
    configured = {item["market"]: item["configured"] for item in data["items"]}
    assert configured[MarketType.SPOT.value] is True

# ======================================================= 五、AI 助手的 Key
# 同一个坑换了个地方：llm.py 的 load_key/save_key 以前也直接 import keyring，
# 服务器上「AI 助手的 Key 存不进去」。现在走 core/secret_store 的同一个 store，
# 但 service 名必须还是 biTrader-ai —— 改了等于把已经存进去的 Key 弄丢。
AI_KEY = "sk-live-abcdef123456"


def _reset_ai_store(monkeypatch) -> None:
    """llm 里那份 store 是进程级缓存的：换替身之前先忘掉它。"""
    monkeypatch.setattr(llm, "_STORE", None)


def test_the_ai_key_falls_back_to_the_encrypted_file(monkeypatch, tmp_path):
    target = _use_fail_keyring(monkeypatch, tmp_path)
    _reset_ai_store(monkeypatch)

    assert llm.save_key(AI_KEY) is True
    assert llm.load_key() == AI_KEY
    assert target.exists()
    assert AI_KEY.encode() not in target.read_bytes()
    assert llm.storage_info()["kind"] == "file"
    assert "本机文件存储" in llm.storage_info()["label"]


def test_the_ai_key_prefers_a_working_keyring(monkeypatch, tmp_path):
    backend = WorkingKeyring()
    monkeypatch.setitem(sys.modules, "keyring", backend)
    target = tmp_path / "secrets.bin"
    monkeypatch.setattr(secret_store, "file_path", lambda: target)
    _reset_ai_store(monkeypatch)

    assert llm.save_key(AI_KEY) is True
    assert llm.load_key() == AI_KEY
    assert llm.storage_info()["kind"] == "keyring"
    assert not target.exists()


def test_the_ai_key_still_lives_in_the_bitrader_ai_service(monkeypatch, tmp_path):
    """service 名钉死：改名字等于让老用户的 Key 凭空消失。"""
    assert llm.KEYRING_SERVICE == "biTrader-ai"
    assert llm.KEYRING_USER == "api_key"

    backend = WorkingKeyring()
    monkeypatch.setitem(sys.modules, "keyring", backend)
    monkeypatch.setattr(secret_store, "file_path", lambda: tmp_path / "secrets.bin")
    _reset_ai_store(monkeypatch)

    llm.save_key(AI_KEY)
    assert backend.items == {("biTrader-ai", "api_key"): AI_KEY}


def test_an_ai_key_saved_before_this_change_is_read_back(monkeypatch, tmp_path):
    """旧版本存进系统密钥环的 Key，第一次读就得回来（没有迁移步骤）。"""
    backend = WorkingKeyring()
    backend.set_password("biTrader-ai", "api_key", "sk-old-key-0001")
    monkeypatch.setitem(sys.modules, "keyring", backend)
    monkeypatch.setattr(secret_store, "file_path", lambda: tmp_path / "secrets.bin")
    _reset_ai_store(monkeypatch)

    assert llm.load_key() == "sk-old-key-0001"


def test_saving_an_empty_ai_key_deletes_it(monkeypatch, tmp_path):
    """保存空串＝删除，删完再读是空串，全程不抛给界面。"""
    target = _use_fail_keyring(monkeypatch, tmp_path)
    _reset_ai_store(monkeypatch)

    assert llm.save_key(AI_KEY) is True
    assert llm.load_key() == AI_KEY

    assert llm.save_key("") is True
    assert llm.load_key() == ""
    assert AI_KEY.encode() not in target.read_bytes()


def test_a_broken_ai_store_does_not_raise(monkeypatch, tmp_path):
    """存储整个坏掉：读回空串、保存回 False，界面拿到的永远不是异常。"""

    class BrokenStore:
        def get_password(self, service, username):
            raise RuntimeError("boom")

        def set_password(self, service, username, value):
            raise RuntimeError("boom")

        def delete_password(self, service, username):
            raise RuntimeError("boom")

    monkeypatch.setattr(llm, "_STORE", BrokenStore())

    assert llm.load_key() == ""
    assert llm.save_key(AI_KEY) is False
    assert llm.save_key("") is True  # 删除失败不算保存失败


def test_the_ai_status_reports_where_the_key_lives(monkeypatch, tmp_path):
    """设置页那张 AI 卡读 /api/ai/status，里面要能看出 Key 存在哪。"""
    _use_fail_keyring(monkeypatch, tmp_path)
    monkeypatch.setenv("BITRADER_DATA_DIR", str(tmp_path / "data"))
    _reset_ai_store(monkeypatch)

    service = llm.LLMService(llm.AISettings(enabled=True, provider="deepseek"))
    status = service.status()

    assert status["storage"]["kind"] == "file"
    assert "本机文件存储" in status["storage"]["label"]
    assert status["storage"]["detail"]


def test_the_ai_key_api_surfaces_the_refusal(monkeypatch, tmp_path):
    """AI 卡保存时也给后端原话：不是「密钥环不可用」那句替用户猜的。"""
    target = _use_fail_keyring(monkeypatch, tmp_path)
    _reset_ai_store(monkeypatch)
    assert llm.save_key(AI_KEY) is True
    _truncate(target)
    _reset_ai_store(monkeypatch)

    class _Ctx:
        network = Network.TESTNET
        ai = llm.LLMService(llm.AISettings(enabled=True, provider="deepseek"))

    state = SimpleNamespace(
        ctx=_Ctx(),  # type: ignore[arg-type]
        locks={market: threading.RLock() for market in MarketType},
        account_lock=threading.RLock(),
    )
    endpoint = None
    for route in routes_mod.build_router(lambda: state).routes:
        if getattr(route, "path", "") == "/api/ai/key":
            endpoint = route.endpoint
            break
    assert endpoint is not None

    with pytest.raises(HTTPException) as caught:
        endpoint(routes_mod.AIKeyPayload(key="sk-new-0000"))
    detail = str(caught.value.detail)
    assert "读不出" in detail, detail
    assert ".bad-" in detail, detail
