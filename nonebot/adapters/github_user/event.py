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


class WebhookEvent(Event):
    """GitHub Webhook 事件（由 HTTP 入口投递进来）。"""

    event: str = ""
    """``X-GitHub-Event``，如 ``pull_request`` / ``issue_comment``。"""

    action: Optional[str] = None
    """``payload.action``，如 ``opened`` / ``created``。"""

    delivery_id: Optional[str] = None
    """``X-GitHub-Delivery``，可用于去重。"""

    repository: Optional[str] = None
    """``owner/name``。"""

    sender: Optional[str] = None
    """触发者登录名。"""

    payload: Dict[str, Any] = {}
    """原始 webhook JSON。"""

    @override
    def get_type(self) -> str:
        return "notice"

    @override
    def get_event_name(self) -> str:
        return f"{self.event}.{self.action}" if self.action else self.event

    @override
    def get_event_description(self) -> str:
        return escape_tag(
            f"{self.get_event_name()} repo={self.repository or '?'} "
            f"sender={self.sender or '?'}"
        )

    @override
    def get_user_id(self) -> str:
        if not self.sender:
            raise ValueError("Event has no user_id!")
        return self.sender


__all__ = ["Event", "RawEvent", "WebhookEvent"]
