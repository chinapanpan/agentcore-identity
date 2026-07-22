#!/usr/bin/env bash
# 【cognito-test 分支】一键部署: Cognito(Essentials) + Pre-Token-Gen V2(注入 role_id) +
#   3 用户 + Lambda target/interceptor + 两个 Gateway(拦截器/Cedar) + Runtime(Strands MCPClient 透传)。
# 账号相关全部由环境/查询推导, 不硬编码。产出写入 cognito_ids_ct.env。
set -euo pipefail

export AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
PW="${DEMO_PASSWORD:-OkxDemo#2026}"
MODEL_ID="${MODEL_ID:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"
PY="${PY:-python3.12}"
ENVF="cognito_ids_ct.env"
: > "$ENVF"
save(){ echo "$1=$2" >> "$ENVF"; }
save AWS_REGION "$AWS_REGION"; save ACCOUNT "$ACCOUNT"

echo "==> 1) Lambda 执行角色 + Pre-Token-Gen Lambda (V2)"
echo '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' > /tmp/lt-ct.json
LAMBDA_ROLE_ARN=$(aws iam create-role --role-name okx-ct-lambda-role --assume-role-policy-document file:///tmp/lt-ct.json --query 'Role.Arn' --output text)
aws iam attach-role-policy --role-name okx-ct-lambda-role --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
sleep 8
zip -jq /tmp/pretoken.zip pretoken_lambda.py
PRETOKEN_ARN=$(aws lambda create-function --region "$AWS_REGION" --function-name okx-ct-pretoken \
  --runtime python3.12 --handler pretoken_lambda.lambda_handler --role "$LAMBDA_ROLE_ARN" \
  --timeout 10 --zip-file fileb:///tmp/pretoken.zip --query 'FunctionArn' --output text)
save LAMBDA_ROLE_ARN "$LAMBDA_ROLE_ARN"; save PRETOKEN_ARN "$PRETOKEN_ARN"

echo "==> 2) Cognito User Pool (ESSENTIALS) + custom:role_id + V2 触发器 + App Client + 3 用户"
POOL_ID=$(aws cognito-idp create-user-pool --region "$AWS_REGION" \
  --pool-name okx-ct-pool --user-pool-tier ESSENTIALS \
  --schema Name=role_id,AttributeDataType=String,Mutable=true,Required=false \
  --lambda-config "PreTokenGenerationConfig={LambdaArn=$PRETOKEN_ARN,LambdaVersion=V2_0}" \
  --query 'UserPool.Id' --output text)
aws lambda add-permission --region "$AWS_REGION" --function-name okx-ct-pretoken \
  --statement-id cognito-invoke --action lambda:InvokeFunction --principal cognito-idp.amazonaws.com \
  --source-arn "arn:aws:cognito-idp:$AWS_REGION:$ACCOUNT:userpool/$POOL_ID" >/dev/null
CLIENT_ID=$(aws cognito-idp create-user-pool-client --region "$AWS_REGION" \
  --user-pool-id "$POOL_ID" --client-name okx-ct-client --no-generate-secret \
  --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --query 'UserPoolClient.ClientId' --output text)
for pair in "viewer-user:1001" "analyst-user:1002" "trader-user:1003"; do
  u="${pair%%:*}"; r="${pair##*:}"
  aws cognito-idp admin-create-user --region "$AWS_REGION" --user-pool-id "$POOL_ID" \
    --username "$u" --user-attributes Name=custom:role_id,Value=$r --message-action SUPPRESS >/dev/null
  aws cognito-idp admin-set-user-password --region "$AWS_REGION" --user-pool-id "$POOL_ID" \
    --username "$u" --password "$PW" --permanent
done
DISCOVERY_URL="https://cognito-idp.${AWS_REGION}.amazonaws.com/${POOL_ID}/.well-known/openid-configuration"
save POOL_ID "$POOL_ID"; save CLIENT_ID "$CLIENT_ID"; save DISCOVERY_URL "$DISCOVERY_URL"

echo "==> 3) Gateway / Runtime IAM 角色"
cat > /tmp/gwt-ct.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},"Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT"}}}]}
EOF
GW_ROLE_ARN=$(aws iam create-role --role-name okx-ct-gateway-role --assume-role-policy-document file:///tmp/gwt-ct.json --query 'Role.Arn' --output text)
cat > /tmp/gwp-ct.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["lambda:InvokeFunction"],"Resource":"arn:aws:lambda:$AWS_REGION:$ACCOUNT:function:okx-ct-*"},
 {"Effect":"Allow","Action":["bedrock-agentcore:GetPolicyEngine","bedrock-agentcore:GetPolicy","bedrock-agentcore:ListPolicies","bedrock-agentcore:BatchGetPolicy","bedrock-agentcore:AuthorizeAction","bedrock-agentcore:PartiallyAuthorizeActions"],"Resource":"*"}]}
