"""以假本地器官验证空间选择、实际写入、取消和独立网页授权。"""

import asyncio
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aureus.channels import WebChannel
from aureus.common import LanguageConfig
from aureus.core.embedding_progress import EmbeddingProgress
from aureus.core.model_runtime import EmbeddingSpaces, ModelRuntime
from aureus.heartbeat import MessageRoute, Perception, TrustLevel
from aureus.heartbeat.expression_recall import EXPRESSION_MAINTENANCE_BATCH, ExpressionRecall
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.memory import ExpressionVectorIndex, LanguageContent, LanguageKind, LanguageStore
from aureus.memory.language import LanguagePackageRow
from aureus.organs import LOCAL_EXPRESSION_EMBEDDING, CallPurpose, KeyResolver, OrganBus
from aureus.organs.interface import OrganRequest, OrganResponse
from aureus.timeline import VisibilityScope
from aureus.web import create_app
from aureus.web.server import SESSION_COOKIE
from tests.web.test_server import FakeHeartbeat, Rig, set_password

SPACES = "/api/resources/learning/vector-spaces"
CLUSTERS = "/api/resources/learning/clusters"
ORIGIN = {"Origin": "http://testserver"}
ACTIVE = "a" * 64
PREVIOUS = "b" * 64
EXPRESSION = LanguageKind.EXPRESSION


