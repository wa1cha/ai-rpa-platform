"""跨路由共享的东西：模板引擎、登录依赖、防作弊头校验、故障注入。

这些是**横切关注点** —— 每个页面都要登录，每个 `/api/*` 都要校验头，
每个写操作都可能被注入故障。放在这里是为了让「有没有漏掉一处」
可以通过读一个文件回答，而不是翻遍所有路由。
"""

import logging
import random
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import ErpUser

logger = logging.getLogger("erp.deps")

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

#: 状态 → 中文。放全局是为了让页面上显示的是「待审核」而不是 PENDING_REVIEW ——
#: 老系统的界面不会把枚举名直接糊给用户看。**注意 RPA 不要按这个中文判断状态**，
#: 它该用 status 的原值或走列表页的 status 查询参数。
_STATUS_LABELS = {
    "DRAFT": "草稿",
    "PENDING_REVIEW": "待审核",
    "APPROVED": "已通过",
}
templates.env.globals["status_label"] = lambda status: _STATUS_LABELS.get(status, status)
#: 审核页可能被关掉（MOCK_ERP_ENABLE_REVIEW_PAGE=false），导航里就不该出现它的链接 ——
#: 否则点进去是 404，而这跟「RPA 该不该做审核」这件事毫无关系。
templates.env.globals["enable_review_page"] = settings.mock_erp_enable_review_page

#: 页面 JS 注入的防作弊请求头名（《模拟ERP设计》§5.1）。
_UI_HEADER = "X-ERP-UI"


DbDep = Annotated[AsyncSession, Depends(get_db)]


class RedirectToLogin(Exception):
    """未登录 / 会话过期。

    用异常而不是在每个路由开头 `if not session` 判断：只要漏判一处，
    那一页就是未鉴权页面 —— 而「漏判」这种事唯一可靠的防法是根本不给出漏判的机会。
    main.py 把它翻译成 303 → /login。
    """


async def current_operator(request: Request, session: DbDep) -> ErpUser:
    """当前登录的 ERP 操作员，没有就抛 `RedirectToLogin`。"""
    operator_id = request.session.get("operator_id")
    if operator_id is None:
        raise RedirectToLogin
    operator = await session.get(ErpUser, operator_id)
    if operator is None:
        # session 里的 id 在库里查不到了（账号被删）。按未登录处理，
        # 顺手清掉这个脏 session，免得每次请求都白查一次库。
        request.session.clear()
        raise RedirectToLogin
    return operator


OperatorDep = Annotated[ErpUser, Depends(current_operator)]


async def require_ui_token(request: Request) -> None:
    """校验页面 JS 带来的 `X-ERP-UI` 头。缺失或不符 → 403。

    这是「RPA 禁止调 /api/*」从君子协定变成技术约束的那一步：token 每次会话
    随机、只出现在页面的 `<meta>` 里。RPA 操作真实页面时页面的 JS 会自然带上，
    绕过页面直接用 requests 发请求则构造不出来。

    诚实地说：模拟 ERP 是我们自己写的，真想加后门随时能加。这条约束的价值在于
    **在数据层复现真实的权限边界**，而不是防住一个假想的攻击者（§5.1 原话）。
    """
    expected = request.session.get("ui_token")
    actual = request.headers.get(_UI_HEADER)
    if not expected or actual != expected:
        raise HTTPException(status_code=403, detail="缺少或无效的 X-ERP-UI 头")


async def inject_fault() -> None:
    """故障注入（《模拟ERP设计》§8.1）。**只被写操作调用**。

    只加在写操作上是刻意的：读操作失败只是让人再刷新一次，而写操作失败才会真的
    驱动「任务失败 → 自动重试 → 僵尸回收」这条链路 —— 那正是要演示的东西。
    默认概率 0；默认值下这里的第一个 if 就直接返回，等于不存在。
    """
    if settings.mock_erp_fail_rate <= 0:
        return
    if random.random() < settings.mock_erp_fail_rate:
        logger.warning("故障注入命中：返回「系统繁忙」")
        raise HTTPException(status_code=503, detail="系统繁忙，请稍后重试")
