"""端到端串联 —— 导入 → 生成任务 → 出队 → 执行 → 重试 / 审核 / 取消。

前面几个测试文件各自盯一个面：队列只看 Redis，状态机只看 service，
接口测试只看一个端点。**这个文件补的是它们之间的缝** —— Phase 3 最怕的
不是某个函数写错，而是「每一步单看都对，串起来状态对不上」。

所以这里每个阶段结束后都断言同一条不变式：

    不变式：Redis 队列里的成员集合 == MySQL 里 status='QUEUED' 的任务集合

这条不变式就是「先写库，后动队列」那个设计要维护的东西。它被破坏时
不会有任何异常抛出，只会表现为「Worker 领不到任务」或者「取消了的任务
还在跑」，等到线上才暴露。放在这里当断言，等于把它钉死在测试里。
"""

import pytest
from sqlalchemy import select

from app.core.enums import Priority, ReviewResult, TaskStatus
from app.models.task import Task
from app.services.task_service import TaskService

PREFIX = "/api/v1"

_HEADER = "订单号,下单时间,客户姓名,电话,收货地址,商品名称,SKU,数量,金额,买家留言,卖家备注"


def _csv(*order_nos: str) -> bytes:
    rows = [
        f"{no},2026-01-01 10:00:00,张三,13800001111,"
        f"北京市朝阳区某路 8 号,测试商品,SKU-001,1,299.00,,"
        for no in order_nos
    ]
    return ("\n".join([_HEADER, *rows])).encode("utf-8")


async def _assert_consistent(db_session, queue) -> None:
    """核对上面那条不变式。

    先 commit 一下再查：MySQL 默认隔离级别是 REPEATABLE READ，本会话只要
    做过一次 SELECT 就定住了快照，之后接口那头提交的改动**看不见** ——
    不结束当前事务的话，这里会拿着旧快照报假失败，而且极难查。
    我们所有写操作都是「改完立刻 commit」，所以这里 commit 不会丢东西。
    """
    await db_session.commit()

    rows = await db_session.execute(
        select(Task.id).where(Task.status == TaskStatus.QUEUED.value)
    )
    db_queued = set(rows.scalars().all())
    redis_queued = set(await queue.peek(1000))

    assert redis_queued == db_queued, (
        f"队列与数据库不一致：Redis={sorted(redis_queued)} MySQL={sorted(db_queued)}"
    )


async def _finish(db_session, task_id: int, status: TaskStatus, worker: str = "rpa-worker-01"):
    """模拟 Worker 把任务领走并跑完（Phase 4 才会有真的 claim 接口）。"""
    task = await db_session.get(Task, task_id)
    task.claimed_by = worker
    task.status = status.value
    await db_session.commit()
    return task


async def _import(api_client, headers, content: bytes):
    return await api_client.post(
        f"{PREFIX}/orders/import",
        files={"file": ("orders.csv", content, "text/csv")},
        data={"dry_run": "false"},
        headers=headers,
    )


# ============================================================
# 主链路
# ============================================================


