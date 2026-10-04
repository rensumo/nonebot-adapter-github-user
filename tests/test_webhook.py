"""Webhook 接收端测试：签名校验、事件解析、回复目标推断（不联网）。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict

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


hook = _load_module("github_user_webhook", "webhook")

SECRET = "test-secret"


def make_headers(
    body: bytes, event: str = "pull_request", secret: str = SECRET, **extra: Any
) -> Dict[str, str]:
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": "delivery-1",
        "X-Hub-Signature-256": hook.sign_payload(secret, body),
        "Content-Type": "application/json",
    }
    headers.update(extra)
    return headers


def pull_request_body(action: str = "opened") -> bytes:
    return json.dumps(
        {
            "action": action,
            "number": 7,
            "repository": {"full_name": "rensumo/demo"},
            "sender": {"login": "alice"},
            "pull_request": {"number": 7, "title": "hi"},
        }
    ).encode()


# --------------------------------------------------------------------------- #
# 签名                                                                         #
# --------------------------------------------------------------------------- #
def test_sign_and_verify():
    body = pull_request_body()
    signature = hook.sign_payload(SECRET, body)
    assert signature.startswith("sha256=")
    assert hook.verify_signature(SECRET, body, signature) is True

    assert hook.verify_signature("other-secret", body, signature) is False
    assert hook.verify_signature(SECRET, body + b" ", signature) is False
    assert hook.verify_signature(SECRET, body, None) is False
    assert hook.verify_signature(SECRET, body, "sha1=deadbeef") is False


def test_get_header_is_case_insensitive():
    headers = {"x-hub-signature-256": "sha256=abc", "X-GitHub-Event": "ping"}
    assert hook.get_header(headers, "X-Hub-Signature-256") == "sha256=abc"
    assert hook.get_header(headers, "x-github-event") == "ping"
    assert hook.get_header(headers, "missing") is None


# --------------------------------------------------------------------------- #
# 解析                                                                         #
# --------------------------------------------------------------------------- #
def test_parse_webhook():
    body = pull_request_body()
    payload = hook.parse_webhook(make_headers(body), body)
    assert payload.event == "pull_request"
    assert payload.action == "opened"
    assert payload.repository == "rensumo/demo"
    assert payload.sender == "alice"
    assert payload.delivery_id == "delivery-1"
    assert payload.name() == "pull_request.opened"
    assert payload.data["pull_request"]["title"] == "hi"


def test_parse_webhook_errors():
    body = pull_request_body()
    try:
        hook.parse_webhook({"X-GitHub-Delivery": "x"}, body)
    except hook.WebhookError as exc:
        assert "x-github-event" in str(exc)
    else:  # pragma: no cover - 失败路径
        raise AssertionError("缺少事件头时应当报错")

    try:
        hook.parse_webhook({"X-GitHub-Event": "push"}, b"{not json")
    except hook.WebhookError as exc:
        assert "JSON" in str(exc)
    else:  # pragma: no cover - 失败路径
        raise AssertionError("非法 JSON 应当报错")


def test_parse_webhook_without_action():
    body = json.dumps({"repository": {"full_name": "a/b"}}).encode()
    payload = hook.parse_webhook(make_headers(body, event="push"), body)
    assert payload.action is None
    assert payload.name() == "push"


# --------------------------------------------------------------------------- #
# 回复目标                                                                     #
# --------------------------------------------------------------------------- #
def test_reply_target_for_issue_comment():
    body = json.dumps(
        {
            "action": "created",
            "issue": {"number": 12},
            "repository": {"full_name": "rensumo/demo"},
        }
    ).encode()
    payload = hook.parse_webhook(make_headers(body, event="issue_comment"), body)
    target = hook.reply_target(payload)
    assert target is not None
    assert (target.kind, target.repo, target.number) == ("issue", "rensumo/demo", 12)


def test_reply_target_for_pull_request_and_commit_comment():
    pr_body = pull_request_body()
    pr_payload = hook.parse_webhook(make_headers(pr_body), pr_body)
    pr_target = hook.reply_target(pr_payload)
    assert pr_target is not None and pr_target.number == 7

    commit_body = json.dumps(
        {
            "action": "created",
            "comment": {"commit_id": "abc123"},
            "repository": {"full_name": "rensumo/demo"},
        }
    ).encode()
    commit_payload = hook.parse_webhook(
        make_headers(commit_body, event="commit_comment"), commit_body
    )
    commit_target = hook.reply_target(commit_payload)
    assert commit_target is not None
    assert (commit_target.kind, commit_target.sha) == ("commit", "abc123")


def test_reply_target_returns_none_when_unknown():
    body = json.dumps({"repository": {"full_name": "rensumo/demo"}}).encode()
    payload = hook.parse_webhook(make_headers(body, event="discussion"), body)
    assert hook.reply_target(payload) is None

    no_repo = json.dumps({"action": "created"}).encode()
    payload2 = hook.parse_webhook(make_headers(no_repo, event="issues"), no_repo)
    assert hook.reply_target(payload2) is None
