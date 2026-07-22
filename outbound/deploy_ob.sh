#!/usr/bin/env bash
# 【identity-outbound 分支】一键部署 outbound OAuth 3LO demo (us-east-1)。
# 拓扑: Runtime A(Agent) --MCPClient--> MCP Gateway(3LO) --user token--> Runtime B(受保护 MCP Server)
#       授权服务器 = 新建 Cognito(Hosted UI); 回调 = 独立 EC2 (callback.chrisai.blog)
# 产出写入 ob_ids.env。严格按依赖顺序执行 (见 docs 里的设计方案 §5)。
set -euo pipefail
cd "$(dirname "$0")"
export AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
PW="${DEMO_PASSWORD:-OkxDemo#2026}"
PY="${PY:-python3.12}"
ENVF=ob_ids.env
: > "$ENVF"; save(){ echo "$1=$2" >> "$ENVF"; }
save AWS_REGION "$AWS_REGION"; save ACCOUNT "$ACCOUNT"
# awscli 1.x: 关掉把 https:// 参数当 URL 抓取的行为
aws configure set cli_follow_urlparam false

echo "==> 1) Cognito 授权服务器 (Pool + Hosted UI 域名 + Resource Server + App Client(secret,code) + user)"
POOL_ID=$(aws cognito-idp create-user-pool --region "$AWS_REGION" --pool-name okx-ob-pool \
  --user-pool-tier ESSENTIALS --auto-verified-attributes email \
  --admin-create-user-config '{"AllowAdminCreateUserOnly":true}' --query 'UserPool.Id' --output text)
DOMAIN_PREFIX="okx-ob-$(echo $ACCOUNT | tail -c 7)"
aws cognito-idp create-user-pool-domain --region "$AWS_REGION" --domain "$DOMAIN_PREFIX" --user-pool-id "$POOL_ID" >/dev/null
aws cognito-idp create-resource-server --region "$AWS_REGION" --user-pool-id "$POOL_ID" \
  --identifier "okx-mcp" --name "OKX MCP Resource" \
  --scopes ScopeName=invoke,ScopeDescription="Invoke OKX MCP tools" >/dev/null
CJ=$(aws cognito-idp create-user-pool-client --region "$AWS_REGION" --user-pool-id "$POOL_ID" \
  --client-name okx-ob-client --generate-secret \
  --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --supported-identity-providers COGNITO --allowed-o-auth-flows code \
  --allowed-o-auth-scopes openid email "okx-mcp/invoke" --allowed-o-auth-flows-user-pool-client \
  --callback-urls "https://example.com/placeholder" --output json)
CLIENT_ID=$(echo "$CJ" | $PY -c "import sys,json;print(json.load(sys.stdin)['UserPoolClient']['ClientId'])")
CLIENT_SECRET=$(echo "$CJ" | $PY -c "import sys,json;print(json.load(sys.stdin)['UserPoolClient']['ClientSecret'])")
aws cognito-idp admin-create-user --region "$AWS_REGION" --user-pool-id "$POOL_ID" --username demo-user \
  --user-attributes Name=email,Value=demo@okx-demo.local Name=email_verified,Value=true --message-action SUPPRESS >/dev/null
aws cognito-idp admin-set-user-password --region "$AWS_REGION" --user-pool-id "$POOL_ID" --username demo-user --password "$PW" --permanent
DISCOVERY_URL="https://cognito-idp.${AWS_REGION}.amazonaws.com/${POOL_ID}/.well-known/openid-configuration"
save POOL_ID "$POOL_ID"; save DOMAIN_PREFIX "$DOMAIN_PREFIX"; save CLIENT_ID "$CLIENT_ID"
save CLIENT_SECRET "$CLIENT_SECRET"; save DEMO_USER demo-user; save DEMO_PASSWORD "$PW"
save DISCOVERY_URL "$DISCOVERY_URL"; save RESOURCE_SCOPE "okx-mcp/invoke"

