"""GitHub REST API 客户端测试（httpx.MockTransport，不联网）。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx

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


api_mod = _load_module("github_user_api", "api")
GitHubAPI = api_mod.GitHubAPI
GitHubAPIError = api_mod.GitHubAPIError


class Recorder:
    """记录请求并按路径返回预设响应。"""

    def __init__(self, routes: Dict[Tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.headers: List[Dict[str, str]] = []
        self.blob_seq = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: Dict[str, Any] = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = {}
        self.calls.append((request.method, path, body))
        self.headers.append(dict(request.headers))

        if path.endswith("/git/blobs"):
            self.blob_seq += 1
            return httpx.Response(200, json={"sha": f"blob{self.blob_seq}"})

        response = self.routes.get((request.method, path))
        if response is None:
            return httpx.Response(404, json={"message": "Not Found"})
        status, payload = response
        return httpx.Response(status, json=payload)


def make_api(recorder: Recorder, **kwargs: Any) -> Any:
    return GitHubAPI(
        token="gho_test",
        transport=httpx.MockTransport(recorder),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# PR / 评论 / 评审                                                             #
# --------------------------------------------------------------------------- #
def test_create_pull_request_payload_and_headers():
    async def main() -> None:
        recorder = Recorder(
            {("POST", "/repos/o/r/pulls"): (201, {"number": 7, "html_url": "u"})}
        )
        api = make_api(recorder)
        result = await api.create_pull_request(
            "o/r",
            title="标题",
            head="bot/fix",
            base="main",
            body="正文",
            draft=True,
        )
        await api.aclose()

        method, path, body = recorder.calls[0]
        assert (method, path) == ("POST", "/repos/o/r/pulls")
        assert body == {
            "title": "标题",
            "head": "bot/fix",
            "base": "main",
            "draft": True,
            "maintainer_can_modify": True,
            "body": "正文",
        }
        headers = {k.lower(): v for k, v in recorder.headers[0].items()}
        assert headers["authorization"] == "Bearer gho_test"
        assert headers["accept"] == "application/vnd.github+json"
        assert headers["x-github-api-version"] == "2022-11-28"
        assert result["number"] == 7

    asyncio.run(main())


def test_comment_review_and_merge():
    async def main() -> None:
        recorder = Recorder(
            {
                ("POST", "/repos/o/r/issues/7/comments"): (201, {"id": 1}),
                ("POST", "/repos/o/r/pulls/7/comments"): (201, {"id": 2}),
                ("POST", "/repos/o/r/pulls/7/reviews"): (200, {"id": 3}),
                ("PUT", "/repos/o/r/pulls/7/merge"): (200, {"merged": True}),
                ("POST", "/repos/o/r/issues/7/labels"): (200, []),
                ("POST", "/repos/o/r/pulls/7/requested_reviewers"): (201, {}),
                ("PATCH", "/repos/o/r/pulls/7"): (200, {"state": "closed"}),
            }
        )
        api = make_api(recorder)

        await api.comment_issue("o/r", 7, "普通评论")
        await api.comment_pull_request("o/r", 7, "PR 评论")
        await api.comment_pull_request_line(
            "o/r", 7, "行内评论", path="a.py", line=12
        )
        await api.review_pull_request("o/r", 7, event="request_changes", body="改一下")
        await api.merge_pull_request("o/r", 7, method="squash")
        await api.add_labels("o/r", 7, ["bug"])
        await api.request_reviewers("o/r", 7, reviewers=["alice"])
        await api.close_pull_request("o/r", 7)
        await api.aclose()

        paths = [(method, path) for method, path, _ in recorder.calls]
        assert paths == [
            ("POST", "/repos/o/r/issues/7/comments"),
            ("POST", "/repos/o/r/issues/7/comments"),
            ("POST", "/repos/o/r/pulls/7/comments"),
            ("POST", "/repos/o/r/pulls/7/reviews"),
            ("PUT", "/repos/o/r/pulls/7/merge"),
            ("POST", "/repos/o/r/issues/7/labels"),
            ("POST", "/repos/o/r/pulls/7/requested_reviewers"),
            ("PATCH", "/repos/o/r/pulls/7"),
        ]
        assert recorder.calls[2][2] == {
            "body": "行内评论",
            "path": "a.py",
            "line": 12,
            "side": "RIGHT",
        }
        assert recorder.calls[3][2]["event"] == "REQUEST_CHANGES"
        assert recorder.calls[4][2] == {"merge_method": "squash"}
        assert recorder.calls[5][2] == {"labels": ["bug"]}
        assert recorder.calls[6][2] == {"reviewers": ["alice"], "team_reviewers": []}
        assert recorder.calls[7][2] == {"state": "closed"}

    asyncio.run(main())


def test_get_pull_request_diff_uses_diff_accept_header():
    async def main() -> None:
        seen: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "application/vnd.github.v3.diff"},
                text="diff --git a/x b/x",
            )

        api = GitHubAPI(token="t", transport=httpx.MockTransport(handler))
        diff = await api.get_pull_request_diff("o/r", 7)
        await api.aclose()
        assert diff.startswith("diff --git")
        assert seen[0].headers["accept"] == "application/vnd.github.v3.diff"

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 错误与 401 重试                                                              #
# --------------------------------------------------------------------------- #
def test_error_mapping():
    async def main() -> None:
        recorder = Recorder(
            {
                ("GET", "/repos/o/missing"): (404, {"message": "Not Found"}),
                ("GET", "/repos/o/limited"): (
                    403,
                    {"message": "API rate limit exceeded for user ID 1."},
                ),
                ("POST", "/repos/o/r/pulls/7/reviews"): (
                    422,
                    {"message": "Can not approve your own pull request"},
                ),
            }
        )
        api = make_api(recorder)

        try:
            await api.request("GET", "/repos/o/missing")
        except GitHubAPIError as exc:
            assert exc.is_not_found and exc.status_code == 404
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 GitHubAPIError")

        try:
            await api.request("GET", "/repos/o/limited")
        except GitHubAPIError as exc:
            assert exc.is_rate_limited
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 GitHubAPIError")

        try:
            await api.review_pull_request("o/r", 7, event="APPROVE")
        except GitHubAPIError as exc:
            assert "approve your own pull request" in exc.message
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 GitHubAPIError")
        await api.aclose()

    asyncio.run(main())


def test_401_refreshes_token_once():
    async def main() -> None:
        calls: List[bool] = []
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(200, json={"login": "bot"})

        async def provider(force: bool = False) -> str:
            calls.append(force)
            return "gho_new" if force else "gho_old"

        api = GitHubAPI(
            token_provider=provider,
            transport=httpx.MockTransport(handler),
        )
        user = await api.get_authenticated_user()
        await api.aclose()
        assert user["login"] == "bot"
        assert calls == [False, True]

    asyncio.run(main())


def test_401_without_refresh_raises():
    async def main() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Bad credentials"})

        api = GitHubAPI(token="bad", transport=httpx.MockTransport(handler))
        try:
            await api.get_authenticated_user()
        except GitHubAPIError as exc:
            assert exc.status_code == 401
        else:  # pragma: no cover - 失败路径
            raise AssertionError("应当抛出 GitHubAPIError")
        await api.aclose()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 提交文件（Git Data API）                                                      #
# --------------------------------------------------------------------------- #
def test_commit_files_creates_branch_and_commit():
    async def main() -> None:
        recorder = Recorder(
            {
                ("GET", "/repos/o/r/git/ref/heads/bot/feature"): (
                    404,
                    {"message": "Not Found"},
                ),
                ("GET", "/repos/o/r/git/ref/heads/main"): (
                    200,
                    {"object": {"sha": "parent-sha"}},
                ),
                ("GET", "/repos/o/r/git/commits/parent-sha"): (
                    200,
                    {"tree": {"sha": "base-tree"}},
                ),
                ("POST", "/repos/o/r/git/trees"): (201, {"sha": "new-tree"}),
                ("POST", "/repos/o/r/git/commits"): (201, {"sha": "new-commit"}),
                ("POST", "/repos/o/r/git/refs"): (201, {"ref": "refs/heads/bot/feature"}),
            }
        )
        api = make_api(recorder)
        commit = await api.commit_files(
            "o/r",
            files={"docs/a.md": "# hi", "docs/gone.md": None},
            message="docs: 更新",
            branch="bot/feature",
            base_branch="main",
        )
        await api.aclose()

        paths = [(method, path) for method, path, _ in recorder.calls]
        assert paths == [
            ("GET", "/repos/o/r/git/ref/heads/bot/feature"),
            ("GET", "/repos/o/r/git/ref/heads/main"),
            ("GET", "/repos/o/r/git/commits/parent-sha"),
            ("POST", "/repos/o/r/git/blobs"),
            ("POST", "/repos/o/r/git/trees"),
            ("POST", "/repos/o/r/git/commits"),
            ("POST", "/repos/o/r/git/refs"),
        ]
        assert recorder.calls[3][2] == {"content": "# hi", "encoding": "utf-8"}
        tree = recorder.calls[4][2]
        assert tree["base_tree"] == "base-tree"
        assert tree["tree"][0] == {
            "path": "docs/a.md",
            "mode": "100644",
            "type": "blob",
            "sha": "blob1",
        }
        assert tree["tree"][1]["sha"] is None  # 删除文件
        commit_payload = recorder.calls[5][2]
        assert commit_payload["parents"] == ["parent-sha"]
        assert commit_payload["message"] == "docs: 更新"
        assert recorder.calls[6][2] == {
            "ref": "refs/heads/bot/feature",
            "sha": "new-commit",
        }
        assert commit["sha"] == "new-commit"

    asyncio.run(main())


def test_commit_files_updates_existing_branch():
    async def main() -> None:
        recorder = Recorder(
            {
                ("GET", "/repos/o/r/git/ref/heads/bot/feature"): (
                    200,
                    {"object": {"sha": "head-sha"}},
                ),
                ("GET", "/repos/o/r/git/commits/head-sha"): (
                    200,
                    {"tree": {"sha": "tree-sha"}},
                ),
                ("POST", "/repos/o/r/git/trees"): (201, {"sha": "t2"}),
                ("POST", "/repos/o/r/git/commits"): (201, {"sha": "c2"}),
                ("PATCH", "/repos/o/r/git/refs/heads/bot/feature"): (200, {}),
            }
        )
        api = make_api(recorder)
        await api.commit_files(
            "o/r", files={"a.txt": "x"}, message="m", branch="bot/feature"
        )
        await api.aclose()
        methods = [method for method, _, _ in recorder.calls]
        assert methods[-1] == "PATCH"
        assert recorder.calls[-1][2] == {"sha": "c2"}

    asyncio.run(main())


def test_open_pull_request_with_files():
    async def main() -> None:
        recorder = Recorder(
            {
                ("GET", "/repos/o/r/git/ref/heads/bot/fix"): (404, {"message": "Not Found"}),
                ("GET", "/repos/o/r/git/ref/heads/main"): (
                    200,
                    {"object": {"sha": "p"}},
                ),
                ("GET", "/repos/o/r/git/commits/p"): (200, {"tree": {"sha": "t"}}),
                ("POST", "/repos/o/r/git/trees"): (201, {"sha": "t2"}),
                ("POST", "/repos/o/r/git/commits"): (201, {"sha": "c"}),
                ("POST", "/repos/o/r/git/refs"): (201, {}),
                ("POST", "/repos/o/r/pulls"): (201, {"number": 9}),
            }
        )
        api = make_api(recorder)
        pr = await api.open_pull_request_with_files(
            "o/r",
            files={"a.txt": "x"},
            title="标题",
            branch="bot/fix",
            base_branch="main",
            body="正文",
        )
        await api.aclose()
        assert pr["number"] == 9
        assert recorder.calls[-1][1] == "/repos/o/r/pulls"

    asyncio.run(main())


def test_get_contents_returns_none_on_404():
    async def main() -> None:
        recorder = Recorder({})
        api = make_api(recorder)
        assert await api.get_contents("o/r", "nope.txt") is None
        await api.aclose()

    asyncio.run(main())


def test_default_api_registry():
    api_mod.set_default_api(None)
    try:
        api_mod.get_github_api()
    except GitHubAPIError as exc:
        assert "尚未就绪" in str(exc)
    else:  # pragma: no cover - 失败路径
        raise AssertionError("应当抛出 GitHubAPIError")

    api = GitHubAPI(token="t")
    api_mod.set_default_api(api)
    assert api_mod.get_github_api() is api
    api_mod.set_default_api(None)
