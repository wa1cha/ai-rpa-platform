"""登录 / 登出 —— RPA 操作路径的第 ①② 步。

用服务端 session cookie，不用 JWT：这是一个「老旧的、同一个浏览器会话一直开着」
的 Web 系统。JWT 的无状态优势在这里没有价值，反而多一份密钥要管。
"""

import asyncio
import logging
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import settings
from app.deps import DbDep, templates
from app.models import ErpUser
from app.security import verify_password

logger = logging.getLogger("erp.auth")

router = APIRouter(tags=["页面"])

_LOGIN_ERROR = "用户名或密码错误"


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=303)


@router.get("/login")
async def login_page(request: Request):
    if request.session.get("operator_id") is not None:
        # 已登录就别再看登录页。RPA 识别「被踢回登录页 → 重登」时，
        # 若重登成功却还停在 /login，它会以为自己又掉线了。
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"operator": None})


@router.post("/login")
async def login_submit(
    request: Request,
    session: DbDep,
    username: str = Form(""),
    password: str = Form(""),
):
    operator = await session.scalar(select(ErpUser).where(ErpUser.username == username))
    # 用户不存在与密码错给同一句话：分开写等于告诉试探者「这个账号是存在的」。
    if operator is None or not verify_password(password, operator.password_hash):
        logger.info("登录失败：username=%s", username)
        return templates.TemplateResponse(
            request, "login.html", {"operator": None, "error": _LOGIN_ERROR}
        )

    # 防作弊 token 在登录成功时才发（§5.1）。它必须每次会话随机 ——
    # 固定值的话，绕过页面直接 requests 调用就能猜出来了。
    request.session["operator_id"] = operator.id
    request.session["ui_token"] = secrets.token_hex(16)

    # 登录后的延时（§9.1）。这一跳是 POST 出的重定向，上面那个只延时
    # 「HTML GET」的中间件覆盖不到，所以在这里显式等一次。
    await asyncio.sleep(settings.page_delay_seconds)
    logger.info("登录成功：%s", operator.username)
    return RedirectResponse(url="/dashboard", status_code=303)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
