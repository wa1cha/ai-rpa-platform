"""AI Worker 的一轮 —— `workers/ai_worker.py` 的 `analyze_once`。

这一层是整条链路的**收口**：出队 → 分析 → 建任务 → 订单到 `TASK_CREATED`。
前面各层（service / rule_engine / task_service.generate）都有自己的单测，
这里只验「它们被正确地串起来了」—— 尤其是「分析完顺手建任务」这一步，
它同时推进了订单状态（ANALYZED → TASK_CREATED）和任务队列。

注入 `fake_analyzer`，不联网；但连真库 + 真 Redis，因为要验的正是
「队列被排空、库里的状态与任务都对」。`analyze_once` 自己开 session，
所以断言统一走 `db_session` 重新查库，不看内存里的对象。
"""

from datetime import datetime

import pytest
from sqlalchemy import func, select

from ai.llm.base import LLMError
from app.core.config import settings
from app.core.enums import OrderStatus, TaskStatus
from app.models.ai_analysis import AiAnalysis
from app.models.task import Task
from app.repositories.task_repository import TaskRepository
from app.workers.ai_worker import analyze_once

pytestmark = pytest.mark.integration


async def _status(session, order_id: int) -> str:
    from app.models.order import Order

    return (
        await session.execute(select(Order.status).where(Order.id == order_id))
    ).scalar_one()


# ============================================================
# 正常一轮
# ============================================================


async def test_analyze_once_drains_the_queue_and_creates_tasks(
    db_session, queue, queue_ai, make_order, fake_analyzer
):
    """出队 → 分析 → 建任务 → 订单 TASK_CREATED、任务入队。整条链一次跑通。"""
    order = await make_order()
    await queue_ai.enqueue_fifo(order.id, datetime.now())

    report = await analyze_once(analyzer=fake_analyzer)

    assert report.popped == 1
    assert report.analyzed == 1
    assert report.failed == 0
    assert report.skipped == 0
    assert report.tasks_created == 1

    # 队列已被排空
    assert await queue_ai.size() == 0
    # 订单推进到了 TASK_CREATED（分析完顺手建任务，两个状态一次到位）
    assert await _status(db_session, order.id) == OrderStatus.TASK_CREATED.value

    task = await TaskRepository(db_session).get_by_order_id(order.id)
    assert task is not None
    assert task.status == TaskStatus.QUEUED.value
    # 任务也进了任务队列 —— 否则订单推进了、RPA 却永远领不到
    assert task.id in await queue.peek(100)


async def test_analyze_once_handles_a_whole_batch(
    db_session, queue_ai, make_order, fake_analyzer
):
    """一批多单一起出队、并发分析，每单都建成任务。"""
    orders = [await make_order() for _ in range(3)]
    for order in orders:
        await queue_ai.enqueue_fifo(order.id, datetime.now())

    report = await analyze_once(analyzer=fake_analyzer)

    assert (report.popped, report.analyzed, report.tasks_created) == (3, 3, 3)
    for order in orders:
        assert await _status(db_session, order.id) == OrderStatus.TASK_CREATED.value


async def test_a_blacklisted_order_goes_to_review_instead_of_the_queue(
    db_session, queue, queue_ai, make_order, fake_analyzer
):
    """黑名单命中 → 硬规则升到 HIGH → 任务进 WAITING_REVIEW、**不入队**。

    这是「硬规则真的影响了最终出口」的端到端证据：fake 永远返回 LOW，
    所以进审核只可能是硬规则干的。也顺带验了「订单推进到 TASK_CREATED」
    与「任务是否入队」是两件事。
    """
    from app.models.customer_blacklist import CustomerBlacklist

    db_session.add(CustomerBlacklist(phone="13800007777", is_active=True))
    await db_session.commit()
    order = await make_order(phone="13800007777")
    await queue_ai.enqueue_fifo(order.id, datetime.now())

    report = await analyze_once(analyzer=fake_analyzer)

    assert report.tasks_created == 1
    task = await TaskRepository(db_session).get_by_order_id(order.id)
    assert task.status == TaskStatus.WAITING_REVIEW.value
    assert task.id not in await queue.peek(100)
    assert await _status(db_session, order.id) == OrderStatus.TASK_CREATED.value


# ============================================================
# LLM 失败也要闭环
# ============================================================


async def test_a_llm_failure_still_produces_a_task(
    db_session, queue_ai, make_order, fake_analyzer
):
    """LLM 挂了：报告里计 failed，但订单照样推进、任务照样建 —— 闭环不断。

    失败兜底给的是 risk_level=MEDIUM，按 `_decide` 会转人工审核，
    于是任务是 WAITING_REVIEW。这正好说明兜底「保守而不是放行」。
    """
    fake_analyzer.error = LLMError("上游不可用")
    order = await make_order()
    await queue_ai.enqueue_fifo(order.id, datetime.now())

    report = await analyze_once(analyzer=fake_analyzer)

    assert report.analyzed == 1
    assert report.failed == 1
    assert report.tasks_created == 1

    analysis = (
        await db_session.execute(
            select(AiAnalysis).where(AiAnalysis.order_id == order.id)
        )
    ).scalar_one()
    assert analysis.risk_level == "MEDIUM"
    task = await TaskRepository(db_session).get_by_order_id(order.id)
    assert task.status == TaskStatus.WAITING_REVIEW.value


# ============================================================
# 边界
# ============================================================


async def test_analyze_once_skips_a_stale_queue_member(
    db_session, queue_ai, make_order, fake_analyzer
):
    """队列里残留了一个早已 ANALYZED 的 order → CAS 拿不到 → 计 skipped，不重复分析。"""
    order = await make_order(status=OrderStatus.ANALYZED.value)
    await queue_ai.enqueue_fifo(order.id, datetime.now())

    report = await analyze_once(analyzer=fake_analyzer)

    assert report.popped == 1
    assert report.skipped == 1
    assert report.analyzed == 0
    total = (
        await db_session.execute(select(func.count()).select_from(AiAnalysis))
    ).scalar_one()
    assert total == 0


async def test_analyze_once_on_an_empty_queue_is_a_noop(db_session, queue_ai, fake_analyzer):
    report = await analyze_once(analyzer=fake_analyzer)

    assert report.popped == 0
    assert report.analyzed == 0
    assert fake_analyzer.calls == []


async def test_analyze_once_without_an_api_key_does_nothing(
    db_session, queue_ai, make_order, monkeypatch
):
    """没配 `AI_API_KEY` 时**不崩作业循环**：记 warning、空跑一轮。

    队列里的单原样留着（等哪天配上 key 再处理），订单状态一动不动。
    """
    monkeypatch.setattr(settings, "ai_api_key", "")
    order = await make_order()
    await queue_ai.enqueue_fifo(order.id, datetime.now())

    report = await analyze_once()

    assert report.popped == 0
    assert await queue_ai.size() == 1                     # 没被动过
    assert await _status(db_session, order.id) == OrderStatus.IMPORTED.value
    assert (
        await db_session.execute(select(func.count()).select_from(Task))
    ).scalar_one() == 0
