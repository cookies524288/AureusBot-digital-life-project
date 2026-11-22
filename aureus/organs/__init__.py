"""Aureus 器官模块：器官接口、总线、假器官、真实器官（LLM）"""

from .api_keys import KeyResolver
from .fake import FakePlanner, FakeReplyer
from .interface import (
    CallAttempt,
    CallPurpose,
    ModelPreference,
    Organ,
    OrganCallError,
    OrganRequest,
    OrganResponse,
)
from .llm_adapter import EMPTY_SYSTEM_MESSAGE, LLMOrgan, assemble_messages, count_prompt_tokens
from .model_config import (
    API_KEY_ENV_PATTERN,
    BUILTIN_ORGANS,
    ModelKind,
    ModelRoute,
    ModelsConfig,
    OrganStrategy,
    ProviderProtocol,
    ReasoningLevel,
    load_models_config,
    validate_models_config,
)
from .organ_bus import OrganBus
from .prompts import (
    PromptStore,
    PromptTemplate,
    PromptTemplateError,
    join_prompt,
    prompt_template_keys,
)
from .topology import organ_tasks

__all__ = [
    "API_KEY_ENV_PATTERN",
    "BUILTIN_ORGANS",
    "EMPTY_SYSTEM_MESSAGE",
    "CallAttempt",
    "CallPurpose",
    "FakePlanner",
    "FakeReplyer",
    "KeyResolver",
    "LLMOrgan",
    "ModelKind",
    "ModelPreference",
    "ModelRoute",
    "ModelsConfig",
    "Organ",
    "OrganBus",
    "OrganCallError",
    "OrganRequest",
    "OrganResponse",
    "OrganStrategy",
    "PromptStore",
    "PromptTemplate",
    "PromptTemplateError",
    "ProviderProtocol",
    "ReasoningLevel",
    "assemble_messages",
    "count_prompt_tokens",
    "join_prompt",
    "load_models_config",
    "organ_tasks",
    "prompt_template_keys",
    "validate_models_config",
]
