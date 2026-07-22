// get_token_price: 允许任意持有 role_id claim 的已认证用户 (role_id 由 Pre-Token-Gen V2 注入 access token)
// 注: role_id 是单值字符串, 落到 Cedar tag 也是字符串, 可直接用 == 精确匹配
//     (与多值的 cognito:groups 需 like 匹配不同)。
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"defi___get_token_price",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("role_id")
};
