"""§9 任务接口测试 —— 六个端点 + 鉴权。

用 `httpx.ASGITransport` 把请求直接喂给 FastAPI app（见 conftest 的 `api_client`）：
不经过网络，但路由匹配、依赖注入、Pydantic 序列化、全局异常处理器全都真跑一遍 ——
而这些正是接口层最容易出错的地方，所以不能只测 service。

断言一律看两样东西：HTTP 状态码（传输/鉴权层）和响应体里的 `code`（业务层）。
两者是两套语义，见《API接口设计》§2/§3，只对其中一个下断言会漏掉另一套的回归。
"""

from datetime import datetime

import pytest

from app.core.enums import Priority, ReviewResult, TaskStatus
from app.models.task_execution import TaskExecution

PREFIX = "/api/v1"


async def _add_execution(db_session, task_id: int, attempt: int, **overrides) -> TaskExecution:
    data = {
        "task_id": task_id,
        "attempt": attempt,
        "status": "SUCCESS",
        "worker_name": "rpa-worker-01",
    }
    data.update(overrides)
    execution = TaskExecution(**data)
    db_session.add(execution)
    await db_session.commit()
    return execution


# ============================================================
# 鉴权（§5 登录 + §9 端点的准入）
# ============================================================


@pytest.mark.integration
async def test_task_endpoints_require_authentication(api_client):
    """没带 token 必须是 401 而不是 403 —— 前端靠这个码决定跳不跳登录页。"""
    response = await api_client.get(f"{PREFIX}/tasks")

    assert response.status_code == 401
    assert response.json()["code"] == 4001


@pytest.mark.integration
async def test_task_endpoints_reject_the_worker_role(api_client, worker_headers):
    """Worker 跑在另一台机器上，只该访问 /rpa/*，管理接口一律 403。"""
    response = await api_client.get(f"{PREFIX}/tasks", headers=worker_headers)

    assert response.status_code == 403
    assert response.json()["code"] == 4003


