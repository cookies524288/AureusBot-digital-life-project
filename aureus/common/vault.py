"""公共地基 common：加密的密钥库与主密钥文件。

设计见 docs/15-model-management.md 第 7 节（D36）。两个文件都在数据根目录下：
- secrets.key：主密钥，第一次录入密钥时随机生成，用户不用记；
- secrets.vault：加密的密钥库，里面是 {密钥名: 密钥}，连名字也是密文。

这里只管文件和加解密：不认识器官，也不决定读取顺序（那是 organs/api_keys.py 的事）。
加密用 cryptography 的 Fernet（带完整性校验），不自己拼算法；主密钥不对或文件被改过，
都会解密失败，不会读出错的内容。报错只写文件路径和原因，不回显文件内容。
"""

import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from filelock import FileLock, Timeout

# 当前软件认识的文件格式版本。文件比软件新（降级）时拒绝读取，不硬读，
# 做法同 docs/14-user-data.md 第 3 节。
KEY_FILE_FORMAT_VERSION = 1
VAULT_FORMAT_VERSION = 1

# 主密钥的保护方式。现在只有 none（不加口令）；字段先留着，以后给主密钥
# 加口令保护时，密钥库不用重新加密（15 号文档 7.9）。
PROTECTION_NONE = "none"

# 改密钥库时最多等别的进程多久。改一次只要几毫秒，等这么久还拿不到，多半是有进程卡住了。
VAULT_LOCK_TIMEOUT_SECONDS = 10.0


class VaultError(Exception):
    """密钥库或主密钥文件有问题。消息是中文，只写文件和原因，不带文件内容。"""