@pytest.mark.integration
async def test_import_generate_dequeue_then_fail_and_retry(
    api_client, auth_headers, db_session, queue
):
    """一条完整的「订单进来到任务跑完」的路，中途失败再人工重试。"""
    # ---------- ① 导入：文件变订单 ----------
    imported = await _import(api_client, auth_headers, _csv("FLOW-1", "FLOW-2"))
    assert imported.json()["data"]["success_rows"] == 2

    # ---------- ② 生成任务：订单变待执行的活 ----------
    service = TaskService(db_session, queue)
    report = await service.generate()
    assert (report.created, report.queued) == (2, 2)
    assert await queue.size() == 2
    await _assert_consistent(db_session, queue)

    # ---------- ③ 出队：Worker 领走第一张 ----------
    first_id = await service.pop_next_runnable()
    assert first_id is not None
    await _finish(db_session, first_id, TaskStatus.RUNNING)
    # 领走之后它不再是 QUEUED，两边仍然一致
    await _assert_consistent(db_session, queue)
    assert await queue.size() == 1

    # ---------- ④ 执行成功 ----------
    await _finish(db_session, first_id, TaskStatus.SUCCESS)
    await _assert_consistent(db_session, queue)

    # ---------- ⑤ 第二张失败 ----------
    second_id = await service.pop_next_runnable()
    assert second_id is not None and second_id != first_id
    await _finish(db_session, second_id, TaskStatus.FAILED)
    await _assert_consistent(db_session, queue)
    assert await queue.size() == 0  # 队列空了，但失败的那张还在等人工处理

    # ---------- ⑥ 人工重试：重新入队 ----------
    retried = await api_client.post(
        f"{PREFIX}/tasks/{second_id}/retry",
        json={"reset_retry_count": True, "reason": "ERP 已恢复"},
        headers=auth_headers,
    )
    assert retried.status_code == 200
    assert retried.json()["data"]["status"] == TaskStatus.QUEUED.value
    await _assert_consistent(db_session, queue)
    assert await service.pop_next_runnable() == second_id

    # ---------- ⑦ 这单不要了，取消掉 ----------
    cancelled = await api_client.post(
        f"{PREFIX}/tasks/{second_id}/cancel",
        json={"reason": "客户已退款"},
        headers=auth_headers,
    )
    assert cancelled.status_code == 200
    await _assert_consistent(db_session, queue)
    assert await queue.size() == 0


# ============================================================
# 人工审核那条支路
# ============================================================


@pytest.mark.integration
async def test_force_review_then_approve_unblocks_the_queue(
    api_client, auth_headers, db_session, queue
):
    """需要审核的任务不进队列，批准之后才入队 —— 这是 §9.3 的意义所在。"""
    await _import(api_client, auth_headers, _csv("REV-1"))

    service = TaskService(db_session, queue)
    report = await service.generate(force_review=True)
    assert (report.queued, report.waiting_review) == (0, 1)
    assert await queue.size() == 0
    await _assert_consistent(db_session, queue)

    task_id = report.task_ids[0]
    approved = await api_client.post(
        f"{PREFIX}/tasks/{task_id}/review",
        json={"result": ReviewResult.APPROVED.value, "reason": "核对无误"},
        headers=auth_headers,
    )
    assert approved.status_code == 200
    assert await queue.size() == 1
    await _assert_consistent(db_session, queue)

    assert await service.pop_next_runnable() == task_id


@pytest.mark.integration
async def test_force_review_then_reject_keeps_it_out_of_the_queue(
    api_client, auth_headers, db_session, queue
):
    await _import(api_client, auth_headers, _csv("REJ-1"))

    service = TaskService(db_session, queue)
    report = await service.generate(force_review=True)
    task_id = report.task_ids[0]

    rejected = await api_client.post(
        f"{PREFIX}/tasks/{task_id}/review",
        json={"result": ReviewResult.REJECTED.value, "reason": "地址不完整"},
        headers=auth_headers,
    )

    assert rejected.status_code == 200
    assert rejected.json()["data"]["status"] == TaskStatus.CANCELLED.value
    await _assert_consistent(db_session, queue)
    assert await service.pop_next_runnable() is None


# ============================================================
# 灾后重建
# ============================================================


@pytest.mark.integration
async def test_reconcile_restores_a_wiped_redis(db_session, queue, make_order, make_task):
    """Redis 被 flush 之后，照 MySQL 重建即可恢复 —— 因为它从来不是事实来源。"""
    for _ in range(2):
        order = await make_order()
        task = await make_task(
            order, status=TaskStatus.QUEUED.value, priority=Priority.HIGH.value
        )
        # make_task 只写 MySQL；入队是 QueueService 的事，这里手动补上，
        # 让初始状态满足「Redis == MySQL 里 QUEUED 的那些」这条不变式。
        await queue.enqueue(task.id, Priority.HIGH)

    assert await queue.size() == 2

    # 模拟 Redis 重启 / 被 flush
    await queue.rebuild([])
    assert await queue.size() == 0

    report = await TaskService(db_session, queue).reconcile_queue()

    assert (report.db_queued, report.redis_after) == (2, 2)
    await _assert_consistent(db_session, queue)
