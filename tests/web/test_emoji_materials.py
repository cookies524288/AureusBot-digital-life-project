"""表情交换与替换只在临时库上执行。"""

from base64 import b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image

from aureus.common import ManualClock
from aureus.memory import EmojiStore
from aureus.organs import OrganRequest, OrganResponse
from aureus.web.resource_api import register_resource_routes

ORIGIN = {"Origin": "http://testserver"}
MATERIALS = "/api/resources/emoji/materials"
REPLACE = "/api/resources/emoji/replacement"
START = datetime(2026, 1, 1, tzinfo=UTC)


def _png(color: str) -> bytes:
    with Image.new("RGB", (8, 8), color) as image, BytesIO() as stream:
        image.save(stream, format="PNG")
        return stream.getvalue()


def _client(store: EmojiStore, clock: ManualClock, requests: list[OrganRequest]) -> TestClient:
    async def ready() -> str:
        return "isolated"

    def same_origin(request: Request) -> bool:
        return request.headers.get("origin") == ORIGIN["Origin"]

    async def suggest(request: OrganRequest) -> OrganResponse:
        requests.append(request)
        return OrganResponse("emoji", '{"selected": 0}')

    app = FastAPI()
    register_resource_routes(
        app,
        ready,
        None,
        None,
        emojis=store,
        clock=clock,
        same_origin=same_origin,
        suggest_emoji_replacement=suggest,
    )
    return TestClient(app)


def test_emoji_material_preview_checks_bytes_and_import_preserves_existing(tmp_path: Path) -> None:
    clock = ManualClock(START)
    source = EmojiStore(tmp_path / "source.db", tmp_path / "source", clock)
    destination = EmojiStore(tmp_path / "dest.db", tmp_path / "dest", clock, registration_limit=1)
    source.open()
    destination.open()
    images = [_png(color) for color in ("red", "blue", "green")]
    digests = [source.upload(data, registration_description="合成素材")[0] for data in images]
    first, _ = destination.upload(images[0], registration_description="本地描述")
    assert destination.review_catalog(first, "rejected")
    with _client(source, clock, []) as client:
        exported = client.post(
            f"{MATERIALS}/export",
            headers=ORIGIN,
            json={
                "keys": [f"asset:{digest}" for digest in digests],
                "source_license": "合成测试 / CC0",
            },
        )
        assert exported.status_code == 200
        package = exported.json()
        assert package["format"] == "aureus.emoji.materials"
        assert all(
            set(item) == {"digest", "description", "source_license", "image"}
            for item in package["members"]
        )
        assert client.get("/api/resources/emoji/thumbnail-cache").json() == {
            "persistent": False,
            "total": 0,
        }
    with _client(destination, clock, []) as client:
        path = f"{MATERIALS}/import"
        assert client.post(path, json=package).status_code == 403
        bad = {**package, "members": [{**package["members"][0], "path": "../x"}]}
        assert client.post(path, headers=ORIGIN, json=bad).status_code == 422
        data = b"invalid image"
        damaged = {
            **package,
            "members": [
                {
                    **package["members"][0],
                    "image": b64encode(data).decode(),
                    "digest": sha256(data).hexdigest(),
                }
            ],
        }
        assert client.post(path, headers=ORIGIN, json=damaged).status_code == 422
        selected = {"register": True}
        preview = client.post(path, headers=ORIGIN, json=package, params=selected).json()
        assert (preview["added"], preview["skipped"], preview["capacity_blocked"]) == (2, 1, 2)
        assert destination.read_media(digests[1]) is None
        assert (
            client.post(
                path, headers=ORIGIN, json=package, params={"confirmed": preview["digest"]}
            ).status_code
            == 409
        )
        confirmed = {**selected, "confirmed": preview["digest"]}
        installed = client.post(path, headers=ORIGIN, json=package, params=confirmed).json()
        assert (installed["added"], installed["registered"]) == (2, 0)
        repeated = client.post(path, headers=ORIGIN, json=package, params=confirmed).json()
        assert (repeated["added"], repeated["skipped"]) == (0, 3)
        assert destination.read_media(digests[1]) == images[1]
        original = next(item for item in destination.browse("", 0, 10) if item.digest == first)
        assert original.review == "rejected" and original.description == "本地描述"


def test_emoji_replacement_preview_expiration_cancel_and_model_refusal(tmp_path: Path) -> None:
    clock = ManualClock(START)
    store = EmojiStore(tmp_path / "emoji.db", tmp_path, clock, registration_limit=1)
    store.open()
    old = store.upload(_png("red"), registration_description="旧")[0]
    new = store.upload(_png("blue"), registration_description="新")[0]
    requests: list[OrganRequest] = []
    with _client(store, clock, requests) as client:
        preview = f"{REPLACE}/{new}"
        payload = client.get(preview).json()["items"][0]
        assert not requests and store.is_registered(old) and not store.is_registered(new)
        assert client.post(f"{preview}/suggest", headers=ORIGIN).json() == {"item": None}
        assert len(requests) == 1 and requests[0].context["task"] == "emoji_replacement"
        payload.pop("incoming_description")
        payload.pop("outgoing_description")
        clock.advance(timedelta(minutes=5))
        assert client.post(f"{REPLACE}/confirm", headers=ORIGIN, json=payload).status_code == 409
        assert store.is_registered(old) and not store.is_registered(new)
        fresh = client.get(preview).json()["items"][0]
        payload.update(token=fresh["token"], expires=fresh["expires"])
        assert client.post(f"{REPLACE}/confirm", headers=ORIGIN, json=payload).json() == {
            "ok": True
        }
        assert not store.is_registered(old) and store.is_registered(new)
