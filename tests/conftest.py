"""pytest 全局夹具 + **测试库保护**。

这个文件最重要的职责不是提供夹具，而是保证**测试永远碰不到开发数据**。
测试的前置动作是把表清空（TRUNCATE），一旦连错库就是一次静默的数据丢失。
所以这里放了两层独立的保护，两层都必须通过：

  第一层（导入期，不连任何服务）—— 检查配置指向的库名是不是白名单里的测试库。
      它拦的是「.env 没配好 / 环境变量没生效」，在收集阶段就炸，此时还没碰过数据库。
  第二层（运行时，对活着的服务）—— MySQL 查 `SELECT DATABASE()`、
      Redis 读 `CLIENT INFO` 里的 `db=`，确认服务端**当前真的**在测试库上。
      它拦的是第一层看不出来的情况：连接串写对了，但服务端把它路由到了别处。

关于事件循环 scope —— 别把两个 scope 搞混
-----------------------------------------
这里有两件独立的事：

  · **夹具的 scope**（function / session）：决定对象多久重建一次。
    本文件里的夹具都是 function 级，每条用例拿一份干净的。
  · **事件循环的 scope**：决定这些协程跑在哪个循环上。
    本套件是 **session 级**（配在 pytest.ini 里），整个会话共用一个循环。

后者不是可选项：被测应用在导入时就建好了进程级连接池（`redis_client`、
`engine`），socket 一旦在某个循环里开出来就绑死了。若每个测试换一个新循环，
第一个用例连上 Redis 之后循环关闭，第二个用例复用旧 socket 就会报
`RuntimeError: Event loop is closed` —— 症状是「第一个集成测试过了，后面全崩」。
详见 pytest.ini 里的长注释。
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from decimal import Decimal

# ============================================================
# 第一层保护 · 第 ①步：在 import app 之前改掉库名
# ============================================================
#
# `app/core/config.py` 里的 `settings = Settings()` 是在**导入时**求值的，
# import 之后再改环境变量已经来不及。所以下面两行必须出现在任何
# `from app... import` 之前 —— 这个顺序不能动，也不能挪到函数里。
#
# 环境变量优先于 .env（pydantic-settings 的优先级：环境变量 > dotenv），
# 因此这里只覆盖「连哪个库」；主机/端口/账号/密码仍从开发者的 .env 读，
# 测试不需要自成一套连接信息。
TEST_MYSQL_DB = "ai_rpa_test"
TEST_REDIS_DB = 15

os.environ["MYSQL_DB"] = TEST_MYSQL_DB
os.environ["REDIS_DB"] = str(TEST_REDIS_DB)

# ============================================================
# 以上顺序敏感，以下是常规导入
# ============================================================

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.enums import OrderStatus, Priority, TaskStatus, UserRole  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.database.mysql import AsyncSessionLocal  # noqa: E402
from app.database.redis import redis_client  # noqa: E402
from app.main import app  # noqa: E402
from app.models.order import Order  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.queue_service import QueueService  # noqa: E402

#: 会被清空的表。外键检查在清理时是关掉的，所以这里的顺序不影响正确性；
#: 仍然按「依赖方在前」排，是为了读起来能直接看出引用关系。
_APP_TABLES = (
    "task_executions",
    "ai_review_logs",
    "notifications",
    "tasks",
    "ai_analyses",
    "orders",
    "import_batches",
    "users",
)

# bcrypt 一次约 0.3 秒。在导入期算一次、所有测试复用同一个哈希串，
# 比每个用例现算一遍省掉几十次 —— 哈希值相同不影响正确性（verify 仍能过）。
_ADMIN_PASSWORD = "test-admin-pw"
_WORKER_PASSWORD = "test-worker-pw"
_ADMIN_HASH = hash_password(_ADMIN_PASSWORD)
_WORKER_HASH = hash_password(_WORKER_PASSWORD)


# ============================================================
# 第一层保护 · 第 ②步：确认配置确实落在测试库上
# ============================================================


def _assert_settings_point_at_test_services() -> None:
    """用**等值判断**，不用 `endswith("_test")`。

    后者会放过 `ai_rpa_test2`、`my_test` 这类同样以 `_test` 结尾的库名，
    而那些库多半是别人随手建的，一样经不起被 TRUNCATE。
    白名单只认这两个精确值。
    """
    if settings.mysql_db != TEST_MYSQL_DB:
        raise RuntimeError(
            f"测试库保护（第一层）失败：settings.mysql_db 是 {settings.mysql_db!r}，"
            f"期望 {TEST_MYSQL_DB!r}。请检查是否有别的环境变量或 .env 覆盖了它。"
        )
    if settings.redis_db != TEST_REDIS_DB:
        raise RuntimeError(
            f"测试库保护（第一层）失败：settings.redis_db 是 {settings.redis_db!r}，"
            f"期望 {TEST_REDIS_DB}。"
        )


_assert_settings_point_at_test_services()


# ============================================================
# 第二层保护：对活着的服务再确认一次
# ============================================================


def _client_info_db(info: dict) -> int | None:
    """从 `CLIENT INFO` 的结果里取出当前连接的 db 号。

    注意 redis-py 的 `client_info()` **已经把 `CLIENT INFO` 的响应解析成 dict 了**
    （`{"id": ..., "addr": ..., "db": "15", ...}`），拿到的不是原始单行文本 ——
    按字符串去 `split()` 会直接 `AttributeError: 'dict' object has no attribute 'split'`。
    db 的值是字符串（形如 `"15"`），所以这里统一 int() 一下。

    取不到或转不成数字时返回 None，由调用方决定「不确认就不清」。
    """
    raw = info.get("db")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _flush_test_redis() -> None:
    """确认 Redis 在 db 15 上再 FLUSHDB。确认不了就**不清**。"""
    info = await redis_client.client_info()
    db = _client_info_db(info)
    if db is None:
        raise RuntimeError(
            "测试库保护（第二层）失败：无法从 Redis CLIENT INFO 里取到 db，"
            f"为避免误清生产/开发数据，不执行 FLUSHDB。原始结果：{info!r}"
        )
    if db != TEST_REDIS_DB:
        raise RuntimeError(
            f"测试库保护（第二层）失败：Redis 当前在 db {db}，"
            f"期望 db {TEST_REDIS_DB}。拒绝 FLUSHDB。"
        )
    await redis_client.flushdb()


async def _truncate_test_tables() -> None:
    """确认 MySQL 连的是测试库再清表。确认不了就**不清**。"""
    async with AsyncSessionLocal() as session:
        current = (await session.execute(text("SELECT DATABASE()"))).scalar_one()
        if current != TEST_MYSQL_DB:
            raise RuntimeError(
                f"测试库保护（第二层）失败：MySQL 连的是 `{current}`，"
                f"期望 `{TEST_MYSQL_DB}`。拒绝 TRUNCATE。"
            )

        # 表之间有外键，直接 TRUNCATE 会被 MySQL 拒绝；关掉外键检查再逐个清。
        # 这是 session 级开关，清完立刻恢复，避免污染连接池里的这条连接。
        await session.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in _APP_TABLES:
            await session.execute(text(f"TRUNCATE TABLE `{table}`"))
        await session.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        await session.commit()


# ============================================================
# 清理
# ============================================================


@pytest.fixture(autouse=True)
async def clean_state(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """每个集成测试前清空 MySQL 与 Redis。

    **只对 `integration` 标记的测试生效。** unit 测试（只有 `_score` / `_member`
    那几条）不碰任何外部服务，必须能在没装 MySQL/Redis 的机器上跑通 ——
    否则「纯逻辑测试」这一层就白分了。这也是它按 marker 而不是无条件执行的原因。
    """
    if request.node.get_closest_marker("integration") is None:
        yield
        return

    await _flush_test_redis()
    await _truncate_test_tables()
    yield


# ============================================================
# 基础夹具
# ============================================================


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """直连测试库的会话，用于在测试里造数据和查状态。

    与被测应用用的是**同一个** engine（`AsyncSessionLocal`），只是另开一条 session。
    测试里 commit 过的数据，接口那头立刻看得到。
    """
    async with AsyncSessionLocal() as session:
        yield session


@pytest.fixture
def queue() -> QueueService:
    """真实的队列服务（连 db 15）。"""
    return QueueService(redis_client)


@pytest.fixture
async def api_client() -> AsyncIterator[httpx.AsyncClient]:
    """进程内 HTTP 客户端 —— 直接把请求喂给 FastAPI app，不经过网络。

    用 ASGITransport 而不是起一个 uvicorn：起服务要占端口、要等启动、
    还得自己保证关干净，而这里要测的是路由/鉴权/序列化，不是网络栈。

    另外它**不会触发 lifespan**，所以 main.py 里那个
    「关闭时 dispose_engine / close_redis」不会在测试之间跑 ——
    正是我们想要的：全局连接池得活到整个测试会话结束。
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        # 这台机器配了系统级 HTTP 代理，httpx 默认 trust_env=True 会读它。
        # ASGITransport 不走网络本不受影响，但显式关掉，与 scripts/ 里的写法一致，
        # 免得以后有人把 transport 换成真实的就又踩一遍。
        trust_env=False,
    ) as client:
        yield client


