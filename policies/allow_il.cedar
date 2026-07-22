// calc_impermanent_loss: 仅 role_id 1002 (analyst) 或 1003 (trader)
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"defi___calc_impermanent_loss",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("role_id") &&
  (principal.getTag("role_id") == "1002" || principal.getTag("role_id") == "1003")
};
