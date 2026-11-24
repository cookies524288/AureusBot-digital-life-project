"""配置接口共用的临时目录、真登录和假心跳，不读用户配置。"""

from pathlib import Path

from fastapi.testclient import TestClient

from aureus.channels import WebChannel
from aureus.common import AureusPaths, ManualClock, WebConfig
from aureus.core.model_runtime import ModelRuntime
from aureus.heartbeat import registered_templates
from aureus.heartbeat.inbox_store import InboxStore
from aureus.identity import Identity
from aureus.memory import EmojiStore
from aureus.organs import KeyResolver, OrganBus, PromptStore
from aureus.timeline import TimelineDatabase
from aureus.web import LoginLimiter, PasswordStore, SessionStore, WebAuth, create_app
from aureus.web.config_files import ConfigService
from tests.web.test_timeline_api import FAST_HASHER, GOOD, NOW, FakeHeartbeat


class ConfigRig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        password_set: bool = True,
        prompt_identity: Identity | None = None,
        inbox_store: InboxStore | None = None,
        emoji_store: EmojiStore | None = None,
    ) -> None:
        self.paths = AureusPaths(tmp_path)
        self.paths.ensure_directories()
        self.clock = ManualClock(NOW)
        config = WebConfig(login_max_failures=3)
        passwords = PasswordStore(self.paths.web_password_file, FAST_HASHER)
        if password_set:
            passwords.set_password(GOOD)
        self.sessions = SessionStore(self.paths.database_file, self.clock, config.session_days)
        self.sessions.open()
        self.timeline = TimelineDatabase(self.paths.database_file, self.clock)
        self.timeline.open()
        self.auth = WebAuth(passwords, self.sessions, LoginLimiter(config, self.clock))
        self.heartbeat = FakeHeartbeat()
        self.service = ConfigService(self.paths)
        self.models = ModelRuntime(
            OrganBus(),
            KeyResolver(self.paths.env_file, environ={}),
            fake_organs=True,
            prompts=PromptStore(self.paths.home / "prompts.toml", registered_templates())
            if prompt_identity is not None
            else None,
        )

        def read_identity() -> Identity:
            assert prompt_identity is not None
            return prompt_identity

        self.app = create_app(
            config=config,
            auth=self.auth,
            sessions=self.sessions,
            channel=WebChannel(self.heartbeat),
            timeline=self.timeline,
            heartbeat=self.heartbeat,
            clock=self.clock,
            config_files=self.service,
            model_runtime=self.models,
            prompt_identity=read_identity if prompt_identity is not None else None,
            inbox_store=inbox_store,
            resource_emojis=emoji_store,
        )
        self.client = TestClient(self.app, client=("127.0.0.1", 50000))

    def log_in(self, password: str = GOOD) -> None:
        assert self.client.post("/api/login", json={"password": password}).status_code == 200

    def close(self) -> None:
        self.sessions.close()
        self.timeline.close()
