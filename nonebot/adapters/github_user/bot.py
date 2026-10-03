"""Bot 实现：持有某个 GitHub 账号的网页会话。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Union

from typing_extensions import override

from nonebot.adapters import Bot as BaseBot
from nonebot.message import handle_event

from .event import Event
from .message import Message, MessageSegment
from .session import GitHubSession

if TYPE_CHECKING:
    import httpx

    from .adapter import Adapter


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


__all__ = ["Bot"]
