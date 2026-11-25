"""用真实接口和合成来源验证表达质检翻页与授权挽回。"""

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aureus.common import ManualClock
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.heartbeat.language_runner import language_sample
from aureus.heartbeat.perception import MessageRoute, Perception, TrustLevel
from aureus.memory import (
    ExpressionQualityDecision,
    ExpressionQualityReason,
    ExpressionQualityStatus,
    ExpressionVectorIndex,
    LanguageKind,
    LanguageProposal,
    LanguageStore,
)
from aureus.memory.language_matching import ExpressionReviewStatus
from aureus.memory.language_package import make_package
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope
from aureus.web.expression_quality_api import register_expression_quality_routes

NOW = datetime(2026, 11, 24, tzinfo=UTC)
ENDPOINT = "/api/resources/learning/quality"


def test_rescue_preserves_rejection_and_cannot_expand_scope(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.sqlite"
    clock = ManualClock(NOW)
    timeline = TimelineDatabase(path, clock)
    timeline.open()
    inbox = InboxStore(path, clock)
    store = LanguageStore(path)
    store.open()
    allowed = {"10", "20"}

    def seed(recipient: str, phrase: str, *, valid: bool = True) -> str:
        perception = Perception(
            "qq",
            "sender",
            "合成对话正文不要返回",
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("bot", "group", recipient, phrase),
            source_message_id="same-id-in-different-groups" if phrase == "一起乐呵" else phrase,
        )
        inbox.add(perception, conversation_name="同名群")
        key = ConversationKey.from_perception(perception)
        messages = inbox.list_messages(key, limit=100)
        message = next(item for item in messages if item.perception.route == perception.route)
        record = timeline.append(
            "message",
            perception.content,
            ActorType.PERSON,
            "sender",
            VisibilityScope.GROUP,
            {
                "channel": "qq",
                "inbox_message_id": message.id if valid else 9999,
                "message_id": phrase,
                "account_id": "bot",
                "message_type": "group",
                "recipient_id": recipient,
                "trust": perception.trust.value,
                "source_message_id": perception.source_message_id,
            },
        )
        scope = key.language_scope_key()
        sample = language_sample(record, perception.source_message_id, scope)
        store.observe([sample], ())
        proposal = LanguageProposal(LanguageKind.EXPRESSION, phrase, "合成庆祝", (record.id,))
        store.observe_extraction(
            LanguageKind.EXPRESSION,
            scope,
            [sample],
            {record.id: sample.content},
            [proposal],
            quality={
                proposal: ExpressionQualityDecision(
                    ExpressionQualityStatus.REJECTED,
                    ExpressionQualityReason.MISMATCH,
                    NOW.isoformat(),
                )
            },
        )
        return scope

    scope = seed("10", "一起乐呵")
    other_scope = seed("20", "一起乐呵")
    seed("10", "无效来源", valid=False)
    seed("10", "已移除")

    async def require_ready(request: Request) -> str:
        if request.cookies.get("session") != "test":
            raise HTTPException(401)
        return "test"

    app = FastAPI()
    register_expression_quality_routes(
        app,
        require_ready,
        lambda request: request.headers.get("origin") == "http://testserver",
        lambda token: token == "test",
        store,
        timeline,
        inbox,
        lambda route: route.recipient_id in allowed,
        clock,
    )
    try:
        with TestClient(app) as client:
            assert client.get(ENDPOINT).status_code == 401
            client.cookies.set("session", "test")
            first = client.get(
                ENDPOINT, params={"limit": 1, "status": "rejected", "start": "2026-11-24"}
            )
            assert first.json()["total"] == 4 and len(first.json()["items"]) == 1
            last = client.get(ENDPOINT, params={"offset": 3, "limit": 1}).json()
            assert last["items"][0]["scope"] == scope
            assert client.get(ENDPOINT, params={"end": "2026-11-23"}).json()["total"] == 0
            assert client.get(ENDPOINT, params={"scope": other_scope}).json()["total"] == 1
            assert "合成对话正文" not in first.text and "source_key" not in first.text
            rows = client.get(ENDPOINT, params={"scope": scope}).json()["items"]
            identifiers = {row["text"]: row["id"] for row in rows}
            target = f"{ENDPOINT}/{identifiers['一起乐呵']}"
            headers = {"Origin": "http://testserver"}
            rescue = {"action": "rescued"}
            assert client.post(target, json=rescue).status_code == 403
            preview = client.post(target, json=rescue, headers=headers).json()
            assert preview["scope"] == scope
            assert not store.entries(LanguageKind.EXPRESSION)
            assert (
                client.post(
                    target, headers=headers, json={**rescue, "scope": other_scope}
                ).status_code
                == 422
            )
            assert (
                client.post(
                    target, headers=headers, json={**rescue, "version": "0" * 64}
                ).status_code
                == 409
            )
            allowed.remove("10")
            assert client.get(ENDPOINT, params={"scope": scope}).json()["total"] == 0
            confirmation = {**rescue, "version": preview["version"]}
            assert client.post(target, headers=headers, json=confirmation).status_code == 409
            allowed.add("10")
            assert (
                client.post(
                    f"{ENDPOINT}/{identifiers['无效来源']}", headers=headers, json=rescue
                ).status_code
                == 409
            )
            response = client.post(target, headers=headers, json=confirmation)
            assert response.status_code == 200
            assert client.post(target, headers=headers, json=confirmation).status_code == 409
            entry_key = response.json()["entry_key"]
            assert [entry.entry_key for entry in store.expression_candidates(scope)] == [entry_key]
            assert not store.expression_candidates(other_scope)
            index = ExpressionVectorIndex(path)
            assert index.coverage(store, "synthetic", frozenset({scope})) == (0, 1)
            assert index.coverage(store, "synthetic", frozenset({other_scope})) == (0, 0)
            assert (
                json.loads(make_package(store, LanguageKind.EXPRESSION, NOW.isoformat()))["count"]
                == 0
            )
            withdraw = {"action": "withdrawn"}
            stale = client.post(target, headers=headers, json=withdraw).json()["version"]
            store.review_expressions([entry_key], ExpressionReviewStatus.APPROVED, "复核", NOW)
            assert (
                client.post(
                    target, headers=headers, json={**withdraw, "version": stale}
                ).status_code
                == 409
            )
            version = client.post(target, headers=headers, json=withdraw).json()["version"]
            assert (
                client.post(
                    target, headers=headers, json={**withdraw, "version": version}
                ).status_code
                == 200
            )
            store.set_removed(LanguageKind.EXPRESSION, [entry_key], False)
            assert not store.expression_candidates(scope)
            updated = client.get(ENDPOINT, params={"scope": scope, "offset": 2, "limit": 1}).json()[
                "items"
            ][0]
            assert updated["status"] == "rejected"
            assert [action for action, _ in updated["actions"]] == ["rescued", "withdrawn"]

            excluded = f"{ENDPOINT}/{identifiers['已移除']}"
            preview = client.post(excluded, headers=headers, json=rescue).json()
            sample = language_sample(
                timeline.append(
                    "message",
                    "其他合成正文",
                    ActorType.PERSON,
                    "sender",
                    VisibilityScope.GROUP,
                ),
                None,
                scope,
            )
            store.observe([sample], ())
            store.observe_extraction(
                LanguageKind.EXPRESSION,
                scope,
                [sample],
                {sample.record_id: sample.content},
                [
                    LanguageProposal(
                        LanguageKind.EXPRESSION, "已移除", "合成庆祝", (sample.record_id,)
                    )
                ],
            )
            [existing] = store.entries(LanguageKind.EXPRESSION)
            store.set_removed(LanguageKind.EXPRESSION, [existing.entry_key], True)
            assert (
                client.post(
                    excluded, headers=headers, json={**rescue, "version": preview["version"]}
                ).status_code
                == 409
            )
    finally:
        timeline.close()
