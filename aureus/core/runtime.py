"""核心组装 core：启动和关闭流程（路线图 0.10，启动顺序见 15 号文档 7.6 节）。

顺序：读配置 → 解锁密钥库 → 设置日志 → 明文密钥警告 → 单实例锁 → 读身份 →
打开时间线、身份版本历史（和 identity.toml 对一遍）、账本、人物档案、反馈 →
注册器官、建安全网关 → 建生物状态和心跳。关闭时倒着来。

本模块没有放进 aureus.core 的 __init__ 导出：它要用心跳，心跳又要用 core 的生物模型，
导出会造成循环导入。要用时写 `from aureus.core.runtime import open_core`。
"""

import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

from loguru import logger

from aureus.actions.web import WebReader
from aureus.agenda import AgendaStore
from aureus.agenda.findings import FindingStore
from aureus.agenda.interests import InterestStore
from aureus.body import Body, BodySampleStore
from aureus.channels import CHANNEL_NAME as TERMINAL_CHANNEL
from aureus.channels import TERMINAL_USER_ID, WEB_CHANNEL_NAME, WEB_USER_ID
from aureus.common import (
    AureusPaths,
    Clock,
    ConfigError,
    HeartbeatConfig,
    Settings,
    SystemClock,
    configure_logging,
    load_settings,
    shutdown_logging,
)
from aureus.common.vault import SecretVault
from aureus.core.model_runtime import ModelRuntime
from aureus.core.snapshots import BYTES_PER_MEGABYTE, prune_snapshots
from aureus.feedback import FeedbackStore
from aureus.heartbeat import Heartbeat
from aureus.hormones import Hormones
from aureus.identity import Identity, IdentityHistory
from aureus.identity.service import IdentityService
from aureus.memory import DiaryBook, MemoryIndex
from aureus.mood import Mood
from aureus.organs import FakeReplyer, KeyResolver, OrganBus, load_models_config
from aureus.organs.api_keys import warn_about_plaintext_keys
from aureus.people import AffinityBook, PeopleDirectory, PeopleError
from aureus.safety import EmergencyStop, Ledger, PermissionChecker, SafetyGateway
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
    people: PeopleDirectory
    identity_history: IdentityHistory
    feedback: FeedbackStore | None = None  # [feedback] enabled 关闭时为 None
    models: ModelRuntime | None = None


def _open_people(
    paths: AureusPaths, clock: Clock, identity: Identity, stack: ExitStack
) -> PeopleDirectory:
    """打开人物档案，登记用户和用户的账号（1.8，05 号文档）。

    用户的账号来自 identity.toml，再加上本机终端和登录后的网页：终端只有本机能用，
    网页要密码登录（D63 第 8 条），所以都默认就是用户。
    账号已经登记在别人名下时报配置错误，不悄悄换主人。
    """
    people = PeopleDirectory(paths.database_file, clock)
    people.open()
    stack.callback(people.close)
    accounts = [(account.platform, account.id) for account in identity.user.accounts]
    accounts.append((TERMINAL_CHANNEL, TERMINAL_USER_ID))
    accounts.append((WEB_CHANNEL_NAME, WEB_USER_ID))
    try:
        people.ensure_user(identity.user.name, accounts)
    except PeopleError as error:
        raise ConfigError(f"身份配置里用户的账号有问题：{error}") from None
    return people


