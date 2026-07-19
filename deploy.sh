#!/usr/bin/env bash
# 一键部署 AgentCore Identity 全流程 Demo (us-east-1)。
# 账号相关全部由环境/查询推导, 不硬编码。产出写入 cognito_ids.env。
set -euo pipefail

export AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
PW="${DEMO_PASSWORD:-OkxDemo#2026}"
MODEL_ID="${MODEL_ID:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"
PY="${PY:-python3}"
ENVF="cognito_ids.env"
: > "$ENVF"
save(){ echo "export $1=$2" >> "$ENVF"; }
save AWS_REGION "$AWS_REGION"; save ACCOUNT "$ACCOUNT"

echo "==> 1) Cognito: User Pool + App Client + 3 组 + 3 用户"
POOL_ID=$(aws cognito-idp create-user-pool --region "$AWS_REGION" \
  --pool-name "okx-agentcore-identity-pool" \
  --policies '{"PasswordPolicy":{"MinimumLength":8,"RequireUppercase":true,"RequireLowercase":true,"RequireNumbers":true,"RequireSymbols":false}}' \
  --query 'UserPool.Id' --output text)
CLIENT_ID=$(aws cognito-idp create-user-pool-client --region "$AWS_REGION" \
  --user-pool-id "$POOL_ID" --client-name "okx-identity-client" --no-generate-secret \
  --explicit-auth-flows "ALLOW_USER_PASSWORD_AUTH" "ALLOW_REFRESH_TOKEN_AUTH" \
  --query 'UserPoolClient.ClientId' --output text)
for g in readonly analyst trader; do
  aws cognito-idp create-group --region "$AWS_REGION" --user-pool-id "$POOL_ID" --group-name "$g" >/dev/null
done
for pair in "readonly-user:readonly" "analyst-user:analyst" "trader-user:trader"; do
  u="${pair%%:*}"; g="${pair##*:}"
  aws cognito-idp admin-create-user --region "$AWS_REGION" --user-pool-id "$POOL_ID" \
    --username "$u" --message-action SUPPRESS \
    --user-attributes Name=email,Value="${u}@okx-demo.local" Name=email_verified,Value=true >/dev/null
  aws cognito-idp admin-set-user-password --region "$AWS_REGION" --user-pool-id "$POOL_ID" \
    --username "$u" --password "$PW" --permanent
  aws cognito-idp admin-add-user-to-group --region "$AWS_REGION" --user-pool-id "$POOL_ID" \
    --username "$u" --group-name "$g"
done
DISCOVERY_URL="https://cognito-idp.${AWS_REGION}.amazonaws.com/${POOL_ID}/.well-known/openid-configuration"
save POOL_ID "$POOL_ID"; save CLIENT_ID "$CLIENT_ID"; save DISCOVERY_URL "$DISCOVERY_URL"

echo "==> 2) IAM 角色 (Lambda / Gateway / Runtime)"
echo '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' > lambda-trust.json
LAMBDA_ROLE_ARN=$(aws iam create-role --role-name okx-identity-lambda-role --assume-role-policy-document file://lambda-trust.json --query 'Role.Arn' --output text)
aws iam attach-role-policy --role-name okx-identity-lambda-role --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
cat > gw-trust.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},"Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT"}}}]}
EOF
GW_ROLE_ARN=$(aws iam create-role --role-name okx-identity-gateway-role --assume-role-policy-document file://gw-trust.json --query 'Role.Arn' --output text)
cat > gw-policy.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Sid":"InvokeLambdas","Effect":"Allow","Action":["lambda:InvokeFunction"],"Resource":"arn:aws:lambda:$AWS_REGION:$ACCOUNT:function:okx-identity-*"},
 {"Sid":"PolicyEngine","Effect":"Allow","Action":["bedrock-agentcore:GetPolicyEngine","bedrock-agentcore:GetPolicy","bedrock-agentcore:ListPolicies","bedrock-agentcore:BatchGetPolicy","bedrock-agentcore:AuthorizeAction","bedrock-agentcore:PartiallyAuthorizeActions"],"Resource":"*"}]}
EOF
aws iam put-role-policy --role-name okx-identity-gateway-role --policy-name okx-gw-invoke --policy-document file://gw-policy.json
cat > rt-trust.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},"Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT"}}}]}
EOF
RT_ROLE_ARN=$(aws iam create-role --role-name okx-identity-runtime-role --assume-role-policy-document file://rt-trust.json --query 'Role.Arn' --output text)
cat > rt-policy.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Sid":"ECR","Effect":"Allow","Action":["ecr:GetDownloadUrlForLayer","ecr:BatchGetImage","ecr:GetAuthorizationToken","ecr:BatchCheckLayerAvailability"],"Resource":"*"},
 {"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"},
 {"Sid":"Bedrock","Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"*"},
 {"Sid":"Workload","Effect":"Allow","Action":["bedrock-agentcore:GetWorkloadAccessToken*"],"Resource":"*"}]}
