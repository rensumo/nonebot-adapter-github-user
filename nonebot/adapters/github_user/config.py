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

from typing import Any, List, Optional

from pydantic import BaseModel, Field

from .compat import field_validator


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
