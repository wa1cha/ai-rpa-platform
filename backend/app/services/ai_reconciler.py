"""AI 分析队列的补偿 —— 把「写库成功、入队失败」和「分析中途死了」的单捞回来。

为什么需要它
------------
`POST /orders/import` 的收尾是「先 commit 订单、再入队」两步，而这两步跨了
两个存储（MySQL / Redis）。**它们本来就不在同一个事务里，假装能原子才是真的
危险**（同 `rpa_service.claim` 里那条已知的顺序风险）。所以缺口是结构性的：

  · 订单已落库，入队那一步挂了（Redis 抖动、进程被杀）→ 这单永远等不到分析；
  · 分析进程领了单、置成 ANALYZING 之后挂了 → 这单永远卡在 ANALYZING。

补偿不靠事务，靠**周期性增量对账**：判据全部取自库里的状态，所以这个模块
是**无状态**的 —— 它自己崩了，下一轮照扫；跑重复了，也只会把同一批 id 重复
`ZADD`（幂等，见 `QueueService.enqueue_fifo_many`）。

三条腿（L1/L2/L3）
------------------
| 腿 | 症状 | 动作 |
| --- | --- | --- |
| L1 | `IMPORTED` 且超过入队宽限 | 判定「写库成功、入队失败」→ 重新入队 |
| L2 | `ANALYZING` 且超过分析超时 | 判定「分析进程中途死了」→ 置回 `IMPORTED` 再入队 |
| L3 | `ANALYZED` 且超过宽限仍无 task | 判定「worker 建任务那步没跑成」→ 补建任务 |

时间边界与「扫哪些行」分开放：截止时刻由本模块的 `*_cutoff()` 算（测试能传
死值），查询在 `OrderRepository.list_by_status_older_than`（与
`TaskRepository.list_stale_running` 同形状）。

L3 复用 `TaskService.generate(order_ids=..., statuses=[ANALYZED])`：`statuses`
是本切片给 `generate` 新加的参数，让它**只认已分析的单** —— 否则
`list_orders_without_task` 会把 `IMPORTED` 的单一并建出任务，等于绕过 AI 分析、
把「导入即录 ERP」接回来。`Task.id IS NULL` 反连接保证不与 worker 已建的任务重复。

**不能拿 `QueueService.rebuild()` 当补偿**：那是 `DELETE` + 全量重写的停机维护
操作（docstring 明写「调用前提：没有 Worker 在跑」）。补偿必须**增量且幂等**。
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import OrderStatus
from app.database.mysql import AsyncSessionLocal
from app.database.redis import redis_client
from app.repositories.order_repository import OrderRepository
from app.services.queue_service import QueueService
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


def enqueue_grace_cutoff(now: datetime | None = None) -> datetime:
    """L1 的分界线：早于此刻仍停在 `IMPORTED` 的，就判定入队那一步失败了。

    独立成函数（同 `zombie_reaper.stale_cutoff`）是为了能在测试里传固定的
    `now` —— 否则测它就得改全局配置或真的等 60 秒。
    """
    return (now or datetime.now()) - timedelta(
        seconds=settings.ai_enqueue_grace_seconds
    )


def analyzing_timeout_cutoff(now: datetime | None = None) -> datetime:
    """L2 的分界线：早于此刻仍停在 `ANALYZING` 的，就判定分析进程已死。"""
    return (now or datetime.now()) - timedelta(
        seconds=settings.ai_analyzing_timeout_seconds
    )


def analyzed_task_grace_cutoff(now: datetime | None = None) -> datetime:
    """L3 的分界线：早于此刻仍停在 `ANALYZED` 且没有任务的，判定建任务那步没跑成。

    复用 `ai_enqueue_grace_seconds`：worker 的「分析完立刻建任务」与导入的
    「commit 后立刻入队」是同一个形态的短窗口，给它同样的宽限即可 —— 只捞
    真的没建上的，不误伤正在建的。
    """
    return (now or datetime.now()) - timedelta(
        seconds=settings.ai_enqueue_grace_seconds
    )


@dataclass(slots=True)
class ReconcileReport:
    """一轮补偿的结果。命中数正常恒为 0，非零就该被看见。"""

    #: L1 —— `IMPORTED` 超时、重新入队的条数
    requeued_imported: int = 0
    #: L2 —— `ANALYZING` 超时、置回 `IMPORTED` 并重排的条数
    reset_analyzing: int = 0
    #: L3 —— `ANALYZED` 超时无任务、补建任务的条数
    tasks_created: int = 0
    #: 本轮动过的订单 id，供日志定位
    order_ids: list[int] = field(default_factory=list)


async def reconcile_ai_queue(
    session: AsyncSession,
    queue: QueueService,
    *,
    grace_cutoff: datetime,
    analyzing_cutoff: datetime,
) -> ReconcileReport:
    """跑一轮 L1 + L2。**只在这里动一次库、一次队列**，顺序与别处一致：
    先写库、后动队列。

    拆成「算 id → 写库 → 动队列」三段，是为了让「写」和「动队列」之间没有任何
    查询夹在中间 —— 队列操作永远发生在所有数据库写入 commit 之后。
    """
    orders = OrderRepository(session)
    report = ReconcileReport()

    # ---------- 第一段：只读库，确定这一轮要动谁 ----------
    # L1：IMPORTED 且超过入队宽限
    stale_imported = await orders.list_by_status_older_than(
        OrderStatus.IMPORTED, grace_cutoff
    )
    l1_ids = [order.id for order in stale_imported]

    # L2：ANALYZING 且超过分析超时
    stuck_analyzing = await orders.list_by_status_older_than(
        OrderStatus.ANALYZING, analyzing_cutoff
    )
    l2_ids: list[int] = []
    for order in stuck_analyzing:
        # CAS：只有它此刻**确实还停在** ANALYZING 才置回。分析进程可能正好在
        # 这一瞬间完成、把状态推到了 ANALYZED —— 那就别把它拉回来重做一遍。
        # 这是与消费端同一把锁的两侧：这边防「拉回已完成的分析」，
        # worker 那边防「两个 worker 分析同一单」。
        if await orders.cas_transition_status(
            order.id, OrderStatus.ANALYZING, OrderStatus.IMPORTED
        ):
            l2_ids.append(order.id)

    # ---------- 第二段：先写库（L2 的状态回退）----------
    await session.commit()

    # ---------- 第三段：后动队列 ----------
    # 两条腿最终都是「把这单推回 AI 队列」，合成一次 ZADD 发出去。
    # 分两组只是为了日志能分别计数，队列侧不需要知道它们的来路不同。
    if l1_ids or l2_ids:
        await queue.enqueue_fifo_many(l1_ids + l2_ids)

    report.requeued_imported = len(l1_ids)
    report.reset_analyzing = len(l2_ids)
    report.order_ids = l1_ids + l2_ids
    return report


async def reconcile_task_creation(
    session: AsyncSession,
    task_queue: QueueService,
    *,
    cutoff: datetime,
) -> int:
    """L3：`ANALYZED` 超过宽限仍没有任务的订单，补建任务。返回补建条数。

    复用 `TaskService.generate(order_ids=..., statuses=[ANALYZED])`：
      · `statuses=[ANALYZED]` 让它只认已分析的单，不会把 `IMPORTED` 的也建出来；
      · `order_ids` 只扫本腿挑出的候选，不全表扫；
      · `generate` 内部把订单推进到 `TASK_CREATED`，与 worker 的正常路径一致。
    """
    orders = OrderRepository(session)
    stale = await orders.list_by_status_older_than(OrderStatus.ANALYZED, cutoff)
    order_ids = [order.id for order in stale]
    if not order_ids:
        return 0

    report = await TaskService(session, task_queue).generate(
        order_ids=order_ids, statuses=[OrderStatus.ANALYZED]
    )
    return report.created


async def reconcile_once(now: datetime | None = None) -> ReconcileReport:
    """跑一轮补偿：先 L1/L2（分析队列），再 L3（分析完没建任务的）。

    一次一个 session 事务，超时与失败都不往外抛 —— 由 `workers/main.py` 的
    作业循环统一兜（记 traceback、下一轮再试）。补偿是中性的维护动作，
    一轮失败不该带走整个作业进程。
    """
    async with AsyncSessionLocal() as session:
        report = await reconcile_ai_queue(
            session,
            QueueService(redis_client, key=settings.ai_queue_key),
            grace_cutoff=enqueue_grace_cutoff(now),
            analyzing_cutoff=analyzing_timeout_cutoff(now),
        )
        report.tasks_created = await reconcile_task_creation(
            session,
            QueueService(redis_client),
            cutoff=analyzed_task_grace_cutoff(now),
        )

    if report.order_ids or report.tasks_created:
        # 命中数正常恒为 0。持续非零说明「写库成功、入队失败」或「分析完没建
        # 任务」在反复发生 —— 那不是可以慢慢容忍的噪音，是个该被查的真 bug。
        logger.warning(
            "AI 补偿：重新入队 %d（导入后入队失败），置回重排 %d（分析超时），"
            "补建任务 %d（分析后未建任务），订单 %s",
            report.requeued_imported,
            report.reset_analyzing,
            report.tasks_created,
            report.order_ids,
        )
    else:
        logger.debug("AI 补偿：无异常订单")
    return report
