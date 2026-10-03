"""API 路由聚合 —— 所有模块的 router 在这里挂到统一的 /api/v1 下。

每个 api 模块自己声明 `router`，这里只做拼装。好处是 main.py 里
永远只有一行 `include_router(api_router)`，新增模块不用动 main.py。
"""

from fastapi import APIRouter

from app.api import (
    ai_analyses,
    auth,
    dashboard,
    health,
    import_batches,
    notifications,
    orders,
    rpa,
    tasks,
)

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(orders.router)
api_router.include_router(import_batches.router)
api_router.include_router(ai_analyses.router)
api_router.include_router(tasks.router)
api_router.include_router(rpa.router)
api_router.include_router(dashboard.router)
api_router.include_router(notifications.router)
