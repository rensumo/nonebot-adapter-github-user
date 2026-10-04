"""Adapter 实现：启动时登录并注册 Bot，调用 API 时复用会话 Cookie。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import urljoin

from typing_extensions import override

from nonebot import get_plugin_config
from nonebot.adapters import Adapter as BaseAdapter

from .bot import Bot
from .config import Config, GitHubUserAccount
from .api import GitHubAPI, set_default_api
from .exception import (
    ActionFailed,
    DeviceVerificationRequired,
    GitHubUserAdapterException,
    NetworkError,
)
from .log import log
from .mail import build_mailbox, make_device_otp_provider
from .oauth import TokenManager, TokenStore, resolve_client_id
from .session import (
    DEFAULT_USER_AGENT,
    CaptchaRequired,
    GitHubSession,
    GitHubUserSessionError,
    NetworkError as SessionNetworkError,
    SessionExpired,
)

if TYPE_CHECKING:
    from nonebot.drivers import Driver

    import httpx


class Adapter(BaseAdapter):
    """GitHub 用户账号适配器。"""

    def __init__(self, driver: "Driver", **kwargs: Any) -> None:
        super().__init__(driver, **kwargs)
        self.github_user_config: Config = get_plugin_config(Config)
        self._sessions: Dict[str, GitHubSession] = {}
        self._api: Optional[GitHubAPI] = None
        self._token_manager: Optional[TokenManager] = None
        self.driver.on_startup(self.startup)
        self.driver.on_shutdown(self.shutdown)

    @classmethod
    @override
    def get_name(cls) -> str:
        return "GitHub-User"

    # ------------------------------------------------------------------ #
    # 生命周期                                                            #
    # ------------------------------------------------------------------ #
    async def startup(self) -> None:
        await self.setup_api()
        accounts = self.github_user_config.account_list()
        if not accounts:
            if self._api is None:
                log(
                    "WARNING",
                    "未配置 GitHub 账号，适配器不会建立任何会话。请设置 "
                    "GITHUB_USER_LOGIN/GITHUB_USER_PASSWORD/GITHUB_USER_ACCOUNTS（网页会话），"
                    "或 GITHUB_USER_TOKEN / GITHUB_USER_OAUTH_CLIENT_ID（API 模式）",
                )
            return

        results = await asyncio.gather(
            *(self.connect_account(account) for account in accounts),
            return_exceptions=True,
        )
        for index, account in enumerate(accounts):
            result = results[index]
            if isinstance(result, BaseException):
                log(
                    "ERROR",
                    f"账号 <y>{account.display}</y> 登录失败：{result}",
                )

    async def shutdown(self) -> None:
        # 断开所有已连接的 Bot（基类会用 self.bots 记录它们）
        for bot in list(self.bots.values()):
            try:
                self.bot_disconnect(bot)
            except Exception as exc:  # noqa: BLE001 - 退出流程不应因此崩溃
                log("WARNING", f"断开 Bot {bot.self_id} 时出错：{exc}")
        for session in list(self._sessions.values()):
            await session.aclose()
        self._sessions.clear()
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
        log(
            "INFO",
            f"GitHub API 已就绪，当前身份 <y>{login or '(未知)'}</y>"
            f"（{config.github_user_api_base_url}）",
        )
        return api

    # ------------------------------------------------------------------ #
    # 账号连接                                                            #
    # ------------------------------------------------------------------ #
    def _build_session(self, account: GitHubUserAccount) -> GitHubSession:
        config = self.github_user_config
        device_otp_provider = None
        mail = config.mail_config()
        if mail is not None:
            device_otp_provider = make_device_otp_provider(
                build_mailbox(mail),
                timeout=config.github_user_mail_poll_timeout,
                interval=config.github_user_mail_poll_interval,
            )
        return GitHubSession(
            account.login,
            account.password,
            totp_secret=account.totp_secret,
            device_otp_provider=device_otp_provider,
            label=account.display,
            base_url=config.github_user_base_url,
            user_agent=config.github_user_user_agent or DEFAULT_USER_AGENT,
            timeout=config.github_user_timeout,
            proxy=account.proxy or config.github_user_proxy,
            cookie_store=account.cookie_store,
            max_retries=config.github_user_max_retries,
            totp_attempts=config.github_user_totp_attempts,
        )

    async def connect_account(self, account: GitHubUserAccount) -> Bot:
        """登录指定账号并把 Bot 注册进 NoneBot。"""

        session = self._build_session(account)
        retries = max(1, self.github_user_config.github_user_login_retries)
        last_error: Optional[BaseException] = None
        for attempt in range(retries):
            try:
                result = await session.login()
            except SessionNetworkError as exc:
                last_error = exc
                if attempt + 1 >= retries:
                    break
                delay = self.github_user_config.github_user_login_backoff * (attempt + 1)
                log(
                    "WARNING",
                    f"账号 <y>{account.display}</y> 登录网络异常，{delay:.0f} 秒后重试：{exc}",
                )
                await asyncio.sleep(delay)
                continue
            except GitHubUserSessionError:
                await session.aclose()
                raise

            bot = Bot(self, session, self_id=result.username or account.login)
            self._sessions[account.login] = session
            self.bot_connect(bot)
            log(
                "INFO",
                f"账号 <y>{account.display}</y> 登录成功，"
                f"用户名 <y>{result.username or '未知'}</y>，"
                f"双因素：{'是' if result.two_factor else '否'}，"
                f"会话复用：{'是' if result.from_cookie else '否'}",
            )
            return bot

        await session.aclose()
        if last_error is not None:
            raise last_error
        raise NetworkError(f"账号 {account.display} 登录失败")

    # ------------------------------------------------------------------ #
    # API 调用                                                            #
    # ------------------------------------------------------------------ #
    @override
    async def _call_api(self, bot: Bot, api: str, **data: Any) -> Any:
        log("DEBUG", f"Calling API <y>{api}</y>")
        method: str = str(data.pop("method", "GET")).upper()
        raw: bool = bool(data.pop("raw", False))
        if api.startswith(("http://", "https://")):
            url = api
        elif api.startswith("/"):
            url = bot.session.absolute_url(api)
        else:
            url = urljoin("https://api.github.com/", api)
        data.setdefault("follow_redirects", True)

        try:
            response = await bot.session.request(method, url, **data)
        except SessionNetworkError as exc:
            raise NetworkError(str(exc)) from exc
        except SessionExpired as exc:
            raise NetworkError(str(exc)) from exc
        except GitHubUserSessionError as exc:
            raise GitHubUserAdapterException(str(exc)) from exc

        if raw:
            return response
        if response.status_code >= 400:
            raise ActionFailed(
                f"GitHub 返回 HTTP {response.status_code}：{response.text[:200]}"
            )
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            try:
                return response.json()
            except ValueError:
                return response.text
        return response.text

    # ------------------------------------------------------------------ #
    # 便捷方法                                                            #
    # ------------------------------------------------------------------ #
    def get_session(self, name: str) -> Optional[GitHubSession]:
        """按登录名或配置中的 ``label`` 取回会话对象。"""

        session = self._sessions.get(name)
        if session is not None:
            return session
        for candidate in self._sessions.values():
            if candidate.label == name:
                return candidate
        return None

    @property
    def sessions(self) -> List[GitHubSession]:
        return list(self._sessions.values())

    async def fetch(
        self, bot: Bot, method: str, url: str, **kwargs: Any
    ) -> "httpx.Response":
        """直接拿到原始响应对象（不解析 JSON，非 2xx 也不抛错）。"""

        return await bot.session.request(method, bot.session.absolute_url(url), **kwargs)


__all__ = ["Adapter", "CaptchaRequired", "DeviceVerificationRequired"]