EOF
aws iam put-role-policy --role-name okx-ct-gateway-role --policy-name okx-ct-gw --policy-document file:///tmp/gwp-ct.json
RT_ROLE_ARN=$(aws iam create-role --role-name okx-ct-runtime-role --assume-role-policy-document file:///tmp/gwt-ct.json --query 'Role.Arn' --output text)
cat > /tmp/rtp-ct.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["ecr:GetDownloadUrlForLayer","ecr:BatchGetImage","ecr:GetAuthorizationToken","ecr:BatchCheckLayerAvailability"],"Resource":"*"},
 {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"},
 {"Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"*"},
 {"Effect":"Allow","Action":["bedrock-agentcore:GetWorkloadAccessToken*"],"Resource":"*"}]}
EOF
aws iam put-role-policy --role-name okx-ct-runtime-role --policy-name okx-ct-rt --policy-document file:///tmp/rtp-ct.json
save GW_ROLE_ARN "$GW_ROLE_ARN"; save RT_ROLE_ARN "$RT_ROLE_ARN"
sleep 10

echo "==> 4) Lambda target + interceptor"
zip -jq /tmp/target-ct.zip target_lambda.py
TARGET_FN_ARN=$(aws lambda create-function --region "$AWS_REGION" --function-name okx-ct-target \
  --runtime python3.12 --handler target_lambda.lambda_handler --role "$LAMBDA_ROLE_ARN" --timeout 30 \
  --zip-file fileb:///tmp/target-ct.zip --query 'FunctionArn' --output text)
zip -jq /tmp/interceptor-ct.zip interceptor_lambda.py
INTERCEPTOR_FN_ARN=$(aws lambda create-function --region "$AWS_REGION" --function-name okx-ct-interceptor \
  --runtime python3.12 --handler interceptor_lambda.lambda_handler --role "$LAMBDA_ROLE_ARN" --timeout 30 \
  --zip-file fileb:///tmp/interceptor-ct.zip --query 'FunctionArn' --output text)
for fn in okx-ct-target okx-ct-interceptor; do
  aws lambda add-permission --region "$AWS_REGION" --function-name "$fn" --statement-id "gw-$fn" \
    --action lambda:InvokeFunction --principal bedrock-agentcore.amazonaws.com --source-account "$ACCOUNT" >/dev/null 2>&1 || true
done
save TARGET_FN_ARN "$TARGET_FN_ARN"; save INTERCEPTOR_FN_ARN "$INTERCEPTOR_FN_ARN"

echo "==> 5) Gateway 配置文件"
echo "{\"customJWTAuthorizer\":{\"discoveryUrl\":\"$DISCOVERY_URL\",\"allowedClients\":[\"$CLIENT_ID\"]}}" > /tmp/authz-ct.json
echo "[{\"interceptor\":{\"lambda\":{\"arn\":\"$INTERCEPTOR_FN_ARN\"}},\"interceptionPoints\":[\"REQUEST\",\"RESPONSE\"],\"inputConfiguration\":{\"passRequestHeaders\":true}}]" > /tmp/interceptors-ct.json
echo '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' > /tmp/credcfg-ct.json
$PY - "$TARGET_FN_ARN" <<'PYEOF'
import json,sys; tools=json.load(open("tool_schema.json"))
json.dump({"mcp":{"lambda":{"lambdaArn":sys.argv[1],"toolSchema":{"inlinePayload":tools}}}}, open("/tmp/targetcfg-ct.json","w"))
PYEOF

wait_ready(){ for i in $(seq 1 30); do st=$(aws bedrock-agentcore-control get-gateway --region "$AWS_REGION" --gateway-identifier "$1" --query status --output text 2>/dev/null||true); [ "$st" = "READY" ] && return 0; sleep 6; done; }
wait_target(){ local tid; tid=$(aws bedrock-agentcore-control list-gateway-targets --region "$AWS_REGION" --gateway-identifier "$1" --query 'items[0].targetId' --output text); for i in $(seq 1 20); do st=$(aws bedrock-agentcore-control get-gateway-target --region "$AWS_REGION" --gateway-identifier "$1" --target-id "$tid" --query status --output text 2>/dev/null||true); [ "$st" = "READY" ] && return 0; sleep 6; done; }

echo "==> 6) Gateway A (拦截器) + target"
GW_A_ID=$(aws bedrock-agentcore-control create-gateway --region "$AWS_REGION" --name okx-ct-gw-interceptor \
  --role-arn "$GW_ROLE_ARN" --protocol-type MCP --authorizer-type CUSTOM_JWT \
  --authorizer-configuration file:///tmp/authz-ct.json --interceptor-configurations file:///tmp/interceptors-ct.json \
  --query gatewayId --output text)
GW_A_URL=$(aws bedrock-agentcore-control get-gateway --region "$AWS_REGION" --gateway-identifier "$GW_A_ID" --query gatewayUrl --output text)
wait_ready "$GW_A_ID"
aws bedrock-agentcore-control create-gateway-target --region "$AWS_REGION" --gateway-identifier "$GW_A_ID" \
  --name defi --target-configuration file:///tmp/targetcfg-ct.json --credential-provider-configurations file:///tmp/credcfg-ct.json >/dev/null
wait_target "$GW_A_ID"
save GW_A_ID "$GW_A_ID"; save GW_A_URL "$GW_A_URL"; save TARGET_NAME defi

echo "==> 7) Policy Engine + Gateway B (Cedar) + target + 3 条 role_id 策略"
PE_ARN=$(aws bedrock-agentcore-control create-policy-engine --region "$AWS_REGION" --name okx_ct_policy_engine \
  --description "Cedar role_id 授权" --query policyEngineArn --output text)
PE_ID=$(echo "$PE_ARN" | sed 's/.*policy-engine\///')
for i in $(seq 1 20); do st=$(aws bedrock-agentcore-control get-policy-engine --region "$AWS_REGION" --policy-engine-id "$PE_ID" --query status --output text); [ "$st" = "ACTIVE" ] && break; sleep 6; done
echo "{\"arn\":\"$PE_ARN\",\"mode\":\"ENFORCE\"}" > /tmp/peconfig-ct.json
GW_B_ID=$(aws bedrock-agentcore-control create-gateway --region "$AWS_REGION" --name okx-ct-gw-cedar \
  --role-arn "$GW_ROLE_ARN" --protocol-type MCP --authorizer-type CUSTOM_JWT \
  --authorizer-configuration file:///tmp/authz-ct.json --policy-engine-configuration file:///tmp/peconfig-ct.json \
  --query gatewayId --output text)
GW_B_URL=$(aws bedrock-agentcore-control get-gateway --region "$AWS_REGION" --gateway-identifier "$GW_B_ID" --query gatewayUrl --output text)
GW_B_ARN="arn:aws:bedrock-agentcore:$AWS_REGION:$ACCOUNT:gateway/$GW_B_ID"
wait_ready "$GW_B_ID"
# ★target 必须先建 (注册工具 action), Cedar 策略才能引用, 否则 unrecognized action
aws bedrock-agentcore-control create-gateway-target --region "$AWS_REGION" --gateway-identifier "$GW_B_ID" \
  --name defi --target-configuration file:///tmp/targetcfg-ct.json --credential-provider-configurations file:///tmp/credcfg-ct.json >/dev/null
wait_target "$GW_B_ID"
RES="AgentCore::Gateway::\"$GW_B_ARN\""
mkpol(){ $PY - "$2" <<'PYEOF'
import json,sys; json.dump({"cedar":{"statement":sys.argv[1]}},open("/tmp/def-ct.json","w"))
PYEOF
aws bedrock-agentcore-control create-policy --region "$AWS_REGION" --policy-engine-id "$PE_ID" --name "$1" --definition file:///tmp/def-ct.json >/dev/null; }
mkpol allow_price "permit(principal is AgentCore::OAuthUser, action == AgentCore::Action::\"defi___get_token_price\", resource == $RES) when { principal.hasTag(\"role_id\") };"
mkpol allow_il "permit(principal is AgentCore::OAuthUser, action == AgentCore::Action::\"defi___calc_impermanent_loss\", resource == $RES) when { principal.hasTag(\"role_id\") && (principal.getTag(\"role_id\") == \"1002\" || principal.getTag(\"role_id\") == \"1003\") };"
mkpol allow_order "permit(principal is AgentCore::OAuthUser, action == AgentCore::Action::\"defi___place_order\", resource == $RES) when { principal.hasTag(\"role_id\") && principal.getTag(\"role_id\") == \"1003\" };"
save PE_ID "$PE_ID"; save PE_ARN "$PE_ARN"; save GW_B_ID "$GW_B_ID"; save GW_B_URL "$GW_B_URL"; save GW_B_ARN "$GW_B_ARN"

echo "==> 8) 构建 arm64 镜像推 ECR + 创建 Runtime (指向 Gateway A)"
ECR_URI="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/okx-ct-agent:latest"
aws ecr create-repository --region "$AWS_REGION" --repository-name okx-ct-agent >/dev/null 2>&1 || true
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com" >/dev/null 2>&1
docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null 2>&1 || true
docker buildx create --name arm64b --driver docker-container --use >/dev/null 2>&1 || docker buildx use arm64b
docker buildx build --platform linux/arm64 -t "$ECR_URI" --push .
echo "{\"customJWTAuthorizer\":{\"discoveryUrl\":\"$DISCOVERY_URL\",\"allowedClients\":[\"$CLIENT_ID\"]}}" > /tmp/rt-authz-ct.json
echo '{"requestHeaderAllowlist":["Authorization"]}' > /tmp/rt-header-ct.json
RT_ARN=$(aws bedrock-agentcore-control create-agent-runtime --region "$AWS_REGION" --agent-runtime-name okx_ct_runtime \
  --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$ECR_URI\"}}" \
  --role-arn "$RT_ROLE_ARN" --network-configuration '{"networkMode":"PUBLIC"}' \
  --protocol-configuration '{"serverProtocol":"HTTP"}' \
  --authorizer-configuration file:///tmp/rt-authz-ct.json --request-header-configuration file:///tmp/rt-header-ct.json \
  --environment-variables "{\"GATEWAY_URL\":\"$GW_A_URL\",\"MODEL_ID\":\"$MODEL_ID\",\"AWS_REGION\":\"$AWS_REGION\"}" \
  --query agentRuntimeArn --output text)
save ECR_URI "$ECR_URI"; save RT_ARN "$RT_ARN"

echo "==> 完成。标识写入 $ENVF"
echo "   GW_A_URL(拦截器)=$GW_A_URL"
echo "   GW_B_URL(Cedar) =$GW_B_URL"
echo "   RT_ARN          =$RT_ARN"
echo "   测试: set -a; source $ENVF; set +a && python mcp_matrix_test_ct.py \"\$GW_A_URL\" interceptor"
