"""
outbound 3LO 端到端测试 (裸 MCP over HTTPS, 确定性证据)。

用法:
  python3.12 e2e_3lo_test.py list          # 只跑 tools/list (无需授权)
  python3.12 e2e_3lo_test.py first         # 首次 tools/call -> 期望 -32042, 打印登录 URL
  python3.12 e2e_3lo_test.py prestore      # 把发起用户 token 预存到回调服务器 (session 绑定用)
  python3.12 e2e_3lo_test.py retry         # 授权后重试 tools/call -> 期望成功

读环境变量 (source ob_ids.env): GW_URL / CLIENT_ID / CLIENT_SECRET / DEMO_USER /
  DEMO_PASSWORD / AWS_REGION / RETURN_URL。可选 CALLBACK_BASE (预存 token 用, 默认从 RETURN_URL 推导)。
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.request

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
GW_URL = os.environ["GW_URL"]
CLIENT_ID = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]
USER = os.environ.get("DEMO_USER", "demo-user")
PW = os.environ["DEMO_PASSWORD"]
RETURN_URL = os.environ.get("RETURN_URL", "https://callback.chrisai.blog/callback")
TOOL = os.environ.get("TOOL_NAME", "okxmcp___get_token_price")


def _secret_hash(user):
    return base64.b64encode(hmac.new(CLIENT_SECRET.encode(),
                                     (user + CLIENT_ID).encode(),
                                     hashlib.sha256).digest()).decode()


def get_token(user=USER):
    c = boto3.client("cognito-idp", region_name=REGION)
    r = c.initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=CLIENT_ID,
                        AuthParameters={"USERNAME": user, "PASSWORD": PW,
                                        "SECRET_HASH": _secret_hash(user)})
    return r["AuthenticationResult"]["AccessToken"]


def invoke_mcp(token, method, params):
    payload = {"jsonrpc": "2.0", "id": 24, "method": method, "params": params}
    req = urllib.request.Request(
        GW_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}",
                 "Accept": "application/json, text/event-stream",
                 "MCP-Protocol-Version": "2025-11-25"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
    # 兼容 SSE (event: message\n data: {...})
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    return json.loads(raw)


def _meta(force):
    return {"aws.bedrock-agentcore.gateway/credentialProviderConfiguration":
            {"oauthCredentialProvider": {"returnUrl": RETURN_URL,
                                         "forceAuthentication": force}}}


def extract_auth_url(resp):
    err = resp.get("error") or {}
    if err.get("code") == -32042:
        els = (err.get("data") or {}).get("elicitations") or []
        if els:
            return els[0].get("url")
    return None


def cmd_list():
    tok = get_token()
    r = invoke_mcp(tok, "tools/list", {})
    names = [t["name"] for t in r.get("result", {}).get("tools", [])]
    print("tools/list ok. visible tools:", names)
    return r


def cmd_first(force=True):
    tok = get_token()
    r = invoke_mcp(tok, "tools/call",
                   {"name": TOOL, "arguments": {"symbol": "BTC"}, "_meta": _meta(force)})
    print(json.dumps(r, ensure_ascii=False, indent=2))
    url = extract_auth_url(r)
    if url:
        print("\n" + "=" * 70)
        print("🔑 需要授权! 请在浏览器打开下面的 URL 登录并同意:")
        print(url)
        print("=" * 70)
    else:
        print("\n(未返回 -32042; 可能 Vault 已有缓存 token — 用 forceAuthentication 或先 cleanup vault)")
    return r


def _prestore(tok):
    """把发起用户 token 预存到回调服务器 (session 绑定所需)。"""
    base = os.environ.get("CALLBACK_BASE") or RETURN_URL.rsplit("/", 1)[0]
    req = urllib.request.Request(base + "/userIdentifier/token",
                                 data=json.dumps({"user_token": tok}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def cmd_prestore():
    print("prestore ->", _prestore(get_token()))


def cmd_start():
    """【推荐·手动演示一步到位】: 预存当前用户 token + 触发 tools/call + 打印登录 URL。

    这是修复手动流程 500 的关键 —— 必须用【发起本次调用的同一个 token】做 session 绑定,
    所以取 token → 立刻 prestore → 再触发, 三步绑定同一身份。
    """
    tok = get_token()
    st = _prestore(tok)
    print(f"① 已预存当前用户 token 到回调服务器 (session 绑定用, http {st})")
    r = invoke_mcp(tok, "tools/call",
                   {"name": TOOL, "arguments": {"symbol": "BTC"}, "_meta": _meta(True)})
    url = extract_auth_url(r)
    if url:
        print("\n" + "=" * 70)
        print("🔑 请在浏览器打开下面的 URL 登录 (demo-user / 见 DEMO_PASSWORD) 并同意:")
        print(url)
        print("=" * 70)
        print("授权完成 (看到绿色 ✅ 授权完成 页) 后, 运行: python3.12 e2e_3lo_test.py retry")
    else:
        print("未拿到登录 URL, 原始响应:\n", json.dumps(r, ensure_ascii=False, indent=2))
    return r


def cmd_retry():
    tok = get_token()
    r = invoke_mcp(tok, "tools/call",
                   {"name": TOOL, "arguments": {"symbol": "BTC"}, "_meta": _meta(False)})
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if "result" in r and not r.get("error"):
        print("\n✅ 授权后调用成功 (Vault 命中, 未再弹 URL)")
    return r


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    {"list": cmd_list, "first": cmd_first, "start": cmd_start,
     "prestore": cmd_prestore, "retry": cmd_retry}[cmd]()
