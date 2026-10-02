"""刻意保留的两处摩擦：故障注入与页面延时（§8.1、§9.1）。

这两条都不是「功能」，而是**为了让 RPA 那一半代码有意义**才存在的东西：
没有随机失败，就永远不知道重试逻辑写没写对；页面不慢，就没人会去写显式等待。
正因如此，它们的开关行为值得被钉住 —— 尤其是「故障只影响写操作」这条边界。
"""

import asyncio

import pytest

from app.config import settings

pytestmark = pytest.mark.integration

_FORM = {
    "source_order_no": "MOCK-1",
    "customer_name": "张伟",
    "phone": "13800001111",
    "address": "杭州市西湖区文三路 1 号",
    "product_name": "儿童积木套装",
    "sku": "SKU-001",
    "quantity": "1",
    "amount": "299.00",
}


# ============================================================
# 故障注入
# ============================================================


@pytest.fixture
def always_failing(monkeypatch):
    """把注入概率拉满。

    monkeypatch 而不是改环境变量：`inject_fault` 在**调用时**读 settings，
    所以改内存里的值就立刻生效，也不用担心污染别的用例（monkeypatch 会还原）。
    """
    monkeypatch.setattr(settings, "mock_erp_fail_rate", 1.0)


async def test_write_operations_fail_when_injection_is_on(
    logged_in, make_inventory, always_failing
):
    await make_inventory(sku="SKU-001", quantity=50)

    response = await logged_in.post("/orders", data=_FORM)

    assert response.status_code == 503
    assert "系统繁忙" in response.text


async def test_submit_review_also_fails(logged_in, make_order, always_failing):
    order = await make_order()

    response = await logged_in.post(f"/orders/{order.erp_order_no}/submit-review")

    assert response.status_code == 503


async def test_approve_also_fails(logged_in, make_order, always_failing):
    order = await make_order(status="PENDING_REVIEW")

    response = await logged_in.post(f"/review/{order.erp_order_no}/approve")

    assert response.status_code == 503


async def test_reads_are_not_affected(logged_in, make_order, always_failing):
    """**读操作不受影响**——这是刻意的边界。

    读失败只是让人再刷一次；只有写失败才会驱动「任务失败 → 重试 → 僵尸回收」
    那条链路，那才是要演示的东西。把读也弄挂只会让演示变成一团糟。
    """
    await make_order()

    for path in ("/dashboard", "/orders", "/orders/new", "/review", "/inventory"):
        assert (await logged_in.get(path)).status_code == 200, path


async def test_injection_is_off_by_default(logged_in, make_inventory):
    """默认必须是 0，否则开发阶段会被随机失败折磨（§8.1）。"""
    assert settings.mock_erp_fail_rate == 0.0
    await make_inventory(sku="SKU-001", quantity=50)

    assert (await logged_in.post("/orders", data=_FORM)).status_code == 303


async def test_a_failed_write_leaves_nothing_behind(logged_in, db, make_inventory, always_failing):
    """注入发生在**校验之前**，所以一笔都不会落库。"""
    from sqlalchemy import func, select

    from app.models import ErpOrder

    await make_inventory(sku="SKU-001", quantity=50)

    await logged_in.post("/orders", data=_FORM)

    assert (await db.scalar(select(func.count()).select_from(ErpOrder))) == 0


# ============================================================
# 页面延时
# ============================================================


@pytest.fixture
def recorded_sleeps(monkeypatch):
    """把 `asyncio.sleep` 换成记录器，不真的等。

    为什么不直接测耗时：那会引入时间相关的抖动，测试要么慢要么偶发变红。
    而这里真正要断言的是「中间件**按配置的时长**调了一次 sleep」，
    用 spy 断言得比秒表精确得多。
    """
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def _spy(delay, *args, **kwargs):
        recorded.append(delay)
        return await real_sleep(0, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", _spy)
    return recorded


async def test_html_pages_are_delayed_by_the_configured_amount(
    logged_in, monkeypatch, recorded_sleeps
):
    monkeypatch.setattr(settings, "mock_erp_page_delay_ms", 7770)

    await logged_in.get("/dashboard")

    assert 7.77 in recorded_sleeps


async def test_static_assets_and_api_are_not_delayed(
    logged_in, monkeypatch, recorded_sleeps
):
    """静态资源和 /health 跟着一起慢只会让调试更难 —— 真实感来自整页跳转。

    断言的是「**没有**按配置时长 sleep」，不是「一次 sleep 都没有」：
    断言后者会把 ASGI 传输层内部的零碎等待也算进来，测试就变得看运气了。
    """
    monkeypatch.setattr(settings, "mock_erp_page_delay_ms", 7770)

    await logged_in.get("/static/app.js")
    await logged_in.get("/health")

    assert 7.77 not in recorded_sleeps


async def test_delay_is_zero_in_the_test_environment(logged_in):
    """夹具已把延时归零 —— 否则整个测试套件会白等几百秒。

    conftest 的第一层保护也会盯着这个值（不为 0 就直接报错），这里再断一次
    是为了让「测试环境不该有延时」这件事在**测试文件里**也看得见。
    """
    assert settings.mock_erp_page_delay_ms == 0
    assert (await logged_in.get("/dashboard")).status_code == 200
