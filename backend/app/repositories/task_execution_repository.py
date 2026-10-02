"""task_executions 表的数据访问 —— 见《数据库设计》§4.6。

写这条记录的是 Phase 4 的 RPA Worker 接口，Phase 3 只读：任务详情页要能
看到「这一单试过几次、每次为什么失败」。
"""

from sqlalchemy import select

from app.models.task_execution import TaskExecution
from app.repositories.base import BaseRepository


class TaskExecutionRepository(BaseRepository[TaskExecution]):
    model = TaskExecution

    async def get_by_attempt(self, task_id: int, attempt: int) -> TaskExecution | None:
        """取某任务的第 N 次执行记录。

        claim / 回传 / 僵尸回收三处都要「本次尝试」的那条，而本次尝试恒等于
        `retry_count + 1` —— 这是三处共同的定位方式，所以收成一个方法，
        免得三处各写一遍 where。

        `uk_task_executions_attempt` 保证 (task_id, attempt) 最多一条，
        所以用 scalar 取而不是 scalars。
        """
        stmt = select(TaskExecution).where(
            TaskExecution.task_id == task_id,
            TaskExecution.attempt == attempt,
        )
        return await self.session.scalar(stmt)

    async def list_for_task(self, task_id: int) -> list[TaskExecution]:
        """按尝试次数升序 —— 前端从上往下读就是 1、2、3 次的完整过程。

        再加 `id` 兜底排序：同一任务不会有两行 attempt 相同
        （`uk_task_executions_attempt`），但排序键写全了，翻页才不会抖。
        """
        stmt = (
            select(TaskExecution)
            .where(TaskExecution.task_id == task_id)
            .order_by(TaskExecution.attempt.asc(), TaskExecution.id.asc())
        )
        return list(await self.session.scalars(stmt))
