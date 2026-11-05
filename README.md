# WorkSwarm 统一授权 Connector v0.7.2

仅保留一条链路：**WorkSwarm → 云端授权服务 → 第三方 API**。
发布者在服务器配置第三方应用，普通用户不填写第三方 Client ID、Secret 或 token。
支持 Gmail、Google Drive、Google Calendar、GitHub、Microsoft（Outlook）、Slack，均为只读操作。

| 组件 | 职责 |
|---|---|
| 服务端 `app / manager / providers / vault / security` | 第三方 OAuth、凭证加密、刷新/撤销、账号隔离、API 验证；强制 OIDC，不接受静态 API Key |
| 客户端 `cloud / cloud_commands / cloud_mcp / session_store` | 平台 OIDC 登录、浏览器跳转、授权等待/取消和 API 转发；仅缓存自己的平台会话，不建立凭证数据库 |
| 发布工具 `tools/` | 生成部署配置与不含服务端代码/秘密的 Windows 安装包、市场包 |

详细操作统一见 [CLOUD.md](CLOUD.md)；测试记录见 [VALIDATION.md](VALIDATION.md)。

## 快速入口

发布者安装源码的服务端依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[server,test]'
.venv/bin/python -m pytest -q
```

使用 `tools/publisher_setup.py` 准备真实 HTTPS 域名/身份系统，服务器上隐藏输入第三方应用凭证，
再运行 `tools/build_cloud_client.py` 生成用户发行包。源码包本身不含已部署服务地址。
把生成的客户端 wheel 放进 broker 数据目录的 `client/`，再把市场包上架。用户在市场安装并点击“连接”，
首次连接会自动下载安装客户端（只需本机有 Python 3.11+）。平台账号可用 Google/GitHub 登录并自动开户，
见 CLOUD.md 第 5a 节。不用市场时仍可解压 Windows 安装包运行 `Install-WorkSwarm-Auth.cmd`。

## 清理与升级

v0.6 删除了旧本机 broker、第三方本机 OAuth、静态密钥认证、手工 token 导入、旧 Gmail 迁移、
原生 CLI 卡片补丁和对应启动/验收脚本。删除 `--local` 安装选项以及 `init / mcp / host-*` 等旧命令。
移除对 psutil/python-dotenv 的直接依赖。客户端发行 ZIP 仅包含 8 个客户端/公共模块，不包含服务器模块。

已有云端服务仍可使用显式 `--home` 指定原数据目录；不会重建旧数据库密钥。
v0.5 云端平台会话继续兼容原 key/cache 文件。不会自动删除用户已有目录或尚未使用完的凭证。
升级后需停用旧 WorkSwarm 连接器配置，使用新版安装器生成的云端 MCP 配置。

## 为什么是 Connector（MCP）而不是 Plugin

WorkSwarm 的 plugin 由 skills、进程内 Python tools 和 rails 组成，不能携带 MCP 服务，也不能声明
pip 依赖。本连接器需要独立进程保管平台会话、开本机回调端口，并依赖 httpx/PyJWT/cryptography/mcp，
因此以 stdio MCP connector 交付：凭证与 agent 进程隔离，可在任何 MCP 宿主复用，并沿用 WorkSwarm
连接器的连接/断开与按会话勾选机制。若需要使用指引，可另做一个只含 skill 的 plugin 作为配套。

服务端曾提供第二种传输 `/mcp`（Streamable HTTP + MCP OAuth 资源服务器），当前已移除。WorkSwarm 较新的
开发版（2026-10-30 查看）已有宿主托管的远程 MCP OAuth，但提供方表只包含 Canva，并要求服务端支持动态注册客户端，
本连接器暂时接不上。若 WorkSwarm 开放接入，可恢复该传输，用户将无需安装 Python 和客户端。
平台令牌的 audience 仍为 `<public_origin>/mcp`，保持兼容。
当前没有修改用户已编译的 WorkSwarm 桌面程序，也未自动部署公网服务。