echo "==> 2) AgentCore OAuth2 Credential Provider (CustomOauth2 + Cognito discoveryUrl) -> callbackUrl"
cat > /tmp/oauth-cfg.json <<EOF
{"customOauth2ProviderConfig":{"oauthDiscovery":{"discoveryUrl":"$DISCOVERY_URL"},"clientId":"$CLIENT_ID","clientSecret":"$CLIENT_SECRET"}}
EOF
RESP=$(aws bedrock-agentcore-control create-oauth2-credential-provider --region "$AWS_REGION" \
  --name okx-ob-cognito-provider --credential-provider-vendor CustomOauth2 \
  --oauth2-provider-config-input file:///tmp/oauth-cfg.json --output json)
PROVIDER_ARN=$(echo "$RESP" | $PY -c "import sys,json;print(json.load(sys.stdin)['credentialProviderArn'])")
CALLBACK_URL=$(echo "$RESP" | $PY -c "import sys,json;print(json.load(sys.stdin)['callbackUrl'])")
save PROVIDER_ARN "$PROVIDER_ARN"; save CALLBACK_URL "$CALLBACK_URL"

echo "==> 3) 回填 AgentCore callbackUrl 到 Cognito App Client"
aws cognito-idp update-user-pool-client --region "$AWS_REGION" --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID" \
  --client-name okx-ob-client --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --supported-identity-providers COGNITO --allowed-o-auth-flows code \
  --allowed-o-auth-scopes openid email "okx-mcp/invoke" --allowed-o-auth-flows-user-pool-client \
  --callback-urls "$CALLBACK_URL" >/dev/null

echo "==> 4) IAM 角色 (Gateway / Runtime)"
cat > /tmp/ob-trust.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},"Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT"}}}]}
EOF
GW_ROLE_ARN=$(aws iam create-role --role-name okx-ob-gateway-role --assume-role-policy-document file:///tmp/ob-trust.json --query 'Role.Arn' --output text 2>/dev/null || aws iam get-role --role-name okx-ob-gateway-role --query 'Role.Arn' --output text)
cat > /tmp/ob-gwp.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["bedrock-agentcore:GetResourceOauth2Token","bedrock-agentcore:GetWorkloadAccessToken","bedrock-agentcore:GetWorkloadAccessTokenForJWT","bedrock-agentcore:GetWorkloadAccessTokenForUserId","bedrock-agentcore:InvokeAgentRuntime"],"Resource":"*"},
 {"Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource":"arn:aws:secretsmanager:$AWS_REGION:$ACCOUNT:secret:bedrock-agentcore-identity*"}]}
EOF
aws iam put-role-policy --role-name okx-ob-gateway-role --policy-name okx-ob-gw --policy-document file:///tmp/ob-gwp.json
RT_ROLE_ARN=$(aws iam create-role --role-name okx-ob-runtime-role --assume-role-policy-document file:///tmp/ob-trust.json --query 'Role.Arn' --output text 2>/dev/null || aws iam get-role --role-name okx-ob-runtime-role --query 'Role.Arn' --output text)
cat > /tmp/ob-rtp.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["ecr:GetDownloadUrlForLayer","ecr:BatchGetImage","ecr:GetAuthorizationToken","ecr:BatchCheckLayerAvailability"],"Resource":"*"},
 {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"},
 {"Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"*"},
 {"Effect":"Allow","Action":["bedrock-agentcore:GetWorkloadAccessToken*"],"Resource":"*"}]}
EOF
aws iam put-role-policy --role-name okx-ob-runtime-role --policy-name okx-ob-rt --policy-document file:///tmp/ob-rtp.json
save GW_ROLE_ARN "$GW_ROLE_ARN"; save RT_ROLE_ARN "$RT_ROLE_ARN"; sleep 10

echo "==> 5) 构建并推送 arm64 镜像 (MCP server + agent)"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com" >/dev/null 2>&1
docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null 2>&1 || true
docker buildx create --name ob_arm64 --driver docker-container --use >/dev/null 2>&1 || docker buildx use ob_arm64
ECR_SERVER="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/okx-ob-mcpserver:latest"
ECR_AGENT="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/okx-ob-agent:latest"
aws ecr create-repository --region "$AWS_REGION" --repository-name okx-ob-mcpserver >/dev/null 2>&1 || true
aws ecr create-repository --region "$AWS_REGION" --repository-name okx-ob-agent >/dev/null 2>&1 || true
docker buildx build --platform linux/arm64 -f Dockerfile.server -t "$ECR_SERVER" --push .
docker buildx build --platform linux/arm64 -f Dockerfile.agent -t "$ECR_AGENT" --push .
save ECR_SERVER "$ECR_SERVER"; save ECR_AGENT "$ECR_AGENT"

