"""起始兴趣配置的校验（D79）。"""

import pytest
from pydantic import ValidationError

from aureus.identity.models import Persona


def test_seed_interests_trim_and_deduplicate() -> None:
    assert Persona(immutable="测试", interests=[" 天文 ", "天文", "植物"]).interests == [
        "天文",
        "植物",
    ]
    assert Persona(immutable="测试").interests == []


@pytest.mark.parametrize("values", [[" "], ["字" * 31], [str(i) for i in range(21)]])
def test_seed_interests_reject_invalid_size(values: list[str]) -> None:
    with pytest.raises(ValidationError, match="interests"):
        Persona(immutable="测试", interests=values)
