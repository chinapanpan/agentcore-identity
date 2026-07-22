# Outbound OAuth 3LO 实战教学：AgentCore Gateway 如何用"用户授权码流"访问受保护 MCP

> 本文是 `identity-outbound` 分支的**教学主文档**，用一个真实部署、真实实测的端到端案例，带你搞懂：
> 1. **Inbound（入站）vs Outbound（出站）授权**到底差在哪；
> 2. **3LO（三腿授权 / authorization_code）** 与 2LO（client_credentials）的区别；
> 3. AgentCore 的 **OAuth2 Credential Provider** 是什么、为什么它 ≠ Cognito；
> 4. Gateway 如何用 **`-32042` URL elicitation** 把"请登录"这段 URL 抛给调用方；
> 5. **Token Vault + session 绑定**如何让"授权一次、后续免打扰"且防 CSRF；
> 6. 为什么下游要用**另一个 AgentCore Runtime 承载 MCP Server**（而非 Lambda）。
>
> 全部资源部署在 **us-east-1**，基于 **Amazon Cognito(Hosted UI) + AgentCore Identity + AgentCore Gateway + 两个 AgentCore Runtime**。配套可跑命令见 [OUTBOUND_RUNBOOK.md](OUTBOUND_RUNBOOK.md)。

---

## 目录

