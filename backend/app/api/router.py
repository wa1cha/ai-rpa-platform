"""API 路由聚合 —— 所有模块的 router 在这里挂到统一的 /api/v1 下。

每个 api 模块自己声明 `router`，这里只做拼装。好处是 main.py 里
永远只有一行 `include_router(api_router)`，新增模块不用动 main.py。
"""

from fastapi import APIRouter

from app.api import ai_analyses, auth, health, import_batches, orders, rpa, tasks

api_router = APIRouter()

# ---------- 已实现 ----------
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(orders.router)
api_router.include_router(import_batches.router)
api_router.include_router(ai_analyses.router)
api_router.include_router(tasks.router)
api_router.include_router(rpa.router)

# ---------- 待实现（按开发阶段逐步放开）----------
# 放开时把对应模块也加进上面的 import：
#   Phase 8 看板/通知    dashboard, notifications
