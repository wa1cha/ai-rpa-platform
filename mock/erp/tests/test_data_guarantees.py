"""数据层的两条保证：来源单号唯一、单号格式。

应用层查重给的是「该来源订单号已录入」这句人话，但它有个绕不过的竞态：
两个请求同时查到「不存在」，然后双双插入。唯一索引才是那个真正拦住的一方，
它不在页面里、也不在服务里，所以在**绕过应用**的层面验证它 —— 直接往会话里
add 两行，看数据库认不认。
"""

import re
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import ErpOrder, ErpOrderStatus

pytestmark = pytest.mark.integration


def _order(**overrides) -> ErpOrder:
    data = {
        "erp_order_no": "ERP202601010001",
        "source_order_no": "SRC-000001",
        "customer_name": "测试客户",
        "phone": "13800001111",
        "address": "测试省测试市测试路 1 号",
        "product_name": "测试商品",
        "sku": "SKU-001",
        "quantity": 1,
        "amount": 99,
        "status": ErpOrderStatus.DRAFT.value,
    }
    data.update(overrides)
    return ErpOrder(**data)


async def test_source_order_no_is_unique_at_the_database_level(db):
    db.add(_order(source_order_no="MOCK-DUP"))
    await db.commit()

    db.add(_order(erp_order_no="ERP202601010002", source_order_no="MOCK-DUP"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_erp_order_no_is_unique_at_the_database_level(db):
    db.add(_order(erp_order_no="ERP-DUP"))
    await db.commit()

    db.add(_order(erp_order_no="ERP-DUP", source_order_no="SRC-000002"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_the_conflict_is_recognised_as_the_source_key(db):
    """`_is_source_no_conflict` 得能把这两类撞键分开。

    分开很重要：来源号重复该回「已录入」，单号重复该换个号重试。认错了就会
    把「有人抢先录了同一笔单」误报成「系统繁忙」，RPA 于是傻等重试。
    """
    from app.routes.orders import _is_source_no_conflict

    db.add(_order(source_order_no="MOCK-DUP"))
    await db.commit()
    db.add(_order(erp_order_no="ERP202601010002", source_order_no="MOCK-DUP"))
    with pytest.raises(IntegrityError) as exc:
        await db.commit()
    await db.rollback()

    assert _is_source_no_conflict(exc.value) is True


async def test_generated_numbers_follow_the_spec_format(logged_in, db, make_inventory):
    """`ERP + YYYYMMDD + 4 位当日序号`，例：ERP202609300001（§4.5）。

    格式是契约：RPA 拿它当「我要读回去的那个值」，对账也认它。
    """
    await make_inventory(sku="SKU-001", quantity=50)

    await logged_in.post(
        "/orders",
        data={
            "source_order_no": "MOCK-1",
            "customer_name": "张伟",
            "phone": "13800001111",
            "address": "杭州市西湖区文三路 1 号",
            "product_name": "儿童积木套装",
            "sku": "SKU-001",
            "quantity": "1",
            "amount": "299.00",
        },
    )

    number = await db.scalar(select(ErpOrder.erp_order_no))
    assert re.fullmatch(rf"ERP{datetime.now().strftime('%Y%m%d')}\d{{4}}", number)


async def test_nothing_is_deducted_from_inventory(logged_in, db, make_inventory):
    """v1 **只校验不扣减**（§12）。

    这条是有意为之：扣减会让并发的多个 RPA 任务争用同一行库存，
    而 v1 只需要「库存不足」这个分支可被触发。哪天有人「顺手」加了扣减，
    这里会红 —— 那时该先想清楚并发怎么处理，而不是默默改掉行为。
    """
    item = await make_inventory(sku="SKU-001", quantity=50)

    await logged_in.post(
        "/orders",
        data={
            "source_order_no": "MOCK-1",
            "customer_name": "张伟",
            "phone": "13800001111",
            "address": "杭州市西湖区文三路 1 号",
            "product_name": "儿童积木套装",
            "sku": "SKU-001",
            "quantity": "3",
            "amount": "299.00",
        },
    )

    await db.refresh(item)
    assert item.quantity == 50
