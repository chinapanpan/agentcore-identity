"""
AgentCore Gateway 拦截器 Lambda (Demo A — 基于拦截器的 Tool 级鉴权)。
【cognito-test 分支场景】鉴权依据 = access token 里由 Pre-Token-Gen V2 注入的自定义 claim `role_id`。
同一函数同时处理 REQUEST 与 RESPONSE 两个拦截点。

★区分拦截点: 事件里 mcp.gatewayResponse 键恒存在, 但 REQUEST 时其值为 null,
  RESPONSE 时为对象 → 用"值是否非空"来判定, 不能只看键是否存在。

鉴权依据: 调用方 access token 的 `role_id` claim (由 pretoken_lambda.py 注入)。
  - REQUEST: 在 gateway 调 target 之前跑。对 tools/call, 若该 role_id 无权限调用目标 tool,
    返回 transformedGatewayResponse (JSON-RPC error) → gateway 直接短路响应, 不调 target (=DENY)。
    否则返回 transformedGatewayRequest 放行。
  - RESPONSE: target 返回后跑。对 tools/list 结果按 role_id 过滤无权限的 tool (不可见);
    同时过滤 result.structuredContent.tools (语义搜索/search 路径), 否则无权限工具会从该路径泄漏。

fail-closed: 任何异常 / 无 token / 无 role_id / 无法解析 → 一律 DENY。
passRequestHeaders 必须在 gateway 配为 true。
本地单测见 interceptor_unit_test.py (合成 payload, 免部署)。
"""
import base64
import json

# role_id → 允许的工具集合。role_id 由 Cognito Pre-Token-Gen V2 从 custom:role_id 注入 access token。
#   1001 = viewer  → 仅查价
#   1002 = analyst → 查价 + 算无常损失
#   1003 = trader  → 全部 (含下单)
ROLE_TOOLS = {
    "1001": {"get_token_price"},
    "1002": {"get_token_price", "calc_impermanent_loss"},
    "1003": {"get_token_price", "calc_impermanent_loss", "place_order"},
}
DELIM = "___"


def _decode_role_id(auth_header):
    """从 Bearer JWT 解出 role_id claim (不校验签名——gateway 入站已校验)。"""
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    parts = auth_header[len("Bearer "):].split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload).decode())
    rid = claims.get("role_id")
    return str(rid) if rid is not None else None


def _allowed_tools_for(role_id):
    return ROLE_TOOLS.get(role_id, set())


def _strip_tool(name):
    # 工具名格式 <target>___<tool>; 取第一个 ___ 之后作为 tool 名 (兼容 tool 名本身含 ___)
    return name.split(DELIM, 1)[1] if DELIM in name else name


def _get_header(headers, name):
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v
    return None


def _deny(msg, req_id=1):
    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayResponse": {
                "statusCode": 200,
                "body": {"jsonrpc": "2.0", "id": req_id,
                         "error": {"code": -32001, "message": f"AUTHZ DENIED: {msg}"}}}}}


def _pass_request(body):
    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayRequest": {"body": body}}}


def _pass_response(status, body):
    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayResponse": {"statusCode": status, "body": body}}}


def _handle_request(mcp):
    gw = mcp.get("gatewayRequest") or {}
    headers = gw.get("headers") or {}
    body = gw.get("body") or {}
    req_id = body.get("id", 1)
    method = body.get("method", "")

    role_id = _decode_role_id(_get_header(headers, "Authorization"))
    if role_id is None:
        return _deny("no valid bearer token / no role_id claim", req_id)  # fail-closed

    if method != "tools/call":            # tools/list 等放行, 由 RESPONSE 过滤
        return _pass_request(body)

    tool = _strip_tool(body.get("params", {}).get("name", ""))
    if tool in _allowed_tools_for(role_id):
        return _pass_request(body)
    return _deny(f"role_id {role_id} may not call tool '{tool}'", req_id)


def _handle_response(mcp):
    gw_req = mcp.get("gatewayRequest") or {}
    gw_resp = mcp.get("gatewayResponse") or {}
    headers = gw_req.get("headers") or {}
    req_body = gw_req.get("body") or {}
    status = gw_resp.get("statusCode", 200)
    resp_body = gw_resp.get("body") or {}

    if req_body.get("method", "") != "tools/list":
        return _pass_response(status, resp_body)   # 非 list 原样返回

    role_id = _decode_role_id(_get_header(headers, "Authorization"))
    allowed = _allowed_tools_for(role_id)
    result = resp_body.get("result") or {}

    def _filter(tools):
        return [t for t in (tools or []) if _strip_tool(t.get("name", "")) in allowed]

    # 过滤标准 tools/list 结果
    if "tools" in result:
        result["tools"] = _filter(result.get("tools"))
    # ★同时过滤语义搜索 / structuredContent 路径下的工具列表, 否则无权限工具会从这里泄漏
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and "tools" in sc:
        sc["tools"] = _filter(sc.get("tools"))
        result["structuredContent"] = sc
    resp_body["result"] = result
    return _pass_response(status, resp_body)


def lambda_handler(event, context):
    try:
        mcp = event.get("mcp", {})
        # gatewayResponse 值非空 → RESPONSE 拦截点; 否则 REQUEST
        if mcp.get("gatewayResponse"):
            return _handle_response(mcp)
        return _handle_request(mcp)
    except Exception as e:  # fail-closed
        return _deny(f"interceptor error: {e}")
