"""§10 RPA Worker 接口测试 —— 领活 / 心跳 / 回传 / 传图 + 角色鉴权。

和 `test_tasks.py` 一样走 `httpx.ASGITransport`，理由也相同：要测的是路由、
依赖注入（尤其是 `require_worker`）和序列化，不是网络栈。

这一组用例的重点是**贯穿全篇的那条不变式**：`retry_count + 1 == attempt`。
每次领活/回传之后都回头查一次库，确认任务状态、执行记录、队列三者互相对得上 ——
接口返回值对、但库里写歪了，才是这类系统最难查的 bug。
"""

from datetime import datetime

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.enums import OrderStatus, Priority, TaskStatus
from app.models.notification import Notification
from app.models.task_execution import TaskExecution

PREFIX = "/api/v1"
WORKER = "rpa-worker-01"


async def _executions(db_session, task_id: int) -> list[TaskExecution]:
    """按 attempt 升序读回某任务的执行记录（绕过接口，直接查库）。

    `populate_existing=True` 不能省：`_running_task` 造执行记录时用的是**同一个**
    session，实例已在 identity map 里。接口那边改的是另一个 session 连接上的一行，
    这里再 SELECT 时 SQLAlchemy 默认会**直接返回缓存的那个实例**、不覆盖属性 ——
    于是读到的永远是「接口改之前」的样子。强制重新填充才看得到真实落库的值。
    """
    stmt = (
        select(TaskExecution)
        .where(TaskExecution.task_id == task_id)
        .order_by(TaskExecution.attempt.asc())
        .execution_options(populate_existing=True)
    )
    return list(await db_session.scalars(stmt))


async def _notification_for(db_session, task_id: int) -> Notification | None:
    return await db_session.scalar(
        select(Notification).where(Notification.related_task_id == task_id)
    )


async def _running_task(
    db_session, make_order, make_task, *, worker_name: str = WORKER, **task_overrides
):
    """造一个「正被 worker_name 领走」的任务，连带它这一次的执行记录。

    领活成功之后库里的样子就是这个 —— 回传/心跳的用例直接从这个状态起步，
    不必先真的走一遍 claim（那是 claim 用例自己的事）。
    """
    order = await make_order()
    # 先铺默认值再 update 覆盖，而不是把 heartbeat_at 直接写死成关键字 ——
    # 用例想指定一个旧心跳时，两种写法会撞成「重复关键字参数」的 TypeError。
    fields = {
        "status": TaskStatus.RUNNING.value,
        "claimed_by": worker_name,
        "heartbeat_at": datetime.now(),
    }
    fields.update(task_overrides)
    task = await make_task(order, **fields)
    execution = TaskExecution(
        task_id=task.id,
        attempt=task.retry_count + 1,
        status="RUNNING",
        worker_name=worker_name,
    )
    db_session.add(execution)
    await db_session.commit()
    return order, task, execution


# ============================================================
# 角色鉴权
# ============================================================


@pytest.mark.integration
async def test_rpa_endpoints_require_authentication(api_client):
    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/claim", json={"worker_name": WORKER}
    )

    assert response.status_code == 401
    assert response.json()["code"] == 4001


@pytest.mark.integration
async def test_rpa_endpoints_reject_the_admin_role(api_client, auth_headers):
    """管理员来调 /rpa/* 同样 403 —— 让非 Worker 走这条线等于允许伪造执行者身份。"""
    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/claim",
        json={"worker_name": WORKER},
        headers=auth_headers,
    )

    assert response.status_code == 403
    assert response.json()["code"] == 4003


# ============================================================
# §10.1 领取任务
# ============================================================


@pytest.mark.integration
async def test_claim_returns_null_when_the_queue_is_empty(api_client, worker_headers):
    """没活干是 `data: null`，不是 404 —— Worker 端只要一条解析路径。"""
    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/claim",
        json={"worker_name": WORKER, "wait_seconds": 0},
        headers=worker_headers,
    )

    assert response.status_code == 200
    assert response.json() == {"code": 0, "message": "ok", "data": None}


