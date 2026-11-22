"""模型运行接线：准备新器官，文件写入成功后在原总线上热切换（D86）。"""

from collections.abc import Callable
from dataclasses import dataclass

from aureus.organs import FakeReplyer, KeyResolver, LLMOrgan, ModelsConfig, Organ, OrganBus


@dataclass(frozen=True)
class PreparedModels:
    """已校验和构造的器官，以及确有可用对话模型的器官名。"""

    organs: tuple[Organ, ...]
    available: frozenset[str]


class ModelRuntime:
    """保留原总线和启动时的密钥解析器，不替换安全网关或重置任何计数。"""

    def __init__(self, bus: OrganBus, resolver: KeyResolver, *, fake_organs: bool) -> None:
        self.bus = bus
        self._resolver = resolver
        # 来源是启动参数 --fake；热重载不能把假器官切换成真实调用。
        self._fake_organs = fake_organs
        # 来源是实际应用的模型路线，只影响可选总结任务是否可以启动。空模型就是关闭。
        self.available_organs: frozenset[str] = frozenset(bus.list_organs())
        self.on_reload: Callable[[frozenset[str]], None] | None = None

    @property
    def key_resolver(self) -> KeyResolver:
        """所有运行中器官共用的内存解析器，供网页刷新密钥快照。"""
        return self._resolver

    @property
    def can_test_models(self) -> bool:
        """假器官模式下禁止发出真实模型测试。"""
        return not self._fake_organs

    def prepare(self, config: ModelsConfig) -> PreparedModels:
        """只构造、不应用；可在线程中执行，失败时文件和原器官都不改变。"""
        organs: list[Organ] = []
        available: set[str] = set()
        for name in config.organs:
            routes = config.routes_for(name)
            if any(route.model.kind == "llm" for route in routes):
                available.add(name)
            organs.append(
                FakeReplyer(organ_id=name)
                if self._fake_organs
                else LLMOrgan(name, routes, self._resolver)
            )
        return PreparedModels(tuple(organs), frozenset(available))

    def apply(self, prepared: PreparedModels) -> None:
        """在事件循环中切换；进行中的旧调用继续完成，不重建网关或账本。"""
        self.bus.replace_all(prepared.organs)
        self.available_organs = prepared.available
        if self.on_reload is not None:
            self.on_reload(prepared.available)
