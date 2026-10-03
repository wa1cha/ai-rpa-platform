"""§11 看板接口 —— `GET /dashboard/summary` 与 `GET /dashboard/trends`。

重点验证几条容易出错的聚合口径：
  · `by_status` / `by_priority` **补 0**，键集固定；
  · 风险计数**只算每单最新一条分析**（一单多条分析时不能重复算）；
  · `workers_online` 按心跳窗口 `claimed_by` 去重；
  · 趋势**连续 N 天、缺日补 0**，`days` 越界返回 4000。
"""

from datetime import date, datetime, timedelta

import pytest

from app.core.enums import (
    OrderStatus,
    Priority,
    RiskLevel,
    TaskStatus,
)

pytestmark = pytest.mark.integration

PREFIX = "/api/v1"


# ============================================================
# 鉴权
# ============================================================


async def test_summary_requires_authentication(api_client):
    response = await api_client.get(f"{PREFIX}/dashboard/summary")

    assert response.status_code == 401
    assert response.json()["code"] == 4001


async def test_summary_rejects_the_worker_role(api_client, worker_headers):
    response = await api_client.get(f"{PREFIX}/dashboard/summary", headers=worker_headers)

    assert response.status_code == 403
    assert response.json()["code"] == 4003


# ============================================================
# §11.1 汇总
# ============================================================


