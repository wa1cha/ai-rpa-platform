"""订单查询业务逻辑 —— 见《API接口设计》§6.1、§6.2。

这个 service 只做两件事：调仓储、把仓储的返回值**翻译成接口契约的形状**。
翻译放在这里而不是仓储里，是因为「脱敏」「金额转字符串」都是**展示规则**，
换了接口（比如给 RPA 用的内部接口）就不该套用同一套规则。
"""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.repositories.order_repository import OrderListRow, OrderRepository, OrderFilters
from app.schemas.common import Page, PageParams
from app.schemas.order import (
    AiAnalysisBrief,
    OrderDetail,
    OrderListItem,
    ReviewLogItem,
    TaskBrief,
    amount_to_str,
    mask_phone,
)


class OrderService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.orders = OrderRepository(session)

    async def list_orders(
        self, filters: OrderFilters, params: PageParams
    ) -> Page[OrderListItem]:
        rows, total = await self.orders.list_orders(filters, params)
        items = [self._to_list_item(row) for row in rows]
        return Page(items=items, total=total, page=params.page, page_size=params.page_size)

    async def get_detail(self, order_id: int) -> OrderDetail:
        found = await self.orders.get_with_analysis_and_task(order_id)
        if found is None:
            raise NotFoundError("订单不存在")
        order, analysis, task = found

        logs: Sequence = await self.orders.list_review_logs(order_id)

        return OrderDetail(
            id=order.id,
            order_no=order.order_no,
            platform=order.platform,
            ordered_at=order.ordered_at,
            customer_name=order.customer_name,
            # 详情页给完整手机号：客服要靠它回拨，遮了就没用了。
            phone=order.phone,
            address=order.address,
            product_name=order.product_name,
            sku=order.sku,
            quantity=order.quantity,
            amount=amount_to_str(order.amount),
            buyer_message=order.buyer_message,
            seller_note=order.seller_note,
            status=order.status,
            imported_at=order.imported_at,
            latest_analysis=AiAnalysisBrief.model_validate(analysis) if analysis else None,
            task=TaskBrief.model_validate(task) if task else None,
            review_logs=[
                ReviewLogItem(
                    field_name=log.field_name,
                    original_value=log.original_value,
                    new_value=log.new_value,
                    reviewer=username,
                    reviewed_at=log.reviewed_at,
                )
                for log, username in logs
            ],
        )

    @staticmethod
    def _to_list_item(row: OrderListRow) -> OrderListItem:
        order = row.order
        return OrderListItem(
            id=order.id,
            order_no=order.order_no,
            platform=order.platform,
            ordered_at=order.ordered_at,
            customer_name=order.customer_name,
            phone=mask_phone(order.phone),
            product_name=order.product_name,
            sku=order.sku,
            quantity=order.quantity,
            amount=amount_to_str(order.amount),
            status=order.status,
            # AI 还没跑过时这三项自然是 None，前端显示「待分析」即可 ——
            # 用 None 而不是 "LOW"/"MEDIUM" 默认值，是为了不把「没分析」
            # 伪装成「分析了且风险低」。
            risk_level=row.risk_level,
            priority=row.priority,
            task_id=row.task_id,
            task_status=row.task_status,
        )
