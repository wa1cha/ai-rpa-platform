"""customer_blacklist 表的数据访问。

规则引擎判定「客户在黑名单」时，只需要一套手机号 —— 所以这里刻意只提供一个
`list_active_phones()`，一次取全量。这样一轮分析（可能几十上百单）只查一次库，
而不是每单一次；把它做成「查单个手机号在不在名单」会让调用方在循环里查库。
"""

from sqlalchemy import select

from app.models.customer_blacklist import CustomerBlacklist
from app.repositories.base import BaseRepository


class CustomerBlacklistRepository(BaseRepository[CustomerBlacklist]):
    model = CustomerBlacklist

    async def list_active_phones(self) -> set[str]:
        """返回所有**生效中**的黑名单手机号。

        只取 `phone` 一列 —— 判定只需要「在不在集合里」，姓名/原因是给人看的备注。
        停用（`is_active=0`）的不返回：停用的语义就是「不再拦截」。
        """
        stmt = select(CustomerBlacklist.phone).where(CustomerBlacklist.is_active.is_(True))
        result = await self.session.execute(stmt)
        return set(result.scalars().all())
