"""核心组装 core：启动和关闭流程（路线图 0.10，启动顺序见 15 号文档 7.6 节）。

顺序：读配置 → 解锁密钥库 → 设置日志 → 明文密钥警告 → 单实例锁 → 读身份 →
打开时间线和账本 → 注册器官、建安全网关 → 建生物状态和心跳。关闭时倒着来。

本模块没有放进 aureus.core 的 __init__ 导出：它要用心跳，心跳又要用 core 的生物模型，
导出会造成循环导入。要用时写 `from aureus.core.runtime import open_core`。
"""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

from loguru import logger

from aureus.common import (
    AureusPaths,
    Clock,
    ConfigError,
    Settings,
    SystemClock,
    configure_logging,
    load_settings,
    shutdown_logging,
)
from aureus.common.vault import SecretVault
from aureus.core.biomodel import BiologicalState
from aureus.core.snapshots import BYTES_PER_MEGABYTE, prune_snapshots
from aureus.heartbeat import Heartbeat
from aureus.identity import Identity
from aureus.identity.service import IdentityService
from aureus.organs import FakeReplyer, KeyResolver, LLMOrgan, OrganBus, load_models_config
from aureus.organs.api_keys import warn_about_plaintext_keys
from aureus.safety import Ledger, SafetyGateway
from aureus.timeline import TimelineDatabase


@dataclass(frozen=True)
class Core:
    """组装好的核心：各个部件都在这里，心跳还没有开始跳（调用方决定什么时候 run）。"""

    paths: AureusPaths
    settings: Settings
    identity: Identity
    timeline: TimelineDatabase
    ledger: Ledger
    gateway: SafetyGateway
    heartbeat: Heartbeat


def _build_organ_bus(
    paths: AureusPaths, reply_organ: str, resolver: KeyResolver, *, fake_organs: bool
) -> OrganBus:
    """注册器官。假器官模式不读 models.toml、不调用任何模型，不花钱。"""
    bus = OrganBus()
    if fake_organs:
        bus.register(FakeReplyer(organ_id=reply_organ))
        return bus
    models_config = load_models_config(paths)
    for organ_id in models_config.organs:
        bus.register(LLMOrgan.from_config(organ_id, models_config, resolver))
    if not bus.is_registered(reply_organ):
        # 启动时就报错，而不是等用户说第一句话才发现回复器不存在。
        raise ConfigError(
            f"心跳要用的回复器官 {reply_organ} 没有在 models.toml 里配置"
            f"（缺少 [organs.{reply_organ}]）。"
        )
    return bus


@contextmanager
def open_core(
    paths: AureusPaths,
    *,
    fake_organs: bool = False,
    clock: Clock | None = None,
    to_console: bool = True,
) -> Iterator[Core]:
    """启动核心，退出 with 块时关闭。

    Args:
        paths: 数据根目录的路径集合。
        fake_organs: 用假器官代替真实模型（不花钱，用来试运行和测试）。
        clock: 时钟；不给就用真实时钟。
        to_console: 日志是否同时输出到终端。

    Yields:
        组装好的核心。

    Raises:
        ConfigError: 配置有问题。
        VaultError: 密钥库存在但打不开（不回退到明文 .env，D36）。
        SingleInstanceLockError: 另一个实例正在运行。
        IdentityError: 身份配置读不了。
    """
    the_clock = clock or SystemClock()
    paths.ensure_directories()
    settings = load_settings(paths)
    # 先解锁，再设日志：解锁过程不写日志，解开的密钥一开始就在日志过滤里。
    resolver = KeyResolver(
        paths.env_file, vault=SecretVault(paths.vault_file, paths.master_key_file)
    )
    hidden = resolver.all_secret_values()
    with ExitStack() as stack:
        configure_logging(paths.log_dir, settings.runtime.log_level, hidden, to_console=to_console)
        stack.callback(shutdown_logging)
        warn_about_plaintext_keys(resolver)
        prune_snapshots(
            paths.snapshots_dir, settings.runtime.snapshots.max_total_megabytes * BYTES_PER_MEGABYTE
        )

        identity_service = IdentityService(paths)
        stack.enter_context(identity_service.acquire_lock())
        identity = identity_service.load_identity()

        timeline = TimelineDatabase(paths.database_file, the_clock)
        timeline.open()
        stack.callback(timeline.close)
        ledger = Ledger(paths.database_file, the_clock)
        ledger.open()
        stack.callback(ledger.close)

        runtime = settings.runtime
        bus = _build_organ_bus(
            paths, runtime.heartbeat.reply_organ, resolver, fake_organs=fake_organs
        )
        gateway = SafetyGateway(bus, ledger, timeline, the_clock, runtime.safety)
        # 时区不用传：心跳会把自己的时区填给生物状态（D42 第 3 条）。
        bio_state = BiologicalState.from_config(runtime.biomodel, the_clock)
        heartbeat = Heartbeat(
            clock=the_clock,
            timeline=timeline,
            organs=gateway,
            identity=identity,
            bio_state=bio_state,
            config=runtime.heartbeat,
        )
        logger.info(
            "核心已组装：数据根目录 {}，{}器官", paths.home, "假" if fake_organs else "真实"
        )
        yield Core(paths, settings, identity, timeline, ledger, gateway, heartbeat)
