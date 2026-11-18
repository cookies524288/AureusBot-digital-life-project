"""一条命令跑完所有检查：格式、代码规范、类型、架构规则、测试。

用法（在项目根目录）：
    uv run python scripts/check.py          只检查，不改文件
    uv run python scripts/check.py --fix    先自动修正格式和能自动修的问题，再检查

见 docs/11-dev-guide.md 第 3.8 节。
"""

import subprocess
import sys
import tomllib
from pathlib import Path

# 项目根目录：本文件在 scripts/ 下，往上一层就是根目录。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Windows 上 uv 生成的 pyright、pytest、lint-imports 启动器会报 trampoline 错误，
# 所以这几个工具改用当前解释器运行（ruff 是原生程序，没有这个问题）。
# import-linter 没有 __main__ 模块，只能调它的命令行入口函数。
LINT_IMPORTS_SNIPPET = (
    "import sys; from importlinter.cli import lint_imports_command; "
    "sys.argv = ['lint-imports']; sys.exit(lint_imports_command())"
)

# 加 --fix 时先跑这些，自动修正能修的问题。
FIXES: list[tuple[str, list[str]]] = [
    ("自动修正格式", ["ruff", "format", "."]),
    ("自动修正可修的问题", ["ruff", "check", "--fix", "."]),
]


def has_import_contracts() -> bool:
    """判断 pyproject.toml 里是否已经写了 import-linter 的架构规则。

    子系统建好之前还没有规则可查，这时运行 import-linter 会直接报错，
    所以没有规则就跳过它。
    """
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
        config = tomllib.load(file)
    contracts = config.get("tool", {}).get("importlinter", {}).get("contracts", [])
    return len(contracts) > 0


def build_checks() -> list[tuple[str, list[str]]]:
    """列出本次要跑的检查，每一项是（显示名称，命令），按从快到慢排列。"""
    checks = [
        ("格式（ruff format）", ["ruff", "format", "--check", "."]),
        ("代码规范（ruff check）", ["ruff", "check", "."]),
        ("类型（pyright）", [sys.executable, "-m", "pyright"]),
    ]
    if has_import_contracts():
        checks.append(("架构规则（import-linter）", [sys.executable, "-c", LINT_IMPORTS_SNIPPET]))
    else:
        print("提示：还没有架构规则，本次跳过 import-linter。")
    checks.append(("测试（pytest）", [sys.executable, "-m", "pytest", "-q"]))
    return checks


def run(name: str, command: list[str]) -> bool:
    """在项目根目录运行一条命令，返回它是否成功。"""
    print(f"\n==> {name}：{' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    return result.returncode == 0


def main() -> int:
    """执行修正（可选）和全部检查，返回进程退出码：0 表示全部通过。"""
    if "--fix" in sys.argv[1:]:
        for name, command in FIXES:
            run(name, command)

    # 前面的检查失败了也继续跑后面的，最后统一汇总——这样一次就能看到所有问题。
    failed = [name for name, command in build_checks() if not run(name, command)]

    print("\n" + "=" * 40)
    if failed:
        print("未通过：" + "、".join(failed))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
