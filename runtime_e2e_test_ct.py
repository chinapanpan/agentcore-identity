"""
【cognito-test 分支】Runtime 端到端驱动: 3 用户 (role_id 1001/1002/1003) 各发一条 prompt,
经 Runtime(入站 JWT) → agent 用 Strands MCPClient 透传 token → Gateway A(拦截器 role_id 授权)。
证明: 同一 agent 代码, 因 role_id 不同, 可见/可用工具集不同。

★用唯一 session id (带序号) 规避 warm-session 缓存 (Task2 教训)。
用法: python runtime_e2e_test_ct.py <run_tag>   (run_tag 用于生成唯一 session, 如时间戳)
需 set -a; source cognito_ids_ct.env; set +a  (RT_ARN/CLIENT_ID/AWS_REGION)
输出: runtime_e2e_ct_result.json
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
PW = os.environ.get("DEMO_PASSWORD", "OkxDemo#2026")
RUN = sys.argv[1] if len(sys.argv) > 1 else "run1"

CASES = [
    ("viewer-user", "1001", "帮我查一下 ETH 现在多少钱？再帮我下单买 0.01 个 BTC。"),
    ("analyst-user", "1002", "价格翻倍(price_ratio=2)时无常损失是多少？顺便帮我下单卖 1 个 ETH。"),
    ("trader-user", "1003", "查一下 BTC 现价, 然后帮我下单买 0.01 个 BTC。"),
]

cog = boto3.client("cognito-idp", region_name=REGION)


def token(u):
    return cog.initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=CLIENT_ID,
                             AuthParameters={"USERNAME": u, "PASSWORD": PW})["AuthenticationResult"]["AccessToken"]


def invoke(u, prompt, idx):
    tok = token(u)
    enc = urllib.parse.quote(RT_ARN, safe="")
    url = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{enc}/invocations?qualifier=DEFAULT"
    sid = f"okx-ct-{u}-{RUN}-{idx:024d}"   # 唯一 session, 规避 warm 缓存
    body = json.dumps({"prompt": prompt}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {tok}", "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sid})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode(errors="replace")}


def main():
    results = []
    for i, (u, rid, prompt) in enumerate(CASES):
        out = invoke(u, prompt, i)
        results.append({"user": u, "role_id": rid, "prompt": prompt, "result": out})
        print(f"\n=== {u} (role_id={rid}) ===")
        print(f"visible_tools = {out.get('gateway_visible_tools')}")
        print(f"tool_trace    = {[t.get('tool') for t in (out.get('tool_trace') or [])]}")
        print(f"answer        = {str(out.get('answer'))[:400]}")
        if out.get("agent_error"):
            print(f"agent_error   = {out['agent_error'][:300]}")
        if out.get("http_error"):
            print(f"http_error    = {out['http_error']}: {out.get('body','')[:300]}")
    json.dump(results, open("runtime_e2e_ct_result.json", "w"), ensure_ascii=False, indent=2)
    print("\nsaved runtime_e2e_ct_result.json")


if __name__ == "__main__":
    main()
