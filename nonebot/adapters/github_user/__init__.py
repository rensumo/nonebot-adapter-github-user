"""NoneBot2 GitHub 用户账号适配器。

与官方 ``nonebot-adapter-github``（GitHub App / Webhook）不同，本适配器以专用 GitHub 账号登录网页端，用会话 Cookie 访问 GitHub。
"""

from .adapter import Adapter as Adapter
from .api import GitHubAPI as GitHubAPI
from .api import GitHubAPIError as GitHubAPIError
from .api import get_github_api as get_github_api
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
from .mail import IMAPMailbox as IMAPMailbox
from .mail import MailboxCodeNotFound as MailboxCodeNotFound
from .mail import MailboxConfig as MailboxConfig
from .mail import MailboxError as MailboxError
from .mail import POP3Mailbox as POP3Mailbox
from .mail import build_mailbox as build_mailbox
from .mail import extract_verification_code as extract_verification_code
from .mail import fetch_verification_code as fetch_verification_code
from .mail import make_device_otp_provider as make_device_otp_provider
from .message import Message as Message
from .message import MessageSegment as MessageSegment
from .oauth import DeviceCode as DeviceCode
from .oauth import DeviceFlowDenied as DeviceFlowDenied
from .oauth import DeviceFlowExpired as DeviceFlowExpired
from .oauth import OAuthDeviceFlow as OAuthDeviceFlow
from .oauth import OAuthError as OAuthError
from .oauth import TokenManager as TokenManager
from .oauth import TokenSet as TokenSet
from .oauth import TokenStore as TokenStore
from .oauth import TokenUnavailable as TokenUnavailable
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
    "DeviceCode",
    "DeviceFlowDenied",
    "DeviceFlowExpired",
    "Event",
    "GitHubAPI",
    "GitHubAPIError",
    "GitHubSession",
    "GitHubUserAccount",
    "GitHubUserAdapterException",
    "IMAPMailbox",
    "LoginResult",
    "MailboxCodeNotFound",
    "MailboxConfig",
    "MailboxError",
    "Message",
    "MessageSegment",
    "NetworkError",
    "OAuthDeviceFlow",
    "OAuthError",
    "POP3Mailbox",
    "RateLimited",
    "RawEvent",
    "SessionExpired",
    "TwoFactorRejected",
    "TwoFactorRequired",
    "TokenManager",
    "TokenSet",
    "TokenStore",
    "TokenUnavailable",
    "build_mailbox",
    "extract_verification_code",
    "fetch_verification_code",
    "generate_totp",
    "get_github_api",
    "make_device_otp_provider",
    "parse_totp_secret",
]
