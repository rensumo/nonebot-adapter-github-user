"""GitHub 网页会话登录流程（表单登录 + TOTP 双因素）。

本模块刻意不依赖 NoneBot，可以单独导入、单独测试：

.. code-block:: python

    session = GitHubSession("bot@example.com", "password", totp_secret="ABCDEF...")
    result = await session.login()
    user = result.username

流程说明（与 GitHub 当前网页端行为一致）：

1. ``GET /login`` 取回 ``authenticity_token`` 与匿名会话 Cookie；
2. ``POST /session`` 提交 ``login`` / ``password``；
3. 若账号开启了双因素，GitHub 会 ``302`` 到 ``/sessions/two-factor``，此时解析页面上的验证码输入框（``app_otp`` / ``otp``）并提交 TOTP 验证码；
4. 若 GitHub 还要求设备验证（``/sessions/verified-device``，验证码发到邮箱），则抛出 :class:`DeviceVerificationRequired`，除非调用方提供了取码回调；
5. ``GET /`` 校验登录态，并解析出真实用户名；
6. 会话 Cookie 常驻内存，可选落盘（``cookie_store``）以便重启后免密登录。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import struct
import time
from dataclasses import dataclass, field
from html import unescape as _unescape_html
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
    Union,
)
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

log = logging.getLogger("nonebot.adapters.github_user")

DEFAULT_BASE_URL = "https://github.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8"
)
DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"

MAX_LOGIN_STEPS = 6
"""登录状态机最多跳转的步数，避免异常重定向造成死循环。"""

RETRY_DELAYS: Tuple[float, ...] = (1.0, 3.0, 7.0)
"""登录过程遇到网络错误时的退避重试间隔（秒）。"""

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_USER_AGENT",
    "AccountRestricted",
    "AuthenticationFailed",
    "CSRFError",
    "CaptchaRequired",
    "CodeProvider",
    "DeviceVerificationRequired",
    "GitHubSession",
    "GitHubUserSessionError",
    "LoginResult",
    "NetworkError",
    "RateLimited",
    "SessionExpired",
    "TwoFactorRejected",
    "TwoFactorRequired",
    "generate_totp",
    "parse_totp_secret",
]


# --------------------------------------------------------------------------- #
# 异常                                                                         #
# --------------------------------------------------------------------------- #
class GitHubUserSessionError(Exception):
    """GitHub 会话相关错误的基类。"""


class NetworkError(GitHubUserSessionError):
    """网络层错误（超时、连接失败等）。"""


class AuthenticationFailed(GitHubUserSessionError):
    """账号或密码被 GitHub 拒绝。"""


class CSRFError(GitHubUserSessionError):
    """表单 ``authenticity_token`` 缺失或失效。"""


class TwoFactorRequired(GitHubUserSessionError):
    """需要双因素验证，但没有可用的验证码来源。"""


class TwoFactorRejected(GitHubUserSessionError):
    """双因素验证码被 GitHub 拒绝。"""


class DeviceVerificationRequired(GitHubUserSessionError):
    """GitHub 要求设备验证（邮件验证码），自动化流程无法继续。"""


class CaptchaRequired(GitHubUserSessionError):
    """GitHub 要求完成人机验证。"""


class AccountRestricted(GitHubUserSessionError):
    """账号被 GitHub 限制或标记。"""


class RateLimited(GitHubUserSessionError):
    """触发 GitHub 登录频率限制。"""


class SessionExpired(GitHubUserSessionError):
    """已建立的会话失效，需要重新登录。"""


# --------------------------------------------------------------------------- #
# TOTP                                                                        #
# --------------------------------------------------------------------------- #
_TOTP_PADDING_RE = re.compile(r"[\s\-]")
_ALGORITHMS = ("sha1", "sha256", "sha512")


def parse_totp_secret(secret: str) -> str:
    """把 TOTP 密钥归一化成不带填充的 base32 字符串。

    同时接受 ``otpauth://totp/...?secret=XXXX`` 形式的 URI、小写字母以及中间带空格/连字符的密钥（GitHub 展示密钥时常常四位一组）。
    """

    text = (secret or "").strip()
    if not text:
        raise ValueError("TOTP 密钥为空")
    if text.lower().startswith("otpauth://"):
        values = parse_qs(urlparse(text).query).get("secret") or []
        if not values:
            raise ValueError("otpauth URI 中缺少 secret 参数")
        text = values[0]
    text = _TOTP_PADDING_RE.sub("", text).upper().rstrip("=")
    if not text:
        raise ValueError("TOTP 密钥为空")
    return text


def generate_totp(
    secret: str,
    at: Optional[float] = None,
    *,
    digits: int = 6,
    period: int = 30,
    algorithm: str = "sha1",
    window: int = 0,
) -> str:
    """按 RFC 6238 生成 TOTP 验证码。

    :param secret: base32 密钥或 ``otpauth://`` URI
    :param at: 计算时刻（Unix 时间戳），默认当前时间
    :param digits: 验证码位数，GitHub 固定 6 位
    :param period: 时间窗口（秒），GitHub 固定 30 秒
    :param algorithm: 摘要算法，GitHub 使用 sha1
    :param window: 相对当前窗口的偏移，``1`` 表示下一个 30 秒
    """

    name = (algorithm or "sha1").lower()
    if name not in _ALGORITHMS:
        raise ValueError(f"不支持的 TOTP 摘要算法：{algorithm}")
    if not 6 <= digits <= 10:
        raise ValueError("TOTP 位数必须在 6 到 10 之间")

    raw = parse_totp_secret(secret)
    padded = raw + "=" * (-len(raw) % 8)
    try:
        key = base64.b32decode(padded, casefold=True)
    except Exception as exc:  # noqa: BLE001 - binascii.Error / ValueError
        raise ValueError("TOTP 密钥不是合法的 base32 字符串") from exc

    timestamp = time.time() if at is None else at
    counter = int(timestamp // period) + int(window)
    digest = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, name)).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def _seconds_to_next_window(period: int = 30) -> float:
    """距离下一个 TOTP 时间窗口的秒数（多留 0.5 秒避免边界抖动）。"""

    return period - (time.time() % period) + 0.5


CodeProvider = Callable[[], Union[str, Awaitable[str]]]
"""验证码来源：可以是同步函数，也可以是协程函数。"""


async def _maybe_await(value: Union[str, Awaitable[str]]) -> str:
    if inspect.isawaitable(value):
        return await value
    return str(value)


@dataclass
class LoginResult:
    """一次成功登录的结果快照。"""

    login: str
    """提交给 GitHub 的登录名（用户名或邮箱）。"""

    username: Optional[str] = None
    """GitHub 上的真实用户名（登录后才能解析出来）。"""

    two_factor: bool = False
    """本次登录是否走了双因素验证流程。"""

    device_verified: bool = False
    """本次登录是否额外完成了设备（邮件）验证。"""

    from_cookie: bool = False
    """是否是直接复用缓存 Cookie 登录的。"""

    cookies: Dict[str, str] = field(default_factory=dict)
    """登录后的 Cookie 快照（含凭据，注意不要写入日志）。"""


class GitHubSession:
    """维护一个 GitHub 账号的网页会话。

    该类负责登录、Cookie 复用、会话失效检测与重新登录；所有方法都是协程，并且可以被多个调用方并发调用（内部用锁串行化登录动作）。
    """

    def __init__(
        self,
        login: str,
        password: str,
        *,
        totp_secret: Optional[str] = None,
        totp_provider: Optional[CodeProvider] = None,
        device_otp_provider: Optional[CodeProvider] = None,
        label: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        proxy: Optional[str] = None,
        cookies: Optional[Mapping[str, str]] = None,
        cookie_store: Optional[Union[str, os.PathLike]] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        client: Optional[httpx.AsyncClient] = None,
        max_retries: int = 2,
        totp_attempts: int = 2,
        verify_ssl: bool = True,
    ) -> None:
        # 注意：属性名不能叫 self.login，否则会覆盖同名方法 login()。
        self.login_name = login
        self.password = password
        self.totp_secret = totp_secret
        self.totp_provider = totp_provider
        self.device_otp_provider = device_otp_provider
        self.label = label or login
        self.base_url = base_url.rstrip("/") or DEFAULT_BASE_URL
        self.user_agent = user_agent
        self.timeout = float(timeout)
        self.proxy = proxy
        self.cookie_store = Path(cookie_store) if cookie_store else None
        self.transport = transport
        self.max_retries = max(0, int(max_retries))
        self.totp_attempts = max(1, int(totp_attempts))
        self.verify_ssl = verify_ssl

        self.username: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = client
        self._initial_cookies: Dict[str, str] = dict(cookies or {})
        self._authenticated = False
        self._two_factor_used = False
        self._device_verified = False
        self._lock: Optional[asyncio.Lock] = None

    # ------------------------------------------------------------------ #
    # 基础属性                                                            #
    # ------------------------------------------------------------------ #
    def __repr__(self) -> str:
        state = "authenticated" if self._authenticated else "anonymous"
        return f"<GitHubSession {self.label!r} {state}>"

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("会话尚未初始化，请先 await session.login()")
        return self._client

    # ------------------------------------------------------------------ #
    # 客户端与底层请求                                                     #
    # ------------------------------------------------------------------ #
    def _loop_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def absolute_url(self, path: str) -> str:
        """把站内路径（如 ``/notifications``）补全成完整 URL。"""

        if path.startswith(("http://", "https://")):
            return path
        return self._url(path)

    async def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client

        cookies = httpx.Cookies()
        domain = urlparse(self.base_url).hostname or ""
        for name, value in self._initial_cookies.items():
            self._set_cookie(cookies, name, value, domain)

        kwargs: Dict[str, Any] = {
            "headers": {
                "User-Agent": self.user_agent,
                "Accept": DEFAULT_ACCEPT,
                "Accept-Language": DEFAULT_ACCEPT_LANGUAGE,
            },
            # 表单登录需要精确观察 302 的 Location，因此默认不自动跟随重定向，需要跟随的地方在调用点显式打开。
            "follow_redirects": False,
            "timeout": httpx.Timeout(self.timeout),
            "cookies": cookies,
            "verify": self.verify_ssl,
        }
        if self.transport is not None:
            kwargs["transport"] = self.transport
        if self.proxy:
            kwargs["proxy"] = self.proxy
        try:
            self._client = httpx.AsyncClient(**kwargs)
        except TypeError:
            # httpx < 0.26 使用 proxies=，这里做一次兼容回退。
            if "proxy" not in kwargs:
                raise
            kwargs["proxies"] = kwargs.pop("proxy")
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    @staticmethod
    def _set_cookie(
        jar: httpx.Cookies, name: str, value: str, domain: str, path: str = "/"
    ) -> None:
        if domain:
            jar.set(name, value, domain=domain, path=path)
        else:
            jar.set(name, value, path=path)

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        client = await self._client_or_create()
        headers = dict(kwargs.pop("headers", None) or {})
        kwargs["headers"] = headers
        kwargs.setdefault("follow_redirects", True)
        try:
            return await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise NetworkError(
                f"请求 GitHub 失败：{type(exc).__name__}: {exc}"
            ) from exc

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("follow_redirects", True)
        return await self._send("GET", url, **kwargs)

    async def _post(
        self, url: str, data: Mapping[str, str], referer: str, **kwargs: Any
    ) -> httpx.Response:
        headers = {
            "Referer": referer,
            "Origin": self.base_url,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        headers.update(kwargs.pop("headers", None) or {})
        kwargs.setdefault("follow_redirects", False)
        return await self._send("POST", url, data=dict(data), headers=headers, **kwargs)

    async def aclose(self) -> None:
        """关闭底层连接池。"""

        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._authenticated = False

    close = aclose

    # ------------------------------------------------------------------ #
    # HTML 解析小工具                                                     #
    # ------------------------------------------------------------------ #
    _AUTH_TOKEN_RE = re.compile(
        r'name="authenticity_token"[^>]*?value="([^"]+)"', re.IGNORECASE
    )
    _AUTH_TOKEN_JSON_RE = re.compile(
        r'"authenticity_token"\s*:\s*"([^"]+)"'
    )
    _FORM_ACTION_RE = re.compile(r"<form[^>]*?action=\"([^\"]*)\"", re.IGNORECASE)
    _OTP_INPUT_RE = re.compile(
        r'name="(app_otp|sms_otp|otp|recovery_code)"', re.IGNORECASE
    )
    _FLASH_ALERT_RE = re.compile(
        r'class="js-flash-alert"[^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL
    )
    _FLASH_ERROR_RE = re.compile(
        r'class="[^"]*flash-error[^"]*"[^>]*>(.*?)</div>\s*</div>',
        re.IGNORECASE | re.DOTALL,
    )
    _TAG_RE = re.compile(r"<[^>]+>")

    @classmethod
    def _parse_authenticity(cls, html: str) -> Optional[str]:
        match = cls._AUTH_TOKEN_RE.search(html) or cls._AUTH_TOKEN_JSON_RE.search(html)
        return match.group(1) if match else None

    @classmethod
    def _form_action(cls, html: str) -> Optional[str]:
        match = cls._FORM_ACTION_RE.search(html)
        return match.group(1) if match else None

    @classmethod
    def _detect_otp_field(cls, html: str) -> Optional[str]:
        found = {m.group(1).lower() for m in cls._OTP_INPUT_RE.finditer(html)}
        for candidate in ("app_otp", "sms_otp", "otp", "recovery_code"):
            if candidate in found:
                return candidate
        return None

    @classmethod
    def _extract_flash_errors(cls, html: str) -> List[str]:
        errors: List[str] = []
        for regex in (cls._FLASH_ALERT_RE, cls._FLASH_ERROR_RE):
            for match in regex.finditer(html):
                text = _unescape_html(cls._TAG_RE.sub(" ", match.group(1)))
                text = " ".join(text.split())
                if text and text not in errors:
                    errors.append(text)
            if errors:
                break
        return errors

    @classmethod
    def _extract_username(cls, html: str) -> Optional[str]:
        patterns = (
            r'<meta name="user-login" content="([^"]+)"',
            r'"current-user-login"\s*:\s*"([^"]+)"',
            r'data-login="([^"]+)"',
        )
        for pattern in patterns:
            match = re.search(pattern, html)
            if match and match.group(1):
                return match.group(1)
        return None

    @staticmethod
    def _looks_like_login_page(html: str) -> bool:
        lowered = html.lower()
        return 'action="/session"' in lowered and 'name="login"' in lowered

    @classmethod
    def _looks_like_two_factor_page(cls, html: str) -> bool:
        return cls._detect_otp_field(html) is not None

    @staticmethod
    def _detect_captcha(html: str) -> bool:
        lowered = html.lower()
        return "octocaptcha" in lowered or "captcha-container" in lowered

    @staticmethod
    def _detect_rate_limit(html: str) -> bool:
        lowered = html.lower()
        return (
            "whoa there" in lowered
            or "abuse detection" in lowered
            or "rate limit" in lowered
        )

    @staticmethod
    def _detect_restricted(html: str) -> bool:
        lowered = html.lower()
        return (
            "account has been flagged" in lowered
            or "account is suspended" in lowered
            or "your account has been" in lowered
        )

    def _error_from_page(self, html: str) -> GitHubUserSessionError:
        errors = self._extract_flash_errors(html)
        message = errors[0] if errors else None
        if self._detect_captcha(html):
            return CaptchaRequired(
                message or "GitHub 要求完成人机验证（CAPTCHA），自动化登录已被拦截"
            )
        if self._detect_rate_limit(html):
            return RateLimited(message or "触发 GitHub 登录频率限制（Whoa there!）")
        if self._detect_restricted(html):
            return AccountRestricted(message or "账号被 GitHub 限制，无法完成登录")
        return AuthenticationFailed(message or "GitHub 拒绝了本次登录请求（用户名或密码可能不正确）")

    # ------------------------------------------------------------------ #
    # Cookie                                                              #
    # ------------------------------------------------------------------ #
    def cookie_dict(self) -> Dict[str, str]:
        if self._client is None:
            return dict(self._initial_cookies)
        return {c.name: c.value for c in self._client.cookies.jar}

    def _cookie_value(self, name: str) -> Optional[str]:
        if self._client is None:
            return self._initial_cookies.get(name)
        return self._client.cookies.get(name)

    async def _restore_cookie_store(self) -> bool:
        if self.cookie_store is None or not self.cookie_store.exists():
            return False
        try:
            data = json.loads(self.cookie_store.read_text("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 缓存损坏不应中断登录
            log.warning("读取会话缓存失败，将改为重新登录：%s", exc)
            return False
        if not isinstance(data, dict):
            return False
        if data.get("login") != self.login_name:
            log.warning("会话缓存属于其他账号，忽略：%s", self.cookie_store)
            return False
        cookies = data.get("cookies")
        if not isinstance(cookies, dict) or not cookies:
            return False

        client = await self._client_or_create()
        domain = urlparse(self.base_url).hostname or ""
        for name, value in cookies.items():
            self._set_cookie(client.cookies, str(name), str(value), domain)
        if await self.verify_login():
            log.info("复用会话缓存登录成功：%s", self.label)
            self.username = self.username or data.get("username")
            return True
        log.info("会话缓存已失效，将使用账号密码重新登录：%s", self.label)
        return False

    def _save_cookie_store(self) -> None:
        if self.cookie_store is None:
            return
        payload = {
            "login": self.login_name,
            "username": self.username,
            "saved_at": time.time(),
            "cookies": self.cookie_dict(),
        }
        try:
            self.cookie_store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cookie_store.with_suffix(self.cookie_store.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.cookie_store)
        except OSError as exc:
            log.warning("写入会话缓存失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 登录流程                                                            #
    # ------------------------------------------------------------------ #
    async def login(
        self, *, force: bool = False, use_cookie_store: bool = True
    ) -> LoginResult:
        """执行登录，失败时抛出 :class:`GitHubUserSessionError` 子类。"""

        async with self._loop_lock():
            if not force:
                if self._authenticated and await self.verify_login():
                    return self._build_result(from_cookie=False)
                if use_cookie_store and await self._restore_cookie_store():
                    return self._build_result(from_cookie=True)

            error: Optional[GitHubUserSessionError] = None
            for attempt in range(self.max_retries + 1):
                try:
                    return await self._login_once()
                except NetworkError as exc:
                    error = exc
                    if attempt >= self.max_retries:
                        break
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    log.warning("登录过程中网络异常，%.0f 秒后重试：%s", delay, exc)
                    await asyncio.sleep(delay)
            assert error is not None
            raise error

    async def _login_once(self) -> LoginResult:
        self._two_factor_used = False
        self._device_verified = False

        token, page_url = await self._fetch_login_form()
        if token is None:
            if await self.verify_login():
                return self._build_result(from_cookie=True)
            raise CSRFError(
                "无法从 GitHub 登录页解析 authenticity_token，页面结构可能变化或请求被风控拦截"
            )

        response = await self._submit_credentials(token, page_url)
        if response.status_code == 422:
            # 令牌过期会让 GitHub 直接返回 422，重新取一次令牌再试。
            log.warning("登录表单令牌失效（HTTP 422），重新获取令牌后重试一次")
            await asyncio.sleep(0.5)
            token, page_url = await self._fetch_login_form()
            if token is None:
                raise CSRFError("重新获取登录表单令牌失败")
            response = await self._submit_credentials(token, page_url)
            if response.status_code == 422:
                raise CSRFError("GitHub 连续拒绝了登录表单令牌（HTTP 422）")

        for _ in range(MAX_LOGIN_STEPS):
            location = self._redirect_location(response)
            if location is None:
                if response.status_code == 200:
                    # 登录成功必然是 302，200 只可能是错误页（密码错误 / 验证码 / 风控）。
                    raise self._error_from_page(response.text)
                if response.status_code == 429:
                    raise RateLimited("GitHub 返回 HTTP 429，登录请求过于频繁")
                if response.status_code >= 400:
                    raise AuthenticationFailed(
                        f"登录失败：GitHub 返回 HTTP {response.status_code}"
                    )
                break

            path = urlparse(location).path
            if path.startswith("/sessions/two-factor"):
                response = await self._submit_two_factor(location)
                self._two_factor_used = True
                continue
            if path.startswith("/sessions/verified-device"):
                response = await self._submit_device_verification(location)
                self._device_verified = True
                continue
            if path == "/login" or path.startswith("/login/"):
                page = await self._get(location)
                raise self._error_from_page(page.text)
            break
        else:
            raise AuthenticationFailed("登录重定向次数过多，流程已中止")

        return await self._finalize()

    async def _submit_credentials(self, token: str, referer: str) -> httpx.Response:
        return await self._post(
            self._url("/session"),
            {
                "commit": "Sign in",
                "authenticity_token": token,
                "login": self.login_name,
                "password": self.password,
                "webauthn-conditional": "undefined",
            },
            referer=referer,
        )

    async def _fetch_login_form(self) -> Tuple[Optional[str], str]:
        response = await self._get(self._url("/login"))
        return self._parse_authenticity(response.text), str(response.url)

    async def _submit_two_factor(self, url: str) -> httpx.Response:
        page = await self._get(url)
        html = page.text
        field_name = self._detect_otp_field(html)
        if field_name is None:
            raise TwoFactorRequired(
                "GitHub 要求双因素验证，但页面上没有可填写的验证码输入框"
            )
        action = urljoin(str(page.url), self._form_action(html) or str(page.url))

        attempts = self.totp_attempts if self._code_available() else 1
        last: Optional[httpx.Response] = None
        for index in range(attempts):
            if index:
                await asyncio.sleep(_seconds_to_next_window())
                refreshed = await self._get(action)
                html = refreshed.text
            token = self._parse_authenticity(html)
            if token is None:
                raise CSRFError("双因素验证页面缺少 authenticity_token")
            code = await self._obtain_code(field_name, window=index)
            last = await self._post(
                action,
                {
                    "authenticity_token": token,
                    field_name: code,
                    "commit": "Verify",
                },
                referer=str(page.url),
            )
            if self._redirect_location(last) is None and (
                last.status_code == 200 and self._looks_like_two_factor_page(last.text)
            ):
                log.warning("双因素验证码被拒绝（第 %d 次尝试）", index + 1)
                continue
            return last

        message = (
            self._extract_flash_errors(last.text)[:1] if last is not None else []
        )
        raise TwoFactorRejected(
            message[0] if message else "双因素验证码被 GitHub 拒绝"
        )

    async def _submit_device_verification(self, url: str) -> httpx.Response:
        page = await self._get(url)
        html = page.text
        field_name = self._detect_otp_field(html) or "otp"
        if self.device_otp_provider is None:
            raise DeviceVerificationRequired(
                "GitHub 要求设备验证：验证码会发送到账号绑定邮箱。"
                "请先在常用设备上完成一次登录，或接入邮箱收信后通过 device_otp_provider 提供验证码"
            )
        token = self._parse_authenticity(html)
        if token is None:
            raise CSRFError("设备验证页面缺少 authenticity_token")
        action = urljoin(str(page.url), self._form_action(html) or str(page.url))
        code = str(await _maybe_await(self.device_otp_provider())).strip()
        if not code:
            raise DeviceVerificationRequired("邮箱验证码为空，无法完成设备验证")
        return await self._post(
            action,
            {"authenticity_token": token, field_name: code},
            referer=str(page.url),
        )

    def _code_available(self) -> bool:
        return bool(self.totp_secret) or self.totp_provider is not None

    async def _obtain_code(self, field_name: str, *, window: int = 0) -> str:
        if self.totp_secret:
            code = generate_totp(self.totp_secret, window=window)
        elif self.totp_provider is not None:
            code = str(await _maybe_await(self.totp_provider())).strip()
        else:
            raise TwoFactorRequired(
                "GitHub 要求双因素验证（字段：%s），但既没有配置 TOTP 密钥，也没有提供验证码回调"
                % field_name
            )
        if not code:
            raise TwoFactorRequired("双因素验证码为空")
        return code

    async def _finalize(self) -> LoginResult:
        if not await self.verify_login():
            raise AuthenticationFailed("登录流程结束后仍未通过登录态校验")
        self._save_cookie_store()
        return self._build_result(from_cookie=False)

    def _build_result(self, *, from_cookie: bool) -> LoginResult:
        return LoginResult(
            login=self.login_name,
            username=self.username,
            two_factor=self._two_factor_used,
            device_verified=self._device_verified,
            from_cookie=from_cookie,
            cookies=self.cookie_dict(),
        )

    @staticmethod
    def _redirect_location(response: httpx.Response) -> Optional[str]:
        if response.status_code not in (301, 302, 303, 307, 308):
            return None
        location = response.headers.get("location")
        if not location:
            return None
        return urljoin(str(response.request.url), location)

    # ------------------------------------------------------------------ #
    # 会话校验与请求                                                       #
    # ------------------------------------------------------------------ #
    async def verify_login(self) -> bool:
        """访问站点首页确认登录态，并尽量解析出真实用户名。"""

        response = await self._get(self._url("/"))
        if self._looks_like_login_page(response.text):
            self._authenticated = False
            return False
        username = self._extract_username(response.text)
        if username:
            self.username = username
        self._authenticated = bool(username) or self._cookie_value("logged_in") == "yes"
        return self._authenticated

    async def ensure_authenticated(self) -> None:
        """确保当前处于登录态，必要时自动重新登录。"""

        if self._authenticated:
            return
        await self.login()

    async def request(
        self, method: str, url: str, *, retry_on_expired: bool = True, **kwargs: Any
    ) -> httpx.Response:
        """带上会话 Cookie 发起请求；会话失效时自动重新登录一次。"""

        attempts = 2 if retry_on_expired else 1
        for attempt in range(attempts):
            await self.ensure_authenticated()
            response = await self._send(method, url, **kwargs)
            if not self._is_login_redirect(response):
                return response
            self._authenticated = False
            if attempt + 1 >= attempts:
                break
            log.warning("会话已失效，正在重新登录：%s", self.label)
            await self.login(force=True)
        raise SessionExpired(f"GitHub 会话已失效且重新登录后仍被重定向到登录页：{url}")

    @staticmethod
    def _is_login_redirect(response: httpx.Response) -> bool:
        urls = [str(hop.url) for hop in response.history]
        urls.append(str(response.url))
        return any(urlparse(item).path in ("/login", "/session") for item in urls)