# ============================================================
# 用户与鉴权
# ============================================================


async def _create_user(
    session: AsyncSession, username: str, password_hash: str, role: UserRole
) -> User:
    user = User(
        username=username,
        password_hash=password_hash,
        role=role.value,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "t_admin", _ADMIN_HASH, UserRole.ADMIN)


@pytest.fixture
def admin_credentials() -> dict[str, str]:
    """给「走真实 `/auth/login`」的用例使用。

    其余用例直接签 token（见 `auth_headers`）就够；只有专门测登录接口的
    用例才需要明文口令，而那正是 `_ADMIN_PASSWORD` 的出处 ——
    放在这里是为了改口令时只有一处要动。
    """
    return {"username": "t_admin", "password": _ADMIN_PASSWORD}


@pytest.fixture
async def auth_headers(admin_user: User) -> dict[str, str]:
    """管理员请求头。

    直接签 token，不绕 `/auth/login` —— 登录接口自己有测试（见 test_tasks.py），
    其余用例关心的是「带着有效管理员身份」这个前提，不该每次都付一次
    口令校验的成本，也避免一个登录 bug 让所有用例一起变红。
    """
    token = create_access_token(subject=admin_user.id, role=admin_user.role)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def worker_headers(db_session: AsyncSession) -> dict[str, str]:
    """Worker 角色的请求头，用来验证「非管理员访问管理接口得到 403」。"""
    user = await _create_user(db_session, "t_worker", _WORKER_HASH, UserRole.WORKER)
    token = create_access_token(subject=user.id, role=user.role)
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 造数据
# ============================================================


