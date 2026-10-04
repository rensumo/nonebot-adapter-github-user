# nonebot-adapter-github-user

[![PyPI version](https://img.shields.io/pypi/v/nonebot-adapter-github-user.svg)](https://pypi.org/project/nonebot-adapter-github-user/) [![Python versions](https://img.shields.io/pypi/pyversions/nonebot-adapter-github-user.svg)](https://pypi.org/project/nonebot-adapter-github-user/)

NoneBot2 的 **GitHub 用户账号适配器**：让机器人以一个**专用的 GitHub 账号**身份登录 GitHub 网页端，并用这个会话去访问 GitHub。

它和官方的 [`nonebot-adapter-github`](https://github.com/nonebot/adapter-github) 不是同一个东西，两者用途不同、互不冲突：

| | adapter-github（官方） | 本适配器 |
| --- | --- | --- |
| 身份 | GitHub App / OAuth App | 一个真实用户账号 |
| 连接方式 | Webhook 接收事件 + App 私钥鉴权 | 网页表单登录（账号密码 + TOTP 双因素） |
| 适用场景 | 接收仓库事件、以 App 身份调 REST API | 以「人」的身份访问 GitHub 页面 / 需要用户会话的场景 |
| 是否接收事件 | 是 | 否（当前只做出站访问） |

除了上面的网页会话，本适配器还支持用 token（PAT / `gh auth token` / OAuth 设备流）走官方 REST API 干活：**开 PR、写评论、提交评审、合并、推分支**，见下文「API 模式」。

## 安装

已发布到 PyPI：<https://pypi.org/project/nonebot-adapter-github-user/>

```bash
pip install nonebot-adapter-github-user
```

想改代码时也可以从源码安装：

```bash
cd nonebot-adapter-github-user
pip install .
```

说明：适配器代码放在 `nonebot/adapters/github_user/` 里，属于 **命名空间包**。两种装法都会把文件合并进已安装的 `nonebot/adapters/` 目录，因此都可以直接 `from nonebot.adapters.github_user import Adapter`。

开发阶段如果不想反复安装，用 `pip install -e .` 会在部分 setuptools 版本上**无法生效**（`nonebot` 是普通包，editable 的命名空间映射挂不进去）。这时在 Bot 入口文件里显式加一行即可（这也是 NoneBot 官方文档推荐的调试方式）：

```python
import nonebot
import nonebot.adapters

nonebot.adapters.__path__.append(  # type: ignore[attr-defined]
    "path/to/nonebot-adapter-github-user/nonebot/adapters"
)
```

## 配置

### 方式一：单账号简写（最小配置）

```dotenv
GITHUB_USER_LOGIN=bot@example.com
GITHUB_USER_PASSWORD=your-password

# 账号开启了双因素认证时再填（GitHub 设置页给出的 base32 密钥，或 otpauth:// URI）
GITHUB_USER_TOTP_SECRET=JBSWY3DPEHPK3PXP

# 可选：会话 Cookie 缓存文件，重启后免密码登录
GITHUB_USER_COOKIE_STORE=./github_user_session.json
```

### 方式二：多账号

```dotenv
GITHUB_USER_ACCOUNTS='[
  {"login":"bot-a@example.com","password":"pw-a","totp_secret":"AAAAAAAAAAAAAAAA","label":"a"},
  {"login":"bot-b@example.com","password":"pw-b","label":"b"}
]'
```

其它可选配置：

```dotenv
GITHUB_USER_BASE_URL=https://github.com
GITHUB_USER_USER_AGENT=Mozilla/5.0 ...
GITHUB_USER_PROXY=http://127.0.0.1:7890
GITHUB_USER_TIMEOUT=30
GITHUB_USER_MAX_RETRIES=2
GITHUB_USER_TOTP_ATTEMPTS=2
GITHUB_USER_LOGIN_RETRIES=3
GITHUB_USER_LOGIN_BACKOFF=5
```

### 设备验证自动取码（IMAP / POP3）

GitHub 对陌生设备会要求「设备验证」，把验证码发到账号邮箱；机器人点不了邮箱，所以适配器支持直接收信取码。IMAP / POP3 都支持，SSL 与 STARTTLS/STLS 都支持：

```dotenv
GITHUB_USER_MAIL_PROTOCOL=imap
GITHUB_USER_MAIL_HOST=imap.qq.com
GITHUB_USER_MAIL_PORT=993            # 可省略，按协议 + SSL 自动推导（993/143/995/110）
GITHUB_USER_MAIL_USER=bot@qq.com
GITHUB_USER_MAIL_PASSWORD=邮箱授权码  # QQ/163 要用「IMAP/POP3 授权码」，不是邮箱登录密码
GITHUB_USER_MAIL_SSL=true            # 默认 true：直连 TLS（IMAPS/POP3S）
GITHUB_USER_MAIL_STARTTLS=false      # 关掉 SSL 时可用 STARTTLS/STLS 升级
GITHUB_USER_MAIL_FOLDER=INBOX        # IMAP 收件目录，POP3 忽略
GITHUB_USER_MAIL_FROM=github.com     # 只认这个发件人，防误读
GITHUB_USER_MAIL_SUBJECT=            # 可选：主题关键字
GITHUB_USER_MAIL_UNSEEN_ONLY=true    # IMAP：只看未读邮件
GITHUB_USER_MAIL_SEARCH_DAYS=2       # 只扫最近 N 天
GITHUB_USER_MAIL_MAX_MESSAGES=15     # 最多扫最新多少封
GITHUB_USER_MAIL_DELETE_AFTER_READ=false
GITHUB_USER_MAIL_POLL_TIMEOUT=120    # 等验证码邮件的最长秒数
GITHUB_USER_MAIL_POLL_INTERVAL=5     # 轮询间隔秒数
```

常用邮箱参数：

| 邮箱 | 协议 | 服务器 | 端口 | 密码栏填什么 |
| --- | --- | --- | --- | --- |
| QQ | IMAP | imap.qq.com | 993 | 设置→账号里生成的授权码 |
| QQ | POP3 | pop.qq.com | 995 | 同上 |
| 163 | IMAP | imap.163.com | 993 | 客户端授权码 |
| Gmail | IMAP | imap.gmail.com | 993 | 应用专用密码 |

配好之后，登录流程遇到 `/sessions/verified-device` 会自动轮询邮箱取码，无需再手动接线。

命令行自检同样支持：

```bash
# 自动从邮箱取码
python -m nonebot.adapters.github_user \
  --mail-protocol imap --mail-host imap.qq.com \
  --mail-user bot@qq.com --mail-password 授权码 \
  --cookie-store ./github_session.json

# 不用邮箱，手动输入邮件里的验证码
python -m nonebot.adapters.github_user --ask-device-otp
```

## 注册适配器

```python
import nonebot
from nonebot.adapters.github_user import Adapter as GitHubUserAdapter

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(GitHubUserAdapter)

nonebot.run()
```

适配器会在 NoneBot 启动时自动登录所有已配置账号，并注册对应的 Bot（`bot.self_id` 是 GitHub 用户名）。账号名可通过 `Adapter.get_name()` 得到：`"GitHub-User"`。

## 使用

```python
from nonebot import get_bots
from nonebot.adapters.github_user import Bot

bot: Bot = next(b for b in get_bots().values() if isinstance(b, Bot))

# 站内路径会被补全成 https://github.com/...
resp = await bot.request("GET", "/notifications")

# 或走 call_api：带 / 的是站内路径，其它视为 api.github.com 的端点
data = await bot.call_api("/notifications", method="GET")

# 会话失效会自动重新登录一次；也可以手动确认
username = await bot.get_authenticated_user()
```

不想用 NoneBot 时，登录流程也可以单独使用：

```python
from nonebot.adapters.github_user import GitHubSession, generate_totp

session = GitHubSession("bot@example.com", "password", totp_secret="JBSWY3DPEHPK3PXP")
result = await session.login()
print(result.username, result.two_factor, session.cookie_dict())
```

部署前可以先在目标机器上做一次登录自检（只读地验证账号密码 / 双因素，不会打印密码，Cookie 输出已脱敏）：

```bash
export GITHUB_USER_LOGIN=bot@example.com
export GITHUB_USER_PASSWORD=your-password
export GITHUB_USER_TOTP_SECRET=JBSWY3DPEHPK3PXP   # 可选
python -m nonebot.adapters.github_user
```

## API 模式：自动开 PR / 评论 / 评审

网页会话适合"以人的身份访问页面"；要让机器人**自动干活**（开 PR、写评论、评审、合并），用 token 走 `api.github.com` 更稳——不触发 CAPTCHA / 设备验证，速率限制也明确（5000 次/小时）。

### 二选一：怎么给适配器一个 token

| 方式 | 你要做的 | 适合 |
| --- | --- | --- |
| **① 在 `.env` 里配 access token** | 自己准备好 token（PAT，或本机 `gh auth token` 的输出）填进 `GITHUB_USER_TOKEN` | 手上已经有 token，想最快跑通 |
| **② 自行登录（OAuth 设备流）** | 跑一次 `--oauth-login`，浏览器里输一次性代码，token 自动落盘 | 不想手动管 token，一条命令搞定 |

两种**只需选一种**：配了 `GITHUB_USER_TOKEN` 就不会走设备流；反过来，跑过设备流之后适配器会直接读 `github_user_token.json`，`.env` 里什么都不用写。

#### ① 在 `.env` 里配 access token

```dotenv
GITHUB_USER_TOKEN=ghp_xxxxxxxx
```

token 从哪来：

- 自己的 PAT：GitHub → Settings → Developer settings → Personal access tokens，scope 至少 `repo`（要改 `.github/workflows/` 里的文件再勾 `workflow`）
- 或者直接复用本机 gh 的登录态：`gh auth token` 把输出粘进去

#### ② 自行登录（OAuth 设备流，一次性）

```bash
python -m nonebot.adapters.github_user --oauth-login --oauth-client-id gh
```

终端会打印一个一次性代码和 <https://github.com/login/device>，浏览器里输一次即可。`--oauth-client-id gh` 表示直接用 GitHub CLI 的公开 client_id（授权页会显示 "GitHub CLI"，那是 GitHub 官方应用的身份，个人自用图省事可以；发布给别人用建议自己注册 OAuth App，见下）。

token 会存到 `--token-store`（默认 `./github_user_token.json`，写入权限 0600，已在 `.gitignore` 里）。之后适配器启动会自动读取它，**`.env` 里不需要再配 token**；想确认生效了没：

```bash
python -m nonebot.adapters.github_user --check-api
```

> **实测提醒**：gh 的 OAuth App 没有开启 expiring tokens，所以用它拿到的 token 是长期有效的（没有 refresh token，也就没有自动续期这回事）。好处是不用管续期，代价是 token 一旦泄露就一直有效。想要 8 小时自动轮换，就自己注册一个开启 expiring tokens 的 OAuth App，换掉 client_id 即可，代码无需改动。

#### 用自己的 OAuth App（可选）

GitHub → Settings → Developer settings → OAuth Apps → New OAuth App，勾上 **Enable Device Flow**，scope 用 `repo,workflow`；然后把 client_id 填给 `--oauth-client-id` 或 `GITHUB_USER_OAUTH_CLIENT_ID`。

### 配置

```dotenv
# 方式①：固定 token（配了它就不会走设备流）
GITHUB_USER_TOKEN=

# 方式②：OAuth 设备流（跑过 --oauth-login 后，下面这段也可以只留 client_id，
#          甚至全部留空——只要 ./github_user_token.json 在）
GITHUB_USER_OAUTH_CLIENT_ID=gh
GITHUB_USER_OAUTH_CLIENT_SECRET=
GITHUB_USER_OAUTH_SCOPES=repo,workflow
GITHUB_USER_TOKEN_STORE=./github_user_token.json

# 企业版自建地址（可选）
GITHUB_USER_API_BASE_URL=https://api.github.com
```

适配器启动时会自动校验 token 并把身份写进日志（token 不可用时只会告警，不会阻断启动）；插件里用 `get_github_api()` 取到客户端。

### 在插件里用

```python
from nonebot.adapters.github_user import get_github_api

api = get_github_api()

# 提交文件 + 开 PR（纯 API，不需要本地 git）
pr = await api.open_pull_request_with_files(
    "owner/repo",
    files={"docs/a.md": "# hello"},
    title="docs: 新增 a.md",
    branch="bot/docs-a",
    base_branch="main",
    body="由机器人自动创建。",
)

# 评论、行内评论、评审、合并
await api.comment_pull_request("owner/repo", pr["number"], "已自动检查 ✅")
await api.comment_pull_request_line(
    "owner/repo", pr["number"], "这里建议加个空行", path="docs/a.md", line=3
)
await api.review_pull_request(
    "owner/repo", pr["number"], event="REQUEST_CHANGES", body="请补充说明"
)
await api.merge_pull_request("owner/repo", pr["number"], method="squash")
```

也可以当独立库用（不需要 NoneBot 运行时）：

```python
from nonebot.adapters.github_user import GitHubAPI

api = GitHubAPI(token="ghp_xxxxxxxx")
print(await api.get_authenticated_user())
```

### API 速查表

约定（下面所有方法通用）：

- `repo` 一律是 `"owner/name"` 字符串；`number` 是 issue / PR 编号；`comment_id` 是行内评论 id
- 返回解析后的 JSON（`dict` / `list`）；出错抛 `GitHubAPIError`，带 `status_code` / `message` / `payload`，另有 `is_not_found` / `is_forbidden` / `is_rate_limited` 三个判断属性
- 服务端返回 401 时会自动强制刷新 token 再重试一次；其余错误原样抛出
- 拿客户端：NoneBot 里用 `get_github_api()`，独立使用 `GitHubAPI(token="ghp_xxx")`；用完 `await api.aclose()`（适配器退出时会自动关）

**身份与泛化调用**

| 方法 | 作用 | 接口 |
| --- | --- | --- |
| `get_authenticated_user()` | 确认 token 身份 | `GET /user` |
| `request(method, path, *, params=None, json=None, headers=None, accept=None, raw_text=False)` | 泛化调用，没封装的接口用它 | 任意 |
| `graphql(query, variables=None)` | 直接发 GraphQL 查询（`errors` 会转成异常） | `POST /graphql` |

**PR**

| 方法 | 作用 | 接口 |
| --- | --- | --- |
| `create_pull_request(repo, *, title, head, base, body=None, draft=False, maintainer_can_modify=True)` | 开 PR；`head` 支持 `owner:branch` 跨仓库 | `POST /repos/{repo}/pulls` |
| `get_pull_request(repo, number)` | 取单个 PR | `GET /repos/{repo}/pulls/{number}` |
| `list_pull_requests(repo, *, state="open", head=None, base=None, per_page=30)` | 列 PR | `GET /repos/{repo}/pulls` |
| `update_pull_request(repo, number, **fields)` | 改标题 / 正文 / `state`（`state="closed"` 即关闭） | `PATCH /repos/{repo}/pulls/{number}` |
| `close_pull_request(repo, number)` | 关闭 PR | `PATCH /repos/{repo}/pulls/{number}` |
| `get_pull_request_diff(repo, number)` | 取 diff 纯文本 | `GET .../pulls/{number}` + `Accept: application/vnd.github.v3.diff` |

**评论与评审**

| 方法 | 作用 | 接口 |
| --- | --- | --- |
| `comment_issue(repo, number, body)` / `comment_pull_request(...)` | issue 或 PR 下的普通评论（同一个方法，两个名字） | `POST /repos/{repo}/issues/{number}/comments` |
| `comment_pull_request_line(repo, number, body, *, path, line, side="RIGHT", start_line=None, commit_id=None)` | PR 某一行上的行内评论 | `POST /repos/{repo}/pulls/{number}/comments` |
| `reply_to_review_comment(repo, number, comment_id, body)` | 回复某条行内评论，留在**同一线程** | `POST /repos/{repo}/pulls/{number}/comments/{comment_id}/replies` |
| `comment_commit(repo, sha, body)` | 提交评论 | `POST /repos/{repo}/commits/{sha}/comments` |
| `add_discussion_comment(discussion_id, body, *, reply_to_id=None)` | Discussion 评论（Discussions 只有 GraphQL 接口） | GraphQL `addDiscussionComment` |
| `review_pull_request(repo, number, *, event="COMMENT", body=None, comments=None)` | 提交评审：`APPROVE` / `REQUEST_CHANGES` / `COMMENT`（不能批准自己开的 PR） | `POST /repos/{repo}/pulls/{number}/reviews` |
| `reply(target, body)` | 按 webhook 事件类型自动挑上面某个接口 | 见上一节「send 走哪个接口」 |

**合并与其它操作**

| 方法 | 作用 | 接口 |
| --- | --- | --- |
| `merge_pull_request(repo, number, *, method="merge", commit_title=None, commit_message=None, sha=None)` | 合并 PR：`merge` / `squash` / `rebase` | `PUT /repos/{repo}/pulls/{number}/merge` |
| `add_labels(repo, number, labels)` | 加标签 | `POST /repos/{repo}/issues/{number}/labels` |
| `request_reviewers(repo, number, *, reviewers=(), team_reviewers=())` | 请求指定人或团队评审 | `POST /repos/{repo}/pulls/{number}/requested_reviewers` |

**分支与提交（全走 API，不需要本地 git）**

| 方法 | 作用 | 接口 |
| --- | --- | --- |
| `get_branch_sha(repo, branch)` | 取分支头 SHA | `GET /repos/{repo}/git/ref/heads/{branch}` |
| `try_get_branch_sha(repo, branch)` | 同上，分支不存在时返回 `None` | 同上 |
| `create_branch(repo, branch, *, from_branch=None, from_sha=None)` | 建分支；同名分支已存在就直接返回它 | `POST /repos/{repo}/git/refs` |
| `delete_branch(repo, branch)` | 删除分支（清理临时分支） | `DELETE /repos/{repo}/git/refs/heads/{branch}` |
| `get_contents(repo, path, *, ref=None)` | 读文件内容，不存在返回 `None` | `GET /repos/{repo}/contents/{path}` |
| `commit_files(repo, *, files, message, branch, base_branch=None)` | 提交若干文件（值给 `None` 表示删除该文件）；分支不存在就从 `base_branch` 拉 | Git Data：blobs → trees → commits → refs |
| `open_pull_request_with_files(repo, *, files, title, branch, base_branch="main", body=None, commit_message=None, draft=False)` | 提交文件 + 开 PR 一把梭 | 上面两者组合 |

### 权限与限制

| 能力 | 需要的 scope |
| --- | --- |
| 开 PR / 评论 / 评审 / 合并 / 推分支 | `repo` |
| 改 `.github/workflows/` 里的文件 | 再加 `workflow` |

- **不能批准自己开的 PR**：GitHub 会返回 `Can not approve your own pull request`；评论、请求修改都正常。所以标准做法是"机器人开 PR + 人或 CI 批准"。
- 分支保护照旧生效：保护分支不能直接 push，只能走 PR。
- 组织仓库可能要求管理员先批准这个 OAuth App，否则 token 对组织仓库无效。

## Webhook 入口：被动响应事件

上面的能力都是"机器人主动干活"；要让它在**别人开 PR / 提 issue / 评论时自动反应**，就把 GitHub 的 webhook 指过来——适配器会注册一个 HTTP 入口，校验签名，把事件转成 NoneBot 事件（`WebhookEvent`，类型为 `notice`），插件用 `on_notice` 就能接住。

### GitHub 侧配置

仓库 → Settings → Webhooks → Add webhook：

- **Payload URL**：`https://你的域名/github/webhook`（路径与 `GITHUB_USER_WEBHOOK_PATH` 一致）
- **Content type**：`application/json`
- **Secret**：生成一串强随机字符串，填进 `GITHUB_USER_WEBHOOK_SECRET`
- **事件**：按需勾选（Pull requests / Issues / Issue comments …）

### 适配器配置

```dotenv
GITHUB_USER_WEBHOOK_PATH=/github/webhook
GITHUB_USER_WEBHOOK_SECRET=和GitHub上填的一模一样
GITHUB_USER_WEBHOOK_EVENTS=                  # 留空=全部；也可写 pull_request,issues
GITHUB_USER_WEBHOOK_SELF_ID=                 # 可选：Bot 的 self_id，默认用 token 登录名
GITHUB_USER_WEBHOOK_ALLOW_UNSIGNED=false     # 仅本地调试用，生产别开
```

两个前提要记住：

- 入口需要 **ASGI driver**（fastapi / aiohttp / quart 等）；用 `none` driver 时只会打警告，不注册路由。
- 处理事件需要一个可用的 Bot，也就是要开 **API 模式**（配 token 或跑过设备流）；只配网页会话时事件会被忽略并打警告。

### 插件里怎么接

```python
from nonebot import on_notice
from nonebot.rule import Rule
from nonebot.adapters.github_user import WebhookEvent

def is_pr(event: WebhookEvent) -> bool:
    return event.event == "pull_request"

pr_event = on_notice(rule=Rule(is_pr))

@pr_event.handle()
async def _(event: WebhookEvent):
    if event.action != "opened":
        return
    # send 会自动把内容评论到对应的 PR / issue / commit 上
    await pr_event.send(f"感谢 @{event.sender} 的提交，机器人开始检查～")
```

`WebhookEvent` 上的字段：`event`（如 `pull_request`）、`action`（如 `opened`）、`repository`（`owner/name`）、`sender`（登录名）、`delivery_id`、`payload`（原始 JSON）。

### 行为细节

- **签名校验**：没配 secret 直接 503 拒绝；签名不对返回 401；合法请求返回 202，校验通过后异步处理，不阻塞 GitHub
- **投递去重**：按 `X-GitHub-Delivery` 记住最近 500 条，重复投递不会重复触发
- **`ping` 事件**：GitHub 保存 webhook 时会发一次，适配器回 202 并记日志
### send 走哪个接口（按事件类型自动挑）

| 事件 | 回复走哪儿 |
| --- | --- |
| `issues` / `issue_comment` / `pull_request` / `pull_request_review` | issue 评论：`POST /repos/{repo}/issues/{n}/comments`（PR 也走这个） |
| `pull_request_review_comment` | 行内评论回复：`POST /repos/{repo}/pulls/{n}/comments/{id}/replies`；失败（评论被删、行号变了）会自动退回普通 PR 评论 |
| `commit_comment` | 提交评论：`POST /repos/{repo}/commits/{sha}/comments` |
| `discussion` / `discussion_comment` | GraphQL `addDiscussionComment`；`discussion_comment` 会带 `replyToId` 回到原线程 |
| 其它（如 `push`） | 不硬猜，直接抛 `ActionFailed` 并说明缺什么 |

同一个分发也能在客户端直接用：`await api.reply(target, "内容")`，其中 `target` 就是 `reply_target(event)` 的返回值。

## 登录流程说明

`nonebot/adapters/github_user/session.py` 实现了 GitHub 网页端当前的登录行为：

1. `GET /login` 取回 `authenticity_token` 与匿名 Cookie（`_gh_sess`）；
2. `POST /session` 提交 `login` / `password`；
3. 令牌过期（HTTP 422）时自动重新取令牌并重试一次；
4. 账号开启双因素时，GitHub 会 302 到 `/sessions/two-factor`，适配器解析页面上真实的验证码输入框（`app_otp` / `sms_otp` / `otp`），用 `totp_secret` 按 RFC 6238 生成验证码；验证码被拒时会等到下一个 30 秒窗口重试（`GITHUB_USER_TOTP_ATTEMPTS`）；
5. 若 GitHub 要求设备验证（`/sessions/verified-device`，验证码发到邮箱）：配了上面的邮箱取码就自动完成，否则抛出 `DeviceVerificationRequired`（也可用 `device_otp_provider` 自定义取码来源）；
6. `GET /` 校验登录态并解析真实用户名；
7. 会话 Cookie 常驻内存，可选写入 `GITHUB_USER_COOKIE_STORE`（权限 0600）；请求被重定向回 `/login` 时视为会话失效，自动重新登录一次。

## 已知限制与建议

- **人机验证（CAPTCHA）**：GitHub 对可疑登录会插入 CAPTCHA，适配器只能检测并抛出 `CaptchaRequired`，无法绕过，触发后建议在常用设备/常用 IP 上先正常登录一次。
- **设备验证**：GitHub 对陌生设备会发邮件验证码，配上「设备验证自动取码」即可无人值守完成；没配的话只能用 `--ask-device-otp` 手动输入。
- **账号风险**：GitHub 的《Acceptable Use Policies》不鼓励用自动化手段登录账号，频繁失败或异地登录可能触发风控甚至限制账号；请使用**专用机器人账号**，并优先考虑官方推荐的 PAT / OAuth Device Flow / GitHub App 方案，只有在确实需要「用户网页会话」时才使用本适配器。
- **出站为主**：当前没有实现 GitHub Webhook 接入，因此没有消息事件，`Bot.send()` 会抛出 `NotImplementedError`。

## 测试

```bash
python -m pytest tests -q
```

登录流程测试使用 `httpx.MockTransport`，不联网、不需要 NoneBot；`tests/test_nonebot_integration.py` 需要已安装 `nonebot2>=2.2`，否则自动跳过。

## 自动打包（GitHub Actions）

本项目根目录的 `.github/workflows/build.yml` 会在**每次 push**（任意分支、任意 tag，也可以在 Actions 页面手动触发）自动执行：

1. 扫描仓库中所有含 `pyproject.toml` 的目录（目前只有本适配器，后续新增包会自动纳入）；
2. 安装依赖并跑 `pytest`（目录下没有 `tests/` 时跳过）；
3. `python -m build` 生成 wheel 与 sdist，并用 `twine check` 校验元数据；
4. 把产物上传为 Actions Artifact（保留 30 天，名称为 `dist-nonebot-adapter-github-user`）。

推送 `v*` 形式的 tag 时（例如 `git tag v0.1.0 && git push origin v0.1.0`），还会自动创建 GitHub Release 并把 wheel / sdist 附上去。

发布用的版本号取自 `pyproject.toml` 的 `version`，发新版前记得先改。

## 发布到 PyPI

本项目已经发布在 PyPI 上，当前版本 **0.1.0**：<https://pypi.org/project/nonebot-adapter-github-user/>

发布用的是 GitHub 官方模板工作流 `.github/workflows/publish.yml`：**发布（Publish）一个 GitHub Release** 时，通过 PyPI Trusted Publishing（OIDC）把包传上去，全程不需要 token、不需要任何 secret。

Trusted Publisher 已经在 PyPI 的 [Publishing](https://pypi.org/manage/account/publishing/) 页面配好，字段如下（换个仓库名时要同步改）：

- PyPI Project Name：`nonebot-adapter-github-user`
- Owner：`rensumo`
- Repository name：`nonebot-adapter-github-user`
- Workflow name：`publish.yml`
- Environment name：`pypi`

之后每次发版：

```bash
# 1. 先把 pyproject.toml 里的 version 改成新版本号（例如 0.1.1）
# 2. 打 tag 推送：build.yml 会跑测试、构建，并生成一个「草稿」Release
git tag v0.1.1
git push origin v0.1.1
# 3. 到 Releases 页面核对草稿里的 wheel / sdist，点「Publish release」
#    这一步才会触发 publish.yml，把包上传到 PyPI
```

几点说明：

- build.yml 生成的是**草稿** Release，不会自动发布；手动点 Publish 就是发版确认动作，因为 PyPI 上传不可逆（同名版本只能 yank，不能覆盖）。
- 想更稳，可以在 Settings → Environments → `pypi` 里加 Required reviewers，发布任务就会停下来等人批准。
- Workflow name 必须填 `publish.yml`：PyPI 的 Trusted Publisher 是按「仓库 + 工作流文件名 + 环境名」三者一起校验的，填错会 403。
- 用 `GITHUB_TOKEN` 自动创建 Release 不会触发 publish.yml（GitHub 刻意阻断这种自触发），所以发布那一步必须由人来点。
