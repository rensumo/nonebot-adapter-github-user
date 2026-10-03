"""登录流程单元测试。

测试只依赖 httpx（MockTransport），不联网、不需要 NoneBot，直接跑 ``python -m pytest tests -q`` 即可。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs

import httpx

PKG_DIR = Path(__file__).resolve().parents[1] / "nonebot" / "adapters" / "github_user"


def _load_session_module():
    spec = importlib.util.spec_from_file_location(
        "github_user_session", PKG_DIR / "session.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


session_mod = _load_session_module()
GitHubSession = session_mod.GitHubSession

TOTP_SECRET = "JBSWY3DPEHPK3PXP"
ERROR_TEMPLATE = (
    '<div class="flash flash-error"><div class="js-flash-alert">{}</div></div>'
)


def _parse_cookies(request: httpx.Request) -> Dict[str, str]:
    jar: Dict[str, str] = {}
    for chunk in request.headers.get("cookie", "").split(";"):
        if "=" in chunk:
            name, _, value = chunk.strip().partition("=")
            jar[name] = value
    return jar


class FakeGitHub:
    """一个够用的 GitHub 登录端点模拟实现。"""

    def __init__(
        self,
        *,
        login: str = "bot@example.com",
        password: str = "secret-password",
        username: str = "bot-account",
        totp_secret: Optional[str] = None,
        require_device_verification: bool = False,
        device_code: str = "123456",
    ) -> None:
        self.login = login
        self.password = password
        self.username = username
        self.totp_secret = totp_secret
        self.require_device_verification = require_device_verification
        self.device_code = device_code

        self.sessions: set = set()
        self.session_seq = 0
        self.token_seq = 0
        self.login_posts: List[Dict[str, List[str]]] = []
        self.two_factor_posts: List[Dict[str, List[str]]] = []
        self.device_posts: List[Dict[str, List[str]]] = []
        self.reject_session_posts = 0
        self.reject_next_two_factor = False
        self.captcha = False
        self.rate_limited = False

    # ------------------------------------------------------------------ #
    def new_token(self) -> str:
        self.token_seq += 1
        return f"csrf-{self.token_seq}"

    def _two_factor_form(self, action: str) -> str:
        token = self.new_token()
        return (
            f'<form action="{action}" method="post">'
            f'<input type="hidden" name="authenticity_token" value="{token}" />'
            '<input type="text" name="app_otp" />'
            "</form>"
        )

    def _device_form(self, action: str) -> str:
        token = self.new_token()
        return (
            f'<form action="{action}" method="post">'
            f'<input type="hidden" name="authenticity_token" value="{token}" />'
            '<input type="text" name="otp" />'
            "</form>"
        )

    def _login_ok(self) -> httpx.Response:
        self.session_seq += 1
        cookie = f"sess-{self.session_seq}"
        self.sessions.add(cookie)
        return httpx.Response(
            302,
            headers={
                "location": "/",
                "set-cookie": f"user_session={cookie}; Path=/; HttpOnly",
            },
        )

    # ------------------------------------------------------------------ #
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        cookies = _parse_cookies(request)
        form: Dict[str, List[str]] = {}
        if request.method in ("POST", "PUT"):
            form = parse_qs(request.content.decode("utf-8"))
        signed_in = cookies.get("user_session") in self.sessions

        if request.method == "GET" and path == "/login":
            if signed_in:
                return httpx.Response(302, headers={"location": "/"})
            token = self.new_token()
            html = (
                '<form action="/session" method="post">'
                f'<input type="hidden" name="authenticity_token" value="{token}" />'
                '<input type="text" name="login" />'
                '<input type="password" name="password" />'
                "</form>"
            )
            return httpx.Response(
                200, html=html, headers={"set-cookie": "_gh_sess=anon; Path=/"}
            )

        if request.method == "POST" and path == "/session":
            self.login_posts.append(form)
            if self.reject_session_posts > 0:
                self.reject_session_posts -= 1
                return httpx.Response(422, html="<html>Invalid authenticity token</html>")
            if self.captcha:
                return httpx.Response(
                    200,
                    html="<html><div id='captcha-container'>"
                    "<iframe src='https://octocaptcha.com/'></iframe></div></html>",
                )
            if self.rate_limited:
                return httpx.Response(
                    200, html="<html><h1>Whoa there!</h1>rate limit</html>"
                )
            if form.get("login") != [self.login] or form.get("password") != [
                self.password
            ]:
                return httpx.Response(
                    200, html=ERROR_TEMPLATE.format("Incorrect username or password.")
                )
            if self.totp_secret:
                return httpx.Response(
                    302, headers={"location": "/sessions/two-factor"}
                )
            if self.require_device_verification:
                return httpx.Response(
                    302, headers={"location": "/sessions/verified-device"}
                )
            return self._login_ok()

        if path.startswith("/sessions/two-factor"):
            if request.method == "GET":
                return httpx.Response(200, html=self._two_factor_form(path))
            self.two_factor_posts.append(form)
            code = (form.get("app_otp") or [""])[0]
            expected = {
                session_mod.generate_totp(self.totp_secret, window=window)
                for window in (-1, 0, 1)
            }
            if self.reject_next_two_factor or code not in expected:
                self.reject_next_two_factor = False
                html = self._two_factor_form(path) + ERROR_TEMPLATE.format(
                    "Two-factor authentication failed."
                )
                return httpx.Response(200, html=html)
            if self.require_device_verification:
                return httpx.Response(
                    302, headers={"location": "/sessions/verified-device"}
                )
            return self._login_ok()

        if path.startswith("/sessions/verified-device"):
            if request.method == "GET":
                return httpx.Response(200, html=self._device_form(path))
            self.device_posts.append(form)
            if (form.get("otp") or [""])[0] != self.device_code:
                html = self._device_form(path) + ERROR_TEMPLATE.format(
                    "The verification code is incorrect."
                )
                return httpx.Response(200, html=html)
            return self._login_ok()

        if request.method == "GET" and path == "/":
            if not signed_in:
                token = self.new_token()
                html = (
                    '<title>Sign in to GitHub</title>'
                    '<form action="/session" method="post">'
                    f'<input type="hidden" name="authenticity_token" value="{token}" />'
                    '<input type="text" name="login" />'
                    "</form>"
                )
                return httpx.Response(200, html=html)
            html = (
                f'<meta name="user-login" content="{self.username}" />'
                "<html>dashboard</html>"
            )
            return httpx.Response(
                200, html=html, headers={"set-cookie": "logged_in=yes; Path=/"}
            )

        if request.method == "GET" and path == "/notifications":
            if not signed_in:
                return httpx.Response(302, headers={"location": "/login"})
            return httpx.Response(200, json={"notifications": []})

        return httpx.Response(404, html="<html>not found</html>")


def make_session(server: FakeGitHub, **kwargs: Any) -> Any:
    options: Dict[str, Any] = {
        "transport": httpx.MockTransport(server.handle),
        "max_retries": 0,
    }
    options.update(kwargs)
    return GitHubSession(server.login, server.password, **options)


# --------------------------------------------------------------------------- #
# TOTP                                                                        #
# --------------------------------------------------------------------------- #
def test_parse_totp_secret_variants():
    assert session_mod.parse_totp_secret("jbsw y3dp ehpk 3pxp") == TOTP_SECRET
    assert (
        session_mod.parse_totp_secret(
            f"otpauth://totp/GitHub:bot?secret={TOTP_SECRET}&issuer=GitHub"
        )
        == TOTP_SECRET
    )


def test_generate_totp_rfc6238_vector():
    # RFC 6238 附录 B：密钥 "12345678901234567890" 在 T=59 时的 SHA1 结果
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert session_mod.generate_totp(secret, 59, digits=8) == "94287082"
    assert session_mod.generate_totp(secret, 59, digits=6) == "287082"
    assert (
        session_mod.generate_totp(secret, 59, digits=6, window=1)
        == session_mod.generate_totp(secret, 89, digits=6)
    )


# --------------------------------------------------------------------------- #
# 登录                                                                         #
# --------------------------------------------------------------------------- #
def test_login_without_two_factor():
    async def main() -> None:
        server = FakeGitHub()
        session = make_session(server)
        result = await session.login()
        assert result.username == "bot-account"
        assert result.two_factor is False
        assert session.authenticated is True
        assert len(server.login_posts) == 1
        await session.aclose()

    asyncio.run(main())


def test_login_with_two_factor():
    async def main() -> None:
        server = FakeGitHub(totp_secret=TOTP_SECRET)
        session = make_session(server, totp_secret=TOTP_SECRET)
        result = await session.login()
        assert result.two_factor is True
        assert result.username == "bot-account"
        assert len(server.two_factor_posts) == 1
        await session.aclose()

    asyncio.run(main())


def test_login_wrong_password_raises():
    async def main() -> None:
        server = FakeGitHub(password="expected")
        session = make_session(server)
        session.password = "wrong"
        try:
            await session.login()
        except session_mod.AuthenticationFailed as exc:
            assert "Incorrect username or password" in str(exc)
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 AuthenticationFailed")
        await session.aclose()

    asyncio.run(main())


def test_two_factor_without_secret_raises():
    async def main() -> None:
        server = FakeGitHub(totp_secret=TOTP_SECRET)
        session = make_session(server)
        try:
            await session.login()
        except session_mod.TwoFactorRequired as exc:
            assert "app_otp" in str(exc)
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 TwoFactorRequired")
        await session.aclose()

    asyncio.run(main())


def test_two_factor_retries_next_window(monkeypatch):
    async def main() -> None:
        monkeypatch.setattr(session_mod, "_seconds_to_next_window", lambda period=30: 0.01)
        server = FakeGitHub(totp_secret=TOTP_SECRET)
        server.reject_next_two_factor = True
        session = make_session(server, totp_secret=TOTP_SECRET)
        result = await session.login()
        assert result.two_factor is True
        assert len(server.two_factor_posts) == 2
        await session.aclose()

    asyncio.run(main())


def test_csrf_token_refresh_retry():
    async def main() -> None:
        server = FakeGitHub()
        server.reject_session_posts = 1
        session = make_session(server)
        result = await session.login()
        assert result.username == "bot-account"
        # 第一次 422 之后会重新取令牌，因此一共提交了两次
        assert len(server.login_posts) == 2
        await session.aclose()

    asyncio.run(main())


def test_device_verification_required():
    async def main() -> None:
        server = FakeGitHub(require_device_verification=True)
        session = make_session(server)
        try:
            await session.login()
        except session_mod.DeviceVerificationRequired as exc:
            assert "设备验证" in str(exc)
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 DeviceVerificationRequired")
        await session.aclose()

    asyncio.run(main())


def test_device_verification_with_provider():
    async def main() -> None:
        server = FakeGitHub(require_device_verification=True, device_code="654321")
        session = make_session(server, device_otp_provider=lambda: "654321")
        result = await session.login()
        assert result.device_verified is True
        assert len(server.device_posts) == 1
        await session.aclose()

    asyncio.run(main())


def test_captcha_and_rate_limit_are_detected():
    async def main() -> None:
        server = FakeGitHub()
        server.captcha = True
        session = make_session(server)
        try:
            await session.login()
        except session_mod.CaptchaRequired:
            pass
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 CaptchaRequired")
        await session.aclose()

        server2 = FakeGitHub()
        server2.rate_limited = True
        session2 = make_session(server2)
        try:
            await session2.login()
        except session_mod.RateLimited:
            pass
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 RateLimited")
        await session2.aclose()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# Cookie 缓存与失效重登                                                        #
# --------------------------------------------------------------------------- #
def test_cookie_store_reuse(tmp_path):
    async def main() -> None:
        store = tmp_path / "cookies.json"
        server = FakeGitHub()
        first = make_session(server, cookie_store=str(store))
        await first.login()
        await first.aclose()
        assert store.exists()

        # 密码已经改变：只有复用 Cookie 才能登录成功
        server.password = "changed-password"
        posts_before = len(server.login_posts)
        second = make_session(server, cookie_store=str(store))
        result = await second.login()
        assert result.from_cookie is True
        assert len(server.login_posts) == posts_before
        await second.aclose()

    asyncio.run(main())


def test_request_relogins_after_session_expired():
    async def main() -> None:
        server = FakeGitHub()
        session = make_session(server)
        await session.login()

        server.sessions.clear()  # 模拟服务端会话过期
        response = await session.request("GET", "https://github.com/notifications")
        assert response.status_code == 200
        assert len(server.login_posts) == 2
        await session.aclose()

    asyncio.run(main())


def test_request_raises_when_relogin_fails():
    async def main() -> None:
        server = FakeGitHub()
        session = make_session(server)
        await session.login()
        server.sessions.clear()
        server.password = "changed-password"
        try:
            await session.request("GET", "https://github.com/notifications")
        except (session_mod.SessionExpired, session_mod.AuthenticationFailed):
            pass
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出会话失效相关异常")
        await session.aclose()

    asyncio.run(main())
