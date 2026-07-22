#!/usr/bin/env bash
# 【identity-outbound】逆序清理 outbound 3LO demo 全部资源, 停止计费。
# ⚠️ 演示完成后再运行。从 ob_ids.env 读取标识。
set +e
cd "$(dirname "$0")"
[ -f ob_ids.env ] && { set -a; source ob_ids.env; set +a; }
export AWS_REGION="${AWS_REGION:-us-east-1}"
echo "== 1) Runtime A =="
[ -n "${RT_A_ARN:-}" ] && aws bedrock-agentcore-control delete-agent-runtime --region "$AWS_REGION" --agent-runtime-id "$(echo $RT_A_ARN|sed 's/.*runtime\///')"
echo "== 2) Gateway target + Gateway =="
[ -n "${GW_ID:-}" ] && [ -n "${TARGET_ID:-}" ] && aws bedrock-agentcore-control delete-gateway-target --region "$AWS_REGION" --gateway-identifier "$GW_ID" --target-id "$TARGET_ID"
sleep 5
[ -n "${GW_ID:-}" ] && aws bedrock-agentcore-control delete-gateway --region "$AWS_REGION" --gateway-identifier "$GW_ID"
echo "== 3) Runtime B =="
[ -n "${RT_B_ARN:-}" ] && aws bedrock-agentcore-control delete-agent-runtime --region "$AWS_REGION" --agent-runtime-id "$(echo $RT_B_ARN|sed 's/.*runtime\///')"
echo "== 4) OAuth2 Credential Provider =="
aws bedrock-agentcore-control delete-oauth2-credential-provider --region "$AWS_REGION" --name okx-ob-cognito-provider
echo "== 5) 回调 EC2 + EIP + Route53 + SG =="
[ -n "${CALLBACK_IID:-}" ] && aws ec2 terminate-instances --region "$AWS_REGION" --instance-ids "$CALLBACK_IID" >/dev/null && aws ec2 wait instance-terminated --region "$AWS_REGION" --instance-ids "$CALLBACK_IID"
[ -n "${CALLBACK_EIP_ALLOC:-}" ] && aws ec2 release-address --region "$AWS_REGION" --allocation-id "$CALLBACK_EIP_ALLOC"
if [ -n "${CALLBACK_EIP:-}" ]; then
  cat > /tmp/ob-r53-del.json <<R53
{"Changes":[{"Action":"DELETE","ResourceRecordSet":{"Name":"callback.chrisai.blog","Type":"A","TTL":60,"ResourceRecords":[{"Value":"$CALLBACK_EIP"}]}}]}
R53
  aws route53 change-resource-record-sets --hosted-zone-id Z0079812ZE2NQ3YHZNI --change-batch file:///tmp/ob-r53-del.json
fi
[ -n "${CALLBACK_SG:-}" ] && { sleep 5; aws ec2 delete-security-group --region "$AWS_REGION" --group-id "$CALLBACK_SG"; }
echo "== 6) Cognito (域名/Client/Pool) =="
[ -n "${DOMAIN_PREFIX:-}" ] && aws cognito-idp delete-user-pool-domain --region "$AWS_REGION" --domain "$DOMAIN_PREFIX" --user-pool-id "$POOL_ID"
[ -n "${POOL_ID:-}" ] && aws cognito-idp delete-user-pool --region "$AWS_REGION" --user-pool-id "$POOL_ID"
echo "== 7) ECR =="
aws ecr delete-repository --region "$AWS_REGION" --repository-name okx-ob-mcpserver --force
aws ecr delete-repository --region "$AWS_REGION" --repository-name okx-ob-agent --force
echo "== 8) IAM 角色 + 实例配置文件 =="
for r in okx-ob-gateway-role okx-ob-runtime-role okx-ob-callback-role; do
  for p in $(aws iam list-role-policies --role-name "$r" --query 'PolicyNames[]' --output text 2>/dev/null); do aws iam delete-role-policy --role-name "$r" --policy-name "$p"; done
  for a in $(aws iam list-attached-role-policies --role-name "$r" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do aws iam detach-role-policy --role-name "$r" --policy-arn "$a"; done
done
aws iam remove-role-from-instance-profile --instance-profile-name okx-ob-callback-profile --role-name okx-ob-callback-role 2>/dev/null
aws iam delete-instance-profile --instance-profile-name okx-ob-callback-profile 2>/dev/null
for r in okx-ob-gateway-role okx-ob-runtime-role okx-ob-callback-role; do aws iam delete-role --role-name "$r"; done
echo "✅ 清理完成 (Route53 zone 保留)。"
