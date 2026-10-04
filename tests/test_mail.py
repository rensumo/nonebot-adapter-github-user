"""邮箱取码测试：验证码提取 + IMAP/POP3 真实协议交互（用假服务器）。

不联网、不需要 NoneBot，直接 ``python -m pytest tests -q`` 即可。
"""

from __future__ import annotations

import asyncio
import importlib.util
import socketserver
import sys
import threading
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List

PKG_DIR = Path(__file__).resolve().parents[1] / "nonebot" / "adapters" / "github_user"


def _load_module(module_name: str, file_stem: str):
    spec = importlib.util.spec_from_file_location(
        module_name, PKG_DIR / f"{file_stem}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mail_mod = _load_module("github_user_mail", "mail")
IMAPMailbox = mail_mod.IMAPMailbox
POP3Mailbox = mail_mod.POP3Mailbox
MailboxConfig = mail_mod.MailboxConfig

IMAP_PASSWORD = "imap-secret"
POP3_PASSWORD = "pop3-secret"

GITHUB_DEVICE_MAIL = (
    "Hey there,\n"
    "\n"
    "A sign in attempt requires further verification because we did not recognize "
    "your device. To complete the sign in, enter the verification code on the "
    "unrecognized device.\n"
    "\n"
    "Verification code: 481920\n"
    "\n"
    "If you did not attempt to sign in, your account may be at risk.\n"
)


def make_raw_mail(
    subject: str = "[GitHub] Please verify your device",
    sender: str = "GitHub <noreply@github.com>",
    body: str = GITHUB_DEVICE_MAIL,
    date: str = "Thu, 04 Oct 2026 09:30:00 +0800",
    html: bool = False,
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "bot@example.com"
    message["Date"] = date
    if html:
        message.set_content("请使用支持 HTML 的客户端查看")
        message.add_alternative(
            "<html><body><p>Verification code: 481920</p></body></html>",
            subtype="html",
        )
    else:
        message.set_content(body)
    return message.as_bytes()


# --------------------------------------------------------------------------- #
# 假 IMAP 服务器                                                               #
# --------------------------------------------------------------------------- #
def _dot_stuff(raw: bytes) -> bytes:
    out = bytearray()
    for line in raw.split(b"\n"):
        line = line.rstrip(b"\r")
        if line.startswith(b"."):
            line = b"." + line
        out += line + b"\r\n"
    return bytes(out)


class _FakeIMAPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:  # noqa: C901 - 假服务器分支本来就多
        messages: List[bytes] = self.server.messages  # type: ignore[attr-defined]
        self.wfile.write(b"* OK [CAPABILITY IMAP4rev1] fake IMAP ready\r\n")

        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            parts = text.split(" ", 2)
            if len(parts) < 2:
                continue
            tag, command = parts[0], parts[1].upper()
            rest = parts[2] if len(parts) > 2 else ""

            if command == "CAPABILITY":
                self._untagged("* CAPABILITY IMAP4rev1")
                self._ok(tag, "CAPABILITY completed")
            elif command == "LOGIN":
                if self.server.password in rest:  # type: ignore[attr-defined]
                    self._ok(tag, "LOGIN completed")
                else:
                    self.wfile.write(f"{tag} NO bad credentials\r\n".encode())
            elif command in ("SELECT", "EXAMINE"):
                self._untagged(f"* {len(messages)} EXISTS")
                self._untagged("* 0 RECENT")
                self._untagged("* FLAGS (\\Seen \\Deleted)")
                self._ok(tag, "SELECT completed")
            elif command == "SEARCH":
                numbers = " ".join(str(i + 1) for i in range(len(messages)))
                self._untagged(f"* SEARCH {numbers}".rstrip())
                self._ok(tag, "SEARCH completed")
            elif command == "FETCH":
                number = int(rest.split()[0])
                raw = messages[number - 1]
                self.wfile.write(
                    f"* {number} FETCH (RFC822 {{{len(raw)}}}\r\n".encode()
                )
                self.wfile.write(raw)
                self.wfile.write(b")\r\n")
                self._ok(tag, "FETCH completed")
            elif command in ("STORE", "EXPUNGE", "CLOSE"):
                self._ok(tag, f"{command} completed")
            elif command == "LOGOUT":
                self._untagged("* BYE")
                self._ok(tag, "LOGOUT completed")
                return
            else:
                self._ok(tag, f"{command} completed")

    def _untagged(self, text: str) -> None:
        self.wfile.write(f"{text}\r\n".encode())

    def _ok(self, tag: str, text: str) -> None:
        self.wfile.write(f"{tag} OK {text}\r\n".encode())


# --------------------------------------------------------------------------- #
# 假 POP3 服务器                                                               #
# --------------------------------------------------------------------------- #
class _FakePOP3Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        messages: List[bytes] = self.server.messages  # type: ignore[attr-defined]
        self.wfile.write(b"+OK fake POP3 ready\r\n")

        while True:
            line = self.rfile.readline()
            if not line:
                return
            parts = line.decode("utf-8", "replace").split()
            if not parts:
                continue
            command = parts[0].upper()

            if command == "USER":
                self.wfile.write(b"+OK user accepted\r\n")
            elif command == "PASS":
                if len(parts) > 1 and parts[1] == self.server.password:  # type: ignore[attr-defined]
                    self.wfile.write(b"+OK mailbox ready\r\n")
                else:
                    self.wfile.write(b"-ERR bad credentials\r\n")
            elif command == "STAT":
                size = sum(len(m) for m in messages)
                self.wfile.write(f"+OK {len(messages)} {size}\r\n".encode())
            elif command == "LIST":
                self.wfile.write(b"+OK scan listing follows\r\n")
                for index, raw in enumerate(messages, start=1):
                    self.wfile.write(f"{index} {len(raw)}\r\n".encode())
                self.wfile.write(b".\r\n")
            elif command == "RETR":
                index = int(parts[1])
                raw = messages[index - 1]
                self.wfile.write(b"+OK message follows\r\n")
                self.wfile.write(_dot_stuff(raw))
                self.wfile.write(b".\r\n")
            elif command == "DELE":
                self.wfile.write(b"+OK message deleted\r\n")
            elif command == "QUIT":
                self.wfile.write(b"+OK bye\r\n")
                return
            else:
                self.wfile.write(b"-ERR unknown command\r\n")


class _FakeServer:
    def __init__(self, handler: Any, messages: List[bytes], password: str) -> None:
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.server.messages = messages  # type: ignore[attr-defined]
        self.server.password = password  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_FakeServer":
        self.thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])


