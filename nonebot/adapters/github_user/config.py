"""适配器配置。

鉴权二选一（详见 README「API 模式」）：

.. code-block:: dotenv

    # 方式①：固定 token（PAT，或本机 `gh auth token` 的输出）
    GITHUB_USER_TOKEN=ghp_xxxxxxxx

    # 方式②：OAuth 设备流（跑一次 --oauth-login，token 落盘）
    GITHUB_USER_OAUTH_CLIENT_ID=gh
    GITHUB_USER_TOKEN_STORE=./github_user_token.json

Webhook 入口（可选）：

.. code-block:: dotenv

    GITHUB_USER_WEBHOOK_PATH=/github/webhook
    GITHUB_USER_WEBHOOK_SECRET=和GitHub上填的一致
"""

from __future__ import annotations

import os
from typing import List, Optional

from pydantic import BaseModel


class Config(BaseModel):
    """GitHub 用户账号适配器配置。"""

    # ------------------------------------------------------------------ #
    # 鉴权：固定 token / OAuth 设备流                                       #
    # ------------------------------------------------------------------ #
    github_user_token: Optional[str] = None
    """固定 token（PAT 或 ``gh auth token`` 的输出）；配了就不走设备流。"""

    github_user_oauth_client_id: Optional[str] = None
    """OAuth App 的 client_id；填 ``gh`` 表示用 GitHub CLI 的公开 client_id。"""

    github_user_oauth_client_secret: Optional[str] = None
    """OAuth App 的 client_secret，没有就留空。"""

    github_user_oauth_scopes: str = "repo,workflow"
    """设备流申请的 scope，逗号分隔。"""

    github_user_token_store: str = "./github_user_token.json"
    """设备流 token 的落盘路径（写入时权限 0600）。"""

    # ------------------------------------------------------------------ #
    # API 客户端                                                           #
    # ------------------------------------------------------------------ #
    github_user_api_base_url: str = "https://api.github.com"
    """REST API 地址，企业版自建可改。"""

    github_user_timeout: float = 30.0
    """单次 HTTP 请求超时（秒）。"""

    github_user_proxy: Optional[str] = None
    """代理地址，如 ``http://127.0.0.1:7890``。"""

    github_user_user_agent: str = "nonebot-adapter-github-user"
    """API 请求的 User-Agent。"""

    # ------------------------------------------------------------------ #
    # Webhook 入口                                                         #
    # ------------------------------------------------------------------ #
    github_user_webhook_path: str = "/github/webhook"
    """接收 webhook 的路径；留空表示不开启入口。"""

    github_user_webhook_secret: Optional[str] = None
    """Webhook secret，用于校验 ``X-Hub-Signature-256``。"""

    github_user_webhook_allow_unsigned: bool = False
    """没配 secret 时是否放行（不安全，只建议本地调试用）。"""

    github_user_webhook_events: str = ""
    """只处理这些事件（逗号分隔的 ``X-GitHub-Event``），留空表示全部。"""

    github_user_webhook_self_id: Optional[str] = None
    """Webhook Bot 的 self_id，默认用 token 对应的登录名。"""

    # ------------------------------------------------------------------ #
    # 派生配置                                                             #
    # ------------------------------------------------------------------ #
    def api_enabled(self) -> bool:
        """是否启用了 API 模式。

        满足任意一条即可：配了固定 token、配了 client_id，或本地已经有
        设备流存下来的 token 文件（跑过 ``--oauth-login`` 就不必再写 env）。
        """

        if self.github_user_token or self.github_user_oauth_client_id:
            return True
        try:
            return bool(self.github_user_token_store) and os.path.exists(
                self.github_user_token_store
            )
        except OSError:
            return False

    def oauth_scope_list(self) -> List[str]:
        return [
            item.strip()
            for item in (self.github_user_oauth_scopes or "").split(",")
            if item.strip()
        ]

    def webhook_event_filter(self) -> List[str]:
        return [
            item.strip()
            for item in (self.github_user_webhook_events or "").split(",")
            if item.strip()
        ]

    def webhook_ready(self) -> bool:
        """入口是否需要注册（有路径，且能校验签名或明确允许不带签名）。"""

        if not self.github_user_webhook_path:
            return False
        return bool(self.github_user_webhook_secret) or (
            self.github_user_webhook_allow_unsigned
        )
