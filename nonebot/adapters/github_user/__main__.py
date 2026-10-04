"""命令行工具：OAuth 设备流登录取 token、校验 token 是否可用。

.. code-block:: shell

    # 1. 一次性授权（浏览器里输一次性代码），token 落盘
    python -m nonebot.adapters.github_user --oauth-login --oauth-client-id gh

    # 2. 验证 token 能用于 API
    python -m nonebot.adapters.github_user --check-api
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from .api import GitHubAPI, GitHubAPIError
from .oauth import (
    OAuthError,
    TokenManager,
    TokenStore,
    TokenUnavailable,
    resolve_client_id,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nonebot.adapters.github_user",
        description="GitHub 适配器命令行工具（设备流登录取 token / 校验 token）",
    )
    parser.add_argument(
        "--oauth-login",
        action="store_true",
        help="走一次 OAuth 设备流授权并保存 token",
    )
    parser.add_argument(
        "--check-api",
        action="store_true",
        help="用已保存或传入的 token 调一次 GET /user 验证",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_USER_TOKEN"),
        help="固定 token（PAT 或 `gh auth token` 的输出），默认读 GITHUB_USER_TOKEN",
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
        "--proxy", default=os.getenv("GITHUB_USER_PROXY"), help="代理地址"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("GITHUB_USER_TIMEOUT", "30")),
        help="单次请求超时（秒）",
    )
    return parser


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


async def run(args: argparse.Namespace) -> int:
    if args.oauth_login:
        return await run_oauth_login(args)
    if args.check_api:
        return await run_check_api(args)
    print(
        "没有指定动作：用 --oauth-login 做一次设备流授权，或用 --check-api 校验已有 token",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台常是 GBK：把不可编码字符替换掉，并把输出改成行缓冲，
    # 这样重定向到管道/文件时设备流的一次性代码也能立刻看到。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace", line_buffering=True)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 少数环境（如被重定向）不支持就不管
            pass
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