def _write_atomically(path: Path, text: str) -> None:
    # 先写临时文件再替换：中途崩溃时原文件完好，不会留下半个文件。
    # mkstemp 在类 Unix 上创建的文件只有所有者能读写（0600），主密钥文件需要这样。
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _read_json_object(path: Path, what: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # from None：不带上原始异常，免得追踪信息里带出文件内容。
        raise VaultError(f"{what} {path} 读不了，或者不是合法的 JSON。") from None
    if not isinstance(raw, dict):
        raise VaultError(f"{what} {path} 的内容格式不对。")
    return raw


def _check_format_version(raw: Mapping[str, Any], supported: int, what: str, path: Path) -> None:
    version = raw.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise VaultError(f"{what} {path} 缺少有效的 format_version。")
    if version > supported:
        raise VaultError(
            f"{what} {path} 的 format_version 是 {version}，"
            f"比当前软件认识的版本 {supported} 更新。请先升级软件。"
        )


def generate_master_key() -> bytes:
    """随机生成一个主密钥（Fernet 格式）。"""
    return Fernet.generate_key()


def load_master_key(path: Path) -> bytes | None:
    """读取主密钥文件。

    Args:
        path: 主密钥文件 secrets.key 的位置。

    Returns:
        主密钥；文件不存在时返回 None。

    Raises:
        VaultError: 文件存在但读不了、格式不对、版本太新或保护方式不认识。
    """
    if not path.exists():
        return None
    raw = _read_json_object(path, "主密钥文件")
    _check_format_version(raw, KEY_FILE_FORMAT_VERSION, "主密钥文件", path)
    if raw.get("protection") != PROTECTION_NONE:
        raise VaultError(f"主密钥文件 {path} 的保护方式不认识，可能来自更新的软件。")
    key = raw.get("key")
    if not isinstance(key, str):
        raise VaultError(f"主密钥文件 {path} 里没有 key 字段。")
    key_bytes = key.strip().encode("utf-8")
    try:
        Fernet(key_bytes)
    except ValueError:
        raise VaultError(f"主密钥文件 {path} 里的 key 格式不对。") from None
    return key_bytes


def _write_master_key(path: Path, key: bytes) -> None:
    body = {
        "format_version": KEY_FILE_FORMAT_VERSION,
        "protection": PROTECTION_NONE,
        "key": key.decode("ascii"),
    }
    _write_atomically(path, json.dumps(body, indent=2) + "\n")


class SecretVault:
    """加密的密钥库：读写 {密钥名: 密钥}。

    每次读写都重新读文件、重新加解密：密钥只有几条，Fernet 很快，
    这样别的程序（比如 keys 命令）改了文件之后，这里不会读到旧内容。
    """

    def __init__(self, vault_file: Path, key_file: Path) -> None:
        self._vault_file = vault_file
        self._key_file = key_file
        # 改密钥库是「读—改—写」：两个 keys set 同时运行，后写的会覆盖掉先写的那条。
        # 用单独的锁文件让改动一个一个来（审查 H5，D52）。只读不加锁：写入是原子替换，
        # 读到的总是一份完整的旧版或新版。
        self._write_lock = FileLock(vault_file.with_name(vault_file.name + ".lock"))

    def _missing_key_error(self) -> VaultError:
        return VaultError(
            f"找不到主密钥文件 {self._key_file}，打不开密钥库 {self._vault_file}。"
            "请把 secrets.key 找回来；实在找不回，删掉 secrets.vault 后重新录入密钥。"
        )

    def _decrypt(self, key: bytes) -> dict[str, str]:
        raw = _read_json_object(self._vault_file, "密钥库")
        _check_format_version(raw, VAULT_FORMAT_VERSION, "密钥库", self._vault_file)
        token = raw.get("token")
        if not isinstance(token, str):
            raise VaultError(f"密钥库 {self._vault_file} 里没有 token 字段。")
        try:
            payload = json.loads(Fernet(key).decrypt(token.encode("utf-8")))
        except (InvalidToken, ValueError):
            # 主密钥不对和文件被改动都走这里：两者没法区分，也不需要区分。
            raise VaultError(
                f"密钥库 {self._vault_file} 解不开：主密钥不对，或者文件被改动过。"
            ) from None
        if not isinstance(payload, dict) or not all(
            isinstance(name, str) and isinstance(value, str) for name, value in payload.items()
        ):
            raise VaultError(f"密钥库 {self._vault_file} 解开后的内容格式不对。")
        return dict(payload)

    def _save(self, secrets: Mapping[str, str]) -> None:
        # 调用方都先 load 过：有密钥库却没有可用主密钥时，load 早就报错了，
        # 所以走到这里时只会是“第一次写”或者“主密钥完好”，不会覆盖掉打不开的旧库。
        key = load_master_key(self._key_file)
        if key is None:
            key = generate_master_key()
            _write_master_key(self._key_file, key)
        payload = json.dumps(dict(secrets), ensure_ascii=False).encode("utf-8")
        body = {
            "format_version": VAULT_FORMAT_VERSION,
            "token": Fernet(key).encrypt(payload).decode("ascii"),
        }
        _write_atomically(self._vault_file, json.dumps(body, indent=2) + "\n")

    def master_key(self) -> bytes | None:
        """读取主密钥，只给日志过滤用：让主密钥本身也不会出现在日志里。

        Returns:
            主密钥；还没有主密钥文件时返回 None。

        Raises:
            VaultError: 主密钥文件存在但读不了或格式不对。
        """
        return load_master_key(self._key_file)

    def load(self) -> dict[str, str]:
        """读出全部密钥。

        Returns:
            {密钥名: 密钥}；还没有密钥库时返回空表。

        Raises:
            VaultError: 密钥库存在但打不开（没有主密钥、主密钥不对、文件被改动、版本太新……）。
                不会当成“没有”处理，免得悄悄用上别处的旧明文。
        """
        if not self._vault_file.exists():
            return {}
        key = load_master_key(self._key_file)
        if key is None:
            raise self._missing_key_error()
        return self._decrypt(key)

    def names(self) -> list[str]:
        """列出所有密钥名（排序），不含密钥本身。"""
        return sorted(self.load())

    def set(self, name: str, secret: str) -> None:
        """录入或更新一条密钥；第一次录入时顺带生成主密钥。值两端的空白会去掉。

        Args:
            name: 密钥名，比如 AUREUS_KEY_EXAMPLE。
            secret: 密钥原文。

        Raises:
            ValueError: 名字或密钥是空的（只有空白也算）。
            VaultError: 密钥库打不开。
        """
        cleaned = secret.strip()
        if not name.strip() or not cleaned:
            raise ValueError("密钥名和密钥都不能为空。")
        with self._locked_for_writing():
            secrets = self.load()
            secrets[name] = cleaned
            self._save(secrets)

    def remove(self, name: str) -> bool:
        """删掉一条密钥。

        Args:
            name: 密钥名。

        Returns:
            这个名字原来存在就返回 True，不存在返回 False（什么都不改）。

        Raises:
            VaultError: 密钥库打不开。
        """
        with self._locked_for_writing():
            secrets = self.load()
            if name not in secrets:
                return False
            del secrets[name]
            self._save(secrets)
        return True

    @contextmanager
    def _locked_for_writing(self) -> Iterator[None]:
        """拿到写锁再改密钥库；等太久就报错，不跳过锁硬写。"""
        self._vault_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._write_lock.acquire(timeout=VAULT_LOCK_TIMEOUT_SECONDS)
        except Timeout:
            raise VaultError(
                f"等了 {VAULT_LOCK_TIMEOUT_SECONDS:g} 秒，另一个程序还在改密钥库 "
                f"{self._vault_file}。请等它结束后再试。"
            ) from None
        try:
            yield
        finally:
            self._write_lock.release()
