"""命令行登录自检：``python -m nonebot.adapters.github_user``。

用于在接入 Bot 之前，先在部署机器上验证账号密码（以及 TOTP 双因素）能否走通 GitHub 的登录流程。

.. code-block:: shell

    export GITHUB_USER_LOGIN=bot@example.com
    export GITHUB_USER_PASSWORD=your-password
    export GITHUB_USER_TOTP_SECRET=JBSWY3DPEHPK3PXP   # 可选
    python -m nonebot.adapters.github_user
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .session import (
    DEFAULT_USER_AGENT,
    GitHubSession,
    GitHubUserSessionError,
)


def _mask(value: str, keep: int = 8) -> str:
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "..."


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
    return parser


async def run_check(args: argparse.Namespace) -> int:
    if not args.login or not args.password:
        print(
            "缺少配置：请用 --login/--password 参数，或设置环境变量 "
            "GITHUB_USER_LOGIN / GITHUB_USER_PASSWORD",
            file=sys.stderr,
        )
        return 2

    session = GitHubSession(
        args.login,
        args.password,
        totp_secret=args.totp,
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
    args = build_parser().parse_args(argv)
    return asyncio.run(run_check(args))


if __name__ == "__main__":
    raise SystemExit(main())
