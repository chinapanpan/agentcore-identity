"""内部工具: 用无头浏览器模拟真人完成 Cognito 登录同意, 自动验证 3LO retry-succeeds 闭环。
demo 现场是真人点 URL; 本脚本仅用于自动化验证 (Cognito hosted UI 有隐藏+可见两套表单, 需选可见的)。"""
import os, sys
from playwright.sync_api import sync_playwright

URL = sys.argv[1]
USER = os.environ["DEMO_USER"]
PW = os.environ["DEMO_PASSWORD"]

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    pg = b.new_page()
    pg.goto(URL, wait_until="domcontentloaded", timeout=60000)
    # 选可见的用户名输入框 (Cognito 有隐藏+可见两套)
    pg.locator('input[name="username"]:visible').first.fill(USER)
    pg.locator('input[name="password"]:visible').first.fill(PW)
    pg.locator('input[name="signInSubmitButton"]:visible, button[name="signInSubmitButton"]:visible').first.click()
    try:
        pg.wait_for_url("**callback.chrisai.blog**", timeout=45000)
    except Exception:
        pg.wait_for_timeout(8000)
    print("FINAL_URL:", pg.url)
    print("BODY:", (pg.content()[:400]).replace("\n", " "))
    b.close()
