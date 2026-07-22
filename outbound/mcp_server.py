"""
Runtime B —— 受 OAuth 保护的 MCP Server (serverProtocol=MCP)。

在 outbound 3LO demo 里, 这是"下游受保护资源":
  Runtime A(Agent) --MCPClient--> MCP Gateway --(带 3LO 拿到的 user token)--> 本 MCP Server

本 server 用 FastMCP 暴露 streamable-http MCP endpoint (0.0.0.0:8080/mcp)。
AgentCore Runtime 的【入站 JWT authorizer】负责校验 Bearer token(验签/issuer/audience);
无有效 token 的请求在到达本进程之前就被 Runtime 前置拦截 —— 这正是"受 OAuth 保护"的含义。
本进程内再打印一次 caller 身份, 便于 demo 时观察是谁(哪个用户 token)调进来的。

工具集(纯 mock, 零副作用, 贴合 OKX Web3 场景):
  - get_token_price(symbol)
  - calc_impermanent_loss(price_ratio)
  - place_order(symbol, side, qty)
"""
from mcp.server.fastmcp import FastMCP

# host=0.0.0.0 + stateless_http=True 是 AgentCore Runtime 托管 MCP 的要求:
# Runtime 前置代理需要无状态 streamable-http, MCP runtime 监听容器内 8000/mcp。
mcp = FastMCP(host="0.0.0.0", port=8000, stateless_http=True)

_PRICES = {"BTC": 64250.0, "ETH": 3120.5, "SOL": 172.3, "OKB": 51.7, "USDT": 1.0}


@mcp.tool()
def get_token_price(symbol: str) -> dict:
    """查询指定加密货币的当前美元价格 (演示 mock 行情)。"""
    s = str(symbol).upper()
    price = _PRICES.get(s)
    if price is None:
        return {"symbol": s, "error": "unknown symbol", "known": list(_PRICES)}
    return {"symbol": s, "price_usd": price, "source": "okx-ob-mcp-server"}


@mcp.tool()
def calc_impermanent_loss(price_ratio: float) -> dict:
    """根据价格变动比 r 计算流动性做市的无常损失百分比 (负数=损失)。"""
    r = float(price_ratio)
    if r <= 0:
        return {"error": "price_ratio must be > 0"}
    il = 2 * (r ** 0.5) / (1 + r) - 1
    return {"price_ratio": r, "impermanent_loss_pct": round(il * 100, 4),
            "note": "相对持有的无常损失(负数=损失)"}


@mcp.tool()
def place_order(symbol: str, side: str, qty: float) -> dict:
    """提交一笔下单 (纯演示 mock, 零副作用, 不触达真实交易所)。"""
    return {"status": "MOCK_ACCEPTED",
            "order": {"symbol": str(symbol).upper(), "side": side, "qty": qty},
            "warning": "这是演示 mock 下单, 未触达任何真实交易系统",
            "served_by": "okx-ob-mcp-server (Runtime B, OAuth 保护)"}


if __name__ == "__main__":
    # streamable-http transport: 暴露 /mcp 端点
    mcp.run(transport="streamable-http")
