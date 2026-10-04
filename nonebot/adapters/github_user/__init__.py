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
    "IMAPMailbox",
    "LoginResult",
    "MailboxCodeNotFound",
    "MailboxConfig",
    "MailboxError",
    "Message",
    "MessageSegment",
    "NetworkError",
    "POP3Mailbox",
    "RateLimited",
    "RawEvent",
    "SessionExpired",
    "TwoFactorRejected",
    "TwoFactorRequired",
    "build_mailbox",
    "extract_verification_code",
    "fetch_verification_code",
    "generate_totp",
    "make_device_otp_provider",
    "parse_totp_secret",
]
