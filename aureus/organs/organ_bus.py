"""器官总线：管理器官注册和调度"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from aureus.organs.interface import Organ, OrganRequest, OrganResponse

if TYPE_CHECKING:
    pass


class OrganBus:
    """异步消息总线，管理器官的注册和调度"""

    def __init__(self):
        self._organs: dict[str, Organ] = {}

    def register(self, organ: Organ) -> None:
        """注册一个器官

        Args:
            organ: 实现 Organ 接口的器官对象
        """
        if organ.organ_id in self._organs:
            raise ValueError(f"器官 {organ.organ_id} 已注册")
        self._organs[organ.organ_id] = organ

    def unregister(self, organ_id: str) -> None:
        """注销一个器官"""
        if organ_id not in self._organs:
            raise ValueError(f"器官 {organ_id} 未注册")
        del self._organs[organ_id]

    def replace_all(self, organs: Sequence[Organ]) -> None:
        """一次替换登记表；进行中的 send 已持有原器官，后续调用用新器官。"""
        updated = {organ.organ_id: organ for organ in organs}
        if len(updated) != len(organs):
            raise ValueError("器官名字重复")
        self._organs = updated

    async def send(self, request: OrganRequest) -> OrganResponse:
        """发送请求到指定器官

        Args:
            request: 包含器官 ID 和请求内容的请求对象

        Returns:
            response: 器官的响应

        Raises:
            ValueError: 如果器官未注册
        """
        if request.organ_id not in self._organs:
            raise ValueError(f"器官 {request.organ_id} 未注册")

        organ = self._organs[request.organ_id]
        return await organ.process(request)

    def list_organs(self) -> list[str]:
        """列出所有已注册的器官"""
        return list(self._organs.keys())

    def is_registered(self, organ_id: str) -> bool:
        """检查器官是否已注册"""
        return organ_id in self._organs
