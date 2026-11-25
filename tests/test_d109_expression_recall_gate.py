"""D109：质检状态和回复候选资格分离。"""

from aureus.memory.language_matching import (
    ExpressionReviewStatus,
    LanguageEntry,
    select_expressions,
)


def test_rejected_or_pending_expression_is_still_selectable_when_enabled() -> None:
    entries = [
        LanguageEntry(
            "轻松说法",
            "庆祝",
            "",
            1,
            (),
            frozenset(),
            review_status=ExpressionReviewStatus.REJECTED,
        ),
        LanguageEntry(
            "待定说法",
            "庆祝",
            "",
            1,
            (),
            frozenset(),
            review_status=ExpressionReviewStatus.PENDING,
        ),
    ]
    selected = select_expressions(entries, "庆祝", "", (), "speaker")
    assert {entry.text for entry in selected} == {"轻松说法", "待定说法"}
