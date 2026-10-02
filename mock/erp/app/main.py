"""模拟 ERP 的应用装配 —— 唯一创建 ERP app 的地方。

这里刻意**不做**主服务那套统一响应外壳（`{code,message,data}`）。那是本平台的
API 契约，而模拟 ERP 是一个独立的老系统：它的页面返回 HTML，它寥寥几个
内部接口直接返回裸 JSON。把它俩对齐反而是错的 —— RPA 要面对的就是
「外面那个系统不按我们的规矩出牌」。
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import PROJECT_ROOT, settings
from app.database import Base, dispose_engine, engine
from app.deps import RedirectToLogin
from app.routes import api, auth, dashboard, inventory, orders, review
from app.seed import seed_erp_data

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger("erp.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("启动 模拟ERP [env=%s] db=%s", settings.app_env, settings.mock_erp_db_url or settings.mock_erp_db)
    # 空库自己长出来。已有库不动 —— 表结构演进走 database/migrations/。
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_erp_data()
    yield
    await dispose_engine()
    logger.info("已释放 ERP 数据库连接池")


app = FastAPI(
    title="模拟 ERP",
    version="1.0.0",
    description="扮演现实中『没有 API、只能点界面』的老旧企业 ERP，给 RPA 一个真实的操作对象",
    lifespan=lifespan,
)

# ============================================================
# 会话 —— 服务端签名的 cookie session
# ============================================================
# max_age 即会话过期时间：超时后 cookie 失效，session 变空，任意页面被
# RedirectToLogin 踢回 /login（§9.4）。这正是 RPA 必须能识别并重登的场景。
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.mock_erp_session_secret,
    session_cookie="erp_session",
    max_age=settings.session_max_age_seconds,
    same_site="lax",
)


@app.middleware("http")
async def slow_pages(request: Request, call_next):
    """页面跳转延时（§9.1）。

    只延时**返回 HTML 的 GET**：静态资源和 /api/* 是页面内部的东西，跟着一起
    变慢只会让调试更难，而「老系统翻页慢」的真实感来自整页跳转。测试里把
    MOCK_ERP_PAGE_DELAY_MS 设为 0，这个分支直接不进。
    """
    response = await call_next(request)
    if (
        settings.page_delay_seconds > 0
        and request.method == "GET"
        and response.headers.get("content-type", "").startswith("text/html")
    ):
        await asyncio.sleep(settings.page_delay_seconds)
    return response


@app.exception_handler(RedirectToLogin)
async def handle_not_logged_in(request: Request, exc: RedirectToLogin) -> RedirectResponse:
    """未登录 / 会话过期 → 回登录页。用 303 而不是 307：
    无论原来是什么方法，跳到登录页都应该是 GET。
    """
    return RedirectResponse(url="/login", status_code=303)


app.mount(
    "/static",
    StaticFiles(directory=str(PROJECT_ROOT / "mock" / "erp" / "static")),
    name="static",
)

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(orders.router)
app.include_router(inventory.router)
# 审核页可以被关掉（MOCK_ERP_ENABLE_REVIEW_PAGE=false）——
# 状态机能走完 APPROVED 靠的就是这一页，但演示「RPA 提交完就结束」时
# 把它藏掉更能说明 RPA 的职责边界。
if settings.mock_erp_enable_review_page:
    app.include_router(review.router)
app.include_router(api.router)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """给启动脚本和 RPA 探活用。不含鉴权 —— 它只回答「进程活着吗」。"""
    return {"status": "ok", "app": "mock-erp"}
