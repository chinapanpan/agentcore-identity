"""
Cognito Pre Token Generation Lambda 触发器 (V2_0 事件) — 本 Demo 的教学核心。

作用: 在 Cognito 签发令牌【之前】被调用, 把用户的 `custom:role_id` 属性
      注入成 access token 里一个干净的自定义 claim `role_id`。

为什么需要它:
  - V1_0 (Basic 功能层) 只能定制 ID token;
  - 要往 **access token** 注入自定义 claim, User Pool 必须是 Essentials/Plus 功能层,
    且触发器事件版本为 **V2_0** (用户身份) 或 V3_0 (含 M2M)。
  - 我们把权限判定放在 access token, 是因为 access token 才是"授权令牌"
    (OAuth 语义: access token 用于访问受保护资源 = 调 Gateway 工具)。

事件结构 (V2_0):
  event.request.userAttributes["custom:role_id"]  ← 用户注册时写入的自定义属性
  event.response.claimsAndScopeOverrideDetails.accessTokenGeneration.claimsToAddOrOverride
      ← 我们在这里塞入 role_id, Cognito 会把它写进 access token

教学点: "自定义属性(custom:role_id, 存在用户目录)" 与 "自定义 claim(role_id, 出现在 JWT)"
        是两个不同的东西, 由本触发器把前者映射成后者。
"""


def lambda_handler(event, context):
    attrs = (event.get("request") or {}).get("userAttributes") or {}
    role_id = attrs.get("custom:role_id")

    # 构造 V2 覆盖结构: 往 access token 加 role_id claim (同时也写进 id token 便于对照演示)
    add_claims = {}
    if role_id is not None:
        add_claims["role_id"] = str(role_id)

    event["response"] = {
        "claimsAndScopeOverrideDetails": {
            "accessTokenGeneration": {
                "claimsToAddOrOverride": add_claims
            },
            "idTokenGeneration": {
                "claimsToAddOrOverride": add_claims
            }
        }
    }
    return event
