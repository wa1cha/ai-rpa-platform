"""`/api/*` 的防作弊头 —— 「RPA 禁止直调 API」的技术实现（§5.1）。

三条约束里最容易被做成「君子协定」的一条。这里要证明的不是「约定存在」，
而是**绕过页面就直接吃 403**：token 每次会话随机、只出现在页面的 `<meta>` 里，
用 requests 是构造不出来的。
"""

import pytest

pytestmark = pytest.mark.integration

_URL = "/api/inventory/check"
_BODY = {"sku": "SKU-001", "quantity": 2}


async def test_missing_header_is_forbidden(logged_in):
    response = await logged_in.post(_URL, json=_BODY)

    assert response.status_code == 403


async def test_guessed_header_is_forbidden(logged_in):
    """猜一个 token 不行 —— 它是每条会话现生成的真随机值（32 位十六进制）。"""
    response = await logged_in.post(_URL, json=_BODY, headers={"X-ERP-UI": "0000000000000000"})

    assert response.status_code == 403


async def test_anonymous_call_is_forbidden_too(client):
    """没登录当然也没有 token。这条挡的是「先拿到 401 提示再去想办法」的路径。"""
    response = await client.post(_URL, json=_BODY)

    assert response.status_code == 403


async def test_the_page_token_works(logged_in, ui_token, make_inventory):
    await make_inventory(sku="SKU-001", quantity=50)

    response = await logged_in.post(_URL, json=_BODY, headers={"X-ERP-UI": ui_token})

    assert response.status_code == 200
    assert response.json() == {"available": True, "stock": 50}


async def test_quantity_over_stock_reports_unavailable(logged_in, ui_token, make_inventory):
    await make_inventory(sku="SKU-003", quantity=0)

    response = await logged_in.post(
        _URL, json={"sku": "SKU-003", "quantity": 1}, headers={"X-ERP-UI": ui_token}
    )

    assert response.json() == {"available": False, "stock": 0}


async def test_unknown_sku_reports_zero_stock_not_404(logged_in, ui_token):
    """SKU 打错字是常态。报 404 会逼前端为「打错字」和「库存不够」写两套处理，
    而它们的后续动作其实一样：改输入。
    """
    response = await logged_in.post(
        _URL, json={"sku": "SKU-999", "quantity": 1}, headers={"X-ERP-UI": ui_token}
    )

    assert response.status_code == 200
    assert response.json() == {"available": False, "stock": 0}


async def test_the_token_is_injected_into_every_page(logged_in):
    """不只是库存页 —— 任何页面都得带上它，否则从那个页面发起的调用会 403。"""
    for path in ("/dashboard", "/orders", "/orders/new", "/inventory", "/review"):
        html = (await logged_in.get(path)).text
        assert 'name="erp-ui-token" content="' in html, path


async def test_the_token_is_stable_within_a_session(logged_in, ui_token):
    """同一会话里 token 不变。每次请求都换的话，页面之间跳转就调不通了。"""
    html = (await logged_in.get("/orders")).text

    assert f'content="{ui_token}"' in html


async def test_the_token_changes_between_sessions(client, erp_operator, erp_credentials):
    """换一次登录换一个 token —— 这才能挡住「抓一个包就永久可用」。"""
    import re

    await client.post("/login", data=erp_credentials)
    first = re.search(r'content="([0-9a-f]+)"', (await client.get("/inventory")).text).group(1)

    await client.get("/logout")
    await client.post("/login", data=erp_credentials)
    second = re.search(r'content="([0-9a-f]+)"', (await client.get("/inventory")).text).group(1)

    assert first != second
