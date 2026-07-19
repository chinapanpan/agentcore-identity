"""
Runtime happy-path 端到端测试 (JWT 入站 → agent 透传 → Gateway 授权)。
用裸 HTTPS POST 带 Authorization: Bearer <用户JWT> 调 InvokeAgentRuntime
(boto3 不支持 bearer-token 调用)。

用法: python runtime_invoke_test.py <username> "<prompt>"
需 source cognito_ids.env (RT_ARN/CLIENT_ID/AWS_REGION)
"""
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
import boto3

REGION = os.environ["AWS_REGION"]
CLIENT_ID = os.environ["CLIENT_ID"]
RT_ARN = os.environ["RT_ARN"]
PW = os.environ.get("DEMO_PASSWORD", "OkxDemo#2026")  # 临时演示用户密码, 部署后即用即删

user = sys.argv[1] if len(sys.argv) > 1 else "trader-user"
prompt = sys.argv[2] if len(sys.argv) > 2 else "帮我查一下 BTC 现在多少钱, 再帮我下单买 0.01 个 BTC。"

cog = boto3.client("cognito-idp", region_name=REGION)
tok = cog.initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=CLIENT_ID,
                        AuthParameters={"USERNAME": user, "PASSWORD": PW})["AuthenticationResult"]["AccessToken"]

# InvokeAgentRuntime REST endpoint (bearer token 路径)
enc = urllib.parse.quote(RT_ARN, safe="")
url = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{enc}/invocations?qualifier=DEFAULT"
sid = f"okx-identity-{user}-session-000000000001"

body = json.dumps({"prompt": prompt}).encode()
req = urllib.request.Request(url, data=body, headers={
    "Authorization": f"Bearer {tok}",
    "Content-Type": "application/json",
    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sid,
})
try:
    with urllib.request.urlopen(req, timeout=90) as resp:
        out = json.loads(resp.read())
except urllib.error.HTTPError as e:
    out = {"http_error": e.code, "body": e.read().decode(errors="replace")}

print(f"=== user={user} ===")
print(json.dumps(out, ensure_ascii=False, indent=2)[:1500])
