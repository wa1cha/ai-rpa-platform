"""Repository 基类 —— 只放「每个仓储都会写一遍」的东西。

分层的边界：**repository 只跟数据库打交道，不抛业务异常、不做业务判断**。
它返回结果或 None，由 service 决定「查不到」意味着什么
（是 404，还是「正好，可以插入」）。这样仓储可以随便被复用。
"""

from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.mysql import Base
from app.schemas.common import Page, PageParams

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """子类只需声明 `model`。"""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, pk: Any) -> ModelT | None:
        return await self.session.get(self.model, pk)

    def add(self, obj: ModelT) -> ModelT:
        """挂到 session 上，**不提交** —— 提交时机由 service 决定，
        因为一个业务动作往往要写多张表，必须一起成功或一起失败。"""
        self.session.add(obj)
        return obj

    async def delete(self, obj: ModelT) -> None:
        await self.session.delete(obj)

    async def _count(self, stmt: Select) -> int:
        """把任意 SELECT 包一层数总数。

        注意：这里数的是**结果行数**，不是主表行数。带 JOIN 又有 1:N 关系时
        会把行数算多 —— 所以分页查询里如果需要按主表计数，调用方要自己
        传 `count_stmt`，不要依赖这个默认实现。
        """
        subquery = stmt.order_by(None).subquery()
        total = await self.session.scalar(select(func.count()).select_from(subquery))
        return int(total or 0)

    async def paginate(
        self,
        stmt: Select,
        params: PageParams,
        *,
        count_stmt: Select | None = None,
    ) -> tuple[Sequence[Any], int]:
        """执行分页查询，返回 `(rows, total)`。

        先数后查会有并发下 total 与数据轻微不一致的可能（期间有人插了一行），
        分页列表页对此无感，不值得为此加锁。
        """
        total = await self._count(count_stmt if count_stmt is not None else stmt)
        result = await self.session.execute(
            stmt.limit(params.page_size).offset(params.offset)
        )
        return result.all(), total

    @staticmethod
    def build_page(rows: Sequence[Any], total: int, params: PageParams, items: list) -> Page:
        """把已映射好的 items 包成统一的 Page 结构。"""
        return Page(items=items, total=total, page=params.page, page_size=params.page_size)
