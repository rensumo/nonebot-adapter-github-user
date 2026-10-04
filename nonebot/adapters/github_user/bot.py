"""Bot 实现：持有某个 GitHub 账号的网页会话。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Union

from typing_extensions import override

from nonebot.adapters import Bot as BaseBot
from nonebot.message import handle_event

from .event import Event, WebhookEvent
from .exception import ActionFailed
from .message import Message, MessageSegment
from .session import GitHubSession
from .webhook import ReplyTarget, WebhookPayload, reply_target

if TYPE_CHECKING:
    import httpx

    from .adapter import Adapter
    from .api import GitHubAPI


class Bot(BaseBot):
    """GitHub 用户账号 Bot，一个实例对应一个已登录的 GitHub 账号。"""

    adapter: "Adapter"
    session: GitHubSession

    @override
    def __init__(
        self,
        adapter: "Adapter",
        session: GitHubSession,
        self_id: Optional[str] = None,
    ) -> None:
        super().__init__(adapter, self_id or session.username or session.login_name)
        self.session = session

    def __repr__(self) -> str:
        return f"<Bot self_id={self.self_id!r} adapter={self.adapter.get_name()!r}>"

    @property
    def username(self) -> Optional[str]:
        """GitHub 上的真实用户名（登录后才有值）。"""

        return self.session.username

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
        raise NotImplementedError(
            "GitHub 用户账号适配器当前只提供出站调用能力，尚不支持会话回复（send）。"
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> "httpx.Response":
        """带会话 Cookie 发起请求，``url`` 支持站内相对路径。"""

        return await self.session.request(
            method, self.session.absolute_url(url), **kwargs
        )

    async def get_authenticated_user(self) -> Optional[str]:
        """重新校验登录态，并返回当前登录的 GitHub 用户名。"""

        if await self.session.verify_login():
            if self.session.username:
                self.self_id = self.session.username
            return self.session.username
        return None


class APIBot(BaseBot):
    """用 token 行动的 Bot（API 模式）：webhook 事件交给它处理，回复即评论。

    ``send`` 会根据事件里的仓库与编号自动选择合适的评论接口，
    因此插件里可以像平常一样 ``await matcher.send("...")``。
    """

    adapter: "Adapter"
    api: "GitHubAPI"

    @override
    def __init__(self, adapter: "Adapter", api: "GitHubAPI", self_id: str) -> None:
        super().__init__(adapter, self_id)
        self.api = api

    def __repr__(self) -> str:
        return f"<APIBot self_id={self.self_id!r} adapter={self.adapter.get_name()!r}>"

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
                "无法从该事件推断回复目标；send 只支持 issue / PR / commit 评论类 webhook 事件"
            )
        if target.kind == "commit":
            if not target.sha:
                raise ActionFailed("commit_comment 事件缺少 commit_id，无法回复")
            return await self.api.comment_commit(target.repo, target.sha, text)
        if target.number is None:
            raise ActionFailed("事件缺少 issue / PR 编号，无法回复")
        return await self.api.comment_issue(target.repo, target.number, text)

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


__all__ = ["APIBot", "Bot"]
