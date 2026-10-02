"""订单主流程 —— RPA 操作路径的第 ③~⑧ 步。

这里盯的是**状态机**：DRAFT → PENDING_REVIEW → APPROVED 每一跳之后，
页面上的可见状态和数据库里的真值是否一致。以及那个最容易被忽略的点 ——
**提交审核必须幂等**：回传失败后重试会点第二次，第二次不该报错（§7.2）。
"""

import re
from datetime import datetime

import pytest
from sqlalchemy import select

from app.models import ErpOrder, ErpOrderStatus

pytestmark = pytest.mark.integration

_FORM = {
    "source_order_no": "MOCK20260928001",
    "customer_name": "张伟",
    "phone": "13800001111",
    "address": "浙江省杭州市西湖区文三路100号",
    "product_name": "儿童积木套装",
    "sku": "SKU-001",
    "quantity": "1",
    "amount": "299.00",
}


async def _status_in_db(db, erp_order_no: str) -> str:
    """从**数据库**读状态。

    刻意查标量而不读 ORM 对象属性：对象属性可能来自 identity map 里那份
    「我以为的」值，标量查询读到的一定是行里的真值。
    """
    return await db.scalar(select(ErpOrder.status).where(ErpOrder.erp_order_no == erp_order_no))


def _row_cells(html: str, erp_order_no: str) -> list[str]:
    """把某一行的单元格文本抠出来，用来断言**列序号**。

    §9.3 要求状态列没有 testid，RPA 只能按第 7 列（索引 6）取。这个函数就是
    在把那条契约写成断言。
    """
    row = re.search(
        rf'<tr data-testid="row-order-{erp_order_no}">(.*?)</tr>', html, re.S
    )
    assert row, f"列表里没有 {erp_order_no} 这一行"
    return [
        re.sub(r"<[^>]+>", "", cell).strip()
        for cell in re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)
    ]


# ============================================================
# 新建
# ============================================================


async def test_create_order_generates_the_number_and_redirects_to_detail(
    logged_in, make_inventory
):
    await make_inventory(sku="SKU-001", quantity=50)

    response = await logged_in.post("/orders", data=_FORM)

    today = datetime.now().strftime("%Y%m%d")
    expected = f"ERP{today}0001"
    assert response.status_code == 303
    assert response.headers["location"] == f"/orders/{expected}"

    detail = await logged_in.get(f"/orders/{expected}")
    assert f'data-testid="text-erp-order-no">{expected}<' in detail.text
    assert "草稿" in detail.text


async def test_created_order_is_draft_in_the_database(logged_in, db, make_inventory):
    await make_inventory(sku="SKU-001", quantity=50)

    await logged_in.post("/orders", data=_FORM)

    erp_order_no = await db.scalar(select(ErpOrder.erp_order_no))
    assert erp_order_no is not None
    assert await _status_in_db(db, erp_order_no) == ErpOrderStatus.DRAFT.value


async def test_daily_sequence_increases(logged_in, db, make_inventory):
    """同一天的第二笔单号要递增到 0002 —— 单号是 RPA 要读回去的值，不能重复。"""
    await make_inventory(sku="SKU-001", quantity=50)

    await logged_in.post("/orders", data=_FORM)
    await logged_in.post("/orders", data={**_FORM, "source_order_no": "MOCK20260928002"})

    numbers = (await db.scalars(select(ErpOrder.erp_order_no).order_by(ErpOrder.id))).all()
    assert len(numbers) == 2
    assert numbers[0].endswith("0001")
    assert numbers[1].endswith("0002")


async def test_new_order_page_lists_every_input_hook(logged_in):
    html = (await logged_in.get("/orders/new")).text

    for testid in (
        "input-source-order-no",
        "input-customer-name",
        "input-phone",
        "input-address",
        "input-product-name",
        "input-sku",
        "input-quantity",
        "input-amount",
        "btn-save-order",
        "dialog-confirm",
        "btn-confirm-yes",
        "btn-confirm-no",
    ):
        assert f'data-testid="{testid}"' in html, testid


# ============================================================
# 状态机：提交审核
# ============================================================


async def test_submit_review_moves_to_pending_and_shows_success(logged_in, db, make_order):
    order = await make_order()

    response = await logged_in.post(f"/orders/{order.erp_order_no}/submit-review")

    assert response.status_code == 303
    assert response.headers["location"] == f"/orders/{order.erp_order_no}?submitted=1"
    assert await _status_in_db(db, order.erp_order_no) == ErpOrderStatus.PENDING_REVIEW.value

    detail = (await logged_in.get(f"/orders/{order.erp_order_no}?submitted=1")).text
    # RPA 判定成功的两个条件（§4.6）：成功提示 + 单号有值
    assert 'data-testid="msg-success">已提交审核<' in detail
    assert f'data-testid="text-erp-order-no">{order.erp_order_no}<' in detail