@pytest.fixture
async def make_order(db_session: AsyncSession) -> Callable[..., Awaitable[Order]]:
    """造一张订单。字段都有合法默认值，用例只覆盖自己关心的那几个。"""
    counter = 0

    async def _make(**overrides) -> Order:
        nonlocal counter
        counter += 1
        data: dict = {
            "order_no": f"TEST-{counter:06d}",
            "platform": "mock",
            "ordered_at": datetime(2026, 1, 1, 10, 0, 0),
            "customer_name": f"测试客户{counter}",
            "phone": f"1380000{counter:04d}",
            "address": "测试省测试市测试路 1 号",
            "product_name": "测试商品",
            "sku": f"SKU-{counter:04d}",
            "quantity": 1,
            "amount": Decimal("99.00"),
            "status": OrderStatus.IMPORTED.value,
        }
        data.update(overrides)
        order = Order(**data)
        db_session.add(order)
        await db_session.commit()
        return order

    return _make


@pytest.fixture
async def make_task(db_session: AsyncSession) -> Callable[..., Awaitable[Task]]:
    """造一个任务。默认是「已入队的普通任务」，覆盖 status 即可造出各种前置状态。"""
    counter = 0

    async def _make(order: Order, **overrides) -> Task:
        nonlocal counter
        counter += 1
        data: dict = {
            "order_id": order.id,
            "priority": Priority.MEDIUM.value,
            "status": TaskStatus.QUEUED.value,
            "need_review": False,
            "queued_at": datetime.now(),
        }
        data.update(overrides)
        task = Task(**data)
        db_session.add(task)
        await db_session.commit()
        return task

    return _make
