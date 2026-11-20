"""收获查询、分享时间窗与只追加约束（D79）。"""

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from aureus.agenda.findings import FindingStore
from aureus.common import ManualClock
from tests.agenda.test_agenda_store import NOW


@pytest.fixture
def findings(tmp_path: Path) -> Iterator[FindingStore]:
    store = FindingStore(tmp_path / "aureus.db", ManualClock(NOW))
    store.open()
    yield store
    store.close()


def test_findings_order_window_and_single_share(findings: FindingStore) -> None:
    first = findings.add(1, "第一条收获", "标题", "https://example.com/a")
    second = findings.add(2, "第二条收获", "题" * 220, "https://example.com/b")
    assert len(second.page_title) == 200
    assert findings.recent(2) == [second, first]
    assert findings.oldest_unshared(NOW) == first
    assert findings.oldest_unshared(NOW + timedelta(seconds=1)) is None
    assert findings.mark_shared(first.id, 9, NOW)
    assert not findings.mark_shared(first.id, 10, NOW)
    assert findings.oldest_unshared(NOW) == second
    stored = findings.recent(2)[1]
    assert (stored.shared_at, stored.shared_record_id) == (NOW, 9)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM findings",
        "UPDATE findings SET text = '改字'",
        "UPDATE findings SET interest_id = 3",
        "UPDATE findings SET page_title = '改标题'",
        "UPDATE findings SET page_url = 'x'",
        "UPDATE findings SET created_at = 'x'",
        "UPDATE findings SET id = 8",
        "INSERT OR REPLACE INTO findings(id,interest_id,text,page_title,page_url,created_at)"
        " VALUES(1,1,'x','x','x','x')",
    ],
)
def test_finding_triggers_reject_rewrite(findings: FindingStore, sql: str) -> None:
    findings.add(1, "收获", "标题", "https://example.com")
    with pytest.raises(sqlite3.IntegrityError), findings._require_open() as conn:
        conn.execute(sql)


def test_shared_finding_cannot_be_changed_again(findings: FindingStore) -> None:
    item = findings.add(1, "收获", "标题", "https://example.com")
    findings.mark_shared(item.id, 4, NOW)
    with pytest.raises(sqlite3.IntegrityError), findings._require_open() as conn:
        conn.execute("UPDATE findings SET shared_at = NULL, shared_record_id = NULL")
