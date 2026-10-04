"""从邮箱里取 GitHub 设备验证码（IMAP / POP3，支持 SSL 与 STARTTLS）。

和 ``session.py`` 一样，本模块不依赖 NoneBot，可以单独使用：

.. code-block:: python

    mailbox = IMAPMailbox(
        MailboxConfig(host="imap.qq.com", username="bot@qq.com", password="授权码")
    )
    code = await fetch_verification_code(mailbox, timeout=120, interval=5)

支持的连接方式：

- IMAP over SSL（默认 993 端口，``use_ssl=True``）
- IMAP + STARTTLS（143 端口，``use_ssl=False, starttls=True``）
- POP3 over SSL（默认 995 端口，``use_ssl=True``）
- POP3 + STLS（110 端口，``use_ssl=False, starttls=True``）

所有阻塞的网络操作都放在线程里跑，不会卡住 NoneBot 的事件循环。
"""

from __future__ import annotations

import asyncio
import email
import functools
import imaplib
import logging
import poplib
import re
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import Message as EmailMessage
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime
from html import unescape as _unescape_html
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
)

log = logging.getLogger("nonebot.adapters.github_user")

DEFAULT_IMAP_SSL_PORT = 993
DEFAULT_IMAP_PLAIN_PORT = 143
DEFAULT_POP3_SSL_PORT = 995
DEFAULT_POP3_PLAIN_PORT = 110

# imaplib 的 timeout 参数是 Python 3.9 才有的；本适配器要求 3.9+，
# 这里做一次兼容，让 mail.py 在 3.8 上也能单独使用（例如写独立脚本时）。
_IMAP_TIMEOUT_SUPPORTED = sys.version_info >= (3, 9)

__all__ = [
    "IMAPMailbox",
    "MailboxAuthError",
    "MailboxCodeNotFound",
    "MailboxConfig",
    "MailboxConnectionError",
    "MailboxError",
    "MailboxReader",
    "MailMessage",
    "POP3Mailbox",
    "build_mailbox",
    "extract_verification_code",
    "fetch_verification_code",
    "make_device_otp_provider",
]


# --------------------------------------------------------------------------- #
# 异常                                                                         #
# --------------------------------------------------------------------------- #
class MailboxError(Exception):
    """邮箱相关错误的基类。"""


class MailboxConnectionError(MailboxError):
    """连接/SSL 握手失败。"""


class MailboxAuthError(MailboxError):
    """账号或授权码被邮箱服务器拒绝。"""


class MailboxCodeNotFound(MailboxError):
    """在设定时间内没有从邮箱里找到验证码。"""


# --------------------------------------------------------------------------- #
# 配置与数据结构                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class MailboxConfig:
    """一个邮箱的收信配置。"""

    host: str
    username: str
    password: str
    protocol: str = "imap"
    """``imap`` 或 ``pop3``。"""

    port: Optional[int] = None
    """留空时按协议与 SSL 开关推导（993 / 143 / 995 / 110）。"""

    use_ssl: bool = True
    """True 走 IMAPS/POP3S 直连 TLS；False 可选配 STARTTLS/STLS。"""

    starttls: bool = False
    """明文端口上是否升级 TLS（IMAP STARTTLS / POP3 STLS）。"""

    folder: str = "INBOX"
    """IMAP 邮箱目录，POP3 忽略此项。"""

    timeout: float = 30.0
    """单次连接/读取超时（秒）。"""

    search_days: int = 2
    """只扫描最近 N 天的邮件（IMAP 用 SINCE，POP3 用邮件 Date 头）。"""

    max_messages: int = 15
    """最多扫描最新的多少封邮件。"""

    unseen_only: bool = True
    """IMAP：只看未读邮件；POP3 无此概念，会自动忽略。"""

    from_contains: str = "github.com"
    """发件人包含该字符串才处理，留空表示不过滤。"""

    subject_contains: str = ""
    """主题包含该字符串才处理，留空表示不过滤。"""

    delete_after_read: bool = False
    """取到验证码后删除该邮件（POP3 用 DELE，IMAP 标记删除并 expunge）。"""

    def resolved_port(self) -> int:
        if self.port:
            return int(self.port)
        protocol = (self.protocol or "imap").lower()
        if protocol == "pop3":
            return DEFAULT_POP3_SSL_PORT if self.use_ssl else DEFAULT_POP3_PLAIN_PORT
        return DEFAULT_IMAP_SSL_PORT if self.use_ssl else DEFAULT_IMAP_PLAIN_PORT