EOF
aws iam put-role-policy --role-name okx-identity-runtime-role --policy-name okx-rt-policy --policy-document file://rt-policy.json
save LAMBDA_ROLE_ARN "$LAMBDA_ROLE_ARN"; save GW_ROLE_ARN "$GW_ROLE_ARN"; save RT_ROLE_ARN "$RT_ROLE_ARN"
sleep 10

echo "==> 3) 部署 Lambda (target + interceptor)"
zip -q target_lambda.zip target_lambda.py
TARGET_FN_ARN=$(aws lambda create-function --region "$AWS_REGION" --function-name okx-identity-target \
  --runtime python3.12 --handler target_lambda.lambda_handler --role "$LAMBDA_ROLE_ARN" --timeout 30 \
  --zip-file fileb://target_lambda.zip --query 'FunctionArn' --output text)
zip -q interceptor_lambda.zip interceptor_lambda.py
INTERCEPTOR_FN_ARN=$(aws lambda create-function --region "$AWS_REGION" --function-name okx-identity-interceptor \
  --runtime python3.12 --handler interceptor_lambda.lambda_handler --role "$LAMBDA_ROLE_ARN" --timeout 30 \
  --zip-file fileb://interceptor_lambda.zip --query 'FunctionArn' --output text)
for fn in okx-identity-target okx-identity-interceptor; do
  aws lambda add-permission --region "$AWS_REGION" --function-name "$fn" --statement-id "gw-invoke-$fn" \
    --action lambda:InvokeFunction --principal bedrock-agentcore.amazonaws.com --source-account "$ACCOUNT" >/dev/null 2>&1 || true
done
save TARGET_FN_ARN "$TARGET_FN_ARN"; save INTERCEPTOR_FN_ARN "$INTERCEPTOR_FN_ARN"

echo "==> 4) 配置文件 (authorizer / target / interceptor / cred)"
echo "{\"customJWTAuthorizer\":{\"discoveryUrl\":\"$DISCOVERY_URL\",\"allowedClients\":[\"$CLIENT_ID\"]}}" > authz.json
echo "[{\"interceptor\":{\"lambda\":{\"arn\":\"$INTERCEPTOR_FN_ARN\"}},\"interceptionPoints\":[\"REQUEST\",\"RESPONSE\"],\"inputConfiguration\":{\"passRequestHeaders\":true}}]" > interceptors.json
echo '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' > credcfg.json
$PY - "$TARGET_FN_ARN" <<'PYEOF'
import json,sys
tools=json.load(open("tool_schema.json"))
json.dump({"mcp":{"lambda":{"lambdaArn":sys.argv[1],"toolSchema":{"inlinePayload":tools}}}}, open("targetcfg.json","w"))
PYEOF

wait_ready(){ # $1=get-cmd-args...  轮询直到 READY
  local id="$1"; shift
  for i in $(seq 1 30); do
    local st; st=$("$@" --query status --output text 2>/dev/null || true)
    [ "$st" = "READY" ] && return 0
    sleep 8
  done; return 1
}

echo "==> 5) Gateway A (拦截器) + target"
GW_A_JSON=$(aws bedrock-agentcore-control create-gateway --region "$AWS_REGION" \
  --name "okx-identity-gw-interceptor" --role-arn "$GW_ROLE_ARN" --protocol-type MCP \
  --authorizer-type CUSTOM_JWT --authorizer-configuration file://authz.json \
  --interceptor-configurations file://interceptors.json \
  --query '{id:gatewayId,url:gatewayUrl}' --output json)
GW_A_ID=$(echo "$GW_A_JSON" | $PY -c "import sys,json;print(json.load(sys.stdin)['id'])")
GW_A_URL=$(echo "$GW_A_JSON" | $PY -c "import sys,json;print(json.load(sys.stdin)['url'])")
wait_ready x aws bedrock-agentcore-control get-gateway --region "$AWS_REGION" --gateway-identifier "$GW_A_ID"
aws bedrock-agentcore-control create-gateway-target --region "$AWS_REGION" --gateway-identifier "$GW_A_ID" \
  --name "defi" --target-configuration file://targetcfg.json --credential-provider-configurations file://credcfg.json >/dev/null
save GW_A_ID "$GW_A_ID"; save GW_A_URL "$GW_A_URL"; save TARGET_NAME defi

echo "==> 6) Policy Engine + 3 条 Cedar 策略 + Gateway B (非拦截器) + target"
PE_JSON=$(aws bedrock-agentcore-control create-policy-engine --region "$AWS_REGION" \
  --name "okx_identity_policy_engine" --description "Cedar 授权 DeFi 工具" \
  --query '{id:policyEngineId,arn:policyEngineArn}' --output json)
PE_ID=$(echo "$PE_JSON" | $PY -c "import sys,json;print(json.load(sys.stdin)['id'])")
PE_ARN=$(echo "$PE_JSON" | $PY -c "import sys,json;print(json.load(sys.stdin)['arn'])")
for i in $(seq 1 20); do st=$(aws bedrock-agentcore-control get-policy-engine --region "$AWS_REGION" --policy-engine-id "$PE_ID" --query status --output text); [ "$st" = "ACTIVE" ] && break; sleep 8; done
echo "{\"arn\":\"$PE_ARN\",\"mode\":\"ENFORCE\"}" > peconfig.json
GW_B_JSON=$(aws bedrock-agentcore-control create-gateway --region "$AWS_REGION" \
  --name "okx-identity-gw-cedar" --role-arn "$GW_ROLE_ARN" --protocol-type MCP \
  --authorizer-type CUSTOM_JWT --authorizer-configuration file://authz.json \
  --policy-engine-configuration file://peconfig.json \
  --query '{id:gatewayId,url:gatewayUrl}' --output json)
GW_B_ID=$(echo "$GW_B_JSON" | $PY -c "import sys,json;print(json.load(sys.stdin)['id'])")
GW_B_URL=$(echo "$GW_B_JSON" | $PY -c "import sys,json;print(json.load(sys.stdin)['url'])")
GW_B_ARN="arn:aws:bedrock-agentcore:$AWS_REGION:$ACCOUNT:gateway/$GW_B_ID"
RES="AgentCore::Gateway::\"$GW_B_ARN\""
mkpol(){ $PY - "$2" <<'PYEOF'
import json,sys; json.dump({"cedar":{"statement":sys.argv[1]}},open("def.json","w"))
PYEOF
aws bedrock-agentcore-control create-policy --region "$AWS_REGION" --policy-engine-id "$PE_ID" --name "$1" --definition file://def.json >/dev/null; }
# 按 cognito:groups 授权 (角色based, 与拦截器一致)。groups tag 为字符串形态, 用 like 匹配组成员身份。
mkpol allow_price "permit(principal is AgentCore::OAuthUser, action == AgentCore::Action::\"defi___get_token_price\", resource == $RES) when { principal.hasTag(\"cognito:groups\") };"
mkpol allow_il "permit(principal is AgentCore::OAuthUser, action == AgentCore::Action::\"defi___calc_impermanent_loss\", resource == $RES) when { principal.hasTag(\"cognito:groups\") && (principal.getTag(\"cognito:groups\") like \"*analyst*\" || principal.getTag(\"cognito:groups\") like \"*trader*\") };"
mkpol allow_order "permit(principal is AgentCore::OAuthUser, action == AgentCore::Action::\"defi___place_order\", resource == $RES) when { principal.hasTag(\"cognito:groups\") && principal.getTag(\"cognito:groups\") like \"*trader*\" };"
wait_ready x aws bedrock-agentcore-control get-gateway --region "$AWS_REGION" --gateway-identifier "$GW_B_ID"
aws bedrock-agentcore-control create-gateway-target --region "$AWS_REGION" --gateway-identifier "$GW_B_ID" \
  --name "defi" --target-configuration file://targetcfg.json --credential-provider-configurations file://credcfg.json >/dev/null
save PE_ID "$PE_ID"; save PE_ARN "$PE_ARN"; save GW_B_ID "$GW_B_ID"; save GW_B_URL "$GW_B_URL"

echo "==> 7) 构建 arm64 镜像推 ECR + 创建 Runtime"
ECR_REPO="okx-identity-agent"
ECR_URI="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:latest"
aws ecr create-repository --region "$AWS_REGION" --repository-name "$ECR_REPO" >/dev/null 2>&1 || true
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com" >/dev/null 2>&1
docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null 2>&1 || true
docker buildx create --name arm64b --driver docker-container --use >/dev/null 2>&1 || docker buildx use arm64b
docker buildx build --platform linux/arm64 -t "$ECR_URI" --push .
echo "{\"customJWTAuthorizer\":{\"discoveryUrl\":\"$DISCOVERY_URL\",\"allowedClients\":[\"$CLIENT_ID\"]}}" > rt-authz.json
echo '{"requestHeaderAllowlist":["Authorization"]}' > rt-header.json
RT_ARN=$(aws bedrock-agentcore-control create-agent-runtime --region "$AWS_REGION" \
  --agent-runtime-name "okx_identity_runtime" \
  --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$ECR_URI\"}}" \
  --role-arn "$RT_ROLE_ARN" --network-configuration '{"networkMode":"PUBLIC"}' \
  --protocol-configuration '{"serverProtocol":"HTTP"}' \
  --authorizer-configuration file://rt-authz.json --request-header-configuration file://rt-header.json \
  --environment-variables "{\"GATEWAY_URL\":\"$GW_A_URL\",\"MODEL_ID\":\"$MODEL_ID\",\"AWS_REGION\":\"$AWS_REGION\"}" \
  --query 'agentRuntimeArn' --output text)
save ECR_URI "$ECR_URI"; save RT_ARN "$RT_ARN"

echo "==> 完成。标识已写入 $ENVF"
echo "   GW_A_URL(拦截器)=$GW_A_URL"
echo "   GW_B_URL(Cedar) =$GW_B_URL"
echo "   RT_ARN          =$RT_ARN"
echo "   测试: source $ENVF && python mcp_matrix_test.py \"\$GW_A_URL\" interceptor"
