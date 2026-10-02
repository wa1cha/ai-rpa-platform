"""内部 API —— **RPA 禁止调用**（§5）。

目前只有一个库存实时校验，供给页面的 JS。返回**裸 JSON**，不套主服务那层
`{code,message,data}` 外壳：这是另一个系统，它有自己的风格。

这些路由跟别处最大的不同是挂了 `require_ui_token` 依赖 —— 它们要求请求带
`X-ERP-UI` 头，那个值只有页面的 JS 拿得到。于是「RPA 必须走界面」这条约束
从一句约定变成了一个 403。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.deps import DbDep, require_ui_token
from app.models import ErpInventory

router = APIRouter(prefix="/api", tags=["内部接口(RPA禁用)"])


class InventoryCheckRequest(BaseModel):
    sku: str = Field(..., description="商品编码")
    quantity: int = Field(1, ge=1, description="想下单的数量")


class InventoryCheckResponse(BaseModel):
    available: bool
    stock: int


@router.post(
    "/inventory/check",
    response_model=InventoryCheckResponse,
    dependencies=[Depends(require_ui_token)],
    summary="库存校验",
)
async def check_inventory(body: InventoryCheckRequest, session: DbDep) -> InventoryCheckResponse:
    """查得到且存量够 → available。SKU 不存在时 stock 记 0 而不是 404：

    页面上这是个「顺手校验」，SKU 打错字是常态，报 404 反而要求前端为
    「打错字」和「库存不够」写两套处理。
    """
    item = await session.scalar(select(ErpInventory).where(ErpInventory.sku == body.sku))
    stock = item.quantity if item is not None else 0
    return InventoryCheckResponse(available=item is not None and body.quantity <= stock, stock=stock)
