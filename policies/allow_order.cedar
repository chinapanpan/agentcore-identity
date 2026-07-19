// place_order: 仅 trader 组 (按 cognito:groups 授权 = 角色based)
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"defi___place_order",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("cognito:groups") && principal.getTag("cognito:groups") like "*trader*"
};
