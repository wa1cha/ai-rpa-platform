"""库存查询 —— 一个**纯只读**页面（§4.8）。

两个用处：让「库存不足」这个异常可查、可解释；给 RPA 一个「读表格 → 判断 →
决定是否继续」的练习。它也是三者里唯一没有写操作、因此永远不受故障注入影响的页面。
"""

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.deps import DbDep, OperatorDep, templates
from app.models import ErpInventory

router = APIRouter(tags=["页面"])


@router.get("/inventory")
async def inventory(request: Request, operator: OperatorDep, session: DbDep):
    items = (await session.scalars(select(ErpInventory).order_by(ErpInventory.sku))).all()
    return templates.TemplateResponse(
        request, "inventory.html", {"operator": operator, "items": items}
    )
