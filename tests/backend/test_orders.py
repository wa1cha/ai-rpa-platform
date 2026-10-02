"""§6 订单接口 + §7 导入批次测试。

分两层：
  · `unit`        两个展示规则纯函数（脱敏、金额格式化）。它们是
                  「列表页遮手机号、详情页不遮」这条规则的实现处，
                  而这条规则一旦写反，泄露的是真实用户手机号。
  · `integration` 真连数据库走接口：导入的四道关卡、筛选、脱敏的落点。
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from redis.exceptions import RedisError

from app.core.enums import ImportBatchStatus, OrderStatus
from app.repositories.order_repository import OrderRepository
from app.schemas.order import amount_to_str, mask_phone
from app.services.import_service import ImportService

PREFIX = "/api/v1"

_HEADER = "订单号,下单时间,客户姓名,电话,收货地址,商品名称,SKU,数量,金额,买家留言,卖家备注"


def _csv(*rows: str) -> bytes:
    """拼一个带表头的 CSV。表头写死在这里，避免用例各写各的漏列。"""
    return ("\n".join([_HEADER, *rows])).encode("utf-8")


def _row(
    order_no: str,
    phone: str = "13800001111",
    amount: str = "299.00",
    quantity: str = "1",
) -> str:
    return (
        f"{order_no},2026-01-01 10:00:00,张三,{phone},"
        f"北京市朝阳区某路 8 号,测试商品,SKU-001,{quantity},{amount},,"
    )


async def _import(api_client, headers, content: bytes, *, dry_run: bool = False):
    return await api_client.post(
        f"{PREFIX}/orders/import",
        files={"file": ("orders.csv", content, "text/csv")},
        data={"dry_run": "true" if dry_run else "false"},
        headers=headers,
    )


# ============================================================
# unit —— 展示规则
# ============================================================


@pytest.mark.unit
def test_mask_phone_keeps_head_and_tail():
    assert mask_phone("13800008888") == "138****8888"


@pytest.mark.unit
def test_mask_phone_leaves_abnormally_short_values_alone():
    """短于 7 位时无法既留头 3 位又留尾 4 位。

    这里选择**原样返回**而不是抛错：脱敏是展示层的事，一条脏数据不该让整个
    列表接口 500。代价是短号码会以明文出现在列表页 —— 但 11 位手机号是
    导入时就校验过的，能走到这里的短号码本身就已经是异常数据。
    """
    assert mask_phone("12345") == "12345"
    assert mask_phone("1234567") == "123****4567"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("299"), "299.00"),
        (Decimal("299.00"), "299.00"),
        (Decimal("0.1"), "0.10"),
        (Decimal("1234.567"), "1234.57"),
    ],
)
def test_amount_to_str_keeps_two_decimals(value, expected):
    """金额走字符串而不是 float：`299.00` 转 float 会变成 `299.0`，小数位丢了。"""
    assert amount_to_str(value) == expected


# ============================================================
# integration —— 导入的四道关卡
# ============================================================


@pytest.mark.integration
async def test_import_requires_authentication(api_client):
    response = await api_client.post(f"{PREFIX}/orders/import")

    assert response.status_code == 401
    assert response.json()["code"] == 4001


@pytest.mark.integration
async def test_import_creates_orders_and_leaves_a_batch(
    api_client, auth_headers, db_session
):
    response = await _import(api_client, auth_headers, _csv(_row("A-1"), _row("A-2")))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total_rows"] == 2
    assert data["success_rows"] == 2
    assert data["failed_rows"] == 0
    assert data["status"] == ImportBatchStatus.COMPLETED.value
    assert data["batch_id"] is not None

    listed = await api_client.get(f"{PREFIX}/orders", headers=auth_headers)
    assert listed.json()["data"]["total"] == 2


@pytest.mark.integration
async def test_dry_run_validates_without_writing(api_client, auth_headers):
    """dry_run 走完前面所有关卡但不落库 —— 所以没有批次记录（batch_id 为 null）。"""
    response = await _import(api_client, auth_headers, _csv(_row("B-1")), dry_run=True)

    data = response.json()["data"]
    assert data["success_rows"] == 1
    assert data["batch_id"] is None

    listed = await api_client.get(f"{PREFIX}/orders", headers=auth_headers)
    assert listed.json()["data"]["total"] == 0


@pytest.mark.integration
async def test_import_reports_every_bad_row_at_once(api_client, auth_headers):
    """一行错不该挡住其他行的报告 —— 用户改文件时想一次看全所有问题。"""
    content = _csv(
        _row("C-1", phone="12345"),          # 手机号格式错
        _row("C-2", quantity="0"),           # 数量必须 ≥ 1
        _row("C-3"),                         # 这行是好的，但整批仍然不落库
    )

    response = await _import(api_client, auth_headers, content)

    data = response.json()["data"]
    assert data["status"] == ImportBatchStatus.FAILED.value
    assert data["success_rows"] == 0
    assert data["failed_rows"] == 2
    reasons = {e["row"]: e["reason"] for e in data["errors"]}
    assert "手机号" in reasons[2]
    assert "数量" in reasons[3]

    # 整批失败 = 一行都不入库（前两道关卡在事务之外，不产生半截数据）
    listed = await api_client.get(f"{PREFIX}/orders", headers=auth_headers)
    assert listed.json()["data"]["total"] == 0


@pytest.mark.integration
async def test_import_rejects_a_duplicate_order_no_inside_the_file(
    api_client, auth_headers
):
    response = await _import(api_client, auth_headers, _csv(_row("D-1"), _row("D-1")))

    data = response.json()["data"]
    assert data["status"] == ImportBatchStatus.FAILED.value
    assert any("文件内重复" in e["reason"] for e in data["errors"])


@pytest.mark.integration
async def test_import_rejects_an_order_that_is_already_in_the_database(
    api_client, auth_headers
):
    await _import(api_client, auth_headers, _csv(_row("E-1")))
    second = await _import(api_client, auth_headers, _csv(_row("E-1")))

    data = second.json()["data"]
    assert data["status"] == ImportBatchStatus.FAILED.value
    assert data["errors"][0]["order_no"] == "E-1"
    assert "已存在" in data["errors"][0]["reason"]


@pytest.mark.integration
async def test_import_rejects_a_file_with_the_wrong_header(api_client, auth_headers):
    """表头不全时连批次记录都不建 —— 文件根本没被当成订单文件处理。"""
    response = await _import(api_client, auth_headers, "订单号,客户姓名\nF-1,张三\n".encode())

    assert response.status_code == 422
    assert response.json()["code"] == 4020


@pytest.mark.integration
async def test_import_rejects_an_unsupported_file_type(api_client, auth_headers):
    response = await api_client.post(
        f"{PREFIX}/orders/import",
        files={"file": ("orders.txt", _csv(_row("G-1")), "text/plain")},
        data={"dry_run": "false"},
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["code"] == 4020


# ============================================================
# integration —— 导入成功后的自动入队（AI 分析队列）
# ============================================================


@pytest.mark.integration
async def test_import_enqueues_the_batch_for_ai_analysis(
    api_client, auth_headers, queue_ai
):
    """「导入成功」包含两件事：订单落库 **且** 这批 order_id 进了 AI 分析队列。

    漏了后一半，订单会静默地永远不被分析（要靠 L1 补偿才捞得回来，但那本是
    不该发生的路径）。这里对着批次详情返回的 order_ids 逐一核对，顺带证明
    队列里放的确实是这批单、而不是别的什么 id。
    """
    response = await _import(api_client, auth_headers, _csv(_row("Q-1"), _row("Q-2")))
    batch_id = response.json()["data"]["batch_id"]

    detail = await api_client.get(
        f"{PREFIX}/import-batches/{batch_id}", headers=auth_headers
    )
    order_ids = detail.json()["data"]["order_ids"]

    assert await queue_ai.size() == 2
    assert sorted(await queue_ai.peek(10)) == sorted(order_ids)


@pytest.mark.integration
async def test_import_does_not_touch_the_task_queue(api_client, auth_headers, queue):
    """入的是**分析队列**，不是任务队列 —— 两个 key 不能串。

    串了的后果很隐蔽：任务队列只认 task_id，混进 order_id 后要么被
    `pop_next_runnable` 回查时丢掉、要么真拿去当 task 用，两种都很难查。
    """
    await _import(api_client, auth_headers, _csv(_row("R-1")))

    assert await queue.size() == 0


@pytest.mark.integration
async def test_dry_run_and_failed_import_enqueue_nothing(
    api_client, auth_headers, queue_ai
):
    """没落库的批次就不该进队列：dry_run 一行没写，失败批次整批回滚。

    这条是「先 commit 再入队」这个顺序的反面证明 —— 入队钩子挂在**成功提交
    之后**，而不是解析通过时。
    """
    await _import(api_client, auth_headers, _csv(_row("S-1")), dry_run=True)
    await _import(api_client, auth_headers, _csv(_row("S-2", phone="12345")))

    assert await queue_ai.size() == 0


@pytest.mark.integration
async def test_import_still_succeeds_when_the_enqueue_fails(db_session, admin_user):
    """入队是 best-effort：Redis 挂了，导入也必须照常成功返回。

    这是整个补偿设计的**前提** —— 正因为这里可以失败且刻意不报错，才必须有
    `ai_reconciler` 的 L1 腿来兜。用桩队列显式抛 `RedisError` 复现，而不是真
    去停 Redis：这样能确定性地只打中「Redis 异常」这一条路径，也不依赖运行中
    的服务状态。
    """

    class _BrokenQueue:
        async def enqueue_fifo_many(self, member_ids, at=None):
            raise RedisError("模拟 Redis 不可用")

    result = await ImportService(db_session, queue=_BrokenQueue()).import_file(
        filename="orders.csv",
        content=_csv(_row("T-1"), _row("T-2")),
        uploaded_by=admin_user.id,
    )

    assert result.status == ImportBatchStatus.COMPLETED
    assert result.success_rows == 2
    # 订单确实落库了 —— 入队失败不该牵连已经完成的写入
    assert await OrderRepository(db_session).find_existing_order_nos(
        ["T-1", "T-2"]
    ) == {"T-1", "T-2"}


# ============================================================
# integration —— 查询
# ============================================================


@pytest.mark.integration
async def test_list_masks_the_phone_but_detail_does_not(
    api_client, auth_headers, make_order
):
    """同一条数据，列表遮、详情不遮 —— 这是全项目唯一需要脱敏的地方。"""
    order = await make_order(phone="13800008888")

    listed = await api_client.get(f"{PREFIX}/orders", headers=auth_headers)
    assert listed.json()["data"]["items"][0]["phone"] == "138****8888"

    detail = await api_client.get(f"{PREFIX}/orders/{order.id}", headers=auth_headers)
    assert detail.json()["data"]["phone"] == "13800008888"


@pytest.mark.integration
async def test_list_amount_is_a_two_decimal_string(api_client, auth_headers, make_order):
    await make_order(amount=Decimal("299"))

    response = await api_client.get(f"{PREFIX}/orders", headers=auth_headers)

    assert response.json()["data"]["items"][0]["amount"] == "299.00"


@pytest.mark.integration
async def test_list_filters_by_status(api_client, auth_headers, make_order):
    await make_order(status=OrderStatus.IMPORTED.value)
    await make_order(status=OrderStatus.COMPLETED.value)

    response = await api_client.get(
        f"{PREFIX}/orders", params={"status": "COMPLETED"}, headers=auth_headers
    )

    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["status"] == "COMPLETED"


@pytest.mark.integration
async def test_list_pagination_reports_the_full_total(api_client, auth_headers, make_order):
    await make_order()
    await make_order()

    response = await api_client.get(
        f"{PREFIX}/orders", params={"page": 1, "page_size": 1}, headers=auth_headers
    )

    data = response.json()["data"]
    assert data["total"] == 2  # 符合条件的总数，不是本页条数
    assert len(data["items"]) == 1
    assert data["page_size"] == 1


@pytest.mark.integration
async def test_list_rejects_an_unknown_status(api_client, auth_headers):
    response = await api_client.get(
        f"{PREFIX}/orders", params={"status": "PAID"}, headers=auth_headers
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4000


@pytest.mark.integration
async def test_order_detail_returns_404_for_a_missing_order(api_client, auth_headers):
    response = await api_client.get(f"{PREFIX}/orders/999999", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["code"] == 4004


# ============================================================
# integration —— 批次查询（§7）
# ============================================================


@pytest.mark.integration
async def test_batch_list_and_detail_carry_the_error_rows(api_client, auth_headers):
    await _import(api_client, auth_headers, _csv(_row("H-1", phone="12345")))

    listed = await api_client.get(f"{PREFIX}/import-batches", headers=auth_headers)
    items = listed.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["status"] == ImportBatchStatus.FAILED.value
    assert items[0]["failed_rows"] == 1

    detail = await api_client.get(
        f"{PREFIX}/import-batches/{items[0]['id']}", headers=auth_headers
    )
    data = detail.json()["data"]
    # 错误明细只在详情页返回：列表一次可能几百条，塞进去纯属浪费带宽
    assert len(data["errors"]) == 1
    assert "手机号" in data["errors"][0]["reason"]
    assert data["order_ids"] == []


@pytest.mark.integration
async def test_batch_detail_returns_404_for_a_missing_batch(api_client, auth_headers):
    response = await api_client.get(f"{PREFIX}/import-batches/999999", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["code"] == 4004


# ============================================================
# integration —— AI 流水线：状态扫描与 CAS
# ============================================================


@pytest.mark.integration
async def test_list_by_status_older_than_requires_both_status_and_age(
    db_session, make_order
):
    """两个条件必须**同时**满足：状态对、**且**够陈旧。

    故意把两种「不该命中」都摆出来 —— 状态不对的、时间不够老的。少判任何一个，
    补偿就会去推一张正在被正常处理、或根本不该它管的单。
    """
    now = datetime(2026, 1, 1, 12, 0, 0)
    old = now - timedelta(minutes=10)
    cutoff = now - timedelta(seconds=60)

    stale = await make_order(status=OrderStatus.IMPORTED.value, updated_at=old)
    await make_order(status=OrderStatus.IMPORTED.value, updated_at=now)  # 太新
    await make_order(status=OrderStatus.ANALYZING.value, updated_at=old)  # 状态不对

    found = await OrderRepository(db_session).list_by_status_older_than(
        OrderStatus.IMPORTED, cutoff
    )

    assert [o.id for o in found] == [stale.id]


@pytest.mark.integration
async def test_cas_transition_succeeds_once_then_reports_false(db_session, make_order):
    """条件更新只在状态仍匹配时生效 —— 这正是「至少一次投递」的消费端幂等。

    第二次调用必须返回 False（而不是抛错、也不是再改一遍）：调用方据它丢弃
    重复投递，不把一次正常的重复记成一条错误。
    """
    order = await make_order(status=OrderStatus.IMPORTED.value)
    repo = OrderRepository(db_session)
    cas = repo.cas_transition_status

    assert await cas(order.id, OrderStatus.IMPORTED, OrderStatus.ANALYZING) is True
    # 状态已不是 IMPORTED，同一条 CAS 必须落空
    assert await cas(order.id, OrderStatus.IMPORTED, OrderStatus.ANALYZING) is False

    await db_session.refresh(order)
    assert order.status == OrderStatus.ANALYZING.value


@pytest.mark.integration
async def test_cas_transition_on_a_missing_order_is_false(db_session):
    """补偿扫描拿到的 id 可能在扫描与处理之间被删掉 —— 这里必须是 False 而非抛错，
    一次竞态不该打断整轮补偿。"""
    repo = OrderRepository(db_session)

    assert (
        await repo.cas_transition_status(
            999999, OrderStatus.IMPORTED, OrderStatus.ANALYZING
        )
        is False
    )
