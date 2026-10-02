"""登录 / 会话 —— RPA 操作路径的第 ①② 步，以及「被踢回登录页」。

`/login` 是 RPA 唯一的入口，也是长跑任务中途掉线的落点。这两件事都属于
「错了整条链路就断了」，所以单独一个文件盯着。
"""

import pytest

from app.models import ErpOrderStatus, ErpUser

pytestmark = pytest.mark.integration


# ============================================================
# 未登录 → 登录页
# ============================================================


@pytest.mark.parametrize("path", ["/dashboard", "/orders", "/orders/new", "/inventory"])
async def test_pages_bounce_anonymous_visitors_to_login(client, path):
    """未登录访问任何页面 → 303 /login。

    用参数化而不是只测 /dashboard：这条保护是靠 `RedirectToLogin` 异常 +
    `OperatorDep` 实现的，漏挂一个参数就会漏一整页，值得逐页点一遍。
    """
    response = await client.get(path)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_a_forged_session_cookie_is_treated_as_logged_out(client):
    """签名不对的 cookie 等于没登录。

    这正是**会话过期**在服务端的表现：SessionMiddleware 解不出有效内容，
    session 就是空的。所以这条用例覆盖的是「RPA 跑到一半被踢回登录页」。
    """
    client.cookies.set("erp_session", "eyJvcGVyYXRvcl9pZCI6IDF9.forged-signature")

    response = await client.get("/dashboard")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_root_redirects_to_dashboard(client):
    response = await client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


# ============================================================
# 登录本身
# ============================================================


async def test_login_page_has_the_expected_hook_points(client):
    response = await client.get("/login")

    assert response.status_code == 200
    assert "<h1>ERP 登录</h1>" in response.text
    assert 'data-testid="input-username"' in response.text
    assert 'data-testid="input-password"' in response.text
    assert 'data-testid="btn-login"' in response.text


async def test_wrong_password_shows_the_error_and_stays_on_login(client, erp_operator):
    response = await client.post(
        "/login", data={"username": erp_operator.username, "password": "not-the-password"}
    )

    assert response.status_code == 200
    assert 'data-testid="msg-login-error"' in response.text
    assert "用户名或密码错误" in response.text


async def test_unknown_username_gets_the_same_message(client):
    """用户不存在与口令错给同一句话 —— 分开写等于告诉试探者账号是否存在。"""
    response = await client.post(
        "/login", data={"username": "nobody", "password": "whatever"}
    )

    assert "用户名或密码错误" in response.text


async def test_successful_login_redirects_and_stores_the_ui_token(
    client, erp_operator, erp_credentials
):
    response = await client.post("/login", data=erp_credentials)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"

    # token 必须真的渲染进页面：/api/* 的防作弊全靠它（§5.1）。
    dashboard = await client.get("/dashboard")
    assert 'name="erp-ui-token" content="' in dashboard.text
    assert 'content=""' not in dashboard.text


async def test_a_damaged_password_hash_does_not_crash_login(db, client):
    """库里存了一条坏哈希时，登录该报「密码错误」，不该 500。"""
    db.add(ErpUser(username="broken", password_hash="not-a-bcrypt-hash"))
    await db.commit()

    response = await client.post("/login", data={"username": "broken", "password": "x"})

    assert response.status_code == 200
    assert "用户名或密码错误" in response.text


async def test_visiting_login_while_logged_in_goes_to_dashboard(logged_in):
    """已登录还去 /login 就跳走。

    否则 RPA 判断「重登成功」时可能仍停在登录页，于是以为自己又掉线了。
    """
    response = await logged_in.get("/login")

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


async def test_logout_clears_the_session(logged_in):
    assert (await logged_in.get("/logout")).status_code == 303

    response = await logged_in.get("/dashboard")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# ============================================================
# 导航与 §9.3 的两处「故意没有 testid」
# ============================================================


async def test_order_entry_has_no_testid_but_inventory_does(logged_in):
    """这是**契约测试**，不是样式测试。

    §9.3 要求 RPA 的代码里同时存在两种定位方式：仪表盘的「订单管理」按
    **链接文本**找（没有 testid），库存入口则有 testid。一旦有人「顺手」
    给订单管理补上 testid，RPA 那条文本定位的分支就再也不会被执行 ——
    而它恰恰是「真实系统不给你 testid」这个练习的全部意义。
    """
    html = (await logged_in.get("/dashboard")).text

    assert '<a href="/orders">订单管理</a>' in html
    assert 'data-testid="nav-orders"' not in html
    assert 'data-testid="nav-inventory"' in html


async def test_dashboard_shows_the_pending_review_count(logged_in, make_order):
    await make_order(status=ErpOrderStatus.PENDING_REVIEW.value)

    html = (await logged_in.get("/dashboard")).text

    assert 'data-testid="stat-pending-review">1<' in html
