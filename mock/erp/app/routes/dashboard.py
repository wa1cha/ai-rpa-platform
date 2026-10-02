"""工作台 —— RPA 操作路径的第 ② 步的落点。

这一页本身没什么业务，它的价值在于**入口是点出来的，不是敲 URL 出来的**：
RPA 从 /login 跳到 /dashboard，再点「订单管理」进列表。真实 RPA 就该这么写，
而不是背下一堆 URL 直接 GET —— 那样项目就退化成 HTTP 客户端了（§4.3）。
"""

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from app.deps import DbDep, OperatorDep, templates
from app.models import ErpOrder, ErpOrderStatus

router = APIRouter(tags=["页面"])


@router.get("/dashboard")
async def dashboard(request: Request, operator: OperatorDep, session: DbDep):
    async def _count(status: str | None = None) -> int:
        stmt = select(func.count()).select_from(ErpOrder)
        if status is not None:
            stmt = stmt.where(ErpOrder.status == status)
        return await session.scalar(stmt) or 0

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "operator": operator,
            "total": await _count(),
            "pending": await _count(ErpOrderStatus.PENDING_REVIEW.value),
            "draft": await _count(ErpOrderStatus.DRAFT.value),
        },
    )