@dataclass
class MailMessage:
    """一封已解析的邮件。"""

    uid: str
    subject: str = ""
    sender: str = ""
    date: str = ""
    body: str = ""
    raw_size: int = 0

    def text_for_code(self) -> str:
        """把主题和正文拼起来用于提取验证码。"""

        return f"{self.subject}\n{self.body}".strip()


# --------------------------------------------------------------------------- #
# 验证码提取                                                                   #
# --------------------------------------------------------------------------- #
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")
_LABELED_CODE_RE = re.compile(
    r"(?:verification\s+code|one[\s-]?time\s+code|launch\s+code|code\s+is|"
    r"验证码|校验码)[^\d\n]{0,24}(\d{4,8})",
    re.IGNORECASE,
)
_BARE_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def extract_verification_code(text: str) -> Optional[str]:
    """从邮件文本里提取验证码。

    先找带标签的（``Verification code: 123456`` / ``验证码 123456``），
    找不到再退回「独立的 6 位数字」。HTML 标签会先被剥掉。
    """

    if not text:
        return None
    plain = _unescape_html(_STRIP_TAGS_RE.sub(" ", text))
    plain = re.sub(r"[ \t\u00a0]+", " ", plain)
    labeled = _LABELED_CODE_RE.search(plain)
    if labeled:
        return labeled.group(1)
    bare = _BARE_CODE_RE.search(plain)
    return bare.group(1) if bare else None


# --------------------------------------------------------------------------- #
# 邮件解析                                                                     #
# --------------------------------------------------------------------------- #
def _decode_part(part: EmailMessage) -> str:
    try:
        payload = part.get_content()
    except Exception:  # noqa: BLE001 - 编码异常时退回原始字节
        raw = part.get_payload(decode=True) or b""
        payload = raw.decode(part.get_content_charset() or "utf-8", "replace")
    return payload if isinstance(payload, str) else str(payload)


def parse_mail(raw: bytes, uid: str = "") -> MailMessage:
    """把原始邮件字节解析成 :class:`MailMessage`。"""

    message = email.message_from_bytes(raw, policy=default_policy)
    subject = str(message.get("Subject", "") or "").strip()
    sender = str(message.get("From", "") or "").strip()
    date = str(message.get("Date", "") or "").strip()

    text_parts: List[str] = []
    html_parts: List[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            content_type = (part.get_content_type() or "").lower()
            if content_type == "text/plain":
                text_parts.append(_decode_part(part))
            elif content_type == "text/html":
                html_parts.append(_decode_part(part))
    else:
        content_type = (message.get_content_type() or "").lower()
        (html_parts if content_type == "text/html" else text_parts).append(
            _decode_part(message)
        )

    # 纯文本优先，但 HTML 部分也一起保留：有些邮件只在 HTML 里带验证码
    body = "\n".join(text_parts)
    if html_parts:
        html_body = "\n".join(html_parts)
        body = f"{body}\n{html_body}".strip()
    return MailMessage(
        uid=uid,
        subject=subject,
        sender=sender,
        date=date,
        body=body,
        raw_size=len(raw),
    )


# --------------------------------------------------------------------------- #
# 收信客户端                                                                   #
# --------------------------------------------------------------------------- #
class MailboxReader:
    """收信读取接口。"""

    def __init__(self, config: MailboxConfig) -> None:
        self.config = config

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} {self.config.protocol}://"
            f"{self.config.host}:{self.config.resolved_port()} "
            f"user={self.config.username}>"
        )

    # -- 子类实现（阻塞） -------------------------------------------------- #
    def fetch_messages(self) -> List[MailMessage]:
        raise NotImplementedError

    def consume(self, message: MailMessage) -> None:
        """取到验证码后可选地删除该邮件。"""

    # -- 异步包装 ---------------------------------------------------------- #
    async def fetch_messages_async(self) -> List[MailMessage]:
        return await _to_thread(self.fetch_messages)

    async def consume_async(self, message: MailMessage) -> None:
        await _to_thread(self.consume, message)

    # -- 过滤 -------------------------------------------------------------- #
    def matches(self, message: MailMessage) -> bool:
        """发件人/主题过滤，默认只留 GitHub 的邮件。"""

        config = self.config
        if config.from_contains and config.from_contains.lower() not in (
            message.sender.lower()
        ):
            return False
        if config.subject_contains and config.subject_contains.lower() not in (
            message.subject.lower()
        ):
            return False
        return True


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


