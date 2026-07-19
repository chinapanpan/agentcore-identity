"""
AgentCore Runtime 上的 Strands agent — Identity/鉴权 全流程 Demo 的入口。

链路:
  用户(Cognito JWT) --Bearer--> Runtime(入站 JWT authorizer 校验)
    --> agent 从 allowlisted Authorization header 取【原始用户 JWT】
    --> 原样透传给 AgentCore Gateway MCP endpoint (不另铸 M2M token, 保住 cognito:groups/username)
    --> Gateway 侧按拦截器 or Cedar 做 tool 级授权
  → 用户能力(可用工具)完全由其身份(组/用户名)决定, 无权限工具在 Runtime 侧也调不动。

演示: agent 读到调用方 groups, 只把"有权限的工具"作为 MCP 工具暴露给 LLM;
即使 LLM 尝试越权, Gateway 也会拒绝 (双保险)。
"""
import base64
import json
import os
import urllib.request
import urllib.error

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

app = BedrockAgentCoreApp()

GATEWAY_URL = os.environ["GATEWAY_URL"]      # 指向 Gateway A 或 B 的 MCP endpoint
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def _decode_claims(token):
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload).decode())


def _mcp(token, method, params=None):
    """直连 Gateway MCP endpoint, 透传用户原始 Bearer token。"""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(GATEWAY_URL, data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": {"http": e.code, "body": e.read().decode(errors="replace")}}


def _list_tools(token):
    r = _mcp(token, "tools/list")
    return r.get("result", {}).get("tools", [])


_TRACE = []  # 记录本次 invoke 的工具调用轨迹, 回填到响应便于观测


def _call_tool(token, full_name, arguments):
    r = _mcp(token, "tools/call", {"name": full_name, "arguments": arguments})
    _TRACE.append({"tool": full_name, "args": arguments,
                   "resp": json.dumps(r, ensure_ascii=False)[:400]})
    return r


@app.entrypoint
def invoke(payload, context):
    prompt = payload.get("prompt", "你好")
    # ① 从 allowlisted Authorization header 拿到【原始用户 JWT】
    auth = None
    try:
        auth = context.request_headers.get("Authorization")
    except Exception:
        auth = None
    if not auth or not auth.startswith("Bearer "):
        return {"error": "no bearer token in request headers (需 allowlist Authorization)"}
    token = auth[len("Bearer "):]
    claims = _decode_claims(token)
    username = claims.get("username")
    groups = claims.get("cognito:groups", [])

    # ② 用【同一个用户 token】向 Gateway 拿该用户可见的工具 (Gateway 已按身份过滤)
    tools = _list_tools(token)
    visible = [t["name"] for t in tools]

    # ③ 把 Gateway 工具包成 Strands 本地工具 (每次调用继续透传该用户 token)
    #    动态生成【带显式命名参数】的函数, 以便 Strands 能推导出正确的工具 schema,
    #    否则 **kwargs 会让 LLM 不知道参数名而传错。
    from strands import tool as strands_tool
    strands_tools = []
    for t in tools:
        full = t["name"]
        short = full.split("___")[-1]
        props = (t.get("inputSchema") or {}).get("properties", {}) or {}
        params = list(props.keys())

        def make(fn_full, fn_short, param_names, schema_props):
            def _impl(**kwargs):
                return json.dumps(_call_tool(token, fn_full, kwargs), ensure_ascii=False)
            # 用 exec 造出带【显式形参 + 类型注解】的包装, 让 Strands 正确推导工具 schema。
            typemap = {"string": "str", "number": "float", "integer": "int",
                       "boolean": "bool", "object": "dict", "array": "list"}
            sig = ", ".join(
                f"{p}: {typemap.get(schema_props[p].get('type', 'string'), 'str')}"
                for p in param_names)
            call = ", ".join(f"{p}={p}" for p in param_names)
            doc = t.get("description", fn_short) + "\\n\\nArgs:\\n" + "\\n".join(
                f"    {p}: {schema_props[p].get('description', p)}" for p in param_names)
            src = (f"def {fn_short}({sig}):\n"
                   f"    '''{doc}'''\n"
                   f"    return _impl({call})\n")
            ns = {"_impl": _impl, "str": str, "float": float, "int": int,
                  "bool": bool, "dict": dict, "list": list}
            exec(src, ns)
            return ns[fn_short]

        strands_tools.append(strands_tool(make(full, short, params, props)))

    # ④ Strands agent 用这些(且仅这些)工具回答
    model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
    sysprompt = (f"你是 OKX 的 Web3 助手。当前用户 ={username}, 用户组={groups}。"
                 f"你只能使用被授权的工具; 若用户请求越权操作, 说明其无权限。")
    _TRACE.clear()
    agent = Agent(model=model, system_prompt=sysprompt, tools=strands_tools)
    err = None
    try:
        result = agent(prompt)
    except Exception as e:
        import traceback
        err = repr(e) + "\n" + traceback.format_exc()
        result = f"(agent 执行异常: {e})"

    return {
        "answer": str(result),
        "identity": {"username": username, "groups": groups},
        "gateway_visible_tools": visible,
        "tool_trace": _TRACE,          # 工具调用轨迹 (观测用)
        "agent_error": err,            # 若 agent 抛异常, 完整栈
        "gateway_url": GATEWAY_URL.split("//")[-1].split(".")[0],
    }


if __name__ == "__main__":
    app.run()
