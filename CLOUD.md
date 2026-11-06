# WorkSwarm 统一授权部署与验收（0.7.2）

本版本仅保留“发布者云端服务 + 用户 Windows stdio 桥接”。发布者配置第三方开发者应用
（Google、GitHub、Microsoft、Slack），用户不配置它们的 Client ID、Secret 或 token。用户首次使用需登录发布者配置
的 WorkSwarm OIDC 身份系统；已有浏览器 SSO 时可复用。之后只在 Google/GitHub 官方页面同意。

这不是已经开通的公网服务。示例域名均需替换。不能从源码得知你实际的 WorkSwarm 账号
issuer，也不能自动取得其签发权限。本包提供可接入既有 OIDC 的接口及一套可选 Keycloak
参考配置。若要复用现有 WorkSwarm 登录，必须由平台管理员接入同一身份系统并配置组声明。

## 1. 发布者准备

- 一台能访问 Google/GitHub/身份系统的 Linux 主机，Docker Engine + Compose v2。
- 一个授权服务域名，如 `auth.company.example`，DNS 指向主机，80/443 对外可达。
- 已有 OIDC 身份服务；或者再准备 `login.company.example`，部署附带 Keycloak。
- 当前 Docker 命令和 Windows GUI/浏览器需实机验收；本交付不会声称它们已经上线。

在源码根目录先安装 Python 包，或使用自己的干净虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[server,test]'
```

选择自带 Keycloak（以下两个域名换成真实域名）：

```bash
.venv/bin/python tools/publisher_setup.py \
  --public-origin https://auth.company.example \
  --identity-domain login.company.example \
  --output /srv/workswarm-auth-runtime
```

默认只启用 gmail、github。要启用更多提供方，追加 `--providers`，例如
`--providers gmail,google-drive,google-calendar,github,microsoft,slack`；对应的回调地址、
准入策略和可用操作会一起生成，无需手工编辑 JSON。

该命令生成私有部署目录：`config/`、`data/`、`secrets/vault.key`、`.env`、Compose、
realm 导入文件及一个不含秘密的 `cloud.json`。输出目录不允许覆盖，防止重建密钥破坏旧凭证。
整个运行目录不要提交到仓库，也不要给用户。只允许将 `cloud.json` 输入客户端构建器。
当前用户要对输出目录的上级目录有写权限；没有 `/srv` 的写权限时，换一个有权限的目录，并同步替换后面命令里的路径。

既有身份系统可使用 `--issuer https://identity.company.example/your-realm` 替代
`--identity-domain`。服务端必须能获取该 issuer 的 discovery/JWKS，JWT 必须为 RS256/ES256。
其 access token 的 audience 必须含 `https://auth.company.example/mcp`；access token 和
浏览器 ID token 的 `groups` 都应含 `workswarm-users`。桌面公共客户端注册为
`workswarm-desktop`，回调 **精确为** `http://127.0.0.1:18766/oauth/callback`，强制 PKCE S256。
浏览器公共客户端注册为 `workswarm-auth-browser`，回调为
`https://auth.company.example/oauth/owner/callback`，同样强制 PKCE。
平台令牌应支持 `auth:read auth:connect auth:use auth:disconnect` scopes。身份服务客户端配置
由管理员完成，普通用户不填写任何客户端凭证。

## 2. 发布者配置第三方应用（仅在服务器操作）

Google 创建 **Web application** OAuth 客户端；启用 Gmail API，登记回调：

```text
https://auth.company.example/oauth/providers/gmail/callback
```

权限为 `openid email https://www.googleapis.com/auth/gmail.readonly`。测试阶段将测试邮箱
加入 Google 测试用户。公开发行需完成适用的 Google 验证/安全评估。不能把之前本机 Desktop
app 客户端直接当成正式 Web 客户端。

GitHub 创建 OAuth App，登记回调：

```text
https://auth.company.example/oauth/providers/github/callback
```

当前 API 权限为 `read:user`；仓库功能不承诺私有仓库。使用授权码流程，不需要 Device Flow。

