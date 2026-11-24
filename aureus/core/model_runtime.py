"""模型运行接线：准备新器官，文件写入成功后在原总线上热切换（D86）。"""

from collections.abc import Callable
from dataclasses import dataclass

from aureus.organs import (
    LOCAL_EXPRESSION_EMBEDDING,
    FakePlanner,
    FakeReplyer,
    KeyResolver,
    LLMOrgan,
    ModelsConfig,
    Organ,
    OrganBus,
    PromptStore,
)
from aureus.organs.llm_adapter import (
    EmbeddingOrgan,
    conversational_routes,
    local_expression_endpoint,
)

EMBEDDING_ORGANS = {"text_embedding": "embedding", "image_embedding": "image_embedding"}


@dataclass(frozen=True)
class PreparedModels:
    """已校验和构造的器官，以及确有可用对话模型的器官名。"""

    organs: tuple[Organ, ...]
    available: frozenset[str]


@dataclass(frozen=True)
class EmbeddingSpaces:
    """同一次热切换接管的实际空间；比较快照身份可发现中途切换。"""

    expression: str | None = None
    text: tuple[tuple[int, str], ...] = ()
    image: tuple[tuple[int, str], ...] = ()


class ModelRuntime:
    """保留原总线和启动时的密钥解析器，不替换安全网关或重置任何计数。"""

    def __init__(
        self,
        bus: OrganBus,
        resolver: KeyResolver,
        *,
        fake_organs: bool,
        fake_reply_organ: str | None = None,
        fake_plan_organ: str | None = None,
        prompts: PromptStore | None = None,
    ) -> None:
        self.bus = bus
        self._resolver = resolver
        self.prompts = prompts
        # 来源是启动参数 --fake；热重载不能把假器官切换成真实调用。
        self._fake_organs = fake_organs
        self.embedding_spaces = EmbeddingSpaces()
        self._fake_reply_organ = fake_reply_organ if fake_organs else None
        self._fake_plan_organ = fake_plan_organ if fake_organs else None
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

    @property
    def local_expression_space(self) -> str | None:
        """心跳补建和查询共用的本地表达空间。"""
        return self.embedding_spaces.expression

    def prepare(self, config: ModelsConfig) -> PreparedModels:
        """只构造、不应用；可在线程中执行，失败时文件和原器官都不改变。"""
        organs: list[Organ] = []
        available: set[str] = set()
        for name in config.organs:
            if name == LOCAL_EXPRESSION_EMBEDDING:
                continue
            routes = config.routes_for(name)
            kind = EMBEDDING_ORGANS.get(name)
            if kind is not None:
                embedding_routes = tuple(route for route in routes if route.model.kind == kind)
                if embedding_routes and not self._fake_organs:
                    organs.append(EmbeddingOrgan(name, embedding_routes, self._resolver))
                    available.add(name)
                    if name == "text_embedding":
                        local = next(
                            (
                                route
                                for route in embedding_routes
                                if local_expression_endpoint(route)
                            ),
                            None,
                        )
                        if local is not None:
                            organs.append(
                                EmbeddingOrgan(LOCAL_EXPRESSION_EMBEDDING, (local,), self._resolver)
                            )
                continue
            supported = conversational_routes(name, routes)
            if supported:
                available.add(name)
            if name in ("vlm", "voice") and not supported:
                continue
            if name == self._fake_plan_organ:
                organs.append(FakePlanner(organ_id=name))
                continue
            organs.append(
                FakeReplyer(organ_id=name)
                if self._fake_organs
                else LLMOrgan(name, routes, self._resolver, prompts=self.prompts)
            )
        # 启动时的假回复器来自心跳配置，models.toml 可以没有同名条目。
        if (
            self._fake_reply_organ is not None
            and self._fake_reply_organ not in config.organs
            and self._fake_reply_organ != self._fake_plan_organ
        ):
            organs.append(FakeReplyer(organ_id=self._fake_reply_organ))
        if self._fake_plan_organ is not None and self._fake_plan_organ not in config.organs:
            organs.append(FakePlanner(organ_id=self._fake_plan_organ))
        return PreparedModels(tuple(organs), frozenset(available))

    def apply(self, prepared: PreparedModels) -> None:
        """在事件循环中切换；进行中的旧调用继续完成，不重建网关或账本。"""
        embeddings = {
            organ.organ_id: organ for organ in prepared.organs if isinstance(organ, EmbeddingOrgan)
        }
        expression = embeddings.get(LOCAL_EXPRESSION_EMBEDDING)
        text = embeddings.get("text_embedding")
        image = embeddings.get("image_embedding")
        self.bus.replace_all(prepared.organs)
        self.embedding_spaces = EmbeddingSpaces(
            expression.local_expression_space if expression else None,
            text.spaces() if text else (),
            image.spaces() if image else (),
        )
        self.available_organs = prepared.available
        if self.on_reload is not None:
            self.on_reload(prepared.available)
