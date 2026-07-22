"""
AgentCore Runtime 上的 Strands agent — 【cognito-test 分支】Cognito 自定义 role_id claim 全流程。

链路:
  用户(Cognito access token, 含 Pre-Token-Gen V2 注入的 role_id) --Bearer--> Runtime(入站 JWT authorizer 校验)
    --> agent 从 allowlisted Authorization header 取【原始用户 access token】
    --> 用 Strands MCPClient 透传该 token 到 AgentCore Gateway MCP endpoint
    --> Gateway 侧按 role_id (拦截器 or Cedar) 做 tool 级授权
  → 用户能力(可用工具)完全由其 role_id 决定。

★★★ 本 Demo 的核心重点 (用户重点 #2): Runtime 内的 MCP client 是否需要改造才能带 Header?
  答: **需要, 且必须显式配置**。
  Strands 的 MCPClient 本身不会自动把"入站用户的 token"透传给下游 Gateway。
  我们用 streamablehttp_client(url, headers={...}) 显式注入 Authorization 头 —
  见下方 _make_mcp_client()。若不注入, Gateway 入站 CUSTOM_JWT 授权器会直接 401。
"""
import base64
import json
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client

app = BedrockAgentCoreApp()

GATEWAY_URL = os.environ["GATEWAY_URL"]      # 指向 Gateway A(拦截器) 或 B(Cedar)
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def _decode_claims(token):
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload).decode())


def _make_mcp_client(user_token):
    """
    ★核心: 构造一个会【携带用户 Authorization 头】的 Strands MCPClient。

    MCPClient 接收一个 transport 工厂 (无参 callable), 每次连接时调用它建立传输层。
    我们在工厂里用 streamablehttp_client(url, headers=...) 把用户的 Bearer token
    作为 HTTP 头注入 —— 这就是"让 MCP client 带 Header"的关键改造点。
    """
    def _transport():
        return streamablehttp_client(
            GATEWAY_URL,
            headers={"Authorization": f"Bearer {user_token}"},   # ← 透传用户原始 access token
        )
    return MCPClient(_transport)


@app.entrypoint
def invoke(payload, context):
    prompt = payload.get("prompt", "你好")
    # ① 从 allowlisted Authorization header 拿到【原始用户 access token】
    try:
        auth = context.request_headers.get("Authorization")
    except Exception:
        auth = None
    if not auth or not auth.startswith("Bearer "):
        return {"error": "no bearer token in request headers (需 allowlist Authorization)"}
    token = auth[len("Bearer "):]
    claims = _decode_claims(token)
    username = claims.get("username")
    role_id = claims.get("role_id")           # ← 由 Pre-Token-Gen V2 注入的自定义 claim

    trace = []
    err = None
    visible = []
    # ② 用【带 Authorization 头的 MCPClient】连 Gateway; with 块内 session 有效
    mcp_client = _make_mcp_client(token)
    try:
        with mcp_client:
            gw_tools = mcp_client.list_tools_sync()   # Gateway 已按 role_id 过滤
            visible = [t.tool_name for t in gw_tools]

            # ③ Strands agent 直接使用 MCPClient 暴露的工具 (调用时自动带同一个 Authorization 头)
            model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
            sysprompt = (f"你是 OKX 的 Web3 助手。当前用户={username}, role_id={role_id}。"
                         f"你只能使用被授权的工具; 若用户请求越权操作, 说明其无权限。")
            agent = Agent(model=model, system_prompt=sysprompt, tools=gw_tools)
            result = agent(prompt)

            # 汇总工具调用轨迹 (从 agent 的 messages 里提取 toolUse/toolResult)
            for m in agent.messages:
                for block in (m.get("content") or []):
                    if isinstance(block, dict) and "toolUse" in block:
                        trace.append({"tool": block["toolUse"].get("name"),
                                      "input": block["toolUse"].get("input")})
    except Exception as e:
        import traceback
        err = repr(e) + "\n" + traceback.format_exc()
        result = f"(agent 执行异常: {e})"

    return {
        "answer": str(result),
        "identity": {"username": username, "role_id": role_id},
        "gateway_visible_tools": visible,
        "tool_trace": trace,
        "agent_error": err,
        "gateway_url": GATEWAY_URL.split("//")[-1].split(".")[0],
    }


if __name__ == "__main__":
    app.run()