async def test_summary_fills_every_enum_key_with_zero(api_client, auth_headers):
    """空库也要返回**完整的键集** —— 前端不必自己兜底缺的状态。"""
    response = await api_client.get(f"{PREFIX}/dashboard/summary", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["orders"]["by_status"] == {s.value: 0 for s in OrderStatus}
    assert data["tasks"]["by_status"] == {s.value: 0 for s in TaskStatus}
    assert data["tasks"]["by_priority"] == {p.value: 0 for p in Priority}
    assert data["orders"]["total"] == 0
    assert data["risk"] == {"high": 0, "medium": 0, "need_contact": 0}
    assert data["rpa"] == {"workers_online": 0, "last_success_at": None}


async def test_summary_counts_orders_and_tasks_by_status_and_priority(
    api_client, auth_headers, make_order, make_task
):
    o1 = await make_order(status=OrderStatus.IMPORTED.value)
    o2 = await make_order(status=OrderStatus.COMPLETED.value)
    o3 = await make_order(status=OrderStatus.COMPLETED.value)
    await make_task(o1, status=TaskStatus.SUCCESS.value, priority=Priority.HIGH.value)
    await make_task(o2, status=TaskStatus.FAILED.value, priority=Priority.LOW.value)

    data = (await api_client.get(f"{PREFIX}/dashboard/summary", headers=auth_headers)).json()[
        "data"
    ]

    assert data["orders"]["total"] == 3
    assert data["orders"]["by_status"]["IMPORTED"] == 1
    assert data["orders"]["by_status"]["COMPLETED"] == 2
    assert data["orders"]["by_status"]["FAILED"] == 0

    assert data["tasks"]["total"] == 2
    assert data["tasks"]["by_status"]["SUCCESS"] == 1
    assert data["tasks"]["by_status"]["FAILED"] == 1
    assert data["tasks"]["by_status"]["RUNNING"] == 0
    assert data["tasks"]["by_priority"] == {"LOW": 1, "MEDIUM": 0, "HIGH": 1}


async def test_summary_today_counts_only_orders_imported_today(
    api_client, auth_headers, make_order
):
    await make_order()  # imported_at = now
    await make_order(imported_at=datetime.now() - timedelta(days=1))

    data = (await api_client.get(f"{PREFIX}/dashboard/summary", headers=auth_headers)).json()[
        "data"
    ]

    assert data["orders"]["total"] == 2
    assert data["orders"]["today"] == 1


async def test_summary_risk_counts_only_the_latest_analysis_per_order(
    api_client, auth_headers, make_order, make_analysis
):
    """一单先判 HIGH、再重分析成 LOW —— 这条单**不该**再算进高风险。"""
    a = await make_order()
    await make_analysis(a, risk_level=RiskLevel.HIGH.value)  # 旧的
    await make_analysis(a, risk_level=RiskLevel.LOW.value)  # 最新 → 有效
    b = await make_order()
    await make_analysis(b, risk_level=RiskLevel.HIGH.value)
    c = await make_order()
    await make_analysis(c, risk_level=RiskLevel.MEDIUM.value, need_contact=True)

    data = (await api_client.get(f"{PREFIX}/dashboard/summary", headers=auth_headers)).json()[
        "data"
    ]

    assert data["risk"] == {"high": 1, "medium": 1, "need_contact": 1}


async def test_summary_workers_online_dedupes_within_the_heartbeat_window(
    api_client, auth_headers, make_order, make_task
):
    now = datetime.now()
    o1 = await make_order()
    await make_task(o1, claimed_by="worker-1", heartbeat_at=now)
    o2 = await make_order()
    await make_task(o2, claimed_by="worker-1", heartbeat_at=now)  # 同一个 worker
    o3 = await make_order()
    await make_task(o3, claimed_by="worker-2", heartbeat_at=now - timedelta(minutes=5))  # 心跳过期
    o4 = await make_order()
    await make_task(o4, claimed_by=None, heartbeat_at=now)  # 没领过

    data = (await api_client.get(f"{PREFIX}/dashboard/summary", headers=auth_headers)).json()[
        "data"
    ]

    assert data["rpa"]["workers_online"] == 1


async def test_summary_reports_the_last_success_time(
    api_client, auth_headers, make_order, make_task
):
    finished = datetime.now().replace(microsecond=0)
    o = await make_order()
    await make_task(o, status=TaskStatus.SUCCESS.value, finished_at=finished)

    data = (await api_client.get(f"{PREFIX}/dashboard/summary", headers=auth_headers)).json()[
        "data"
    ]

    assert datetime.fromisoformat(data["rpa"]["last_success_at"]) == finished


# ============================================================
# §11.2 趋势
# ============================================================


async def test_trends_returns_contiguous_days_ending_today(
    api_client, auth_headers, make_order, make_task
):
    await make_order()
    o2 = await make_order()
    o3 = await make_order()
    await make_task(o2, status=TaskStatus.SUCCESS.value, finished_at=datetime.now())
    await make_task(o3, status=TaskStatus.FAILED.value, finished_at=datetime.now())

    data = (
        await api_client.get(
            f"{PREFIX}/dashboard/trends", params={"days": 3}, headers=auth_headers
        )
    ).json()["data"]

    days = data["days"]
    assert len(days) == 3
    # 从早到晚、逐日相连、最后一天是今天
    assert [d["date"] for d in days] == [
        (date.today() - timedelta(days=2)).isoformat(),
        (date.today() - timedelta(days=1)).isoformat(),
        date.today().isoformat(),
    ]
    # 前几天没数据 → 补 0；今天有 3 单导入、各一个成功/失败
    assert days[0] == {"date": days[0]["date"], "imported": 0, "success": 0, "failed": 0}
    assert days[1] == {"date": days[1]["date"], "imported": 0, "success": 0, "failed": 0}
    assert days[2]["imported"] == 3
    assert days[2]["success"] == 1
    assert days[2]["failed"] == 1


async def test_trends_defaults_to_seven_days(api_client, auth_headers):
    data = (await api_client.get(f"{PREFIX}/dashboard/trends", headers=auth_headers)).json()[
        "data"
    ]

    assert len(data["days"]) == 7


async def test_trends_rejects_days_out_of_range(api_client, auth_headers):
    for bad in (0, 31, -1):
        response = await api_client.get(
            f"{PREFIX}/dashboard/trends", params={"days": bad}, headers=auth_headers
        )
        assert response.status_code == 400, bad
        assert response.json()["code"] == 4000, bad
