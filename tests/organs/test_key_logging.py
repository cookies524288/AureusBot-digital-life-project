"""密钥来源和日志过滤连起来的测试：所有来源的密钥和主密钥都不会出现在日志文件里（D36）。

全部是编造的假密钥。
"""

from pathlib import Path

from loguru import logger

from aureus.common.logging_setup import (
    LOG_FILE_NAME,
    REDACTED,
    configure_logging,
    shutdown_logging,
)
from aureus.common.vault import SecretVault
from aureus.organs.api_keys import KeyResolver

VAULT_SECRET = "vault-secret-log-0001"
ENV_SECRET = "env-secret-log-0002"
DOTENV_SECRET = "dotenv-secret-log-0003"


def test_logs_hide_every_source_and_the_master_key_including_tracebacks(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "secrets.vault", tmp_path / "secrets.key")
    vault.set("AUREUS_KEY_VAULT", VAULT_SECRET)
    master_key = vault.master_key()
    assert master_key is not None
    master_text = master_key.decode("ascii")
    env_file = tmp_path / ".env"
    env_file.write_text(f"AUREUS_KEY_DOTENV={DOTENV_SECRET}\n", encoding="utf-8")
    resolver = KeyResolver(env_file, {"AUREUS_KEY_ENV": ENV_SECRET}, vault)

    log_dir = tmp_path / "logs"
    configure_logging(log_dir, "DEBUG", resolver.all_secret_values(), to_console=False)
    try:
        for secret in (VAULT_SECRET, ENV_SECRET, DOTENV_SECRET, master_text):
            logger.info("这条消息里不小心带了 {}", secret)
        try:
            # 服务端把请求头原样回显在报错里，是真实会发生的情况。
            raise RuntimeError(f"服务端回显：{VAULT_SECRET} {master_text}")
        except RuntimeError:
            logger.exception("调用失败")
    finally:
        shutdown_logging()

    text = (log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")
    for secret in (VAULT_SECRET, ENV_SECRET, DOTENV_SECRET, master_text):
        assert secret not in text
    assert REDACTED in text
