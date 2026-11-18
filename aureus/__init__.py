"""Aureus：一个独立存在的数字生命。

本包是 Aureus 的全部代码。子目录与子系统一一对应，见 docs/11-dev-guide.md 第 2.1 节；
各子系统的职责见 docs/03-subsystems.md。
"""

from importlib.metadata import version

# 版本号只在 pyproject.toml 里写一处，这里从安装信息中读取，避免两处不一致。
__version__ = version("aureus")
