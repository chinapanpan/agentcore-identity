"""
【cognito-test 分支】拦截器 Lambda 的本地单元测试 (合成 payload, 不需部署 / 不需 AWS)。
鉴权依据 = access token 里的 role_id claim (1001/1002/1003)。
覆盖: REQUEST 放行/拒绝、RESPONSE 过滤 tools/list、
      RESPONSE 过滤 structuredContent.tools (语义搜索路径)、
      fail-closed (无 token / 坏 token / 无 role_id / 异常)。
运行: python interceptor_unit_test.py
"""
import base64
import json
import sys

import interceptor_lambda as I


def _jwt_for(role_id):
    """构造一个未签名 JWT (拦截器不验签, 只解 payload)。role_id=None 表示不带该 claim。"""
    hdr = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    claims = {"username": "u", "token_use": "access"}
    if role_id is not None:
        claims["role_id"] = role_id
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{hdr}.{body}.sig"


def _bearer(role_id):
    return {"Authorization": f"Bearer {_jwt_for(role_id)}"}


def req_event(role_id, method, tool=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
    if tool:
        body["params"] = {"name": f"defi___{tool}", "arguments": {}}
    return {"mcp": {"gatewayRequest": {"headers": _bearer(role_id), "body": body},
                    "gatewayResponse": None}}


def resp_event(role_id, tools=None, sc_tools=None):
    result = {}
    if tools is not None:
        result["tools"] = [{"name": f"defi___{t}"} for t in tools]
    if sc_tools is not None:
        result["structuredContent"] = {"tools": [{"name": f"defi___{t}"} for t in sc_tools]}
    return {"mcp": {
        "gatewayRequest": {"headers": _bearer(role_id),
                           "body": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}},
        "gatewayResponse": {"statusCode": 200, "body": {"jsonrpc": "2.0", "id": 1, "result": result}}}}


ALL = ["get_token_price", "calc_impermanent_loss", "place_order"]
PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✓ {name}")
    else:
        FAIL += 1; print(f"  ✗ {name}  <<< FAILED")


def is_deny(out):
    r = out["mcp"].get("transformedGatewayResponse", {})
    return bool(r.get("body", {}).get("error"))


def is_pass_req(out):
    return "transformedGatewayRequest" in out["mcp"]


def listed(out):
    r = out["mcp"]["transformedGatewayResponse"]["body"]["result"]
    return set(n["name"].split("___")[1] for n in r.get("tools", []))


def listed_sc(out):
    r = out["mcp"]["transformedGatewayResponse"]["body"]["result"]
    return set(n["name"].split("___")[1] for n in r.get("structuredContent", {}).get("tools", []))


# role_id → 期望可用工具集
EXPECT = {"1001": {"get_token_price"},
          "1002": {"get_token_price", "calc_impermanent_loss"},
          "1003": set(ALL)}

print("== REQUEST: tools/call 授权矩阵 (按 role_id) ==")
for rid, allowed in EXPECT.items():
    for tool in ALL:
        out = I.lambda_handler(req_event(rid, "tools/call", tool), None)
        want_allow = tool in allowed
        check(f"role_id {rid} call {tool} -> {'ALLOW' if want_allow else 'DENY'}",
              is_pass_req(out) == want_allow)

print("== REQUEST: tools/list 放行 (由 RESPONSE 过滤) ==")
check("role_id 1003 tools/list passes through",
      is_pass_req(I.lambda_handler(req_event("1003", "tools/list"), None)))

print("== RESPONSE: result.tools 过滤 ==")
for rid, allowed in EXPECT.items():
    out = I.lambda_handler(resp_event(rid, tools=ALL), None)
    check(f"role_id {rid} sees {sorted(allowed)}", listed(out) == allowed)

print("== RESPONSE: structuredContent.tools 过滤 (语义搜索路径) ==")
for rid, allowed in EXPECT.items():
    out = I.lambda_handler(resp_event(rid, sc_tools=ALL), None)
    check(f"role_id {rid} structuredContent sees {sorted(allowed)}", listed_sc(out) == allowed)

print("== RESPONSE: 两路径同时存在都过滤 ==")
out = I.lambda_handler(resp_event("1001", tools=ALL, sc_tools=ALL), None)
check("role_id 1001 result.tools filtered", listed(out) == {"get_token_price"})
check("role_id 1001 structuredContent.tools filtered", listed_sc(out) == {"get_token_price"})

print("== fail-closed ==")
ev = req_event("1003", "tools/call", "place_order"); ev["mcp"]["gatewayRequest"]["headers"] = {}
check("no token -> DENY", is_deny(I.lambda_handler(ev, None)))
ev = req_event("1003", "tools/call", "place_order"); ev["mcp"]["gatewayRequest"]["headers"] = {"Authorization": "Bearer garbage"}
check("malformed token -> DENY", is_deny(I.lambda_handler(ev, None)))
check("no role_id claim call place_order -> DENY",
      is_deny(I.lambda_handler(req_event(None, "tools/call", "place_order"), None)))
check("unknown role_id call place_order -> DENY",
      is_deny(I.lambda_handler(req_event("9999", "tools/call", "place_order"), None)))
check("garbage event -> DENY", is_deny(I.lambda_handler({}, None)))

print(f"\n{'='*40}\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
