"""表单校验的**每一条提示文本**。

这些提示不是随便写的：它们是 RPA 用来分支的依据。「库存不足，当前可用：0」
和「商品编码不存在」在 RPA 眼里必须是两件事 —— 前者说明单子有问题、该交给人，
后者说明 SKU 填错了、该修数据。所以提示文本一旦被改，测试必须红，
否则 RPA 的分支会静默走错路。

顺带钉住一个行为：失败时**保留已填内容**。老系统不会替你记住输入，
RPA 重放时必须能靠回显判断「上次填到哪了」。
"""

import re

import pytest
from sqlalchemy import func, select

from app.models import ErpOrder

pytestmark = pytest.mark.integration

_VALID = {
    "source_order_no": "MOCK20260928001",
    "customer_name": "张伟",
    "phone": "13800001111",
    "address": "浙江省杭州市西湖区文三路100号",
    "product_name": "儿童积木套装",
    "sku": "SKU-001",
    "quantity": "1",
    "amount": "299.00",
}


def _error_of(html: str) -> str:
    """把 msg-form-error 里的文本抠出来。"""
    match = re.search(r'data-testid="msg-form-error">([^<]*)<', html)
    assert match, "页面上没有 msg-form-error"
    return match.group(1).strip()


async def _post(logged_in, **overrides):
    return await logged_in.post("/orders", data={**_VALID, **overrides})


# ============================================================
# §4.5 表里那五条 —— 一条不能少、一个字不能改
# ============================================================


async def test_source_order_no_is_required(logged_in):
    response = await _post(logged_in, source_order_no="")

    assert response.status_code == 400
    assert _error_of(response.text) == "来源订单号不能为空"


async def test_phone_format(logged_in, make_inventory):
    await make_inventory(sku="SKU-001")

    response = await _post(logged_in, phone="12345")

    assert response.status_code == 400
    assert _error_of(response.text) == "手机号格式不正确"


async def test_unknown_sku(logged_in, make_inventory):
    await make_inventory(sku="SKU-001")

    response = await _post(logged_in, sku="SKU-999")

    assert response.status_code == 400
    assert _error_of(response.text) == "商品编码不存在"


async def test_quantity_over_stock_reports_the_available_number(logged_in, make_inventory):
    """提示里必须带上**当前可用量** —— RPA 要不要重试、改成几，全靠这个数。"""
    await make_inventory(sku="SKU-003", quantity=0)

    response = await _post(logged_in, sku="SKU-003", quantity="1")

    assert response.status_code == 400
    assert _error_of(response.text) == "库存不足，当前可用：0"


async def test_duplicate_source_order_no(logged_in, make_inventory):
    await make_inventory(sku="SKU-001")
    assert (await _post(logged_in)).status_code == 303  # 先成功录一笔

    response = await _post(logged_in)  # 同一来源单号再录一次

    assert response.status_code == 400
    assert _error_of(response.text) == "该来源订单号已录入"


# ============================================================
# 校验失败时的两个附带行为
# ============================================================


async def test_rejected_form_echoes_back_what_was_typed(logged_in, make_inventory):
    await make_inventory(sku="SKU-001")

    response = await _post(logged_in, phone="12345")

    assert 'value="MOCK20260928001"' in response.text
    assert 'value="张伟"' in response.text
    assert "浙江省杭州市西湖区文三路100号" in response.text


async def test_a_rejected_order_never_reaches_the_database(logged_in, db, make_inventory):
    """校验失败必须**一笔都不落库** —— 半截数据比报错更难查。"""
    await make_inventory(sku="SKU-001")

    await _post(logged_in, phone="12345")
    await _post(logged_in, sku="SKU-999")
    await _post(logged_in, source_order_no="")

    assert (await db.scalar(select(func.count()).select_from(ErpOrder))) == 0


# ============================================================
# 格式与边界
# ============================================================


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("quantity", "0", "数量必须大于 0"),
        ("quantity", "abc", "数量必须是整数"),
        ("amount", "-1", "金额不能为负数"),
        ("amount", "abc", "金额格式不正确"),
        ("customer_name", "", "客户姓名不能为空"),
        ("address", "", "收货地址不能为空"),
        ("product_name", "", "商品名称不能为空"),
    ],
)
async def test_numeric_and_required_edges(logged_in, make_inventory, field, value, message):
    await make_inventory(sku="SKU-001")

    response = await _post(logged_in, **{field: value})

    assert response.status_code == 400
    assert _error_of(response.text) == message


@pytest.mark.parametrize("phone", ["13800001111", "19912345678", "15012345678"])
async def test_accepted_phone_formats(logged_in, make_inventory, phone):
    await make_inventory(sku="SKU-001")

    response = await _post(logged_in, phone=phone)

    assert response.status_code == 303


@pytest.mark.parametrize("phone", ["12345678901", "1380000111", "138000011112", "08800001111"])
async def test_rejected_phone_formats(logged_in, make_inventory, phone):
    """`^1[3-9]\\d{9}$`：长度必须 11 位、必须 1 开头、第二位不能是 2。"""
    await make_inventory(sku="SKU-001")

    response = await _post(logged_in, phone=phone)

    assert response.status_code == 400
    assert _error_of(response.text) == "手机号格式不正确"


async def test_quantity_equal_to_stock_is_accepted(logged_in, make_inventory):
    """边界：数量 == 库存该放行（「不足」是严格大于）。"""
    await make_inventory(sku="SKU-004", quantity=15)

    response = await _post(logged_in, sku="SKU-004", quantity="15")

    assert response.status_code == 303
