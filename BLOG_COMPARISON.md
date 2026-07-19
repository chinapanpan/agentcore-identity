# 与 AWS 官方博客的对比分析

**对比对象**：AWS 官方博客
[Apply fine-grained access control with Amazon Bedrock AgentCore Gateway interceptors](https://aws.amazon.com/blogs/machine-learning/apply-fine-grained-access-control-with-bedrock-agentcore-gateway-interceptors/)

本文档把本 Demo 的设计、代码与测试结论逐条对照该博客，标出**一致点**、**设计差异**，以及我们据此做出的**一处安全修复**。

---

## 1. 结论速览

| 维度 | AWS 博客 | 本 Demo | 判定 |
|------|---------|---------|------|
| 拦截点分工 | REQUEST 拦截器做调用鉴权；RESPONSE 拦截器过滤 `tools/list` | 同左（单 Lambda 处理两个拦截点） | ✅ 一致 |
| 工具命名 | `target___action`（三下划线） | `defi___<tool>`（三下划线） | ✅ 一致 |
| 拦截器是否验签 | 否（假定 Gateway 入站已验签） | 否（同样假定入站已验签） | ✅ 一致 |
| DENY 机制 | 返回结构化 MCP error 短路，不调 target（未给 code） | 返回 `transformedGatewayResponse` + JSON-RPC `error.code=-32001` 短路 | ✅ 一致（本 Demo 更具体） |
| **授权信号** | **OAuth `scope`**（`target` / `target:tool`） | **`cognito:groups`**（RBAC 角色） | ⚠️ 设计差异（见 §2） |
| **`structuredContent.tools` 过滤** | **明确过滤**（语义搜索路径） | 原先**只过滤 `result.tools`** → **已修复** | 🔧 已修复（见 §3） |
| 授权信号注入 target | 把 `authorization` 注入 tool 参数供 target 侧纵深防御 | 未做（另用 Cedar 作第二机制） | ➕ 可选增强（见 §4） |
| 第二种非拦截器机制 | 未涉及 | **Cedar Policy Engine** | ➕ 本 Demo 更全 |

---

## 2. 设计差异：授权信号 —— OAuth scope vs. cognito:groups

**博客的做法（scope-based）**：用户拿到的 OAuth token 的 `scope` claim 直接携带工具粒度权限，命名约定：
- 整个 target：`mcp-target-123`
- 单个工具：`mcp-target-123:getOrder`

核心判定：
```python
def check_tool_authorization(scopes, tool, target):
    return target in scopes or f"{target}:{tool}" in scopes
```

**本 Demo 的做法（groups-based / RBAC）**：用户属于 Cognito 用户组（`readonly`/`analyst`/`trader`），
拦截器按 `cognito:groups` 映射到工具集合：
```python
GROUP_TOOLS = {
    "readonly": {"get_token_price"},
    "analyst":  {"get_token_price", "calc_impermanent_loss"},
    "trader":   {"get_token_price", "calc_impermanent_loss", "place_order"},
}
```

**两者都对，适用场景不同**：

| | OAuth scope（博客） | cognito:groups（本 Demo） |
|---|---|---|
| 粒度 | 原生 per-tool，token 里直接带 | 先分角色，角色再映射工具 |
| 与 AgentCore 入站鉴权的贴合度 | 高——入站 `allowedScopes` 可直接复用同一 scope | 用 `allowedClients` 校验，组信息在 `cognito:groups` claim |
| 变更工具权限 | 改 Resource Server scope + App Client | 改组成员 或 改 `GROUP_TOOLS`/Cedar 策略 |
| 贴合"C 端用户角色"的表达 | 需要为每个用户/角色配 scope | **天然**（用户→组→权限），本 Demo 因此选它 |
| 客户端拿 scope 的方式 | hosted-UI OAuth 流 / client_credentials | 用户登录即带 `cognito:groups` |

> 我们选 `cognito:groups` 是为了**诚实表达"不同 C 端用户"**（见 README §2、§7）：
> `scope` 若走 M2M `client_credentials` 会退化成"按客户端"而非"按用户"。
> 若你的场景已用 OAuth scope 建模工具权限，博客的 scope 方案可直接套用——把本 Demo 拦截器里的
> `_decode_jwt_groups` 换成读 `scope` claim、`GROUP_TOOLS` 换成 `check_tool_authorization` 即可，其余结构不变。

---

## 3. 据博客做出的安全修复：过滤 `structuredContent.tools`

**博客明确指出**：当 target 对**语义搜索 / `tools/list`** 返回工具列表时，工具可能出现在
`result.tools`，**也可能**出现在 `result.structuredContent.tools`：

```python
tools = gateway_response['body']['result'].get('tools', [])
if not tools:
    tools = gateway_response['body']['result'].get('structuredContent', {}).get('tools', [])
```

**本 Demo 原实现只过滤了 `result.tools`**——这是一个真实的**可见性绕过缺口**：
一旦启用 Gateway 语义搜索，无权限工具会从 `structuredContent` 路径泄漏给用户。

**修复**（`interceptor_lambda.py` RESPONSE 分支）：现在**两条路径都过滤**：
```python
if "tools" in result:
    result["tools"] = _filter(result.get("tools"))
sc = result.get("structuredContent")
if isinstance(sc, dict) and "tools" in sc:
    sc["tools"] = _filter(sc.get("tools"))          # ★新增: 堵住语义搜索路径
```

**验证**：`interceptor_unit_test.py`（合成 payload，无需部署）覆盖了两条路径 + fail-closed，**22/22 通过**：
```
== ★RESPONSE: structuredContent.tools 过滤 (博客对齐, 修复点) ==
  ✓ readonly structuredContent sees ['get_token_price']
  ✓ analyst  structuredContent sees ['calc_impermanent_loss', 'get_token_price']
  ✓ trader   structuredContent sees [全部 3 个]
```

> 顺带加固：`_strip_tool` 从 `name.index(DELIM)` 改为 `name.split(DELIM, 1)[1]`，
> 使工具名本身含 `___` 时也能正确取出 tool 部分。

---

## 4. 可选增强：把身份注入 target 供纵深防御

博客的 REQUEST 拦截器会把 `authorization` 注入 `params.arguments.authorization`，让 **target Lambda 自己**
再做一层校验（纵深防御）。本 Demo 未在拦截器里这么做，但**等价的"target 侧自校验"思想已由 Demo B 的
Cedar / in-Lambda 说明覆盖**（见 README §5.2、§9）。若要完全复刻博客，可在
`interceptor_lambda._handle_request` 的放行分支里给 `body["params"]["arguments"]["authorization"]` 赋值。
本 Demo 未纳入，以保持"拦截器只做鉴权、不改业务入参"的清晰边界。

---

## 5. 一致性印证（博客反向验证了本 Demo 的正确性）

- **`tools/list` 过滤放在 RESPONSE 拦截器**：博客同样如此——因为工具列表是 target 返回后才有的，只能在 RESPONSE 阶段裁剪。
- **三下划线 `___` 命名**：与博客完全一致（注：AWS 部分 CDK 文档误写成双下划线，devguide 与本博客均为三下划线）。
- **拦截器不重复验签**：博客也假定 Gateway 入站边界已完成 JWT 校验，拦截器只解码取 claim。
- **REQUEST 短路 = 不调 target**：博客称"return a structured MCP error … preventing the backend tool handler from executing"，与本 Demo `transformedGatewayResponse` 短路语义一致。

---

## 6. 小结

- 本 Demo 与博客在**拦截器架构、命名、拦截点分工、短路语义**上完全一致，博客反向印证了设计正确性。
- **授权信号**是有意的设计差异（groups vs scope），两者皆可，本 Demo 因"按用户"诉求选 groups，并给出切换到 scope 的路径。
- 对比博客发现并**修复了一个真实缺口**（`structuredContent.tools` 未过滤），已用本地单测验证。
- 本 Demo 额外提供了博客未覆盖的 **Cedar Policy Engine** 非拦截器方案，覆盖面更广。
