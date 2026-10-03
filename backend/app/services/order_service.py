"""订单查询业务逻辑 —— 见《API接口设计》§6.1、§6.2。

这个 service 只做两件事：调仓储、把仓储的返回值**翻译成接口契约的形状**。
翻译放在这里而不是仓储里，是因为「脱敏」「金额转字符串」都是**展示规则**，
换了接口（比如给 RPA 用的内部接口）就不该套用同一套规则。

§6.4 的「重新触发 AI 分析」是个例外：它不是只读，而是一次**写 + 入队**的动作，
所以下面 `reanalyze` 里能看到事务与队列的编排 —— 编排放 service 层，
正是为了守住「先写库、后动队列」这条铁律。
"""

import logging
from collections.abc import Sequence

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import OrderStatus, TaskStatus
from app.core.exceptions import InvalidStateError, NotFoundError
from app.database.redis import redis_client
from app.repositories.order_repository import OrderListRow, OrderRepository, OrderFilters
from app.repositories.task_repository import TaskRepository
from app.schemas.common import Page, PageParams
from app.schemas.order import (
    AiAnalysisBrief,
    OrderDetail,
    OrderListItem,
    OrderReanalyzeResult,
    ReviewLogItem,
    TaskBrief,
    amount_to_str,
    mask_phone,
)
from app.services.queue_service import QueueService

logger = logging.getLogger(__name__)

#: 允许重新分析的订单状态：已分析过的订单。`IMPORTED`（待分析）没必要「重新」分析。
_REANALYZE_ORDER_STATUSES = frozenset(
    {OrderStatus.ANALYZED.value, OrderStatus.TASK_CREATED.value}
)

#: 允许被删除重建的任务状态 —— 一律是「还没被 RPA 执行」的任务。
#: `RUNNING` 或终态（`SUCCESS`/`FAILED`/`CANCELLED`）已经动过 ERP，撤不回来。
_REANALYZE_DELETABLE_TASK_STATUSES: tuple[TaskStatus, ...] = (
    TaskStatus.PENDING,
    TaskStatus.QUEUED,
    TaskStatus.WAITING_REVIEW,
)


class OrderService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        task_queue: QueueService | None = None,
        ai_queue: QueueService | None = None,
    ) -> None:
        self.session = session
        self.orders = OrderRepository(session)
        self.tasks = TaskRepository(session)
        #: 两个队列都默认自建，与 `ImportService` 的写法一致 —— 只有
        #: `reanalyze` 会用到它们，其余接口根本不碰队列。测试要桩住就显式传进来。
        self.task_queue = task_queue or QueueService(redis_client)
        self.ai_queue = ai_queue or QueueService(redis_client, key=settings.ai_queue_key)

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

    # ==========================================================
    # §6.4 重新触发 AI 分析
    # ==========================================================

    async def reanalyze(
        self, order_id: int, reason: str | None = None
    ) -> OrderReanalyzeResult:
        """把一张已分析过的订单退回「待分析」，清掉尚未执行的旧任务。

        算法（顺序不可换）：

            校验 → CAS 订单退回 IMPORTED → 条件删除旧任务 → commit
                 → 摘任务队列 → 重入 AI 队列

        **为什么要删旧任务**：订单与任务是 1:1（`uk_tasks_order`），而 `generate()`
        的反连接（`Task.id IS NULL`）只会给「还没有任务的订单」建任务。不删的话，
        重分析写出的新分析行会被反连接静默跳过，任务仍引用旧的 `ai_analysis_id`，
        RPA 就会按过期的判断去录 ERP。删掉它、让流水线照原路重建，
        复用的是同一条已被验证过的路径。

        **为什么 CAS 订单和删任务在同一个事务里**：这两件事必须一起成功或一起失败。
        否则会出现「订单退回了 IMPORTED，但旧任务还在」的分裂态 —— 既会被重分析，
        又会被旧任务驱动录 ERP。

        **为什么响应状态是 IMPORTED 而不是文档写的 ANALYZING**：真正把
        `IMPORTED→ANALYZING` 的是 AI Worker 出队后的 CAS（`ai_service.analyze_order`）。
        这里若抢先置成 ANALYZING，Worker 的 `WHERE status='IMPORTED'` 会拿 0 行而
        SKIPPED，订单就卡在 ANALYZING 直到 L2 超时 —— 等于自己造一次故障。
        `IMPORTED` 在本系统里本就是「待分析」态（导入接口也是这么置的）。

        `reason` 只进日志、不落库（见 `OrderReanalyzeRequest` 的说明）。
        """
        order = await self.orders.get(order_id)
        if order is None:
            raise NotFoundError("订单不存在")

        if order.status not in _REANALYZE_ORDER_STATUSES:
            raise InvalidStateError(
                f"订单当前状态为 {order.status}，只有已分析过的订单"
                f"（{'、'.join(sorted(_REANALYZE_ORDER_STATUSES))}）才能重新分析"
            )
        from_status = OrderStatus(order.status)

        task = await self.tasks.get_by_order_id(order_id)
        if task is not None and task.status not in {
            s.value for s in _REANALYZE_DELETABLE_TASK_STATUSES
        }:
            raise InvalidStateError(
                f"任务当前状态为 {task.status}，已被 RPA 执行或已结束，不能重新分析"
            )
        old_task_id = task.id if task is not None else None

        # 条件 UPDATE：并发下若订单已被人改走，rowcount 为 0，这里明确拒绝而不是硬改。
        promoted = await self.orders.cas_transition_status(
            order_id, from_status, OrderStatus.IMPORTED
        )
        if not promoted:
            raise InvalidStateError("订单状态已变化，请刷新后重试")

        if old_task_id is not None:
            # 条件 DELETE 自身带状态过滤，即便并发把它认领成 RUNNING 也删不掉。
            await self.tasks.delete_by_order_id_if_status_in(
                order_id, _REANALYZE_DELETABLE_TASK_STATUSES
            )

        await self.session.commit()

        # 库已落定（事实来源），再动 Redis。队列失败不报错：订单已回到 IMPORTED，
        # 缺口由补偿兜住 —— 任务队列有手动对账，AI 队列有 L1。
        if old_task_id is not None:
            try:
                await self.task_queue.remove(old_task_id)
            except RedisError as exc:
                logger.warning(
                    "重分析：任务 %d 摘除失败，交由手动对账兜底：%s", old_task_id, exc
                )
        try:
            await self.ai_queue.enqueue_fifo(order_id)
        except RedisError as exc:
            logger.warning(
                "重分析：订单 %d 重新入队失败，交由 L1 补偿兜底：%s", order_id, exc
            )

        logger.info(
            "订单 %d 重新触发 AI 分析%s", order_id, f"（原因：{reason}）" if reason else ""
        )
        return OrderReanalyzeResult(order_id=order_id, status=OrderStatus.IMPORTED.value)

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
