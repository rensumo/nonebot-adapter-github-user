"""NoneBot 集成冒烟测试：装了 nonebot2 的环境才会真正执行，否则自动跳过。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("nonebot")

if importlib.util.find_spec("nonebot.compat") is None:
    pytest.skip(
        "NoneBot 版本过低（缺少 nonebot.compat），本适配器要求 nonebot2>=2.2",
        allow_module_level=True,
    )

import nonebot  # noqa: E402
import nonebot.adapters  # noqa: E402
from nonebot.drivers import Request, URL  # noqa: E402

ADAPTER_PARENT = Path(__file__).resolve().parents[1] / "nonebot" / "adapters"

# NoneBot 的适配器是命名空间包，未通过 pip 安装时（例如直接跑源码）需要把适配器所在目录挂到 nonebot.adapters 的搜索路径上。
if str(ADAPTER_PARENT) not in nonebot.adapters.__path__:  # type: ignore[attr-defined]
    nonebot.adapters.__path__.append(str(ADAPTER_PARENT))  # type: ignore[attr-defined]


def _install_legacy_shims() -> None:
    """为老版本 NoneBot 补上适配器依赖的公共接口（适配器要求 nonebot2>=2.2）。"""

    if not hasattr(nonebot, "get_plugin_config"):

        def _get_plugin_config(model, *args, **kwargs):
            return model(**dict(nonebot.get_driver().config))

        nonebot.get_plugin_config = _get_plugin_config  # type: ignore[attr-defined]


def test_adapter_and_webhook_smoke(tmp_path):
    # 部分 NoneBot 版本在初始化时会创建 asyncio.Event()，因此需要先准备好事件循环。
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        nonebot.init(
            # 显式写法兼容各版本 NoneBot（"none" 简写在部分版本不可用）
            driver="nonebot.drivers.none:Driver",
            github_user_token="ghp_test",
            github_user_webhook_secret="wh-secret",
        )
        _install_legacy_shims()
        get_plugin_config = nonebot.get_plugin_config

        from nonebot.adapters.github_user import (
            Adapter,
            Bot,
            Config,
            GitHubAPI,
            Message,
            MessageSegment,
            webhook as webhook_mod,
        )

        config = get_plugin_config(Config)
        assert config.github_user_token == "ghp_test"
        assert config.api_enabled() is True
        assert config.oauth_scope_list() == ["repo", "workflow"]
        assert config.webhook_ready() is True
        assert config.webhook_event_filter() == []
        # 没配 token / client_id，token 文件也不存在时，API 模式不启用
        assert (
            Config(github_user_token_store=str(tmp_path / "nope.json")).api_enabled()
            is False
        )
        # 只有 token 文件也算启用（跑过 --oauth-login 后 env 什么都不用写）
        store = tmp_path / "token.json"
        store.write_text("{}", encoding="utf-8")
        assert (
            Config(github_user_token_store=str(store)).api_enabled() is True
        )

        driver = nonebot.get_driver()
        adapter = Adapter(driver)
        assert Adapter.get_name() == "GitHub-User"
        assert adapter.get_name() == "GitHub-User"

        manager = adapter.build_token_manager()
        assert manager is not None
        assert manager.static_token == "ghp_test"
        assert adapter.api is None  # startup 之前不建客户端

        assert str(Message("hello")) == "hello"
        segment = MessageSegment.markdown("**hi**")
        assert segment.is_text() is True
        assert str(segment) == "**hi**"
        assert Message([segment]) == Message(MessageSegment.markdown("**hi**"))

        bot = Bot(adapter, GitHubAPI(token="ghp_test"), self_id="rensumo")
        assert bot.self_id == "rensumo"
        assert "GitHub-User" in repr(bot)

        async def _run() -> None:
            adapter.bot_connect(bot)
            assert adapter.bots == {"rensumo": bot}

            # ---- webhook 投递：签名校验 + 事件解析 + 回复目标 ----
            recorded: list = []

            async def _fake_handle(event) -> None:
                recorded.append(event)

            bot.handle_event = _fake_handle  # type: ignore[method-assign]
            body = json.dumps(
                {
                    "action": "opened",
                    "number": 3,
                    "repository": {"full_name": "rensumo/nonebot-adapter-github-user"},
                    "sender": {"login": "alice"},
                    "pull_request": {"number": 3},
                }
            ).encode()
            headers = {
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-test-1",
                "X-Hub-Signature-256": webhook_mod.sign_payload("wh-secret", body),
            }
            request = Request(
                method="POST",
                url=URL("/github/webhook"),
                headers=headers,
                content=body,
            )
            response = await adapter._handle_webhook(request)
            assert response.status_code == 202
            await asyncio.sleep(0)  # 让 create_task 里的处理跑起来
            assert len(recorded) == 1
            assert recorded[0].get_event_name() == "pull_request.opened"
            assert recorded[0].repository == "rensumo/nonebot-adapter-github-user"
            assert recorded[0].sender == "alice"
            target = Bot.reply_target(recorded[0])
            assert target is not None and target.number == 3

            # 同一 delivery 重复投递会被忽略
            duplicate = await adapter._handle_webhook(request)
            assert duplicate.status_code == 202
            await asyncio.sleep(0)
            assert len(recorded) == 1

            # 签名不对 → 401
            bad = Request(
                method="POST",
                url=URL("/github/webhook"),
                headers={**headers, "X-Hub-Signature-256": "sha256=deadbeef"},
                content=body,
            )
            assert (await adapter._handle_webhook(bad)).status_code == 401

            # 缺事件头 → 400
            broken = Request(
                method="POST",
                url=URL("/github/webhook"),
                headers={
                    "X-Hub-Signature-256": webhook_mod.sign_payload("wh-secret", body)
                },
                content=body,
            )
            assert (await adapter._handle_webhook(broken)).status_code == 400

            # ping → 202
            ping_body = b"{}"
            ping = Request(
                method="POST",
                url=URL("/github/webhook"),
                headers={
                    "X-GitHub-Event": "ping",
                    "X-Hub-Signature-256": webhook_mod.sign_payload(
                        "wh-secret", ping_body
                    ),
                },
                content=ping_body,
            )
            assert (await adapter._handle_webhook(ping)).status_code == 202

            await adapter.shutdown()

        loop.run_until_complete(_run())
        assert adapter.bots == {}
    finally:
        loop.close()
        asyncio.set_event_loop(None)
