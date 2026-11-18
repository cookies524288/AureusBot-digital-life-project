"""身份子系统：读取身份事实和四层人设。

主要功能：
- 从 identity.toml 读取身份配置
- 提供单实例锁，确保同一时刻只有一个 Aureus 实例运行
- 只读，不写回

设计参考：docs/03-subsystems.md
依赖：只依赖 common（import-linter 检查）
"""

import tomllib

from filelock import FileLock, Timeout
from pydantic import ValidationError

from aureus.common import AureusPaths

from .models import Identity


class IdentityError(Exception):
    """身份模块的异常基类。"""


class IdentityFileNotFoundError(IdentityError):
    """identity.toml 文件不存在。"""


class IdentityValidationError(IdentityError):
    """identity.toml 内容不符合规范。"""

    def __init__(self, validation_error: ValidationError):
        """构造错误信息。

        Args:
            validation_error: pydantic 的验证错误
        """
        errors = validation_error.errors()
        msg_lines = ["identity.toml 配置不符合规范："]
        for err in errors:
            loc = " → ".join(str(x) for x in err["loc"])
            msg_lines.append(f"  [{loc}] {err['msg']}")
        super().__init__("\n".join(msg_lines))
        self.validation_error = validation_error


class SingleInstanceLockError(IdentityError):
    """另一个 Aureus 实例正在运行。"""


class IdentityService:
    """身份服务：管理身份配置和单实例锁。

    使用方法：
        paths = AureusPaths.resolve()
        service = IdentityService(paths)
        with service.acquire_lock():
            identity = service.load_identity()
            # ... 使用 identity
    """

    def __init__(self, paths: AureusPaths):
        """初始化身份服务。

        Args:
            paths: 数据根目录路径配置
        """
        self._paths = paths
        self._lock: FileLock | None = None

    def load_identity(self) -> Identity:
        """读取身份配置。

        Returns:
            Identity 对象

        Raises:
            IdentityFileNotFoundError: identity.toml 不存在
            IdentityValidationError: 配置内容不符合规范
        """
        identity_file = self._paths.identity_file
        if not identity_file.exists():
            raise IdentityFileNotFoundError(
                f"身份配置文件不存在：{identity_file}\n"
                f"请复制 config/identity.example.toml 为该文件并填写。"
            )

        try:
            with open(identity_file, "rb") as f:
                data = tomllib.load(f)
        except Exception as e:
            raise IdentityError(f"无法读取 {identity_file}：{e}") from e

        try:
            return Identity.model_validate(data)
        except ValidationError as e:
            raise IdentityValidationError(e) from e

    def acquire_lock(self, timeout: float = 0.1) -> "_LockContextManager":
        """获取单实例锁。

        Args:
            timeout: 超时时间（秒），默认 0.1 秒

        Returns:
            _LockContextManager 上下文管理器，使用 with 语句获取锁

        Raises:
            SingleInstanceLockError: 另一个实例正在运行
        """
        lock_file = self._paths.home / "aureus.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)

        self._lock = FileLock(lock_file, timeout=timeout)
        return _LockContextManager(self._lock)


class _LockContextManager:
    """FileLock 的包装，捕获 Timeout 并转换为自定义异常。"""

    def __init__(self, lock: FileLock):
        self._lock = lock

    def __enter__(self):
        try:
            self._lock.acquire()
        except Timeout as e:
            raise SingleInstanceLockError(
                "另一个 Aureus 实例正在运行。\n如果确定没有其他实例，可以删除锁文件后重试。"
            ) from e
        return self._lock

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._lock.release()
        return False