async def test_detail_hides_the_submit_button_once_not_draft(logged_in, make_order):
    """按钮消失是**给 RPA 的信号**：找不到按钮 = 这单已经提交过了（§4.6）。"""
    order = await make_order(status=ErpOrderStatus.PENDING_REVIEW.value)

    html = (await logged_in.get(f"/orders/{order.erp_order_no}")).text

    assert 'data-testid="btn-submit-review"' not in html
    assert "待审核" in html


async def test_submit_review_is_idempotent(logged_in, db, make_order):
    """第二次提交不报错、不改状态 —— 这就是「RPA 重试不重复录入」的同一套逻辑。"""
    order = await make_order(status=ErpOrderStatus.PENDING_REVIEW.value)

    response = await logged_in.post(f"/orders/{order.erp_order_no}/submit-review")

    assert response.status_code == 303
    assert await _status_in_db(db, order.erp_order_no) == ErpOrderStatus.PENDING_REVIEW.value


# ============================================================
# 状态机：审核（不由 RPA 完成）
# ============================================================


async def test_review_page_lists_pending_orders(logged_in, make_order):
    pending = await make_order(status=ErpOrderStatus.PENDING_REVIEW.value)
    await make_order(status=ErpOrderStatus.DRAFT.value)

    html = (await logged_in.get("/review")).text

    assert f'data-testid="btn-approve-{pending.erp_order_no}"' in html
    assert html.count("data-testid=\"row-review-") == 1


async def test_approve_moves_to_approved_and_leaves_the_review_list(
    logged_in, db, make_order
):
    order = await make_order(status=ErpOrderStatus.PENDING_REVIEW.value)

    response = await logged_in.post(f"/review/{order.erp_order_no}/approve")

    assert response.status_code == 303
    assert response.headers["location"] == "/review"
    assert await _status_in_db(db, order.erp_order_no) == ErpOrderStatus.APPROVED.value

    review_html = (await logged_in.get("/review")).text
    assert order.erp_order_no not in review_html
    assert "已通过" in (await logged_in.get(f"/orders/{order.erp_order_no}")).text


async def test_approve_only_accepts_pending_orders(logged_in, db, make_order):
    """状态机只允许 PENDING_REVIEW → APPROVED 这一条边。

    对草稿点「通过」不该把它直接推到已通过 —— 那会绕过提交审核这一步。
    """
    draft = await make_order(status=ErpOrderStatus.DRAFT.value)

    await logged_in.post(f"/review/{draft.erp_order_no}/approve")

    assert await _status_in_db(db, draft.erp_order_no) == ErpOrderStatus.DRAFT.value


# ============================================================
# 找不到的单
# ============================================================


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/orders/ERP999999990001"),
        ("post", "/orders/ERP999999990001/submit-review"),
        ("post", "/review/ERP999999990001/approve"),
    ],
)
async def test_unknown_order_is_404(logged_in, method, path):
    response = await getattr(logged_in, method)(path)

    assert response.status_code == 404


# ============================================================
# 列表：搜索与分页
# ============================================================


async def test_search_matches_erp_or_source_number(logged_in, make_order):
    target = await make_order(source_order_no="MOCK20260928001")
    other = await make_order(source_order_no="MOCK20260928999")

    html = (await logged_in.get("/orders?keyword=MOCK20260928001")).text

    assert target.erp_order_no in html
    assert other.erp_order_no not in html


async def test_keyword_matching_nothing_renders_an_empty_table(logged_in, make_order):
    await make_order()

    html = (await logged_in.get("/orders?keyword=没有这个东西")).text

    assert "没有匹配的订单" in html


async def test_status_filter(logged_in, make_order):
    draft = await make_order(status=ErpOrderStatus.DRAFT.value)
    pending = await make_order(status=ErpOrderStatus.PENDING_REVIEW.value)

    html = (await logged_in.get("/orders?status=PENDING_REVIEW")).text

    assert pending.erp_order_no in html
    assert draft.erp_order_no not in html


async def test_list_paginates_twenty_per_page(logged_in, make_order):
    for _ in range(21):
        await make_order()

    first = (await logged_in.get("/orders?page=1")).text
    second = (await logged_in.get("/orders?page=2")).text

    assert first.count('data-testid="row-order-') == 20
    assert second.count('data-testid="row-order-') == 1
    assert 'data-testid="btn-page-next"' in first
    assert 'data-testid="btn-page-prev"' in second


async def test_status_column_has_no_testid_and_sits_at_index_six(logged_in, make_order):
    """§9.3 的另一半：状态列**故意没有 testid**，只能按列序号取。

    这条断言同时锁住两件事：testid 必须缺席，且「状态」必须还在第 7 列。
    哪天有人插了一列，RPA 按序号取的就会是错的单元格 —— 那时这里会红。
    """
    order = await make_order(status=ErpOrderStatus.PENDING_REVIEW.value)

    html = (await logged_in.get("/orders")).text

    assert "cell-status-" not in html
    cells = _row_cells(html, order.erp_order_no)
    assert len(cells) == 8
    assert cells[6] == "待审核"
    assert cells[0] == order.erp_order_no
