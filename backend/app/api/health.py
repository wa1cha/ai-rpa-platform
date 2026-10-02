"""健康检查 —— 见《API接口设计》§13.1。

供 Docker healthcheck 和部署后自检使用，**公开无鉴权**：
部署脚本拿到 401 是没法判断服务死活的。

一个刻意的选择：这个接口**永远返回 HTTP 200 + code 0**，
依赖是否正常放在 `data.status` 里。因为「健康检查接口本身能响应」和
「它依赖的下游是否正常」是两件事，混在一起会让调用方分不清
「服务挂了」和「服务活着但数据库连不上」。
"""

import asyncio

from fastapi import APIRouter, Depends
from redis.asyncio.client import Redis as RedisClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database.mysql import get_db
from app.database.redis import get_redis
from app.schemas.common import ApiResponse, HealthChecks, HealthData

router = APIRouter(tags=["系统"])

#: 单个依赖的探测超时。健康检查自己卡住比失败更糟 ——
#: 调用方会一直等，容器编排会以为进程还活着。
PROBE_TIMEOUT_SECONDS = 3

#: 未配置 AI 密钥时的标记。它与「配了但连不上」是两回事，见 _check_ai。
AI_UNCONFIGURED = "unconfigured"


def _describe(exc: BaseException) -> str:
    """把异常压成一行。

    保留原文而不是统一成 "error"：排查时「连接被拒绝」和「认证失败」
    是两件事，丢了原文就得再去翻日志。
    """
    text_ = f"{type(exc).__name__}: {exc}"
    return text_[:200]


async def _check_mysql(session: AsyncSession) -> str:
    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 —— 探活要吞掉一切异常并如实汇报
        return _describe(exc)
    return "ok"


async def _check_redis(client: RedisClient) -> str:
    try:
        await asyncio.wait_for(client.ping(), PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        return _describe(exc)
    return "ok"


def _check_ai() -> str:
    """AI 目前只检查「密钥是否配置」。

    不做真实的 HTTP 探测，理由是：健康检查会被容器按 10~30 秒的间隔反复调用，
    每次都打一次 LLM 接口既慢又费钱。真正的连通性探测在 Phase 5 由
    `services/ai_service.py` 提供（它有现成的客户端和重试策略），
    届时这里改成调用它，而不是在这里另起一套 HTTP 逻辑。
    """
    if not settings.ai_api_key.strip():
        return AI_UNCONFIGURED
    return "ok"


def _is_broken(value: str) -> bool:
    """`unconfigured` 不算故障。

    AI 在 Phase 5 才接入，一个还没填密钥的干净仓库不应该让整个平台
    显示成 degraded —— 那会让「真的挂了」这个信号失去意义。
    """
    return value not in ("ok", AI_UNCONFIGURED)


@router.get(
    "/health",
    response_model=ApiResponse[HealthData],
    summary="健康检查",
    description="探测 MySQL / Redis / AI 三项依赖，任一异常时 status=degraded。",
)
async def health_check(
    session: AsyncSession = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
) -> ApiResponse[HealthData]:
    # 三项并发探测：串行的话最坏情况要等 3 倍超时，健康检查的响应时间
    # 直接决定编排系统的判断速度。
    mysql_result, redis_result = await asyncio.gather(
        _check_mysql(session),
        _check_redis(redis),
    )
    ai_result = _check_ai()

    checks = HealthChecks(mysql=mysql_result, redis=redis_result, ai=ai_result)
    status = "degraded" if any(_is_broken(v) for v in checks.model_dump().values()) else "healthy"

    return ApiResponse.ok(
        HealthData(status=status, checks=checks, version=settings.app_version)
    )
