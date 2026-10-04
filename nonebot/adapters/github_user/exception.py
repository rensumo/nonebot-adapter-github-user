"""适配器对外暴露的异常类型。"""

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
    """网络不可用或请求失败（API 层连不上 GitHub）。"""


class ActionFailed(GitHubUserAdapterException, BaseActionFailed):
    """GitHub 返回了非 2xx，或无法确定回复目标。"""


__all__ = [
    "ActionFailed",
    "GitHubUserAdapterException",
    "NetworkError",
]
