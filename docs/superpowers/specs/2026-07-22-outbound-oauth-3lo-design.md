# AgentCore Gateway Outbound OAuth 3LO Demo — 设计方案

> 分支：`identity-outbound` ｜ 区域：`us-east-1` ｜ 账号：`340636688520`
> 日期：2026-07-22 ｜ 资源前缀：`okx-ob-*`（与旧 inbound demo 的 `okx-ct-*` 隔离）

## 1. 目标（一句话主线）

当 Runtime A 上的 Strands Agent 通过 MCP Gateway 去调用下游 MCP 工具时，若用户尚未授权，Gateway 返回一个 **`-32042` URL-elicitation 错误** —— 弹出一段登录 URL；用户点开、在 Cognito Hosted UI 登录并同意后，token 存入 AgentCore Identity Token Vault，Agent 重试调用即成功；二次调用直接命中 Vault，免再授权。

这是 **outbound（出站）OAuth 三腿授权（3LO / authorization_code / USER_FEDERATION）** 演示，与旧 demo 的 inbound（入站 JWT + role_id 工具过滤）是两条正交的链路。本 demo **纯聚焦 outbound 3LO**，不做 role_id RBAC。

## 2. 关键技术结论（研究实证）

- **AgentCore Gateway 原生支持交互式 3LO**：target 配 `credentialProviderType=OAUTH` + `grantType=AUTHORIZATION_CODE` 时，`tools/call` 命中且 Vault 无缓存 token → Gateway 返回 MCP `-32042`（URL elicitation），payload 里含登录 URL。这不是只能靠 in-agent `@requires_access_token` 才能做到。
- **3LO 只支持 MCP-server 与 OpenAPI target，不支持 Lambda target** —— 故下游必须是真实的、受 OAuth 保护的 MCP server；用第二个 AgentCore Runtime（serverProtocol=MCP）承载它，形成 **agent → gateway → agent** 拓扑。
- **两个"provider"不是一回事**：
  - **Cognito** = OAuth 授权服务器（发 token 的一方，有 Hosted UI 登录页）。
  - **AgentCore OAuth2 Credential Provider** = AgentCore 作为 OAuth *client* 的一张凭证登记卡（存 client_id/secret + discoveryUrl，产出唯一 callback URL，管 Token Vault）。两者都必须建。
- `create-oauth2-credential-provider` 支持 `CustomOauth2`（用 Cognito discoveryUrl）及专用 `CognitoOauth2` vendor；返回 `callbackUrl` —— 需回填到 Cognito App Client 的 CallbackURLs。
- 3LO 完成后需一个**公网 HTTPS 回调服务**接住 Cognito 重定向并调 `CompleteResourceTokenAuth` 完成 session 绑定（防 CSRF）。

## 3. 架构

```
                              ┌─────────────────────────────────────────────┐
  用户 (浏览器)                │            us-east-1 (账号 340636688520)       │
     │  ①调用 Agent A          │                                               │
     ▼                         │   ┌──────────────┐   ②tools/call             │
  Runtime A ─────────────────────▶│ MCP Gateway   │──────────┐               │
  (Strands Agent,              │   │ (outbound     │          │ ③未授权→-32042 │
   MCPClient)                  │   │  OAuth 3LO)   │◀─────────┘  含登录URL      │
     ▲  ⑦重试成功               │   └──────┬───────┘                            │
     │                         │          │ ⑥拿到 Vault 里的 user token         │
     │                         │          ▼ 调下游 MCP (Bearer)                 │
     │                         │   ┌──────────────┐                            │
     │                         │   │ Runtime B     │ = 受 OAuth 保护的 MCP Server │
     │                         │   │ (serverProto  │   inbound JWT authorizer   │
     │                         │   │  =MCP)        │   验签+aud, 无效则拒          │
     │                         │   └──────────────┘                            │
     │                         └─────────────────────────────────────────────┘
     │  ④点开登录URL→Cognito Hosted UI 登录同意
     │  ⑤浏览器重定向到 callback.chrisai.blog (独立 EC2, 公网HTTPS)
     ▼                              → CompleteResourceTokenAuth → token 入 Vault
  Cognito Hosted UI (授权服务器)
```

## 4. 组件清单（全部 us-east-1，前缀 `okx-ob-*`）

