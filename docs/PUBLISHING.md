# 发布指南

面向部署授权服务并上架连接器的发布者。命令里的域名和路径都是示例，请换成你自己的。

| 开发者 | 发布者 |
|---|---|
| 维护代码、修复 Bug、发布新版本，解答部署中的技术问题 | 提供主机、域名和证书；创建第三方应用并完成服务商审核 |
| | 运行服务、备份数据、管理用户账号、负责数据合规；构建并上架连接器 |

开发者不需要你的 Client Secret、`vault.key` 或 `.env`，请不要发送。

## 准备

- 一台 Linux 主机：Docker Engine、Compose v2、Git、Python 3.11 或更高版本；能访问身份系统和第三方服务。
  Ubuntu、Debian 还要运行 `sudo apt install python3-venv`。
- 授权服务域名（如 `auth.company.example`），DNS 指向这台主机，80/443 对外可达。
- 平台登录二选一：已有 OIDC 身份系统；或再准备一个域名（如 `login.company.example`），部署附带的 Keycloak。
- 要启用的第三方服务的开发者账号。
- 一台装有 WorkSwarm 和 Python 3.11+（带 `py` 启动器）的 Windows，用于上架和验收；一个 WorkSwarm Hub 账号。
- 本仓库的读取权限，向仓库所有者申请。

## 部署步骤

除非命令里写了 `cd`，都在源码目录下运行。

### 1. 获取源码并运行测试

```bash
git clone <仓库地址> workswarm-auth
cd workswarm-auth
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[server,test]'
.venv/bin/python -m pytest -q
```

### 2. 生成部署配置

`--output` 目录不能已存在，当前用户要对上级目录有写权限。`/srv` 默认只有 root 可写，主机专用于本服务时可以先交给当前用户：

```bash
sudo chown "$(id -un)" /srv
.venv/bin/python tools/publisher_setup.py \
  --public-origin https://auth.company.example \
  --identity-domain login.company.example \
  --providers gmail,google-drive,google-calendar,github,microsoft,slack \
  --output /srv/workswarm-auth-runtime
```

使用已有身份系统时，把 `--identity-domain` 换成 `--issuer`（要求见 [CLOUD.md](../CLOUD.md) 第 1 节）。
输出目录含密钥，不要提交到仓库或发给别人。

### 3. 创建第三方应用并录入凭证

每个服务创建一个 OAuth 应用，所有用户共用。只需配置第 2 步 `--providers` 里启用的服务。
回调地址必须和下表完全一致：

| 服务 | 回调地址 | 权限 |
|---|---|---|
| Gmail | `https://auth.company.example/oauth/providers/gmail/callback` | `openid`、`email`、`gmail.readonly` |
| Google Drive | `https://auth.company.example/oauth/providers/google-drive/callback` | `openid`、`email`、`drive.metadata.readonly` |
| Google Calendar | `https://auth.company.example/oauth/providers/google-calendar/callback` | `openid`、`email`、`calendar.calendarlist.readonly` |
| GitHub | `https://auth.company.example/oauth/providers/github/callback` | `read:user` |
| Microsoft | `https://auth.company.example/oauth/providers/microsoft/callback` | `User.Read`、`Mail.Read`、`offline_access` |
| Slack | `https://auth.company.example/oauth/providers/slack/callback` | `team:read`（Bot） |

**Google**（Gmail、Drive、Calendar 可共用一个客户端）

