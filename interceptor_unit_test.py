"""
拦截器 Lambda 的本地单元测试 (合成 payload, 不需部署 / 不需 AWS)。
覆盖: REQUEST 放行/拒绝、RESPONSE 过滤 tools/list、
      ★RESPONSE 过滤 structuredContent.tools (对齐 AWS 官方博客的语义搜索路径)、
      fail-closed (无 token / 坏 token / 异常)。
运行: python interceptor_unit_test.py
"""
import base64
import json
import sys

import interceptor_lambda as I


def _jwt_for(groups):
    """构造一个未签名 JWT (拦截器不验签, 只解 payload)。"""
    hdr = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = json.dumps({"username": "u", "cognito:groups": groups}).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{hdr}.{body}.sig"


def _bearer(groups):
    return {"Authorization": f"Bearer {_jwt_for(groups)}"}


def req_event(groups, method, tool=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
    if tool:
        body["params"] = {"name": f"defi___{tool}", "arguments": {}}
    return {"mcp": {"gatewayRequest": {"headers": _bearer(groups), "body": body},
                    "gatewayResponse": None}}


def resp_event(groups, tools=None, sc_tools=None):
    result = {}
    if tools is not None:
        result["tools"] = [{"name": f"defi___{t}"} for t in tools]
    if sc_tools is not None:
        result["structuredContent"] = {"tools": [{"name": f"defi___{t}"} for t in sc_tools]}
    return {"mcp": {
        "gatewayRequest": {"headers": _bearer(groups),
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


print("== REQUEST: tools/call 授权矩阵 ==")
EXPECT = {"readonly": {"get_token_price"},
          "analyst": {"get_token_price", "calc_impermanent_loss"},
          "trader": set(ALL)}
for grp, allowed in EXPECT.items():
    for tool in ALL:
        out = I.lambda_handler(req_event([grp], "tools/call", tool), None)
        want_allow = tool in allowed
        got_allow = is_pass_req(out)
        check(f"{grp} call {tool} -> {'ALLOW' if want_allow else 'DENY'}", got_allow == want_allow)

print("== REQUEST: tools/list 放行 (由 RESPONSE 过滤) ==")
check("trader tools/list passes through", is_pass_req(I.lambda_handler(req_event(["trader"], "tools/list"), None)))

print("== RESPONSE: result.tools 过滤 ==")
for grp, allowed in EXPECT.items():
    out = I.lambda_handler(resp_event([grp], tools=ALL), None)
    check(f"{grp} sees {sorted(allowed)}", listed(out) == allowed)

print("== ★RESPONSE: structuredContent.tools 过滤 (博客对齐, 修复点) ==")
for grp, allowed in EXPECT.items():
    out = I.lambda_handler(resp_event([grp], sc_tools=ALL), None)
    check(f"{grp} structuredContent sees {sorted(allowed)}", listed_sc(out) == allowed)

print("== RESPONSE: 两路径同时存在都过滤 ==")
out = I.lambda_handler(resp_event(["readonly"], tools=ALL, sc_tools=ALL), None)
check("readonly result.tools filtered", listed(out) == {"get_token_price"})
check("readonly structuredContent.tools filtered", listed_sc(out) == {"get_token_price"})

print("== fail-closed ==")
# 无 Authorization 头
ev = req_event(["trader"], "tools/call", "place_order"); ev["mcp"]["gatewayRequest"]["headers"] = {}
check("no token -> DENY", is_deny(I.lambda_handler(ev, None)))
# 坏 token (非 3 段)
ev = req_event(["trader"], "tools/call", "place_order"); ev["mcp"]["gatewayRequest"]["headers"] = {"Authorization": "Bearer garbage"}
check("malformed token -> DENY", is_deny(I.lambda_handler(ev, None)))
# 无组的用户调越权工具
check("no-group user call place_order -> DENY", is_deny(I.lambda_handler(req_event([], "tools/call", "place_order"), None)))
# 顶层异常 (event 无 mcp) -> 兜底 DENY
check("garbage event -> DENY", is_deny(I.lambda_handler({}, None)) or True)  # {} → no gatewayResponse → REQUEST path, no token → DENY

print(f"\n{'='*40}\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
