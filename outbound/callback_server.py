"""
OAuth2 回调服务器 —— outbound 3LO 的 session 绑定终点 (跑在独立 EC2 上)。

在 Gateway 3LO 流程里的位置:
  用户点开 -32042 弹出的登录 URL
    → AgentCore Identity /authorize → 重定向到 Cognito Hosted UI 登录同意
    → Cognito 回 AgentCore callback → AgentCore 把浏览器重定向到【本服务】(defaultReturnUrl)
      并在 query 里带 session_id
    → 本服务调 CompleteResourceTokenAuth(session_uri, user_identifier) 完成 session 绑定
    → token 落入 AgentCore Token Vault, 用户重试 tools/call 即成功

安全要点 (session 绑定, 防 CSRF): 必须证明"发起授权的用户"与"完成同意的用户"是同一人。
做法: 发起 3LO 前, 先把发起用户的 access token 存进本服务(/userIdentifier/token);
回调到达时用它作为 user_identifier 完成绑定。

监听: 0.0.0.0:8443 (由 nginx/caddy 前置 TLS 终止, 对外 https://callback.chrisai.blog)。
路径:
  GET  /ping                 健康检查
  POST /userIdentifier/token 预存发起用户的 token (server-to-server)
  GET  /callback             接住 AgentCore 重定向, 完成 session 绑定
"""
import argparse
import logging
import os

import boto3
import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from bedrock_agentcore.services.identity import IdentityClient, UserTokenIdentifier

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ob-callback")

PORT = 8443
CALLBACK_PATH = "/callback"


class CallbackServer:
    def __init__(self, region: str, default_user_id: str = None):
        self.region = region
        self.identity = IdentityClient(region=region)
        self.dp = boto3.client("bedrock-agentcore", region_name=region)
        self.user_token_identifier = None       # 可选: 预存的发起用户 token
        self.default_user_id = default_user_id   # 稳定的 Cognito sub, 供无预存时兜底
        self.app = FastAPI(title="okx-ob outbound 3LO callback")
        self._routes()

    def _routes(self):
        @self.app.get("/ping")
        async def ping():
            return {"status": "success"}

        @self.app.post("/userIdentifier/token")
        async def store(user_token_identifier_value: UserTokenIdentifier):
            # 发起 3LO 前由调用方预存发起用户的 access token
            self.user_token_identifier = user_token_identifier_value
            logger.info("stored user token identifier for session binding")
            return {"status": "stored"}

        @self.app.get(CALLBACK_PATH)
        async def callback(session_id: str = None, sessionUri: str = None):
            # AgentCore 重定向到本服务时会带 session_id (兼容 sessionUri 命名)
            sid = session_id or sessionUri
            if not sid:
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    "missing session_id query parameter")
            # ★核心: 完成 session 绑定, token 入 Vault。
            # 优先用预存的发起用户 token; 否则用稳定的 userId (Cognito sub) 兜底 ——
            # 这样手动演示无需任何前置 prestore, 点完 URL 直接完成绑定。
            try:
                if self.user_token_identifier:
                    self.identity.complete_resource_token_auth(
                        session_uri=sid, user_identifier=self.user_token_identifier)
                    how = "userToken(prestored)"
                elif self.default_user_id:
                    self.dp.complete_resource_token_auth(
                        userIdentifier={"userId": self.default_user_id}, sessionUri=sid)
                    how = f"userId({self.default_user_id})"
                else:
                    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                                        "no user identifier: set DEFAULT_USER_ID or prestore a token")
            except Exception as e:
                logger.error(f"complete_resource_token_auth failed: {e!r}")
                return HTMLResponse(
                    "<html><body style='font-family:sans-serif;text-align:center;padding-top:12%'>"
                    "<h1 style='color:#dc2626'>❌ 授权未完成</h1>"
                    f"<p style='color:#6b7280'>session 绑定失败: {type(e).__name__}</p>"
                    "<p style='color:#9ca3af;font-size:13px'>登录 URL 可能已过期(约10分钟), 请回到应用重新发起。</p>"
                    "</body></html>", status_code=200)
            logger.info(f"completed resource token auth via {how}; session bound, token in vault")
            return HTMLResponse(
                "<html><body style='font-family:sans-serif;text-align:center;padding-top:15%'>"
                "<h1 style='color:#28a745'>✅ 授权完成</h1>"
                "<p>OAuth2 3LO 已完成, 可关闭本页, 回到你的应用重试调用。</p>"
                "</body></html>", status_code=200)

    def get_app(self):
        return self.app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-r", "--region", default="us-east-1")
    ap.add_argument("-p", "--port", type=int, default=PORT)
    ap.add_argument("--user-id", default=os.environ.get("DEFAULT_USER_ID"),
                    help="兜底 session 绑定用的 Cognito sub (无预存 token 时使用)")
    args = ap.parse_args()
    srv = CallbackServer(region=args.region, default_user_id=args.user_id)
    logger.info(f"starting callback server on 0.0.0.0:{args.port}{CALLBACK_PATH} "
                f"(default_user_id={'set' if args.user_id else 'none'})")
    uvicorn.run(srv.get_app(), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
