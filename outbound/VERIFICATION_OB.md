# outbound 3LO 端到端验证结果 (us-east-1, 账号 340636688520)

验证日期: 2026-07-22 | 分支: identity-outbound

## 全链路已实测通过

| 步骤 | 命令 | 结果 |
|------|------|------|
| Runtime B 无 token 调用 | curl (no auth) | HTTP 401 ✅ |
| Runtime B 有效 token tools/list | curl+token | 返回 3 工具 ✅ |
| Runtime B tools/call get_token_price | curl+token | $64250 ✅ |
| Gateway tools/list | e2e list | 列出 3 工具+search ✅ |
| Gateway 首次 tools/call (未授权) | e2e first | **-32042 + 登录URL** ✅ |
| Runtime A 首次调用 (产品形态) | invoke RT_A | status=AUTHORIZATION_REQUIRED + authorization_url ✅ |
| 浏览器登录同意 → 回调 | consent driver | callback "✅授权完成" (session 绑定) ✅ |
| 授权后重试 tools/call | e2e retry | get_token_price=$64250, 无 -32042 ✅ |
| 二次调用免授权 | e2e retry | Vault 命中, 直接成功 ✅ |
| Runtime A 授权后 place_order | invoke RT_A | status=OK, MOCK_ACCEPTED ✅ |

## 关键实测结论 (踩坑记录)
1. Gateway 3LO 需 MCP 协议版本头 `MCP-Protocol-Version: 2025-11-25`, 否则 -32022。
2. MCP runtime 监听 8000/mcp (非 8080); Accept 头须含 application/json, text/event-stream。
3. mcpServer target 的 mcpToolSchema.inlinePayload 是 JSON **字符串** {"tools":[...]}。
4. 回调 EC2 的 IAM 角色需 secretsmanager:GetSecretValue (bedrock-agentcore-identity*)
   + bedrock-agentcore:CompleteResourceTokenAuth, 否则回调 500。
5. session 绑定: 发起 3LO 前需把发起用户 token POST 到回调服务器 /userIdentifier/token。
6. 未遇到 issue #809 的 -32603; 3LO 打自建 Cognito-backed runtime target 实测可行。
