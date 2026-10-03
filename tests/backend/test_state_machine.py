"""任务状态机测试 —— `services/task_service.py`，对应《需求规格》§9。

这批用例**基本都是集成测试**：它们验的不是某个函数的返回值，而是
「MySQL 的状态」与「Redis 的队列」在每次流转后是否**一致**。
这个一致性是 Phase 3 全部设计的落点（模块开头那句「先写库，后动队列」），
只有连真 MySQL + 真 Redis 才测得到 —— mock 出来的队列没有「一致性」可言。

唯一例外是 `_decide`：它是纯函数，一个 SimpleNamespace 就能喂饱，
放 `unit` 层，这样连不上数据库的机器也能验证「什么情况转人工审核」这条规则。
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.enums import OrderStatus, Priority, ReviewResult, RiskLevel, TaskStatus
from app.core.exceptions import ErrorCode, InvalidStateError
from app.models.order import Order
from app.models.task import Task
from app.services.task_service import TaskService

# ============================================================
# 工具
# ============================================================


async def _db_status(session, task_id: int) -> str:
    """从**数据库**读当前状态。

    刻意用 `select(Task.status)` 拿标量而不是读 ORM 对象的属性：
    标量查询不经过 identity map，读到的一定是行里的真值。
    读对象属性则可能读到内存里被改过、但没提交成功的那份，
    那样断言就变成了「我以为的」而不是「数据库里的」。
    """
    result = await session.execute(select(Task.status).where(Task.id == task_id))
    return result.scalar_one()


async def _in_queue(queue, task_id: int) -> bool:
    return task_id in await queue.peek(1000)


# ============================================================
# unit —— _decide 的转审核规则（《需求规格》§8.2）
# ============================================================


def _analysis(risk_level, need_contact=False, priority="MEDIUM"):
    return SimpleNamespace(
        risk_level=risk_level, need_contact=need_contact, priority=priority
    )


@pytest.mark.unit
def test_decide_without_analysis_queues_directly_at_medium():
    """Phase 3 没有 AI，绝大多数订单走这条路。

    不默认转审核 —— 否则所有订单都堆在 WAITING_REVIEW 里，队列等于没建。
    """
    assert TaskService._decide(None, False) == (False, Priority.MEDIUM)


@pytest.mark.unit
def test_decide_force_review_wins_over_analysis():
    need_review, priority = TaskService._decide(
        _analysis(RiskLevel.LOW, priority="HIGH"), True
    )
    assert need_review is True
    assert priority == Priority.MEDIUM


@pytest.mark.unit
@pytest.mark.parametrize("risk", [RiskLevel.MEDIUM, RiskLevel.HIGH])
def test_decide_medium_and_high_risk_go_to_review(risk):
    need_review, _ = TaskService._decide(_analysis(risk), False)
    assert need_review is True


@pytest.mark.unit
def test_decide_need_contact_forces_review_even_at_low_risk():
    """买家留言要求电话确认时，哪怕风险等级是 LOW 也要人看一眼。"""
    need_review, _ = TaskService._decide(
        _analysis(RiskLevel.LOW, need_contact=True), False
    )
    assert need_review is True


@pytest.mark.unit
def test_decide_low_risk_without_contact_takes_analysis_priority():
    need_review, priority = TaskService._decide(
        _analysis(RiskLevel.LOW, priority="HIGH"), False
    )
    assert need_review is False
    assert priority == Priority.HIGH


# ============================================================
# integration —— 生成任务
# ============================================================


@pytest.mark.integration
async def test_generate_creates_task_and_enqueues(db_session, queue, make_order):
    order = await make_order()
    service = TaskService(db_session, queue)

    report = await service.generate(order_ids=[order.id])

    assert (report.scanned, report.created, report.queued, report.waiting_review) == (
        1,
        1,
        1,
        0,
    )
    task_id = report.task_ids[0]
    assert await _db_status(db_session, task_id) == TaskStatus.QUEUED.value
    assert await _in_queue(queue, task_id)


@pytest.mark.integration
async def test_generate_is_idempotent_for_orders_that_already_have_a_task(
    db_session, queue, make_order
):
    order = await make_order()
    service = TaskService(db_session, queue)

    await service.generate(order_ids=[order.id])
    second = await service.generate(order_ids=[order.id])

    assert second.created == 0
    assert await queue.size() == 1


@pytest.mark.integration
async def test_generate_with_force_review_holds_task_out_of_the_queue(
    db_session, queue, make_order
):
    order = await make_order()
    service = TaskService(db_session, queue)

    report = await service.generate(order_ids=[order.id], force_review=True)

    assert (report.queued, report.waiting_review) == (0, 1)
    task_id = report.task_ids[0]
    assert await _db_status(db_session, task_id) == TaskStatus.WAITING_REVIEW.value
    assert not await _in_queue(queue, task_id)
    assert await queue.size() == 0


# ============================================================
# integration —— 订单随建任务推进到 TASK_CREATED
# ============================================================


async def _order_status(session, order_id: int) -> str:
    return (
        await session.execute(select(Order.status).where(Order.id == order_id))
    ).scalar_one()


@pytest.mark.integration
async def test_generate_promotes_an_analyzed_order_to_task_created(
    db_session, queue, make_order
):
    """AI 路径：给一张 `ANALYZED` 的单建任务后，订单要推进到 `TASK_CREATED`。

    这一步此前全代码库都缺失 —— 订单分析完就永远停在 ANALYZED，
    「订单已进 ERP」这个状态没人写。`statuses=[ANALYZED]` 正是 AI 侧
    （worker / L3 补偿）传进来的形态。
    """
    order = await make_order(status=OrderStatus.ANALYZED.value)

    report = await TaskService(db_session, queue).generate(
        order_ids=[order.id], statuses=[OrderStatus.ANALYZED]
    )

    assert report.created == 1
    assert report.orders_promoted == 1
    assert await _order_status(db_session, order.id) == OrderStatus.TASK_CREATED.value


@pytest.mark.integration
async def test_generate_without_statuses_leaves_the_order_status_alone(
    db_session, queue, make_order
):
    """手动 Phase-3 路径（不传 statuses）捡到的是 IMPORTED 单 —— 状态保持不变。

    这是「只在 ANALYZED 时推进」的边界：否则手动建任务会把一张还没分析的单
    直接标成 TASK_CREATED，等于宣称「已分析」。
    """
    order = await make_order(status=OrderStatus.IMPORTED.value)

    report = await TaskService(db_session, queue).generate(order_ids=[order.id])

    assert report.created == 1
    assert report.orders_promoted == 0
    assert await _order_status(db_session, order.id) == OrderStatus.IMPORTED.value


@pytest.mark.integration
async def test_generate_ignores_orders_outside_the_given_statuses(
    db_session, queue, make_order
):
    """`statuses=[ANALYZED]` 时 IMPORTED 的单**不该被建任务** ——

    否则等于绕过 AI 分析、把「导入即录 ERP」接回来。
    """
    order = await make_order(status=OrderStatus.IMPORTED.value)

    report = await TaskService(db_session, queue).generate(
        order_ids=[order.id], statuses=[OrderStatus.ANALYZED]
    )

    assert report.scanned == 0
    assert report.created == 0
    assert await queue.size() == 0


# ============================================================
# integration —— 审核
# ============================================================


@pytest.mark.integration
async def test_review_approved_moves_to_queued_and_enqueues(
    db_session, queue, make_order, make_task, admin_user
):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.WAITING_REVIEW.value, need_review=True)
    service = TaskService(db_session, queue)

    result = await service.review(task.id, ReviewResult.APPROVED, "没问题", admin_user.id)

    assert result.status == TaskStatus.QUEUED.value
    assert await _db_status(db_session, task.id) == TaskStatus.QUEUED.value
    assert await _in_queue(queue, task.id)
    await db_session.refresh(task)
    assert task.review_result == ReviewResult.APPROVED.value
    assert task.review_reason == "没问题"
    assert task.reviewed_by == admin_user.id
    assert task.reviewed_at is not None
    assert task.queued_at is not None


@pytest.mark.integration
async def test_review_rejected_cancels_and_stays_out_of_the_queue(
    db_session, queue, make_order, make_task, admin_user
):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.WAITING_REVIEW.value, need_review=True)
    service = TaskService(db_session, queue)

    result = await service.review(task.id, ReviewResult.REJECTED, "地址不完整", admin_user.id)

    assert result.status == TaskStatus.CANCELLED.value
    assert await _db_status(db_session, task.id) == TaskStatus.CANCELLED.value
    assert not await _in_queue(queue, task.id)
    await db_session.refresh(task)
    assert task.finished_at is not None


@pytest.mark.integration
async def test_review_rejects_a_task_that_is_not_waiting_for_review(
    db_session, queue, make_order, make_task, admin_user
):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.QUEUED.value)
    service = TaskService(db_session, queue)

    with pytest.raises(InvalidStateError) as exc:
        await service.review(task.id, ReviewResult.APPROVED, None, admin_user.id)

    assert exc.value.code == ErrorCode.INVALID_STATE
    assert exc.value.http_status == 409
    # 状态与审核字段都必须原封不动
    assert await _db_status(db_session, task.id) == TaskStatus.QUEUED.value
    await db_session.refresh(task)
    assert task.review_result is None and task.reviewed_at is None


# ============================================================
# integration —— 重试
# ============================================================


@pytest.mark.integration
async def test_retry_failed_task_requeues_and_clears_previous_attempt(
    db_session, queue, make_order, make_task
):
    order = await make_order()
    task = await make_task(
        order,
        status=TaskStatus.FAILED.value,
        retry_count=2,
        last_error="ERP 连接超时",
        claimed_by="rpa-worker-01",
        heartbeat_at=datetime(2026, 1, 1),
        finished_at=datetime(2026, 1, 1),
    )
    service = TaskService(db_session, queue)

    result = await service.retry(task.id, reset_retry_count=True, reason="ERP 已恢复")

    assert (result.status, result.retry_count) == (TaskStatus.QUEUED.value, 0)
    assert await _db_status(db_session, task.id) == TaskStatus.QUEUED.value
    assert await _in_queue(queue, task.id)
    await db_session.refresh(task)
    assert task.retry_count == 0
    assert task.last_error is None
    assert task.finished_at is None
    # 上一轮领取的痕迹要抹掉，否则详情页会显示「被 rpa-worker-01 领取着」
    assert task.claimed_by is None
    assert task.heartbeat_at is None
    # 人工处置说明写 `cancel_reason`，不碰 `review_reason` ——
    # 后者是审核意见，两者混用会让「这行到底是审核结论还是处置原因」需要靠猜。
    assert task.cancel_reason == "ERP 已恢复"
    assert task.review_reason is None


@pytest.mark.integration
async def test_retry_without_reset_keeps_the_automatic_retry_budget(
    db_session, queue, make_order, make_task
):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.FAILED.value, retry_count=2)
    service = TaskService(db_session, queue)

    result = await service.retry(task.id, reset_retry_count=False, reason=None)

    assert result.retry_count == 2


@pytest.mark.integration
async def test_retry_only_accepts_failed_tasks(db_session, queue, make_order, make_task):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.SUCCESS.value)
    service = TaskService(db_session, queue)

    with pytest.raises(InvalidStateError) as exc:
        await service.retry(task.id, reset_retry_count=False, reason=None)

    assert exc.value.code == ErrorCode.INVALID_STATE
    assert await _db_status(db_session, task.id) == TaskStatus.SUCCESS.value


# ============================================================
# integration —— 取消
# ============================================================


@pytest.mark.integration
async def test_cancel_queued_task_also_removes_it_from_redis(
    db_session, queue, make_order, make_task
):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.QUEUED.value)
    await queue.enqueue(task.id, Priority.MEDIUM)
    service = TaskService(db_session, queue)

    result = await service.cancel(task.id, "客户改主意了")

    assert result.status == TaskStatus.CANCELLED.value
    assert await _db_status(db_session, task.id) == TaskStatus.CANCELLED.value
    assert not await _in_queue(queue, task.id)
    await db_session.refresh(task)
    assert task.finished_at is not None
    # 同上：取消原因走 `cancel_reason`，不借用审核意见那一列
    assert task.cancel_reason == "客户改主意了"
    assert task.review_reason is None


@pytest.mark.integration
async def test_cancel_waiting_review_task(db_session, queue, make_order, make_task):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.WAITING_REVIEW.value, need_review=True)
    service = TaskService(db_session, queue)

    result = await service.cancel(task.id, None)

    assert result.status == TaskStatus.CANCELLED.value
    assert not await _in_queue(queue, task.id)


@pytest.mark.integration
async def test_cancel_refuses_a_running_task(db_session, queue, make_order, make_task):
    """RUNNING 不可取消 —— RPA 正在 ERP 里操作，改我们的库只会让两边不一致。"""
    order = await make_order()
    task = await make_task(order, status=TaskStatus.RUNNING.value, claimed_by="rpa-worker-01")
    service = TaskService(db_session, queue)

    with pytest.raises(InvalidStateError) as exc:
        await service.cancel(task.id, None)

    assert "正在执行" in exc.value.message
    assert await _db_status(db_session, task.id) == TaskStatus.RUNNING.value


@pytest.mark.integration
async def test_cancel_refuses_an_already_finished_task(
    db_session, queue, make_order, make_task
):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.SUCCESS.value)
    service = TaskService(db_session, queue)

    with pytest.raises(InvalidStateError) as exc:
        await service.cancel(task.id, None)

    assert "已结束" in exc.value.message


# ============================================================
# integration —— 出队
# ============================================================


@pytest.mark.integration
async def test_pop_next_runnable_returns_a_queued_task(
    db_session, queue, make_order, make_task
):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.QUEUED.value)
    await queue.enqueue(task.id, Priority.MEDIUM)
    service = TaskService(db_session, queue)

    assert await service.pop_next_runnable() == task.id
    assert await queue.size() == 0


@pytest.mark.integration
async def test_pop_next_runnable_discards_stale_members(
    db_session, queue, make_order, make_task
):
    """Redis 不是事实来源：库里已经不该跑的任务残留在队列里时，出队必须丢掉它。"""
    base = datetime(2026, 1, 1, 12, 0, 0)

    good_order = await make_order()
    good = await make_task(good_order, status=TaskStatus.QUEUED.value)

    cancelled_order = await make_order()
    cancelled = await make_task(cancelled_order, status=TaskStatus.CANCELLED.value)

    # 用显式递增的时间戳，让出队顺序确定：不存在的 id → 已取消的 → 有效的
    await queue.enqueue(999_999, Priority.MEDIUM, base)
    await queue.enqueue(cancelled.id, Priority.MEDIUM, base + timedelta(seconds=1))
    await queue.enqueue(good.id, Priority.MEDIUM, base + timedelta(seconds=2))

    service = TaskService(db_session, queue)

    assert await service.pop_next_runnable() == good.id
    # 两条残留都被丢掉了，队列里不留垃圾
    assert await queue.size() == 0


@pytest.mark.integration
async def test_pop_next_runnable_returns_none_on_empty_queue(db_session, queue):
    assert await TaskService(db_session, queue).pop_next_runnable() is None


# ============================================================
# integration —— 队列对账
# ============================================================


@pytest.mark.integration
async def test_reconcile_rebuilds_the_queue_from_mysql(
    db_session, queue, make_order, make_task
):
    """脏队列（多一条野的）经对账后与 MySQL 完全一致。"""
    o1 = await make_order()
    queued_1 = await make_task(o1, status=TaskStatus.QUEUED.value)
    o2 = await make_order()
    queued_2 = await make_task(o2, status=TaskStatus.QUEUED.value)
    o3 = await make_order()
    await make_task(o3, status=TaskStatus.SUCCESS.value)

    await queue.enqueue(888_888, Priority.HIGH)  # 野的：MySQL 里根本没这个任务

    report = await TaskService(db_session, queue).reconcile_queue()

    assert report.db_queued == 2
    assert report.redis_after == 2
    assert set(await queue.peek(100)) == {queued_1.id, queued_2.id}