def _build_organ_bus(
    paths: AureusPaths, heartbeat: HeartbeatConfig, resolver: KeyResolver, *, fake_organs: bool
) -> ModelRuntime:
    """注册器官。假器官模式不读 models.toml、不调用任何模型，不花钱。"""
    bus = OrganBus()
    models = ModelRuntime(bus, resolver, fake_organs=fake_organs)
    if fake_organs:
        bus.register(FakeReplyer(organ_id=heartbeat.reply_organ))
        models.available_organs = frozenset(bus.list_organs())
        return models
    models_config = load_models_config(paths)
    models.apply(models.prepare(models_config))
    # 启动时就报错，而不是等用户说第一句话才发现器官不存在。
    if not bus.is_registered(heartbeat.reply_organ):
        raise ConfigError(
            f"心跳要用的回复器官 {heartbeat.reply_organ} 没有在 models.toml 里配置"
            f"（缺少 [organs.{heartbeat.reply_organ}]）。"
        )
    if heartbeat.planning_enabled and not bus.is_registered(heartbeat.plan_organ):
        raise ConfigError(
            f"心跳要用的规划器官 {heartbeat.plan_organ} 没有在 models.toml 里配置"
            f"（缺少 [organs.{heartbeat.plan_organ}]）。"
            "不想用规划器，可以在 aureus.toml 的 [heartbeat] 里写 planning_enabled = false。"
        )
    return models


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
    with ExitStack() as stack:
        identity_service = IdentityService(paths)
        stack.enter_context(identity_service.acquire_lock())
        settings = load_settings(paths)
        # 先解锁，再设日志：解锁过程不写日志，解开的密钥一开始就在日志过滤里。
        resolver = KeyResolver(
            paths.env_file, vault=SecretVault(paths.vault_file, paths.master_key_file)
        )
        hidden = resolver.all_secret_values()
        configure_logging(paths.log_dir, settings.runtime.log_level, hidden, to_console=to_console)
        stack.callback(shutdown_logging)
        warn_about_plaintext_keys(resolver)
        prune_snapshots(
            paths.snapshots_dir, settings.runtime.snapshots.max_total_megabytes * BYTES_PER_MEGABYTE
        )

        from_file = identity_service.load_identity()

        timeline = TimelineDatabase(paths.database_file, the_clock)
        timeline.open()
        stack.callback(timeline.close)
        # 人设后三层以数据库里的最新版本为准；identity.toml 改过的层在这里记成一次用户修改（D62）。
        identity_history = IdentityHistory(paths.database_file, the_clock)
        identity_history.open()
        stack.callback(identity_history.close)
        identity = identity_history.sync_from_file(from_file)
        runtime = settings.runtime
        calendar = runtime.time.build_calendar()
        ledger = Ledger(paths.database_file, the_clock, calendar=calendar)
        ledger.open()
        stack.callback(ledger.close)
        # 身体信号样本（D64）：启动时清掉超过保留期限的行；样本只是读数，不是经历。
        # 扮演模式（D65）是对照组：不创建身体，也不记样本。
        roleplay = runtime.heartbeat.roleplay_mode
        samples: BodySampleStore | None = None
        if not roleplay:
            samples = BodySampleStore(paths.database_file, the_clock)
            samples.open()
            stack.callback(samples.close)
            removed = samples.prune(runtime.body.sample_retention_days)
            if removed:
                logger.info("清理了 {} 行过期的身体信号样本", removed)
        people = _open_people(paths, the_clock, identity, stack)
        # 反馈（D67）：关掉时不创建，认知状态的信心显示没有数据，其它行为不变。
        feedback: FeedbackStore | None = None
        if runtime.feedback.enabled:
            feedback = FeedbackStore(paths.database_file, runtime.feedback, the_clock)
            feedback.open()
            stack.callback(feedback.close)

        heartbeat_config = runtime.heartbeat
        if heartbeat_config.advance_biomodel is not None:
            logger.warning(
                "配置里的 [heartbeat] advance_biomodel 已不起作用（旧的生物模型在 D69 删除），"
                "请从配置里删掉它"
            )
        if fake_organs and heartbeat_config.planning_enabled:
            # 假器官只有回复器：试运行和测试时不规划，每次一次调用。
            heartbeat_config = heartbeat_config.model_copy(update={"planning_enabled": False})
        models = _build_organ_bus(paths, heartbeat_config, resolver, fake_organs=fake_organs)
        bus = models.bus
        # 网关和心跳共用同一个权限检查器，两边看到的急停状态一致（D54）。
        emergency_stop = EmergencyStop(paths.halt_file, the_clock)
        permissions = PermissionChecker(emergency_stop)
        # 身体和账本用同一个日历，每日额度和每月上限的边界一致（D55、D56）。
        body: Body | None = None
        if not roleplay:
            body = Body(
                runtime.body, runtime.safety.monthly_token_limit, ledger, the_clock, calendar
            )
        else:
            logger.warning("扮演模式已开启：身体信号全部关闭，只在提示词里写「你有精力和心情」。")
        gateway = SafetyGateway(
            bus, ledger, timeline, the_clock, runtime.safety, permissions, energy_gate=body
        )
        # 激素（D69）：关掉开关或扮演模式（D65，对照组只有提示词）时不创建。
        hormones = Hormones(runtime.hormones) if runtime.hormones.enabled and not roleplay else None
        # 好感（D71）：扮演模式是对照组，和激素一样不创建；关掉时不记变化、不进上下文。
        # 日程（D74）：记下答应的事、到期提醒、兑现记账；扮演模式是对照组，不创建。
        agenda: AgendaStore | None = None
        if runtime.agenda.enabled and not roleplay:
            agenda = AgendaStore(paths.database_file, the_clock, runtime.agenda)
            agenda.open()
            stack.callback(agenda.close)
        # 心情（D73）：扮演模式是对照组，不创建；关掉时不算心情、不进上下文。
        mood = Mood(runtime.mood) if runtime.mood.enabled and not roleplay else None
        affinity: AffinityBook | None = None
        if runtime.people.affinity_enabled and not roleplay:
            affinity = AffinityBook(
                paths.database_file,
                the_clock,
                runtime.people,
                calendar,
                min_confidence=runtime.hormones.min_confidence,
            )
            affinity.open()
            stack.callback(affinity.close)
        interests = InterestStore(paths.database_file, the_clock, runtime.interests)
        findings = FindingStore(paths.database_file, the_clock)
        for store in (interests, findings):
            store.open()
            stack.callback(store.close)
        try:
            added, removed = interests.sync_seeds(identity.persona.interests)
            logger.info("起始兴趣同步：加入 {} 条，结束 {} 条", added, removed)
        except Exception:
            logger.exception("起始兴趣同步失败，继续启动")
        exploration = runtime.exploration
        persona_growth = runtime.persona_growth
        memory: MemoryIndex | None = None
        if runtime.memory.enabled:
            try:
                memory = MemoryIndex(paths.database_file, timeline)
                memory.open()
                stack.callback(memory.close)
            except sqlite3.Error:
                memory = None
                logger.exception("记忆索引打不开，本次运行关闭记忆")
        if roleplay:
            exploration = exploration.model_copy(update={"enabled": False})
            persona_growth = persona_growth.model_copy(update={"enabled": False})
        heartbeat = Heartbeat(
            clock=the_clock,
            timeline=timeline,
            organs=gateway,
            identity=identity,
            config=heartbeat_config,
            timezone=calendar.timezone,
            permissions=permissions,
            emergency_stop=emergency_stop,
            body=body,
            people=people,
            identity_history=identity_history,
            samples=samples,
            feedback=feedback,
            judge=runtime.judge,
            hormones=hormones,
            affinity=affinity,
            empathy=runtime.empathy,
            mood=mood,
            agenda=agenda,
            initiative=None if roleplay else runtime.initiative,
            calendar=calendar,
            interests=interests,
            findings=findings,
            exploration=exploration,
            search=None,  # D79：当前唯一的搜索选项是 none。
            reader=WebReader(exploration),
            has_exploration_organ=exploration.organ in models.available_organs,
            memory=memory,
            memory_config=runtime.memory,
            diary=DiaryBook(paths.diary_dir),
            diary_config=runtime.diary,
            has_diary_organ=runtime.diary.organ in models.available_organs,
            persona_growth=persona_growth,
            has_persona_organ=persona_growth.organ in models.available_organs,
        )
        models.on_reload = heartbeat.update_available_organs
        logger.info(
            "核心已组装：数据根目录 {}，{}器官", paths.home, "假" if fake_organs else "真实"
        )
        yield Core(
            paths,
            settings,
            identity,
            timeline,
            ledger,
            gateway,
            heartbeat,
            people,
            identity_history,
            feedback,
            models,
        )