# --------------------------------------------------------------------------- #
# 验证码提取                                                                   #
# --------------------------------------------------------------------------- #
def test_extract_verification_code_variants():
    assert mail_mod.extract_verification_code(GITHUB_DEVICE_MAIL) == "481920"
    assert mail_mod.extract_verification_code("Your one-time code is 135790") == "135790"
    assert mail_mod.extract_verification_code("验证码：246810，请勿泄露") == "246810"
    assert mail_mod.extract_verification_code("<p>Code: <b>998877</b></p>") == "998877"
    assert mail_mod.extract_verification_code("随便一封 20261004 的邮件") is None
    assert mail_mod.extract_verification_code("") is None


def test_parse_mail_plain_and_html():
    plain = mail_mod.parse_mail(make_raw_mail())
    assert plain.subject == "[GitHub] Please verify your device"
    assert "github.com" in plain.sender
    assert "481920" in plain.text_for_code()

    html_only = mail_mod.parse_mail(
        make_raw_mail(subject="[GitHub] Verify", html=True)
    )
    assert mail_mod.extract_verification_code(html_only.text_for_code()) == "481920"


# --------------------------------------------------------------------------- #
# IMAP / POP3 端到端                                                           #
# --------------------------------------------------------------------------- #
def _config(server: _FakeServer, protocol: str, password: str, **kwargs: Any):
    return MailboxConfig(
        host="127.0.0.1",
        username="bot@example.com",
        password=password,
        protocol=protocol,
        port=server.port,
        use_ssl=False,
        timeout=5,
        **kwargs,
    )


def test_imap_fetch_and_code():
    async def main() -> None:
        with _FakeServer(_FakeIMAPHandler, [make_raw_mail()], IMAP_PASSWORD) as server:
            mailbox = IMAPMailbox(_config(server, "imap", IMAP_PASSWORD))
            messages = await mailbox.fetch_messages_async()
            assert len(messages) == 1
            assert "481920" in messages[0].text_for_code()

            code = await mail_mod.fetch_verification_code(
                mailbox, timeout=2, interval=0.2
            )
            assert code == "481920"

    asyncio.run(main())


def test_pop3_fetch_and_code():
    async def main() -> None:
        with _FakeServer(_FakePOP3Handler, [make_raw_mail()], POP3_PASSWORD) as server:
            mailbox = POP3Mailbox(_config(server, "pop3", POP3_PASSWORD))
            messages = await mailbox.fetch_messages_async()
            assert len(messages) == 1
            assert messages[0].subject == "[GitHub] Please verify your device"

            code = await mail_mod.fetch_verification_code(
                mailbox, timeout=2, interval=0.2
            )
            assert code == "481920"

    asyncio.run(main())


