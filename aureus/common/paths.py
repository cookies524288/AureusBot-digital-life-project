"""公共地基 common：数据根目录与各类路径。

用户数据（数据库、配置、密钥、日志……）全部放在软件目录之外的“数据根目录”里，
软件目录里只有代码。别处需要路径时，一律从 AureusPaths 取，不要自己拼接。
设计见 docs/14-user-data.md（D28）。
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_data_path

# 没有指定位置时，数据根目录放在系统的用户数据目录下、以这个名字命名的文件夹里。
APP_NAME = "Aureus"

# 指定数据根目录位置的环境变量。开发时把它指向项目目录之外，个人设定就不会混进 git。
HOME_ENV_VAR = "AUREUS_HOME"


class HomePathError(ValueError):
    """数据根目录的位置不合法：不是绝对路径，或者那里已经有一个文件。"""


def _require_absolute(home: Path, origin: str) -> None:
    # 为什么不接受相对路径：它的含义随运行时所在的目录变化，
    # 换个地方启动就会悄悄新建一份空数据，看起来像“失忆”。
    if not home.is_absolute():
        raise HomePathError(f"数据根目录必须是绝对路径，但{origin}给的是 {str(home)!r}。")


def resolve_home(
    explicit: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """确定数据根目录的位置。

    优先级：显式参数（比如命令行的 --home） > 环境变量 AUREUS_HOME > 系统的用户数据目录。

    Args:
        explicit: 显式指定的位置；为 None 或空白时忽略。
        environ: 环境变量表，默认读真实的环境变量；测试时可以传入假的。

    Returns:
        数据根目录的绝对路径。这里只确定位置，不创建文件夹。

    Raises:
        HomePathError: 指定的位置不是绝对路径。
    """
    environment = os.environ if environ is None else environ
    if explicit is not None and str(explicit).strip():
        raw, origin = str(explicit), "命令行参数"
    elif environment.get(HOME_ENV_VAR, "").strip():
        raw, origin = environment[HOME_ENV_VAR], f"环境变量 {HOME_ENV_VAR}"
    else:
        return user_data_path(APP_NAME, appauthor=False)
    home = Path(raw.strip()).expanduser()
    _require_absolute(home, origin)
    return home


def home_is_default(
    explicit: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """数据根目录是不是落在默认位置（既没指定，也没设环境变量）。

    初始化命令靠它决定要不要提示用户选位置（D46）。参数含义同 resolve_home。
    """
    environment = os.environ if environ is None else environ
    if explicit is not None and str(explicit).strip():
        return False
    return not environment.get(HOME_ENV_VAR, "").strip()


@dataclass(frozen=True)
class AureusPaths:
    """数据根目录，以及它下面所有约定好的位置。布局见 docs/14-user-data.md 第 2 节。

    只要给出 home，其余路径都由它推出来；别处不要自己拼路径。
    """

    home: Path
    config_dir: Path = field(init=False)  # 配置文件夹
    runtime_config_file: Path = field(init=False)  # 运行配置 aureus.toml（不含密钥）
    models_config_file: Path = field(init=False)  # 模型配置 models.toml（不含密钥）
    identity_file: Path = field(init=False)  # 身份事实与四层人设 identity.toml
    env_file: Path = field(init=False)  # 旧的明文密钥 .env，最低优先级的回退（D36）
    vault_file: Path = field(init=False)  # 加密的密钥库 secrets.vault（D36）
    master_key_file: Path = field(init=False)  # 主密钥 secrets.key，解开密钥库用（D36）
    data_dir: Path = field(init=False)  # 它的“家”：运行产生的数据
    database_file: Path = field(init=False)  # 核心数据库 aureus.db
    halt_file: Path = field(init=False)  # 急停标记 halt.json，文件在就是急停中（D54）
    log_dir: Path = field(init=False)  # 日志
    diary_dir: Path = field(init=False)  # 每日日记
    memory_dir: Path = field(init=False)  # 记忆
    skills_dir: Path = field(init=False)  # 技能
    snapshots_dir: Path = field(init=False)  # 定期快照

    def __post_init__(self) -> None:
        _require_absolute(self.home, "调用方")
        config_dir = self.home / "config"
        data_dir = self.home / "data"
        derived = {
            "config_dir": config_dir,
            "runtime_config_file": config_dir / "aureus.toml",
            "models_config_file": config_dir / "models.toml",
            "identity_file": config_dir / "identity.toml",
            "env_file": self.home / ".env",
            "vault_file": self.home / "secrets.vault",
            "master_key_file": self.home / "secrets.key",
            "data_dir": data_dir,
            "database_file": data_dir / "aureus.db",
            "halt_file": data_dir / "halt.json",
            "log_dir": data_dir / "logs",
            "diary_dir": data_dir / "diary",
            "memory_dir": data_dir / "memory",
            "skills_dir": data_dir / "skills",
            "snapshots_dir": data_dir / "snapshots",
        }
        # 语法：frozen=True 的类不能直接给属性赋值，这里用 object.__setattr__ 填一次，
        # 填完之后就再也改不了。
        for name, value in derived.items():
            object.__setattr__(self, name, value)

    def ensure_directories(self) -> None:
        """创建所有约定好的文件夹（已存在的跳过）。只建文件夹，不创建任何文件。

        Raises:
            HomePathError: 数据根目录的位置上已经有一个文件。
        """
        if self.home.exists() and not self.home.is_dir():
            raise HomePathError(f"数据根目录 {self.home} 已经存在，但它是一个文件，不是文件夹。")
        folders = (
            self.config_dir,
            self.log_dir,
            self.diary_dir,
            self.memory_dir,
            self.skills_dir,
            self.snapshots_dir,
        )
        for folder in folders:
            folder.mkdir(parents=True, exist_ok=True)