Google Drive / Calendar 可复用 Gmail 的 Google 客户端：在同一项目启用 Drive API、Calendar API，
在数据访问中加入 `drive.metadata.readonly`、`calendar.calendarlist.readonly`，并为 Web 客户端
登记 `/oauth/providers/google-drive/callback` 与 `/oauth/providers/google-calendar/callback`。

Microsoft：在 Entra ID“应用注册”中新建应用，帐户类型选“任何组织目录 + 个人 Microsoft 帐户”，
重定向 URI 平台选 Web，地址 `https://auth.company.example/oauth/providers/microsoft/callback`；
Graph 委托权限 `User.Read`、`Mail.Read`、`offline_access`。Client Secret 录入 **Value**
（约 40 字符），不是 Secret ID（36 位 GUID）。注册账号必须属于某个目录；个人账号可先开通
Azure 获得默认目录。授权测试账号需有 Outlook 邮箱，否则自动校验（读 1 封邮件）失败。

Slack：在 api.slack.com/apps 选 “Paste a manifest”，Bot scope 为 `team:read`，
redirect_urls 为 `https://auth.company.example/oauth/providers/slack/callback`，关闭 token
rotation。使用 Basic Information 中的 Client ID / Client Secret（不是 Signing Secret）。

隐藏输入各提供方的应用凭证（逐个提示；留空保留已有配置；不打印 Secret）：

```bash
.venv/bin/python tools/configure_cloud_providers.py \
  --config /srv/workswarm-auth-runtime/config/providers.json
```

不要将 Secret 输入普通用户电脑、聊天、市场表单、命令行参数或 ZIP。它们只留在发布者服务端。

## 3. 启动服务

在生成的 `.env` 添加 `WORKSWARM_SOURCE=/你的源码绝对路径`。
自带身份系统还需设置 `KEYCLOAK_IMAGE` 为团队批准的真实镜像（建议固定 digest）；Caddy、
Postgres 镜像也建议在生产验收后固定 digest。没有默认演示管理员密码：随机密码在私有 `.env`
的 `KC_BOOTSTRAP_ADMIN_PASSWORD`，仅由管理员本地读取。
生成的 realm 需要 Keycloak 25 及以上（access token 的 `sub` 由其中的 `basic` scope 提供）。
`realm.json` 只含公共客户端，生成为 0644 供 Keycloak 容器用户读取，不要改回 0600。

```bash
sudo chown -R 10001:10001 /srv/workswarm-auth-runtime/config \
  /srv/workswarm-auth-runtime/data /srv/workswarm-auth-runtime/secrets
cd /srv/workswarm-auth-runtime
docker compose -f compose.yaml -f compose.identity.yaml up -d --build
```

用既有身份系统时只运行 `docker compose -f compose.yaml up -d --build`。
Caddy 终止 TLS，broker/Keycloak/Postgres 不向宿主发布端口。不要打开 OAuth URL 访问日志。
broker 单实例单 worker；健康检查只代表进程可用，不代表身份系统和第三方 API 已验收。

自带 Keycloak 首次启动导入 `workswarm` realm，无默认用户、关闭公开注册。管理员进入
自己的 HTTPS Keycloak 管理台创建试用用户，分配 `workswarm-users` 组并设定初始密码。
切勿把这个管理员账号当普通用户账号使用。后续改 realm 导入 JSON 不会自动更新已有 realm，
应通过管理台/受控管理员操作变更并重新验收。

## 4. 生成客户端包和市场包

发布者在装有源码包的机器上运行：

```bash
.venv/bin/python tools/build_cloud_client.py \
  --profile /srv/workswarm-auth-runtime/cloud.json \
  --output /srv/workswarm-auth-releases/0.7.2
```

输出两个文件：

- `workswarm_auth-0.7.2-py3-none-any.whl`：客户端 wheel，只含客户端模块和公共 profile。
  **复制到 broker 数据目录的 `client/` 下**（命令见本节末尾），
  broker 会在 `https://auth.company.example/client/<文件名>` 公开提供下载（只提供这一类文件）。
