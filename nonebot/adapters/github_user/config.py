"""适配器配置。

两种配置方式二选一：

1. 单账号简写（推荐，最小配置）：

   .. code-block:: dotenv

      GITHUB_USER_LOGIN=bot@example.com
      GITHUB_USER_PASSWORD=your-password
      GITHUB_USER_TOTP_SECRET=ABCDEFGHIJKLMNOP

2. 多账号列表：

   .. code-block:: dotenv

      GITHUB_USER_ACCOUNTS='[{"login":"a@example.com","password":"...","totp_secret":"..."}]'
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from .compat import field_validator
from .mail import MailboxConfig


class GitHubUserAccount(BaseModel):
    """一个 GitHub 账号的登录信息。"""

    login: str
    """用户名或邮箱。"""

    password: str
    """账号密码。"""

    totp_secret: Optional[str] = None
    """TOTP 密钥（base32 或 otpauth:// URI），未开启双因素时留空。"""

    label: Optional[str] = None
    """日志里显示的别名，默认使用登录名。"""

    proxy: Optional[str] = None
    """该账号单独使用的代理。"""

    cookie_store: Optional[str] = None
    """会话 Cookie 缓存文件路径，留空表示不缓存。"""

    enabled: bool = True
    """是否启用该账号。"""

    @field_validator("login", "password", "totp_secret", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @property
    def display(self) -> str:
        return self.label or self.login


class Config(BaseModel):
    """GitHub 用户账号适配器配置。"""

    github_user_accounts: List[GitHubUserAccount] = Field(default_factory=list)
    """多账号配置。"""

    github_user_login: Optional[str] = None
    """单账号简写：用户名或邮箱。"""

    github_user_password: Optional[str] = None
    """单账号简写：密码。"""

    github_user_totp_secret: Optional[str] = None
    """单账号简写：TOTP 密钥，未开启双因素时留空。"""

    github_user_cookie_store: Optional[str] = None
    """单账号简写：会话 Cookie 缓存文件路径。"""

    github_user_base_url: str = "https://github.com"
    """GitHub 站点地址，一般无需修改。"""

    github_user_user_agent: Optional[str] = None
    """自定义 User-Agent，默认使用内置的桌面浏览器标识。"""

    github_user_proxy: Optional[str] = None
    """全局代理地址，如 ``http://127.0.0.1:7890``。"""

    github_user_timeout: float = 30.0
    """单次 HTTP 请求超时（秒）。"""

    github_user_max_retries: int = 2
    """登录过程中网络错误的重试次数。"""

    github_user_totp_attempts: int = 2
    """双因素验证码被拒绝后，最多尝试几个时间窗口。"""

    github_user_login_retries: int = 3
    """适配器启动时登录失败的重试次数。"""

    github_user_login_backoff: float = 5.0
    """启动登录重试的基础退避秒数（按次数线性增长）。"""

    # ------------------------------------------------------------------ #
    # 设备验证自动取码（IMAP / POP3）                                      #
    # ------------------------------------------------------------------ #
    github_user_mail_protocol: Optional[str] = None
    """收信协议：``imap`` 或 ``pop3``；留空表示不启用邮箱取码。"""

    github_user_mail_host: Optional[str] = None
    """收信服务器地址，如 ``imap.qq.com`` / ``pop.qq.com``。"""

    github_user_mail_port: Optional[int] = None
    """端口，留空按协议与 SSL 开关推导（993 / 143 / 995 / 110）。"""

    github_user_mail_user: Optional[str] = None
    """邮箱账号（通常是完整邮箱地址）。"""

    github_user_mail_password: Optional[str] = None
    """邮箱密码或授权码（QQ/163 等要用 IMAP/POP3 授权码，不是登录密码）。"""

    github_user_mail_ssl: bool = True
    """True 走 IMAPS/POP3S 直连 TLS；False 时可配 STARTTLS/STLS。"""

    github_user_mail_starttls: bool = False
    """明文端口上是否升级 TLS（IMAP STARTTLS / POP3 STLS）。"""

    github_user_mail_folder: str = "INBOX"
    """IMAP 收件目录，POP3 忽略。"""

    github_user_mail_from: str = "github.com"
    """只处理发件人包含该字符串的邮件，留空表示不过滤。"""

    github_user_mail_subject: str = ""
    """只处理主题包含该字符串的邮件，留空表示不过滤。"""

    github_user_mail_unseen_only: bool = True
    """IMAP：只看未读邮件。"""

    github_user_mail_search_days: int = 2
    """只扫描最近 N 天的邮件。"""

    github_user_mail_max_messages: int = 15
    """最多扫描最新的多少封邮件。"""

    github_user_mail_delete_after_read: bool = False
    """取到验证码后是否删除该邮件。"""

    github_user_mail_poll_timeout: float = 120.0
    """等待验证码邮件的最长秒数。"""

    github_user_mail_poll_interval: float = 5.0
    """轮询邮箱的间隔秒数。"""

    # ------------------------------------------------------------------ #
    # API 模式：PAT（或 gh auth token）/ OAuth 设备流                       #
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

    github_user_api_base_url: str = "https://api.github.com"
    """REST API 地址，企业版自建可改。"""

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

    def account_list(self) -> List[GitHubUserAccount]:
        """返回最终生效的账号列表（简写配置会转成单元素列表）。"""

        accounts = [account for account in self.github_user_accounts if account.enabled]
        if not accounts and self.github_user_login and self.github_user_password:
            accounts.append(
                GitHubUserAccount(
                    login=self.github_user_login,
                    password=self.github_user_password,
                    totp_secret=self.github_user_totp_secret,
                    cookie_store=self.github_user_cookie_store,
                    proxy=self.github_user_proxy,
                )
            )
        return accounts

    def mail_config(self) -> Optional[MailboxConfig]:
        """邮箱取码配置；host / user / password 没配全时返回 None。"""

        if not (
            self.github_user_mail_host
            and self.github_user_mail_user
            and self.github_user_mail_password
        ):
            return None
        return MailboxConfig(
            host=self.github_user_mail_host,
            username=self.github_user_mail_user,
            password=self.github_user_mail_password,
            protocol=self.github_user_mail_protocol or "imap",
            port=self.github_user_mail_port,
            use_ssl=self.github_user_mail_ssl,
            starttls=self.github_user_mail_starttls,
            folder=self.github_user_mail_folder,
            timeout=self.github_user_timeout,
            search_days=self.github_user_mail_search_days,
            max_messages=self.github_user_mail_max_messages,
            unseen_only=self.github_user_mail_unseen_only,
            from_contains=self.github_user_mail_from,
            subject_contains=self.github_user_mail_subject,
            delete_after_read=self.github_user_mail_delete_after_read,
        )
