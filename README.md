# WorkSwarm 统一授权 Connector v0.7.2

仅保留一条链路：**WorkSwarm → 云端授权服务 → 第三方 API**。
发布者在服务器配置第三方应用，普通用户不填写第三方 Client ID、Secret 或 token。
支持 Gmail、Google Drive、Google Calendar、GitHub、Microsoft（Outlook）、Slack，均为只读操作。

## 架构

![WorkSwarm Auth 架构图：用户侧桥接客户端、发布者云端授权 broker、平台身份服务和第三方服务](docs/architecture.png)

| 组件 | 职责 |
|---|---|
| 服务端 `app / manager / providers / vault / security` | 第三方 OAuth、凭证加密、刷新/撤销、账号隔离、API 验证；强制 OIDC，不接受静态 API Key |
| 客户端 `cloud / cloud_commands / cloud_mcp / session_store` | 平台 OIDC 登录、浏览器跳转、授权等待/取消和 API 转发；仅缓存自己的平台会话，不建立凭证数据库 |
| 发布工具 `tools/` | 生成部署配置与不含服务端代码/秘密的 Windows 安装包、市场包 |

详细操作统一见 [CLOUD.md](CLOUD.md)；测试记录见 [VALIDATION.md](VALIDATION.md)。

## 用户如何使用

以下步骤面向普通用户，前提是发布者已完成部署并上架连接器。用户只需在 Windows 上安装 Python 3.11 或更高版本
（推荐 python.org 的 3.12 安装包，自带 `py` 启动器），不需要第三方 Client ID、Secret 或 token。

### 首次使用

1. **安装连接器**：在 WorkSwarm 连接器市场找到“第三方统一授权（云端托管）”并安装。
2. **点“连接”**：首次连接会自动下载并安装客户端，约需 20–60 秒，看到 8 个工具即完成。之后每次连接只需一两秒。
3. **新建会话**：在输入框旁的扩展面板中勾选本连接器。WorkSwarm 按会话加载连接器，没有勾选时 agent 看不到工具。
4. **提出需求**：例如“帮我连接 Gmail，然后看看最近一周的邮件”。
5. **登录平台**：浏览器会打开登录页。如果发布者已开启 Google 或 GitHub 登录，可直接用这些账号登录，首次登录自动开户。
6. **授权第三方服务**：在服务商的官方页面确认权限并同意，看到授权成功的提示后回到 WorkSwarm。
   整个过程请使用同一个浏览器，中途不要刷新页面。

每个服务只需授权一次，之后直接提问即可。

### 可以做什么

所有操作都是只读的。用自然语言提出需求即可，agent 会选择对应的操作。

| 服务 | 示例说法 | 可用操作 |
|---|---|---|
| Gmail | “帮我连接 Gmail，看看最近的邮件” | `gmail_search`、`gmail_read` |
| Google Drive | “列出我云端硬盘里的文件” | `drive_list` |
| Google Calendar | “列出我的日历” | `calendar_list` |
| GitHub | “列出我的 GitHub 仓库” | `github_profile`、`github_repos` |
| Microsoft（Outlook） | “看看我的 Outlook 邮件” | `microsoft_profile`、`outlook_list` |
| Slack | “查看我的 Slack 连接身份” | `slack_identity` |

### 管理连接

| 想做的事 | 对 agent 说 | 结果 |
|---|---|---|
| 查看连接状态 | “看看我连接了哪些服务” | 列出每个连接的状态；`authorization=authorized` 且 `validation=passed` 才算可用 |
| 重新登录平台 | “调用 auth_login 重新登录” | 打开平台登录页；已有连接不受影响，不需要重新授权 |
| 连接同一服务的另一个账号 | “用新的连接 ID 再连接一个 Gmail 账号” | 两个账号分别保存，互不影响 |
| 断开连接 | “断开我的 GitHub 连接” | 删除服务器上保存的凭证，并尝试在服务商处撤销授权 |

### 常见问题

