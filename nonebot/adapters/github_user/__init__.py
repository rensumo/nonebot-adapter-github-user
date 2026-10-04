"""NoneBot2 GitHub 用户账号适配器。

以一个专用 GitHub 账号的用户 token（PAT / OAuth 设备流）调用官方 REST /
GraphQL API，并可选接收 GitHub webhook 事件。
"""

from .adapter import Adapter as Adapter
from .api import GitHubAPI as GitHubAPI
from .api import GitHubAPIError as GitHubAPIError
from .api import get_github_api as get_github_api
from .bot import Bot as Bot
from .config import Config as Config
from .event import Event as Event
from .event import RawEvent as RawEvent
from .event import WebhookEvent as WebhookEvent
from .exception import ActionFailed as ActionFailed
from .exception import GitHubUserAdapterException as GitHubUserAdapterException
from .exception import NetworkError as NetworkError
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
from .webhook import ReplyTarget as ReplyTarget
from .webhook import WebhookError as WebhookError
from .webhook import WebhookPayload as WebhookPayload
from .webhook import WebhookSignatureError as WebhookSignatureError
from .webhook import parse_webhook as parse_webhook
from .webhook import reply_target as reply_target
from .webhook import sign_payload as sign_payload
from .webhook import verify_signature as verify_signature

__all__ = [
    "ActionFailed",
    "Adapter",
    "Bot",
    "Config",
    "DeviceCode",
    "DeviceFlowDenied",
    "DeviceFlowExpired",
    "Event",
    "GitHubAPI",
    "GitHubAPIError",
    "GitHubUserAdapterException",
    "Message",
    "MessageSegment",
    "NetworkError",
    "OAuthDeviceFlow",
    "OAuthError",
    "RawEvent",
    "ReplyTarget",
    "TokenManager",
    "TokenSet",
    "TokenStore",
    "TokenUnavailable",
    "WebhookError",
    "WebhookEvent",
    "WebhookPayload",
    "WebhookSignatureError",
    "get_github_api",
    "parse_webhook",
    "reply_target",
    "sign_payload",
    "verify_signature",
]
