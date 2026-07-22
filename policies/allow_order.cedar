// place_order: 仅 role_id 1003 (trader)
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"defi___place_order",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("role_id") && principal.getTag("role_id") == "1003"
};