| 现象 | 处理 |
|---|---|
| 连接立即失败，提示找不到 `py` | 安装 python.org 版 Python（包含 `py` 启动器），然后重启 WorkSwarm |
| 首次连接失败 | 多半是无法访问 PyPI 或发布者服务器；查看 WorkSwarm 日志中以 `[workswarm-auth]` 开头的行 |
| agent 说找不到工具 | 当前会话没有勾选本连接器 |
| `workswarm_login_required` | 平台会话已失效，调用 `auth_login` 重新登录 |
| `cloud_service_unavailable` | 连不上发布者服务器，检查网络后重试 |
| `operation_denied_by_policy` 或 `operation_not_allowed` | 操作名不对，不代表账号被禁止；改用错误信息里 `allowed_operations` 列出的操作 |
| `connection_link_expired_or_used` | 连接链接只能用一次，让 agent 重新发起连接 |
| `reauthorization_required` | 第三方授权已失效，让 agent 重新连接该服务 |

## 发布者需要做什么

以下步骤面向部署服务并上架连接器的人。源码包不含已部署的服务地址，连接器包必须用你自己的部署配置生成。
下面的命令和域名都是示例，第三方应用配置和完整参数见 [CLOUD.md](CLOUD.md)。

### 准备

- 一台 Linux 主机，安装 Docker Engine 和 Compose v2，能访问身份系统和第三方服务。
- 一个授权服务域名（如 `auth.company.example`），DNS 指向这台主机，80/443 对外可达。
- 平台登录二选一：已有的 OIDC 身份系统；或再准备一个域名（如 `login.company.example`），部署附带的 Keycloak。
- 要启用的第三方服务的开发者账号（Google、GitHub、Microsoft、Slack）。

### 发布步骤

**1. 安装源码并运行测试**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[server,test]'
.venv/bin/python -m pytest -q
```

**2. 生成部署配置**

```bash
.venv/bin/python tools/publisher_setup.py \
  --public-origin https://auth.company.example \
  --identity-domain login.company.example \
  --providers gmail,google-drive,google-calendar,github,microsoft,slack \
  --output /srv/workswarm-auth-runtime
```

使用已有身份系统时，把 `--identity-domain` 换成 `--issuer`（对接要求见 CLOUD.md 第 1 节）。
输出目录含密钥和配置，不要提交到仓库，也不要发给用户。

**3. 配置第三方服务的 Client ID 和 Secret**

每个服务都需要发布者创建一个自己的 OAuth 应用，所有用户共用，用户不需要接触这些信息。
只配置第 2 步 `--providers` 里启用的服务。把下面的 `auth.company.example` 换成你的授权服务域名。

| 服务 | 回调地址 | 需要的权限 |
|---|---|---|
| Gmail | `https://auth.company.example/oauth/providers/gmail/callback` | `openid`、`email`、`gmail.readonly` |
| Google Drive | `https://auth.company.example/oauth/providers/google-drive/callback` | `openid`、`email`、`drive.metadata.readonly` |
| Google Calendar | `https://auth.company.example/oauth/providers/google-calendar/callback` | `openid`、`email`、`calendar.calendarlist.readonly` |
| GitHub | `https://auth.company.example/oauth/providers/github/callback` | `read:user` |
| Microsoft | `https://auth.company.example/oauth/providers/microsoft/callback` | `User.Read`、`Mail.Read`、`offline_access` |
| Slack | `https://auth.company.example/oauth/providers/slack/callback` | `team:read`（Bot） |

回调地址必须和表中完全一致，否则授权时会报错。

*Google（Gmail、Drive、Calendar 可共用一个客户端）*