@pytest.mark.integration
async def test_claim_marks_the_task_running_and_opens_an_execution(
    api_client, worker_headers, queue, db_session, make_order, make_task
):
    order = await make_order(customer_name="张三", buyer_message="今天能发货吗")
    task = await make_task(order, status=TaskStatus.QUEUED.value, priority=Priority.HIGH.value)
    await queue.enqueue(task.id, Priority.HIGH)

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/claim",
        json={"worker_name": WORKER, "wait_seconds": 0},
        headers=worker_headers,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["task"] == {
        "id": task.id,
        "priority": "HIGH",
        "attempt": 1,
        "need_review": False,
    }
    # Worker 是拿着这份数据去填 ERP 的，所以订单字段必须完整、手机号不脱敏
    assert data["order"]["order_no"] == order.order_no
    assert data["order"]["customer_name"] == "张三"
    assert data["order"]["phone"] == order.phone
    assert data["order"]["buyer_message"] == "今天能发货吗"
    assert data["order"]["amount"] == "99.00"
    assert data["instruction"]["erp_url"] == settings.mock_erp_base_url
    assert task.id not in await queue.peek(100)

    await db_session.refresh(task)
    assert task.status == TaskStatus.RUNNING.value
    assert task.claimed_by == WORKER
    assert task.heartbeat_at is not None

    executions = await _executions(db_session, task.id)
    assert [(e.attempt, e.status, e.worker_name) for e in executions] == [
        (1, "RUNNING", WORKER)
    ]


@pytest.mark.integration
async def test_claim_counts_attempt_as_retry_count_plus_one(
    api_client, worker_headers, queue, db_session, make_order, make_task
):
    """重试过的任务再被领走时，attempt 必须是 2，而不是又开一条 1。"""
    order = await make_order()
    task = await make_task(
        order, status=TaskStatus.QUEUED.value, retry_count=1, last_error="上一轮超时"
    )
    await queue.enqueue(task.id, Priority.MEDIUM)

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/claim", json={"worker_name": WORKER}, headers=worker_headers
    )

    assert response.json()["data"]["task"]["attempt"] == 2
    await db_session.refresh(task)
    # 上一轮的失败原因不该跟着新一次执行一起露在列表页上
    assert task.last_error is None
    assert [e.attempt for e in await _executions(db_session, task.id)] == [2]


@pytest.mark.integration
async def test_claim_long_poll_times_out_and_returns_null(api_client, worker_headers):
    """`wait_seconds > 0` 但一直没活时，等满就回 null，而不是挂死。"""
    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/claim",
        json={"worker_name": WORKER, "wait_seconds": 1},
        headers=worker_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"] is None


@pytest.mark.integration
async def test_claim_rejects_wait_seconds_above_the_cap(api_client, worker_headers):
    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/claim",
        json={"worker_name": WORKER, "wait_seconds": 31},
        headers=worker_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000


# ============================================================
# §10.2 心跳
# ============================================================


@pytest.mark.integration
async def test_heartbeat_refreshes_the_timestamp(
    api_client, worker_headers, db_session, make_order, make_task
):
    _, task, _ = await _running_task(
        db_session, make_order, make_task, heartbeat_at=datetime(2026, 1, 1)
    )

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/{task.id}/heartbeat",
        json={"worker_name": WORKER},
        headers=worker_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "task_id": task.id,
        "status": "RUNNING",
        "cancel": False,
    }
    await db_session.refresh(task)
    assert task.heartbeat_at > datetime(2026, 1, 1)


@pytest.mark.integration
async def test_heartbeat_tells_a_displaced_worker_to_stop(
    api_client, worker_headers, db_session, make_order, make_task
):
    """任务已被回收、并被**另一个** Worker 领走时，原来那个必须停手。

    这就是为什么心跳不只看状态、还要看 `claimed_by` —— 只看状态的话，
    「RUNNING 但换了人」会被误判成「一切正常」，两个 Worker 同时操作同一张单。
    """
    _, task, _ = await _running_task(
        db_session, make_order, make_task, worker_name="rpa-worker-99"
    )

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/{task.id}/heartbeat",
        json={"worker_name": WORKER},
        headers=worker_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["cancel"] is True
    assert response.json()["data"]["status"] == "RUNNING"