| # | 组件 | 形态 | 职责 |
|---|------|------|------|
| 1 | **Cognito User Pool（授权服务器）** | 新建，Hosted UI 域名 + Resource Server | outbound OAuth 的 3LO 授权服务器，用户在此登录同意 |
| 2 | **App Client（OAuth code flow）** | `generate-secret`，`authorization_code` grant | AgentCore 作为 OAuth client 用它换 token；CallbackURLs=AgentCore callback |
| 3 | **OAuth2 Credential Provider** | AgentCore Identity，`CustomOauth2`+Cognito discoveryUrl | 存 client 凭证，产出 callback URL，管 Token Vault |
| 4 | **Runtime B = 受保护 MCP Server** | AgentCore Runtime（容器，serverProtocol=MCP） | 暴露 MCP 工具；入站校验 token（验签+aud），无效→拒 |
| 5 | **MCP Gateway** | target=mcpServer，`OAUTH`/`AUTHORIZATION_CODE` | 指向 Runtime B；无缓存 token 时返回 `-32042` 弹登录 URL |
| 6 | **Runtime A = Strands Agent** | AgentCore Runtime（容器，HTTP） | 用 MCPClient 调 Gateway；把 `-32042` 里的 URL 透出给用户 |
| 7 | **回调 EC2** | 独立 EC2 + `callback.chrisai.blog` + Let's Encrypt | 接住 Cognito 重定向 → `CompleteResourceTokenAuth` 完成 session 绑定 |

## 5. 部署顺序（有严格依赖，脚本按序执行）

1. 建 Cognito Pool + Hosted UI 域名 + Resource Server(scope) + App Client(带 secret，code grant)
2. 建 AgentCore OAuth2 Credential Provider → **拿到 callbackUrl**
3. 把 callbackUrl 回填进 Cognito App Client 的 CallbackURLs
4. 构建镜像并部署 Runtime B（serverProtocol=MCP），配 inbound authorizer = 这个 Cognito 池
5. 建 Gateway（inbound authorizer）+ mcpServer target（endpoint=Runtime B MCP endpoint，挂 OAuth provider，AUTHORIZATION_CODE）
6. 部署 Runtime A（Agent），requestHeaderAllowlist + MCPClient 指向 Gateway
7. 起回调 EC2（DNS + Let's Encrypt 证书 + callback 服务），回调 URL 注册进 AgentCore allowed return URLs

## 6. 数据流（正反用例）

- **反向（未授权）**：用户调 Agent A → Gateway `tools/call` → 无 Vault token → 返回 `-32042` + 登录 URL → Agent A 把 URL 透给用户。**"弹出登录 URL"的核心时刻。**
- **正向（授权后）**：用户点 URL → Cognito 登录同意 → 重定向到 `callback.chrisai.blog` → `CompleteResourceTokenAuth` → token 入 Vault → 用户重试 → Gateway 用 Vault token 调 Runtime B → 成功。
- **二次调用**：Vault 有缓存/refresh token → 不再弹 URL，直接成功。

## 7. 交付物结构（`identity/outbound/`）

```
identity/outbound/
├── deploy_ob.sh              # 一键部署（按 §5 七步顺序）
├── cleanup_ob.sh            # 逆序清理
├── mcp_server.py             # Runtime B: 受保护 MCP server (streamable-http)
├── agent.py                  # Runtime A: Strands Agent + MCPClient, 透出 -32042 URL
├── callback_server.py        # 回调服务 (EC2)
├── setup_callback_ec2.sh     # 起 EC2 + DNS + 证书 + 部署回调服务
├── e2e_3lo_test.py           # 端到端：首次弹URL / 授权后成功 / 二次免授权
├── Dockerfile.server         # Runtime B 镜像
├── Dockerfile.agent          # Runtime A 镜像
├── requirements*.txt
├── OUTBOUND_GUIDE.md         # 教学主文档（对标 COGNITO_GUIDE.md）
└── OUTBOUND_RUNBOOK.md       # 可复制的人工 live demo 手册（对标 DEMO_RUNBOOK.md）
```

## 8. 验证边界（诚实声明）

- `-32042` 弹 URL 后的"用户点击登录同意"是**人工交互**，脚本无法全自动跑通（需真人在浏览器点）。
- E2E 脚本能自动验证：① 首次 `tools/call` 确实返回 `-32042` 且含合法登录 URL；②（人工授权后）重试成功；③ 二次免授权。
- `-32042` 的字段大小写/结构以**真实 region 的实测响应为准**校正（doc 中 `oauthCredentialProvider` 大小写不一致）。
- RUNBOOK 提供逐条可复制的人工 demo 命令 + 讲解点，对标现有 DEMO_RUNBOOK.md 风格。

## 9. 清理

`cleanup_ob.sh` 逆序删除：Runtime A → Gateway target/Gateway → Runtime B → OAuth2 Credential Provider → Cognito(域名/Client/Pool) → 回调 EC2 + DNS 记录 → ECR → IAM 角色。回调 EC2 独立计费，务必删。
