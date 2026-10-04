"""OAuth 设备流（Device Authorization Grant）与 token 管理。

设备流专为没有浏览器的场景设计：程序拿一个 ``user_code`` 让用户在浏览器里输入，
之后轮询换取 access token。配合 GitHub 的「expiring tokens」，可以拿到
access token（8 小时）+ refresh token（6 个月不用才失效），实现无人值守续期。

.. code-block:: python

    flow = OAuthDeviceFlow(client_id="xxxx")
    tokens = await flow.login(on_code=lambda code: print("去", code.uri, "输入", code.user_code))
    print(tokens.access_token)

本模块不依赖 NoneBot，可以单独使用。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Union,
)

import httpx

log = logging.getLogger("nonebot.adapters.github_user")

DEFAULT_OAUTH_BASE_URL = "https://github.com"
DEFAULT_API_BASE_URL = "https://api.github.com"
DEFAULT_SCOPES = ("repo", "workflow")
DEVICE_CODE_URL = "/login/device/code"
ACCESS_TOKEN_URL = "/login/oauth/access_token"

GH_CLI_CLIENT_ID = "178c6fc778ccc68e1d6a"
"""GitHub CLI 官方 OAuth App 的公开 client_id。

它不是密钥（gh 自己的源码注释也说可以进版本库），但它代表的是 **GitHub CLI
这个应用的身份**：用户授权列表里会显示 "GitHub CLI"，你无法单独撤销或改名，
而且那是 GitHub 的应用，对方随时可以收紧。个人自用图省事可以填它，
发布给别人用建议自己注册一个 OAuth App。
"""

__all__ = [
    "DEFAULT_API_BASE_URL",
    "DEFAULT_OAUTH_BASE_URL",
    "DEFAULT_SCOPES",
    "DeviceCode",
    "DeviceFlowDenied",
    "DeviceFlowExpired",
    "OAuthDeviceFlow",
    "OAuthError",
    "GH_CLI_CLIENT_ID",
    "TokenManager",
    "TokenSet",
    "TokenStore",
    "TokenUnavailable",
    "resolve_client_id",
]


def resolve_client_id(value: Optional[str]) -> Optional[str]:
    """把配置里的 client_id 归一化：填 ``gh`` 就用 GitHub CLI 的公开 client_id。"""

    if not value:
        return None
    text = value.strip()
    if text.lower() == "gh":
        return GH_CLI_CLIENT_ID
    return text or None


# --------------------------------------------------------------------------- #
# 异常                                                                         #
# --------------------------------------------------------------------------- #
class OAuthError(Exception):
    """OAuth / token 相关错误基类。"""


class DeviceFlowDenied(OAuthError):
    """用户在授权页面上点了拒绝。"""


class DeviceFlowExpired(OAuthError):
    """设备码过期（默认 15 分钟）。"""


class TokenUnavailable(OAuthError):
    """本地没有可用 token，需要先跑一次设备流登录。"""


class TokenRefreshError(OAuthError):
    """刷新 token 失败（通常要重新走一次设备流）。"""


# --------------------------------------------------------------------------- #
# 数据结构                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class DeviceCode:
    """``POST /login/device/code`` 的返回。"""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int = 900
    interval: int = 5


@dataclass
class TokenSet:
    """一份 access token（可能带 refresh token 与过期时间）。"""

    access_token: str
    token_type: str = "bearer"
    scopes: List[str] = field(default_factory=list)
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None
    refresh_expires_at: Optional[float] = None
    login: Optional[str] = None
    client_id: Optional[str] = None

    @property
    def expires_in(self) -> Optional[float]:
        if self.expires_at is None:
            return None
        return self.expires_at - time.time()

    def is_expired(self, skew: float = 60.0) -> bool:
        """提前 ``skew`` 秒算过期，避免请求正卡在边界上。"""

        if self.expires_at is None:
            return False
        return time.time() + skew >= self.expires_at

    def refresh_expired(self, skew: float = 60.0) -> bool:
        if self.refresh_expires_at is None:
            return False
        return time.time() + skew >= self.refresh_expires_at

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TokenSet":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------- #
# token 落盘                                                                   #
# --------------------------------------------------------------------------- #
class TokenStore:
    """把 token 存成 0600 的 JSON 文件。"""

    def __init__(self, path: Union[str, os.PathLike]) -> None:
        self.path = Path(path)

    def load(self) -> Optional[TokenSet]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("读取 token 缓存失败：%s", exc)
            return None
        if not isinstance(data, dict) or not data.get("access_token"):
            return None
        return TokenSet.from_dict(data)

    def save(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(tokens.to_dict(), ensure_ascii=False), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- #
# 设备流                                                                       #
# --------------------------------------------------------------------------- #
CodeHandler = Callable[[DeviceCode], Union[None, Awaitable[None]]]


async def _maybe_await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


class OAuthDeviceFlow:
    """实现 GitHub 的 OAuth 2.0 Device Authorization Grant。"""

    def __init__(
        self,
        client_id: str,
        *,
        client_secret: Optional[str] = None,
        scopes: Iterable[str] = DEFAULT_SCOPES,
        base_url: str = DEFAULT_OAUTH_BASE_URL,
        api_base_url: str = DEFAULT_API_BASE_URL,
        timeout: float = 30.0,
        proxy: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = list(scopes)
        self.base_url = base_url.rstrip("/")
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = timeout
        self.proxy = proxy
        self.transport = transport
        self._client = client

    def __repr__(self) -> str:
        return f"<OAuthDeviceFlow client_id={self.client_id!r} scopes={self.scopes}>"

    async def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: Dict[str, Any] = {
                "timeout": httpx.Timeout(self.timeout),
                "headers": {"Accept": "application/json"},
            }
            if self.transport is not None:
                kwargs["transport"] = self.transport
            if self.proxy:
                kwargs["proxy"] = self.proxy
            try:
                self._client = httpx.AsyncClient(**kwargs)
            except TypeError:
                if "proxy" not in kwargs:
                    raise
                kwargs["proxies"] = kwargs.pop("proxy")
                self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, url: str, data: Dict[str, str]) -> Dict[str, Any]:
        client = await self._client_or_create()
        try:
            response = await client.post(
                url, data=data, headers={"Accept": "application/json"}
            )
        except httpx.HTTPError as exc:
            raise OAuthError(f"请求 {url} 失败：{type(exc).__name__}: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise OAuthError(
                f"{url} 返回了非 JSON 响应（HTTP {response.status_code}）"
            ) from exc
        if not isinstance(payload, dict):
            raise OAuthError(f"{url} 返回了意外的数据：{payload!r}")
        return payload

    # ------------------------------------------------------------------ #
    # 设备流三步                                                          #
    # ------------------------------------------------------------------ #
    async def request_device_code(self) -> DeviceCode:
        """第一步：申请设备码与用户码。"""

        payload = await self._post(
            f"{self.base_url}{DEVICE_CODE_URL}",
            {
                "client_id": self.client_id,
                "scope": " ".join(self.scopes),
            },
        )
        if "device_code" not in payload:
            raise OAuthError(f"申请设备码失败：{payload}")
        return DeviceCode(
            device_code=str(payload["device_code"]),
            user_code=str(payload.get("user_code", "")),
            verification_uri=str(
                payload.get("verification_uri") or f"{self.base_url}/login/device"
            ),
            expires_in=int(payload.get("expires_in", 900)),
            interval=int(payload.get("interval", 5)),
        )

    async def poll_for_token(
        self,
        device: DeviceCode,
        *,
        on_pending: Optional[Callable[[], Union[None, Awaitable[None]]]] = None,
    ) -> TokenSet:
        """第二步：轮询直到用户完成授权。"""

        interval = max(1, device.interval)
        deadline = time.monotonic() + max(30, device.expires_in)
        client = await self._client_or_create()

        while True:
            payload = await self._post(
                f"{self.base_url}{ACCESS_TOKEN_URL}",
                {
                    "client_id": self.client_id,
                    "device_code": device.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            error = payload.get("error")
            if not error:
                return self._token_from_payload(payload)
            if error == "authorization_pending":
                if on_pending is not None:
                    await _maybe_await(on_pending())
            elif error == "slow_down":
                interval += 5
            elif error == "access_denied":
                raise DeviceFlowDenied("用户拒绝了本次授权")
            elif error in ("expired_token", "incorrect_device_code"):
                raise DeviceFlowExpired("设备码已过期或被拒绝，请重新发起设备流")
            else:
                raise OAuthError(
                    f"设备流换取 token 失败：{payload.get('error_description') or error}"
                )

            if time.monotonic() >= deadline:
                raise DeviceFlowExpired(
                    f"等待授权超过 {device.expires_in} 秒仍未完成（user_code={device.user_code}）"
                )
            await asyncio.sleep(interval)
            if client.is_closed:  # pragma: no cover - 防御性
                raise OAuthError("HTTP 客户端已关闭")

    async def refresh(self, tokens: TokenSet) -> TokenSet:
        """用 refresh token 换新的 access token。"""

        if not tokens.refresh_token:
            raise TokenRefreshError("没有 refresh token，需要重新走一次设备流")
        data = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        payload = await self._post(f"{self.base_url}{ACCESS_TOKEN_URL}", data)
        if payload.get("error"):
            raise TokenRefreshError(
                f"刷新 token 失败：{payload.get('error_description') or payload['error']}"
            )
        refreshed = self._token_from_payload(payload)
        # GitHub 在 refresh 响应里不一定返回新的 refresh token，
        # 这时继续沿用旧的（文档明确要求这样处理）。
        if refreshed.refresh_token is None:
            refreshed.refresh_token = tokens.refresh_token
            refreshed.refresh_expires_at = tokens.refresh_expires_at
        refreshed.login = refreshed.login or tokens.login
        return refreshed

    async def login(
        self,
        *,
        on_code: Optional[CodeHandler] = None,
        on_pending: Optional[Callable[[], Union[None, Awaitable[None]]]] = None,
    ) -> TokenSet:
        """一步到位：申请设备码 → 提示用户 → 轮询换取 token。"""

        device = await self.request_device_code()
        if on_code is not None:
            await _maybe_await(on_code(device))
        return await self.poll_for_token(device, on_pending=on_pending)

    async def fetch_login(self, tokens: TokenSet) -> Optional[str]:
        """用 token 调 ``GET /user``，确认身份并拿到登录名。"""

        client = await self._client_or_create()
        try:
            response = await client.get(
                f"{self.api_base_url}/user",
                headers={
                    "Authorization": f"Bearer {tokens.access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
        except httpx.HTTPError as exc:
            raise OAuthError(f"校验 token 失败：{exc}") from exc
        if response.status_code != 200:
            return None
        payload = response.json()
        return payload.get("login") if isinstance(payload, dict) else None

    def _token_from_payload(self, payload: Dict[str, Any]) -> TokenSet:
        scopes = str(payload.get("scope", "")).split(",")
        now = time.time()
        expires_in = payload.get("expires_in")
        refresh_expires_in = payload.get("refresh_token_expires_in")
        return TokenSet(
            access_token=str(payload.get("access_token", "")),
            token_type=str(payload.get("token_type", "bearer")),
            scopes=[s for s in (item.strip() for item in scopes) if s],
            refresh_token=payload.get("refresh_token"),
            expires_at=(now + float(expires_in)) if expires_in else None,
            refresh_expires_at=(
                now + float(refresh_expires_in) if refresh_expires_in else None
            ),
            client_id=self.client_id,
        )


# --------------------------------------------------------------------------- #
# token 管理（静态 token / 落盘 / 自动刷新）                                     #
# --------------------------------------------------------------------------- #
class TokenManager:
    """对外只暴露 :meth:`get_token`，内部负责刷新与落盘。"""

    def __init__(
        self,
        *,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        scopes: Iterable[str] = DEFAULT_SCOPES,
        static_token: Optional[str] = None,
        store: Optional[TokenStore] = None,
        base_url: str = DEFAULT_OAUTH_BASE_URL,
        api_base_url: str = DEFAULT_API_BASE_URL,
        timeout: float = 30.0,
        proxy: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.static_token = static_token or None
        self.store = store
        self._tokens: Optional[TokenSet] = self._load_tokens()
        self._flow: Optional[OAuthDeviceFlow] = None
        self._lock = asyncio.Lock()
        self._flow_kwargs: Dict[str, Any] = {
            "client_secret": client_secret,
            "scopes": list(scopes),
            "base_url": base_url,
            "api_base_url": api_base_url,
            "timeout": timeout,
            "proxy": proxy,
            "transport": transport,
            "client": client,
        }
        self.client_id = client_id

    def _load_tokens(self) -> Optional[TokenSet]:
        if self.store is None:
            return None
        return self.store.load()

    @property
    def tokens(self) -> Optional[TokenSet]:
        return self._tokens

    def flow(self) -> OAuthDeviceFlow:
        if self._flow is None:
            if not self.client_id:
                raise TokenUnavailable(
                    "没有配置 client_id，无法发起设备流；"
                    "请设置 GITHUB_USER_OAUTH_CLIENT_ID（或改用 GITHUB_USER_TOKEN）"
                )
            self._flow = OAuthDeviceFlow(self.client_id, **self._flow_kwargs)
        return self._flow

    async def aclose(self) -> None:
        if self._flow is not None:
            await self._flow.aclose()
            self._flow = None

    async def get_token(
        self, *, refresh_if_needed: bool = True, force: bool = False
    ) -> str:
        """拿一个可用的 access token。

        静态 token 直接返回；否则读缓存，快过期就用 refresh token 换新的。
        ``force=True`` 用于服务端返回 401 时强制刷新一次。
        没有可用 token 时抛 :class:`TokenUnavailable`（提示先登录一次）。
        """

        if self.static_token:
            return self.static_token
        async with self._lock:
            tokens = self._tokens
            if tokens is None:
                raise TokenUnavailable(
                    "本地没有 GitHub token，请先执行一次 OAuth 设备流登录"
                    "（python -m nonebot.adapters.github_user --oauth-login）"
                )
            need_refresh = force or (refresh_if_needed and tokens.is_expired())
            if not need_refresh:
                return tokens.access_token
            if not tokens.refresh_token or tokens.refresh_expired():
                if force:
                    # 强制刷新但没得刷：把旧 token 交回去，让上层如实报 401
                    return tokens.access_token
                raise TokenUnavailable(
                    "token 已过期且无法刷新（refresh token 缺失或已过期），请重新执行设备流登录"
                )
            refreshed = await self.flow().refresh(tokens)
            self._tokens = refreshed
            if self.store is not None:
                self.store.save(refreshed)
            log.info("已用 refresh token 续期，新的 access token 有效期至 %s", refreshed.expires_at)
            return refreshed.access_token

    async def login(
        self,
        *,
        on_code: Optional[CodeHandler] = None,
        on_pending: Optional[Callable[[], Union[None, Awaitable[None]]]] = None,
    ) -> TokenSet:
        """跑一次完整的设备流并把 token 落盘。"""

        flow = self.flow()
        tokens = await flow.login(on_code=on_code, on_pending=on_pending)
        tokens.login = await flow.fetch_login(tokens)
        self._tokens = tokens
        if self.store is not None:
            self.store.save(tokens)
        return tokens