1. 在 [Google Cloud Console](https://console.cloud.google.com/) 新建项目，启用 Gmail API、Google Drive API、Google Calendar API。
2. 配置 OAuth 同意屏幕：受众选“外部”，在数据访问中加入上表的权限；测试阶段把测试账号加入“测试用户”。
3. 创建 OAuth 客户端，类型选 **Web application**，加入三个回调地址。保存 Client ID 和 Client Secret。

**GitHub**

1. **Settings → Developer settings → OAuth Apps → New OAuth App**。
2. Homepage URL 填 `https://auth.company.example`，callback URL 填上表地址。
3. 点 **Generate a new client secret**，保存 Client ID 和 Client Secret。权限由服务端申请，不用在 GitHub 上配置。

**Microsoft**

1. 在 [Microsoft Entra 管理中心](https://entra.microsoft.com/) 打开 **应用注册 → 新注册**。
2. 帐户类型选“任何组织目录中的帐户和个人 Microsoft 帐户”，重定向 URI 平台选 **Web**，填上表地址。
3. 在 **API 权限** 中添加 Microsoft Graph 委托权限：`User.Read`、`Mail.Read`、`offline_access`。
4. 在 **证书和密码** 中新建客户端密码，复制 **值**（约 40 个字符），不是“机密 ID”（36 位 GUID）。
5. **概述** 页的“应用程序（客户端）ID”就是 Client ID。

**Slack**

1. 在 [Slack API](https://api.slack.com/apps) 点 **Create New App → From a manifest**，选择工作区，粘贴：

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

2. 在 **Basic Information** 中保存 Client ID 和 Client Secret（不是 Signing Secret）。

**录入服务器**

```bash
.venv/bin/python tools/configure_cloud_providers.py \
  --config /srv/workswarm-auth-runtime/config/providers.json
```

工具会按服务逐个提示输入 Client ID 和 Secret，Secret 输入时不显示，看到 `Publisher configuration saved` 才算成功。

- Google 三个服务共用客户端时，三次输入同一组值。
- 两项都留空表示保留原配置；只填一项会报错退出，不保存任何内容。
- Secret 只留在服务器上，不要写进聊天、命令行参数或连接器包。
- 服务启动后再修改，要用 `sudo` 运行，并重启 broker（`$C restart broker`，`$C` 见第 4 步）。

### 4. 启动服务

编辑 `/srv/workswarm-auth-runtime/.env`：

```text
WORKSWARM_SOURCE=/源码的绝对路径/workswarm-auth
KEYCLOAK_IMAGE=quay.io/keycloak/keycloak:26.4
```

`KEYCLOAK_IMAGE` 只在使用附带 Keycloak 时需要，版本需 25 或更高（26.4 已实测），生产环境建议固定镜像 digest。

```bash
sudo chown -R 10001:10001 /srv/workswarm-auth-runtime/config \
  /srv/workswarm-auth-runtime/data /srv/workswarm-auth-runtime/secrets
cd /srv/workswarm-auth-runtime
C="docker compose -f compose.yaml -f compose.identity.yaml"   # 使用已有身份系统时：C="docker compose -f compose.yaml"
$C up -d --build
$C ps
curl -sS https://auth.company.example/health
```

本文后面的 `$C` 都指这条命令，在部署目录下运行；新开终端要重新设置。
所有容器应为 running 或 healthy，`/health` 应返回 `"ok":true`。这只说明进程正常，不代表第三方应用已配置正确。

### 5. 开通用户账号（使用附带 Keycloak 时）

Keycloak 没有预置用户，也关闭了公开注册。不在 `workswarm-users` 组的用户无法使用连接器。

**方式一：管理员手动建号**（适合少量用户）

1. 查看管理员账号（会在屏幕上显示密码）：`grep '^KC_BOOTSTRAP_ADMIN_' /srv/workswarm-auth-runtime/.env`
2. 打开 `https://login.company.example/admin/`，切换到 `workswarm` realm。
3. 在 **Users** 新建用户，在 **Groups** 加入 `workswarm-users`，在 **Credentials** 设置初始密码。

不要把管理员账号当普通用户使用。

**方式二：Google 或 GitHub 登录，首次登录自动开户**（适合用户较多）

登录用的应用要和第 3 步的应用分开创建：

| 登录方式 | 创建 | 回调地址 |
|---|---|---|
| Google | 新的 Google Cloud 项目，Web application，权限只需 `openid`、`email`、`profile` | `https://login.company.example/realms/workswarm/broker/google/endpoint` |
| GitHub | 新的 OAuth App | `https://login.company.example/realms/workswarm/broker/github/endpoint` |

Google 登录应用单独放一个项目，就不受 Gmail、Drive 审核和测试用户名单的限制。然后运行：

```bash
.venv/bin/python tools/configure_login_providers.py \
  --runtime /srv/workswarm-auth-runtime --providers google,github
```

开启后，能通过这些应用登录的人都会被准入。不需要自动开户时加 `--no-auto-enroll`，改由管理员加组。

### 6. 构建客户端包和连接器包

```bash
.venv/bin/python tools/build_cloud_client.py \
  --profile /srv/workswarm-auth-runtime/cloud.json \
  --output /srv/workswarm-auth-releases/0.7.2
```

| 生成的文件 | 用途 |
|---|---|
| `workswarm_auth-0.7.2-py3-none-any.whl` | 客户端包，放到服务器上供用户首次连接时下载 |
| `workswarm-auth-cloud-market-0.7.2.zip` | 连接器包，上架到 WorkSwarm Hub |

放置客户端包，并确认能下载（应输出 `200`）：

```bash
sudo install -D -o 10001 -g 10001 -m 0644 \
  /srv/workswarm-auth-releases/0.7.2/workswarm_auth-0.7.2-py3-none-any.whl \
  /srv/workswarm-auth-runtime/data/client/workswarm_auth-0.7.2-py3-none-any.whl
curl -sS -o /dev/null -w '%{http_code}\n' https://auth.company.example/client/workswarm_auth-0.7.2-py3-none-any.whl
```

### 7. 上架

先把连接器包复制到装有 WorkSwarm 的 Windows，例如：

```powershell
scp <用户>@<主机>:/srv/workswarm-auth-releases/0.7.2/workswarm-auth-cloud-market-0.7.2.zip .
```

然后任选一种方式发布：

- **在 Hub 上传 zip**：在 Hub 发布页上传，按提示填写信息并选择发布范围。界面以 Hub 实际显示为准。
- **在 WorkSwarm 客户端发布**：
  1. 解压到本机连接器目录：
     ```powershell
     Expand-Archive .\workswarm-auth-cloud-market-0.7.2.zip -DestinationPath "$env:USERPROFILE\.jiuwenswarm\agent\workspace\mcp\mcp_hub" -Force
     ```
  2. 重启 WorkSwarm，在 **扩展 → 连接器广场** 搜索 `第三方统一授权`，打开详情页，点 **发布** 并登录 Hub。
  3. 确认名称和简介，选择发布范围，提交。版本号沿用包内的 `0.7.2`。

  不要用“新建自定义连接器”来发布：它只接受 `npx`、`uvx`、`python`、`python3` 启动命令，本连接器用的是 `py`。
  这种方式根据 WorkSwarm 客户端代码整理，界面名称可能随版本变化。

提交后到 Hub 的 **我的上传** 查看实际状态。Public 发布需审核通过才会展示。再次发布要升版本号，格式为三段数字（如 `0.7.3`）或七位小写提交号。

### 8. 验收

在一台干净的 Windows 上从市场安装，完成平台登录、第三方授权和一次 `service_call`；
再用两个账号确认彼此看不到对方的连接。完整标准见 [CLOUD.md](../CLOUD.md) 第 6 节。

## 上线审核与认证

以下是常见要求的整理，不是法律意见；以服务商、认证机构和当地法规的当前规定为准。
建议先在小团队范围跑通验收，再申请公开。

### WorkSwarm Hub 与第三方平台

| 平台 | 自己或小团队 | 公开发布 | 企业客户可能额外要求 |
|---|---|---|---|
| WorkSwarm Hub | Private；能否从连接器广场安装取决于客户端版本，不能安装时按第 7 步解压到 `mcp_hub` | Public，需 Hub 审核 | — |
| Google | Testing 模式，需添加测试用户，授权约 7 天过期 | OAuth 应用验证：已验证域名、主页、隐私政策、权限用途说明、演示视频，并遵守 Limited Use 政策。Gmail、Drive 是受限权限，通常还需 **CASA 安全评估，每年复评** | — |
| Microsoft | 测试账号可用 | 发布者验证（加入微软合作伙伴计划并验证域名），否则很多企业租户会拦截；读邮件可能还需租户管理员同意 | Microsoft 365 应用合规（Publisher Attestation、Microsoft 365 Certification） |
| Slack | 开发工作区可用 | 开启 Distribution 才能装到其他工作区，不需要审核 | Slack Marketplace 上架审核（可选） |
| GitHub | 可用 | 可用，不需要审核 | GitHub Marketplace 上架审核（可选） |

### 安全与合规认证

授权服务代管用户的邮箱、云盘等凭证，企业客户的安全审查通常会问到。按目标市场选择：

| 认证 | 适用场景 | 说明 |
|---|---|---|
| SOC 2 Type II | 海外企业客户 | 会计师事务所审计一段时间（通常 3–12 个月）内的控制执行情况，每年更新 |
| ISO/IEC 27001 | 通用 | 信息安全管理体系，三年一周期，每年监督审核；云服务可加 27017、27018 |
| 等保 2.0 | 在中国境内运营 | 定级、备案、测评；处理邮件等个人数据的系统常见二级或三级，三级每年测评 |
| PIPL、GDPR 等 | 处理当地用户个人信息 | 隐私政策、用户同意、数据处理协议（DPA）；数据存放地和跨境问题需法务评估 |
| ICP 备案 | 服务器在中国大陆 | 授权服务和平台登录的域名都要备案 |

### 上线前要确认或补齐

- **准入范围**：确认自动开户符合预期，否则用 `--no-auto-enroll`。
- **使用授权**：仓库还没有 LICENSE 文件，上架前向代码所有者确认。
- **凭证保护**：`vault.key` 是部署目录里的文件；认证通常要求改用 KMS 或 HSM，并支持密钥轮换。
- **高可用与恢复**：服务端是单实例 SQLite，需要多实例、数据库高可用和演练过的恢复流程。
- **供应链**：启动器下载客户端不校验哈希，依赖版本未锁定；还需要依赖清单（SBOM）和漏洞披露渠道。
- **审计与运维**：审计记录只在本地数据库中，需要导出到集中日志，并设置告警和保留期限。
- **安全测试**：CASA、SOC 2 等审查通常要求第三方渗透测试报告。

## 维护

- **更新客户端**：客户端按版本号缓存，改动后必须升版本号，重新执行第 6、7 步。只改客户端时不用重启服务。
- **更换服务地址**：已安装的用户会继续用旧地址，需删除 `%USERPROFILE%\.workswarm-auth-cloud` 后重新登录；第三方应用的回调地址也要同步修改。
- **不要重新生成部署配置**：新的 `vault.key` 会让已保存的凭证无法解密，所以 `publisher_setup.py` 不允许覆盖已有目录。

### 备份

需要备份 `data/`（连接凭证和审计记录）、`config/`（第三方应用凭证）、`secrets/vault.key`、`.env` 和 Keycloak 数据库。
`data/` 和 `vault.key` 要放在不同位置：

```bash
$C stop broker
sudo tar czf /备份目录/broker-data.tgz data config
sudo tar czf /另一个备份目录/broker-secrets.tgz secrets .env
$C exec -T identity-db pg_dump -U keycloak keycloak | gzip > /备份目录/keycloak.sql.gz
$C start broker
```

### 恢复

1. 在同一路径用 `sudo tar xzf` 解压两个备份（保留文件属主），然后 `$C up -d identity-db`。
2. 在 `identity` 启动前导入数据库：`gunzip -c /备份目录/keycloak.sql.gz | $C exec -T identity-db psql -U keycloak keycloak`。
3. 运行 `$C up -d`，用已有账号验证 `auth_status` 和一次 `service_call`。

备份和恢复命令未经演练，正式使用前请先在测试机上走一遍。

### 升级服务端

1. 先备份。
2. 在源码目录 `git pull`，然后在部署目录 `$C up -d --build`。不要重新运行 `publisher_setup.py`。
3. 用 `/health` 和一次真实连接确认。客户端也有改动时，升版本号后重新执行第 6、7 步。

修改 `realm.json` 不会影响已创建的 realm，已有配置要在 Keycloak 管理台修改。

## 排查

查看容器状态和日志（发给别人前先去掉敏感信息）：

```bash
$C ps
$C logs --tail=100 proxy
$C logs --tail=100 broker
$C logs --tail=100 identity
```

| 现象 | 原因和处理 |
|---|---|
| 提示 `Set WORKSWARM_SOURCE` 或 `Set an approved Keycloak image` | `.env` 没有填写，见第 4 步 |
| 读取 `config/config.env` 时 `permission denied` | `config/` 已归容器用户所有，用 `sudo` 运行 `docker compose` |
| HTTPS 打不开，`proxy` 日志显示证书申请失败 | 域名没有解析到本机，或 80/443 被防火墙拦截 |
| `identity` 读取 `realm.json` 时 Permission denied | `realm.json` 权限应为 `0644` |
| `Invalid host header` | 访问的域名和 `--public-origin` 不一致 |
| `cloud_identity_unavailable` | Keycloak 没有启动好，或登录域名无法访问 |
| `cloud_issuer_mismatch` | 身份系统的 issuer 和连接器包不一致，确认部署和构建用的是同一份 `cloud.json` |
| `cloud_session_verification_pending` | Broker 拒绝了平台令牌：确认 Keycloak ≥ 25、用户在 `workswarm-users` 组；已有身份系统还要检查 audience 和 `sub` |
| `cloud_login_callback_port_in_use` | 用户电脑的 18766 端口被占用 |
| 下载客户端失败，`/client/` 返回 404 | 没有放客户端包，或版本和连接器包不一致，见第 6 步 |
| `provider_not_configured`、`provider_client_secret_required` | 凭证没有录入完整；重新录入后重启 broker |
| 服务商提示回调地址不匹配（如 `redirect_uri_mismatch`） | 服务商后台登记的地址和第 3 步表格不完全一致 |
| Microsoft 同意后授权失败 | 录入的是“机密 ID”，要改成客户端密码的“值” |
| Google 提示无权访问 | 应用处于 Testing 状态，账号不在测试用户名单里 |

用户登录后调用 `auth_providers`，每个已启用的服务应显示 `configured: true`。
