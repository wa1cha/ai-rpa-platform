"""审核列表 —— PENDING_REVIEW → APPROVED。

**这一步不由 RPA 完成**（§4.7）。RPA 的职责到「提交审核」为止，审核是 ERP 侧
人工的事。保留这一页是为了让 ERP 的状态机能走完，后台看板才有完整数据。
换句话说：它是「RPA 的职责边界在哪」的一个可见证据。
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.deps import DbDep, OperatorDep, inject_fault, templates
from app.models import ErpOrder, ErpOrderStatus

logger = logging.getLogger("erp.review")

router = APIRouter(tags=["页面"])


@router.get("/review")
async def review_list(request: Request, operator: OperatorDep, session: DbDep):
    orders = (
        await session.scalars(
            select(ErpOrder)
            .where(ErpOrder.status == ErpOrderStatus.PENDING_REVIEW.value)
            .order_by(ErpOrder.id)
        )
    ).all()
    return templates.TemplateResponse(
        request, "review.html", {"operator": operator, "orders": orders}
    )


@router.post("/review/{erp_order_no}/approve")
async def approve(erp_order_no: str, operator: OperatorDep, session: DbDep):
    """审核通过：PENDING_REVIEW → APPROVED。写操作，故障注入生效。"""
    await inject_fault()

    order = await session.scalar(select(ErpOrder).where(ErpOrder.erp_order_no == erp_order_no))
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")

    # 只接受 PENDING_REVIEW → APPROVED。重复点「通过」不该把已通过的订单
    # 再改一次状态 —— 状态机只允许这一条边。
    if order.status == ErpOrderStatus.PENDING_REVIEW.value:
        order.status = ErpOrderStatus.APPROVED.value
        await session.commit()
        logger.info("订单 %s 审核通过", erp_order_no)

    return RedirectResponse(url="/review", status_code=303)
