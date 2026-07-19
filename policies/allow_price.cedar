// get_token_price: 允许所有已认证用户 (只要携带有效 JWT, 即有 cognito:groups tag)
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"defi___get_token_price",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("cognito:groups")
};
