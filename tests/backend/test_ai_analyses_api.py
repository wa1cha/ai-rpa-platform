"""§8 分析结果接口 —— `GET /ai-analyses` 与 `POST /ai-analyses/{id}/review`。

两个端点都是管理员权限。走进程内 HTTP（`api_client`），把路由、鉴权、
参数校验、序列化真跑一遍。

`review` 这一侧的重点是三条业务约束：
  · 只允许改白名单里的字段（不许动 `model_name` / `status` 这类）；
  · 每个字段写一条 `ai_review_logs`（原值/新值/修正人）；
  · **只有任务还停在 WAITING_REVIEW 才同步 `priority`** ——
    QUEUED/RUNNING 的任务不能因后台改数据而与 RPA 手上的数据不一致。
"""

import pytest
from sqlalchemy import select

from app.core.enums import Priority, RiskLevel, TaskStatus
from app.models.ai_review_log import AiReviewLog

pytestmark = pytest.mark.integration

PREFIX = "/api/v1"


async def _logs(db_session, analysis_id: int) -> list[AiReviewLog]:
    rows = await db_session.execute(
        select(AiReviewLog).where(AiReviewLog.ai_analysis_id == analysis_id)
    )
    return list(rows.scalars().all())


# ============================================================
# 鉴权
# ============================================================


async def test_analyses_require_authentication(api_client):
    response = await api_client.get(f"{PREFIX}/ai-analyses")

    assert response.status_code == 401
    assert response.json()["code"] == 4001


async def test_analyses_reject_the_worker_role(api_client, worker_headers):
    response = await api_client.get(f"{PREFIX}/ai-analyses", headers=worker_headers)

    assert response.status_code == 403
    assert response.json()["code"] == 4003


# ============================================================
# §8.1 列表
# ============================================================


