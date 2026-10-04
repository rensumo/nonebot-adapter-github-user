# nonebot-adapter-github-user

NoneBot2 的 **GitHub 用户账号适配器**：让机器人以一个**专用的 GitHub 账号**身份登录 GitHub 网页端，并用这个会话去访问 GitHub。

它和官方的 [`nonebot-adapter-github`](https://github.com/nonebot/adapter-github) 不是同一个东西，两者用途不同、互不冲突：

| | adapter-github（官方） | 本适配器 |
| --- | --- | --- |
| 身份 | GitHub App / OAuth App | 一个真实用户账号 |
| 连接方式 | Webhook 接收事件 + App 私钥鉴权 | 网页表单登录（账号密码 + TOTP 双因素） |
| 适用场景 | 接收仓库事件、以 App 身份调 REST API | 以「人」的身份访问 GitHub 页面 / 需要用户会话的场景 |
| 是否接收事件 | 是 | 否（当前只做出站访问） |

## 安装

```bash
cd nonebot-adapter-github-user
pip install .
```

说明：适配器代码放在 `nonebot/adapters/github_user/` 里，属于 **命名空间包**。`pip install .` 会把文件合并进已安装的 `nonebot/adapters/` 目录，因此可以直接 `from nonebot.adapters.github_user import Adapter`。

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

## 登录流程说明

`nonebot/adapters/github_user/session.py` 实现了 GitHub 网页端当前的登录行为：

1. `GET /login` 取回 `authenticity_token` 与匿名 Cookie（`_gh_sess`）；
2. `POST /session` 提交 `login` / `password`；
3. 令牌过期（HTTP 422）时自动重新取令牌并重试一次；
4. 账号开启双因素时，GitHub 会 302 到 `/sessions/two-factor`，适配器解析页面上真实的验证码输入框（`app_otp` / `sms_otp` / `otp`），用 `totp_secret` 按 RFC 6238 生成验证码；验证码被拒时会等到下一个 30 秒窗口重试（`GITHUB_USER_TOTP_ATTEMPTS`）；
5. 若 GitHub 要求设备验证（`/sessions/verified-device`，验证码发到邮箱），默认抛出 `DeviceVerificationRequired`，可通过 `device_otp_provider` 回调接入邮箱；
6. `GET /` 校验登录态并解析真实用户名；
7. 会话 Cookie 常驻内存，可选写入 `GITHUB_USER_COOKIE_STORE`（权限 0600）；请求被重定向回 `/login` 时视为会话失效，自动重新登录一次。

## 已知限制与建议

- **人机验证（CAPTCHA）**：GitHub 对可疑登录会插入 CAPTCHA，适配器只能检测并抛出 `CaptchaRequired`，无法绕过，触发后建议在常用设备/常用 IP 上先正常登录一次。
- **设备验证**：邮件验证码必须由邮箱侧提供，否则无法自动完成。
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

workflow 里已经带好 `publish-pypi` 任务，走的是 PyPI **Trusted Publishing**（OIDC），不需要 token、也不需要往仓库里塞 secret；代价是要先在两边各配置一次：

1. 在 PyPI 的 [Publishing](https://pypi.org/manage/account/publishing/) 页面添加一个 pending publisher，字段照抄：PyPI Project Name 填 `nonebot-adapter-github-user`，Owner 填 `rensumo`，Repository name 填 `nonebot-adapter-github-user`，Workflow name 填 `build.yml`，Environment name 填 `pypi`。
2. 在这个仓库的 Settings → Secrets and variables → Actions → Variables 里新建一个仓库变量 `PUBLISH_TO_PYPI=true`。
3. 把 `pyproject.toml` 里的 `version` 改成新版本号，然后推一个 `v*` tag：

```bash
git tag v0.1.0
git push origin v0.1.0
```

这个 tag 会先触发构建与测试，再把 wheel / sdist 传到 PyPI；`PUBLISH_TO_PYPI` 没打开时该任务直接跳过，不会让流水线变红。
