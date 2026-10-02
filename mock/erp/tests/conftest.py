"""模拟 ERP 的测试夹具 + 测试库保护。

与主平台的 `tests/conftest.py` 结构相似（两层测试库保护、按 marker 清理），
但这里有两条**它没有**的约束：

1. **必须在 import app 之前改环境变量。** `app/config.py` 里的
   `settings = ErpSettings()` 是导入时求值的。

2. **绝不能让 `app` 解析到主服务那个同名的包。** 夹具里只 import 本目录下的
   `app.*`；靠 `mock/erp/pytest.ini` 的 `pythonpath = .` 保证这一点。
   单独跑 `python -m pytest mock/erp` 没问题；但**不要**把两套测试塞进同一次
   运行（`pytest tests mock/erp`），那会让两边共用 rootdir 与 `pythonpath`，
   本文件会立刻 ModuleNotFoundError —— 详见 pytest.ini 里的说明。

顺带把三个会让测试变慢或变随机的开关**钉死在测试值上**（页面延时、故障注入、
审核页），免得开发者的 .env 一改，测试就慢得离谱或者开始随机失败。
"""

import os

# ============================================================
# ① 顺序敏感：必须在 import app.* 之前
# ============================================================

TEST_ERP_DB = "mock_erp_test"

os.environ["MOCK_ERP_DB"] = TEST_ERP_DB
#: 页面延时设为 0：测试不该为「模拟老系统慢」付出每页 1.5 秒的代价。
#: 延时的正确性由 test_faults.py 里那条专门的用例验证（它用 spy 而不是真等）。
os.environ["MOCK_ERP_PAGE_DELAY_MS"] = "0"
os.environ["MOCK_ERP_FAIL_RATE"] = "0"
os.environ["MOCK_ERP_ENABLE_REVIEW_PAGE"] = "true"
os.environ["MOCK_ERP_SESSION_SECRET"] = "test-only-erp-session-secret"

# ============================================================
# ② 以上顺序敏感，以下是常规导入
# ============================================================

import re  # noqa: E402
from decimal import Decimal  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ErpInventory, ErpOrder, ErpOrderStatus, ErpUser  # noqa: E402
from app.security import hash_password  # noqa: E402

#: 会被清空的表。没有外键，顺序随意。
_ERP_TABLES = ("erp_orders", "erp_inventory", "erp_users")

#: 测试用操作员的口令。bcrypt 一次约 0.3 秒，导入期算一次、所有用例复用同一个
#: 哈希串（哈希相同不影响正确性，verify 照样过）。
_OPERATOR_PASSWORD = "test-erp-pw"
_OPERATOR_HASH = hash_password(_OPERATOR_PASSWORD)


# ============================================================
# 第一层保护：确认配置真的落在测试库上（不连任何服务就能判断）
# ============================================================


def _assert_settings_point_at_the_test_db() -> None:
    """用**等值判断**，不用 `endswith("_test")`。

    后者会放过 `mock_erp_test2` 这类同样以 `_test` 结尾、但也许存着东西的库。
    这里每个集成测试前都会 TRUNCATE，连错库就是一次静默的数据丢失。
    """
    if settings.mock_erp_db != TEST_ERP_DB:
        raise RuntimeError(
            f"测试库保护（第一层）失败：settings.mock_erp_db 是 {settings.mock_erp_db!r}，"
            f"期望 {TEST_ERP_DB!r}。检查是否有别的环境变量覆盖了 MOCK_ERP_DB。"
        )
    # 这两个开关若不生效，症状分别是「测试莫名变慢」和「测试随机变红」，
    # 都属于「查半天发现是环境问题」的那一类。在这里直接炸掉更省事。
    if settings.mock_erp_page_delay_ms != 0:
        raise RuntimeError(
            f"页面延时没有归零（{settings.mock_erp_page_delay_ms}ms）——"
            "模拟 ERP 的测试必须在 MOCK_ERP_PAGE_DELAY_MS=0 下跑。"
        )
    if settings.mock_erp_fail_rate != 0:
        raise RuntimeError(
            f"故障注入概率不是 0（{settings.mock_erp_fail_rate}）——"
            "否则测试会随机失败。需要测故障的用例自己在用例内 monkeypatch。"
        )


_assert_settings_point_at_the_test_db()


# ============================================================
# 第二层保护：对活着的服务再确认一次，再清表
# ============================================================


async def _truncate_erp_tables() -> None:
    """确认 MySQL 连的是测试库再清表。确认不了就**不清**。"""
    async with AsyncSessionLocal() as session:
        current = (await session.execute(text("SELECT DATABASE()"))).scalar_one()
        if current != TEST_ERP_DB:
            raise RuntimeError(
                f"测试库保护（第二层）失败：MySQL 连的是 `{current}`，"
                f"期望 `{TEST_ERP_DB}`。拒绝 TRUNCATE。"
            )
        for table in _ERP_TABLES:
            await session.execute(text(f"TRUNCATE TABLE `{table}`"))
        await session.commit()