- [1. 场景与拓扑](#1-场景与拓扑)
- [2. Inbound vs Outbound Auth](#2-inbound-vs-outbound-auth)
- [3. 2LO vs 3LO](#3-2lo-vs-3lo)
- [4. 【重点】OAuth2 Credential Provider ≠ Cognito](#4-重点oauth2-credential-provider--cognito)
- [5. 【重点】-32042 URL Elicitation：登录 URL 怎么抛出来的](#5-重点-32042-url-elicitation登录-url-怎么抛出来的)
- [6. Token Vault 与 Session 绑定](#6-token-vault-与-session-绑定)
- [7. 为什么下游用 Runtime 承载 MCP Server](#7-为什么下游用-runtime-承载-mcp-server)
- [8. 端到端实测结果](#8-端到端实测结果)
- [9. 复现与踩坑](#9-复现与踩坑)

---

## 1. 场景与拓扑

目标效果：**Agent 去调某个 MCP 工具时，若用户尚未授权，会出现一段 URL 让用户点开登录；授权后 Agent 才能正常调用。**

```
  用户(浏览器) ──①调用──> Runtime A (Agent)
                              │ ②经 MCPClient/裸MCP 调 Gateway tools/call
                              ▼
                        MCP Gateway (outbound OAuth 3LO)
                              │ ③Vault 无 token -> 返回 -32042 + 登录URL
                              ▼（授权后）用 Vault token(Bearer) 调下游
                        Runtime B (受 OAuth 保护的 MCP Server)
  用户 ──④点登录URL──> Cognito Hosted UI 登录同意
       ──⑤重定向──> callback.chrisai.blog(独立EC2) -> CompleteResourceTokenAuth -> token入Vault
```

一句话：**agent → gateway → agent**，中间那跳的"出站授权"需要用户亲自点 URL 授权一次。

---

## 2. Inbound vs Outbound Auth

AgentCore 的认证分两个方向，务必分清（这也是与旧 `cognito-test` 分支最大的不同——那条链路只讲 inbound）：

| | Inbound（入站） | **Outbound（出站）** |
|---|---|---|
| 问题 | "谁能调用我（Runtime/Gateway）" | "我（Gateway）用什么身份去调下游资源" |
| 用谁的身份 | 调用方（用户）的 token | Gateway 代表用户拿到的**下游授权 token** |
| 机制 | `customJWTAuthorizer`（验签/iss/aud） | **凭证提供者 + Token Vault**（API Key / OAuth 2LO / **3LO**） |
| 本例体现 | Runtime A/B、Gateway 都配了入站 Cognito JWT 校验 | Gateway target 配 `OAUTH / AUTHORIZATION_CODE`，代表用户拿下游 token |

**本 demo 的重点是 Outbound**：Gateway 需要一个"用户授权过的" token 才能访问受保护的下游 MCP（Runtime B）。

---

## 3. 2LO vs 3LO

OAuth 出站有两种"腿数"：

| | 2LO（client_credentials） | **3LO（authorization_code）** |
|---|---|---|
| 参与方 | 2 方：Gateway ↔ 授权服务器 | **3 方：用户 + Gateway + 授权服务器** |
| 需要用户点同意吗 | 否（M2M，机器身份） | **是（用户在浏览器登录、点授权）** |
| 典型场景 | 服务对服务 | 代表用户访问其个人资源（LinkedIn/Google/…） |
| 是否弹 URL | 不弹 | **弹登录 URL（本 demo 的主角）** |

本 demo 要的正是"弹 URL 让用户登录"，所以 target 用 **`grantType=AUTHORIZATION_CODE`**。

---

## 4. 【重点】OAuth2 Credential Provider ≠ Cognito

这是最容易混的点。两个"provider"名字撞车，实为不同层面：

| | **Cognito**（授权服务器 / IdP） | **AgentCore OAuth2 Credential Provider** |
|---|---|---|
| 角色 | OAuth **Authorization Server**（发 token 的一方） | AgentCore 作为 OAuth **Client** 的一张"凭证登记卡" |
| 有登录页吗 | 有（Hosted UI，用户在此登录同意） | 没有——它不发 token、不存用户 |
| 存什么 | 用户、密码、resource server、scope | client_id / client_secret / discoveryUrl / scope |
| 产出 | authorization code、access/refresh token | **一个唯一的 callback URL**（要回填到 Cognito） |

类比：**Cognito 是发身份证的机关；Credential Provider 是你在这个机关登记的"办事账号"**。两个都要有。

创建（本例 `CustomOauth2` + Cognito discoveryUrl）：

```bash
aws bedrock-agentcore-control create-oauth2-credential-provider \
  --name okx-ob-cognito-provider --credential-provider-vendor CustomOauth2 \
  --oauth2-provider-config-input '{"customOauth2ProviderConfig":{
     "oauthDiscovery":{"discoveryUrl":"<COGNITO_DISCOVERY_URL>"},
     "clientId":"<APP_CLIENT_ID>","clientSecret":"<SECRET>"}}'
# 返回里的 callbackUrl 必须回填到 Cognito App Client 的 CallbackURLs
```

> 它独有的 3 个作用：① 安全存 client_secret（进 Secrets Manager）；② 产出 3LO 重定向锚点 callback URL；③ 管 Token Vault（按 provider+用户存 token）。

---

## 5. 【重点】-32042 URL Elicitation：登录 URL 怎么抛出来的

当 `tools/call` 命中一个 `AUTHORIZATION_CODE` 的 target，且 Vault 里没有该用户的下游 token 时，Gateway 会返回一个 **MCP URL 模式 elicitation 错误 `-32042`**（需 Gateway 建在 MCP 协议版本 **`2025-11-25`** 上）：

```json
{"jsonrpc":"2.0","id":24,"error":{
  "code":-32042,"message":"This request requires more information.",
  "data":{"elicitations":[{"mode":"url","elicitationId":"…",
    "url":"https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/authorize?request_uri=urn:ietf:…",
    "message":"Please login to this URL for authorization."}]}}}
```

调用方（Runtime A / 测试脚本）拿到这个 `url` 就展示给用户。用户点开后：
`AgentCore /authorize` → 重定向 Cognito Hosted UI 登录同意 → 回 AgentCore callback（换 code 拿 token）→ 重定向到 target 的 `defaultReturnUrl`（我们的回调服务器）。

**关键工程点**：
- 请求要带头 `MCP-Protocol-Version: 2025-11-25` 和 `Accept: application/json, text/event-stream`，否则报 `-32022`。
- 可用 `params._meta` 覆盖：`returnUrl`（临时改回调）、`forceAuthentication:true`（清 Vault，强制每次弹 URL，**演示利器**）。
- Strands 旧版 `MCPClient` 会把 `-32042` 吞成一句话丢掉 URL（sdk-python #1742，PR #1745 修复）。本 demo 的 Runtime A 用裸 MCP 发 `tools/call` 并自己解析 `elicitations[].url`，保证稳定拿到登录 URL。

---

## 6. Token Vault 与 Session 绑定

- 用户授权后，AgentCore Identity 把 access/refresh token 存进 **Token Vault**，按 **provider + 用户身份** 归档。
- 之后同一用户再调，Gateway 直接取 Vault 里的 token（过期用 refresh token 续），**不再弹 URL**。
- **Session 绑定（防 CSRF）**：3LO 授权 URL 有效期约 10 分钟；必须证明"发起授权的用户 = 完成同意的用户"。做法：
  1. 发起 3LO 前，把发起用户的 access token POST 到回调服务器（`/userIdentifier/token`）暂存；
  2. 浏览器带 `session_id` 落到回调服务器时，用它作为 `user_identifier` 调 **`CompleteResourceTokenAuth(session_uri, user_identifier)`** 完成绑定。
- 回调服务器必须是**公网可达的 HTTPS**，且其 URL 要注册进 Gateway workload identity 的 `allowedResourceOauth2ReturnUrls`。本例用独立 EC2 + `callback.chrisai.blog` + Let's Encrypt（Caddy 自动签发）。

---

## 7. 为什么下游用 Runtime 承载 MCP Server

**3LO 只支持 MCP-server 与 OpenAPI target，不支持 Lambda target**。所以受保护的下游不能是 Lambda，得是一个真正的 MCP server。本例把它部署成**第二个 AgentCore Runtime**（`serverProtocol=MCP`）：

- 用 FastMCP 暴露 streamable-http（容器内 **8000/mcp**，`stateless_http=True`）；
- Runtime 的入站 `customJWTAuthorizer` 负责验 token——Gateway 出站拿到的 token 作为 `Authorization: Bearer` 打进来，正好过它的入站校验；
- 于是形成 **agent → gateway → agent** 的联邦：一个 Runtime 通过 Gateway 调另一个 Runtime 上的 MCP 工具。

> 这是 AWS 官方 sample `02-AgentCore-gateway/05-mcp-server-as-a-target` 的模式：Gateway `mcpServer` target 的 `endpoint` 指向另一个 Runtime 的 `/invocations` URL。

---

## 8. 端到端实测结果

| 步骤 | 现象 | 结果 |
|------|------|:---:|
| Runtime B 无 token | HTTP 401 | ✅ |
| Runtime B 有 token tools/call | get_token_price=$64250 | ✅ |
| Gateway 首次 tools/call（未授权） | **-32042 + 登录 URL** | ✅ |
| Runtime A 首次调用（产品形态） | `AUTHORIZATION_REQUIRED` + authorization_url | ✅ |
| 浏览器登录同意 → 回调 | 「✅ 授权完成」+ session 绑定 | ✅ |
| 授权后重试 tools/call | 成功返回业务结果，无 -32042 | ✅ |
| 二次调用 | Vault 命中，免授权 | ✅ |
| Runtime A 授权后 place_order | `status:OK` + MOCK_ACCEPTED | ✅ |

**同一段 Agent 代码，未授权时抛登录 URL、授权后直接可用**——出站授权闭环完整。

---

## 9. 复现与踩坑

```bash
export AWS_REGION=us-east-1
./deploy_ob.sh              # 建 Cognito + credential provider + 2 Runtime + Gateway(3LO)
./setup_callback_ec2.sh     # 起回调 EC2 + callback.chrisai.blog + 证书
# 验证见 OUTBOUND_RUNBOOK.md（Step 2 弹 URL / Step 4 人工登录 / Step 5 成功）
./cleanup_ob.sh             # 演示后清理（含独立 EC2, 务必删）
```

**实测踩坑（本例结论）**：
1. Gateway 3LO 需 MCP 协议头 `MCP-Protocol-Version: 2025-11-25`，否则 `-32022`。
2. MCP runtime 监听 **8000/mcp**（不是 8080）；请求 `Accept` 须含 `text/event-stream`。
3. `mcpServer` target 的 `mcpToolSchema.inlinePayload` 是 JSON **字符串** `{"tools":[…]}`（与 Lambda target 的数组不同）。
4. 回调 EC2 的 IAM 角色需 `bedrock-agentcore:CompleteResourceTokenAuth` + `secretsmanager:GetSecretValue`（`bedrock-agentcore-identity*`），否则回调 500。
5. `forceAuthentication:true` 用于演示——保证每次都弹 URL；生产用 false 走 Vault 缓存。
6. 未遇到 issue #809 的 `-32603`；3LO 打自建 Cognito-backed runtime target 实测可行。

---

*本文全部结论来自 us-east-1 的真实部署与实测（2026-07-22）；关键证据见 `VERIFICATION_OB.md`。*
