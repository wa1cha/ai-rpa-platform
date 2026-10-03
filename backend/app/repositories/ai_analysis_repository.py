"""ai_analyses 表的数据访问 —— 分析结果列表（《API接口设计》§8.1）。

一个订单可以有多条分析（支持重新分析），所以这里除了列表，还提供
`get_latest()`：任务详情/订单详情要的「有效结果」永远是**最新一条**，
定义与 `OrderRepository.latest_analysis_id_subquery()` 保持一致。
"""

from dataclasses import dataclass

from sqlalchemy import Select, and_, select

from app.models.ai_analysis import AiAnalysis
from app.models.order import Order
from app.repositories.base import BaseRepository
from app.schemas.common import PageParams


@dataclass(slots=True)
class AiAnalysisListRow:
    """列表一行：分析本身 + 从订单 JOIN 来的两个展示字段。"""

    analysis: AiAnalysis
    order_no: str
    buyer_message: str | None


@dataclass(slots=True)
class AiAnalysisFilters:
    """分析列表的筛选条件 —— 对应《API接口设计》§8.1 的五个查询参数。"""

    risk_level: str | None = None
    priority: str | None = None
    need_contact: bool | None = None
    status: str | None = None
    order_no: str | None = None


class AiAnalysisRepository(BaseRepository[AiAnalysis]):
    model = AiAnalysis

    async def get_latest(self, order_id: int) -> AiAnalysis | None:
        """某订单最新一次分析。排序键与 `latest_analysis_id_subquery` 同一套。"""
        stmt = (
            select(AiAnalysis)
            .where(AiAnalysis.order_id == order_id)
            .order_by(AiAnalysis.created_at.desc(), AiAnalysis.id.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

    def _list_stmt(self, filters: AiAnalysisFilters) -> Select:
        stmt = select(AiAnalysis, Order.order_no, Order.buyer_message).join(
            Order, Order.id == AiAnalysis.order_id
        )

        conditions = []
        if filters.risk_level:
            conditions.append(AiAnalysis.risk_level == filters.risk_level)
        if filters.priority:
            conditions.append(AiAnalysis.priority == filters.priority)
        if filters.need_contact is not None:
            conditions.append(AiAnalysis.need_contact.is_(filters.need_contact))
        if filters.status:
            conditions.append(AiAnalysis.status == filters.status)
        if filters.order_no:
            conditions.append(Order.order_no == filters.order_no)
        if conditions:
            stmt = stmt.where(and_(*conditions))

        # 最新在前；加 id DESC 让同一秒内的多条有稳定顺序，翻页不重不漏。
        return stmt.order_by(AiAnalysis.created_at.desc(), AiAnalysis.id.desc())

    async def list_analyses(
        self, filters: AiAnalysisFilters, params: PageParams
    ) -> tuple[list[AiAnalysisListRow], int]:
        # AiAnalysis JOIN Order 是 N:1，行数不膨胀，基类默认计数方式对。
        rows, total = await self.paginate(self._list_stmt(filters), params)
        return [
            AiAnalysisListRow(
                analysis=row[0], order_no=row[1], buyer_message=row[2]
            )
            for row in rows
        ], total
