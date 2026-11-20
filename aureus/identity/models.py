"""身份数据模型。

定义身份事实、用户账号信息和四层人设结构。
所有模型使用 pydantic，严格验证（extra=forbid）。

设计参考：docs/03-subsystems.md
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aureus.common.limits import MAX_INTEREST_CHARS, MAX_SEED_INTERESTS


class Facts(BaseModel):
    """身份事实：给程序用的结构化信息。只有用户能改。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Bot 的名字")
    alias_names: list[str] = Field(
        default_factory=list, description="昵称列表：用户可以用这些名字称呼 Bot"
    )
    gender: str = Field(default="", description="性别，可以留空")
    birthday: str = Field(..., description="出生日期：YYYY-MM-DD 格式")


class UserAccount(BaseModel):
    """用户在各平台的账号标识。"""

    model_config = ConfigDict(extra="forbid")

    platform: str = Field(..., description="平台名：qq、wechat 等")
    id: str = Field(..., description="该平台上的账号 ID")


class User(BaseModel):
    """用户信息。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Bot 对用户的称呼")
    accounts: list[UserAccount] = Field(default_factory=list, description="用户在各平台的账号")


class PersonaLocks(BaseModel):
    """人设三层中各层是否被用户锁定。"""

    model_config = ConfigDict(extra="forbid")

    mutable: bool = Field(default=False, description="可变人格是否被锁定")
    behavior_style: bool = Field(default=False, description="行为风格是否被锁定")
    expression_style: bool = Field(default=False, description="表达风格是否被锁定")


class Persona(BaseModel):
    """四层人设：会被拼进给大模型的提示词里。

    层          谁能改                     注入哪些器官
    ─────────────────────────────────────────────────────
    不变人格    只有用户                   全部器官
    可变人格    它自己按规则改，用户也能改  全部器官
    行为风格    它自己按规则改，用户也能改  规划器
    表达风格    它自己按规则改，用户也能改  规划器、回复器
    """

    model_config = ConfigDict(extra="forbid")

    immutable: str = Field(..., description="不变人格：性格、经历、价值观、底线")
    mutable: str = Field(default="", description="可变人格：可以留空，让它自己慢慢长出来")
    behavior_style: str = Field(default="", description="行为风格：什么时候开口、怎么回应")
    expression_style: str = Field(default="", description="表达风格：语气、句子长短、口头禅")
    locks: PersonaLocks = Field(default_factory=PersonaLocks, description="各层的锁定状态")
    interests: list[str] = Field(default_factory=list, description="起始兴趣（D79）")

    @field_validator("interests")
    @classmethod
    def _check_interests(cls, values: list[str]) -> list[str]:
        texts = list(dict.fromkeys(text.strip() for text in values))
        valid_lengths = all(1 <= len(text) <= MAX_INTEREST_CHARS for text in texts)
        if len(texts) > MAX_SEED_INTERESTS or not valid_lengths:
            raise ValueError(
                f"interests 最多 {MAX_SEED_INTERESTS} 条，每条 1～{MAX_INTEREST_CHARS} 字"
            )
        return texts


class Identity(BaseModel):
    """完整身份配置。"""

    model_config = ConfigDict(extra="forbid")

    facts: Facts = Field(..., description="身份事实")
    user: User = Field(..., description="用户信息")
    persona: Persona = Field(..., description="四层人设")
