"""
AgentCore Gateway 的 Lambda Target — 暴露 3 个 Web3 工具 (贴合 OKX)。
Gateway 调用本函数时:
  - event  = 工具入参 (inputSchema 定义的属性 map)
  - context.client_context.custom['bedrockAgentCoreToolName'] = "<target>___<tool>"
    (三下划线分隔, 需 strip 前缀拿到真实工具名)
工具本身不做鉴权 (鉴权在 Gateway 侧: 拦截器 or Cedar)。place_order 为纯 mock, 零副作用。
"""
import json

# 简单 mock 行情 (演示用, 非真实价格)
_PRICES = {"BTC": 64250.0, "ETH": 3120.5, "SOL": 172.3, "OKB": 51.7, "USDT": 1.0}


def _get_token_price(args):
    symbol = str(args.get("symbol", "BTC")).upper()
    price = _PRICES.get(symbol)
    if price is None:
        return {"symbol": symbol, "error": "unknown symbol", "known": list(_PRICES)}
    return {"symbol": symbol, "price_usd": price, "source": "okx-demo-mock"}


def _calc_impermanent_loss(args):
    # IL = 2*sqrt(r)/(1+r) - 1, r = 价格变动比
    r = float(args.get("price_ratio", 2.0))
    if r <= 0:
        return {"error": "price_ratio must be > 0"}
    il = 2 * (r ** 0.5) / (1 + r) - 1
    return {"price_ratio": r, "impermanent_loss_pct": round(il * 100, 4),
            "note": "相对持有的无常损失(负数=损失)"}


def _place_order(args):
    # 纯 mock: 不接任何真实交易所, 零副作用
    return {"status": "MOCK_ACCEPTED",
            "order": {"symbol": str(args.get("symbol", "BTC")).upper(),
                      "side": args.get("side", "buy"),
                      "qty": args.get("qty", 0.01)},
            "warning": "这是演示 mock 下单, 未触达任何真实交易系统"}


_TOOLS = {
    "get_token_price": _get_token_price,
    "calc_impermanent_loss": _calc_impermanent_loss,
    "place_order": _place_order,
}

_DELIM = "___"


def lambda_handler(event, context):
    # 解析被调用的工具名
    tool_name = "unknown"
    try:
        raw = context.client_context.custom["bedrockAgentCoreToolName"]
        tool_name = raw[raw.index(_DELIM) + len(_DELIM):] if _DELIM in raw else raw
    except Exception as e:
        return {"error": f"cannot resolve tool name: {e}"}

    fn = _TOOLS.get(tool_name)
    if fn is None:
        return {"error": f"unknown tool: {tool_name}", "available": list(_TOOLS)}

    args = event if isinstance(event, dict) else {}
    result = fn(args)
    return result
