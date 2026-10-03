"""适配器对外暴露的异常类型。

``session`` 模块内部是纯 Python 异常，这里统一包装成 NoneBot 的 ``AdapterException`` 家族，方便插件按 NoneBot 习惯捕获。
"""

from __future__ import annotations

from nonebot.exception import ActionFailed as BaseActionFailed
from nonebot.exception import AdapterException
from nonebot.exception import NetworkError as BaseNetworkError


class GitHubUserAdapterException(AdapterException):
    """GitHub 用户账号适配器异常基类。"""

    def __init__(self, message: str = "") -> None:
        super().__init__("GitHub-User", message)
        self.message = message

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.message!r})"


class NetworkError(GitHubUserAdapterException, BaseNetworkError):
    """网络不可用或请求失败。"""


class ActionFailed(GitHubUserAdapterException, BaseActionFailed):
    """请求成功返回，但 GitHub 返回了非 2xx 结果。"""


class AuthenticationFailed(GitHubUserAdapterException):
    """用户名或密码被 GitHub 拒绝。"""


class CSRFError(GitHubUserAdapterException):
    """登录表单令牌缺失或失效。"""


class TwoFactorRequired(GitHubUserAdapterException):
    """需要双因素验证，但没有可用的验证码来源。"""


class TwoFactorRejected(GitHubUserAdapterException):
    """双因素验证码被 GitHub 拒绝。"""


class DeviceVerificationRequired(GitHubUserAdapterException):
    """GitHub 要求设备（邮箱）验证，自动化流程无法继续。"""


class CaptchaRequired(GitHubUserAdapterException):
    """GitHub 要求完成人机验证。"""


class AccountRestricted(GitHubUserAdapterException):
    """账号被 GitHub 限制或标记。"""


class RateLimited(GitHubUserAdapterException):
    """触发 GitHub 频率限制。"""


class SessionExpired(GitHubUserAdapterException):
    """会话失效且重新登录失败。"""


__all__ = [
    "AccountRestricted",
    "ActionFailed",
    "AuthenticationFailed",
    "CSRFError",
    "CaptchaRequired",
    "DeviceVerificationRequired",
    "GitHubUserAdapterException",
    "NetworkError",
    "RateLimited",
    "SessionExpired",
    "TwoFactorRejected",
    "TwoFactorRequired",
]