def test_skips_non_github_mail_and_times_out():
    async def main() -> None:
        other = make_raw_mail(sender="spam@example.com", body="Verification code: 111111")
        with _FakeServer(_FakePOP3Handler, [other], POP3_PASSWORD) as server:
            mailbox = POP3Mailbox(_config(server, "pop3", POP3_PASSWORD))
            try:
                await mail_mod.fetch_verification_code(
                    mailbox, timeout=0.6, interval=0.2
                )
            except mail_mod.MailboxCodeNotFound:
                pass
            else:  # pragma: no cover - 失败路径
                raise AssertionError("应当抛出 MailboxCodeNotFound")

    asyncio.run(main())


def test_wrong_password_reports_auth_error():
    async def main() -> None:
        with _FakeServer(_FakePOP3Handler, [make_raw_mail()], POP3_PASSWORD) as server:
            mailbox = POP3Mailbox(_config(server, "pop3", "wrong-password"))
            try:
                await mailbox.fetch_messages_async()
            except mail_mod.MailboxAuthError:
                pass
            else:  # pragma: no cover - 失败路径
                raise AssertionError("应当抛出 MailboxAuthError")

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# SSL / STARTTLS 接线                                                          #
# --------------------------------------------------------------------------- #
def test_imap_ssl_wiring(monkeypatch):
    calls: Dict[str, Any] = {}

    class FakeIMAP4SSL:
        def __init__(self, host, port, ssl_context=None, timeout=None):
            calls["ssl"] = (host, port, ssl_context is not None, timeout)

        def login(self, user, password):
            return "OK", [b""]

        def select(self, folder, readonly=False):
            return "OK", [b"0"]

        def search(self, charset, *criteria):
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

        def shutdown(self):
            pass

    monkeypatch.setattr(mail_mod.imaplib, "IMAP4_SSL", FakeIMAP4SSL)
    mailbox = IMAPMailbox(
        MailboxConfig(host="imap.example.com", username="u", password="p")
    )
    assert mailbox.fetch_messages() == []
    assert calls["ssl"][:3] == ("imap.example.com", 993, True)
    if sys.version_info >= (3, 9):
        # imaplib 的 timeout 参数是 3.9+ 才有的
        assert calls["ssl"][3] == 30.0


def test_imap_starttls_wiring(monkeypatch):
    calls: Dict[str, Any] = {}

    class FakeIMAP4:
        def __init__(self, host, port, timeout=None):
            calls["plain"] = (host, port, timeout)

        def starttls(self, ssl_context=None):
            calls["starttls"] = ssl_context is not None

        def login(self, user, password):
            return "OK", [b""]

        def select(self, folder, readonly=False):
            return "OK", [b"0"]

        def search(self, charset, *criteria):
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

        def shutdown(self):
            pass

    monkeypatch.setattr(mail_mod.imaplib, "IMAP4", FakeIMAP4)
    mailbox = IMAPMailbox(
        MailboxConfig(
            host="imap.example.com",
            username="u",
            password="p",
            use_ssl=False,
            starttls=True,
        )
    )
    mailbox.fetch_messages()
    assert calls["plain"][:2] == ("imap.example.com", 143)
    if sys.version_info >= (3, 9):
        assert calls["plain"][2] == 30.0
    assert calls["starttls"] is True


def test_pop3_ssl_wiring(monkeypatch):
    calls: Dict[str, Any] = {}

    class FakePOP3SSL:
        def __init__(self, host, port, timeout=None, context=None):
            calls["ssl"] = (host, port, timeout, context is not None)

        def user(self, name):
            return b"+OK"

        def pass_(self, password):
            return b"+OK"

        def list(self):
            return b"+OK", [], 0

        def quit(self):
            return b"+OK"

    monkeypatch.setattr(mail_mod.poplib, "POP3_SSL", FakePOP3SSL)
    mailbox = POP3Mailbox(
        MailboxConfig(host="pop.example.com", username="u", password="p", protocol="pop3")
    )
    assert mailbox.fetch_messages() == []
    assert calls["ssl"] == ("pop.example.com", 995, 30.0, True)


def test_build_mailbox_rejects_unknown_protocol():
    try:
        mail_mod.build_mailbox(
            MailboxConfig(host="h", username="u", password="p", protocol="smtp")
        )
    except mail_mod.MailboxError as exc:
        assert "不支持的收信协议" in str(exc)
    else:  # pragma: no cover - 失败路径
        raise AssertionError("应当抛出 MailboxError")