@pytest.mark.integration
async def test_heartbeat_on_a_vanished_task_asks_the_worker_to_stop(
    api_client, worker_headers
):
    """任务没了也要回 200 + cancel，而不是 404。

    心跳是高频、无人值守的调用；抛错的话 Worker 端的自然反应是「重试」，
    而这里要的是「立刻停手」。
    """
    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/999999/heartbeat",
        json={"worker_name": WORKER},
        headers=worker_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "task_id": 999999,
        "status": "MISSING",
        "cancel": True,
    }


# ============================================================
# §10.3 回传结果
# ============================================================


@pytest.mark.integration
async def test_result_success_completes_task_and_order(
    api_client, worker_headers, db_session, make_order, make_task
):
    order, task, _ = await _running_task(db_session, make_order, make_task)

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/{task.id}/result",
        json={
            "worker_name": WORKER,
            "success": True,
            "erp_order_no": "ERP20261002001",
            "duration_ms": 18400,
        },
        headers=worker_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "task_id": task.id,
        "status": "SUCCESS",
        "retry_scheduled": False,
        "retry_count": None,
    }

    await db_session.refresh(task)
    assert task.status == TaskStatus.SUCCESS.value
    assert task.finished_at is not None
    # 完成的任务不该再显示「被某个 Worker 领着」
    assert task.claimed_by is None
    assert task.heartbeat_at is None

    await db_session.refresh(order)
    assert order.status == OrderStatus.COMPLETED.value

    executions = await _executions(db_session, task.id)
    assert executions[0].status == "SUCCESS"
    assert executions[0].erp_order_no == "ERP20261002001"
    assert executions[0].duration_ms == 18400
    assert executions[0].finished_at is not None


@pytest.mark.integration
async def test_result_success_without_an_erp_order_no_is_rejected(
    api_client, worker_headers, db_session, make_order, make_task
):
    """成功必须带 ERP 单号 —— 它是主库与 ERP 之间唯一的对账凭据。"""
    _, task, _ = await _running_task(db_session, make_order, make_task)

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/{task.id}/result",
        json={"worker_name": WORKER, "success": True},
        headers=worker_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000
    assert "erp_order_no" in response.json()["message"]


@pytest.mark.integration
async def test_result_failure_within_budget_requeues_the_task(
    api_client, worker_headers, queue, db_session, make_order, make_task
):
    order, task, _ = await _running_task(db_session, make_order, make_task)

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/{task.id}/result",
        json={
            "worker_name": WORKER,
            "success": False,
            "error_code": "ERP_SUBMIT_FAILED",
            "error_message": "收货地址超出配送范围",
            "duration_ms": 25000,
        },
        headers=worker_headers,
    )

    assert response.json()["data"] == {
        "task_id": task.id,
        "status": "QUEUED",
        "retry_scheduled": True,
        "retry_count": 1,
    }

    await db_session.refresh(task)
    assert task.status == TaskStatus.QUEUED.value
    assert task.retry_count == 1
    assert task.last_error == "收货地址超出配送范围"
    assert task.claimed_by is None
    # 先写库、后入队：队列里必须真有它，否则这次重试就白记了
    assert task.id in await queue.peek(100)

    await db_session.refresh(order)
    # 还够重试，订单不该被标成失败
    assert order.status != OrderStatus.FAILED.value

    executions = await _executions(db_session, task.id)
    assert executions[0].status == "FAILED"
    assert executions[0].error_code == "ERP_SUBMIT_FAILED"


