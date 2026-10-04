"""NoneBot 集成冒烟测试：装了 nonebot2 的环境才会真正执行，否则自动跳过。

本地（未安装 NoneBot 的机器）执行时只会看到 skipped，不影响登录流程测试。
"""

from __future__ import annotations

import asyncio
import importlib.util
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

ADAPTER_PARENT = Path(__file__).resolve().parents[1] / "nonebot" / "adapters"

# NoneBot 的适配器是命名空间包，未通过 pip 安装时（例如直接跑源码）需要把适配器所在目录挂到 nonebot.adapters 的搜索路径上。
if str(ADAPTER_PARENT) not in nonebot.adapters.__path__:  # type: ignore[attr-defined]
    nonebot.adapters.__path__.append(str(ADAPTER_PARENT))  # type: ignore[attr-defined]


def _install_legacy_shims() -> None:
    """为老版本 NoneBot 补上适配器依赖的公共接口。

    适配器声明的最低版本是 nonebot2>=2.2，这里的 shim 只用于让老版本环境也能跑通冒烟测试，不会影响适配器自身的代码。
    """

    if not hasattr(nonebot, "get_plugin_config"):

        def _get_plugin_config(model, *args, **kwargs):
            return model(**dict(nonebot.get_driver().config))

        nonebot.get_plugin_config = _get_plugin_config  # type: ignore[attr-defined]


def test_adapter_bot_and_message_smoke(tmp_path):
    # 部分 NoneBot 版本在初始化时会创建 asyncio.Event()，因此需要先准备好事件循环。
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        nonebot.init(
            # 显式写法兼容各版本 NoneBot（"none" 简写在部分版本不可用）
            driver="nonebot.drivers.none:Driver",
            github_user_login="bot@example.com",
            github_user_password="pw",
            github_user_totp_secret="JBSWY3DPEHPK3PXP",
            github_user_mail_protocol="pop3",
            github_user_mail_host="pop.example.com",
            github_user_mail_user="bot@example.com",
            github_user_mail_password="mail-pw",
            github_user_mail_ssl=False,
            github_user_mail_starttls=True,
            github_user_token="ghp_test",
        )

        _install_legacy_shims()
        get_plugin_config = nonebot.get_plugin_config
        from nonebot.adapters.github_user import (
            Adapter,
            Bot,
            Config,
            GitHubSession,
            Message,
            MessageSegment,
        )

        config = get_plugin_config(Config)
        accounts = config.account_list()
        assert len(accounts) == 1
        assert accounts[0].login == "bot@example.com"
        assert accounts[0].totp_secret == "JBSWY3DPEHPK3PXP"
        assert accounts[0].display == "bot@example.com"

        mail = config.mail_config()
        assert mail is not None
        assert mail.protocol == "pop3"
        assert mail.host == "pop.example.com"
        assert (mail.use_ssl, mail.starttls) == (False, True)
        assert mail.resolved_port() == 110

        # 没配 host/user/password 时不启用邮箱取码
        assert Config(github_user_login="a", github_user_password="b").mail_config() is None

        # API 模式：固定 token / client_id / 已存在的 token 文件，满足其一即启用
        empty_store = tmp_path / "no_token.json"
        bare = Config(
            github_user_login="a",
            github_user_password="b",
            github_user_token_store=str(empty_store),
        )
        assert bare.api_enabled() is False
        with_token = Config(
            github_user_login="a",
            github_user_password="b",
            github_user_token="ghp_x",
            github_user_token_store=str(empty_store),
        )
        assert with_token.api_enabled() is True
        assert with_token.oauth_scope_list() == ["repo", "workflow"]
        with_flow = Config(
            github_user_login="a",
            github_user_password="b",
            github_user_oauth_client_id="gh",
            github_user_token_store=str(empty_store),
        )
        assert with_flow.api_enabled() is True

        # 跑过 --oauth-login 之后，只有 token 文件也算启用（env 不用配 token）
        empty_store.write_text("{}", encoding="utf-8")
        with_store = Config(
            github_user_login="a",
            github_user_password="b",
            github_user_token_store=str(empty_store),
        )
        assert with_store.api_enabled() is True

        driver = nonebot.get_driver()
        adapter = Adapter(driver)
        assert Adapter.get_name() == "GitHub-User"
        assert adapter.get_name() == "GitHub-User"

        # 适配器按配置构造 token 管理器（只构造，不联网）
        manager = adapter.build_token_manager()
        assert manager is not None
        assert manager.static_token == "ghp_test"
        assert adapter.api is None  # startup 之前不建客户端

        session = GitHubSession(
            "bot@example.com", "pw", totp_secret="JBSWY3DPEHPK3PXP"
        )
        assert session.login_name == "bot@example.com"
        assert session.totp_secret == "JBSWY3DPEHPK3PXP"
        assert session.authenticated is False

        bot = Bot(adapter, session, self_id="bot-account")
        assert bot.username is None
        assert "GitHub-User" in repr(bot)

        assert str(Message("hello")) == "hello"
        segment = MessageSegment.markdown("**hi**")
        assert segment.is_text() is True
        assert str(segment) == "**hi**"
        assert Message([segment]) == Message(MessageSegment.markdown("**hi**"))

        async def _connect_and_shutdown() -> None:
            # bot_connect 依赖运行中的事件循环（内部会 create_task）
            adapter.bot_connect(bot)
            assert adapter.bots == {"bot-account": bot}
            await adapter.shutdown()

        loop.run_until_complete(_connect_and_shutdown())
        assert adapter.bots == {}
    finally:
        loop.close()
        asyncio.set_event_loop(None)