1. 在 [Google Cloud Console](https://console.cloud.google.com/) 新建项目，启用 Gmail API、Google Drive API 和 Google Calendar API。
2. 配置 OAuth 同意屏幕，受众选“外部”，在数据访问中加入上表的权限。
3. 测试阶段把测试账号加入“测试用户”。
4. 创建 OAuth 客户端，类型选 **Web application**，把三个回调地址都加入“已获授权的重定向 URI”。
5. 保存 Client ID 和 Client Secret。

*GitHub*

1. 打开 **Settings → Developer settings → OAuth Apps → New OAuth App**。
2. Homepage URL 填 `https://auth.company.example`，Authorization callback URL 填上表的地址。
3. 注册后点 **Generate a new client secret**，保存 Client ID 和 Client Secret。

权限由服务端在授权时申请，不用在 GitHub 上配置。

*Microsoft*

1. 在 [Microsoft Entra 管理中心](https://entra.microsoft.com/) 打开 **应用注册 → 新注册**。
2. 帐户类型选“任何组织目录中的帐户和个人 Microsoft 帐户”，重定向 URI 平台选 **Web**，填上表的地址。
3. 在 **API 权限** 中添加 Microsoft Graph 的委托权限：`User.Read`、`Mail.Read`、`offline_access`。
4. 在 **证书和密码** 中新建客户端密码，立即复制 **值**（约 40 个字符），不要复制“机密 ID”（36 位 GUID）。
5. 在 **概述** 页复制“应用程序（客户端）ID”，它就是 Client ID。

*Slack*

1. 在 [Slack API](https://api.slack.com/apps) 点 **Create New App → From a manifest**，选择工作区，粘贴以下内容：

   ```json
   {
     "display_information": { "name": "WorkSwarm Auth" },
     "features": { "bot_user": { "display_name": "WorkSwarm Auth" } },
     "oauth_config": {
       "redirect_urls": ["https://auth.company.example/oauth/providers/slack/callback"],
       "scopes": { "bot": ["team:read"] }
     },
     "settings": { "token_rotation_enabled": false }
   }
   ```

2. 在 **Basic Information** 中保存 Client ID 和 Client Secret，不要使用 Signing Secret。

*录入服务器*

在服务器上运行：

```bash
.venv/bin/python tools/configure_cloud_providers.py \
  --config /srv/workswarm-auth-runtime/config/providers.json
```

工具会按启用的服务逐个提示，Secret 输入时不显示：

```text
gmail: leave both blank to retain its current configuration.
Client ID: <输入 Client ID>
Client Secret (hidden): <输入 Client Secret>
google-drive: leave both blank to retain its current configuration.
Client ID: ...
```

- Gmail、Drive、Calendar 共用客户端时，三次都输入同一组值。
- 两项都留空表示保留原有配置；只填一项会报错退出，且不保存任何内容。
- 看到 `Publisher configuration saved` 才算保存成功。
- Secret 只留在服务器上，不要写进聊天、命令行参数或连接器包。

服务启动后（第 4 步）再修改凭证时，配置文件已归容器用户所有，要用 `sudo` 运行上面的命令，然后重启 broker 才会生效：

```bash
cd /srv/workswarm-auth-runtime
docker compose -f compose.yaml -f compose.identity.yaml restart broker
```

使用已有身份系统时，去掉 `-f compose.identity.yaml`。

用户登录后调用 `auth_providers`，可以检查配置结果：每个服务应显示 `configured: true`。
显示 `provider_not_configured` 或 `provider_client_secret_required` 表示凭证没有录入完整。

**4. 启动服务**

在生成的 `.env` 中设置 `WORKSWARM_SOURCE`（源码绝对路径）；使用附带 Keycloak 时还要设置 `KEYCLOAK_IMAGE`。

```bash
sudo chown -R 10001:10001 /srv/workswarm-auth-runtime/config \
  /srv/workswarm-auth-runtime/data /srv/workswarm-auth-runtime/secrets
cd /srv/workswarm-auth-runtime
docker compose -f compose.yaml -f compose.identity.yaml up -d --build
```

使用已有身份系统时只运行 `docker compose -f compose.yaml up -d --build`。

**5. 开通用户账号**（使用附带 Keycloak 时）

默认由管理员在 Keycloak 创建用户并加入 `workswarm-users` 组。也可以让用户用 Google 或 GitHub 登录，首次登录自动开户。
登录用的应用要和第 3 步的应用分开创建：

| 登录方式 | 在哪里创建 | 回调地址 |
|---|---|---|
| Google | 新的 Google Cloud 项目，类型选 Web application，权限只需 `openid`、`email`、`profile` | `https://login.company.example/realms/workswarm/broker/google/endpoint` |
| GitHub | 新的 OAuth App，权限由服务端在登录时申请 | `https://login.company.example/realms/workswarm/broker/github/endpoint` |

用户较多时，把 Google 登录应用单独放一个项目：这样它不受 Gmail、Drive 审核和测试用户名单的限制。

然后在服务器上运行，工具会打印回调地址，并逐个提示输入 Client ID 和 Secret：

```bash
.venv/bin/python tools/configure_login_providers.py \
  --runtime /srv/workswarm-auth-runtime --providers google,github
```

开启后，凡是能通过对应应用登录的人都会被准入。不需要时加 `--no-auto-enroll`，改由管理员手动加组。

**6. 生成客户端包和连接器包**

```bash
.venv/bin/python tools/build_cloud_client.py \
  --profile /srv/workswarm-auth-runtime/cloud.json \
  --output /srv/workswarm-auth-releases/0.7.2
```

| 生成的文件 | 用途 |
|---|---|
| `workswarm_auth-0.7.2-py3-none-any.whl` | 复制到 `/srv/workswarm-auth-runtime/data/client/`，并确保 broker 容器用户（uid 10001）可读；用户首次连接时从这里下载 |
| `workswarm-auth-cloud-market-0.7.2.zip` | 上架到 WorkSwarm Hub 的连接器包 |
| `workswarm-auth-cloud-windows-0.7.2.zip` | 不用市场时的手工安装包，解压后运行 `Install-WorkSwarm-Auth.cmd` |

**7. 上架**

把连接器包上传到 WorkSwarm Hub，并选择发布范围（见下一节）。

**8. 实机验收**

在一台干净的 Windows 上从市场安装，完成平台登录、第三方授权和一次 `service_call`。
再用两个账号确认彼此看不到对方的连接。完整标准见 CLOUD.md 第 6 节。

### 选择发布范围

| | 自己或小团队 | 公开发布 |
|---|---|---|
| WorkSwarm Hub | 可不经过 Hub，直接分发包内的 `mcp.json`，由用户粘贴添加；Private 发布的条目能否从广场安装取决于客户端版本，需先实测 | Public，需经过 Hub 审核 |
| Google | Testing 模式，需添加测试用户，授权约 7 天过期 | 需通过 OAuth 验证；Gmail、Drive 使用受限权限，通常还需第三方安全评估 |
| Microsoft | 测试账号可用 | 通常需完成发布者验证，否则许多企业账号无法同意授权 |
| Slack | 开发工作区可用 | 需开启 Distribution，才能安装到其他工作区 |
| GitHub | 可用 | 可用 |

第三方服务的审核要求可能变化，请以各服务商当前规定为准。建议先按小团队范围跑通验收，再申请公开。

### 公开发布前要确认

- **准入范围**：确认自动开户是否符合预期，或改用 `--no-auto-enroll`。
- **合规材料**：准备隐私政策和应用主页（Google 验证需要），并为代码选择许可证。
- **下载校验**：启动器下载 wheel 时不校验哈希，依赖版本也未锁定；公开发布前应补上，或评估这一风险。
- **数据安全**：服务端是单实例 SQLite，用户的第三方凭证加密存放其中。需要自行备份数据目录和 `secrets/vault.key`，并限制访问；两者不要放在一起备份。
- **支持平台**：连接器目前只支持 Windows。

### 发布后的维护

- **更新客户端**：客户端按版本号缓存，改动后必须升版本号，重新执行第 6、7 步，否则已安装的用户不会更新。只改客户端时不需要重启服务。
- **更换服务地址**：已安装的用户会继续使用旧地址，需要删除 `%USERPROFILE%\.workswarm-auth-cloud` 后重新登录。第三方应用的回调地址也要同步修改。
- **保护部署目录**：重新生成 `vault.key` 会导致已保存的凭证无法解密，所以 `publisher_setup.py` 不允许覆盖已有目录。