@pytest.mark.integration
async def test_result_failure_over_budget_fails_the_task_and_notifies(
    api_client, worker_headers, queue, db_session, make_order, make_task
):
    """额度用尽 → 终态 FAILED，订单同失败，并落一条 TASK_FAILED 通知。"""
    order, task, _ = await _running_task(
        db_session, make_order, make_task, retry_count=3, max_retry=3
    )

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/{task.id}/result",
        json={
            "worker_name": WORKER,
            "success": False,
            "error_code": "ERP_LOGIN_FAILED",
            "error_message": "ERP 登录失败",
        },
        headers=worker_headers,
    )

    assert response.json()["data"] == {
        "task_id": task.id,
        "status": "FAILED",
        "retry_scheduled": False,
        "retry_count": 3,
    }

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED.value
    assert task.finished_at is not None
    assert task.retry_count == 3  # 额度用尽，不再 +1
    assert task.id not in await queue.peek(100)

    await db_session.refresh(order)
    assert order.status == OrderStatus.FAILED.value

    notification = await _notification_for(db_session, task.id)
    assert notification is not None
    assert notification.type == "TASK_FAILED"
    assert notification.status == "SENT"
    assert notification.content == "ERP 登录失败"


@pytest.mark.integration
async def test_result_from_a_worker_that_does_not_own_the_task_is_rejected(
    api_client, worker_headers, db_session, make_order, make_task
):
    _, task, _ = await _running_task(
        db_session, make_order, make_task, worker_name="rpa-worker-99"
    )

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/{task.id}/result",
        json={"worker_name": WORKER, "success": True, "erp_order_no": "ERP-X"},
        headers=worker_headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == 4009


@pytest.mark.integration
async def test_result_on_a_task_that_is_not_running_is_rejected(
    api_client, worker_headers, make_order, make_task
):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.QUEUED.value)

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/{task.id}/result",
        json={"worker_name": WORKER, "success": True, "erp_order_no": "ERP-X"},
        headers=worker_headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == 4009
    assert "QUEUED" in response.json()["message"]


# ============================================================
# §10.4 上传失败截图
# ============================================================


@pytest.mark.integration
async def test_screenshot_upload_stores_the_file_outside_the_webroot(
    api_client, worker_headers, db_session, make_order, make_task, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "screenshot_dir", tmp_path)
    _, task, execution = await _running_task(db_session, make_order, make_task)

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/{task.id}/screenshot",
        files={"file": ("fail.png", b"\x89PNG-fake-bytes", "image/png")},
        data={"attempt": "1"},
        headers=worker_headers,
    )

    assert response.status_code == 200
    # 返回的是**取图接口路径**，不是静态 URL —— 截图含客户明文信息，要鉴权
    expected = f"{PREFIX}/tasks/{task.id}/executions/{execution.id}/screenshot"
    assert response.json()["data"] == {"path": expected}

    await db_session.refresh(execution)
    # 库里只存文件名，不存绝对路径 —— 换部署目录时那些路径全会失效
    assert execution.screenshot_path == f"{execution.id}.png"
    assert (tmp_path / f"{execution.id}.png").read_bytes() == b"\x89PNG-fake-bytes"


@pytest.mark.integration
async def test_screenshot_upload_rejects_an_unknown_extension(
    api_client, worker_headers, db_session, make_order, make_task, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "screenshot_dir", tmp_path)
    _, task, _ = await _running_task(db_session, make_order, make_task)

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/{task.id}/screenshot",
        files={"file": ("payload.sh", b"#!/bin/sh", "text/plain")},
        data={"attempt": "1"},
        headers=worker_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000
    assert ".sh" in response.json()["message"]


@pytest.mark.integration
async def test_screenshot_upload_rejects_an_empty_file(
    api_client, worker_headers, db_session, make_order, make_task, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "screenshot_dir", tmp_path)
    _, task, _ = await _running_task(db_session, make_order, make_task)

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/{task.id}/screenshot",
        files={"file": ("fail.png", b"", "image/png")},
        data={"attempt": "1"},
        headers=worker_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000


@pytest.mark.integration
async def test_screenshot_upload_rejects_an_attempt_without_execution(
    api_client, worker_headers, db_session, make_order, make_task, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "screenshot_dir", tmp_path)
    _, task, _ = await _running_task(db_session, make_order, make_task)

    response = await api_client.post(
        f"{PREFIX}/rpa/tasks/{task.id}/screenshot",
        files={"file": ("fail.png", b"x", "image/png")},
        data={"attempt": "7"},
        headers=worker_headers,
    )

    assert response.status_code == 404
    assert response.json()["code"] == 4004
