"""
AgentCore Gateway 拦截器 Lambda (Demo A — 基于拦截器的 Tool 级鉴权)。
同一函数同时处理 REQUEST 与 RESPONSE 两个拦截点。

★区分拦截点: 事件里 mcp.gatewayResponse 键恒存在, 但 REQUEST 时其值为 null,
  RESPONSE 时为对象 → 用"值是否非空"来判定, 不能只看键是否存在。

鉴权依据: 调用方 JWT 的 `cognito:groups` claim。
  - REQUEST: 在 gateway 调 target 之前跑。对 tools/call, 若该 group 无权限调用目标 tool,
    返回 transformedGatewayResponse (JSON-RPC error) → gateway 直接短路响应, 不调 target (=DENY)。
    否则返回 transformedGatewayRequest 放行。
  - RESPONSE: target 返回后跑。对 tools/list 结果按 group 过滤无权限的 tool (不可见)。

fail-closed: 任何异常 / 无 token / 无法解析 → 一律 DENY。
passRequestHeaders 必须在 gateway 配为 true。
"""
import base64
import json

GROUP_TOOLS = {
    "readonly": {"get_token_price"},
    "analyst": {"get_token_price", "calc_impermanent_loss"},
    "trader": {"get_token_price", "calc_impermanent_loss", "place_order"},
}
DELIM = "___"


def _decode_jwt_groups(auth_header):
    """从 Bearer JWT 解出 cognito:groups (不校验签名——gateway 入站已校验)。"""
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    parts = auth_header[len("Bearer "):].split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload).decode())
    return claims.get("cognito:groups", []) or []


def _allowed_tools_for(groups):
    allowed = set()
    for g in groups:
        allowed |= GROUP_TOOLS.get(g, set())
    return allowed


def _strip_tool(name):
    return name[name.index(DELIM) + len(DELIM):] if DELIM in name else name


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

    groups = _decode_jwt_groups(_get_header(headers, "Authorization"))
    if groups is None:
        return _deny("no valid bearer token", req_id)  # fail-closed

    if method != "tools/call":            # tools/list 等放行, 由 RESPONSE 过滤
        return _pass_request(body)

    tool = _strip_tool(body.get("params", {}).get("name", ""))
    if tool in _allowed_tools_for(groups):
        return _pass_request(body)
    return _deny(f"groups {groups} may not call tool '{tool}'", req_id)


def _handle_response(mcp):
    gw_req = mcp.get("gatewayRequest") or {}
    gw_resp = mcp.get("gatewayResponse") or {}
    headers = gw_req.get("headers") or {}
    req_body = gw_req.get("body") or {}
    status = gw_resp.get("statusCode", 200)
    resp_body = gw_resp.get("body") or {}

    if req_body.get("method", "") != "tools/list":
        return _pass_response(status, resp_body)   # 非 list 原样返回

    groups = _decode_jwt_groups(_get_header(headers, "Authorization")) or []
    allowed = _allowed_tools_for(groups)
    result = resp_body.get("result") or {}
    tools = result.get("tools", [])
    result["tools"] = [t for t in tools if _strip_tool(t.get("name", "")) in allowed]
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
