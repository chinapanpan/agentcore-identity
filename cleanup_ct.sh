#!/usr/bin/env bash
# 【cognito-test 分支】一键清理全部资源。按依赖顺序删除, 尽力而为(不因单点失败中止)。
# 依赖 cognito_ids_ct.env 里的标识; 缺失的项跳过。
set -uo pipefail
export AWS_REGION="${AWS_REGION:-us-east-1}"
[ -f cognito_ids_ct.env ] && source cognito_ids_ct.env || true
ACCOUNT="${ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
R="--region $AWS_REGION"
del(){ echo "  - $*"; eval "$@" >/dev/null 2>&1 || echo "    (跳过/已不存在)"; }

echo "==> 1) 删除 Runtime"
if [ -n "${RT_ARN:-}" ]; then
  del aws bedrock-agentcore-control delete-agent-runtime $R --agent-runtime-id "${RT_ARN##*/}"
fi

echo "==> 2) 删除两个 Gateway 的 target, 再删 Gateway"
for GW in "${GW_A_ID:-}" "${GW_B_ID:-}"; do
  [ -z "$GW" ] && continue
  for TID in $(aws bedrock-agentcore-control list-gateway-targets $R --gateway-identifier "$GW" --query 'items[].targetId' --output text 2>/dev/null); do
    del aws bedrock-agentcore-control delete-gateway-target $R --gateway-identifier "$GW" --target-id "$TID"
  done
  sleep 3
  del aws bedrock-agentcore-control delete-gateway $R --gateway-identifier "$GW"
done

echo "==> 3) 删除 Cedar 策略 + Policy Engine"
if [ -n "${PE_ID:-}" ]; then
  for PID in $(aws bedrock-agentcore-control list-policies $R --policy-engine-id "$PE_ID" --query 'policies[].policyId' --output text 2>/dev/null); do
    del aws bedrock-agentcore-control delete-policy $R --policy-engine-id "$PE_ID" --policy-id "$PID"
  done
  sleep 3
  del aws bedrock-agentcore-control delete-policy-engine $R --policy-engine-id "$PE_ID"
fi

echo "==> 4) 删除 Lambda (含 pretoken) + CloudWatch 日志组"
for fn in okx-ct-target okx-ct-interceptor okx-ct-pretoken; do
  del aws lambda delete-function $R --function-name "$fn"
  del aws logs delete-log-group $R --log-group-name "/aws/lambda/$fn"
done

echo "==> 5) 删除 Cognito (先用户/client, 再 pool)"
if [ -n "${POOL_ID:-}" ]; then
  for u in viewer-user analyst-user trader-user; do
    del aws cognito-idp admin-delete-user $R --user-pool-id "$POOL_ID" --username "$u"
  done
  [ -n "${CLIENT_ID:-}" ] && del aws cognito-idp delete-user-pool-client $R --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID"
  del aws cognito-idp delete-user-pool $R --user-pool-id "$POOL_ID"
fi

echo "==> 6) 删除 ECR 仓库 (含镜像)"
del aws ecr delete-repository $R --repository-name okx-ct-agent --force

echo "==> 7) 删除 Runtime 日志组"
[ -n "${RT_ARN:-}" ] && del aws logs delete-log-group $R --log-group-name "/aws/bedrock-agentcore/runtimes/${RT_ARN##*/}-DEFAULT"

echo "==> 8) 删除 IAM 角色 (先删内联策略/解绑托管策略)"
del aws iam delete-role-policy --role-name okx-ct-gateway-role --policy-name okx-ct-gw
del aws iam delete-role --role-name okx-ct-gateway-role
del aws iam delete-role-policy --role-name okx-ct-runtime-role --policy-name okx-ct-rt
del aws iam delete-role --role-name okx-ct-runtime-role
del aws iam detach-role-policy --role-name okx-ct-lambda-role --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
del aws iam delete-role --role-name okx-ct-lambda-role

echo "==> 清理完成。建议手动复核: Gateway / Runtime / Cognito / ECR / Policy Engine 是否已空。"
