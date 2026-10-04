"""OAuth 设备流与 token 管理测试（httpx.MockTransport，不联网）。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
import types
from pathlib import Path
from typing import Any, Dict, List

import httpx

PKG_DIR = Path(__file__).resolve().parents[1] / "nonebot" / "adapters" / "github_user"


def _load_module(module_name: str, file_stem: str):
    spec = importlib.util.spec_from_file_location(
        module_name, PKG_DIR / f"{file_stem}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


oauth_mod = _load_module("github_user_oauth", "oauth")
OAuthDeviceFlow = oauth_mod.OAuthDeviceFlow
TokenManager = oauth_mod.TokenManager
TokenSet = oauth_mod.TokenSet
TokenStore = oauth_mod.TokenStore

DEVICE_CODE_PAYLOAD = {
    "device_code": "device-123",
    "user_code": "WDJB-MJHT",
    "verification_uri": "https://github.com/login/device",
    "expires_in": 900,
    "interval": 1,
}


def _mock_handler(
    token_responses: List[Dict[str, Any]],
    *,
    user: Dict[str, Any] | None = None,
    calls: List[httpx.Request] | None = None,
):
    queue = list(token_responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        path = request.url.path
        if path == "/login/device/code":
            return httpx.Response(200, json=DEVICE_CODE_PAYLOAD)
        if path == "/login/oauth/access_token":
            payload = queue.pop(0) if queue else {"error": "expired_token"}
            return httpx.Response(200, json=payload)
        if path == "/user":
            return httpx.Response(200, json=user or {"login": "bot-account"})
        return httpx.Response(404, json={"message": "not found"})

    return handler


def make_flow(token_responses: List[Dict[str, Any]], **kwargs: Any) -> Any:
    return OAuthDeviceFlow(
        "client-id",
        transport=httpx.MockTransport(_mock_handler(token_responses)),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 设备流                                                                       #
# --------------------------------------------------------------------------- #
def test_device_login_success():
    async def main() -> None:
        shown: List[str] = []
        flow = make_flow(
            [
                {"error": "authorization_pending"},
                {
                    "access_token": "gho_test",
                    "token_type": "bearer",
                    "scope": "repo,workflow",
                    "expires_in": 28800,
                    "refresh_token": "ghr_test",
                    "refresh_token_expires_in": 15811200,
                },
            ]
        )

        async def on_code(code) -> None:
            shown.append(code.user_code)

        tokens = await flow.login(on_code=on_code)
        await flow.aclose()

        assert shown == ["WDJB-MJHT"]
        assert tokens.access_token == "gho_test"
        assert tokens.refresh_token == "ghr_test"
        assert tokens.scopes == ["repo", "workflow"]
        assert tokens.expires_at is not None
        assert tokens.is_expired() is False
        assert tokens.login is None

    asyncio.run(main())


def test_device_login_records_login_name():
    async def main() -> None:
        flow = make_flow(
            [{"access_token": "gho_test", "scope": "repo"}],
        )
        tokens = await flow.login()
        tokens.login = await flow.fetch_login(tokens)
        await flow.aclose()
        assert tokens.login == "bot-account"

    asyncio.run(main())


def test_device_flow_access_denied():
    async def main() -> None:
        flow = make_flow([{"error": "access_denied"}])
        try:
            await flow.login()
        except oauth_mod.DeviceFlowDenied:
            pass
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 DeviceFlowDenied")
        finally:
            await flow.aclose()

    asyncio.run(main())


def test_device_flow_expired():
    async def main() -> None:
        flow = make_flow([{"error": "expired_token"}])
        try:
            await flow.login()
        except oauth_mod.DeviceFlowExpired:
            pass
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 DeviceFlowExpired")
        finally:
            await flow.aclose()

    asyncio.run(main())


def test_polling_interval_grows_on_slow_down(monkeypatch):
    async def main() -> None:
        slept: List[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        # 只替换模块里的 asyncio.sleep，避免影响其它测试
        monkeypatch.setattr(
            oauth_mod,
            "asyncio",
            types.SimpleNamespace(sleep=fake_sleep),
        )
        flow = make_flow(
            [
                {"error": "authorization_pending"},
                {"error": "slow_down"},
                {"access_token": "gho_test", "scope": "repo"},
            ]
        )
        tokens = await flow.login()
        assert tokens.access_token == "gho_test"
        assert slept == [1, 6]  # pending 用原间隔，slow_down 后 +5

    asyncio.run(main())


def test_refresh_keeps_previous_refresh_token():
    async def main() -> None:
        flow = make_flow([{"access_token": "gho_new", "expires_in": 28800}])
        old = TokenSet(
            access_token="gho_old",
            refresh_token="ghr_old",
            expires_at=time.time() - 10,
            refresh_expires_at=time.time() + 100000,
        )
        new = await flow.refresh(old)
        await flow.aclose()
        assert new.access_token == "gho_new"
        assert new.refresh_token == "ghr_old"
        assert new.refresh_expires_at == old.refresh_expires_at

    asyncio.run(main())


def test_refresh_without_refresh_token_raises():
    async def main() -> None:
        flow = make_flow([])
        try:
            await flow.refresh(TokenSet(access_token="gho_old"))
        except oauth_mod.TokenRefreshError:
            pass
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 TokenRefreshError")
        finally:
            await flow.aclose()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# token 存储与管理                                                             #
# --------------------------------------------------------------------------- #
def test_token_store_roundtrip(tmp_path):
    store = TokenStore(tmp_path / "token.json")
    assert store.load() is None
    tokens = TokenSet(
        access_token="gho_x",
        refresh_token="ghr_x",
        scopes=["repo"],
        expires_at=time.time() + 100,
        login="bot",
    )
    store.save(tokens)
    assert store.path.exists()
    data = json.loads(store.path.read_text("utf-8"))
    assert data["access_token"] == "gho_x"
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "gho_x"
    assert loaded.login == "bot"
    store.clear()
    assert store.load() is None


def test_token_manager_static_token():
    async def main() -> None:
        manager = TokenManager(static_token="pat_123")
        assert await manager.get_token() == "pat_123"
        await manager.aclose()

    asyncio.run(main())


def test_token_manager_without_token_raises(tmp_path):
    async def main() -> None:
        manager = TokenManager(client_id="cid", store=TokenStore(tmp_path / "none.json"))
        try:
            await manager.get_token()
        except oauth_mod.TokenUnavailable as exc:
            assert "--oauth-login" in str(exc)
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 TokenUnavailable")
        await manager.aclose()

    asyncio.run(main())


def test_token_manager_refreshes_expired_token(tmp_path):
    async def main() -> None:
        store = TokenStore(tmp_path / "token.json")
        store.save(
            TokenSet(
                access_token="gho_old",
                refresh_token="ghr_old",
                expires_at=time.time() - 5,
                refresh_expires_at=time.time() + 100000,
            )
        )
        manager = TokenManager(
            client_id="cid",
            store=store,
            transport=httpx.MockTransport(
                _mock_handler([{"access_token": "gho_new", "expires_in": 28800}])
            ),
        )
        assert await manager.get_token() == "gho_new"
        # 新 token 已经落盘
        assert store.load().access_token == "gho_new"  # type: ignore[union-attr]
        await manager.aclose()

    asyncio.run(main())


def test_token_manager_force_refresh(tmp_path):
    async def main() -> None:
        store = TokenStore(tmp_path / "token.json")
        store.save(
            TokenSet(
                access_token="gho_old",
                refresh_token="ghr_old",
                expires_at=time.time() + 100000,  # 还没过期
                refresh_expires_at=time.time() + 100000,
            )
        )
        manager = TokenManager(
            client_id="cid",
            store=store,
            transport=httpx.MockTransport(
                _mock_handler([{"access_token": "gho_forced", "expires_in": 28800}])
            ),
        )
        assert await manager.get_token() == "gho_old"
        assert await manager.get_token(force=True) == "gho_forced"
        await manager.aclose()

    asyncio.run(main())


def test_expired_without_refresh_raises(tmp_path):
    async def main() -> None:
        store = TokenStore(tmp_path / "token.json")
        store.save(TokenSet(access_token="gho_old", expires_at=time.time() - 5))
        manager = TokenManager(client_id="cid", store=store)
        try:
            await manager.get_token()
        except oauth_mod.TokenUnavailable as exc:
            assert "重新执行设备流登录" in str(exc)
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 TokenUnavailable")
        await manager.aclose()

    asyncio.run(main())


def test_client_id_required_for_flow():
    async def main() -> None:
        manager = TokenManager(store=TokenStore("nope.json"))
        try:
            manager.flow()
        except oauth_mod.TokenUnavailable as exc:
            assert "client_id" in str(exc)
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 TokenUnavailable")
        await manager.aclose()

    asyncio.run(main())


def test_resolve_client_id_alias():
    assert oauth_mod.resolve_client_id(None) is None
    assert oauth_mod.resolve_client_id("") is None
    assert oauth_mod.resolve_client_id("gh") == oauth_mod.GH_CLI_CLIENT_ID
    assert oauth_mod.resolve_client_id(" GH ") == oauth_mod.GH_CLI_CLIENT_ID
    assert oauth_mod.resolve_client_id("Iv1.abc") == "Iv1.abc"
