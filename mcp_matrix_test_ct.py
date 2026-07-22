"""
【cognito-test 分支】确定性权限矩阵测试 (核心证据)。
用每个用户的 Cognito access token (含 Pre-Token-Gen V2 注入的 role_id claim) 直连 Gateway MCP,
对每个 tool 跑 tools/call, 记录 ALLOW/DENIED, 生成 role_id×tool 权限矩阵;
同时跑 tools/list 看每个用户可见的工具集 (验证 RESPONSE 拦截器 / Cedar 过滤)。

用法: python mcp_matrix_test_ct.py <GATEWAY_URL> <MECHANISM_LABEL>
环境: set -a; source cognito_ids_ct.env; set +a  (需 CLIENT_ID/AWS_REGION)
输出: matrix_ct_<label>.json
"""
import json
import os
import sys
import urllib.request
import urllib.error
import boto3

REGION = os.environ["AWS_REGION"]
CLIENT_ID = os.environ["CLIENT_ID"]
PW = os.environ.get("DEMO_PASSWORD", "OkxDemo#2026")
GATEWAY_URL = sys.argv[1]
LABEL = sys.argv[2] if len(sys.argv) > 2 else "interceptor"

# 用户 → role_id (由 custom:role_id 注入 access token)
USERS = {"viewer-user": "1001", "analyst-user": "1002", "trader-user": "1003"}
TOOLS = {
    "get_token_price": {"symbol": "BTC"},
    "calc_impermanent_loss": {"price_ratio": 2.0},
    "place_order": {"symbol": "BTC", "side": "buy", "qty": 0.01},
}
TARGET = os.environ.get("TARGET_NAME", "defi")
cog = boto3.client("cognito-idp", region_name=REGION)


def get_token(user):
    r = cog.initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=CLIENT_ID,
                          AuthParameters={"USERNAME": user, "PASSWORD": PW})
    return r["AuthenticationResult"]["AccessToken"]


def mcp_call(token, method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(GATEWAY_URL, data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode(errors="replace") or "{}")
    except Exception as e:
        return -1, {"transport_error": str(e)}


def classify(status, payload):
    if isinstance(payload, dict) and payload.get("error"):
        return "DENIED"
    res = payload.get("result", {}) if isinstance(payload, dict) else {}
    if isinstance(res, dict) and res.get("isError"):
        txt = json.dumps(res, ensure_ascii=False)
        if "DENIED" in txt or "denied" in txt.lower() or "forbidden" in txt.lower():
            return "DENIED"
    if status == 200 and res:
        return "ALLOW"
    if status in (401, 403):
        return "DENIED"
    return f"OTHER({status})"


def main():
    out = {"mechanism": LABEL, "gateway_url": GATEWAY_URL, "role_map": USERS,
           "matrix": {}, "tools_list": {}, "raw": {}}
    for user, rid in USERS.items():
        tok = get_token(user)
        st, pl = mcp_call(tok, "tools/list")
        visible = sorted(
            t["name"].split("___")[-1]
            for t in pl.get("result", {}).get("tools", [])
        ) if isinstance(pl, dict) else []
        out["tools_list"][user] = visible
        out["matrix"][user] = {}
        out["raw"][user] = {}
        for tool, args in TOOLS.items():
            st, pl = mcp_call(tok, "tools/call",
                              {"name": f"{TARGET}___{tool}", "arguments": args})
            out["matrix"][user][tool] = classify(st, pl)
            out["raw"][user][tool] = {"http": st, "payload": pl}

    fn = f"matrix_ct_{LABEL}.json"
    json.dump(out, open(fn, "w"), ensure_ascii=False, indent=2)
    print(f"\n=== role_id 权限矩阵 [{LABEL}] ===")
    header = ["user (role_id) \\ tool"] + list(TOOLS)
    print(" | ".join(f"{h:24}" for h in header))
    for user, rid in USERS.items():
        row = [f"{user}({rid})"] + [out["matrix"][user][t] for t in TOOLS]
        print(" | ".join(f"{c:24}" for c in row))
    print("\n=== tools/list 可见工具 ===")
    for user in USERS:
        print(f"  {user:16} -> {out['tools_list'][user]}")
    print(f"\nsaved {fn}")


if __name__ == "__main__":
    main()
