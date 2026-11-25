"""表达向量候选上限（D119）。"""

from aureus.memory.expression_index import (
    MAX_VECTOR_CANDIDATES,
    IndexedExpression,
    rank_expressions,
)
from aureus.memory.language_matching import LanguageEntry


def test_rank_expressions_respects_candidate_limit() -> None:
    """60 条已索引候选按上限截断，默认硬上限是 50。"""
    indexed = [
        IndexedExpression(
            LanguageEntry(
                f"说法{index:02}",
                f"场景{index:02}",
                "",
                1,
                (index,),
                frozenset(),
                entry_key=f"synthetic:{index:02}",
            ),
            (1.0, index / 100),
            0,
        )
        for index in range(60)
    ]
    assert MAX_VECTOR_CANDIDATES == 50
    assert len(rank_expressions(indexed, (1.0, 0.0), 50)) == 50
    assert len(rank_expressions(indexed, (1.0, 0.0), 5)) == 5
