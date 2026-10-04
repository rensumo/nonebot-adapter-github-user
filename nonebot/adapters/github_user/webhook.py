"""GitHub Webhook 接收端：签名校验、事件解析、回复目标推断。

纯函数模块，不依赖 NoneBot，方便单独测试：

.. code-block:: python

    payload = parse_webhook(request.headers, request.body)
    if not verify_signature(secret, request.body, headers.get("x-hub-signature-256")):
        raise WebhookSignatureError("签名不对")

GitHub 会发这些关键请求头：

- ``X-Hub-Signature-256``：``sha256=<HMAC-SHA256(secret, body) 的十六进制>``
- ``X-GitHub-Event``：事件名，如 ``pull_request`` / ``issue_comment``
- ``X-GitHub-Delivery``：投递 id，可用于去重
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

SIGNATURE_HEADER = "x-hub-signature-256"
EVENT_HEADER = "x-github-event"
DELIVERY_HEADER = "x-github-delivery"

__all__ = [
    "DELIVERY_HEADER",
    "EVENT_HEADER",
    "SIGNATURE_HEADER",
    "ReplyTarget",
    "WebhookError",
    "WebhookPayload",
    "WebhookSignatureError",
    "get_header",
    "parse_webhook",
    "reply_target",
    "sign_payload",
    "verify_signature",
]


class WebhookError(Exception):
    """Webhook 相关错误。"""


class WebhookSignatureError(WebhookError):
    """签名缺失或不匹配。"""


def get_header(headers: Mapping[str, Any], name: str) -> Optional[str]:
    """按大小写不敏感的方式取请求头。"""

    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return None if value is None else str(value)
    return None


def sign_payload(secret: str, body: bytes) -> str:
    """算出 ``X-Hub-Signature-256`` 的值（自检、测试用）。"""

    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, signature: Optional[str]) -> bool:
    """校验 ``X-Hub-Signature-256``。"""

    if not signature or not signature.startswith("sha256="):
        return False
    expected = sign_payload(secret, body)[len("sha256=") :]
    provided = signature[len("sha256=") :]
    return hmac.compare_digest(expected, provided)


@dataclass
class WebhookPayload:
    """解析后的 webhook 事件。"""

    event: str
    """``X-GitHub-Event``，如 pull_request。"""

    data: Dict[str, Any] = field(default_factory=dict)
    """原始 JSON 体。"""

    action: Optional[str] = None
    """``data["action"]``，如 opened / created。"""

    delivery_id: Optional[str] = None
    """``X-GitHub-Delivery``，用于去重。"""

    repository: Optional[str] = None
    """``owner/name``。"""

    sender: Optional[str] = None
    """触发者的登录名。"""

    def name(self) -> str:
        """``pull_request.opened`` 形式的事件名。"""

        return f"{self.event}.{self.action}" if self.action else self.event


def parse_webhook(
    headers: Mapping[str, Any], body: bytes
) -> WebhookPayload:
    """把请求头和原始 body 解析成 :class:`WebhookPayload`。"""

    event = get_header(headers, EVENT_HEADER)
    if not event:
        raise WebhookError(f"缺少 {EVENT_HEADER} 请求头")

    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise WebhookError(f"请求体不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise WebhookError("请求体 JSON 顶层不是对象")

    repository = data.get("repository")
    sender = data.get("sender")
    return WebhookPayload(
        event=str(event),
        data=data,
        action=data.get("action") if isinstance(data.get("action"), str) else None,
        delivery_id=get_header(headers, DELIVERY_HEADER),
        repository=(
            repository.get("full_name") if isinstance(repository, dict) else None
        ),
        sender=sender.get("login") if isinstance(sender, dict) else None,
    )


@dataclass
class ReplyTarget:
    """这条事件应该回复到哪里。"""

    repo: str
    kind: str = "issue"
    """``issue``（PR 也走 issue 评论接口）或 ``commit``。"""

    number: Optional[int] = None
    """issue / PR 编号。"""

    sha: Optional[str] = None
    """commit 评论时的提交 SHA。"""


def reply_target(payload: WebhookPayload) -> Optional[ReplyTarget]:
    """从事件里推断回复目标；推断不出来返回 None。

    覆盖 issue / PR 相关事件（走 issue 评论接口）和 commit_comment
    （走 commit 评论接口）。
    """

    data = payload.data
    repository = data.get("repository")
    repo = repository.get("full_name") if isinstance(repository, dict) else None
    if not repo:
        return None

    if payload.event == "commit_comment":
        comment = data.get("comment")
        sha = comment.get("commit_id") if isinstance(comment, dict) else None
        return ReplyTarget(repo=str(repo), kind="commit", sha=sha) if sha else None

    for key in ("issue", "pull_request"):
        item = data.get(key)
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        if isinstance(number, int):
            return ReplyTarget(repo=str(repo), number=number)

    # review_comment 这类事件会带 pull_request，上面已覆盖；
    # discussion 等需要 GraphQL，这里不处理。
    return None
