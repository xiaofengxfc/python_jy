"""密钥存放的两种落点：系统密钥环，或本机的加密文件。

Windows 凭据管理器、macOS 钥匙串、带 GNOME/KDE 会话的 Linux 桌面都有可用的
keyring 后端。Debian/Ubuntu 服务器上这些都没有：没有图形界面、没有 DBus 会话，
keyring 会退化成 keyring.backends.fail.Keyring，set_password 直接抛
NoKeyringError，界面上表现为「API Key 存不进去」，报错还是一句英文。

所以这里不直接 import keyring 就用，而是：

1. 探测：往系统密钥环写一条探针、读回来、删掉。能过就用系统密钥环；
2. 探不过（或者 keyring 根本没装）就落到数据目录下的 secrets.bin：
   AES-256-GCM 加密，钥匙由 /etc/machine-id（缺失时退回 /var/lib/dbus/machine-id）
   加服务名，经 PBKDF2-HMAC-SHA256 派生；文件 600、目录 700，
   写入走临时文件 + 原子替换；
3. 两种后端都有 get_password / set_password / delete_password，
   CredentialStore 不用关心是哪一种，只多问一句 describe()。

文件后端的边界写在设置页上：钥匙由本机 machine-id 派生，所以同一台机器上的
别的系统用户读不到，把文件拷到别的机器上也解不开。反过来说，本机 root 读得到，
这一点不如系统密钥环，所以能探测到密钥环就优先用密钥环。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import platform
import uuid
from pathlib import Path
from typing import Any

from .. import APP_NAME
from .. import paths

log = logging.getLogger(__name__)

KIND_KEYRING = "keyring"
KIND_FILE = "file"
KIND_NONE = "none"

SECRET_FILE_NAME = "secrets.bin"
PROBE_SERVICE = f"{APP_NAME}-probe"
PROBE_USER = "probe"
KDF_SALT_LABEL = f"{APP_NAME}-secrets-v1"
KDF_ITERATIONS = 260_000
MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))


def file_path() -> Path:
    """文件后端的位置：跟着数据目录走。"""
    return paths.data_dir() / SECRET_FILE_NAME


def machine_id() -> str:
    """本机身份。Linux 上是 machine-id，其它平台退回主机名 + 第一块网卡。"""
    for path in MACHINE_ID_PATHS:
        try:
            text = path.read_text(encoding="ascii", errors="ignore").strip()
        except OSError:
            continue
        if text:
            return text
    return f"{platform.node()}|{uuid.getnode():012x}"


def _slot(service: str, username: str) -> str:
    return f"{service}\x00{username}"


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:  # Windows 只切只读位；有的网络盘直接不支持
        log.debug("设置权限失败 %s：%s", path, exc)


class FileSecretBackend:
    """数据目录下的一份加密文件。没有系统密钥环时用它。"""

    kind = KIND_FILE

    def __init__(self, path: Path | str | None = None, machine: str | None = None) -> None:
        self._path = Path(path) if path else file_path()
        self._machine = machine or machine_id()

    # ---------------------------------------------------------- 读写接口
    def get_password(self, service: str, username: str) -> str:
        return self._read_all().get(_slot(service, username), "")

    def set_password(self, service: str, username: str, value: str) -> None:
        data = self._read_all()
        data[_slot(service, username)] = value
        self._write_all(data)

    def delete_password(self, service: str, username: str) -> None:
        data = self._read_all()
        if data.pop(_slot(service, username), None) is not None:
            self._write_all(data)

    # ------------------------------------------------------------- 说明
    def describe(self) -> dict[str, str]:
        return {
            "kind": KIND_FILE,
            "label": "本机文件存储",
            "path": str(self._path),
            "detail": (
                "这台机器上没有可用的系统密钥环（服务器一般没装 SecretService / DBus），"
                f"密钥改为 AES-256-GCM 加密后存在 {self._path}（文件权限 600、目录 700）。"
                "加密钥匙由本机 machine-id 派生：同一台机器上的其它系统用户读不到，"
                "把文件拷到别的机器上也解不开。本机 root 能读到，能上服务器就更稳妥。"
            ),
        }

    # --------------------------------------------------------- 加密细节
    def _cipher(self, salt: bytes):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=KDF_ITERATIONS,
        )
        key = kdf.derive(f"{self._machine}|{KDF_SALT_LABEL}".encode("utf-8"))
        return AESGCM(key)

    def _read_all(self) -> dict[str, str]:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            log.warning("读取密钥文件失败 %s：%s", self._path, exc)
            return {}
        try:
            box = json.loads(raw.decode("utf-8"))
            salt = base64.b64decode(box["salt"])
            nonce = base64.b64decode(box["nonce"])
            blob = base64.b64decode(box["data"])
            plain = self._cipher(salt).decrypt(nonce, blob, None)
            data = json.loads(plain.decode("utf-8"))
        except Exception as exc:
            # 换了机器、从旧机器拷回来、手改坏了，都是这一条：回空，不崩。
            log.warning(
                "密钥文件解不开（多半是换了机器，或者文件坏了）%s：%s", self._path, exc
            )
            return {}
        return data if isinstance(data, dict) else {}

    def _write_all(self, data: dict[str, str]) -> None:
        salt = os.urandom(16)
        nonce = os.urandom(12)
        plain = json.dumps(data, ensure_ascii=False).encode("utf-8")
        blob = self._cipher(salt).encrypt(nonce, plain, None)
        payload = json.dumps(
            {
                "v": 1,
                "kdf": "pbkdf2-sha256",
                "iterations": KDF_ITERATIONS,
                "salt": base64.b64encode(salt).decode("ascii"),
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "data": base64.b64encode(blob).decode("ascii"),
            }
        ).encode("utf-8")

        self._path.parent.mkdir(parents=True, exist_ok=True)
        _chmod(self._path.parent, 0o700)
        tmp = self._path.with_name(f"{self._path.name}.tmp{os.getpid()}")
        try:
            handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
            _chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        _chmod(self._path, 0o600)


class KeyringBackend:
    """系统密钥环（Windows 凭据管理器 / macOS 钥匙串 / Linux SecretService）。"""

    kind = KIND_KEYRING

    def __init__(self, module: Any) -> None:
        self._module = module

    def get_password(self, service: str, username: str) -> str:
        return self._module.get_password(service, username) or ""

    def set_password(self, service: str, username: str, value: str) -> None:
        self._module.set_password(service, username, value)

    def delete_password(self, service: str, username: str) -> None:
        self._module.delete_password(service, username)

    def describe(self) -> dict[str, str]:
        return {
            "kind": KIND_KEYRING,
            "label": "系统密钥环",
            "path": "",
            "detail": (
                "密钥保存在操作系统的凭据库里（Windows 凭据管理器 / macOS 钥匙串 / "
                "Linux SecretService），不写进配置文件。"
            ),
        }


class _NullBackend:
    """两种都不可用时的兜底：读回空，写就报错。绝不落明文。"""

    kind = KIND_NONE

    def get_password(self, service: str, username: str) -> str:
        return ""

    def set_password(self, service: str, username: str, value: str) -> None:
        raise RuntimeError(
            "没有可用的密钥存储：系统密钥环不可用，本机文件存储又缺 cryptography 依赖"
        )

    def delete_password(self, service: str, username: str) -> None:
        return None

    def describe(self) -> dict[str, str]:
        return {
            "kind": KIND_NONE,
            "label": "不可用",
            "path": "",
            "detail": (
                "既没有可用的系统密钥环，也没有装上 cryptography："
                "现在保存不了密钥（不会退回明文文件）。请装 python3-cryptography 后重启。"
            ),
        }


def keyring_usable(module: Any) -> bool:
    """探测系统密钥环：写一条探针、读回来、删掉。

    只看 keyring.get_keyring() 的类型不够：fail 后端是明着的，但有的环境会挑一个
    「看着能用、其实只读」的后端，写的时候才炸。探针写不进去或读不回原值，就当不可用。
    """
    token = uuid.uuid4().hex
    try:
        module.set_password(PROBE_SERVICE, PROBE_USER, token)
    except Exception as exc:
        log.info("系统密钥环不可用（探针写入失败）：%s", exc)
        return False
    try:
        readback = module.get_password(PROBE_SERVICE, PROBE_USER) or ""
    except Exception as exc:
        log.info("系统密钥环不可用（探针读回失败）：%s", exc)
        readback = ""
    finally:
        try:
            module.delete_password(PROBE_SERVICE, PROBE_USER)
        except Exception:
            log.debug("清理密钥环探针失败", exc_info=True)
    if readback != token:
        log.info("系统密钥环不可用（探针读不回原值）")
        return False
    return True


def _cryptography_ready() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except ImportError:
        return False
    return True


def open_store(path: Path | str | None = None) -> Any:
    """选一个真的能用的后端：系统密钥环优先，其次是本机加密文件。"""
    try:
        import keyring
    except ImportError:
        keyring = None
        log.info("没有安装 keyring 模块，直接使用本机文件存储")
    if keyring is not None and keyring_usable(keyring):
        return KeyringBackend(keyring)
    if not _cryptography_ready():
        log.error("系统密钥环不可用，且没有安装 cryptography：密钥将无法保存")
        return _NullBackend()
    store = FileSecretBackend(path=path)
    log.warning("系统密钥环不可用，密钥改存本机文件：%s", store.describe()["path"])
    return store


def describe(backend: Any) -> dict[str, str]:
    """给接口用的说明。替身后端（测试里那种）也要能问。"""
    describe_fn = getattr(backend, "describe", None)
    if callable(describe_fn):
        return describe_fn()
    return {
        "kind": KIND_NONE,
        "label": "未知存储",
        "path": "",
        "detail": "这个后端没有提供说明。",
    }