- `workswarm-auth-cloud-market-0.7.2.zip`：市场连接器包。启动命令为 `py -3`，运行内嵌的引导程序
  （源码见 `tools/client_bootstrap.py`）：首次启动从上面的地址下载 wheel，在
  `%LOCALAPPDATA%\WorkSwarmAuth\0.7.2` 建虚拟环境并安装，之后直接启动桥接程序。
  安装输出全部写到 stderr，桥接程序直接继承 stdin/stdout，不经过任何 shell 转码。

构建器使用固定白名单，不遍历或复制发布者运行目录。市场包并不会部署服务器。

复制 wheel 并确认可以下载。`data/` 已归容器用户所有，所以要用 `sudo`；`client/` 不存在时会自动创建，第二条命令应输出 `200`：

```bash
sudo install -D -o 10001 -g 10001 -m 0644 \
  /srv/workswarm-auth-releases/0.7.2/workswarm_auth-0.7.2-py3-none-any.whl \
  /srv/workswarm-auth-runtime/data/client/workswarm_auth-0.7.2-py3-none-any.whl
curl -sS -o /dev/null -w '%{http_code}\n' https://auth.company.example/client/workswarm_auth-0.7.2-py3-none-any.whl
```

## 5. 用户 Windows 操作（没有 providers.json）

1. 安装 Python 3.11+（推荐 3.12，python.org 安装包默认带 `py` 启动器）。
2. 在 WorkSwarm 市场安装本连接器并点击“连接”。首次连接自动下载并安装客户端（约 20–60 秒，
   需能访问 PyPI 或镜像）；之后秒级启动。云端配置和加密平台会话存到
   `%USERPROFILE%\.workswarm-auth-cloud`，首次启动时由 wheel 内置的 profile 自动生成。
3. 如果宿主有工具超时设置，首次 SSO 调用设为至少 240 秒；也可先在 PowerShell 运行下方 `cloud-login`。
4. 调用 `auth_connect(provider="github", connection_id="github-default")`。
   首次先登录平台 OIDC，再到 GitHub 同意；后续浏览器 SSO 可免登录。整个 provider 流程须用
   同一浏览器。普通用户不接触 Google/GitHub Client ID、Secret 或 token。
5. `auth_connect` 返回 pending；后台最多等待 300 秒，完成同意后自动执行 API 验证。
   调用 `auth_status` 确认 `authorization=authorized` 且 `validation=passed`。
6. 调用 `service_call(connection_id="github-default", operation="github_profile")` 读回资料。
   其他提供方同理，可用操作见 `auth_providers`；白名单之外的操作名返回 403，
   并附带 `allowed_operations`，列出这个连接可用的操作名。

WorkSwarm 中的注意事项：

- WorkSwarm 按会话加载 MCP：在对话输入区的扩展面板中勾选本连接器，agent 才能看到工具。
- 连接链接只能使用一次。授权中途刷新页面会得到 `connection_link_expired_or_used`，重新调用
  `auth_connect` 即可。
- 平台会话失效后，查询和调用工具只返回 `workswarm_login_required`，不会弹出浏览器。用户在场时调用
  `auth_login` 重新登录，它不发起第三方授权，已有连接保持不变；已登录时直接返回 `already_signed_in`。

可先完成平台登录以避免宿主工具超时：

```powershell
$p = "$env:LOCALAPPDATA\WorkSwarmAuth\0.7.2\.venv\Scripts\python.exe"
& $p -X utf8 -m workswarm_auth.cli cloud-login
& $p -X utf8 -m workswarm_auth.cli cloud-connect --provider github --connection github-default
& $p -X utf8 -m workswarm_auth.cli cloud-status
```

第二条命令会打开第三方页面并等待真实 API 验证；Ctrl+C 取消本次流程，不断开旧授权。
`auth_cancel(connection_id, attempt_id)` 同样只取消当前尝试；`auth_disconnect` 才删除服务器上的
第三方凭证并尝试撤销。`cloud-logout` 只删除本机平台会话；不删除已连接的第三方账号。