@pytest.fixture(autouse=True)
async def clean_state(request: pytest.FixtureRequest):
    """每个集成测试前清空三张表。

    **只对 `integration` 生效** —— unit 测试（配置校验、口令哈希那几条）不碰
    数据库，必须能在没装 MySQL 的机器上跑通。
    """
    if request.node.get_closest_marker("integration") is None:
        yield
        return
    await _truncate_erp_tables()
    yield


# ============================================================
# 基础夹具
# ============================================================


@pytest.fixture
async def db() -> AsyncSession:
    """直连测试库的会话，用来造数据、查状态。

    与被测应用共用同一个 engine，只是另开一条 session —— commit 过的数据
    接口那头立刻看得到。
    """
    async with AsyncSessionLocal() as session:
        yield session


@pytest.fixture
async def client() -> httpx.AsyncClient:
    """进程内 HTTP 客户端，直接把请求喂给 ASGI app。

    用 ASGITransport 而不是起 uvicorn：这里要测的是路由、校验、会话、模板渲染，
    不是网络栈；起服务还要占端口、等启动、保证关干净。

    它**不触发 lifespan**，所以 main.py 里那句 `create_all` 不会跑 ——
    正是我们想要的：测试库的表必须来自 `database/schema/009_mock_erp.sql`
    （由 scripts/init_test_db.sh 载入），而不是来自模型的 create_all。
    这样建表脚本本身也被顺带验证了。
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://erp-testserver",
        # 这台机器配了系统级 HTTP 代理，httpx 默认 trust_env=True 会读它。
        # ASGITransport 不走网络本不受影响，显式关掉免得起疑。
        trust_env=False,
    ) as http_client:
        yield http_client


@pytest.fixture
def erp_credentials() -> dict[str, str]:
    """测试操作员的登录凭据（明文口令只在这里出现一处）。"""
    return {"username": "t_operator", "password": _OPERATOR_PASSWORD}


@pytest.fixture
async def erp_operator(db: AsyncSession, erp_credentials: dict) -> ErpUser:
    operator = ErpUser(
        username=erp_credentials["username"], password_hash=_OPERATOR_HASH
    )
    db.add(operator)
    await db.commit()
    return operator


@pytest.fixture
async def logged_in(client: httpx.AsyncClient, erp_operator: ErpUser) -> httpx.AsyncClient:
    """已登录的客户端（session cookie 在 httpx 的 cookie jar 里）。

    走真实的 POST /login 而不是手搓 session cookie：登录写 token 这个动作
    本身就是要测的一部分，绕过它会让「token 没写进 session」这类 bug 漏网。
    """
    response = await client.post(
        "/login", data={"username": erp_operator.username, "password": _OPERATOR_PASSWORD}
    )
    assert response.status_code == 303, response.text
    return client


@pytest.fixture
async def ui_token(logged_in: httpx.AsyncClient) -> str:
    """从页面 `<meta>` 里把 X-ERP-UI 的 token 抠出来。

    刻意**不**去解 session cookie：页面的 JS 就是这么拿的，测试跟着走同一条路，
    等于顺带验证了「token 真的渲染到页面上了」。
    """
    html = (await logged_in.get("/inventory")).text
    match = re.search(r'name="erp-ui-token" content="([0-9a-f]*)"', html)
    assert match, "页面上没有 erp-ui-token —— 会话里没写 token，或模板漏了 meta"
    return match.group(1)


# ============================================================
# 造数据
# ============================================================


@pytest.fixture
def make_inventory(db: AsyncSession):
    """造一条库存。`quantity=0` 是那条「库存不足」分支的关键数据。"""
    counter = 0

    async def _make(sku: str | None = None, quantity: int = 50) -> ErpInventory:
        nonlocal counter
        counter += 1
        item = ErpInventory(sku=sku or f"SKU-{counter:03d}", quantity=quantity)
        db.add(item)
        await db.commit()
        return item

    return _make


@pytest.fixture
def make_order(db: AsyncSession):
    """直接造一张 ERP 订单。

    给「详情 / 提交审核 / 审核」这类**不关心录入过程**的用例使用 ——
    让它们走一遍表单只是在重复 test_order_flow 已经验过的东西。
    """
    counter = 0

    async def _make(**overrides) -> ErpOrder:
        nonlocal counter
        counter += 1
        data: dict = {
            "erp_order_no": f"ERP2026010100{counter:02d}",
            "source_order_no": f"SRC-{counter:06d}",
            "customer_name": f"测试客户{counter}",
            "phone": "13800001111",
            "address": "测试省测试市测试路 1 号",
            "product_name": "测试商品",
            "sku": "SKU-001",
            "quantity": 1,
            "amount": Decimal("99.00"),
            "status": ErpOrderStatus.DRAFT.value,
        }
        data.update(overrides)
        order = ErpOrder(**data)
        db.add(order)
        await db.commit()
        return order

    return _make
