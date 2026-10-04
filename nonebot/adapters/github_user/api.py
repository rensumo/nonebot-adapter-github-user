"""GitHub REST API 客户端：开 PR、评论、评审、合并、推文件。

走 ``api.github.com`` + ``Authorization: Bearer <token>``，token 可以来自
PAT（``gh auth token``）或 :mod:`oauth` 里的设备流。权限要求：

- ``repo``：仓库读写（推分支、开 PR、评论、合并都靠它）
- ``workflow``：需要改 ``.github/workflows/`` 下的文件时才要

不含 git 二进制依赖：提交文件走 Git Data API（blobs → trees → commits → refs）。
本模块不依赖 NoneBot，可单独使用。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional
from urllib.parse import quote

import httpx

log = logging.getLogger("nonebot.adapters.github_user")

DEFAULT_API_BASE_URL = "https://api.github.com"
DEFAULT_USER_AGENT = "nonebot-adapter-github-user"
API_VERSION = "2022-11-28"

__all__ = [
    "DEFAULT_API_BASE_URL",
    "GitHubAPI",
    "GitHubAPIError",
    "TokenProvider",
    "get_github_api",
    "set_default_api",
]

TokenProvider = Callable[..., Awaitable[str]]
"""返回 access token 的协程；支持 ``force=True`` 表示强制刷新。"""

_default_api: Optional["GitHubAPI"] = None


def set_default_api(api: Optional["GitHubAPI"]) -> None:
    """由适配器在启动时登记，供插件用 :func:`get_github_api` 取用。"""

    global _default_api
    _default_api = api


def get_github_api() -> "GitHubAPI":
    """取当前适配器登记的 API 客户端。"""

    if _default_api is None:
        raise GitHubAPIError(
            0,
            "GitHub API 客户端尚未就绪：请配置 GITHUB_USER_TOKEN 或 "
            "GITHUB_USER_OAUTH_CLIENT_ID，并确认适配器已启动",
        )
    return _default_api


class GitHubAPIError(Exception):
    """API 调用失败（HTTP >= 400）。"""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        url: str = "",
        payload: Optional[Any] = None,
    ) -> None:
        super().__init__(f"GitHub API {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.url = url
        self.payload = payload

    @property
    def is_not_found(self) -> bool:
        return self.status_code == 404

    @property
    def is_forbidden(self) -> bool:
        return self.status_code == 403

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429 or (
            self.status_code == 403 and "rate limit" in self.message.lower()
        )


class GitHubAPI:
    """一层薄封装，覆盖机器人做 PR 需要的能力。"""

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        token_provider: Optional[TokenProvider] = None,
        api_base_url: str = DEFAULT_API_BASE_URL,
        timeout: float = 30.0,
        proxy: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        client: Optional[httpx.AsyncClient] = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        if not token and token_provider is None:
            raise ValueError("必须提供 token 或 token_provider")
        self.token = token
        self.token_provider = token_provider
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = timeout
        self.proxy = proxy
        self.transport = transport
        self.user_agent = user_agent
        self._client = client

    def __repr__(self) -> str:
        mode = "static-token" if self.token else "token-provider"
        return f"<GitHubAPI {self.api_base_url} ({mode})>"

    # ------------------------------------------------------------------ #
    # 底层请求                                                            #
    # ------------------------------------------------------------------ #
    async def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: Dict[str, Any] = {
                "timeout": httpx.Timeout(self.timeout),
                "headers": {
                    "User-Agent": self.user_agent,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
            }
            if self.transport is not None:
                kwargs["transport"] = self.transport
            if self.proxy:
                kwargs["proxy"] = self.proxy
            try:
                self._client = httpx.AsyncClient(**kwargs)
            except TypeError:
                if "proxy" not in kwargs:
                    raise
                kwargs["proxies"] = kwargs.pop("proxy")
                self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _access_token(self, *, force: bool = False) -> str:
        if self.token_provider is None:
            assert self.token is not None
            return self.token
        return await self.token_provider(force=force)

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.api_base_url}/{path.lstrip('/')}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Optional[Any] = None,
        headers: Optional[Mapping[str, str]] = None,
        accept: Optional[str] = None,
        raw_text: bool = False,
        retry_on_unauthorized: bool = True,
    ) -> Any:
        """发一次请求；401 时会强制刷新 token 再重试一次。"""

        client = await self._client_or_create()
        url = self._url(path)
        force = False
        for attempt in range(2):
            token = await self._access_token(force=force)
            request_headers: Dict[str, str] = {"Authorization": f"Bearer {token}"}
            if accept:
                request_headers["Accept"] = accept
            if headers:
                request_headers.update(headers)
            try:
                response = await client.request(
                    method.upper(),
                    url,
                    params=params,
                    json=json,
                    headers=request_headers,
                )
            except httpx.HTTPError as exc:
                raise GitHubAPIError(
                    0, f"请求失败：{type(exc).__name__}: {exc}", url=url
                ) from exc

            if (
                response.status_code == 401
                and retry_on_unauthorized
                and attempt == 0
                and self.token_provider is not None
            ):
                log.warning("GitHub 返回 401，尝试刷新 token 后重试：%s", url)
                force = True
                continue
            if response.status_code >= 400:
                raise self._error_from_response(response, url)
            if raw_text:
                return response.text
            if not response.content:
                return None
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                try:
                    return response.json()
                except ValueError:
                    return response.text
            return response.text
        raise GitHubAPIError(401, "刷新 token 后仍然鉴权失败", url=url)

    @staticmethod
    def _error_from_response(response: httpx.Response, url: str) -> GitHubAPIError:
        payload: Any = None
        message = f"HTTP {response.status_code}"
        try:
            payload = response.json()
        except ValueError:
            payload = response.text[:500]
        if isinstance(payload, dict):
            message = str(payload.get("message") or message)
            errors = payload.get("errors")
            if errors:
                message = f"{message}（{errors}）"
        elif payload:
            message = f"{message}：{payload}"
        return GitHubAPIError(
            response.status_code, message, url=url, payload=payload
        )

    async def get_authenticated_user(self) -> Dict[str, Any]:
        """``GET /user``：确认 token 身份。"""

        return await self.request("GET", "/user")

    # ------------------------------------------------------------------ #
    # PR / 评论 / 评审 / 合并                                              #
    # ------------------------------------------------------------------ #
    async def create_pull_request(
        self,
        repo: str,
        *,
        title: str,
        head: str,
        base: str,
        body: Optional[str] = None,
        draft: bool = False,
        maintainer_can_modify: bool = True,
    ) -> Dict[str, Any]:
        """开 PR。``head`` 是源分支（同仓库就用分支名，跨仓库用 ``owner:branch``）。"""

        payload: Dict[str, Any] = {
            "title": title,
            "head": head,
            "base": base,
            "draft": draft,
            "maintainer_can_modify": maintainer_can_modify,
        }
        if body is not None:
            payload["body"] = body
        return await self.request("POST", f"/repos/{repo}/pulls", json=payload)

    async def get_pull_request(self, repo: str, number: int) -> Dict[str, Any]:
        return await self.request("GET", f"/repos/{repo}/pulls/{number}")

    async def list_pull_requests(
        self,
        repo: str,
        *,
        state: str = "open",
        head: Optional[str] = None,
        base: Optional[str] = None,
        per_page: int = 30,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"state": state, "per_page": per_page}
        if head:
            params["head"] = head
        if base:
            params["base"] = base
        return await self.request("GET", f"/repos/{repo}/pulls", params=params)

    async def update_pull_request(
        self, repo: str, number: int, **fields: Any
    ) -> Dict[str, Any]:
        """改标题/正文/状态（``state="closed"`` 即关闭）等。"""

        return await self.request("PATCH", f"/repos/{repo}/pulls/{number}", json=fields)

    async def get_pull_request_diff(self, repo: str, number: int) -> str:
        return await self.request(
            "GET",
            f"/repos/{repo}/pulls/{number}",
            accept="application/vnd.github.v3.diff",
            raw_text=True,
        )

    async def comment_issue(self, repo: str, number: int, body: str) -> Dict[str, Any]:
        """在 issue 或 PR 下发一条普通评论（PR 也是 issue）。"""

        return await self.request(
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            json={"body": body},
        )

    # 语义化别名，读代码时更直观
    comment_pull_request = comment_issue

    async def comment_pull_request_line(
        self,
        repo: str,
        number: int,
        body: str,
        *,
        path: str,
        line: int,
        side: str = "RIGHT",
        start_line: Optional[int] = None,
        commit_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """在 PR 的某一行上提行内评论。"""

        payload: Dict[str, Any] = {
            "body": body,
            "path": path,
            "line": line,
            "side": side,
        }
        if start_line is not None:
            payload["start_line"] = start_line
        if commit_id:
            payload["commit_id"] = commit_id
        return await self.request(
            "POST", f"/repos/{repo}/pulls/{number}/comments", json=payload
        )

    async def review_pull_request(
        self,
        repo: str,
        number: int,
        *,
        event: str = "COMMENT",
        body: Optional[str] = None,
        comments: Optional[List[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """提交评审：``event`` 取 ``APPROVE`` / ``REQUEST_CHANGES`` / ``COMMENT``。

        注意：GitHub 不允许批准自己开的 PR（会返回 422）。
        """

        payload: Dict[str, Any] = {"event": event.upper()}
        if body is not None:
            payload["body"] = body
        if comments:
            payload["comments"] = list(comments)
        return await self.request(
            "POST", f"/repos/{repo}/pulls/{number}/reviews", json=payload
        )

    async def merge_pull_request(
        self,
        repo: str,
        number: int,
        *,
        method: str = "merge",
        commit_title: Optional[str] = None,
        commit_message: Optional[str] = None,
        sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """合并 PR；``method`` 取 ``merge`` / ``squash`` / ``rebase``。"""

        payload: Dict[str, Any] = {"merge_method": method}
        if commit_title:
            payload["commit_title"] = commit_title
        if commit_message:
            payload["commit_message"] = commit_message
        if sha:
            payload["sha"] = sha
        return await self.request(
            "PUT", f"/repos/{repo}/pulls/{number}/merge", json=payload
        )

    async def add_labels(
        self, repo: str, number: int, labels: Iterable[str]
    ) -> List[Dict[str, Any]]:
        return await self.request(
            "POST",
            f"/repos/{repo}/issues/{number}/labels",
            json={"labels": list(labels)},
        )

    async def request_reviewers(
        self,
        repo: str,
        number: int,
        *,
        reviewers: Iterable[str] = (),
        team_reviewers: Iterable[str] = (),
    ) -> Dict[str, Any]:
        payload = {
            "reviewers": list(reviewers),
            "team_reviewers": list(team_reviewers),
        }
        return await self.request(
            "POST", f"/repos/{repo}/pulls/{number}/requested_reviewers", json=payload
        )

    async def close_pull_request(self, repo: str, number: int) -> Dict[str, Any]:
        return await self.update_pull_request(repo, number, state="closed")

    # ------------------------------------------------------------------ #
    # 分支与提交（Git Data API，不需要 git 二进制）                          #
    # ------------------------------------------------------------------ #
    async def get_branch_sha(self, repo: str, branch: str) -> str:
        data = await self.request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        return str(data["object"]["sha"])

    async def try_get_branch_sha(self, repo: str, branch: str) -> Optional[str]:
        try:
            return await self.get_branch_sha(repo, branch)
        except GitHubAPIError as exc:
            if exc.is_not_found:
                return None
            raise

    async def create_branch(
        self,
        repo: str,
        branch: str,
        *,
        from_branch: Optional[str] = None,
        from_sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """新建分支；已有同名分支时直接返回它。"""

        existing = await self.try_get_branch_sha(repo, branch)
        if existing:
            return {"ref": f"refs/heads/{branch}", "object": {"sha": existing}}
        sha = from_sha or (await self.get_branch_sha(repo, from_branch or "main"))
        return await self.request(
            "POST",
            f"/repos/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )

    async def get_contents(
        self, repo: str, path: str, *, ref: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """读文件内容；不存在返回 None。"""

        params = {"ref": ref} if ref else None
        try:
            return await self.request(
                "GET", f"/repos/{repo}/contents/{quote(path)}", params=params
            )
        except GitHubAPIError as exc:
            if exc.is_not_found:
                return None
            raise

    async def commit_files(
        self,
        repo: str,
        *,
        files: Mapping[str, Optional[str]],
        message: str,
        branch: str,
        base_branch: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把若干文件写进 ``branch``（不存在则从 ``base_branch`` 拉一个），返回新的 commit。

        ``files`` 的值给 ``None`` 表示删除该文件。走 Git Data API，
        纯 HTTP，不需要本地 git。
        """

        if not files:
            raise ValueError("files 不能为空")

        existing_sha = await self.try_get_branch_sha(repo, branch)
        if existing_sha is None:
            parent_sha = await self.get_branch_sha(repo, base_branch or "main")
        else:
            parent_sha = existing_sha

        parent_commit = await self.request(
            "GET", f"/repos/{repo}/git/commits/{parent_sha}"
        )
        base_tree = str(parent_commit["tree"]["sha"])

        entries: List[Dict[str, Any]] = []
        for path, content in files.items():
            if content is None:
                entries.append(
                    {
                        "path": path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": None,
                    }
                )
                continue
            blob = await self.request(
                "POST",
                f"/repos/{repo}/git/blobs",
                json={"content": content, "encoding": "utf-8"},
            )
            entries.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )

        tree = await self.request(
            "POST",
            f"/repos/{repo}/git/trees",
            json={"base_tree": base_tree, "tree": entries},
        )
        commit = await self.request(
            "POST",
            f"/repos/{repo}/git/commits",
            json={
                "message": message,
                "tree": tree["sha"],
                "parents": [parent_sha],
            },
        )

        if existing_sha is None:
            await self.request(
                "POST",
                f"/repos/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )
        else:
            await self.request(
                "PATCH",
                f"/repos/{repo}/git/refs/heads/{branch}",
                json={"sha": commit["sha"]},
            )
        return commit

    async def open_pull_request_with_files(
        self,
        repo: str,
        *,
        files: Mapping[str, Optional[str]],
        title: str,
        branch: str,
        base_branch: str = "main",
        body: Optional[str] = None,
        commit_message: Optional[str] = None,
        draft: bool = False,
    ) -> Dict[str, Any]:
        """一把梭：提交文件 → 开 PR。"""

        await self.commit_files(
            repo,
            files=files,
            message=commit_message or title,
            branch=branch,
            base_branch=base_branch,
        )
        return await self.create_pull_request(
            repo,
            title=title,
            head=branch,
            base=base_branch,
            body=body,
            draft=draft,
        )