async def _to_thread(func: Callable[..., Any], *args: Any) -> Any:
    """``asyncio.to_thread`` 的 3.8 兼容写法（阻塞调用丢到线程池）。"""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args))


class IMAPMailbox(MailboxReader):
    """IMAP / IMAPS 收信。"""

    def _connect(self) -> imaplib.IMAP4:
        config = self.config
        port = config.resolved_port()
        try:
            if config.use_ssl:
                ssl_kwargs: Dict[str, Any] = {"ssl_context": _ssl_context()}
                if _IMAP_TIMEOUT_SUPPORTED:
                    ssl_kwargs["timeout"] = config.timeout
                client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                    config.host, port, **ssl_kwargs
                )
            else:
                if _IMAP_TIMEOUT_SUPPORTED:
                    client = imaplib.IMAP4(config.host, port, timeout=config.timeout)
                else:
                    client = imaplib.IMAP4(config.host, port)
                if config.starttls:
                    client.starttls(ssl_context=_ssl_context())
        except (OSError, ssl.SSLError) as exc:
            raise MailboxConnectionError(
                f"连接 IMAP 服务器失败（{config.host}:{port}，ssl={config.use_ssl}）：{exc}"
            ) from exc

        try:
            client.login(config.username, config.password)
        except imaplib.IMAP4.error as exc:
            try:
                client.shutdown()
            except Exception:  # noqa: BLE001 - 关闭失败不影响报错
                pass
            raise MailboxAuthError(f"IMAP 登录失败：{exc}") from exc
        return client

    @staticmethod
    def _close(client: imaplib.IMAP4, selected: bool = False) -> None:
        if selected:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass

    def fetch_messages(self) -> List[MailMessage]:
        config = self.config
        client = self._connect()
        selected = False
        try:
            typ, data = client.select(
                config.folder, readonly=not config.delete_after_read
            )
            selected = True
            if typ != "OK":
                raise MailboxError(f"IMAP 选择目录 {config.folder} 失败：{data}")

            criteria: List[str] = []
            if config.unseen_only:
                criteria.append("UNSEEN")
            if config.search_days > 0:
                since = (datetime.now() - timedelta(days=config.search_days)).strftime(
                    "%d-%b-%Y"
                )
                criteria += ["SINCE", since]

            typ, data = client.search(None, *(criteria or ["ALL"]))
            if typ != "OK":
                raise MailboxError(f"IMAP 搜索失败：{data}")
            numbers = (data[0] or b"").split()
            if not numbers:
                return []

            messages: List[MailMessage] = []
            for number in reversed(numbers[-config.max_messages :]):
                typ, payload = client.fetch(number, "(RFC822)")
                if typ != "OK" or not payload:
                    continue
                raw = b"".join(
                    part[1] for part in payload if isinstance(part, tuple) and part[1]
                )
                if raw:
                    messages.append(parse_mail(raw, uid=number.decode("ascii", "ignore")))
            return messages
        finally:
            self._close(client, selected)

    def consume(self, message: MailMessage) -> None:
        if not self.config.delete_after_read or not message.uid:
            return
        client = self._connect()
        selected = False
        try:
            typ, _ = client.select(self.config.folder, readonly=False)
            selected = True
            if typ != "OK":
                return
            client.store(message.uid, "+FLAGS", "(\\Deleted)")
            client.expunge()
        finally:
            self._close(client, selected)


