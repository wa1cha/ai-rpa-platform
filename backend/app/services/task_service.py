"""任务业务逻辑 —— 状态机、入队/出队、审核与重试，见《需求规格》§9。

本文件是「MySQL 状态」和「Redis 队列」两个世界的接缝，所以有一条贯穿全篇的
原则：

    **先写库，后动队列。**

反过来（先入队、库写失败）会在队列里留下一条指向「还没到 QUEUED 的任务」
的记录；先写库则最坏情况是「库里说 QUEUED 但队列里没有」，而这一种可以用
`reconcile_queue()` 照 MySQL 重建修复。**可修复的失败优于不可修复的失败**，
这是选这个顺序的唯一理由。
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    TERMINAL_TASK_STATUSES,
    OrderStatus,
    Priority,
    ReviewResult,
    RiskLevel,
    TaskStatus,
)
from app.core.exceptions import InvalidStateError, NotFoundError
from app.models.order import Order
from app.models.task import Task
from app.repositories.task_execution_repository import TaskExecutionRepository
from app.repositories.task_repository import TaskFilters, TaskRepository
from app.schemas.common import Page, PageParams
from app.schemas.order import AiAnalysisBrief, amount_to_str
from app.schemas.task import (
    TaskDetail,
    TaskExecutionItem,
    TaskListItem,
    TaskOrderBrief,
    TaskRetryResult,
    TaskStatusChanged,
)
from app.services.queue_service import QueueService

logger = logging.getLogger(__name__)

#: 命中这些风险等级就转人工审核 —— 见《需求规格》§8.2。
#: AI 报 MEDIUM 的典型场景是「买家留言与默认物流冲突」，那正是要人看一眼的。
_REVIEW_RISK_LEVELS = frozenset({RiskLevel.MEDIUM, RiskLevel.HIGH})

#: 可以取消的状态。`RUNNING` 不在里面，原因见 `cancel()`。
_CANCELLABLE_STATUSES = frozenset(
    {TaskStatus.PENDING, TaskStatus.WAITING_REVIEW, TaskStatus.QUEUED}
)


@dataclass(slots=True)
class GenerateReport:
    """`gen_tasks.py` 的执行报告。字段是给人看的，所以要能回答
    「扫了多少、建了几张、几张要审核、几张已入队」。"""

    scanned: int = 0
    created: int = 0
    queued: int = 0
    waiting_review: int = 0
    #: 顺便从 ANALYZED 推进到 TASK_CREATED 的订单数（见 `generate` 的说明）
    orders_promoted: int = 0
    task_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class ReconcileReport:
    """队列对账报告。`redis_before` 和 `db_queued` 不相等就说明之前是脏的。"""

    db_queued: int = 0
    redis_before: int = 0
    redis_after: int = 0


class TaskService:
    def __init__(self, session: AsyncSession, queue: QueueService) -> None:
        self.session = session
        self.queue = queue
        self.tasks = TaskRepository(session)
        self.executions = TaskExecutionRepository(session)

    # ==========================================================
    # 查询
    # ==========================================================

    async def list_tasks(
        self, filters: TaskFilters, params: PageParams
    ) -> Page[TaskListItem]:
        rows, total = await self.tasks.list_tasks(filters, params)
        items = [
            TaskListItem(
                id=row.task.id,
                order_id=row.task.order_id,
                order_no=row.order_no,
                customer_name=row.customer_name,
                priority=row.task.priority,
                status=row.task.status,
                need_review=row.task.need_review,
                retry_count=row.task.retry_count,
                max_retry=row.task.max_retry,
                last_error=row.task.last_error,
                claimed_by=row.task.claimed_by,
                created_at=row.task.created_at,
            )
            for row in rows
        ]
        return self.tasks.build_page(rows, total, params, items)

    async def get_detail(self, task_id: int) -> TaskDetail:
        found = await self.tasks.get_with_order_and_analysis(task_id)
        if found is None:
            raise NotFoundError("任务不存在")
        task, order, analysis = found

        return TaskDetail(
            id=task.id,
            order=self.to_order_brief(order),
            analysis=AiAnalysisBrief.model_validate(analysis) if analysis else None,
            priority=task.priority,
            status=task.status,
            need_review=task.need_review,
            retry_count=task.retry_count,
            max_retry=task.max_retry,
            last_error=task.last_error,
            review_result=task.review_result,
            review_reason=task.review_reason,
            cancel_reason=task.cancel_reason,
            claimed_by=task.claimed_by,
            queued_at=task.queued_at,
            finished_at=task.finished_at,
            executions=[
                TaskExecutionItem.model_validate(e)
                for e in await self.executions.list_for_task(task_id)
            ],
        )

    async def list_executions(self, task_id: int) -> list[TaskExecutionItem]:
        """§9.6。任务不存在时返回 404 而不是空数组 —— 空数组会让前端以为
        「这个任务没执行过」，掩盖掉「任务 id 写错了」。"""
        await self._get_or_404(task_id)
        return [
            TaskExecutionItem.model_validate(e)
            for e in await self.executions.list_for_task(task_id)
        ]

    # ==========================================================
    # 生成任务
    # ==========================================================

    async def generate(
        self,
        *,
        order_ids: Sequence[int] | None = None,
        statuses: Sequence[OrderStatus] | None = None,
        force_review: bool = False,
    ) -> GenerateReport:
        """给「还没有任务的订单」建任务，并把不用审核的直接入队。

        Phase 3 的 AI 还没接上，所以绝大多数订单没有分析记录，此时
        `priority` 取占位值 MEDIUM、`need_review` 取 False —— 这正是
        《需求规格》§8.4 说的「队列先建，AI 后接，只负责填 priority 字段」。
        分析记录一旦存在（Phase 5 之后），这里就自动改用它给出的结论，
        一行都不用改。

        `statuses` 透传给仓储做状态过滤：AI 侧传 `[ANALYZED]`，只给「已分析」
        的单建任务；手动 Phase-3 路径不传，保持「捡所有无任务订单」的旧行为。

        建完任务后，把**仍处 ANALYZED** 的订单推进到 `TASK_CREATED`（同一个
        commit 里），补上此前全代码库缺失的那一步。只在 ANALYZED 时推进——
        手动路径捡到的是 IMPORTED 单，状态保持不变，既有行为不受影响。

        `force_review=True` 把所有任务都推进 `WAITING_REVIEW`，
        用来演示 / 测试人工审核通路 —— 否则本地没有 AI，那条路径永远走不到。
        """
        candidates = await self.tasks.list_orders_without_task(
            order_ids=order_ids, statuses=statuses
        )
        report = GenerateReport(scanned=len(candidates))
        if not candidates:
            return report

        now = datetime.now()
        pending: list[Task] = []

        for order, analysis in candidates:
            need_review, priority = self._decide(analysis, force_review)
            task = Task(
                order_id=order.id,
                ai_analysis_id=analysis.id if analysis else None,
                priority=priority.value,
                # 直接落到终态，不停留 PENDING：PENDING 的语义是「已生成、待入队」，
                # 而这里生成和入队是同一个动作，中间态没有任何人能观察到。
                status=(
                    TaskStatus.WAITING_REVIEW if need_review else TaskStatus.QUEUED
                ).value,
                need_review=need_review,
                # queued_at 由 Python 显式赋值，不用 server_default ——
                # 队列的 score 要用它，而 commit 之后再读 server_default
                # 生成的列会触发懒加载，异步下直接 MissingGreenlet。
                queued_at=None if need_review else now,
            )
            self.session.add(task)
            pending.append(task)
            if need_review:
                report.waiting_review += 1

            # 建出任务后把订单推进到 TASK_CREATED。**只在 ANALYZED 时推进**：
            # 手动 Phase-3 路径（statuses=None）捡到的是 IMPORTED 单，保持原状，
            # 既有「任务已存在、订单仍 IMPORTED」的行为不受影响。
            if order.status == OrderStatus.ANALYZED.value:
                order.status = OrderStatus.TASK_CREATED.value
                report.orders_promoted += 1

        # flush 只发 INSERT、拿回自增 id，不提交。把要用的值先抄成普通
        # Python 元组，再 commit —— 本项目 `expire_on_commit=False`，
        # 这些我们自己赋过值的属性提交后其实还读得到；但 `created_at` /
        # `updated_at` 是 `server_default`，SQLAlchemy 从来没加载过它们，
        # 任何时候读都会发一次查询。抄下来是为了让「哪些值已加载」这件事
        # 在代码里显式可见，而不是靠记住那个设置。
        await self.session.flush()
        created = [
            (t.id, t.priority, t.queued_at, t.status) for t in pending
        ]
        await self.session.commit()

        report.created = len(created)
        report.task_ids = [row[0] for row in created]

        for task_id, priority, queued_at, status in created:
            if status == TaskStatus.QUEUED:
                await self.queue.enqueue(task_id, priority, queued_at)
                report.queued += 1

        logger.info(
            "生成任务：扫描 %d，新建 %d，入队 %d，待审核 %d，订单推进 TASK_CREATED %d",
            report.scanned,
            report.created,
            report.queued,
            report.waiting_review,
            report.orders_promoted,
        )
        return report

    @staticmethod
    def _decide(analysis, force_review: bool) -> tuple[bool, Priority]:
        """决定一个任务是「直接入队」还是「转人工审核」，以及它的优先级。

        Phase 5 接上 AI 之后，`analysis.risk_level` / `need_contact` 就是
        硬规则引擎和 AI 合并后的最终结论（见《需求规格》§8.1「冲突时以硬规则为准」），
        这里不需要知道规则细节，只消费结论 —— 规则改动只影响 Phase 5 那一侧。
        """
        if force_review:
            return True, Priority.MEDIUM
        if analysis is None:
            # 还没分析过：中优先级、直接入队。不默认转审核，否则 Phase 3
            # 所有订单都会堆在 WAITING_REVIEW 里，队列等于没建。
            return False, Priority.MEDIUM

        need_review = (
            analysis.risk_level in _REVIEW_RISK_LEVELS or bool(analysis.need_contact)
        )
        priority = Priority(analysis.priority or Priority.MEDIUM.value)
        return need_review, priority

    # ==========================================================
    # 队列
    # ==========================================================

    async def pop_next_runnable(self, block_seconds: float = 0) -> int | None:
        """从队列取出一个**回查确认仍然可执行**的任务 id，没有则返回 None。

        出队后必须回查 MySQL，不能直接信 Redis：

        - 人工取消后如果 Redis 那次 `remove` 没成功（网络抖动），队列里还留着它；
        - 任务已经跑完 `SUCCESS`，队列里的残留没人清；
        - Redis 是加速层不是事实来源，它自己也可能被 flush 过。

        所以这里循环「弹出 → 校验 → 不合格就丢」，直到弹到一个真的 QUEUED
        或者队列空。**丢弃是安全的**：MySQL 说它不是 QUEUED，它就不该被执行；
        万一它确实该在队列里，`reconcile_queue()` 会把它补回来。

        本函数只负责「交出一个可执行的 id」；Phase 4 的 `claim` 接口在它之后
        接着写 `status=RUNNING` / `claimed_by` 和 `task_executions`。

        `block_seconds > 0` 时走 Redis 的阻塞版出队（见 `QueueService.pop_one`），
        claim 接口传 30 秒做长轮询，避免 Worker 空转轮询打满 MySQL。

        **回查前必须先 `rollback()` 丢掉旧快照**：调用方（`/rpa/tasks/claim`）
        在进入本函数之前，鉴权依赖 `get_current_user` 已经查过一次库，而本请求
        与它共用同一个 session（FastAPI 缓存 `get_db`）—— MySQL 默认 REPEATABLE
        READ，那次 SELECT 就把事务快照固定了。随后这里可能刚在 `bzpopmin` 上挂了
        最多 30 秒；期间别的连接新提交的 `QUEUED` 任务，在旧快照里根本不存在，
        于是被误判成「已失效的队列残留」丢掉 —— 表现是**新任务一条都领不到，
        Worker 一直空轮询**。回滚只读事务即可，下一次 `get` 取新快照重读。
        鉴权到此刻之间没有任何写入，回滚没有副作用。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + block_seconds if block_seconds > 0 else None

        dropped = 0
        try:
            while True:
                remaining = 0.0
                if deadline is not None:
                    # 按「剩余时间」阻塞：中途丢掉一条残留就重新等满 30 秒的话，
                    # 队列有脏数据时这个请求会迟迟不返回。
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return None

                task_id = await self.queue.pop_one(block_seconds=remaining)
                if task_id is None:
                    return None

                await self.session.rollback()

                task = await self.tasks.get(task_id)
                if task is not None and task.status == TaskStatus.QUEUED:
                    return task_id
                dropped += 1
        finally:
            # 残留条数是要盯的指标：持续非零说明「写库成功、出队失败」这条
            # 失败路径在反复发生，那是个真 bug，不该被静默吞掉。
            if dropped:
                logger.warning("出队丢弃 %d 条已失效的队列残留", dropped)

    async def reconcile_queue(self) -> ReconcileReport:
        """照 MySQL 全量重建 Redis 队列 —— 见 `QueueService.rebuild`。

        用途：Redis 被重启 / flush、或者怀疑队列脏了。因为事实来源始终是
        `tasks.status = 'QUEUED'`，重建结果与「Redis 从没出过事」等价。
        """
        report = ReconcileReport()
        report.redis_before = await self.queue.size()

        rows = await self.tasks.list_queued_for_reconcile()
        report.db_queued = len(rows)
        report.redis_after = await self.queue.rebuild(rows)

        logger.info(
            "队列对账：Redis %d -> %d，MySQL 中 QUEUED %d",
            report.redis_before,
            report.redis_after,
            report.db_queued,
        )
        return report

    # ==========================================================
    # 状态流转
    # ==========================================================

    async def review(
        self, task_id: int, result: ReviewResult, reason: str | None, reviewer_id: int
    ) -> TaskStatusChanged:
        """§9.3 人工审核：通过 → QUEUED 入队，驳回 → CANCELLED。"""
        task = await self._get_or_404(task_id)
        if task.status != TaskStatus.WAITING_REVIEW:
            raise self._invalid(
                task, "只有待审核的任务可以审核", f"result={result.value}"
            )

        now = datetime.now()
        task.review_result = result.value
        task.review_reason = reason
        task.reviewed_by = reviewer_id
        task.reviewed_at = now

        if result == ReviewResult.APPROVED:
            task.status = TaskStatus.QUEUED
            task.queued_at = now
            new_status, enqueue = TaskStatus.QUEUED, True
        else:
            task.status = TaskStatus.CANCELLED
            task.finished_at = now
            new_status, enqueue = TaskStatus.CANCELLED, False

        # 先写库、后动队列 —— 顺序的理由见模块开头。这里的 `priority` 是
        # ORM 自己赋的值（不是 server_default），`expire_on_commit=False`
        # 下 commit 之后照样读得到，不必提前抄。
        await self.session.commit()

        if enqueue:
            await self.queue.enqueue(task_id, task.priority, now)
        else:
            # 驳回的任务几乎不可能在队列里（进 WAITING_REVIEW 时就没入队），
            # 但「几乎不可能」不等于不会，顺手清一下，代价是一次 ZREM。
            await self.queue.remove(task_id)

        logger.info("任务 %d 审核 %s -> %s", task_id, result.value, new_status.value)
        return TaskStatusChanged(task_id=task_id, status=new_status.value)

    async def retry(
        self, task_id: int, *, reset_retry_count: bool, reason: str | None
    ) -> TaskRetryResult:
        """§9.4 手动重试：仅 `FAILED` 可重试。

        与自动重试的区别不只是「谁触发」：自动重试是「再试一次同样的输入」，
        人工重试的前提是**人已经把根因处理掉了**（ERP 恢复了、库存补上了），
        所以允许把 `retry_count` 归零，不占用那 3 次自动额度。
        """
        task = await self._get_or_404(task_id)
        if task.status != TaskStatus.FAILED:
            raise self._invalid(task, "只有失败的任务可以重试", "retry")

        now = datetime.now()
        if reset_retry_count:
            task.retry_count = 0

        task.status = TaskStatus.QUEUED
        task.queued_at = now
        task.last_error = None
        task.finished_at = None
        # 上一轮领取的痕迹要抹掉，否则详情页会显示「被 rpa-worker-01 领取着」，
        # 而它其实刚回到队尾。
        task.claimed_by = None
        task.heartbeat_at = None
        # 人工处置说明写 `cancel_reason`，不碰 `review_reason` ——
        # 后者是审核意见，两者混用会让「这行到底是审核结论还是取消原因」
        # 变成需要靠猜的事（见《数据库设计》§4.5 的说明）。
        if reason:
            task.cancel_reason = reason

        retry_count = task.retry_count
        await self.session.commit()
        await self.queue.enqueue(task_id, task.priority, now)

        logger.info("任务 %d 人工重试，retry_count=%d", task_id, retry_count)
        return TaskRetryResult(
            task_id=task_id, status=TaskStatus.QUEUED.value, retry_count=retry_count
        )

    async def cancel(self, task_id: int, reason: str | None) -> TaskStatusChanged:
        """§9.5 取消任务。

        `RUNNING` 明确不可取消：RPA 此刻正在 ERP 里操作，强行把状态改成
        CANCELLED 只是改了我们的库，ERP 那边的单子照建不误 —— 主库和 ERP
        就此不一致，而且没人知道该信谁。宁可让它跑完再人工处理。
        """
        task = await self._get_or_404(task_id)
        if task.status == TaskStatus.RUNNING:
            raise InvalidStateError(
                "任务正在执行，无法取消，请等待当前执行结束后再处理"
            )
        if task.status not in _CANCELLABLE_STATUSES:
            hint = (
                "任务已结束，无需取消"
                if task.status in TERMINAL_TASK_STATUSES
                else "当前状态不允许取消"
            )
            raise self._invalid(task, hint, "cancel")

        task.status = TaskStatus.CANCELLED
        task.finished_at = datetime.now()
        if reason:
            task.cancel_reason = reason

        await self.session.commit()
        await self.queue.remove(task_id)

        logger.info("任务 %d 已取消", task_id)
        return TaskStatusChanged(task_id=task_id, status=TaskStatus.CANCELLED.value)

    # ==========================================================
    # 内部工具
    # ==========================================================

    async def _get_or_404(self, task_id: int) -> Task:
        task = await self.tasks.get(task_id)
        if task is None:
            raise NotFoundError("任务不存在")
        return task

    @staticmethod
    def _invalid(task: Task, hint: str, action: str) -> InvalidStateError:
        """状态不符时统一报错，把「现在是什么状态」告诉调用方 ——
        只说「不允许」的报错，前端和排障的人都得再去查一次库。"""
        return InvalidStateError(f"{hint}（当前状态 {task.status}，操作：{action}）")

    @staticmethod
    def to_order_brief(order: Order) -> TaskOrderBrief:
        """订单 → 任务上下文快照。任务详情（管理员看）和 claim（Worker 拿去填
        ERP）用的是同一份，所以它是公开的，`RpaService` 也调它。"""
        return TaskOrderBrief(
            id=order.id,
            order_no=order.order_no,
            customer_name=order.customer_name,
            # 不脱敏：管理员要核对，Worker 要照抄进 ERP。见 TaskOrderBrief 的说明。
            phone=order.phone,
            address=order.address,
            product_name=order.product_name,
            sku=order.sku,
            quantity=order.quantity,
            amount=amount_to_str(order.amount),
            buyer_message=order.buyer_message,
        )
