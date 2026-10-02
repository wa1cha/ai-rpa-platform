"""僵尸任务回收测试 —— 见《数据库设计》§6.1。

分两层：
  · `stale_cutoff()` / `build_jobs()` 是纯逻辑，标 `unit`，不碰任何外部服务；
  · `reap_once()` 真连 MySQL 与 Redis，标 `integration`。

回收逻辑和回传结果的失败路径**同构**（`retry_count + 1` 再决定入队还是终态），
所以这里不只断言状态，也断言那条执行记录被写成 `WORKER_LOST` ——
「ERP 拒绝了我」和「Worker 根本没回话」必须是两种能分开统计的故障。
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.enums import OrderStatus, Priority, TaskStatus
from app.models.notification import Notification
from app.models.task_execution import TaskExecution
from app.workers.main import build_jobs
from app.workers.zombie_reaper import reap_once, stale_cutoff


# ============================================================
# 纯逻辑
# ============================================================


@pytest.mark.unit
def test_stale_cutoff_is_now_minus_the_configured_timeout():
    now = datetime(2026, 10, 2, 12, 0, 0)

    cutoff = stale_cutoff(now)

    assert cutoff == now - timedelta(seconds=settings.zombie_timeout_seconds)


@pytest.mark.unit
def test_worker_process_hosts_the_zombie_reaper():
    """作业进程得真的把回收挂上 —— 否则「写了回收但没人跑」是静默失效。"""
    jobs = {job.name: job for job in build_jobs()}

    assert "zombie_reaper" in jobs
    assert jobs["zombie_reaper"].interval_seconds == settings.zombie_scan_interval_seconds


# ============================================================
# 真库回收
# ============================================================


async def _stale_running_task(db_session, make_order, make_task, **task_overrides):
    """造一个 RUNNING、心跳停在很久以前的任务，连带它的执行记录。"""
    order = await make_order()
    task = await make_task(
        order,
        status=TaskStatus.RUNNING.value,
        claimed_by="rpa-worker-01",
        heartbeat_at=datetime(2026, 1, 1),
        **task_overrides,
    )
    execution = TaskExecution(
        task_id=task.id,
        attempt=task.retry_count + 1,
        status="RUNNING",
        worker_name="rpa-worker-01",
    )
    db_session.add(execution)
    await db_session.commit()
    return order, task, execution


@pytest.mark.integration
async def test_reap_requeues_a_task_whose_worker_vanished(
    db_session, queue, make_order, make_task
):
    order, task, execution = await _stale_running_task(db_session, make_order, make_task)

    report = await reap_once()

    assert (report.scanned, report.requeued, report.failed) == (1, 1, 0)
    assert report.task_ids == [task.id]

    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED.value
    assert task.retry_count == 1  # +1，和回传失败走的是同一条规则
    assert task.claimed_by is None
    assert task.heartbeat_at is None
    assert task.finished_at is None
    assert task.id in await queue.peek(100)

    await db_session.refresh(execution)
    assert execution.status == "FAILED"
    assert execution.error_code == "WORKER_LOST"
    assert execution.finished_at is not None

    await db_session.refresh(order)
    assert order.status != OrderStatus.FAILED.value  # 还够重试


@pytest.mark.integration
async def test_reap_fails_the_task_when_the_retry_budget_is_gone(
    db_session, queue, make_order, make_task
):
    order, task, execution = await _stale_running_task(
        db_session, make_order, make_task, retry_count=3, max_retry=3
    )

    report = await reap_once()

    assert (report.scanned, report.requeued, report.failed) == (1, 0, 1)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED.value
    assert task.retry_count == 3
    assert task.finished_at is not None
    assert task.id not in await queue.peek(100)

    await db_session.refresh(order)
    assert order.status == OrderStatus.FAILED.value

    notification = await db_session.scalar(
        select(Notification).where(Notification.related_task_id == task.id)
    )
    assert notification is not None
    assert notification.type == "TASK_FAILED"
    assert "失联" in notification.content


@pytest.mark.integration
async def test_reap_leaves_a_healthy_running_task_alone(
    db_session, queue, make_order, make_task
):
    """心跳是刚刚刷过的，不算僵尸 —— 误收会打断一个正常干活的 Worker。"""
    order = await make_order()
    task = await make_task(
        order,
        status=TaskStatus.RUNNING.value,
        claimed_by="rpa-worker-01",
        heartbeat_at=datetime.now(),
    )
    await db_session.commit()

    report = await reap_once()

    assert report.scanned == 0

    await db_session.refresh(task)
    assert task.status == TaskStatus.RUNNING.value
    assert task.claimed_by == "rpa-worker-01"


@pytest.mark.integration
async def test_reap_collects_a_running_task_with_no_heartbeat_at_all(
    db_session, queue, make_order, make_task
):
    """心跳为 NULL 也算僵尸。

    正常路径上 claim 一定写 heartbeat_at，出现 NULL 说明这行是别的途径
    置成 RUNNING 的 —— 它永远等不到心跳，不收就会一直卡着。
    """
    order = await make_order()
    task = await make_task(
        order,
        status=TaskStatus.RUNNING.value,
        claimed_by="rpa-worker-01",
        heartbeat_at=None,
    )
    await db_session.commit()

    report = await reap_once()

    assert (report.scanned, report.requeued) == (1, 1)
    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED.value


@pytest.mark.integration
async def test_reap_requeues_with_the_original_priority(
    db_session, queue, make_order, make_task
):
    """回收后重新入队要保持原优先级，否则 HIGH 的单子被降级到队尾。"""
    _, task, _ = await _stale_running_task(
        db_session, make_order, make_task, priority=Priority.HIGH.value
    )

    await reap_once()

    # HIGH 的 score 段最小，排在最前 —— peek 的第一个就是它
    assert (await queue.peek(100))[0] == task.id
