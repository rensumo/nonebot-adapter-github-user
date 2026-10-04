"""命令行登录自检：``python -m nonebot.adapters.github_user``。

用于在接入 Bot 之前，先在部署机器上验证账号密码（以及 TOTP 双因素）能否走通 GitHub 的登录流程。

.. code-block:: shell

    export GITHUB_USER_LOGIN=bot@example.com
    export GITHUB_USER_PASSWORD=your-password
    export GITHUB_USER_TOTP_SECRET=JBSWY3DPEHPK3PXP      # 可选：开了 TOTP 双因素时
    export GITHUB_USER_MAIL_PROTOCOL=imap                # 可选：设备验证自动取码
    export GITHUB_USER_MAIL_HOST=imap.qq.com
    export GITHUB_USER_MAIL_USER=bot@qq.com
    export GITHUB_USER_MAIL_PASSWORD=邮箱授权码
    python -m nonebot.adapters.github_user
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Awaitable, Callable, List, Optional

from .api import GitHubAPI, GitHubAPIError
from .mail import (
    MailboxConfig,
    MailboxError,
    build_mailbox,
    make_device_otp_provider,
)
from .oauth import (
    OAuthError,
    TokenManager,
    TokenStore,
    TokenUnavailable,
    resolve_client_id,
)
from .session import (
    DEFAULT_USER_AGENT,
    GitHubSession,
    GitHubUserSessionError,
)


def _mask(value: str, keep: int = 8) -> str:
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "..."


def _interactive_provider() -> Callable[[], Awaitable[str]]:
    """让用户在终端里手动输入邮件收到的验证码。"""

    async def provider() -> str:
        prompt = "请输入 GitHub 发到邮箱的验证码，然后回车："
        loop = asyncio.get_running_loop()
        return (await loop.run_in_executor(None, input, prompt)).strip()

    return provider


def _chained_provider(
    providers: List[Callable[[], Awaitable[str]]],
) -> Callable[[], Awaitable[str]]:
    """依次尝试多个取码来源，前面的失败就换下一个。"""

    async def provider() -> str:
        errors: List[str] = []
        for item in providers:
            try:
                return await item()
            except MailboxError as exc:
                errors.append(str(exc))
        raise MailboxError("；".join(errors) or "没有可用的验证码来源")

    return provider


def _build_device_otp_provider(
    args: argparse.Namespace,
) -> Optional[Callable[[], Awaitable[str]]]:
    providers: List[Callable[[], Awaitable[str]]] = []
    if args.mail_host and args.mail_user and args.mail_password:
        mailbox = build_mailbox(
            MailboxConfig(
                host=args.mail_host,
                username=args.mail_user,
                password=args.mail_password,
                protocol=args.mail_protocol or "imap",
                port=args.mail_port,
                use_ssl=not args.mail_no_ssl,
                starttls=args.mail_starttls,
                folder=args.mail_folder,
                timeout=args.timeout,
                from_contains=args.mail_from,
                subject_contains=args.mail_subject,
                delete_after_read=args.mail_delete_after_read,
            )
        )
        providers.append(
            make_device_otp_provider(
                mailbox,
                timeout=args.mail_poll_timeout,
                interval=args.mail_poll_interval,
            )
        )
    if args.ask_device_otp:
        providers.append(_interactive_provider())
    if not providers:
        return None
    if len(providers) == 1:
        return providers[0]
    return _chained_provider(providers)


def _token_manager_from_args(args: argparse.Namespace) -> TokenManager:
    return TokenManager(
        client_id=resolve_client_id(args.oauth_client_id),
        client_secret=args.oauth_client_secret,
        scopes=[
            item.strip()
            for item in (args.oauth_scopes or "").split(",")
            if item.strip()
        ],
        static_token=args.token,
        store=TokenStore(args.token_store),
        api_base_url=args.api_base_url,
        timeout=args.timeout,
        proxy=args.proxy,
    )


async def run_oauth_login(args: argparse.Namespace) -> int:
    """跑一次 OAuth 设备流，把 token 存下来。"""

    manager = _token_manager_from_args(args)
    if not manager.client_id:
        print(
            "缺少 client_id：用 --oauth-client-id 指定（填 gh 表示用 GitHub CLI 的公开 client_id），"
            "或设置 GITHUB_USER_OAUTH_CLIENT_ID",
            file=sys.stderr,
        )
        return 2

    pending = {"count": 0}

    async def on_code(code) -> None:
        print("请在浏览器打开：", code.verification_uri)
        print("输入一次性代码：", code.user_code)
        print(f"（{max(1, code.expires_in // 60)} 分钟内有效，正在等待授权…）")

    async def on_pending() -> None:
        pending["count"] += 1
        if pending["count"] % 6 == 0:
            print("仍未授权，继续等待…")

    try:
        tokens = await manager.login(on_code=on_code, on_pending=on_pending)
    except OAuthError as exc:
        print(f"❌ 设备流登录失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await manager.aclose()

    print("✅ 授权成功")
    print("GitHub 账号：", tokens.login or "(未能确认)")
    print("scope：", ", ".join(tokens.scopes) or "(接口未返回)")
    if tokens.expires_at:
        remain = int(tokens.expires_at - time.time())
        print(f"access token {remain} 秒后过期（到期会用 refresh token 自动续期）")
    else:
        print("access token 长期有效（该 OAuth App 未开启 expiring tokens）")
    print("已保存到：", args.token_store)
    return 0


async def run_check_api(args: argparse.Namespace) -> int:
    """用已有 token 调一次 ``GET /user``，确认能用于 API。"""

    manager = _token_manager_from_args(args)
    try:
        token = await manager.get_token()
    except TokenUnavailable as exc:
        await manager.aclose()
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    api = GitHubAPI(
        token=token,
        api_base_url=args.api_base_url,
        timeout=args.timeout,
        proxy=args.proxy,
    )
    try:
        user = await api.get_authenticated_user()
    except GitHubAPIError as exc:
        print(f"❌ API 调用失败：{exc}", file=sys.stderr)
        return 1
    finally:
        await api.aclose()
        await manager.aclose()

    login = user.get("login") if isinstance(user, dict) else None
    print(f"✅ token 可用，当前身份：{login or '(未返回 login)'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nonebot.adapters.github_user",
        description="GitHub 网页会话登录自检（不会打印密码）",
    )
    parser.add_argument(
        "--login",
        default=os.getenv("GITHUB_USER_LOGIN"),
        help="用户名或邮箱，默认读环境变量 GITHUB_USER_LOGIN",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("GITHUB_USER_PASSWORD"),
        help="密码，默认读环境变量 GITHUB_USER_PASSWORD",
    )
    parser.add_argument(
        "--totp",
        default=os.getenv("GITHUB_USER_TOTP_SECRET"),
        help="TOTP 密钥（base32 或 otpauth:// URI），默认读 GITHUB_USER_TOTP_SECRET",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("GITHUB_USER_BASE_URL", "https://github.com"),
        help="GitHub 站点地址",
    )
    parser.add_argument(
        "--proxy", default=os.getenv("GITHUB_USER_PROXY"), help="代理地址"
    )
    parser.add_argument(
        "--cookie-store",
        default=os.getenv("GITHUB_USER_COOKIE_STORE"),
        help="登录成功后写入的会话 Cookie 缓存文件",
    )
    parser.add_argument(
        "--user-agent",
        default=os.getenv("GITHUB_USER_USER_AGENT", DEFAULT_USER_AGENT),
        help="自定义 User-Agent",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("GITHUB_USER_TIMEOUT", "30")),
        help="单次请求超时（秒）",
    )
    parser.add_argument(
        "--mail-protocol",
        default=os.getenv("GITHUB_USER_MAIL_PROTOCOL"),
        choices=["imap", "pop3"],
        help="设备验证取码用的收信协议，默认读 GITHUB_USER_MAIL_PROTOCOL（imap/pop3）",
    )
    parser.add_argument(
        "--mail-host",
        default=os.getenv("GITHUB_USER_MAIL_HOST"),
        help="收信服务器，如 imap.qq.com / pop.qq.com，默认读 GITHUB_USER_MAIL_HOST",
    )
    parser.add_argument(
        "--mail-port",
        type=int,
        default=os.getenv("GITHUB_USER_MAIL_PORT"),
        help="收信端口，默认按协议与 SSL 推导（993/143/995/110）",
    )
    parser.add_argument(
        "--mail-user",
        default=os.getenv("GITHUB_USER_MAIL_USER"),
        help="邮箱账号，默认读 GITHUB_USER_MAIL_USER",
    )
    parser.add_argument(
        "--mail-password",
        default=os.getenv("GITHUB_USER_MAIL_PASSWORD"),
        help="邮箱密码或授权码，默认读 GITHUB_USER_MAIL_PASSWORD",
    )
    parser.add_argument(
        "--mail-no-ssl",
        action="store_true",
        help="不用直连 SSL，改用明文端口（可再配 --mail-starttls）",
    )
    parser.add_argument(
        "--mail-starttls",
        action="store_true",
        help="明文端口上升级 TLS（IMAP STARTTLS / POP3 STLS）",
    )
    parser.add_argument(
        "--mail-folder",
        default=os.getenv("GITHUB_USER_MAIL_FOLDER", "INBOX"),
        help="IMAP 收件目录，默认 INBOX",
    )
    parser.add_argument(
        "--mail-from",
        default=os.getenv("GITHUB_USER_MAIL_FROM", "github.com"),
        help="发件人过滤关键字，默认 github.com",
    )
    parser.add_argument(
        "--mail-subject",
        default=os.getenv("GITHUB_USER_MAIL_SUBJECT", ""),
        help="主题过滤关键字，默认不过滤",
    )
    parser.add_argument(
        "--mail-delete-after-read",
        action="store_true",
        help="取到验证码后删除该邮件",
    )
    parser.add_argument(
        "--mail-poll-timeout",
        type=float,
        default=float(os.getenv("GITHUB_USER_MAIL_POLL_TIMEOUT", "120")),
        help="等验证码邮件的最长秒数，默认 120",
    )
    parser.add_argument(
        "--mail-poll-interval",
        type=float,
        default=float(os.getenv("GITHUB_USER_MAIL_POLL_INTERVAL", "5")),
        help="轮询邮箱间隔秒数，默认 5",
    )
    parser.add_argument(
        "--ask-device-otp",
        action="store_true",
        help="设备验证时在终端手动输入邮箱收到的验证码",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_USER_TOKEN"),
        help="API 模式的固定 token（PAT 或 `gh auth token` 的输出），默认读 GITHUB_USER_TOKEN",
    )
    parser.add_argument(
        "--oauth-login",
        action="store_true",
        help="走一次 OAuth 设备流授权并保存 token（不需要账号密码）",
    )
    parser.add_argument(
        "--oauth-client-id",
        default=os.getenv("GITHUB_USER_OAUTH_CLIENT_ID"),
        help="OAuth App 的 client_id；填 gh 表示直接用 GitHub CLI 的公开 client_id",
    )
    parser.add_argument(
        "--oauth-client-secret",
        default=os.getenv("GITHUB_USER_OAUTH_CLIENT_SECRET"),
        help="OAuth App 的 client_secret（没有可留空）",
    )
    parser.add_argument(
        "--oauth-scopes",
        default=os.getenv("GITHUB_USER_OAUTH_SCOPES", "repo,workflow"),
        help="设备流申请的 scope，逗号分隔，默认 repo,workflow",
    )
    parser.add_argument(
        "--token-store",
        default=os.getenv("GITHUB_USER_TOKEN_STORE", "./github_user_token.json"),
        help="token 落盘路径，默认 ./github_user_token.json",
    )
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("GITHUB_USER_API_BASE_URL", "https://api.github.com"),
        help="REST API 地址，默认 https://api.github.com",
    )
    parser.add_argument(
        "--check-api",
        action="store_true",
        help="用已保存/传入的 token 调一次 GET /user 验证",
    )
    return parser


async def run_check(args: argparse.Namespace) -> int:
    if args.oauth_login:
        return await run_oauth_login(args)
    if args.check_api:
        return await run_check_api(args)
    if not args.login or not args.password:
        print(
            "缺少配置：请用 --login/--password 参数，或设置环境变量 "
            "GITHUB_USER_LOGIN / GITHUB_USER_PASSWORD",
            file=sys.stderr,
        )
        return 2

    device_otp_provider = _build_device_otp_provider(args)
    if device_otp_provider is not None:
        if args.mail_host and not args.ask_device_otp:
            print(
                f"设备验证：将从 {args.mail_protocol or 'imap'}://{args.mail_host} "
                f"自动取码（最长 {args.mail_poll_timeout:g} 秒）"
            )
        elif args.mail_host and args.ask_device_otp:
            print(
                f"设备验证：先尝试从 {args.mail_host} 自动取码，"
                f"失败后再提示手动输入"
            )
        else:
            print("设备验证：将提示手动输入邮箱收到的验证码")

    session = GitHubSession(
        args.login,
        args.password,
        totp_secret=args.totp,
        device_otp_provider=device_otp_provider,
        base_url=args.base_url,
        proxy=args.proxy,
        cookie_store=args.cookie_store,
        user_agent=args.user_agent,
        timeout=args.timeout,
    )
    print(f"正在登录 {args.login} ...")
    try:
        # force=True：跳过 Cookie 缓存，真正验证一次账号密码（和 TOTP）
        result = await session.login(force=True)
    except GitHubUserSessionError as exc:
        print(f"❌ 登录失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 自检脚本需要打印任何失败
        print(f"❌ 未预期的错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await session.aclose()

    print("✅ 登录成功")
    print(f"用户名：{result.username or '(未能解析)'}")
    print(f"双因素：{'是' if result.two_factor else '否'}")
    print(f"设备验证：{'是' if result.device_verified else '否'}")
    print("会话 Cookie（已脱敏）：")
    for name, value in sorted(result.cookies.items()):
        print(f"  {name} = {_mask(value)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台常是 GBK，打印邮件主题里的 emoji 会抛 UnicodeEncodeError，
    # 这里把不可编码字符替换掉，保证自检脚本不会因为日志内容崩掉。
    # line_buffering：把输出重定向到管道/文件时，设备流的一次性代码也要立刻可见。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace", line_buffering=True)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 少数环境（如被重定向）不支持就不管
            pass
    args = build_parser().parse_args(argv)
    return asyncio.run(run_check(args))


if __name__ == "__main__":
    raise SystemExit(main())