async def test_list_returns_analysis_with_its_order_context(
    api_client, auth_headers, make_order, make_analysis
):
    order = await make_order(buyer_message="能不能明天到")
    analysis = await make_analysis(order, risk_level=RiskLevel.HIGH.value)

    response = await api_client.get(f"{PREFIX}/ai-analyses", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 1
    item = body["data"]["items"][0]
    assert item["id"] == analysis.id
    assert item["order_no"] == order.order_no
    # 列表带买家留言 —— 「为什么这么判」要对着留言看
    assert item["buyer_message"] == "能不能明天到"
    assert item["risk_level"] == "HIGH"


async def test_list_filters_by_risk_level(
    api_client, auth_headers, make_order, make_analysis
):
    high = await make_analysis(await make_order(), risk_level=RiskLevel.HIGH.value)
    await make_analysis(await make_order(), risk_level=RiskLevel.LOW.value)

    response = await api_client.get(
        f"{PREFIX}/ai-analyses", params={"risk_level": "HIGH"}, headers=auth_headers
    )

    items = response.json()["data"]["items"]
    assert [i["id"] for i in items] == [high.id]


async def test_list_filters_by_need_contact(
    api_client, auth_headers, make_order, make_analysis
):
    a = await make_analysis(await make_order(), need_contact=True)
    await make_analysis(await make_order(), need_contact=False)

    response = await api_client.get(
        f"{PREFIX}/ai-analyses", params={"need_contact": "true"}, headers=auth_headers
    )

    items = response.json()["data"]["items"]
    assert [i["id"] for i in items] == [a.id]


async def test_list_filters_by_order_no(
    api_client, auth_headers, make_order, make_analysis
):
    order = await make_order()
    a = await make_analysis(order)
    await make_analysis(await make_order())

    response = await api_client.get(
        f"{PREFIX}/ai-analyses", params={"order_no": order.order_no}, headers=auth_headers
    )

    items = response.json()["data"]["items"]
    assert [i["id"] for i in items] == [a.id]


async def test_list_rejects_an_unknown_risk_level(api_client, auth_headers):
    response = await api_client.get(
        f"{PREFIX}/ai-analyses", params={"risk_level": "CRITICAL"}, headers=auth_headers
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000
    assert "CRITICAL" in response.json()["message"]


async def test_list_rejects_an_unknown_status(api_client, auth_headers):
    response = await api_client.get(
        f"{PREFIX}/ai-analyses", params={"status": "MAYBE"}, headers=auth_headers
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000


async def test_list_paginates(api_client, auth_headers, make_order, make_analysis):
    for _ in range(3):
        await make_analysis(await make_order())

    response = await api_client.get(
        f"{PREFIX}/ai-analyses",
        params={"page": 2, "page_size": 2},
        headers=auth_headers,
    )

    data = response.json()["data"]
    assert data["total"] == 3
    assert data["page"] == 2
    assert len(data["items"]) == 1


# ============================================================
# §8.2 人工修正
# ============================================================


async def test_review_applies_a_change_and_writes_a_log(
    api_client, auth_headers, db_session, make_order, make_analysis, make_task
):
    order = await make_order()
    analysis = await make_analysis(order, risk_level=RiskLevel.LOW.value)
    task = await make_task(
        order, status=TaskStatus.WAITING_REVIEW.value, need_review=True,
        priority=Priority.MEDIUM.value,
    )

    response = await api_client.post(
        f"{PREFIX}/ai-analyses/{analysis.id}/review",
        json={
            "changes": [{"field_name": "risk_level", "new_value": "HIGH"}],
            "reason": "人工复核发现疑似欺诈",
        },
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["analysis_id"] == analysis.id
    assert body["task_updated"] is True
    assert body["applied"] == [
        {"field_name": "risk_level", "original_value": "LOW", "new_value": "HIGH"}
    ]

    await db_session.refresh(analysis)
    assert analysis.risk_level == "HIGH"

    logs = await _logs(db_session, analysis.id)
    assert len(logs) == 1
    assert logs[0].field_name == "risk_level"
    assert logs[0].original_value == "LOW"
    assert logs[0].new_value == "HIGH"
    assert logs[0].reviewer is not None


async def test_review_syncs_priority_of_a_waiting_task(
    api_client, auth_headers, db_session, make_order, make_analysis, make_task
):
    """任务仍 WAITING_REVIEW → 改 priority 会同步到任务上（人还没放行，改得动）。"""
    order = await make_order()
    analysis = await make_analysis(order, priority=Priority.MEDIUM.value)
    task = await make_task(
        order, status=TaskStatus.WAITING_REVIEW.value, need_review=True,
        priority=Priority.MEDIUM.value,
    )

    await api_client.post(
        f"{PREFIX}/ai-analyses/{analysis.id}/review",
        json={"changes": [{"field_name": "priority", "new_value": "HIGH"}]},
        headers=auth_headers,
    )

    await db_session.refresh(task)
    assert task.priority == "HIGH"


async def test_review_does_not_touch_a_task_that_is_no_longer_waiting(
    api_client, auth_headers, db_session, make_order, make_analysis, make_task
):
    """任务已 QUEUED（RPA 可能正在领）→ 只记录、**不改任务**。

    这一条是 §8.2 第 4 点的落点：后台改数据不能影响正在执行的任务，
    否则 RPA 拿着旧数据执行、后端已改，两边不一致。`task_updated=false`。
    """
    order = await make_order()
    analysis = await make_analysis(order, priority=Priority.MEDIUM.value)
    task = await make_task(
        order, status=TaskStatus.QUEUED.value, priority=Priority.MEDIUM.value
    )

    response = await api_client.post(
        f"{PREFIX}/ai-analyses/{analysis.id}/review",
        json={"changes": [{"field_name": "priority", "new_value": "HIGH"}]},
        headers=auth_headers,
    )

    assert response.json()["data"]["task_updated"] is False
    await db_session.refresh(task)
    assert task.priority == "MEDIUM"          # 任务没被动
    await db_session.refresh(analysis)
    assert analysis.priority == "HIGH"        # 分析记录改了
    assert len(await _logs(db_session, analysis.id)) == 1


async def test_review_works_with_no_task_at_all(
    api_client, auth_headers, make_order, make_analysis
):
    """订单还没有任务（分析完但 worker 没跑 L3）—— 修正只记录，不报错。"""
    analysis = await make_analysis(await make_order())

    response = await api_client.post(
        f"{PREFIX}/ai-analyses/{analysis.id}/review",
        json={"changes": [{"field_name": "action", "new_value": "电话确认后录入"}]},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["task_updated"] is False


async def test_review_rejects_a_field_outside_the_whitelist(
    api_client, auth_headers, make_order, make_analysis
):
    """`model_name` / `status` 这类不是人工该改的字段 —— 白名单外一律拒绝。"""
    analysis = await make_analysis(await make_order())

    response = await api_client.post(
        f"{PREFIX}/ai-analyses/{analysis.id}/review",
        json={"changes": [{"field_name": "model_name", "new_value": "gpt-4"}]},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000
    assert "model_name" in response.json()["message"]


async def test_review_rejects_an_invalid_field_value(
    api_client, auth_headers, make_order, make_analysis
):
    analysis = await make_analysis(await make_order())

    response = await api_client.post(
        f"{PREFIX}/ai-analyses/{analysis.id}/review",
        json={"changes": [{"field_name": "priority", "new_value": "URGENT"}]},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000


async def test_review_rejects_an_empty_change_list(
    api_client, auth_headers, make_order, make_analysis
):
    """`changes` 必须至少一项 —— 空改动是调用方 bug，不该静默成功。"""
    analysis = await make_analysis(await make_order())

    response = await api_client.post(
        f"{PREFIX}/ai-analyses/{analysis.id}/review",
        json={"changes": []},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000


async def test_review_returns_404_for_a_missing_analysis(api_client, auth_headers):
    response = await api_client.post(
        f"{PREFIX}/ai-analyses/999999/review",
        json={"changes": [{"field_name": "risk_level", "new_value": "HIGH"}]},
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["code"] == 4004
