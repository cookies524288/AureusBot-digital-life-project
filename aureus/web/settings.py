"""网页设置页的服务层：看设定、改设定、回退版本（路线图 1.10，D63 第 1、2、4、5 条）。

不碰 HTTP：接口层把请求变成 IdentityEdit 交给这里。顺序是先核对密码（改身份事实和
不变人格要再输一次），再写回 identity.toml 并记版本（IdentityEditor），最后让心跳换上
新设定、记时间线（Heartbeat.apply_identity_edit）。前两步任何一步失败，心跳都不动。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from aureus.identity import (
    PERSONA_LAYERS,
    Identity,
    IdentityEdit,
    IdentityEditError,
    IdentityEditor,
    IdentityEditResult,
    IdentityHistory,
    IdentityLayer,
)

from .auth import WebAuth

# 设置页列出每一层最近几个版本，回退只能选这几个（D63 第 5 条）。
ROLLBACK_CHOICES = 3
# 这条记进时间线的来源，用户在网页上改的（D63 第 4 条）。
VIA_WEB = "web"


class HeartbeatHandle(Protocol):
    """设置页用到的心跳接口。"""

    @property
    def identity(self) -> Identity:
        """此刻生效的身份。"""
        ...

    def apply_identity_edit(
        self,
        identity: Identity,
        layers: Sequence[IdentityLayer],
        *,
        up_to_version_id: int,
        via: str,
    ) -> None:
        """用户在运行中改了设定，下一次回复就用新设定。"""
        ...


@dataclass(frozen=True)
class SettingsService:
    """设置页背后的服务。"""

    editor: IdentityEditor
    history: IdentityHistory
    heartbeat: HeartbeatHandle
    auth: WebAuth

    def view(self) -> dict[str, Any]:
        """页面要显示的全部内容：五层现在的样子、锁定状态、每层最近的版本。"""
        identity = self.heartbeat.identity
        persona = identity.persona
        layers: dict[str, Any] = {
            IdentityLayer.FACTS.value: {
                "facts": identity.facts.model_dump(mode="json"),
                "user": identity.user.model_dump(mode="json"),
            },
            IdentityLayer.IMMUTABLE.value: persona.immutable,
        }
        for layer in PERSONA_LAYERS:
            layers[layer.value] = getattr(persona, layer.value)
        return {
            "layers": layers,
            "locks": persona.locks.model_dump(),
            "history": {
                layer.value: [
                    {
                        "id": version.id,
                        "source": version.source.describe(),
                        "time": version.created_at.isoformat(),
                        "content": version.content,
                    }
                    for version in self.history.versions(layer, limit=ROLLBACK_CHOICES)
                ]
                for layer in PERSONA_LAYERS
            },
        }

    def apply(
        self, edit: IdentityEdit, *, password: str | None, remote_addr: str
    ) -> IdentityEditResult:
        """做一次修改。改身份事实和不变人格时，password 必须是当前密码。

        Raises:
            LoginLockedError、LoginRejectedError: 需要密码而密码没通过（计入登录失败限流）。
            IdentityEditError、IdentityValidationError: 修改不合法，文件和心跳都没动。
        """
        if edit.requires_password():
            self.auth.confirm_password(password or "", remote_addr)
        result = self.editor.apply(edit)
        self.heartbeat.apply_identity_edit(
            result.identity,
            result.changed_layers,
            up_to_version_id=result.up_to_version_id,
            via=VIA_WEB,
        )
        return result

    def roll_back(
        self, layer: IdentityLayer, version_id: int, *, remote_addr: str
    ) -> IdentityEditResult:
        """回退到这一层最近几个版本里的一个：存成一个新版本，历史不删（D62、D63 第 5 条）。

        只开放不用输密码的三层：底线层的回退和改它一样，要走 apply 并输密码。

        Raises:
            IdentityEditError: 这一层不能这样回退，或者这个版本不在最近几个里。
        """
        if layer not in PERSONA_LAYERS:
            raise IdentityEditError(f"{layer.describe()}不能在这里回退。")
        recent = self.history.versions(layer, limit=ROLLBACK_CHOICES)
        chosen = next((version for version in recent if version.id == version_id), None)
        if chosen is None:
            raise IdentityEditError("只能回退到最近的几个版本。")
        edit = IdentityEdit(text={layer: chosen.content})
        return self.apply(edit, password=None, remote_addr=remote_addr)
