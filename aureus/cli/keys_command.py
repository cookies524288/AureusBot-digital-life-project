"""命令行：keys 子命令，管理加密的密钥库（D36）。

0.7a 的最小入口，设计见 docs/15-model-management.md 7.5；0.10 做完整命令行时再并进去。
密钥一律通过提示输入（不回显），不经命令行参数：命令行参数会留在 shell 历史里。
所有输出只写密钥的名字，不写密钥本身。
"""

import argparse
import re
import sys
from collections.abc import Callable
from pathlib import Path

from dotenv import dotenv_values

from aureus.common.paths import AureusPaths
from aureus.common.vault import SecretVault, VaultError
from aureus.organs.api_keys import KEY_NAME_PREFIX
from aureus.organs.model_config import API_KEY_ENV_PATTERN

# 读一条密钥的函数：参数是提示语，返回用户输入的内容。
# 默认是不回显的 getpass；测试时换成假的。
ReadSecret = Callable[[str], str]

EXIT_OK = 0
EXIT_FAILURE = 1


def key_name_argument(text: str) -> str:
    """检查命令行上给的密钥名，格式和 models.toml 里的 api_key_env 一致。

    Args:
        text: 命令行上给的密钥名。

    Returns:
        原样返回通过检查的名字。

    Raises:
        argparse.ArgumentTypeError: 格式不对。消息里不回显输入：万一粘贴进来的是密钥本身。
    """
    if not re.fullmatch(API_KEY_ENV_PATTERN, text):
        raise argparse.ArgumentTypeError(
            f"密钥名要以 {KEY_NAME_PREFIX} 开头，只能用大写字母、数字和下划线。"
        )
    return text


def _set_key(vault: SecretVault, name: str, read_secret: ReadSecret) -> int:
    # 先读一遍密钥库再提示输入：库打不开时在用户敲密钥之前就报错，不让人白输一遍。
    existed = any(name in (entry.name, entry.legacy_name) for entry in vault.load_entries())
    secret = read_secret(f"请输入 {name} 的密钥（输入时不显示）：")
    if not secret.strip():
        print("错误：没有输入密钥，什么都没有改。", file=sys.stderr)
        return EXIT_FAILURE
    vault.set(name, secret)
    action = "更新" if existed else "录入"
    print(f"已{action}：{name}")
    return EXIT_OK


def _list_keys(vault: SecretVault) -> int:
    names = vault.names()
    if not names:
        print("密钥库里还没有密钥。")
    for name in names:
        print(name)
    return EXIT_OK


def _remove_key(vault: SecretVault, name: str) -> int:
    if vault.remove(name):
        print(f"已删除：{name}")
    else:
        print(f"密钥库里没有 {name}，什么都没有改。")
    return EXIT_OK


def _migrate_from_dotenv(vault: SecretVault, env_file: Path) -> int:
    if not env_file.exists():
        print(f"没有找到 {env_file}，没有需要搬的密钥。")
        return EXIT_OK
    entries = {
        name: value.strip()
        for name, value in dotenv_values(env_file).items()
        if name.startswith(KEY_NAME_PREFIX) and value is not None and value.strip()
    }
    if not entries:
        print(f"{env_file} 里没有已填写的 {KEY_NAME_PREFIX} 开头的密钥，没有需要搬的密钥。")
        return EXIT_OK
    existing = {
        name
        for entry in vault.load_entries()
        for name in (entry.name, entry.legacy_name)
        if name is not None
    }
    moved: dict[str, str] = {}
    for name in sorted(entries):
        if not re.fullmatch(API_KEY_ENV_PATTERN, name):
            print(f"名字格式不对，跳过：{name}")
        elif name in existing:
            # 不覆盖：密钥库里的是用户后来录入的，.env 里的可能是旧的。
            print(f"密钥库里已有，跳过（不覆盖）：{name}")
        else:
            vault.set(name, entries[name])
            moved[name] = entries[name]
            print(f"已搬进密钥库：{name}")
    if not moved:
        print("没有新搬的密钥。")
        return EXIT_OK
    # 用户搬完之后多半会去删 .env，所以先核对：从密钥库里读出的必须和 .env 里的一致。
    stored = vault.load()
    if any(stored.get(name) != secret for name, secret in moved.items()):
        print(
            "错误：搬完后核对不一致，请不要删除 .env，并重新运行 keys migrate。",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    print(f"核对一致：搬进的 {len(moved)} 条密钥都能从密钥库里原样读出。")
    print(f"{env_file} 没有被改动；确认密钥库用得上之后，删不删它由你决定。")
    return EXIT_OK


def run_keys_command(args: argparse.Namespace, paths: AureusPaths, read_secret: ReadSecret) -> int:
    """执行一条 keys 命令。

    Args:
        args: 解析好的命令行参数，要有 action；set 和 remove 还要有 name。
        paths: 数据根目录的路径集合。
        read_secret: 读一条密钥的函数。

    Returns:
        退出码：0 成功，1 失败。

    Raises:
        ValueError: action 不是 set、list、remove、migrate 之一（解析器会先挡住，这里是兜底）。
    """
    vault = SecretVault(paths.vault_file, paths.master_key_file)
    try:
        if args.action == "set":
            return _set_key(vault, args.name, read_secret)
        if args.action == "list":
            return _list_keys(vault)
        if args.action == "remove":
            return _remove_key(vault, args.name)
        if args.action == "migrate":
            return _migrate_from_dotenv(vault, paths.env_file)
    except VaultError as error:
        # 密钥库打不开就停下，不当成“没有密钥”，也不回退到 .env（D36）。
        print(f"错误：{error}", file=sys.stderr)
        return EXIT_FAILURE
    raise ValueError(f"不认识的 keys 操作：{args.action}")