没有发布者公共 profile 的源码包直接运行安装器会明确失败。旧本机模式、`--local`、
静态 API Key 和本机第三方 token 导入已移除；开发和测试也使用云端 OIDC 路径。

## 5a. 平台账号：用 Google / GitHub 登录并自动开户（自带 Keycloak 时）

默认需要管理员在 Keycloak 为每个用户建账号并加入 `workswarm-users`。启动服务后，可让用户直接用
Google 或 GitHub 登录平台，首次登录即自动建号并加入准入组：

```bash
.venv/bin/python tools/configure_login_providers.py --runtime /srv/workswarm-auth-runtime \
  --providers google,github
```

- 先在 Google / GitHub 新建 OAuth 应用，回调为 `https://login.company.example/realms/workswarm/broker/google/endpoint`
  （GitHub 把 `google` 换成 `github`）。脚本会打印这两个地址，并隐藏输入 Client ID / Secret。
- 已有可用的同一 Google 客户端时，可用 `--reuse google=gmail` 从 providers.json 复制凭证（不显示）；
  providers.json 此时已归容器用户所有，需用 `sudo` 运行。用户较多时不建议复用，Google 登录应用要单独放一个项目。
  GitHub OAuth App 只能登记一个回调，需为登录单独建一个 App。
- 默认开启自动开户（realm 默认组 = `workswarm-users`）：**凡能用对应账号登录的人都会被准入**。
  请用受限的提供方应用把关，例如 Google OAuth 处于 Testing 状态只允许测试用户，或使用 Workspace
  内部应用；不需要时加 `--no-auto-enroll`，改由管理员手动加组。
- 若 WorkSwarm 平台本身提供标准 OIDC 登录，最佳做法是由平台管理员把它接为 Keycloak 身份提供方
  或直接作为 `--issuer`，让用户沿用 WorkSwarm 账号。

## 6. 验收标准与边界

- 干净 Windows 用户目录没有 provider 配置或第三方密钥，也可以连接和读取 API。
- Alice、Bob 使用同名 connection_id，仍不能查询、验证或调用对方的连接。
- 拒绝/取消/超时不清除旧凭证；另一授权尝试完成不能冒充当前尝试成功。
- 转发下一跳 URL 到另一浏览器失败，回放 state 失败；不含准入组声明的令牌被拒绝。
  退出组后，已有签名 JWT 在其到期前仍可能有效，应配置短 access-token 有效期。
- 刷新后的平台 token 和第三方 token 在身份复核成功前均不可用于业务调用。
- 发行 ZIP 不包含 providers/remote 配置、环境文件、vault、密钥或真实 token。

单元测试覆盖签名 OIDC 响应、真实 loopback HTTP 回调、实际 broker 路由及模拟第三方 API；
已验证和尚未验证的范围见 VALIDATION.md。
本版已移除原生连接卡补丁；当前兼容路径为 stdio 桥接工具流程。宿主原生 OAuth 支持需单独接入。
本机仅用独立 SessionStore 对平台会话做 Fernet 文件加密，不创建 SQLite 或加载服务端 Vault。
密钥与密文同属当前系统用户，未新增 Windows DPAPI/ACL 隔离。云端存储与审计仍是单实例 SQLite，不声称 HA/KMS/生产安全审查已完成。

## 7. 无公网域名时的测试部署

Google 只接受公网域名或 `localhost` 作为回调地址。没有公网域名时，可用
`--public-origin https://localhost:8443 --identity-domain localhost:8444` 生成配置，
在 Caddyfile 顶部加 `{ local_certs }` 使用自签测试 CA，仅把 8443/8444 发布到服务器的 127.0.0.1，
Windows 通过 `ssh -N -L 8443:127.0.0.1:8443 -L 8444:127.0.0.1:8444 <user>@<server>` 访问。
此时 broker 需要在容器内访问 `https://localhost:8444`，应让 broker 与 Caddy 共用网络命名空间
（`--network container:<caddy>`），并把 Caddy 的上游改为 `127.0.0.1:8766`。
Windows 需把测试 CA 导入“受信任的根证书”，并追加到桥接程序 venv 的 certifi 证书库。
