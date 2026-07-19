// calc_impermanent_loss: 仅 analyst 组或 trader 组 (按 cognito:groups 授权 = 角色based)
// 注: Cognito 的 cognito:groups claim 作为 tag 落地时是字符串形态,
//     用 like "*group*" 匹配组成员身份, 兼容单组/多组表示。
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"defi___calc_impermanent_loss",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("cognito:groups") &&
  (principal.getTag("cognito:groups") like "*analyst*" || principal.getTag("cognito:groups") like "*trader*")
};
