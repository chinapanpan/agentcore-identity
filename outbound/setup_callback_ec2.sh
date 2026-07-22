#!/usr/bin/env bash
# 起一台独立 EC2 作为 outbound 3LO 回调服务器: callback.chrisai.blog (公网 HTTPS)。
# - AL2023 arm64, 开 443
# - user-data: 装 caddy(自动 Let's Encrypt) + python 依赖 + 跑 callback_server.py:8443
# - Caddy 反代 https://callback.chrisai.blog -> 127.0.0.1:8443, 自动签发/续期证书
# - Route53: callback.chrisai.blog A 记录指向本机 EIP
# 依赖环境: ob_ids.env (AWS_REGION/ACCOUNT), 以及 Route53 zone Z0079812ZE2NQ3YHZNI
set -euo pipefail
cd "$(dirname "$0")"
set -a; source ob_ids.env; set +a
save(){ echo "$1=$2" >> ob_ids.env; }

AWS_REGION="${AWS_REGION:-us-east-1}"
DOMAIN="callback.chrisai.blog"
ZONE_ID="Z0079812ZE2NQ3YHZNI"
VPC=$(aws ec2 describe-vpcs --region "$AWS_REGION" --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNET=$(aws ec2 describe-subnets --region "$AWS_REGION" --filters Name=default-for-az,Values=true --query 'Subnets[0].SubnetId' --output text)
AMI=$(aws ec2 describe-images --region "$AWS_REGION" --owners amazon \
  --filters 'Name=name,Values=al2023-ami-2023*-arm64' 'Name=state,Values=available' \
  --query 'reverse(sort_by(Images,&CreationDate))[0].ImageId' --output text)

echo "==> 回调 EC2 IAM 角色 + 实例配置文件 (需 CompleteResourceTokenAuth + 读 provider secret)"
cat > /tmp/ob-ec2-trust.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
CB_ROLE_ARN=$(aws iam create-role --role-name okx-ob-callback-role \
  --assume-role-policy-document file:///tmp/ob-ec2-trust.json --query 'Role.Arn' --output text 2>/dev/null \
  || aws iam get-role --role-name okx-ob-callback-role --query 'Role.Arn' --output text)
cat > /tmp/ob-cb-policy.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["bedrock-agentcore:CompleteResourceTokenAuth","bedrock-agentcore:GetResourceOauth2Token","bedrock-agentcore:GetWorkloadAccessToken*"],"Resource":"*"},
 {"Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource":"arn:aws:secretsmanager:$AWS_REGION:$ACCOUNT:secret:bedrock-agentcore-identity*"}]}
EOF
aws iam put-role-policy --role-name okx-ob-callback-role --policy-name okx-ob-cb --policy-document file:///tmp/ob-cb-policy.json
aws iam attach-role-policy --role-name okx-ob-callback-role --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore 2>/dev/null || true
aws iam create-instance-profile --instance-profile-name okx-ob-callback-profile 2>/dev/null || true
aws iam add-role-to-instance-profile --instance-profile-name okx-ob-callback-profile --role-name okx-ob-callback-role 2>/dev/null || true
save CB_ROLE_ARN "$CB_ROLE_ARN"
sleep 10

echo "==> 安全组 (开 443 + 80 给 Let's Encrypt HTTP-01/重定向)"
SG=$(aws ec2 create-security-group --region "$AWS_REGION" --group-name okx-ob-callback-sg \
  --description "okx-ob callback https" --vpc-id "$VPC" --query GroupId --output text 2>/dev/null \
  || aws ec2 describe-security-groups --region "$AWS_REGION" --filters Name=group-name,Values=okx-ob-callback-sg --query 'SecurityGroups[0].GroupId' --output text)
for p in 80 443; do
  aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$SG" \
    --protocol tcp --port $p --cidr 0.0.0.0/0 >/dev/null 2>&1 || true
done
save CALLBACK_SG "$SG"

echo "==> user-data (bootstrap caddy + callback server)"
CB64=$(base64 -w0 callback_server.py)
cat > /tmp/ob-userdata.sh <<UD
#!/bin/bash
set -x
dnf install -y python3.12 python3.12-pip
python3.12 -m pip install fastapi uvicorn bedrock-agentcore
mkdir -p /opt/okx-ob
echo "$CB64" | base64 -d > /opt/okx-ob/callback_server.py

# callback server systemd
cat > /etc/systemd/system/okx-ob-callback.service <<SVC
[Unit]
Description=okx-ob outbound 3LO callback
After=network.target
[Service]
ExecStart=/usr/bin/python3.12 /opt/okx-ob/callback_server.py --region $AWS_REGION --port 8443
Restart=always
[Install]
WantedBy=multi-user.target
SVC
systemctl daemon-reload && systemctl enable --now okx-ob-callback

# Caddy (自动 HTTPS) —— AL2023 用二进制
dnf install -y 'dnf-command(copr)' || true
curl -fsSL https://github.com/caddyserver/caddy/releases/download/v2.8.4/caddy_2.8.4_linux_arm64.tar.gz -o /tmp/caddy.tgz
tar -xzf /tmp/caddy.tgz -C /usr/local/bin caddy
cat > /etc/caddy.Caddyfile <<CADDY
$DOMAIN {
    reverse_proxy 127.0.0.1:8443
}
CADDY
cat > /etc/systemd/system/caddy.service <<SVC
[Unit]
Description=Caddy
After=network.target
[Service]
ExecStart=/usr/local/bin/caddy run --config /etc/caddy.Caddyfile --adapter caddyfile
Restart=always
AmbientCapabilities=CAP_NET_BIND_SERVICE
[Install]
WantedBy=multi-user.target
SVC
systemctl daemon-reload && systemctl enable --now caddy
UD

echo "==> 启动 EC2"
IID=$(aws ec2 run-instances --region "$AWS_REGION" --image-id "$AMI" --instance-type t4g.small \
  --subnet-id "$SUBNET" --security-group-ids "$SG" \
  --iam-instance-profile Name=okx-ob-callback-profile \
  --user-data file:///tmp/ob-userdata.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=okx-ob-callback}]' \
  --query 'Instances[0].InstanceId' --output text)
save CALLBACK_IID "$IID"
echo "IID=$IID  等待 running..."
aws ec2 wait instance-running --region "$AWS_REGION" --instance-ids "$IID"

echo "==> 分配 EIP + 关联"
EIP_ALLOC=$(aws ec2 allocate-address --region "$AWS_REGION" --domain vpc --query AllocationId --output text)
aws ec2 associate-address --region "$AWS_REGION" --instance-id "$IID" --allocation-id "$EIP_ALLOC" >/dev/null
EIP=$(aws ec2 describe-addresses --region "$AWS_REGION" --allocation-ids "$EIP_ALLOC" --query 'Addresses[0].PublicIp' --output text)
save CALLBACK_EIP_ALLOC "$EIP_ALLOC"; save CALLBACK_EIP "$EIP"
echo "EIP=$EIP"

echo "==> Route53: $DOMAIN A -> $EIP"
cat > /tmp/ob-r53.json <<R53
{"Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"$DOMAIN","Type":"A","TTL":60,"ResourceRecords":[{"Value":"$EIP"}]}}]}
R53
aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" --change-batch file:///tmp/ob-r53.json \
  --query 'ChangeInfo.Status' --output text

echo ""
echo "✅ 回调 EC2 就绪中。域名 https://$DOMAIN/ping (等 1-3 分钟 caddy 签发证书 + DNS 生效)"
echo "   验证: curl -s https://$DOMAIN/ping"