rt_ready(){ for i in $(seq 1 40); do st=$(aws bedrock-agentcore-control get-agent-runtime --region "$AWS_REGION" --agent-runtime-id "$1" --query status --output text 2>/dev/null||echo P); [ "$st" = READY ] && return 0; sleep 8; done; }

echo "==> 6) Runtime B (受保护 MCP Server, serverProtocol=MCP, inbound JWT)"
cat > /tmp/ob-rtb-authz.json <<EOF
{"customJWTAuthorizer":{"discoveryUrl":"$DISCOVERY_URL","allowedClients":["$CLIENT_ID"]}}
EOF
RT_B_ARN=$(aws bedrock-agentcore-control create-agent-runtime --region "$AWS_REGION" --agent-runtime-name okx_ob_mcpserver \
  --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$ECR_SERVER\"}}" \
  --role-arn "$RT_ROLE_ARN" --network-configuration '{"networkMode":"PUBLIC"}' \
  --protocol-configuration '{"serverProtocol":"MCP"}' --authorizer-configuration file:///tmp/ob-rtb-authz.json \
  --query agentRuntimeArn --output text)
save RT_B_ARN "$RT_B_ARN"
ENC=$($PY -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$RT_B_ARN")
RT_B_MCP_URL="https://bedrock-agentcore.${AWS_REGION}.amazonaws.com/runtimes/${ENC}/invocations?qualifier=DEFAULT"
save RT_B_MCP_URL "$RT_B_MCP_URL"
rt_ready "$(echo $RT_B_ARN | sed 's/.*runtime\///')"

echo "==> 7) Gateway (MCP 2025-11-25, inbound JWT) + mcpServer target (OAUTH/AUTHORIZATION_CODE)"
RETURN_URL="${RETURN_URL:-https://callback.chrisai.blog/callback}"; save RETURN_URL "$RETURN_URL"
cat > /tmp/ob-gw-authz.json <<EOF
{"customJWTAuthorizer":{"discoveryUrl":"$DISCOVERY_URL","allowedClients":["$CLIENT_ID"]}}
EOF
cat > /tmp/ob-gw-proto.json <<'EOF'
{"mcp":{"supportedVersions":["2025-11-25"],"searchType":"SEMANTIC"}}
EOF
GW_ID=$(aws bedrock-agentcore-control create-gateway --region "$AWS_REGION" --name okx-ob-gateway \
  --role-arn "$GW_ROLE_ARN" --protocol-type MCP --protocol-configuration file:///tmp/ob-gw-proto.json \
  --authorizer-type CUSTOM_JWT --authorizer-configuration file:///tmp/ob-gw-authz.json --query gatewayId --output text)
GW_URL=$(aws bedrock-agentcore-control get-gateway --region "$AWS_REGION" --gateway-identifier "$GW_ID" --query gatewayUrl --output text)
save GW_ID "$GW_ID"; save GW_URL "$GW_URL"
for i in $(seq 1 30); do st=$(aws bedrock-agentcore-control get-gateway --region "$AWS_REGION" --gateway-identifier "$GW_ID" --query status --output text 2>/dev/null||true); [ "$st" = READY ] && break; sleep 6; done
$PY - > /tmp/ob-tools.json <<'PY'
import json
print(json.dumps({"tools":[
 {"name":"get_token_price","description":"查询指定加密货币的当前美元价格 (演示 mock 行情)。","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}},
 {"name":"calc_impermanent_loss","description":"根据价格变动比 r 计算流动性做市的无常损失百分比。","inputSchema":{"type":"object","properties":{"price_ratio":{"type":"number"}},"required":["price_ratio"]}},
 {"name":"place_order","description":"提交一笔下单 (纯演示 mock, 零副作用)。","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"side":{"type":"string"},"qty":{"type":"number"}},"required":["symbol","side","qty"]}},
],},ensure_ascii=False))
PY
$PY - "$RT_B_MCP_URL" <<'PY'
import json,sys
json.dump({"mcp":{"mcpServer":{"endpoint":sys.argv[1],"mcpToolSchema":{"inlinePayload":open("/tmp/ob-tools.json").read()},"listingMode":"DEFAULT"}}},open("/tmp/ob-targetcfg.json","w"),ensure_ascii=False)
PY
$PY - "$PROVIDER_ARN" "$RETURN_URL" <<'PY'
import json,sys
json.dump([{"credentialProviderType":"OAUTH","credentialProvider":{"oauthCredentialProvider":{"providerArn":sys.argv[1],"scopes":["openid","email","okx-mcp/invoke"],"grantType":"AUTHORIZATION_CODE","defaultReturnUrl":sys.argv[2]}}}],open("/tmp/ob-credcfg.json","w"))
PY
TGT=$(aws bedrock-agentcore-control create-gateway-target --region "$AWS_REGION" --gateway-identifier "$GW_ID" --name okxmcp \
  --target-configuration file:///tmp/ob-targetcfg.json --credential-provider-configurations file:///tmp/ob-credcfg.json --query targetId --output text)
save TARGET_ID "$TGT"
for i in $(seq 1 30); do st=$(aws bedrock-agentcore-control get-gateway-target --region "$AWS_REGION" --gateway-identifier "$GW_ID" --target-id "$TGT" --query status --output text 2>/dev/null||echo P); [ "$st" = READY ] && break; sleep 6; done

echo "==> 8) 注册回调 return URL 到 Gateway 的 workload identity"
WI=$(aws bedrock-agentcore-control list-workload-identities --region "$AWS_REGION" --query "workloadIdentities[?starts_with(name,'$GW_ID')].name | [0]" --output text 2>/dev/null || true)
[ -z "$WI" -o "$WI" = None ] && WI="$GW_ID"
aws bedrock-agentcore-control update-workload-identity --region "$AWS_REGION" --name "$WI" \
  --allowed-resource-oauth2-return-urls "$RETURN_URL" >/dev/null 2>&1 || echo "(注意: 若失败, 手动注册 return URL)"
save GW_WORKLOAD_IDENTITY "$WI"

echo "==> 9) Runtime A (Agent, HTTP, allowlist Authorization) 指向 Gateway"
cat > /tmp/ob-rta-authz.json <<EOF
{"customJWTAuthorizer":{"discoveryUrl":"$DISCOVERY_URL","allowedClients":["$CLIENT_ID"]}}
EOF
echo '{"requestHeaderAllowlist":["Authorization"]}' > /tmp/ob-rta-header.json
RT_A_ARN=$(aws bedrock-agentcore-control create-agent-runtime --region "$AWS_REGION" --agent-runtime-name okx_ob_agent \
  --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$ECR_AGENT\"}}" \
  --role-arn "$RT_ROLE_ARN" --network-configuration '{"networkMode":"PUBLIC"}' \
  --protocol-configuration '{"serverProtocol":"HTTP"}' --authorizer-configuration file:///tmp/ob-rta-authz.json \
  --request-header-configuration file:///tmp/ob-rta-header.json \
  --environment-variables "{\"GATEWAY_URL\":\"$GW_URL\",\"RETURN_URL\":\"$RETURN_URL\",\"AWS_REGION\":\"$AWS_REGION\"}" \
  --query agentRuntimeArn --output text)
save RT_A_ARN "$RT_A_ARN"
rt_ready "$(echo $RT_A_ARN | sed 's/.*runtime\///')"

echo ""
echo "✅ 控制面部署完成。标识写入 $ENVF"
echo "   下一步: ./setup_callback_ec2.sh   (起回调 EC2 + callback.chrisai.blog + 证书)"
echo "   然后:   验证见 OUTBOUND_RUNBOOK.md"
