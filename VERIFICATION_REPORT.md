# 代码验证报告 — AgentCore Identity 全流程 Demo

**区域**：us-east-1  ·  **日期**：2026-07-19  ·  **状态**：✅ 全部通过

本报告汇总真实部署与实测证据，逐条对应客户需求。原始数据见同目录
`matrix_interceptor.json`、`matrix_cedar.json`、`runtime_e2e_result.json`。

---

## 1. 需求对照表

| # | 客户需求 | 实现 | 证据 | 结论 |
|---|---------|------|------|------|
| 1a | 基于 Strands SDK 开发 Agent 并部署到 Runtime | `agent.py` + 容器化部署到 `okx_identity_runtime` | Runtime 状态 READY，E2E 调通 | ✅ |
| 1b | Gateway 创建 Lambda Target，提供 3 个 Tool | `target_lambda.py` + `tool_schema.json`，Target `defi` | `tools/list` 返回 3 工具 | ✅ |
| 2  | 不同用户不同 Tool 权限 | Cognito 3 用户 3 组 + Gateway 授权 | 权限矩阵按组分层 | ✅ |
| 2-正向 | 有权限→成功调用 | 各用户对授权工具 `tools/call` | 返回真实结果 | ✅ |
| 2-反向 | 无权限→被过滤/拒绝 | 各用户对越权工具 `tools/call` | `tools/call` DENIED + `tools/list` 不可见 | ✅ |
| 3-A | Gateway 拦截器鉴权 Demo | `interceptor_lambda.py`（REQUEST+RESPONSE） | `matrix_interceptor.json` | ✅ |
| 3-B | Gateway 非拦截器鉴权 Demo | Cedar Policy Engine（`policies/*.cedar`） | `matrix_cedar.json` | ✅ |
| 4  | 图文并茂原理解释 | `README.md` + 3 张 SVG | 架构/时序/矩阵 | ✅ |
| — | 测试后清理资源 | `cleanup.sh` | 见 §5 | ✅ |

---

## 2. 权限矩阵（两种机制结果一致）

直连 Gateway MCP endpoint，用各用户真实 JWT 逐一 `tools/call`（确定性证据，规避 LLM 非确定性）：

| 用户 / 组 | get_token_price | calc_impermanent_loss | place_order |
|-----------|:---:|:---:|:---:|
| readonly-user / readonly | ALLOW | **DENIED** | **DENIED** |
| analyst-user / analyst | ALLOW | ALLOW | **DENIED** |
| trader-user / trader | ALLOW | ALLOW | ALLOW |

- **拦截器**拒绝签名：`error.code = -32001`，`"AUTHZ DENIED: groups [...] may not call tool 'X'"`
- **Cedar**拒绝签名：`error.code = -32002`，`"Tool Execution Denied: ... policy enforcement [No policy applies to the request (denied by default)]"`
- 两者 `tools/list` 均只返回该用户有权限的工具子集。

## 3. fail-closed（拒绝安全）验证

| 场景 | 结果 |
|------|------|
| 无 `Authorization` 头 | HTTP **401** `Missing Bearer token`（入站授权器，在拦截器之前） |
| 伪造/损坏 token | HTTP **401** `Invalid Bearer token` |
| 拦截器 Lambda 内部异常 | 返回 DENY（`interceptor_lambda.py` 顶层 `try/except` 兜底） |
| Cedar 无策略命中 | 默认拒绝（Cedar default-deny） |

## 4. 端到端（Runtime + Strands Agent）验证

同一 Agent、同一代码，仅因调用者身份不同 → 可见/可用工具不同（`runtime_e2e_result.json`）：

| 用户 | Agent 可见工具 | 实际调用 | 说明 |
|------|---------------|---------|------|
| trader-user | 3 个全部 | get_token_price, place_order | 查价 $64,250 + mock 下单 MOCK_ACCEPTED |
| analyst-user | price + IL | calc_impermanent_loss | IL(r=4) = -20%；下单被拒（工具不可见） |
| readonly-user | 仅 price | get_token_price | ETH $3,120.5；下单被拒 |

## 5. 资源清理

`cleanup.sh` 按依赖顺序删除：Runtime → Gateway targets → Gateway → Policy Engine 内策略+引擎
→ Lambda + 日志组 → Cognito（用户/组/client → pool；本 Demo 未建 hosted-UI domain）→ ECR → IAM 角色。
执行后逐项校验返回 not-found，确保无残留计费。

---

## 6. 结论

三个客户问题均已用**真实部署 + 实测数据**回答：
1. **Runtime User 鉴权** = Cognito JWT 入站授权器（`customJWTAuthorizer`）+ 请求头白名单读取身份。
2. **Runtime→Gateway 鉴权** = 透传用户原始 JWT（同一 Cognito 信任域），保住用户身份。
3. **每个 Tool 鉴权** = Gateway 侧两种机制（拦截器 / Cedar），均实现 per-user 的正反用例，且 fail-closed。
