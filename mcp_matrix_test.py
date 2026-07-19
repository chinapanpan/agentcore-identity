"""
确定性权限矩阵测试 (核心证据)。
用每个用户的 Cognito JWT 直连 Gateway MCP endpoint (绕过 LLM 非确定性),
对每个 tool 跑 tools/call, 记录 200/DENIED, 生成权限矩阵。
同时跑一次 tools/list 看每个用户可见的工具集 (验证 RESPONSE 拦截器过滤 / Cedar 行为)。

用法: python mcp_matrix_test.py <GATEWAY_URL> <MECHANISM_LABEL>
环境: 需 source cognito_ids.env (POOL_ID/CLIENT_ID/AWS_REGION)
输出: matrix_<label>.json
"""
import json
import os
import sys
import boto3
import urllib.request

REGION = os.environ["AWS_REGION"]
CLIENT_ID = os.environ["CLIENT_ID"]
PW = os.environ.get("DEMO_PASSWORD", "OkxDemo#2026")  # 临时演示用户密码, 部署后即用即删
GATEWAY_URL = sys.argv[1]
LABEL = sys.argv[2] if len(sys.argv) > 2 else "interceptor"

USERS = ["readonly-user", "analyst-user", "trader-user"]
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
    """判定该次 tools/call 是 ALLOW 还是 DENY。"""
    if isinstance(payload, dict) and payload.get("error"):
        return "DENIED"
    res = payload.get("result", {}) if isinstance(payload, dict) else {}
    # MCP tools/call 错误也可能包在 result.isError
    if isinstance(res, dict) and res.get("isError"):
        # 检查是否 authz 拒绝
        txt = json.dumps(res, ensure_ascii=False)
        if "DENIED" in txt or "denied" in txt or "forbidden" in txt.lower():
            return "DENIED"
    if status == 200 and res:
        return "ALLOW"
    if status in (401, 403):
        return "DENIED"
    return f"OTHER({status})"


def main():
    out = {"mechanism": LABEL, "gateway_url": GATEWAY_URL, "matrix": {}, "tools_list": {}, "raw": {}}
    for user in USERS:
        tok = get_token(user)
        # tools/list
        st, pl = mcp_call(tok, "tools/list")
        visible = sorted(
            t["name"].split("___")[-1]
            for t in pl.get("result", {}).get("tools", [])
        ) if isinstance(pl, dict) else []
        out["tools_list"][user] = visible
        # tools/call each tool
        out["matrix"][user] = {}
        out["raw"][user] = {}
        for tool, args in TOOLS.items():
            st, pl = mcp_call(tok, "tools/call",
                              {"name": f"{TARGET}___{tool}", "arguments": args})
            verdict = classify(st, pl)
            out["matrix"][user][tool] = verdict
            out["raw"][user][tool] = {"http": st, "payload": pl}

    fn = f"matrix_{LABEL}.json"
    json.dump(out, open(fn, "w"), ensure_ascii=False, indent=2)

    # 打印矩阵
    print(f"\n=== 权限矩阵 [{LABEL}] ===")
    header = ["user \\ tool"] + list(TOOLS)
    print(" | ".join(f"{h:22}" for h in header))
    for user in USERS:
        row = [user] + [out["matrix"][user][t] for t in TOOLS]
        print(" | ".join(f"{c:22}" for c in row))
    print("\n=== tools/list 可见工具 ===")
    for user in USERS:
        print(f"  {user:16} -> {out['tools_list'][user]}")
    print(f"\nsaved {fn}")


if __name__ == "__main__":
    main()
