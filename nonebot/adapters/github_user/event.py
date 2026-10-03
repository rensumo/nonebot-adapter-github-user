"""事件类型。

当前适配器以「用专用账号访问 GitHub」为主，尚未接入 GitHub Webhook，因此这里只提供最小可用的基类与一个通用事件，供后续扩展使用。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from typing_extensions import override

from nonebot.adapters import Event as BaseEvent
from nonebot.compat import model_dump
from nonebot.utils import escape_tag


class Event(BaseEvent):
    """GitHub 用户账号适配器事件基类。"""

    @override
    def get_event_name(self) -> str:
        return self.get_type()

    @override
    def get_event_description(self) -> str:
        return escape_tag(repr(model_dump(self)))

    @override
    def get_message(self):
        raise ValueError("Event has no message!")

    @override
    def get_user_id(self) -> str:
        raise ValueError("Event has no user_id!")

    @override
    def get_session_id(self) -> str:
        raise ValueError("Event has no session_id!")

    @override
    def is_tome(self) -> bool:
        return False


class RawEvent(Event):
    """携带原始负载的通用事件。"""

    kind: str = "github_user"
    """事件种类，供插件按类型分发。"""

    payload: Dict[str, Any] = {}
    """原始数据。"""

    source: Optional[str] = None
    """事件来源说明，例如仓库全名或页面地址。"""

    @override
    def get_type(self) -> str:
        return self.kind


__all__ = ["Event", "RawEvent"]
