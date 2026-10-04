"""Adapter 实现：校验 token、注册 Bot、接收 GitHub webhook。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Optional

from typing_extensions import override

from nonebot import get_plugin_config
from nonebot.adapters import Adapter as BaseAdapter
from nonebot.drivers import ASGIMixin, HTTPServerSetup, Request, Response, URL

from .api import GitHubAPI, GitHubAPIError, set_default_api
from .bot import Bot
from .config import Config
from .event import WebhookEvent
from .exception import ActionFailed, NetworkError
from .log import log
from .oauth import (
    OAuthError,
    TokenManager,
    TokenStore,
    TokenUnavailable,
    resolve_client_id,
)
from .webhook import (
    SIGNATURE_HEADER,
    WebhookError,
    get_header,
    parse_webhook,
    verify_signature,
)

if TYPE_CHECKING:
    from nonebot.drivers import Driver


class Adapter(BaseAdapter):
    """GitHub 用户账号适配器。"""

    def __init__(self, driver: "Driver", **kwargs: Any) -> None:
        super().__init__(driver, **kwargs)
        self.github_user_config: Config = get_plugin_config(Config)
        self._api: Optional[GitHubAPI] = None
        self._token_manager: Optional[TokenManager] = None
        self._api_login: Optional[str] = None
        self._webhook_bot: Optional[Bot] = None
        self._deliveries: "OrderedDict[str, None]" = OrderedDict()
        self.setup()
        self.driver.on_startup(self.startup)
        self.driver.on_shutdown(self.shutdown)

    @classmethod
    @override
    def get_name(cls) -> str:
        return "GitHub-User"

    # ------------------------------------------------------------------ #
    # Webhook 入口                                                        #
    # ------------------------------------------------------------------ #
    def setup(self) -> None:
        """注册 HTTP webhook 路由（需要 ASGI 类型的 driver）。"""

        config = self.github_user_config
        if not config.github_user_webhook_path:
            return
        if not isinstance(self.driver, ASGIMixin):
            log(
                "WARNING",
                "当前 driver 不支持 HTTP 服务端，GitHub webhook 入口未启用；"
                "请使用 fastapi / aiohttp / quart 等 ASGI driver",
            )
            return
        if not config.webhook_ready():
            log(
                "WARNING",
                "配置了 GITHUB_USER_WEBHOOK_PATH 但没有 secret：入口已注册但会拒绝所有请求。"
                "请设置 GITHUB_USER_WEBHOOK_SECRET（或明确设置 GITHUB_USER_WEBHOOK_ALLOW_UNSIGNED=true）",
            )
        self.setup_http_server(
            HTTPServerSetup(
                URL(config.github_user_webhook_path),
                "POST",
                "GitHub Webhook",
                self._handle_webhook,
            )
        )
        log(
            "INFO",
            f"GitHub webhook 入口已注册：<y>POST {config.github_user_webhook_path}</y>",
        )

    async def _handle_webhook(self, request: Request) -> Response:
        """校验签名 → 解析事件 → 交给 Bot 处理。"""

        config = self.github_user_config
        body = request.content or b""
        if isinstance(body, str):
            body = body.encode("utf-8")

        secret = config.github_user_webhook_secret
        signature = get_header(request.headers, SIGNATURE_HEADER)
        if not secret:
            if not config.github_user_webhook_allow_unsigned:
                log("ERROR", "收到 webhook 但未配置 GITHUB_USER_WEBHOOK_SECRET，已拒绝")
                return Response(status_code=503, content="webhook secret 未配置")
        elif not verify_signature(secret, body, signature):
            log("WARNING", "webhook 签名校验失败，已拒绝")
            return Response(status_code=401, content="invalid signature")

        try:
            payload = parse_webhook(request.headers, body)
        except WebhookError as exc:
            log("WARNING", f"webhook 解析失败：{exc}")
            return Response(status_code=400, content=str(exc))

        if payload.event == "ping":
            log("INFO", f"收到 webhook ping（repo={payload.repository or '-'}）")
            return Response(status_code=202, content="pong")

        if payload.delivery_id and self._seen_delivery(payload.delivery_id):
            log("DEBUG", f"重复投递，已忽略：{payload.delivery_id}")
            return Response(status_code=202, content="duplicate")

        allowed = config.webhook_event_filter()
        if allowed and payload.event not in allowed:
            log("DEBUG", f"事件 {payload.event} 不在白名单内，已忽略")
            return Response(status_code=202, content="ignored")

        bot = self._webhook_bot or next(iter(self.bots.values()), None)
        if bot is None:
            log(
                "WARNING",
                "收到 webhook 但没有可用的 Bot：配置 GITHUB_USER_TOKEN 或 "
                "GITHUB_USER_OAUTH_CLIENT_ID 后重启即可处理事件",
            )
            return Response(status_code=202, content="no bot")

        event = WebhookEvent(
            event=payload.event,
            action=payload.action,
            delivery_id=payload.delivery_id,
            repository=payload.repository,
            sender=payload.sender,
            payload=payload.data,
        )
        log(
            "INFO",
            f"收到 webhook <y>{event.get_event_name()}</y> "
            f"repo={payload.repository or '-'} sender={payload.sender or '-'}",
        )
        task = asyncio.create_task(bot.handle_event(event))
        task.add_done_callback(self._log_task_error)
        return Response(status_code=202, content="accepted")

    def _seen_delivery(self, delivery_id: str, keep: int = 500) -> bool:
        """投递去重：近期见过的返回 True。"""

        if delivery_id in self._deliveries:
            return True
        self._deliveries[delivery_id] = None
        while len(self._deliveries) > keep:
            self._deliveries.popitem(last=False)
        return False

    @staticmethod
    def _log_task_error(task: "asyncio.Task[Any]") -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log("ERROR", f"处理 webhook 事件时出错：{error!r}")

    # ------------------------------------------------------------------ #
    # 生命周期                                                            #
    # ------------------------------------------------------------------ #
    async def startup(self) -> None:
        await self.setup_api()
        if self._api is None:
            log(
                "WARNING",
                "未配置 GitHub token，适配器不会建立 Bot。请设置 GITHUB_USER_TOKEN，"
                "或设置 GITHUB_USER_OAUTH_CLIENT_ID 后执行 "
                "python -m nonebot.adapters.github_user --oauth-login",
            )
            return
        self._register_webhook_bot()

    async def shutdown(self) -> None:
        # 断开所有已连接的 Bot（基类会用 self.bots 记录它们）
        for bot in list(self.bots.values()):
            try:
                self.bot_disconnect(bot)
            except Exception as exc:  # noqa: BLE001 - 退出流程不应因此崩溃
                log("WARNING", f"断开 Bot {bot.self_id} 时出错：{exc}")
        self._webhook_bot = None
        self._deliveries.clear()
        set_default_api(None)
        if self._api is not None:
            await self._api.aclose()
            self._api = None
        if self._token_manager is not None:
            await self._token_manager.aclose()
            self._token_manager = None

    # ------------------------------------------------------------------ #
    # API 模式（PAT / gh auth token / OAuth 设备流）                        #
    # ------------------------------------------------------------------ #
    @property
    def api(self) -> Optional[GitHubAPI]:
        """API 客户端；配置了 token 或 client_id 时可用。"""

        return self._api

    @property
    def token_manager(self) -> Optional[TokenManager]:
        return self._token_manager

    def _register_webhook_bot(self) -> Optional[Bot]:
        """API 模式下建一个 Bot：webhook 事件交给它，回复即评论。"""

        if self._api is None:
            return None
        config = self.github_user_config
        self_id = config.github_user_webhook_self_id or self._api_login
        if not self_id:
            self_id = "github-user"
        bot = Bot(self, self._api, self_id=self_id)
        self._webhook_bot = bot
        self.bot_connect(bot)
        log("INFO", f"Webhook Bot 已注册：<y>{bot.self_id}</y>")
        return bot

    def build_token_manager(self) -> Optional[TokenManager]:
        """按配置构造 token 管理器（不联网）。"""

        config = self.github_user_config
        if not config.api_enabled():
            return None
        return TokenManager(
            client_id=resolve_client_id(config.github_user_oauth_client_id),
            client_secret=config.github_user_oauth_client_secret,
            scopes=config.oauth_scope_list(),
            static_token=config.github_user_token,
            store=TokenStore(config.github_user_token_store),
            api_base_url=config.github_user_api_base_url,
            timeout=config.github_user_timeout,
            proxy=config.github_user_proxy,
        )

    async def setup_api(self) -> Optional[GitHubAPI]:
        """构造 API 客户端并校验 token。"""

        manager = self.build_token_manager()
        if manager is None:
            return None
        config = self.github_user_config
        self._token_manager = manager

        async def provider(force: bool = False) -> str:
            return await manager.get_token(force=force)

        api = GitHubAPI(
            token_provider=provider,
            api_base_url=config.github_user_api_base_url,
            timeout=config.github_user_timeout,
            proxy=config.github_user_proxy,
        )
        self._api = api
        set_default_api(api)
        try:
            user = await api.get_authenticated_user()
        except Exception as exc:  # noqa: BLE001 - 未授权时只提示，不阻断启动
            log(
                "WARNING",
                f"GitHub API token 不可用：{exc}；"
                "可执行 python -m nonebot.adapters.github_user --oauth-login 重新授权",
            )
            return api
        login = user.get("login") if isinstance(user, dict) else None
        self._api_login = login
        log(
            "INFO",
            f"GitHub API 已就绪，当前身份 <y>{login or '(未知)'}</y>"
            f"（{config.github_user_api_base_url}）",
        )
        return api

    # ------------------------------------------------------------------ #
    # API 调用                                                            #
    # ------------------------------------------------------------------ #
    @override
    async def _call_api(self, bot: Bot, api: str, **data: Any) -> Any:
        """``bot.call_api(...)`` 的落地实现。

        ``api`` 直接当 REST 路径用（如 ``/repos/owner/repo/pulls/1``），
        其余关键字参数透传给 :meth:`GitHubAPI.request`。
        """

        log("DEBUG", f"Calling API <y>{api}</y>")
        try:
            return await bot.api.request(str(data.pop("method", "GET")), api, **data)
        except GitHubAPIError as exc:
            if exc.status_code == 0:
                raise NetworkError(str(exc)) from exc
            raise ActionFailed(str(exc)) from exc
        except (TokenUnavailable, OAuthError) as exc:
            raise NetworkError(str(exc)) from exc


__all__ = ["Adapter"]