class VectorRig:
    def __init__(self, tmp_path: Path) -> None:
        self.web = Rig(tmp_path)
        set_password(self.web)
        path = tmp_path / "aureus.db"
        self.language = LanguageStore(path)
        self.language.open()
        self.index = ExpressionVectorIndex(path)
        self.requests: list[OrganRequest] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.wait = False
        self.invalid = False
        self.grants: set[str] = set()
        self.models = ModelRuntime(OrganBus(), KeyResolver(tmp_path / "unused"), fake_organs=True)
        self.models.embedding_spaces = EmbeddingSpaces(expression=ACTIVE)
        self.inbox = InboxStore(path, self.web.clock)
        self.allowed: set[ConversationKey] = set()
        progress = EmbeddingProgress(
            self.models,
            True,
            self.language,
            self.index,
            inbox=self.inbox,
            allows_conversation=self.allowed.__contains__,
        )
        self.recall = ExpressionRecall(self.language, self.index, LanguageConfig(), self.embed)
        self.recall.space = ACTIVE
        self.app = create_app(
            config=self.web.config,
            auth=self.web.auth,
            sessions=self.web.sessions,
            channel=WebChannel(FakeHeartbeat()),
            timeline=self.web.timeline,
            resource_language=self.language,
            expression_cluster_grants=lambda _token: self.grants,
            embedding_progress=progress,
            expression_recall=self.recall,
        )

    async def embed(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        self.started.set()
        if self.wait:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
        return OrganResponse(
            request.organ_id,
            "",
            embeddings=((1.0, 2.0),) * len(request.embedding_inputs),
            metadata={"embedding_space": PREVIOUS if self.invalid else ACTIVE},
        )

    def add(self, number: int) -> str:
        return self.language.add_entry(
            EXPRESSION, LanguageContent(f"合成表达 {number}", "合成情景", "不传例句")
        )

    def scoped_package(self) -> str:
        heard = Perception(
            "qq",
            "synthetic-speaker",
            "测试材料",
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            MessageRoute("bot", "group", "group", "1"),
        )
        self.inbox.add(heard)
        route = ConversationKey.from_perception(heard)
        self.allowed.add(route)
        scope = route.language_scope_key()
        package = LanguagePackageRow(LanguageContent("限群表达", "合成情景"), 1, "合成来源", "{}")
        assert self.language.save_package(EXPRESSION, [package]) == 1
        key = f"package:{package.digest}"
        assert self.language.package_scope(EXPRESSION, key, [scope], confirm=True) == ()
        return scope


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[VectorRig]:
    instance = VectorRig(tmp_path)
    yield instance
    instance.web.close()


def _finished(client: TestClient, expected: str) -> dict[str, object]:
    for _ in range(100):
        job = client.get(SPACES).json()["job"]
        if job and job["state"] not in {"running", "writing"}:
            assert job["state"] == expected
            return job
        time.sleep(0.02)
    pytest.fail("补建状态没有结束")


def test_selected_space_writes_only_a_batch_and_does_not_grant_clusters(rig: VectorRig) -> None:
    for number in range(EXPRESSION_MAINTENANCE_BATCH + 1):
        rig.add(number)
    entries = rig.language.expression_candidates(None)
    rig.index.publish(PREVIOUS, entries[:1], ((1.0, 2.0),))
    with TestClient(rig.app) as client:
        assert client.get(SPACES).status_code == 401
        client.cookies.set(SESSION_COOKIE, rig.web.sessions.create())
        eligible = EXPRESSION_MAINTENANCE_BATCH + 1
        assert client.get(SPACES).json()["items"] == [
            {"space": ACTIVE, "available": True, "established": 0, "eligible": eligible},
            {"space": PREVIOUS, "available": False, "established": 1, "eligible": eligible},
        ]
        assert client.post(SPACES + "/backfill", json={"space": ACTIVE}).status_code == 403
        assert (
            client.post(SPACES + "/backfill", headers=ORIGIN, json={"space": PREVIOUS}).status_code
            == 409
        )
        assert not rig.requests
        started = client.post(SPACES + "/backfill", headers=ORIGIN, json={"space": ACTIVE})
        assert started.status_code == 202
        assert _finished(client, "written")["written"] == EXPRESSION_MAINTENANCE_BATCH
        [request] = rig.requests
        assert request.organ_id == LOCAL_EXPRESSION_EMBEDDING
        assert request.purpose is CallPurpose.OPTIONAL and request.embedding_space == ACTIVE
        assert len(request.embedding_inputs) == EXPRESSION_MAINTENANCE_BATCH
        assert "不传例句" not in str(request.embedding_inputs)
        assert rig.index.coverage(rig.language, ACTIVE, frozenset()) == (eligible - 1, eligible)
        assert client.get(CLUSTERS).json()["total"] == 0
        [indexed, *_] = rig.index.indexed(ACTIVE, entries)
        rig.grants.add(indexed.entry.entry_key)
        items = client.get(CLUSTERS).json()["items"]
        assert items and all(item["title"] == indexed.entry.text for item in items)
        location = items[0]["location"]
        assert client.get(CLUSTERS + "/detail", params={"location": location}).status_code == 200
        rig.grants.clear()
        assert client.get(CLUSTERS + "/detail", params={"location": location}).status_code == 404


@pytest.mark.parametrize("change", ["model", "source"])
def test_backfill_discards_completed_embedding_after_route_or_source_change(
    rig: VectorRig, change: str
) -> None:
    scope = rig.scoped_package()
    rig.wait = True
    with TestClient(rig.app) as client:
        client.cookies.set(SESSION_COOKIE, rig.web.sessions.create())
        started = client.post(SPACES + "/backfill", headers=ORIGIN, json={"space": ACTIVE})
        assert started.status_code == 202 and rig.started.wait(2)
        assert "限群表达" in str(rig.requests[0].embedding_inputs)
        if change == "model":
            rig.models.embedding_spaces = EmbeddingSpaces(expression=PREVIOUS)
            rig.recall.space = PREVIOUS
        else:
            rig.allowed.clear()
        rig.release.set()
        assert _finished(client, "cancelled")["written"] == 0
        assert rig.index.coverage(rig.language, ACTIVE, frozenset({scope})) == (0, 1)
        assert rig.index.coverage(rig.language, PREVIOUS, frozenset({scope})) == (0, 1)
        assert all(not item["established"] for item in client.get(SPACES).json()["items"])


@pytest.mark.parametrize(("cancel", "state"), [(True, "cancelled"), (False, "failed")])
def test_cancel_or_invalid_response_does_not_claim_writes(
    rig: VectorRig, cancel: bool, state: str
) -> None:
    rig.add(1)
    rig.wait = cancel
    rig.invalid = not cancel
    with TestClient(rig.app) as client:
        owner = rig.web.sessions.create()
        client.cookies.set(SESSION_COOKIE, owner)
        response = client.post(SPACES + "/backfill", headers=ORIGIN, json={"space": ACTIVE})
        assert response.status_code == 202 and rig.started.wait(2)
        if cancel:
            url = SPACES + "/backfill/" + response.json()["id"]
            client.cookies.set(SESSION_COOKIE, rig.web.sessions.create())
            assert client.get(SPACES).json()["job"] is None
            assert client.delete(url, headers=ORIGIN).status_code == 404
            client.cookies.set(SESSION_COOKIE, owner)
            assert client.delete(url, headers=ORIGIN).json() == {"state": "cancelled"}
        assert _finished(client, state)["written"] == 0
        assert client.get(SPACES).json()["items"][0]["established"] == 0
        assert not rig.index.indexed(ACTIVE, rig.language.expression_candidates(None))
        assert client.get(CLUSTERS).json()["total"] == 0
