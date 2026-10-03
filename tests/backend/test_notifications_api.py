"""§12 通知接口 —— `GET /notifications`。

v1 只读：这里验证分页、`status` 过滤、未知状态报 4000、鉴权，
以及**空表返回空 Page**（`?status=PENDING` 在不存在的初始态下就是空 —— 见
`NotificationService.notify_task_failed` 的 `status=SENT` 说明）。
"""

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification

pytestmark = pytest.mark.integration

PREFIX = "/api/v1"


async def _notify(db_session: AsyncSession, **overrides) -> Notification:
    data = {
        "type": "TASK_FAILED",
        "channel": "LOG",
        "title": "任务最终失败",
        "content": "已重试 3/3 次",
        "status": "SENT",
        "sent_at": datetime.now(),
    }
    data.update(overrides)
    record = Notification(**data)
    db_session.add(record)
    await db_session.commit()
    return record


# ============================================================
# 鉴权
# ============================================================


async def test_notifications_require_authentication(api_client):
    response = await api_client.get(f"{PREFIX}/notifications")

    assert response.status_code == 401
    assert response.json()["code"] == 4001


async def test_notifications_reject_the_worker_role(api_client, worker_headers):
    response = await api_client.get(f"{PREFIX}/notifications", headers=worker_headers)

    assert response.status_code == 403
    assert response.json()["code"] == 4003


# ============================================================
# §12.1 列表
# ============================================================


async def test_list_returns_an_empty_page_on_an_empty_table(api_client, auth_headers):
    response = await api_client.get(f"{PREFIX}/notifications", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {"items": [], "total": 0, "page": 1, "page_size": 20}


async def test_list_returns_the_contracted_fields_newest_first(
    api_client, auth_headers, db_session
):
    older = await _notify(db_session, title="早")
    newer = await _notify(db_session, title="晚", type="TASK_NEED_REVIEW")

    data = (await api_client.get(f"{PREFIX}/notifications", headers=auth_headers)).json()[
        "data"
    ]

    assert data["total"] == 2
    items = data["items"]
    assert [i["id"] for i in items] == [newer.id, older.id]
    # 字段严格按 §12：没有 related_order_id / error_message 这类表里有、接口没承诺的
    assert set(items[0]) == {
        "id",
        "type",
        "channel",
        "title",
        "content",
        "status",
        "related_task_id",
        "created_at",
        "sent_at",
    }
    assert items[0]["type"] == "TASK_NEED_REVIEW"
    assert items[0]["channel"] == "LOG"


async def test_list_filters_by_status(api_client, auth_headers, db_session):
    sent = await _notify(db_session, status="SENT")
    await _notify(db_session, status="PENDING")

    data = (
        await api_client.get(
            f"{PREFIX}/notifications", params={"status": "SENT"}, headers=auth_headers
        )
    ).json()["data"]

    assert data["total"] == 1
    assert [i["id"] for i in data["items"]] == [sent.id]


async def test_list_rejects_an_unknown_status(api_client, auth_headers):
    response = await api_client.get(
        f"{PREFIX}/notifications", params={"status": "READ"}, headers=auth_headers
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000
    assert "READ" in response.json()["message"]


async def test_list_paginates(api_client, auth_headers, db_session):
    for _ in range(3):
        await _notify(db_session)

    data = (
        await api_client.get(
            f"{PREFIX}/notifications",
            params={"page": 2, "page_size": 2},
            headers=auth_headers,
        )
    ).json()["data"]

    assert data["total"] == 3
    assert data["page"] == 2
    assert len(data["items"]) == 1
