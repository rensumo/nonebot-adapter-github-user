"""NoneBot2 GitHub 用户账号适配器。

与官方 ``nonebot-adapter-github``（GitHub App / Webhook）不同，本适配器以专用 GitHub 账号登录网页端，用会话 Cookie 访问 GitHub。
"""

from .adapter import Adapter as Adapter
from .bot import Bot as Bot
from .config import Config as Config
from .config import GitHubUserAccount as GitHubUserAccount
from .event import Event as Event
from .event import RawEvent as RawEvent
from .exception import AccountRestricted as AccountRestricted
from .exception import ActionFailed as ActionFailed
from .exception import AuthenticationFailed as AuthenticationFailed
from .exception import CaptchaRequired as CaptchaRequired
from .exception import CSRFError as CSRFError
from .exception import DeviceVerificationRequired as DeviceVerificationRequired
from .exception import GitHubUserAdapterException as GitHubUserAdapterException
from .exception import NetworkError as NetworkError
from .exception import RateLimited as RateLimited
from .exception import SessionExpired as SessionExpired
from .exception import TwoFactorRejected as TwoFactorRejected
from .exception import TwoFactorRequired as TwoFactorRequired
from .message import Message as Message
from .message import MessageSegment as MessageSegment
from .session import GitHubSession as GitHubSession
from .session import LoginResult as LoginResult
from .session import generate_totp as generate_totp
from .session import parse_totp_secret as parse_totp_secret

__all__ = [
    "AccountRestricted",
    "ActionFailed",
    "Adapter",
    "AuthenticationFailed",
    "Bot",
    "CSRFError",
    "CaptchaRequired",
    "Config",
    "DeviceVerificationRequired",
    "Event",
    "GitHubSession",
    "GitHubUserAccount",
    "GitHubUserAdapterException",
    "LoginResult",
    "Message",
    "MessageSegment",
    "NetworkError",
    "RateLimited",
    "RawEvent",
    "SessionExpired",
    "TwoFactorRejected",
    "TwoFactorRequired",
    "generate_totp",
    "parse_totp_secret",
]
