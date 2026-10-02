"""僵尸任务回收 —— 扫描 + 回收的动作本体，见《数据库设计》§6.1。

这个文件只做「什么时候扫、扫的边界在哪」；**怎么收**在
`RpaService.reap_stale`（它得和 claim / 回传共用同一条 attempt 不变式）。
拆开是为了让「定时」和「业务」各自可测：这个模块唯一的输入是时间。

**为什么是独立进程而不是 FastAPI 的 lifespan**：回收是后台作业，不该和
「能不能处理 HTTP 请求」绑在一起。Web 进程多开几个实例时，每个副本都会跑
一份回收 —— 逻辑幂等、不会收重，但白白多扫几遍库。更重要的原因在 Phase 5：
AI Worker 也是同一种「周期性后台作业」，两者该共用一个进程模型
（见 `workers/main.py`），而不是一个挂 lifespan、一个独立跑。
"""

import logging
from datetime import datetime, timedelta

from app.core.config import settings
from app.database.mysql import AsyncSessionLocal
from app.database.redis import redis_client
from app.services.queue_service import QueueService
from app.services.rpa_service import ReapReport, RpaService

logger = logging.getLogger(__name__)


def stale_cutoff(now: datetime | None = None) -> datetime:
    """算出「早于此刻的心跳算失联」的那条分界线。

    独立成函数是为了能在测试里传一个固定的 `now` —— 否则测这个模块就得
    真的 sleep 5 分钟，或者去改全局配置。
    """
    return (now or datetime.now()) - timedelta(
        seconds=settings.zombie_timeout_seconds
    )


async def reap_once() -> ReapReport:
    """跑一轮回收。一次一个 session 事务，超时与失败都不往外抛由调用方统一兜。"""
    async with AsyncSessionLocal() as session:
        report = await RpaService(session, QueueService(redis_client)).reap_stale(
            stale_cutoff()
        )

    if report.scanned:
        # 命中数正常恒为 0，非零就是「有 Worker 挂了」，该被看见。
        logger.warning(
            "僵尸回收：扫描 %d，重新入队 %d，判失败 %d，任务 %s",
            report.scanned,
            report.requeued,
            report.failed,
            report.task_ids,
        )
    else:
        logger.debug("僵尸回收：无异常任务")
    return report
