"""
Runtime A —— 面向用户的 Agent (产品形态), outbound 3LO 主入口。

链路:
  用户(Cognito access token) --Bearer--> Runtime A (入站 JWT 校验)
    --> Runtime A 用【入站用户 token】经 MCPClient/裸MCP 调 MCP Gateway 的 tools/call
    --> Gateway 发现下游 target 需 3LO 且 Vault 无缓存 token
        --> 返回 -32042 URL elicitation (含登录 URL)
    --> Runtime A 把登录 URL 透出给用户 ("请点开这个 URL 登录授权")
  用户在浏览器登录同意 -> 回调服务器完成 session 绑定 -> token 入 Vault
  用户重试 -> Gateway 用 Vault token 调下游 MCP Server -> 成功返回业务结果

为什么直接用裸 MCP 调 tools/call 而非全靠 Strands Agent 自主调用:
  Strands 旧版 MCPClient 会把 -32042 错误吞成一句 "This request requires more information."
  丢掉登录 URL (见 sdk-python #1742, 修复 PR #1745)。为让 demo 稳定拿到 URL,
  本入口显式发 tools/call 并解析 -32042.data.elicitations[].url 透出给用户。
"""
import base64
import json
import os
import urllib.request
import urllib.error

from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

GATEWAY_URL = os.environ["GATEWAY_URL"]
RETURN_URL = os.environ.get("RETURN_URL", "https://callback.chrisai.blog/callback")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def _decode_claims(token):
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode())
    except Exception:
        return {}


def _invoke_mcp(token, method, params):
    payload = {"jsonrpc": "2.0", "id": 24, "method": method, "params": params}
    req = urllib.request.Request(
        GATEWAY_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}",
                 "Accept": "application/json, text/event-stream",
                 "MCP-Protocol-Version": "2025-11-25"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    return json.loads(raw)


def _meta(force):
    return {"aws.bedrock-agentcore.gateway/credentialProviderConfiguration":
            {"oauthCredentialProvider": {"returnUrl": RETURN_URL,
                                         "forceAuthentication": bool(force)}}}


@app.entrypoint
def invoke(payload, context):
    """
    payload:
      { "tool": "get_token_price", "arguments": {...},
        "force_auth": false }         # 演示用: true 强制重新授权(每次弹 URL)
    """
    try:
        auth = context.request_headers.get("Authorization")
    except Exception:
        auth = None
    if not auth or not auth.startswith("Bearer "):
        return {"error": "no bearer token (Runtime 需 allowlist Authorization)"}
    token = auth[len("Bearer "):]
    claims = _decode_claims(token)

    short_tool = payload.get("tool", "get_token_price")
    tool = short_tool if "___" in short_tool else f"okxmcp___{short_tool}"
    args = payload.get("arguments", {"symbol": "BTC"})
    force = payload.get("force_auth", False)

    # 先看可见工具 (tools/list 无需下游授权)
    listed = _invoke_mcp(token, "tools/list", {})
    visible = [t["name"] for t in listed.get("result", {}).get("tools", [])]

    # 关键: tools/call —— 若未授权, Gateway 返回 -32042
    resp = _invoke_mcp(token, "tools/call",
                       {"name": tool, "arguments": args, "_meta": _meta(force)})

    err = resp.get("error") or {}
    if err.get("code") == -32042:
        els = (err.get("data") or {}).get("elicitations") or []
        url = els[0].get("url") if els else None
        return {
            "status": "AUTHORIZATION_REQUIRED",
            "message": "调用该工具需要你先登录授权。请在浏览器打开下面的 URL 完成登录并同意, "
                       "然后重试本次调用。",
            "authorization_url": url,
            "identity": {"username": claims.get("username")},
            "gateway_visible_tools": visible,
        }

    if "result" in resp and not resp.get("error"):
        return {
            "status": "OK",
            "identity": {"username": claims.get("username")},
            "gateway_visible_tools": visible,
            "tool": tool,
            "result": resp["result"],
        }

    return {"status": "ERROR", "raw": resp, "gateway_visible_tools": visible}


if __name__ == "__main__":
    app.run()
