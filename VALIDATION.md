# v0.7.2 验证范围

## 单元测试

在 Linux / Python 3.12 上运行 `pytest -q`：**83 passed**。覆盖签名 OIDC 响应、真实 loopback HTTP 回调、broker 路由、
模拟第三方 API、客户端 wheel 与连接器包内容。

## 已验证

| 功能 | 验证方式 | 结果 |
|---|---|---|
| 平台登录、发起连接、API 校验、断开并吊销 | Docker 环境，真实 Keycloak，模拟 GitHub | 通过 |
| 转发连接链接被拒绝、重放被拒绝、跨用户隔离、不在准入组的用户被拒绝 | 同上 | 通过 |
| Gmail：`gmail_search`、`gmail_read` | Windows WorkSwarm，真实 API，0.6.0 客户端 | 通过 |
| Google Drive：`drive_list` | 同上 | 通过 |
| Google Calendar：`calendar_list` | 同上 | 通过 |
| GitHub：`github_profile`、`github_repos`，断开时吊销令牌 | 同上 | 通过 |
| Microsoft：`microsoft_profile`、`outlook_list` | 同上 | 通过 |
| Slack：`slack_identity` | 同上 | 通过 |
| 首次连接自动安装客户端：首次约 14–21 秒就绪，之后约 2 秒，共 8 个工具 | Linux 模拟 WorkSwarm 启动 `mcp.json` | 通过 |
| `auth_login`：未登录时查询不打开浏览器，重新登录后已有连接保留 | 真实 Keycloak 和脚本化浏览器；Windows WorkSwarm 上的 0.7.1 客户端 | 通过 |
| 操作名错误时返回 `allowed_operations`，请求不发往第三方 | 真实授权服务 | 通过 |
| Google 登录入口：登录页显示按钮并可跳转到 Google | 真实 Keycloak | 通过 |

## 尚未验证

- 使用 `docker compose` 启动完整服务，以及真实域名和公网证书
- 0.7.2 客户端下六个服务的完整回归
- 真实 Google、GitHub 账号登录平台并自动开户
- Windows 上首次连接自动下载并安装客户端
- 从 WorkSwarm 连接器广场安装
- agent 根据 `allowed_operations` 自行改正操作名

## 已知边界

- 服务端存储与审计为单实例 SQLite，未做高可用、KMS 或生产安全审查。
- 启动器下载客户端时不校验哈希，依赖版本未锁定。
- Google 应用处于 Testing 状态时，授权约 7 天后失效；公开发行需完成 Google 验证。
- Microsoft 没有吊销接口，断开时返回手动撤销链接。
- GitHub 断开只吊销本连接的令牌；同一 App 的其他授权仍在时，再次连接可能不出现 Authorize 页。

部署与验收步骤见 [发布指南](docs/PUBLISHING.md)。