@pytest.mark.integration
async def test_login_then_me_roundtrip(api_client, admin_user, admin_credentials):
    login = await api_client.post(f"{PREFIX}/auth/login", json=admin_credentials)
    assert login.status_code == 200
    token = login.json()["data"]["access_token"]
    assert login.json()["data"]["user"]["username"] == "t_admin"

    me = await api_client.get(
        f"{PREFIX}/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert me.status_code == 200
    assert me.json()["data"]["username"] == "t_admin"
    assert me.json()["data"]["role"] == "ADMIN"


@pytest.mark.integration
async def test_login_rejects_a_wrong_password(api_client, admin_user, admin_credentials):
    """错误口令返回 4002，且提示语与「用户不存在」完全一致（防用户名枚举）。"""
    response = await api_client.post(
        f"{PREFIX}/auth/login",
        json={**admin_credentials, "password": "definitely-wrong"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == 4002


# ============================================================
# §9.1 列表
# ============================================================


@pytest.mark.integration
async def test_list_returns_tasks_with_their_order(
    api_client, auth_headers, make_order, make_task
):
    order = await make_order(customer_name="张三")
    task = await make_task(order, status=TaskStatus.QUEUED.value)

    response = await api_client.get(f"{PREFIX}/tasks", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 1
    item = body["data"]["items"][0]
    assert item["id"] == task.id
    assert item["order_no"] == order.order_no
    assert item["customer_name"] == "张三"
    # 列表刻意不带地址和买家留言（省带宽），也不该出现
    assert "address" not in item


@pytest.mark.integration
async def test_list_filters_by_status(api_client, auth_headers, make_order, make_task):
    queued_order = await make_order()
    await make_task(queued_order, status=TaskStatus.QUEUED.value)
    failed_order = await make_order()
    await make_task(failed_order, status=TaskStatus.FAILED.value)

    response = await api_client.get(
        f"{PREFIX}/tasks", params={"status": "FAILED"}, headers=auth_headers
    )

    items = response.json()["data"]["items"]
    assert [i["status"] for i in items] == ["FAILED"]


@pytest.mark.integration
async def test_list_filters_by_priority(api_client, auth_headers, make_order, make_task):
    high_order = await make_order()
    await make_task(high_order, priority=Priority.HIGH.value)
    low_order = await make_order()
    await make_task(low_order, priority=Priority.LOW.value)

    response = await api_client.get(
        f"{PREFIX}/tasks", params={"priority": "HIGH"}, headers=auth_headers
    )

    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["priority"] == "HIGH"


@pytest.mark.integration
async def test_list_rejects_an_unknown_status(api_client, auth_headers):
    response = await api_client.get(
        f"{PREFIX}/tasks", params={"status": "NOT_A_STATUS"}, headers=auth_headers
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000
    # 报错要告诉调用方可选值是什么，否则只能去翻代码
    assert "NOT_A_STATUS" in response.json()["message"]


@pytest.mark.integration
async def test_list_rejects_an_unknown_priority(api_client, auth_headers):
    response = await api_client.get(
        f"{PREFIX}/tasks", params={"priority": "URGENT"}, headers=auth_headers
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000


# ============================================================
# §9.2 详情 / §9.6 执行记录
# ============================================================


@pytest.mark.integration
async def test_detail_returns_full_order_snapshot(
    api_client, auth_headers, make_order, make_task
):
    order = await make_order(
        customer_name="李四",
        phone="13800001111",
        address="北京市朝阳区某路 8 号",
        buyer_message="请工作日送",
    )
    task = await make_task(order, status=TaskStatus.QUEUED.value)

    response = await api_client.get(f"{PREFIX}/tasks/{task.id}", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["order"]["order_no"] == order.order_no
    # 详情页的手机号与地址**不脱敏**：管理员要核对，Worker 要照抄进 ERP
    assert data["order"]["phone"] == "13800001111"
    assert data["order"]["address"] == "北京市朝阳区某路 8 号"
    assert data["order"]["buyer_message"] == "请工作日送"
    assert data["order"]["amount"] == "99.00"
    # Phase 3 还没有 AI，分析记录为空是正常的
    assert data["analysis"] is None
    assert data["executions"] == []


@pytest.mark.integration
async def test_detail_returns_404_for_a_missing_task(api_client, auth_headers):
    response = await api_client.get(f"{PREFIX}/tasks/999999", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["code"] == 4004


@pytest.mark.integration
async def test_executions_come_back_ordered_by_attempt(
    api_client, auth_headers, db_session, make_order, make_task
):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.SUCCESS.value)
    await _add_execution(db_session, task.id, attempt=2, status="SUCCESS")
    await _add_execution(db_session, task.id, attempt=1, status="FAILED", error_code="TIMEOUT")

    response = await api_client.get(
        f"{PREFIX}/tasks/{task.id}/executions", headers=auth_headers
    )

    assert response.status_code == 200
    items = response.json()["data"]
    assert [i["attempt"] for i in items] == [1, 2]
    assert items[0]["error_code"] == "TIMEOUT"


@pytest.mark.integration
async def test_executions_return_404_for_a_missing_task(api_client, auth_headers):
    """任务不存在时返回 404 而不是空数组 —— 空数组会让前端以为「这个任务没执行过」。"""
    response = await api_client.get(
        f"{PREFIX}/tasks/999999/executions", headers=auth_headers
    )

    assert response.status_code == 404
    assert response.json()["code"] == 4004


# ============================================================
# §9.3 审核 / §9.4 重试 / §9.5 取消
# ============================================================


@pytest.mark.integration
async def test_review_approve_enqueues_the_task(
    api_client, auth_headers, queue, make_order, make_task
):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.WAITING_REVIEW.value, need_review=True)

    response = await api_client.post(
        f"{PREFIX}/tasks/{task.id}/review",
        json={"result": ReviewResult.APPROVED.value, "reason": "审核通过"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"task_id": task.id, "status": "QUEUED"}
    assert task.id in await queue.peek(100)


@pytest.mark.integration
async def test_review_on_a_non_waiting_task_returns_4009(
    api_client, auth_headers, queue, make_order, make_task
):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.QUEUED.value)

    response = await api_client.post(
        f"{PREFIX}/tasks/{task.id}/review",
        json={"result": ReviewResult.APPROVED.value},
        headers=auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == 4009
    # 报错要把「当前是什么状态」告诉调用方，否则前端还得再拉一次详情
    assert "QUEUED" in response.json()["message"]
    assert task.id not in await queue.peek(100)


@pytest.mark.integration
async def test_retry_reports_the_new_retry_count(
    api_client, auth_headers, queue, make_order, make_task
):
    order = await make_order()
    task = await make_task(
        order, status=TaskStatus.FAILED.value, retry_count=2, last_error="ERP 超时"
    )

    response = await api_client.post(
        f"{PREFIX}/tasks/{task.id}/retry",
        json={"reset_retry_count": True, "reason": "ERP 已恢复"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "task_id": task.id,
        "status": "QUEUED",
        "retry_count": 0,
    }
    assert task.id in await queue.peek(100)


@pytest.mark.integration
async def test_cancel_removes_the_task_from_the_queue(
    api_client, auth_headers, queue, make_order, make_task
):
    order = await make_order()
    task = await make_task(order, status=TaskStatus.QUEUED.value)
    await queue.enqueue(task.id, Priority.MEDIUM)

    response = await api_client.post(
        f"{PREFIX}/tasks/{task.id}/cancel",
        json={"reason": "客户已退款"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"task_id": task.id, "status": "CANCELLED"}
    assert task.id not in await queue.peek(100)


@pytest.mark.integration
async def test_cancel_a_running_task_returns_4009(
    api_client, auth_headers, make_order, make_task
):
    order = await make_order()
    task = await make_task(
        order,
        status=TaskStatus.RUNNING.value,
        claimed_by="rpa-worker-01",
        heartbeat_at=datetime(2026, 1, 1),
    )

    response = await api_client.post(
        f"{PREFIX}/tasks/{task.id}/cancel", json={}, headers=auth_headers
    )

    assert response.status_code == 409
    assert response.json()["code"] == 4009
    assert "正在执行" in response.json()["message"]


@pytest.mark.integration
async def test_review_with_an_invalid_result_is_a_parameter_error(
    api_client, auth_headers, make_order, make_task
):
    """`result` 用的是枚举类型，非法值由 Pydantic 挡在 400/4000，不会漏进 service。"""
    order = await make_order()
    task = await make_task(order, status=TaskStatus.WAITING_REVIEW.value, need_review=True)

    response = await api_client.post(
        f"{PREFIX}/tasks/{task.id}/review",
        json={"result": "MAYBE"},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000