class POP3Mailbox(MailboxReader):
    """POP3 / POP3S 收信。"""

    def _connect(self) -> poplib.POP3:
        config = self.config
        port = config.resolved_port()
        try:
            if config.use_ssl:
                client: poplib.POP3 = poplib.POP3_SSL(
                    config.host,
                    port,
                    timeout=config.timeout,
                    context=_ssl_context(),
                )
            else:
                client = poplib.POP3(config.host, port, config.timeout)
                if config.starttls:
                    client.stls(context=_ssl_context())
        except (OSError, ssl.SSLError) as exc:
            raise MailboxConnectionError(
                f"连接 POP3 服务器失败（{config.host}:{port}，ssl={config.use_ssl}）：{exc}"
            ) from exc

        try:
            client.user(config.username)
            client.pass_(config.password)
        except poplib.error_proto as exc:
            try:
                client.quit()
            except Exception:  # noqa: BLE001
                pass
            raise MailboxAuthError(f"POP3 登录失败：{exc}") from exc
        return client

    @staticmethod
    def _close(client: poplib.POP3) -> None:
        try:
            client.quit()
        except Exception:  # noqa: BLE001
            pass

    def fetch_messages(self) -> List[MailMessage]:
        config = self.config
        client = self._connect()
        try:
            _resp, listings, _octets = client.list()
            numbers = [int(line.split()[0]) for line in listings if line.split()]
            if not numbers:
                return []

            messages: List[MailMessage] = []
            for number in reversed(numbers[-config.max_messages :]):
                _resp, lines, octets = client.retr(number)
                raw = b"\r\n".join(lines)
                uid = f"{number}:{octets}"
                message = parse_mail(raw, uid=uid)
                if self._within_days(message):
                    messages.append(message)
            return messages
        finally:
            self._close(client)

    def _within_days(self, message: MailMessage) -> bool:
        if self.config.search_days <= 0 or not message.date:
            return True
        try:
            parsed = parsedate_to_datetime(message.date)
        except (TypeError, ValueError):
            return True
        if parsed is None:
            return True
        if parsed.tzinfo is None:
            cutoff = datetime.now() - timedelta(days=self.config.search_days)
        else:
            cutoff = datetime.now(parsed.tzinfo) - timedelta(days=self.config.search_days)
        return parsed >= cutoff

    def consume(self, message: MailMessage) -> None:
        if not self.config.delete_after_read or not message.uid:
            return
        number = message.uid.split(":", 1)[0]
        client = self._connect()
        try:
            client.dele(int(number))
        finally:
            self._close(client)


def build_mailbox(config: MailboxConfig) -> MailboxReader:
    """按协议创建收信客户端。"""

    protocol = (config.protocol or "imap").strip().lower()
    if protocol in ("imap", "imaps"):
        return IMAPMailbox(config)
    if protocol in ("pop3", "pop3s"):
        return POP3Mailbox(config)
    raise MailboxError(f"不支持的收信协议：{config.protocol!r}（只支持 imap / pop3）")


# --------------------------------------------------------------------------- #
# 取码                                                                         #
# --------------------------------------------------------------------------- #
async def fetch_verification_code(
    mailbox: MailboxReader,
    *,
    timeout: float = 120.0,
    interval: float = 5.0,
    ignore_codes: Iterable[str] = (),
    ignore_uids: Iterable[str] = (),
) -> str:
    """轮询邮箱直到取到验证码。

    :param timeout: 最长等待秒数（GitHub 的验证码邮件通常几秒内到达）
    :param interval: 轮询间隔秒数
    :param ignore_codes: 已经用过的验证码，避免重复提交
    :param ignore_uids: 已经处理过的邮件 uid
    :raises MailboxCodeNotFound: 超时仍未找到
    """

    ignore_code_set: Set[str] = {str(code) for code in ignore_codes}
    ignore_uid_set: Set[str] = {str(uid) for uid in ignore_uids}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    scanned: List[str] = []

    while True:
        try:
            messages = await mailbox.fetch_messages_async()
        except MailboxError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络/协议异常统一抛出
            raise MailboxConnectionError(f"读取邮箱失败：{exc}") from exc

        for message in messages:
            if message.uid in ignore_uid_set:
                continue
            if not mailbox.matches(message):
                continue
            scanned.append(f"{message.date} | {message.sender} | {message.subject}")
            code = extract_verification_code(message.text_for_code())
            if not code or code in ignore_code_set:
                continue
            log.info("从邮箱中取到验证码邮件：%s", message.subject or "(无主题)")
            if mailbox.config.delete_after_read:
                try:
                    await mailbox.consume_async(message)
                except Exception as exc:  # noqa: BLE001 - 删除失败不影响取码
                    log.warning("删除已读验证邮件失败：%s", exc)
            return code

        if loop.time() >= deadline:
            detail = "；".join(scanned[-5:]) if scanned else "（没有匹配的邮件）"
            raise MailboxCodeNotFound(
                f"等待 {timeout:g} 秒仍未从邮箱取到验证码，最近看到的邮件：{detail}"
            )
        await asyncio.sleep(max(0.5, interval))


def make_device_otp_provider(
    mailbox: MailboxReader,
    *,
    timeout: float = 120.0,
    interval: float = 5.0,
) -> Callable[[], Awaitable[str]]:
    """生成给 ``GitHubSession(device_otp_provider=...)`` 用的取码回调。"""

    async def provider() -> str:
        return await fetch_verification_code(
            mailbox, timeout=timeout, interval=interval
        )

    return provider
