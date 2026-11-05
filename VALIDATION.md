# v0.7.2 验证记录

## 单元测试

隔离 Linux / Python 3.12，`pytest -q`：**83 passed，1 warning**（Starlette TestClient 的 httpx
弃用提示）。相对 0.6.0：移除 1 项远程 MCP 协议测试；新增多提供方渲染、客户端 wheel 下载路由、
wheel 内置 profile 初始化、Google/GitHub 登录配置共 4 项，并扩充发行包测试（wheel 内容与 RECORD 校验、
市场引导程序解码与版本/地址）。

## 本次修补与清理

| 项目 | 内容 |
|---|---|
| `publisher_setup.py` | `realm.json` 生成为 0644（Keycloak 容器用户需读取）；realm 显式定义 `basic` scope（oidc-sub-mapper），否则 Keycloak 25+ 的 access token 没有 `sub`；新增 `--providers` |
| `build_cloud_client.py` | 市场包 manifest 补充 WorkSwarm 要求的 `display_description` |
| `configure_cloud_providers.py` | 逐个提示 providers.json 中的全部提供方，不再只限 gmail/github |
| 0.7.2 操作名提示 | `service_call` 说明操作名来自 `auth_providers`；操作名不对时返回 `allowed_operations` 和下一步提示 |
| 0.7.1 重新登录 | 新增 MCP 工具 `auth_login`：只做平台登录，不发起第三方授权；`workswarm_login_required` 错误附带下一步提示 |
| 0.7.0 市场自动安装 | broker 新增 `/client/<wheel>` 下载；构建器生成带 profile 的客户端 wheel；市场包改为 `py -3` 引导程序，首次连接自动安装；客户端从 wheel 内置 profile 自动初始化 |
| 0.7.0 平台账号 | 新增 `tools/configure_login_providers.py`：Keycloak 接入 Google/GitHub 登录，默认组自动开户 |
| 移除远程 MCP 传输 | 删除 `remote_mcp.py`、`/mcp` 与 `/.well-known/oauth-protected-resource/mcp`；`security.py` 不再依赖 MCP 库的令牌类型 |

## 实机验收（2026-11-02 至 11-03）

环境：Linux Docker（Keycloak 26.4 + PostgreSQL 17 + Caddy 测试 CA + broker），Windows 上的
WorkSwarm 客户端经 SSH 隧道访问，服务地址 `https://localhost:8443` / `https://localhost:8444`。

| 项目 | 结果 |
|---|---|
| Docker 端到端（模拟 GitHub）：登录、连接、校验、转发链接 403、重放拒绝、跨用户隔离、非群组拒绝、断开吊销 | 通过 |
| Windows 安装、测试 CA 信任、平台登录、WorkSwarm 自定义 MCP 接入（7 个工具） | 通过 |
| Gmail：gmail_search、gmail_read | 通过 |
| GitHub：github_profile、github_repos；断开时 `DELETE /applications/{id}/token` 返回 204 | 通过 |
| Google Drive：drive_list | 通过 |
| Google Calendar：calendar_list | 通过 |
| Slack：slack_identity | 通过 |
| Microsoft：microsoft_profile、outlook_list | 通过 |
| 清理后的 broker：全部 7 个已有连接 `auth_validate` 均为 passed；`/mcp` 返回 404 | 通过 |
| 0.7.0 市场引导（Linux 模拟 WorkSwarm 启动 mcp.json）：空环境首次启动自动安装并就绪 21.4 秒，二次启动 1.9 秒；自动生成 cloud.json；平台登录与 auth_connect 正常 | 通过 |
| 0.7.0 Google 登录：Keycloak 登录页出现 Google 按钮，跳转 Google 无错误；新用户自动加入 workswarm-users | 通过（真实 Google 账号登录待 Windows 实测） |
| 0.7.1 `auth_login`（真实 Keycloak 与授权服务，脚本化浏览器）：未登录时查询不打开浏览器并返回提示；登录成功；再次调用不重复打开浏览器；已有连接可见 | 通过 |
| 0.7.1 市场引导（Linux 模拟 WorkSwarm 启动 mcp.json，从真实服务下载）：首次 13.7 秒、二次 1.8 秒就绪，8 个工具 | 通过 |
| 0.7.2 操作名提示（真实授权服务）：错误操作名返回该连接的 `allowed_operations`，请求未到第三方 | 通过 |
| Windows 上的 `auth_login` 流程（0.7.1：未登录提示、重新登录、已有连接保留） | 通过 |
| WorkSwarm 中 agent 根据 `allowed_operations` 自行改正操作名 | 待实测 |
| Windows 上的 `py` 启动器、系统证书库下载与 WorkSwarm 市场安装 | 待实测 |

## 边界

- 云端存储与审计为单实例 SQLite，未做 HA/KMS/生产安全审查。
- Google 应用处于 Testing 状态时授权 7 天后失效；公开发行需完成 Google 验证。
- Microsoft 没有吊销接口，断开时返回手动撤销链接。GitHub 断开只吊销本连接的令牌，
  同一 App 的其他授权仍在时，再次连接可能不出现 Authorize 页。

部署与实机验收步骤见 [CLOUD.md](CLOUD.md)。
