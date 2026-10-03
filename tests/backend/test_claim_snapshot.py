"""回归测试：长轮询领任务时，库快照不能停在请求开头。

背景（Phase 7 容器化时暴露的真实 bug）
--------------------------------------
`/rpa/tasks/claim` 的鉴权依赖 `get_current_user` 与端点**共用同一个 session**
（FastAPI 默认缓存 `get_db`）。那次查库会在 MySQL 默认的 REPEATABLE READ 下
固定本请求的事务快照。随后 `pop_next_runnable` 要在 Redis 的 `bzpopmin` 上挂起
最长 30 秒 —— 期间别的连接（AI worker）新建并提交的 `QUEUED` 任务，在旧快照里
根本看不到，于是被当成「已失效的队列残留」丢掉。表现是：**Worker 一直空轮询，
新任务一条都领不到**，而库里任务好端端停在 QUEUED。

修法见 `TaskService.pop_next_runnable`：回查前先 `rollback()` 丢掉旧快照。
本用例复现的就是「快照已固定 → 另一条连接提交 → 回查」这条路径。
"""

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core.enums import OrderStatus, Priority, TaskStatus
from app.database.mysql import AsyncSessionLocal
from app.models.order import Order
from app.models.task import Task
from app.services.task_service import TaskService

pytestmark = pytest.mark.integration


async def test_pop_next_runnable_sees_a_task_committed_after_its_snapshot(
    db_session, queue
):
    # 这个用例的前提是服务端隔离级别为 REPEATABLE READ（MySQL 默认）。
    # 换成 READ COMMITTED 的话「旧快照」根本不存在，用例会失去意义 —— 与其
    # 悄悄变成一个永远通过的假测试，不如直接把前提摆出来。
    isolation = await db_session.scalar(text("SELECT @@transaction_isolation"))
    assert isolation.upper().startswith("REPEATABLE"), f"隔离级别为 {isolation}"

    # ① 先在 db_session 上读一次，把事务快照固定下来（模拟鉴权那一次查库）。
    await db_session.execute(select(Order).limit(1))

    # ② 另开一条连接写入并提交一个新 QUEUED 任务，模拟「长轮询挂起期间
    #    AI worker 建好任务并入队」。必须用独立连接 —— 同 session 提交会把
    #    上面那个快照一起结束，就复现不出问题了。
    async with AsyncSessionLocal() as other:
        order = Order(
            order_no="TEST-SNAP-000001",
            platform="mock",
            ordered_at=datetime(2026, 1, 1, 10, 0, 0),
            customer_name="快照测试客户",
            phone="13800000001",
            address="测试省测试市测试路 1 号",
            product_name="测试商品",
            sku="SKU-SNAP",
            quantity=1,
            amount=Decimal("1.00"),
            status=OrderStatus.IMPORTED.value,
        )
        other.add(order)
        await other.flush()
        task = Task(
            order_id=order.id,
            priority=Priority.MEDIUM.value,
            status=TaskStatus.QUEUED.value,
            need_review=False,
            queued_at=datetime.now(),
        )
        other.add(task)
        await other.commit()
        new_task_id = task.id

    await queue.enqueue(new_task_id, Priority.MEDIUM)

    # ③ 旧 session 取数：快照若没刷新，就看不到刚提交的任务 —— 它会被当成
    #    残留丢掉，函数返回 None。
    assert await TaskService(db_session, queue).pop_next_runnable(0) == new_task_id
