"""Bot 实现：用 token 行动的 Bot（webhook 事件也交给它）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Union

from typing_extensions import override

from nonebot.adapters import Bot as BaseBot
from nonebot.message import handle_event

from .event import Event, WebhookEvent
from .exception import ActionFailed
from .message import Message, MessageSegment
from .webhook import ReplyTarget, WebhookPayload, reply_target

if TYPE_CHECKING:
    from .adapter import Adapter
    from .api import GitHubAPI


class Bot(BaseBot):
    """用 token 行动的 GitHub Bot。

    - webhook 事件由适配器投递给它处理（``self_id`` 是 token 对应的登录名）
    - ``send`` 会根据事件里的仓库与编号自动挑选评论接口，因此插件里
      可以像平常一样 ``await matcher.send("...")``
    - 主动干活直接用 ``bot.api``（或 ``get_github_api()``）
    """

    adapter: "Adapter"
    api: "GitHubAPI"

    @override
    def __init__(self, adapter: "Adapter", api: "GitHubAPI", self_id: str) -> None:
        super().__init__(adapter, self_id)
        self.api = api

    def __repr__(self) -> str:
        return f"<Bot self_id={self.self_id!r} adapter={self.adapter.get_name()!r}>"

    @override
    async def handle_event(self, event: Event) -> None:
        await handle_event(self, event)

    @override
    async def send(
        self,
        event: Event,
        message: Union[str, Message, MessageSegment],
        **kwargs: Any,
    ) -> Any:
        text = message if isinstance(message, str) else str(message)
        target = self.reply_target(event)
        if target is None:
            raise ActionFailed(
                "无法从该事件推断回复目标；send 支持 issue / PR / 行内评论 / commit / "
                "discussion 这几类 webhook 事件"
            )
        return await self.api.reply(target, text)

    @staticmethod
    def reply_target(event: Event) -> Optional[ReplyTarget]:
        """从事件里推断回复目标（非 webhook 事件返回 None）。"""

        if not isinstance(event, WebhookEvent):
            return None
        return reply_target(
            WebhookPayload(
                event=event.event,
                data=event.payload,
                action=event.action,
            )
        )

    async def get_authenticated_user(self) -> Optional[str]:
        """用当前 token 调一次 ``GET /user``，返回登录名。"""

        user = await self.api.get_authenticated_user()
        return user.get("login") if isinstance(user, dict) else None


__all__ = ["Bot"]
