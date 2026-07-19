"""
端到端测试驱动: 依次用 3 个用户经 Runtime 调用 Agent, 汇总为 runtime_e2e_result.json。
证明"同一 Agent, 因调用者身份不同 → 可见/可用工具不同"。

用法: python runtime_e2e_test.py
需 source cognito_ids.env (RT_ARN/CLIENT_ID/AWS_REGION)
"""
import json
import os
import urllib.parse
import urllib.request
import urllib.error
import boto3

REGION = os.environ["AWS_REGION"]
CLIENT_ID = os.environ["CLIENT_ID"]
RT_ARN = os.environ["RT_ARN"]
PW = os.environ.get("DEMO_PASSWORD", "OkxDemo#2026")

# (用户, prompt, 用于生成不同 session 的序号)
CASES = [
    ("trader-user", "帮我查 BTC 价格, 再市价买入 0.01 BTC"),
    ("analyst-user", "计算价格变动比为 4 时的无常损失; 顺便帮我下单买 BTC"),
    ("readonly-user", "帮我查 ETH 价格; 再帮我下单买点 BTC"),
]

cog = boto3.client("cognito-idp", region_name=REGION)
enc = urllib.parse.quote(RT_ARN, safe="")
url = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{enc}/invocations?qualifier=DEFAULT"


def token(user):
    return cog.initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=CLIENT_ID,
                             AuthParameters={"USERNAME": user, "PASSWORD": PW})["AuthenticationResult"]["AccessToken"]


def invoke(user, prompt, idx):
    # 用全新 session id, 避免 warm 缓存串扰
    sid = f"okx-e2e-{user}-{idx:020d}"
    req = urllib.request.Request(url, data=json.dumps({"prompt": prompt}).encode(), headers={
        "Authorization": f"Bearer {token(user)}", "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sid})
    with urllib.request.urlopen(req, timeout=100) as r:
        return json.loads(r.read())


def main():
    recs = []
    for i, (user, prompt) in enumerate(CASES):
        out = invoke(user, prompt, i + 1)
        recs.append({
            "user": user,
            "prompt": prompt,
            "identity": out.get("identity"),
            "visible_tools": out.get("gateway_visible_tools"),
            # 记录短名, 便于阅读
            "tools_called": [t["tool"].split("___")[-1] for t in (out.get("tool_trace") or [])],
            "answer": out.get("answer"),
        })
        print(f"[{user}] visible={recs[-1]['visible_tools']} called={recs[-1]['tools_called']}")
    json.dump({"runtime_e2e": recs}, open("runtime_e2e_result.json", "w"),
              ensure_ascii=False, indent=2)
    print(f"saved runtime_e2e_result.json ({len(recs)} records)")


if __name__ == "__main__":
    main()
