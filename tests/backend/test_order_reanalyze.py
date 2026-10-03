"""§6.4 重新触发 AI 分析 —— 端点 + 鉴权 + 状态机。

这一条链路的要点是**顺序与事务**：

    校验 → CAS 订单退回 IMPORTED → 条件删任务 → 同一事务 commit
         → 摘任务队列 → 重入 AI 队列（Redis 在后、best-effort）

所以断言也分两层：库里的状态是真值（订单 `IMPORTED`、旧任务消失），
Redis 只是加速（AI 队列有该单、任务队列不含旧任务）。任务一旦被 RPA 执行过
（`RUNNING` 或终态）就必须拒绝 —— 撤不回已经录进 ERP 的东西。
"""

import pytest
from redis.exceptions import RedisError
from sqlalchemy import select

from app.core.enums import OrderStatus, Priority, TaskStatus
from app.models.task import Task

PREFIX = "/api/v1"


async def _task_of(db_session, order_id: int) -> Task | None:
    return await db_session.scalar(select(Task).where(Task.order_id == order_id))


# ============================================================
# 开心路径
# ============================================================


@pytest.mark.integration
async def test_reanalyze_deletes_queued_task_and_reenqueues(
    api_client, auth_headers, db_session, make_order, make_task, queue, queue_ai
):
    """最常见的场景：已建任务、已入队 —— 重分析要删旧任务、重入分析队列。"""
    order = await make_order(status=OrderStatus.TASK_CREATED.value)
    task = await make_task(order, status=TaskStatus.QUEUED.value)
    await queue.enqueue(task.id, Priority.MEDIUM)

    response = await api_client.post(
        f"{PREFIX}/orders/{order.id}/reanalyze", headers=auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"] == {"order_id": order.id, "status": OrderStatus.IMPORTED.value}

    await db_session.refresh(order)
    assert order.status == OrderStatus.IMPORTED.value
    assert await _task_of(db_session, order.id) is None
    assert order.id in await queue_ai.peek(100)
    assert task.id not in await queue.peek(100)


@pytest.mark.integration
async def test_reanalyze_order_without_task(
    api_client, auth_headers, db_session, make_order, queue_ai
):
    """`ANALYZED` 但还没建任务（分析完、流水线没跑完）也能重分析。"""
    order = await make_order(status=OrderStatus.ANALYZED.value)

    response = await api_client.post(
        f"{PREFIX}/orders/{order.id}/reanalyze", headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == OrderStatus.IMPORTED.value
    await db_session.refresh(order)
    assert order.status == OrderStatus.IMPORTED.value
    assert order.id in await queue_ai.peek(100)


@pytest.mark.integration
async def test_reanalyze_allows_waiting_review_task(
    api_client, auth_headers, db_session, make_order, make_task
):
    """待人工审核的任务同样「尚未执行」，允许撤掉重来（新分析会照原路重建）。"""
    order = await make_order(status=OrderStatus.TASK_CREATED.value)
    await make_task(order, status=TaskStatus.WAITING_REVIEW.value)

    response = await api_client.post(
        f"{PREFIX}/orders/{order.id}/reanalyze", headers=auth_headers
    )

    assert response.status_code == 200
    assert await _task_of(db_session, order.id) is None


@pytest.mark.integration
async def test_reanalyze_accepts_body_optional_and_reason(
    api_client, auth_headers, make_order
):
    """带 reason 与不带 body 都应成功 —— reason 只进日志，不影响结果。"""
    order = await make_order(status=OrderStatus.ANALYZED.value)

    response = await api_client.post(
        f"{PREFIX}/orders/{order.id}/reanalyze",
        headers=auth_headers,
        json={"reason": "AI 判断与实际不符"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == OrderStatus.IMPORTED.value


# ============================================================
# 状态机拒绝
# ============================================================


@pytest.mark.integration
@pytest.mark.parametrize("task_status", [TaskStatus.RUNNING.value, TaskStatus.SUCCESS.value])
async def test_reanalyze_rejects_executed_task(
    api_client, auth_headers, db_session, make_order, make_task, task_status
):
    """RUNNING / 终态任务已动过 ERP，必须 409 且订单与任务都原样不动。"""
    order = await make_order(status=OrderStatus.TASK_CREATED.value)
    task = await make_task(order, status=task_status)

    response = await api_client.post(
        f"{PREFIX}/orders/{order.id}/reanalyze", headers=auth_headers
    )

    assert response.status_code == 409
    assert response.json()["code"] == 4009
    await db_session.refresh(order)
    assert order.status == OrderStatus.TASK_CREATED.value
    assert await _task_of(db_session, order.id) is not None
    assert task.id  # 任务行还在


@pytest.mark.integration
async def test_reanalyze_rejects_unanalyzed_order(
    api_client, auth_headers, db_session, make_order
):
    """`IMPORTED`（还没分析过）不叫「重新」分析，直接 409。"""
    order = await make_order(status=OrderStatus.IMPORTED.value)

    response = await api_client.post(
        f"{PREFIX}/orders/{order.id}/reanalyze", headers=auth_headers
    )

    assert response.status_code == 409
    assert response.json()["code"] == 4009
    await db_session.refresh(order)
    assert order.status == OrderStatus.IMPORTED.value


@pytest.mark.integration
async def test_reanalyze_unknown_order(api_client, auth_headers):
    response = await api_client.post(
        f"{PREFIX}/orders/99999999/reanalyze", headers=auth_headers
    )

    assert response.status_code == 404
    assert response.json()["code"] == 4004


# ============================================================
# 鉴权 / 参数校验
# ============================================================


@pytest.mark.integration
async def test_reanalyze_requires_admin(api_client, worker_headers, make_order):
    """Worker 只该访问 /rpa/*，管理接口 403。"""
    order = await make_order(status=OrderStatus.ANALYZED.value)

    response = await api_client.post(
        f"{PREFIX}/orders/{order.id}/reanalyze", headers=worker_headers
    )

    assert response.status_code == 403
    assert response.json()["code"] == 4003


@pytest.mark.integration
async def test_reanalyze_reason_too_long(api_client, auth_headers, make_order):
    order = await make_order(status=OrderStatus.ANALYZED.value)

    response = await api_client.post(
        f"{PREFIX}/orders/{order.id}/reanalyze",
        headers=auth_headers,
        json={"reason": "x" * 501},
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000


# ============================================================
# Redis 不可用：库已提交，队列失败不报错
# ============================================================


@pytest.mark.integration
async def test_reanalyze_survives_redis_outage(
    api_client, auth_headers, db_session, make_order, make_task, monkeypatch
):
    """Redis 掉线时接口仍返回 200（订单已退回 IMPORTED），缺口交给补偿兜底。"""
    from app.services import order_service as order_service_module

    class _BrokenQueue:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def remove(self, *args, **kwargs) -> None:
            raise RedisError("redis down")

        async def enqueue_fifo(self, *args, **kwargs) -> None:
            raise RedisError("redis down")

    monkeypatch.setattr(order_service_module, "QueueService", _BrokenQueue)

    order = await make_order(status=OrderStatus.TASK_CREATED.value)
    await make_task(order, status=TaskStatus.QUEUED.value)

    response = await api_client.post(
        f"{PREFIX}/orders/{order.id}/reanalyze", headers=auth_headers
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == OrderStatus.IMPORTED.value
    await db_session.refresh(order)
    assert order.status == OrderStatus.IMPORTED.value
    assert await _task_of(db_session, order.id) is None
